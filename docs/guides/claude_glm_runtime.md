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
| `config_dir` | `~/.orcho/claude-glm-config` |

Set team or personal defaults in the `claude_glm` section of the normal Orcho
configuration. For example:

```json
{
  "claude_glm": {
    "opus_model": "glm-5.3",
    "sonnet_model": "glm-5.3",
    "haiku_model": "glm-4.7",
    "max_context_tokens": 200000,
    "config_dir": ""
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
| `CLAUDE_GLM_CONFIG_DIR` | `config_dir` |

The context override must be a positive integer. Model overrides must be
non-empty strings. An empty `config_dir` means the per-user default above.

## Why this runtime keeps its own CLI configuration directory

The adapter sets `CLAUDE_CONFIG_DIR` for the child it launches, and does not
let the ambient one through. The CLI resolves credentials from its
configuration directory in preference to the environment, so a child that
inherited the operator's ordinary directory would authenticate as whoever is
logged in there rather than with the GLM token — which fails against the GLM
endpoint even though the token is valid.

Setting `CLAUDE_CONFIG_DIR` by hand does not solve this, and is worth
understanding before trying: it is process-global, so isolating this runtime
that way also strips the credentials of every other Claude-family runtime in
the same pipeline. An adapter-owned directory is what makes a mixed-vendor
run — some phases on this runtime, others on the ordinary one — possible at
all.

The directory holds onboarding and trust state as well as credentials, so it
is per-user and stable rather than per-run: a location inside a run's
checkout would be discarded between runs and would dirty the tree the run is
judged on. Point `config_dir` somewhere else if you need to, but keep those
two properties. See [the configuration reference](../expert/03_config.md)
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

Four flags cover every phase, because two of them govern a group:
`--runtime-repair-changes` also sets `repair_escalation`, and
`--runtime-review-changes` also sets `validate_plan` and `final_acceptance`.
Passing all four therefore routes a whole pipeline to this runtime. Passing
only some leaves the rest on their configured defaults — a partial swap that
looks like a whole one until a phase authenticates against the wrong endpoint.
To set a grouped phase independently of its siblings, use the `phases.*`
configuration block, which is per-phase.

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

### `401 authentication_failed` on a token that works elsewhere

If the same token succeeds against the GLM endpoint with `curl` but every
phase on this runtime fails authentication, the child is authenticating as
something other than that token. Run the CLI directly with the adapter's
environment and look at `apiKeySource` in its output: `"none"` means it
resolved credentials from a configuration directory instead of the
environment.

The adapter now supplies its own configuration directory, so this should not
occur; on releases before that fix it happened on every host with a logged-in
Claude subscription. If you see it, check which `config_dir` the adapter
resolved before changing anything else — and do not set `CLAUDE_CONFIG_DIR`
yourself as a workaround, for the reason given above.

### `[claude-code:unrecognized_model]` in the output

This line is printed on every call and is harmless — the request proceeds and
returns `rc=0`. It is a consequence of passing a model alias, which is what
makes the CLI substitute `ANTHROPIC_DEFAULT_OPUS_MODEL`; that substitution
works. Do not read it as the cause of a failing run.
