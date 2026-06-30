# ADR 0107 — Companion-repo delivery disclosure from durable plan scope

Status: Accepted

Extends ADR 0102 (run topology and delivery-scope axes) in the *disclosure*
dimension. ADR 0102 stays in force; this ADR widens what the delivery gate
detects and how an incomplete multi-repo delivery is surfaced. Append-only:
nothing in 0102 is rewritten.

## Context

ADR 0102 gave delivery three axes and taught the mono delivery gate to *collect*
sibling-repository changes from `meta.auto_detect.delivery_projects` and either
disclose them (`expanded_mono`) or park a reversible blocker (`strict_mono`).
That closed the "silently ship a half-change" gap for the strict case, but three
problems remained for the common dog-food shape — a `feature` run on `orcho-core`
whose plan declares a mandatory edit in a companion repository (`orcho-mcp`):

1. **Detection was too narrow.** Sibling repositories were known only from
   `auto_detect.delivery_projects` (the topology heuristic's alias list). A
   companion a run was *required* by its own plan to touch — declared in the
   durable `ParsedPlan` / per-subtask `owned_files` / `allowed_modifications`
   (e.g. `../orcho-mcp/**`) — was invisible unless the topology heuristic
   happened to name it too.

2. **State was binary.** Collection reported only *dirty* sibling files. It
   could not distinguish a companion that still needs work (uncommitted) from
   one whose required edit already *landed as a commit*, nor from one that was
   *declared but never touched*. A clean-but-committed companion looked
   identical to a planned-but-untouched one.

3. **Completion was dishonest.** Under `expanded_mono` the primary checkout
   commits and the run finalizes green DONE even when the companion repository
   is still dirty — exactly the dog-food incident (run `20260625_181331_2a392f`):
   `orcho-core` delivered, `orcho-mcp` left uncommitted, and nothing told the
   operator the multi-repo delivery was only half done.

The fix must reuse the ADR 0102 delivery-scope machinery (no parallel disclosure
system), keep the strict/expanded blocking semantics byte-identical, stay
provider-neutral, and never scrape the transcript — detection must rest on
durable artifacts only.

## Decision

Detect companion repositories from the **durable plan scope**, classify each one
with a typed observable state, propagate the disclosure through delivery and
finalization, and refuse to present a multi-repo run as fully complete while a
declared companion is still uncommitted.

### Detection — durable plan scope ∪ delivery_projects

A focused module `pipeline/engine/companion_scope.py` owns the new
responsibility (kept out of the already-large `delivery_scope.py` /
`commit_delivery.py` bodies, per the Architecture Fitness Gate):

- `derive_companion_aliases(plan, delivery_projects, known_aliases, primary_alias)`
  is pure. It unions the `meta.auto_detect.delivery_projects` aliases (taken
  verbatim — declared aliases by construction) with the alias tokens it
  recognises in the durable plan scope: the `ParsedPlan.owned_files` /
  `allowed_modifications` and each subtask's `owned_files` /
  `allowed_modifications`. A plan-scope reference contributes an alias **only
  when a token matches a registered workspace alias**
  (`pipeline/project/project_aliases.load_workspace_project_aliases`), so a
  `[subtask-id]` tag or a primary-repo path never leaks in. The primary alias is
  always excluded. No transcript parsing — the `parsed_plan.json` artifact is the
  durable source, loaded at delivery time from the run dir.

### Companion base revisions — the observable `committed` signal

`committed` must rest on a durable signal, not a dirty-vs-clean heuristic. At
first detection of the companion set, `evaluate_delivery_scope` records each
companion repository's `HEAD` sha into
`session['auto_detect']['companion_base_revisions']` (additive durable meta) when
no base is already recorded. That base is the revision a later `committed`
observation is measured against. `core/io/git_helpers.git_committed_files_since(cwd, base_ref)`
returns the files changed by commits in `base_ref..HEAD` — the commit-range diff
that backs the `committed` state (distinct from `git_changed_files`, which
reports uncommitted working-tree state).

### Typed per-repo state

`CompanionRepoState` (a closed `StrEnum`) carries one of three values per
companion repo, classified by `classify_companion_state` in priority order:

- `dirty` — the companion working tree has uncommitted changes (it still needs a
  commit / follow-up before delivery is complete);
- `committed` — the working tree is clean **and** an observable durable signal
  shows the required edit landed: HEAD advanced past the recorded base revision
  over declared paths (commit-range diff), **or** a recorded companion delivery
  result names a `commit_sha`;
- `planned_requirement` — declared by the plan scope but neither dirty nor moved
  past its base: the required companion edit has not happened.

`assess_companion_repos` does the I/O assessment, reusing
`delivery_scope.collect_sibling_changes` as the dirty-path collection base, and
returns one `CompanionRepo` (`alias`, `path`, `state`, `changed_paths`) per
resolvable companion plus any newly captured base revisions. An unregistered /
missing alias yields no entry and never crashes delivery.

### Enrichment on the delivery decision

`DeliveryScopeAssessment` gains `companions`. `evaluate_delivery_scope` derives
the full companion set, classifies each repo, and attaches the result — but the
strict/expanded gate decision stays driven by the **dirty** companions only, so
ADR 0102's blocking semantics are byte-identical (a `committed` or
`planned_requirement` companion never blocks or widens). The companions ride on
`CommitDeliveryDecision.scope_companions`, serialised additively in `to_dict`
(each `CompanionRepo` → `{alias, path, state, changed_paths}`). The
`patch_text`-is-never-serialised invariant is preserved, and the backward-compatible
`scope_disclosure` string format (`[alias]/rel`) is unchanged.

