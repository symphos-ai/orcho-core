"""Unit coverage for worktree bootstrap actions."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest

from pipeline.engine.worktree_bootstrap import (
    WorktreeBootstrapError,
    run_worktree_bootstrap,
    run_worktree_teardown,
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


# ── worktree_teardown (ADR 0131) ─────────────────────────────────────────────


def test_teardown_runs_step_in_worktree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    source.mkdir()
    worktree.mkdir()

    result = run_worktree_teardown(
        [{
            "run": [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('down.ok').write_text('ok')",
            ],
        }],
        source_root=source,
        worktree_path=worktree,
    )

    assert result["status"] == "ok"
    assert (worktree / "down.ok").read_text(encoding="utf-8") == "ok"


@pytest.mark.parametrize("config", [[], None, False, {"enabled": False}])
def test_teardown_disabled_or_empty_is_skipped(tmp_path: Path, config) -> None:
    result = run_worktree_teardown(
        config, source_root=tmp_path, worktree_path=tmp_path,
    )
    assert result["status"] == "skipped"
    assert result["steps"] == []


def test_teardown_failing_step_is_best_effort_not_raised(tmp_path: Path) -> None:
    # A terminal-run cleanup must never raise — a failing step (e.g. a
    # ``docker compose down`` against a stack already gone) is recorded and the
    # remaining steps still run.
    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    source.mkdir()
    worktree.mkdir()

    result = run_worktree_teardown(
        [
            {"run": [sys.executable, "-c", "import sys; sys.exit(7)"]},
            {"run": [
                sys.executable, "-c",
                "from pathlib import Path; Path('after.ok').write_text('ok')",
            ]},
        ],
        source_root=source,
        worktree_path=worktree,
    )

    assert result["status"] == "failed"
    assert result["steps"][0]["status"] == "failed"
    assert "exit code 7" in result["steps"][0]["error"]
    # The step after the failure still ran (best-effort, no short-circuit).
    assert (worktree / "after.ok").read_text(encoding="utf-8") == "ok"
    assert result["steps"][1]["status"] == "ok"


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


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    """Create empty source + worktree roots; return (source, worktree)."""
    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    source.mkdir()
    worktree.mkdir()
    return source, worktree


# --- _normalise_steps: disabled / empty forms -> skipped (line 50, 77-78, 84-85)


@pytest.mark.parametrize(
    "config",
    [None, False, {"enabled": False}, {"steps": []}],
)
def test_disabled_or_empty_config_is_skipped(tmp_path: Path, config) -> None:
    """Branches 50/78/84-85: disabled/empty shapes short-circuit to skipped."""
    source, worktree = _roots(tmp_path)

    result = run_worktree_bootstrap(
        config, source_root=source, worktree_path=worktree,
    )

    assert result == {"status": "skipped", "steps": []}


# --- _normalise_steps: shape coercion + errors (lines 81-82, 88-93, 94-96, 101)


def test_tuple_config_runs_as_step_list(tmp_path: Path) -> None:
    """Branch 81-82: a tuple config is normalised into a list of steps."""
    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    (source / "libs").mkdir(parents=True)
    worktree.mkdir()
    (source / "libs" / "a.txt").write_text("x", encoding="utf-8")

    result = run_worktree_bootstrap(
        ({"copy": "libs"},),
        source_root=source,
        worktree_path=worktree,
    )

    assert result["status"] == "ok"
    assert (worktree / "libs" / "a.txt").read_text(encoding="utf-8") == "x"


def test_mapping_steps_must_be_a_list(tmp_path: Path) -> None:
    """Branch 88-91: {'steps': <non-list>} raises 'steps must be a list'."""
    source, worktree = _roots(tmp_path)

    with pytest.raises(WorktreeBootstrapError, match="steps must be a list"):
        run_worktree_bootstrap(
            {"steps": "not-a-list"},
            source_root=source,
            worktree_path=worktree,
        )


def test_bare_mapping_config_is_treated_as_single_step(tmp_path: Path) -> None:
    """Branch 93: a bare dict config (no 'steps') becomes one step."""
    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    (source / "libs").mkdir(parents=True)
    worktree.mkdir()
    (source / "libs" / "a.txt").write_text("y", encoding="utf-8")

    result = run_worktree_bootstrap(
        {"copy": "libs"},
        source_root=source,
        worktree_path=worktree,
    )

    assert result["status"] == "ok"
    assert (worktree / "libs" / "a.txt").read_text(encoding="utf-8") == "y"


@pytest.mark.parametrize("config", [7, "steps"])
def test_invalid_top_level_config_type_raises(tmp_path: Path, config) -> None:
    """Branch 94-96: an int/str top-level config is rejected."""
    source, worktree = _roots(tmp_path)

    with pytest.raises(
        WorktreeBootstrapError,
        match="must be a list, dict, false, or null",
    ):
        run_worktree_bootstrap(
            config, source_root=source, worktree_path=worktree,
        )


def test_non_mapping_step_in_list_raises(tmp_path: Path) -> None:
    """Branch 101: a non-Mapping step element raises 'must be a dict'."""
    source, worktree = _roots(tmp_path)

    with pytest.raises(WorktreeBootstrapError, match="step 1 must be a dict"):
        run_worktree_bootstrap(
            ["not-a-dict"],
            source_root=source,
            worktree_path=worktree,
        )


# --- _run_step / _action_name dispatch (lines 135, 143-145, 155)


def test_step_without_supported_action_raises(tmp_path: Path) -> None:
    """Branches 155 + 135: an unknown step shape -> 'no supported action'."""
    source, worktree = _roots(tmp_path)

    with pytest.raises(
        WorktreeBootstrapError, match="no supported action",
    ):
        run_worktree_bootstrap(
            [{"frobnicate": "x"}],
            source_root=source,
            worktree_path=worktree,
        )


def test_type_command_alias_routes_to_run(tmp_path: Path) -> None:
    """Branch 143-144: type 'command' resolves to the run action."""
    source, worktree = _roots(tmp_path)

    result = run_worktree_bootstrap(
        [{
            "type": "command",
            "command": [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('cmd.ok').write_text('ok')",
            ],
        }],
        source_root=source,
        worktree_path=worktree,
    )

    assert result["steps"][0]["action"] == "run"
    assert (worktree / "cmd.ok").read_text(encoding="utf-8") == "ok"


def test_type_shell_routes_to_shell(tmp_path: Path) -> None:
    """Branch 145 + 130-134: type 'shell' resolves to the shell action."""
    source, worktree = _roots(tmp_path)

    result = run_worktree_bootstrap(
        [{"type": "shell", "shell": "printf done > shell.ok"}],
        source_root=source,
        worktree_path=worktree,
    )

    assert result["steps"][0]["action"] == "shell"
    assert (worktree / "shell.ok").read_text(encoding="utf-8") == "done"


# --- _copy_step error + single-file branches (lines 167, 172, 180, 184, 191-192)


@pytest.mark.parametrize("bad_from", ["", "   ", 123])
def test_copy_step_requires_non_empty_source(tmp_path: Path, bad_from) -> None:
    """Branch 167: empty/non-string copy source -> 'non-empty source'."""
    source, worktree = _roots(tmp_path)

    with pytest.raises(WorktreeBootstrapError, match="non-empty source"):
        run_worktree_bootstrap(
            [{"copy": bad_from}],
            source_root=source,
            worktree_path=worktree,
        )


@pytest.mark.parametrize("bad_to", ["   ", 123])
def test_copy_step_requires_non_empty_target(tmp_path: Path, bad_to) -> None:
    """Branch 172: empty/non-string copy target -> 'non-empty target'."""
    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    (source / "libs").mkdir(parents=True)
    worktree.mkdir()

    with pytest.raises(WorktreeBootstrapError, match="non-empty target"):
        run_worktree_bootstrap(
            [{"copy": "libs", "to": bad_to}],
            source_root=source,
            worktree_path=worktree,
        )


def test_copy_step_missing_source_raises(tmp_path: Path) -> None:
    """Branch 180: a source path that does not exist -> 'does not exist'."""
    source, worktree = _roots(tmp_path)

    with pytest.raises(WorktreeBootstrapError, match="source does not exist"):
        run_worktree_bootstrap(
            [{"copy": "absent"}],
            source_root=source,
            worktree_path=worktree,
        )


def test_copy_step_refuses_existing_target_without_overwrite(
    tmp_path: Path,
) -> None:
    """Branch 184: existing target with overwrite:false -> 'target exists'."""
    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    (source / "libs").mkdir(parents=True)
    (worktree / "libs").mkdir(parents=True)

    with pytest.raises(WorktreeBootstrapError, match="target exists"):
        run_worktree_bootstrap(
            [{"copy": "libs", "overwrite": False}],
            source_root=source,
            worktree_path=worktree,
        )


def test_copy_step_copies_single_file_and_creates_parent(
    tmp_path: Path,
) -> None:
    """Branch 191-192: copying a single file makes parent dirs and content."""
    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    source.mkdir()
    worktree.mkdir()
    (source / "config.ini").write_text("k=v", encoding="utf-8")

    result = run_worktree_bootstrap(
        [{"copy": "config.ini", "to": "nested/config.ini"}],
        source_root=source,
        worktree_path=worktree,
    )

    assert result["steps"][0]["status"] == "ok"
    assert (worktree / "nested" / "config.ini").read_text(
        encoding="utf-8",
    ) == "k=v"


# --- _python_step error branches (lines 234, 241)


@pytest.mark.parametrize("bad_script", ["", "   ", 5])
def test_python_step_requires_script_path(tmp_path: Path, bad_script) -> None:
    """Branch 234: empty/missing python script -> 'needs a script path'."""
    source, worktree = _roots(tmp_path)

    with pytest.raises(WorktreeBootstrapError, match="needs a script path"):
        run_worktree_bootstrap(
            [{"python": bad_script}],
            source_root=source,
            worktree_path=worktree,
        )


def test_python_step_args_must_be_a_list(tmp_path: Path) -> None:
    """Branch 241: non-list python args -> 'args must be a list'."""
    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    source.mkdir()
    (worktree / "scripts").mkdir(parents=True)
    (worktree / "scripts" / "b.py").write_text("pass\n", encoding="utf-8")

    with pytest.raises(WorktreeBootstrapError, match="args must be a list"):
        run_worktree_bootstrap(
            [{"python": "scripts/b.py", "args": "not-a-list"}],
            source_root=source,
            worktree_path=worktree,
        )


# --- _coerce_argv: string command + empty (lines 304-306, 313)


def test_run_step_string_command_is_shlex_split_and_executed(
    tmp_path: Path,
) -> None:
    """Branch 304-306: a string command is shlex-split and really runs."""
    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    source.mkdir()
    worktree.mkdir()
    (worktree / "make.py").write_text(
        "open('shlex.ok', 'w').write('ok')\n", encoding="utf-8",
    )

    result = run_worktree_bootstrap(
        [{"run": f"{shlex.quote(sys.executable)} make.py"}],
        source_root=source,
        worktree_path=worktree,
    )

    assert result["steps"][0]["action"] == "run"
    assert (worktree / "shlex.ok").read_text(encoding="utf-8") == "ok"


@pytest.mark.parametrize("empty_cmd", ["", "   ", []])
def test_run_step_empty_command_raises(tmp_path: Path, empty_cmd) -> None:
    """Branch 313: empty string/argv command -> 'non-empty command'."""
    source, worktree = _roots(tmp_path)

    with pytest.raises(WorktreeBootstrapError, match="non-empty command"):
        run_worktree_bootstrap(
            [{"run": empty_cmd}],
            source_root=source,
            worktree_path=worktree,
        )


# --- _run_subprocess: command not found (lines 335-338)


def test_run_step_missing_binary_raises_command_not_found(
    tmp_path: Path,
) -> None:
    """Branch 335-338: a missing binary surfaces as 'command not found'."""
    source, worktree = _roots(tmp_path)

    with pytest.raises(WorktreeBootstrapError, match="command not found"):
        run_worktree_bootstrap(
            [{"run": ["definitely-not-a-real-binary-xyz"]}],
            source_root=source,
            worktree_path=worktree,
        )


# --- _resolve_command_cwd branches (lines 359-360, 361-363, 369)


def test_cwd_source_runs_in_source_root(tmp_path: Path) -> None:
    """Branch 359-360: cwd:'source' runs the command in source_root."""
    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    source.mkdir()
    worktree.mkdir()

    result = run_worktree_bootstrap(
        [{
            "run": [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('here.ok').write_text('ok')",
            ],
            "cwd": "source",
        }],
        source_root=source,
        worktree_path=worktree,
    )

    assert result["steps"][0]["cwd"] == str(source.resolve())
    assert (source / "here.ok").read_text(encoding="utf-8") == "ok"


def test_custom_relative_cwd_resolves_under_worktree(tmp_path: Path) -> None:
    """Branch 363: a custom relative cwd is resolved beneath the worktree."""
    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    source.mkdir()
    (worktree / "sub").mkdir(parents=True)

    result = run_worktree_bootstrap(
        [{
            "run": [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('rel.ok').write_text('ok')",
            ],
            "cwd": "sub",
        }],
        source_root=source,
        worktree_path=worktree,
    )

    assert result["steps"][0]["cwd"] == str((worktree / "sub").resolve())
    assert (worktree / "sub" / "rel.ok").read_text(encoding="utf-8") == "ok"


@pytest.mark.parametrize("bad_cwd", ["   ", 9])
def test_cwd_must_be_a_string(tmp_path: Path, bad_cwd) -> None:
    """Branch 361-362: an empty/non-string cwd -> 'cwd must be a string'."""
    source, worktree = _roots(tmp_path)

    with pytest.raises(WorktreeBootstrapError, match="cwd must be a string"):
        run_worktree_bootstrap(
            [{"run": [sys.executable, "-c", "pass"], "cwd": bad_cwd}],
            source_root=source,
            worktree_path=worktree,
        )


def test_absolute_path_must_be_relative(tmp_path: Path) -> None:
    """Branch 369: an absolute copy source -> 'must be relative'."""
    source, worktree = _roots(tmp_path)

    with pytest.raises(WorktreeBootstrapError, match="must be relative"):
        run_worktree_bootstrap(
            [{"copy": str(tmp_path / "abs")}],
            source_root=source,
            worktree_path=worktree,
        )


# --- _timeout branches (lines 384-385, 389)


def test_timeout_must_be_an_integer(tmp_path: Path) -> None:
    """Branch 384-385: a non-coercible timeout -> 'must be an integer'."""
    source, worktree = _roots(tmp_path)

    with pytest.raises(WorktreeBootstrapError, match="must be an integer"):
        run_worktree_bootstrap(
            [{"run": [sys.executable, "-c", "pass"], "timeout": "x"}],
            source_root=source,
            worktree_path=worktree,
        )


def test_timeout_must_be_positive(tmp_path: Path) -> None:
    """Branch 389: a non-positive timeout -> 'must be positive'."""
    source, worktree = _roots(tmp_path)

    with pytest.raises(WorktreeBootstrapError, match="must be positive"):
        run_worktree_bootstrap(
            [{"run": [sys.executable, "-c", "pass"], "timeout": 0}],
            source_root=source,
            worktree_path=worktree,
        )


# --- _platform_matches branches (lines 400, 404)


def test_string_platforms_matching_current_runs_step(tmp_path: Path) -> None:
    """Branch 400: a string 'platforms' for the current platform runs."""
    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    (source / "libs").mkdir(parents=True)
    worktree.mkdir()
    (source / "libs" / "a.txt").write_text("z", encoding="utf-8")

    result = run_worktree_bootstrap(
        [{"copy": "libs", "platforms": sys.platform}],
        source_root=source,
        worktree_path=worktree,
    )

    assert result["steps"][0]["status"] == "ok"
    assert (worktree / "libs" / "a.txt").read_text(encoding="utf-8") == "z"


@pytest.mark.parametrize("bad_platforms", [7, {"os": "linux"}])
def test_platforms_invalid_type_raises(tmp_path: Path, bad_platforms) -> None:
    """Branch 404: a non-str/non-list 'platforms' -> 'string or list'."""
    source, worktree = _roots(tmp_path)

    with pytest.raises(
        WorktreeBootstrapError, match="must be a string or list",
    ):
        run_worktree_bootstrap(
            [{"copy": "libs", "platforms": bad_platforms}],
            source_root=source,
            worktree_path=worktree,
        )


# --- _shell_step real subprocess success + failure (lines 266-284, 289-293)


def test_shell_step_creates_file_and_reports_ok(tmp_path: Path) -> None:
    """Branch 266-284: a successful shell step runs and returns status ok."""
    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    source.mkdir()
    worktree.mkdir()

    result = run_worktree_bootstrap(
        [{"shell": "printf hi > shell-made.txt"}],
        source_root=source,
        worktree_path=worktree,
    )

    assert result["steps"][0] == {
        "index": 1,
        "action": "shell",
        "status": "ok",
        "cwd": str(worktree.resolve()),
    }
    assert (worktree / "shell-made.txt").read_text(encoding="utf-8") == "hi"


@pytest.mark.parametrize("bad_cmd", ["", "   "])
def test_shell_step_requires_command_string(tmp_path: Path, bad_cmd) -> None:
    """Branch 268: an empty shell command -> 'needs a command string'."""
    source, worktree = _roots(tmp_path)

    with pytest.raises(WorktreeBootstrapError, match="needs a command string"):
        run_worktree_bootstrap(
            [{"shell": bad_cmd}],
            source_root=source,
            worktree_path=worktree,
        )


def test_shell_step_nonzero_exit_raises_without_output(tmp_path: Path) -> None:
    """Branch 289-293: a non-zero shell exit -> 'failed with exit code'."""
    source, worktree = _roots(tmp_path)

    with pytest.raises(WorktreeBootstrapError) as excinfo:
        run_worktree_bootstrap(
            [{"shell": "echo leaked-shell-output; exit 5"}],
            source_root=source,
            worktree_path=worktree,
        )

    message = str(excinfo.value)
    assert "failed with exit code 5" in message
    assert "leaked-shell-output" not in message


# --- timeout lifecycle branches (lines 285-288 shell, 339-342 run)


@pytest.mark.slow_process
@pytest.mark.serial
def test_shell_step_timeout_raises(tmp_path: Path) -> None:
    """Branch 285-288: a shell step exceeding its timeout -> 'timed out'."""
    source, worktree = _roots(tmp_path)

    with pytest.raises(WorktreeBootstrapError, match="timed out"):
        run_worktree_bootstrap(
            [{"shell": f"{shlex.quote(sys.executable)} -c "
                       "'import time; time.sleep(10)'", "timeout": 1}],
            source_root=source,
            worktree_path=worktree,
        )


@pytest.mark.slow_process
@pytest.mark.serial
def test_run_step_timeout_raises(tmp_path: Path) -> None:
    """Branch 339-342: a run step exceeding its timeout -> 'timed out'."""
    source, worktree = _roots(tmp_path)

    with pytest.raises(WorktreeBootstrapError, match="timed out"):
        run_worktree_bootstrap(
            [{
                "run": [sys.executable, "-c", "import time; time.sleep(10)"],
                "timeout": 1,
            }],
            source_root=source,
            worktree_path=worktree,
        )
