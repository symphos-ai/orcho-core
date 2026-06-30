"""`find_runs_dir` precedence — covers the original CLI walk-up
semantics moved out of `cli.orcho._runs_dir`.

Walk-up beats `$ORCHO_WORKSPACE` because physical user presence is a
stronger context signal than a global env var. `$ORCHO_RUNSPACE`
beats both. Explicit kwargs (`runs_dir`, `workspace`) beat all of
them.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.infra import config
from sdk.runs import find_runs_dir


@pytest.fixture(autouse=True)
def _reset_config_cache():
    config._reset_config()
    yield
    config._reset_config()


class TestFindRunsDirPrecedence:
    def test_walkup_beats_orcho_workspace_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ws_a = tmp_path / "atas" / "workspace-orchestrator"
        (ws_a / "runspace" / "runs").mkdir(parents=True)

        ws_b = tmp_path / "qcg" / "workspace-orchestrator"
        (ws_b / "runspace" / "runs").mkdir(parents=True)

        cwd = ws_a.parent / "bot_1"
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        monkeypatch.setenv("ORCHO_WORKSPACE", str(ws_b))
        monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)

        # Default cwd → walk-up enabled. Walk-up should hit ws_a's
        # runspace/runs as the user physically sits inside ws_a's tree.
        assert find_runs_dir() == ws_a / "runspace" / "runs"

    def test_orcho_runspace_env_wins_over_walkup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ws_walkup = tmp_path / "atas" / "workspace-orchestrator"
        (ws_walkup / "runspace" / "runs").mkdir(parents=True)

        ws_explicit = tmp_path / "explicit-workspace"
        (ws_explicit / "runspace" / "runs").mkdir(parents=True)

        cwd = ws_walkup.parent / "bot_1"
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        monkeypatch.setenv("ORCHO_RUNSPACE", str(ws_explicit / "runspace"))

        assert find_runs_dir() == ws_explicit / "runspace" / "runs"

    def test_falls_back_to_orcho_workspace_when_walkup_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ws_b = tmp_path / "qcg" / "workspace-orchestrator"
        (ws_b / "runspace" / "runs").mkdir(parents=True)

        monkeypatch.setenv("ORCHO_WORKSPACE", str(ws_b))
        monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)

        assert find_runs_dir(cwd=None) == ws_b / "runspace" / "runs"

    def test_explicit_runs_dir_beats_everything(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        explicit = tmp_path / "explicit" / "runs"
        explicit.mkdir(parents=True)

        # Plant red herrings everywhere: env, walk-up tree.
        red = tmp_path / "red" / "runspace"
        (red / "runs").mkdir(parents=True)
        monkeypatch.setenv("ORCHO_RUNSPACE", str(red))
        monkeypatch.setenv("ORCHO_WORKSPACE", str(tmp_path / "red"))

        assert find_runs_dir(runs_dir=explicit) == explicit
