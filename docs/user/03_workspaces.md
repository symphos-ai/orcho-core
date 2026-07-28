# Connecting your project

## Start inside the repository you already have

Do not move or re-parent a repository to adopt Orcho. Enter it and initialise
the control workspace:

```bash
cd /path/to/any/project
orcho workspace init
orcho run --task "Add tests for auth module" --mock
```

Orcho stores its run state outside the checkout in a deterministic managed
workspace. The repository remains the canonical edit and delivery target.
Later CLI calls use the current directory as the project and resolve its
managed workspace automatically, while the MCP snippet printed by init carries
the workspace path explicitly.

---

## Configure the generated plugin scaffold

`workspace init` creates a language-neutral plugin scaffold in the control
workspace and prints its exact path. It is intentionally inert because init
does not guess project commands or policy. Generic mode can run immediately,
but effective recurring use depends on adapting that scaffold to the project.

Copy it into the project and configure it from repository evidence:

```
your-project/
└── .orcho/
    └── multiagent/
        └── plugin.py    ← configured project copy of the generated scaffold
```

**Minimal plugin.py:**
```python
PLUGIN = {
    "name": "My Project",
    "language": "Python 3.12",
    "verification_envs": {"project": {"python": "python"}},
    "verification": {
        "default_env": "project",
        "commands": {"lint": {"run": ["python", "-m", "ruff", "check", "."], "cost": "fast"}},
        "gate_sets": {"hygiene": {"commands": ["lint"], "default_policy": "warn"}},
        "selection": [{"always": ["hygiene"]}],
        "schedule": [{"after_phase": "implement", "gate_sets": ["hygiene"]}],
    },
}
```

With a configured plugin the agent knows the project language, how to run
tests, and which files matter. More importantly, recurring readiness commands
become engine-owned scheduled gates instead of prose repeated across tasks.
Declare scheduled readiness with the
[scheduled verification guide](../guides/scheduled_verification.md); the full
field reference is in [../expert/01_plugin.md](../expert/01_plugin.md).

### Accelerate setup without delegating the decision

Start with a read-only candidate:

```bash
cd /path/to/project
orcho workspace fine-tune --dry-run
```

Fine-tune uses repository markers to propose environments and commands. It
does not write `plugin.py`, infer a complete gate lifecycle, or approve the
proposal.

For deeper setup, adopt the generated agent-rule template alongside the
plugin: merge its rules into the project's existing root `AGENTS.md`, preserve
the existing instructions, and keep the generated root `CLAUDE.md` shim. Then
ask the coding agent to configure Orcho for the repository. The generated rules
require it to inspect manifests, package scripts, CI workflows, developer
documentation, services, credentials, and worktree constraints instead of
guessing from the language.

A useful request is:

```text
Inspect this repository and configure the generated Orcho plugin from
repository evidence. Reuse project-native commands, classify their cost,
propose selection, schedule, policy, and failure routing, and report every
unresolved assumption. Do not invent commands or silently weaken failures.
```

The agent may prepare the plugin diff and run bounded candidate checks to
accelerate setup. The engineer remains the authority: review the discovered
commands and environments, decide which checks are load-bearing, approve their
selection, schedule, policy, and failure consequences, then inspect the
resolved contract:

```bash
orcho quality-gates
```

Do not rely on the configured gates until that review is complete. Automation
shortens repository discovery and drafting; it does not replace the engineering
decision about what is authoritative or release-blocking.

---

## Best practice for several related projects

After the single-project journey is working, related repositories benefit from
one intentional shared root. This is recommended for cross-project operation,
not required for ordinary Orcho use:

```bash
orcho workspace init ~/www/my-workspace
```

The command creates:

```
~/www/my-workspace/
├── workspace-orchestrator/    ← workspace configuration (created by the command)
│   ├── orcho-env.sh           ← exports ORCHO_WORKSPACE / ORCHO_RUNSPACE
│   ├── runspace/runs/         ← pipeline run results are written here
│   ├── .orcho/config.json            ← committable team workspace policy
│   ├── .orcho/config.local.json      ← gitignored personal workspace overrides
│   ├── .orcho/.gitignore             ← ignores config.local.json only
│   ├── .orcho/multiagent/plugin.py  ← empty plugin template, safe by default
│   ├── .orcho/multiagent/AGENTS.md  ← matching project agent-rule template
│   ├── .orcho/multiagent/CLAUDE.md  ← shim shipped with the rule template
│   ├── .orcho/multiagent/prompts/   ← workspace-level prompt override guides
│   └── .orcho/.task-files/          ← task and verification ownership guide
├── api/                       ← your project 1 (detected automatically)
├── frontend/                  ← your project 2
└── mobile/                    ← your project 3
```

