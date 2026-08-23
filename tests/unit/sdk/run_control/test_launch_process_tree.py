from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdk.run_control.launch import (
    LaunchedRun,
    _spawn_detached,
    cancel_run,
    write_launch_state,
)


def _run(tmp_path: Path) -> LaunchedRun:
    return LaunchedRun(
        run_id="run", pid=123, pgid=123, run_dir=tmp_path, project_dir=str(tmp_path),
        command=["python"], started_at="now", mock=False, output_mode="summary",
    )


def test_supervisor_state_adds_detached_process_tree_descriptor(tmp_path: Path) -> None:
    write_launch_state(_run(tmp_path))

    state = json.loads((tmp_path / "run_supervisor.json").read_text())
    assert state["pid"] == 123
    assert state["pgid"] == 123
    assert state["process_tree"] == {
        "platform": "windows" if __import__("sys").platform == "win32" else "posix",
        "root_pid": 123,
        "group_id": 123,
        "group_owned": True,
    }


def test_spawn_detached_uses_adapter_flags(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class Popen:
        pass

    monkeypatch.setattr("sdk.run_control.launch.detached_spawn_kwargs", lambda: {"creationflags": 512})
    monkeypatch.setattr(
        "sdk.run_control.launch.subprocess.Popen",
        lambda *args, **kwargs: captured.update(kwargs) or Popen(),
    )
    with (tmp_path / "log").open("w") as log:
        _spawn_detached(["python"], project_dir=str(tmp_path), env={}, log_fd=log)

    assert captured["creationflags"] == 512
    assert "start_new_session" not in captured


def test_cancel_uses_legacy_pid_fallback_and_adapter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run_supervisor.json").write_text(json.dumps({"pid": 321, "pgid": 321}))
    calls: list[tuple[object, int, bool]] = []
    monkeypatch.setattr("sdk.run_control.launch.find_runs_dir", lambda **kwargs: tmp_path)
    monkeypatch.setattr("sdk.run_control.launch.pid_is_alive", lambda pid: True)
    monkeypatch.setattr(
        "sdk.run_control.launch.terminate_recorded_tree",
        lambda descriptor, *, fallback_pid, hard, deadline: (
            calls.append((descriptor, fallback_pid, hard)) or "hard"
        ),
    )

    result = cancel_run("run", runs_dir=str(tmp_path), mode="hard")

    assert result.status == "signal_sent(hard)"
    assert calls == [(None, 321, True)]


def test_cancel_reports_the_mode_actually_delivered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A graceful request that could only be served by a hard tree kill must
    not be reported as graceful — the pipeline never got to checkpoint."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run_supervisor.json").write_text(json.dumps({"pid": 321, "pgid": 321}))
    monkeypatch.setattr("sdk.run_control.launch.find_runs_dir", lambda **kwargs: tmp_path)
    monkeypatch.setattr("sdk.run_control.launch.pid_is_alive", lambda pid: True)
    monkeypatch.setattr(
        "sdk.run_control.launch.terminate_recorded_tree",
        lambda descriptor, *, fallback_pid, hard, deadline: "hard",
    )

    result = cancel_run("run", runs_dir=str(tmp_path), mode="graceful")

    assert result.status == "signal_sent(hard)"
