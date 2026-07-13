"""Workspace extension-point scaffolding for ``orcho workspace init``."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final


@dataclass(frozen=True, slots=True)
class WorkspaceScaffoldResult:
    """Filesystem outcome for workspace extension-point scaffolding."""

    created_paths: tuple[Path, ...]
    skipped_paths: tuple[Path, ...]
    warnings: tuple[str, ...]
    extension_points: tuple[str, ...]


_PLUGIN_BODY: Final[str] = '''\
"""Workspace-visible plugin template.

Project-specific plugin configuration is loaded from:

    <project>/.orcho/multiagent/plugin.py

Copy this file into a project and fill in only the fields that help Orcho
understand that project. An empty PLUGIN dict is valid and changes nothing.
"""

PLUGIN = {
    # "name": "My Project",
    # "language": "Python",
    # "architecture": "FastAPI service with PostgreSQL",
    # "build_prompt_extra": "Run pytest after changing Python code.",
    # "review_focus_extra": "Check auth, migrations, and API compatibility.",
    # "skill_trust": {
    #     # Project and workspace skills are opt-in. Keep defaults unless
    #     # this repository intentionally trusts local skill instructions.
    #     # "trust_project": True,
    #     # "trust_workspace": True,
    # },
}
'''

_PROMPT_README_BODY: Final[str] = """\
# {title} Prompt Overrides

This directory is a workspace-level override point for composable prompt parts.
Resolution order is:

1. project/.orcho/multiagent/prompts/{layer}/NAME.md
2. workspace-orchestrator/.orcho/multiagent/prompts/{layer}/NAME.md
3. core/_prompts/{layer}/NAME.md

To customize a part, copy the shipped core file with the same relative name
into this directory, then edit the copy. Keep runtime facts, parser contracts,
language policy, git policy, and review-target policy out of editable prompt
parts; those are owned by code-level prompt contracts.
"""

_TASK_FILES_README_BODY: Final[str] = """\
# Task Files

Put reusable task markdown files in a project's:

    project/.orcho/.task-files/

Then run:

    orcho run --project /path/to/project --task-file NAME.md

Bare NAME.md lookup checks the explicit project's .orcho/.task-files first,
then walks up from the current directory looking for .orcho/.task-files.
Workspace-level task files are a convention guide here; copy task files into a
project when you want bare-name lookup to find them predictably.

## Writing a good task file

Verification is the engine's job. A full or broad suite is not an implement
subtask: implement work uses only targeted tests for its concrete change. The
project's required gate runs separately after implementation.

For the complete authoring guide, see:

https://github.com/symphos-ai/orcho-core/blob/main/docs/authoring-task-files.md
"""


def scaffold_workspace_extensions(
    workspace_dir: Path,
    *,
    dry_run: bool,
) -> WorkspaceScaffoldResult:
    """Create discoverable, non-destructive workspace extension-point files."""
    root = workspace_dir / ".orcho"
    multiagent = root / "multiagent"
    prompts = multiagent / "prompts"
    task_files = root / ".task-files"

    created: list[Path] = []
    skipped: list[Path] = []
    warnings: list[str] = []

    dirs = (
        root,
        multiagent,
        prompts,
        prompts / "roles",
        prompts / "tasks",
        prompts / "formats",
        task_files,
    )
    for directory in dirs:
        _ensure_dir(
            directory,
            dry_run=dry_run,
            created=created,
            skipped=skipped,
            warnings=warnings,
        )

    files = {
        multiagent / "plugin.py": _PLUGIN_BODY,
        prompts / "roles" / "README.md": _PROMPT_README_BODY.format(
            title="Roles", layer="roles",
        ),
        prompts / "tasks" / "README.md": _PROMPT_README_BODY.format(
            title="Tasks", layer="tasks",
        ),
        prompts / "formats" / "README.md": _PROMPT_README_BODY.format(
            title="Formats", layer="formats",
        ),
        task_files / "README.md": _TASK_FILES_README_BODY,
    }
    for path, body in files.items():
        _ensure_file(
            path,
            body,
            dry_run=dry_run,
            created=created,
            skipped=skipped,
            warnings=warnings,
        )

    return WorkspaceScaffoldResult(
        created_paths=tuple(created),
        skipped_paths=tuple(skipped),
        warnings=tuple(warnings),
        extension_points=(
            str(multiagent / "plugin.py"),
            str(prompts),
            str(task_files),
        ),
    )


def _ensure_dir(
    path: Path,
    *,
    dry_run: bool,
    created: list[Path],
    skipped: list[Path],
    warnings: list[str],
) -> None:
    if path.is_dir():
        skipped.append(path)
        return
    if path.exists():
        warnings.append(
            f"existing {path} is not a directory; leaving it untouched."
        )
        skipped.append(path)
        return
    created.append(path)
    if not dry_run:
        path.mkdir(parents=True, exist_ok=True)


def _ensure_file(
    path: Path,
    body: str,
    *,
    dry_run: bool,
    created: list[Path],
    skipped: list[Path],
    warnings: list[str],
) -> None:
    if path.is_file():
        skipped.append(path)
        return
    if path.exists():
        warnings.append(
            f"existing {path} is not a file; leaving it untouched."
        )
        skipped.append(path)
        return
    created.append(path)
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
