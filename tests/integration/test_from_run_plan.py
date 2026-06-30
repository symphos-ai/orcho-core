"""End-to-end ``--from-run-plan`` flow.

PR4 (over-run follow-up plan §1 MVP):

1. Run a ``plan``-shaped profile to produce ``parsed_plan.json`` in
   the parent run directory.
2. Spawn a child run with ``from_run_plan_parent_dir`` pointing at the
   parent. The child uses an ``implement``-only-style profile.
3. Assert the child run:

   * inherits ``state.parsed_plan`` from the parent;
   * does NOT re-run ``plan`` or ``validate_plan`` (the projection
     stripped the leading planning block);
   * reaches downstream phases (``implement`` / ``final_acceptance``)
     with the parent's plan already loaded;
   * stamps ``plan_source="run"`` + ``plan_source_run_id`` so
     evidence / dashboards can correlate child → parent.

These tests exercise the seam ``run_pipeline(from_run_plan_parent_dir=...)``
directly (no CLI subprocess) so the assertions can read child session
dicts in-memory. The CLI plumbing is covered by
``tests/unit/cli/test_from_run_plan_cli.py``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from agents.runtimes import MockAgentProvider
from pipeline.plan_artifacts import (
    LATEST_FILENAME,
    load_parsed_plan_artifact,
)
from pipeline.plugins import PluginConfig
from pipeline.project.state_setup import build_pipeline_state
from pipeline.project_orchestrator import run_pipeline

# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def project_dir(tmp_path: Path) -> str:
    """Override the global ``project_dir`` fixture: this test exercises
    the real engine including worktree isolation, which requires a
    proper git repo with HEAD."""
    from tests.conftest import init_git_repo
    project = tmp_path / "project_dir"
    init_git_repo(project)
    return str(project)


# ── helpers ─────────────────────────────────────────────────────────────────


def _patches():
    """Isolate the project orchestrator from the host environment.

    Identical to ``tests/acceptance/test_full_mock_flow._patches`` —
    the integration test shares that pattern so a host without a git
    project / without ``load_plugin`` configured still runs the mock
    pipeline cleanly.
    """
    return (
        patch(
            "pipeline.project.session_run.load_plugin",
            return_value=PluginConfig(),
        ),
        patch(
            "core.io.git_helpers.has_uncommitted",
            return_value=True,
        ),
        patch(
            "core.io.git_helpers.git_diff_stat",
            return_value="1 file changed",
        ),
    )


def _run_parent_plan(
    project: Path, run_dir: Path,
) -> tuple[dict, Path]:
    """Drive a ``plan``-shaped run that persists ``parsed_plan.json``.

    Returns the parent session dict and the parent run directory
    (it's the same directory passed in — returned for clarity at
    call sites).
    """
    provider = MockAgentProvider(latency=0.0, test_pass_rate=1.0)
    lp, hu, gd = _patches()
    with lp, hu, gd:
        # ``plan`` profile produces a plan and pauses on the
        # human_feedback_always handoff — for the integration test we
        # want the file persisted but do not care about the pause.
        session = run_pipeline(
            task="Add structured logging",
            project_dir=str(project),
            output_dir=run_dir,
            profile_name="planning",
            provider=provider,
        )
    return session, run_dir


def _run_child_from_parent(
    project: Path,
    child_run_dir: Path,
    parent_run_dir: Path,
    *,
    profile_name: str = "feature",
    max_rounds: int = 1,
) -> dict:
    """Drive a child run that hydrates from the parent's parsed plan."""
    provider = MockAgentProvider(latency=0.0, test_pass_rate=1.0)
    lp, hu, gd = _patches()
    with lp, hu, gd:
        return run_pipeline(
            task="Add structured logging",
            project_dir=str(project),
            output_dir=child_run_dir,
            profile_name=profile_name,
            provider=provider,
            from_run_plan_parent_dir=parent_run_dir,
            max_rounds=max_rounds,
        )


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def parent_with_plan(
    project_dir: str, tmp_path: Path,
) -> tuple[dict, Path]:
    """Spin up a parent run that owns ``parsed_plan.json``."""
    parent_dir = tmp_path / "parent_run"
    parent_dir.mkdir()
    session, run_dir = _run_parent_plan(Path(project_dir), parent_dir)
    return session, run_dir


# ── tests ────────────────────────────────────────────────────────────────────


