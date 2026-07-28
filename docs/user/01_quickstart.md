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

For the native CLI path, install the `orcho` distribution with `pipx` (core CLI
+ MCP server). Pick your OS. Note: `pipx ensurepath` only updates `PATH` for
**new** shells, so after it you must reopen your terminal before `pipx` and the
installed `orcho` resolve — the `↻` line marks where.

```bash
# macOS
brew install pipx        # skip if pipx is already installed
pipx ensurepath
# ↻ reopen your terminal so the installed `orcho` is on PATH:
pipx install orcho
orcho --help
```

```bash
# Linux
python3 -m pip install --user pipx   # or: sudo apt install pipx / sudo dnf install pipx
python3 -m pipx ensurepath
# ↻ reopen your terminal so `pipx` (and later `orcho`) are on PATH:
pipx install orcho
orcho --help
```

```powershell
# Windows (native, PowerShell — supported and exercised in CI)
py -m pip install --user pipx
py -m pipx ensurepath
# ↻ IMPORTANT: close this window and open a NEW PowerShell now — `ensurepath`
#   only updates PATH for new shells, so `pipx` is not found until you reopen.
pipx install orcho
orcho --help
```

Windows-specific detail (WSL2, agent-CLI paths, output streaming) lives in
[../expert/05_windows.md](../expert/05_windows.md).

For an isolated container path (OS-agnostic):

```bash
docker pull ghcr.io/symphos-ai/orcho
alias orcho='docker run --rm -it \
  -v "$PWD":/workspace \
  -v ~/.orcho-auth:/agent-auth:ro \
  ghcr.io/symphos-ai/orcho orcho'
```

If you prefer a project-managed Python environment:

```bash
# macOS / Linux
python -m venv .venv
source .venv/bin/activate
python -m pip install orcho
```

```powershell
# native Windows / PowerShell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
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

The `orcho` distribution gives you both `orcho` and `orcho-mcp`. Shell helpers,
source-checkout setup, and MCP client config are described in
[early_adopter_install.md](early_adopter_install.md).

Verify:

```bash
orcho --help
orcho-mcp --help
```

---

## Connect the repository you already have

Open the project in its current location and initialise Orcho from there:

```bash
cd ~/www/my-project
orcho workspace init
```

```powershell
# native Windows / PowerShell
Set-Location $HOME\www\my-project
orcho workspace init
```

The repository is not moved or copied. Orcho registers its absolute path and
creates a managed control workspace outside the checkout. The command prints
that workspace path and MCP client setup, but later CLI commands resolve it
from `--project` automatically.

If you connect Orcho to an MCP client, do not run `orcho_workspace_info`
from the shell: it is an MCP tool, not a terminal command. Add the
server to the client config first. For Codex CLI/app, Claude Code,
Gemini CLI, the Claude app, and Antigravity see
`orcho-mcp/docs/mcp_client_setup.md`.

---

## First run

Start with a free dry run. `--mock` swaps the real model for a mock agent,
so the whole plan → implement → review → repair cycle runs end-to-end
without spending tokens or calling your code-agent CLI — the fastest way to
see how Orcho behaves:

```bash
orcho run --mock \
  --task "Add input validation to the login endpoint. Return 400 if email is empty." \
  --project .
```

Then the real run (drops `--mock`, so it calls your code-agent CLI and
spends tokens):

```bash
orcho run \
  --task "Add input validation to the login endpoint. Return 400 if email is empty." \
  --project .
```

Pick a small real task, and run Orcho on a separate branch or a copy of
the project if you want to watch the behavior risk-free first.

Orcho will:
1. Write an implementation plan
2. Implement it
3. Review the code
4. Fix the findings

The result lands in the managed workspace path printed by
`orcho workspace init`, under `runspace/runs/{timestamp}/`.

---

## What next

- [All commands →](02_commands.md)
- [Connect your project →](03_workspaces.md)
- [Read the results →](04_results.md)
