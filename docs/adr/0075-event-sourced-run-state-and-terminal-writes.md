# ADR 0075 — Event-sourced run-state, repair, and terminal-write consolidation

- Status: Accepted
- Date: 2026-06-07
- Relates to: ADR 0035 (terminal-status and resume observability),
  ADR 0045 (RunStatus artefact map), ADR 0042 (project pipeline application
  boundary), ADR 0047 (cross-project application boundary)

> 2026-06-09 status note: later increments completed the Deferred items listed
> below. Bootstrap isolation halts now flow through `mark_run_halted`, the
> operator CLI exposes `orcho repair-state`, active handoff transitions moved to
> `pipeline/run_state/handoff.py`, and cross terminal settlement / safe repair
> live in `pipeline/run_state/terminal.py` and
> `pipeline/run_state/cross_repair.py`. The current living contract is
> [run_state_machine.md](../architecture/run_state_machine.md).

This is the first ADR to record the `pipeline/run_state` layer formally.
The layer was introduced in code across three increments — a read-only
event projection + consistency checker, an opt-in repair API, and the
terminal-write helpers this ADR adds. There is no earlier ADR for that
work to supersede; the increments are referred to below by their code-level
names (the package docstrings call them the run-state "brain").

## Context

A run's lifecycle facts — which phases ran, whether it paused on a phase
handoff, whether it halted, failed, or was interrupted — are emitted as a
durable event stream (`events.jsonl`) and also materialised into a flat
`meta.json` body that the pipeline mutates in place as it runs. The two can
drift: a process can be killed after a halt-decision artifact lands but
before `meta.status` flips, leaving a torn shape that no single writer owns.

Two structural problems motivated the layer:

1. **No client-neutral way to read or check run state.** Embedders and
   diagnostics had to re-parse `meta.json` and re-derive lifecycle facts.
   There was no reusable projection of the event stream and no
   non-repairing consistency check that could name a torn shape.

2. **Terminal state was written by hand at every call site.** The flip to
   `done` / `halted` / `failed` / `interrupted` was open-coded in finalize,
   the phase-handoff halt paths, the SDK halt transition, the failure path,
   the delivery-halt branches, and the atexit hook. Each site set
   `status` / `halt_reason` / `halted_at` / `interrupted_at` and decided
   independently whether to clear a stale `phase_handoff`. Divergence there
   is exactly what produces a torn run: a halt path that forgets to clear
   the active handoff, or a `done` that leaves one behind, contradicts what
   the repair layer would heal the run to.

ADR 0035 established the invariant that every non-`done` terminal carries a
non-null `halt_reason`. This ADR makes that invariant — and the
stale-handoff policy — enforceable from one place instead of trusting each
call site to re-implement it.

## Decision

Treat the event stream as the source of truth for a run's lifecycle and
`meta.json` as a materialised snapshot (a cache) of that truth. The
`pipeline/run_state` package owns three responsibilities, each isolated:

### 1. Projection + consistency (read-only)

A pure reducer folds the event stream into a typed snapshot, a projector
runs it over a run directory, and a consistency checker diagnoses known
torn shapes by stable problem codes. This layer is read-only: it never
writes, never repairs, and depends at most on the observability events
module. It is never imported by runtime / resume / finalization paths.

### 2. Repair (opt-in, off-line)

`repair_run_state` consumes the consistency diagnosis and, for a strictly
limited set of self-healable shapes, proposes (dry-run default) or applies a
minimal, crash-safe `meta.json` mutation that brings the materialised body
back in line with the event-derived projection. It heals a torn halt to the
exact post-halt shape the live halt writer produces, clears a stale active
`phase_handoff` on a settled terminal, and **refuses** to flip an
interrupted run that still carries an undecided handoff — that needs an
operator decision through the sanctioned handoff API. Repairs are
idempotent and change no durable schema beyond a repair-audit artifact.

### 3. Terminal-write helpers (the consolidation this ADR adds)

`pipeline/run_state/terminal.py` provides the only sanctioned way to write a
terminal status into a run's flat state mapping:

- `mark_run_done(state)` — `status='done'`; clears stale `phase_handoff`.
- `mark_run_halted(state, *, halt_reason, halted_at=None)` —
  `status='halted'`, `halt_reason`, optional `halted_at`; clears stale
  `phase_handoff`.
