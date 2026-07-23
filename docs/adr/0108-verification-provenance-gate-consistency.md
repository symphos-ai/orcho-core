# ADR 0108 — One environment-provenance overlay and project-aware phase receipts

Status: Accepted

## Context

ADR 0125 made a failed environment-provenance check downgrade the gate(s)
scheduled at that phase to `FAIL`, so a fresh, exit-0 command receipt produced by
an interpreter that imported the codebase from the wrong tree can no longer read
as green. It did so by introducing one read-only reader
(`environment_provenance_failures(run_dir)`) and one pure downgrade helper
(`environment_provenance_gate_failures(command_phases, failures)` over
`command_phase_schedule(contract)`), and wiring **two** surfaces to it: the typed
SDK projection `sdk.get_verification_timeline` and the live DONE/HALTED render in
`pipeline.project.verification_timeline`.

Two follow-on gaps remained after ADR 0125:

1. **The downgrade reached only two of four gate surfaces.** "What do this run's
   official verification gates look like" is answered in four places, not two.
   Besides the SDK projection and the live timeline, the final-acceptance
   **readiness** summary (`pipeline.verification_readiness`) and the Stage 6
   **delivery** assessment (`pipeline.verification_delivery`) classify the same
   required gates from their command receipts. ADR 0125 wired the downgrade into
   the two *timeline* surfaces only, and it did so as **open-coded copies** — each
   timeline module recomputed "which gate failed provenance" inline. Readiness
   and delivery did not apply the rule at all, so a provenance-failed required
   gate could read `FAIL` on the timeline yet still be absent from the readiness
   `required_failed` bucket and from the delivery blocking set. The terminal
   delivery path keys on the delivery assessment; if that assessment did not see
   the downgrade, a `final_acceptance = APPROVED` run could finalize as a clean
   committed/done while the timeline simultaneously showed the gate red. A
   per-surface rule is also a drift hazard: four copies of "which gate failed
   provenance" can diverge.

2. **The phase-receipt provenance probe was core-only.** ADR 0076's
   `collect_environment_checks(cwd)` ran a single hard-coded `pipeline_import`
   invariant: it asserted the interpreter imports `pipeline` from
   `<cwd>/pipeline/__init__.py`. That is correct for an engine checkout that
   *contains* a local `pipeline/` package, but for any other project — for
   example an MCP-shaped checkout whose package lives under `src/` and whose
   `pipeline` is a declared dependency in a separate checkout — there is no local
   `pipeline/__init__.py`, so the probe recorded `expected = None`,
   `passed = false`. That manufactured a provenance failure for a checkout that
   simply is not the engine, which — once the overlay above reaches delivery —
   would block delivery on a false signal.

Constraints carried over from ADR 0125: no new gate-status vocabulary, and the
public wire shape must not drift.

## Decision

### 1. One environment-provenance overlay — the single effective classification

Introduce one pure helper,
`pipeline.verification_readiness.apply_environment_provenance(status_by_command,
contract, run_dir)`, layered **on top of** the ready
`classify_required_receipts` output. For each phase-scheduled command whose
scheduled phase recorded a failed `verification_environment` check, it returns a
new `ReceiptClassification(status="failed")` carrying the provenance failure's
operator-evidence as its `reason` (the `"<check>: expected <X> actual <Y>"`
string) and its `path` repointed at the failing phase receipt, while preserving
the original `source_run_id`. It reuses the ADR 0125 primitives
(`command_phase_schedule`, `environment_provenance_failures`,
`environment_provenance_gate_failures`) and is never-raise: any IO / JSON /
lookup error degrades to the input map unchanged.

All **four** official surfaces now apply this one overlay over their
`classify_required_receipts` result — there is exactly one effective
classification, computed in one place:

- **Readiness** — `build_final_acceptance_readiness` and `required_receipt_gaps`
  overlay before bucketing, so a provenance-`FAIL` lands in `required_failed`
  and, under a `require` policy, in the release gaps.
- **Delivery** — `assess_delivery_verification` overlays before `partition_gaps`,
  so a provenance-failed `require` gate enters `blocking_gaps` and
  `blocking == True`.
- **SDK timeline** — `_project_with_contract` consumes the overlaid
  classification; its previous open-coded `provenance_failed` / `provenance_active`
  block is removed. `GateProjection.detail` now derives from the classification's
  `reason` for a non-manual failed gate; `status="failed"` maps to `FAIL`.
- **Live timeline** — `_run_level_projection` consumes the overlaid
  classification; its local provenance recomputation is removed.

The two timeline modules no longer carry their own copy of the rule; the SDK and
live surfaces, plus readiness and delivery, are four consumers of one helper.

**Why the overlay is not folded into `classify_required_receipts` itself.**
`classify_required_receipts` has many consumers beyond these four gate surfaces —
the auto-run collector, `gate_repair`, the verification autorun path, and others
that drive *execution* decisions, not just *reporting*. Folding the provenance
downgrade into the base classifier would blast that radius across all of them and
could feed a downgraded status back into the gate-rerun / repair loop, risking
re-run churn on a gate the command receipt already passed. The downgrade is a
*reporting/gating* overlay, so it lives as a separate helper applied by exactly
the surfaces that should gate on it, and the base classification stays
single-responsibility. The overlay also deliberately does **not** reason about
manual/operator-only policy: it downgrades every phase-scheduled provenance
failure (manual included), and each surface keeps a manual gate non-blocking
through its own existing policy routing — readiness `manual_only_gaps`, delivery
`manual_only`, SDK `SKIPPED`, live `manual_only`.

