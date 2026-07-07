# Claude-Compatible GLM Runtime

Use this guide when you want Orcho to treat a GLM Coding Plan-backed Claude
Code wrapper as a separate runtime named `claude-glm`.

The important design point is that `claude-glm` is not a hidden replacement
for `claude`. It is a distinct executable and a distinct runtime identity in
Orcho events, metrics, retry labels, and phase routing. That makes mixed
workflows clear: for example, the plan phase can run on `claude` while the
implementation and repair phases run on `claude-glm`.

## Prerequisites

- A working `claude` CLI.
- A GLM Coding Plan key or equivalent Claude-compatible credential.
- A local `claude-glm` executable that accepts the same non-interactive
  Claude Code flags Orcho uses, including `--print`, `--model`, output format,
  and permission-mode flags.

Orcho does not manage external account setup. It resolves and invokes the
`claude-glm` command, records the runtime as `claude-glm`, and lets the wrapper
provide the correct endpoint, token, and model defaults to the underlying CLI.

## Create the wrapper

After a normal `pipx install orcho` or `pipx install orcho-core`, install the
packaged wrapper with:

```bash
orcho runtimes install claude-glm
```

That writes `~/.local/bin/claude-glm` by default. To choose a different
location:

```bash
orcho runtimes install claude-glm --path /absolute/path/to/claude-glm
```

From a source checkout, the equivalent manual command is:

```bash
mkdir -p "$HOME/.local/bin"
install -m 0755 core/_runtime_wrappers/claude-glm.sh "$HOME/.local/bin/claude-glm"
```

Orcho discovers `~/bin/claude-glm`, `~/.local/bin/claude-glm`, common Homebrew
locations, and `CLAUDE_GLM_BIN`. If you use another location, set:

```bash
export CLAUDE_GLM_BIN="/absolute/path/to/claude-glm"
```

The example wrapper reads `ANTHROPIC_AUTH_TOKEN` first. On macOS it can also
read the key from Keychain service `zai-coding-plan-key`:

```bash
printf "Z.AI Coding Plan key: "
stty -echo
IFS= read -r ZAI_CODING_PLAN_KEY
stty echo
printf "\n"
security add-generic-password -U -a "$USER" -s zai-coding-plan-key -w "$ZAI_CODING_PLAN_KEY"
unset ZAI_CODING_PLAN_KEY
```

## Verify the wrapper

First verify that the wrapper itself is executable:

```bash
"$HOME/.local/bin/claude-glm" --version
```

Then verify that Orcho resolves it as a separate runtime:

```bash
CLAUDE_GLM_BIN="$HOME/.local/bin/claude-glm" \
python -c 'from agents.runtimes.claude_glm import ClaudeGlmAgent; print(ClaudeGlmAgent(model="glm-5.2[1m]").bin)'
```

When credentials are ready, run a tiny model smoke test:

```bash
"$HOME/.local/bin/claude-glm" --print --model 'glm-5.2[1m]' 'Reply OK only.'
```

## Route phases to GLM

For a one-off run:

```bash
orcho run \
  --task "Implement the approved plan" \
  --project /path/to/project \
  --runtime-implement claude-glm \
  --model-implement 'glm-5.2[1m]' \
  --runtime-repair-changes claude-glm \
  --model-repair-changes 'glm-5.2[1m]'
```

For a workspace, set phase routing in `.orcho/config.local.json`:

```json
{
  "phases": {
    "plan": {"runtime": "claude", "model": "claude-opus-4-8[1m]", "effort": "high"},
    "validate_plan": {"runtime": "codex", "model": "gpt-5.5", "effort": "medium"},
    "implement": {"runtime": "claude-glm", "model": "glm-5.2[1m]", "effort": "medium"},
    "repair_changes": {"runtime": "claude-glm", "model": "glm-5.2[1m]", "effort": "medium"},
    "review_changes": {"runtime": "codex", "model": "gpt-5.5", "effort": "medium"},
    "final_acceptance": {"runtime": "codex", "model": "gpt-5.5", "effort": "low"}
  }
}
```

The resulting run artifacts and metrics will say `claude-glm` for the GLM-backed
phases. That is the expected signal that Orcho is using the wrapper runtime
instead of the original `claude` runtime.

## Troubleshooting

If Orcho says `CLAUDE_GLM_BIN` is missing, either put the wrapper in one of the
discovery locations or set `CLAUDE_GLM_BIN` to the absolute path.

If `claude-glm` says the token is missing, store the key in Keychain as shown
above or export `ANTHROPIC_AUTH_TOKEN` for the process that starts Orcho.

If a run starts but fails after invocation, check that the wrapper preserves
Claude Code's non-interactive flags and that the model name matches the GLM
endpoint you configured.