class TestParentProducesArtifact:
    """Pre-condition for PR4: PR2 must have shipped — a plan-only run
    persists ``parsed_plan.json`` in its run dir. If this test fails,
    the failure is in PR2 / PR2.5 territory, not PR4."""

    def test_parent_run_persists_parsed_plan_json(
        self,
        parent_with_plan: tuple[dict, Path],
    ) -> None:
        _session, parent_dir = parent_with_plan
        latest = parent_dir / LATEST_FILENAME
        assert latest.is_file(), (
            "PR2 invariant broken: plan profile must persist "
            f"{LATEST_FILENAME}"
        )
        # Loadable + schema-valid.
        plan = load_parsed_plan_artifact(parent_dir)
        assert plan.subtasks, "parent plan must have at least one subtask"


class TestChildHydration:
    """Once a parent run owns ``parsed_plan.json``, a child run
    started via ``from_run_plan_parent_dir`` must inherit that plan
    AND skip its own planning phases."""

    def test_child_skips_plan_and_validate_plan_phases(
        self,
        project_dir: str,
        tmp_path: Path,
        parent_with_plan: tuple[dict, Path],
    ) -> None:
        _parent_session, parent_dir = parent_with_plan
        child_dir = tmp_path / "child_run"
        child_dir.mkdir()

        child_session = _run_child_from_parent(
            Path(project_dir), child_dir, parent_dir,
        )

        # The projection stripped the leading plan + validate_plan
        # block, so neither phase appears in the child session.
        assert "plan" not in child_session["phases"], (
            "child run must not re-run the plan phase — projection "
            "should have stripped the planning block"
        )
        assert "validate_plan" not in child_session["phases"], (
            "child run must not re-run validate_plan — projection "
            "should have stripped the leading validate_plan step"
        )

    def test_child_starts_at_implement_phase(
        self,
        project_dir: str,
        tmp_path: Path,
        parent_with_plan: tuple[dict, Path],
    ) -> None:
        _parent_session, parent_dir = parent_with_plan
        child_dir = tmp_path / "child_run_impl"
        child_dir.mkdir()

        child_session = _run_child_from_parent(
            Path(project_dir), child_dir, parent_dir,
        )

        # The first non-skipped phase the child runs must be implement
        # (the first phase after the stripped planning block in the
        # shipped ``advanced`` profile).
        assert "implement" in child_session["phases"], (
            "child run should reach implement with the parent's plan "
            "already hydrated"
        )

    def test_child_stamps_plan_source_run_metadata(
        self,
        project_dir: str,
        tmp_path: Path,
        parent_with_plan: tuple[dict, Path],
    ) -> None:
        """The child session must record ``plan_source="run"`` and
        ``plan_source_run_id`` so evidence / dashboards can correlate
        child → parent without rescanning the disk."""
        _parent_session, parent_dir = parent_with_plan
        child_dir = tmp_path / "child_run_meta"
        child_dir.mkdir()

        _child_session = _run_child_from_parent(
            Path(project_dir), child_dir, parent_dir,
        )

        # Meta on disk is the authoritative record (the in-memory
        # session dict mirrors it but may filter fields).
        meta_path = child_dir / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta.get("plan_source") == "run"
        assert meta.get("plan_source_run_id") == parent_dir.name

    def test_direct_from_run_plan_ignores_missing_parent_checkout(
        self,
        project_dir: str,
        tmp_path: Path,
        parent_with_plan: tuple[dict, Path],
    ) -> None:
        """Direct ``--from-run-plan`` uses the parent run for its plan, not
        worktree continuity. CLI/MCP also stamp the parent run dir for lineage;
        that must not force attachment to a missing parent checkout."""
        _parent_session, parent_dir = parent_with_plan
        missing_parent_checkout = tmp_path / "missing_parent_checkout"
        parent_meta_path = parent_dir / "meta.json"
        parent_meta = json.loads(parent_meta_path.read_text(encoding="utf-8"))
        parent_meta["worktree"] = {
            "isolation": "per_run",
            "path": str(missing_parent_checkout),
            "base_ref": "deadbeef",
        }
        parent_meta_path.write_text(
            json.dumps(parent_meta) + "\n", encoding="utf-8",
        )

        child_dir = tmp_path / "child_direct_from_plan_missing_parent"
        child_dir.mkdir()
        provider = MockAgentProvider(latency=0.0, test_pass_rate=1.0)
        lp, hu, gd = _patches()
        with lp, hu, gd:
            child_session = run_pipeline(
                task="Add structured logging",
                project_dir=project_dir,
                output_dir=child_dir,
                profile_name="feature",
                provider=provider,
                from_run_plan_parent_dir=parent_dir,
                followup_parent_run_id=parent_dir.name,
                followup_parent_run_dir=str(parent_dir),
                max_rounds=1,
            )

        assert "implement" in child_session["phases"]
        child_wt = child_session["worktree"]
        assert child_wt.get("isolation") == "per_run"
        assert child_wt["path"] != str(missing_parent_checkout)
        assert child_wt["path"] != str(Path(project_dir))


