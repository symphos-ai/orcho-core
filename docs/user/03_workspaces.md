# Connecting your project

## The minimum to start

Orcho works with **any** project without any setup:

```bash
orcho run --task "Add tests for auth module" --project /path/to/any/project
```

The agent figures out the project structure on its own.

---

## Better results with plugin.py

To give the agent your project's specifics, add a `plugin.py`:

```
your-project/
└── .orcho/
    └── multiagent/
        └── plugin.py    ← create this file
```

**Minimal plugin.py:**
```python
from pipeline.plugins import PluginConfig

plugin = PluginConfig(
    name="My Project",
    tech_stack="FastAPI + PostgreSQL",
    test_runner="pytest",
)
```

With a plugin the agent knows the project language, how to run tests,
and which files matter. The full field reference is in
[../expert/01_plugin.md](../expert/01_plugin.md).

---

## Several projects (workspace)

If you work with several related repositories, create a parent folder
and let Orcho lay the rails:

```bash
orcho workspace init ~/www/my-workspace
```

The command creates:

```
~/www/my-workspace/
├── workspace-orchestrator/    ← workspace configuration (created by the command)
│   ├── orcho-env.sh           ← exports ORCHO_WORKSPACE / ORCHO_RUNSPACE
│   ├── runspace/runs/         ← pipeline run results are written here
│   ├── .orcho/config.local.json      ← workspace-local override config
│   ├── .orcho/multiagent/plugin.py  ← empty plugin template, safe by default
│   ├── .orcho/multiagent/AGENTS.md  ← matching project agent-rule template
│   ├── .orcho/multiagent/CLAUDE.md  ← shim shipped with the rule template
│   ├── .orcho/multiagent/prompts/   ← workspace-level prompt override guides
│   └── .orcho/.task-files/          ← task and verification ownership guide
├── api/                       ← your project 1 (detected automatically)
├── frontend/                  ← your project 2
└── mobile/                    ← your project 3
```

To make the shell see the new workspace:

```bash
source ~/www/my-workspace/workspace-orchestrator/orcho-env.sh
```

`workspace init` creates `.orcho/config.local.json` only on the first
run. It holds the starting config of every workspace-level setting you
can override for this group of projects: models and effort per phase,
artifact language, timeouts, session policy, pipeline knobs, and the
artifact mirror. The file is filled with the real current values so you
can read and edit it right away. A repeated `workspace init` does not
overwrite manual changes.

`workspace init` also creates discoverable extension-point guides. They
are only created when missing and are never overwritten. Prompt overrides
resolve project first, then workspace, then core. Project plugins still
live at `project/.orcho/multiagent/plugin.py`; the workspace plugin file
is a copyable template with `PLUGIN = {}`.

The generated `AGENTS.md` and `CLAUDE.md` live beside the plugin because they
form one project-configuration template. When a project adopts the plugin,
merge the rules into that project's root `AGENTS.md` and keep the shim at the
same root so native agent runtimes discover them. Existing project instructions
are never overwritten. The task guide applies the same ownership rule to task
files, direct `--task` input, and follow-ups: scheduled project gates remain
engine-owned, while implementation can still run focused tests, lint on
changed files, and other bounded feedback. Commands that are manual-only or
not configured may be requested explicitly. The plugin template includes a
commented, language-neutral gate pattern that starts at `warn` and declares no
commands until the project has been inspected. The matching agent rules include
a setup playbook for discovering project-native commands and environments,
choosing selection and scheduling, validating the contract, and reporting
unresolved assumptions.

From there — the usual commands:

```bash
# Cross-project run
orcho cross \
  --task "Add OAuth2 support" \
  --projects api:~/www/my-workspace/api frontend:~/www/my-workspace/frontend
```

Useful `orcho workspace init` flags:

- `--dry-run` — show what would be created, touching nothing.
- `--mcp-config ~/www/my-workspace/.mcp.json` — also write the MCP
  client snippet into `.mcp.json`. Existing entries for other servers
  are preserved.
- `--force` — allow initialising a directory that itself looks like a
  repo (by default the command refuses, to keep you out of trouble).
- `--no-interactive` — skip interactive questions about unmarked
  folders (CI / non-TTY).
- `--no-scaffold` — skip the extension-point README files and plugin
  template.

### Folders without auto-detection (nested git)

If the group contains a folder **without** a root marker (`.git`,
`pyproject.toml`, …) but with a repository inside (for example
`my-unity-project/UnityProj/.git`), Orcho does not add it
automatically. In interactive mode (TTY) you get a prompt:

```
Folder 'my-unity-project' was not auto-detected as a project.
  Treat 'my-unity-project' as a workspace project? [y/N]
  Found nested git repo at 'UnityProj'. Use it as git root? [Y/n]
```

After you agree, the `config.local.json` entry takes the form:

```json
{
  "projects": {
    "my-unity-project": {"path": "/path/to/my-unity-project", "git_dir": "UnityProj"}
  }
}
```

This is the single source of `git_dir` for worktree isolation and diff
capture. To add an entry by hand, edit the file directly.

---

## Where results are stored

By default everything is written to
`workspace-orchestrator/runspace/runs/`.

Override:
```bash
export ORCHO_RUNSPACE=/custom/path/to/output
```
