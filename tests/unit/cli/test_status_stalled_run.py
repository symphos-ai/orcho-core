"""``orcho status`` stops describing a vanished run as working (J1).

A detached run whose process is killed without writing a terminal event
leaves ``meta.status`` at ``running`` forever — the pipeline that would have
corrected the record is gone. Status printed that verbatim, so runs whose
processes had been gone for hours read as live work and the only way to tell
was probing the recorded pid by hand.

The verdict is not re-derived here: ``run_diagnosis`` already owns it, and
status asks that one owner so the two surfaces cannot disagree.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cli import orcho
from core.io.ansi import strip_ansi

_DEAD_PID = 2_147_480_000  # far above any live pid; the probe answers "dead"


def _make_args(**kwargs):
    from types import SimpleNamespace

    return SimpleNamespace(
        run_id=kwargs.pop("run_id", None),
        workspace=kwargs.pop("workspace", None),
        verbose=kwargs.pop("verbose", False),
        **kwargs,
    )


@pytest.fixture
def runs_dir(tmp_path: Path, monkeypatch) -> Path:
    rd = tmp_path / "runs"
    rd.mkdir()
    monkeypatch.setenv("ORCHO_RUNSPACE", str(tmp_path))
    return rd


def _write_running_run(
    runs_dir: Path,
    run_id: str,
    *,
    age_seconds: float,
    launch: bool = True,
    status: str = "running",
) -> Path:
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({
        "task": "hedge",
        "project": "/some/proj",
        "profile": "small_task",
        "timestamp": "2026-08-28T09:21:52",
        "status": status,
        "phases": {"implement": [{}]},
    }), encoding="utf-8")
    if launch:
        (run_dir / "run_supervisor.json").write_text(json.dumps({
            "pid": _DEAD_PID,
            "started_at": (
                datetime.now(UTC) - timedelta(seconds=age_seconds)
            ).isoformat(),
        }), encoding="utf-8")
    # The event writer's real format: naive local wall-clock.
    stamped = (datetime.now() - timedelta(seconds=age_seconds)).isoformat()
    (run_dir / "events.jsonl").write_text(
        json.dumps({
            "seq": 1, "ts": stamped, "kind": "agent.tool_use", "payload": {},
        }) + "\n",
        encoding="utf-8",
    )
    return run_dir


def test_status_names_the_dead_process_and_the_repair_command(
    runs_dir: Path, capsys,
) -> None:
    run_dir = _write_running_run(runs_dir, "20260828_092152", age_seconds=8 * 3600)

    assert orcho.cmd_status(_make_args(run_id="20260828_092152")) == 0

    out = strip_ansi(capsys.readouterr().out)
    assert "Stalled:" in out
    assert f"pid {_DEAD_PID} is no longer alive" in out
    assert "orcho repair-state" in out
    # Reporting only: the durable record is left exactly as it was.
    assert json.loads((run_dir / "meta.json").read_text())["status"] == "running"


def test_status_stays_quiet_for_a_live_run(runs_dir: Path, capsys) -> None:
    _write_running_run(runs_dir, "20260828_fresh", age_seconds=1.0)

    assert orcho.cmd_status(_make_args(run_id="20260828_fresh")) == 0

    out = strip_ansi(capsys.readouterr().out)
    assert "Stalled:" not in out


def test_status_stays_quiet_when_no_pid_was_recorded(
    runs_dir: Path, capsys,
) -> None:
    """Unknown must never render as dead."""
    _write_running_run(
        runs_dir, "20260828_nolaunch", age_seconds=8 * 3600, launch=False,
    )

    assert orcho.cmd_status(_make_args(run_id="20260828_nolaunch")) == 0

    assert "Stalled:" not in strip_ansi(capsys.readouterr().out)


def test_a_failed_diagnosis_leaves_status_intact(
    runs_dir: Path, capsys, monkeypatch,
) -> None:
    """Enrichment must never replace the answer the operator asked for."""
    _write_running_run(runs_dir, "20260828_092152", age_seconds=8 * 3600)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("diagnosis unavailable")

    monkeypatch.setattr("sdk.run_control.run_diagnosis", _boom)

    assert orcho.cmd_status(_make_args(run_id="20260828_092152")) == 0

    out = strip_ansi(capsys.readouterr().out)
    assert "20260828_092152" in out
    assert "Stalled:" not in out


def test_a_terminal_run_is_never_probed(runs_dir: Path, capsys, monkeypatch) -> None:
    """Only a ``running`` record can be lying about it."""
    _write_running_run(
        runs_dir, "20260828_done", age_seconds=8 * 3600, status="done",
    )
    calls: list[str] = []
    monkeypatch.setattr(
        "sdk.run_control.run_diagnosis",
        lambda run_id, **kw: calls.append(run_id),
    )

    assert orcho.cmd_status(_make_args(run_id="20260828_done")) == 0

    assert calls == []
    assert "Stalled:" not in strip_ansi(capsys.readouterr().out)
