from __future__ import annotations

import shlex
import shutil
import subprocess
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Protocol

from sdk.workspace import init_workspace


class DemoBootstrapError(ValueError):
    """Raised when a packaged demo cannot be bootstrapped safely."""


class ResourceNode(Protocol):
    name: str

    def is_dir(self) -> bool: ...

    def is_file(self) -> bool: ...

    def iterdir(self): ...

    def read_bytes(self) -> bytes: ...


@dataclass(frozen=True, slots=True)
class DemoBootstrapResult:
    demo: str
    root: Path
    project_dir: Path
    workspace_dir: Path
    source_label: str


_DEMO_ASSETS = {
    "golden-api": {
        "asset_dir": "golden-api",
        "default_root": Path("/tmp/orcho_demo_1a"),
        "sentinel": ".orcho-demo-1a",
    },
}


def demo_names() -> tuple[str, ...]:
    return tuple(sorted(_DEMO_ASSETS))


def bootstrap_demo(
    demo: str,
    *,
    root: Path | None = None,
) -> DemoBootstrapResult:
    spec = _validate_demo(demo)
    demo_root = (root or spec["default_root"]).expanduser()
    project_dir = demo_root / "project"
    workspace_dir = demo_root / "workspace-orchestrator"
    sentinel = demo_root / spec["sentinel"]

    _prepare_demo_root(demo_root, sentinel)
    fixture_src = _asset_root(spec["asset_dir"])

    demo_root.mkdir(parents=True, exist_ok=True)
    _copy_resource_tree(fixture_src, project_dir)
    _write_demo_plugin(project_dir)
    _init_project_git(project_dir)
    init_workspace(demo_root)
    sentinel.touch()

    return DemoBootstrapResult(
        demo=demo,
        root=demo_root,
        project_dir=project_dir,
        workspace_dir=workspace_dir,
        source_label=f"packaged demo asset: {demo}",
    )


def render_demo_bootstrap(result: DemoBootstrapResult) -> str:
    project_arg = shlex.quote(str(result.project_dir))
    workspace_arg = shlex.quote(str(result.workspace_dir))
    return f"""DEMO golden-api workspace ready.

  Project (copy):  {result.project_dir}
  Workspace:       {result.workspace_dir}
  Source fixture:  {result.source_label}

Run the pipeline:

  orcho run \\
    --task "Fix validation bug in sample API" \\
    --project {project_arg} \\
    --workspace {workspace_arg} \\
    --profile feature \\
    --mock \\
    --mock-validate-plan-reject 1 \\
    --max-rounds 2 \\
    --stream-output

Inspect the run:

  orcho evidence --format md --workspace {workspace_arg}
  orcho status --workspace {workspace_arg}
  orcho diff <run-id> --stat --workspace {workspace_arg}
  orcho metrics --workspace {workspace_arg}
"""


def _validate_demo(demo: str) -> dict:
    try:
        return _DEMO_ASSETS[demo]
    except KeyError as exc:
        names = ", ".join(demo_names())
        raise DemoBootstrapError(f"unknown demo {demo!r}; available: {names}") from exc


def _asset_root(asset_dir: str) -> ResourceNode:
    root = resources.files("core").joinpath("_demo_assets", asset_dir)
    if not root.is_dir():
        raise DemoBootstrapError(f"packaged demo asset is missing: {asset_dir}")
    return root


def _prepare_demo_root(demo_root: Path, sentinel: Path) -> None:
    if not str(demo_root) or demo_root == Path("/"):
        raise DemoBootstrapError(f"refusing to operate on demo root {demo_root!s}")

    if demo_root.exists():
        if sentinel.is_file():
            shutil.rmtree(demo_root)
        else:
            raise DemoBootstrapError(
                f"{demo_root} exists but is not an Orcho demo directory; "
                "remove it manually or pass --root to use a different path"
            )


def _copy_resource_tree(source: ResourceNode, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        child_dest = destination / child.name
        if child.is_dir():
            _copy_resource_tree(child, child_dest)
        elif child.is_file():
            child_dest.parent.mkdir(parents=True, exist_ok=True)
            child_dest.write_bytes(child.read_bytes())


def _init_project_git(project_dir: Path) -> None:
    commands = (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "demo@example.invalid"),
        ("git", "config", "user.name", "Orcho Demo"),
        ("git", "add", "."),
        ("git", "commit", "-q", "-m", "Initial demo fixture"),
    )
    for cmd in commands:
        subprocess.run(cmd, cwd=project_dir, check=True)


def _write_demo_plugin(project_dir: Path) -> None:
    plugin_dir = project_dir / ".orcho" / "multiagent"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.py").write_text(
        """PLUGIN = {
    "name": "Golden API Demo",
    "language": "Python",
    "architecture": "Tiny validation module plus pytest tests",
    "file_hints": ["app/__init__.py", "app/validation.py", "tests/__init__.py"],
}
""",
        encoding="utf-8",
    )
