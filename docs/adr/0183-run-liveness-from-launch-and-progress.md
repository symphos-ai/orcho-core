# ADR 0183 — Run liveness from launch and durable progress

## Status

Accepted.

## Context

A detached launcher records a PID and an offset-aware launch timestamp in
`run_supervisor.json`, while `events.jsonl` is the durable record of lifecycle
progress. A `meta.json` status can remain `running` after its launcher exits,
but process liveness alone is not sufficient evidence: a PID can be reused,
and an event may be in the normal interval between durable writes.

ADR 0180 already defines the watchdog-owned `startup_command.json` proof for a
startup timeout. It does not establish how an operator can safely distinguish
an old abandoned launcher from a recent or progressing run when that watchdog
artifact is absent or the supervising process no longer exists.

## Decision

`pipeline.run_state.liveness` owns one bounded, read-only observation of the
launcher and durable event stream. It reads the launch artifact once, accepts
only a positive integer PID and an offset-aware `started_at`, asks the platform
PID probe once, and classifies the latest durable event as absent, fresh,
stale, or unknown. A `run.end` or `run.interrupted` event is a terminal
boundary. The grace defaults to `AppConfig.startup_stall_seconds`.

The observation is on demand. It writes no artifact, starts no thread, and
does no polling. Read, time, or probe failures are unknown/no-op facts, never
evidence that a process is dead.

The existing `stalled` diagnosis remains the only public condition. Before
`active`, diagnosis may return that condition for a recorded dead PID only
when the launch and durable progress are stale (or progress is absent after an
old launch) and no terminal event exists. It may also report the same
condition when an old launcher has not advanced beyond the startup boundary
and has no `startup_command.json`. Both retain the existing
`inspect_or_cancel` action; the dead-PID reason directs an operator to
`orcho repair-state` as prose, not as a new machine action.

`validate_run_state` remains a pure durable-file validator. It neither reads
`run_supervisor.json` nor invokes the PID probe. After validation,
`repair_run_state` may make a separate liveness observation. Only a running,
old, non-terminal run with a proven-dead recorded PID and stale durable
progress receives the repair-local `running_without_live_process` issue. Its
safe repair writes the canonical interrupted shape
(`status="interrupted"`, `interrupted_at`,
`halt_reason="interrupted_orphan"`) and preserves any active handoff for the
operator. Existing halt repair, stale-handoff repair, and undecided-handoff
refusal keep their precedence.

An alive PID is not proof that it belongs to this run because PID reuse can
produce a false negative for abandonment. It therefore prevents automatic
interruption but does not prove health. No process observation alone changes a
run; stale durable progress and the absence of a terminal boundary are always
required.

## Consequences

- Launch/cancel continue to own their established `run_supervisor.json`
  contract; liveness is a tolerant reader, not a second launcher protocol.
- Generic CLI, JSON, and repair-audit consumers surface the new repair issue
  through their existing issue/change fields, with no formatter-specific path.
- `RunDiagnosis`, `RunStateRepairReport`, the SDK schema snapshot, MCP action
  vocabulary, phase sequence, gates, delivery flow, and agent-side stall path
  remain unchanged.
- A future machine-callable repair recommendation or new diagnosis field would
  be a public wire change and must land with the matching orcho-mcp work.
