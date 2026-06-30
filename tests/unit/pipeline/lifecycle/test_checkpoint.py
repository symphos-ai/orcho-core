"""
Unit tests for pipeline checkpoint store.

Tests use :memory: SQLite — zero filesystem, instant execution.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.checkpoint import CheckpointStore, PipelineState, PipelineStatus

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def store() -> CheckpointStore:
    """In-memory checkpoint store for fast tests."""
    return CheckpointStore(":memory:", run_id="test_run_001")


@pytest.fixture
def file_store(tmp_path: Path) -> CheckpointStore:
    """File-backed checkpoint store for persistence tests."""
    return CheckpointStore(tmp_path / "checkpoints.db", run_id="test_run_002")


# ─────────────────────────────────────────────────────────────────────────────
# Store basics
# ─────────────────────────────────────────────────────────────────────────────

class TestStoreInit:
    def test_run_id(self, store: CheckpointStore) -> None:
        assert store.run_id == "test_run_001"

    def test_auto_run_id_when_none(self) -> None:
        s = CheckpointStore(":memory:")
        assert len(s.run_id) > 0  # auto-generated timestamp

    def test_context_manager(self) -> None:
        with CheckpointStore(":memory:") as s:
            s.save_config({"task": "test"})
        # should not raise


# ─────────────────────────────────────────────────────────────────────────────
# Config persistence
# ─────────────────────────────────────────────────────────────────────────────

class TestSaveConfig:
    def test_save_and_load_config(self, store: CheckpointStore) -> None:
        cfg = {"task": "Add logging", "project": "/path", "model": "claude-sonnet"}
        store.save_config(cfg)
        state = store.load()
        assert state.run_config == cfg

    def test_config_overwrite(self, store: CheckpointStore) -> None:
        store.save_config({"task": "v1"})
        store.save_config({"task": "v2"})
        state = store.load()
        assert state.run_config["task"] == "v2"

    def test_status_defaults_to_running(self, store: CheckpointStore) -> None:
        store.save_config({"task": "test"})
        state = store.load()
        assert state.status == PipelineStatus.RUNNING


# ─────────────────────────────────────────────────────────────────────────────
# Phase checkpoints
# ─────────────────────────────────────────────────────────────────────────────

class TestSavePhase:
    def test_save_single_phase(self, store: CheckpointStore) -> None:
        store.save_phase("plan", {"output": "plan text", "model": "opus"})
        state = store.load()
        assert state.completed == ["plan"]
        assert state.phases["plan"]["output"] == "plan text"

    def test_save_multiple_phases_preserves_order(self, store: CheckpointStore) -> None:
        store.save_phase("plan", {"output": "p"})
        store.save_phase("validate_plan", {"critique": "ok"})
        store.save_phase("implement", {"output": "b"})
        state = store.load()
        assert state.completed == ["plan", "validate_plan", "implement"]

    def test_phase_data_roundtrip(self, store: CheckpointStore) -> None:
        data = {
            "output": "long plan text with unicode: ёжик",
            "files": ["src/main.py", "tests/test_main.py"],
            "count": 42,
            "nested": {"a": [1, 2, 3]},
        }
        store.save_phase("plan", data)
        state = store.load()
        assert state.phases["plan"] == data

    def test_has_phase(self, store: CheckpointStore) -> None:
        store.save_phase("plan", {})
        state = store.load()
        assert state.has_phase("plan")
        assert not state.has_phase("implement")

    def test_should_skip(self, store: CheckpointStore) -> None:
        store.save_phase("plan", {})
        store.save_phase("validate_plan", {})
        state = store.load()
        assert state.should_skip("plan")
        assert state.should_skip("validate_plan")
        assert not state.should_skip("implement")

    def test_last_completed_phase(self, store: CheckpointStore) -> None:
        store.save_phase("plan", {})
        store.save_phase("validate_plan", {})
        state = store.load()
        assert state.last_completed_phase == "validate_plan"

    def test_last_completed_phase_empty(self, store: CheckpointStore) -> None:
        state = store.load()
        assert state.last_completed_phase is None


# ─────────────────────────────────────────────────────────────────────────────
# Status management
# ─────────────────────────────────────────────────────────────────────────────

class TestSetStatus:
    def test_set_done(self, store: CheckpointStore) -> None:
        store.save_config({"task": "t"})
        store.set_status(PipelineStatus.DONE)
        state = store.load()
        assert state.status == PipelineStatus.DONE

    def test_set_awaiting_human_review(self, store: CheckpointStore) -> None:
        store.save_config({"task": "t"})
        store.set_status(PipelineStatus.AWAITING_HUMAN_REVIEW)
        state = store.load()
        assert state.status == PipelineStatus.AWAITING_HUMAN_REVIEW

    def test_set_failed(self, store: CheckpointStore) -> None:
        store.save_config({"task": "t"})
        store.set_status(PipelineStatus.FAILED)
        state = store.load()
        assert state.status == PipelineStatus.FAILED

    def test_set_cancelled(self, store: CheckpointStore) -> None:
        """Manual cancel (UI Cancel-run at the phase-handoff pause)
 round-trips through the checkpoint, so resume logic doesn't re-enter
 the pause."""
        store.save_config({"task": "t"})
        store.set_status(PipelineStatus.AWAITING_PHASE_HANDOFF)
        store.set_status(PipelineStatus.CANCELLED)
        state = store.load()
        assert state.status == PipelineStatus.CANCELLED


# ─────────────────────────────────────────────────────────────────────────────
# Resume simulation
# ─────────────────────────────────────────────────────────────────────────────

class TestResume:
    """Simulate crash-recovery: save phases → new store → load."""

    def test_resume_after_plan(self, file_store: CheckpointStore) -> None:
        db_path = file_store._db_path
        run_id = file_store.run_id

        # Simulate: plan completes, then crash before build
        file_store.save_config({"task": "Add logging", "max_rounds": 1})
        file_store.save_phase("plan", {"output": "plan markdown"})
        file_store.save_phase("validate_plan", {"critique": "approved review"})
        file_store.set_status(PipelineStatus.AWAITING_HUMAN_REVIEW)
        file_store.close()

        # Resume: new process opens same DB
        store2 = CheckpointStore(db_path, run_id=run_id)
        state = store2.load()
        assert state.status == PipelineStatus.AWAITING_HUMAN_REVIEW
        assert state.completed == ["plan", "validate_plan"]
        assert state.should_skip("plan")
        assert state.should_skip("validate_plan")
        assert not state.should_skip("implement")
        assert state.run_config["task"] == "Add logging"
        store2.close()

    def test_resume_continues_from_build(self, file_store: CheckpointStore) -> None:
        db_path = file_store._db_path
        run_id = file_store.run_id

        # Session 1: plan + validate_plan + build crash
        file_store.save_config({"task": "Add logging"})
        file_store.save_phase("plan", {"output": "p"})
        file_store.save_phase("validate_plan", {"critique": "ok"})
        file_store.close()

        # Session 2: resume → skip plan/validate_plan, run build
        store2 = CheckpointStore(db_path, run_id=run_id)
        state = store2.load()
        assert not state.should_skip("implement")
        # Simulate build completing
        store2.save_phase("implement", {"output": "build output"})
        store2.save_phase("review_round_1", {"critique": "approved review"})
        store2.set_status(PipelineStatus.DONE)
        store2.close()

        # Verify final state
        store3 = CheckpointStore(db_path, run_id=run_id)
        final = store3.load()
        assert final.status == PipelineStatus.DONE
        assert final.completed == ["plan", "validate_plan", "implement", "review_round_1"]
        store3.close()


# ─────────────────────────────────────────────────────────────────────────────
# List runs
# ─────────────────────────────────────────────────────────────────────────────

class TestListRuns:
    def test_list_empty(self, store: CheckpointStore) -> None:
        assert store.list_runs() == []

    def test_list_single_run(self, store: CheckpointStore) -> None:
        store.save_config({"task": "t1"})
        runs = store.list_runs()
        assert len(runs) == 1
        assert runs[0]["run_id"] == "test_run_001"
        assert runs[0]["status"] == "running"

    def test_list_multiple_runs(self, tmp_path: Path) -> None:
        db_path = tmp_path / "multi.db"
        s1 = CheckpointStore(db_path, run_id="run_A")
        s1.save_config({"task": "A"})
        s1.set_status(PipelineStatus.DONE)

        s2 = CheckpointStore(db_path, run_id="run_B")
        s2.save_config({"task": "B"})

        runs = s2.list_runs()
        assert len(runs) == 2
        run_ids = [r["run_id"] for r in runs]
        assert "run_A" in run_ids
        assert "run_B" in run_ids


# ─────────────────────────────────────────────────────────────────────────────
# Empty state
# ─────────────────────────────────────────────────────────────────────────────

class TestEmptyState:
    def test_load_nonexistent_run(self, store: CheckpointStore) -> None:
        state = store.load("nonexistent_run_id")
        assert state.completed == []
        assert state.phases == {}
        assert state.status == PipelineStatus.RUNNING

    def test_pipeline_state_defaults(self) -> None:
        state = PipelineState()
        assert state.completed == []
        assert state.phases == {}
        assert state.status == PipelineStatus.RUNNING
        assert state.run_id == ""
        assert state.last_completed_phase is None


# ─────────────────────────────────────────────────────────────────────────────
# Edge cases
# ─────────────────────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_duplicate_phase_appends(self, store: CheckpointStore) -> None:
        """Duplicate phase save appends (log-style), last value wins in phases dict."""
        store.save_phase("plan", {"output": "v1"})
        store.save_phase("plan", {"output": "v2"})
        state = store.load()
        # Both in completed (append-only log)
        assert state.completed.count("plan") == 2
        # Last value wins in phases dict
        assert state.phases["plan"]["output"] == "v2"

    def test_large_data(self, store: CheckpointStore) -> None:
        """Store large phase outputs without issues."""
        large_output = "x" * 100_000
        store.save_phase("implement", {"output": large_output})
        state = store.load()
        assert len(state.phases["implement"]["output"]) == 100_000

    def test_special_characters(self, store: CheckpointStore) -> None:
        store.save_phase("plan", {"output": 'quotes "and" \\backslash\\ 日本語'})
        state = store.load()
        assert "日本語" in state.phases["plan"]["output"]


# ─────────────────────────────────────────────────────────────────────────────
# E1 — agent_sessions: persist agent.session_id across subprocess restarts
# ─────────────────────────────────────────────────────────────────────────────


class TestAgentSessions:
    """``set_agent_session`` / ``get_agent_sessions`` pin the E1 contract:
    ``agent.session_id`` survives ``orcho_run_resume`` so ``--resume <sid>``
    actually fires on the next subprocess's first invoke."""

    def test_get_empty_for_fresh_run(self, store: CheckpointStore) -> None:
        assert store.get_agent_sessions() == {}

    def test_set_then_get_one(self, store: CheckpointStore) -> None:
        store.set_agent_session("plan_agent", "sid-86481484")
        assert store.get_agent_sessions() == {"plan_agent": "sid-86481484"}

    def test_set_multiple_roles_independent(
        self, store: CheckpointStore,
    ) -> None:
        store.set_agent_session("plan_agent", "sid-A")
        store.set_agent_session("validate_plan_agent", "sid-B")
        store.set_agent_session("implement_agent", "sid-C")
        assert store.get_agent_sessions() == {
            "plan_agent": "sid-A",
            "validate_plan_agent": "sid-B",
            "implement_agent": "sid-C",
        }

    def test_set_replaces_existing(self, store: CheckpointStore) -> None:
        """Repeated saves for the same role overwrite — the last invoke
        of a phase advances ``agent.session_id``, the next subprocess
        must see the latest."""
        store.set_agent_session("plan_agent", "sid-old")
        store.set_agent_session("plan_agent", "sid-new")
        assert store.get_agent_sessions() == {"plan_agent": "sid-new"}

    def test_set_none_clears(self, store: CheckpointStore) -> None:
        """Passing ``None`` removes the record so the next subprocess
        starts the role fresh — used when a runtime explicitly burns
        a session."""
        store.set_agent_session("plan_agent", "sid-x")
        store.set_agent_session("plan_agent", None)
        assert store.get_agent_sessions() == {}

    def test_isolated_per_run(self, file_store: CheckpointStore) -> None:
        """Two runs sharing the same DB file see independent
        agent_sessions — checkpoints.db can be shared across runs
        (it isn't today, but the schema must not assume single-run).
        """
        file_store.set_agent_session("plan_agent", "sid-run-002")
        # Different run_id on same DB.
        from pipeline.checkpoint import CheckpointStore as _CS
        other = _CS(file_store._db_path, run_id="other_run")
        assert other.get_agent_sessions() == {}
        other.set_agent_session("plan_agent", "sid-other")
        # Original run unchanged.
        assert file_store.get_agent_sessions() == {
            "plan_agent": "sid-run-002",
        }
        # Cross-query by explicit run_id.
        assert file_store.get_agent_sessions(run_id="other_run") == {
            "plan_agent": "sid-other",
        }

    def test_survives_close_and_reopen(self, tmp_path) -> None:
        """The whole point: after a process death + restart, the new
        ``CheckpointStore`` instance sees the persisted session_ids.
        Simulates ``orcho_run_resume`` exactly."""
        db_path = tmp_path / "ckpt.db"
        s1 = CheckpointStore(db_path, run_id="run_resume")
        s1.set_agent_session("plan_agent", "sid-86481484")
        s1.set_agent_session("validate_plan_agent", "sid-d19b8eca")
        s1.close()

        # New process: instantiate against the same on-disk DB.
        s2 = CheckpointStore(db_path, run_id="run_resume")
        assert s2.get_agent_sessions() == {
            "plan_agent": "sid-86481484",
            "validate_plan_agent": "sid-d19b8eca",
        }
