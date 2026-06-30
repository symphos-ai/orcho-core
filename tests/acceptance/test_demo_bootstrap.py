"""tests/acceptance/test_demo_bootstrap.py — DEMO-1A bootstrap script.

Pins the contract of ``examples/scripts/bootstrap_demo_1a.sh``:

* It exists and is executable.
* It creates a disposable ``project/`` (copy of the fixture) and runs
  ``orcho workspace init`` to create ``workspace-orchestrator/``.
* It never mutates the source fixture under
  ``examples/golden-api/``.
* It is idempotent on its own demo dir (sentinel-gated wipe).
* It refuses to wipe a target dir that lacks the sentinel — so a
  stray ``ORCHO_DEMO_ROOT`` pointing at live data is safe.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parents[2]
SCRIPT = CORE_DIR / "examples" / "scripts" / "bootstrap_demo_1a.sh"
FIXTURE = CORE_DIR / "examples" / "golden-api"


def _run(demo_root: Path, *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(SCRIPT)],
        cwd=CORE_DIR,
        env={**os.environ, "ORCHO_DEMO_ROOT": str(demo_root)},
        capture_output=True,
        text=True,
        check=check,
    )


def _snapshot(d: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(d.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(d))] = hashlib.md5(p.read_bytes()).hexdigest()
    return out


def _git_status(repo: Path) -> str:
    return subprocess.check_output(
        ["git", "status", "--short", "--untracked-files=all"],
        cwd=repo,
        text=True,
    )


class TestBootstrapDemo1A:
    def test_script_exists_and_is_executable(self) -> None:
        assert SCRIPT.is_file(), f"missing: {SCRIPT}"
        assert os.access(SCRIPT, os.X_OK), "script must be executable"

    def test_creates_project_copy_and_workspace_init(
        self, tmp_path: Path,
    ) -> None:
        demo_root = tmp_path / "demo"
        result = _run(demo_root)

        assert (demo_root / "project").is_dir()
        assert (demo_root / "workspace-orchestrator").is_dir()
        # Sentinel is what gates the next idempotent wipe.
        assert (demo_root / ".orcho-demo-1a").is_file()
        # Project copy carries the fixture's distinguishing files.
        assert (demo_root / "project" / "pyproject.toml").is_file()
        assert (demo_root / "project" / "app" / "validation.py").is_file()
        assert (
            demo_root / "project" / ".orcho" / "multiagent" / "plugin.py"
        ).is_file()
        # The public copy-paste demo must be a real git repo so the default
        # worktree-isolated run can review and capture diffs.
        assert (demo_root / "project" / ".git").is_dir()
        assert _git_status(demo_root / "project") == ""
        # Workspace starts with the no-op local config scaffold.
        local_config = (
            demo_root / "workspace-orchestrator" / ".orcho" / "config.local.json"
        )
        assert local_config.is_file()
        local_data = json.loads(local_config.read_text(encoding="utf-8"))
        assert local_data["phases"]["implement"]["model"]
        # Stdout includes the copy-pastable run + inspect commands.
        assert "orcho run" in result.stdout
        assert "orcho evidence" in result.stdout
        assert "orcho diff <run-id> --stat" in result.stdout
        assert str(demo_root / "project") in result.stdout

    def test_printed_commands_quote_paths_with_spaces(
        self, tmp_path: Path,
    ) -> None:
        demo_root = tmp_path / "demo root"
        result = _run(demo_root)
        command_block = result.stdout.split("Run the pipeline:", 1)[1]

        assert (demo_root / "project").is_dir()
        assert "--project " in command_block
        assert str(demo_root / "project") not in command_block
        assert str(demo_root / "project").replace(" ", "\\ ") in command_block
        assert (
            str(demo_root / "workspace-orchestrator").replace(" ", "\\ ")
            in command_block
        )

    def test_does_not_mutate_source_fixture(self, tmp_path: Path) -> None:
        before = _snapshot(FIXTURE)
        _run(tmp_path / "demo")
        after = _snapshot(FIXTURE)
        assert before == after, "source fixture must remain untouched"

    def test_idempotent_when_sentinel_present(self, tmp_path: Path) -> None:
        demo_root = tmp_path / "demo"
        _run(demo_root)
        # Drop a stale file inside the workspace; the second run must
        # wipe it because the sentinel marks the dir as ours.
        stale = demo_root / "workspace-orchestrator" / "stale.txt"
        stale.write_text("from-previous-run")
        _run(demo_root)
        assert not stale.exists()
        assert (
            demo_root / "workspace-orchestrator" / ".orcho" / "config.local.json"
        ).is_file()
        assert (demo_root / "project").is_dir()

    def test_refuses_to_wipe_dir_without_sentinel(
        self, tmp_path: Path,
    ) -> None:
        demo_root = tmp_path / "live"
        demo_root.mkdir()
        sacred = demo_root / "user_data.txt"
        sacred.write_text("important — keep me")
        # No sentinel → script must refuse rather than rm -rf.
        result = _run(demo_root, check=False)
        assert result.returncode != 0
        assert sacred.exists()
        assert sacred.read_text() == "important — keep me"
        assert "Refusing to wipe" in result.stderr
