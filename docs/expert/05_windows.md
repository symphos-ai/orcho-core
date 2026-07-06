# Windows — installation and setup

## Requirements

- Python 3.12+ (download: https://python.org)
- Git for Windows (download: https://git-scm.com/download/win)
- Claude CLI (`npm install -g @anthropic-ai/claude-code`)
- Codex CLI (`npm install -g @openai/codex`)
- Node.js 18+ (for the Claude and Codex CLIs)

---

## Installation

```powershell
# 1. Clone the stable engine
git clone git@github.com:symphos-ai/orcho-core.git "$env:LOCALAPPDATA\orcho-core"

# 2. Create a venv and install
cd "$env:LOCALAPPDATA\orcho-core"
python -m venv .venv
.\.venv\Scripts\pip install -e ".[dev]"

# 3. Hook up the PowerShell profile
Add-Content $PROFILE ". `"$env:LOCALAPPDATA\orcho-core\shell\orcho-env-base.ps1`""

# 4. Restart PowerShell and verify
orcho --help
```

---

## Verify the installation

```powershell
# Check that the binaries are found
python -c "from core.infra.config import get_claude_bin, get_codex_bin; print(get_claude_bin(), get_codex_bin())"

# Test without an API
orcho run --mock --task "Hello world" --project C:\path\to\project
```

---

## If claude/codex are not found

```powershell
# Set the paths explicitly
$env:CLAUDE_BIN = "$env:APPDATA\npm\claude.cmd"
$env:CODEX_BIN  = "$env:APPDATA\npm\codex.cmd"
```

Or add them to `$PROFILE` permanently.

---

## Windows specifics

- Node.js `.cmd` shims are launched via `cmd /c` automatically (no manual step needed)
- Paths use `\` — Orcho accepts both formats (`/` and `\`)
- `ORCHO_CORE` default: `%LOCALAPPDATA%\orcho-core`
- `ORCHO_RUNSPACE` default: `%ORCHO_WORKSPACE%\runspace`
- **Agent output streaming:** Windows has no pseudo-terminal, so Orcho streams
  each agent process over a pipe (drained by a background reader thread) instead
  of a PTY. Agents therefore run without a controlling terminal — their stdout
  is a plain pipe, so a CLI that changes its output when `stdout` is not a TTY
  behaves as it would under any non-interactive pipe. Live output still streams
  line-by-line to `output.log` exactly as on macOS and Linux.

---

## WSL2 (alternative)

If you prefer a Unix environment:

```bash
# In a WSL2 terminal — installation is the same as on Linux
git clone git@github.com:symphos-ai/orcho-core.git ~/.local/share/orcho-core
cd ~/.local/share/orcho-core
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Projects must live in the WSL2 file system (`~/`), not in `/mnt/c/` — otherwise git is slow.
