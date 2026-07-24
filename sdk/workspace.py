"""sdk/workspace.py — workspace bootstrap.

:func:`init_workspace` lays the minimum filesystem rails Orcho needs
against a *project-group* directory: a directory that holds one or
more project repositories side-by-side. The classic shape::

    ~/www/my-org/
        proj-a/              ← user's project repo
        proj-b/              ← user's project repo
        workspace-orchestrator/   ← created by this function
            runspace/
                runs/
            .orcho/
                multiagent/
                    prompts/
            orcho-env.sh

It also detects child project repos one level deep, records them as
workspace-local project aliases, and optionally prints an MCP-config
snippet or merges it into a `.mcp.json` file.

Idempotent and safe:

* refuses ``/`` and the user home directory exactly;
* refuses a target that itself looks like an individual project repo
  (``.git`` or one of the canonical manifest files at the root)
  unless ``force=True`` — the command expects a *group root*, not a
  single project;
* never deletes;
* never overwrites a file destructively (rewrites only when content
  is byte-identical or the file is an Orcho-owned key inside a
  ``.mcp.json`` merge with explicit ``force=True``);
* repeats cleanly — re-running on an already-initialised target
  emits zero new paths but still returns the expected
  :class:`WorkspaceInitResult`.

This module is intentionally side-effect-light at import time. All
filesystem work happens inside :func:`init_workspace`.
"""
from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from sdk.errors import WorkspaceInitError
from sdk.runtimes import (
    DetectedRuntime,
    assess_runtime_availability,
    detect_cli_runtimes,
    runtime_installed,
)
from sdk.workspace_scaffold import scaffold_workspace_extensions

# ─── Constants ──────────────────────────────────────────────────────────────


#: Subdir name created under the project-group root. The user does not
#: have to know this string — the function returns the full path.
_WORKSPACE_SUBDIR: Final[str] = "workspace-orchestrator"

#: Subpath of the runtime state directory inside the workspace.
_RUNSPACE_SUBDIR: Final[str] = "runspace"
_RUNS_SUBDIR: Final[str] = "runs"

#: Workspace-local config directory. The personal file is read through
#: ``$ORCHO_WORKSPACE/.orcho/config.local.json`` after shared ``config.json``.
_ORCHO_CONFIG_DIR: Final[str] = ".orcho"
_LOCAL_CONFIG_FILE: Final[str] = "config.local.json"

#: Name of the bash file that exports ``ORCHO_WORKSPACE`` /
#: ``ORCHO_RUNSPACE`` for the user's shell.
_ENV_FILE: Final[str] = "orcho-env.sh"

#: Markers used to recognise an individual project repository.
_PROJECT_REPO_MARKERS: Final[frozenset[str]] = frozenset({
    ".git",
    "pyproject.toml",
    "package.json",
    "composer.json",
    "go.mod",
    "Cargo.toml",
})

#: Subdirectories of the group root we never report as projects.
_EXCLUDED_CHILD_NAMES: Final[frozenset[str]] = frozenset({
    _WORKSPACE_SUBDIR,
    "node_modules",
    ".venv",
    ".git",
    "__pycache__",
    ".idea",
    ".vscode",
})


# ─── Dataclasses ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DetectedProject:
    """One child project repo discovered under the group root."""

    name: str
    path: str


@dataclass(frozen=True, slots=True)
class UndetectedCandidate:
    """A child folder that was NOT auto-detected as a project.

    Carries pre-scanned ``nested_git_dirs``: relative paths (shallowest
    first) to directories or files named ``.git`` found inside the folder.
    """

    name: str
    path: str
    nested_git_dirs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExtraProject:
    """A project interactively confirmed by the user during ``workspace init``.

    ``git_dir`` is the relative path from ``path`` to the actual git repo
    root (e.g. ``"UnityProj"`` when the repo is at ``path/UnityProj/.git``).
    Empty string means the project root itself is (or will be) the git root.
    """

    name: str
    path: str
    git_dir: str = field(default="")


