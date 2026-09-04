"""Resume inherits the run's persisted ``max_rounds`` budget.

The defect this pins: an operator starts a run with ``max_rounds=4``, the
first subprocess gets ``--max-rounds 4``, then a phase-handoff resume
re-spawns without the flag. The orchestrator's argparse default (1) then
applies, so the repair loop silently shrinks to a single round *and*
``bootstrap`` writes the shrunken value back over
``checkpoints.db:run_meta.config_json`` — after which the operator cannot
even audit what they originally asked for.

``resume_run`` already inherits ``mock`` / ``output_mode`` (from
``run_supervisor.json``) and the profile (from ``meta.json``) so a resume
continues the same run rather than re-negotiating it. ``max_rounds`` is
the missing member of that set, and its persisted home is the run's own
checkpoint store — the value bootstrap writes. Reading it back there
keeps a single owner: nothing re-derives the budget, and no second copy
is introduced on the neutral state file.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.checkpoint import CheckpointStore
from sdk.run_control.launch import resume_run

pytestmark = [pytest.mark.sdk, pytest.mark.project_run, pytest.mark.filesystem_light]


class _Popen:
    pid = 4242


def _seed_resumable_run(
    tmp_path: Path, *, run_id: str = "run", config: dict | None = None,
) -> tuple[Path, Path]:
    """Build the minimal on-disk shape ``resume_run`` accepts."""
    project = tmp_path / "project"
    runs = tmp_path / "runs"
    run_dir = runs / run_id
    project.mkdir()
    run_dir.mkdir(parents=True)
    (run_dir / "run_supervisor.json").write_text(
        json.dumps({
            "project_dir": str(project), "mock": True, "output_mode": "summary",
        }),
        encoding="utf-8",
    )
    (run_dir / "meta.json").write_text(
        json.dumps({"task": "Do it", "profile": "feature"}), encoding="utf-8",
    )
    if config is not None:
        store = CheckpointStore(run_dir / "checkpoints.db", run_id=run_id)
        store.save_config(config)
        store.close()
    return project, runs


def _no_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "sdk.run_control.launch._spawn_detached", lambda _cmd, **_kw: _Popen(),
    )


def _flag_value(command: list[str], flag: str) -> str | None:
    return command[command.index(flag) + 1] if flag in command else None


def test_resume_re_emits_persisted_non_default_max_rounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run started with ``max_rounds=4`` resumes with ``--max-rounds 4``."""
    _seed_resumable_run(tmp_path, config={"task": "Do it", "max_rounds": 4})
    _no_spawn(monkeypatch)

    result = resume_run("run", runs_dir=str(tmp_path / "runs"))

    assert _flag_value(result.run.command, "--max-rounds") == "4", (
        "resume dropped the operator's budget; the orchestrator's argparse "
        f"default would apply instead. argv={result.run.command}"
    )


def test_resume_re_emits_persisted_default_max_rounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``max_rounds=1`` is re-emitted too — inheritance is not a
    "non-default only" special case, so the resumed argv states the
    budget explicitly instead of relying on an argparse default that a
    later release could change."""
    _seed_resumable_run(tmp_path, config={"task": "Do it", "max_rounds": 1})
    _no_spawn(monkeypatch)

    result = resume_run("run", runs_dir=str(tmp_path / "runs"))

    assert _flag_value(result.run.command, "--max-rounds") == "1"


@pytest.mark.parametrize(
    "config",
    [
        pytest.param(None, id="no-checkpoint-store"),
        pytest.param({"task": "Do it"}, id="config-without-max-rounds"),
        pytest.param({"task": "Do it", "max_rounds": 0}, id="non-positive"),
        pytest.param({"task": "Do it", "max_rounds": "4"}, id="non-integer"),
    ],
)
def test_resume_omits_flag_when_nothing_is_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: dict | None,
) -> None:
    """Nothing to inherit ⇒ previous behaviour, not a fabricated budget.

    A pre-existing run dir, a store written before the budget was
    recorded, or a corrupt value must leave the flag off so the
    orchestrator applies its own default — the fix narrows a silent loss,
    it does not invent a value.
    """
    _seed_resumable_run(tmp_path, config=config)
    _no_spawn(monkeypatch)

    result = resume_run("run", runs_dir=str(tmp_path / "runs"))

    assert "--max-rounds" not in result.run.command


def test_resume_still_inherits_mock_profile_and_output_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The budget rides alongside the existing inheritance set, not instead
    of it."""
    _seed_resumable_run(tmp_path, config={"task": "Do it", "max_rounds": 4})
    _no_spawn(monkeypatch)

    result = resume_run("run", runs_dir=str(tmp_path / "runs"))

    command = result.run.command
    assert "--mock" in command
    assert _flag_value(command, "--profile") == "feature"
    assert _flag_value(command, "--output") == "summary"
    assert _flag_value(command, "--resume") == "run"
