# ADR 0103 — Stalled-command diagnostics and dual-source recovery

Status: Accepted

## Context

A run can stop making progress not because a provider is unreachable (ADR 0101)
but because a *command the agent launched* hangs. Two distinct shapes showed up
in live testing:

1. **Terminal hang.** A child command runs past the runtime idle-timeout with
   no output and no exit — a silent, stuck process. The run cannot continue;
   the only safe move is to stop the run's own child process group and let the
   operator resume or halt.
2. **Non-terminal risk.** An agent reaches for free-text process polling to
   wait on a backgrounded command — `pgrep -f "pytest -q -m"`,
   `kill -0 $(pgrep -f ...)`, or a `while kill -0 $(pgrep -f ...); do …; done`
   loop. Free-text argv matching is hazardous: it can match an *unrelated* host
   process (the dogfood hazard — a real `pytest -q -m` running elsewhere on the
   machine), so the agent may believe its command is alive when it is not, or
   act on a foreign process. This is a *risk flag*, not a fatal condition: the
   run should keep going.

Before this work the engine had no typed, durable way to describe either shape,
no live observability for the non-terminal risk while the phase was still
running, and no provider-neutral place for the carrier types to live without an
`agents → pipeline` back edge.

Two hazards had to be avoided:

- **Broad process matching as a mechanism.** Detecting or terminating work by
  scanning the host for processes whose argv matches a free-text pattern is
  exactly the hazard we are flagging; the engine must never adopt it itself.
- **Uncontrolled kills.** Killing must remain bounded to the run's own child
  process group and must have a single, well-defined trigger — never a
  side effect of a diagnostic.

## Decision

### Neutral carrier + provider-neutral sink

The carrier types live in `agents/stall_protocol.py`, which depends only on
`core.observability` — never on `pipeline`. This keeps the dependency direction
one-way (`pipeline → agents`) so `pipeline/run_state/*` and `sdk/*` can import
the carrier without a back edge.

- `StallReason` — bounded vocabulary: `unsafe_process_polling`,
  `silent_child_command`, `output_inactivity`.
- `StalledCommand` — a frozen, **bounded** record: `phase`, `elapsed_s`,
  `command_preview` (truncated), `output_tail` (truncated), `reason`,
  `process_group` (`int | None`). Truncation happens at construction so the
  carrier is always safe to persist.
- `AgentCommandStalledError` — the terminal escalation carrying a
  `StalledCommand`.
- `StallDiagnosticSink` — a narrow provider-neutral `Protocol` with
  `record(StalledCommand)`. The default `EventStallDiagnosticSink` emits a
  **non-terminal** `agent.command_stalled` event through core observability and
  never touches the run session, never raises, and never kills.

### Two sources, never conflated

A stall is observable through two distinct sources that must not be mixed:

1. **Terminal — `session["failure"]`.** On idle-timeout the agents layer raises
   `AgentCommandStalledError`. `pipeline/project/run.py` catches it next to
   `AgentAccessError`, writes a terminal `session["failure"]` record
   (`failure_kind="stalled_command"`) built by
   `pipeline/run_state/stalled_command.py`, marks the run failed
   (`mark_run_stalled`, preserving an active handoff like `mark_run_failed`),
   and emits `agent.command_stalled(terminal=True)` followed by `run.end`.
2. **Live non-terminal — event-backed projection.** On a non-terminal
   detection the stream monitor calls the sink, which emits
   `agent.command_stalled(terminal=False)` **during the stream event**, before
   the phase finishes. `sdk.evidence_slices.active_stall_diagnostics` reads
   those events from the run event-store so the diagnostic is observable while
   the phase is still *running*. This write-through emission — not any
   after-phase bookkeeping — is the source of non-terminal observability. An
   optional finalization snapshot may mirror it but is explicitly flagged as
   NOT the live source.

`sdk.actions.compute_next_actions` reads **both** sources and projects typed
recovery actions without breaking the existing terminal logic.