@dataclass(frozen=True, slots=True)
class WorkspaceInitResult:
    """Outcome of an :func:`init_workspace` call.

    Field semantics:

    ``created_paths`` — paths the function would create on this run
    (or did create when ``dry_run=False``). Empty on a repeat call
    against an already-initialised target.

    ``skipped_paths`` — paths that already existed in the right shape
    and required no work.

    ``warnings`` — non-fatal observations the caller should surface
    to the user (for example, an existing ``orcho-env.sh`` whose
    content differs and was therefore not overwritten).

    ``mcp_snippet`` is always populated — it is the JSON object a
    user can paste into any ``.mcp.json``. ``mcp_config_path`` /
    ``mcp_config_action`` are set only when the caller asked the
    function to write to a file.

    ``missing_runtimes`` — configured per-phase runtime ids whose CLI
    executable was not found on PATH at init time (pre-switch view).
    ``runtime_override`` echoes the runtime the workspace config was
    switched to, or ``None`` when no switch was requested/needed.
    """

    group_root: str
    workspace_dir: str
    runs_dir: str
    env_file: str
    local_config_file: str
    detected_projects: tuple[DetectedProject, ...]
    created_paths: tuple[str, ...]
    skipped_paths: tuple[str, ...]
    warnings: tuple[str, ...]
    mcp_server_name: str
    mcp_snippet: dict
    mcp_config_path: str | None
    mcp_config_action: str
    dry_run: bool
    detected_runtimes: tuple[DetectedRuntime, ...] = ()
    extra_projects: tuple[ExtraProject, ...] = ()
    undetected_count: int = 0
    interactive: bool = False
    extension_points: tuple[str, ...] = ()
    missing_runtimes: tuple[str, ...] = ()
    runtime_override: str | None = None


# ─── Public entry point ─────────────────────────────────────────────────────


