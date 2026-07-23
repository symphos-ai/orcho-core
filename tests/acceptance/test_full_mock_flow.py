"""
tests/acceptance/test_full_mock_flow.py

Acceptance tests for the complete multi-agent pipeline using MockAgentProvider.
Zero subprocess / zero API calls — instant execution, deterministic.

Coverage:
  A. Single-project pipeline (run_pipeline):
     A1. Full flow: research → plan → build → clean review → QA
     A2. Fix round triggered by Codex issue
     A3. Max rounds respected (no infinite loop)
     A4. profile_name="task" bypasses plan and validation
     A5. dry_run=True — all phases present, zero agent subprocess calls
     A6. Session JSON structure and required keys
     A7. Progress log written with all phase markers
     A8. output.log created alongside session

  B. Cross-project pipeline (run_cross_pipeline):
     B1. Full flow across 3 projects — all sub-pipelines run
     B2. cross_plan.md written to output_dir
     B3. CONTRACT CHECK phase runs
     B4. per-project subdirs and meta.json written
     B6. Task propagated to all sub-pipelines

  C. Plan Human Review (PipelineMode.PLAN → human approval → --resume):
     C1. PLAN mode exits after validate_plan — no build/review phases
     C2. Session stores plan output for human inspection
     C3. meta.json written (checkpoint for --resume)
     C4. validate_plan critique accessible in session
     C5. Plan file written to artifacts dir
     C6. Progress log marks PLAN-only completion (no BUILD markers)
     C7. Session status field = 'awaiting_phase_handoff' (Phase 5 cutover: plan profile uses human_feedback_always handoff)
     C8. Two-phase resume: PLAN → human approve → TASK continues from build
     C9. Two-phase resume: PLAN → human corrections → replan → approve → TASK
     C10. Corrections text injected into replan prompt
     C11. Cross-project PLAN mode exits before per-project pipelines
"""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import core.observability.logging as _logging_module
from agents.runtimes import MockAgentProvider
from pipeline.plugins import PluginConfig
from pipeline.project_orchestrator import run_pipeline

# ─────────────────────────────────────────────────────────────────────────────
# Module-level autouse: reset core.logging globals between every test
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_logging_globals():
    """Prevent progress_log / agent_log / event-store state leaking between tests.

    ``init_event_store`` resets ``_path`` / ``_seq`` / ``_phase`` per run but
    NOT the phase-context globals (``_phase_key`` / ``_round`` / ``_phase_title``)
    — those are owned by ``set_phase_context`` / ``clear_phase_context``. Under
    randomized test ordering a test that left phase context set could leak a
    canonical ``phase_kind`` into the NEXT run's event emission (e.g. tagging the
    cross release gate onto the validate-plan rail). Clearing it on teardown
    keeps every test's event stream classification self-contained.
    """
    import agents.stream as _stream
    import core.observability.events as _events
    yield
    _logging_module._progress_log = None
    _stream._agent_log = None
    _events.clear_phase_context()
    _events.init_event_store(None)


