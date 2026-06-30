# Runtime Context Telemetry Probe — Claude CLI vs Codex CLI

**Date:** 2026-05-18
**Author:** runtime probe before M14.5 / M14.6
**Status:** research artifact (no Orcho code change)

## Goal

Determine what `claude` (Claude Code CLI) and `codex` (Codex CLI) expose
**machine-readably** about context fullness, compaction events, cache
state, and token usage. The probe answers a single question: should
M14.5 (memory) and M14.6 (lab cases) build around **real runtime
signals**, or around **Orcho-synthesised estimates**?

Mapping rubric is the M14.4 ADR 0029 source hierarchy:

```text
runtime_reported  -> live used / remaining / window indicator
provider_usage    -> per-call usage buckets + known model window
orcho_estimated   -> Orcho prompt-side estimate
config_static     -> static-config fallback
unknown           -> no usable source
```

The probe **does not** modify Orcho runtime behaviour, **does not**
implement M14.5 / M14.6, and **does not** wire any new evidence
surface. It only inspects the CLI surface area.

## Method

CLI versions used: `claude 2.1.142 (Claude Code)` and `codex-cli 0.125.0`.

Three probe shapes, run on each CLI:

1. **Minimal call** — `"Say hello in 3 words."`, structured-output
   format, no tools, no session persistence. Reveals the baseline
   per-turn / per-result event schema.
2. **Larger prompt** — same task but with a ~3 KB synthetic `LOREM`
   payload appended. Reveals whether telemetry shape changes when the
   prompt grows (does anything fullness-adjacent appear).
3. **Schema observation** — every field name surfaced across both
   calls is recorded, including those that are `null` today (these are
   the candidate slots for future runtime signals).

Resume / session continuation was inspected against `~/.codex/history.jsonl`
and the per-call `session_id` shape, but no synthetic multi-turn run
was needed — the per-call schema already pins what is or isn't exposed.

## Findings

### Claude Code CLI

Per-message envelope (`assistant.message`):

| Field | Value in probe | Probe class |
|---|---|---|
| `usage.input_tokens` | `6` | `provider_usage` |
| `usage.output_tokens` | `4` | `provider_usage` |
| `usage.cache_creation_input_tokens` | `11677` | `provider_usage` |
| `usage.cache_read_input_tokens` | `0` | `provider_usage` |
| `usage.cache_creation.ephemeral_5m_input_tokens` | `0` | `provider_usage` (cache TTL split) |
| `usage.cache_creation.ephemeral_1h_input_tokens` | `11677` | `provider_usage` (cache TTL split) |
| `usage.service_tier` | `"standard"` | meta |
| `usage.inference_geo` | `"not_available"` | meta |
| **`context_management`** | **`null`** | **reserved slot** — see below |
| `diagnostics` | `null` | reserved slot |

Per-result envelope (`result`):

| Field | Value in probe | Probe class |
|---|---|---|
| `duration_ms` | `2422` | meta |
| `duration_api_ms` | `2909` | meta |
| `ttft_ms` | `1459` | meta |
| `num_turns` | `1` | meta |
| `total_cost_usd` | `0.07382...` | `provider_usage` |
| `usage.iterations[]` | per-turn breakdown | `provider_usage` |
| **`modelUsage.<model>.contextWindow`** | **`1000000`** for `opus-4-7[1m]`, **`200000`** for `haiku-4-5` | **`runtime_reported`** |
| `modelUsage.<model>.maxOutputTokens` | `64000` / `32000` | `runtime_reported` (output-side cap) |
| `modelUsage.<model>.inputTokens` / `outputTokens` / cache counters / `costUSD` / `webSearchRequests` | per-model rollup | `provider_usage` |
| `terminal_reason` | `"completed"` | reserved slot — see below |
| `stop_reason` | `"end_turn"` | meta |
| `fast_mode_state` | `"off"` | meta |
| `permission_denials[]` | `[]` | meta |

Sibling event streams from the same call:

- `rate_limit_event.rate_limit_info` → `{status, resetsAt, rateLimitType: "five_hour", overageStatus, overageDisabledReason, isUsingOverage}`. **Quota signal**, not a context-fullness signal, but worth surfacing.
- `system.subtype="post_turn_summary"` → `{status_category, status_detail, needs_action}`. The runtime emits its **own** semantic summary of the turn (e.g. `status_detail: "greeted user with 3 words"`). Currently single-line free text.
- `system.subtype="init"` → full session inventory (`model`, `tools`, `mcp_servers`, `slash_commands` — including `clear`, `compact`, `context`, `usage` user-facing commands — `agents`, `skills`, `plugins`, `memory_paths.auto`). Confirms the runtime has **internal awareness** of all four context-lifecycle operations (clear, compact, context-view, usage-view) as first-class CLI verbs.

