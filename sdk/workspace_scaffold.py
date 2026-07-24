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
    # "language": "Describe the project's actual language/runtime.",
    # "architecture": "Describe the project's real components and boundaries.",
    # Keep recurring verification in the typed contract below instead of
    # repeating broad test commands in every task. workspace init cannot infer
    # project-native tools; add real environments, commands, selection, and
    # schedules after inspecting the project.
    # BEGIN ORCHO VERIFICATION EXAMPLE
    # "work_mode": "pro",
    # "verification": {
    #     "commands": {},
    #     "gate_sets": {},
    #     "selection": [],
    #     "schedule": [],
    # },
    # END ORCHO VERIFICATION EXAMPLE
    # "review_focus_extra": "Check auth, migrations, and API compatibility.",
    # "skill_trust": {
    #     # Project and workspace skills are opt-in. Keep defaults unless
    #     # this repository intentionally trusts local skill instructions.
    #     # "trust_project": True,
    #     # "trust_workspace": True,
    # },
}
'''

_AGENTS_BODY: Final[str] = """\
# Orcho Project Agent Rules

This template belongs with the adjacent `plugin.py` verification contract.
When adopting that plugin in a project, merge these rules into the project's
root `AGENTS.md` and keep a root `CLAUDE.md` shim pointing to it. Native agent
runtimes discover root instruction files; Orcho does not inject this template
into prompts or overwrite existing project rules.

## Configuring Orcho for this project

When asked to configure Orcho, do not fill the plugin from language stereotypes
or copy commands from this template. Inspect the repository first:

1. Read existing root instructions, manifests, package-manager scripts,
   build files, CI workflows, and developer documentation.
2. Identify the commands the project already treats as authoritative for
   formatting, lint, type checks, unit/integration tests, builds, migrations,
   and end-to-end checks.
3. Identify the real execution environment: checkout-relative binaries,
   required services, dependency repositories, generated assets, credentials,
   serial-only infrastructure, and commands unsafe in an isolated worktree.
4. Separate fast deterministic feedback from broad, slow, destructive,
   networked, or credential-dependent verification. Do not call a command
   cheap merely because its name sounds familiar.
5. Edit `.orcho/multiagent/plugin.py` using only facts found in this project:
   declare environments and commands, group them by purpose, select them by
   `always`, `paths`, `task_kind`, or `operator`, then schedule them or leave
   them manual-only.
6. Choose policy from evidence; do not default every new gate to `warn`.
   Use `require` immediately for a project-established, load-bearing check when
   its command and isolated execution environment are proven and its failure
   must prevent delivery. Use `warn` for genuinely advisory diagnostics whose
   failure should be visible but should not block. Keep unproven, broad,
   service-dependent, credential-dependent, or destructive checks manual until
   their environment and consequence are deliberate. If any delivery-selected
   check is load-bearing, make the delivery boundary `require` as well.
   Use repair routing only for failures the implementation agent can fix in
   code; environment, service, credential, and provenance failures need an
   operator handoff.
7. Run `orcho quality-gates --project .` and fix every contract validation
   error. Execute cheap candidate commands once to confirm their exact argv and
   cwd. Do not launch broad or destructive candidates merely to finish setup;
   keep them operator-selected until deliberately validated.
8. Report what was configured, why each gate is selected and scheduled, which
   checks remain manual, and which assumptions still need operator confirmation.

An empty generated verification skeleton is a starting point, not a completed
project configuration.

## Verification ownership

Before adding verification commands to a task, inspect the target project's:

    <project>/.orcho/multiagent/plugin.py

You can also inspect the effective contract with:

    orcho quality-gates --project /path/to/project

Apply the same rules whether the task comes from `--task`, `--task-file`, a
follow-up, or an edited plan:

- If a broad or recurring command is selected and scheduled by the project
  verification contract, the Orcho engine owns its official execution and
  durable receipt. Do not turn it into an implement subtask or run it again
  merely to satisfy task acceptance.
- Implementation agents may run useful targeted checks while debugging:
  focused tests, lint on changed files, a narrow type check, or another bounded
  command that gives fast feedback. The contract does not prohibit native
  tools; it prevents duplicate ownership of official proof.
- State acceptance as observable behavior, structure, and targeted evidence.
  Say that the repository gate remains engine-owned instead of copying its
  broad command into the task.
- If a command exists but is manual-only, declared but unscheduled, or absent
  from the contract, a task may explicitly require it when it is necessary
  evidence. Label why it is needed. Recurring commands should move into the
  project contract rather than being copied into every task.
- Never invoke `orcho verify` from an implement subtask to manufacture official
  receipts. Verification execution and receipt ownership belong to the engine.
- Work in the checkout supplied by Orcho. Do not hard-code a canonical project
  path as the edit or test subject.

## Task authoring

Keep one task bounded. Name anticipated files, the behavior being changed,
targeted tests that help implementation, and explicit exclusions. Do not ask an
agent to prove unbounded claims such as "nothing anywhere broke."

The detailed task and gate guide is:

    workspace-orchestrator/.orcho/.task-files/README.md
"""

_CLAUDE_BODY: Final[str] = "@./AGENTS.md\n"

_WORKSPACE_SHARED_CONFIG_BODY: Final[str] = (
    "{\n"
    '  "_comment": "Team-shared workspace configuration. Add active settings '
    'deliberately; personal overrides belong in config.local.json."\n'
    "}\n"
)

_WORKSPACE_GITIGNORE_BODY: Final[str] = (
    "# Personal workspace overrides stay local. Commit config.json for shared policy.\n"
    "config.local.json\n"
)

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

These rules apply to both reusable task files and direct `--task` input.

Start by inspecting the target project's effective verification contract:

    orcho quality-gates --project /path/to/project

The contract is usually declared in:

    project/.orcho/multiagent/plugin.py

### When a gate is selected and scheduled

Verification is the engine's job. A full or broad suite is not an implement
subtask: the engine executes the scheduled command, records its receipt, and
applies the configured consequence.

Write acceptance in terms of:

- observable behavior or structure;
- anticipated files and explicit exclusions;
- targeted tests or checks useful while implementing;
- the fact that the repository gate remains engine-owned.

Implementation agents are still allowed to use native tools. Focused tests,
lint on changed files, a narrow type check, and other bounded feedback are
appropriate when they help produce correct code. The rule is against duplicate
ownership of the same broad proof, not against useful local verification.

### When a command is not engine-owned

If a useful command is manual-only, declared but unscheduled, or not present in
the verification contract, the task may require it explicitly. This is
especially appropriate for one-off migration checks or expensive commands that
should run only for a particular change. Explain why that command is necessary.

If the same command keeps appearing in tasks, configure it once as a selected
and scheduled project gate instead.

### Minimal project gate pattern

The generated `.orcho/multiagent/plugin.py` contains a language-neutral,
validation-safe starting pattern. It deliberately declares no command because
`workspace init` has not inspected the project:

- add the project's real verification environment and native command;
- group it in a gate set;
- select the gate set with `always`, `paths`, `task_kind`, or `operator`;
- schedule it at an executable hook, or keep it manual-only;
- use `require` for a proven load-bearing check, `warn` for a genuinely
  advisory diagnostic, and manual selection for an unproven or operationally
  expensive check. Do not use `warn` merely to avoid deciding the consequence.

Do not copy `orcho verify run ...` into the task. That command creates official
receipts and remains engine/operator-owned.

The adjacent `.orcho/multiagent/AGENTS.md` is the matching project-rule
template. When a project adopts the plugin, merge that template into the
project's root `AGENTS.md` and keep the supplied `CLAUDE.md` shim at the same
root. Existing project instructions must be preserved rather than overwritten.
The template also tells an agent how to inspect an unfamiliar repository,
derive project-native environments and commands, choose selection/scheduling,
and validate the resulting contract without guessing a language toolchain.

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
    agents_file = multiagent / "AGENTS.md"
    claude_file = multiagent / "CLAUDE.md"

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
        root / "config.json": _WORKSPACE_SHARED_CONFIG_BODY,
        root / ".gitignore": _WORKSPACE_GITIGNORE_BODY,
        agents_file: _AGENTS_BODY,
        claude_file: _CLAUDE_BODY,
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
            str(agents_file),
            str(claude_file),
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
