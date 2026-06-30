"""Unit coverage for worktree bootstrap actions."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from pipeline.engine.worktree_bootstrap import (
    WorktreeBootstrapError,
    run_worktree_bootstrap,
)


def test_copy_step_copies_gitignored_dependency_dir(tmp_path: Path) -> None:
    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    (source / "libs").mkdir(parents=True)
    worktree.mkdir()
    (source / "libs" / "native.dll").write_bytes(b"dll")

    result = run_worktree_bootstrap(
        [{"copy": "libs"}],
        source_root=source,
        worktree_path=worktree,
    )

    assert result["status"] == "ok"
    assert (worktree / "libs" / "native.dll").read_bytes() == b"dll"
    assert result["steps"][0]["action"] == "copy"


def test_run_step_executes_portable_argv_in_worktree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    source.mkdir()
    worktree.mkdir()

    result = run_worktree_bootstrap(
        [{
            "run": [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('vendor.ok').write_text('ok')",
            ],
        }],
        source_root=source,
        worktree_path=worktree,
    )

    assert result["status"] == "ok"
    assert (worktree / "vendor.ok").read_text(encoding="utf-8") == "ok"
    assert result["steps"][0]["cwd"] == str(worktree.resolve())


def test_python_step_can_run_tracked_project_script(tmp_path: Path) -> None:
    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    source.mkdir()
    (worktree / "scripts").mkdir(parents=True)
    (worktree / "scripts" / "bootstrap.py").write_text(
        "from pathlib import Path\nPath('script.ok').write_text('ok')\n",
        encoding="utf-8",
    )

    result = run_worktree_bootstrap(
        [{"python": "scripts/bootstrap.py"}],
        source_root=source,
        worktree_path=worktree,
    )

    assert result["status"] == "ok"
    assert (worktree / "script.ok").read_text(encoding="utf-8") == "ok"


def test_copy_step_refuses_path_escape(tmp_path: Path) -> None:
    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    source.mkdir()
    worktree.mkdir()

    with pytest.raises(WorktreeBootstrapError, match="escapes"):
        run_worktree_bootstrap(
            [{"copy": "../outside"}],
            source_root=source,
            worktree_path=worktree,
        )


def test_failed_run_step_raises_without_capturing_output(tmp_path: Path) -> None:
    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    source.mkdir()
    worktree.mkdir()

    with pytest.raises(WorktreeBootstrapError) as excinfo:
        run_worktree_bootstrap(
            [{
                "run": [
                    sys.executable,
                    "-c",
                    "import sys; print('secret-output'); sys.exit(7)",
                ],
            }],
            source_root=source,
            worktree_path=worktree,
        )

    message = str(excinfo.value)
    assert "exit code 7" in message
    assert "secret-output" not in message


def test_platform_mismatch_skips_step(tmp_path: Path) -> None:
    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    source.mkdir()
    worktree.mkdir()

    result = run_worktree_bootstrap(
        [{"copy": "libs", "platforms": ["definitely-not-this-platform"]}],
        source_root=source,
        worktree_path=worktree,
    )

    assert result["status"] == "ok"
    assert result["steps"] == [{
        "index": 1,
        "action": "copy",
        "status": "skipped",
        "reason": "platform mismatch",
    }]
