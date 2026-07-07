# Configuration — all environment variables

## Settings precedence

```
env vars
  >  $ORCHO_WORKSPACE/.orcho/config.local.json
  >  ~/.orcho/config.local.json
  >  _config/config.local.json
  >  _config/config.defaults.json
```

## Environment variables

### Paths

| Variable | Default | Purpose |
|-----------|-------------|-----------|
| `ORCHO_CORE` | `~/.local/share/orcho-core` | Path to the stable engine |
| `ORCHO_CORE_DEV` | `~/www/orcho` | Path to the dev copy |
| `ORCHO_WORKSPACE` | — | Current workspace |
| `ORCHO_RUNSPACE` | `$ORCHO_WORKSPACE/runspace` | Where run results are written |

### Models

The canonical override is the per-phase variable `MODEL_<PHASE>` (see `_PHASE_ENV_MAP` in `core/infra/config.py`). It covers all 7 phases granularly:

| Variable | Default | Phase |
|-----------|-------------|------|
| `MODEL_PLAN` | `claude-opus-4-8[1m]` | plan |
| `MODEL_VALIDATE_PLAN` | `gpt-5.5` | validate_plan |
| `MODEL_IMPLEMENT` | `claude-opus-4-8[1m]` | implement |
| `MODEL_REVIEW_CHANGES` | `gpt-5.5` (codex) | review_changes |
| `MODEL_REPAIR_CHANGES` | `claude-opus-4-8[1m]` | repair_changes |
| `MODEL_REPAIR_ESCALATION` | `claude-opus-4-8[1m]` | repair_escalation |
| `MODEL_FINAL_ACCEPTANCE` | `gpt-5.5` | final_acceptance |

`CODEX_MODEL` (env var) is the fallback for `MODEL_REVIEW_CHANGES` when the latter is unset.

The same applies to runtimes: `RUNTIME_<PHASE>` accepts any registered runtime
id, including the built-ins `claude`, `claude-glm`, `codex`, and `gemini`.

### Binaries (if not found automatically)

| Variable | Example |
|-----------|--------|
| `CLAUDE_BIN` | `/custom/path/to/claude` |
| `CLAUDE_GLM_BIN` | `/custom/path/to/claude-glm` |
| `CODEX_BIN` | `/custom/path/to/codex` |

`claude-glm` is a Claude-compatible wrapper runtime with separate metrics and
event labels. See [../guides/claude_glm_runtime.md](../guides/claude_glm_runtime.md)
for the wrapper contract, key setup, and smoke tests.

### Timeouts

Hard timeout (`*_TIMEOUT`) is off by default (`0` / unset): long agent runs are not cut off just because of wall-clock duration. Protection against a real hang comes from the idle watchdog: the process is killed if it is alive but has not streamed output for longer than the threshold.

| Variable | Default |
|-----------|-------------|
| `CLAUDE_IDLE_TIMEOUT` | `1800` (seconds without output) |
| `CODEX_IDLE_TIMEOUT` | `900` (seconds without output) |
| `GEMINI_IDLE_TIMEOUT` | `900` (seconds without output) |
| `CLAUDE_TIMEOUT` | off (`0`) |
| `CODEX_TIMEOUT` | off (`0`) |
| `GEMINI_TIMEOUT` | off (`0`) |

### Artifact language

| Variable | Default |
|-----------|-------------|
| `PLAN_LANGUAGE` | `Russian` |
| `TASK_LANGUAGE` | `Russian` |
| `CONTENT_LANGUAGE` | `English` (language of outward delivery artifacts — commit message, PR title/body) |

---

## config.local.json — local overrides

`orcho workspace init` creates the first workspace-local config:

```
$ORCHO_WORKSPACE/.orcho/config.local.json
```

This is the main file for settings of a specific workspace: models, effort,
timeouts, artifact language, session policy, pipeline knobs, and the artifact
mirror. The file is created with the real current values from the defaults,
package-local, and user-global layers, so it can be read and edited right
away. A repeated `workspace init` does not overwrite the file.

For machine-wide settings use:

```
~/.orcho/config.local.json
```

For quick DEV edits the package-level gitignored file still works:

```
_config/config.local.json
```

Example:

```json
{
  "phases": {
    "plan":             {"runtime": "claude", "model": "claude-opus-4-8[1m]", "effort": "high"},
    "validate_plan":    {"runtime": "codex",  "model": "gpt-5.5",         "effort": "medium"},
    "implement":        {"runtime": "claude-glm", "model": "glm-5.2[1m]", "effort": "medium"},
    "review_changes":   {"runtime": "codex",  "model": "gpt-5.5",         "effort": "medium"},
    "repair_changes":   {"runtime": "claude-glm", "model": "glm-5.2[1m]", "effort": "medium"},
    "final_acceptance": {"runtime": "codex",  "model": "gpt-5.5",         "effort": "low"}
  },
  "timeouts": {
    "claude_idle_seconds": 1800,
    "codex_idle_seconds": 900,
    "gemini_idle_seconds": 900,
    "claude_seconds": 0,
    "codex_seconds": 0,
    "gemini_seconds": 0
  },
  "language": {
    "plan_language": "English",
    "task_language": "English",
    "content_language": "English"
  }
}
```

Keys in `phases` match the canonical phase names from `_PHASE_ENV_MAP`
(`core/infra/config.py`). Local layers are an overlay: you can set
just one field, for example `{"phases":{"implement":{"effort":"medium"}}}`,
and `runtime/model` stay as defined by the lower layer. `repair_escalation`
is optional — add it if the profile uses second-round repair.

Disable all local layers entirely for a deterministic run:

```bash
ORCHO_DISABLE_LOCAL_CONFIG=1 orcho ...
```

---

## Switching dev/stable

```bash
# Default — stable
orcho run --task "..." --project .

# Run from the dev copy (~/www/orcho)
orcho-dev run --task "..." --project .
```
