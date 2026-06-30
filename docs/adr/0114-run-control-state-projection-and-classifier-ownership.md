# ADR 0114 — RunControlState: single classifier ownership and SDK-exported lifecycle projection

- **Status:** Accepted (P0 + P1 delivered; see Implementation status)
- **Date:** 2026-06-27 (accepted 2026-06-30)
- **Deciders:** project owner
- **Pillar:** 2 of the control-state realignment (pillar 1 = ADR 0112 ParticipantSet; pillar 3 = ADR 0115 Finalization Reducer)
- **Relates to:** [ADR 0108](0108-verification-provenance-gate-consistency.md)
  (the "one effective classification, N consumers" precedent this generalizes),
  [ADR 0035](0035-terminal-status-and-resume-observability.md) (terminal/resume
  observability), [ADR 0047](0047-cross-project-application-boundary.md) (share
  substrate, not bodies), [ADR 0112](0112-multi-project-participant-set-and-scope-expansion-resetup.md)
- **Supersedes:** nothing (append-only)

## Context

When MCP began driving Orcho as an external control plane (typed actions, not
"CLI text a human interprets"), it exposed that the same run-lifecycle questions
— "terminal? resume-meaningful? correction-active? follow-up-needed?
superseded?" — are answered by several independent classifiers. A read-only
audit (2026-06-27) inventoried the actual sites; the picture is more precise than
"no ownership", and the precise version makes the fix smaller.

**Ownership already exists for resume/terminal.** `pipeline/control/resume_context.py`
is the authoritative home: the `is_terminal_*` family (`:581-672`),
`is_terminal_resume_parent` (`:663`), `get_resume_intent_options` (`:696`). SDK
`RunService.resume` (`sdk/run_control/service.py:330-378`) and `load_status`
(`sdk/status.py:110`) already pass through it. So this is **promote + export +
collapse parallels, not build a new read model.**

**The leaks are concrete:**

1. **Export gap forces value-replication.** `is_terminal_resume_parent` is
   pipeline-internal, not SDK-exported. So `orcho-mcp` copies it **by value** —
   `services/run_projection.py:481-509` (`_TERMINAL_HALT_REASONS` +
   `_is_terminal_resume_parent`), with comments that literally say "keep in
   lockstep with core; never import the pipeline-internal predicate". MCP further
   holds two large decision-table modules with **no core counterpart**:
   `project_run_diagnosis` (`run_projection.py:1402-1697`, a 7-branch
   resume/continuation classifier) and `project_recovery_lineage`
   (`run_lineage.py:383-569`). `resume_meaningful` is recomputed twice
   (`run_projection.py:1144-1189` and `observe/live_status.py:93-123`).
2. **Terminal-status vocabulary defined 3×.** `_TERMINAL_SUCCESS`
   (`resume_context.py:557`), `_TERMINAL_SUCCESS_STATUSES` (`sdk/actions.py:108`),
   `_RESUMABLE_TERMINAL_STATUSES` (`actions.py:101`) vs `_FAILURE_TERMINAL_STATUSES`
   (`run_state/setup_failure.py:71`) — independent frozensets of the same enum,
   plus the MCP value-copy.
3. **`commit_delivery.status → halt_reason` mapped 3×.** `pipeline/project/run.py:1331-1349`
   (live finalize), `sdk/run_control/delivery.py:_HALT_REASONS:57-63` (decide
   path), and `_settle_run:793-848`.
4. **"Decidable / torn handoff" classified 3×.** `resume_preflight._DECIDABLE_STATUSES`
   (`:44`), `sdk/phase_handoff._is_decidable_handoff_status` (`:355`),
   `run_state/repair.py:_plan_changes` (`:204-263`).
5. **Status → next-actions and handoff default-action tables held in MCP**
   (`observe/summary.py:124-147`, `observe/handoff_hints.py:43-161`), parallel to
   core `compute_next_actions`.