### 2. Strengthened terminal invariant

The reach of (1) into delivery makes the masking guarantee end-to-end: **a
required gate that reads `FAIL` on the timeline cannot be reported as a clean
success by readiness, delivery, or the terminal path.** Because
`assess_delivery_verification` now applies the overlay, a provenance-failed
`require` gate is `blocking`, and `resolve_commit_delivery`
(`pipeline.engine.commit_delivery`) returns `verification_blocked` in
non-interactive mode — `pipeline.project.run` maps that to a
`commit_delivery_verification_blocked` halt. The independent release gate keeps
its own priority, so a `final_acceptance = APPROVED` verdict no longer masks a
provenance-failed required gate: APPROVED clears only the release gate, while the
delivery verification gate independently blocks. The run does not finalize as a
clean committed/done; the typed provenance evidence (the failing check,
`expected` / `actual`, and the phase `receipt_path`) remains readable from the
durable `verification_environment` receipt — no terminal-banner parsing.

### 3. Project-aware phase-receipt provenance

`collect_environment_checks(cwd, *, contract=None, ctx=None)` (and
`write_phase_verification_receipt(..., contract=None, ctx=None)`, which forwards
them) selects checks by three branches:

- **(a) Core checkout.** When `<cwd>/pipeline/__init__.py` exists, run the
  unchanged ADR 0125 `pipeline_import` subprocess invariant. This is the
  load-bearing core guard and is deliberately byte-for-byte preserved: an engine
  checkout whose interpreter imports `pipeline` from outside the checkout still
  records `pipeline_import.passed = false` and still produces a provenance
  failure.
- **(b) Non-core checkout with declared assertions.** When there is no local
  `pipeline/` but the contract declares a verification env (its `default_env`,
  else the single declared env) carrying non-empty `assertions`, execute those
  declared assertions via `pipeline.verification_env.run_env_assertions` against
  `ctx` and map each result into a receipt check. An MCP-shaped checkout can then
  prove its own import provenance — for example `orcho_mcp` imported from
  `{checkout}/src` and `pipeline` imported from a `{dependency:orcho-core}`
  checkout — and the resulting receipt carries `passed = true` checks with no
  provenance failure.
- **(c) Otherwise.** Neither a local `pipeline/` nor declared assertions: record
  **no** failing provenance check — a single informational, non-failing
  `environment_provenance` check — so a checkout with nothing to assert never
  yields a false provenance failure (`environment_provenance_failures` returns
  empty), and the writer never raises on `expected = None`.

The probe is never-raise: a subprocess / IO / resolution error in (b) degrades to
the (c) non-failing set, never a false failure, and `commands` always carries at
least one diagnostic entry. The `implement` and `repair_changes` handlers thread
`contract = state.extras["verification_contract"]` and
`ctx = state.extras["verification_placeholders"]` (set before the phases run);
when absent, both default to `None` and the probe falls back to (a)/(c).

### 4. Deliberately preserved invariants

- **Gate-status vocabulary is unchanged.** `GateStatus` remains exactly the six
  values `PASS` / `FAIL` / `MISSING` / `STALE` / `SKIPPED` / `FRESH` (no
  `MANUAL`). The overlay only sets a classification status of `failed`, which
  maps to the existing `FAIL`.
- **No public wire change.** `GateProjection.detail` and
  `GateProjection.receipt_path` already exist from ADR 0125; this change only
  routes their existing content through the shared overlay. The provenance-`FAIL`
  `detail` / `receipt_path` / `rerun_hint` are byte-identical to ADR 0125 for the
  already-`FAIL` case, so `python tools/dump_sdk_schema.py --check` shows no drift
  in `docs/sdk_schema.json`. Because there is **no** wire-shape change, this ADR
  needs no `orcho-mcp` companion update — the MCP schema is untouched (contrast
  ADR 0125, whose additive `detail` field did require the companion).

## Consequences

- There is one effective gate classification. Readiness, delivery, the SDK
  projection, and the live DONE/HALTED render cannot disagree about whether a
  required gate failed provenance, because they all read the same overlay over
  the same base classification — the two timeline modules no longer keep their
  own copies.
- A `final_acceptance = APPROVED` run with a provenance-failed `require` gate
  halts at `commit_delivery_verification_blocked` instead of finalizing clean.
- A non-engine checkout (no local `pipeline/`) is no longer falsely failed: it
  proves provenance through its own declared `verification_envs` assertions, or
  records a non-failing informational check when it declares none. The engine
  checkout-local `pipeline_import` invariant is unchanged.
- The base `classify_required_receipts` and its execution-side consumers
  (auto-run collector, `gate_repair`, verification autorun) are untouched, so the
  downgrade adds no re-run / repair-loop pressure.

This ADR builds on ADR 0125 (environment-provenance failure downgrades its phase
gate to `FAIL` — the rule this change makes the single effective classification
across all four surfaces and extends into project-aware phase receipts), ADR 0095
(verification-gate timeline durable trails — the surfaces being unified), ADR
0083 (Stage 6 delivery-gate assessment — the blocking delivery gate that carries
the invariant to the terminal path), ADR 0082 (final-acceptance readiness — one
of the four surfaces), and ADR 0076 (durable verification-environment receipt —
the source of the provenance signal and the receipt the project-aware probe
writes). It is append-only and supersedes none of them.