def init_workspace(
    project_group_root: Path | str,
    *,
    workspace_name: str | None = None,
    mcp_config: Path | str | None = None,
    mcp_server_name: str | None = None,
    orcho_mcp_command: str = "orcho-mcp",
    force: bool = False,
    dry_run: bool = False,
    extra_projects: Sequence[ExtraProject] = (),
    undetected_count: int = 0,
    interactive: bool = False,
    no_scaffold: bool = False,
    runtime_override: str | None = None,
) -> WorkspaceInitResult:
    """Initialise an Orcho workspace under ``project_group_root``.

    See module docstring for full semantics. Raises
    :class:`sdk.errors.WorkspaceInitError` on refused targets or
    config conflicts.

    ``runtime_override`` switches every configured phase whose runtime
    executable is not on PATH over to the given (installed) runtime in
    the personal workspace-local config — both when the file is first created
    and when it already exists.
    """
    group_root = _coerce_group_root(project_group_root, dry_run=dry_run)
    _refuse_unsafe_root(group_root)
    _refuse_repo_root(group_root, force=force)

    # Pre-switch availability view — recorded on the result so callers
    # can render "runtime X missing" / "switched to Y" without probing
    # PATH again.
    availability = assess_runtime_availability(
        planned_phase_runtimes(group_root).values()
    )

    workspace_dir = group_root / _WORKSPACE_SUBDIR
    runspace_dir = workspace_dir / _RUNSPACE_SUBDIR
    runs_dir = runspace_dir / _RUNS_SUBDIR
    local_config_dir = workspace_dir / _ORCHO_CONFIG_DIR
    local_config_file = local_config_dir / _LOCAL_CONFIG_FILE
    env_file = workspace_dir / _ENV_FILE

    created: list[Path] = []
    skipped: list[Path] = []
    warnings: list[str] = []

    # Directories — additive.
    for d in (
        workspace_dir,
        runspace_dir,
        runs_dir,
        local_config_dir,
    ):
        if d.is_dir():
            skipped.append(d)
        else:
            created.append(d)
            if not dry_run:
                d.mkdir(parents=True, exist_ok=True)

    # Env script — non-destructive write.
    env_action = _write_env_file(env_file, dry_run=dry_run)
    if env_action == "created":
        created.append(env_file)
    elif env_action == "identical":
        skipped.append(env_file)
    elif env_action == "differs":
        warnings.append(
            f"existing {env_file.name} differs from the generated template; "
            "leaving it untouched. Delete it and re-run to regenerate."
        )
        skipped.append(env_file)

    extension_points: tuple[str, ...] = ()
    if not no_scaffold:
        scaffold = scaffold_workspace_extensions(workspace_dir, dry_run=dry_run)
        created.extend(scaffold.created_paths)
        skipped.extend(scaffold.skipped_paths)
        warnings.extend(scaffold.warnings)
        extension_points = scaffold.extension_points

    detected = _detect_projects(group_root)

    # Workspace-local config — non-destructive scaffold.
    config_action = _write_workspace_local_config(
        local_config_file,
        detected_projects=detected,
        extra_projects=list(extra_projects),
        dry_run=dry_run,
        runtime_override=runtime_override,
    )
    if config_action == "created":
        created.append(local_config_file)
    elif config_action in {"exists", "updated"}:
        skipped.append(local_config_file)
    elif config_action == "blocked":
        warnings.append(
            f"existing {local_config_file} is not a file; leaving it untouched."
        )
        skipped.append(local_config_file)

    # MCP snippet — always computed, optionally written.
    server_name = (
        mcp_server_name
        or _default_server_name(workspace_name or group_root.name)
    )
    snippet = _build_mcp_snippet(
        server_name=server_name,
        workspace_dir=workspace_dir,
        orcho_mcp_command=orcho_mcp_command,
    )

    mcp_config_path: Path | None = None
    mcp_config_action = "printed"  # default: snippet on stdout only
    if mcp_config is not None:
        mcp_config_path = Path(mcp_config).expanduser()
        mcp_config_action = _apply_mcp_config(
            mcp_config_path,
            server_name=server_name,
            server_entry=snippet["mcpServers"][server_name],
            force=force,
            dry_run=dry_run,
        )

    return WorkspaceInitResult(
        group_root=str(group_root),
        workspace_dir=str(workspace_dir),
        runs_dir=str(runs_dir),
        env_file=str(env_file),
        local_config_file=str(local_config_file),
        detected_projects=tuple(detected),
        created_paths=tuple(str(p) for p in created),
        skipped_paths=tuple(str(p) for p in skipped),
        warnings=tuple(warnings),
        mcp_server_name=server_name,
        mcp_snippet=snippet,
        mcp_config_path=str(mcp_config_path) if mcp_config_path else None,
        mcp_config_action=mcp_config_action,
        dry_run=dry_run,
        detected_runtimes=detect_cli_runtimes(),
        extra_projects=tuple(extra_projects),
        undetected_count=undetected_count,
        interactive=interactive,
        extension_points=extension_points,
        missing_runtimes=availability.missing_runtimes,
        runtime_override=(
            runtime_override
            if runtime_override and availability.missing_runtimes
            else None
        ),
    )


# ─── Target validation ──────────────────────────────────────────────────────


def preflight_workspace_target(
    project_group_root: Path | str,
    *,
    force: bool = False,
) -> Path:
    """Validate an ``init`` target WITHOUT mutating the filesystem.

    Resolves the target and applies the same refusals as
    :func:`init_workspace` (filesystem root, exact ``$HOME``, and a single
    project repo-root unless ``force``), then returns the resolved group
    root. Callers MUST run this before any interactive discovery or prompt
    so we never mutate a child (e.g. ``git init``) on a target we would
    ultimately reject. Raises :class:`WorkspaceInitError` on a refused
    target.
    """
    group_root = _coerce_group_root(project_group_root, dry_run=True)
    _refuse_unsafe_root(group_root)
    _refuse_repo_root(group_root, force=force)
    return group_root


def _coerce_group_root(value: Path | str, *, dry_run: bool) -> Path:
    """Resolve ``value`` to an absolute directory path.

    Creates the directory when it doesn't exist (real run only). The
    ``dry_run`` path returns the resolved path without touching the
    filesystem so callers can preview safely.
    """
    candidate = Path(value).expanduser()
    if candidate.exists():
        if not candidate.is_dir():
            raise WorkspaceInitError(
                f"{candidate} exists but is not a directory"
            )
        return candidate.resolve()
    if not dry_run:
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate.resolve()
    # Dry-run: parent must exist so the path is plausible; we still
    # return the absolute form without creating it.
    parent = candidate.parent.resolve()
    if not parent.is_dir():
        raise WorkspaceInitError(
            f"parent directory does not exist: {parent}"
        )
    return (parent / candidate.name).resolve()


