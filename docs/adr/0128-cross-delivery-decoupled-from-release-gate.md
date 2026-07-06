# ADR 0128 — Cross delivery decoupled from the release-gate policy

- **Status:** Proposed
- **Date:** 2026-07-06
- **Deciders:** project owner
- **Related:**
  - [ADR 0099](0099-deferred-delivery-decision-gate.md) — the deferred,
    non-interactive delivery decision gate and the single
    `apply_commit_delivery` apply path the cross loop reuses per alias
  - [ADR 0107](0107-companion-repo-delivery-disclosure.md) — companion
    repo delivery; cross delivery transports each child's retained
    worktree diff
  - [ADR 0115](0115-finalization-reducer.md) — the finalization reducer
    and the single release-verdict source (`is_approved`) the per-alias
    override gate routes through

## Context

Cross-project runs execute each project alias in a one-shot, retained run
worktree on its own throwaway branch. The cross-level delivery step
(`pipeline.cross_project.cross_delivery.run_cross_delivery`) is what
transports those per-alias diffs back into the operator's project
checkouts, using the proven mono commit-delivery primitives.

`pipeline.cross_project.session_run._run_delivery_and_finalize` decided
whether to run that step. It gated on the **release-gate policy**:

```python
if not ctx.release_skipped_by_policy:
    ctx.delivery_result = run_cross_delivery(..., override=...)
```

`ctx.release_skipped_by_policy` is `True` whenever the resolved profile
carries no release gate — either the cross-final-acceptance (CFA) gate is
disabled or its run policy is `NEVER`. Gate-less profiles (for example
`small_task`) therefore reached delivery with `release_skipped_by_policy=
True` and `cfa_outcome=None`, and the inline `if` **skipped delivery
entirely**. The alias diffs were left stranded on their one-shot run
worktree branches with no signal to the operator — the run reported
`done`, but nothing was delivered.

This is asymmetric with mono. The single-project path gates delivery on
the run having *finished* — `_session_allows_commit_delivery`
(`pipeline.project.run`) returns `True` when `status == "done"`, or when
the operator is overriding a REJECTED verdict — **not** on whether a
release gate was configured. A mono run under a gate-less profile still
delivers when it finishes cleanly.

The inline `override=(ctx.cfa_outcome.outcome == "override_continue")`
was also not null-safe: it was only ever evaluated on the
`not release_skipped_by_policy` branch (where `cfa_outcome` is set), so
extending delivery to the policy-skip branch — where `cfa_outcome is
None` — would raise `AttributeError`.

## Decision

Cross delivery is decoupled from the release-gate policy. A disabled or
`NEVER` release gate **bypasses gating**; it does not suppress delivery.

The 'deliver now?' decision moves out of the inline condition into a
small, testable classifier, `_cross_delivery_plan(ctx) -> (should_deliver,
override)`, mirroring the intent of mono
`_session_allows_commit_delivery`:

- `should_deliver` is `True` at this point for both the approved path and
  the policy-disabled path. `_run_delivery_and_finalize` is reached only
  after the release gate neither **halted** (`_run_release_gate` returns
  early) nor **paused** (`_finalize_release_verdict` returns early), so a
  rejected run is already intercepted upstream. A run that arrives here is
  finished and not rejected — the mono `finished + not-rejected ⇒ deliver`
  rule — so delivery must run.
- `override` is null-safe:
  `override = ctx.cfa_outcome is not None and ctx.cfa_outcome.outcome ==
  "override_continue"`. On the policy-skip path (`cfa_outcome is None`) it
  is `False`; on the approved path it carries the real CFA outcome, exactly
  as before.

`_run_delivery_and_finalize` stays a thin sequencer: the decision lives in
the helper, not in an inline condition.

### What does not change

- The **rejected-halt** on the release-gate path
  (`if not ctx.release_skipped_by_policy and _finalize_release_verdict(...)`)
  is untouched — a rejected verdict without override still halts before
  delivery.
- The release-gate policy resolution (`_run_release_gate`, `cfa_gate_
  policy`, profile `cross_gates`) is untouched.
- The per-alias verdict logic inside `run_cross_delivery`
  (`_child_verdict` / `_override_session`) is untouched — it already skips
  non-successful aliases and lifts non-APPROVED verdicts only under
  `override`.
- The mono delivery path and the per-child `project_alias` early-return
  are untouched.

### Wire / evidence shape

No wire-format change. `session["phases"]["cross_delivery"]` is still
`CrossDeliveryResult.to_evidence()` — `overall: str`, per-alias
`delivery_status` map, `disabled_by_config: bool`. Only the **population
condition** changes: on a policy-disabled cross run the `cross_delivery`
phase is now **present** and filled with per-alias statuses, where before
the phase was absent. Consumers reading cross-run evidence (for example
the MCP surfacing layer) must handle `cross_delivery` being populated on
gate-less runs; the fields themselves are unchanged.

The finalizer already accommodates this: `_decide_status`
(`pipeline.cross_project.finalization`) overlays the delivery aggregate on
a base `done` decision regardless of whether CFA ran, so a
policy-skipped-but-delivered run finalizes `done` on `ok`/`disabled` and
surfaces `partial` / `failed` / `halted` delivery outcomes.

## Consequences

- Gate-less cross profiles now deliver alias diffs into the operator's
  checkouts on a clean run, closing the "APPROVED/finished ships nothing
  while changes exist" gap for the policy-skip path.
- Delivery failures on gate-less runs are now surfaced (via the existing
  `partial` / `failed` overlay) instead of silently swallowed.
- Cross and mono now share one delivery-intent rule: *finished and not
  rejected ⇒ deliver*, independent of whether a release gate is
  configured.