class TestRunStartEventOrder:
    """``run.start`` event must carry the projected profile name
    (``advanced#from_run_plan``) and ``plan_source="run"``.

    Regression: PR4 originally applied ``--from-run-plan`` profile
    projection AFTER ``_resolve_run_id_and_setup_logging`` emitted
    ``run.start`` — so meta.json saw the projected name but the event
    trace recorded the requested name. PR4.5 reorders projection
    upstream so both surfaces converge.
    """

    def test_run_start_event_carries_projected_profile_and_plan_source(
        self,
        project_dir: str,
        tmp_path: Path,
        parent_with_plan: tuple[dict, Path],
    ) -> None:
        _parent_session, parent_dir = parent_with_plan
        child_dir = tmp_path / "child_event_order"
        child_dir.mkdir()
        _run_child_from_parent(
            Path(project_dir), child_dir, parent_dir,
        )

        events_path = child_dir / "events.jsonl"
        assert events_path.is_file()
        all_events = [
            json.loads(line)
            for line in events_path.read_text().splitlines()
            if line.strip()
        ]
        run_start_events = [
            evt for evt in all_events if evt.get("kind") == "run.start"
        ]
        assert len(run_start_events) == 1, (
            "expected exactly one run.start event in child run, got "
            f"{len(run_start_events)} (kinds: "
            f"{sorted({e.get('kind') for e in all_events})!r})"
        )
        payload = run_start_events[0].get("payload") or {}
        # The projection ran upstream of the emit: both fields land
        # on the event payload, not just on meta.json.
        assert payload.get("plan_source") == "run", (
            "run.start must carry plan_source='run' on a "
            f"--from-run-plan child run, got payload={payload!r}"
        )
        assert payload.get("projected_profile") == (
            "feature#from_run_plan"
        ), (
            "run.start must carry the projected profile name, got "
            f"payload={payload!r}"
        )
        # ``profile`` stays the requested name so consumers can
        # distinguish "what was asked for" from "what actually ran".
        assert payload.get("profile") == "feature"


class TestPlanningOnlyProfileRefused:
    """A profile that consists entirely of a planning block has
    nothing left to run after projection — refuse loud BEFORE the
    run dir is materialized so we never leave half-created
    artefacts behind."""

    def test_planning_only_profile_raises_before_run_dir_artefacts(
        self,
        project_dir: str,
        tmp_path: Path,
        parent_with_plan: tuple[dict, Path],
    ) -> None:
        from pipeline.runtime.profile import LoopStep, Profile
        from pipeline.runtime.roles import ProfileKind
        from pipeline.runtime.steps import PhaseStep

        _parent_session, parent_dir = parent_with_plan
        planning_only = Profile(
            name="plan_only_local",
            kind=ProfileKind.SCOPED,
            variant="plan",
            steps=(
                LoopStep(
                    steps=(
                        PhaseStep(phase="plan"),
                        PhaseStep(phase="validate_plan"),
                    ),
                    until="validate_plan.approved",
                    max_rounds=2,
                ),
            ),
        )
        child_dir = tmp_path / "child_refused"
        child_dir.mkdir()
        provider = MockAgentProvider(latency=0.0, test_pass_rate=1.0)
        lp, hu, gd = _patches()
        with lp, hu, gd, pytest.raises(ValueError) as exc:
            run_pipeline(
                task="Add structured logging",
                project_dir=project_dir,
                output_dir=child_dir,
                profile_obj=planning_only,
                provider=provider,
                from_run_plan_parent_dir=parent_dir,
                max_rounds=1,
            )
        assert "consists entirely of planning phases" in str(exc.value)
        # The refusal fired upstream of ``_resolve_run_id_and_setup_logging``:
        # no meta.json was materialized in the child dir.
        assert not (child_dir / "meta.json").exists(), (
            "planning-only refusal must fire BEFORE the run dir is "
            "materialized; a half-created meta.json indicates the "
            "projection ran too late"
        )


