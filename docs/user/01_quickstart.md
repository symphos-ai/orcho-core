# Quickstart — 5 minutes to the first run

## What Orcho is

Orcho drives your task through a cycle: **plan → implement → review → repair**.
You state the task — Orcho takes it through planning, implementation,
review, and repair phases using the code-agent CLIs you already have.

You do not write prompts by hand. You say what to do.

---

## Install

**Requirements:** Python 3.12+ and at least one supported code-agent CLI
tool (for example Claude CLI or Codex CLI). It must be a CLI tool that
Orcho can invoke from a terminal; an IDE or a chat app is not enough by
itself. The selected profile may need a second CLI for reviewer phases.

Once the packages are published, install the core CLI with:

```bash
pipx install orcho
orcho --help
```

If you prefer a project-managed Python environment:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install orcho
```

For source-checkout development:

```bash
git clone <orcho-core-repo-url> ~/orcho-preview/orcho-core
cd ~/orcho-preview/orcho-core
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

This gives you the core CLI. Optional MCP server, shell helpers, and MCP
client config are described in
[early_adopter_install.md](early_adopter_install.md).

Verify:

```bash
orcho --help
```

---

## Create a workspace

Create or pick a parent folder for your project:

```text
~/www/my-workspace/
├── my-project/              ← your repo
└── workspace-orchestrator/  ← Orcho creates this itself
    └── .orcho/              ← settings and extension-point guides
```

```bash
orcho workspace init ~/www/my-workspace
source ~/www/my-workspace/workspace-orchestrator/orcho-env.sh
```

If you connect Orcho to an MCP client, do not run `orcho_workspace_info`
from the shell: it is an MCP tool, not a terminal command. Add the
server to the client config first. For Codex CLI/app, Claude Code,
Gemini CLI, the Claude app, and Antigravity see
`orcho-mcp/docs/mcp_client_setup.md`.

---

## First run

```bash
orcho run \
  --task "Add input validation to the login endpoint. Return 400 if email is empty." \
  --project ~/www/my-workspace/my-project
```

Pick a small real task, and run Orcho on a separate branch or a copy of
the project if you want to watch the behavior risk-free first.

Orcho will:
1. Write an implementation plan
2. Implement it
3. Review the code
4. Fix the findings

The result lands in `workspace-orchestrator/runspace/runs/{timestamp}/`.

---

## What next

- [All commands →](02_commands.md)
- [Connect your project →](03_workspaces.md)
- [Read the results →](04_results.md)
