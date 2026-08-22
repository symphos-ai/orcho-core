# ADR 0180: Startup stall and interruption observability

## Status

Accepted.

## Decision

ADR 0179 bounds engine-owned service process trees. A single-project startup
window now runs from `run.start` to the first `phase.start`.
`startup_command.json` is atomically written with the canonical shape
`{armed_at,budget_s,baseline_events_size,baseline_output_size,command?}`.
`command` is `{identity,cwd,started_at,declared_timeout_s,effective_timeout_s}`;
it is bounded and never records environment variables or stdin.

No heartbeat framework or per-service events are introduced. Expiry without
event/output progress writes `status="halted"`, canonical
`halt_reason="startup_stalled"`, and
`halt={phase:"startup",cause,budget_s,elapsed_s,command}`, one `run.end`, and
a presentation-only progress line. Evidence reads that structured record.

SIGTERM and SIGBREAK where available persist `interrupted`, preserve an active
handoff, and emit one `run.interrupted`; atexit is only a fallback. Diagnosis
returns `stalled` before `active`, with non-resume `inspect_or_cancel`.

MCP/schema changes are intentionally absent. Windows hosted SIGBREAK/Job Object
evidence is deferred to configured CI; local POSIX checks do not prove it.
