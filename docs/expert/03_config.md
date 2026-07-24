# Configuration — all environment variables

## Settings precedence

| Priority (low → high) | Path | Audience |
|---:|---|---|
| 0 | `_config/config.defaults.json` | shipped defaults |
| 1 | `_config/config.local.json` | package-local developer override |
| 2 | `~/.orcho/config.local.json` | personal user preference |
| 3 | `$ORCHO_WORKSPACE/.orcho/config.json` | committable team workspace policy |
| 4 | `$ORCHO_WORKSPACE/.orcho/config.local.json` | gitignored personal workspace override |
| 5 | environment variables | process-specific override |

The workspace pair follows the familiar `settings.json` / `settings.local.json`
naming pattern: the un-suffixed file is safe to share, while the `.local` file
is personal and wins over it. `ORCHO_DISABLE_LOCAL_CONFIG=1` disables every
non-default JSON overlay in this table; environment variables still apply.

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

## Shared and personal workspace configuration

`orcho workspace init` scaffolds both workspace files without overwriting an
existing file:

```
$ORCHO_WORKSPACE/.orcho/config.json
$ORCHO_WORKSPACE/.orcho/config.local.json
```

Put team policy that should be reviewed and committed in `config.json`.
It starts as a neutral comment-only JSON object, so a fresh workspace does not
silently change runtime behavior. `.orcho/.gitignore` ignores only
`config.local.json`, not `config.json`.

Use `config.local.json` for personal workspace preferences: models, effort,
timeouts, artifact language, session policy, pipeline knobs, artifact mirror,
and workspace project aliases. It is generated with concrete starting values
from defaults plus package/user personal layers, then overrides the shared
workspace file. Repeated `workspace init` never overwrites either file.

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

### Delivery publication

`commit.publish` accepts exactly `off`, `auto`, or `always`; the default is
`auto`. Invalid, blank, and unknown values safely fall back to `auto`.
`off` avoids provider discovery. `always` requests best-effort publication only
after a successful `commit_on_branch` delivery; it does not push the current or
default branch, and `commit_in_place` is never published by this setting.

For a protected-default workspace, the minimal local override is:

```json
{"commit":{"publish":"always"}}
```

An opened PR URL is confirmation of publication. Without one, treat the branch
as ready for manual publication and consult the delivery notices/warnings; do
not infer that it was pushed.

### profiles_v2 overlays

Built-in execution profiles can be tuned through the `profiles_v2` block in
either workspace file. Put team-wide overlays in shared `config.json` by hand;
prefer the CLI writer for a validated personal override:

```bash
orcho profile customize feature --mode pro --phase-effort implement=high
```

The stored shape is a per-profile overlay keyed by `_profile` for top-level
profile fields and by phase name for phase-step fields:

```json
{
  "profiles_v2": {
    "feature": {
      "_profile": {"default_mode": "pro"},
      "implement": {"effort": "high"},
      "validate_plan": {
        "handoff": {"type": "human_feedback_always"}
      }
    }
  }
}
```

The loader deep-merges each phase patch into the built-in profile before normal
profile validation, so the same schema errors surface for local overlays as for
the shipped JSON.

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
