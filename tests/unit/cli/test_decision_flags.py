"""CLI surface for ``--decision`` / ``--decision-feedback`` / ``--no-interactive``.

These flags live on the common run-args helper, so both ``orcho run``
and ``orcho cross`` accept them. The supported target set differs per
subcommand — ``contract_check`` is cross-only — and bogus targets on
``orcho run`` must fail clearly instead of being silently dropped.
"""
from __future__ import annotations

import argparse
import sys

import pytest

from cli.orcho import build_parser


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = build_parser()
    return parser.parse_args(argv)


class TestModeFlagNoCrossCollision:
    """``orcho run --mode {fast,pro,governed}`` (verification strictness, T6)
    must live on the mono parser without colliding with the cross
    ``--mode {full,plan}`` slice selector. The two are distinct subparsers
    with disjoint choice sets — a value valid for one is rejected by the
    other, proving no shared/leaked flag.
    """

    @pytest.mark.parametrize("mode", ["fast", "pro", "governed"])
    def test_run_accepts_work_mode(self, mode: str) -> None:
        args = _parse(["run", "--task", "T", "--project", "/p", "--mode", mode])
        assert args.mode == mode

    def test_run_mode_defaults_to_none(self) -> None:
        # No --mode → None, so the projection falls through to the profile's
        # default_mode rather than an implicit override.
        args = _parse(["run", "--task", "T", "--project", "/p"])
        assert args.mode is None

    def test_run_rejects_cross_mode_values(self) -> None:
        # 'full' / 'plan' are the CROSS slice selectors — invalid on mono run.
        for bogus in ("full", "plan"):
            with pytest.raises(SystemExit):
                _parse(["run", "--task", "T", "--project", "/p", "--mode", bogus])

    def test_cross_keeps_full_plan_choices(self) -> None:
        # No regression: cross still accepts {full, plan}.
        for mode in ("full", "plan"):
            args = _parse([
                "cross", "--task", "T", "--projects", "api:/a", "--mode", mode,
            ])
            assert args.mode == mode

    def test_cross_rejects_work_mode_values(self) -> None:
        # The mono work-mode vocabulary is NOT accepted by cross — the two
        # flags do not share a choice set.
        for bogus in ("fast", "pro", "governed"):
            with pytest.raises(SystemExit):
                _parse([
                    "cross", "--task", "T", "--projects", "api:/a",
                    "--mode", bogus,
                ])

    def test_mono_mode_not_forwarded_as_cross_mode_in_argv(self) -> None:
        # The mono --mode is threaded to the run via the ORCHO_WORK_MODE env
        # (run_pipeline's signature is locked), never through the argv child
        # dispatch builder as a cross ``--mode``. build_orch_argv only emits
        # ``--mode`` for an explicit cross_mode; the default mono build does
        # not carry one.
        from pipeline.argv import build_orch_argv

        argv = build_orch_argv(project="/p", task="t")
        assert "--mode" not in argv

    def test_sdk_adapter_does_not_leak_work_mode_as_cross_mode(self) -> None:
        # A mono run namespace carrying a work-mode (--mode pro) must NOT be
        # re-emitted into the orchestrator argv as the cross ``--mode``. The
        # work-mode rides the ORCHO_WORK_MODE env channel instead.
        from sdk.runner import build_orch_argv_from_args

        ns = argparse.Namespace(
            project="/p", task="t", task_file=None, workspace=None,
            resume=None, run_id=None, max_rounds=None,
            mock_validate_plan_reject=0, model=None, output_dir=None,
            dry_run=False, mock=False, output=None, verbose=False,
            stream_output=False, profile=None, mode="pro", session_mode=None,
            model_plan=None, model_implement=None, model_repair_changes=None,
            model_review_changes=None, runtime_plan=None, runtime_implement=None,
            runtime_repair_changes=None, runtime_review_changes=None,
            attach=None, attach_text=None, attach_image=None, attach_binary=None,
        )
        argv = build_orch_argv_from_args(ns)
        assert "--mode" not in argv
        assert "pro" not in argv


