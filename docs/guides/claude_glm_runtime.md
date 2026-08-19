# Claude-Compatible GLM Runtime

Use this guide to route selected phases through GLM while continuing to invoke
the installed plain `claude` CLI. `claude-glm` is an Orcho runtime identity,
not an executable, shell script, or replacement command. Its distinct identity
is retained in phase routing, events, metrics, and retry labels.

When Orcho invokes this runtime, the adapter gives the child `claude` process
the GLM endpoint, `ANTHROPIC_AUTH_TOKEN`, model defaults, and context settings.
Those values are adapter-owned: they do not change the parent shell or the
ordinary `claude` runtime.

## Prerequisites

- A working plain `claude` CLI on `PATH`, or an absolute executable path set in
  `CLAUDE_GLM_BIN`.
- A GLM Coding Plan credential exported as `ANTHROPIC_AUTH_TOKEN` in the
  process that starts Orcho.

For example, in a POSIX shell:

```bash
export ANTHROPIC_AUTH_TOKEN='<GLM Coding Plan key>'
orcho run --task "Implement the approved plan" --project /path/to/project \
  --runtime-implement claude-glm --model-implement glm-5.3
```

`CLAUDE_GLM_BIN` is optional. It is the path to the underlying
Claude-compatible executable (normally a `claude` executable), not a path to a
`claude-glm` command. If it is unset, the runtime follows `CLAUDE_BIN` and
normal `claude` discovery. A set value must name an existing file:

```bash
export CLAUDE_GLM_BIN='/opt/claude/bin/claude'
```

## Windows setup

In PowerShell, set the token in the parent process and run Orcho directly in
that same process. No extra command or shell layer is needed around the prompt:

```powershell
$env:ANTHROPIC_AUTH_TOKEN = '<GLM Coding Plan key>'
orcho run --task "Implement the approved plan" --project C:\work\api --runtime-implement claude-glm --model-implement glm-5.3
```

If `claude` is not on `PATH`, point `CLAUDE_GLM_BIN` directly at the
Claude-compatible executable:

```powershell
$env:CLAUDE_GLM_BIN = 'C:\Program Files\Claude\claude.exe'
orcho run --task "Implement the approved plan" --project C:\work\api --runtime-implement claude-glm --model-implement glm-5.3
```

To make the token available to later terminals, set it at User scope, then
open a new PowerShell window:

```powershell
[Environment]::SetEnvironmentVariable('ANTHROPIC_AUTH_TOKEN', '<GLM Coding Plan key>', 'User')
```

## Adapter defaults and overrides

The shipped adapter defaults are:

| Setting | Default |
|---------|---------|
| `opus_model` | `glm-5.3` |
| `sonnet_model` | `glm-5.3` |
| `haiku_model` | `glm-4.7` |
| `max_context_tokens` | `200000` |

Set team or personal defaults in the `claude_glm` section of the normal Orcho
configuration. For example:

```json
{
  "claude_glm": {
    "opus_model": "glm-5.3",
    "sonnet_model": "glm-5.3",
    "haiku_model": "glm-4.7",
    "max_context_tokens": 200000
  }
}
```

JSON follows the normal configuration order: shipped defaults, package-local,
user, workspace shared, then workspace personal. The following environment
variables override the final JSON values for the process that launches Orcho:

| Environment variable | `claude_glm` field |
|---------|---------|
| `CLAUDE_GLM_OPUS_MODEL` | `opus_model` |
| `CLAUDE_GLM_SONNET_MODEL` | `sonnet_model` |
| `CLAUDE_GLM_HAIKU_MODEL` | `haiku_model` |
| `CLAUDE_GLM_MAX_CONTEXT_TOKENS` | `max_context_tokens` |

The context override must be a positive integer. Model overrides must be
non-empty strings. See [the configuration reference](../expert/03_config.md)
for the full configuration-layer table.

## Route phases to GLM

For a one-off run:

```bash
orcho run \
  --task "Implement the approved plan" \
  --project /path/to/project \
  --runtime-implement claude-glm \
  --model-implement glm-5.3 \
  --runtime-repair-changes claude-glm \
  --model-repair-changes glm-5.3
```

For a team-wide workspace policy, use `.orcho/config.json`; use
`.orcho/config.local.json` for a personal override:

```json
{
  "phases": {
    "plan": {"runtime": "claude", "model": "claude-opus-4-8[1m]", "effort": "high"},
    "validate_plan": {"runtime": "codex", "model": "gpt-5.5", "effort": "medium"},
    "implement": {"runtime": "claude-glm", "model": "glm-5.3", "effort": "medium"},
    "repair_changes": {"runtime": "claude-glm", "model": "glm-5.3", "effort": "medium"},
    "review_changes": {"runtime": "codex", "model": "gpt-5.5", "effort": "medium"},
    "final_acceptance": {"runtime": "codex", "model": "gpt-5.5", "effort": "low"}
  }
}
```

Run artifacts and metrics record `claude-glm` for GLM-backed phases. This is
the expected signal that the adapter, rather than the ordinary `claude`
runtime, launched the phase.

## Troubleshooting

If Orcho reports that `ANTHROPIC_AUTH_TOKEN` is missing, export it in the
parent shell before starting Orcho. If `CLAUDE_GLM_BIN` is rejected, correct it
to an existing plain Claude-compatible executable or unset it to use normal
`claude` discovery. If the run fails after invocation, confirm that the
configured model is accepted by the GLM endpoint.
