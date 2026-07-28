"""Installed-style journey for connecting one existing repository in place."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sdk.workspace_paths import managed_workspace_dir


def _env(data_home: Path) -> dict[str, str]:
    env = {
        **os.environ,
        "XDG_DATA_HOME": str(data_home),
    }
    env.pop("ORCHO_WORKSPACE", None)
    env.pop("ORCHO_RUNSPACE", None)
    return env


def test_init_then_status_from_existing_repo_needs_no_workspace_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "existing project"
    project.mkdir()
    (project / ".git").mkdir()
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    env = _env(data_home)

    init = subprocess.run(
        [
            sys.executable,
            "-m",
            "cli.orcho",
            "workspace",
            "init",
            "--force",
            "--no-interactive",
            "--no-scaffold",
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    workspace = managed_workspace_dir(project)
    assert f"Project:        {project}" in init.stdout
    assert f"Workspace:      {workspace}" in init.stdout
    assert not (project / "workspace-orchestrator").exists()
    config = json.loads(
        (workspace / ".orcho" / "config.local.json").read_text(encoding="utf-8")
    )
    assert config["projects"] == {"existing project": str(project)}

    run_dir = workspace / "runspace" / "runs" / "ONBOARDING_SMOKE"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(
        json.dumps({
            "project": str(project),
            "task": "Onboarding smoke",
            "status": "done",
        }),
        encoding="utf-8",
    )

    status = subprocess.run(
        [sys.executable, "-m", "cli.orcho", "status", "ONBOARDING_SMOKE"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert "ONBOARDING_SMOKE" in status.stdout
    assert "done" in status.stdout.lower()
