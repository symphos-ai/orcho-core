# ADR 0060 — Stream-first session capture for resumable interrupted phases

- **Status:** Proposed
- **Date:** 2026-05-31
- **Deciders:** project owner
- **Relates to:** [ADR 0036](0036-agent-session-persistence-across-subprocess-restart.md)
  (extends the agent-session persistence mechanism),
  [ADR 0033](0033-worktree-foundation.md)
  (active-checkout `ContextVar` seam the runtime already reads in `_on_line`)

## Context

A phase that is interrupted **mid-invoke** loses its provider session, so on
`orcho_run_resume` it restarts the agent in a fresh (`stateless`) conversation
instead of resuming the work already in flight.

### Observed failure

Run `20260531_162421` resumed after planning. `plan` and `validate_plan`
resumed their parent sessions, but `implement` came back as
`session=stateless`. The checkpoint tells the story:

```
agent_sessions:
  plan_agent           ab62cdc1…  16:30:42
  validate_plan_agent  019e7e6f…  16:31:04
  implement_agent      — (absent)
```

`events.jsonl` shows `implement` was **not** skipped on the parent run — it
ran hard:

```
phase.start IMPLEMENT   seq 51   16:31:04
…140 × agent.tool_use, 45 × agent.text…
last IMPLEMENT event    seq 212  16:52:14
agent.end IMPLEMENT      count = 0
```

The implement agent worked for ~21 minutes and the run was killed **inside the
single `agent.invoke()` call** — there is no `agent.end`. Both places that
persist the session sit *after* the invoke returns, so neither fired.

### Why both persistence points missed it

1. **Runtime capture is post-process.** `agents/runtimes/claude.py` reads
   `session_id` only after the subprocess exits —
   `new_sid = _extract_session_id(stdout)` (`claude.py:616`), from the full
   accumulated `stdout`. The live `_on_line` callback (`claude.py:570`) parses
   every stream line for stdout rendering and the destructive-git guardrail but
   **does not capture `session_id`**, even though Claude's stream-json carries
   `"session_id": "<uuid>"` on the very first (`init`) line.

2. **Checkpoint write is post-invoke.** `_session_aware_invoke` persists via
   `_ckpt.set_agent_session(role_attr, sid)` (`pipeline/phases/builtin.py:1626`)
   only after `agent.invoke()` returns. The docstring on
   `checkpoint.set_agent_session` already aims for "at most one invocation
   stale" durability — but a phase is a *single long invoke*, so an
   interruption inside that invoke loses the whole session.

Net: the provider session id existed and was streaming live within
milliseconds of phase start, but nothing wrote it durably until the invoke
returned — which, for an interrupted phase, never happens.

This is correct-by-design for the *clean* path (post-invoke is the
authoritative final sync — it also handles session rotation and `None`-blanking
a burned session). It is simply blind to the in-flight window.

## Decision (proposed)

Capture and persist the provider `session_id` **on first sight from the live
stream**, not only after the invoke returns — so an interrupted phase leaves a
resumable session behind.

Fold this into the existing **runtime context-var seam** rather than a new
mechanism. The claude runtime already reads core-owned `ContextVar`s from
inside `_on_line` (`get_active_worktree_checkout`, ADR 0033;
`get_active_sandbox_policy`). Add one more of the same shape:

### 1. Core owns the sink (protocol)

A new context-var in core exposing a session-persist callback:

```python
# core side (path/context layer — sibling of the ADR 0033 active-checkout var)
set_session_persist_sink(cb: Callable[[str], None] | None) -> Token
get_session_persist_sink() -> Callable[[str], None] | None
```

`_session_aware_invoke` arms it around the invoke and resets after:

```python
_role_attr = _agent_to_role_attr(state, agent)
_sink = (lambda sid: _ckpt.set_agent_session(_role_attr, sid)) \
        if (_ckpt is not None and _role_attr is not None) else None
_tok = set_session_persist_sink(_sink)
try:
    raw = agent.invoke(wire_prompt, cwd, …)
finally:
    reset_session_persist_sink(_tok)
```

