# SPDX-License-Identifier: Apache-2.0
"""
pipeline/participant_promotion.py — discovery-time participant promotion
(ADR 0112 §4, increment C).

When an out-of-set repo is discovered mid-run (a sibling repo the run started
touching that the participant set does not yet know about), it must be promoted
into the run as a first-class :class:`pipeline.participants.Participant` *before*
the next verification or delivery step — otherwise its diff lives in a checkout
the gates never look at (the false-green ADR 0112 §3 calls out) or never reaches
delivery scope (ADR 0107). This module owns that single, idempotent transaction.

Split from the pure-domain substrate
-------------------------------------
:mod:`pipeline.participants` is **pure domain** (stdlib + ``worktree_source``,
no git / FS I/O, no cross-project import — enforced by
``test_module_does_not_import_cross_project``). All the I/O of promotion lives
HERE instead: worktree creation, the run-scoped session / state mutation, and the
resolver-snapshot refresh. The substrate only gains a pure registration method
(:meth:`ParticipantSet.add_participant`) this module drives.

The four steps of :func:`add_participant`
-----------------------------------------
1. **Non-colliding worktree identity (fix F1).** A stable alias is derived from
   the discovered repo (``basename`` + a short realpath hash — there is no
   plan-alias for an out-of-set repo). The participant's worktree is created with
   ``run_id=<alias>`` (so its path is ``wt_<alias>``, distinct from the primary's
   ``wt_<primary_run_id>``) and ``branch_run_id=f"{primary_run_id}__{alias}"`` (so
   its ``orcho/run/*`` branch is unique in the shared source-repo ref namespace).
   Inheriting the run's ``run_id`` would collide on the primary worktree path and
   silently degrade into the canonical checkout. The discovered repo is **dirty**
   (the detector only fires on uncommitted sibling changes), but the fresh worktree
   is built from ``HEAD`` and is therefore CLEAN — so the discovered ``git diff
   --binary HEAD`` + untracked files are replayed into the worktree via the
   canonical pre-run dirty intake (ADR 0044). Without this the gate would verify a
   pristine tree and pass vacuously while the changes that triggered promotion
   remain only in the canonical checkout; a failed transfer is a loud halt, never a
   silent clean verification.
2. **Registration.** A bound :class:`Participant` is built from the resolved
   worktree and registered in the run-scoped set via
   :meth:`ParticipantSet.add_participant`.
3. **Verification coverage (live resolver).** The run's
   ``state.extras['verification_placeholders']`` snapshot is rebuilt from the
   mutated set (:func:`...state_setup.refresh_verification_placeholders`) so the
   next gate resolves ``{dependency:repo}`` to the participant's worktree, not the
   stale pre-promotion snapshot.
4. **Delivery coverage (real surface).** The discovered repo is appended to
   ``session['auto_detect']['delivery_projects']`` — the durable list
   :func:`pipeline.engine.delivery_scope.collect_sibling_changes` /
   :func:`...evaluate_delivery_scope` read (ADR 0107) — so delivery scope includes
   the participant. The repo's absolute path is appended (resolvable directly by
   :func:`...project_aliases.resolve_project_alias`, which accepts an existing path
   as well as a registered alias).

NOT touched, by design
----------------------
Per-participant **receipts** (ADR 0084) and **allowed_modifications**
(ADR 0087) are deliberately left alone: the verification-receipt index is
per-run (``verification_receipt_index``), and ``allowed_modifications`` comes from
the plugin config — neither is a per-participant promotion concern in increment C.
The promotion sanction is the default *record → re-setup → continue*; the
RunShape sanction / mode matrix is increment D, out of scope here.

Idempotency
-----------
The whole transaction is idempotent on the realpath identity of ``repo``: a repeat
promotion of an already-present repo is an early no-op — no second worktree is
created, the resolver snapshot and ``delivery_projects`` are not duplicated.

Discovery-time detection + seam (T2)
------------------------------------
:func:`detect_out_of_set_repos` is the phase-agnostic, mode-neutral detector: it
generalizes the ``scope_expansion`` change-detection inputs (``git_changed_files``
on the checkout, ``derive_in_plan_patterns``, and ``collect_sibling_changes`` as
the candidate source) to surface repos the run is acting on that the participant
set does not yet cover — without re-implementing the classifier.
:func:`evaluate_scope_expansion_promotion` is the thin seam ``pipeline.project.run``
calls from its per-phase pre/end hooks (modelled on
``gate_repair.evaluate_isolated_source_preflight``): a strict no-op under dry-run /
no-contract / waiver / re-entrant gate hook, else it promotes each detected repo
via :func:`add_participant` BEFORE the next verification (the default
*record → re-setup → continue* sanction — no mode matrix / handoff, which is
increment D).
"""
from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core.io.git_helpers import git_changed_files
from pipeline.engine.pre_run_dirty import (
    apply_pre_run_dirty_seed,
    resolve_pre_run_dirty_intake,
)
from pipeline.engine.worktree import (
    WorktreeConfigError,
    WorktreeContext,
    resolve_worktree_for_run,
)
from pipeline.participants import Participant
from pipeline.project.state_setup import (
    PARTICIPANT_SET_EXTRAS_KEY,
    refresh_verification_placeholders,
)
from pipeline.runtime.run_shape import OperatingMode, operating_mode_from_state

