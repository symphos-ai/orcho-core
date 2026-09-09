# ADR 0190 — Live, observational progress for running verification gates

- **Status:** Accepted
- **Date:** 2026-09-06
- **Related:** [ADR 0020](0020-run-evidence-in-core.md),
  [ADR 0021](0021-public-sdk-boundary.md),
  [ADR 0095](0095-verification-gate-timeline.md),
  [ADR 0173](0173-verification-command-timeout.md),
  [ADR 0177](0177-concurrent-stderr-drain-and-stall-byte-accounting.md),
  [ADR 0179](0179-bounded-service-process-trees.md),
  [ADR 0183](0183-run-liveness-from-launch-and-progress.md),
  [ADR 0186](0186-gate-hooks-route-the-whole-failure-set.md)

## Context

An engine-owned scheduled gate runs the run's declared checks (test
suites, linters) as blocking subprocesses. A real suite can run for
minutes and print nothing an outside watcher can see: the executor
(`pipeline/verification_command.py`) captured the whole of stdout/stderr
with a single blocking `subprocess.run` and only recorded them into the
terminal receipt and the per-command `.log` **after** the process
settled. Between `gate.start` and `gate.end` there was no durable signal.

The consequences were operator-facing. A CLI run could sit on a
`▶ running…` line with no further output; `orcho-mcp`'s
`orcho_run_live_status` could only report "a gate started", with no way
to inspect output while a suite is running, and the interval
between phases (or before the final gate) read as "starting" indefinitely.
The only fix available to a watcher — re-tailing the `.log` — does not
exist yet (the log is written after the fact) and would couple every
consumer to an internal file format.

## Decision

Add a durable, **observational** progress stream for running gate
commands, produced by `orcho-core` and consumed through the public SDK.

1. **Event vocabulary.** A new durable event kind
   `gate.progress` (`EventKind.GATE_PROGRESS`) on
   `<run_dir>/events.jsonl`. Required payload: `name` (command
   identity), `invocation_id` (unique per execution, so a repair-loop
   rerun of the same command is a distinct stream), and `elapsed_s`.
   Optional: `hook`, `has_output`, `last_output_at` (ISO),
   `stdout_tail` / `stderr_tail` (each bounded ~2000 chars, separately
   labelled), and `stdout_bytes` / `stderr_bytes`. `phase` rides on the
   top-level `Event.phase` field. The existing `gate.start` / `gate.end`
   boundaries gain an **optional** `invocation_id` so a reader can pair
   the settled boundary with its progress stream; historical runs omit
   it and readers tolerate its absence.

2. **Streaming capture.** The executor's blocking `subprocess.run` is
   replaced by `core/io/bounded_proc.run_bounded`, extended with an
   optional `on_output(stream_label, chunk)` callback wired into the
   reader threads. The no-callback path is byte-for-byte identical to
   before, and the outcome mapping preserves ADR 0173 semantics:
   partial-output-on-timeout retention, stdout/stderr distinction, the
   real returncode on completion, and process-tree kill on
   timeout/cancellation. A single canonical owner,
   `pipeline/verification_progress.py`, defines the bounded
   `GateProgressRecord` and the `GateProgressAggregator` that decodes
   chunks with a per-stream incremental UTF-8 decoder (`errors='replace'`,
   so a multibyte sequence split across a chunk boundary never raises and
   never leaks a `b'..'` repr), maintains rolling bounded tails, and
   **coalesces** output updates at a minimum 0.5-second interval. Volume
   never bypasses this limit. A periodic publisher flushes pending output
   even when no further bytes arrive. Initial state, first output, and the
   final flush are bounded lifecycle exceptions. The publisher stops before
   gate.end. Events never re-append the full log.

3. **Best-effort publication.** Emission and the optional CLI presenter
   are wrapped so a storage/emit/presenter failure can never change the
   command's real pass/fail outcome. The terminal receipt
   (`verification_command_receipts/<command>.json` + executions copy) and
   the per-command `.log` stay authoritative and unchanged in shape.

4. **SDK reader.** `sdk/gate_progress.py::read_active_gate_progress`
   returns a frozen `GateProgressSnapshot` for the **latest** invocation,
   an artifact-only projection over `events.jsonl`. It returns `None`
   when the run has no progress events (historical runs stay readable),
   when the latest invocation has settled (a matching `gate.end` exists),
   or when the run is terminal (a `run.end` exists) — it never advertises
   a finished command as running.

The `gate.progress` `kind` string, its payload field names/types, and the
`GateProgressSnapshot` field names/types are the load-bearing wire
contract shared with `orcho-mcp`; changing any requires updating both
repos' schema snapshots together.

## Consequences

- A watcher (CLI terminal, `orcho-mcp` live-status / watch) sees a
  long-running gate produce output, with a bounded tail of its most
  recent output, without re-parsing any log.
- Progress is strictly observational. It reports facts — timestamps,
  byte counters, bounded tails — and never a fabricated percentage or a
  process-health verdict. It does not change the hard timeout, gate
  selection, retry, pause, or acceptance policies, and adds no soft/idle
  timers, restarts, or health verdicts. `gate.end` remains authoritative
  for the settled outcome.
- The wire contract is single-owned in
  `pipeline/verification_progress.py`, so producer and reader cannot
  drift.

## Addendum — 2026-09-09: the required-receipt auto-run is a visible gate too

Observed on the Orcho-on-Orcho dogfood (parent `20260908_131908_4064f0`):
seven silent minutes between `repair_changes` (skipped, review clean) and
`final_acceptance`. The pre-final required-receipt auto-run (ADR 0094) ran
`cli-sdk-unit` and `broad-non-e2e` through `sdk.verify.verify_run`, which
emitted neither the `gate.start` / `gate.end` boundary (ADR 0095) nor
`gate.progress`; `events.jsonl` was silent, `orcho_run_live_status` read
"starting" with `active_gate=null`. The scheduled after-phase gates already
had both — the auto-run was the one engine-owned gate execution without a
boundary.

Rule: every engine-owned gate execution is bracketed by the paired
boundary and publishes progress, whichever producer runs it.

- `pipeline/project/gate_events.py` is the single owner of the boundary
  payload (`emit_gate_start` / `emit_gate_end`); `gate_repair` delegates.
- `sdk.verify.verify_run` gains an internal `observer` seam
  (`CommandObserver`: `start(command) -> GateProgressContext | None`,
  `end(command, outcome | None)`), called around each executed command; the
  progress context returned by `start` is threaded into `run_command`. Absent
  observer (CLI / SDK default): unchanged.
- `verification_autorun.materialize_required_receipts` passes a
  `_GateBoundaryObserver(hook, phase, presenter)`: `start` emits `gate.start`
  with a fresh `invocation_id` and builds the progress context, `end` emits
  the paired `gate.end` (`failed` when the executor raised), and any boundary
  the executor left open is settled `failed` before the result is built. The
  run-level adapter labels the pre-final pass `hook="before_phase"` /
  `phase=<final phase>` (the correction pre-review pass likewise) and enables
  the terminal presenter only under TERMINAL presentation, as the scheduled
  gates do. The single batched `verify_run` call is kept.
