"""First dedicated unit suite for ``sdk/runner.py``.

``sdk/runner.py`` is the public SDK launch boundary: ``build_orch_argv_from_args``
(Namespace → argv) plus the two thin wrappers ``run_pipeline_from_args`` /
``run_cross_from_args`` (Namespace → ``sys.argv`` → orchestrator ``main()`` →
exit code). The wrappers must never raise ``SystemExit``; they map it (and the
typed SDK/agent errors) onto a process exit code.

These tests close the branches NOT already exercised indirectly by
``tests/unit/cli/test_decision_flags.py`` / ``tests/unit/agents/test_argv_builder.py``:

* both wrappers' success ``return 0`` and ``SystemExit`` code mapping
  (``int(e.code or 0)``);
* ``OrchoError`` with a non-1 ``exit_code`` flowing through to the return value;
* the full ``run_cross_from_args`` argv-assembly cascade (task-file, workspace,
  resume, decision loop, no-interactive, max-rounds, mock-validate, output-dir,
  dry-run, mock, output-mode variants, ``--mode plan``, session-split, plan-file,
  and the model/runtime flag pairs) plus its ``AgentCallError`` / ``OrchoError``
  boundaries.

Every test asserts the returned code / assembled argv / stderr text and names
the branch it closes. ``sys.argv`` is saved and restored around every test so
the wrappers' global mutation never leaks into a sibling test.
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator

import pytest

from core.io.retry import AgentCallError
from sdk import runner
from sdk.errors import OrchoError


@pytest.fixture(autouse=True)
def _isolate_sys_argv() -> Iterator[None]:
    """Save/restore ``sys.argv`` — both wrappers overwrite it in place."""
    saved = sys.argv[:]
    try:
        yield
    finally:
        sys.argv = saved


class _ExitCodeError(OrchoError):
    """An SDK error whose ``exit_code`` is deliberately not the default 1."""

    exit_code = 5


def _run_ns(**overrides: object) -> argparse.Namespace:
    """Minimal namespace for the run wrappers.

    ``build_orch_argv_from_args`` reads ``args.project`` directly and everything
    else via ``getattr(..., default)``, so only ``project`` /
    ``decision`` / ``decision_feedback`` must be present.
    """
    base: dict[str, object] = {
        "project": "/p",
        "task": "T",
        "decision": None,
        "decision_feedback": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# ── build_orch_argv_from_args: falsy normalization (unique vs decision_flags) ─


def test_build_argv_falsy_mock_validate_is_not_emitted() -> None:
    """``mock_validate_plan_reject=0`` normalizes away — no stray flag (line 45).

    The ``... or 0`` guard means a falsy reject count must not surface as
    ``--mock-validate-plan-reject 0`` in the orchestrator argv.
    """
    argv = runner.build_orch_argv_from_args(_run_ns(mock_validate_plan_reject=0))
    assert "--mock-validate-plan-reject" not in argv
    # And cross_mode is pinned to None, so the mono adapter never carries --mode.
    assert "--mode" not in argv


def test_build_argv_truthy_mock_validate_is_emitted() -> None:
    """A positive reject count is threaded through as the flag + value."""
    argv = runner.build_orch_argv_from_args(_run_ns(mock_validate_plan_reject=3))
    assert "--mock-validate-plan-reject" in argv
    assert argv[argv.index("--mock-validate-plan-reject") + 1] == "3"


# ── run_pipeline_from_args: success / SystemExit / OrchoError ────────────────


def test_run_from_args_success_returns_zero(monkeypatch) -> None:
    """A clean ``main()`` return maps to exit code 0 (line 114)."""
    called: dict[str, list[str]] = {}

    def fake_main() -> None:
        called["argv"] = sys.argv[:]

    monkeypatch.setattr("pipeline.project_orchestrator.main", fake_main)
    rc = runner.run_pipeline_from_args(_run_ns())
    assert rc == 0
    # The wrapper installed an orchestrator argv before calling main.
    assert called["argv"][0] == "orchestrator"


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        pytest.param(0, 0, id="exit-0"),
        pytest.param(3, 3, id="exit-3"),
        pytest.param(None, 0, id="exit-none-coerces-to-0"),
    ],
)
def test_run_from_args_maps_systemexit_code(
    monkeypatch, code, expected
) -> None:
    """``SystemExit`` is caught and mapped via ``int(e.code or 0)`` (line 116)."""
    def fake_main() -> None:
        raise SystemExit(code)

    monkeypatch.setattr("pipeline.project_orchestrator.main", fake_main)
    assert runner.run_pipeline_from_args(_run_ns()) == expected


def test_run_from_args_orcho_error_returns_exit_code(monkeypatch, capsys) -> None:
    """A non-1 ``OrchoError.exit_code`` is returned and the message hits stderr (128-130)."""
    def fake_main() -> None:
        raise _ExitCodeError("sdk boundary failure")

    monkeypatch.setattr("pipeline.project_orchestrator.main", fake_main)
    rc = runner.run_pipeline_from_args(_run_ns())
    assert rc == 5
    assert "sdk boundary failure" in capsys.readouterr().err


# ── run_cross_from_args: full argv cascade ───────────────────────────────────


def _capture_cross_argv(monkeypatch) -> dict[str, list[str]]:
    """Patch the cross CLI ``main`` to snapshot the assembled ``sys.argv``."""
    captured: dict[str, list[str]] = {}

    def fake_main() -> None:
        captured["argv"] = sys.argv[:]

    monkeypatch.setattr("pipeline.cross_project.cli.main", fake_main)
    return captured


def test_cross_from_args_assembles_full_argv_cascade(monkeypatch) -> None:
    """Every populated cross field is threaded into argv (150-203).

    Drives the task-file / workspace / resume / decision-loop / decision-feedback
    / no-interactive / max-rounds / mock-validate / output-dir / dry-run / mock /
    non-summary output / ``--mode plan`` / session-split / plan-file / model+runtime
    pair branches in one pass, and asserts the success ``return 0`` (line 210).
    """
    captured = _capture_cross_argv(monkeypatch)
    ns = argparse.Namespace(
        task="T",
        task_file="/tmp/task.md",
        projects=["api:/a", "web:/w"],
        workspace="/ws",
        resume="20260101_000000",
        decision=["contract_check=run"],
        decision_feedback="looks good",
        no_interactive=True,
        max_rounds=5,
        mock_validate_plan_reject=2,
        output_dir="/out",
        dry_run=True,
        mock=True,
        output="debug",  # non-summary → explicit --output debug (175)
        verbose=False,
        stream_output=False,
        mode="plan",  # != full → --mode plan (181)
        profile="lite",
        session_split=["plan=fresh", "implement=reuse"],
        plan_file="/plan.md",
        model_plan="m1",
        model_build="m2",
        model_fix="m3",
        model_review="m4",
        runtime_plan="r1",
        runtime_build="r2",
        runtime_fix="r3",
        runtime_review="r4",
    )
    rc = runner.run_cross_from_args(ns)
    assert rc == 0

    argv = captured["argv"]
    assert argv[0] == "cross_orchestrator"

    def _val(flag: str) -> str:
        return argv[argv.index(flag) + 1]

    assert _val("--task") == "T"
    assert _val("--task-file") == "/tmp/task.md"
    # --projects is variadic: both aliases follow the flag.
    pidx = argv.index("--projects")
    assert argv[pidx + 1 : pidx + 3] == ["api:/a", "web:/w"]
    assert _val("--workspace") == "/ws"
    assert _val("--resume") == "20260101_000000"
    assert _val("--decision") == "contract_check=run"
    assert _val("--decision-feedback") == "looks good"
    assert "--no-interactive" in argv
    assert _val("--max-rounds") == "5"
    assert _val("--mock-validate-plan-reject") == "2"
    assert _val("--output-dir") == "/out"
    assert "--dry-run" in argv
    assert "--mock" in argv
    assert _val("--output") == "debug"
    assert _val("--mode") == "plan"
    assert _val("--profile") == "lite"
    # --session-split repeats once per entry.
    assert argv.count("--session-split") == 2
    assert _val("--plan-file") == "/plan.md"
    assert _val("--model-plan") == "m1"
    assert _val("--model-build") == "m2"
    assert _val("--model-fix") == "m3"
    assert _val("--model-review") == "m4"
    assert _val("--runtime-plan") == "r1"
    assert _val("--runtime-build") == "r2"
    assert _val("--runtime-fix") == "r3"
    assert _val("--runtime-review") == "r4"


def test_cross_from_args_verbose_maps_to_output_debug(monkeypatch) -> None:
    """``verbose`` with a summary output selects ``--output debug`` (177)."""
    captured = _capture_cross_argv(monkeypatch)
    ns = argparse.Namespace(
        decision=None, decision_feedback=None, no_interactive=False,
        output="summary", verbose=True, stream_output=False,
    )
    assert runner.run_cross_from_args(ns) == 0
    argv = captured["argv"]
    assert argv[argv.index("--output") + 1] == "debug"


def test_cross_from_args_stream_maps_to_output_live(monkeypatch) -> None:
    """``stream_output`` with a summary output selects ``--output live`` (179)."""
    captured = _capture_cross_argv(monkeypatch)
    ns = argparse.Namespace(
        decision=None, decision_feedback=None, no_interactive=False,
        output="summary", verbose=False, stream_output=True,
    )
    assert runner.run_cross_from_args(ns) == 0
    argv = captured["argv"]
    assert argv[argv.index("--output") + 1] == "live"


# ── run_cross_from_args: SystemExit / AgentCallError / OrchoError ────────────


def _bare_cross_ns() -> argparse.Namespace:
    """A namespace that builds an empty argv (all optional fields absent)."""
    return argparse.Namespace(decision=None, decision_feedback=None)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        pytest.param(0, 0, id="exit-0"),
        pytest.param(4, 4, id="exit-4"),
        pytest.param(None, 0, id="exit-none-coerces-to-0"),
    ],
)
def test_cross_from_args_maps_systemexit_code(
    monkeypatch, code, expected
) -> None:
    """Cross wrapper maps ``SystemExit`` via ``int(e.code or 0)`` (211-212)."""
    def fake_main() -> None:
        raise SystemExit(code)

    monkeypatch.setattr("pipeline.cross_project.cli.main", fake_main)
    assert runner.run_cross_from_args(_bare_cross_ns()) == expected


def test_cross_from_args_agent_call_error_returns_one(monkeypatch, capsys) -> None:
    """An ``AgentCallError`` is a controlled halt → exit 1, no traceback (213-217)."""
    def fake_main() -> None:
        raise AgentCallError("API unreachable (runtime=claude)")

    monkeypatch.setattr("pipeline.cross_project.cli.main", fake_main)
    rc = runner.run_cross_from_args(_bare_cross_ns())
    assert rc == 1
    err = capsys.readouterr().err
    assert "API unreachable" in err
    assert "Traceback" not in err


def test_cross_from_args_orcho_error_returns_exit_code(monkeypatch, capsys) -> None:
    """A non-1 ``OrchoError.exit_code`` flows through the cross wrapper (218-220)."""
    def fake_main() -> None:
        raise _ExitCodeError("cross sdk boundary failure")

    monkeypatch.setattr("pipeline.cross_project.cli.main", fake_main)
    rc = runner.run_cross_from_args(_bare_cross_ns())
    assert rc == 5
    assert "cross sdk boundary failure" in capsys.readouterr().err