def _refuse_unsafe_root(group_root: Path) -> None:
    if str(group_root) == "/":
        raise WorkspaceInitError(
            "refusing to initialise the filesystem root (/)"
        )
    home = Path.home().resolve()
    if group_root == home:
        raise WorkspaceInitError(
            f"refusing to initialise the user home directory exactly ({home}). "
            "Pick a subdirectory."
        )


def _refuse_repo_root(group_root: Path, *, force: bool) -> None:
    """If the target itself looks like a single project repo, refuse.

    Repo-likeness is the presence of any of :data:`_PROJECT_REPO_MARKERS`
    *at the root*. Child directories that look like repos are fine —
    that's the expected case.
    """
    if force:
        return
    if not group_root.exists():
        return  # nothing to inspect on a not-yet-created dir
    for marker in _PROJECT_REPO_MARKERS:
        if (group_root / marker).exists():
            raise WorkspaceInitError(
                f"{group_root} looks like an individual project repo "
                f"(found {marker!r} at the root). `orcho workspace init` "
                "expects a *group* directory that holds one or more "
                "project repos. Re-run with --force if this is "
                "intentional, or point at the parent directory."
            )


# ─── Internals: env script ──────────────────────────────────────────────────


_ENV_SCRIPT_BODY: Final[str] = (
    "#!/usr/bin/env bash\n"
    "# Generated by `orcho workspace init`.\n"
    "# Source this file to point your shell at the Orcho workspace.\n"
    "# Works under both bash (BASH_SOURCE) and zsh (which leaves\n"
    "# BASH_SOURCE empty but sets $0 to the sourced file path).\n"
    '_orcho_env_src="${BASH_SOURCE[0]:-$0}"\n'
    'export ORCHO_WORKSPACE="$(cd "$(dirname "$_orcho_env_src")" '
    '&& pwd)"\n'
    'export ORCHO_RUNSPACE="${ORCHO_WORKSPACE}/runspace"\n'
    'unset _orcho_env_src\n'
)


def _write_env_file(env_file: Path, *, dry_run: bool) -> str:
    """Return one of ``"created"`` / ``"identical"`` / ``"differs"``."""
    if env_file.is_file():
        try:
            existing = env_file.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        if existing == _ENV_SCRIPT_BODY:
            return "identical"
        return "differs"
    if not dry_run:
        env_file.write_text(_ENV_SCRIPT_BODY, encoding="utf-8")
        # Mark executable for the user — group/world unchanged. Mode
        # 0o755 mirrors the typical ``chmod +x`` outcome.
        env_file.chmod(env_file.stat().st_mode | stat.S_IXUSR
                       | stat.S_IXGRP | stat.S_IXOTH)
    return "created"


# ─── Internals: workspace-local config ──────────────────────────────────────


_CONFIG_SECTIONS: Final[tuple[str, ...]] = (
    "timeouts",
    "session",
    "codemap",
    "hypothesis",
    "pipeline",
    "language",
    "artifacts",
)


def _project_entry_value(path: str, git_dir: str) -> object:
    """Return the correct config.local.json projects-map value for one project."""
    if git_dir:
        return {"path": path, "git_dir": git_dir}
    return path


def _workspace_local_config_template(
    detected_projects: list[DetectedProject],
    extra_projects: list[ExtraProject] | None = None,
) -> dict:
    """Return the first-run ``.orcho/config.local.json`` snapshot.

    The file is intentionally concrete, not a blank form: it captures
    the current package defaults plus package/user local layers so the
    user can immediately see and edit the values this workspace will own.
    """
    data = _workspace_local_config_seed()
    data["_comment"] = (
        "Personal workspace overrides generated by `orcho workspace init`. "
        "This file is read from $ORCHO_WORKSPACE/.orcho/config.local.json "
        "after package and user config.local.json plus workspace config.json. "
        "Environment variables still win."
    )
    projects: dict[str, object] = {}
    for project in detected_projects:
        projects[project.name] = _project_entry_value(project.path, "")
    for project in (extra_projects or []):
        projects[project.name] = _project_entry_value(project.path, project.git_dir)
    if projects:
        data["projects"] = projects
    return data