class TestHardFailWithoutParsedPlanArtifact:
    """If the parent run has no ``parsed_plan.json`` (pre-PR2 runs,
    failed planning), the resolver / loader must fail loud BEFORE
    any agent IO. ``run_pipeline`` surfaces the
    :class:`ParsedPlanArtifactError` so the operator can see the
    actionable diagnostic."""

    def test_parent_dir_without_parsed_plan_json_raises(
        self,
        project_dir: str,
        tmp_path: Path,
    ) -> None:
        from pipeline.plan_artifacts import ParsedPlanArtifactError

        # Bare parent dir — no parsed_plan.json.
        bare_parent = tmp_path / "bare_parent"
        bare_parent.mkdir()
        child_dir = tmp_path / "child_after_bare"
        child_dir.mkdir()
        provider = MockAgentProvider(latency=0.0, test_pass_rate=1.0)
        lp, hu, gd = _patches()
        with lp, hu, gd, pytest.raises(ParsedPlanArtifactError):
            run_pipeline(
                task="Add structured logging",
                project_dir=project_dir,
                output_dir=child_dir,
                profile_name="feature",
                provider=provider,
                from_run_plan_parent_dir=bare_parent,
                max_rounds=1,
            )


# ── plan-only follow-up promotion (no explicit --from-run-plan) ──────────────


def _run_followup_no_flag(
    project: Path,
    child_run_dir: Path,
    parent_run_dir: Path,
    *,
    profile_name: str = "feature",
) -> tuple[dict, Any]:
    """Drive a PLAIN follow-up (``resume_mode='followup'``, no flag).

    This exercises the promotion chokepoint (T2): a follow-up whose parent
    left a ``parsed_plan.json`` and carries no undelivered diff must be
    promoted into a ``--from-run-plan`` continuation. Returns the child
    session AND the captured :class:`PipelineState` so callers can assert
    ``state.parsed_plan`` hydration directly (the session dict does not
    surface the parsed plan).
    """
    provider = MockAgentProvider(latency=0.0, test_pass_rate=1.0)
    captured: dict[str, Any] = {}
    real_build = build_pipeline_state

    def _spy(inputs):
        setup = real_build(inputs)
        captured["state"] = setup.state
        return setup

    lp, hu, gd = _patches()
    with lp, hu, gd, patch(
        "pipeline.project.session_run.build_pipeline_state", _spy,
    ):
        session = run_pipeline(
            task="Add structured logging",
            project_dir=str(project),
            output_dir=child_run_dir,
            profile_name=profile_name,
            provider=provider,
            resume_mode="followup",
            followup_parent_run_id=parent_run_dir.name,
            followup_parent_run_dir=str(parent_run_dir),
            max_rounds=1,
        )
    return session, captured["state"]