@pytest.fixture(autouse=True)
def _adr0119_legacy_bypass_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin delivery to the ADR 0119 ``bypass`` opt-out for this legacy slice.

    ADR 0119 shipped ``branch_policy=worktree_branch`` as the delivery default,
    which publishes an isolated run's own branch instead of committing onto the
    target checkout — so a parent run leaves its checkout clean and a follow-up /
    correction rerun refuses to start. These end-to-end flows predate that policy
    and assert the prior "commit onto the checkout" behavior, so they run under
    ``bypass`` (the ADR's explicit legacy opt-out). The new branch-policy
    behavior is covered by
    ``tests/unit/pipeline/engine/test_commit_delivery.py`` and
    ``test_delivery_branch.py``.
    """
    import pipeline.engine.delivery_branch as _db

    monkeypatch.setattr(_db, "normalize_branch_policy", lambda _raw: "bypass")


# ─────────────────────────────────────────────────────────────────────────────
# Shared plugin config (real object, not MagicMock)
# ─────────────────────────────────────────────────────────────────────────────

PLUGIN = PluginConfig(
    name="Acceptance Test Project",
    language="Python",
    architecture="FastAPI + SQLAlchemy",
    file_hints=["src/", "tests/"],
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _init_git_repo(path: Path) -> None:
    """Initialize ``path`` as a git repo with one committed file.

    The pipeline engine requires ``project_dir`` to be a real git repo
    so worktree isolation can attach. Tests that hand the engine a
    bare tmp dir would otherwise hit the WorktreeConfigError hard-fail.
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@orcho.invalid"],
        cwd=path, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Orcho Test"], cwd=path, check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=path, check=True,
    )
    (path / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "my_project"
    _init_git_repo(p)
    return p


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runs" / "20260502_000000"
    d.mkdir(parents=True)
    return d


def _build_clean_review_provider() -> MockAgentProvider:
    p = MockAgentProvider(latency=0.0, test_pass_rate=1.0)
    p.codex = lambda model, **_kw: _AlwaysApproved()
    return p


def _build_issue_provider() -> MockAgentProvider:
    p = MockAgentProvider(latency=0.0, test_pass_rate=1.0)
    p.codex = lambda model, **_kw: _AlwaysIssue()
    return p


@pytest.fixture
def clean_review_provider() -> MockAgentProvider:
    return _build_clean_review_provider()


@pytest.fixture
def issue_provider() -> MockAgentProvider:
    return _build_issue_provider()


# ─────────────────────────────────────────────────────────────────────────────
# Codex stubs
# ─────────────────────────────────────────────────────────────────────────────

class _AlwaysApproved:
    model = "stub-codex"
    session_id: str | None = None

    def invoke(
        self, prompt: str, cwd: str, *,
        mutates_artifacts: bool = False,
        continue_session: bool = False,
        attachments: tuple = (),
    ) -> str:
        # ADR 0025: release-gate prompts (project final_acceptance)
        # need a release-shape APPROVED payload — parse_release
        # would raise on a review-shape envelope.
        if _prompt_requests_release(prompt):
            return _approved_release_json()
        return _approved_review_json()

    def reset_session(self) -> None:
        self.session_id = None


class _AlwaysIssue:
    """Reviews the working tree as issued; passes plan-validation prompts."""
    model = "stub-codex"
    session_id: str | None = None

    def invoke(
        self, prompt: str, cwd: str, *,
        mutates_artifacts: bool = False,
        continue_session: bool = False,
        attachments: tuple = (),
    ) -> str:
        # Release-gate prompts get an APPROVED release payload — this
        # stub raises issues for the review loop, not for the closing
        # release gate.
        if _prompt_requests_release(prompt):
            return _approved_release_json()
        # Plan-validation prompts. Detection must be delta-safe:
        # under PER_PHASE session split, round 2+ wire omits the
        # cached task body, so the anchor must live in a TURN/NONE
        # part. PR3 emits a ``plan_tasks:execution_plan`` part whose
        # body always starts with the ``## Tasks`` heading rendered
        # by render_validate_plan_tasks — present on every round.
        if (
            "Review the implementation plan document" in prompt
            or "Review the proposed solution before code is written" in prompt
            or "there is no\nimplementation diff yet" in prompt
        ):
            return _approved_review_json()
        return _rejected_review_json("Missing null check in handler.")

    def reset_session(self) -> None:
        self.session_id = None


class _ApprovedReviewRejectedRelease:
    """APPROVED review/plan loop, REJECTED closing release gate (ADR 0106).

    review_changes + validate_plan pass, so the run reaches ``final_acceptance``
    with a real diff, where the release gate rejects — driving the T1
    rejected-release terminal path.
    """
    model = "stub-codex"
    session_id: str | None = None

    def invoke(
        self, prompt: str, cwd: str, *,
        mutates_artifacts: bool = False,
        continue_session: bool = False,
        attachments: tuple = (),
    ) -> str:
        if _prompt_requests_release(prompt):
            return _rejected_release_json()
        return _approved_review_json()

    def reset_session(self) -> None:
        self.session_id = None


# ─────────────────────────────────────────────────────────────────────────────
# Run helpers
# ─────────────────────────────────────────────────────────────────────────────

def _patches():
    """Context managers that isolate single-project pipeline from the OS."""
    return (
        patch("pipeline.project.session_run.load_plugin", return_value=PLUGIN),
        patch("core.io.git_helpers.has_uncommitted", return_value=True),
        patch("core.io.git_helpers.git_diff_stat", return_value="1 file changed"),
    )


def _run(
    task: str = "Add structured logging",
    *,
    project: Path,
    run_dir: Path,
    provider: MockAgentProvider,
    max_rounds: int = 1,
    profile_name: str = "feature",
    dry_run: bool = False,
) -> dict:
    """Phase 6: ``profile_name`` (str) replaces the legacy ``skip_plan``
    bool. Pass ``profile_name="task"`` for the build-only flow.
    """
    lp, hu, gd = _patches()
    with lp, hu, gd:
        return run_pipeline(
            task=task,
            project_dir=str(project),
            output_dir=run_dir,
            max_rounds=max_rounds,
            profile_name=profile_name,
            dry_run=dry_run,
            provider=provider,
        )


def _force_enable_cross_gates(real_loader):
    """Wrap ``load_profiles_v2_with_plugins`` so loaded profiles always
    have ``contract_check`` and ``cross_final_acceptance`` enabled.

    The cross-project acceptance tests assert behaviour of those gates
    (verdict shape, parse-error halt, terminal status). They must not
    silently start passing or failing when a developer toggles
    ``enabled: false`` in ``_config/pipeline_profiles_v2.json`` for
    unrelated local work — the test fixture, not the shipped config,
    owns whether the gates run for these tests.

    Implemented as a wrapper around the real loader so projection,
    validation, and every other field stay byte-identical to the
    file-loaded value; only the two known gate enable flags are
    forced True.
    """
    import dataclasses

    from pipeline.runtime.profile import CrossGatePolicy

    def _wrapped(*args, **kwargs):
        loaded = real_loader(*args, **kwargs)
        forced: dict[str, object] = {}
        for name, profile in loaded.items():
            current_gates = dict(profile.cross_gates)
            for gate_name in ("contract_check", "cross_final_acceptance"):
                policy = current_gates.get(gate_name)
                if policy is None:
                    current_gates[gate_name] = CrossGatePolicy(enabled=True)
                elif not policy.enabled:
                    current_gates[gate_name] = dataclasses.replace(
                        policy, enabled=True,
                    )
            forced[name] = dataclasses.replace(
                profile, cross_gates=current_gates,
            )
        return forced

    return _wrapped


def _cross_run(task: str, *, cross_projects: dict[str, Path],
               cross_run_dir: Path, provider: MockAgentProvider) -> dict:
    from pipeline.cross_project.orchestrator import run_cross_pipeline
    from pipeline.profiles.loader import load_profiles_v2_with_plugins

    lp, hu, gd = _patches()
    with (
        lp, hu, gd,
        patch("pipeline.cross_project.run_setup.load_plugin", return_value=PLUGIN),
        patch(
            "pipeline.profiles.loader.load_profiles_v2_with_plugins",
            _force_enable_cross_gates(load_profiles_v2_with_plugins),
        ),
    ):
        return run_cross_pipeline(
            task=task,
            projects=cross_projects,
            output_dir=cross_run_dir,
            provider=provider,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Module-scoped scenario snapshots (read-only)
# ─────────────────────────────────────────────────────────────────────────────
# Snapshots collapse repeated execution of the same production scenario
# into a single in-memory parsed result. Consumer tests read fields from
# the snapshot dict; they never re-run the pipeline. Mutating / resume /
# corruption / interactive-decision tests keep their function-scoped
# fixtures — they MUST NOT consume the snapshots.
#
# Each snapshot has a function-scoped ``*_smoke_e2e`` sentinel that
# re-runs the scenario from scratch. If the sentinel fails while
# consumers keep passing, the snapshot has gone stale relative to the
# real scenario — fix by rebuilding the snapshot, not by patching it.


def _reset_test_globals() -> None:
    """Mirror of the autouse ``reset_logging_globals`` teardown.

    The autouse fixture is function-scoped, so it does NOT wrap
    module-scope fixture setup. The snapshot builder must reset these
    singletons itself — before the scenario run (so it starts clean) and
    after (so the first consumer test does not observe dirtied state).
    """
    import agents.stream as _stream
    import core.observability.events as _events
    _logging_module._progress_log = None
    _stream._agent_log = None
    _events.clear_phase_context()
    _events.init_event_store(None)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text().splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def _read_text_safe(path: Path) -> str:
    return path.read_text() if path.exists() else ""


def _collect_single_project_snapshot(
    session: dict, project: Path, run_dir: Path,
) -> dict:
    meta_path = run_dir / "meta.json"
    progress_path = run_dir / "progress.log"
    output_path = run_dir / "output.log"
    return {
        "session": session,
        "meta": json.loads(meta_path.read_text()) if meta_path.exists() else {},
        "events": _read_jsonl(run_dir / "events.jsonl"),
        "progress_log_text": _read_text_safe(progress_path),
        "output_log_text": _read_text_safe(output_path),
        "meta_json_exists": meta_path.exists(),
        "progress_log_exists": progress_path.exists(),
        "output_log_exists": output_path.exists(),
        "project_path_str": str(project),
    }


@pytest.fixture(scope="module")
def clean_advanced_snapshot(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """One module-scoped run of the advanced/clean scenario.

    Consumer tests in TestA1/A6/A7/A8 read fields from this dict. Mutating
    tests (TestA2 issue provider, TestA3 max-rounds, TestA4 task profile,
    TestA5 dry-run, TestA7's lite profile test, test_task_recorded) keep
    function-scoped fixtures.
    """
    root = tmp_path_factory.mktemp("clean_advanced")
    project = root / "proj"
    _init_git_repo(project)
    run_dir = root / "runs" / "20260502_000000"
    run_dir.mkdir(parents=True)
    provider = _build_clean_review_provider()

    _reset_test_globals()
    try:
        session = _run(project=project, run_dir=run_dir, provider=provider)
    finally:
        _reset_test_globals()

    return _collect_single_project_snapshot(session, project, run_dir)


# ═════════════════════════════════════════════════════════════════════════════
# A. Single-project pipeline
# ═════════════════════════════════════════════════════════════════════════════

class TestA1_FullFlow:
    """Research → Plan → Build → clean review → QA."""

    def test_plan_phase_present(self, clean_advanced_snapshot) -> None:
        assert "plan" in clean_advanced_snapshot["session"]["phases"]

    def test_build_phase_present(self, clean_advanced_snapshot) -> None:
        assert "implement" in clean_advanced_snapshot["session"]["phases"]

    def test_validate_plan_phase_present(self, clean_advanced_snapshot) -> None:
        assert "validate_plan" in clean_advanced_snapshot["session"]["phases"]

    def test_codex_review_ran(self, clean_advanced_snapshot) -> None:
        rounds = clean_advanced_snapshot["session"]["phases"].get("rounds", [])
        assert len(rounds) == 1
        assert "critique" in rounds[0]

    def test_final_qa_present(self, clean_advanced_snapshot) -> None:
        assert "final_acceptance" in clean_advanced_snapshot["session"]["phases"]

    def test_task_recorded(self, project, run_dir, clean_review_provider) -> None:
        # Custom task — stays function-scoped (does not share the snapshot).
        s = _run("My acceptance task", project=project, run_dir=run_dir, provider=clean_review_provider)
        assert s["task"] == "My acceptance task"

    def test_project_dir_in_session(self, clean_advanced_snapshot) -> None:
        s = clean_advanced_snapshot["session"]
        project_str = clean_advanced_snapshot["project_path_str"]
        found = (
            project_str in str(s.get("project_dir", ""))
            or project_str in str(s.get("project", ""))
            or any(project_str in str(v) for v in s.values() if isinstance(v, str))
        )
        assert found, f"project path not found in session keys: {list(s.keys())}"

    def test_no_fix_on_clean_review(self, clean_advanced_snapshot) -> None:
        rounds = clean_advanced_snapshot["session"]["phases"].get("rounds", [])
        assert len(rounds) == 1
        assert "repair_output" not in rounds[0]


class TestA2_FixRound:
    def test_repair_output_present_when_issue_found(self, project, run_dir, issue_provider) -> None:
        s = _run(project=project, run_dir=run_dir, provider=issue_provider, max_rounds=1)
        rounds = s["phases"].get("rounds", [])
        assert len(rounds) == 1
        assert "repair_output" in rounds[0]

    def test_critique_recorded(self, project, run_dir, issue_provider) -> None:
        s = _run(project=project, run_dir=run_dir, provider=issue_provider, max_rounds=1)
        rounds = s["phases"].get("rounds", [])
        assert "null check" in rounds[0]["critique"]


class TestA3_MaxRounds:
    def test_rounds_capped_at_max(self, project, run_dir, issue_provider) -> None:
        s = _run(project=project, run_dir=run_dir, provider=issue_provider, max_rounds=2)
        assert len(s["phases"].get("rounds", [])) <= 2

    def test_zero_rounds_no_review(self, project, run_dir, clean_review_provider) -> None:
        s = _run(project=project, run_dir=run_dir, provider=clean_review_provider,
                 max_rounds=0, profile_name="task")
        assert s["phases"].get("rounds", []) == []


class TestA4_SkipPlan:
    def test_plan_absent(self, project, run_dir, clean_review_provider) -> None:
        s = _run(project=project, run_dir=run_dir, provider=clean_review_provider, profile_name="task")
        assert "plan" not in s["phases"]

    def test_validate_plan_absent(self, project, run_dir, clean_review_provider) -> None:
        s = _run(project=project, run_dir=run_dir, provider=clean_review_provider, profile_name="task")
        assert "validate_plan" not in s["phases"]

    def test_build_still_present(self, project, run_dir, clean_review_provider) -> None:
        s = _run(project=project, run_dir=run_dir, provider=clean_review_provider, profile_name="task")
        assert "implement" in s["phases"]


class TestA5_DryRun:
    def test_plan_is_dry_run(self, project, run_dir, clean_review_provider) -> None:
        s = _run(project=project, run_dir=run_dir, provider=clean_review_provider, dry_run=True)
        assert s["phases"]["plan"][-1]["output"] == "[DRY RUN]"

    def test_build_is_dry_run(self, project, run_dir, clean_review_provider) -> None:
        s = _run(project=project, run_dir=run_dir, provider=clean_review_provider, dry_run=True)
        assert s["phases"]["implement"]["output"] == "[DRY RUN]"

    def test_validate_plan_present_in_dry_run(self, project, run_dir, clean_review_provider) -> None:
        s = _run(project=project, run_dir=run_dir, provider=clean_review_provider, dry_run=True)
        assert "validate_plan" in s["phases"]


class TestA6_SessionStructure:
    REQUIRED_TOP = {"task", "phases", "timestamp"}

    def test_required_top_level_keys(self, clean_advanced_snapshot) -> None:
        s = clean_advanced_snapshot["session"]
        for key in self.REQUIRED_TOP:
            assert key in s, f"Missing key: {key}"

    def test_meta_json_written(self, clean_advanced_snapshot) -> None:
        assert clean_advanced_snapshot["meta_json_exists"]

    def test_meta_json_valid(self, clean_advanced_snapshot) -> None:
        data = clean_advanced_snapshot["meta"]
        assert "task" in data and "phases" in data

    def test_timestamp_present(self, clean_advanced_snapshot) -> None:
        ts = clean_advanced_snapshot["session"].get("timestamp", "")
        assert ts, "timestamp is empty"

    def test_build_phase_has_output_key(self, clean_advanced_snapshot) -> None:
        assert "output" in clean_advanced_snapshot["session"]["phases"]["implement"]

    def test_rounds_is_list(self, clean_advanced_snapshot) -> None:
        assert isinstance(
            clean_advanced_snapshot["session"]["phases"].get("rounds", []), list,
        )


class TestA7_ProgressLog:
    def test_progress_log_created(self, clean_advanced_snapshot) -> None:
        assert clean_advanced_snapshot["progress_log_exists"]

    def test_build_phase_in_log(self, clean_advanced_snapshot) -> None:
        assert "[IMPLEMENT]" in clean_advanced_snapshot["progress_log_text"]

    def test_plan_phase_in_log(self, clean_advanced_snapshot) -> None:
        assert "[PLAN]" in clean_advanced_snapshot["progress_log_text"]

    def test_start_end_pairs(self, clean_advanced_snapshot) -> None:
        log = clean_advanced_snapshot["progress_log_text"]
        assert "START" in log and "END" in log

    def test_done_marker(self, clean_advanced_snapshot) -> None:
        assert "DONE" in clean_advanced_snapshot["progress_log_text"]

    def test_lite_done_summary_uses_dynamic_phase_ids(self, project, run_dir, clean_review_provider) -> None:
        _run(
            project=project,
            run_dir=run_dir,
            provider=clean_review_provider,
            profile_name="small_task",
        )
        log = (run_dir / "progress.log").read_text()
        expected = "plan=ok | validate_plan=ok | implement=ok"
        assert expected in log
        for legacy_token in ("Plan=", "Build=", "Implement=", "Rounds=", "QA="):
            assert legacy_token not in log

        events = [
            json.loads(line)
            for line in (run_dir / "events.jsonl").read_text().splitlines()
        ]
        run_end = [event for event in events if event["kind"] == "run.end"][-1]
        assert run_end["payload"]["summary"] == expected


class TestA8_OutputLog:
    def test_output_log_created(self, clean_advanced_snapshot) -> None:
        assert clean_advanced_snapshot["output_log_exists"]

    def test_output_log_has_content(self, clean_advanced_snapshot) -> None:
        assert len(clean_advanced_snapshot["output_log_text"]) > 0


def test_clean_advanced_smoke_e2e(
    project, run_dir, clean_review_provider,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Sentinel for the clean/advanced scenario.

    Re-runs the scenario from scratch each test (function-scoped fixtures)
    and asserts the smoke contract. If this fails while the
    ``clean_advanced_snapshot`` consumers keep passing, the snapshot has
    drifted and the fixture builder needs to be re-evaluated. Phase set
    mirrors what TestA1 asserts today — keep them aligned.
    """
    s = _run(project=project, run_dir=run_dir, provider=clean_review_provider)
    for phase in ("plan", "validate_plan", "implement", "final_acceptance"):
        assert phase in s["phases"], f"sentinel: phase {phase!r} missing from session"
    rounds = s["phases"].get("rounds", [])
    assert len(rounds) == 1, "sentinel: expected exactly one review round"
    assert (run_dir / "meta.json").exists()
    assert (run_dir / "progress.log").exists()
    assert (run_dir / "output.log").exists()

    # ADR 0086 regression: the correction-route UX is gated on triage
    # evidence. A non-correction run must carry NO ``Correction route`` text
    # in progress.log or on the terminal.
    assert "Correction route" not in (run_dir / "progress.log").read_text()
    assert "Correction route" not in capsys.readouterr().out


class TestA9_RejectedReleaseTerminal:
    """T4 (ADR 0106) — single-project rejected ``final_acceptance`` reaches an
    actionable non-success terminal, persists the rejected delivery decision,
    and surfaces as actionable via the SDK; the approved control stays a clean
    ``done``.

    Override-done (a rejected release whose delivery was applied anyway) is
    UNREACHABLE in this non-interactive mock: auto mode hard-blocks a rejected
    release (the resolver returns ``not_applicable`` and never ships), and the
    SDK delivery gate refuses shipping actions on a rejected release. The
    override path requires an interactive TTY delivery dialog (ADR 0069), so
    the durable ``delivery_override`` marker is covered at unit level by T1
    (``test_rejected_release_with_applied_delivery_records_override`` in
    ``tests/unit/pipeline/project/test_finalize_done_order.py``).
    """

    @staticmethod
    def _run_rejected(*, project: Path, run_dir: Path) -> dict:
        provider = MockAgentProvider(latency=0.0, test_pass_rate=1.0)
        provider.codex = lambda model, **_kw: _ApprovedReviewRejectedRelease()
        return _run(project=project, run_dir=run_dir, provider=provider)

    def test_rejected_release_halts_not_done_with_visible_blockers(
        self, project, run_dir,
    ) -> None:
        s = self._run_rejected(project=project, run_dir=run_dir)

        # Not a silent successful done: actionable halted terminal.
        assert s["status"] == "halted"
        assert s["halt_reason"] == "final_acceptance_rejected"

        # Blockers are visible on the durable rejected_outcome marker.
        outcome = s["rejected_outcome"]
        assert outcome["status"] == "halted"
        assert outcome["release_verdict"] == "REJECTED"
        assert outcome["release_blockers"], "blockers must be visible"
        assert outcome["release_blockers"][0]["id"] == "RB1"

        # The override path did NOT fire (delivery never applied).
        assert "delivery_override" not in s

        # meta.json mirrors the halted terminal for resume / dashboards.
        meta = json.loads((run_dir / "meta.json").read_text())
        assert meta["status"] == "halted"
        assert meta["halt_reason"] == "final_acceptance_rejected"

    def test_rejected_release_persists_commit_delivery_decision(
        self, project, run_dir,
    ) -> None:
        self._run_rejected(project=project, run_dir=run_dir)
        meta = json.loads((run_dir / "meta.json").read_text())
        cd = meta["commit_delivery"]
        assert cd["status"] == "not_applicable"
        assert cd["release_verdict"] == "REJECTED"
        assert cd["release_summary"]  # carries the rejection summary
        assert cd["release_blockers"]
        assert cd["release_blockers"][0]["id"] == "RB1"
        assert cd["release_blockers"][0]["title"] == "Data loss on apply"

    def test_rejected_release_sdk_surfaces_are_actionable(
        self, project, run_dir,
    ) -> None:
        from sdk import load_status
        from sdk.run_control.delivery import delivery_decision_state

        self._run_rejected(project=project, run_dir=run_dir)
        runs_dir = run_dir.parent
        run_id = run_dir.name

        # Delivery gate (ADR 0111 + ADR 0133): an auto-refused rejected release
        # is a decidable correction whose only forward motion is an ordinary
        # follow-up through resume with an operator comment. Shipping, ``skip``
        # (ADR 0106) AND the now-inert ``fix`` repeat are all blocked — only
        # ``halt`` remains — and the reason routes the client to the follow-up
        # (NOT 'no pending delivery gate').
        state = delivery_decision_state(run_id, runs_dir=runs_dir, cwd=None)
        assert state.decidable is True
        assert state.kind == "correction"
        assert set(state.blocked_actions) == {"fix", "approve", "apply", "skip"}
        assert state.available_actions == ("halt",)
        assert "fix" not in state.available_actions
        assert "skip" not in state.available_actions
        assert state.default_action is None
        assert state.reason is not None
        # The reason names the follow-up handle as the run's resolved (stamped)
        # id from the persisted gate context — which the run-dir name may differ
        # from in this fixture, so assert against ``state.run_id``.
        assert f"orcho_run_resume run_id={state.run_id}" in state.reason
        assert "operator comment" in state.reason
        assert "from_run_plan" not in state.reason
        assert "inert" in state.reason

        # The durable rejected blockers stay on the persisted gate context (the
        # follow-up reason intentionally points forward rather than re-listing
        # them) — they remain available to richer surfaces.
        meta_after = json.loads((run_dir / "meta.json").read_text())
        blockers = meta_after["commit_delivery"]["release_blockers"]
        assert blockers and blockers[0]["id"] == "RB1"

        # next_actions: non-empty actionable recovery for the halted terminal.
        status = load_status(run_id, runs_dir=runs_dir, cwd=None)
        assert status.next_actions, "halted rejected run must be actionable"

    def test_approved_release_is_clean_done_without_markers(
        self, project, run_dir, clean_review_provider,
    ) -> None:
        # Success control: APPROVED release => clean done, no reject/override
        # markers.
        s = _run(project=project, run_dir=run_dir, provider=clean_review_provider)
        assert s["status"] == "done"
        assert s.get("halt_reason") is None
        assert "rejected_outcome" not in s
        assert "delivery_override" not in s


# ═════════════════════════════════════════════════════════════════════════════
# B. Cross-project pipeline
# ═════════════════════════════════════════════════════════════════════════════

def _make_project(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    _init_git_repo(p)
    return p


@pytest.fixture
def cross_projects(tmp_path: Path) -> dict[str, Path]:
    return {
        "unity": _make_project(tmp_path, "unity"),
        "api":   _make_project(tmp_path, "api"),
        "stats": _make_project(tmp_path, "stats"),
    }


@pytest.fixture
def cross_run_dir(tmp_path: Path) -> Path:
    d = tmp_path / "cross_run" / "20260502_000000"
    d.mkdir(parents=True)
    return d


# ─────────────────────────────────────────────────────────────────────────────
# Cross-project module-scoped snapshot (read-only)
# ─────────────────────────────────────────────────────────────────────────────


_CROSS_ALIASES: tuple[str, ...] = ("unity", "api", "stats")


def _collect_cross_snapshot(
    session: dict,
    cross_projects: dict[str, Path],
    cross_run_dir: Path,
) -> dict:
    per_meta: dict[str, dict] = {}
    per_handoff_md: dict[str, str] = {}
    per_handoff_json: dict[str, dict] = {}
    per_plan_files: dict[str, list[str]] = {}
    per_dir_exists: dict[str, bool] = {}
    per_progress_text: dict[str, str] = {}
    per_events: dict[str, list[dict]] = {}
    for alias in cross_projects:
        alias_dir = cross_run_dir / alias
        per_dir_exists[alias] = alias_dir.is_dir()
        meta_path = alias_dir / "meta.json"
        per_meta[alias] = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        md_path = alias_dir / "implementation_handoff.md"
        per_handoff_md[alias] = _read_text_safe(md_path)
        js_path = alias_dir / "implementation_handoff.json"
        per_handoff_json[alias] = json.loads(js_path.read_text()) if js_path.exists() else {}
        per_plan_files[alias] = sorted(p.name for p in alias_dir.glob("plan_*.md"))
        # Per-alias durable child artifacts (may be absent if children
        # share the cross spine — captured either way for route-regression
        # assertions that must hold on the child surface specifically).
        per_progress_text[alias] = _read_text_safe(alias_dir / "progress.log")
        per_events[alias] = _read_jsonl(alias_dir / "events.jsonl")
    return {
        "session": session,
        "events": _read_jsonl(cross_run_dir / "events.jsonl"),
        "progress_log_text": _read_text_safe(cross_run_dir / "progress.log"),
        "cross_plan_md_text": _read_text_safe(cross_run_dir / "cross_plan.md"),
        "cross_plan_md_exists": (cross_run_dir / "cross_plan.md").exists(),
        "cross_run_id": cross_run_dir.name,
        "aliases": tuple(cross_projects.keys()),
        "alias_to_resolved_path_str": {
            alias: str(path.resolve()) for alias, path in cross_projects.items()
        },
        "alias_to_meta": per_meta,
        "alias_to_handoff_md_exists": {a: bool(v) for a, v in per_handoff_md.items()},
        "alias_to_handoff_md_text": per_handoff_md,
        "alias_to_handoff_json": per_handoff_json,
        "alias_to_plan_files": per_plan_files,
        "alias_to_dir_exists": per_dir_exists,
        "alias_to_progress_text": per_progress_text,
        "alias_to_events": per_events,
    }


@pytest.fixture(scope="module")
def cross_clean_snapshot(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """One module-scoped cross-project run for the advanced/clean scenario.

    Consumer tests in TestB1/B2/B3/B3_ChildRunLinkage/B4/B7/B8 happy/B10
    happy read from this dict instead of paying for a fresh
    ``_cross_run(...)`` (which builds three git repos and a full cross
    pipeline). Tests with custom Codex stubs, alternate profiles,
    ``build_prompt`` patches, or different example projects keep their
    function-scoped fixtures.
    """
    root = tmp_path_factory.mktemp("cross_clean")
    projects = {
        name: _make_project(root, name) for name in _CROSS_ALIASES
    }
    cross_dir = root / "cross_run" / "20260502_000000"
    cross_dir.mkdir(parents=True)
    provider = _build_clean_review_provider()

    # Task string deliberately mirrors the most content-rich pre-refactor
    # invocation (``test_cross_plan_has_content``) so the cross_plan.md
    # length assertion stays meaningful. Other consumers are task-agnostic.
    _reset_test_globals()
    try:
        session = _cross_run(
            "Task with logging",
            cross_projects=projects,
            cross_run_dir=cross_dir,
            provider=provider,
        )
    finally:
        _reset_test_globals()

    return _collect_cross_snapshot(session, projects, cross_dir)


class TestB1_CrossFullFlow:
    def test_returns_session(self, cross_clean_snapshot) -> None:
        result = cross_clean_snapshot["session"]
        assert result is not None
        assert "phases" in result

    def test_all_projects_in_phases(self, cross_clean_snapshot) -> None:
        projects_in_session = cross_clean_snapshot["session"]["phases"].get("projects", {})
        for name in cross_clean_snapshot["aliases"]:
            assert name in projects_in_session, f"'{name}' missing from phases.projects"

    def test_each_project_session_has_phases(self, cross_clean_snapshot) -> None:
        for name, sub in cross_clean_snapshot["session"]["phases"]["projects"].items():
            assert "phases" in sub, f"'{name}' sub-session missing 'phases'"

    def test_child_done_summaries_use_projected_phase_ids(self, cross_clean_snapshot) -> None:
        summaries = [
            event["payload"]["summary"]
            for event in cross_clean_snapshot["events"]
            if event.get("kind") == "run.end"
            and isinstance(event.get("payload"), dict)
            and "summary" in event["payload"]
        ]
        assert len(summaries) == len(cross_clean_snapshot["aliases"])
        for summary in summaries:
            assert "implement=ok" in summary
            for legacy_token in ("Plan=", "Build=", "Implement=", "Rounds=", "QA="):
                assert legacy_token not in summary


class TestB2_CrossPlanFile:
    def test_cross_plan_md_created(self, cross_clean_snapshot) -> None:
        assert cross_clean_snapshot["cross_plan_md_exists"]

    def test_cross_plan_has_content(self, cross_clean_snapshot) -> None:
        assert len(cross_clean_snapshot["cross_plan_md_text"]) > 10


class TestB3_ContractCheck:
    def test_contract_check_in_phases(self, cross_clean_snapshot) -> None:
        assert "contract_check" in cross_clean_snapshot["session"]["phases"]

    def test_contract_check_in_progress_log(self, cross_clean_snapshot) -> None:
        assert "CONTRACT_CHECK" in cross_clean_snapshot["progress_log_text"]

    def test_contract_check_carries_typed_contract(self, cross_clean_snapshot) -> None:
        """REA-3.6 part A: each per-alias contract check produces the typed
        reviewer contract — verdict, short_summary, findings — not prose."""
        contract = cross_clean_snapshot["session"]["phases"]["contract_check"]
        assert set(contract.keys()) == set(cross_clean_snapshot["aliases"])
        for alias, entry in contract.items():
            assert isinstance(entry, dict), f"{alias}: legacy string shape leaked"
            assert entry["verdict"] in ("APPROVED", "REJECTED")
            assert isinstance(entry["short_summary"], str) and entry["short_summary"]
            assert isinstance(entry["findings"], list)
            assert "rendered" in entry and entry["rendered"]
            assert "raw_response" in entry


class TestCrossDemoExample:
    """REA-3.6 part C: ``examples/cross-api-web/`` is the cross-project
    demo fixture. CI exercises it end-to-end (mock provider) so the
    advertised one-command workflow can never silently break."""

    EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "cross-api-web"

    def _projects(self, tmp_path: Path) -> dict[str, Path]:
        # Copy out so the source tree isn't mutated by the orchestrator.
        api_dst = tmp_path / "api"
        web_dst = tmp_path / "web"
        shutil.copytree(self.EXAMPLE_DIR / "api", api_dst)
        shutil.copytree(self.EXAMPLE_DIR / "web", web_dst)
        _init_git_repo(api_dst)
        _init_git_repo(web_dst)
        return {"api": api_dst, "web": web_dst}

    def test_cross_demo_runs_end_to_end(
        self, tmp_path: Path, clean_review_provider,
    ) -> None:
        projects = self._projects(tmp_path)
        run_dir = tmp_path / "cross_run" / "CROSS_DEMO"
        result = _cross_run(
            "Align user payload contract between api and web",
            cross_projects=projects, cross_run_dir=run_dir,
            provider=clean_review_provider,
        )
        # Both children produced their per-alias artifact dirs and meta.
        for alias in projects:
            assert (run_dir / alias / "meta.json").exists(), (
                f"{alias}: per-alias meta.json missing"
            )
        # Contract check carries the typed reviewer contract for both.
        contract = result["phases"]["contract_check"]
        assert set(contract) == set(projects)
        for entry in contract.values():
            assert entry["verdict"] in ("APPROVED", "REJECTED")
            assert isinstance(entry["short_summary"], str) and entry["short_summary"]
        # Child run.start events on the shared event spine carry the
        # parent_run_id + project_alias linkage.
        events = [
            json.loads(line)
            for line in (run_dir / "events.jsonl").read_text().splitlines()
            if line.strip()
        ]
        children = [
            e["payload"] for e in events
            if e.get("kind") == "run.start"
            and e["payload"].get("run_kind") == "single_project"
        ]
        assert {c["project_alias"] for c in children} == set(projects)
        for payload in children:
            assert payload["parent_run_id"] == run_dir.name


class TestB3_ChildRunLinkage:
    """REA-3.6 part B: child run.start events carry parent_run_id + alias
    so MCP / evidence can rebuild the parent → children timeline."""

    def test_child_run_start_carries_parent_run_id_and_alias(self, cross_clean_snapshot) -> None:
        # Sub-pipelines under a cross run share the cross run's
        # events.jsonl. Reading that single file should let a consumer
        # reconstruct the whole parent → children tree without touching
        # filesystem layout.
        parent_run_id = cross_clean_snapshot["cross_run_id"]
        run_starts = [
            event["payload"]
            for event in cross_clean_snapshot["events"]
            if event.get("kind") == "run.start"
        ]
        assert run_starts, "cross run events.jsonl missing run.start entries"

        # First run.start is the cross run itself; children follow.
        assert run_starts[0]["run_kind"] == "cross_project"
        children = [p for p in run_starts if p["run_kind"] == "single_project"]
        aliases = cross_clean_snapshot["aliases"]
        assert {p["project_alias"] for p in children} == set(aliases)
        for payload in children:
            assert payload["parent_run_id"] == parent_run_id, (
                f"child {payload['project_alias']!r}: parent_run_id "
                f"{payload['parent_run_id']!r} != {parent_run_id!r}"
            )
            expected = cross_clean_snapshot["alias_to_resolved_path_str"][
                payload["project_alias"]
            ]
            assert payload["project"] == expected


class TestB11_CrossPreservesChildRouteAndSkipEvidence:
    """T3 — the cross parent must not erase, relabel, or contaminate child
    evidence.

    Three guarantees, all read from the existing module-scoped clean cross
    snapshot (no extra full cross run):

    1. A child phase that did not run (``repair_changes`` is skipped on a
       clean review) reads ``=skip`` on the parent's shared event spine —
       the parent never upgrades a child skip to ``ok``/success.
    2. A non-correction cross child (the ``advanced`` projection) carries
       NO ``Correction route`` text on any durable surface — parent
       progress.log, parent events.jsonl, or the per-alias artifacts.
    3. ADR 0085 boundary: the cross projection never yields a
       ``correction_triage`` phase in any child sub-session (correction is
       not projected into cross).

    The SILENT request-form boundary — that children are launched with the
    exact ``ProjectRunRequest(presentation=SILENT, no_interactive=True)``
    form whose evidence preservation T2 proved — is locked separately by
    ``TestB11b`` (it needs to observe the request object, not a snapshot
    artifact).
    """

    @staticmethod
    def _child_run_end_summaries(snap: dict) -> list[str]:
        # Child sub-pipelines emit their own ``run.end`` on the SHARED
        # cross event spine; the cross-level ``run.end`` carries
        # ``summary=None``. Select the per-child summaries by their
        # phase-chip shape (``implement=...``) — the same selector spirit
        # as TestB1's child-summary test.
        out: list[str] = []
        for event in snap["events"]:
            if event.get("kind") != "run.end":
                continue
            summary = (event.get("payload") or {}).get("summary")
            if isinstance(summary, str) and "implement=" in summary:
                out.append(summary)
        return out

    def test_child_skip_phase_not_relabeled_ok_on_parent_side(
        self, cross_clean_snapshot,
    ) -> None:
        snap = cross_clean_snapshot
        summaries = self._child_run_end_summaries(snap)
        assert len(summaries) == len(snap["aliases"]), (
            f"expected one child run.end summary per alias, got {summaries}"
        )
        for summary in summaries:
            # Clean review passes round 1 → repair_changes never runs →
            # it is a genuine skip in the child DONE summary. The parent
            # surfaces that summary verbatim on its spine and must NOT
            # render the skipped phase as ok/success.
            assert "repair_changes=skip" in summary, summary
            assert "repair_changes=ok" not in summary, summary
        # The cross-level run.end does NOT re-summarize children into a
        # success chip line — it carries no per-child phase chips at all.
        cross_level = [
            (event.get("payload") or {}).get("summary")
            for event in snap["events"]
            if event.get("kind") == "run.end"
        ]
        assert any(summary is None for summary in cross_level), (
            "cross-level run.end should not synthesize a child-chip summary"
        )

    def test_child_session_embedded_verbatim_in_parent(
        self, cross_clean_snapshot,
    ) -> None:
        snap = cross_clean_snapshot
        projects = snap["session"]["phases"]["projects"]
        for alias in snap["aliases"]:
            sub = projects[alias]
            assert sub.get("status") == "done", alias
            # The skipped repair phase left NO record in the child phase
            # log (absent → ``skip`` in the DONE summary). The parent
            # stores the child sub-session as-is; it never synthesizes a
            # ``repair_changes`` record (which a relabel-to-ok would need).
            assert "repair_changes" not in sub.get("phases", {}), alias

    def test_non_correction_cross_children_have_no_route_text(
        self, cross_clean_snapshot,
    ) -> None:
        snap = cross_clean_snapshot
        # Cross children share the parent spine — they write no per-alias
        # progress.log / events.jsonl — so the shared cross artifacts ARE
        # the child surface. None of them may carry correction-route text.
        assert "Correction route" not in snap["progress_log_text"]
        for event in snap["events"]:
            assert "Correction route" not in json.dumps(event), event.get("kind")
        # Per-alias artifact dirs likewise carry no child-local route text
        # (empty for cross children, asserted defensively so a future
        # per-alias progress.log can never silently leak route lines).
        for alias in snap["aliases"]:
            assert "Correction route" not in snap["alias_to_progress_text"][alias]
            for event in snap["alias_to_events"][alias]:
                assert "Correction route" not in json.dumps(event)

    def test_cross_projection_never_yields_correction_triage(
        self, cross_clean_snapshot,
    ) -> None:
        # ADR 0085: the correction profile is not projected into cross, so
        # no child sub-session may contain a ``correction_triage`` phase.
        snap = cross_clean_snapshot
        for alias in snap["aliases"]:
            sub = snap["session"]["phases"]["projects"][alias]
            assert "correction_triage" not in sub.get("phases", {}), alias


class TestB11b_CrossChildLaunchForm:
    """T3 boundary — lock the child launch form to the one T2 proved.

    A regression that drops ``presentation=SILENT`` (or flips
    ``no_interactive``) when the cross orchestrator builds a child
    ``ProjectRunRequest`` would silently diverge cross children from the
    project-profile SILENT path whose route-evidence preservation T2
    proved. Capture every request ``project_dispatch`` builds and assert
    the form, so such a divergence fails loudly here.
    """

    def test_cross_children_launched_with_silent_request_form(
        self, cross_projects, cross_run_dir, clean_review_provider,
    ) -> None:
        from pipeline.project.types import PresentationPolicy

        real = sys.modules[
            "pipeline.cross_project.project_dispatch"
        ].run_project_pipeline
        captured: list = []

        def _capture(request):
            captured.append(request)
            return real(request)

        with patch(
            "pipeline.cross_project.project_dispatch.run_project_pipeline",
            side_effect=_capture,
        ):
            _cross_run(
                "Task", cross_projects=cross_projects,
                cross_run_dir=cross_run_dir, provider=clean_review_provider,
            )

        assert len(captured) == len(cross_projects), (
            f"expected one child request per alias, got {len(captured)}"
        )
        for request in captured:
            # The exact form T2's SILENT route-evidence test exercised.
            assert request.presentation is PresentationPolicy.SILENT
            assert request.no_interactive is True
            # ADR 0085 boundary: cross launches the projected workflow
            # profile, never the internal correction profile.
            assert request.profile_name != "correction"
            assert request.profile_obj is not None


class TestB4_PerProjectArtifacts:
    def test_per_project_folders_created(self, cross_clean_snapshot) -> None:
        for name in cross_clean_snapshot["aliases"]:
            assert cross_clean_snapshot["alias_to_dir_exists"][name], (
                f"Missing dir: {name}/"
            )

    def test_per_project_meta_written(self, cross_clean_snapshot) -> None:
        for name in cross_clean_snapshot["aliases"]:
            data = cross_clean_snapshot["alias_to_meta"][name]
            assert data, f"meta.json missing or empty for {name}"
            assert "phases" in data

    def test_per_project_plan_file_written(self, cross_clean_snapshot) -> None:
        for name in cross_clean_snapshot["aliases"]:
            assert cross_clean_snapshot["alias_to_plan_files"][name], (
                f"No plan_*.md found for {name}"
            )


class TestB7_CrossProfileProjection:
    """Killer-feature invariants for cross-aware profile projection (ADR 0024).

    The cross run uses the requested ``--profile`` as the single workflow
    knob. Children execute the projected ``project_steps`` of that profile
    via an in-memory ``Profile`` object — there is no ``task`` sub-profile
    fallback. Each alias receives an ``implementation_handoff.{md,json}``
    artifact that the child reads as a hard precondition before mutating
    phases run.
    """

    def test_handoff_artifacts_written_per_alias(self, cross_clean_snapshot) -> None:
        for alias in cross_clean_snapshot["aliases"]:
            assert cross_clean_snapshot["alias_to_handoff_md_exists"][alias], (
                f"{alias}: implementation_handoff.md missing"
            )
            assert cross_clean_snapshot["alias_to_handoff_json"][alias], (
                f"{alias}: implementation_handoff.json missing"
            )

    def test_handoff_json_carries_v1_payload(self, cross_clean_snapshot) -> None:
        aliases = list(cross_clean_snapshot["aliases"])
        for alias in aliases:
            payload = cross_clean_snapshot["alias_to_handoff_json"][alias]
            # v1 contract: cross-plan body + subtask + sibling context.
            assert payload["alias"] == alias
            assert payload["profile"] == "feature"
            assert payload["parent_run_id"] == cross_clean_snapshot["cross_run_id"]
            assert payload["full_cross_plan_markdown"], (
                f"{alias}: full_cross_plan_markdown empty"
            )
            # ADR 0054 regression: this field is MARKDOWN (its companion
            # ``full_cross_plan_path`` points at cross_plan.md), NOT the
            # canonical cross_plan.json. The dispatcher must render the
            # markdown, never assign ``ctx.plan_output`` (the JSON) verbatim.
            full_md = payload["full_cross_plan_markdown"]
            assert full_md.lstrip().startswith("# Cross-Project Plan"), (
                f"{alias}: full_cross_plan_markdown is not the rendered md"
            )
            with pytest.raises((json.JSONDecodeError, ValueError)):
                # A rendered markdown document is not a JSON object.
                _obj = json.loads(full_md)
                if isinstance(_obj, dict) and "subtasks" in _obj:
                    raise ValueError("field carries canonical cross-plan JSON")
            assert payload["project_subtask"], (
                f"{alias}: project_subtask empty"
            )
            other = sorted(a for a in aliases if a != alias)
            assert sorted(payload["sibling_aliases"]) == other

    def test_child_meta_keeps_requested_profile_name(self, cross_clean_snapshot) -> None:
        """meta.json records ``profile=feature`` (requested) plus
        ``projected_profile=feature#project`` (synthetic). The legacy
        ``task`` sub-profile must not appear as the canonical name."""
        for alias in cross_clean_snapshot["aliases"]:
            meta = cross_clean_snapshot["alias_to_meta"][alias]
            assert meta["profile"] == "feature", (
                f"{alias}: child meta.profile should be the requested name"
            )
            assert meta.get("projected_profile") == "feature#project"
            assert meta.get("plan_source") == "cross"

    def test_child_run_start_event_carries_projection_fields(self, cross_clean_snapshot) -> None:
        """``run.start`` event for each child carries the new
        ``projected_profile`` + ``plan_source`` discriminators."""
        children = [
            e["payload"] for e in cross_clean_snapshot["events"]
            if e.get("kind") == "run.start"
            and e["payload"].get("run_kind") == "single_project"
        ]
        assert children, "expected child run.start events"
        for c in children:
            assert c["profile"] == "feature"
            assert c["plan_source"] == "cross"
            assert c["projected_profile"] == "feature#project"

    def test_task_is_not_fallback_anywhere(self, cross_clean_snapshot) -> None:
        """The retired ``CROSS_SUB_PROFILE = "task"`` constant left a
        trail in child sessions / events. After projection this must be
        gone: no child's canonical profile is ``task``.
        """
        result = cross_clean_snapshot["session"]
        for alias, sub in result["phases"]["projects"].items():
            assert sub.get("profile") != "task", (
                f"{alias}: child still reports profile=task (legacy fallback)"
            )
        # Events spine must not surface task as canonical either.
        single_project_events = [
            e["payload"] for e in cross_clean_snapshot["events"]
            if e.get("kind") == "run.start"
            and e["payload"].get("run_kind") == "single_project"
        ]
        for c in single_project_events:
            assert c["profile"] != "task"


class TestB7b_HandoffReachesImplementPrompt:
    """The handoff is only load-bearing if its body actually lands in the
    child implement agent's prompt. After M9 the implement handler
    bypasses ``phases.run_build`` for live runs and goes through
    ``prompts.build_prompt`` + the session-aware invoke helper directly.
    Spy on ``build_prompt`` — that is the layer where ``handoff_contract``
    enters the rendered prompt.
    """

    def test_implement_prompt_contains_handoff_body(
        self, cross_projects, cross_run_dir, clean_review_provider,
    ) -> None:
        captured_handoffs: list[str] = []

        import pipeline.prompts as _prompts
        real_build_prompt = _prompts.build_prompt

        def _capture(*args, **kwargs):
            captured_handoffs.append(kwargs.get("handoff_contract", ""))
            return real_build_prompt(*args, **kwargs)

        # Patch the import site M9 uses inside _phase_implement too.
        with patch.object(_prompts, "build_prompt", _capture):
            _cross_run("Align user payload", cross_projects=cross_projects,
                       cross_run_dir=cross_run_dir,
                       provider=clean_review_provider)

        # At least one implement phase ran across the children.
        assert captured_handoffs, "expected at least one build_prompt call"
        # Every implement invocation received a non-empty handoff body
        # carrying the cross-plan markdown + the per-project subtask.
        for body in captured_handoffs:
            assert body, "implement phase received empty handoff_contract"
            assert "cross handoff" in body.lower() or "cross plan" in body.lower(), (
                "handoff body missing cross-plan markers"
            )

    def test_handoff_body_carries_subtask_and_plan_markers(
        self, cross_projects, cross_run_dir, clean_review_provider,
    ) -> None:
        """The handoff body must carry the per-project subtask AND a
        section pointing to the full cross plan — these are the
        load-bearing fields the prompt builder prepends so the agent
        reads cross context before plan-contract.
        """
        captured_handoffs: list[str] = []
        import pipeline.prompts as _prompts
        real_build_prompt = _prompts.build_prompt

        def _capture(*args, **kwargs):
            captured_handoffs.append(kwargs.get("handoff_contract", ""))
            return real_build_prompt(*args, **kwargs)

        with patch.object(_prompts, "build_prompt", _capture):
            _cross_run("Align payload", cross_projects=cross_projects,
                       cross_run_dir=cross_run_dir,
                       provider=clean_review_provider)

        assert captured_handoffs, "expected at least one build_prompt call"
        # ``pipeline.cross_project.handoff._render_markdown`` emits stable
        # section headers — assert the canonical-grammar sections so the
        # prompt structure stays under contract. With the ADR 0054 typed
        # cross plan the architect always emits a conformant plan, so the
        # handoff uses the structured slices (interface contract + this
        # alias's subtask + cross-alias implementation order) and never
        # falls back to the full-plan dump. If a future refactor breaks
        # the wiring, the implement prompt would silently drop cross
        # context.
        for body in captured_handoffs:
            assert "## Project subtask" in body, (
                "handoff missing 'Project subtask' section"
            )
            assert "## Interface contract" in body, (
                "handoff missing 'Interface contract' section"
            )
            assert "## Implementation order" in body, (
                "handoff missing 'Implementation order' section"
            )


class TestB8_CrossContractCheckFailure:
    """contract_check parse error / rejection must mark the cross run
    ``status="failed"`` (CLI exits non-zero). The reviewer-gate spine is
    JSON-only; malformed output is a hard failure, not silent success.
    """

    def test_approved_contract_check_keeps_status_done(self, cross_clean_snapshot) -> None:
        """Happy path: all contract checks approved → session.status='done'.
        Pins the inverse of the failure invariant so a regression in
        either direction surfaces."""
        result = cross_clean_snapshot["session"]
        assert result["status"] == "done"
        # Every alias's contract check carries an APPROVED verdict.
        for entry in result["phases"]["contract_check"].values():
            assert entry["verdict"] == "APPROVED"

    def test_contract_check_parse_error_pauses_cross_run_via_cfa_gate(
        self, cross_projects, cross_run_dir, clean_review_provider,
    ) -> None:
        """Reviewer JSON that fails to parse on the FINAL contract check
        feeds the CFA gate as a precondition blocker → CFA REJECTED →
        Phase A pause. Pre-Phase-A this branch terminated as
        status=failed with a ``parse error``-naming failure_reason.

        The reviewer-gate spine is still JSON-only; prose / malformed
        output is still a hard rejection — but at the cross level the
        rejection now routes through the recovery contract (override
        or halt) instead of terminating the run."""
        class _GarbageContractCheck:
            model = "stub-codex"
            session_id: str | None = None

            def invoke(self, prompt, cwd, *, mutates_artifacts=False,
                       continue_session=False, attachments=()) -> str:
                # ADR 0025: per-project final_acceptance uses release_json.
                if _prompt_requests_release(prompt):
                    return _approved_release_json()
                # Cross-level contract_check (artifact-bundle) carries
                # the bundle's "# Cross-contract bundle" header. Emit
                # garbage so ``parse_review`` raises and the
                # orchestrator surfaces it as a run failure. Every
                # other reviewer prompt (plan validation, per-project
                # review_changes, etc.) is APPROVED so the run reaches
                # the terminal gate.
                if "Cross-contract bundle" in prompt:
                    return "this is not JSON at all"
                return _approved_review_json()

            def reset_session(self) -> None:
                self.session_id = None

        clean_review_provider.codex = lambda model, **_kw: _GarbageContractCheck()
        result = _cross_run("Task", cross_projects=cross_projects,
                            cross_run_dir=cross_run_dir,
                            provider=clean_review_provider)
        # Phase A — contract_check parse error → CFA precondition →
        # pause with narrowed actions (no reviewer ran on the CFA
        # agent path, so retry_feedback is omitted).
        assert result["status"] == "awaiting_phase_handoff"
        handoff = result.get("phase_handoff") or {}
        assert handoff.get("id", "").startswith("cfa:")
        assert handoff.get("available_actions") == ["continue", "halt"]
        gate = result["phases"]["cross_final_acceptance"]
        assert gate["source"] == "precondition"

    def test_rejected_contract_check_pauses_cross_run_via_cfa_gate(
        self, cross_projects, cross_run_dir, clean_review_provider,
    ) -> None:
        # Phase A — contract_check REJECTED feeds the CFA gate as a
        # precondition blocker. CFA REJECTS via source=precondition,
        # which pauses the run on a ``cfa:``-prefixed handoff with
        # narrowed actions ``[continue, halt]``. Pre-Phase-A this
        # branch terminated as status=failed.
        # Swap the reviewer stub to a contract-check-rejecter for the
        # FINAL contract check pass — initial plan validation still gets
        # APPROVED (filtered by the plan-validation prompt marker).
        class _RejectContractCheck:
            model = "stub-codex"
            session_id: str | None = None
            _call_count = 0

            def invoke(self, prompt, cwd, *, mutates_artifacts=False,
                       continue_session=False, attachments=()) -> str:
                # ADR 0025: per-project final_acceptance uses release_json.
                # The test rejects the cross-level contract_check, not
                # the per-project release gates.
                if _prompt_requests_release(prompt):
                    return _approved_release_json()
                # Cross-level contract_check (artifact-bundle) carries
                # the bundle's "# Cross-contract bundle" header. Reject
                # only that path; every other reviewer prompt (plan
                # validation, per-project reviewers) is APPROVED so
                # the run reaches the terminal gate.
                if "Cross-contract bundle" in prompt:
                    return _rejected_review_json(
                        "Schema field drift between api and web."
                    )
                return _approved_review_json()

            def reset_session(self) -> None:
                self.session_id = None

        clean_review_provider.codex = lambda model, **_kw: _RejectContractCheck()
        result = _cross_run("Task", cross_projects=cross_projects,
                            cross_run_dir=cross_run_dir,
                            provider=clean_review_provider)
        assert result["status"] == "awaiting_phase_handoff", (
            f"Phase A: contract_check REJECTED must pause via the CFA "
            f"gate, not status=failed; got {result.get('status')!r}"
        )
        handoff = result.get("phase_handoff") or {}
        assert handoff.get("id", "").startswith("cfa:")
        assert handoff.get("available_actions") == ["continue", "halt"]


class TestB9_CrossReviewProfile:
    """``cross --profile review`` runs with no global planning. No
    cross_plan.md should be produced, no handoff artifacts should be
    written, and the child banner must not promise a handoff that never
    existed.
    """

    def _cross_review(self, *, cross_projects, cross_run_dir, provider):
        from pipeline.cross_project.orchestrator import run_cross_pipeline
        lp, hu, gd = _patches()
        with (
            lp, hu, gd,
            patch("pipeline.cross_project.run_setup.load_plugin",
                  return_value=PLUGIN),
        ):
            return run_cross_pipeline(
                task="Audit",
                projects=cross_projects,
                output_dir=cross_run_dir,
                provider=provider,
                profile_name="delivery_audit",
            )

    def test_no_cross_plan_md_produced(
        self, cross_projects, cross_run_dir, clean_review_provider,
    ) -> None:
        self._cross_review(cross_projects=cross_projects,
                           cross_run_dir=cross_run_dir,
                           provider=clean_review_provider)
        assert not (cross_run_dir / "cross_plan.md").exists()

    def test_no_handoff_artifacts_per_alias(
        self, cross_projects, cross_run_dir, clean_review_provider,
    ) -> None:
        self._cross_review(cross_projects=cross_projects,
                           cross_run_dir=cross_run_dir,
                           provider=clean_review_provider)
        for alias in cross_projects:
            assert not (cross_run_dir / alias / "implementation_handoff.md").exists()
            assert not (cross_run_dir / alias / "implementation_handoff.json").exists()

    def test_child_run_start_records_no_handoff_path(
        self, cross_projects, cross_run_dir, clean_review_provider,
    ) -> None:
        """Children launched by a planless cross run see no cross handoff
        (``state.extras['cross_handoff']`` is empty) and the projected
        profile contains no implement/repair phases so handoff_path is
        legitimately None.
        """
        self._cross_review(cross_projects=cross_projects,
                           cross_run_dir=cross_run_dir,
                           provider=clean_review_provider)
        # Each child's meta records plan_source=cross even when no
        # cross-level plan was produced — the discriminator is the run
        # mode, not the presence of a handoff.
        for alias in cross_projects:
            meta = json.loads((cross_run_dir / alias / "meta.json").read_text())
            assert meta["plan_source"] == "cross"


class TestB10_CrossFinalAcceptance:
    """ADR 0025 Phase 3: system release gate runs once after
    contract_check and writes the terminal session.status. The gate has
    two paths (precondition / agent), discriminated by ``source``.
    """

    def test_happy_path_records_approved_gate(self, cross_clean_snapshot) -> None:
        result = cross_clean_snapshot["session"]
        assert result["status"] == "done"
        gate = result["phases"]["cross_final_acceptance"]
        assert gate["verdict"] == "APPROVED"
        assert gate["ship_ready"] is True
        assert gate["source"] == "agent"
        assert gate["release_blockers"] == []
        assert gate["contract_status"]["task_contract"] == "satisfied"

    def test_child_reject_blocks_via_precondition(
        self, cross_projects, cross_run_dir, clean_review_provider,
    ) -> None:
        """A per-project release REJECTED makes the system gate
        synthesise a CFA_CHILD_REJECTED_<alias> blocker without
        invoking the cross agent."""

        # The Phase 1 codex stub returns release JSON for any
        # release_json prompt. Force the FIRST alias's
        # final_acceptance to REJECT by routing its specific prompt
        # through a rejected payload while the rest stay approved.
        target_alias = next(iter(cross_projects))

        class _RejectFirstAliasRelease:
            model = "stub-codex"
            session_id: str | None = None

            def __init__(self):
                self._rejected_for_target = False

            def invoke(self, prompt, cwd, *, mutates_artifacts=False,
                       continue_session=False, attachments=()) -> str:
                # The per-project final_acceptance prompt runs inside
                # the child sub-pipeline with cwd = project path. The
                # first release prompt we see in the target alias's
                # cwd gets the rejection.
                if _prompt_requests_release(prompt):
                    if (
                        not self._rejected_for_target
                        and target_alias in cwd
                    ):
                        self._rejected_for_target = True
                        return json.dumps({
                            "verdict": "REJECTED",
                            "ship_ready": False,
                            "short_summary": "Child release blocker",
                            "release_blockers": [{
                                "id": "C1",
                                "severity": "P1",
                                "title": "Per-project blocker",
                                "body": "blocker body",
                                "required_fix": "fix it",
                                "why_blocks_release": "broken contract",
                            }],
                            "verification_gaps": [],
                            "contract_status": {
                                "task_contract": "incomplete",
                                "interfaces": "broken",
                                "persistence": "safe",
                                "tests": "weak",
                            },
                        })
                    return _approved_release_json()
                return _approved_review_json()

            def reset_session(self) -> None:
                self.session_id = None

        clean_review_provider.codex = lambda model, **_kw: _RejectFirstAliasRelease()
        result = _cross_run("Task", cross_projects=cross_projects,
                            cross_run_dir=cross_run_dir,
                            provider=clean_review_provider)
        # Phase A — child reject feeds CFA precondition → pause.
        assert result["status"] == "awaiting_phase_handoff"
        gate = result["phases"]["cross_final_acceptance"]
        assert gate["source"] == "precondition"
        ids = [b["id"] for b in gate["release_blockers"]]
        assert f"CFA_CHILD_REJECTED_{target_alias}" in ids
        handoff = result.get("phase_handoff") or {}
        assert handoff.get("id", "").startswith("cfa:")
        assert handoff.get("available_actions") == ["continue", "halt"]

    def test_contract_check_reject_blocks_via_precondition(
        self, cross_projects, cross_run_dir, clean_review_provider,
    ) -> None:
        """contract_check REJECTED feeds into the gate as a
        CFA_CONTRACT_REJECTED_<alias> blocker; the system gate
        synthesises the rejection (no agent call) and sets
        status=failed. ``failure_reason`` references
        cross_final_acceptance so log greppers can locate the gate as
        the terminal cause."""

        class _RejectContractOnly:
            model = "stub-codex"
            session_id: str | None = None

            def invoke(self, prompt, cwd, *, mutates_artifacts=False,
                       continue_session=False, attachments=()) -> str:
                # release_json prompts (per-project + cross_final): APPROVED.
                if _prompt_requests_release(prompt):
                    return _approved_release_json()
                # Cross-level contract_check (artifact-bundle): REJECTED.
                # Plan validation and per-project reviewers stay APPROVED
                # so cross_final_acceptance reaches the precondition path
                # with a REJECTED contract_check entry.
                if "Cross-contract bundle" in prompt:
                    return _rejected_review_json(
                        "Schema field drift",
                        summary="Interface contracts disagree",
                    )
                return _approved_review_json()

            def reset_session(self) -> None:
                self.session_id = None

        clean_review_provider.codex = lambda model, **_kw: _RejectContractOnly()
        result = _cross_run("Task", cross_projects=cross_projects,
                            cross_run_dir=cross_run_dir,
                            provider=clean_review_provider)
        # Phase A — contract_check reject feeds CFA precondition → pause.
        assert result["status"] == "awaiting_phase_handoff"
        gate = result["phases"]["cross_final_acceptance"]
        assert gate["source"] == "precondition"
        ids = [b["id"] for b in gate["release_blockers"]]
        assert any(b.startswith("CFA_CONTRACT_REJECTED_") for b in ids)
        # contract_status.interfaces marked broken from observable
        # contract_check verdicts.
        assert gate["contract_status"]["interfaces"] == "broken"
        handoff = result.get("phase_handoff") or {}
        assert handoff.get("id", "").startswith("cfa:")
        assert handoff.get("available_actions") == ["continue", "halt"]

    def test_crashed_child_blocks_via_precondition(
        self, cross_projects, cross_run_dir, clean_review_provider,
    ) -> None:
        """A child sub-pipeline that crashes mid-flight is caught by
        the runner into ``{status: failed, error: ...}``. The cross
        runner reaches the gate, which surfaces a
        CFA_MISSING_CHILD_<alias> blocker rather than re-raising before
        the gate."""

        target_alias = next(iter(cross_projects))

        # ADR 0046 Phase D: cross now dispatches via
        # ``run_project_pipeline(request)`` instead of the legacy
        # 28-kwarg ``run_pipeline(**kwargs)`` wrapper. Patch the new
        # symbol at the project_dispatch import site and read
        # ``project_alias`` off the typed request.
        from pipeline.cross_project import project_dispatch

        real_run_project_pipeline = project_dispatch.run_project_pipeline

        def _crash_target(request):
            if request.project_alias == target_alias:
                raise RuntimeError("simulated child crash")
            return real_run_project_pipeline(request)

        with patch(
            "pipeline.cross_project.project_dispatch.run_project_pipeline",
            side_effect=_crash_target,
        ):
            result = _cross_run("Task", cross_projects=cross_projects,
                                cross_run_dir=cross_run_dir,
                                provider=clean_review_provider)

        # Phase A — crashed child → CFA precondition → pause.
        assert result["status"] == "awaiting_phase_handoff"
        gate = result["phases"]["cross_final_acceptance"]
        assert gate["source"] == "precondition"
        ids = [b["id"] for b in gate["release_blockers"]]
        assert f"CFA_MISSING_CHILD_{target_alias}" in ids
        # The crashed child's sub-session is recorded as failed (not
        # absent) — the runner caught the exception rather than
        # re-raising.
        child_entry = result["phases"]["projects"][target_alias]
        assert child_entry["status"] == "failed"
        assert "simulated child crash" in child_entry["error"]
        handoff = result.get("phase_handoff") or {}
        assert handoff.get("id", "").startswith("cfa:")
        assert handoff.get("available_actions") == ["continue", "halt"]

    def test_gate_verdict_event_emitted_before_run_end(
        self, cross_projects, cross_run_dir, clean_review_provider,
    ) -> None:
        """Event-spine ordering invariant: cross_final_acceptance.verdict
        is recorded before run.end on the events stream."""
        _cross_run("Task", cross_projects=cross_projects,
                   cross_run_dir=cross_run_dir,
                   provider=clean_review_provider)
        events = [
            json.loads(line)
            for line in (cross_run_dir / "events.jsonl").read_text().splitlines()
            if line.strip()
        ]
        kinds = [e["kind"] for e in events]
        gate_idx = kinds.index("cross_final_acceptance.verdict")
        # Child sub-pipelines emit their own run.end events earlier on
        # the spine; the cross-level terminal run.end is the last one.
        run_end_idx = len(kinds) - 1 - kinds[::-1].index("run.end")
        assert gate_idx < run_end_idx, (
            "cross_final_acceptance.verdict must be emitted before "
            "the cross-level run.end on the events spine — terminal "
            "status depends on the gate verdict"
        )
        # Verdict payload carries the registered keys.
        verdict_event = events[gate_idx]
        for key in ("approved", "verdict", "ship_ready",
                    "source", "short_summary"):
            assert key in verdict_event["payload"], (
                f"cross_final_acceptance.verdict payload missing {key!r}"
            )

    def test_gate_phase_events_not_classified_as_canonical_rail(
        self, cross_projects, cross_run_dir, clean_review_provider,
    ) -> None:
        """cross_final_acceptance is a cross-only milestone, not one of
        the seven canonical dashboard-rail phase kinds. Its phase.start /
        phase.end events must carry ``phase_kind=None`` (i.e. missing or
        explicitly null in the payload) — tagging it as VALIDATE_PLAN
        would mislead Web / MCP consumers into rendering the system
        release gate on the validate-plan rail.
        """
        _cross_run("Task", cross_projects=cross_projects,
                   cross_run_dir=cross_run_dir,
                   provider=clean_review_provider)
        events = [
            json.loads(line)
            for line in (cross_run_dir / "events.jsonl").read_text().splitlines()
            if line.strip()
        ]
        gate_phase_events = [
            e for e in events
            if e["kind"] in ("phase.start", "phase.end")
            and e.get("payload", {}).get("title", "").startswith(
                "CROSS-FINAL-ACCEPTANCE"
            )
        ]
        assert len(gate_phase_events) >= 2, (
            "expected at least phase.start + phase.end for the gate; "
            f"found {len(gate_phase_events)}"
        )
        for ev in gate_phase_events:
            assert ev["payload"].get("phase_kind") is None, (
                "cross_final_acceptance phase events must not claim a "
                "canonical phase_kind — got "
                f"{ev['payload'].get('phase_kind')!r}"
            )

    def test_release_summary_evidence_carries_cross_gate(
        self, cross_projects, cross_run_dir, clean_review_provider, tmp_path,
    ) -> None:
        """Evidence end-to-end: a REJECTED gate produces a release_summary
        entry for ``cross_final_acceptance`` plus findings (via SDK
        list_findings) carrying the CFA_* blocker ids."""

        target_alias = next(iter(cross_projects))

        class _RejectFirstAlias:
            model = "stub-codex"
            session_id: str | None = None
            _rejected = False

            def invoke(self, prompt, cwd, *, mutates_artifacts=False,
                       continue_session=False, attachments=()) -> str:
                if _prompt_requests_release(prompt):
                    if not self._rejected and target_alias in cwd:
                        self._rejected = True
                        return json.dumps({
                            "verdict": "REJECTED",
                            "ship_ready": False,
                            "short_summary": "Child release blocker",
                            "release_blockers": [{
                                "id": "C1", "severity": "P1",
                                "title": "Per-project blocker",
                                "body": "blocker body",
                                "required_fix": "fix it",
                                "why_blocks_release": "broken contract",
                            }],
                            "verification_gaps": [],
                            "contract_status": {
                                "task_contract": "incomplete",
                                "interfaces": "broken",
                                "persistence": "safe",
                                "tests": "weak",
                            },
                        })
                    return _approved_release_json()
                return _approved_review_json()

            def reset_session(self) -> None:
                self.session_id = None

        clean_review_provider.codex = lambda model, **_kw: _RejectFirstAlias()
        _cross_run("Task", cross_projects=cross_projects,
                   cross_run_dir=cross_run_dir,
                   provider=clean_review_provider)

        # Direct collector + SDK reads against the cross run dir
        # (cross runs persist meta.json identically).
        from pipeline.evidence.collector import collect_evidence
        bundle = collect_evidence(cross_run_dir)
        # release_summary carries an entry for cross_final_acceptance.
        summaries = {s["phase"]: s for s in bundle["release_summary"]}
        assert "cross_final_acceptance" in summaries
        cfa = summaries["cross_final_acceptance"]
        assert cfa["ship_ready"] is False
        assert cfa["verdict"] == "REJECTED"
        # Release blocker with why_blocks_release survives in raw
        # evidence (the field the review-shape mirror drops).
        cfa_blockers = cfa["release_blockers"]
        assert cfa_blockers
        assert all("why_blocks_release" in b for b in cfa_blockers)
        # findings slice picks up the same blockers via the SDK /
        # _phase_attempts normalizer (Phase 1 follow-up cf6bde4 +
        # Phase 3 tuple extension).
        cfa_findings = [
            f for f in bundle["findings"]
            if f["phase"] == "cross_final_acceptance"
        ]
        assert cfa_findings, (
            "evidence findings slice must surface the cross gate's "
            "release blockers via the dual-shape mirror"
        )


def test_cross_clean_smoke_e2e(cross_projects, cross_run_dir, clean_review_provider) -> None:
    """Sentinel for the clean cross-project scenario.

    Re-runs the cross scenario from scratch each test (function-scoped
    fixtures) and asserts the smoke contract. If this fails while the
    ``cross_clean_snapshot`` consumers keep passing, the snapshot has
    drifted from the real scenario — fix by rebuilding the snapshot, not
    by patching it.
    """
    result = _cross_run("Task", cross_projects=cross_projects,
                        cross_run_dir=cross_run_dir,
                        provider=clean_review_provider)
    assert result["status"] == "done", "sentinel: cross run should reach status=done"
    assert "contract_check" in result["phases"]
    assert set(result["phases"]["projects"]) == set(cross_projects)
    assert (cross_run_dir / "cross_plan.md").exists()
    for alias in cross_projects:
        assert (cross_run_dir / alias / "meta.json").exists(), (
            f"sentinel: {alias}/meta.json missing"
        )


class TestB6_TaskPropagated:
    def test_task_in_all_sub_sessions(self, cross_projects, cross_run_dir, clean_review_provider) -> None:
        task = "Cross-project structured logging"
        result = _cross_run(task, cross_projects=cross_projects,
                            cross_run_dir=cross_run_dir, provider=clean_review_provider)
        assert result["task"] == task
        for name, sub in result["phases"]["projects"].items():
            # sub-task may equal task or be a subtask extracted from cross-plan
            assert "task" in sub, f"'task' key missing in sub-session '{name}'"


# ═════════════════════════════════════════════════════════════════════════════
# C. Plan Human Review (PipelineMode.PLAN → human approval → --resume)
#
# Workflow:
#   1. User runs:  ma run --mode plan --task "..."
#   2. Pipeline executes PLAN + VALIDATE_PLAN, then STOPS.
#   3. Human reads plan artifacts, decides: approve / reject with corrections.
#   4a. Approve → ma run --mode task --task "..." (resumes from BUILD)
#   4b. Reject  → ma run --mode plan --task "..." --corrections "fix X"
#                 (replan with corrections injected, then stop again)
#
# This section tests the contract for steps 1-4.
# ═════════════════════════════════════════════════════════════════════════════




def _run_plan_only(
    task: str = "Add structured logging",
    *,
    project: Path,
    run_dir: Path,
    provider: MockAgentProvider,
) -> dict:
    """Run pipeline in PLAN-only mode (exits after validate_plan)."""
    lp, hu, gd = _patches()
    with lp, hu, gd:
        return run_pipeline(
            task=task,
            project_dir=str(project),
            output_dir=run_dir,
            max_rounds=1,
            provider=provider,
            profile_name="planning",
        )


def _run_task_only(
    task: str = "Add structured logging",
    *,
    project: Path,
    run_dir: Path,
    provider: MockAgentProvider,
    max_rounds: int = 1,
) -> dict:
    """Run pipeline in TASK mode (skips plan, starts from BUILD)."""
    lp, hu, gd = _patches()
    with lp, hu, gd:
        return run_pipeline(
            task=task,
            project_dir=str(project),
            output_dir=run_dir,
            max_rounds=max_rounds,
            provider=provider,
            profile_name="task",
        )


class TestC1_PlanModeExitsAfterPlanQA:
    """PLAN mode: pipeline runs plan + validate_plan, then stops. No build/review."""

    def test_plan_present(self, project, run_dir, clean_review_provider) -> None:
        s = _run_plan_only(project=project, run_dir=run_dir, provider=clean_review_provider)
        assert "plan" in s["phases"]

    def test_validate_plan_present(self, project, run_dir, clean_review_provider) -> None:
        s = _run_plan_only(project=project, run_dir=run_dir, provider=clean_review_provider)
        assert "validate_plan" in s["phases"]

    def test_build_absent(self, project, run_dir, clean_review_provider) -> None:
        s = _run_plan_only(project=project, run_dir=run_dir, provider=clean_review_provider)
        assert "implement" not in s["phases"]

    def test_rounds_empty(self, project, run_dir, clean_review_provider) -> None:
        s = _run_plan_only(project=project, run_dir=run_dir, provider=clean_review_provider)
        assert s["phases"].get("rounds", []) == []

    def test_final_qa_absent(self, project, run_dir, clean_review_provider) -> None:
        s = _run_plan_only(project=project, run_dir=run_dir, provider=clean_review_provider)
        assert "final_acceptance" not in s["phases"]


class TestC2_PlanOutputInspectable:
    """Session stores plan output so human can review it.

    Stage 3 contract: phases.plan is a list of per-attempt dicts; the last
    entry is the most recent (and only one in single-shot runs).
    """

    def test_plan_output_non_empty(self, project, run_dir, clean_review_provider) -> None:
        s = _run_plan_only(project=project, run_dir=run_dir, provider=clean_review_provider)
        assert len(s["phases"]["plan"][-1]["output"]) > 0

    def test_plan_output_is_string(self, project, run_dir, clean_review_provider) -> None:
        s = _run_plan_only(project=project, run_dir=run_dir, provider=clean_review_provider)
        assert isinstance(s["phases"]["plan"][-1]["output"], str)


class TestC3_MetaJsonCheckpoint:
    """meta.json written — serves as checkpoint for --resume."""

    def test_meta_json_exists(self, project, run_dir, clean_review_provider) -> None:
        _run_plan_only(project=project, run_dir=run_dir, provider=clean_review_provider)
        assert (run_dir / "meta.json").exists()

    def test_meta_json_has_plan_phase(self, project, run_dir, clean_review_provider) -> None:
        _run_plan_only(project=project, run_dir=run_dir, provider=clean_review_provider)
        data = json.loads((run_dir / "meta.json").read_text())
        assert "plan" in data["phases"]

    def test_meta_json_no_build_phase(self, project, run_dir, clean_review_provider) -> None:
        _run_plan_only(project=project, run_dir=run_dir, provider=clean_review_provider)
        data = json.loads((run_dir / "meta.json").read_text())
        assert "implement" not in data["phases"]


class TestC4_PlanQACritiqueAccessible:
    """validate_plan critique is stored so human can see reviewer feedback."""

    def test_critique_in_validate_plan(self, project, run_dir, clean_review_provider) -> None:
        s = _run_plan_only(project=project, run_dir=run_dir, provider=clean_review_provider)
        assert "critique" in s["phases"]["validate_plan"][-1]

    def test_critique_is_string(self, project, run_dir, clean_review_provider) -> None:
        s = _run_plan_only(project=project, run_dir=run_dir, provider=clean_review_provider)
        assert isinstance(s["phases"]["validate_plan"][-1]["critique"], str)


class TestC5_PlanFileWritten:
    """Plan .md file written to artifacts dir for human reading.

    Когда orchestrator получает ``output_dir`` (всегда в наших тестах),
    он подменяет ``plugin.ma_artifacts_dir`` на абсолютный путь run_dir-а
    (см. project_orchestrator.py: "Centralise артефакты"). Поэтому
    plan_*.md лежит в run_dir, а не в project/.orcho/artifacts/.
    """

    def test_plan_md_exists(self, project, run_dir, clean_review_provider) -> None:
        _run_plan_only(project=project, run_dir=run_dir, provider=clean_review_provider)
        plans = list(run_dir.glob("plan_*.md"))
        assert plans, f"No plan_*.md in {run_dir}"

    def test_plan_md_has_content(self, project, run_dir, clean_review_provider) -> None:
        _run_plan_only(project=project, run_dir=run_dir, provider=clean_review_provider)
        plans = sorted(run_dir.glob("plan_*.md"), key=lambda f: f.stat().st_mtime)
        assert len(plans[-1].read_text()) > 0


class TestC6_ProgressLogPlanOnly:
    """Progress log shows PLAN completion without BUILD markers."""

    def test_plan_in_progress_log(self, project, run_dir, clean_review_provider) -> None:
        _run_plan_only(project=project, run_dir=run_dir, provider=clean_review_provider)
        log = (run_dir / "progress.log").read_text()
        assert "[PLAN]" in log

    def test_no_build_in_progress_log(self, project, run_dir, clean_review_provider) -> None:
        _run_plan_only(project=project, run_dir=run_dir, provider=clean_review_provider)
        log = (run_dir / "progress.log").read_text()
        assert "[IMPLEMENT]" not in log


class TestC7_SessionStatus:
    """Session status field marks plan-only run as awaiting phase handoff.

    Phase 5 cutover: the ``plan`` built-in profile now declares
    ``handoff: human_feedback_always`` on validate_plan, so the run
    pauses on every verdict (approved or rejected) instead of finalising
    as ``awaiting_human_review``. The human decides via the SDK.
    """

    def test_status_awaiting_phase_handoff_on_plan_profile(
        self, project, run_dir, clean_review_provider,
    ) -> None:
        s = _run_plan_only(project=project, run_dir=run_dir, provider=clean_review_provider)
        assert s.get("status") == "awaiting_phase_handoff"
        # Plan profile = human_feedback_always; round-1 approval pauses
        # with the full action set so the human can disagree with the
        # reviewer agent's approval via retry_feedback.
        handoff = s["phase_handoff"]
        assert handoff["type"] == "human_feedback_always"
        assert handoff["trigger"] == "approved"
        assert sorted(handoff["available_actions"]) == [
            "continue", "halt", "retry_feedback",
        ]

    def test_full_pipeline_status_done(self, project, run_dir, clean_review_provider) -> None:
        s = _run(project=project, run_dir=run_dir, provider=clean_review_provider)
        assert s.get("status") == "done"


class TestC8_ResumeApproved:
    """Two-phase resume: PLAN → human approves → TASK continues from BUILD.

    Simulates the human-in-the-loop workflow:
      1. Run PLAN mode → get plan
      2. Human approves (no corrections)
      3. Run TASK mode → picks up from BUILD
    """

    def test_plan_then_task_produces_build(self, project, tmp_path, clean_review_provider) -> None:
        # Phase 1: PLAN only
        plan_dir = tmp_path / "run_plan"
        plan_dir.mkdir()
        s_plan = _run_plan_only(project=project, run_dir=plan_dir, provider=clean_review_provider)
        assert "plan" in s_plan["phases"]
        assert "implement" not in s_plan["phases"]

        # Phase 2: TASK only (human approved, no corrections)
        task_dir = tmp_path / "run_task"
        task_dir.mkdir()
        s_task = _run_task_only(project=project, run_dir=task_dir, provider=clean_review_provider)
        assert "implement" in s_task["phases"]
        assert "plan" not in s_task["phases"]

    def test_task_mode_has_review_rounds(self, project, tmp_path, clean_review_provider) -> None:
        task_dir = tmp_path / "run_task"
        task_dir.mkdir()
        s = _run_task_only(project=project, run_dir=task_dir, provider=clean_review_provider)
        assert len(s["phases"].get("rounds", [])) >= 1

    def test_task_mode_has_final_qa(self, project, tmp_path, clean_review_provider) -> None:
        task_dir = tmp_path / "run_task"
        task_dir.mkdir()
        s = _run_task_only(project=project, run_dir=task_dir, provider=clean_review_provider)
        assert "final_acceptance" in s["phases"]


class TestC9_ResumeWithCorrections:
    """Two-phase resume with corrections: PLAN → human rejects → replan.

    Simulates:
      1. PLAN mode → plan produced
      2. Human adds corrections (text feedback)
      3. Run PLAN mode again with corrections in task (triggers replan)
      4. Then TASK mode after approval
    """

    def test_replan_with_corrections(self, project, tmp_path, issue_provider) -> None:
        """When Codex finds issues in plan, replan_output is recorded."""
        plan_dir = tmp_path / "run_plan"
        plan_dir.mkdir()
        s = _run_plan_only(project=project, run_dir=plan_dir, provider=issue_provider)
        # The issue_provider's plan artifact review returns approved JSON while
        # review_uncommitted returns issues. Verify the phase ran regardless.
        assert "validate_plan" in s["phases"]

    def test_corrections_in_task_string(self, project, tmp_path, clean_review_provider) -> None:
        """Human corrections can be prepended to task for replan pass."""
        corrections = "CORRECTIONS: add error handling to auth module"
        combined_task = f"{corrections}\n\nOriginal task: Add structured logging"

        plan_dir = tmp_path / "run_corrections"
        plan_dir.mkdir()
        s = _run_plan_only(
            combined_task,
            project=project,
            run_dir=plan_dir,
            provider=clean_review_provider,
        )
        assert s["task"] == combined_task
        assert "plan" in s["phases"]

    def test_full_correction_cycle(self, project, tmp_path, clean_review_provider) -> None:
        """Full cycle: PLAN → corrections → re-PLAN → approve → TASK."""
        # Step 1: Initial plan
        dir1 = tmp_path / "run1_plan"
        dir1.mkdir()
        s1 = _run_plan_only(project=project, run_dir=dir1, provider=clean_review_provider)
        assert "plan" in s1["phases"]
        assert "implement" not in s1["phases"]

        # Step 2: Replan with corrections
        dir2 = tmp_path / "run2_replan"
        dir2.mkdir()
        s2 = _run_plan_only(
            "CORRECTIONS: also handle auth errors\n\nAdd structured logging",
            project=project,
            run_dir=dir2,
            provider=clean_review_provider,
        )
        assert "plan" in s2["phases"]
        assert "implement" not in s2["phases"]

        # Step 3: Human approves → TASK mode
        dir3 = tmp_path / "run3_task"
        dir3.mkdir()
        s3 = _run_task_only(project=project, run_dir=dir3, provider=clean_review_provider)
        assert "implement" in s3["phases"]
        assert "final_acceptance" in s3["phases"]


class TestC10_CorrectionsPropagation:
    """Verify corrections text appears in plan output (injected into prompt)."""

    def test_task_with_corrections_prefix(self, project, tmp_path, clean_review_provider) -> None:
        corrections_task = "HUMAN REVIEW CORRECTIONS: add retry logic to API calls\n\nAdd logging"
        plan_dir = tmp_path / "run_corr"
        plan_dir.mkdir()
        s = _run_plan_only(corrections_task, project=project,
                           run_dir=plan_dir, provider=clean_review_provider)
        # Task recorded as-is (corrections are part of the task string)
        assert "HUMAN REVIEW CORRECTIONS" in s["task"]


class TestC11_CrossProjectPlanMode:
    """Cross-project in PLAN mode should produce cross_plan.md but NOT run
    per-project sub-pipelines (those run in TASK mode after approval)."""

    def test_cross_plan_mode_no_project_builds(
        self, cross_projects, cross_run_dir, clean_review_provider
    ) -> None:
        from pipeline.cross_project.orchestrator import run_cross_pipeline
        lp, hu, gd = _patches()
        with (
            lp, hu, gd,
            patch("pipeline.cross_project.run_setup.load_plugin", return_value=PLUGIN),
        ):
            result = run_cross_pipeline(
                task="Task",
                projects=cross_projects,
                output_dir=cross_run_dir,
                provider=clean_review_provider,
                cross_mode="plan",
            )
        # cross_plan.md should exist
        assert (cross_run_dir / "cross_plan.md").exists()
        # No per-project sub-pipelines ran
        assert "projects" not in result["phases"]
        # Status is awaiting human review
        assert result["status"] == "awaiting_human_review"


# ═════════════════════════════════════════════════════════════════════════════
# Stage 3: PLAN ↔ validate_plan loop (budget from profile)
# ═════════════════════════════════════════════════════════════════════════════


class _ScriptedValidatePlanCodex:
    """Codex IAgentRuntime stub returning scripted validate_plan responses.

    Each invoke() prompt that carries the plan markdown (validate_plan path)
    pops the next scripted reply. Non-validate_plan reviews always return
    approved JSON so review_changes / final_acceptance don't block.
    """
    model = "stub-codex"
    session_id: str | None = None

    def __init__(self, validate_plan_responses: list[str]) -> None:
        self._responses = list(validate_plan_responses)
        self._n = 0

    def invoke(
        self, prompt: str, cwd: str, *,
        mutates_artifacts: bool = False,
        continue_session: bool = False,
        attachments: tuple = (),
    ) -> str:
        # ADR 0025: project final_acceptance carries release_json — emit
        # the release-shape APPROVED payload so the closing gate doesn't
        # halt under parse_release.
        if _prompt_requests_release(prompt):
            return _approved_release_json()
        # M4: the validate_plan file wrapper composes the normal
        # validate_plan task. PR3 cutover: detection must be
        # delta-safe (round 2+ wire omits cached prefix parts).
        # ``## Tasks`` heading rides in the TURN/NONE
        # plan_tasks:execution_plan part body — present on every
        # round. Earlier markers ``File:`` prefix (PR1) and
        # ``# Implementation Plan`` body header (PR3) were dropped
        # as composition tightened.
        if "## Tasks" in prompt:
            if self._n < len(self._responses):
                r = self._responses[self._n]
                self._n += 1
                return r
            return self._responses[-1] if self._responses else _approved_review_json()
        return _approved_review_json()

    def reset_session(self) -> None:
        self.session_id = None


def _make_provider(validate_plan_responses: list[str]) -> MockAgentProvider:
    p = MockAgentProvider(latency=0.0, test_pass_rate=1.0)
    stub = _ScriptedValidatePlanCodex(validate_plan_responses)
    p.codex = lambda model, **_kw: stub
    return p


def _approved_review_json(summary: str = "No blocking issues.") -> str:
    return json.dumps({
        "verdict": "APPROVED",
        "short_summary": summary,
        "findings": [],
        "risks": [],
        "checks": ["Reviewed change"],
    })


def _approved_release_json(summary: str = "Ship-ready.") -> str:
    """ADR 0025: release-gate APPROVED payload for mock stubs that
    encounter a ``release_json`` reviewer prompt (project
    ``final_acceptance``)."""
    return json.dumps({
        "verdict":            "APPROVED",
        "ship_ready":         True,
        "short_summary":      summary,
        "release_blockers":   [],
        "verification_gaps":  [],
        "contract_status": {
            "task_contract": "satisfied",
            "interfaces":    "not_applicable",
            "persistence":   "not_applicable",
            "tests":         "sufficient",
        },
    })


def _prompt_requests_release(prompt: str) -> bool:
    """Detect the ``release_json`` system-tail block in a reviewer
    prompt — matches the orcho renderer output exactly. Substring
    search for the bare token would false-positive on a review prompt
    that mentions the word.
    """
    return (
        'kind="contract"' in prompt
        and 'name="release_json"' in prompt
        and "<orcho:system-block " in prompt
    )


def _rejected_release_json(
    summary: str = "Blocking defect in the delivery path.",
) -> str:
    """ADR 0025/0106: release-gate REJECTED payload.

    The review/plan loop stays APPROVED; only the closing release gate
    (project ``final_acceptance``) rejects, carrying a real release blocker so
    the rejected terminal (T1) and the persisted decision (T2) have blockers to
    surface.
    """
    return json.dumps({
        "verdict": "REJECTED",
        "ship_ready": False,
        "short_summary": summary,
        "release_blockers": [{
            "id": "RB1",
            "severity": "P0",
            "title": "Data loss on apply",
            "body": "The change drops a column without a migration guard.",
            "required_fix": "Guard the destructive write behind a migration.",
            "why_blocks_release": "Shipping would destroy user data.",
        }],
        "verification_gaps": [],
        "contract_status": {
            "task_contract": "incomplete",
            "interfaces": "broken",
            "persistence": "risky",
            "tests": "missing",
        },
    })


def _rejected_review_json(body: str, *, summary: str | None = None) -> str:
    return json.dumps({
        "verdict": "REJECTED",
        "short_summary": summary or f"P2: {body}",
        "findings": [{
            "id": "F1",
            "severity": "P2",
            "title": "Review finding",
            "body": body,
            "required_fix": f"Address: {body}",
        }],
        "risks": [],
        "checks": ["Reviewed change"],
    })


def _run_no_hypothesis(
    *, project, run_dir, provider, profile_name: str = "feature",
) -> dict:
    """Run pipeline with hypothesis stage forced off so the scripted
    validate_plan stub isn't consumed by an earlier hypothesis-validation call.

    Plan loop budget comes from the active profile's
    ``LoopStep.max_rounds`` (``advanced`` declares 2).
    """
    lp, hu, gd = _patches()
    with lp, hu, gd, patch("pipeline.project.profile_dispatch.maybe_run_hypothesis",
               return_value=(None, [])):
        return run_pipeline(
            task="Add structured logging",
            project_dir=str(project),
            output_dir=run_dir,
            profile_name=profile_name,
            hypothesis_enabled=False,
            provider=provider,
        )


class TestStage3_PlanQaLoop:
    """The plan loop budget lives in the active profile (``advanced``
    declares ``max_rounds=2``)."""

    def test_approved_on_first_round_emits_one_attempt(self, project, run_dir) -> None:
        # Profile-declared budget (advanced.plan_loop.max_rounds=2) is
        # an upper bound — APPROVED on round 1 exits the loop immediately
        # with a single PLAN + single validate_plan attempt.
        provider = _make_provider([_approved_review_json()])
        s = _run_no_hypothesis(project=project, run_dir=run_dir, provider=provider)
        assert len(s["phases"]["plan"]) == 1
        assert len(s["phases"]["validate_plan"]) == 1
        assert s["phases"]["validate_plan"][0]["approved"] is True

    def test_approved_on_first_round_skips_replan(self, project, run_dir) -> None:
        provider = _make_provider([_approved_review_json("Looks good.")])
        s = _run_no_hypothesis(project=project, run_dir=run_dir, provider=provider)
        # Approved on round 1 → only one PLAN + one QA attempt, even
        # though advanced's plan loop declares ``max_rounds=2``.
        assert len(s["phases"]["plan"]) == 1
        assert len(s["phases"]["validate_plan"]) == 1
        assert s["phases"]["validate_plan"][0]["approved"] is True

    def test_rejected_then_approved_runs_two_rounds(self, project, run_dir) -> None:
        provider = _make_provider([
            _rejected_review_json("Missing tests for edge X."),
            _approved_review_json("All addressed."),
        ])
        s = _run_no_hypothesis(project=project, run_dir=run_dir, provider=provider)
        assert len(s["phases"]["plan"]) == 2
        assert len(s["phases"]["validate_plan"]) == 2
        assert s["phases"]["validate_plan"][0]["approved"] is False
        assert s["phases"]["validate_plan"][1]["approved"] is True
        # Round 2 PLAN must have used replan_prompt → carries critique.
        assert "replan_critique" in s["phases"]["plan"][1]
        assert "Missing tests" in s["phases"]["plan"][1]["replan_critique"]

    def test_attempt_indices_are_one_based(self, project, run_dir) -> None:
        provider = _make_provider([
            _rejected_review_json("Rejected."),
            _approved_review_json(),
        ])
        s = _run_no_hypothesis(project=project, run_dir=run_dir, provider=provider)
        plan_attempts = [a["attempt"] for a in s["phases"]["plan"]]
        qa_attempts = [a["attempt"] for a in s["phases"]["validate_plan"]]
        assert plan_attempts == [1, 2]
        assert qa_attempts == [1, 2]

    def test_max_rounds_reached_pauses_via_phase_handoff(self, project, run_dir) -> None:
        # Phase 5 cutover: the built-in ``advanced`` profile declares
        # ``handoff: human_feedback_on_reject`` on validate_plan, so a
        # fully-rejected plan loop pauses at the final round. The
        # legacy best-effort "proceeds to BUILD anyway" path is
        # intentionally gone: the operator must decide via the SDK
        # before downstream phases run.
        provider = _make_provider([
            _rejected_review_json("Issues round 1."),
            _rejected_review_json("Issues round 2."),
        ])
        s = _run_no_hypothesis(project=project, run_dir=run_dir, provider=provider)
        assert len(s["phases"]["validate_plan"]) == 2
        assert all(qa["approved"] is False for qa in s["phases"]["validate_plan"])
        assert s["status"] == "awaiting_phase_handoff"
        assert "implement" not in s["phases"]

    def test_validate_plan_verdict_event_emitted(self, project, run_dir) -> None:
        provider = _make_provider([
            _rejected_review_json("Rejected."),
            _approved_review_json(),
        ])
        _run_no_hypothesis(project=project, run_dir=run_dir, provider=provider)
        from core.observability import events as evstore
        evs = evstore.read_all(run_dir)
        verdicts = [e for e in evs if e.kind == "validate_plan.verdict"]
        assert len(verdicts) == 2
        assert verdicts[0].payload["attempt"] == 1
        assert verdicts[0].payload["approved"] is False
        assert verdicts[1].payload["attempt"] == 2
        assert verdicts[1].payload["approved"] is True


class TestStage5_QualityGate:
    """The built-in advanced profile declares ``human_feedback_on_reject``
    on validate_plan, so a fully-rejected plan loop pauses before BUILD
    via the generic phase-handoff trigger. session.status flips to
    ``awaiting_phase_handoff`` and BUILD/REVIEW/FIX/FINAL_ACCEPTANCE
    never run. Dashboard pivots into ``phase_handoff_review`` state."""

    def test_gate_blocks_when_all_rejected(self, project, run_dir) -> None:
        provider = _make_provider([
            _rejected_review_json("Issue 1."),
            _rejected_review_json("Issue 2."),
        ])
        s = _run_no_hypothesis(project=project, run_dir=run_dir, provider=provider)
        assert s["status"] == "awaiting_phase_handoff"
        # BUILD must NOT have run.
        assert "implement" not in s["phases"]
        # validate_plan list captures all attempts even when gate fires.
        assert len(s["phases"]["validate_plan"]) == 2

    def test_gate_records_round_history(self, project, run_dir) -> None:
        provider = _make_provider([
            _rejected_review_json("Issue 1."),
            _rejected_review_json("Issue 2."),
        ])
        s = _run_no_hypothesis(project=project, run_dir=run_dir, provider=provider)
        # New payload (compat mirror of meta.phase_handoff) carries the
        # current-round summary. Full per-round history lives in
        # session["phases"]["validate_plan"] as before.
        handoff = s["phase_handoff"]
        assert handoff["phase"] == "validate_plan"
        assert handoff["type"] == "human_feedback_on_reject"
        assert handoff["trigger"] == "rejected"
        assert handoff["round"] == 2
        assert handoff["loop_max_rounds"] == 2
        assert "Issue 2" in handoff["last_output"]
        # The review/edit UI needs the plan file path to render the
        # human-review screen; the validate_plan handler writes
        # ``plan_file`` to the phase log and the runner surfaces it
        # via ``artifacts`` in the persisted payload.
        assert handoff["artifacts"].get("plan_file"), (
            "expected plan_file in phase_handoff.artifacts"
        )
        # Per-round history still on session["phases"]["validate_plan"].
        rounds = s["phases"]["validate_plan"]
        assert len(rounds) == 2
        assert all(r["approved"] is False for r in rounds)

    def test_gate_emits_blocked_event(self, project, run_dir) -> None:
        provider = _make_provider([
            _rejected_review_json("rej1."),
            _rejected_review_json("rej2."),
        ])
        _run_no_hypothesis(project=project, run_dir=run_dir, provider=provider)
        from core.observability import events as evstore
        evs = evstore.read_all(run_dir)
        # Phase 3 cutover: the legacy ``validate_plan.gate_blocked``
        # event was replaced with the generic ``phase.handoff_requested``
        # emitted by ``_apply_phase_handoff_pause``.
        requested = [e for e in evs if e.kind == "phase.handoff_requested"]
        assert len(requested) == 1
        payload = requested[0].payload
        assert payload["phase"] == "validate_plan"
        assert payload["handoff_type"] == "human_feedback_on_reject"
        assert payload["trigger"] == "rejected"
        assert payload["round"] == 2
        assert payload["handoff_id"] == "validate_plan:plan_round:2"

    def test_gate_skipped_when_disabled(self, project, run_dir) -> None:
        # The built-in advanced profile carries
        # ``handoff: human_feedback_on_reject`` declaratively, so the
        # pause fires on a fully-rejected plan loop. A run that wants
        # to skip the pause must switch profiles (e.g. ``lite``) or
        # author a custom profile with ``handoff: human_bypass``.
        provider = _make_provider([
            _rejected_review_json("rej1."),
            _rejected_review_json("rej2."),
        ])
        s = _run_no_hypothesis(project=project, run_dir=run_dir, provider=provider)
        assert s["status"] == "awaiting_phase_handoff"
        assert "implement" not in s["phases"]

    def test_gate_not_triggered_when_approved(self, project, run_dir) -> None:
        # Approval on round 1 → no gate.
        provider = _make_provider([_approved_review_json()])
        s = _run_no_hypothesis(project=project, run_dir=run_dir, provider=provider)
        assert s["status"] == "done"
        assert "implement" in s["phases"]

    def test_gate_persists_status_to_meta_json(self, project, run_dir) -> None:
        # Dashboard reads meta.json — gate must round-trip via that file.
        provider = _make_provider([
            _rejected_review_json("rej1."),
            _rejected_review_json("rej2."),
        ])
        _run_no_hypothesis(project=project, run_dir=run_dir, provider=provider)
        meta_path = run_dir / "meta.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["status"] == "awaiting_phase_handoff"
        assert "phase_handoff" in meta
        handoff = meta["phase_handoff"]
        assert handoff["phase"] == "validate_plan"
        assert handoff["round"] == 2
        # Full per-round history is on session["phases"]["validate_plan"];
        # the persisted handoff payload only carries current-round info.
        assert len(meta["phases"]["validate_plan"]) == 2

    def test_plan_profile_pauses_via_human_feedback_always(
        self, project, run_dir,
    ) -> None:
        """Phase 5 cutover: the built-in ``plan`` profile declares
        ``handoff: human_feedback_always`` on validate_plan, so every
        round (approved or rejected) pauses for a human decision. A
        first-round rejection still pauses immediately — the orchestrator
        does not need to iterate the full auto budget the way
        ``human_feedback_on_reject`` does. The available action set is
        ``[continue, retry_feedback, halt]`` on both verdicts — the
        human keeps feedback authority even when the reviewer agent
        approved."""
        provider = _make_provider([
            _rejected_review_json("plan-issue-1"),
            _rejected_review_json("plan-issue-2"),
        ])
        lp, hu, gd = _patches()
        with lp, hu, gd, patch(
            "pipeline.project.profile_dispatch.maybe_run_hypothesis",
            return_value=(None, []),
        ):
            s = run_pipeline(
                task="Plan-only profile pause regression",
                project_dir=str(project),
                output_dir=run_dir,
                hypothesis_enabled=False,
                profile_name="planning",
                provider=provider,
            )
        assert s["status"] == "awaiting_phase_handoff"
        handoff = s["phase_handoff"]
        assert handoff["phase"] == "validate_plan"
        assert handoff["type"] == "human_feedback_always"
        assert handoff["trigger"] == "rejected"
        assert handoff["round"] == 1  # always-fires on first verdict
        # rejected verdict offers continue_with_waiver (ADR 0072); approved
        # triggers do not (nothing to waive).
        assert sorted(handoff["available_actions"]) == [
            "continue", "continue_with_waiver", "halt", "retry_feedback",
        ]
        assert handoff["artifacts"].get("plan_file")


class TestStage5_MockPlanQaReject:
    """MockAgentProvider.validate_plan_reject_rounds drives the gate from the
    dashboard / CLI without a real LLM. Distinguishes plan_*.md from
    hypothesis_*.md by filename so the upstream hypothesis loop isn't
    starved by the same reject script."""

    def test_default_zero_keeps_approved_json(self, project, run_dir, clean_review_provider) -> None:
        # Sanity: existing tests rely on default-approve behaviour.
        s = _run_no_hypothesis(project=project, run_dir=run_dir, provider=clean_review_provider)
        assert s["phases"]["validate_plan"][0]["approved"] is True

    def test_reject_one_then_approve(self, project, run_dir) -> None:
        from agents.runtimes import MockAgentProvider
        provider = MockAgentProvider(latency=0.0, validate_plan_reject_rounds=1)
        s = _run_no_hypothesis(project=project, run_dir=run_dir, provider=provider)
        assert len(s["phases"]["validate_plan"]) == 2
        assert s["phases"]["validate_plan"][0]["approved"] is False
        assert s["phases"]["validate_plan"][1]["approved"] is True

    def test_reject_all_triggers_gate(self, project, run_dir) -> None:
        from agents.runtimes import MockAgentProvider
        provider = MockAgentProvider(latency=0.0, validate_plan_reject_rounds=2)
        s = _run_no_hypothesis(project=project, run_dir=run_dir, provider=provider)
        assert s["status"] == "awaiting_phase_handoff"
        assert len(s["phases"]["validate_plan"]) == 2
        assert all(qa["approved"] is False for qa in s["phases"]["validate_plan"])
        assert "implement" not in s["phases"]

    def test_make_provider_propagates_flag(self) -> None:
        from agents.runtimes import make_provider
        p = make_provider(mock=True, mock_validate_plan_reject_rounds=2)
        # Same codex instance is reused across rounds so the counter
        # spans them. Verify on a synthetic plan_*.md path.
        cx = p.codex("model")
        assert cx is p.codex("model")
        a = cx.review_file("plan_20260504.md")
        b = cx.review_file("plan_20260504.md")
        c = cx.review_file("plan_20260504.md")
        assert "REJECTED" in a and "REJECTED" in b
        assert "APPROVED" in c

    def test_hypothesis_files_unaffected(self) -> None:
        # Hypothesis artifact review on hypothesis_*.md must NOT consume the
        # plan-qa reject budget for those legacy mock calls.
        from agents.runtimes import MockAgentProvider
        p = MockAgentProvider(latency=0.0, validate_plan_reject_rounds=1)
        cx = p.codex("model")
        h = cx.review_file("/tmp/hypothesis_abc.md")
        assert "APPROVED" in h
        a = cx.review_file("plan_20260504.md")
        # Plan budget intact: first plan call is rejected.
        assert "REJECTED" in a
        b = cx.review_file("plan_20260504.md")
        assert "APPROVED" in b


class TestStage5_ResumeAuditTrail:
    """Resume after a validate_plan gate Approve must preserve the parent's
    validate_plan rounds in meta.json (and parent events in events.jsonl).
    Without this the audit trail vanishes the moment the user clicks
    "Approve manually & continue to BUILD" — useless for post-mortems."""

    @staticmethod
    def _parent_run_id(run_dir) -> str:
        """Production code uses run_id = run_dir.name (CLI sets output_dir
        to runs/<resume_from>/). The test fixture's run_dir.name is decoupled
        from the orchestrator's freshly-generated session_ts, so we read the
        actual run_id that Run 1 wrote into checkpoints.db."""
        from pipeline.checkpoint import CheckpointStore
        with CheckpointStore(run_dir / "checkpoints.db") as ck:
            runs = ck.list_runs()
        assert runs, "expected at least one run row in checkpoints.db"
        return runs[0]["run_id"]

    def test_resume_hydrates_phases_from_parent(self, project, run_dir) -> None:
        from agents.runtimes import MockAgentProvider
        # Run 1: gate-blocked.
        provider = MockAgentProvider(latency=0.0, validate_plan_reject_rounds=2)
        s1 = _run_no_hypothesis(project=project, run_dir=run_dir,
                                provider=provider)
        assert s1["status"] == "awaiting_phase_handoff"
        run_id = self._parent_run_id(run_dir)

        # Phase 4 resume contract: record the human's decision via the
        # SDK before re-dispatching. ``continue`` simulates the operator
        # clicking "Approve manually & continue to BUILD" in the
        # dashboard — the machine verdict stays rejected, but dispatch
        # proceeds past validate_plan. The SDK locates the run by
        # directory name (``run_dir.name``), which the fixture pins
        # independently of the checkpoint-assigned ``session_ts``.
        from sdk.phase_handoff import phase_handoff_decide
        phase_handoff_decide(
            run_dir.name,
            s1["phase_handoff"]["id"],
            "continue",
            note="audit-trail resume",
            runs_dir=run_dir.parent,
            cwd=None,
        )

        # Run 2: TASK-mode resume into the same run_dir.
        provider2 = MockAgentProvider(latency=0.0)
        provider2.codex = lambda m, **_kw: _AlwaysApproved()

        lp, hu, gd = _patches()
        with lp, hu, gd, patch("pipeline.project.profile_dispatch.maybe_run_hypothesis",
                   return_value=(None, [])):
            s2 = run_pipeline(
                task="Approved continuation",
                project_dir=str(project),
                output_dir=run_dir,   # gate flag dropped on Approve
                profile_name="task",
                resume_from=run_id,
                provider=provider2,
            )

        # Audit trail survived: plan / validate_plan lists hydrated from
        # checkpoint into the resumed session, even though TASK mode
        # never ran the PLAN loop itself.
        assert "plan" in s2["phases"]
        assert "validate_plan" in s2["phases"]
        assert len(s2["phases"]["validate_plan"]) == 2
        assert all(qa.get("approved") is False for qa in s2["phases"]["validate_plan"])
        # And BUILD ran in the resumed session.
        assert "implement" in s2["phases"]

    def test_resume_appends_events_jsonl(self, project, run_dir) -> None:
        from agents.runtimes import MockAgentProvider
        provider = MockAgentProvider(latency=0.0, validate_plan_reject_rounds=2)
        s1 = _run_no_hypothesis(project=project, run_dir=run_dir, provider=provider)
        run_id = self._parent_run_id(run_dir)

        # Snapshot parent events count.
        from core.observability import events as evstore
        parent_events = evstore.read_all(run_dir)
        parent_seq_max = max((e.seq for e in parent_events), default=0)
        assert any(e.kind == "validate_plan.verdict" for e in parent_events)
        assert any(e.kind == "phase.handoff_requested" for e in parent_events)

        # Record continue decision before resume (Phase 4 contract).
        from sdk.phase_handoff import phase_handoff_decide
        phase_handoff_decide(
            run_dir.name,
            s1["phase_handoff"]["id"],
            "continue",
            runs_dir=run_dir.parent,
            cwd=None,
        )

        # Resume.
        provider2 = MockAgentProvider(latency=0.0)
        provider2.codex = lambda m, **_kw: _AlwaysApproved()

        lp, hu, gd = _patches()
        with lp, hu, gd, patch("pipeline.project.profile_dispatch.maybe_run_hypothesis",
                   return_value=(None, [])):
            run_pipeline(
                task="Continuation",
                project_dir=str(project),
                output_dir=run_dir,
                profile_name="task",
                resume_from=run_id,
                provider=provider2,
            )

        all_events = evstore.read_all(run_dir)
        # Parent events still present.
        assert any(e.kind == "validate_plan.verdict" for e in all_events)
        assert any(e.kind == "phase.handoff_requested" for e in all_events)
        # Resume added BUILD events on top, with seq strictly increasing.
        post_resume = [e for e in all_events if e.seq > parent_seq_max]
        assert post_resume, "expected at least one event from the resumed run"
        assert any(e.kind == "phase.start"
                   and e.payload.get("phase_kind") == "IMPLEMENT"
                   for e in post_resume)


class TestPhase5_BuiltinProfileCutover:
    """E2E coverage for the Phase 5 default-handoff matrix on built-in
    profiles. The cutover bakes pause semantics into the profile JSON
    (no longer a runtime flag), so every built-in advanced/enterprise
    run pauses on a fully-rejected plan loop and every plan run pauses
    on every verdict."""

    def test_advanced_profile_pauses_without_runtime_flag(
        self, project, run_dir,
    ) -> None:
        """Reject-everything plan loop pauses via the declared
        ``human_feedback_on_reject`` policy on the built-in advanced
        profile."""
        provider = _make_provider([
            _rejected_review_json("Issue 1."),
            _rejected_review_json("Issue 2."),
        ])
        s = _run_no_hypothesis(
            project=project, run_dir=run_dir, provider=provider,
        )
        assert s["status"] == "awaiting_phase_handoff"
        handoff = s["phase_handoff"]
        assert handoff["type"] == "human_feedback_on_reject"
        assert handoff["trigger"] == "rejected"
        assert handoff["round"] == 2
        # ``implement`` did not run — pause is upstream.
        assert "implement" not in s["phases"]

    def test_advanced_profile_approved_first_round_skips_pause(
        self, project, run_dir,
    ) -> None:
        """``human_feedback_on_reject`` does NOT fire on approved
        verdicts. An approved round 1 closes the loop cleanly and the
        pipeline runs end-to-end."""
        provider = _make_provider([_approved_review_json()])
        s = _run_no_hypothesis(
            project=project, run_dir=run_dir, provider=provider,
        )
        assert s["status"] == "done"
        assert "phase_handoff" not in s
        assert "implement" in s["phases"]

    def test_plan_profile_approved_pauses_with_full_actions(
        self, project, run_dir, clean_review_provider,
    ) -> None:
        """``human_feedback_always`` fires on approved too with the
        full action set. The human can still pick ``retry_feedback`` to
        push back against an approval they disagree with — the bonus
        human-directed round budget reserves the extra round on top of
        ``LoopStep.max_rounds`` so this stays legal."""
        s = _run_plan_only(
            project=project, run_dir=run_dir, provider=clean_review_provider,
        )
        assert s["status"] == "awaiting_phase_handoff"
        handoff = s["phase_handoff"]
        assert handoff["type"] == "human_feedback_always"
        assert handoff["trigger"] == "approved"
        assert sorted(handoff["available_actions"]) == [
            "continue", "halt", "retry_feedback",
        ]

    def test_lite_profile_does_not_pause(
        self, project, run_dir, clean_review_provider,
    ) -> None:
        """``lite`` profile omits the handoff declaration — semantic
        bypass. A clean-review run finishes as ``done`` with no pause
        payload."""
        # Use the standard runner with the lite profile (default in
        # this test harness's _run is advanced; build a tailored
        # invocation here).
        provider = clean_review_provider
        lp, hu, gd = _patches()
        with lp, hu, gd, patch(
            "pipeline.project.profile_dispatch.maybe_run_hypothesis",
            return_value=(None, []),
        ):
            s = run_pipeline(
                task="Lite profile smoke",
                project_dir=str(project),
                output_dir=run_dir,
                hypothesis_enabled=False,
                profile_name="small_task",
                provider=provider,
            )
        assert s["status"] == "done"
        assert "phase_handoff" not in s

class TestStage5_PhaseHandoffResume:
    """Phase 4 resume mechanics: ``continue`` skips the plan loop and
    proceeds, ``retry_feedback`` executes one extra human-directed
    ``plan → validate_plan`` round and either closes the loop on approval
    or chains a fresh handoff on a fresh rejection.

    These tests drive the contract end-to-end through ``run_pipeline``
    + the SDK decision helper; they do not exercise MCP / Web (those
    transport wirings land in later slices)."""

    @staticmethod
    def _parent_run_id(run_dir: Path) -> str:
        from pipeline.checkpoint import CheckpointStore
        with CheckpointStore(run_dir / "checkpoints.db") as ck:
            runs = ck.list_runs()
        assert runs, "expected at least one run row in checkpoints.db"
        return runs[0]["run_id"]

    def _pause_on_rejected_plan(
        self, project: Path, run_dir: Path,
    ) -> dict:
        """Run a single advanced-profile pipeline with two rejected
        validate_plan rounds; the profile declares
        ``human_feedback_on_reject``, so the final-round rejection
        pauses on phase handoff. Return the paused session dict."""
        from agents.runtimes import MockAgentProvider
        provider = MockAgentProvider(latency=0.0, validate_plan_reject_rounds=2)
        s = _run_no_hypothesis(
            project=project, run_dir=run_dir, provider=provider,
        )
        assert s["status"] == "awaiting_phase_handoff"
        return s

    def _pause_on_rejected_review(
        self, project: Path, run_dir: Path,
    ) -> dict:
        from agents.runtimes import MockAgentProvider
        provider = MockAgentProvider(latency=0.0)
        provider.codex = lambda m, **_kw: _AlwaysIssue()
        s = _run(
            project=project,
            run_dir=run_dir,
            provider=provider,
            profile_name="task",
            max_rounds=1,
        )
        assert s["status"] == "awaiting_phase_handoff"
        assert s["phase_handoff"]["phase"] == "review_changes"
        assert s["phase_handoff"]["id"] == "review_changes:repair_round:1"
        assert s["phases"]["rounds"][0].get("repair_output")
        return s

    def test_continue_resume_skips_plan_loop_and_proceeds(
        self, project, run_dir,
    ) -> None:
        from sdk.phase_handoff import phase_handoff_decide
        s_pause = self._pause_on_rejected_plan(project, run_dir)
        handoff_id = s_pause["phase_handoff"]["id"]
        run_id = self._parent_run_id(run_dir)

        # Decide continue.
        phase_handoff_decide(
            run_dir.name, handoff_id, "continue",
            note="proceed past validate_plan",
            runs_dir=run_dir.parent, cwd=None,
        )

        # Resume into the same dir; build / review / final_acceptance
        # should run while plan + validate_plan are skipped because the
        # decision marks them as closed.
        from agents.runtimes import MockAgentProvider
        provider2 = MockAgentProvider(latency=0.0)
        provider2.codex = lambda m, **_kw: _AlwaysApproved()
        lp, hu, gd = _patches()
        with lp, hu, gd, patch(
            "pipeline.project.profile_dispatch.maybe_run_hypothesis",
            return_value=(None, []),
        ):
            s_resume = run_pipeline(
                task="Continue past validate_plan",
                project_dir=str(project),
                output_dir=run_dir,
                profile_name="feature",
                resume_from=run_id,
                hypothesis_enabled=False,
                provider=provider2,
            )

        # The plan loop did not re-execute (still exactly two attempts
        # from the parent run).
        assert len(s_resume["phases"]["validate_plan"]) == 2
        assert all(
            qa.get("approved") is False
            for qa in s_resume["phases"]["validate_plan"]
        )
        # Implement ran.
        assert "implement" in s_resume["phases"]
        # Active handoff payload was cleared; run is no longer paused.
        assert "phase_handoff" not in s_resume
        assert s_resume["status"] != "awaiting_phase_handoff"

    def test_retry_feedback_resume_runs_extra_round_then_closes(
        self, project, run_dir,
    ) -> None:
        """A single retry round, providers approves on the retry → loop
        closes, no new handoff, dispatch continues to implement."""
        from sdk.phase_handoff import phase_handoff_decide
        s_pause = self._pause_on_rejected_plan(project, run_dir)
        handoff_id = s_pause["phase_handoff"]["id"]
        run_id = self._parent_run_id(run_dir)

        phase_handoff_decide(
            run_dir.name, handoff_id, "retry_feedback",
            feedback="please add rollback steps",
            runs_dir=run_dir.parent, cwd=None,
        )

        # Provider for resume approves the next validate_plan round.
        from agents.runtimes import MockAgentProvider
        provider2 = MockAgentProvider(latency=0.0)
        provider2.codex = lambda m, **_kw: _AlwaysApproved()
        lp, hu, gd = _patches()
        with lp, hu, gd, patch(
            "pipeline.project.profile_dispatch.maybe_run_hypothesis",
            return_value=(None, []),
        ):
            s_resume = run_pipeline(
                task="Retry with feedback",
                project_dir=str(project),
                output_dir=run_dir,
                profile_name="feature",
                resume_from=run_id,
                hypothesis_enabled=False,
                provider=provider2,
            )

        # No new handoff — the retry round was approved.
        assert s_resume["status"] != "awaiting_phase_handoff"
        assert "phase_handoff" not in s_resume
        # Three validate_plan attempts now: two rejected from parent +
        # one approved human-directed retry.
        rounds = s_resume["phases"]["validate_plan"]
        assert len(rounds) == 3
        assert rounds[-1].get("approved") is True
        # Build ran after approval.
        assert "implement" in s_resume["phases"]

        # Metrics cross-subprocess aggregation: the final ``metrics.json``
        # must reflect work from BOTH subprocesses. Before the resume-
        # init load_from_disk fix, the resume collector started empty
        # and overwrote the prior snapshot — the rollup showed only the
        # post-resume attempts (1 plan + 1 validate_plan) and lost the
        # two pre-pause attempts entirely.
        metrics_path = run_dir / "metrics.json"
        assert metrics_path.is_file()
        metrics = json.loads(metrics_path.read_text())
        attempts_by_phase: dict[str, list[int]] = {}
        for a in metrics.get("phase_attempts", []):
            attempts_by_phase.setdefault(a["phase"], []).append(a["attempt"])
        # Two plan rounds before pause + one resume round = 3 total.
        assert sorted(attempts_by_phase.get("plan", [])) == [1, 2, 3]
        # Same for validate_plan.
        assert sorted(attempts_by_phase.get("validate_plan", [])) == [1, 2, 3]
        # Implement only ran in the resume subprocess — single attempt.
        assert attempts_by_phase.get("implement") == [1]

    def test_retry_feedback_resume_summary_replays_persisted_decision(
        self, project, run_dir, capsys,
    ) -> None:
        """Summary resume banner replays the ACTUALLY-persisted decision.

        The ``↺ resume from checkpoint`` line must surface the operator's
        stored ``retry_feedback`` action + feedback, read from the handoff
        decision artifact — not a synthetic ``human_feedback`` state field,
        which the checkpoint never carries. Without threading the real
        artifact the decision-replay field silently vanishes, so this test
        fails if the resume line drops back to a bare phase-count banner.
        The round *result* is intentionally absent: the banner prints during
        checkpoint hydration, before the replayed round runs.
        """
        from core.io.ansi import strip_ansi
        from core.observability.logging import apply_output_mode, get_output_mode
        from sdk.phase_handoff import phase_handoff_decide

        s_pause = self._pause_on_rejected_plan(project, run_dir)
        handoff_id = s_pause["phase_handoff"]["id"]
        run_id = self._parent_run_id(run_dir)

        feedback = "add rollback steps to the plan"
        phase_handoff_decide(
            run_dir.name, handoff_id, "retry_feedback",
            feedback=feedback,
            runs_dir=run_dir.parent, cwd=None,
        )

        from agents.runtimes import MockAgentProvider
        provider2 = MockAgentProvider(latency=0.0)
        provider2.codex = lambda m, **_kw: _AlwaysApproved()

        _before = get_output_mode()
        lp, hu, gd = _patches()
        try:
            apply_output_mode("summary")
            capsys.readouterr()  # drop the default-mode pause output
            with lp, hu, gd, patch(
                "pipeline.project.profile_dispatch.maybe_run_hypothesis",
                return_value=(None, []),
            ):
                run_pipeline(
                    task="Retry with feedback",
                    project_dir=str(project),
                    output_dir=run_dir,
                    profile_name="feature",
                    resume_from=run_id,
                    hypothesis_enabled=False,
                    provider=provider2,
                )
            out = strip_ansi(capsys.readouterr().out)
        finally:
            apply_output_mode(_before)

        resume = [ln for ln in out.splitlines() if "resume from checkpoint" in ln]
        assert resume, f"summary resume banner missing:\n{out}"
        assert "decision replay: retry_feedback" in resume[0], resume[0]
        assert feedback in resume[0], resume[0]

    def test_retry_feedback_rejected_again_chains_new_handoff(
        self, project, run_dir,
    ) -> None:
        """Retry round rejected again → fresh handoff at round 3 with a
        separate decision artifact path; dispatch pauses again."""
        from sdk.phase_handoff import phase_handoff_decide
        s_pause = self._pause_on_rejected_plan(project, run_dir)
        first_handoff_id = s_pause["phase_handoff"]["id"]
        run_id = self._parent_run_id(run_dir)

        phase_handoff_decide(
            run_dir.name, first_handoff_id, "retry_feedback",
            feedback="rounds 1+2 missed rollback; add it",
            runs_dir=run_dir.parent, cwd=None,
        )

        # Provider for resume keeps rejecting.
        from agents.runtimes import MockAgentProvider
        provider2 = MockAgentProvider(
            latency=0.0, validate_plan_reject_rounds=99,
        )
        lp, hu, gd = _patches()
        with lp, hu, gd, patch(
            "pipeline.project.profile_dispatch.maybe_run_hypothesis",
            return_value=(None, []),
        ):
            s_resume = run_pipeline(
                task="Retry then reject",
                project_dir=str(project),
                output_dir=run_dir,
                profile_name="feature",
                resume_from=run_id,
                hypothesis_enabled=False,
                provider=provider2,
            )

        # Paused again on a fresh handoff at round 3.
        assert s_resume["status"] == "awaiting_phase_handoff"
        new_handoff = s_resume["phase_handoff"]
        assert new_handoff["round"] == 3
        assert new_handoff["id"] == "validate_plan:plan_round:3"
        assert new_handoff["id"] != first_handoff_id
        # Separate decision artifact paths.
        from sdk.phase_handoff import safe_handoff_id
        first_safe = safe_handoff_id(first_handoff_id)
        second_safe = safe_handoff_id(new_handoff["id"])
        assert first_safe != second_safe
        decisions_dir = run_dir / "phase_handoff_decisions"
        assert (decisions_dir / f"{first_safe}.json").exists()
        # Second decision artifact does NOT exist yet — we haven't
        # decided the new handoff.
        assert not (decisions_dir / f"{second_safe}.json").exists()

    def test_review_continue_resume_skips_repair_loop_and_proceeds(
        self, project, run_dir,
    ) -> None:
        from agents.runtimes import MockAgentProvider
        from sdk.phase_handoff import phase_handoff_decide

        s_pause = self._pause_on_rejected_review(project, run_dir)
        run_id = self._parent_run_id(run_dir)
        phase_handoff_decide(
            run_dir.name,
            s_pause["phase_handoff"]["id"],
            "continue",
            note="accept repaired state",
            runs_dir=run_dir.parent,
            cwd=None,
        )

        provider2 = MockAgentProvider(latency=0.0)
        provider2.codex = lambda m, **_kw: _AlwaysApproved()
        lp, hu, gd = _patches()
        with lp, hu, gd:
            s_resume = run_pipeline(
                task="Continue past review",
                project_dir=str(project),
                output_dir=run_dir,
                profile_name="task",
                resume_from=run_id,
                provider=provider2,
            )

        assert s_resume["status"] == "done"
        assert "phase_handoff" not in s_resume
        assert len(s_resume["phases"]["rounds"]) == 1
        assert "final_acceptance" in s_resume["phases"]

    def test_review_continue_with_waiver_ships_without_reopening_findings(
        self, project, run_dir,
    ) -> None:
        """``continue_with_waiver`` on a rejected review: the repair loop
        is skipped like ``continue``, but a durable waiver is persisted
        and injected into the closing gate so the run ships to ``done``
        without reopening the waived findings."""
        from agents.runtimes import MockAgentProvider
        from sdk.phase_handoff import phase_handoff_decide

        s_pause = self._pause_on_rejected_review(project, run_dir)
        # The runtime must offer the waiver action on a rejected review.
        assert (
            "continue_with_waiver"
            in s_pause["phase_handoff"]["available_actions"]
        )
        run_id = self._parent_run_id(run_dir)
        phase_handoff_decide(
            run_dir.name,
            s_pause["phase_handoff"]["id"],
            "continue_with_waiver",
            feedback="Accepted risk: the flagged issue is known and waived.",
            note="operator waiver",
            runs_dir=run_dir.parent,
            cwd=None,
        )

        provider2 = MockAgentProvider(latency=0.0)
        provider2.codex = lambda m, **_kw: _AlwaysApproved()
        lp, hu, gd = _patches()
        with lp, hu, gd:
            s_resume = run_pipeline(
                task="Continue past review under operator waiver",
                project_dir=str(project),
                output_dir=run_dir,
                profile_name="task",
                resume_from=run_id,
                provider=provider2,
            )

        # Repair loop skipped like continue; run shipped.
        assert s_resume["status"] == "done"
        assert "phase_handoff" not in s_resume
        assert len(s_resume["phases"]["rounds"]) == 1
        assert "final_acceptance" in s_resume["phases"]

        # The waiver was durably persisted to meta so a post-mortem /
        # fresh-process reader can see the run shipped under a waiver.
        waiver = s_resume["phase_handoff_waiver"]
        assert waiver["phase"] == "review_changes"
        assert waiver["waiver_text"] == (
            "Accepted risk: the flagged issue is known and waived."
        )

        # Evidence bundle projects the waiver as a verdict-exception.
        from pipeline.evidence import collect_evidence
        bundle = collect_evidence(run_dir)
        kinds = [e["kind"] for e in bundle["errors"]]
        assert "phase_handoff_waiver" in kinds

    def test_review_retry_feedback_repairs_then_reviews(
        self, project, run_dir,
    ) -> None:
        from agents.runtimes import MockAgentProvider
        from sdk.phase_handoff import phase_handoff_decide

        s_pause = self._pause_on_rejected_review(project, run_dir)
        run_id = self._parent_run_id(run_dir)
        phase_handoff_decide(
            run_dir.name,
            s_pause["phase_handoff"]["id"],
            "retry_feedback",
            feedback="tighten the null-check path",
            runs_dir=run_dir.parent,
            cwd=None,
        )

        provider2 = MockAgentProvider(latency=0.0)
        provider2.codex = lambda m, **_kw: _AlwaysApproved()
        lp, hu, gd = _patches()
        with lp, hu, gd:
            s_resume = run_pipeline(
                task="Retry review with feedback",
                project_dir=str(project),
                output_dir=run_dir,
                profile_name="task",
                resume_from=run_id,
                provider=provider2,
            )

        assert s_resume["status"] == "done"
        assert "phase_handoff" not in s_resume
        rounds = s_resume["phases"]["rounds"]
        assert [r["round"] for r in rounds] == [1, 2]
        assert rounds[1].get("repair_output")
        assert rounds[1]["critique"] == ""
        assert "final_acceptance" in s_resume["phases"]

    def test_review_retry_feedback_rejected_again_repauses(
        self, project, run_dir,
    ) -> None:
        from agents.runtimes import MockAgentProvider
        from sdk.phase_handoff import phase_handoff_decide

        s_pause = self._pause_on_rejected_review(project, run_dir)
        run_id = self._parent_run_id(run_dir)
        phase_handoff_decide(
            run_dir.name,
            s_pause["phase_handoff"]["id"],
            "retry_feedback",
            feedback="try one more repair",
            runs_dir=run_dir.parent,
            cwd=None,
        )

        provider2 = MockAgentProvider(latency=0.0)
        provider2.codex = lambda m, **_kw: _AlwaysIssue()
        lp, hu, gd = _patches()
        with lp, hu, gd:
            s_resume = run_pipeline(
                task="Retry review then reject",
                project_dir=str(project),
                output_dir=run_dir,
                profile_name="task",
                resume_from=run_id,
                provider=provider2,
            )

        assert s_resume["status"] == "awaiting_phase_handoff"
        assert s_resume["phase_handoff"]["id"] == "review_changes:repair_round:2"
        rounds = s_resume["phases"]["rounds"]
        assert [r["round"] for r in rounds] == [1, 2]
        assert "null check" in rounds[1]["critique"]

    def test_halt_decision_refuses_subsequent_resume(
        self, project, run_dir,
    ) -> None:
        """halt is terminal by audit contract. After
        ``phase_handoff_decide(... halt ...)`` flips meta.status to
        ``halted``, an attempted ``run_pipeline(resume_from=...)``
        against the same run must refuse to re-dispatch — the SDK
        cleared ``meta.phase_handoff`` as part of the halt transition,
        so without a guard the orchestrator would silently treat the
        run as "no active payload" and run the pipeline from scratch."""
        from agents.runtimes import MockAgentProvider
        from pipeline.project.bootstrap import PhaseHandoffHaltedError
        from sdk.phase_handoff import phase_handoff_decide

        s_pause = self._pause_on_rejected_plan(project, run_dir)
        handoff_id = s_pause["phase_handoff"]["id"]
        run_id = self._parent_run_id(run_dir)

        phase_handoff_decide(
            run_dir.name, handoff_id, "halt",
            note="give up", runs_dir=run_dir.parent, cwd=None,
        )

        # Verify meta is in the halted-via-handoff state before the
        # resume attempt — the carry-forward guard reads exactly these
        # fields.
        meta = json.loads((run_dir / "meta.json").read_text())
        assert meta["status"] == "halted"
        assert meta["halt_reason"] == "phase_handoff_halt"

        provider2 = MockAgentProvider(latency=0.0)
        lp, hu, gd = _patches()
        with lp, hu, gd, patch(
            "pipeline.project.profile_dispatch.maybe_run_hypothesis",
            return_value=(None, []),
        ), pytest.raises(PhaseHandoffHaltedError):
            run_pipeline(
                task="should-be-refused",
                project_dir=str(project),
                output_dir=run_dir,
                profile_name="feature",
                resume_from=run_id,
                hypothesis_enabled=False,
                provider=provider2,
            )

        # Halt remains terminal — meta.json not overwritten with a
        # fresh ``running`` session.
        meta_after = json.loads((run_dir / "meta.json").read_text())
        assert meta_after["status"] == "halted"
        assert meta_after["halt_reason"] == "phase_handoff_halt"

    def test_halt_finalizes_evidence_and_metrics_bundle(
        self, project, run_dir,
    ) -> None:
        """A run halted from a phase-handoff pause must land on disk
        with the same curated artefacts as a ``done`` run.

        Two on-disk invariants are exercised end-to-end here:

        * ``metrics.json`` is snapshotted at pause time by the
          orchestrator subprocess (it holds the in-memory
          accumulator; the SDK halt path runs in a separate process
          and could not reconstruct the numbers from events alone).
        * ``evidence.json`` is finalised by the SDK halt path so the
          terminal status carries a curated bundle, not just raw
          ``events.jsonl`` + ``meta.json``.
        """
        from pipeline.evidence.schema import validate_bundle
        from sdk.phase_handoff import phase_handoff_decide

        s_pause = self._pause_on_rejected_plan(project, run_dir)
        handoff_id = s_pause["phase_handoff"]["id"]

        # Pause-time invariant: metrics.json reflects the work that
        # actually ran before the rc=4 exit.
        metrics_path = run_dir / "metrics.json"
        assert metrics_path.is_file(), (
            "pause must snapshot metrics.json so a later halt is not "
            "left without a metrics rollup"
        )
        metrics_at_pause = json.loads(metrics_path.read_text())
        assert "phase_attempts" in metrics_at_pause
        # Two validate_plan rounds ran before the handoff fired.
        attempts_phases = [
            a.get("phase") for a in metrics_at_pause["phase_attempts"]
        ]
        assert attempts_phases.count("validate_plan") == 2

        # Halt — terminal transition.
        phase_handoff_decide(
            run_dir.name, handoff_id, "halt",
            note="give up", runs_dir=run_dir.parent, cwd=None,
        )

        # Both curated artefacts land on disk after halt.
        evidence_path = run_dir / "evidence.json"
        assert evidence_path.is_file()
        assert metrics_path.is_file()

        bundle = json.loads(evidence_path.read_text())
        # Structural validity (placeholder shortcuts when collection
        # fails; the v1 path is exercised on the happy mock run).
        validate_bundle(bundle)
        assert bundle["status"] == "halted"
        # Phases that actually ran appear in the rollup; phases that
        # never started are absent (the collector reads from the event
        # spine, not from a static profile shape).
        phase_names = {
            (p.get("name") or "").lower() for p in bundle.get("phases", [])
        }
        assert "plan" in phase_names
        assert "validate_plan" in phase_names
        assert "implement" not in phase_names

    def test_torn_halt_resume_heals_meta_and_refuses(
        self, project, run_dir,
    ) -> None:
        """Torn-write defence: a halt decision artifact exists but meta
        was not yet finalised (still ``awaiting_phase_handoff`` with
        the active payload). Resume must refuse to continue dispatch
        AND heal the meta state so the next launch is rejected by the
        carry-forward guard. Without this defence a torn halt would
        silently degrade into a fresh dispatch.
        """
        from agents.runtimes import MockAgentProvider
        from pipeline.project.bootstrap import PhaseHandoffHaltedError
        from sdk.phase_handoff import phase_handoff_decide, safe_handoff_id

        s_pause = self._pause_on_rejected_plan(project, run_dir)
        handoff_id = s_pause["phase_handoff"]["id"]
        run_id = self._parent_run_id(run_dir)

        # Record halt — but then manually un-finalise meta so it looks
        # like the SDK's halt write landed only partially (artifact
        # there, meta.status still on the pause record).
        phase_handoff_decide(
            run_dir.name, handoff_id, "halt",
            note="terminal", runs_dir=run_dir.parent, cwd=None,
        )
        # Verify the decision artifact is in place.
        assert (
            run_dir / "phase_handoff_decisions"
            / f"{safe_handoff_id(handoff_id)}.json"
        ).is_file()
        # Manually restore the pre-halt meta state (simulate torn write).
        torn_meta = json.loads((run_dir / "meta.json").read_text())
        torn_meta["status"] = "awaiting_phase_handoff"
        torn_meta.pop("halted_at", None)
        torn_meta.pop("halt_reason", None)
        torn_meta["phase_handoff"] = s_pause["phase_handoff"]
        (run_dir / "meta.json").write_text(
            json.dumps(torn_meta, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        provider2 = MockAgentProvider(latency=0.0)
        lp, hu, gd = _patches()
        with (
            lp, hu, gd,
            patch(
                "pipeline.project.profile_dispatch.maybe_run_hypothesis",
                return_value=(None, []),
            ),
            pytest.raises(PhaseHandoffHaltedError),
        ):
            run_pipeline(
                task="should-be-refused-torn",
                project_dir=str(project),
                output_dir=run_dir,
                profile_name="feature",
                resume_from=run_id,
                hypothesis_enabled=False,
                provider=provider2,
            )

        # Resume helper healed the torn meta to a proper halted state.
        meta_after = json.loads((run_dir / "meta.json").read_text())
        assert meta_after["status"] == "halted"
        assert meta_after["halt_reason"] == "phase_handoff_halt"
        assert "phase_handoff" not in meta_after
        # Dispatch did NOT proceed past validate_plan.
        assert "implement" not in meta_after.get("phases", {})

    def test_corrupt_decision_artifact_blocks_resume(
        self, project, run_dir,
    ) -> None:
        """A decision artifact whose persisted ``handoff_id`` does not
        match the active payload (corruption / tampering / hash
        collision) must abort resume — the strict SDK reader catches
        the mismatch and the orchestrator surfaces it as a RuntimeError
        instead of silently trusting the payload."""
        from agents.runtimes import MockAgentProvider
        from sdk.phase_handoff import safe_handoff_id

        s_pause = self._pause_on_rejected_plan(project, run_dir)
        active_id = s_pause["phase_handoff"]["id"]
        run_id = self._parent_run_id(run_dir)

        # Manually write an artifact whose payload claims a different
        # ``handoff_id`` than the path resolves to — exactly the
        # corruption the strict reader exists to catch.
        decisions_dir = run_dir / "phase_handoff_decisions"
        decisions_dir.mkdir(parents=True, exist_ok=True)
        bad_artifact = (
            decisions_dir / f"{safe_handoff_id(active_id)}.json"
        )
        bad_artifact.write_text(
            json.dumps({
                "run_id":     run_dir.name,
                "handoff_id": "validate_plan:plan_round:999",  # mismatch
                "phase":      "validate_plan",
                "action":     "continue",
                "feedback":   None,
                "note":       None,
                "decided_at": "2026-05-21T00:00:00+00:00",
            }) + "\n",
            encoding="utf-8",
        )

        provider2 = MockAgentProvider(latency=0.0)
        lp, hu, gd = _patches()
        with lp, hu, gd, patch(
            "pipeline.project.profile_dispatch.maybe_run_hypothesis",
            return_value=(None, []),
        ), pytest.raises(RuntimeError, match="strict validation"):
            run_pipeline(
                task="should-be-refused",
                project_dir=str(project),
                output_dir=run_dir,
                profile_name="feature",
                resume_from=run_id,
                hypothesis_enabled=False,
                provider=provider2,
            )

    def test_state_halt_writes_top_level_halt_reason_in_meta(
        self, project, run_dir,
    ) -> None:
        """When the pipeline halts via ``state.halt`` (programmatic halt
        from a phase handler, quality gate, runner, or guardrail —
        distinct from the SDK ``phase_handoff_decide(halt)`` path), the
        finalized ``meta.json`` must carry ``halt_reason`` at the top
        level — same shape the SDK halt path produces. Without that,
        downstream consumers that key off ``meta.halt_reason``
        (SDK resume-gate, MCP wire, dashboards) silently see ``None``
        on every non-handoff halt; the actual reason hides under
        ``meta.halt.reason``.

        Triggers the plan-parse-failure branch in
        ``pipeline/phases/builtin/`` (round-1 ``state.stop``) by
        forcing the architect to emit un-parseable plan output.
        """
        from agents.runtimes import MockAgentProvider

        class _GarbagePlanClaude:
            model = "stub-claude"
            session_id: str | None = None
            # Pipeline reads these after invoke for the live card.
            last_estimated_tokens_in: int = 0
            last_estimated_tokens_out: int = 0
            last_tool_use_count: int = 0

            def invoke(
                self, prompt: str, cwd: str, *,
                mutates_artifacts: bool = False,
                continue_session: bool = False,
                attachments: tuple = (),
            ) -> str:
                # Output that fails both JSON fence detection and
                # markdown ``## Task N`` section detection — plan_parser
                # raises and the phase handler triggers state.stop.
                return "this is not a parseable plan in any contract"

            def reset_session(self) -> None:
                self.session_id = None

        provider = MockAgentProvider(latency=0.0, test_pass_rate=1.0)
        provider.claude = lambda model, **_kw: _GarbagePlanClaude()
        provider.codex = lambda model, **_kw: _AlwaysApproved()

        lp, hu, gd = _patches()
        with lp, hu, gd, patch(
            "pipeline.project.profile_dispatch.maybe_run_hypothesis",
            return_value=(None, []),
        ):
            session = run_pipeline(
                task="trigger plan parse failure",
                project_dir=str(project),
                output_dir=run_dir,
                profile_name="feature",
                hypothesis_enabled=False,
                provider=provider,
            )

        # In-memory session matches the on-disk meta shape.
        assert session["status"] == "halted"
        assert session.get("halt_reason"), (
            "session must carry top-level halt_reason on state.halt "
            "path (previously hidden under session.halt.reason)"
        )
        assert "plan rejected before implement" in session["halt_reason"]

        # On-disk meta.json carries the same fields — this is what
        # downstream consumers actually read.
        meta = json.loads((run_dir / "meta.json").read_text())
        assert meta["status"] == "halted"
        assert meta.get("halt_reason") == session["halt_reason"]
        # Nested halt block preserved for backwards compatibility with
        # consumers that already read ``halt.phase``.
        assert isinstance(meta.get("halt"), dict)
        assert meta["halt"].get("reason") == meta["halt_reason"]

    def test_plan_profile_continue_finalizes_done_not_human_review(
        self, project, run_dir,
    ) -> None:
        """When a ``plan`` profile pauses on phase handoff and the human
        decides ``continue``, finalize must short-circuit straight to
        ``done`` — the legacy ``awaiting_human_review`` fallthrough was
        the pre-handoff plan-profile tail and conflicts with the new
        contract (the human already signed off via the decision API)."""
        from agents.runtimes import MockAgentProvider
        from sdk.phase_handoff import phase_handoff_decide

        provider = MockAgentProvider(latency=0.0, validate_plan_reject_rounds=2)
        lp, hu, gd = _patches()
        with lp, hu, gd, patch(
            "pipeline.project.profile_dispatch.maybe_run_hypothesis",
            return_value=(None, []),
        ):
            s_pause = run_pipeline(
                task="Plan profile + handoff",
                project_dir=str(project),
                output_dir=run_dir,
                profile_name="planning",
                hypothesis_enabled=False,
                provider=provider,
            )
        assert s_pause["status"] == "awaiting_phase_handoff"
        handoff_id = s_pause["phase_handoff"]["id"]
        run_id = self._parent_run_id(run_dir)

        phase_handoff_decide(
            run_dir.name, handoff_id, "continue",
            runs_dir=run_dir.parent, cwd=None,
        )

        provider2 = MockAgentProvider(latency=0.0)
        provider2.codex = lambda m, **_kw: _AlwaysApproved()
        with lp, hu, gd, patch(
            "pipeline.project.profile_dispatch.maybe_run_hypothesis",
            return_value=(None, []),
        ):
            s_resume = run_pipeline(
                task="Plan continuation",
                project_dir=str(project),
                output_dir=run_dir,
                profile_name="planning",
                resume_from=run_id,
                hypothesis_enabled=False,
                provider=provider2,
            )
        assert s_resume["status"] == "done"
        assert "phase_handoff" not in s_resume


# ═════════════════════════════════════════════════════════════════════════════
# D. Correction follow-up (ADR 0085): final_acceptance rejected → fix
# ═════════════════════════════════════════════════════════════════════════════

def _phase_start_kinds(run_dir: Path) -> list[str]:
    """Ordered ``phase_kind`` of every ``phase.start`` event in a run dir."""
    kinds: list[str] = []
    for event in _read_jsonl(run_dir / "events.jsonl"):
        if event.get("kind") != "phase.start":
            continue
        payload = event.get("payload") or {}
        pk = payload.get("phase_kind")
        if pk:
            kinds.append(pk)
    return kinds


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
class TestD_FixToCorrection:
    """End-to-end mock proof of the fix → correction follow-up (ADR 0085).

    A parent run establishes a retained worktree; the operator's ``fix``
    choice at the correction gate is modelled by handing the correction
    driver a ``commit_decision_fix`` parent session. The driver writes
    ``correction_context.md`` and re-enters ``run_pipeline`` under the
    internal ``correction`` profile, which starts at ``correction_triage``
    and reuses the parent's worktree.
    """

    @staticmethod
    def _as_fix_halt(parent_session: dict) -> dict:
        """Mark the real parent session as a ``commit_decision_fix`` halt
        carrying a rejected final_acceptance, so the driver loops once and
        composes correction context from the rejection."""
        s = dict(parent_session)
        s["status"] = "halted"
        s["halt_reason"] = "commit_decision_fix"
        phases = dict(s.get("phases", {}))
        phases["final_acceptance"] = {
            "verdict": "REJECTED",
            "approved": False,
            "ship_ready": False,
            "short_summary": "Release gate is red on the touched path.",
            "verification_gaps": [
                {
                    "risk": "Could ship without the gate passing.",
                    "missing_evidence": "the gate command was not run green",
                    "required_check": "run the project gate and make it pass",
                },
            ],
            "critique": "## Release gate\n\nblocked: close the release gap",
        }
        s["phases"] = phases
        return s

    def test_fix_to_correction_followup(self, tmp_path: Path) -> None:
        from pipeline.project.correction_followup import (
            drive_correction_followups,
        )

        # 1) Parent run — a real pipeline so a retained worktree + meta.json
        #    exist for the follow-up to reuse.
        project = tmp_path / "proj"
        _init_git_repo(project)
        runs = tmp_path / "runs"
        parent_dir = runs / "20260601_000000"
        parent_dir.mkdir(parents=True)

        _reset_test_globals()
        try:
            parent_session = _run(
                task="Add structured logging",
                project=project,
                run_dir=parent_dir,
                provider=_build_clean_review_provider(),
                # A worktree-isolated profile (per_run): the scoped ``task``
                # profile is worktree_isolation=off (commit e1df2df), so it
                # leaves no retained worktree for the correction follow-up to
                # reuse. ``feature`` retains one, which the follow-up continuity
                # guard requires.
                profile_name="feature",
            )
        finally:
            _reset_test_globals()

        parent_worktree = (parent_session.get("worktree") or {}).get("path")
        assert parent_worktree, "parent run did not record a worktree path"

        # 2) Drive the correction follow-up off a fix-halt parent session.
        #    The child uses the default mock provider (no codex override) so
        #    the ``[correction_triage]`` reviewer branch returns triage JSON.
        announcements: list[str] = []

        def _child_run_pipeline(**kwargs):
            lp, hu, gd = _patches()
            with lp, hu, gd, patch(
                "pipeline.project.profile_dispatch.maybe_run_hypothesis",
                return_value=(None, []),
            ):
                return run_pipeline(
                    provider=MockAgentProvider(latency=0.0, test_pass_rate=1.0),
                    **kwargs,
                )

        _reset_test_globals()
        try:
            child_session = drive_correction_followups(
                prev_session=self._as_fix_halt(parent_session),
                prev_output_dir=parent_dir,
                base_task="Add structured logging",
                stable_kwargs={"project_dir": str(project)},
                run_pipeline=_child_run_pipeline,
                mint_run_id=lambda: "20260601_000001",
                announce=announcements.append,
            )
        finally:
            _reset_test_globals()

        child_dir = runs / "20260601_000001"

        # ── announce explicitly names the correction profile ────────────
        assert announcements, "expected a correction follow-up announcement"
        assert any("profile 'correction'" in m for m in announcements)

        # ── child run dispatched the internal ``correction`` profile ────
        assert child_session["profile"] == "correction"
        child_meta = json.loads((child_dir / "meta.json").read_text())
        assert child_meta["profile"] == "correction"

        # ── phase order starts at correction_triage; no plan loop ───────
        phases = child_session["phases"]
        assert "correction_triage" in phases
        assert "plan" not in phases
        assert "validate_plan" not in phases
        kinds = _phase_start_kinds(child_dir)
        assert kinds, "child run wrote no phase.start events"
        assert kinds[0] == "CORRECTION_TRIAGE", (
            f"correction run must open on correction_triage, got {kinds}"
        )
        assert "PLAN" not in kinds and "VALIDATE_PLAN" not in kinds
        assert "IMPLEMENT" in kinds
        assert kinds.index("CORRECTION_TRIAGE") < kinds.index("IMPLEMENT")

        # ── correction context artifact + persisted triage record ───────
        assert (child_dir / "correction_context.md").is_file()
        triage = phases["correction_triage"]
        assert triage["kind"] in (
            "code_fix", "contract_ack", "gate_rerun", "blocked",
        )
        assert "summary" in triage

        # ── retained parent worktree reused (same path, diff_source) ────
        child_worktree = child_session.get("worktree") or {}
        assert child_worktree.get("path") == parent_worktree
        continuity = child_worktree.get("followup_continuity") or {}
        assert continuity.get("diff_source") == "worktree", (
            f"correction follow-up did not reuse the parent worktree: {continuity}"
        )


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
class TestE_CorrectionRoutes:
    """The ``correction_triage.kind`` drives the correction route (ADR 0086).

    Each scenario runs a real parent ``task`` pipeline so a dirty retained
    worktree exists, writes a ``correction_context.md`` carrying an
    ``orcho-mock-triage-kind`` directive (so the mock triage classifies a
    chosen kind), then dispatches the internal ``correction`` profile against
    that worktree as a follow-up. We assert the route's observable effects:
    skip records with the route reason, the ``final_acceptance`` gate firing
    for shortcut routes, the halt for ``blocked``, and the rejected-acceptance
    halt for the ``REJECTED`` final gate.
    """

    @staticmethod
    def _child_progress(child_dir: Path) -> list[str]:
        return (child_dir / "progress.log").read_text().splitlines()

    @staticmethod
    def _done_line(lines: list[str]) -> str:
        # The DONE *summary* chips line — NOT the correction-route DONE line
        # (ADR 0086) that finalization now emits as a second DONE entry. The
        # route line is selected via ``_route_done_line``; excluding it here
        # keeps this helper pinned on the per-phase chip summary.
        done = [
            ln for ln in lines
            if "DONE" in ln and "Correction route" not in ln
        ]
        assert done, "no DONE summary line in progress.log"
        return done[-1]

    @staticmethod
    def _route_done_line(lines: list[str]) -> str:
        # The correction-route DONE line in progress.log (ADR 0086): the
        # ``[DONE]`` entry carrying the compact route summary.
        route = [
            ln for ln in lines
            if "[DONE]" in ln and "Correction route" in ln
        ]
        assert route, "no correction-route DONE line in progress.log"
        return route[-1]

    @staticmethod
    def _assert_route_done_is_second(lines: list[str]) -> None:
        # ADR 0086 ordering: finalization writes exactly two DONE-status
        # entries to progress.log — the per-phase chips summary first, then
        # the compact correction-route line. Pinning the order (not just
        # presence) catches a regression that re-orders or merges them.
        # progress.log lines are ``[ts]  STATUS  [PHASE]  title``; the DONE
        # banner's START line also carries ``[DONE]``, so filter it out.
        done_entries = [
            ln for ln in lines if "[DONE]" in ln and "START" not in ln
        ]
        assert len(done_entries) == 2, (
            f"expected chips DONE + route DONE, got: {done_entries}"
        )
        assert "Correction route" not in done_entries[0], (
            "chips summary must be the FIRST DONE entry"
        )
        assert "Correction route" in done_entries[1], (
            "route line must be the SECOND DONE entry"
        )

    @staticmethod
    def _triage_end_line(lines: list[str]) -> str:
        # The correction_triage END line in progress.log (ADR 0086): the
        # full route decision is folded into the END outcome by T2's glue.
        end = [
            ln for ln in lines
            if "[CORRECTION_TRIAGE]" in ln and "Correction route" in ln
        ]
        assert end, "no correction_triage END route line in progress.log"
        return end[-1]

    def _drive(
        self,
        tmp_path: Path,
        *,
        triage_kind: str,
        reject: bool = False,
        silent: bool = False,
    ) -> tuple[dict, Path]:
        """Run parent ``task`` + child ``correction`` route run.

        ``reject=False`` suppresses commit-delivery so an APPROVED shortcut
        run ends cleanly ``done`` (delivery transport is out of scope here).
        ``reject=True`` leaves delivery enabled and drives the interactive
        ``fix`` decision so a REJECTED final gate halts on
        ``commit_decision_fix`` — the rejected-acceptance path.

        ``silent=True`` dispatches the child through the SAME typed request
        form the cross-project orchestrator uses for its children
        (``pipeline.cross_project.project_dispatch`` →
        ``run_project_pipeline(ProjectRunRequest(...,
        profile_obj=..., profile_name=...,
        presentation=PresentationPolicy.SILENT, no_interactive=True))``)
        instead of the legacy ``run_pipeline`` wrapper. Every other kwarg is
        byte-identical to the TERMINAL path, so the only deltas are the
        presentation policy, the non-interactive companion invariant, and
        the explicit in-memory ``profile_obj`` cross children always carry —
        which is exactly what proves SILENT preserves the durable
        correction-route evidence. ``silent`` is incompatible with ``reject``
        (the rejected-acceptance path needs an interactive ``fix`` prompt,
        which SILENT forbids by construction).
        """
        if silent and reject:
            raise ValueError("silent SILENT runs cannot drive interactive reject")
        from pipeline.control.resume_context import (
            extract_followup_session_seeds,
        )
        from pipeline.project.correction_followup import _pinned_run_id

        project = tmp_path / "proj"
        _init_git_repo(project)
        runs = tmp_path / "runs"
        parent_dir = runs / "20260601_000000"
        parent_dir.mkdir(parents=True)

        _reset_test_globals()
        try:
            parent_session = _run(
                task="Add structured logging",
                project=project,
                run_dir=parent_dir,
                provider=_build_clean_review_provider(),
                # A worktree-isolated profile (per_run): the scoped ``task``
                # profile is worktree_isolation=off (commit e1df2df), so it
                # leaves no retained worktree for the correction follow-up to
                # reuse. ``feature`` retains one, which the follow-up continuity
                # guard requires.
                profile_name="feature",
            )
        finally:
            _reset_test_globals()

        child_dir = runs / "20260601_000001"
        child_dir.mkdir(parents=True)
        # The directive rides in the recorded correction context, which the
        # triage prompt embeds verbatim, so the mock classifies ``triage_kind``.
        (child_dir / "correction_context.md").write_text(
            "# Correction Context\n\n"
            "## Release Summary\n\nRelease gate flagged the touched path.\n\n"
            f"orcho-mock-triage-kind: {triage_kind}\n",
            encoding="utf-8",
        )
        seeds = extract_followup_session_seeds(parent_session)

        cms = list(_patches())
        if reject:
            cms += [
                patch(
                    "pipeline.engine.commit_delivery.stdio_interactive",
                    return_value=True,
                ),
                patch(
                    "pipeline.engine.commit_delivery._prompt_action",
                    return_value="fix",
                ),
            ]
        else:
            # Isolate route behaviour from delivery transport: an APPROVED
            # shortcut run would otherwise attempt (and, in the bare test repo,
            # fail) auto-delivery. Disabling it keeps the terminal status clean.
            cms += [
                patch(
                    "pipeline.project.run._session_allows_commit_delivery",
                    return_value=False,
                ),
            ]

        _reset_test_globals()
        try:
            with contextlib.ExitStack() as stack:
                for cm in cms:
                    stack.enter_context(cm)
                with _pinned_run_id("20260601_000001"):
                    if silent:
                        # Mirror the cross-project child request form:
                        # the typed ProjectRunRequest with SILENT +
                        # no_interactive=True AND an explicit in-memory
                        # ``profile_obj`` alongside ``profile_name``, routed
                        # through run_project_pipeline (NOT the run_pipeline
                        # shim). Cross children always carry a ``profile_obj``
                        # (``project_dispatch`` passes ``ctx.child_profile``),
                        # so the SILENT evidence proof must exercise the same
                        # profile_obj adaptation path. The object is loaded
                        # via the same v2 loader cross ``profile_setup`` uses.
                        from core.infra.paths import CONFIG_DIR
                        from pipeline.profiles.loader import (
                            load_profiles_v2_with_plugins,
                        )
                        from pipeline.project.app import run_project_pipeline
                        from pipeline.project.types import (
                            PresentationPolicy,
                            ProjectRunRequest,
                        )

                        correction_profile = load_profiles_v2_with_plugins(
                            CONFIG_DIR / "pipeline_profiles_v2.json"
                        )["correction"]
                        child_session = run_project_pipeline(
                            ProjectRunRequest(
                                task=(
                                    "Correction follow-up: resolve the "
                                    "listed blockers."
                                ),
                                project_dir=str(project),
                                output_dir=child_dir,
                                provider=MockAgentProvider(
                                    latency=0.0, test_pass_rate=1.0,
                                ),
                                profile_name="correction",
                                resume_mode="followup",
                                resume_from=None,
                                followup_parent_run_id=parent_dir.name,
                                followup_parent_run_dir=str(parent_dir),
                                followup_parent_status="halted",
                                followup_base_task="Add structured logging",
                                followup_session_seeds=seeds,
                                hypothesis_enabled=False,
                                profile_obj=correction_profile,
                                presentation=PresentationPolicy.SILENT,
                                no_interactive=True,
                            ),
                        ).session
                    else:
                        child_session = run_pipeline(
                            task="Correction follow-up: resolve the listed blockers.",
                            project_dir=str(project),
                            output_dir=child_dir,
                            provider=MockAgentProvider(latency=0.0, test_pass_rate=1.0),
                            profile_name="correction",
                            resume_mode="followup",
                            resume_from=None,
                            followup_parent_run_id=parent_dir.name,
                            followup_parent_run_dir=str(parent_dir),
                            followup_parent_status="halted",
                            followup_base_task="Add structured logging",
                            followup_session_seeds=seeds,
                            hypothesis_enabled=False,
                            no_interactive=False,
                        )
        finally:
            _reset_test_globals()

        return child_session, child_dir

    # ── (a) gate_rerun happy ────────────────────────────────────────────
    def test_gate_rerun_skips_code_phases_and_runs_final_acceptance(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        session, child_dir = self._drive(tmp_path, triage_kind="gate_rerun")
        phases = session["phases"]

        assert phases["correction_triage"]["kind"] == "gate_rerun"
        # implement / review_changes / repair_changes are skipped: not run,
        # so they never reach the session phase map.
        assert "implement" not in phases
        # final_acceptance really ran the agent (APPROVED release verdict).
        assert phases["final_acceptance"]["verdict"] == "APPROVED"
        assert session.get("halt_reason") is None

        # Skip records carry the route reason (kind + summary), printed by the
        # runner's pre-phase skip channel into progress.log.
        lines = self._child_progress(child_dir)
        skip_lines = [ln for ln in lines if "skipped:" in ln]
        assert any("IMPLEMENT" in ln for ln in skip_lines)
        assert all("correction route 'gate_rerun'" in ln for ln in skip_lines)
        assert len(skip_lines) >= 3  # implement + review_changes + repair_changes

        # DONE summary renders skipped phases as ``skip`` (not ``ok``/``fail``).
        done = self._done_line(lines)
        assert "implement=skip" in done
        assert "review_changes=skip" in done
        assert "repair_changes=skip" in done
        assert "final_acceptance=ok" in done

        # ADR 0086 route presentation (T2): the correction_triage END line in
        # progress.log carries the FULL route decision — kind + every skipped
        # phase — not a bare ``route=<kind>`` tag. Phase order is sorted, the
        # same order CorrectionRoute.to_evidence() uses.
        triage_end = self._triage_end_line(lines)
        assert "Correction route: gate_rerun" in triage_end
        assert "skipping implement/repair_changes/review_changes" in triage_end
        for skipped in ("implement", "review_changes", "repair_changes"):
            assert skipped in triage_end

        # ADR 0086 route presentation (T3): the DONE block carries a compact
        # route line with the final_acceptance outcome; it is neutral, not a
        # success chip line.
        route_done = self._route_done_line(lines)
        assert "Correction route: gate_rerun" in route_done
        assert "final_acceptance=ok" in route_done

        # Terminal output mirrors both: a neutral route chip (no amber ⚠ on a
        # shortcut skip).
        out = capsys.readouterr().out
        assert "Correction route: gate_rerun" in out
        assert "skipping implement/repair_changes/review_changes" in out

    # ── (b) gate_rerun reject ───────────────────────────────────────────
    def test_gate_rerun_rejected_final_acceptance_is_not_successful(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ORCHO_MOCK_RELEASE_REJECT", "1")
        session, child_dir = self._drive(
            tmp_path, triage_kind="gate_rerun", reject=True,
        )
        phases = session["phases"]

        # final_acceptance ran and REJECTED; the run is NOT a clean success.
        assert phases["final_acceptance"]["verdict"] == "REJECTED"
        assert phases["final_acceptance"]["approved"] is False
        assert session["status"] != "done"
        assert session["status"] == "halted"
        assert session["halt_reason"] == "commit_decision_fix"

        # Skip phases are still rendered as skip — not counted as work/ok.
        done = self._done_line(self._child_progress(child_dir))
        assert "implement=skip" in done
        assert "implement=ok" not in done

    # ── (c) contract_ack ────────────────────────────────────────────────
    def test_contract_ack_skips_code_phases_and_keeps_context(
        self, tmp_path: Path,
    ) -> None:
        session, child_dir = self._drive(tmp_path, triage_kind="contract_ack")
        phases = session["phases"]

        assert phases["correction_triage"]["kind"] == "contract_ack"
        assert "implement" not in phases
        assert phases["final_acceptance"]["verdict"] == "APPROVED"

        # Retained correction context is present in the evidence chain.
        assert (child_dir / "correction_context.md").is_file()
        assert "route" in phases["correction_triage"]
        assert phases["correction_triage"]["route"]["kind"] == "contract_ack"

        lines = self._child_progress(child_dir)
        done = self._done_line(lines)
        assert "implement=skip" in done
        assert "final_acceptance=ok" in done

        # ADR 0086 route presentation: contract_ack is a shortcut route, so it
        # gets the same full-text decision (END line) + DONE route line as
        # gate_rerun. This shares the gate_rerun scenario's machinery, so no
        # separate full pipeline run is added here — the assertions ride the
        # existing contract_ack run to keep the suite fast.
        triage_end = self._triage_end_line(lines)
        assert "Correction route: contract_ack" in triage_end
        assert "skipping implement/repair_changes/review_changes" in triage_end
        route_done = self._route_done_line(lines)
        assert "Correction route: contract_ack" in route_done
        assert "final_acceptance=ok" in route_done

    # ── (d) blocked ─────────────────────────────────────────────────────
    def test_blocked_halts_before_any_code_phase(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        session, child_dir = self._drive(tmp_path, triage_kind="blocked")
        phases = session["phases"]

        assert session["status"] == "halted"
        assert session["halt_reason"] == "correction_triage_blocked"
        assert phases["correction_triage"]["kind"] == "blocked"
        # No implement / review / final_acceptance agent ran.
        assert "implement" not in phases
        assert "final_acceptance" not in phases

        kinds = _phase_start_kinds(child_dir)
        assert kinds == ["CORRECTION_TRIAGE"], (
            f"blocked run must stop at triage, got {kinds}"
        )
        lines = self._child_progress(child_dir)
        done = self._done_line(lines)
        assert "correction_triage=halt" in done

        # ADR 0086 route presentation (T2): the correction_triage END line in
        # progress.log carries the blocked decision with the halt phrasing and
        # the compact reason / blockers — not a bare ``route=blocked`` tag.
        triage_end = self._triage_end_line(lines)
        assert "Correction route: blocked" in triage_end
        assert "halting before implement" in triage_end
        assert "blocker" in triage_end  # reason/blockers digest is present

        # ADR 0086 route presentation (T3): the DONE/HALT block carries the
        # amber route line. The ⚠ icon is a terminal-only affordance, so it is
        # asserted on stdout (progress.log keeps the plain text).
        route_done = self._route_done_line(lines)
        assert "Correction route: blocked" in route_done
        assert "halted before implement" in route_done
        out = capsys.readouterr().out
        assert "⚠" in out
        assert "Correction route: blocked" in out
        # The blocked child run halts at triage: no IMPLEMENT / FINAL_ACCEPTANCE
        # banner is ever written to the child progress.log.
        child_log = "\n".join(lines)
        assert "[IMPLEMENT]" not in child_log
        assert "[FINAL_ACCEPTANCE]" not in child_log

    # ── (e) code_fix (Stage 0 regression) ───────────────────────────────
    def test_code_fix_runs_full_phase_chain(self, tmp_path: Path) -> None:
        session, child_dir = self._drive(tmp_path, triage_kind="code_fix")
        phases = session["phases"]

        assert phases["correction_triage"]["kind"] == "code_fix"
        # The full Stage 0 chain runs: implement executes, no route skip.
        assert "implement" in phases
        assert phases["final_acceptance"]["verdict"] == "APPROVED"
        assert session.get("halt_reason") is None

        kinds = _phase_start_kinds(child_dir)
        assert "IMPLEMENT" in kinds
        assert kinds.index("CORRECTION_TRIAGE") < kinds.index("IMPLEMENT")

        done = self._done_line(self._child_progress(child_dir))
        assert "implement=ok" in done
        assert "final_acceptance=ok" in done

    # ── SILENT route-evidence preservation (cross-child request form) ────
    #
    # The cross-project orchestrator dispatches every child through
    # ``run_project_pipeline(ProjectRunRequest(...,
    # presentation=PresentationPolicy.SILENT, no_interactive=True))`` — the
    # SILENT presentation suppresses the child's stdout banners / route
    # chips, but ``log_phase(...)`` + the event-store emit are unconditional
    # (ADR 0046 stop #9), so the child's ``progress.log`` + ``events.jsonl``
    # + persisted ``session`` must still carry the FULL correction-route
    # evidence. These two scenarios drive the project-profile path under that
    # exact request form and assert the durable artifacts directly (NOT just
    # ``capsys``), proving the route evidence survives the silent child path
    # the cross orchestrator relies on (correction is not projected into
    # cross per ADR 0085, so this project-profile SILENT run is how the
    # evidence path is covered).

    @staticmethod
    def _phase_end_outcomes(events: list[dict]) -> list[str]:
        return [
            (e.get("payload") or {}).get("outcome", "")
            for e in events
            if e.get("kind") == "phase.end"
        ]

    @staticmethod
    def _last_index(events: list[dict], pred) -> int:
        idxs = [i for i, e in enumerate(events) if pred(e)]
        assert idxs, "no matching event"
        return idxs[-1]

    def test_silent_gate_rerun_preserves_route_evidence(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        session, child_dir = self._drive(
            tmp_path, triage_kind="gate_rerun", silent=True,
        )
        phases = session["phases"]

        # (1) Route evidence is stamped on the persisted session under the
        # shortcut route — kind / skip_phases / halt / reason all present.
        triage = phases["correction_triage"]
        assert triage["kind"] == "gate_rerun"
        route = triage["route"]
        assert route["kind"] == "gate_rerun"
        assert route["skip_phases"]  # non-empty list of skipped phases
        assert route["halt"] is False
        assert "reason" in route
        # Shortcut really skipped the code phases and ran final_acceptance.
        assert "implement" not in phases
        assert phases["final_acceptance"]["verdict"] == "APPROVED"

        # (2) progress.log carries BOTH the correction_triage END decision
        # line and the second [DONE] route line — neither is a terminal-only
        # affordance, so SILENT must keep them.
        lines = self._child_progress(child_dir)
        triage_end = self._triage_end_line(lines)
        assert "Correction route: gate_rerun" in triage_end
        assert (
            "skipping implement/repair_changes/review_changes" in triage_end
        )
        route_done = self._route_done_line(lines)
        assert "Correction route: gate_rerun" in route_done
        assert "final_acceptance=" in route_done
        self._assert_route_done_is_second(lines)

        # (3) events.jsonl mirrors the route: a phase.end outcome carries the
        # route text, and run.end is recorded AFTER the route-DONE entry
        # (route text lands in the DONE phase.end ``title``).
        events = _read_jsonl(child_dir / "events.jsonl")
        assert any(
            "Correction route: gate_rerun" in outcome
            for outcome in self._phase_end_outcomes(events)
        ), "no phase.end outcome carrying the gate_rerun route text"
        route_done_idx = self._last_index(
            events,
            lambda e: e.get("kind") == "phase.end"
            and "Correction route: gate_rerun"
            in (e.get("payload") or {}).get("title", ""),
        )
        run_end_idx = self._last_index(
            events, lambda e: e.get("kind") == "run.end",
        )
        assert route_done_idx < run_end_idx, (
            "run.end must follow the correction-route DONE event"
        )

        # (5) SILENT gates the print half: no route text on stdout, while the
        # durable channels above all carry it. log_phase channels stay intact
        # (asserted via progress.log + events.jsonl above).
        out = capsys.readouterr().out
        assert "Correction route" not in out

    def test_silent_blocked_halts_and_preserves_route_evidence(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        session, child_dir = self._drive(
            tmp_path, triage_kind="blocked", silent=True,
        )
        phases = session["phases"]

        # (4) Blocked halts before any code phase, with the canonical reason.
        assert session["status"] == "halted"
        assert session["halt_reason"] == "correction_triage_blocked"
        assert phases["correction_triage"]["kind"] == "blocked"
        assert "implement" not in phases
        assert "final_acceptance" not in phases

        lines = self._child_progress(child_dir)
        # The blocked child halts at triage: no IMPLEMENT / FINAL_ACCEPTANCE
        # banner is ever written to the child progress.log.
        child_log = "\n".join(lines)
        assert "[IMPLEMENT]" not in child_log
        assert "[FINAL_ACCEPTANCE]" not in child_log

        # (2) progress.log carries the halting END decision + the DONE route
        # summary, both under SILENT.
        triage_end = self._triage_end_line(lines)
        assert "Correction route: blocked" in triage_end
        assert "halting before implement" in triage_end
        route_done = self._route_done_line(lines)
        assert "Correction route: blocked" in route_done
        assert "halted before implement" in route_done
        self._assert_route_done_is_second(lines)

        # (3) events.jsonl: a phase.end outcome carries the blocked route
        # text, and run.end follows the route-DONE entry.
        events = _read_jsonl(child_dir / "events.jsonl")
        assert any(
            "Correction route: blocked" in outcome
            for outcome in self._phase_end_outcomes(events)
        ), "no phase.end outcome carrying the blocked route text"
        route_done_idx = self._last_index(
            events,
            lambda e: e.get("kind") == "phase.end"
            and "Correction route: blocked"
            in (e.get("payload") or {}).get("title", ""),
        )
        run_end_idx = self._last_index(
            events, lambda e: e.get("kind") == "run.end",
        )
        assert route_done_idx < run_end_idx, (
            "run.end must follow the correction-route DONE event"
        )

        # (5) SILENT gates the print half — no route text (and no amber ⚠
        # route chip) reaches stdout, while progress.log + events carry it.
        out = capsys.readouterr().out
        assert "Correction route" not in out
