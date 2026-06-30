# SPDX-License-Identifier: Apache-2.0
"""Guard tests for discovery-time participant promotion (ADR 0112 §4, increment C).

Pins the increment-C transaction and its phase-agnostic seam against real,
git-backed temp trees (no mocks of the worktree/git layer — so ``realpath``
normalisation, including the macOS ``/var`` → ``/private/var`` symlink, matches
the production path math, exactly as ``test_isolated_source_resolver`` /
``test_isolated_source_provenance_guard`` do). The cases cover the two regressions
the plan calls out by name:

  * **F1** (``worktree_isolation_distinct``) — a promoted participant gets its own
    non-colliding worktree identity (``wt_<alias>``), so its ``editable_checkout``
    differs from both its canonical sibling/delivery target and the primary
    worktree;
  * **F2** (``delivery_coverage_extends``) — promotion writes the discovered repo
    into the REAL delivery surface (``session['auto_detect']['delivery_projects']``),
    so ``collect_sibling_changes`` / ``evaluate_delivery_scope`` observe it.

Plus the heart invariant (``bind_before_verification`` — the live resolver binds
``{dependency:repo}`` to the participant worktree), idempotency, the
phase-agnostic detector + seam, single-participant byte-parity, and an
ADR-0110-unchanged check that the scope-expansion classifier is byte-identical
(its pure functions are untouched by increment C).

Markers: ``git_worktree`` (real linked worktrees) + ``serial`` (shared git /
filesystem state — xdist-unsafe).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pipeline.engine.delivery_scope import (
    collect_sibling_changes,
    evaluate_delivery_scope,
)
from pipeline.engine.scope_expansion import (
    FileScopeSignals,
    build_scope_expansion_assessment,
    build_scope_expansion_signals,
    render_scope_expansion_lines,
)
from pipeline.engine.worktree import resolve_worktree_for_run
from pipeline.participant_promotion import (
    add_participant,
    detect_out_of_set_repos,
    evaluate_scope_expansion_promotion,
)
from pipeline.participants import ParticipantSet
from pipeline.plugins import PluginConfig
from pipeline.project.state_setup import PARTICIPANT_SET_EXTRAS_KEY
from pipeline.verification_contract import VerificationContract

pytestmark = [pytest.mark.git_worktree, pytest.mark.serial]


# ── git-backed setup helpers (mirror test_isolated_source_provenance_guard) ───


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@orcho.invalid"], cwd=repo, check=True,
    )
    subprocess.run(["git", "config", "user.name", "Orcho Test"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True,
    )
    (repo / "f.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def _empty_contract() -> VerificationContract:
    """A minimal declared contract (no deps) — enough to arm the seam guards."""
    return VerificationContract(
        dependency_repos={}, verification_envs={}, commands={}, schedule=(),
        default_env="", required=(), work_mode="",
    )


def _dep_contract(repo: Path, *, name: str = "dep") -> VerificationContract:
    """A contract declaring ``{dependency:name}`` → ``repo`` (the discovered repo)."""
    return VerificationContract(
        dependency_repos={name: {"path": str(repo)}}, verification_envs={},
        commands={}, schedule=(), default_env="", required=(), work_mode="",
    )


def _mode_contract(work_mode: str) -> VerificationContract:
    """A minimal declared contract carrying an explicit ``work_mode``.

    Used by the real-state-assembly tests so ``build_pipeline_state`` projects
    the OperatingMode from the resolved verification ``work_mode`` (the real run
    source) instead of a hand-injected ``extras['operating_mode']``.
    """
    return VerificationContract(
        dependency_repos={}, verification_envs={}, commands={}, schedule=(),
        default_env="", required=(), work_mode=work_mode,
    )


def _build_real_run_state(
    *, primary: Path, run_dir: Path, primary_ctx: Any,
    contract: VerificationContract | None, session: dict[str, Any],
) -> Any:
    """Build a run state through the REAL assembly path (``build_pipeline_state``).

    The OperatingMode is NOT hand-injected: ``build_pipeline_state`` projects it
    once from the resolved ``contract.work_mode`` (the same source a real run
    uses), so the promotion seam reads the run's true posture. Returns the built
    :class:`PipelineState`.
    """
    from agents.protocols import SessionMode
    from pipeline.project.state_setup import StateInputs, build_pipeline_state
    from pipeline.project.types import PresentationPolicy

    inputs = StateInputs(
        task="t", project_path=primary, plugin=PluginConfig(),
        phase_config=None, agent_registry=None, output_dir=run_dir,
        dry_run=False, session=session, session_ts="run_primary",
        git_cwd=str(primary_ctx.path), change_handoff="uncommitted",
        cross_handoff_text="", plan_source="local", handoff_path=None,
        auto_waiver_allowed=False, followup_seed_count=0, ckpt=None,
        attachments=None, session_mode=SessionMode.AUTO,
        implement_model="m", repair_model="m", repair_escalation_model="m",
        chain_same_model_only=False, presentation=PresentationPolicy.SILENT,
        render_phase_outputs=False, from_run_plan_loaded=None,
        followup_parent_run_id=None, from_run_plan_parent_dir=None,
        from_run_plan_stripped=(), verification_contract=contract,
    )
    return build_pipeline_state(inputs).state


def _make_primary_worktree(tmp_path: Path) -> tuple[Path, Path, Any, ParticipantSet]:
    """Materialise a primary repo + its real isolated per-run worktree.

    Returns ``(primary_repo, run_dir, primary_ctx, participant_set)`` with the set
    mono-seeded from the primary worktree (the production state_setup seeding)."""
    primary = tmp_path / "primary"
    _init_repo(primary)
    run_dir = tmp_path / "runs" / "run_primary"
    run_dir.mkdir(parents=True)
    primary_ctx = resolve_worktree_for_run(
        run_id="run_primary",
        project_dir=primary,
        run_dir=run_dir,
        worktree_config={"enabled": True, "isolation": "per_run"},
    )
    assert primary_ctx.mode == "per_run"
    pset = ParticipantSet.for_mono(
        checkout=str(primary_ctx.path), project=str(primary),
        worktree=primary_ctx.to_dict(),
    )
    return primary, run_dir, primary_ctx, pset


def _make_run(
    *, primary: Path, run_dir: Path, primary_ctx: Any, pset: ParticipantSet,
    contract: VerificationContract | None,
    delivery_projects: list[str] | None = None,
    dry_run: bool = False,
) -> SimpleNamespace:
    """A light stub of ``_PipelineRun`` carrying only what promotion reads."""
    extras: dict[str, Any] = {
        "run_id": "run_primary",
        "git_cwd": str(primary_ctx.path),
        PARTICIPANT_SET_EXTRAS_KEY: pset,
    }
    if contract is not None:
        extras["verification_contract"] = contract
    state = SimpleNamespace(
        project_dir=str(primary), output_dir=run_dir, parsed_plan=None,
        plugin=None, dry_run=dry_run, extras=extras,
    )
    session: dict[str, Any] = {}
    if delivery_projects is not None:
        session["auto_detect"] = {"delivery_projects": list(delivery_projects)}
    return SimpleNamespace(
        state=state, session=session, session_ts="run_primary",
        worktree_context=primary_ctx, _in_gate_hook=False,
    )


def _worktree_dirs(run_dir: Path) -> set[str]:
    root = run_dir / "worktrees"
    if not root.is_dir():
        return set()
    return {p.name for p in root.iterdir() if p.is_dir()}


# ── idempotent_add ────────────────────────────────────────────────────────────


def test_idempotent_add(tmp_path: Path) -> None:
    primary, run_dir, primary_ctx, pset = _make_primary_worktree(tmp_path)
    sibling = tmp_path / "sibling"
    _init_repo(sibling)
    run = _make_run(
        primary=primary, run_dir=run_dir, primary_ctx=primary_ctx, pset=pset,
        contract=_empty_contract(),
    )

    part = add_participant(run, str(sibling))
    assert part is not None
    assert part.is_bound and part.editable_checkout
    assert pset.get(str(sibling)) is part
    assert len(pset) == 2
    wt_after_first = _worktree_dirs(run_dir)

    # Repeat for the same repo: early no-op — same participant object, no second
    # worktree directory created, set unchanged.
    part_again = add_participant(run, str(sibling))
    assert part_again is part
    assert len(pset) == 2
    assert _worktree_dirs(run_dir) == wt_after_first


# ── worktree_isolation_distinct (F1) ─────────────────────────────────────────


def test_worktree_isolation_distinct(tmp_path: Path) -> None:
    primary, run_dir, primary_ctx, pset = _make_primary_worktree(tmp_path)
    sibling = tmp_path / "sibling"
    _init_repo(sibling)
    run = _make_run(
        primary=primary, run_dir=run_dir, primary_ctx=primary_ctx, pset=pset,
        contract=_empty_contract(),
    )

    part = add_participant(run, str(sibling))
    assert part is not None
    editable = Path(part.editable_checkout).resolve()

    # Distinct from the canonical sibling / delivery target (no isolation loss).
    assert editable != sibling.resolve()
    assert Path(part.delivery_target).resolve() == sibling.resolve()
    assert editable != Path(part.delivery_target).resolve()
    # Distinct from the primary worktree (no path collision).
    assert editable != Path(primary_ctx.path).resolve()
    # Separate identity: the checkout lives under a ``wt_<alias>`` directory, NOT
    # the primary's ``wt_run_primary`` (the F1 collision the fix prevents).
    assert f"wt_{part.alias}" in editable.parts
    assert "wt_run_primary" not in editable.parts
    assert part.isolation == "per_run"


# ── bind_before_verification (heart invariant) ───────────────────────────────


def test_bind_before_verification(tmp_path: Path) -> None:
    primary, run_dir, primary_ctx, pset = _make_primary_worktree(tmp_path)
    sibling = tmp_path / "sibling"
    _init_repo(sibling)
    contract = _dep_contract(sibling, name="dep")
    run = _make_run(
        primary=primary, run_dir=run_dir, primary_ctx=primary_ctx, pset=pset,
        contract=contract,
    )

    # Before promotion the snapshot is absent: the sibling is not yet a
    # participant, so nothing has bound {dependency:dep} to a worktree.
    assert run.state.extras.get("verification_placeholders") is None

    part = add_participant(run, str(sibling))
    assert part is not None

    # The REAL snapshot the gates/autorun read (state.extras['verification_
    # placeholders'], refreshed in-place by add_participant) now binds
    # {dependency:dep} to the participant's worktree — never the canonical sibling
    # (the ADR 0112 §3 false-green). Asserting on the live snapshot, not a freshly
    # built context, pins _refresh_resolver_snapshot: break it and this fails.
    snapshot = run.state.extras["verification_placeholders"]
    resolved = Path(snapshot.dependencies["dep"]).resolve()
    assert resolved == Path(part.editable_checkout).resolve()
    assert resolved != sibling.resolve()


# ── dirty_changes_seeded_into_verification_subject (review follow-up F1) ──────


def test_dirty_changes_seeded_into_verification_subject(tmp_path: Path) -> None:
    """The discovered dirty diff must ride into the isolated worktree.

    The detector only fires on a dirty sibling, but the promoted worktree is built
    from ``HEAD`` and is clean. Without transferring the diff, verification binds to
    a pristine tree and passes vacuously while the changes that triggered promotion
    stay only in the canonical checkout (the review F1 blocker). This dirties the
    sibling — a tracked edit AND an untracked file — before promotion and asserts
    BOTH land in the verification-bound ``editable_checkout``.
    """
    primary, run_dir, primary_ctx, pset = _make_primary_worktree(tmp_path)
    sibling = tmp_path / "sibling"
    _init_repo(sibling)
    # Dirty the canonical sibling: a tracked edit + a brand-new untracked file.
    (sibling / "f.txt").write_text("base\nsibling-change\n", encoding="utf-8")
    (sibling / "untracked.txt").write_text("new-file\n", encoding="utf-8")
    contract = _dep_contract(sibling, name="dep")
    run = _make_run(
        primary=primary, run_dir=run_dir, primary_ctx=primary_ctx, pset=pset,
        contract=contract,
    )

    part = add_participant(run, str(sibling))
    assert part is not None
    editable = Path(part.editable_checkout).resolve()

    # The isolated worktree is distinct from the canonical sibling (F1 isolation)…
    assert editable != sibling.resolve()
    # …yet it now carries the discovered tracked edit AND the untracked file —
    # the verification subject is NOT a pristine HEAD tree.
    assert (editable / "f.txt").read_text(encoding="utf-8") == "base\nsibling-change\n"
    assert (editable / "untracked.txt").is_file()
    assert (editable / "untracked.txt").read_text(encoding="utf-8") == "new-file\n"

    # The live resolver binds {dependency:dep} to that same dirty worktree, so the
    # next gate verifies the discovered changes rather than a clean source.
    snapshot = run.state.extras["verification_placeholders"]
    bound = Path(snapshot.dependencies["dep"]).resolve()
    assert bound == editable
    assert (bound / "f.txt").read_text(encoding="utf-8") == "base\nsibling-change\n"


# ── delivery_coverage_extends (F2) ───────────────────────────────────────────


def test_delivery_coverage_extends(tmp_path: Path) -> None:
    primary, run_dir, primary_ctx, pset = _make_primary_worktree(tmp_path)
    sibling = tmp_path / "sibling"
    _init_repo(sibling)
    # The sibling carries a dirty change so the delivery surface can observe it.
    (sibling / "f.txt").write_text("base\nsibling-change\n", encoding="utf-8")
    run = _make_run(
        primary=primary, run_dir=run_dir, primary_ctx=primary_ctx, pset=pset,
        contract=_empty_contract(), delivery_projects=[],
    )

    # Before promotion: the sibling is not in the delivery surface, so
    # collect_sibling_changes cannot see it.
    before = collect_sibling_changes(
        delivery_projects=list(run.session["auto_detect"]["delivery_projects"]),
        primary_project_dir=primary,
        workspace=tmp_path,
    )
    assert not any(str(sibling) in alias for alias in before)

    add_participant(run, str(sibling))

    # Promotion appended the discovered repo to the REAL delivery surface.
    projects = run.session["auto_detect"]["delivery_projects"]
    assert any(Path(p).resolve() == sibling.resolve() for p in projects)

    # …which collect_sibling_changes / evaluate_delivery_scope now observe.
    after = collect_sibling_changes(
        delivery_projects=list(projects),
        primary_project_dir=primary,
        workspace=tmp_path,
    )
    assert after, "delivery surface did not include the promoted sibling"
    flat = [p for paths in after.values() for p in paths]
    assert any(p.endswith("/f.txt") for p in flat), flat  # the dirty file is collected

    run.session.setdefault("auto_detect", {})["delivery_scope"] = "expanded_mono"
    assessment = evaluate_delivery_scope(
        session=run.session, primary_project_dir=primary, run_dir=run_dir,
        workspace=tmp_path,
    )
    assert assessment is not None
    # The promoted sibling actually rides into the final ADR 0107 disclosure /
    # affected-project set — not merely a non-None assessment. Break delivery
    # coverage extension and these fail (the dirty file vanishes from disclosure
    # and the repo from affected_projects).
    assert any(p.endswith("/f.txt") for p in assessment.disclosure), (
        assessment.disclosure
    )
    assert any(
        Path(a).resolve() == sibling.resolve() for a in assessment.affected_projects
    ), assessment.affected_projects


# ── detector_fires_any_phase ─────────────────────────────────────────────────


@pytest.mark.parametrize("phase", ["implement", "review_changes", "final_acceptance"])
def test_detector_fires_any_phase(tmp_path: Path, phase: str) -> None:
    primary, run_dir, primary_ctx, pset = _make_primary_worktree(tmp_path)
    sibling = tmp_path / "sibling"
    _init_repo(sibling)
    (sibling / "f.txt").write_text("base\nsibling-change\n", encoding="utf-8")
    run = _make_run(
        primary=primary, run_dir=run_dir, primary_ctx=primary_ctx, pset=pset,
        contract=_empty_contract(), delivery_projects=[str(sibling)],
    )

    # The detector surfaces the dirty out-of-set sibling, regardless of phase.
    detected = detect_out_of_set_repos(run)
    assert any(Path(r).resolve() == sibling.resolve() for r in detected)
    assert len(pset) == 1, "detection must not mutate the set"

    # The seam actually promotes it (before the next verification for this phase).
    evaluate_scope_expansion_promotion(run, phase)
    assert pset.get(str(sibling)) is not None
    assert len(pset) == 2
    # Now in-set → no longer detected.
    assert not detect_out_of_set_repos(run)


# ── governed participant-add routes to phase-handoff (ADR 0112 §5, increment D) ─


def _participant_add_handoff_id(repo: str) -> str:
    from pipeline.runtime.handoff import (
        SCOPE_EXPANSION_HANDOFF_PHASE,
        scope_expansion_participant_add_trigger,
    )

    return (
        f"{SCOPE_EXPANSION_HANDOFF_PHASE}:"
        f"{scope_expansion_participant_add_trigger(repo)}:1"
    )


def _seed_participant_add_decision(run_dir: Path, repo: str, *, action: str) -> None:
    import json

    from sdk.phase_handoff import safe_handoff_id

    handoff_id = _participant_add_handoff_id(repo)
    decisions = run_dir / "phase_handoff_decisions"
    decisions.mkdir(parents=True, exist_ok=True)
    (decisions / f"{safe_handoff_id(handoff_id)}.json").write_text(
        json.dumps({
            "run_id": run_dir.name,
            "handoff_id": handoff_id,
            "phase": "final_acceptance",
            "action": action,
            "decided_at": "2026-06-29T12:00:00+00:00",
        }),
        encoding="utf-8",
    )


def test_governed_participant_add_routes_to_handoff(tmp_path: Path) -> None:
    # ADR 0112 §5: governed mode routes any participant-add through phase-handoff
    # for operator sanction BEFORE promoting — the seam raises the
    # scope_expansion:participant_add:<repo> signal and does not grow the set.
    import os

    primary, run_dir, primary_ctx, pset = _make_primary_worktree(tmp_path)
    sibling = tmp_path / "sibling"
    _init_repo(sibling)
    (sibling / "f.txt").write_text("base\nsibling-change\n", encoding="utf-8")
    run = _make_run(
        primary=primary, run_dir=run_dir, primary_ctx=primary_ctx, pset=pset,
        contract=_empty_contract(), delivery_projects=[str(sibling)],
    )
    run.state.extras["operating_mode"] = "governed"

    evaluate_scope_expansion_promotion(run, "implement")

    # Pause raised, set NOT yet promoted.
    signal = run.state.phase_handoff_request
    assert signal is not None
    repo_real = os.path.realpath(str(sibling))
    assert signal.trigger == f"scope_expansion:participant_add:{repo_real}"
    assert signal.phase == "final_acceptance"
    assert len(pset) == 1, "governed must not promote before operator sanction"

    # After an operator decision, the resumed seam promotes (no re-pause).
    run.state.phase_handoff_request = None
    _seed_participant_add_decision(run_dir, repo_real, action="continue")
    evaluate_scope_expansion_promotion(run, "implement")
    assert run.state.phase_handoff_request is None
    assert pset.get(str(sibling)) is not None
    assert len(pset) == 2


@pytest.mark.parametrize("mode", ["fast", "pro"])
def test_non_governed_participant_add_promotes_without_handoff(
    tmp_path: Path, mode: str,
) -> None:
    # fast / pro keep the increment-C default: promote (record → continue), no
    # phase-handoff pause for a participant-add.
    primary, run_dir, primary_ctx, pset = _make_primary_worktree(tmp_path)
    sibling = tmp_path / "sibling"
    _init_repo(sibling)
    (sibling / "f.txt").write_text("base\nsibling-change\n", encoding="utf-8")
    run = _make_run(
        primary=primary, run_dir=run_dir, primary_ctx=primary_ctx, pset=pset,
        contract=_empty_contract(), delivery_projects=[str(sibling)],
    )
    run.state.extras["operating_mode"] = mode

    evaluate_scope_expansion_promotion(run, "implement")

    assert getattr(run.state, "phase_handoff_request", None) is None
    assert pset.get(str(sibling)) is not None
    assert len(pset) == 2


# ── F1: OperatingMode comes from REAL state assembly, not hand-injected extras ─
#
# The unit tests above set ``extras['operating_mode']`` directly. F1 (review): a
# real run never populated that key — both sanction sites fell back to FAST, so a
# ``governed`` run silently promoted instead of routing to phase-handoff. These
# pin the projected source: ``build_pipeline_state`` stamps the OperatingMode
# ONCE from the resolved ``verification_contract.work_mode``, and the SAME single
# reader drives the route. No ``extras['operating_mode']`` is set by hand here.


def test_real_state_assembly_projects_operating_mode_from_work_mode(
    tmp_path: Path,
) -> None:
    # The projection source, isolated: a governed contract → a governed posture
    # on the built state, read identically by both sanction sites' single reader
    # (run_shape.operating_mode_from_state, aliased by session_keys'
    # _operating_mode_for_state). No hand-injected extras['operating_mode'].
    from pipeline.phases.builtin.session_keys import _operating_mode_for_state
    from pipeline.runtime.run_shape import OperatingMode, operating_mode_from_state

    primary, run_dir, primary_ctx, _pset = _make_primary_worktree(tmp_path)
    state = _build_real_run_state(
        primary=primary, run_dir=run_dir, primary_ctx=primary_ctx,
        contract=_mode_contract("governed"), session={},
    )

    # The stamp was projected by run-state assembly, not injected by the test.
    assert state.extras["operating_mode"] == "governed"
    assert operating_mode_from_state(state) is OperatingMode.GOVERNED
    # The final_acceptance read site resolves the SAME posture.
    assert _operating_mode_for_state(state) is OperatingMode.GOVERNED

    # ``pro`` projects identically (the pro blocker → handoff route source).
    pro_state = _build_real_run_state(
        primary=primary, run_dir=run_dir, primary_ctx=primary_ctx,
        contract=_mode_contract("pro"), session={},
    )
    assert pro_state.extras["operating_mode"] == "pro"
    assert _operating_mode_for_state(pro_state) is OperatingMode.PRO


def test_real_state_assembly_falls_back_to_auto_detect_then_fast(
    tmp_path: Path,
) -> None:
    # With no verification contract, the projection falls back to the auto-detect
    # resolved ``actual_mode``; with neither, it defaults to the conservative
    # ``fast`` posture. Neither path hand-injects extras['operating_mode'].
    from pipeline.runtime.run_shape import OperatingMode, operating_mode_from_state

    primary, run_dir, primary_ctx, _pset = _make_primary_worktree(tmp_path)

    auto_state = _build_real_run_state(
        primary=primary, run_dir=run_dir, primary_ctx=primary_ctx,
        contract=None, session={"auto_detect": {"actual_mode": "governed"}},
    )
    assert auto_state.extras["operating_mode"] == "governed"
    assert operating_mode_from_state(auto_state) is OperatingMode.GOVERNED

    default_state = _build_real_run_state(
        primary=primary, run_dir=run_dir, primary_ctx=primary_ctx,
        contract=None, session={},
    )
    assert default_state.extras["operating_mode"] == "fast"
    assert operating_mode_from_state(default_state) is OperatingMode.FAST


def test_governed_route_fires_from_real_state_assembly(tmp_path: Path) -> None:
    # End-to-end: a governed run assembled the real way (no hand-injected
    # operating_mode) routes a discovered out-of-set repo to the
    # scope_expansion:participant_add:<repo> phase-handoff instead of promoting
    # it silently — the F1 regression (governed silently degraded to fast).
    import os

    primary, run_dir, primary_ctx, _pset = _make_primary_worktree(tmp_path)
    sibling = tmp_path / "sibling"
    _init_repo(sibling)
    (sibling / "f.txt").write_text("base\nsibling-change\n", encoding="utf-8")
    session: dict[str, Any] = {
        "auto_detect": {"delivery_projects": [str(sibling)]},
    }
    state = _build_real_run_state(
        primary=primary, run_dir=run_dir, primary_ctx=primary_ctx,
        contract=_mode_contract("governed"), session=session,
    )
    pset = state.extras[PARTICIPANT_SET_EXTRAS_KEY]
    run = SimpleNamespace(
        state=state, session=session, session_ts="run_primary",
        worktree_context=primary_ctx, _in_gate_hook=False,
    )

    evaluate_scope_expansion_promotion(run, "implement")

    signal = run.state.phase_handoff_request
    assert signal is not None, "governed route did not fire from the real posture"
    repo_real = os.path.realpath(str(sibling))
    assert signal.trigger == f"scope_expansion:participant_add:{repo_real}"
    assert len(pset) == 1, "governed must not promote before operator sanction"


def test_fast_route_promotes_from_real_state_assembly(tmp_path: Path) -> None:
    # Contrast: a fast run assembled the real way promotes the discovered repo
    # (record → continue) with no phase-handoff pause — proving the projected
    # posture, not a FAST fallback, is what distinguishes the routes.
    primary, run_dir, primary_ctx, _pset = _make_primary_worktree(tmp_path)
    sibling = tmp_path / "sibling"
    _init_repo(sibling)
    (sibling / "f.txt").write_text("base\nsibling-change\n", encoding="utf-8")
    session: dict[str, Any] = {
        "auto_detect": {"delivery_projects": [str(sibling)]},
    }
    state = _build_real_run_state(
        primary=primary, run_dir=run_dir, primary_ctx=primary_ctx,
        contract=_mode_contract("fast"), session=session,
    )
    pset = state.extras[PARTICIPANT_SET_EXTRAS_KEY]
    run = SimpleNamespace(
        state=state, session=session, session_ts="run_primary",
        worktree_context=primary_ctx, _in_gate_hook=False,
    )

    evaluate_scope_expansion_promotion(run, "implement")

    assert getattr(run.state, "phase_handoff_request", None) is None
    assert pset.get(str(sibling)) is not None
    assert len(pset) == 2


# ── promotion_runs_before_pre_phase_autorun (ordering invariant) ─────────────


@pytest.mark.parametrize(
    ("phase", "profile_name"),
    [
        ("final_acceptance", "feature"),   # pre-final auto-run path
        ("review_changes", "correction"),  # correction pre-review auto-run path
    ],
)
def test_promotion_runs_before_pre_phase_autorun(
    monkeypatch: pytest.MonkeyPatch, phase: str, profile_name: str,
) -> None:
    """``_on_phase_pre`` must promote out-of-set repos BEFORE its pre-phase
    required-receipt auto-runs.

    The correction pre-review and pre-final auto-runs materialize receipts over
    the current ``verification_placeholders`` / participant set; if they fire
    before promotion, a just-dirtied out-of-set sibling escapes this phase's
    verification (the F1 regression this follow-up closes). This pins the call
    order at the seam: break it (move promotion back below the auto-runs) and
    this fails.
    """
    import pipeline.participant_promotion as promo
    import pipeline.project.gate_repair as gate_repair
    import pipeline.project.run as run_mod

    calls: list[str] = []
    monkeypatch.setattr(
        promo, "evaluate_scope_expansion_promotion",
        lambda *a, **k: calls.append("promotion"),
    )
    monkeypatch.setattr(
        run_mod, "_auto_run_required_receipts_live",
        lambda *a, **k: calls.append("autorun"),
    )
    # Stub the remaining pre-phase seam work so the stub ``self`` is enough.
    monkeypatch.setattr(
        gate_repair, "evaluate_isolated_source_preflight", lambda *a, **k: None,
    )
    monkeypatch.setattr(gate_repair, "evaluate_pre_phase_gates", lambda *a, **k: None)
    monkeypatch.setattr(run_mod, "_print_scheduled_gate_live_blocks", lambda *a, **k: None)

    st = SimpleNamespace(phase_log={}, extras={}, halt=False)
    this = SimpleNamespace(profile_name=profile_name)

    run_mod._PipelineRun._on_phase_pre(this, phase, st)

    assert "promotion" in calls and "autorun" in calls
    assert calls.index("promotion") < calls.index("autorun"), calls


# ── single_participant_parity ────────────────────────────────────────────────


def test_single_participant_parity(tmp_path: Path) -> None:
    primary, run_dir, primary_ctx, pset = _make_primary_worktree(tmp_path)
    # Only the primary worktree changes; no sibling is tracked.
    (Path(primary_ctx.path) / "f.txt").write_text("base\nprimary\n", encoding="utf-8")
    run = _make_run(
        primary=primary, run_dir=run_dir, primary_ctx=primary_ctx, pset=pset,
        contract=_empty_contract(), delivery_projects=[],
    )

    # Detector empty; the set, the resolver snapshot, and delivery_projects are
    # byte-identical before/after the seam (no add_participant call).
    set_len_before = len(pset)
    placeholders_before = run.state.extras.get("verification_placeholders")
    delivery_before = list(run.session["auto_detect"]["delivery_projects"])
    worktrees_before = _worktree_dirs(run_dir)

    assert detect_out_of_set_repos(run) == set()
    evaluate_scope_expansion_promotion(run, "implement")

    assert len(pset) == set_len_before == 1
    assert run.state.extras.get("verification_placeholders") is placeholders_before
    assert run.session["auto_detect"]["delivery_projects"] == delivery_before
    assert _worktree_dirs(run_dir) == worktrees_before


# ── adr_0110_unchanged ───────────────────────────────────────────────────────


def _signals_fixture() -> list[FileScopeSignals]:
    """A fixed set of out-of-plan signals (a public-wire change + an other file)."""
    return list(
        build_scope_expansion_signals(
            changed_files=["sdk/contract.py", "pipeline/util.py"],
            in_plan_patterns=["pipeline/util.py"],  # second file is in-plan → skipped
            diff_stats_by_file={"sdk/contract.py": {"added": 80, "removed": 2}},
            gate_status_by_category={},
            explained_files=(),
            repeated_paths=(),
            sdk_flags_by_file={},
        )
    )


def test_adr_0110_unchanged() -> None:
    """The scope-expansion classifier (ADR 0110) is byte-identical under increment
    C: its pure functions yield the same classification + durable render for the
    same signals, and promotion never touches them."""
    signals = _signals_fixture()
    # Only out-of-plan files are signalled (the in-plan file is dropped).
    assert [s.path for s in signals] == ["sdk/contract.py"]

    first = build_scope_expansion_assessment(signals)
    second = build_scope_expansion_assessment(_signals_fixture())
    # Deterministic classification (pure function of signals).
    assert first.to_dict() == second.to_dict()
    # Durable render is stable and reads only the dict (no re-classification).
    assert render_scope_expansion_lines(first.to_dict()) == render_scope_expansion_lines(
        second.to_dict(),
    )

    # Increment C's module does not import or mutate the classifier surface.
    import pipeline.participant_promotion as promo

    source = Path(promo.__file__).read_text(encoding="utf-8")
    assert "build_scope_expansion_assessment" not in source
    assert "classify_file_signals" not in source
