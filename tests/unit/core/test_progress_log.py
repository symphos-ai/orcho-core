"""
Unit tests for log_phase() and set_progress_log() added to orchestrator.py.

Coverage:
 log_phase() writes correctly formatted lines to task-progress.log
 outcome appears as " → <outcome>" suffix
 status field is left-padded to 6 chars
 set_progress_log(None) makes log_phase() a no-op
 multiple calls append (not overwrite)
 log_phase() creates parent dirs if missing
 banner() auto-calls log_phase(status="START")
"""

from pathlib import Path

import pytest

from core.observability import (
    logging as orchestrator,  # ADR 0042 Phase J: log primitives live in core.observability.logging
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _reset_log(tmp_path: Path) -> Path:
    """Point the module-level _progress_log at a fresh file and return it."""
    log_file = tmp_path / "progress.log"
    orchestrator.set_progress_log(tmp_path)
    return log_file


def _lines(log_file: Path) -> list[str]:
    return log_file.read_text(encoding="utf-8").splitlines()


# ── set_progress_log ──────────────────────────────────────────────────────────

class TestSetProgressLog:
    def test_none_disables_logging(self, tmp_path: Path) -> None:
        """set_progress_log(None) → log_phase is a no-op, no file created."""
        orchestrator.set_progress_log(None)
        orchestrator.log_phase("PLAN", "PLAN")
        # No file should exist anywhere in tmp_path
        assert list(tmp_path.glob("*.log")) == []

    def test_sets_log_path(self, tmp_path: Path) -> None:
        log_file = _reset_log(tmp_path)
        orchestrator.log_phase("PLAN", "PLAN")
        assert log_file.exists()

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        deep = tmp_path / "a" / "b" / "c"
        orchestrator.set_progress_log(deep)
        orchestrator.log_phase("PLAN", "PLAN")
        assert (deep / "progress.log").exists()


# ── log_phase — line format ───────────────────────────────────────────────────

class TestLogPhaseFormat:
    @pytest.fixture(autouse=True)
    def setup_log(self, tmp_path: Path) -> None:
        self.log_file = _reset_log(tmp_path)

    def test_contains_phase_and_title(self) -> None:
        orchestrator.log_phase("PLAN", "PLAN — Claude reads project")
        line = _lines(self.log_file)[0]
        assert "[PLAN]" in line
        assert "PLAN — Claude reads project" in line

    def test_default_status_is_start(self) -> None:
        orchestrator.log_phase("IMPLEMENT", "BUILD")
        line = _lines(self.log_file)[0]
        assert "START" in line

    def test_custom_status(self) -> None:
        orchestrator.log_phase("IMPLEMENT", "BUILD", status="END")
        line = _lines(self.log_file)[0]
        assert "END" in line

    def test_status_padded_to_6(self) -> None:
        """Status column must be left-padded to 6 chars for alignment."""
        orchestrator.log_phase("X", "Y", status="DONE")
        line = _lines(self.log_file)[0]
        # 'DONE ' (padded) OR 'DONE' with at least 2 trailing spaces before next column
        assert "DONE  " in line or "DONE   " in line

    def test_outcome_appended_with_arrow(self) -> None:
        orchestrator.log_phase("PLAN", "PLAN", status="END", outcome="4,321 chars")
        line = _lines(self.log_file)[0]
        assert "→ 4,321 chars" in line

    def test_no_outcome_no_arrow(self) -> None:
        orchestrator.log_phase("PLAN", "PLAN")
        line = _lines(self.log_file)[0]
        assert "→" not in line

    def test_timestamp_prefix(self) -> None:
        """Line must start with [YYYY-MM-DD HH:MM:SS] timestamp."""
        import re
        orchestrator.log_phase("PLAN", "PLAN")
        line = _lines(self.log_file)[0]
        assert re.match(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", line)


# ── log_phase — append behaviour ─────────────────────────────────────────────

class TestLogPhaseAppend:
    @pytest.fixture(autouse=True)
    def setup_log(self, tmp_path: Path) -> None:
        self.log_file = _reset_log(tmp_path)

    def test_multiple_calls_append(self) -> None:
        orchestrator.log_phase("PLAN", "PLAN", status="START")
        orchestrator.log_phase("PLAN", "PLAN", status="END", outcome="ok")
        orchestrator.log_phase("IMPLEMENT", "BUILD", status="START")
        lines = _lines(self.log_file)
        assert len(lines) == 3

    def test_order_preserved(self) -> None:
        orchestrator.log_phase("A", "first")
        orchestrator.log_phase("B", "second")
        lines = _lines(self.log_file)
        assert "first" in lines[0]
        assert "second" in lines[1]


# ── banner() auto-logs START ──────────────────────────────────────────────────

class TestBannerAutoLogs:
    @pytest.fixture(autouse=True)
    def setup_log(self, tmp_path: Path) -> None:
        self.log_file = _reset_log(tmp_path)

    def test_banner_writes_start_entry(self) -> None:
        orchestrator.banner("PLAN", "PLAN — test")
        lines = _lines(self.log_file)
        assert len(lines) == 1
        assert "START" in lines[0]
        assert "[PLAN]" in lines[0]

    def test_banner_does_not_write_end(self) -> None:
        orchestrator.banner("PLAN", "PLAN")
        lines = _lines(self.log_file)
        assert all("END" not in line for line in lines)


# ── v2 phase end helper ──────────────────────────────────────────────────────

class TestV2PhaseEndHelper:
    def test_skipped_phase_prints_reason_and_logs_outcome(self, tmp_path, capsys):
        from pipeline.runtime import PipelineState

        log_file = _reset_log(tmp_path)
        state = PipelineState(task="t", project_dir="/p", plugin=None)
        state.extras["repair_round"] = 1
        state.phase_log["repair_changes"] = {"skipped": "review clean"}

        # ADR 0042 Phase E: the phase-log-end helper lives in
        # ``pipeline.project.profile_dispatch`` now. The orchestrator
        # used to re-export it via the ``_emit_phase_log_end`` alias;
        # after Phase F that alias is no longer pulled in (no
        # orchestrator-local consumer). Point the test directly at the
        # canonical name.
        from pipeline.project.profile_dispatch import emit_phase_log_end
        emit_phase_log_end("repair_changes", state)

        assert "skipped: review clean" in capsys.readouterr().out
        assert "→ skipped: review clean" in _lines(log_file)[0]


# ── Outcome content checks ────────────────────────────────────────────────────

class TestOutcomeVariants:
    """Verify the exact outcome strings the pipeline produces."""

    @pytest.fixture(autouse=True)
    def setup_log(self, tmp_path: Path) -> None:
        self.log_file = _reset_log(tmp_path)

    @pytest.mark.parametrize("outcome,expected", [
        ("lgtm",                       "lgtm"),
        ("issues found (18 lines)",    "issues found (18 lines)"),
        ("replan triggered",           "replan triggered"),
        ("ok",                         "ok"),
        ("dry-run",                    "dry-run"),
        ("skipped (no plan file)",     "skipped (no plan file)"),
        ("8,432 chars",                "8,432 chars"),
        ("3 files changed, +120/-40",  "3 files changed, +120/-40"),
    ])
    def test_outcome_in_log(self, outcome: str, expected: str) -> None:
        orchestrator.log_phase("CONTRACT_CHECK", "title", status="END", outcome=outcome)
        line = _lines(self.log_file)[0]
        assert expected in line