def _workspace_local_config_seed() -> dict:
    """Build concrete config values for a newly initialised workspace."""
    from core.infra import config as core_config
    from core.infra.paths import CONFIG_DIR, user_config_dir

    defaults_path = CONFIG_DIR / "config.defaults.json"
    try:
        raw_defaults = json.loads(defaults_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw_defaults = {}

    if not isinstance(raw_defaults, dict):
        raw_defaults = {}

    cfg: dict = {"phases": core_config._extract_phases(raw_defaults)}
    for section in _CONFIG_SECTIONS:
        cfg[section] = dict(raw_defaults.get(section, {}))

    for local_path in (
        CONFIG_DIR / "config.local.json",
        user_config_dir() / "config.local.json",
    ):
        if not local_path.exists():
            continue
        try:
            local = json.loads(local_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(local, dict):
            core_config._merge_local_layer(cfg, local)
    return cfg


def _write_workspace_local_config(
    path: Path,
    *,
    detected_projects: list[DetectedProject],
    extra_projects: list[ExtraProject] | None = None,
    dry_run: bool,
    runtime_override: str | None = None,
) -> str:
    """Return ``created`` / ``exists`` / ``updated`` / ``blocked``."""
    if path.is_file():
        action = _merge_project_aliases(
            path,
            detected_projects,
            extra_projects=extra_projects or [],
            dry_run=dry_run,
        )
        if runtime_override and _override_runtimes_in_file(
            path, runtime_override, dry_run=dry_run,
        ):
            action = "updated"
        return action
    if path.exists():
        return "blocked"
    data = _workspace_local_config_template(detected_projects, extra_projects)
    if runtime_override:
        _apply_runtime_override(data.get("phases", {}), runtime_override)
    if not dry_run:
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return "created"


def _apply_runtime_override(
    phases: dict[str, dict], override: str,
) -> tuple[str, ...]:
    """Point phases whose runtime executable is missing at ``override``.

    Mutates ``phases`` in place and returns the names of the phases
    that changed. The model is runtime-specific, so a switched phase
    borrows the model of a phase already configured for ``override``;
    with no donor the model is left empty and the runtime falls back
    to its own default model.
    """
    donor_model = next(
        (
            spec.get("model", "")
            for spec in phases.values()
            if isinstance(spec, dict)
            and spec.get("runtime") == override
            and spec.get("model")
        ),
        "",
    )
    changed: list[str] = []
    for phase, spec in phases.items():
        if not isinstance(spec, dict):
            continue
        runtime = str(spec.get("runtime", "claude"))
        if runtime == override or runtime_installed(runtime):
            continue
        spec["runtime"] = override
        spec["model"] = donor_model
        changed.append(phase)
    return tuple(changed)


def _override_runtimes_in_file(
    path: Path, override: str, *, dry_run: bool,
) -> bool:
    """Apply :func:`_apply_runtime_override` to an existing config file.

    The file may carry only a partial ``phases`` overlay (or none), so
    the switch is computed against the *effective* phase map — seed
    layers plus the file itself — and the changed phases are written
    back as explicit entries. Returns True when anything changed.
    """
    from core.infra import config as core_config

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False

    effective = _workspace_local_config_seed()
    core_config._merge_local_layer(effective, data)
    phases = effective.get("phases", {})
    changed = _apply_runtime_override(phases, override)
    if not changed:
        return False

    file_phases = data.get("phases")
    if not isinstance(file_phases, dict):
        file_phases = {}
        data["phases"] = file_phases
    for phase in changed:
        file_phases[phase] = phases[phase]
    if not dry_run:
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return True


def planned_phase_runtimes(project_group_root: Path | str) -> dict[str, str]:
    """Per-phase runtime ids the workspace under ``project_group_root`` uses.

    Combines the config seed (package defaults plus package/user local
    layers) with the workspace-local config file when it already
    exists — i.e. the effective post-init map for both a fresh and a
    repeat ``init``. Pure read; never touches the filesystem beyond
    reading config files.
    """
    from core.infra import config as core_config

    seed = _workspace_local_config_seed()
    local_file = (
        Path(project_group_root).expanduser()
        / _WORKSPACE_SUBDIR / _ORCHO_CONFIG_DIR / _LOCAL_CONFIG_FILE
    )
    if local_file.is_file():
        try:
            local = json.loads(local_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            local = None
        if isinstance(local, dict):
            core_config._merge_local_layer(seed, local)
    return {
        phase: str(spec.get("runtime", "claude"))
        for phase, spec in seed.get("phases", {}).items()
        if isinstance(spec, dict)
    }


def _merge_project_aliases(
    path: Path,
    detected_projects: list[DetectedProject],
    *,
    extra_projects: list[ExtraProject] | None = None,
    dry_run: bool,
) -> str:
    all_new: list[tuple[str, str, str]] = [
        (p.name, p.path, "") for p in detected_projects
    ] + [(p.name, p.path, p.git_dir) for p in (extra_projects or [])]
    if not all_new:
        return "exists"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "exists"
    if not isinstance(data, dict):
        return "exists"

    projects = data.get("projects")
    if projects is None:
        projects = {}
        data["projects"] = projects
    if not isinstance(projects, dict):
        return "exists"

    changed = False
    for name, proj_path, git_dir in all_new:
        if name not in projects:
            projects[name] = _project_entry_value(proj_path, git_dir)
            changed = True
    if not changed:
        return "exists"
    if not dry_run:
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return "updated"


# ─── Internals: MCP snippet + config merge ──────────────────────────────────


_SLUG_RE: Final[re.Pattern[str]] = re.compile(r"[^a-zA-Z0-9]+")


def _default_server_name(name: str) -> str:
    """Derive ``orcho-<slug>`` from a human name.

    Strips non-alphanumerics, lowercases, collapses runs. Empty
    inputs fall back to ``orcho`` so the result is always a valid
    JSON object key. If the slug already starts with ``orcho-`` or is
    exactly ``orcho``, the prefix is not duplicated.
    """
    slug = _SLUG_RE.sub("-", name.strip()).strip("-").lower()
    if not slug:
        return "orcho"
    if slug == "orcho" or slug.startswith("orcho-"):
        return slug
    return f"orcho-{slug}"


def _build_mcp_snippet(
    *,
    server_name: str,
    workspace_dir: Path,
    orcho_mcp_command: str,
) -> dict:
    return {
        "mcpServers": {
            server_name: {
                "command": orcho_mcp_command,
                "args": [],
                "env": {
                    "ORCHO_WORKSPACE": str(workspace_dir),
                },
            },
        },
    }


def _apply_mcp_config(
    path: Path,
    *,
    server_name: str,
    server_entry: dict,
    force: bool,
    dry_run: bool,
) -> str:
    """Merge ``server_entry`` into ``mcpServers[server_name]`` of ``path``.

    Returns one of:

    * ``"wrote"`` — new file created from scratch.
    * ``"merged"`` — file existed, this server name was absent, entry
      added alongside existing servers.
    * ``"no-op"`` — file existed, the server entry was byte-identical.
    * ``"replaced"`` — file existed, the server entry was different,
      ``force=True``, only that entry was replaced.

    Raises :class:`WorkspaceInitError` on parse failure, missing
    parent directory, or a conflicting entry without ``force``.
    """
    if not path.exists():
        parent = path.parent
        if not parent.is_dir():
            raise WorkspaceInitError(
                f"parent directory does not exist: {parent}"
            )
        if not dry_run:
            path.write_text(
                json.dumps(
                    {"mcpServers": {server_name: server_entry}},
                    indent=2, ensure_ascii=False,
                ) + "\n",
                encoding="utf-8",
            )
        return "wrote"

    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError) as e:
        raise WorkspaceInitError(
            f"could not parse existing MCP config {path}: {e}"
        ) from e
    if not isinstance(data, dict):
        raise WorkspaceInitError(
            f"existing MCP config {path} is not a JSON object"
        )

    servers = data.get("mcpServers")
    if servers is None:
        servers = {}
        data["mcpServers"] = servers
    elif not isinstance(servers, dict):
        raise WorkspaceInitError(
            f"existing 'mcpServers' in {path} is not a JSON object"
        )

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


# ─── Internals: project detection ───────────────────────────────────────────


def _iter_candidate_children(group_root: Path):
    """Yield child dirs of ``group_root`` that pass the exclusion filter."""
    if not group_root.is_dir():
        return
    for child in sorted(group_root.iterdir(), key=lambda p: p.name):
        if not child.is_dir():
            continue
        if child.name in _EXCLUDED_CHILD_NAMES:
            continue
        if child.name.startswith("."):
            continue
        yield child


def _find_nested_git_dirs(folder: Path, *, max_depth: int = 3) -> list[str]:
    """Return relative paths (shallowest first) to nested ``.git`` dirs/files.

    Prunes ``_EXCLUDED_CHILD_NAMES`` to avoid scanning caches. Recognises
    both ``.git`` directories (normal clone) and ``.git`` files (gitlink /
    submodule / worktree). The root ``.git`` at ``folder`` itself is not
    included — callers want sub-repos, not the project-root repo.
    """
    results: list[tuple[int, str]] = []
    folder_str = str(folder)
    for dirpath, dirnames, filenames in os.walk(folder_str):
        rel = os.path.relpath(dirpath, folder_str)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth > max_depth:
            dirnames.clear()
            continue
        # Check BEFORE pruning: .git may be in dirnames/filenames.
        has_git_dir = ".git" in dirnames
        has_git_file = ".git" in filenames
        if depth > 0 and (has_git_dir or has_git_file):
            results.append((depth, rel))
            # Don't recurse into a git repo's internals.
            dirnames.clear()
            continue
        # Prune traversal into caches and hidden dirs (but not at root level
        # so that any top-level .git of the outer project is not traversed).
        dirnames[:] = [
            d for d in dirnames
            if d not in _EXCLUDED_CHILD_NAMES and not d.startswith(".")
        ]
    results.sort(key=lambda t: t[0])
    return [r for _, r in results]


def discover_undetected_candidates(
    group_root: Path | str,
) -> list[UndetectedCandidate]:
    """Return child folders that were NOT auto-detected as projects.

    Each candidate carries pre-scanned ``nested_git_dirs`` (shallowest first).
    """
    root = Path(group_root).expanduser().resolve()
    candidates: list[UndetectedCandidate] = []
    for child in _iter_candidate_children(root):
        if _looks_like_project(child):
            continue
        nested = _find_nested_git_dirs(child)
        candidates.append(
            UndetectedCandidate(
                name=child.name,
                path=str(child),
                nested_git_dirs=tuple(nested),
            )
        )
    return candidates


def _detect_projects(group_root: Path) -> list[DetectedProject]:
    """One level deep — every child dir that smells like a project.

    Excludes well-known noise (``workspace-orchestrator``,
    ``node_modules``, etc.). Hidden directories (leading ``.``) are
    skipped except those that happen to be explicit project markers
    themselves — but those are top-level files, not directories, so
    in practice every excluded leading-dot name is a tooling cache.
    """
    out: list[DetectedProject] = []
    for child in _iter_candidate_children(group_root):
        if not _looks_like_project(child):
            continue
        out.append(DetectedProject(name=child.name, path=str(child)))
    return out


def _looks_like_project(d: Path) -> bool:
    return any((d / marker).exists() for marker in _PROJECT_REPO_MARKERS)


__all__ = [
    "DetectedProject",
    "DetectedRuntime",
    "ExtraProject",
    "UndetectedCandidate",
    "WorkspaceInitResult",
    "discover_undetected_candidates",
    "init_workspace",
    "planned_phase_runtimes",
    "preflight_workspace_target",
]
