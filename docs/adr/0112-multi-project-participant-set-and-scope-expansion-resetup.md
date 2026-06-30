# ADR 0112 — Multi-project participant set and scope-expansion re-setup (completes ADR 0051)

- **Status:** Accepted (all increments P0/B/C/D delivered; see Implementation status)
- **Date:** 2026-06-27 (accepted 2026-06-30)
- **Deciders:** project owner
- **Completes:** [ADR 0051](0051-shared-runtime-path-context-layer.md) (cross ↔ mono DRY shared layer, Proposed)
- **Governed by:** [ADR 0047](0047-cross-project-application-boundary.md) (share substrate, not bodies)
- **Builds on:** [ADR 0108](0108-verification-provenance-gate-consistency.md),
  [ADR 0110](0110-scope-expansion-notice.md),
  [ADR 0107](0107-companion-repo-delivery-disclosure.md),
  [ADR 0084](0084-verification-contract-cross-repo-receipt-graph.md),
  [ADR 0087](0087-allowed-modifications.md),
  [ADR 0038](0038-cross-plan-phase-handoff-parity.md)
- **Supersedes:** nothing (append-only)

## Context

A run that edits more than one repository today is served by two divergent code
paths. The cross pipeline (`pipeline/cross_project/`) carries an explicit,
declared project set; the mono pipeline (`pipeline/project/`) carries one
project and discovers a second repository only by accident, when an honest
implementation reaches into a neighbouring repo mid-run (a companion `orcho-mcp`
edit that follows an SDK change, a regenerated sibling fixture).

ADR 0051 already named the root cause: cross holds **parallel reimplementations**
of machinery mono owns, and every divergence has produced a bug at a place where
one path resolved a checkout the other path resolved correctly. Its own Context
records the canonical example — cross gates once "reviewed the SOURCE checkout,
not the per-alias worktree where the change lives → false REJECTs (fixed in
`c732010`)."

