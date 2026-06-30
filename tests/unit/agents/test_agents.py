"""Unit tests for orchestrator-level agent logging plumbing.

The runtime wrapper contracts for ClaudeAgent / CodexAgent live in
``test_claude_agent.py`` and ``test_codex_agent.py`` respectively.
This file keeps the orchestrator-side glue: progress log placement,
output log timestamp wiring.
"""

from pathlib import Path

import pytest

import agents as agents_module

# ── set_progress_log (runs/{ts}/ semantics — ts encoded in dir, not filename) ──

class TestSetProgressLog:
    """Tests for set_progress_log under the runs/{ts}/ structure."""

    @pytest.fixture(autouse=True)
    def reset_progress_log(self):
        """Isolate _progress_log global between tests.
 The real global lives in core.logging (orchestrator proxies via __getattr__).
 """
        import core.observability.logging as _logging
        yield
        _logging._progress_log = None

    def test_no_ts_creates_fixed_name(self, tmp_path: Path) -> None:
        """session_ts is ignored — file is always named progress.log."""
        from core.observability import (
            logging as orchestrator,  # ADR 0042 Phase J: log primitives canonical home
        )
        orchestrator.set_progress_log(tmp_path, session_ts="")
        assert orchestrator._progress_log is not None
        assert orchestrator._progress_log.name == "progress.log"

    def test_with_ts_still_creates_fixed_name(self, tmp_path: Path) -> None:
        """Even with session_ts the file is always named progress.log."""
        from core.observability import (
            logging as orchestrator,  # ADR 0042 Phase J: log primitives canonical home
        )
        orchestrator.set_progress_log(tmp_path, session_ts="20260501_114200")
        assert orchestrator._progress_log is not None
        assert orchestrator._progress_log.name == "progress.log"

    def test_none_output_dir_leaves_log_none(self) -> None:
        from core.observability import (
            logging as orchestrator,  # ADR 0042 Phase J: log primitives canonical home
        )
        orchestrator._progress_log = None
        orchestrator.set_progress_log(None, session_ts="20260501_114200")
        assert orchestrator._progress_log is None

    def test_log_phase_writes_to_fixed_file(self, tmp_path: Path) -> None:
        from core.observability import (
            logging as orchestrator,  # ADR 0042 Phase J: log primitives canonical home
        )
        orchestrator.set_progress_log(tmp_path, session_ts="20260501_114200")
        orchestrator.log_phase("PLAN", "Test phase", "START")
        log_file = tmp_path / "progress.log"
        assert log_file.exists()
        content = log_file.read_text(encoding="utf-8")
        assert "PLAN" in content
        assert "Test phase" in content


# ── agent_output log timestamp (run_pipeline integration) ──────────────────


class TestAgentOutputLog:
    """Verify run_pipeline creates output.log (fixed name, ts in dir)."""

    def test_agent_log_is_output_log(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        """run_pipeline sets agent log to output.log (fixed name)."""
        from pipeline.project.app import run_pipeline

        captured_path: list[Path] = []

        def fake_set_agent_log(p):
            if p is not None:
                captured_path.append(p)

        monkeypatch.setattr(agents_module, "set_agent_log", fake_set_agent_log)

        # Stub out the unified runtime entry point so the pipeline doesn't
        # actually call subprocesses. Both Claude and Codex agents satisfy
        # ``IAgentRuntime`` with a single ``invoke()`` method after Phase 7.
        monkeypatch.setattr(
            agents_module.ClaudeAgent, "invoke",
            lambda *a, **kw: "stub output",
        )
        monkeypatch.setattr(
            agents_module.CodexAgent, "invoke",
            lambda *a, **kw: "stub review output",
        )

        from tests.conftest import init_git_repo
        project = tmp_path / "project"
        init_git_repo(project)

        run_pipeline(
            task="stub task",
            project_dir=str(project),
            max_rounds=0,
            profile_name="task",
            output_dir=tmp_path / "kanban",
            dry_run=True,
        )

        assert len(captured_path) == 1, "set_agent_log should be called once"
        log_name = captured_path[0].name
        assert log_name == "output.log", f"expected output.log, got: {log_name}"


# ── back-compat smoke: imports stable ──────────────────────────────────────


class TestPublicImports:
    """The public surface re-exports stay stable across Phase 7's
    Protocol collapse. Plugin authors import these from ``agents``."""

    def test_iagentruntime_reachable(self) -> None:
        assert hasattr(agents_module, "IAgentRuntime")

    def test_claude_agent_reachable(self) -> None:
        assert hasattr(agents_module, "ClaudeAgent")
        # Stub fixture below verifies the runtime contract (invoke()).

    def test_codex_agent_reachable(self) -> None:
        assert hasattr(agents_module, "CodexAgent")

    def test_session_mode_enum_reachable(self) -> None:
        """SessionMode is the orchestrator-side enum, kept stable."""
        assert hasattr(agents_module, "SessionMode")

    def test_subprocess_reexport_for_monkeypatching(self) -> None:
        """``agents.subprocess`` re-export survives — tests rely on it."""
        assert agents_module.subprocess is not None
