# core/ — subdomains

## Why subdomains

`core/` is the infrastructure layer. Before the refactor it was flat (10+ files in one folder).
Subdomains give explicit responsibility boundaries and prevent god objects.

**Rule:** modules within one subdomain may import each other.
Across subdomains — only through the public API (`__init__.py`).

---

## core/infra/ — config and platform

### `config.py`

| What | Type | Purpose |
|----|-----|-----------|
| `AppConfig` | `@dataclass(frozen=True)` | Immutable config, lazy singleton |
| `AppConfig.load()` | `@classmethod @cache` | The only way to obtain the config |
| `_find_binary()` | `fn(name, candidates) -> str` | Binary lookup (env → PATH → candidates) |
| `_wrap_windows_cmd()` | `fn(bin_path) -> list[str]` | `.cmd` wrapper for subprocess on Windows |
| `get_claude_bin()` | `fn() -> str` | Specialized `_find_binary` for claude |
| `_reset_config()` | test helper | Reset the `AppConfig.load()` cache between tests |

**Important:** `AppConfig.load()` is cached forever. In tests, use `_reset_config()`.

### `platform.py`

| What | Type | Purpose |
|----|-----|-----------|
| `_IS_WINDOWS` | `bool` | The only OS check in the package |
| `engine_home()` | `fn() -> Path` | Path to the stable engine (`$ORCHO_CORE`) |
| `workspace_dir()` | `fn() -> Path\|None` | Current workspace (`$ORCHO_WORKSPACE`) |
| `runspace_dir()` | `fn() -> Path` | Where to write runs/ (`$ORCHO_RUNSPACE`) |
| `default_engine_home()` | `fn() -> Path` | Platform-specific default install path |
| `claude_candidates()` | `fn() -> list[str]` | claude binary search paths (per OS) |
| `codex_candidates()` | `fn() -> list[str]` | codex binary search paths (per OS) |

---

## core/observability/ — logging and metrics

### `logging.py`

Colored stdout output. No files — console only.
File logging is configured in `pipeline/engine/run_logging.py`.

```python
banner("Phase 1: BUILD")     # bold phase header
success("Done in 45s")       # green
warn("Retrying...")           # yellow
C.BOLD, C.GREEN, C.RESET      # color constants
```

### `metrics.py`

```python
class MetricsCollector:
    def record_phase(self, phase: str, tokens_in: int, tokens_out: int, duration_s: float): ...
    def add_round(self, count: int = 1) -> None: ...
    def as_dict(self) -> dict: ...
    def save(self, output_dir: Path) -> Path: ...           # → metrics.json
    def load_from_disk(self, path: Path) -> int: ...        # cross-subprocess merge on resume

def load_historical_runs(runspace_dir: Path, limit: int = 10) -> list[dict]:
    """Reads metrics.json from the last N runs for `orcho metrics`."""
```

