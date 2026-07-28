# Getting started with Orcho

Orcho takes a development task to a verified result: plan the work, run
agents, pass review gates, collect evidence, and see the final change.

For a project-specific readiness contract, start with the
[scheduled verification guide](../guides/scheduled_verification.md).

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

`pipx ensurepath` updates `PATH` for **future** shells, not the current one — so
after it you must **open a new terminal** before `pipx` (and the installed
`orcho`) resolve. The `↻` line in each block marks exactly where to reopen.

**macOS**

```bash
brew install pipx        # skip if pipx is already installed
pipx ensurepath
# ↻ reopen your terminal so the installed `orcho` is on PATH:
pipx install orcho
orcho --help
```

**Linux**

```bash
python3 -m pip install --user pipx   # or: sudo apt install pipx / sudo dnf install pipx
python3 -m pipx ensurepath
# ↻ reopen your terminal so `pipx` (and later `orcho`) are on PATH:
pipx install orcho
orcho --help
```

**Windows** (native, in PowerShell — supported and exercised in CI)

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
# ↻ IMPORTANT: close this window and open a NEW PowerShell now — `ensurepath`
#   only updates PATH for new shells, so `pipx` is not found until you reopen.
pipx install orcho
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

## 3. Connect your existing repository in place

The workspace is where Orcho keeps runs, evidence, and settings. It does not
need to contain your project, and your project does not need to be moved under
an Orcho-specific parent.

Enter the repository where it already lives:

```bash
cd ~/www/my-project
orcho workspace init
```

Or point at it explicitly:

```bash
orcho workspace init ~/www/my-project
```

Orcho registers exactly that repository and creates a deterministic managed
control workspace outside the checkout. The output shows both paths. It also
includes workspace settings, prompt override guides, a copyable plugin
template, and a task-file guide. Re-running `workspace init` is idempotent and
leaves existing scaffold files untouched.

The generated plugin is a safe, language-neutral starting point rather than a
finished project contract. A generic smoke run works immediately, but the
recommended next step is to adapt the scaffold into
`<project>/.orcho/multiagent/plugin.py` using the repository's real
architecture, commands, and environments. That configured plugin is where
Orcho gains durable project context and authoritative scheduled verification.

For the complete setup path, including agent-assisted discovery, see
[Configure the generated plugin scaffold](03_workspaces.md#configure-the-generated-plugin-scaffold).
The read-only `orcho workspace fine-tune --dry-run` command can propose an
initial contract from repository markers, and the generated agent rules tell a
coding agent how to inspect manifests, CI, and developer documentation before
drafting the project configuration. Both are setup accelerators, not authority:
an engineer reviews the resulting plugin and makes the final decision on
commands, environments, selection, schedule, policy, and failure consequences.
Use the [scheduled verification guide](../guides/scheduled_verification.md) for
the authoring workflow and the [plugin reference](../expert/01_plugin.md) for
every field.

## 4. Pick a control surface

### MCP — the recommended path

Use MCP if you want to drive Orcho from an MCP-aware client: start a
run, check status/evidence, make a QA gate decision, and resume a task
without reading raw logs.

Add the Orcho server to the MCP config of your project context.
`orcho workspace init` prints the exact command and JSON shape with the managed
workspace already filled in. To write a JSON client config during init:

```bash
ORCHO_MCP_COMMAND="$(command -v orcho-mcp)"

cd ~/www/my-project
orcho workspace init \
  --mcp-config .mcp.json \
  --mcp-server-name orcho-my-project \
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

Use the CLI directly from the project. No env script is required for the
project-oriented journey: the current directory identifies the project and
resolves the managed workspace created by init.

Try it first with `--mock`. This runs the full pipeline end-to-end with a
mock agent instead of a real model — no tokens spent, nothing calls your
code-agent CLI — so you can watch the mechanics risk-free before the first
real run:

```bash
orcho run --mock \
  --task "Add input validation: return 400 if email is empty or not valid format"
```

Then the real run (this one calls your configured code-agent CLI and spends
tokens):

```bash
orcho run \
  --task "Add input validation: return 400 if email is empty or not valid format"
```

The native Windows command has the same shape:

```powershell
orcho run `
  --task "Add input validation: return 400 if email is empty or not valid format"
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
orcho evidence
```

What changed in the project:

```bash
cd ~/www/my-project
git diff
```

Run artifacts live under the managed workspace path printed by init:
`<managed-workspace>/runspace/runs/`.

## 6. Best practice for several related repositories

Do not reorganise projects for the first mono-project run. If you later want
Orcho to coordinate several repositories in cross-project mode, a deliberate
shared root makes aliases, history, MCP configuration, and delivery easier to
reason about:

```text
~/work/my-product/
├── api/
├── web/
├── contracts/
└── workspace-orchestrator/
```

Initialise that shared root explicitly:

```bash
orcho workspace init ~/work/my-product
```

Either run from `~/work/my-product` and let Orcho discover the workspace, or
activate it once in a Unix shell to run from any directory:

```bash
source ~/work/my-product/workspace-orchestrator/orcho-env.sh
```

Then run cross-project work with an explicit project list:

```bash
orcho cross \
  --task "Change the API contract and update both consumers" \
  --projects api web
```

The aliases come from the projects registered by `workspace init`.
`alias:/absolute/path` remains available for projects that are not registered
in the active workspace.

This layout is a best practice, not an engine requirement. Absolute project
paths remain valid when repositories cannot or should not share a parent.

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

If the CLI does not see status/evidence, give it the project or the workspace
explicitly:

```bash
orcho status --workspace <managed-workspace-printed-by-init>
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
