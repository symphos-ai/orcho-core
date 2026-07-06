# Getting started with Orcho

Orcho takes a development task to a verified result: plan the work, run
agents, pass review gates, collect evidence, and see the final change.

Everything starts with an Orcho workspace — a folder next to your
project where Orcho keeps runs, evidence, metrics, and settings. After
that you pick the control surface you prefer:

- **MCP** — the primary path when you work from an MCP-aware client.
- **CLI** — the direct terminal path for people who want everything by hand.

## 1. Prepare the prerequisites

You need:

- Python 3.12+
- a project with code
- at least one **code-agent CLI** tool for real runs

It has to be a CLI tool that Orcho can invoke from a terminal. IDEs,
web/app versions of assistants, and chat interfaces are not enough by
themselves.

Check that at least one is available:

```bash
claude --version
# or
codex --version
```

The MCP path additionally needs an MCP-aware client.

## 2. Install Orcho

For most local use, install the `orcho` distribution with `pipx`. It installs
the core CLI and the MCP server while keeping them out of your project
environment. Pick your OS.

`pipx ensurepath` updates `PATH` for **future** shells, not the current one, so
the blocks below call pipx as `python -m pipx` and you should **open a new
terminal** before the installed `orcho` command resolves.

**macOS**

```bash
brew install pipx        # skip if pipx is already installed
pipx ensurepath
pipx install orcho
# open a new terminal, then:
orcho --help
```

**Linux**

```bash
python3 -m pip install --user pipx   # or: sudo apt install pipx / sudo dnf install pipx
python3 -m pipx ensurepath
python3 -m pipx install orcho
# open a new terminal, then:
orcho --help
```

**Windows** (native, in PowerShell — supported and exercised in CI)

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
py -m pipx install orcho
# ensurepath added orcho to PATH for new shells only —
# open a NEW PowerShell window, then verify:
orcho --help
```

Prefer a Unix shell on Windows? Install into WSL2 with the Linux steps above.
Windows-specific detail (agent-CLI paths, WSL2 layout, output streaming) lives
in [../expert/05_windows.md](../expert/05_windows.md).

If you want to try Orcho in a container, pull the official image and mount the
project plus an explicit credential directory:

```bash
docker pull ghcr.io/symphos-ai/orcho
alias orcho='docker run --rm -it \
  -v "$PWD":/workspace \
  -v ~/.orcho-auth:/agent-auth:ro \
  ghcr.io/symphos-ai/orcho orcho'
```

If you intentionally want Orcho in a project-managed environment, install it
with `pip`:

```bash
python -m pip install orcho
```

For a minimal engine-only dependency, install `orcho-core` directly:

```bash
python -m pip install orcho-core
```

`orcho[mcp]` and `orcho[all]` remain as back-compat aliases; plain `orcho`
already includes the MCP server. The source-checkout path for contributors and
pre-package testers lives in a separate guide:
[early_adopter_install.md](early_adopter_install.md).

After installing, verify:

```bash
orcho --help
orcho-mcp --help
```

For MCP it matters that the MCP client can start the server command. With
`pipx`, use the absolute path printed by `command -v orcho-mcp`; with a
Docker, register a `docker run ... orcho-mcp` stdio server; with a source
checkout, see `ORCHO_MCP_COMMAND` in
[early_adopter_install.md](early_adopter_install.md).

## 3. Create a workspace next to your project

The workspace is where Orcho keeps runs, evidence, and settings. First
prepare a parent folder and put your project inside. Any normal way
works: `git clone`, `cp -R`, or moving an existing repo. Orcho does not
copy or move your project for you.

Keep the workspace next to the repo, not inside it:

```text
~/www/my-workspace/
├── my-project/              ← your repo
└── workspace-orchestrator/  ← Orcho creates this itself
    └── .orcho/              ← settings and extension-point guides
```

When the parent folder is ready, ask Orcho to create its part:

```bash
orcho workspace init ~/www/my-workspace
```

If you have several related projects, keep them under the same parent
folder before running `orcho workspace init`:

```text
~/www/my-workspace/
├── api/
├── frontend/
└── workspace-orchestrator/
    └── .orcho/
