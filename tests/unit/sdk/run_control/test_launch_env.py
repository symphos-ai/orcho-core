from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdk.run_control.launch import LaunchSpec, launch_run, resume_run


class _Popen:
    pid = 12345


def _capture_spawn(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    captured: dict[str, object] = {}

    def _spawn(_cmd, **kwargs):
        captured.update(kwargs)
        return _Popen()

    monkeypatch.setattr("sdk.run_control.launch._spawn_detached", _spawn)
    return captured


@pytest.mark.parametrize("profile", ["feature", "task", "auto-detect"])
def test_launch_profile_is_argv_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str,
) -> None:
    project = tmp_path / "project"
    runs = tmp_path / "runs"
    project.mkdir()
    runs.mkdir()
    monkeypatch.setenv("ORCHO_PIPELINE", "planning")
    captured = _capture_spawn(monkeypatch)

    result = launch_run(
        LaunchSpec(
            project_dir=str(project), task="Do it", runs_dir=str(runs),
            profile=profile,
        ),
        run_id="child",
    )

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["ORCHO_RUN_ID"] == "child"
    assert "ORCHO_PIPELINE" not in env
    assert "--profile" in result.run.command
    assert result.run.command[result.run.command.index("--profile") + 1] == profile


def test_resume_strips_ambient_pipeline_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    runs = tmp_path / "runs"
    run_dir = runs / "run"
    project.mkdir()
    run_dir.mkdir(parents=True)
    (run_dir / "run_supervisor.json").write_text(json.dumps({
        "project_dir": str(project), "mock": False, "output_mode": "summary",
    }), encoding="utf-8")
    (run_dir / "meta.json").write_text(json.dumps({
        "task": "Do it", "profile": "task",
    }), encoding="utf-8")
    monkeypatch.setenv("ORCHO_PIPELINE", "planning")
    captured = _capture_spawn(monkeypatch)

    result = resume_run("run", runs_dir=str(runs))

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["ORCHO_RUN_ID"] == "run"
    assert "ORCHO_PIPELINE" not in env
    assert result.run.command[result.run.command.index("--profile") + 1] == "task"
