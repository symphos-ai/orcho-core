# ADR 0104 — Setup/preflight terminal-state projection and merge rule

Status: Accepted

## Context

A run can die terminally **before any pipeline phase runs**. Worktree bootstrap
fails, a preflight check aborts, or — most awkwardly — the launcher supervising
the run process reaps an abnormal exit (a signal death, an orphaned child) that
bypassed the pipeline's own in-process `run.end` writer entirely.

In every one of those shapes the durable record is honest but *thin*:

- `meta.json` may carry a terminal `status` / `halt_reason`
  (`status="halted"`, `halt_reason="worktree_bootstrap_failed"`), **or nothing
  at all** when a SIGKILL beat every in-process writer and only the launcher's
  reap-time state file recorded the death.
- There is no phase attempt under `meta.phases`, no `run.end` error event in
  `events.jsonl`, and no `meta.failure` record.

The existing SDK error/recovery projections key on exactly those richer
artifacts — provider-access failures (ADR 0101) and stalled-command failures
(ADR 0103) read `meta.failure`; the errors slice reads `run.end` error events.
A setup/preflight death produces none of them, so the run reads as a *silent
failure*: `status` says the run is over, the errors slice is empty, and nothing
points the operator at the failing setup command.

A second, related problem: **two surfaces disagreed about the status itself.**
The companion launcher integration already reconciles a missing / `running`
`meta.status` against its supervisor state file (`merged_status_from_meta`),
remapping a signal-induced `failed` to `interrupted`. The core SDK read
`meta.status` directly. For a run the launcher reaped after a signal, the two
could diverge (`failed` on one surface, `interrupted` on the other).

Two hazards had to be avoided:

- **New artifact format.** The fix must only *enrich projections* from files
  the run already writes (`meta.json`, `events.jsonl`, a runtime log, and the
  optional launcher state file). No new durable schema.
- **Provider coupling.** Reading the launcher's state file must stay a
  provider-neutral *file contract* — core must not import the launcher package,
  must not name a specific launcher, and must keep the merge rule expressible by
  any embedder that supervises run processes.

## Decision

Add `pipeline/run_state/setup_failure.py`: a pure, SDK-free, clock-free module
with two responsibilities — the **status/halt-reason merge rule** and the
**typed synthesis** of the missing setup/preflight error.

### Status / halt-reason merge rule (explicit and idempotent)

`merged_status(meta, run_dir)` resolves the status with an explicit rule that is
byte-for-byte idempotent with the companion launcher integration's
`merged_status_from_meta`:

1. `meta.status` is a non-empty `str` other than `running` → return it
   verbatim. **Terminal `meta` wins; the launcher state is not consulted.**
2. else `supervisor_terminal_status(run_dir)` if not `None`.
3. else `meta.status` if it is a non-empty `str` (the trivial `running` value,
   surfaced as a stable "still running" answer).
4. else `None` (unknown).

`supervisor_terminal_status` reads the optional launcher state file and returns
its terminal `status`, with **one remap that lives only in this launcher
branch**: a `failed` status carrying a negative `exit_code` becomes
`interrupted` (a negative exit code is a signal death, which the run lifecycle
names `interrupted`; a positive code stays `failed`). The remap is deliberately
*absent* from the `meta` branch of rule (1) — terminal `meta` is never
rewritten. `merged_halt_reason` follows the same precedence: `meta.halt_reason`
when non-empty, else the launcher's reap-time taxonomy (`signal:<NAME>` /
`abnormal_exit:<rc>` / `interrupted_orphan` / `orphaned_no_supervisor`).

Keeping one implementation of the rule on the core side means `load_status` and
the errors/halt slice resolve the **same** status/halt_reason for the same run
dir, and that value agrees with the launcher integration. The terminal-`meta`
+ launcher-`exit_code<0` case in particular resolves to a single status on every
surface — no interrupted-vs-failed divergence.

### Setup/preflight failure synthesis (gated)