class TestDecisionFlagsParseShape:
    def test_run_accepts_decision_surface(self) -> None:
        args = _parse([
            "run",
            "--task", "T",
            "--project", "/p",
            "--decision", "contract_check=run",
            "--decision-feedback", "ignored on run",
            "--no-interactive",
        ])
        assert args.decision == ["contract_check=run"]
        assert args.decision_feedback == "ignored on run"
        assert args.no_interactive is True

    def test_cross_accepts_decision_surface(self) -> None:
        args = _parse([
            "cross",
            "--task", "T",
            "--projects", "api:/a", "web:/w",
            "--decision", "contract_check=run",
            "--no-interactive",
        ])
        assert args.decision == ["contract_check=run"]
        assert args.no_interactive is True

    def test_decision_is_repeatable(self) -> None:
        args = _parse([
            "cross",
            "--task", "T",
            "--projects", "api:/a",
            "--decision", "contract_check=run",
            "--decision", "contract_check=skip",
        ])
        assert args.decision == [
            "contract_check=run",
            "contract_check=skip",
        ]


class TestDecisionValidationViaSDK:
    """The SDK boundary (run_pipeline_from_args / run_cross_from_args)
    catches OperatorDecisionError and returns exit code 2 instead of
    letting it bubble. Use the public SDK entry points to exercise that
    path without standing up the full orchestrator."""

    def test_run_rejects_cross_only_target(self, capsys) -> None:
        from sdk import run_pipeline_from_args

        ns = argparse.Namespace(
            decision=["contract_check=run"],
            decision_feedback=None,
        )
        rc = run_pipeline_from_args(ns)
        assert rc == 2
        err = capsys.readouterr().err
        assert "unknown target 'contract_check'" in err
        assert "orcho run" in err

    def test_run_auth_error_prints_without_traceback(self, monkeypatch, capsys) -> None:
        from core.io.retry import AgentAuthenticationError
        from sdk import run_pipeline_from_args

        def fake_main() -> None:
            raise AgentAuthenticationError(
                "Runtime credentials were rejected for runtime='claude'; "
                "refresh the CLI login and retry."
            )

        # ADR 0042 Phase J: sdk.runner is non-CLI code, so it goes
        # through the stable 4-name shim instead of importing the CLI
        # leaf directly. Patch the shim name it resolves lazily.
        monkeypatch.setattr("pipeline.project_orchestrator.main", fake_main)

        ns = argparse.Namespace(
            project="/p",
            task="T",
            task_file=None,
            workspace=None,
            resume=None,
            run_id=None,
            max_rounds=None,
            mock_validate_plan_reject=0,
            model=None,
            output_dir=None,
            dry_run=False,
            mock=False,
            output="summary",
            verbose=False,
            stream_output=False,
            profile="lite",
            mode=None,
            session_mode=None,
            decision=None,
            decision_feedback=None,
            model_plan=None,
            model_implement=None,
            model_repair_changes=None,
            model_review_changes=None,
            runtime_plan=None,
            runtime_implement=None,
            runtime_repair_changes=None,
            runtime_review_changes=None,
            attach=None,
            attach_text=None,
            attach_image=None,
            attach_binary=None,
        )
        rc = run_pipeline_from_args(ns)

        captured = capsys.readouterr()
        assert rc == 1
        assert "Runtime credentials were rejected" in captured.err
        assert "Traceback" not in captured.err

    def test_run_api_error_halts_without_traceback(self, monkeypatch, capsys) -> None:
        # Regression: an API-client failure (connection refused, API
        # unreachable, or any non-zero CLI exit) already halts the run and
        # writes the structured FAILED record, but the typed AgentCallError it
        # re-raises must be caught at the SDK boundary and turned into a clean
        # exit code — not a Python traceback. Before the fix only the auth
        # subclass was caught, so a bare AgentCallError crashed the CLI.
        from core.io.retry import ApiConnectionError
        from sdk import run_pipeline_from_args

        def fake_main() -> None:
            raise ApiConnectionError(
                "API unreachable (runtime=claude, exit=0): "
                "API Error: Unable to connect to API (ConnectionRefused)",
            )

        monkeypatch.setattr("pipeline.project_orchestrator.main", fake_main)

        ns = argparse.Namespace(
            project="/p", task="T", task_file=None, workspace=None,
            resume=None, run_id=None, max_rounds=None,
            mock_validate_plan_reject=0, model=None, output_dir=None,
            dry_run=False, mock=False, output="summary", verbose=False,
            stream_output=False, profile="lite", mode=None, session_mode=None,
            decision=None, decision_feedback=None,
            model_plan=None, model_implement=None, model_repair_changes=None,
            model_review_changes=None, runtime_plan=None, runtime_implement=None,
            runtime_repair_changes=None, runtime_review_changes=None,
            attach=None, attach_text=None, attach_image=None, attach_binary=None,
        )
        rc = run_pipeline_from_args(ns)

        captured = capsys.readouterr()
        assert rc == 1
        assert "API unreachable" in captured.err
        assert "Traceback" not in captured.err

    def test_cross_rejects_unknown_target(self, capsys) -> None:
        from sdk import run_cross_from_args

        ns = argparse.Namespace(
            decision=["unknown_target=run"],
            decision_feedback=None,
            no_interactive=False,
        )
        rc = run_cross_from_args(ns)
        assert rc == 2
        err = capsys.readouterr().err
        assert "unknown target" in err

    def test_cross_rejects_unsupported_decision(self, capsys) -> None:
        from sdk import run_cross_from_args

        ns = argparse.Namespace(
            decision=["contract_check=maybe"],
            decision_feedback=None,
            no_interactive=False,
        )
        rc = run_cross_from_args(ns)
        assert rc == 2
        err = capsys.readouterr().err
        assert "unsupported decision" in err

    def test_cross_rejects_feedback_without_decision(self, capsys) -> None:
        from sdk import run_cross_from_args

        ns = argparse.Namespace(
            decision=None,
            decision_feedback="orphan",
            no_interactive=False,
        )
        rc = run_cross_from_args(ns)
        assert rc == 2
        err = capsys.readouterr().err
        assert "without --decision" in err