#### Reserved-slot semantics

- **`assistant.message.context_management`** is `null` in both probes (small prompts, no compaction). The field is part of the per-message schema, so when the runtime actually performs a context-management action mid-call, this is where it surfaces. Shape unknown until a real event fires; the field's existence is the contract handle.
- **`result.terminal_reason`** is `"completed"` in the probe. The naming suggests `"compacted"` / `"window_full"` / `"rate_limited"` could surface here. Unconfirmed.

### Codex CLI

Per-event stream (`codex exec --json`):

| Event | Fields |
|---|---|
| `thread.started` | `thread_id` |
| `turn.started` | — (empty) |
| `item.completed` | `item: {id, type: "agent_message", text}` |
| `turn.completed.usage` | `input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_output_tokens` |

That is the **entire** machine-readable surface. No `context_window`,
no `context_management`, no compaction marker, no fullness ratio, no
rate-limit event, no per-call cost. The `~/.codex/history.jsonl`
file is `{session_id, ts, text}` only — no telemetry.

Larger-prompt probe: `input_tokens` grew `17756 → 19257`,
`cached_input_tokens` stayed at `3456`. **Schema did not change** —
no fullness field appeared when the prompt grew.

### Mapping the M14.4 source hierarchy

| Source | Claude CLI today | Codex CLI today |
|---|---|---|
| `runtime_reported` | **available** — `modelUsage[model].contextWindow` is the live model-window value; cumulative `usage.input_tokens + cache_creation + cache_read` is the used-side; remaining derives | **not available** — no window field |
| `provider_usage` | available — `usage.*` per-turn buckets | available — `turn.completed.usage` |
| `orcho_estimated` | Orcho's `last_estimated_tokens_in` (already wired) | Orcho's `last_estimated_tokens_in` (already wired) |
| `config_static` | available (we know Claude windows) | available |
| `unknown` | terminal fallback | terminal fallback |

### Compaction event today

Neither CLI emits a machine-readable "I just auto-compacted" event in
the probed runs. Claude has the **reserved slot**
(`context_management` field on `assistant.message`) that almost
certainly fills when compaction fires — but the probe did not push
hard enough to trigger one. Codex has no such slot at all.

### Cache state today

Claude exposes detailed cache-creation / cache-read counters split by
TTL bucket (`ephemeral_5m` / `ephemeral_1h`). Codex exposes a single
`cached_input_tokens` field. Both are already wired into the Orcho
adapters at the per-call level; neither exposes cache-invalidation
events.

### Session-resume state

Both CLIs expose a stable `session_id` per call. Claude has rich
session-resume primitives (`-c/--continue`, `-r/--resume`,
`--session-id <uuid>`, `--fork-session`, `--from-pr`). Codex has
`codex exec resume`. Neither surfaces a *delta* between fresh-call and
resumed-call telemetry — the per-call schema is identical; the only
trace of continuity is the cache-read counter growing.

## Recommendation

Historical note (later on 2026-05-18): M14.4.1 implemented the Claude adapter
capture described below. The recommendation remains useful as source evidence
for why Claude can activate `runtime_reported` and Codex cannot, but the
adapter-side capture is no longer missing.

**Build M14.5 / M14.6 around two facts:**

1. **Claude's `runtime_reported` branch is live today.** Specifically:
   - `modelUsage[model].contextWindow` → durable `context_window_tokens`
   - cumulative `usage.input_tokens + cache_creation_input_tokens + cache_read_input_tokens` across the session → `context_used_tokens`
   - `context_window_tokens - context_used_tokens` → `context_remaining_tokens`

   This means the M14.4 `resolve_context_pressure` resolver can wake up
   the `runtime_reported` branch **without a contract change**, the
   moment the Claude adapter starts stamping
   `agent.last_context_window_tokens` /
   `agent.last_context_used_tokens` from `modelUsage.contextWindow` and
   the cumulative usage running sum. The resolver branch is already
   written; only the adapter-side capture is missing.

2. **Codex stays at `provider_usage`.** The Codex `runtime_reported`
   branch does not exist today and would have to be modelled by Orcho
   from `cached_input_tokens` rate-of-change + a static config window.
   Anything Orcho computes here is honestly `orcho_estimated`, not
   `runtime_reported`. The branch labels in M14.4 already capture this
   asymmetry — do not paper over it by inventing a fake
   `runtime_reported` for Codex.

3. **No CLI exposes a machine-readable auto-compact event today.**
   Claude has the `context_management` reserved slot that would surface
   one; Codex has nothing. M14.5 (memory) and M14.6 (lab) must not be
   built around the assumption that "the runtime tells us when it
   compacted". The compaction contract in M14.4
   (`coding_agent_compaction`) stays the right shape — it defines
   *what* must survive, not *when* compaction fires.