Because MCP must return a *typed* action, every divergence between "core says X"
and "MCP recomputes Y" is a visible bug, not a tolerable cosmetic difference.
That is why MCP exposed this and the CLI did not.

This is the same shape ADR 0108 already fixed for the verification gate (four
surfaces recomputing provenance → one overlay, N consumers). ADR 0114 applies
that proven move to run-control lifecycle.

## Decision (proposed)

**Invariant:** each run-lifecycle question has exactly **one** authoritative
classifier in `orcho-core`, exposed through the SDK; SDK/CLI/MCP/web are pure
consumers that map/render it. No surface re-derives a lifecycle decision.

1. **One shared terminal-status vocabulary.** Collapse the parallel frozensets
   (#2) into a single module. `pipeline/run_state/` may not import runtime/SDK, so
   the natural home is `pipeline/run_state/status_vocab.py`, imported by
   `resume_context`, `sdk/actions`, `setup_failure`, and the cross path.
2. **One `commit_delivery.status → halt_reason` map** (#3), living next to
   `CommitDeliveryStatus` in `pipeline/engine/commit_delivery.py`, consumed by
   both `run.py` (live) and `sdk/run_control/delivery.py` (decide). No second
   hand-rolled ladder.
3. **SDK-export the canonical lifecycle projection.** Export the resume/terminal
   predicates (`is_terminal_resume_parent`, the `is_terminal_*` family,
   `get_resume_intent_options`) and a **continuation/diagnosis classifier** that
   covers what MCP currently re-derives in `project_run_diagnosis` /
   `project_recovery_lineage` (terminality, halt class, resume-meaningful,
   delivery-gate, follow-up action, superseded/closed, continuation subject,
   next action). This is the `RunControlState` read-model: a typed projection,
   not new behavior.
4. **One decidable-handoff predicate** (#4): `_is_decidable_handoff_status` is the
   owner; `resume_preflight` and `run_state/repair` call it.
5. **Architecture guard.** A fitness test asserting that `orcho-mcp` (and the CLI)
   hold **no** core-owned lifecycle decision tables/constants outside a narrow,
   named mapper allowlist (wire-rename / shape-adaptation only). The guard makes
   any future re-derivation — and any half-migration — fail CI rather than hide.

Legitimately MCP-private logic stays: `services/status_merge.py` reconciles
`mcp_supervisor.json` (core SDK does not surface the supervisor file) — but its
meta-side rule must read core's `setup_failure.merged_status`, not duplicate it.

## Scope / non-goals

- **Not** a new orchestrator body and **not** the finalization reducer (that is
  ADR 0115). This pillar is the **read model** (how state is *classified and
  reported*); 0115 is the **write model** (how a verdict *settles* state).
- **Not** a from-scratch read model — it promotes and exports what
  `resume_context` already owns.
- **No** gate-status vocabulary change. SDK exports are additive; any MCP wire
  change ships with its `orcho-mcp` companion + mock smoke in the same change.

## Sequencing

1. **P0 (cheap, correctness, unblocks the guard):** status-vocab module (1),
   single halt_reason map (2), SDK-export the canonical predicates (3, predicates
   only), decidable-handoff dedup (4), and the architecture guard (5). After this,
   MCP/CLI can drop their value-replicas of the *predicates*.
2. **P1 (incremental migration):** export the fuller continuation/diagnosis
   classifier (3, classifier) and migrate MCP `project_run_diagnosis`,
   `project_recovery_lineage`, `live_status`, and the next-actions/default-action
   tables onto it — **one surface at a time**, the guard failing on any remaining
   local decision table. Finish each surface fully (no parallel paths — orcho-core
   "No Backcompat Ceremony").

## Cross-project (traced 2026-06-27)

Folding the cross path onto the shared read model is **bounded** — cross already
reuses core in most places (`cross_project/cli.py:490-500` calls
`get_resume_intent_options`/`should_prompt_for_resume_intent`). Two genuine fold
targets:

- `cross_project/cli.py:527-528` hand-rolls `is_terminal_success ∨
  is_terminal_phase_handoff_halt` (2 of the 7 predicates) instead of the aggregate
  `is_terminal_resume_parent` — a latent drift the moment cross gains a
  commit/delivery/FA terminal. Fold onto the aggregate.
- `cross_project/project_dispatch.py:253-259,429` re-derives per-alias child
  terminal/resumable state from a `sub_status` string vocab instead of calling the
  `is_terminal_*` family on the child `meta.json` it already reads — a value-replica
  of the child's own lifecycle. Fold onto the shared predicates.

Stays cross-specific (do NOT fold): the cross handoff topology
(`phase_handoff_kind ∈ {plan,project,cfa}`) and the `run_state/cross*.py`
invariants, which deliberately value-replicate because `run_state` must not import
`cross_project` (package layering). Cross also adds a 4th terminal frozenset
(`run_state/cross.py:47 _TERMINAL_CROSS_STATUSES`) — broader by necessity (`failed`
also clears handoff); the shared vocab module (§1) should host it as a named cross
variant rather than an anonymous copy.

## Guard tests

- The architecture guard (5) fails if MCP/CLI reintroduce a lifecycle constant or
  decision branch outside the mapper allowlist.
- A drift test: for a representative set of meta fixtures, the SDK projection and
  the MCP-reported state are **identical** (no field where MCP disagrees with core
  about terminality / resume-meaningful / next action).
- Migrating a surface to the exported classifier must delete its local copy in the
  same change (asserted by the guard), not leave both.

## Implementation status

Append-only progress log. **Closed 2026-06-30 — ADR Accepted; both sequencing
phases (P0 + P1) delivered.**

- **P0 — single classifier ownership: implemented** (`feat(run-state): ADR 0114
  P0 — single classifier ownership for run lifecycle`). Status-vocab module,
  single `halt_reason` map, SDK-exported canonical predicates, decidable-handoff
  dedup, and the architecture guard. MCP/CLI dropped their predicate replicas.
  Companion: `final_acceptance` verification gaps scoped to the declared
  readiness schedule.
- **P1 — incremental migration: implemented** (`feat(sdk): ADR 0114 P1 (core)`
  + `P1-core-v2 — public recovery-lineage read-model + RunDiagnosis.recovery`,
  and the matching orcho-mcp surface migration). The fuller
  continuation/diagnosis classifier is SDK-exported; MCP `run_diagnosis`,
  `recovery_lineage`, `live_status`, and the next-actions/default-action tables
  read it — no parallel local decision tables (the guard enforces this).

Pillar 2 of the control-state realignment is complete (pillar 1 = ADR 0112,
pillar 3 = ADR 0115).

## Consequences

- One source of truth per lifecycle question; MCP/CLI/web cannot disagree with
  core (the class of "core says resume meaningful, preflight says inert" bug
  disappears).
- MCP shrinks from a second brain to a thin adapter for the resume/diagnosis
  surface; the two big decision-table modules collapse onto the exported core
  classifier.
- The guard structurally prevents the half-migration trap (some surfaces on the
  contract, some still guessing) — the realignment cannot silently stall in a
  worse-than-either intermediate state.
- Foundation for ADR 0115: a clean, single read model is what the reducer's
  output is validated against.

## Related

- [ADR 0108](0108-verification-provenance-gate-consistency.md) — the precedent.
- [ADR 0035](0035-terminal-status-and-resume-observability.md) — terminal/resume
  observability surface this consolidates.
- [ADR 0115](0115-finalization-reducer.md) — the write-model counterpart.
- Audit sites (single-project, line-verified): `pipeline/control/resume_context.py`,
  `sdk/run_control/`, `sdk/actions.py`, `pipeline/run_state/setup_failure.py`,
  `orcho-mcp services/run_projection.py`, `services/run_lineage.py`,
  `observe/live_status.py`, `observe/summary.py`, `observe/handoff_hints.py`.
- Cross-project classifiers (`pipeline/cross_project/`) are a known untraced gap.