class TestPlanOnlyFollowupPromotion:
    """A plain follow-up (no ``--from-run-plan``) from a planning parent
    that left a ``parsed_plan.json`` and no undelivered diff is promoted to
    a plan-artifact continuation: the planning block is stripped, a fresh
    worktree is allocated for the child profile, and ``state.parsed_plan``
    is hydrated from the parent artifact."""

    def test_plan_only_followup_strips_planning_starts_implement_fresh_worktree(
        self,
        project_dir: str,
        tmp_path: Path,
        parent_with_plan: tuple[dict, Path],
    ) -> None:
        parent_session, parent_dir = parent_with_plan
        child_dir = tmp_path / "child_followup_promote"
        child_dir.mkdir()

        child_session, state = _run_followup_no_flag(
            Path(project_dir), child_dir, parent_dir,
        )

        # (а) leading plan/validate_plan stripped → execution starts at
        # implement, just like an explicit --from-run-plan child.
        assert "plan" not in child_session["phases"], (
            "promoted follow-up must not re-run plan — the planning block "
            "should have been stripped"
        )
        assert "validate_plan" not in child_session["phases"]
        assert "implement" in child_session["phases"], (
            "promoted follow-up should reach implement with the parent's "
            "plan already hydrated"
        )

        # (б) state.parsed_plan hydrated FROM the parent artifact.
        parent_plan = load_parsed_plan_artifact(parent_dir)
        assert state.parsed_plan is not None, (
            "state.parsed_plan must be hydrated on a promoted plan-only "
            "follow-up"
        )
        assert [s.id for s in state.parsed_plan.subtasks] == [
            s.id for s in parent_plan.subtasks
        ]
        assert state.parsed_plan.goal == parent_plan.goal
        assert state.extras.get("plan_artifact_continuation") is True
        assert "plan" in state.extras.get("from_run_plan_stripped_phases", [])

        # The existing --from-run-plan correlation metadata is reused (no
        # second hydration path): plan_source=run + plan_source_run_id.
        meta = json.loads((child_dir / "meta.json").read_text(encoding="utf-8"))
        assert meta.get("plan_source") == "run"
        assert meta.get("plan_source_run_id") == parent_dir.name

        # (в) a FRESH per-profile worktree was allocated for the child — NOT
        # the planning parent's off-isolation source checkout.
        child_wt = child_session["worktree"]
        assert child_wt.get("isolation") == "per_run", (
            "the feature child must get an isolated per_run worktree"
        )
        assert child_wt["path"] != str(Path(project_dir)), (
            "child worktree must not be the shared source checkout"
        )
        assert child_wt["path"] != parent_session["worktree"]["path"], (
            "child worktree must be fresh, not the parent worktree"
        )

        # (г) continuity metadata explains the plan-artifact start.
        continuity = child_wt["followup_continuity"]
        assert continuity["diff_source"] == "plan_artifact"
        assert continuity["blocked"] is False
        assert "plan artifact continuation" in continuity["mode_label"]


class TestUndeliveredDiffContinuityPreserved:
    """Regression: the T2 plan-artifact signal must NOT weaken strict
    undelivered-diff continuity. A parent carrying an undelivered diff keeps
    its prior block / reuse behavior even when a plan artifact is also
    present (the diff branches win first)."""

    def _write_parent_meta(
        self, parent_run_dir: Path, worktree: dict | None,
    ) -> None:
        parent_run_dir.mkdir(parents=True, exist_ok=True)
        meta: dict = {"id": parent_run_dir.name}
        if worktree is not None:
            meta["worktree"] = worktree
        (parent_run_dir / "meta.json").write_text(
            json.dumps(meta) + "\n", encoding="utf-8",
        )

    def _write_diff_patch(self, parent_run_dir: Path) -> None:
        (parent_run_dir / "diff.patch").write_text(
            "diff --git a/app.txt b/app.txt\n"
            "--- a/app.txt\n+++ b/app.txt\n"
            "@@ -1 +1,2 @@\n base\n+undelivered\n",
            encoding="utf-8",
        )

    def _assert_artifact_block(
        self, parent_dir: Path, child_dir: Path, project_dir: str,
    ) -> None:
        """Run a follow-up and assert it blocks on the artifact diff.

        ``run_pipeline`` runs under the default TERMINAL presentation, so the
        worktree block surfaces as ``print_error`` + ``sys.exit(2)`` (the typed
        ``WorktreeConfigError`` is re-raised only under SILENT). The durable
        proof is the persisted blocked continuity in the child ``meta.json``.
        """
        provider = MockAgentProvider(latency=0.0, test_pass_rate=1.0)
        lp, hu, gd = _patches()
        with lp, hu, gd, pytest.raises(SystemExit) as exc:
            run_pipeline(
                task="Add structured logging",
                project_dir=str(project_dir),
                output_dir=child_dir,
                profile_name="feature",
                provider=provider,
                resume_mode="followup",
                followup_parent_run_id=parent_dir.name,
                followup_parent_run_dir=str(parent_dir),
                max_rounds=1,
            )
        assert exc.value.code == 2
        # Blocked strictly before any write phase: no child checkout, and the
        # blocked classification is persisted to meta.json.
        assert not (child_dir / "checkout").exists()
        meta = json.loads((child_dir / "meta.json").read_text(encoding="utf-8"))
        continuity = meta["worktree"]["followup_continuity"]
        assert continuity["blocked"] is True
        assert continuity["diff_source"] == "artifact"
        assert "diff.patch" in continuity["reason"]

    def test_artifact_only_diff_still_blocks_without_plan(
        self,
        project_dir: str,
        tmp_path: Path,
    ) -> None:
        """Artifact-only undelivered diff, no plan → block (unchanged)."""
        parent_dir = tmp_path / "artifact_parent"
        self._write_parent_meta(parent_dir, worktree=None)
        self._write_diff_patch(parent_dir)
        child_dir = tmp_path / "child_artifact_block"
        child_dir.mkdir()

        self._assert_artifact_block(parent_dir, child_dir, project_dir)

    def test_artifact_diff_with_plan_still_blocks_not_promoted(
        self,
        project_dir: str,
        tmp_path: Path,
        parent_with_plan: tuple[dict, Path],
    ) -> None:
        """Critical T2 regression: a parent with BOTH a persisted plan AND an
        undelivered artifact diff must still BLOCK — the artifact branch wins
        over the plan-artifact promotion, so the change is never silently
        dropped onto a clean HEAD."""
        _parent_session, parent_dir = parent_with_plan
        # The planning parent already owns parsed_plan.json; add an
        # undelivered artifact diff on top and drop the parent worktree block
        # so the only undelivered-diff source is the artifact.
        self._write_diff_patch(parent_dir)
        self._write_parent_meta(parent_dir, worktree=None)
        child_dir = tmp_path / "child_artifact_plan_block"
        child_dir.mkdir()

        self._assert_artifact_block(parent_dir, child_dir, project_dir)

    def test_dirty_isolated_parent_reuses_worktree_not_promoted(
        self,
        project_dir: str,
        tmp_path: Path,
    ) -> None:
        """A dirty ISOLATED parent worktree → reuse, even though the parent
        also owns a plan artifact. The dirty-worktree branch wins over the
        plan-artifact promotion, so the child continues on the parent's
        worktree carrying the undelivered change (continuity preserved)."""
        # A feature parent runs isolated and retains its worktree; the patched
        # ``has_uncommitted`` makes that isolated worktree look dirty.
        parent_dir = tmp_path / "feature_parent"
        parent_dir.mkdir()
        provider = MockAgentProvider(latency=0.0, test_pass_rate=1.0)
        lp, hu, gd = _patches()
        with lp, hu, gd:
            parent_session = run_pipeline(
                task="Add structured logging",
                project_dir=str(project_dir),
                output_dir=parent_dir,
                profile_name="feature",
                provider=provider,
                max_rounds=1,
            )
        parent_wt_path = parent_session["worktree"]["path"]
        assert parent_session["worktree"].get("isolation") == "per_run"

        child_dir = tmp_path / "child_reuse"
        child_dir.mkdir()
        child_session, _state = _run_followup_no_flag(
            Path(project_dir), child_dir, parent_dir,
        )

        child_wt = child_session["worktree"]
        # Reuse, NOT a fresh plan-artifact worktree.
        assert child_wt["path"] == parent_wt_path
        continuity = child_wt["followup_continuity"]
        assert continuity["diff_source"] == "worktree"
        assert continuity["blocked"] is False
        assert "reused parent" in continuity["mode_label"]