# ``state.extras`` key carrying an active operator phase-handoff waiver — the
# explicit human ship decision. Mirrors ``scope_expansion_support`` so the
# promotion seam respects the SAME waiver gate the scope-expansion classifier
# does (it is the human's decision to proceed despite open scope).
_PHASE_HANDOFF_WAIVER_KEY = "phase_handoff_waiver"


def add_participant(run: Any, repo: str, *, base_ref: str = "") -> Participant | None:
    """Promote out-of-set ``repo`` into ``run`` as a bound participant (ADR 0112 §4).

    Runs the four steps documented at module level and returns the registered
    :class:`Participant`. Idempotent on the realpath identity of ``repo``: if the
    repo is already a participant the existing entry is returned early and no
    worktree is created. Returns ``None`` only when the run carries no
    participant set (degenerate test fixtures) — nothing to promote into.
    """
    pset = run.state.extras.get(PARTICIPANT_SET_EXTRAS_KEY)
    if pset is None:
        return None

    # Idempotency: an already-present repo (realpath identity) is an early no-op —
    # no second worktree, no duplicated snapshot / delivery entry.
    existing = pset.get(repo)
    if existing is not None:
        return existing

    alias = _stable_alias(repo)
    ctx = _promote_worktree(run, repo, alias)  # step 1 (fix F1)
    participant = _build_participant(repo, alias, ctx, base_ref)  # step 2
    pset.add_participant(participant)

    _refresh_resolver_snapshot(run, pset)  # step 3 (live verification resolver)
    _extend_delivery_coverage(run, repo)   # step 4 (real delivery surface)
    return participant


def _stable_alias(repo: str) -> str:
    """Derive a stable, collision-resistant alias for an out-of-set repo.

    There is no plan-alias for a discovered repo, so the alias is its directory
    basename plus a short hash of its realpath. The basename keeps the alias (and
    thus the ``wt_<alias>`` worktree path / ``orcho/run/*`` branch) human-readable;
    the hash disambiguates two repos that share a basename. Stable across repeated
    promotion of the same repo (pure function of the realpath).
    """
    real = os.path.realpath(repo)
    base = os.path.basename(real.rstrip(os.sep)) or "repo"
    digest = hashlib.sha1(real.encode("utf-8")).hexdigest()[:8]  # noqa: S324 — identity, not security
    return f"{base}__{digest}"