```

The generated `.orcho/` includes workspace settings, prompt override
guides, a copyable plugin template, and a task-file guide. Re-running
`workspace init` leaves existing scaffold files untouched.

## 4. Pick a control surface

### MCP — the recommended path

Use MCP if you want to drive Orcho from an MCP-aware client: start a
run, check status/evidence, make a QA gate decision, and resume a task
without reading raw logs.

Add the Orcho server to the MCP config of your project/workspace
context. `orcho workspace init` can print and write the snippet for you:

```bash
ORCHO_MCP_COMMAND="$(command -v orcho-mcp)"

orcho workspace init ~/www/my-workspace \
  --mcp-config ~/www/my-workspace/.mcp.json \
  --mcp-server-name orcho-my-workspace \
  --orcho-mcp-command "$ORCHO_MCP_COMMAND"
```

After restarting the MCP client, open the `orcho_getting_started` prompt
or the `orcho://docs/getting-started` resource.

Different MCP clients register servers differently. Codex CLI/app uses
`codex mcp add`; Claude Code uses `claude mcp add`; Gemini CLI uses
`gemini mcp add`; the Claude app and Antigravity read their own JSON
config files. Copy-paste instructions live in
`orcho-mcp/docs/mcp_client_setup.md`.

### CLI — the terminal path

Use the CLI if you want to work directly from a shell.

On **macOS / Linux** (and WSL2 or Git Bash on Windows), source the generated
env file to point the shell at the workspace:

```bash
source ~/www/my-workspace/workspace-orchestrator/orcho-env.sh
```

Try it first with `--mock`. This runs the full pipeline end-to-end with a
mock agent instead of a real model — no tokens spent, nothing calls your
code-agent CLI — so you can watch the mechanics risk-free before the first
real run:

```bash
orcho run --mock \
  --task "Add input validation: return 400 if email is empty or not valid format" \
  --project ~/www/my-workspace/my-project
```

Then the real run (this one calls your configured code-agent CLI and spends
tokens):

```bash
orcho run \
  --task "Add input validation: return 400 if email is empty or not valid format" \
  --project ~/www/my-workspace/my-project
```

On **native Windows (PowerShell)**, `orcho-env.sh` is a bash script; set the
same two variables directly (or pass `--workspace` on each command):

```powershell
$env:ORCHO_WORKSPACE = "$HOME\www\my-workspace\workspace-orchestrator"
$env:ORCHO_RUNSPACE  = "$env:ORCHO_WORKSPACE\runspace"

orcho run `
  --task "Add input validation: return 400 if email is empty or not valid format" `
  --project "$HOME\www\my-workspace\my-project"
```

Orcho will change files in the project you point it at. For a first run,
prefer a separate branch or a copy of the project.

## 5. Inspect the result

These commands work on top of the same workspace.

Status of the latest run:

```bash
orcho status
```

Evidence in readable form:

```bash
orcho evidence --format md
```

What changed in the project:

```bash
cd ~/www/my-workspace/my-project
git diff
```

Run artifacts live here:

```text
~/www/my-workspace/workspace-orchestrator/runspace/runs/
```

## If something goes wrong

If Orcho cannot find the agent CLI:

```bash
export CLAUDE_BIN="$(which claude)"   # macOS / Linux
# or
export CODEX_BIN="$(which codex)"
```

```powershell
$env:CLAUDE_BIN = (Get-Command claude).Source   # native Windows / PowerShell
# or
$env:CODEX_BIN  = (Get-Command codex).Source
```

If the CLI does not see status/evidence, make sure the shell is
connected to the workspace:

```bash
source ~/www/my-workspace/workspace-orchestrator/orcho-env.sh   # macOS / Linux
```

```powershell
$env:ORCHO_WORKSPACE = "$HOME\www\my-workspace\workspace-orchestrator"   # native Windows
```

If MCP looks at the wrong place, check `ORCHO_WORKSPACE` in the MCP
server config. Each MCP server process is bound to one workspace.

## What next

| I want to | Read |
| --- | --- |
| Work through MCP | the `orcho_getting_started` prompt or `orcho://docs/getting-started` |
| Work through the CLI | [01_quickstart.md](01_quickstart.md) |
| All CLI commands | [02_commands.md](02_commands.md) |
| Workspaces and multiple projects | [03_workspaces.md](03_workspaces.md) |
| Where the results live | [04_results.md](04_results.md) |
| Teach the agent my project via plugin.py | [../expert/01_plugin.md](../expert/01_plugin.md) |
| Custom prompts | [../expert/02_prompts.md](../expert/02_prompts.md) |
