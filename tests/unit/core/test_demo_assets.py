from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import pytest

from core.infra.demo_assets import (
    DemoBootstrapError,
    bootstrap_demo,
    demo_names,
    render_demo_bootstrap,
)


def _git_status(repo: Path) -> str:
    return subprocess.check_output(
        ["git", "status", "--short", "--untracked-files=all"],
        cwd=repo,
        text=True,
    )


class TestPackagedDemoBootstrap:
    def test_demo_names_include_golden_api(self) -> None:
        assert demo_names() == ("golden-api",)

    def test_bootstrap_creates_disposable_project_and_workspace(
        self,
        tmp_path: Path,
    ) -> None:
        result = bootstrap_demo("golden-api", root=tmp_path / "demo root")

        assert result.project_dir.is_dir()
        assert result.workspace_dir.is_dir()
        assert (result.root / ".orcho-demo-1a").is_file()
        assert (result.project_dir / "app" / "validation.py").is_file()
        assert (result.project_dir / ".orcho" / "multiagent" / "plugin.py").is_file()
        assert (result.project_dir / ".git").is_dir()
        assert _git_status(result.project_dir) == ""

        local_config = result.workspace_dir / ".orcho" / "config.local.json"
        assert local_config.is_file()
        assert json.loads(local_config.read_text(encoding="utf-8"))["phases"][
            "implement"
        ]["model"]

    def test_rendered_commands_quote_paths_with_spaces(self, tmp_path: Path) -> None:
        result = bootstrap_demo("golden-api", root=tmp_path / "demo root")
        out = render_demo_bootstrap(result)

        assert "--project " in out
        assert "--profile feature" in out
        assert shlex.quote(str(result.project_dir)) in out
        assert shlex.quote(str(result.workspace_dir)) in out

    def test_refuses_to_wipe_without_sentinel(self, tmp_path: Path) -> None:
        demo_root = tmp_path / "live"
        demo_root.mkdir()
        sacred = demo_root / "user_data.txt"
        sacred.write_text("important", encoding="utf-8")

        with pytest.raises(DemoBootstrapError, match="not an Orcho demo directory"):
            bootstrap_demo("golden-api", root=demo_root)

        assert sacred.read_text(encoding="utf-8") == "important"

    def test_unknown_demo_names_available_demos(self) -> None:
        with pytest.raises(DemoBootstrapError, match="available: golden-api"):
            bootstrap_demo("missing")