### 2. Runtime calls the sink (provider behavior)

The claude runtime's `_on_line` extracts `session_id` from the first line that
carries it and, on the `None → sid` transition only, sets `self.session_id`
and calls the sink:

```python
def _on_line(line: str) -> None:
    parse_claude_line(line, agent_label="invoke")
    if self.session_id is None:
        sid = _extract_session_id(line)        # per-line, not whole-stdout
        if sid:
            self.session_id = sid
            sink = get_session_persist_sink()
            if sink is not None:
                with contextlib.suppress(Exception):
                    sink(sid)                  # immediate WAL commit
    … existing guardrail check …
```

Idempotent and cheap: the guard fires once per invoke (first line with a sid),
and `set_agent_session` is `INSERT OR REPLACE` with an immediate commit, so a
mid-stream kill any time after that first line leaves a durable row.

### 3. Post-invoke persistence stays authoritative

`builtin.py:1626` is **not** removed. It remains the final sync that:

- captures a *rotated* session id (a clean invoke may end on a different sid),
- `None`-blanks a deliberately burned session (follow-up burn / reset),
- covers runtimes that never call the sink.

The stream-first write is strictly an *early-durability* addition layered under
it, never a replacement.

## Scope and boundaries

- **Core owns the protocol** — the context-var, the sink contract, and the
  post-invoke authoritative sync. **Plugins own provider parsing** — how to
  pull a session id out of their own stream. This honours the orcho-core
  contract ("core owns the protocol; plugins own provider behavior").
- **Public `invoke()` signature is unchanged.** A context-var (not a new
  parameter) keeps the `orcho.agent_runtimes` API stable for third-party
  runtimes and degrades gracefully: a runtime that never reads the sink keeps
  today's post-invoke-only behavior with no regression.
- **codex / gemini** adopt the same `_on_line` hook incrementally. Until they
  do they are unchanged — no behavior regression, just no early durability.
- **No wire-format / MCP change.** This is internal durability plumbing, not a
  runtime-schema, profile-shape, mode-flag, or gate-primitive change, so no
  `orcho-mcp` alignment is required (the MCP-validation rule does not trigger).

## Consequences

### Positive

- An interrupted-mid-phase invoke now leaves a resumable `session_id` in the
  checkpoint. On resume, the seed → `_followup_resume_pending` → `--resume`
  path (unchanged) re-attaches the phase to the *same* provider conversation.
- Combined with worktree reuse, the resumed agent sees what it already
  read/edited and continues rather than redoing ~20 minutes of work blind.

### Caveats / risks (validation items)

- **The phase still re-runs from scratch.** An interrupted phase is not in the
  completed set, so the handler re-executes. The value is provider-side context
  continuity + worktree reuse, not skipping the phase. (If we later want the
  agent to *resume its own turn* rather than start a new turn in the same
  session, that is a separate, larger change.)
- **Mid-turn provider state.** A session killed mid `tool_use` may be in an
  incomplete turn-state. `--resume` opens a fresh user-turn, which is generally
  safe for claude/codex but must be smoke-tested per runtime before this is
  marked Accepted.
- **Poisoned-row edge.** If the invoke is aborted right after the first line
  (e.g. `StreamAbort` from the destructive-git guardrail), a row is written for
  a session that did almost nothing. `--resume` into it is still valid; if the
  provider rejects an unknown/garbage sid, the runtime must fall back to a fresh
  session rather than hard-fail. Confirm the fallback path.

## Validation

- Mock E2E: start an `implement` phase under `--mock`, kill the run after the
  first stream line, resume, and assert `agent_sessions` has an `implement_agent`
  row and the resumed implement banner reads `--resume`, not `stateless`.
- Unit: `_on_line` sets `self.session_id` and calls the sink exactly once on the
  first sid-bearing line; subsequent lines are no-ops.
- Regression: clean (uninterrupted) runs still end with the post-invoke sync as
  the last writer; a deliberately burned session still `None`-blanks its row.
