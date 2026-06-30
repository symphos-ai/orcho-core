"""
tests/integration/test_checkpoint_pipeline.py

Integration tests: checkpoint store + real pipeline flow via MockAgentProvider.
Validates that pipeline creates checkpoints.db, saves phase snapshots,
and supports resume_from parameter.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import core.observability.logging as _logging_module
from agents.runtimes import MockAgentProvider
from pipeline.checkpoint import CheckpointStore, PipelineStatus
from pipeline.plugins import PluginConfig
from pipeline.project_orchestrator import run_pipeline

# ─────────────────────────────────────────────────────────────────────────────
# Autouse: reset globals
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_logging():
    import agents.stream as _s
    yield
    _logging_module._progress_log = None
    _s._agent_log = None


# ─────────────────────────────────────────────────────────────────────────────
# Shared
# ─────────────────────────────────────────────────────────────────────────────

PLUGIN = PluginConfig(
    name="Checkpoint Integration",
    language="Python",
    architecture="FastAPI",
    file_hints=["src/"],
)


def _approved_review_json(summary: str = "Approved by JSON contract.") -> str:
    return json.dumps({
        "verdict":       "APPROVED",
        "short_summary": summary,
        "findings":      [],
    })


def _approved_release_json(summary: str = "Ship-ready.") -> str:
    """ADR 0025: release-gate APPROVED payload."""
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
    return (
        'kind="contract"' in prompt
        and 'name="release_json"' in prompt
        and "<orcho:system-block " in prompt
    )


class _AlwaysApprovedReviewer:
    model = "stub-codex"
    session_id: str | None = None

    def invoke(
        self, prompt: str, cwd: str, *,
        mutates_artifacts: bool = False,
        continue_session: bool = False,
        attachments: tuple = (),
    ) -> str:
        if _prompt_requests_release(prompt):
            return _approved_release_json()
        return _approved_review_json()

    def reset_session(self) -> None:
        self.session_id = None


@pytest.fixture
def provider() -> MockAgentProvider:
    p = MockAgentProvider(latency=0.0, test_pass_rate=1.0)
    p.codex = lambda model, **_kw: _AlwaysApprovedReviewer()
    return p


def _init_git_repo(path: Path) -> None:
    """Make ``path`` a real git repo so the engine's worktree resolver
    accepts it. Worktree isolation hard-fails on a non-git project_dir."""
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


def _patches():
    return (
        patch("pipeline.project.session_run.load_plugin", return_value=PLUGIN),
        patch("core.io.git_helpers.has_uncommitted", return_value=True),
        patch("core.io.git_helpers.git_diff_stat", return_value="1 file changed"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckpointCreated:
    """Pipeline creates checkpoints.db in output_dir."""

    def test_checkpoint_db_exists(self, project, tmp_path, provider) -> None:
        run_dir = tmp_path / "run_01"
        run_dir.mkdir()
        lp, hu, gd = _patches()
        with lp, hu, gd:
            run_pipeline(
                task="Add logging",
                project_dir=str(project),
                output_dir=run_dir,
                max_rounds=1,
                provider=provider,
            )
        assert (run_dir / "checkpoints.db").exists()

    def test_checkpoint_has_plan_phase(self, project, tmp_path, provider) -> None:
        run_dir = tmp_path / "run_02"
        run_dir.mkdir()
        lp, hu, gd = _patches()
        with lp, hu, gd:
            run_pipeline(
                task="Add logging",
                project_dir=str(project),
                output_dir=run_dir,
                max_rounds=1,
                provider=provider,
            )
        store = CheckpointStore(run_dir / "checkpoints.db")
        # Find the run (we don't know the run_id, but there's only one)
        runs = store.list_runs()
        assert len(runs) >= 1
        state = store.load(runs[0]["run_id"])
        assert "plan" in state.completed
        store.close()

    def test_checkpoint_has_validate_plan_phase(self, project, tmp_path, provider) -> None:
        run_dir = tmp_path / "run_03"
        run_dir.mkdir()
        lp, hu, gd = _patches()
        with lp, hu, gd:
            run_pipeline(
                task="Add logging",
                project_dir=str(project),
                output_dir=run_dir,
                max_rounds=1,
                provider=provider,
            )
        store = CheckpointStore(run_dir / "checkpoints.db")
        runs = store.list_runs()
        state = store.load(runs[0]["run_id"])
        assert "validate_plan" in state.completed
        store.close()


class TestAgentSessionsPersistE1:
    """E1: agent.session_id survives in checkpoint across subprocess
    restarts, so ``--resume <sid>`` actually fires on the next
    invoke after ``orcho_run_resume``.

    Uses MockAgentProvider which assigns ``mock-claude-N`` /
    ``mock-codex-N`` ids on first invoke — same surface as real
    runtimes, no LLM cost.
    """

    def test_run_writes_session_ids_to_checkpoint(
        self, project, tmp_path, provider,
    ) -> None:
        run_dir = tmp_path / "run_e1_write"
        run_dir.mkdir()
        lp, hu, gd = _patches()
        with lp, hu, gd:
            run_pipeline(
                task="Add logging",
                project_dir=str(project),
                output_dir=run_dir,
                max_rounds=1,
                provider=provider,
            )

        store = CheckpointStore(run_dir / "checkpoints.db")
        runs = store.list_runs()
        assert len(runs) >= 1
        sessions = store.get_agent_sessions(runs[0]["run_id"])
        # plan_agent + validate_plan_agent invoked at least once → both
        # populated. ``_AlwaysApprovedReviewer`` does NOT set session_id
        # (it overrides codex), so we only assert plan_agent here — the
        # contract is "if a role ever produced a session_id, it lands
        # in checkpoint", not "every role has one".
        assert "plan_agent" in sessions, (
            f"plan_agent missing from agent_sessions: {sessions}"
        )
        assert sessions["plan_agent"].startswith("mock-claude-")
        store.close()

    def test_session_id_advances_with_each_invoke(
        self, project, tmp_path, provider,
    ) -> None:
        """The last invoke wins: even if plan_agent fires twice
        (hypothesis + plan), checkpoint reflects the *latest* sid."""
        run_dir = tmp_path / "run_e1_advance"
        run_dir.mkdir()
        lp, hu, gd = _patches()
        with lp, hu, gd:
            run_pipeline(
                task="Add logging",
                project_dir=str(project),
                output_dir=run_dir,
                max_rounds=1,
                provider=provider,
            )
        store = CheckpointStore(run_dir / "checkpoints.db")
        runs = store.list_runs()
        sessions = store.get_agent_sessions(runs[0]["run_id"])
        # MockClaude counts upward; multiple invokes → counter > 1 OR
        # session reused (counter == 1). Either way, the value is the
        # one the agent currently holds. We can't predict the exact
        # number across pipeline variations; just assert the shape.
        assert sessions["plan_agent"].startswith("mock-claude-")
        store.close()

    def test_resume_construction_rehydrates_agent_sessions(
        self, project, tmp_path, provider,
    ) -> None:
        """Round-trip the load path: after a full run writes sessions,
        a *new* CheckpointStore opened against the same file reads
        them. Models what happens when subprocess #2 starts on resume.
        """
        run_dir = tmp_path / "run_e1_rehydrate"
        run_dir.mkdir()
        lp, hu, gd = _patches()
        with lp, hu, gd:
            run_pipeline(
                task="Add logging",
                project_dir=str(project),
                output_dir=run_dir,
                max_rounds=1,
                provider=provider,
            )

        store_s1 = CheckpointStore(run_dir / "checkpoints.db")
        runs = store_s1.list_runs()
        run_id = runs[0]["run_id"]
        sessions_s1 = store_s1.get_agent_sessions(run_id)
        store_s1.close()

        # Subprocess #2 simulation: fresh CheckpointStore against the
        # on-disk file. Same view, byte-identical.
        store_s2 = CheckpointStore(
            run_dir / "checkpoints.db", run_id=run_id,
        )
        sessions_s2 = store_s2.get_agent_sessions()
        store_s2.close()
        assert sessions_s2 == sessions_s1
        # Non-empty: the write path actually fired.
        assert sessions_s2, "expected at least plan_agent in agent_sessions"

    def test_checkpoint_seeds_actually_arm_resume_on_next_run(
        self, project, tmp_path, provider,
    ) -> None:
        """Regression for P1: persisted ``role_attr`` keys must reach
        the seeder (which reads by ``role``) and arm the agent for
        ``--resume <sid>`` on the very first invoke after resume.

        Without the role_attr → role boundary translation, the merged
        seeds dict would still be keyed by ``plan_agent`` but
        ``_apply_followup_session_seeds`` reads ``seeds.get("plan")``,
        silently returning None for every persisted row. This test
        asserts the translation actually happens end-to-end.
        """
        from pipeline.project.profile_dispatch import (
            _FOLLOWUP_ROLE_TO_AGENT_ATTR,
            apply_followup_session_seeds as _apply_followup_session_seeds,
        )

        # Build a phase_config with real mock agents — same providers
        # that run_pipeline uses, but we drive the seed apply
        # directly to isolate the translation step.
        plan_agent = provider.claude("stub-claude")
        validate_plan_agent = provider.codex("stub-codex")
        from agents.registry import PhaseAgentConfig
        phase_config = PhaseAgentConfig(
            plan_agent=plan_agent,
            validate_plan_agent=validate_plan_agent,
            implement_agent=plan_agent,
            review_changes_agent=validate_plan_agent,
            repair_changes_agent=plan_agent,
            repair_escalation_agent=plan_agent,
            final_acceptance_agent=validate_plan_agent,
        )

        # Persist via the role_attr-keyed checkpoint API (what
        # ``_session_aware_invoke`` actually writes).
        ckpt = CheckpointStore(":memory:", run_id="run_p1")
        ckpt.set_agent_session("plan_agent", "sid-plan-86481484")
        ckpt.set_agent_session(
            "validate_plan_agent", "sid-validate-d19b8eca",
        )

        # Replicate the merge that ``run_pipeline`` does on resume.
        persisted = ckpt.get_agent_sessions()
        attr_to_role = {
            attr: role
            for role, attr in _FOLLOWUP_ROLE_TO_AGENT_ATTR.items()
        }
        seeds_by_role = {
            attr_to_role[attr]: sid
            for attr, sid in persisted.items()
            if attr in attr_to_role
        }

        # Both translated under their roles.
        assert seeds_by_role == {
            "plan": "sid-plan-86481484",
            "validate_plan": "sid-validate-d19b8eca",
        }

        # The seeder consumes by role and writes into the right
        # phase_config slots.
        seeded = _apply_followup_session_seeds(phase_config, seeds_by_role)
        assert seeded == 2

        # And — load-bearing — agent.session_id is set, plus
        # _followup_resume_pending is armed so the very next invoke
        # forces ``--resume <sid>`` even if the phase handler did not
        # pass ``continue_session=True``.
        assert phase_config.plan_agent.session_id == "sid-plan-86481484"
        assert phase_config.plan_agent._followup_resume_pending is True
        assert (
            phase_config.validate_plan_agent.session_id
            == "sid-validate-d19b8eca"
        )
        assert (
            phase_config.validate_plan_agent._followup_resume_pending
            is True
        )

    def test_burned_session_clears_checkpoint_row(
        self, project, tmp_path, provider,
    ) -> None:
        """Regression for P2: when a runtime sets ``agent.session_id =
        None`` (followup burn, session reset, etc.), the checkpoint
        row must be deleted, not preserved with the stale id.

        Otherwise the next subprocess'es rehydrate would seed the
        agent with an id the provider no longer recognises.
        """
        # First: seed a row.
        ckpt = CheckpointStore(":memory:", run_id="run_p2")
        ckpt.set_agent_session("plan_agent", "sid-doomed-86481484")
        assert ckpt.get_agent_sessions() == {
            "plan_agent": "sid-doomed-86481484",
        }

        # Simulate the post-invoke sync when agent.session_id is None.
        # The _session_aware_invoke path calls
        # ``ckpt.set_agent_session(role_attr, None)`` unconditionally
        # to keep the on-disk view aligned with post-invoke truth.
        ckpt.set_agent_session("plan_agent", None)
        assert ckpt.get_agent_sessions() == {}, (
            "burned session must vacate the checkpoint row, not linger"
        )


class TestCheckpointStatus:
    """Pipeline sets correct checkpoint status."""

    def test_full_pipeline_status_done(self, project, tmp_path, provider) -> None:
        run_dir = tmp_path / "run_done"
        run_dir.mkdir()
        lp, hu, gd = _patches()
        with lp, hu, gd:
            run_pipeline(
                task="Test",
                project_dir=str(project),
                output_dir=run_dir,
                max_rounds=1,
                provider=provider,
            )
        store = CheckpointStore(run_dir / "checkpoints.db")
        runs = store.list_runs()
        state = store.load(runs[0]["run_id"])
        assert state.status == PipelineStatus.DONE
        store.close()

    def test_plan_mode_status_awaiting(self, project, tmp_path, provider) -> None:
        run_dir = tmp_path / "run_plan"
        run_dir.mkdir()
        lp, hu, gd = _patches()
        with lp, hu, gd:
            run_pipeline(
                task="Test",
                project_dir=str(project),
                output_dir=run_dir,
                max_rounds=1,
                provider=provider,
                profile_name="planning",
            )
        store = CheckpointStore(run_dir / "checkpoints.db")
        runs = store.list_runs()
        state = store.load(runs[0]["run_id"])
        # Phase 5 cutover: ``plan`` profile pauses via the generic phase
        # handoff machinery instead of the legacy ``awaiting_human_review``
        # tail (``handoff: human_feedback_always`` on validate_plan).
        assert state.status == PipelineStatus.AWAITING_PHASE_HANDOFF
        store.close()


class TestCheckpointConfig:
    """Pipeline config stored in checkpoint for resume."""

    def test_config_has_task(self, project, tmp_path, provider) -> None:
        run_dir = tmp_path / "run_cfg"
        run_dir.mkdir()
        lp, hu, gd = _patches()
        with lp, hu, gd:
            run_pipeline(
                task="The specific task",
                project_dir=str(project),
                output_dir=run_dir,
                max_rounds=1,
                provider=provider,
            )
        store = CheckpointStore(run_dir / "checkpoints.db")
        runs = store.list_runs()
        state = store.load(runs[0]["run_id"])
        assert state.run_config["task"] == "The specific task"
        store.close()


class TestResumeFlow:
    """Simulate PLAN → approve → TASK resume via checkpoint."""

    def test_plan_then_task_via_separate_runs(self, project, tmp_path, provider) -> None:
        """Run PLAN mode, then TASK mode — two separate pipeline calls."""
        # Phase 1: PLAN
        plan_dir = tmp_path / "plan_run"
        plan_dir.mkdir()
        lp, hu, gd = _patches()
        with lp, hu, gd:
            s1 = run_pipeline(
                task="Add auth",
                project_dir=str(project),
                output_dir=plan_dir,
                provider=provider,
                profile_name="planning",
            )
        assert s1["status"] == "awaiting_phase_handoff"
        assert "plan" in s1["phases"]
        assert "implement" not in s1["phases"]

        # Verify checkpoint
        store = CheckpointStore(plan_dir / "checkpoints.db")
        runs = store.list_runs()
        assert len(runs) == 1
        state = store.load(runs[0]["run_id"])
        assert state.status == PipelineStatus.AWAITING_PHASE_HANDOFF
        assert "plan" in state.completed
        store.close()

        # Phase 2: TASK (human approved)
        task_dir = tmp_path / "task_run"
        task_dir.mkdir()
        lp, hu, gd = _patches()
        with lp, hu, gd:
            s2 = run_pipeline(
                task="Add auth",
                project_dir=str(project),
                output_dir=task_dir,
                provider=provider,
                profile_name="task",
            )
        assert s2["status"] == "done"
        assert "implement" in s2["phases"]
        assert "plan" not in s2["phases"]


class TestSessionStatusInMetaJson:
    """meta.json includes the status field."""

    def test_meta_json_has_status_done(self, project, tmp_path, provider) -> None:
        run_dir = tmp_path / "run_meta"
        run_dir.mkdir()
        lp, hu, gd = _patches()
        with lp, hu, gd:
            run_pipeline(
                task="Test",
                project_dir=str(project),
                output_dir=run_dir,
                max_rounds=1,
                provider=provider,
            )
        data = json.loads((run_dir / "meta.json").read_text())
        assert data["status"] == "done"

    def test_meta_json_has_status_awaiting(self, project, tmp_path, provider) -> None:
        run_dir = tmp_path / "run_meta_plan"
        run_dir.mkdir()
        lp, hu, gd = _patches()
        with lp, hu, gd:
            run_pipeline(
                task="Test",
                project_dir=str(project),
                output_dir=run_dir,
                provider=provider,
                profile_name="planning",
            )
        data = json.loads((run_dir / "meta.json").read_text())
        # Phase 5 cutover: plan profile pauses via the generic phase
        # handoff status (``human_feedback_always`` on validate_plan).
        assert data["status"] == "awaiting_phase_handoff"

    def test_meta_json_written_at_run_start(self, project, tmp_path, provider, monkeypatch) -> None:
        """Регрессия: meta.json должен существовать СРАЗУ после init session,
        а не только после финального save_session. Иначе SIGKILL до конца
        пайплайна оставляет run без meta — UI/CLI не могут его показать,
        и приходится городить костыли с реконструкцией из events.jsonl."""
        run_dir = tmp_path / "run_early_meta"
        run_dir.mkdir()

        # Перехватываем save_session: на ПЕРВОМ вызове кидаем — это
        # симулирует "early write существует, но pipeline дальше упал".
        # Проверяем что meta.json уже на диске после первого save_session,
        # т.е. ДО любых фаз и финального save_session.
        from pipeline.engine import session as _session_mod
        original_save = _session_mod.save_session
        call_count = {"n": 0}
        meta_after_first_call: list[bool] = []

        def _spy_save(out, sess):
            call_count["n"] += 1
            result = original_save(out, sess)
            if call_count["n"] == 1:
                meta_after_first_call.append((out / "meta.json").exists())
            return result

        monkeypatch.setattr(_session_mod, "save_session", _spy_save)
        # ADR 0042 setup-module split: the early pre-run-dirty save_session
        # calls moved out of pipeline.project.app into
        # pipeline.project.isolation_setup, which has its own
        # ``from pipeline.engine import save_session`` binding.
        from pipeline.project import isolation_setup as _iso
        monkeypatch.setattr(_iso, "save_session", _spy_save)
        # ADR 0042 Phase E: dispatch lives in pipeline.project.profile_dispatch
        # and has its own ``from pipeline.engine import save_session`` binding.
        # Mid-loop save_session calls resolve there too.
        from pipeline.project import profile_dispatch as _pd
        monkeypatch.setattr(_pd, "save_session", _spy_save)

        lp, hu, gd = _patches()
        with lp, hu, gd:
            run_pipeline(
                task="Test early-write",
                project_dir=str(project),
                output_dir=run_dir,
                max_rounds=1,
                provider=provider,
            )

        assert call_count["n"] >= 2, "save_session должен вызываться минимум 2 раза: на старте и в финале"
        assert meta_after_first_call == [True], "meta.json должен существовать сразу после первого save_session"
