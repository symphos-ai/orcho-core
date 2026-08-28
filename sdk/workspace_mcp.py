"""Read and write helpers for an Orcho workspace's MCP client setup.

This focused module owns the MCP server identity, JSON snippet construction,
optional config-file merge, and read-only resolution used by
``orcho workspace mcp``.  Workspace bootstrap remains responsible for creating
the workspace itself; this module never materializes a workspace.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from sdk.errors import NoWorkspace, WorkspaceInitError
from sdk.runtimes import DetectedRuntime, detect_cli_runtimes
from sdk.workspace_paths import infer_workspace_from_project

_SLUG_RE: Final[re.Pattern[str]] = re.compile(r"[^a-zA-Z0-9]+")
_RUNS_RELATIVE_PATH: Final[Path] = Path("runspace") / "runs"


@dataclass(frozen=True, slots=True)
class WorkspaceMcpSetup:
    """Read-only MCP setup information for one active workspace."""

    workspace_dir: str
    mcp_server_name: str
    mcp_snippet: dict
    detected_runtimes: tuple[DetectedRuntime, ...]


def default_server_name(name: str) -> str:
    """Derive a stable ``orcho-<slug>`` name from a human name."""
    slug = _SLUG_RE.sub("-", name.strip()).strip("-").lower()
    if not slug:
        return "orcho"
    if slug == "orcho" or slug.startswith("orcho-"):
        return slug
    return f"orcho-{slug}"


def build_mcp_snippet(
    *,
    server_name: str,
    workspace_dir: Path,
    orcho_mcp_command: str,
) -> dict:
    """Build the portable JSON client entry for ``workspace_dir``."""
    return {
        "mcpServers": {
            server_name: {
                "command": orcho_mcp_command,
                "args": [],
                "env": {"ORCHO_WORKSPACE": str(workspace_dir)},
            },
        },
    }


def build_workspace_mcp_setup(
    *,
    workspace: Path | str | None = None,
    mcp_server_name: str | None = None,
    orcho_mcp_command: str = "orcho-mcp",
    cwd: Path | str | None = None,
) -> WorkspaceMcpSetup:
    """Resolve an active workspace and build its MCP client setup.

    Resolution intentionally prefers a workspace bound to the current project
    over ``$ORCHO_WORKSPACE``.  That avoids emitting a random ambient
    workspace while working inside a different registered project.  No paths
    are created or modified.
    """
    cwd_path = Path.cwd() if cwd is None else Path(cwd).expanduser()
    resolved = _resolve_active_workspace(workspace=workspace, cwd=cwd_path)
    server_name = mcp_server_name or default_server_name(
        _workspace_identity_name(resolved, cwd_path)
    )
    return WorkspaceMcpSetup(
        workspace_dir=str(resolved),
        mcp_server_name=server_name,
        mcp_snippet=build_mcp_snippet(
            server_name=server_name,
            workspace_dir=resolved,
            orcho_mcp_command=orcho_mcp_command,
        ),
        detected_runtimes=detect_cli_runtimes(),
    )


def apply_mcp_config(
    path: Path,
    *,
    server_name: str,
    server_entry: dict,
    force: bool,
    dry_run: bool,
) -> str:
    """Merge ``server_entry`` into ``mcpServers[server_name]`` of ``path``.

    Returns ``wrote``, ``merged``, ``no-op``, or ``replaced``.  The merge and
    replacement rules are kept byte-for-byte compatible with workspace init.
    """
    if not path.exists():
        parent = path.parent
        if not parent.is_dir():
            raise WorkspaceInitError(f"parent directory does not exist: {parent}")
        if not dry_run:
            path.write_text(
                json.dumps(
                    {"mcpServers": {server_name: server_entry}},
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
        return "wrote"

    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspaceInitError(f"could not parse existing MCP config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkspaceInitError(f"existing MCP config {path} is not a JSON object")

    servers = data.get("mcpServers")
    if servers is None:
        servers = {}
        data["mcpServers"] = servers
    elif not isinstance(servers, dict):
        raise WorkspaceInitError(f"existing 'mcpServers' in {path} is not a JSON object")

    existing = servers.get(server_name)
    if existing is None:
        servers[server_name] = server_entry
        action = "merged"
    elif existing == server_entry:
        return "no-op"
    elif force:
        servers[server_name] = server_entry
        action = "replaced"
    else:
        raise WorkspaceInitError(
            f"{path}: 'mcpServers.{server_name}' already exists with a "
            "different value. Re-run with --force to replace just that "
            "entry, or pick a different --mcp-server-name."
        )

    if not dry_run:
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return action


def _resolve_active_workspace(
    *,
    workspace: Path | str | None,
    cwd: Path,
) -> Path:
    if workspace is not None:
        candidate = Path(workspace).expanduser()
        return _require_active_workspace(candidate, source="Provided workspace")

    project_bound = infer_workspace_from_project(cwd)
    if project_bound is not None:
        return _require_active_workspace(project_bound, source="Project-bound workspace")

    ambient = os.environ.get("ORCHO_WORKSPACE")
    if ambient:
        return _require_active_workspace(Path(ambient).expanduser(), source="$ORCHO_WORKSPACE")

    raise NoWorkspace(
        "Could not resolve an active workspace from the current project, cwd, "
        "or $ORCHO_WORKSPACE. Pass --workspace PATH or run `orcho workspace init`."
    )


def _require_active_workspace(candidate: Path, *, source: str) -> Path:
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as exc:
        raise NoWorkspace(f"{source} could not be resolved: {candidate}") from exc
    if not (resolved / _RUNS_RELATIVE_PATH).is_dir():
        raise NoWorkspace(
            f"{source} has no runspace/runs/: {resolved}. Run `orcho workspace init` first."
        )
    return resolved


def _workspace_identity_name(workspace_dir: Path, cwd: Path) -> str:
    """Choose a useful default name without consulting ambient workspaces."""
    try:
        cwd_resolved = cwd.resolve()
        if cwd_resolved != workspace_dir:
            return cwd_resolved.name
    except (OSError, RuntimeError):
        pass
    return (
        workspace_dir.parent.name
        if workspace_dir.name == "workspace-orchestrator"
        else workspace_dir.name
    )


__all__ = [
    "WorkspaceMcpSetup",
    "apply_mcp_config",
    "build_mcp_snippet",
    "build_workspace_mcp_setup",
    "default_server_name",
]