- `mark_run_failed(state, *, halt_reason)` — `status='failed'`,
  `halt_reason`; **preserves** `phase_handoff`.
- `mark_run_interrupted(state, *, interrupted_at, halt_reason='interrupted')`
  — `status='interrupted'`, `interrupted_at`, `halt_reason`; **preserves**
  `phase_handoff`.

The helpers are pure in-place mutations of an arbitrary mapping — the same
flat top-level shape whether it is the in-memory session dict or a
`meta.json` body loaded off disk. They do no file IO, emit no events, and
touch no checkpoint: persistence, the `run.end` event, and checkpoint status
remain the caller's responsibility, so the helpers cannot double-write or
reorder the `run.end` boundary. The package depends on nothing and never
imports runtime / resume / finalization paths.

**Stale-handoff policy (load-bearing).** `done` and `halted` are settled
terminals — any lingering active `phase_handoff` is stale and is cleared.
`failed` and `interrupted` preserve an active `phase_handoff`, because an
interrupted-or-failed run with an undecided handoff still needs an operator
decision, and the repair layer deliberately refuses to flip it. The
post-halt shape `mark_run_halted` writes for
`halt_reason='phase_handoff_halt'` matches byte-for-byte what the repair
layer heals a torn halt to, so the live halt writer and the off-line repair
are one source of the shape rather than two that can drift.

The terminal writers are wired into the minimal safe set of terminal
lifecycle sites for this stage: finalize status resolution (`done` /
`halted`), the phase-handoff halt paths (torn-halt heal and the in-process
halt sync), the SDK halt transition (mutating the loaded `meta` dict before
the SDK writes `meta.json`), the phase-failure path, the four delivery-halt
branches, and the atexit-interrupted hook. This is deliberately not yet
*every* terminal write — the bootstrap isolation-setup halts remain
open-coded and deferred (see below). Active phase-handoff writes (pause /
continue / retry_feedback / continue_with_waiver, which set `status='running'`
and manage the active payload) are **not** terminal and stay in the handoff
code — the helpers own terminal transitions only.

## Deferred

- **Bootstrap isolation-setup halts.** `pipeline/project/isolation_setup.py`
  still writes its terminal `halted` status + `halt_reason` directly in the
  pre-run-dirty intake, dirty-seed, and worktree-bootstrap failure paths
  (`pre_run_dirty_halt`, `pre_run_dirty_seed_failed`,
  `worktree_bootstrap_failed`). These halts fire before any agent phase and
  were intentionally left out of this stage to keep the change to a minimal,
  safe set; routing them through `mark_run_halted` is a follow-up. They are
  honest terminal writers, not a pattern to copy.

- **CLI `orcho run repair-state`.** The repair API has no first-class CLI
  verb yet. The single-project CLI uses a flat subcommand scheme (`run`,
  `cross`, `status`, …) with no nested subcommands under `run`, so attaching
  a `repair-state <run_id>` verb cleanly needs dedicated parser design.
  Until then, repair is reachable through the run-directory-generic
  `repair_run_state` API.

- **Cross-project terminal consolidation.** Cross-project finalization keeps
  its own status decision and `cross_<status>` reason taxonomy plus a
  per-alias metrics rollup the single-run helpers have no data for.
  Consolidating cross onto the shared terminal helpers is a separate future
  cross-parity phase; until then cross remains diagnostic through the same
  run-directory-generic `repair_run_state`, which is agnostic to single vs
  cross run directories.

## Consequences

- One enforced contract for terminal writes: the ADR 0035 non-null
  `halt_reason` invariant and the stale-handoff policy live in one module
  instead of being re-derived at each call site.
- The live halt writer and the off-line repair cannot drift on the
  post-halt shape — both produce the same bytes.
- The read-only projection / consistency layer and the opt-in repair API
  stay free of any runtime import, so diagnostics and repair can run against
  a run directory without pulling the pipeline in.
- New terminal lifecycle paths added in the future call the helpers rather
  than open-coding the flip, keeping the invariant intact by construction.
- Unit tests cover the helper contract directly; project lifecycle tests
  pin the observable contract at the real call sites (finalize, failure,
  atexit). See `tests/unit/pipeline/run_state/test_terminal.py` and
  `tests/unit/pipeline/project/test_finalize_done_order.py`.
