"""CLI-level coverage for the auto-correction follow-up wiring (ADR 0070).

The driver itself is unit-tested in
``tests/unit/pipeline/project/test_correction_followup.py``. This module
pins ``main()``'s behavior *around* the driver — the parts a driver-only
test cannot see:

* P1 — the ``awaiting_phase_handoff → rc=4`` CLI contract must hold on the
  FINAL session, i.e. even when a correction follow-up round (not just the
  first run) is what paused for a phase handoff.
* P2 — the loop must be gated on the same stdin+stdout-TTY test the
  commit-delivery gate uses, so a non-TTY invocation never auto-resumes
  even without ``--no-interactive``.

``run_pipeline`` is monkeypatched to a scripted stand-in so no real
pipeline / worktree / provider runs; ``main()`` is driven only far enough
to reach the post-run disposition logic.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def _fix_halt() -> dict[str, Any]:
    return {"status": "halted", "halt_reason": "commit_decision_fix"}


class _ScriptedRunPipeline:
    """Returns a scripted sequence of sessions; records every call.

    The same callable backs both ``main()``'s direct invocation and the
    follow-up rounds the driver runs, because both read the
    ``pipeline.project.cli.run_pipeline`` module global.
    """

    def __init__(self, sessions: list[dict[str, Any]]) -> None:
        self._sessions = sessions
        self.calls = 0

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        idx = self.calls
        self.calls += 1
        return self._sessions[min(idx, len(self._sessions) - 1)]


class TestAutoCorrectionFollowupCli:
    @pytest.fixture(autouse=True)
    def _isolated_workspace(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Pin workspace env + provide a valid single-project dir.

        ``main()`` resolves a workspace and validates the project before it
        ever reaches ``run_pipeline``; without isolation it would leak host
        state or fail on a missing project.
        """
        runspace = tmp_path / "runspace"
        (runspace / "runs").mkdir(parents=True)
        monkeypatch.setenv("ORCHO_WORKSPACE", str(tmp_path))
        monkeypatch.setenv("ORCHO_RUNSPACE", str(runspace))
        from core.infra import config as _config
        _config._reset_config()

        project = tmp_path / "proj"
        project.mkdir()
        (project / "pyproject.toml").write_text("[project]\nname='p'\n")
        self._project = project
        yield
        shutil.rmtree(runspace, ignore_errors=True)
        _config._reset_config()

    def _run_main(
        self, monkeypatch: pytest.MonkeyPatch, argv: list[str],
    ) -> int:
        """Drive ``main()`` with synthetic argv; return the exit code
        (0 when ``main()`` falls through without ``sys.exit``)."""
        from pipeline.project import cli

        saved_argv = sys.argv
        sys.argv = ["orchestrator", *argv]
        try:
            try:
                cli.main()
                return 0
            except SystemExit as exc:
                code = exc.code
                return code if isinstance(code, int) else 0
        finally:
            sys.argv = saved_argv

    def _base_argv(self, *, no_interactive: bool = False) -> list[str]:
        argv = [
            "--task", "demo",
            "--project", str(self._project),
            "--mock",
        ]
        if no_interactive:
            argv.append("--no-interactive")
        return argv

    # ── P1: rc=4 survives a follow-up that pauses for handoff ──────────────

    def test_followup_handoff_pause_still_yields_rc4(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # First run halts for fix; the correction round then pauses for a
        # phase handoff. The final-session check must still emit rc=4.
        fake = _ScriptedRunPipeline([
            _fix_halt(),
            {"status": "awaiting_phase_handoff"},
        ])
        monkeypatch.setattr("pipeline.project.cli.run_pipeline", fake)
        monkeypatch.setattr(
            "pipeline.project.cli._stdio_interactive", lambda: True,
        )

        rc = self._run_main(monkeypatch, self._base_argv())

        assert rc == 4, "follow-up handoff pause must preserve the rc=4 contract"
        assert fake.calls == 2, "expected initial run + one correction round"

    def test_followup_to_done_exits_clean(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake = _ScriptedRunPipeline([_fix_halt(), {"status": "done"}])
        monkeypatch.setattr("pipeline.project.cli.run_pipeline", fake)
        monkeypatch.setattr(
            "pipeline.project.cli._stdio_interactive", lambda: True,
        )

        rc = self._run_main(monkeypatch, self._base_argv())

        assert rc == 0
        assert fake.calls == 2

    # ── P2: the loop is gated on a real TTY, not just no_interactive ───────

    def test_no_followup_when_stdio_not_a_tty(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Interactive flag is absent (no --no-interactive), but stdio is not
        # a TTY (the default in a captured test). The loop must NOT fire.
        fake = _ScriptedRunPipeline([_fix_halt()])
        monkeypatch.setattr("pipeline.project.cli.run_pipeline", fake)
        monkeypatch.setattr(
            "pipeline.project.cli._stdio_interactive", lambda: False,
        )

        self._run_main(monkeypatch, self._base_argv())

        assert fake.calls == 1, "non-TTY run must not auto-resume a fix halt"

    def test_no_followup_when_no_interactive(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Even with a TTY, --no-interactive must keep the run halted for an
        # external controller (CI / MCP contract).
        fake = _ScriptedRunPipeline([_fix_halt()])
        monkeypatch.setattr("pipeline.project.cli.run_pipeline", fake)
        monkeypatch.setattr(
            "pipeline.project.cli._stdio_interactive", lambda: True,
        )

        self._run_main(monkeypatch, self._base_argv(no_interactive=True))

        assert fake.calls == 1


class TestCorrectionFollowupResumeCli:
    """Acceptance coverage for the retained-change ``--resume`` gate."""

    @pytest.fixture(autouse=True)
    def _isolated_workspace(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        runspace = tmp_path / "runspace"
        (runspace / "runs").mkdir(parents=True)
        monkeypatch.setenv("ORCHO_WORKSPACE", str(tmp_path))
        monkeypatch.setenv("ORCHO_RUNSPACE", str(runspace))
        from core.infra import config as _config
        _config._reset_config()

        self._project = tmp_path / "proj"
        self._project.mkdir()
        (self._project / "pyproject.toml").write_text("[project]\nname='p'\n")
        yield
        shutil.rmtree(runspace, ignore_errors=True)
        _config._reset_config()

    def _run_main(
        self, _monkeypatch: pytest.MonkeyPatch, argv: list[str],
    ) -> int:
        from pipeline.project import cli

        saved_argv = sys.argv
        sys.argv = ["orchestrator", *argv]
        try:
            try:
                cli.main()
                return 0
            except SystemExit as exc:
                return exc.code if isinstance(exc.code, int) else 0
        finally:
            sys.argv = saved_argv

    def _write_resume_meta(self, run_id: str, **overrides: Any) -> None:
        import json

        meta = {
            "run_id": run_id,
            "task": "parent task",
            "project": str(self._project),
            "profile": "feature",
            "status": "halted",
        }
        meta.update(overrides)
        run_dir = self._project.parent / "runspace" / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "meta.json").write_text(
            json.dumps(meta), encoding="utf-8",
        )

    def _resume_argv(
        self, run_id: str, *, no_interactive: bool = False,
    ) -> list[str]:
        argv = ["--resume", run_id, "--project", str(self._project), "--mock"]
        if no_interactive:
            argv.append("--no-interactive")
        return argv

    @staticmethod
    def _retained_decision(*, blocked: bool = False):
        from pipeline.control.continuation import ContinuationDecision

        return ContinuationDecision(
            run_id="parent-run",
            continuation_subject="retained_change",
            recommended_next_action="start_followup",
            allowed_intents=("followup", "exit"),
            requires_operator_comment=True,
            checkpoint_resumable=False,
            retained_worktree="/tmp/retained",
            diff_source="worktree",
            blocked=blocked,
            reason="retained worktree is unavailable" if blocked else "retained change",
        )

    def _patch_decision(
        self, monkeypatch: pytest.MonkeyPatch, decision: Any,
    ) -> None:
        monkeypatch.setattr(
            "pipeline.control.continuation.resolve_continuation_decision",
            lambda **_kwargs: decision,
        )

    def test_retained_change_prompts_followup_or_exit_and_launches(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        run_id = "parent-run"
        self._write_resume_meta(run_id)
        self._patch_decision(monkeypatch, self._retained_decision())
        monkeypatch.setattr("pipeline.project.cli._stdio_interactive", lambda: True)
        prompts: list[str] = []
        answers = iter(["followup", "Repair the rejection evidence"])
        monkeypatch.setattr(
            "builtins.input", lambda prompt: prompts.append(prompt) or next(answers),
        )
        launched: list[Any] = []
        monkeypatch.setattr(
            "sdk.run_control.launch.launch_correction_followup",
            lambda request: launched.append(request) or SimpleNamespace(
                run=SimpleNamespace(run_id="child-run"),
            ),
        )

        rc = self._run_main(monkeypatch, self._resume_argv(run_id))

        assert rc == 0
        assert prompts == [
            "Choose [followup/exit] (followup): ",
            "Operator comment (required): ",
        ]
        assert len(launched) == 1
        assert launched[0].operator_comment == "Repair the rejection evidence"
        assert "Started correction follow-up child-run." in capsys.readouterr().out

    def test_retained_change_rejects_empty_comment_with_rc2(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        run_id = "parent-run"
        self._write_resume_meta(run_id)
        self._patch_decision(monkeypatch, self._retained_decision())
        monkeypatch.setattr("pipeline.project.cli._stdio_interactive", lambda: True)
        answers = iter(["followup", "   "])
        monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
        monkeypatch.setattr(
            "sdk.run_control.launch.launch_correction_followup",
            lambda _request: pytest.fail("empty comment must not launch"),
        )
        rc = self._run_main(monkeypatch, self._resume_argv(run_id))

        assert rc == 2
        assert "Operator comment is required" in capsys.readouterr().err

    def test_retained_change_rejects_invalid_intent_with_rc2(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        run_id = "parent-run"
        self._write_resume_meta(run_id)
        self._patch_decision(monkeypatch, self._retained_decision())
        monkeypatch.setattr("pipeline.project.cli._stdio_interactive", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _prompt: "resume")

        rc = self._run_main(monkeypatch, self._resume_argv(run_id))

        assert rc == 2
        assert "Choose exactly 'followup' or 'exit'." in capsys.readouterr().err

    def test_retained_change_explicit_exit_succeeds_without_launch(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run_id = "parent-run"
        self._write_resume_meta(run_id)
        self._patch_decision(monkeypatch, self._retained_decision())
        monkeypatch.setattr("pipeline.project.cli._stdio_interactive", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _prompt: "exit")
        monkeypatch.setattr(
            "sdk.run_control.launch.launch_correction_followup",
            lambda _request: pytest.fail("exit must not launch"),
        )

        assert self._run_main(monkeypatch, self._resume_argv(run_id)) == 0

    def test_blocked_retained_change_reports_error_without_launch(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        run_id = "parent-run"
        self._write_resume_meta(run_id)
        self._patch_decision(monkeypatch, self._retained_decision(blocked=True))
        monkeypatch.setattr("pipeline.project.cli._stdio_interactive", lambda: True)
        monkeypatch.setattr(
            "sdk.run_control.launch.launch_correction_followup",
            lambda _request: pytest.fail("blocked decision must not launch"),
        )

        rc = self._run_main(monkeypatch, self._resume_argv(run_id))

        assert rc == 2
        assert "Correction follow-up is blocked" in capsys.readouterr().err

    def test_noninteractive_retained_change_requires_operator_input_with_rc2(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        run_id = "parent-run"
        self._write_resume_meta(run_id)
        self._patch_decision(monkeypatch, self._retained_decision())
        monkeypatch.setattr("pipeline.project.cli._stdio_interactive", lambda: False)

        rc = self._run_main(
            monkeypatch, self._resume_argv(run_id, no_interactive=True),
        )

        assert rc == 2
        assert "Correction follow-up requires operator input" in capsys.readouterr().err

    def test_checkpoint_resume_bypasses_correction_gate(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pipeline.control.continuation import ContinuationDecision

        run_id = "checkpoint-run"
        self._write_resume_meta(run_id, status="running")
        checkpoint = ContinuationDecision(
            run_id=run_id,
            continuation_subject="checkpoint",
            recommended_next_action="resume_checkpoint",
            allowed_intents=(),
            requires_operator_comment=False,
            checkpoint_resumable=True,
            retained_worktree=None,
            diff_source=None,
            blocked=False,
            reason="checkpoint-resumable terminal state",
        )
        self._patch_decision(monkeypatch, checkpoint)
        monkeypatch.setattr("pipeline.project.cli._stdio_interactive", lambda: False)
        pipeline = _ScriptedRunPipeline([{"status": "done"}])
        monkeypatch.setattr("pipeline.project.cli.run_pipeline", pipeline)

        rc = self._run_main(
            monkeypatch, self._resume_argv(run_id, no_interactive=True),
        )

        assert rc == 0
        assert pipeline.calls == 1