def _isolation_inputs(run: Any) -> tuple[dict[str, Any], str | None, bool]:
    """Mirror the run's worktree isolation regime for the promoted participant.

    Returns ``(worktree_config, profile_isolation, isolation_requested)``. The
    promoted participant gets the SAME regime as the primary so a per_run run
    isolates the new repo too; an off / single-checkout run promotes it in-place.
    """
    wc = getattr(run, "worktree_context", None)
    primary_mode = getattr(wc, "mode", "off") if wc is not None else "off"
    if primary_mode == "off":
        return {"enabled": False}, None, False
    return {"enabled": True, "isolation": primary_mode}, primary_mode, True


def _promote_worktree(run: Any, repo: str, alias: str) -> WorktreeContext:
    """Step 1 — resolve a NON-colliding isolated worktree for the participant.

    The worktree identity is decoupled from the run's ``run_id`` to avoid the F1
    collision: ``run_id=<alias>`` lands the checkout at ``wt_<alias>`` (distinct
    from the primary ``wt_<primary_run_id>``) and ``branch_run_id`` keeps the
    ``orcho/run/*`` branch unique in the shared source-repo ref namespace.

    GUARD (fix F1): when isolation is requested and the worktree is NOT degraded
    yet its checkout collapsed onto the canonical sibling / delivery target, that
    is a silent isolation loss — running edits/verification there would diff a
    clean source tree. Fail loudly (mirrors the cross loud-degrade in
    ``isolation_setup``). A legitimately off / degraded worktree is allowed to
    collapse ``editable_checkout`` onto its delivery target (degraded contract §2).

    When the worktree is genuinely isolated (``ctx.path`` differs from the
    canonical repo) the discovered dirty diff is replayed into it (see
    :func:`_seed_discovered_changes`) so verification covers the changes that
    triggered promotion rather than a pristine ``HEAD`` tree.
    """
    primary_run_id = str(
        run.state.extras.get("run_id") or getattr(run, "session_ts", "") or "",
    )
    run_dir = run.state.output_dir or Path(repo)
    worktree_config, profile_isolation, requested = _isolation_inputs(run)
    branch_run_id = f"{primary_run_id}__{alias}" if primary_run_id else alias
    ctx = resolve_worktree_for_run(
        run_id=alias,
        project_dir=Path(repo),
        run_dir=Path(run_dir),
        worktree_config=worktree_config,
        profile_isolation=profile_isolation,
        branch_run_id=branch_run_id,
    )
    if (
        requested
        and ctx.degraded_reason is None
        and ctx.mode != "off"
        and _same_real(str(ctx.path), repo)
    ):
        raise WorktreeConfigError(
            f"promoted participant [{alias}] worktree isolation was requested and "
            f"not degraded, yet its checkout resolved to the canonical source "
            f"{repo} instead of an isolated worktree. Refusing to edit/verify the "
            f"user's tree — a clean source would pass verification vacuously. Most "
            f"common cause: a leftover orcho/run/* branch or worktree at "
            f"wt_{alias}; prune it (git worktree remove / git branch -D) and rerun."
        )
    if not _same_real(str(ctx.path), repo):
        # Genuinely isolated worktree (clean from HEAD): transfer the discovered
        # dirty changes so the verification subject is not pristine (review F1).
        _seed_discovered_changes(
            repo, alias, ctx, worktree_config, profile_isolation, Path(run_dir),
        )
    return ctx


