"""``orcho run`` exit codes reflect the persisted terminal status.

``0`` — done; ``4`` — paused on a phase handoff; ``3`` — halted (a parked
delivery gate, an operator halt, a rejected release). A halted run used to exit
``0``, and the DONE tail after a deferred park still read ``Release: approved``
with no delivery line, so scripts and operators took a parked run for a
shipped one.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    runspace = tmp_path / "runspace"
    (runspace / "runs").mkdir(parents=True)
    monkeypatch.setenv("ORCHO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("ORCHO_RUNSPACE", str(runspace))
    from core.infra import config as _config

    _config._reset_config()
    project = tmp_path / "proj"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname='p'\n", encoding="utf-8")
    yield project
    shutil.rmtree(runspace, ignore_errors=True)
    _config._reset_config()


def _run_main(project: Path, session: dict, monkeypatch: pytest.MonkeyPatch) -> int:
    from pipeline.project import cli

    monkeypatch.setattr("pipeline.project.cli.run_pipeline", lambda **_kw: session)
    monkeypatch.setattr("pipeline.project.cli._stdio_interactive", lambda: False)
    saved = sys.argv
    sys.argv = [
        "orchestrator", "--task", "demo", "--project", str(project), "--mock",
        "--no-interactive",
    ]
    try:
        try:
            cli.main()
            return 0
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 0
    finally:
        sys.argv = saved


@pytest.mark.parametrize(
    ("session", "rc"),
    [
        ({"status": "done"}, 0),
        ({"status": "awaiting_phase_handoff"}, 4),
        ({"status": "halted", "halt_reason": "commit_delivery_pending"}, 3),
        ({"status": "halted", "halt_reason": "commit_decision_halt"}, 3),
        ({"status": "halted", "halt_reason": "final_acceptance_rejected"}, 3),
    ],
    ids=["done", "handoff-pause", "parked-delivery", "operator-halt", "rejected"],
)
def test_exit_code_follows_terminal_status(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, session: dict, rc: int,
) -> None:
    assert _run_main(isolated, session, monkeypatch) == rc
