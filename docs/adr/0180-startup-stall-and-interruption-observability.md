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

## Addendum 2026-09-04: setup heartbeat

Field runs showed a successful worktree bootstrap (`npm ci`, ~270 s) being
retro-halted as `startup_stalled` at the checkpoint that follows isolation
setup: bootstrap steps emit no event and write no `output.log`, so the
window's only progress signals never moved. The fix keeps the single owner.
`StartupWatchdog.mark_progress()` restarts the idle budget, re-snapshots the
baselines, and rewrites `startup_command.json` with a fresh `armed_at`, so
`armed_at` now means "start of the current idle window" and diagnosis reads it
unchanged. The module helper `heartbeat_startup_watchdog()` is called by the
bootstrap path per completed step and once after a successful bootstrap. The
watchdog stays armed across heartbeats; a recorded command timeout is not
cleared. This is still not a heartbeat framework: no events, no schema change,
and bootstrap `run` steps are not routed through the bounded service-command
observer.
