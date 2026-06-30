"""``pipeline.cross_project.cli.main()`` — cross CLI entry point.

Strategy mirrors the single-project ``test_cli_orcho.py::TestProjectOrchestratorMain``:
call ``main()`` directly with monkeypatched ``sys.argv`` and a mocked
``run_cross_pipeline``. ``ORCHO_WORKSPACE`` is set to a ``tmp_path`` so
``config.get_runs_dir()`` resolves without touching the user's real
workspace. ``--projects`` paths are created as empty tmp_path dirs (the
``parse_projects`` step requires every project path to exist on disk).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


class TestCrossOrchestratorMain:
    @pytest.fixture
    def main_env(self, tmp_path: Path, monkeypatch):
        """Scratch workspace + two project dirs + a mocked
        ``run_cross_pipeline``. Returns paths and the mock for
        assertions."""
        from pipeline.cross_project import orchestrator as cross

        workspace = tmp_path / "ws"
        workspace.mkdir()
        unity = tmp_path / "unity"
        unity.mkdir()
        api = tmp_path / "api"
        api.mkdir()
        monkeypatch.setenv("ORCHO_WORKSPACE", str(workspace))
        # main() writes ORCHO_RUNSPACE directly when --workspace is
        # supplied; pre-register it with monkeypatch so the teardown
        # restores it (otherwise the SUT's write leaks to later tests).
        monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)

        run_cross_mock = MagicMock(return_value=None)
        monkeypatch.setattr(cross, "run_cross_pipeline", run_cross_mock)
        return {
            "workspace": workspace,
            "unity": unity,
            "api": api,
            "run_cross_pipeline": run_cross_mock,
        }

    def _set_argv(self, monkeypatch, *args: str) -> None:
        monkeypatch.setattr(sys, "argv", ["orcho-cross", *args])

    def test_happy_path_calls_run_cross_pipeline_with_projects_dict(
        self, main_env, monkeypatch
    ) -> None:
        self._set_argv(
            monkeypatch,
            "--task", "T",
            "--projects",
            f"unity:{main_env['unity']}",
            f"api:{main_env['api']}",
            "--mock",
            "--workspace", str(main_env["workspace"]),
        )
        from pipeline.cross_project.cli import main
        main()

        assert main_env["run_cross_pipeline"].called
        kwargs = main_env["run_cross_pipeline"].call_args.kwargs
        assert kwargs["task"] == "T"
        # The kwarg name is ``projects`` (not ``project_specs``); the
        # value is an alias → Path dict produced by parse_projects.
        assert "projects" in kwargs
        projects = kwargs["projects"]
        assert set(projects.keys()) == {"unity", "api"}
        assert projects["unity"] == main_env["unity"]
        assert projects["api"] == main_env["api"]

    def test_project_aliases_resolve_from_workspace_config(
        self, main_env, monkeypatch
    ) -> None:
        local_config = (
            main_env["workspace"] / ".orcho" / "config.local.json"
        )
        local_config.parent.mkdir(parents=True)
        local_config.write_text(
            json.dumps({
                "projects": {
                    "unity": str(main_env["unity"]),
                    "api": str(main_env["api"]),
                }
            }),
            encoding="utf-8",
        )
        self._set_argv(
            monkeypatch,
            "--task", "T",
            "--projects", "unity", "api",
            "--mock",
            "--workspace", str(main_env["workspace"]),
        )
        from pipeline.cross_project.cli import main
        main()

        projects = main_env["run_cross_pipeline"].call_args.kwargs["projects"]
        assert projects == {
            "unity": main_env["unity"].resolve(),
            "api": main_env["api"].resolve(),
        }

    def test_task_file_wins_over_task(
        self, main_env, monkeypatch, tmp_path: Path
    ) -> None:
        task_file = tmp_path / "task.md"
        task_file.write_text("body from file", encoding="utf-8")
        self._set_argv(
            monkeypatch,
            "--task", "from-cli",
            "--task-file", str(task_file),
            "--projects", f"unity:{main_env['unity']}",
            "--mock",
            "--workspace", str(main_env["workspace"]),
        )
        from pipeline.cross_project.cli import main
        main()

        kwargs = main_env["run_cross_pipeline"].call_args.kwargs
        assert kwargs["task"] == "body from file"

    def test_failed_status_exits_1(
        self, main_env, monkeypatch
    ) -> None:
        """``run_cross_pipeline`` never calls ``sys.exit``. The CLI
        entrypoint maps ``session['status'] == 'failed'`` (contract_check
        parse error or rejected verdict) to exit code 1 so CI catches
        the cross-run failure as a normal non-zero exit.
        """
        main_env["run_cross_pipeline"].return_value = {
            "status": "failed",
            "failure_reason": "contract_check rejected for api",
        }
        self._set_argv(
            monkeypatch,
            "--task", "T",
            "--projects",
            f"unity:{main_env['unity']}",
            f"api:{main_env['api']}",
            "--mock",
            "--workspace", str(main_env["workspace"]),
        )
        from pipeline.cross_project.cli import main
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1

    def test_cross_final_acceptance_failure_reason_exits_1(
        self, main_env, monkeypatch, capsys,
    ) -> None:
        """ADR 0025 Phase 3: when the system release gate rejects, the
        runner reports ``status='failed'`` with a ``failure_reason``
        naming ``cross_final_acceptance``. The CLI must surface that
        reason on stderr so log greppers and dashboards can attribute
        the failure to the gate (vs contract_check) and exit non-zero.
        """
        main_env["run_cross_pipeline"].return_value = {
            "status": "failed",
            "failure_reason": (
                "cross_final_acceptance: agent REJECTED"
            ),
        }
        self._set_argv(
            monkeypatch,
            "--task", "T",
            "--projects",
            f"unity:{main_env['unity']}",
            f"api:{main_env['api']}",
            "--mock",
            "--workspace", str(main_env["workspace"]),
        )
        from pipeline.cross_project.cli import main
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1
        combined = capsys.readouterr()
        assert "cross_final_acceptance" in (combined.out + combined.err), (
            "CLI must surface the failure_reason text so the gate name "
            "is grep-able in logs and CI output"
        )

    def test_done_status_exits_zero(
        self, main_env, monkeypatch
    ) -> None:
        """Happy-path lock: ``status='done'`` does not raise SystemExit
        from main(). Without this, a regression that always exits could
        masquerade as passing CI."""
        main_env["run_cross_pipeline"].return_value = {"status": "done"}
        self._set_argv(
            monkeypatch,
            "--task", "T",
            "--projects",
            f"unity:{main_env['unity']}",
            f"api:{main_env['api']}",
            "--mock",
            "--workspace", str(main_env["workspace"]),
        )
        from pipeline.cross_project.cli import main
        # Normal completion: no SystemExit raised.
        main()

    def test_missing_task_exits_1(
        self, main_env, monkeypatch, capsys: pytest.CaptureFixture
    ) -> None:
        self._set_argv(
            monkeypatch,
            "--projects", f"unity:{main_env['unity']}",
            "--mock",
            "--workspace", str(main_env["workspace"]),
        )
        from pipeline.cross_project.cli import main
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1
        assert "provide --task or --task-file" in capsys.readouterr().err
        assert not main_env["run_cross_pipeline"].called

    def test_mode_full_accepted_and_forwarded(
        self, main_env, monkeypatch
    ) -> None:
        self._set_argv(
            monkeypatch,
            "--task", "T",
            "--projects", f"unity:{main_env['unity']}",
            "--mode", "full",
            "--mock",
            "--workspace", str(main_env["workspace"]),
        )
        from pipeline.cross_project.cli import main
        main()
        assert main_env["run_cross_pipeline"].call_args.kwargs["cross_mode"] == "full"

    def test_mode_plan_accepted_and_forwarded(
        self, main_env, monkeypatch
    ) -> None:
        self._set_argv(
            monkeypatch,
            "--task", "T",
            "--projects", f"unity:{main_env['unity']}",
            "--mode", "plan",
            "--mock",
            "--workspace", str(main_env["workspace"]),
        )
        from pipeline.cross_project.cli import main
        main()
        assert main_env["run_cross_pipeline"].call_args.kwargs["cross_mode"] == "plan"

    def test_output_flag_last_wins(
        self, main_env, monkeypatch
    ) -> None:
        calls: list[str] = []
        monkeypatch.setattr(
            "core.observability.logging.apply_output_mode",
            lambda mode: calls.append(mode) or mode,
        )
        self._set_argv(
            monkeypatch,
            "--task", "T",
            "--projects", f"unity:{main_env['unity']}",
            "--stream-output",
            "--verbose",
            "--output", "summary",
            "--mock",
            "--workspace", str(main_env["workspace"]),
        )
        from pipeline.cross_project.cli import main
        main()
        assert calls == ["summary"]

    def test_invalid_mode_exits_2(
        self, main_env, monkeypatch
    ) -> None:
        self._set_argv(
            monkeypatch,
            "--task", "T",
            "--projects", f"unity:{main_env['unity']}",
            "--mode", "bogus",
            "--mock",
            "--workspace", str(main_env["workspace"]),
        )
        from pipeline.cross_project.cli import main
        with pytest.raises(SystemExit) as exc:
            main()
        # argparse's ``choices=`` rejection → SystemExit(2).
        assert exc.value.code == 2

    def test_plan_file_forwarded_as_string(
        self, main_env, monkeypatch
    ) -> None:
        self._set_argv(
            monkeypatch,
            "--task", "T",
            "--projects", f"unity:{main_env['unity']}",
            "--plan-file", "cross_plan.md",
            "--mock",
            "--workspace", str(main_env["workspace"]),
        )
        from pipeline.cross_project.cli import main
        main()
        # ``plan_file`` is passed verbatim as a string (argparse
        # ``type=str``), not wrapped in Path.
        assert main_env["run_cross_pipeline"].call_args.kwargs["plan_file"] == "cross_plan.md"

    def test_resume_with_task_treated_as_followup(
        self, main_env, monkeypatch, tmp_path
    ) -> None:
        # ``--resume RUN_ID --task X`` is the canonical follow-up
        # invocation: a brand-new run that carries the parent run as
        # context. ``resume_from`` must NOT be forwarded (no checkpoint
        # hydration); parent linkage is recorded in followup_* kwargs.
        import json
        run_id = "20260512_001"
        runs_dir = main_env["workspace"] / "runspace" / "runs"
        runs_dir.mkdir(parents=True)
        parent_dir = runs_dir / run_id
        parent_dir.mkdir()
        (parent_dir / "meta.json").write_text(json.dumps({
            "task": "original",
            "projects": {"unity": str(main_env["unity"])},
            "status": "done",
        }), encoding="utf-8")
        self._set_argv(
            monkeypatch,
            "--task", "T",
            "--projects", f"unity:{main_env['unity']}",
            "--resume", run_id,
            "--no-interactive",
            "--mock",
            "--workspace", str(main_env["workspace"]),
        )
        from pipeline.cross_project.cli import main
        main()
        kwargs = main_env["run_cross_pipeline"].call_args.kwargs
        assert kwargs["resume_from"] is None
        assert kwargs["resume_mode"] == "followup"
        assert kwargs["followup_parent_run_id"] == run_id
        assert kwargs["followup_base_task"] == "original"

    def test_resume_followup_extracts_per_alias_session_seeds(
        self, main_env, monkeypatch, tmp_path
    ) -> None:
        # When the parent cross run has child sub-pipeline meta.json
        # files with Step-0 session ids per alias, the cross
        # follow-up must extract them into a per-alias seed map and
        # pass it through to ``run_cross_pipeline``.
        import json
        run_id = "20260512_010"
        runs_dir = main_env["workspace"] / "runspace" / "runs"
        runs_dir.mkdir(parents=True)
        parent_dir = runs_dir / run_id
        parent_dir.mkdir()
        # Top-level cross meta.
        (parent_dir / "meta.json").write_text(json.dumps({
            "task": "original cross",
            "projects": {"unity": str(main_env["unity"])},
            "status": "done",
        }), encoding="utf-8")
        # Per-alias child meta carrying the Step-0 shape.
        alias_dir = parent_dir / "unity"
        alias_dir.mkdir()
        (alias_dir / "meta.json").write_text(json.dumps({
            "task": "unity child task",
            "project": str(main_env["unity"]),
            "status": "done",
            "phases": {
                "plan": [{"attempt": 1, "output": "p",
                          "session_id": "unity-plan-sid"}],
                "implement": {"output": "i",
                              "meta": {"session_id": "unity-impl-sid"}},
            },
        }), encoding="utf-8")
        self._set_argv(
            monkeypatch,
            "--task", "follow-up cross",
            "--projects", f"unity:{main_env['unity']}",
            "--resume", run_id,
            "--no-interactive",
            "--mock",
            "--workspace", str(main_env["workspace"]),
        )
        from pipeline.cross_project.cli import main
        main()
        kwargs = main_env["run_cross_pipeline"].call_args.kwargs
        assert kwargs["resume_mode"] == "followup"
        assert kwargs["followup_session_seeds_per_alias"] == {
            "unity": {"plan": "unity-plan-sid",
                      "implement": "unity-impl-sid"},
        }

    def test_resume_followup_with_no_extractable_seeds_passes_none(
        self, main_env, monkeypatch, tmp_path
    ) -> None:
        # Parent cross run has no per-alias child meta (e.g. parent
        # never spawned a sub-pipeline that captured session ids).
        # Follow-up should pass ``followup_session_seeds_per_alias=None``
        # so the downstream loop falls through to fresh children.
        import json
        run_id = "20260512_011"
        runs_dir = main_env["workspace"] / "runspace" / "runs"
        runs_dir.mkdir(parents=True)
        parent_dir = runs_dir / run_id
        parent_dir.mkdir()
        (parent_dir / "meta.json").write_text(json.dumps({
            "task": "original",
            "projects": {"unity": str(main_env["unity"])},
            "status": "done",
        }), encoding="utf-8")
        self._set_argv(
            monkeypatch,
            "--task", "follow-up",
            "--projects", f"unity:{main_env['unity']}",
            "--resume", run_id,
            "--no-interactive",
            "--mock",
            "--workspace", str(main_env["workspace"]),
        )
        from pipeline.cross_project.cli import main
        main()
        kwargs = main_env["run_cross_pipeline"].call_args.kwargs
        assert kwargs["resume_mode"] == "followup"
        assert kwargs["followup_session_seeds_per_alias"] is None

    def test_resume_no_task_incomplete_parent_is_checkpoint(
        self, main_env, monkeypatch
    ) -> None:
        # Bare ``--resume RUN_ID`` against an incomplete parent and a
        # non-interactive invocation continues the existing run in
        # place: resume_from forwards, follow-up kwargs stay empty.
        import json
        run_id = "20260512_002"
        runs_dir = main_env["workspace"] / "runspace" / "runs"
        runs_dir.mkdir(parents=True)
        parent_dir = runs_dir / run_id
        parent_dir.mkdir()
        (parent_dir / "meta.json").write_text(json.dumps({
            "task": "T",
            "projects": {"unity": str(main_env["unity"])},
            "status": "interrupted",
        }), encoding="utf-8")
        self._set_argv(
            monkeypatch,
            "--projects", f"unity:{main_env['unity']}",
            "--resume", run_id,
            "--no-interactive",
            "--mock",
            "--workspace", str(main_env["workspace"]),
        )
        from pipeline.cross_project.cli import main
        main()
        kwargs = main_env["run_cross_pipeline"].call_args.kwargs
        assert kwargs["resume_from"] == run_id
        # ``resume_mode`` is a follow-up marker; CHECKPOINT leaves it
        # absent so meta.json doesn't carry a misleading field.
        assert kwargs["resume_mode"] is None
        assert kwargs["followup_parent_run_id"] is None

    def test_resume_against_phase_handoff_halt_short_circuits(
        self, main_env, monkeypatch, capsys: pytest.CaptureFixture,
    ) -> None:
        """ADR 0038 + ADR 0035: ``orcho cross --resume <run_id>`` against
        a checkpoint whose meta carries
        ``halt_reason='phase_handoff_halt'`` must exit 0 with the
        "cannot be resumed" hint and NEVER spawn
        ``run_cross_pipeline``.

        The SDK halt path stamps ``halted_at`` + ``halt_reason`` +
        writes ``evidence.json`` synchronously when the operator
        decides ``halt``; re-entering ``run_cross_pipeline`` on
        resume would call ``_finalize_cross_terminal`` a second time
        and overwrite those terminal-state timestamps. Mirror of the
        single-run ``main()`` guard
        (``project_orchestrator.py::_is_terminal_phase_handoff_halt``).
        """
        run_id = "20260524_001_halted"
        runs_dir = main_env["workspace"] / "runspace" / "runs"
        runs_dir.mkdir(parents=True)
        parent_dir = runs_dir / run_id
        parent_dir.mkdir()
        (parent_dir / "meta.json").write_text(json.dumps({
            "task": "T",
            "projects": {"unity": str(main_env["unity"])},
            "status":      "halted",
            "halt_reason": "phase_handoff_halt",
            "halted_at":   "2026-05-24T20:00:00+00:00",
        }), encoding="utf-8")
        self._set_argv(
            monkeypatch,
            "--projects", f"unity:{main_env['unity']}",
            "--resume", run_id,
            "--no-interactive",
            "--mock",
            "--workspace", str(main_env["workspace"]),
        )
        from pipeline.cross_project.cli import main
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
        # ``run_cross_pipeline`` must NOT be invoked — the guard
        # short-circuits before spawning the cross pipeline.
        main_env["run_cross_pipeline"].assert_not_called()
        # Hint surfaces the actual blocking status so the operator
        # knows why the resume bounced.
        stderr = capsys.readouterr().err
        assert "cannot be resumed from checkpoint" in stderr
        assert "halted" in stderr

    def test_resume_against_terminal_done_short_circuits(
        self, main_env, monkeypatch, capsys: pytest.CaptureFixture,
    ) -> None:
        """Parity regression: the existing ``terminal_success`` half of
        the guard must keep working alongside the new
        ``phase_handoff_halt`` arm. Resuming a ``done`` parent without
        ``--task`` exits 0 with the hint and skips
        ``run_cross_pipeline``."""
        run_id = "20260524_002_done"
        runs_dir = main_env["workspace"] / "runspace" / "runs"
        runs_dir.mkdir(parents=True)
        parent_dir = runs_dir / run_id
        parent_dir.mkdir()
        (parent_dir / "meta.json").write_text(json.dumps({
            "task": "T",
            "projects": {"unity": str(main_env["unity"])},
            "status": "done",
        }), encoding="utf-8")
        self._set_argv(
            monkeypatch,
            "--projects", f"unity:{main_env['unity']}",
            "--resume", run_id,
            "--no-interactive",
            "--mock",
            "--workspace", str(main_env["workspace"]),
        )
        from pipeline.cross_project.cli import main
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
        main_env["run_cross_pipeline"].assert_not_called()
        stderr = capsys.readouterr().err
        assert "cannot be resumed from checkpoint" in stderr
        assert "done" in stderr

    def _write_resumable_cross_meta(
        self, main_env, run_id: str, **meta_overrides
    ) -> None:
        """Helper: write a minimal resumable cross meta.json for the
        profile-inheritance tests below."""
        import json
        runs_dir = main_env["workspace"] / "runspace" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        parent_dir = runs_dir / run_id
        parent_dir.mkdir()
        base_meta = {
            "task": "T",
            "projects": {"unity": str(main_env["unity"])},
            "status": "interrupted",
        }
        base_meta.update(meta_overrides)
        (parent_dir / "meta.json").write_text(
            json.dumps(base_meta), encoding="utf-8",
        )

    def test_resume_inherits_meta_profile_when_no_explicit(
        self, main_env, monkeypatch,
    ) -> None:
        """``orcho cross --resume RUN_ID`` without ``--profile`` must
        inherit ``meta.profile`` from the original cross run — not
        fall back to the cross fresh-run default. Mirror of the
        single-project ``test_resume_inherits_meta_profile`` and the
        MCP ``test_resume_without_profile_inherits_from_meta`` L4."""
        run_id = "20260522_cross_inherit"
        self._write_resumable_cross_meta(main_env, run_id, profile="lite")
        self._set_argv(
            monkeypatch,
            "--projects", f"unity:{main_env['unity']}",
            "--resume", run_id,
            "--no-interactive",
            "--mock",
            "--workspace", str(main_env["workspace"]),
        )
        from pipeline.cross_project.cli import main
        main()
        kwargs = main_env["run_cross_pipeline"].call_args.kwargs
        assert kwargs["profile_name"] == "lite", (
            f"expected meta.profile=lite to be inherited, "
            f"got {kwargs['profile_name']!r}"
        )

    def test_resume_explicit_profile_overrides_meta(
        self, main_env, monkeypatch,
    ) -> None:
        """Explicit ``--profile <name>`` on resume is a deliberate
        switch and must win over ``meta.profile``."""
        run_id = "20260522_cross_override"
        self._write_resumable_cross_meta(main_env, run_id, profile="lite")
        self._set_argv(
            monkeypatch,
            "--projects", f"unity:{main_env['unity']}",
            "--resume", run_id,
            "--profile", "advanced",
            "--no-interactive",
            "--mock",
            "--workspace", str(main_env["workspace"]),
        )
        from pipeline.cross_project.cli import main
        main()
        kwargs = main_env["run_cross_pipeline"].call_args.kwargs
        assert kwargs["profile_name"] == "advanced", (
            f"explicit --profile advanced must win over meta.profile=lite; "
            f"got {kwargs['profile_name']!r}"
        )

    def test_resume_legacy_meta_without_profile_falls_back_to_default(
        self, main_env, monkeypatch,
    ) -> None:
        """Cross runs whose meta predates ``profile`` capture fall back
        to ``CROSS_DEFAULT_PROFILE`` (``"feature"``) — never to
        ``"task"`` (which is invalid for cross anyway: no cross policy)."""
        run_id = "20260522_cross_legacy"
        # meta with no profile field at all
        self._write_resumable_cross_meta(main_env, run_id)
        self._set_argv(
            monkeypatch,
            "--projects", f"unity:{main_env['unity']}",
            "--resume", run_id,
            "--no-interactive",
            "--mock",
            "--workspace", str(main_env["workspace"]),
        )
        from pipeline.cross_project.cli import main
        main()
        kwargs = main_env["run_cross_pipeline"].call_args.kwargs
        assert kwargs["profile_name"] == "feature"

    def test_fresh_run_uses_cross_default_profile(
        self, main_env, monkeypatch,
    ) -> None:
        """Sanity: fresh cross run (no ``--resume``, no ``--profile``)
        falls through to ``CROSS_DEFAULT_PROFILE``. Pins that the new
        ``default=None`` argparse setting does not accidentally pass
        ``None`` through to ``run_cross_pipeline``."""
        self._set_argv(
            monkeypatch,
            "--task", "fresh cross",
            "--projects", f"unity:{main_env['unity']}",
            "--no-interactive",
            "--mock",
            "--workspace", str(main_env["workspace"]),
        )
        from pipeline.cross_project.cli import main
        main()
        kwargs = main_env["run_cross_pipeline"].call_args.kwargs
        assert kwargs["profile_name"] == "feature"

    def test_resume_honours_explicit_workspace_without_env(
        self, main_env, monkeypatch
    ) -> None:
        # Regression: cross --resume used to load meta.json via
        # ``config.get_runs_dir()`` BEFORE applying ``--workspace`` to
        # the environment, so explicit ``--workspace`` was ignored when
        # ``$ORCHO_WORKSPACE`` / ``$ORCHO_RUNSPACE`` were unset and the
        # CLI bailed out with "No orcho workspace resolved". Strip both
        # env vars to make the regression reproducible.
        import json
        monkeypatch.delenv("ORCHO_WORKSPACE", raising=False)
        monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)

        run_id = "20260518_120000"
        runs_dir = main_env["workspace"] / "runspace" / "runs"
        runs_dir.mkdir(parents=True)
        parent_dir = runs_dir / run_id
        parent_dir.mkdir()
        (parent_dir / "meta.json").write_text(json.dumps({
            "task": "T",
            "projects": {"unity": str(main_env["unity"])},
            "status": "interrupted",
        }), encoding="utf-8")

        self._set_argv(
            monkeypatch,
            "--projects", f"unity:{main_env['unity']}",
            "--resume", run_id,
            "--workspace", str(main_env["workspace"]),
            "--no-interactive",
            "--mock",
        )
        from pipeline.cross_project.cli import main
        main()
        kwargs = main_env["run_cross_pipeline"].call_args.kwargs
        assert kwargs["resume_from"] == run_id

    def test_resume_honours_explicit_workspace_with_wrong_worktree_env(
        self, main_env, monkeypatch, tmp_path
    ) -> None:
        # Regression: ``config.get_runs_dir()`` reads ``ORCHO_RUNSPACE``
        # before ``ORCHO_WORKSPACE``. If an ambient ``ORCHO_RUNSPACE``
        # points at a different (existing) worktree, ``--resume RUN_ID
        # --workspace /correct/ws`` used to read meta from the wrong
        # runs dir and surface a misleading "no persisted task" error.
        # Setting both env vars together (in the early --workspace
        # apply) is what makes the documented "CLI flag wins" contract
        # actually hold.
        import json
        wrong_workspace = tmp_path / "wrong-ws"
        wrong_workspace.mkdir()
        (wrong_workspace / "runspace" / "runs").mkdir(parents=True)
        monkeypatch.setenv(
            "ORCHO_RUNSPACE", str(wrong_workspace / "runspace"),
        )
        # Strip ORCHO_WORKSPACE so only the wrong WORKTREE could win.
        monkeypatch.delenv("ORCHO_WORKSPACE", raising=False)

        run_id = "20260518_140000"
        runs_dir = main_env["workspace"] / "runspace" / "runs"
        runs_dir.mkdir(parents=True)
        parent_dir = runs_dir / run_id
        parent_dir.mkdir()
        (parent_dir / "meta.json").write_text(json.dumps({
            "task": "T",
            "projects": {"unity": str(main_env["unity"])},
            "status": "interrupted",
        }), encoding="utf-8")

        self._set_argv(
            monkeypatch,
            "--projects", f"unity:{main_env['unity']}",
            "--resume", run_id,
            "--workspace", str(main_env["workspace"]),
            "--no-interactive",
            "--mock",
        )
        from pipeline.cross_project.cli import main
        main()
        kwargs = main_env["run_cross_pipeline"].call_args.kwargs
        assert kwargs["resume_from"] == run_id

    def test_resume_latest_honours_explicit_workspace_without_env(
        self, main_env, monkeypatch
    ) -> None:
        # Companion to the previous regression: the ``latest`` sentinel
        # resolver also runs before the env apply was moved, so bare
        # ``--resume`` with an explicit workspace must succeed.
        import json
        monkeypatch.delenv("ORCHO_WORKSPACE", raising=False)
        monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)

        run_id = "20260518_130000"
        runs_dir = main_env["workspace"] / "runspace" / "runs"
        runs_dir.mkdir(parents=True)
        parent_dir = runs_dir / run_id
        parent_dir.mkdir()
        (parent_dir / "meta.json").write_text(json.dumps({
            "task": "T",
            "projects": {"unity": str(main_env["unity"])},
            "status": "interrupted",
        }), encoding="utf-8")

        self._set_argv(
            monkeypatch,
            "--projects", f"unity:{main_env['unity']}",
            "--resume",
            "--workspace", str(main_env["workspace"]),
            "--no-interactive",
            "--mock",
        )
        from pipeline.cross_project.cli import main
        main()
        kwargs = main_env["run_cross_pipeline"].call_args.kwargs
        assert kwargs["resume_from"] == run_id

    def test_resume_no_task_completed_parent_exits_with_hint(
        self, main_env, monkeypatch, capsys
    ) -> None:
        # Bare ``--resume RUN_ID`` against a done parent with no task
        # has nothing to do; CLI should exit 0 with a follow-up hint
        # rather than silently rerun-into-completed-run.
        import json
        run_id = "20260512_003"
        runs_dir = main_env["workspace"] / "runspace" / "runs"
        runs_dir.mkdir(parents=True)
        parent_dir = runs_dir / run_id
        parent_dir.mkdir()
        (parent_dir / "meta.json").write_text(json.dumps({
            "task": "T",
            "projects": {"unity": str(main_env["unity"])},
            "status": "done",
        }), encoding="utf-8")
        self._set_argv(
            monkeypatch,
            "--projects", f"unity:{main_env['unity']}",
            "--resume", run_id,
            "--no-interactive",
            "--mock",
            "--workspace", str(main_env["workspace"]),
        )
        from pipeline.cross_project.cli import main
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
        err = capsys.readouterr().err
        assert run_id in err
        assert "follow-up" in err
        assert not main_env["run_cross_pipeline"].called

    def test_malformed_projects_arg_exits_1(
        self, main_env, monkeypatch, capsys: pytest.CaptureFixture
    ) -> None:
        # "unityp" has no colon and no workspace alias → parse_projects
        # raises ValueError → main() catches and exits 1.
        self._set_argv(
            monkeypatch,
            "--task", "T",
            "--projects", "unityp",
            "--mock",
            "--workspace", str(main_env["workspace"]),
        )
        from pipeline.cross_project.cli import main
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "Unknown project alias" in err
        assert "alias:/path/to/project" in err

    def test_missing_project_path_exits_1(
        self, main_env, monkeypatch, capsys: pytest.CaptureFixture
    ) -> None:
        # parse_projects raises FileNotFoundError when the path doesn't
        # exist → main() catches the same except clause as ValueError.
        self._set_argv(
            monkeypatch,
            "--task", "T",
            "--projects", "unity:/nonexistent/path",
            "--mock",
            "--workspace", str(main_env["workspace"]),
        )
        from pipeline.cross_project.cli import main
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "Project not found" in err

    def test_three_project_mode_parses_to_3_entry_dict(
        self, main_env, monkeypatch, tmp_path: Path
    ) -> None:
        stats = tmp_path / "stats"
        stats.mkdir()
        self._set_argv(
            monkeypatch,
            "--task", "T",
            "--projects",
            f"unity:{main_env['unity']}",
            f"api:{main_env['api']}",
            f"stats:{stats}",
            "--mock",
            "--workspace", str(main_env["workspace"]),
        )
        from pipeline.cross_project.cli import main
        main()

        projects = main_env["run_cross_pipeline"].call_args.kwargs["projects"]
        assert set(projects.keys()) == {"unity", "api", "stats"}

    def test_keyboard_interrupt_exits_130_with_warning_emoji(
        self, main_env, monkeypatch, capsys: pytest.CaptureFixture
    ) -> None:
        main_env["run_cross_pipeline"].side_effect = KeyboardInterrupt()
        self._set_argv(
            monkeypatch,
            "--task", "T",
            "--projects", f"unity:{main_env['unity']}",
            "--mock",
            "--workspace", str(main_env["workspace"]),
        )
        from pipeline.cross_project.cli import main
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 130
        captured = capsys.readouterr()
        # U+26A0 warning sign — differs from the single-project
        # orchestrator's plain "\nInterrupted".
        assert "\n⚠ Interrupted" in captured.out

    def test_run_id_collision_exits_2(
        self, main_env, monkeypatch, capsys: pytest.CaptureFixture
    ) -> None:
        # In cross main, _assert_fresh_run_dir_available is called
        # directly (not inside run_cross_pipeline). Monkeypatch it to
        # raise so the except clause at line 896-898 runs.
        from pipeline.cross_project import orchestrator as cross
        from pipeline.project_orchestrator import RunIdCollisionError

        def _raise(*a, **k):
            raise RunIdCollisionError("cross run_id already exists")

        monkeypatch.setattr(cross, "_assert_fresh_run_dir_available", _raise)
        self._set_argv(
            monkeypatch,
            "--task", "T",
            "--projects", f"unity:{main_env['unity']}",
            "--mock",
            "--workspace", str(main_env["workspace"]),
        )
        from pipeline.cross_project.cli import main
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 2
        assert "cross run_id already exists" in capsys.readouterr().err

    def test_help_exits_0_and_prints_epilog(
        self, monkeypatch, capsys: pytest.CaptureFixture
    ) -> None:
        self._set_argv(monkeypatch, "--help")
        from pipeline.cross_project.cli import main
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
        # Cross epilog mentions the Unity+API+stats example.
        assert "--projects" in capsys.readouterr().out

    def test_auto_workspace_local_config_drives_phase_config(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from pipeline.cross_project import orchestrator as cross

        group = tmp_path / "group"
        workspace = group / "workspace-orchestrator"
        api = group / "api"
        web = group / "web"
        for d in (workspace, api, web):
            d.mkdir(parents=True)
        local_cfg = workspace / ".orcho" / "config.local.json"
        local_cfg.parent.mkdir()
        local_cfg.write_text(
            json.dumps({
                "phases": {
                    "validate_plan": {
                        "runtime": "codex",
                        "model": "gpt-workspace-validate",
                        "effort": "high",
                    },
                    "review_changes": {
                        "runtime": "codex",
                        "model": "gpt-workspace-review",
                        "effort": "high",
                    },
                    "final_acceptance": {
                        "runtime": "codex",
                        "model": "gpt-workspace-final",
                        "effort": "high",
                    },
                },
                "pipeline": {
                    "change_handoff": "commit",
                },
            }),
            encoding="utf-8",
        )
        monkeypatch.delenv("ORCHO_WORKSPACE", raising=False)
        monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)
        monkeypatch.delenv("ORCHO_DISABLE_LOCAL_CONFIG", raising=False)
        run_cross_mock = MagicMock(return_value=None)
        monkeypatch.setattr(cross, "run_cross_pipeline", run_cross_mock)
        self._set_argv(
            monkeypatch,
            "--task", "T",
            "--projects", f"api:{api}", f"web:{web}",
            "--mock",
        )

        from pipeline.cross_project.cli import main
        main()

        kwargs = run_cross_mock.call_args.kwargs
        phase_config = kwargs["phase_config"]
        assert phase_config.validate_plan_agent.model == "gpt-workspace-validate"
        assert phase_config.review_changes_agent.model == "gpt-workspace-review"
        assert phase_config.final_acceptance_agent.model == "gpt-workspace-final"


class TestCrossCliModelOverrideBridge:
    """ADR 0022 bridge from CLI surface to phase_config kwargs.

    Companion to ``tests/unit/pipeline/cross_project/test_cross_orchestrator.py``:
    that file pins the helper signatures; this one drives them through
    the actual CLI so the public --model-build / --model-fix /
    --model-review flags don't silently break the bridge again.
    """

    @pytest.fixture
    def main_env(self, tmp_path: Path, monkeypatch):
        from pipeline.cross_project import orchestrator as cross

        workspace = tmp_path / "ws"
        workspace.mkdir()
        unity = tmp_path / "unity"
        unity.mkdir()
        monkeypatch.setenv("ORCHO_WORKSPACE", str(workspace))

        run_cross_mock = MagicMock(return_value=None)
        monkeypatch.setattr(cross, "run_cross_pipeline", run_cross_mock)
        return {
            "workspace": workspace,
            "unity": unity,
            "run_cross_pipeline": run_cross_mock,
        }

    def test_model_build_flag_populates_implement_agent(
        self, main_env, monkeypatch
    ) -> None:
        # Without --mock, --model-* flags trigger
        # build_phase_config_from_overrides. Pre-bridge, this would
        # have raised TypeError on the unknown ``build=`` kwarg.
        monkeypatch.setattr(
            sys, "argv",
            [
                "orcho-cross",
                "--task", "T",
                "--projects", f"unity:{main_env['unity']}",
                "--model-build", "claude-custom-build",
                "--workspace", str(main_env["workspace"]),
            ],
        )
        from pipeline.cross_project.cli import main
        main()

        phase_config = main_env["run_cross_pipeline"].call_args.kwargs["phase_config"]
        assert phase_config is not None
        # ADR 0022 slot name; pre-rename was .build_agent.
        assert phase_config.implement_agent.model == "claude-custom-build"

    def test_model_review_flag_populates_review_changes_agent(
        self, main_env, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            sys, "argv",
            [
                "orcho-cross",
                "--task", "T",
                "--projects", f"unity:{main_env['unity']}",
                "--model-review", "gpt-custom-review",
                "--workspace", str(main_env["workspace"]),
            ],
        )
        from pipeline.cross_project.cli import main
        main()

        phase_config = main_env["run_cross_pipeline"].call_args.kwargs["phase_config"]
        assert phase_config is not None
        # The --model-review override binds all three reviewer slots
        # per build_phase_config_from_overrides; review_changes_agent
        # is the one run_cross_pipeline actually reads for codex init.
        assert phase_config.review_changes_agent.model == "gpt-custom-review"
