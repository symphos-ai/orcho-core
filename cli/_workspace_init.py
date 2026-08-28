"""Focused interactive workflow for ``orcho workspace init``."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from cli._formatters import (
    format_error,
    format_project_plugin_outcomes,
    format_workspace_init,
)
from cli._workspace_runtime_gate import workspace_runtime_gate
from core.io.journey_prompt import ask_yn, help_line, is_color_active, title
from sdk import init_workspace
from sdk.errors import OrchoError
from sdk.workspace import discover_undetected_candidates, preflight_workspace_target
from sdk.workspace_paths import project_repo_marker
from sdk.workspace_project_plugin import (
    ProjectPluginCandidate,
    derive_project_plugin_candidates,
    materialize_project_plugins,
)


def run_workspace_init(args: argparse.Namespace) -> int:
    """Run workspace initialization and its one explicit plugin decision."""
    project_group_root = args.project_group_root or os.getcwd()
    workspace_dir = getattr(args, "workspace_dir", None)
    no_interactive = bool(getattr(args, "no_interactive", False))
    dry_run = bool(getattr(args, "dry_run", False))
    force = bool(getattr(args, "force", False))
    no_scaffold = bool(getattr(args, "no_scaffold", False))

    try:
        preflight_workspace_target(project_group_root, force=force)
        gate = workspace_runtime_gate(
            project_group_root,
            workspace_dir=workspace_dir,
            no_interactive=no_interactive,
            dry_run=dry_run,
            force=force,
        )
    except OrchoError as exc:
        print(format_error(exc), file=sys.stderr)
        return exc.exit_code

    project_mode = project_repo_marker(project_group_root) is not None
    candidates = [] if project_mode else discover_undetected_candidates(project_group_root)
    extra_projects: list = []
    discovery_interactive = False
    if candidates and _interactive_eligible(no_interactive=no_interactive, dry_run=dry_run):
        from pipeline.project.project_discovery_prompt import prompt_for_extra_projects

        discovery_interactive = True
        extra_projects = prompt_for_extra_projects(candidates)

    try:
        result = init_workspace(
            project_group_root,
            workspace_dir=workspace_dir,
            workspace_name=getattr(args, "workspace_name", None),
            mcp_config=getattr(args, "mcp_config", None),
            mcp_server_name=getattr(args, "mcp_server_name", None),
            orcho_mcp_command=getattr(args, "orcho_mcp_command", None),
            force=force,
            dry_run=dry_run,
            extra_projects=extra_projects,
            undetected_count=len(candidates) - len(extra_projects),
            interactive=discovery_interactive,
            no_scaffold=no_scaffold,
            runtime_override=gate.runtime_override,
        )
    except OrchoError as exc:
        print(format_error(exc), file=sys.stderr)
        return exc.exit_code

    print(format_workspace_init(result, verbose=bool(getattr(args, "verbose", False))))
    project_paths = _registered_project_paths(result)
    if project_paths and _interactive_eligible(
        no_interactive=no_interactive, dry_run=dry_run
    ):
        # Derived once, read-only: the prompt can disclose empty candidates
        # up front and materialisation reuses the same inspection results.
        candidates = derive_project_plugin_candidates(project_paths)
        if _confirm_project_plugin_materialization(candidates):
            print(format_project_plugin_outcomes(materialize_project_plugins(candidates)))
    _emit_delivery_setup_hints(result, project_group_root)
    return 0


def _interactive_eligible(*, no_interactive: bool, dry_run: bool) -> bool:
    return (
        not no_interactive
        and not dry_run
        and bool(getattr(sys.stdin, "isatty", lambda: False)())
    )


def _registered_project_paths(result) -> tuple[str, ...]:
    """Return the current init's detected and confirmed projects once each."""
    paths: list[str] = []
    seen: set[str] = set()
    for project in (*result.detected_projects, *result.extra_projects):
        if project.path not in seen:
            seen.add(project.path)
            paths.append(project.path)
    return tuple(paths)


def _confirm_project_plugin_materialization(
    candidates: tuple[ProjectPluginCandidate, ...],
) -> bool:
    """Ask the one opt-in question that permits writes into project trees.

    Projects whose inspection found no repo markers are disclosed before the
    question so an empty skeleton is a stated outcome, not a silent one.
    """
    color = is_color_active(sys.stdout)
    project_count = len(candidates)
    noun = "project" if project_count == 1 else "projects"
    sys.stdout.write("\n" + title("Project plugin configuration", color=color) + "\n")
    sys.stdout.write(
        help_line(
            "Create starter plugin-configs to declare project gates, route "
            "repairable failures, and retain readiness evidence.",
            color=color,
        )
        + "\n"
    )
    for candidate in candidates:
        if not candidate.empty:
            continue
        name = Path(candidate.project_path).name
        sys.stdout.write(
            help_line(
                f"Note: no repo markers detected in {name} — a skeleton will "
                "be created; fill lint/test commands yourself.",
                color=color,
            )
            + "\n"
        )
    sys.stdout.write(
        help_line(
            "Learn more: https://docs.orcho.dev/extend/project-instructions/",
            color=color,
        )
        + "\n"
    )
    answer = ask_yn(
        f"  Create plugin-configs for {project_count} registered {noun}?",
        default_yes=False,
        stdin=sys.stdin,
        stdout=sys.stdout,
        color=color,
    )
    return answer is True


def _emit_delivery_setup_hints(result, project_group_root: str) -> None:
    """Best-effort delivery setup hint after successful workspace init."""
    try:
        from pipeline.engine.delivery_publish import collect_delivery_setup_hints

        candidates: list[str] = []
        seen: set[str] = set()

        def _add(path: str) -> None:
            if path and path not in seen:
                seen.add(path)
                candidates.append(path)

        for project in result.detected_projects:
            _add(project.path)
        for project in result.extra_projects:
            _add(project.path)
        if project_group_root and (Path(project_group_root) / ".git").exists():
            _add(project_group_root)

        for path in candidates:
            hints = collect_delivery_setup_hints(Path(path))
            if hints:
                print(f"\nDelivery setup:\n  {hints[0]}")
                return
    except Exception:  # noqa: BLE001 -- a hint must never break workspace init
        return


__all__ = ["run_workspace_init"]