That exact disease re-appeared in the mono path. In a mono run whose plan grew a
companion `orcho-mcp` edit, the engine changed `orcho-core` inside an **isolated
per-run worktree** while the companion repo was edited in its **canonical
checkout**. The developer-side core verification then ran with
`cwd = <canonical orcho-core>` — a clean tree with none of the run's undelivered
diff — and reported green. The provenance machinery that should have caught this
(ADR 0108) did not fire: with no participant declaration to aim at, the probe
fell into branch (c) ("neither a local `pipeline/` nor declared assertions →
record a non-failing informational check") and passed vacuously. The
scope-expansion classifier (ADR 0110) *did* notice the out-of-plan companion
files — but only at `final_acceptance`, long after the verification it would have
re-pointed had already run against the wrong tree.

Two structural facts fall out:

1. **`cross` vs `mono` is only how the participant set is seeded, not two
   machineries.** Both modes can gain a participant mid-run — mono organically,
   cross when the agent finds the declared cross-plan under-declared a project.
   The capability to *expand* the set is universal; only the *initial seed*
   differs.
2. **Source resolution, verification provenance, and delivery already know how to
   operate over a set of repos** (ADR 0108 `{dependency:X}`, ADR 0107/0084/0087
   companion disclosure and cross-repo receipts) — but nothing **promotes a
   newly-discovered repo into that set at the moment of discovery**, with an
   isolated checkout and a wired resolver. ADR 0110 classifies after the fact; it
   does not re-enter setup.

## Decision (proposed)

### 1. One participant set as the shared substrate

Introduce a single run-scoped **participant set** — the shared-substrate
realization ADR 0051 calls for, owned by `orcho-core` as protocol (per ADR 0047:
substrate, not a merged orchestrator body). Each participant is a small typed
value object:

```
Participant {
  alias            # stable handle used by aliasized plan paths / disclosure
  repo             # repository identity
  editable_checkout  # the isolated per-run worktree — the ONLY edit/verify root
  base_ref         # the start head the diff is measured against
  delivery_target  # where the delivered diff lands (canonical checkout)
}
```

The participant set is the **single source of truth** consumed by all three
lifecycle stages:

- **Setup** — isolation/worktree creation and the source resolver.
- **Execution** — agent cwd binding; every edit/import resolves through the set.
- **Control** — verification provenance, scope-expansion classification, and
  delivery completeness iterate the *same* set.

`cross` seeds the set from the cross-plan; `mono` seeds it with one participant.
Both then share the identical expansion, resolution, verification, and delivery
substrate. The mono and cross app modules (`pipeline/project/app.py`,
`pipeline/cross_project/app.py`) remain thin, separate orchestrations over this
substrate; this ADR introduces **no** merged orchestrator body and does not move
either app's lifecycle work.

### 2. Symmetric isolation

Every participant gets an isolated worktree. No participant edits its canonical
`delivery_target` tree mid-run. The false-green above was born of asymmetry —
core isolated, companion edited in canonical — and symmetric isolation removes
the asymmetry by construction. (Isolation-off degraded mode, where
`worktree.path == source`, follows the existing in-place contract; see ADR 0051's
folded `target_dirty` edge.)

### 3. Source resolution reads the participant set, fail-closed

The `{dependency:X}` resolution ADR 0108 branch (b) relies on resolving a
participant dependency to **its `editable_checkout`** — never the
canonical sibling. The ambient/sibling fallback is legal **only** when the
dependency is not a participant (pure local development). For a run that owns an
isolated worktree for a repo, an unresolved or sibling-pointing source for that
repo is a hard error, not a silent fallback. `ORCHO_*_SRC`-style env values
become a **projection** of the resolver, inherited by every phase/subtask/verify
step, not an independently re-derived path.

This makes the provenance gate (ADR 0108) effective instead of vacuous: a
participant's verification env declares the `{dependency:X}` assertion bound to
that participant's worktree, so branch (b) proves real provenance and a
wrong-tree verification downgrades to `FAIL` end-to-end per ADR 0108's terminal
invariant — rather than slipping through branch (c).

### 4. Scope-expansion-driven re-setup (the new node)

Detection of an out-of-set repo is lifted from a `final_acceptance` post-mortem
to a **discovery-time** transition, available in any phase, any mode. The
detector generalizes the existing strict-mono sibling check
(`pipeline/control/resume_context.py`) and the out-of-plan signals
(`pipeline/engine/scope_expansion.py`, ADR 0110) — but instead of only
classifying, a sanctioned discovery runs:

```
add_participant(repo):
  1. create an isolated worktree for repo (symmetry, §2)
  2. register Participant{...} in the run's participant set
  3. wire the source resolver so {dependency:repo} → its worktree (§3)
  4. extend delivery + verification coverage to the new participant
     (reusing ADR 0107 disclosure / 0084 receipts / 0087 allowed_modifications)
```

`add_participant` is **idempotent**: a repo already in the set is a no-op.

**Invariant (the heart of this ADR):** resolver-binding and isolation happen at
`add_participant` time, *not* at `final_acceptance`. ADR 0110 classifies after
the fact; by then verification has already run against the unbound tree. Binding
at discovery is what closes the false-green at the root rather than reporting it
late.

ADR 0110's pure classifier and its durable evidence shape are unchanged and still
render at `final_acceptance`; this ADR only moves the *promotion + binding*
action earlier and leaves the *classification + surfacing* where it is.

### 5. Sanction is an OperatingMode policy, not a hardcode

Today `scope_expansion.py` couples classification to verdict (`has_blocker →
forces REJECTED`). That coupling is the "prison" rule: a fixed wall independent
of how the operator chose to run. This ADR separates **what happened**
(classification — stays pure and deterministic) from **what to do about it**
(sanction — a `RunShape` policy projected from OperatingMode, via the existing
`pipeline/runtime/semantic_mode_defaults.py` / `pipeline/runtime/run_shape.py`).

A new `RunShape.scope_expansion_sanction` knob governs the `add_participant`
transition:

| Operating mode | Behaviour on participant-add / scope expansion |
| --- | --- |
| fast | auto-sanction (record → re-setup → continue), surfaced as `notice`; no pause |
| pro | `notice` auto; `risk` auto + alert; `blocker` → phase-handoff (not a silent reject) |
| governed | any participant-add alerts and routes through phase-handoff for operator sanction |

Escalation reuses the existing phase-handoff lifecycle (ADR 0038): a new trigger
`scope_expansion` with a handoff id such as
`scope_expansion:participant_add:<repo>`. `orcho_phase_handoff_decide` is
source-agnostic and accepts it without change; `orcho_handoff_advice` already
returns a typed recommendation. ADR 0110 already honours
`continue_with_waiver` — the operator escape hatch is preserved; what changes is
that the **default routing** (auto / handoff / reject) is now mode-projected
rather than fixed.

The only behaviour that stays hard regardless of mode is genuine safety
(`security` / `persistence` / `destructive_delete`): even there the contract is
**alert + default halt + waiver**, never an un-waivable dead-end. This keeps the
principle: verifiable autonomy, advisory authority.

### Cross / mono governance difference

The mechanism is identical for both modes; only the *trust level* of the
transaction differs. A mono run growing an undeclared companion is organic
expansion. A cross run adding a neighbour the architect omitted is **amending an
operator-declared scope contract** (the cross-plan); that is a higher-trust event
and, under any non-fast mode, leans toward handoff. This is governance via the
§5 policy, not a second code path.

## Scope / non-goals

- This is an execution-layer reshape with high blast radius, as ADR 0051 warned;
  it ships on its own design + migration sequence, not bundled into a bug fix.
- This ADR does **not** merge the mono and cross orchestrator bodies (ADR 0047).
  Our concrete multi-repo case is the "third real caller" ADR 0047 was waiting
  for to sanction extracting the shared substrate — it satisfies that condition,
  it does not waive it.
- No new gate-status vocabulary and no public-wire break is introduced by the
  resolver/participant substrate itself. Any MCP-visible surface (e.g. exposing
  participant-set state or the `scope_expansion` handoff to the client) ships
  with its `orcho-mcp` companion and an E2E mock smoke in the same change, per
  the MCP-validation rule — it is not deferred.

## Sequencing

This ships as **four small, independently-verifiable increments — one Orcho run
each**, not one mega-change. The deliberate slicing is itself the discipline: a
single run that tried to land the whole reshape is exactly what produces the
runaway context tail (see ADR 0113's evidence). Each increment is the smallest
step that is green and useful on its own.

1. **A — detection / fail-closed (closes the false-green).** Make source
   resolution fail-closed for any repo that has an isolated run worktree (key it
   off the existing isolation metadata — `meta.worktree.path` — not yet the
   participant set), and run the ADR 0108 provenance check as a **preflight
   before implement** so a wrong-tree source fails instead of passing vacuously.
   This is a *subset* of §3 and needs neither §1 nor §2; it is the cheapest step
   that closes the correctness hole, independently verifiable by the first guard
   test below.
2. **B — participant-set substrate + symmetric isolation.** Introduce the typed
   participant set (§1) and symmetric isolation (§2): every participant gets an
   isolated worktree, no participant edits its canonical tree mid-run. Migrate
   A's fail-closed resolver from raw isolation metadata to reading the
   participant set. This is the *prevention* half (A was *detection*).
3. **C — scope-expansion re-setup.** Lift detection to discovery time and
   implement the `add_participant` transition (§4), riding the participant set
   from B.
4. **D — sanction policy.** Move the verdict out of `scope_expansion.py` into a
   `RunShape.scope_expansion_sanction` projection and wire the `scope_expansion`
   phase-handoff trigger (§5).

(The earlier coarse framing folded A+B into a single "P0"; in practice the
false-green is closed by A alone, so A ships first and B follows as a separate
run.)

## Guard tests (the regression that would have saved the original run)

- A cross-project / scope-expanded run with an isolated worktree carrying a dirty
  diff: verification launched from a companion repo **must FAIL** if the resolved
  source for the changed repo points at the canonical sibling instead of the
  participant's worktree (increment A).
- `add_participant` is idempotent and binds the resolver *before* the next
  verification step runs, not at `final_acceptance` (increment C).
- Sanction routing follows OperatingMode: a `governed` run pauses on
  `scope_expansion:participant_add:*`; a `fast` run auto-sanctions and surfaces a
  `notice`; an active `continue_with_waiver` is respected in every mode (increment D).

## Consequences

- One participant set feeds Setup, Execution, and Control; source resolution,
  provenance, scope-expansion, and delivery cannot disagree about where a repo's
  in-flight code lives.
- A multi-repo change can no longer report a green verification that ran against a
  tree without the run's diff; the ADR 0108 invariant becomes reachable for the
  mono-scope-expansion path, not just the cross path.
- Scope expansion stops being a silent `cd` into a canonical sibling and becomes
  an explicit, recorded, mode-governed transition — visible to the operator and
  consistent between cross and mono.
- Orcho stays advisory: the operator's OperatingMode chooses the autonomy level,
  and the waiver remains an escape hatch in every mode. The engine enforces
  safety, it does not trap the operator.

## Open questions (inherited from ADR 0051)

- Where does the shared substrate live so `orcho-core` keeps mono importable
  without a cross dependency (direction rule: cross may depend on mono internals,
  not vice versa)?
- How much of `pipeline/cross_project/finalization.py` collapses into the shared
  finalization service vs stays cross-specific (run.end ownership, per-alias
  rollup)?

## Related

- [ADR 0051](0051-shared-runtime-path-context-layer.md) — the shared-layer
  roadmap this ADR realizes.
- [ADR 0047](0047-cross-project-application-boundary.md) — share substrate, not
  bodies; the three-caller threshold this case meets.
- [ADR 0108](0108-verification-provenance-gate-consistency.md) — the provenance
  gate the participant-bound resolver makes effective.
- [ADR 0110](0110-scope-expansion-notice.md) — the classifier whose detection is
  lifted to discovery time and whose verdict is moved to the sanction policy.
- [ADR 0107](0107-companion-repo-delivery-disclosure.md),
  [ADR 0084](0084-verification-contract-cross-repo-receipt-graph.md),
  [ADR 0087](0087-allowed-modifications.md) — the per-set delivery/verification
  surfaces reused by `add_participant`.
- [ADR 0038](0038-cross-plan-phase-handoff-parity.md) — the phase-handoff
  lifecycle the `scope_expansion` sanction trigger rides on.
- `docs/architecture/verification_contract.md` — the staged verification contract
  this slots into.

## Implementation status

Append-only progress log. **Closed 2026-06-30 — ADR Accepted.** All increments
(P0 fail-closed source resolution, B participant-set substrate + symmetric
isolation, C discovery-time `add_participant` re-setup, D OperatingMode-projected
scope-expansion sanction) are implemented and delivered to canonical `orcho-core`;
D landed as commit `0f86175`. The §1/§2/§4 substrate and the §5 sanction pillar
are complete. Only the open questions inherited from ADR 0051 remain (tracked
there, not here). The original per-increment entries below are preserved as
written.

- **P0 — fail-closed source resolution + provenance preflight: implemented.**
  Delivered in the change that introduces this ADR slice (uncommitted at the time
  of writing; the delivering commit on branch `orcho/run/20260627_123726_b22557`
  carries the diff). Covers §3 and the parts of §1/§3 needed to make the ADR 0108
  gate effective for the mono isolated-worktree path, without yet building the
  participant-set value object (§1) or symmetric isolation (§2):

  - **Fail-closed resolver** — `pipeline/engine/worktree_source.py`
    (`IsolatedSource`, `resolve_isolated_repo_source`, `isolated_source_from_meta`):
    a repo that owns an isolated per-run worktree resolves its verify/edit source
    to the worktree; a sibling-pointing or unresolved source is a hard
    `IsolatedSourceError`, not a silent fallback. The signature is shaped to later
    read a participant set by repo identity (§1) but does not build one.
  - **`{dependency:X}` + verify-env cwd binding** —
    `pipeline/verification_contract.py` (`placeholder_context_for`, which also
    derives the isolated source from the resolved `checkout` vs `project` gap) and
    `pipeline/verification_env.py` (`resolve_env_runtime`). `ORCHO_*_SRC`-style env
    values become a projection of the resolver rather than independently
    re-derived.
  - **Effective provenance (ADR 0108 branch (b))** — with `{dependency:X}` bound
    to the worktree, `collect_environment_checks` runs the declared assertions and
    a wrong-tree import downgrades the gate to `FAIL` via
    `apply_environment_provenance`, reaching readiness/delivery (the ADR 0108
    terminal invariant) instead of slipping through the vacuous branch (c).
  - **Provenance preflight at `before_phase(implement)`** —
    `pipeline/project/gate_repair.py` (`evaluate_isolated_source_preflight`),
    invoked from the existing pre-phase seam in `pipeline/project/run.py`. An
    isolated run whose source is sibling-pointing/unbound aborts before the
    implement/review cycle is spent; single-checkout runs are unaffected. No new
    phase/gate primitive — the existing pre-phase hook and provenance machinery
    are reused.
  - **Guard coverage** —
    `tests/unit/pipeline/verification/test_isolated_source_provenance_guard.py`
    (a git-backed isolated worktree + canonical sibling) pins the three
    invariants: sibling-source verify fails provenance, the preflight aborts
    before implement, and a single-checkout run is unaffected.

- **Not yet implemented:** the participant-set value object and `add_participant`
  discovery-time re-setup (§1, §4), symmetric isolation for every participant
  (§2), and the OperatingMode-projected `scope_expansion_sanction` knob (§5).

- **B — participant-set substrate + symmetric isolation + resolver migration:
  implemented.** Lands §1, §2, and §3's migration on top of P0 (uncommitted at the
  time of writing). The participant set now exists as a typed value object and is
  the run-scoped source of truth Setup/Execution/Control read; A's fail-closed
  resolver reads it instead of raw isolation metadata. C (§4) and D (§5) are still
  **not implemented** (see the dedicated note below).

  - **Typed `Participant` + run-scoped in-memory `ParticipantSet` (§1)** — the new
    substrate home is `pipeline/participants.py`. `Participant` is a frozen
    value object (`alias`, `repo`, `editable_checkout`, `base_ref`,
    `delivery_target`); `ParticipantSet` is an ordered, repo-identity-keyed
    (realpath-normalised) container with mono-seed, provisional-add, post-dispatch
    `bind_editable_checkout`, identity lookup, and an `isolated_source_for` bridge
    to the §3 resolver. The module is **mono-importable**: it imports only stdlib
    and `pipeline.engine.worktree_source` and references nothing from the
    cross-project package (verified by a source grep and an import-graph check in
    `tests/unit/pipeline/test_participants.py`). The set is **in-memory only**
    (mono: `state.extras["participant_set"]`; cross: the run-scoped
    `_CrossRunContext`/dispatch context) and is **never persisted** — the durable
    form stays `session['worktree']` / `meta.worktree`, from which the resolver
    re-seeds the set on resume / cold paths, so durable reproducibility is
    unchanged.
  - **Symmetric isolation (§2)** — each seeded/bound participant's
    `editable_checkout` is the single root of edits and verification. Mono seeds
    one participant from the resolved run checkout in
    `pipeline/project/state_setup.py`. Cross is two-phase: one **provisional**
    participant per alias is seeded in `pipeline/cross_project/run_setup.py`
    (canonical `delivery_target`, `editable_checkout` unbound), and its
    `editable_checkout` is bound **post-dispatch** in
    `pipeline/cross_project/project_dispatch.py` from the child's ACTUAL isolated
    worktree (`session['worktree']['path']`) right after the child session is
    saved — the parent set carries the child's real isolated path, and no parent
    worktree is created (the bind reads the child's own mono-isolation worktree).
    The **degraded isolation-off path is preserved** (not removed): when a child
    runs in-place, its worktree path equals the canonical tree, so
    `editable_checkout == delivery_target`. Both `app.py` facades stay thin — the
    seeding/binding lives in the focused setup modules.
  - **Resolver migration A → participant set (§3)** — `placeholder_context_for`
    (`pipeline/verification_contract.py`) and the durable/resume fallback in
    `pipeline/project/gate_repair.py` derive the `IsolatedSource` through a
    one-participant `ParticipantSet` instead of calling the meta/path derivations
    as parallel paths (the old `_isolated_source_from_paths` is removed; the
    derivation is encapsulated in the set constructor). The **fail-closed contract
    A is not weakened**: a participant whose source points at the canonical sibling
    still raises `IsolatedSourceError`, and single-checkout resolution stays
    byte-identical (`ctx.isolated_source is None` when checkout == project). The
    A-guard test is green unchanged, with an added participant-read sibling
    fail-closed test and a single-checkout parity test.

- **Increments C and D remain NOT implemented.** **C** — scope-expansion-driven
  re-setup / the `add_participant` discovery-time transition (§4) — and **D** —
  moving the sanction verdict into the `RunShape.scope_expansion_sanction`
  OperatingMode projection and wiring the `scope_expansion` phase-handoff trigger
  (§5) — are deferred to their own runs per the Sequencing plan. B deliberately
  builds neither `add_participant` nor the sanction projection.

- **C — discovery-time `add_participant` re-setup + scope-expansion detector/seam:
  implemented.** Lands §4 on top of B (uncommitted at the time of writing).
  Supersedes the B-era "C remains NOT implemented" note above for §4 only; **D
  (§5) stays NOT implemented** (see the closing note). The default sanction is the
  plain `record → re-setup → continue` — there is no `RunShape` mode matrix, no
  `scope_expansion:participant_add` handoff trigger, and no
  `fast`/`pro`/`governed` projection (all of that is D).

  - **Named entry point — `pipeline/participant_promotion.add_participant(run,
    repo, *, base_ref="")`.** The idempotent discovery-time transaction runs the
    four §4 steps, keyed on the discovered repo's realpath identity (a repeat for
    an already-present repo is an early no-op — no second worktree, no duplicated
    snapshot/delivery entry):
    1. **Non-colliding worktree identity (fix F1).** A stable alias is derived from
       the discovered repo (`basename` + a short realpath hash — there is no
       plan-alias for an out-of-set repo). Its worktree is created via
       `resolve_worktree_for_run(run_id=<alias>, branch_run_id=f"{primary_run_id}__{alias}")`,
       so the checkout path is `wt_<alias>` (distinct from the primary's
       `wt_<primary_run_id>`) and the `orcho/run/*` branch is unique in the shared
       source-repo ref namespace. A **loud guard** refuses (raises
       `WorktreeConfigError`, mirroring the cross loud-degrade in
       `isolation_setup`) when isolation is requested and not degraded yet the
       checkout collapsed onto the canonical sibling. An off / legitimately
       degraded worktree collapses `editable_checkout` onto its delivery target
       (the degraded contract §2).
    2. **Registration.** A bound `Participant` (its own `isolation` from the
       resolved worktree mode) is registered via the pure-domain
       `ParticipantSet.add_participant` (idempotent by repo identity).
    3. **Verification coverage — live resolver.** `state.extras["verification_placeholders"]`
       is rebuilt from the mutated set
       (`pipeline/project/state_setup.refresh_verification_placeholders`, reusing
       the same `placeholder_context_for` builder the initial seeding ran), so the
       next gate resolves `{dependency:repo}` to the participant's worktree, not
       the stale pre-promotion snapshot. This required a correction in the resolver
       owner: `placeholder_context_for` now redirects **each** declared dependency
       through the participant that owns *its* repo when the threaded set carries it
       — not only the call's selected (primary) participant — so a dependency naming
       a promoted out-of-set sibling binds to that sibling's worktree even though the
       snapshot is built with the primary's identity. The F2 verification guard
       asserts on the live `state.extras["verification_placeholders"]` snapshot
       (`dependencies["dep"] == participant.editable_checkout`), so a regression that
       stops refreshing the snapshot or stops redirecting fails the test.
    4. **Delivery coverage — real surface (ADR 0107).** The discovered repo's path
       is appended to `session['auto_detect']['delivery_projects']` — the durable
       list `collect_sibling_changes` / `evaluate_delivery_scope` read — so
       delivery scope includes the participant. The strict/expanded delivery
       **policy** is untouched (that is the D sanction).
  - **Phase-agnostic detector + thin seam.** `detect_out_of_set_repos(run)`
    generalizes the `scope_expansion` change-detection inputs
    (`git_changed_files` on the run checkout, `derive_in_plan_patterns`, and
    `collect_sibling_changes` over `delivery_projects` as the candidate source) to
    surface repos the run is acting on that the participant set does not yet cover
    — **without re-implementing the classifier** (it reuses `_path_matches`, it
    does not re-classify). `evaluate_scope_expansion_promotion(run, phase)` is the
    seam (modelled on `gate_repair.evaluate_isolated_source_preflight`): a strict
    no-op under dry-run, no contract, no run dir, an active operator waiver, or the
    re-entrant `_in_gate_hook`, else it promotes each detected repo via
    `add_participant` BEFORE the next verification. It is wired into
    `pipeline/project/run.py` as one thin routing call in each of `_on_phase_pre`
    (before the isolated-source preflight / pre-phase gates) and `_on_phase_end`
    (before the `after_phase` gate); run.py holds only the calls and preserves the
    `_in_gate_hook` re-entrancy guard.
  - **Untouched by design.** Per-participant **receipts** (ADR 0084 — the per-run
    `verification_receipt_index`) and **`allowed_modifications`** (ADR 0087 — from
    the plugin config) are not promotion concerns and are unchanged. The
    **scope-expansion classifier and its durable ADR 0110 evidence**
    (`pipeline/engine/scope_expansion.py`, the `final_acceptance` render) are
    byte-identical — C moves only promotion + binding earlier, never classification
    or surfacing. A single-participant run is byte-identical: the detector is empty,
    `add_participant` is never called, and the set / resolver snapshot /
    `delivery_projects` do not change.
  - **Guard coverage** — `tests/unit/pipeline/test_participant_promotion.py`
    (real git-backed temp trees) pins: idempotent add, F1 worktree-isolation
    distinctness, the heart invariant (live resolver binds `{dependency:repo}` to
    the participant worktree, asserted on the live snapshot), F2 delivery-coverage
    extension observed through `collect_sibling_changes` *and* the final
    `evaluate_delivery_scope` assessment (the promoted repo's dirty file appears in
    `disclosure` and the repo in `affected_projects`, not merely a non-`None`
    result), the phase-agnostic detector + seam, single-participant byte-parity, and
    an ADR-0110-unchanged classifier check.

- **C correction — discovered dirty diff replayed into the isolated worktree.** A
  review of the C slice found a behavioural gap in step 1: the detector only fires
  on a **dirty** out-of-set sibling, but the freshly-resolved participant worktree
  is built from the repo's `HEAD` and is therefore **clean**. Binding verification
  to it without the diff verifies a pristine tree and passes vacuously (the §3
  false-green) while the changes that triggered promotion stay only in the
  canonical checkout. The fix reuses the canonical pre-run dirty intake
  (ADR 0044): when the participant worktree is genuinely isolated
  (`ctx.path != repo`), `pipeline/participant_promotion._seed_discovered_changes`
  snapshots the repo's `git diff --binary HEAD` + untracked files into a
  participant-scoped seed dir (`<run_dir>/promoted/<alias>`, never the primary's
  `pre_run_dirty` dir) via `resolve_pre_run_dirty_intake` and replays them into the
  worktree via `apply_pre_run_dirty_seed`, so the verification subject carries
  exactly the discovered changes. A clean repo is a no-op; a transfer that cannot
  apply cleanly is a **loud halt** (`WorktreeConfigError`, mirroring the F1 guard)
  — a clean worktree never silently stands in for the discovered changes. The new
  `test_dirty_changes_seeded_into_verification_subject` guard dirties the sibling
  (a tracked edit **and** an untracked file) before promotion and asserts both land
  in the verification-bound `editable_checkout`.

- **D — scope-expansion sanction as an OperatingMode policy + phase-handoff
  trigger: implemented.** Lands §5 on top of C (uncommitted at the time of
  writing). This supersedes the C-era "Increment D remains NOT implemented" note
  above: with D the §5 pillar is **complete** — the verdict coupling is gone, the
  route is mode-projected, and both `scope_expansion` handoff triggers ride the
  ADR 0038 lifecycle. The §1/§2/§4 substrate (B/C) and §5 (D) are now all
  implemented; only the open questions inherited from ADR 0051 remain.

  - **Sanction is a projected policy, not a hardcode (§5).** The "prison rule"
    (`scope_expansion.py` coupling `has_blocker → forces REJECTED`) is removed
    from the *consumers*, not the classifier. A closed `ScopeExpansionSanction`
    enum (`pipeline/runtime/roles.py`: `AUTO_CONTINUE` / `AUTO_ALERT` / `HANDOFF`
    / `HALT_WAIVER`) names the routes; a `ScopeExpansionSanctionPolicy` carrier
    on `RunShape.scope_expansion_sanction` (`pipeline/runtime/run_shape.py`)
    holds the *posture* projected from `OperatingMode` (mirroring
    `OperatingModePolicy`, with the same `policy.operating_mode == operating_mode`
    consistency invariant). The carrier is a projection, **not** a baked outcome:
    the route is always computed by
    `pipeline/runtime/scope_expansion_sanction.decide(*, status,
    category_is_genuine_safety, operating_mode, has_active_waiver)`, a pure total
    function mirroring `session_disposition.decide`, with an import-time
    exhaustive table (`project_scope_expansion_sanction`) modelled on
    `semantic_mode_defaults._DEFAULT_OPERATING_MODE`. The ADR 0110 classifier
    stays byte-identical and a pure fact: `has_blocker` is a fact, not a verdict
    (`test_adr_0110_unchanged` still passes; the engine module names no release
    verdict and imports no sanction projection).
  - **The §5 matrix (`fast` / `pro` / `governed`).** `decide` projects each
    classified out-of-plan item: `fast` → `AUTO_CONTINUE` for any benign status
    (record → re-setup → continue, surfaced as a notice, no pause); `pro` →
    `notice` auto, `risk` `AUTO_ALERT` (continue + alert), `blocker` `HANDOFF`
    (phase-handoff, **not** a silent reject); `governed` → any participant-add /
    scope expansion `HANDOFF` with an alert. The consumer seam
    (`pipeline/phases/builtin/scope_expansion_support.route_scope_expansion_sanction`
    + thin routing glue in
    `pipeline/phases/builtin/handlers/final_acceptance.py`, resolving the run's
    `OperatingMode` via the existing `_operating_mode_for_state` substrate) emits
    a release gap (forces REJECTED) **only** for `HALT_WAIVER`; `HANDOFF` records
    a `needs_phase_handoff` route, `AUTO_ALERT`/`AUTO_CONTINUE` never block.
  - **Genuine safety and the operator escape hatch are preserved in every mode.**
    A genuine-safety class (`security` / `persistence` / `destructive_delete`,
    derived from the classifier's own category / evidence facts) is `HALT_WAIVER`
    — alert + default halt + waiver — in **every** mode including `fast`, never
    silently auto-sanctioned. An active `continue_with_waiver` (ADR 0072/0073)
    fully disarms the gate (`AUTO_CONTINUE`) in every mode, even over a
    genuine-safety class — the single operator escape hatch, no new parallel
    waiver path. The two dogfood forms that the old fixed coupling hard-rejected
    (a benign `sdk/__init__` export add; a companion large diff on
    `run_projection.py`) now continue / route to handoff under `fast`/`pro`
    instead of a hard REJECT, pinned by a reproducer test.
  - **Both scope-expansion handoff triggers (§5, on ADR 0038) — wired to the
    runtime pause.** `pipeline/runtime/handoff.build_scope_expansion_handoff_signal`
    builds a `PhaseHandoffRequested` on the existing ADR 0038 lifecycle for
    **both** trigger families: `scope_expansion:participant_add:<repo>` (via
    `scope_expansion_participant_add_trigger`) and the generic out-of-plan
    `scope_expansion:out_of_plan` (`SCOPE_EXPANSION_OUT_OF_PLAN_TRIGGER`). Each
    builder is now **called from a real run seam**, not only tests, and the
    signal is set on `state.phase_handoff_request` so the runner breaks out of
    the phase walk and the orchestrator pause tail
    (`pipeline/project/handoff.apply_phase_handoff_pause`) persists
    `meta.phase_handoff` + `meta.status='awaiting_phase_handoff'` and exits rc=4:
    - the **out-of-plan** seam: when `route_scope_expansion_sanction` marks a
      `needs_phase_handoff`, the `final_acceptance` handler raises the pause via
      the focused `scope_expansion_support.raise_scope_expansion_handoff` helper
      (thin routing glue in the handler — architecture fitness). Genuine-safety
      `HALT_WAIVER` still rejects via the release-gap path and raises no pause;
      an active waiver leaves the assessment empty (no pause);
    - the **participant-add** seam: in `governed` mode
      `participant_promotion.evaluate_scope_expansion_promotion` routes each
      discovered out-of-set repo through the pause (one operator sanction at a
      time) before promoting it; a recorded operator decision (durable decision
      artifact) lets the resumed seam fall through to `add_participant`, so the
      pause is idempotent across resume. `fast`/`pro` keep the increment-C
      default (record → re-setup → continue / promote, no pause).

    Each pause carries the operator action set `continue` / `halt` /
    `continue_with_waiver`. `retry_feedback` is **deliberately omitted**: the
    sanction is raised at the **terminal** `final_acceptance` seam — a bare
    top-level phase with no plan/repair loop to retry into — so offering a retry
    would mis-route the resume. `continue_with_waiver` is the durable escape
    hatch. The `final_acceptance` seam is widened at **both** support-check sites —
    `handoff._SUPPORTED_HANDOFF_PHASES` and `runner._validate_handoff_support`
    (bare top-level, like `implement`) — so neither trigger is dropped; the
    existing `rejected`/`approved`/`incomplete` triggers are untouched.
    `phase_handoff_decide` accepts each opaque trigger and applies the action;
    `request_handoff_advice` returns a typed recommendation for both (advice
    eligibility now recognises the `scope_expansion:` family and, since the seam
    offers no retry, no longer requires `retry_feedback` in `available_actions`
    for it; a recommendation outside the offered set is clamped to `halt`); the
    trigger is preserved byte-identically in `meta.phase_handoff` and in the
    durable advice artifact.
  - **Resume lifecycle (F1).** Closing the pause after the operator decision is a
    dedicated resume arm, `pipeline/project/handoff._apply_scope_expansion_handoff_resume`,
    dispatched in `apply_phase_handoff_resume` on `active.phase == "final_acceptance"`.
    Without it the `final_acceptance` handoff fell through to the generic
    `continue` arm, which `strip_plan_loop()` + marks `plan`/`validate_plan`
    completed + rehydrates the plan — mis-resuming a finished run as a plan-loop
    continuation (and `retry_feedback` into a plan retry). The arm instead reports
    `final_acceptance` completed (so the resumed walk short-circuits the already-run
    terminal phase, honouring the idempotency note on
    `raise_scope_expansion_handoff`): `continue` closes the payload, and
    `continue_with_waiver` persists a durable `phase_handoff_waiver` to the session
    + `state.extras`. Covered by the resume-dispatch tests in
    `tests/unit/pipeline/test_scope_expansion_handoff.py` (continue / waiver close
    the terminal pause without stripping the plan loop; waiver required;
    `retry_feedback` rejected by both the decide gate and the resume arm).
  - **MCP visibility (T4 determination): OPAQUE — branch (a).** Both triggers are
    confirmed opaque with **no new MCP-visible surface**: `docs/sdk_schema.json`
    is unmodified (the phase-handoff `trigger` and `HandoffAdviceCall.trigger` are
    pre-existing opaque `str` fields carrying new *values*, not a new field /
    type); no new phase-handoff action enum value; the MCP-visible
    `HandoffAdviceResult` shape is unchanged; the handoff_advice evidence slice
    derives its `trigger` label from the verdict (`rejected` / `incomplete`), so
    the opaque `scope_expansion:*` string never reaches the MCP-visible slice (it
    lives only in the in-core durable artifact and the pre-existing
    `meta.phase_handoff` payload). Per the §5 MCP-validation rule this is branch
    (a): **orcho-mcp is not modified** and an in-core hermetic mock-smoke
    (`tests/unit/pipeline/test_scope_expansion_handoff.py`,
    `test_mock_smoke_scope_expansion_handoff_decide_advice_evidence`, parametrized
    over both triggers under `MockAgentProvider`) discharges D — driving both
    triggers through decide + advice and asserting handoff_advice evidence
    visibility, with no real model calls. Had a client-visible field/condition
    been introduced, branch (b) would have required a paired orcho-mcp surface +
    E2E mock-smoke in the same change (or halt-as-blocked); it did not arise.
  - **Guard coverage** — `tests/unit/pipeline/runtime/test_scope_expansion_sanction.py`
    (the §5 matrix, genuine-safety halt in every mode, waiver-disarm in every
    mode, the policy-not-outcome knob, the import-time table guard),
    `tests/unit/pipeline/phases/test_final_acceptance_scope_expansion.py`
    (mode-routed verdicts, genuine-safety halt, waiver-disarm, the dogfood
    reproducers, classifier byte-parity, and the out-of-plan **end-to-end** pause
    test — REAL handler → REAL `apply_phase_handoff_pause` → REAL decide / advice,
    asserting `meta.phase_handoff.trigger` + `awaiting_phase_handoff` with no
    hand-built signal or seeded paused run),
    `tests/unit/pipeline/test_participant_promotion.py` (the governed
    participant-add seam raises the pause before promoting and promotes after a
    recorded decision; `fast`/`pro` promote with no pause), and
    `tests/unit/pipeline/test_scope_expansion_handoff.py` (both triggers through
    signal build / both support sites / decide / advice / evidence, trigger
    persistence, and the T4 mock-smoke).
  - **OperatingMode is projected from the real run source (follow-up F1).** The
    sanction sites resolve the run's posture from a single projected stamp,
    `state.extras['operating_mode']`, written **once** at run-state assembly by
    `pipeline/project/state_setup.build_pipeline_state`
    (`_resolve_operating_mode`). Its priority mirrors the verification work-mode
    resolution: the effective `verification_contract.work_mode` (which already
    folds the explicit CLI `--mode`, carried via `ORCHO_WORK_MODE` — also where
    an auto-detect run lands its `actual_mode` — the project/contract work_mode,
    and the profile default) → the auto-detect `actual_mode` on
    `session['auto_detect']` (no-contract fallback) → the conservative `fast`
    default. Both sites read that one stamp through the single
    `pipeline/runtime/run_shape.operating_mode_from_state` helper:
    `final_acceptance` via its `_operating_mode_for_state` alias, and the
    participant-promotion governed route directly (its earlier private
    `extras`-resolver was removed — no second path). The prior review found that
    nothing populated `extras['operating_mode']` in a real run, so `pro` /
    `governed` runs silently degraded to `fast` (a `pro` blocker never opened
    phase-handoff; a `governed` participant-add promoted silently). This closes
    the run-mode source. Covered without hand-injecting the stamp by
    `tests/unit/pipeline/test_participant_promotion.py`
    (`test_real_state_assembly_projects_operating_mode_from_work_mode`,
    `test_real_state_assembly_falls_back_to_auto_detect_then_fast`,
    `test_governed_route_fires_from_real_state_assembly`,
    `test_fast_route_promotes_from_real_state_assembly`) and the reader unit
    tests in `tests/unit/pipeline/runtime/test_run_shape.py`.

- **Companion / complementary work.** D owns the §5 sanction knob and the two
  scope-expansion phase-handoff triggers; it deliberately does **not** own the
  companion-changes *notice UX* — that is the companion task
  **`correction-ux-stage-2-scope-expansion-notice`** (Stage 2: surfacing
  scope-expansion notices in the correction UX, built on the durable
  `scope_expansion` / `scope_expansion_sanction` evidence D records). D is also
  complementary to **`correction-route-convergence-guard`** (which guards
  correction-route convergence); the two compose — D decides the sanction route,
  the convergence guard keeps the correction loop from diverging — without either
  owning the other's surface.