Run from `~/www/my-workspace` to use cwd discovery. To make a Unix shell use
this workspace from any directory instead:

```bash
source ~/www/my-workspace/workspace-orchestrator/orcho-env.sh
```

`workspace init` creates `.orcho/config.json` and `.orcho/config.local.json`
only when they are missing. `config.json` is a neutral, comment-only starting
point for committable team policy. `config.local.json` is gitignored and holds
personal workspace overrides with real starting values: models and effort per
phase, artifact language, timeouts, session policy, pipeline knobs, and the
artifact mirror. It wins over the shared file. A repeated `workspace init`
does not overwrite either file or `.orcho/.gitignore`.

The full order is package `config.local.json` → user `config.local.json` →
workspace `config.json` → workspace `config.local.json` → environment
variables. This matches the common `settings.json` / `settings.local.json`
convention: commit the shared file; keep the local file personal.

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
commented, language-neutral declaration pattern to complete after the project
has been inspected. The matching agent rules include
a setup playbook for discovering project-native commands and environments,
choosing selection and scheduling, validating the contract, and reporting
unresolved assumptions.

From there, register/use the intended repositories explicitly:

```bash
# Cross-project run
orcho cross \
  --task "Add OAuth2 support" \
  --projects api frontend
```

These are registered project aliases. Use `alias:/absolute/path` only when a
project is not registered in the active workspace.

Useful `orcho workspace init` flags:

- `--dry-run` — show what would be created, touching nothing.
- `--workspace-dir PATH` — override the managed control-workspace location
  when initialising a single existing repository.
- `--mcp-config ~/www/my-workspace/.mcp.json` — also write the MCP
  client snippet into `.mcp.json`. Existing entries for other servers
  are preserved.
- `--force` — continue scaffolding without an installed agent runtime or
  replace a conflicting MCP entry.
- `--no-interactive` — skip interactive questions about unmarked
  folders (CI / non-TTY).
- `--no-scaffold` — skip extension-point templates, including the shared
  `config.json` and `.orcho/.gitignore` scaffold; the personal config snapshot
  is still created.

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

After you agree, the personal `config.local.json` entry takes the form:

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

## Reclaiming expired retained worktrees

Inspect retention without changing anything. The report is read-only and uses
the conservative 30-day root fallback cutoff by default:

```bash
orcho workspace cleanup --workspace /path/to/workspace
```

The report separately summarises checkout and run-root eligible/protected
reason codes. A checkout is protected only while it holds work that
cannot be recovered from anywhere else: uncommitted changes, commits no remote
has, a run that is still live or paused, an unexpired retention window, or
metadata and paths that cannot be read safely. Runs with no retained checkout
are reported separately as having nothing to reclaim.

`--older-than DAYS` changes only the legacy run-root-id fallback cutoff; it
does not reinterpret a readable `worktree.retention_until` and does not change
the worktree-tier retention predicate. For example, inspect roots with a
seven-day fallback cutoff:

```bash
orcho workspace cleanup --older-than 7 --workspace /path/to/workspace
```

To reclaim only expired checkout material while keeping every run directory:

```bash
orcho workspace cleanup --reclaim-worktrees --older-than 30
```

To reclaim eligible checkouts and then their fully eligible run roots:

```bash
orcho workspace cleanup --reclaim-both --older-than 30
```

Both commands archive by default under
`runspace/cleanup_archive/<receipt_id>/`. Use `--delete` only with a reclaim
tier for irreversible removal:

```bash
orcho workspace cleanup --reclaim-both --older-than 7 --delete
```

`--reclaim-worktrees` never removes a
run root; only `--reclaim-both` authorizes root archive/delete after dependent
checkout groups succeed. An inert root (no checkout record, a missing checkout,
or an already reclaimed checkout) can be removed when its own stopped/deadline
predicate is eligible. Every execution writes a durable receipt under
`runspace/cleanup_receipts/`. Reclaimed `meta.json` records preserve the old
`worktree.path` as historical evidence and add `worktree.reclaimed`; that path
cannot be resumed or followed up in place.