def _seed_discovered_changes(
    repo: str,
    alias: str,
    ctx: WorktreeContext,
    worktree_config: dict[str, Any],
    profile_isolation: str | None,
    run_dir: Path,
) -> None:
    """Replay the canonical repo's discovered dirty diff into the isolated worktree.

    The detector only fires on a DIRTY out-of-set sibling
    (``collect_sibling_changes`` runs ``git status``), yet the freshly-resolved
    worktree is built from the repo's ``HEAD`` and is therefore CLEAN. Binding
    verification to it without the diff verifies a pristine tree and passes
    vacuously — the ADR 0112 §3 false-green this follow-up closes. Reuse the
    canonical pre-run dirty intake (ADR 0044): snapshot the repo's ``git diff
    --binary HEAD`` + untracked files into a participant-scoped seed dir
    (``<run_dir>/promoted/<alias>`` — never the primary's ``pre_run_dirty`` dir) and
    replay them into the worktree, so the verification subject carries exactly the
    changes that triggered promotion.

    A clean repo yields a non-``include`` intake (nothing to seed → early return). A
    transfer that cannot apply cleanly is a loud halt (mirrors the F1 guard): a
    clean worktree must never silently stand in for the discovered changes.
    """
    seed_dir = run_dir / "promoted" / alias
    intake = resolve_pre_run_dirty_intake(
        project_dir=Path(repo),
        run_dir=seed_dir,
        run_id=alias,
        pre_run_config={
            "non_interactive_default": "include",
            "include_untracked": "all",
        },
        worktree_config=worktree_config,
        profile_isolation=profile_isolation,
        resume_from=None,
        no_interactive=True,
    )
    if intake.action != "include":
        return
    applied = apply_pre_run_dirty_seed(
        intake, project_dir=Path(repo), worktree_path=Path(ctx.path),
    )
    if applied.status != "seeded":
        raise WorktreeConfigError(
            f"promoted participant [{alias}] could not seed the discovered "
            f"uncommitted changes from {repo} into its isolated worktree "
            f"({applied.status}: {applied.error}). Refusing to bind verification to "
            f"a clean worktree that would pass vacuously while the changes that "
            f"triggered promotion remain only in the canonical checkout."
        )


def _build_participant(
    repo: str, alias: str, ctx: WorktreeContext, base_ref: str,
) -> Participant:
    """Step 2 — build the bound participant from the resolved worktree.

    ``editable_checkout`` is the worktree checkout under live isolation, or the
    repo itself when off / degraded (``ctx.path == repo``, the degraded contract).
    ``isolation`` records the worktree's own mode so the set resolves THIS repo by
    its own regime — a degraded ``off`` participant inside a per_run set collapses
    (no spurious fail-closed), while an isolated one redirects to its worktree.
    """
    return Participant(
        alias=alias,
        repo=repo,
        editable_checkout=str(ctx.path),
        base_ref=base_ref,
        delivery_target=repo,
        isolation=ctx.mode,
    )


def _refresh_resolver_snapshot(run: Any, participant_set: Any) -> None:
    """Step 3 — rebuild the verification placeholder snapshot from the mutated set.

    Without this, the next gate resolves ``{dependency:repo}`` against the snapshot
    taken before the repo joined the set (stale → canonical sibling). No-op when no
    verification contract is declared.
    """
    refresh_verification_placeholders(
        run.state,
        project_path=Path(run.state.project_dir),
        output_dir=run.state.output_dir,
        checkout=str(run.state.extras.get("git_cwd") or ""),
        participant_set=participant_set,
    )


def _extend_delivery_coverage(run: Any, repo: str) -> None:
    """Step 4 — append the discovered repo to ``delivery_projects`` (ADR 0107).

    The delivery owner (:func:`...evaluate_delivery_scope` /
    :func:`...collect_sibling_changes`) reads ``session['auto_detect']
    ['delivery_projects']`` — a list of alias / path strings — NOT the participant
    set, so adding to the set alone never extends delivery coverage. The repo's
    absolute path is appended (``resolve_project_alias`` accepts an existing path
    directly). The strict/expanded delivery POLICY is untouched (that is the
    increment-D sanction). Receipts (ADR 0084, per-run ``verification_receipt_index``)
    and ``allowed_modifications`` (ADR 0087, from the plugin config) are NOT
    per-participant and stay untouched here by design.
    """
    session = getattr(run, "session", None)
    if not isinstance(session, dict):
        return
    auto = session.setdefault("auto_detect", {})
    if not isinstance(auto, dict):
        return
    entry = os.path.abspath(repo)
    projects = list(auto.get("delivery_projects") or [])
    if any(isinstance(p, str) and _same_real(p, entry) for p in projects):
        return
    projects.append(entry)
    auto["delivery_projects"] = projects


