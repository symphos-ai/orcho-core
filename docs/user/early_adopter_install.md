# Install Orcho from a source checkout

Use this path when you are contributing, testing a branch before it is
published, or installing the MCP control-surface package from a local checkout.

## What the tester needs

- Python 3.12+
- Git access to the Orcho repositories you want to install
- at least one code-agent CLI available in the terminal
- one project they are willing to let Orcho edit, preferably on a branch
  or a disposable copy for the first run

Check the agent CLI:

```bash
claude --version
# or
codex --version
```

## Install the local Orcho suite

Clone the repos side by side:

```bash
mkdir -p ~/orcho-preview
cd ~/orcho-preview

git clone <orcho-core-repo-url> orcho-core
git clone <orcho-mcp-repo-url> orcho-mcp
```

Create the core environment:

```bash
cd ~/orcho-preview/orcho-core
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Install MCP into the same environment:

```bash
pip install -e ../orcho-mcp
```

Add shell helpers:

```bash
export ORCHO_CORE="$HOME/orcho-preview/orcho-core"

orcho() {
  (
    source "$ORCHO_CORE/.venv/bin/activate" &&
    "$ORCHO_CORE/.venv/bin/python" -m cli.orcho "$@"
  )
}

orcho-mcp() {
  (
    source "$ORCHO_CORE/.venv/bin/activate" &&
    "$ORCHO_CORE/.venv/bin/python" -m orcho_mcp "$@"
  )
}
```

Check:

```bash
orcho --help
orcho-mcp --help
command -v orcho-mcp
```

For early-adopter source installs, the MCP command is usually the absolute
path inside the shared Orcho environment:

```bash
export ORCHO_MCP_COMMAND="$ORCHO_CORE/.venv/bin/orcho-mcp"
test -x "$ORCHO_MCP_COMMAND"
```

## Connect the tester project in place

Enter the existing repository and initialise Orcho:

```bash
cd ~/www/my-project
orcho workspace init
```

Orcho prints the external managed workspace path. The repository is not moved,
and later CLI runs resolve the workspace from `--project`.

For MCP, write a project-local config snippet:

```bash
orcho workspace init ~/www/my-project \
  --mcp-config ~/www/my-project/.mcp.json \
  --mcp-server-name orcho-my-project \
  --orcho-mcp-command "$ORCHO_MCP_COMMAND"
```

If the editable console script is not present in `.venv/bin`, use:

```bash
--orcho-mcp-command "$ORCHO_CORE/.venv/bin/python -m orcho_mcp"
```

Some MCP clients require `command` and `args` separately. In that case,
edit `.mcp.json` to:

```json
{
  "mcpServers": {
    "orcho-my-project": {
      "command": "/Users/me/orcho-preview/orcho-core/.venv/bin/python",
      "args": ["-m", "orcho_mcp"],
      "env": {
        "ORCHO_WORKSPACE": "/Users/me/.local/share/orcho/workspaces/my-project-<digest>/workspace-orchestrator"
      }
    }
  }
}
```

Restart the MCP client after changing `.mcp.json`.

Client-specific setup differs. Codex CLI/app uses `codex mcp add`,
Claude Code uses `claude mcp add`, Gemini CLI uses `gemini mcp add`,
and GUI clients usually read JSON config files. The canonical
copy-paste instructions live in
`../orcho-mcp/docs/mcp_client_setup.md`.

## First real run

Pick a small task:

```bash
orcho run \
  --task "Add input validation: return 400 if email is empty" \
  --project ~/www/my-project
```

Then inspect:

```bash
orcho status
orcho evidence

cd ~/www/my-project
git diff
```

## What to tell testers

- Orcho will edit files in the project they point it at.
- First run should be a small real task.
- Use a branch or disposable copy if they want to explore safely.
- The workspace is state, not source code.
- The MCP server is tied to one workspace through `ORCHO_WORKSPACE`.
- For intentional cross-project work, create a shared group workspace and
  register its project list explicitly.

## Public package path

For the native CLI path, the package path is:

```bash
pipx install orcho
cd ~/www/my-project
orcho workspace init
```

Plain `orcho` installs the core CLI and MCP server. The historical extras
remain aliases:

```bash
pipx install 'orcho[mcp]'
pipx install 'orcho[all]'
```

For the isolated container path, use the official image and mount a credential
directory:

```bash
docker pull ghcr.io/symphos-ai/orcho
docker run --rm -it \
  -v "$PWD":/workspace \
  -v ~/.orcho-auth:/agent-auth:ro \
  ghcr.io/symphos-ai/orcho \
  orcho --help
```