### Concrete next-step proposals (priority order)

| # | Slice | Cost | Payoff |
|---|---|---|---|
| 1 | **Adapter capture for Claude `runtime_reported`** — extend `agents/runtimes/claude.py::_capture_usage` to also stash `last_context_window_tokens` (from `result.modelUsage[chosen_model].contextWindow`) and a running `last_context_used_tokens` sum. The `resolve_context_pressure` resolver already handles the values. | small | the `runtime_reported` branch goes live in goldens and traces; M14.4's source label flips from `orcho_estimated` to `runtime_reported` on Claude runs |
| 2 | **`rate_limit_event` capture** — Claude emits `five_hour` / overage events Orcho currently ignores. Worth recording as a sibling evidence surface (not context-fullness, but adjacent). | small | observability win, no architectural change |
| 3 | **`post_turn_summary` capture** — Claude's own per-turn summary (`status_category` / `status_detail`) is a free signal Orcho currently discards. Could feed M14.5 memory directly when it lands. | small | M14.5 input candidate |
| 4 | **M14.6 lab cases with `runtime_reported` ground truth** — once #1 lands, lab cases can assert against real `contextWindow` / `used_tokens` instead of estimates. The "compaction preserved the subtle decision" case becomes meaningful: trigger a long enough run to actually push Claude into auto-compact, then check the `context_management` reserved slot fired and decisions survived. | medium | first M14 slice that exercises real runtime context behaviour |
| 5 | **M14.5 memory as artifact-backed surface** — orthogonal to runtime telemetry; it's the recovery side. Wait until #4 produces real evidence of what gets lost across auto-compact, then size memory to plug the demonstrated gap. | medium | avoids over-building memory around imagined losses |

**Defer:**

- Codex `runtime_reported` — wait for OpenAI to add `context_window`-aware events.
- Provider-specific compaction-event normalisation — wait for `context_management` to surface a non-null shape so the normalizer has a target.
- Any policy / profile knob — premature until lab data exists.

## Raw probe outputs (key excerpts)

### Claude minimal call (result envelope)

```json
{"type":"result","subtype":"success","is_error":false,"duration_ms":2422,"duration_api_ms":2909,"ttft_ms":1459,"num_turns":1,"result":"Hello there, friend!","stop_reason":"end_turn","total_cost_usd":0.0738,"usage":{"input_tokens":6,"cache_creation_input_tokens":11677,"cache_read_input_tokens":0,"output_tokens":12,"service_tier":"standard","cache_creation":{"ephemeral_1h_input_tokens":11677,"ephemeral_5m_input_tokens":0},"iterations":[{"input_tokens":6,"output_tokens":12,"cache_read_input_tokens":0,"cache_creation_input_tokens":11677,"type":"message"}]},"modelUsage":{"claude-haiku-4-5-20251001":{"inputTokens":450,"outputTokens":12,"cacheReadInputTokens":0,"cacheCreationInputTokens":0,"costUSD":0.00051,"contextWindow":200000,"maxOutputTokens":32000},"claude-opus-4-7[1m]":{"inputTokens":6,"outputTokens":12,"cacheReadInputTokens":0,"cacheCreationInputTokens":11677,"costUSD":0.0733,"contextWindow":1000000,"maxOutputTokens":64000}},"terminal_reason":"completed"}
```

### Claude assistant message (the `context_management` slot)

```json
{"type":"assistant","message":{"model":"claude-opus-4-7","usage":{"input_tokens":6,"cache_creation_input_tokens":11677,"cache_read_input_tokens":0,"output_tokens":4,"service_tier":"standard","inference_geo":"not_available"},"diagnostics":null,"context_management":null}}
```

### Codex minimal call (entire stream)

```json
{"type":"thread.started","thread_id":"019e37fd-e04d-7171-964e-6e1f3dbb705f"}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"Hello from Codex"}}
{"type":"turn.completed","usage":{"input_tokens":17756,"cached_input_tokens":3456,"output_tokens":5,"reasoning_output_tokens":0}}
```

### Codex larger-prompt call (schema unchanged)

```json
{"type":"turn.completed","usage":{"input_tokens":19257,"cached_input_tokens":3456,"output_tokens":2,"reasoning_output_tokens":0}}
```

## Bottom line

The M14.1–M14.4 observe-only foundation is correctly shaped. The
M14.4 source hierarchy maps cleanly onto what Claude actually exposes
today (one provider crosses into `runtime_reported`, one does not).
The next high-leverage M14 work is **not** another observe-only
slice; it is **wiring the Claude adapter to feed the
`runtime_reported` branch that M14.4 already implements**, then using
that ground truth to drive M14.6 lab cases that can finally exercise
real runtime context behaviour. M14.5 memory should follow what the
lab demonstrates is actually lost, not what we imagine might be.
