"""ADR 0131: plugin ``worktree_teardown`` at run finalization.

Covers the finalization helper that invokes a plugin's declared teardown steps
in the worktree cwd, and its best-effort contract (a terminal-run cleanup must
never raise).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from pipeline.project.finalization import (
    _run_plugin_worktree_teardown,
    _teardown_worktree_for_finalization,
)


def _run(
    *, teardown, project_dir: Path, worktree: Path, status: str = "halted",
) -> SimpleNamespace:
    return SimpleNamespace(
        session={"status": status},
        plugin=SimpleNamespace(worktree_teardown=teardown),
        worktree_context=SimpleNamespace(project_dir=project_dir, path=worktree),
    )


def test_teardown_runs_declared_steps_in_worktree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    source.mkdir()
    worktree.mkdir()

    run = _run(
        teardown=[{
            "run": [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('torn.ok').write_text('ok')",
            ],
        }],
        project_dir=source,
        worktree=worktree,
    )
    _run_plugin_worktree_teardown(run)

    assert (worktree / "torn.ok").read_text(encoding="utf-8") == "ok"


def test_teardown_skipped_on_resumable_pause(tmp_path: Path) -> None:
    # A run paused awaiting a phase-handoff decision keeps its worktree AND its
    # external stack for resume — teardown must NOT run.
    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    source.mkdir()
    worktree.mkdir()

    run = _run(
        teardown=[{
            "run": [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('torn.ok').write_text('ok')",
            ],
        }],
        project_dir=source,
        worktree=worktree,
        status="awaiting_phase_handoff",
    )
    _run_plugin_worktree_teardown(run)

    assert not (worktree / "torn.ok").exists()


def test_teardown_noop_without_declared_steps(tmp_path: Path) -> None:
    # No plugin teardown declared → the helper is a clean no-op.
    run = _run(teardown=[], project_dir=tmp_path, worktree=tmp_path)
    _run_plugin_worktree_teardown(run)  # must not raise


def test_teardown_is_best_effort_and_never_raises(tmp_path: Path) -> None:
    # Even a wholly broken teardown (missing binary) must not raise into
    # finalization — the run is already terminal.
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run = _run(
        teardown=[{"run": ["definitely-not-a-real-binary-xyz", "down"]}],
        project_dir=tmp_path,
        worktree=worktree,
    )
    _run_plugin_worktree_teardown(run)  # swallowed, no exception


def test_off_mode_teardown_is_not_presented_as_removed(tmp_path: Path) -> None:
    run = SimpleNamespace(
        worktree_context=SimpleNamespace(
            mode="off",
            project_dir=tmp_path,
            path=tmp_path,
        ),
        session={"status": "done"},
        plugin=SimpleNamespace(worktree_teardown=[]),
    )

    assert _teardown_worktree_for_finalization(run) is None


def test_isolated_teardown_preserves_retained_disposition(
    tmp_path: Path,
    monkeypatch,
) -> None:
    retained = tmp_path / "worktree"
    run = SimpleNamespace(
        worktree_context=SimpleNamespace(
            mode="per_run",
            project_dir=tmp_path,
            path=retained,
        ),
        session={"status": "done"},
        plugin=SimpleNamespace(worktree_teardown=[]),
    )
    monkeypatch.setattr(
        "pipeline.engine.worktree.teardown_worktree",
        lambda ctx, *, retain: SimpleNamespace(
            error=f"retained worktree at {ctx.path}",
        ),
    )

    assert _teardown_worktree_for_finalization(run) == (
        f"retained worktree at {retained}"
    )