def _same_real(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` name the same location (realpath-normalised)."""
    if not a or not b:
        return False
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except OSError:
        return a == b


# ── Discovery-time detection + seam (ADR 0112 §4, increment C / T2) ───────────


def detect_out_of_set_repos(run: Any) -> set[str]:
    """Return realpaths of repos the run is acting on that are NOT yet participants.

    Phase-agnostic and mode-neutral: it reads only the working tree and the
    durable plan, never the phase identity or a RunShape projection. It does NOT
    re-implement the ``scope_expansion`` classifier — it *generalizes* that
    classifier's three change-detection inputs across the whole participant set:

    * :func:`core.io.git_helpers.git_changed_files` on the run **checkout** — the
      run worktree's own changed files (the phase-agnostic "work happened"
      signal);
    * :func:`pipeline.engine.scope_expansion.derive_in_plan_patterns` — the
      declared in-plan scope, used to tell whether that work expanded beyond plan;
    * :func:`pipeline.engine.delivery_scope.collect_sibling_changes` — the **real
      delivery surface** and the candidate source: sibling repos
      ``session['auto_detect']['delivery_projects']`` already tracks that carry
      dirty changes (it runs ``git_changed_files`` per sibling internally).

    A candidate sibling repo is *out-of-set* when it is dirty and the run-scoped
    :class:`pipeline.participants.ParticipantSet` does not already cover it
    (realpath identity — the same key the resolver reads). Empty when every change
    is within an existing participant (the membership filter), and empty for a
    single-participant run with no tracked siblings.
    """
    pset = run.state.extras.get(PARTICIPANT_SET_EXTRAS_KEY)
    if pset is None:
        return set()
    state = run.state
    primary_dir = Path(str(getattr(state, "project_dir", "") or ""))
    workspace = _resolve_delivery_workspace(primary_dir)
    checkout = str(state.extras.get("git_cwd") or primary_dir)

    # Phase-agnostic change/scope context — generalizes the scope_expansion
    # classifier's inputs (reused, never duplicated): the run's own changed files
    # classified against the declared in-plan scope. Used to bound the sibling
    # scan; the per-repo membership filter below is authoritative.
    run_changed = _safe_changed_files(checkout)
    in_plan = _in_plan_patterns(run)
    run_expanded = _run_expanded_scope(run_changed, in_plan)

    sibling_changes = _collect_sibling_changes(run, primary_dir, workspace)
    if not sibling_changes and not run_expanded:
        return set()

    out: set[str] = set()
    for alias in sibling_changes:
        repo = _resolve_alias_path(alias, workspace)
        if repo is None:
            continue
        repo_real = os.path.realpath(str(repo))
        if pset.get(repo_real) is None:  # dirty sibling not yet a participant
            out.add(repo_real)
    return out


def evaluate_scope_expansion_promotion(run: Any, phase: str) -> None:
    """``on_phase_pre`` / ``on_phase_end`` seam: promote out-of-set repos BEFORE
    the next verification step (ADR 0112 §4 increment C; §5 increment D).

    Modelled on ``gate_repair.evaluate_isolated_source_preflight``: a strict no-op
    under dry-run, without a verification contract or output dir, when an operator
    waiver is active (the human ship decision the scope-expansion classifier also
    respects), and — crucially — when ``run._in_gate_hook`` is set, so the gate's
    own ``repair_changes`` re-firing of the phase callbacks never re-enters
    promotion.

    Increment D (mode projection): in ``governed`` mode the operating-mode
    matrix routes *any* participant-add through phase-handoff for operator
    sanction (ADR 0112 §5) rather than promoting silently. The first
    not-yet-decided out-of-set repo raises a
    ``scope_expansion:participant_add:<repo>`` signal on
    ``state.phase_handoff_request`` and the seam returns, so the runner breaks
    and the orchestrator pause tail persists ``meta.phase_handoff`` +
    ``awaiting_phase_handoff``. Once the operator records a decision (durable
    decision artifact), the resumed run falls through to promotion. In
    ``fast`` / ``pro`` the default *record → re-setup → continue* promotion is
    unchanged. ``phase`` is accepted for call-site symmetry but NOT gated on:
    promotion is phase-agnostic so the next verification in ANY phase sees the
    participant.
    """
    del phase  # phase-agnostic seam — accepted for symmetry, intentionally unused
    if getattr(run, "_in_gate_hook", False):
        return
    state = getattr(run, "state", None)
    if state is None:
        return
    if getattr(state, "dry_run", False):
        return
    if getattr(state, "output_dir", None) is None:
        return
    if state.extras.get("verification_contract") is None:
        return
    if _operator_waiver_active(state):
        return
    governed = operating_mode_from_state(state) is OperatingMode.GOVERNED
    for repo in sorted(detect_out_of_set_repos(run)):
        if governed and not _participant_add_decided(run, repo):
            # ADR 0112 §5: governed routes the participant-add to phase-handoff
            # for operator sanction before promoting. Raise the pause for the
            # first undecided repo and stop — a single operator sanction at a
            # time; the resumed run (decision artifact present) promotes instead.
            _raise_participant_add_handoff(run, repo)
            return
        add_participant(run, repo)


def _participant_add_handoff_id(repo: str) -> str:
    """Stable handoff id for the participant-add pause of ``repo``."""
    from pipeline.runtime.handoff import (
        SCOPE_EXPANSION_HANDOFF_PHASE,
        scope_expansion_participant_add_trigger,
    )

    trigger = scope_expansion_participant_add_trigger(repo)
    return f"{SCOPE_EXPANSION_HANDOFF_PHASE}:{trigger}:1"


def _participant_add_decided(run: Any, repo: str) -> bool:
    """True when a durable phase-handoff decision already exists for ``repo``.

    The promotion seam fires on every phase pre/end hook; without this check a
    governed participant-add would re-raise the same pause forever. A recorded
    operator decision (any action) lets the resumed run fall through to
    promotion. Soft-degrades to ``False`` (raise the pause) when the run dir or
    decision artifact cannot be read.
    """
    run_dir = getattr(getattr(run, "state", None), "output_dir", None)
    if run_dir is None:
        return False
    from sdk.phase_handoff import safe_handoff_id

    decision_path = (
        Path(run_dir)
        / "phase_handoff_decisions"
        / f"{safe_handoff_id(_participant_add_handoff_id(repo))}.json"
    )
    return decision_path.exists()


def _raise_participant_add_handoff(run: Any, repo: str) -> Any:
    """Raise the governed participant-add pause on ``state.phase_handoff_request``.

    Builds the ``scope_expansion:participant_add:<repo>`` signal (the same ADR
    0038 lifecycle the out-of-plan handoff rides) and sets it on the run state so
    the runner breaks and the orchestrator pause tail persists it. Never clobbers
    an already-pending request.
    """
    if getattr(run.state, "phase_handoff_request", None) is not None:
        return None
    from pipeline.runtime.handoff import (
        build_scope_expansion_handoff_signal,
        scope_expansion_participant_add_trigger,
    )

    trigger = scope_expansion_participant_add_trigger(repo)
    signal = build_scope_expansion_handoff_signal(
        trigger=trigger,
        artifacts={
            "operating_mode": OperatingMode.GOVERNED.value,
            "participant_repo": repo,
        },
        last_output=(
            f"Out-of-set repository '{repo}' was discovered mid-run; governed "
            "mode routes the participant-add to phase-handoff for operator "
            "sanction before promoting it into the run."
        ),
    )
    if signal is not None:
        run.state.phase_handoff_request = signal
    return signal


def _operator_waiver_active(state: Any) -> bool:
    """True when an active operator phase-handoff waiver carries a verdict.

    Byte-identical predicate to ``scope_expansion_support._operator_waiver_active``
    so the promotion seam gates on the SAME human ship decision; replicated (3
    lines) rather than imported to avoid a private cross-module dependency.
    """
    waiver = state.extras.get(_PHASE_HANDOFF_WAIVER_KEY)
    return isinstance(waiver, Mapping) and bool(
        str(waiver.get("waiver_text") or "").strip(),
    )


def _safe_changed_files(checkout: str) -> list[str]:
    """Run-worktree changed files, never raising (empty for a clean / non-repo)."""
    if not checkout:
        return []
    try:
        return [f for f in git_changed_files(checkout) if f]
    except Exception:  # noqa: BLE001 — detection must never crash a phase hook
        return []


def _in_plan_patterns(run: Any) -> tuple[str, ...]:
    """Declared in-plan globs (durable plan + project allowed_modifications).

    Mirrors ``scope_expansion_support._in_plan_patterns`` — loads the durable
    parsed plan (falling back to the in-memory one) and folds it through
    :func:`...scope_expansion.derive_in_plan_patterns`.
    """
    from pipeline.engine.scope_expansion import derive_in_plan_patterns

    state = run.state
    output_dir = getattr(state, "output_dir", None)
    plan: Any = getattr(state, "parsed_plan", None)
    if output_dir is not None:
        try:
            from pipeline.plan_artifacts import load_parsed_plan_artifact

            plan = load_parsed_plan_artifact(Path(output_dir))
        except Exception:  # noqa: BLE001 — fall back to the in-memory plan
            plan = getattr(state, "parsed_plan", None)
    project_allowed = getattr(
        getattr(state, "plugin", None), "allowed_modifications", None,
    )
    return derive_in_plan_patterns(plan, project_allowed or ())


def _run_expanded_scope(run_changed: list[str], in_plan: tuple[str, ...]) -> bool:
    """True when the run touched a file outside the declared in-plan scope.

    Reuses the classifier's path matcher (:func:`...scope_expansion._path_matches`)
    rather than duplicating it. The signal only widens the sibling scan; it never
    classifies or writes durable scope-expansion evidence (that stays the
    classifier's job).
    """
    if not run_changed:
        return False
    from pipeline.engine.scope_expansion import _path_matches

    return any(not _path_matches(f, in_plan) for f in run_changed)


def _collect_sibling_changes(
    run: Any, primary_dir: Path, workspace: str | Path | None,
) -> Mapping[str, tuple[str, ...]]:
    """Dirty sibling repos from the real delivery surface (never raises).

    Reuses :func:`pipeline.engine.delivery_scope.collect_sibling_changes` over the
    durable ``session['auto_detect']['delivery_projects']`` candidate list.
    """
    from pipeline.engine.delivery_scope import collect_sibling_changes

    session = getattr(run, "session", None)
    auto = session.get("auto_detect") if isinstance(session, dict) else None
    projects = [
        p for p in ((auto or {}).get("delivery_projects") or [])
        if isinstance(p, str) and p
    ]
    if not projects:
        return {}
    try:
        return collect_sibling_changes(
            delivery_projects=projects,
            primary_project_dir=primary_dir,
            workspace=workspace,
        )
    except Exception:  # noqa: BLE001 — detection must never crash a phase hook
        return {}


def _resolve_delivery_workspace(primary_dir: Path) -> str | Path | None:
    """Workspace dir delivery uses for alias resolution (reuses delivery_scope)."""
    from pipeline.engine.delivery_scope import _resolve_workspace

    return _resolve_workspace(primary_dir, None)


def _resolve_alias_path(alias: str, workspace: str | Path | None) -> Path | None:
    """Resolve a delivery alias / path to a repo path (reuses delivery_scope)."""
    from pipeline.engine.delivery_scope import _resolve_alias_path as _resolve

    return _resolve(alias, workspace)


__all__ = [
    "add_participant",
    "detect_out_of_set_repos",
    "evaluate_scope_expansion_promotion",
]
