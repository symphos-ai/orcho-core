"""`find_runs_dir` / `find_run` resolution contract."""
from __future__ import annotations

from pathlib import Path

import pytest

from sdk import NoWorkspace, RunNotFound
from sdk.runs import find_run, find_runs_dir


def test_explicit_runs_dir_wins(populated_runs: Path):
    rd = find_runs_dir(runs_dir=populated_runs)
    assert rd == populated_runs


def test_explicit_workspace(populated_runs: Path):
    workspace = populated_runs.parent.parent  # tmp_path/
    rd = find_runs_dir(workspace=workspace)
    assert rd == populated_runs


def test_no_walk_up_when_cwd_none(tmp_path: Path, monkeypatch):
    # Strip env so config resolution can't slip in.
    monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)
    monkeypatch.delenv("ORCHO_WORKSPACE", raising=False)
    with pytest.raises(NoWorkspace):
        find_runs_dir(cwd=None)


def test_runs_dir_must_exist(tmp_path: Path):
    with pytest.raises(NoWorkspace):
        find_runs_dir(runs_dir=tmp_path / "does-not-exist")


def test_find_run_latest(populated_runs: Path):
    ref = find_run(runs_dir=populated_runs)
    assert ref.run_id == "20260507_120000"
    assert ref.run_dir == populated_runs / "20260507_120000"


def test_find_run_by_id(populated_runs: Path):
    ref = find_run("20260506_090000", runs_dir=populated_runs)
    assert ref.run_dir.name == "20260506_090000"


def test_find_run_unknown_id(populated_runs: Path):
    with pytest.raises(RunNotFound):
        find_run("nope", runs_dir=populated_runs)


def test_find_run_empty_dir(runs_root: Path):
    with pytest.raises(RunNotFound):
        find_run(runs_dir=runs_root)