class TestCrossSdkArgvBridge:
    def test_threads_profile_to_cross_orchestrator(self, monkeypatch) -> None:
        from sdk import run_cross_from_args

        captured: dict[str, list[str]] = {}

        def fake_main() -> None:
            captured["argv"] = sys.argv[:]

        monkeypatch.setattr("pipeline.cross_project.cli.main", fake_main)

        rc = run_cross_from_args(argparse.Namespace(
            task="T",
            task_file=None,
            projects=["api:/a"],
            workspace=None,
            resume=None,
            decision=None,
            decision_feedback=None,
            no_interactive=False,
            max_rounds=None,
            mock_validate_plan_reject=0,
            output_dir=None,
            dry_run=False,
            mock=False,
            output="summary",
            verbose=False,
            stream_output=False,
            mode="full",
            profile="lite",
            plan_file=None,
            model_plan=None,
            model_build=None,
            model_fix=None,
            model_review=None,
            runtime_plan=None,
            runtime_build=None,
            runtime_fix=None,
            runtime_review=None,
        ))

        assert rc == 0
        argv = captured["argv"]
        assert "--profile" in argv
        assert argv[argv.index("--profile") + 1] == "lite"


class TestBuildOrchArgvProjectOptional:
    """``orcho run --resume RUN_ID`` is now legal without ``--project``;
    the project orchestrator resolves it from ``meta.json``. The SDK
    argv builder must not emit ``--project None`` in that case — doing
    so short-circuits the resume hydration and breaks the CLI surface.
    """

    def test_omits_project_when_none(self) -> None:
        from pipeline.argv import build_orch_argv

        argv = build_orch_argv(
            project=None,
            task="from cli",
            resume="20260514_000000",
        )
        assert "--project" not in argv
        assert "None" not in argv
        # Resume is still threaded through.
        assert "--resume" in argv

    def test_emits_project_when_supplied(self) -> None:
        from pipeline.argv import build_orch_argv

        argv = build_orch_argv(
            project="/p", task="x",
        )
        assert "--project" in argv
        assert "/p" in argv

    def test_from_args_adapter_skips_project_none(self) -> None:
        from sdk.runner import build_orch_argv_from_args

        ns = argparse.Namespace(
            project=None,
            task=None,
            task_file=None,
            workspace=None,
            resume="20260514_000000",
            run_id=None,
            max_rounds=None,
            mock_validate_plan_reject=0,
            model=None,
            output_dir=None,
            dry_run=False,
            mock=False,
            output=None,
            verbose=False,
            stream_output=False,
            profile=None,
            mode=None,
            session_mode=None,
            model_plan=None,
            model_implement=None,
            model_repair_changes=None,
            model_review_changes=None,
            runtime_plan=None,
            runtime_implement=None,
            runtime_repair_changes=None,
            runtime_review_changes=None,
            attach=None,
            attach_text=None,
            attach_image=None,
            attach_binary=None,
        )
        argv = build_orch_argv_from_args(ns)
        assert "--project" not in argv
        assert "None" not in argv
        assert "--resume" in argv
