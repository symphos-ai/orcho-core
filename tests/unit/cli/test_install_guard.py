from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from cli.install_guard import guard_against_retained_worktree_install


def _retained_checkout(tmp_path: Path) -> Path:
    return (
        tmp_path
        / "workspace-orchestrator"
        / "runspace"
        / "worktrees"
        / "wt_20260615_120000"
        / "checkout"
    )


def test_global_entrypoint_into_retained_worktree_stops(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    checkout = _retained_checkout(tmp_path)
    checkout.mkdir(parents=True)

    with pytest.raises(SystemExit) as exc:
        guard_against_retained_worktree_install(
            checkout,
            argv0="/venv/bin/orcho",
            env={},
        )

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "ORCHO_RETAINED_WORKTREE_INSTALL" in err
    assert str(checkout) in err
    assert "python -m pip show orcho-core" in err
    assert "python -m cli.orcho --help" in err


def test_module_run_inside_retained_worktree_is_allowed(
    tmp_path: Path,
) -> None:
    checkout = _retained_checkout(tmp_path)
    cli_file = checkout / "cli" / "orcho.py"
    cli_file.parent.mkdir(parents=True)
    cli_file.write_text("# synthetic module entrypoint\n", encoding="utf-8")

    guard_against_retained_worktree_install(
        checkout,
        argv0=str(cli_file),
        env={},
    )


def test_pytest_import_inside_retained_worktree_is_allowed(
    tmp_path: Path,
) -> None:
    """Importing the package under a test runner must never trip the guard.

    pytest collection imports ``cli.orcho`` from inside the retained checkout
    while ``sys.argv[0]`` points at the ``pytest`` binary outside it. That is a
    legitimate in-checkout test run, not a stale global console-script leak.
    """
    checkout = _retained_checkout(tmp_path)
    checkout.mkdir(parents=True)

    guard_against_retained_worktree_install(
        checkout,
        argv0="/venv/bin/pytest",
        env={},
    )


def test_python_dash_m_invocation_is_allowed(tmp_path: Path) -> None:
    checkout = _retained_checkout(tmp_path)
    checkout.mkdir(parents=True)

    guard_against_retained_worktree_install(
        checkout,
        argv0="/usr/bin/python3.12",
        env={},
    )


def test_regular_checkout_is_allowed(tmp_path: Path) -> None:
    checkout = tmp_path / "orcho-core"
    checkout.mkdir()

    guard_against_retained_worktree_install(
        checkout,
        argv0="/venv/bin/orcho",
        env={},
    )


def test_env_bypass_is_allowed(tmp_path: Path) -> None:
    checkout = _retained_checkout(tmp_path)
    checkout.mkdir(parents=True)

    guard_against_retained_worktree_install(
        checkout,
        argv0="/venv/bin/orcho",
        env={"ORCHO_ALLOW_RETAINED_WORKTREE_INSTALL": "1"},
    )


def test_console_entrypoint_importing_retained_worktree_stops(
    tmp_path: Path,
) -> None:
    checkout = _retained_checkout(tmp_path)
    fake_cli = checkout / "cli"
    fake_cli.mkdir(parents=True)
    repo_root = Path(__file__).resolve().parents[3]
    shutil.copy2(repo_root / "cli" / "orcho.py", fake_cli / "orcho.py")
    shutil.copy2(
        repo_root / "cli" / "install_guard.py",
        fake_cli / "install_guard.py",
    )

    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    entrypoint = bin_dir / "orcho"
    entrypoint.write_text(
        "from cli.orcho import main\n"
        "raise SystemExit(main())\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["PYTHONPATH"] = str(checkout)
    result = subprocess.run(
        [sys.executable, str(entrypoint), "--help"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "ORCHO_RETAINED_WORKTREE_INSTALL" in result.stderr
    assert str(checkout) in result.stderr
    assert "python -m pip show orcho-core" in result.stderr
    assert "Traceback" not in result.stderr