The terminal `agent.command_stalled(terminal=True)` event has a **single
authoritative emit-site**: `pipeline/project/run.py`, alongside
`session["failure"]` and `run.end`. The stream layer builds the carrier, performs
the scoped kill, and raises — it deliberately does **not** emit the terminal
event itself, so a single idle-timeout yields exactly one terminal record in the
evidence bundle. The non-terminal write-through stays in the stream (via the
sink) because no pipeline handler catches it.

The default `EventStallDiagnosticSink` is wired as a **production default**, not
an opt-in: the Claude, Codex, and Gemini runtimes pass it (plus the active phase
label from `core.observability.events.current_phase()`) on every `_stream_run`
call. A bare `_stream_run` with no `stall_sink` keeps the historical
return-on-idle behaviour for non-runtime callers and tests.

### Reason categories and recovery contract

Recovery is a bounded, consistent verb set shared by every consumer (event
payload, live projection, terminal failure, evidence record):

```
recovery_actions = [interrupt, resume_from_checkpoint, halt]
```

- `interrupt` — stop the run's **own** child process group; actionable while
  the run is live.
- `resume_from_checkpoint` — resume a terminal run from its checkpoint.
- `halt` — durable, meta-only option (never projected as an executable action).

The SDK projects the subset that is actionable for the run's current state
(interrupt while running, resume after a terminal stall); `halt` stays
meta-only, mirroring the ADR 0101 provider-access pattern.

### Unsafe process polling is a non-terminal diagnostic

The guard in `agents/command_guard.py` detects free-text process polling purely
from the command **text** (`pgrep -f` / `pkill -f`, including the
`kill -0 $(pgrep -f …)` and `while`-loop forms). It never scans, signals, or
kills any process. It emits `agent.guardrail` with
`guardrail="unsafe_process_polling"`, `action="warn"` — a diagnostic only. A
warn never aborts the call, never fails the run, and never kills the
subprocess. Polling the run's own child by PID (`kill -0 <pid>`) is safe and is
deliberately not flagged.

### Single stop condition for killing

The stream layer's scoped kill of the run's own child process group has exactly
one trigger: the existing **idle-timeout**. Operator-interrupt and
recovery-action are separate controlled paths, also bounded to the run's own
child group. `unsafe_process_polling` by itself never kills and never makes the
run terminal-failed.

### No broad pgrep as a mechanism

The engine never uses `pgrep -f` (or any broad, by-name process matching) as a
detection or termination mechanism. Detection of unsafe polling is text-only;
killing is keyed on the run's own child process id / group. The only `pgrep`
literals in the codebase are the guard's detection patterns and test fixtures —
never an executed mechanism.

### Durable evidence contract

`pipeline/evidence/collector.py` emits one `command_stalled` error record per
`agent.command_stalled` event, covering both paths (discriminated by the
`terminal` flag) with `phase`, `reason`, `elapsed_s`, `recovery_actions`, and
the bounded preview/tail/process_group. `pipeline/evidence/schema.py` lists
`command_stalled` in `KNOWN_ERROR_KINDS` and validates the record shape
additively — other error kinds are untouched, so this is not a
`schema_version` bump.

## Consequences

- Operators get a typed, bounded description of *why* a run stalled and *what
  to do next*, observable live for the non-terminal case and durably for the
  terminal case.
- The carrier/sink split keeps `orcho-core`'s layering intact and lets any
  embedder register an alternative sink.
- Killing stays bounded and single-triggered; a risk flag can never escalate to
  a kill on its own.
- Evidence and SDK projections agree on one recovery verb set, so a post-mortem
  bundle and a live status surface describe the same options.

## Status surface (MCP)

The change is MCP pass-through: `next_actions`/`Action` wire shape is unchanged
(new entries reuse existing tools), no `RunStatus` field was added, the new
event kind is relayed generically, and the `command_stalled` evidence record is
an additive error-kind under the existing v1 bundle schema. See ADR 0101 for
the recovery-projection pattern this builds on.