class TestContradictoryChildProfileThroughChokepoint:
    """A plan-only follow-up whose child profile has no implement / review
    phases downstream of planning must be blocked at the shared request-assembly
    chokepoint — for every transport — and never run as a false plan-artifact
    continuation with a wrong continuity label."""

    @pytest.mark.parametrize(
        "profile_name", ["planning", "research", "code_review"],
    )
    def test_plan_only_followup_with_contradictory_profile_is_blocked(
        self,
        project_dir: str,
        tmp_path: Path,
        parent_with_plan: tuple[dict, Path],
        profile_name: str,
    ) -> None:
        from pipeline.project.followup_worktree import (
            FollowupPlanContinuationError,
        )

        _parent_session, parent_dir = parent_with_plan
        child_dir = tmp_path / f"child_contradictory_{profile_name}"
        child_dir.mkdir()

        provider = MockAgentProvider(latency=0.0, test_pass_rate=1.0)
        lp, hu, gd = _patches()
        with lp, hu, gd, pytest.raises(FollowupPlanContinuationError) as exc:
            run_pipeline(
                task="Add structured logging",
                project_dir=str(project_dir),
                output_dir=child_dir,
                profile_name=profile_name,
                provider=provider,
                resume_mode="followup",
                followup_parent_run_id=parent_dir.name,
                followup_parent_run_dir=str(parent_dir),
                max_rounds=1,
            )
        assert profile_name in str(exc.value)
        # Blocked before profile setup / run-dir artefacts: no meta.json, and
        # critically no false plan-artifact continuation ran.
        assert not (child_dir / "meta.json").exists()