`load_from_disk` rehydrates `_phases` / `_rounds` / `_total_retries`
from a prior `metrics.json` snapshot (written by the pause-side
writer in `_apply_phase_handoff_pause`) so a resume subprocess
extends rather than replaces the prior subprocess's work. Best-
effort: missing file / malformed JSON / malformed entries return
`0` and leave the collector empty. See
[ADR 0035](../adr/0035-terminal-status-and-resume-observability.md)
for the cross-subprocess metrics aggregation contract, and
[Run artifacts](../reference/run_artifacts.md#metricsjson) for the
on-disk shape.

### `trace.py`

Only with `--output debug` (or the legacy alias `--verbose` / `-v`).
Does not affect production output. `apply_output_mode("debug")` in
`logging.py` enables tracing itself.

```python
vtrace("Loading plugin", plugin_path)   # prints with --output debug
vdump("RunContext", ctx)                 # formatted object dump
with vtimed("codex review"):            # measures block duration
    result = codex.review(...)
```

---

## core/io/ — input/output

### `retry.py`

```python
@dataclass
class RetryPolicy:
    max_attempts: int = 3
    base_delay_s: float = 5.0
    error_types: tuple[type[Exception], ...] = (...)

def call_with_retry(fn, *args, policy=RetryPolicy(), **kwargs):
    """Call fn with automatic retries on API errors."""
```

Classifies errors: `RateLimitError`, `ApiTimeoutError`, `ContextWindowError`.
`ContextWindowError` is **not** retried (a retry would not help).

### `git_helpers.py`

Thin wrappers around the `git` CLI. No logic — just calls.

```python
has_uncommitted(cwd: str) -> bool       # are there uncommitted changes
git_diff_stat(cwd: str) -> str          # git diff --stat output
```

### `prompt_loader.py`

```python
def render_prompt(name: str, project_dir: Path | None = None, **vars) -> str:
    """Load and render a prompt with 3-level resolution.

    Resolution order (first found wins):
    1. project_dir/.orcho/multiagent/prompts/{name}.md
    2. workspace_dir()/.orcho/multiagent/prompts/{name}.md
    3. core.infra.paths.PROMPTS_DIR/{name}.md
    """
```

Core asset paths resolve through `core.infra.paths` (`CONFIG_DIR` /
`PROMPTS_DIR`) rather than ad-hoc `__file__` parent walks.

---

## core/context/ — codemap

Optional codebase analysis. Disabled by default (`codemap.enabled: false`).

```python
def build_repo_map(project_dir: Path) -> str:
    """Builds a symbol map of the repository.

    Priority: tree-sitter (if installed) → regex fallback.
    Supports: Python, C#, PHP, TypeScript.
    """

def inject_context(prompt: str, codemap: str) -> str:
    """Appends the codemap to the end of the prompt if non-empty."""
```

To enable:
```json
// _config/config.local.json
{ "codemap": { "enabled": true } }
```

Optional grammars (improve parsing quality):
```bash
pip install "orcho-core[codemap]"
```

---

## profiles_v2 overlay — operator override without editing built-in JSON

The built-in profiles (`advanced`, `enterprise`, `plan`, `lite`, `review`, `task`)
live in `_config/pipeline_profiles_v2.json` and are part of the installed
package. A direct edit of that file survives exactly one `orcho-promote` run.

For durable overrides without forking a profile, use the `profiles_v2` block in
any JSON config layer. Author team-wide overlays in workspace `config.json`;
use `config.local.json` for personal ones:

```json
// $ORCHO_WORKSPACE/.orcho/config.json (shared)
// or $ORCHO_WORKSPACE/.orcho/config.local.json (personal)
{
  "profiles_v2": {
    "advanced": {
      "validate_plan": {
        "handoff": {"type": "human_feedback_always"}
      }
    }
  }
}
```

Semantics:

* The top-level key is the built-in profile name. If the name does not exist —
  `ProfileLoadError` pointing at the typo.
* The next level is `phase` (a phase name within the profile). The loader walks
  `steps` + `loop.steps` recursively and finds the step by phase name. If the
  phase name does not occur in this profile, or occurs twice —
  `ProfileLoadError` (a by-phase-name overlay must be unambiguous).
* The patch dict is deep-merged into the found step. Sibling fields
  (`execution`, `prompt`, ...) that the overlay does not mention stay
  untouched. Lists/scalars are replaced wholesale — to extend
  `quality_gates` you would have to repeat the whole array.

Precedence (high → low, same as the other overlay sections):
1. `$ORCHO_WORKSPACE/.orcho/config.local.json`
2. `$ORCHO_WORKSPACE/.orcho/config.json`
3. `~/.orcho/config.local.json`
4. `_config/config.local.json` (package)

Environment variables are above this list. From low to high, the complete
order is package → user → workspace shared → workspace personal → environment.
The workspace names follow `settings.json` / `settings.local.json`: commit the
shared team policy and keep the personal override gitignored.

To disable the overlay path entirely (for CI / a harness pinned to the
built-in shape): `ORCHO_DISABLE_LOCAL_CONFIG=1`.

Apply happens in `pipeline.profiles.loader.load_profiles_v2` before
`parse_profile` is called, so all the usual dataclass invariants (unknown
`handoff.type`, conflicting `human_review`+`handoff`, runtime support
matrix) apply to overridden profiles exactly as they do to built-ins.