`detect_setup_preflight_failure(meta, run_dir, events)` returns a typed record
(`kind="setup_failed"`, with `message` / `at` / `halt_reason` /
`runtime_log_hint`) **only** when *all* of:

- **No phase attempts** — `meta.phases` is empty/absent and there is no
  `release_summary`. The run never reached a pipeline phase, so a synthesized
  setup error cannot swallow a genuine in-phase failure.
- **No terminal cause already on record** — no `meta.failure` (provider-access /
  stalled-command already own the surface), no active `meta.phase_handoff` (an
  operator handoff is pending), and no `run.end` event *that the errors rollup
  itself surfaces as a breadcrumb*. The gate is kept aligned with
  `collector._build_errors`: a `run.end` only counts as an existing terminal
  cause when it carries an `error` (→ `run_failed`) or a `halted` status (→
  `run_halted`). A *bare* `run.end` with `status="failed"` / `"interrupted"` and
  no `error` produces **no** breadcrumb, so it must not suppress the synthesis —
  otherwise the errors slice would stay empty for exactly the silent
  setup/preflight death this projection exists to surface.
- **Merged terminal state says the run failed** — `merged_status` resolves to
  `failed` / `halted` / `interrupted`.
- **A concrete setup/preflight signal is present** — the death is attributable
  to a setup/preflight cause (a failed `meta.worktree_bootstrap` record, or a
  launcher that reaped an abnormal exit: an abnormal-exit `halt_reason` in the
  launcher state, or the launcher *driving* the terminal status because the
  pipeline never wrote one). A *clean, already-explained* operator/gate halt —
  `plan_rejected`, `phase_handoff_halt` — leaves a terminal `meta` with a
  `halt_reason` and empty `phases` too, but carries no such signal, so the
  synthesis defers and the benign halt is not buried under a synthetic error.

The record's `message` names the actionable cause — the merged `halt_reason`
(e.g. `worktree_bootstrap_failed`, `signal:<NAME>`, `abnormal_exit:<rc>`), the
`meta.worktree_bootstrap.error` string when present — and always points at the
runtime log (`runner.log`) for the failing setup command. `at` reuses the
already-persisted `halted_at` / `interrupted_at` stamp; no clock is read.

Because the synthesis defers whenever a richer terminal cause exists, the
existing provider-access (ADR 0101) and stalled-command (ADR 0103) projections,
the handoff projection, and the `run.end`-driven errors rollup stay
byte-identical — the synthetic error is purely additive for the previously
silent setup/preflight shape.

### Launcher state file as a provider-neutral contract

The optional launcher state file (`mcp_supervisor.json`) is read as plain JSON
via a tolerant loader (absent / unreadable / non-dict → `None`). Core does not
import the launcher package and does not assume one launcher; any embedder that
supervises run processes can write the same file shape (`status`, `exit_code`,
`halt_reason`) to feed the merge rule. This keeps the protocol in core and the
provider behavior in the launcher: core owns *how a terminal state is projected
from durable files*; the launcher owns *what it writes when it reaps a process*.

## Consequences

- A setup/preflight death is no longer silent: the errors slice carries a typed
  `setup_failed` record naming the cause and pointing at the runtime log.
- `load_status` and the errors/halt slice agree with each other and with the
  launcher integration on status and halt_reason, including the
  signal-reaped-after-`failed` case.
- The merge rule is owned once on the core side. The launcher integration
  (`merged_status_from_meta` / `merged_halt_reason_from_meta`) remains the
  authority on its own surface; this ADR fixes the rule so the two never drift.
  A companion follow-up delegates the launcher integration's status merge to
  this core rule and threads the new `setup_failed` error kind — tracked as the
  plan's MCP follow-up, not deferred silently.
- No new durable artifact format is introduced; every input is a file the run
  already writes.

This ADR builds on ADR 0101 (provider-access recovery projection) and ADR 0103
(stalled-command diagnostics); the synthesis gate is explicitly designed to not
fire when either of those terminal causes is already on record.