### Finalization caveat + actionable next step

`pipeline/project/run.py::_record_multi_project_delivery(session, decision)`
propagates the decision's companions (no git re-scan) into a durable
`session['multi_project_delivery']` block —
`{primary_status, companions: [{alias, path, state, changed_paths}]}` — at every
commit-delivery persist site. No-op when the run touched no companion, so a clean
single-repo run is byte-identical.

`pipeline/project/finalization.build_companion_delivery_caveat(session)` (a
focused helper, not inlined into `finalize_project_run`) returns a
`CompanionDeliveryCaveat` **only** when the primary delivered
(`primary_status ∈ {committed, applied_uncommitted}`) **and** at least one
declared companion is `dirty`. `FinalizationResult.companion_caveat` carries it;
the terminal wrapper renders an amber `DONE — COMPANION DELIVERY INCOMPLETE`
header plus per-repo disclosure and an actionable next step ("review and commit
the companion repo(s), or start a cross-run / follow-up for companion delivery").
The run stays `done` (the primary genuinely shipped), but it never *reads* as
fully complete with a companion left behind. Finalization ordering (capture diff
→ delivery → no-diff / rejected backstops → persist → checkpoint) is unchanged:
the caveat is read at result construction, after the delivery step.

### Durable evidence block — `multi_project_delivery`

`pipeline/evidence/collector._build_multi_project_delivery(meta)` projects the
durable block into an additive top-level evidence-bundle key
`multi_project_delivery` (`primary_status`, per-repo `companions`, and a
convenience `dirty` alias list). The v1 schema permits additive top-level keys,
so single-repo bundles stay byte-identical (the key is omitted). This is a
core-durable surface; MCP does not consume it yet.

### `scope_disclosure` semantics extended (backward-compatible)

The SDK delivery surfaces (`sdk/run_control/delivery.decide_delivery` and
`delivery_decision_state`) project the enriched disclosure through the existing
typed `scope_disclosure` field. `_scope_disclosure(ctx)` now merges the legacy
`scope_disclosure` list (the strict-mono violation siblings, kept first in
original order so an existing consumer sees a byte-identical prefix) with every
`scope_companions[].changed_paths` (dirty and observably committed), appended,
sorted, de-duplicated. The string format stays `[alias]/rel`. The per-repo typed
state and full path are **not** folded into these strings — they live in the
core-durable `multi_project_delivery` evidence block — so the field stays a plain
`tuple[str, ...]`.

### MCP parity decision — core-only, no break

No new MCP-visible SDK field and no breaking value structure are introduced.
`scope_disclosure` keeps its shape (`tuple[str, ...]` of `[alias]/rel`); only its
*population* is enriched (a possibly longer list of the same string format).
`docs/sdk_schema.json` is unchanged (shape-only schema snapshot still passes).
The existing orcho-mcp consumers read `scope_disclosure` defensively as
`list[str]` (`run_control/delivery.py`, `services/delivery_gate.py`,
`schemas/*`), so they tolerate the enriched values without change. Therefore the
conditional orcho-mcp parity work (the plan's T6) is **not** triggered; this
delivery is orcho-core only. Were a future change to add an MCP-visible field or
break the value structure, that would re-activate the same-change orcho-mcp
projection + E2E mock smoke discipline (ADR 0102's MCP wire rule).

## Consequences

- The delivery gate now detects companion repositories a run is *required* by its
  durable plan to touch, not only those the topology heuristic guessed.
- A clean-but-committed companion is observably distinct from a
  planned-but-untouched one, resting on a recorded base revision + commit-range
  signal rather than a dirty-vs-clean heuristic.
- A multi-repo run whose primary shipped while a declared companion stayed dirty
  no longer finalizes as a green DONE: it carries a typed caveat, a durable
  evidence block, and an actionable next step.
- ADR 0102's strict/expanded blocking semantics are byte-identical; the
  `committed` / `planned_requirement` states are additive disclosure, never a
  block. A clean single-repo run records no companion block and is unchanged.
- The SDK/MCP wire shape is unchanged: `scope_disclosure` semantics are extended
  backward-compatibly, no new field, `docs/sdk_schema.json` untouched, T6 not
  activated.

A full operator authoring guide for resolving an incomplete companion delivery
(follow-up vs. cross-run) is deferred.
<!-- TODO(orcho-phase-topology): expand companion-delivery resolution guide -->

## References

- [ADR 0102 — run topology and delivery-scope axes](0102-run-topology-and-delivery-scope-axes.md)
  (extended here in the disclosure dimension)
- [ADR 0099 — deferred delivery decision gate and out-of-band decide surface](0099-deferred-delivery-decision-gate.md)
- [ADR 0032 — commit-decision gate](0032-commit-decision-gate.md)
- `pipeline/engine/companion_scope.py` — companion derivation + typed per-repo state
- `pipeline/engine/delivery_scope.py` — `DeliveryScopeAssessment.companions`, `evaluate_delivery_scope`
- `pipeline/engine/commit_delivery.py` — `CommitDeliveryDecision.scope_companions`
- `core/io/git_helpers.py` — `git_committed_files_since` (commit-range `committed` signal)
- `pipeline/project/run.py` — `_record_multi_project_delivery` (durable `multi_project_delivery` block)
- `pipeline/project/finalization.py` — `build_companion_delivery_caveat` / `CompanionDeliveryCaveat`
- `pipeline/evidence/collector.py` — `_build_multi_project_delivery` (evidence-bundle block)
- `sdk/run_control/delivery.py` — `_scope_disclosure` (extended scope-disclosure projection)
