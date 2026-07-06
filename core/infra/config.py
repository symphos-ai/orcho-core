"""
core/config.py — Runtime configuration for multi-agent core.

Layered loading (last wins):
    1. _config/config.defaults.json   (in git, defaults)
    2. local config layers            (gitignored / user / workspace)
    3. environment variables          (highest priority)

Discovers CLI tool paths automatically (no hardcoded paths).
Override via environment variables:
    CLAUDE_BIN   — path to claude binary
    CODEX_BIN    — path to codex binary

Per-phase model + runtime overrides come from environment variables
``MODEL_<PHASE>`` / ``RUNTIME_<PHASE>`` (see ``_PHASE_ENV_MAP``). Callers
that need a phase's default model at import time use
``config.phase_model(phase, default)``; callers that need the full
resolved spec (env + JSON) call ``AppConfig.load().phase_model_map``.

JSON schema: ``phases`` carries one ``{runtime, model[, effort]}`` object
per phase id (see ADR 0022 for the phase taxonomy).
"""

import json
import os
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

# ── JSON config layers ────────────────────────────────────────────────────────
from core.infra.paths import (
    CONFIG_DIR as _CONFIG_DIR,
    user_config_dir,
    workspace_config_dir,
)

_LOCAL_CONFIG_NAME = "config.local.json"
_LOCAL_CONFIG_DISABLE_VALUES = ("1", "true", "yes", "on")


def _extract_phases(raw: dict) -> dict[str, dict[str, str]]:
    """Extract the ``phases`` block from a raw config layer.

    Each entry is normalised to ``{runtime, model[, effort]}``. ``effort``
    is only set when configured so ``phase_effort_map`` yields ``None``
    and the underlying CLI keeps its own default.
    """
    src = raw.get("phases")
    if not isinstance(src, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for phase, spec in src.items():
        if not isinstance(spec, dict):
            continue
        entry = {
            "runtime": str(spec.get("runtime", "claude")),
            "model":   str(spec.get("model", "")),
        }
        if spec.get("effort"):
            entry["effort"] = str(spec["effort"])
        out[phase] = entry
    return out


def _extract_phase_overlay(raw: dict) -> dict[str, dict[str, str]]:
    """Extract only explicitly configured phase fields from a local layer.

    Unlike :func:`_extract_phases`, this helper never invents ``runtime``
    or ``model`` defaults. Local layers are overlays; a workspace that
    sets only ``implement.effort`` must not erase the model loaded from a
    lower-priority layer.
    """
    src = raw.get("phases")
    if not isinstance(src, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for phase, spec in src.items():
        if not isinstance(spec, dict):
            continue
        entry: dict[str, str] = {}
        for key in ("runtime", "model", "effort"):
            value = spec.get(key)
            if value is not None and value != "":
                entry[key] = str(value)
        if entry:
            out[phase] = entry
    return out


def _local_config_disabled() -> bool:
    raw = os.environ.get("ORCHO_DISABLE_LOCAL_CONFIG", "").strip().lower()
    return raw in _LOCAL_CONFIG_DISABLE_VALUES


def _iter_local_config_paths(
    *, workspace: Path | str | None = None
) -> Iterable[Path]:
    """Yield local config candidates in increasing priority order."""
    yield _CONFIG_DIR / _LOCAL_CONFIG_NAME
    yield user_config_dir() / _LOCAL_CONFIG_NAME
    if workspace is not None:
        yield Path(workspace).expanduser() / ".orcho" / _LOCAL_CONFIG_NAME
    elif ws_dir := workspace_config_dir():
        yield ws_dir / _LOCAL_CONFIG_NAME


def _merge_local_layer(cfg: dict, local: dict) -> None:
    for phase, spec in _extract_phase_overlay(local).items():
        cfg["phases"].setdefault(phase, {}).update(spec)
    # ``worktree`` (ADR 0033), ``pre_run_dirty`` (ADR 0044),
    # ``commit`` (ADR 0032/0043), and ``sandbox`` (ADR 0034) are
    # top-level sections that must be
    # reachable by ``config.local.json`` overlays
    # the same way ``pipeline`` / ``artifacts`` already are. Without
    # them, operator overrides for these sections silently fall back
    # to the hard-coded Python defaults — bypassing both
    # ``config.defaults.json`` and any local overlay layer.
    for section in ("timeouts", "session", "codemap", "hypothesis",
                    "language", "artifacts", "pipeline", "commit",
                    "worktree", "pre_run_dirty", "sandbox", "cli",
                    "accounting"):
        if section in local and isinstance(local[section], dict):
            overlay = {
                key: value
                for key, value in local[section].items()
                if not key.startswith("_") and value is not None
            }
            cfg.setdefault(section, {}).update(overlay)


def load_profile_overlays() -> dict[str, dict[str, dict]]:
    """Collect per-profile overlays from local config layers.

    Returns ``{profile_name: {patch_key: patch_dict}}``. Phase patch keys
    target a single PhaseStep by phase name; the reserved ``"_profile"``
    key patches the top-level profile object itself. The returned
    structure is the *aggregate* across all local layers — later layers
    in :func:`_iter_local_config_paths` (workspace > user > package) win per
    ``(profile, patch_key)`` key, mirroring the precedence the other overlay
    sections already use.

    Honors :envvar:`ORCHO_DISABLE_LOCAL_CONFIG` (returns ``{}`` when set)
    so test harnesses and CI runs can opt out of operator overrides.

    The overlay shape this helper consumes is intentionally flat by
    phase name:

    .. code-block:: json

        {
          "profiles_v2": {
            "delivery_audit": {
              "_profile": {"worktree_isolation": "off"}
            },
            "feature": {
              "validate_plan": {"handoff": {"type": "human_feedback_always"}}
            }
          }
        }

    Loader-side semantics (missing-phase / duplicate-phase errors,
    deep-merge into the JSON profile tree) live in
    :mod:`pipeline.profiles.loader`; this function is purely the
    layered read.
    """
    overlays: dict[str, dict[str, dict]] = {}
    if _local_config_disabled():
        return overlays
    for local_path in _iter_local_config_paths():
        if not local_path.exists():
            continue
        try:
            local = json.loads(local_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A bad local layer should not crash the engine; the
            # phase-level overlay path silently skips for the same
            # reason. The profile loader will still parse the bare JSON.
            continue
        block = local.get("profiles_v2")
        if not isinstance(block, dict):
            continue
        for profile_name, phase_patches in block.items():
            if not isinstance(phase_patches, dict):
                continue
            profile_bucket = overlays.setdefault(profile_name, {})
            for phase_name, patch in phase_patches.items():
                if not isinstance(patch, dict):
                    continue
                # Later layer wins per (profile, phase); replace, do
                # not deep-merge across layers. Deep-merge into the
                # built-in profile happens once at loader-apply time.
                profile_bucket[phase_name] = patch
    return overlays


def _merge_json_layers(*, workspace: Path | str | None = None) -> dict:
    """Merge config.defaults.json + local config layers into one dict.

    Local layer wins per-phase if both layers carry the same phase key.
    """
    defaults_path = _CONFIG_DIR / "config.defaults.json"
    if not defaults_path.exists():
        return {"phases": {}, "timeouts": {}}

    raw_defaults = json.loads(defaults_path.read_text(encoding="utf-8"))

    cfg: dict = {
        "phases":     _extract_phases(raw_defaults),
        "timeouts":   dict(raw_defaults.get("timeouts", {})),
        "session":    dict(raw_defaults.get("session",  {})),
        "codemap":    dict(raw_defaults.get("codemap",  {})),
        "hypothesis": dict(raw_defaults.get("hypothesis", {})),
        "language":   dict(raw_defaults.get("language", {})),
        "artifacts":  dict(raw_defaults.get("artifacts", {})),
        "pipeline":   dict(raw_defaults.get("pipeline", {})),
        "commit":     dict(raw_defaults.get("commit", {})),
        # ADR 0033 / 0034 / 0044: these top-level runtime-policy
        # sections must flow from ``config.defaults.json`` into the
        # merged config. Otherwise ``AppConfig.load`` falls back to
        # hard-coded Python defaults and local overlays are bypassed.
        "worktree":      dict(raw_defaults.get("worktree", {})),
        "pre_run_dirty": dict(raw_defaults.get("pre_run_dirty", {})),
        "sandbox":       dict(raw_defaults.get("sandbox", {})),
        "cli":           dict(raw_defaults.get("cli", {})),
        "accounting":    dict(raw_defaults.get("accounting", {})),
    }

    if _local_config_disabled():
        return cfg

    for local_path in _iter_local_config_paths(workspace=workspace):
        if not local_path.exists():
            continue
        local = json.loads(local_path.read_text(encoding="utf-8"))
        _merge_local_layer(cfg, local)
    return cfg


_RAW_CONFIG = _merge_json_layers()
_PHASES    = _RAW_CONFIG.get("phases", {})
_TIMEOUTS  = _RAW_CONFIG.get("timeouts", {})
_CLI       = _RAW_CONFIG.get("cli", {})

_OUTPUT_MODES = ("summary", "live", "debug")
_TRUE_VALUES = ("1", "true", "yes", "on")
_FALSE_VALUES = ("0", "false", "no", "off")
_SESSION_SPLIT_VALUES = ("stateless", "per_phase", "per_role", "common")


def cli_output_mode(default: str = "summary") -> str:
    """Default ``--output`` transcript mode for orcho CLI entry points.

    Precedence (high → low):

    1. ``ORCHO_OUTPUT_MODE`` environment variable.
    2. ``cli.output_mode`` from the JSON config layer
       (``_config/config.defaults.json`` + local config layers).
    3. ``default`` argument.

    Unknown values fall back to ``default`` so a stale local config
    cannot break argparse choice validation.
    """
    env_val = os.environ.get("ORCHO_OUTPUT_MODE")
    if env_val:
        env_val = env_val.strip().lower()
        if env_val in _OUTPUT_MODES:
            return env_val
    cfg_val = str(_CLI.get("output_mode", "")).strip().lower()
    if cfg_val in _OUTPUT_MODES:
        return cfg_val
    return default


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_VALUES:
            return True
        if normalized in _FALSE_VALUES:
            return False
    if isinstance(value, int) and not isinstance(value, bool):
        return bool(value)
    return default


def _resolve_accounting(raw: dict[str, Any]) -> dict[str, Any]:
    accounting_defaults: dict[str, Any] = {
        "enabled": False,
    }
    accounting_defaults.update(raw.get("accounting", {}))
    if v := os.environ.get("ORCHO_ACCOUNTING"):
        accounting_defaults["enabled"] = _coerce_bool(v, default=False)
    return accounting_defaults


def _parse_session_split_override(raw: Any) -> dict[str, str]:
    """Normalize pipeline.session_split_override.

    Accepted shapes:
    - JSON object: ``{"implement": "common"}``
    - CLI/env string: ``"implement=common,repair_changes=common"``

    Phase-name validation belongs to the active profile because profile
    projection can remove phases. This helper validates only the value domain
    and the syntactic ``phase=split`` contract.
    """
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, str):
        parsed: dict[str, str] = {}
        for chunk in raw.split(","):
            entry = chunk.strip()
            if not entry:
                continue
            if "=" not in entry:
                raise ValueError(
                    "pipeline.session_split_override entries must be "
                    f"'phase=split', got {entry!r}"
                )
            phase, split = entry.split("=", 1)
            parsed[phase.strip()] = split.strip()
        items = parsed.items()
    else:
        raise ValueError(
            "pipeline.session_split_override must be an object or a "
            f"comma-separated string, got {type(raw).__name__}"
        )

    out: dict[str, str] = {}
    for phase, split in items:
        phase_key = str(phase).strip()
        split_value = str(split).strip()
        if not phase_key:
            raise ValueError("pipeline.session_split_override has an empty phase")
        if split_value not in _SESSION_SPLIT_VALUES:
            raise ValueError(
                "pipeline.session_split_override "
                f"{phase_key!r}={split_value!r} is not one of "
                f"{list(_SESSION_SPLIT_VALUES)}"
            )
        out[phase_key] = split_value
    return out


def apply_session_split_override_env(values: Iterable[str] | None) -> None:
    """Apply CLI ``--session-split phase=split`` values for this process."""
    entries = [str(value).strip() for value in values or () if str(value).strip()]
    if not entries:
        return
    raw = ",".join(entries)
    # Validate before mutating env so argparse callers fail without leaving a
    # half-applied process-global override behind.
    _parse_session_split_override(raw)
    os.environ["ORCHO_SESSION_SPLIT_OVERRIDE"] = raw
    _reset_config()


def phase_model(phase: str, default: str = "") -> str:
    """Default model for a phase. Precedence (high → low):

    1. ``MODEL_<PHASE>`` environment variable (canonical per-phase override).
    2. ``phases.<phase>.model`` from the JSON config layer
       (``_config/config.defaults.json`` + local config layers).
    3. ``default`` argument.

    The full :func:`AppConfig.load` surface uses ``_PHASE_ENV_MAP`` which
    is wider (also resolves ``RUNTIME_<PHASE>`` and the entire phase
    spec); this helper is the cheap accessor when the caller only needs
    the model string.
    """
    env_val = os.environ.get(f"MODEL_{phase.upper()}")
    if env_val:
        return env_val
    spec = _PHASES.get(phase) or {}
    return str(spec.get("model", default))


def _phase_runtime(phase: str, default: str = "claude") -> str:
    spec = _PHASES.get(phase) or {}
    return str(spec.get("runtime", default))


# ── Binary discovery ─────────────────────────────────────────────────────────
def _find_binary(name: str, candidates: list[str]) -> str:
    """
    Find a binary by name. Checks:
    1. Environment variable override  (CLAUDE_BIN / CODEX_BIN)
    2. PATH via shutil.which
    3. Hardcoded candidate paths (in order)
    Raises RuntimeError if not found.
    """
    env_key = f"{name.upper()}_BIN"
    if env_val := os.environ.get(env_key):
        if Path(env_val).exists():
            return env_val
        raise RuntimeError(f"{env_key}={env_val!r} set but file not found")

    if found := shutil.which(name):
        return found

    for path in candidates:
        # expandvars handles Windows %APPDATA% / %LOCALAPPDATA%; expanduser
        # handles Unix ~/. Both are no-ops when not applicable.
        expanded = os.path.expandvars(os.path.expanduser(path))
        if Path(expanded).exists():
            return expanded

    raise RuntimeError(
        f"Cannot find '{name}' binary. "
        f"Install it or set {env_key}=/path/to/{name}"
    )


def _wrap_windows_cmd(bin_path: str) -> list[str]:
    """Return a subprocess prefix for ``.cmd`` shims on Windows.

    Node.js CLI tools installed via npm on Windows are ``.cmd`` batch scripts.
    They cannot be executed directly by ``subprocess`` without the shell — we
    use ``cmd /c`` as a thin wrapper instead of ``shell=True`` so we keep
    full control over argument quoting.

    On Unix this is a no-op: returns ``[bin_path]``.
    """
    import sys as _sys
    if _sys.platform == "win32" and bin_path.lower().endswith(".cmd"):
        return ["cmd", "/c", bin_path]
    return [bin_path]


def get_claude_bin() -> str:
    from core.infra.platform import claude_candidates
    return _find_binary("claude", claude_candidates())


def get_codex_bin() -> str:
    from core.infra.platform import codex_candidates
    return _find_binary("codex", codex_candidates())


def get_gemini_bin() -> str:
    from core.infra.platform import gemini_candidates
    return _find_binary("gemini", gemini_candidates())


# ── Workspace / runspace (lazy resolution) ──────────────────────────────────
# Раньше RUNSPACE_DIR / RUNS_DIR резолвились как module-level Path при import,
# из-за чего dashboard'овский выбор workspace игнорировался pipeline-ом
# (значения были зафиксированы при старте процесса). Теперь — функции,
# которые читают env при каждом вызове. Module-level proxy сохранены для
# тестов и dashboard.services.history (Wave 2 удалит).
from core.infra.platform import (  # noqa: E402  # late import: keeps the rationale comment grouped with the imports
    WorkspaceNotResolvedError,
    runspace_dir as _resolve_runspace,
    workspace_dir as _resolve_workspace,
)


def get_workspace_dir() -> Path:
    """Return current workspace root or raise WorkspaceNotResolvedError."""
    ws = _resolve_workspace()
    if ws is None:
        from core.infra.platform import _WORKSPACE_HINT
        raise WorkspaceNotResolvedError(_WORKSPACE_HINT)
    return ws


def get_runspace_dir() -> Path:
    """Return <workspace>/runspace or raise."""
    return _resolve_runspace()


def get_runs_dir() -> Path:
    """Return <workspace>/runspace/runs or raise."""
    return get_runspace_dir() / "runs"


class _LazyPath:
    """Module-level Path proxy: вычисляется при каждом доступе, raise — при
    отсутствии workspace. Сохраняет API ``config.RUNS_DIR / "20260504"`` для
    обратной совместимости с тестами и dashboard.services.history."""

    def __init__(self, resolver):
        self._resolver = resolver

    def _path(self) -> Path:
        return self._resolver()

    def __truediv__(self, other):
        return self._path() / other

    def __fspath__(self) -> str:
        return str(self._path())

    def __str__(self) -> str:
        return str(self._path())

    def __repr__(self) -> str:
        try:
            return repr(self._path())
        except WorkspaceNotResolvedError:
            return "<LazyPath: workspace not resolved>"

    def exists(self) -> bool:
        try:
            return self._path().exists()
        except WorkspaceNotResolvedError:
            return False

    def __getattr__(self, name):
        # Делегируем любые методы Path (mkdir, iterdir, parent, name, …).
        return getattr(self._path(), name)


RUNSPACE_DIR = _LazyPath(get_runspace_dir)
RUNS_DIR     = _LazyPath(get_runs_dir)

# ── Agent watchdogs (seconds) ────────────────────────────────────────────────
def _optional_timeout(env_key: str, config_key: str, default: int = 0) -> int | None:
    """Return a positive timeout or None when disabled.

    The old ``*_TIMEOUT`` knobs were hard wall-clock caps. For autonomous agent
    runs, a wall cap is too blunt: a healthy Claude build can legitimately run
    for hours. We therefore keep hard caps opt-in (0/None = disabled) and pair
    them with idle watchdogs that reset whenever the streaming subprocess emits
    output.
    """
    raw = os.environ.get(env_key, _TIMEOUTS.get(config_key, default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return value if value > 0 else None


# Hard wall-clock caps. Disabled by default; set env/config when CI genuinely
# needs a maximum duration.
CLAUDE_TIMEOUT = _optional_timeout("CLAUDE_TIMEOUT", "claude_seconds", 0)
CODEX_TIMEOUT  = _optional_timeout("CODEX_TIMEOUT",  "codex_seconds", 0)
GEMINI_TIMEOUT = _optional_timeout("GEMINI_TIMEOUT", "gemini_seconds", 0)

# Idle watchdogs. These protect against a wedged child process while allowing
# long-running agents to continue as long as they keep streaming progress.
CLAUDE_IDLE_TIMEOUT = _optional_timeout("CLAUDE_IDLE_TIMEOUT", "claude_idle_seconds", 1800)
CODEX_IDLE_TIMEOUT  = _optional_timeout("CODEX_IDLE_TIMEOUT",  "codex_idle_seconds", 900)
GEMINI_IDLE_TIMEOUT = _optional_timeout("GEMINI_IDLE_TIMEOUT", "gemini_idle_seconds", 900)

_RUNTIME_TIMEOUTS = {
    "claude": CLAUDE_TIMEOUT,
    "codex":  CODEX_TIMEOUT,
    "gemini": GEMINI_TIMEOUT,
}
_RUNTIME_IDLE_TIMEOUTS = {
    "claude": CLAUDE_IDLE_TIMEOUT,
    "codex":  CODEX_IDLE_TIMEOUT,
    "gemini": GEMINI_IDLE_TIMEOUT,
}


def agent_timeout(runtime: str) -> int | None:
    """Hard wall-clock cap for a runtime; None means disabled."""
    return _RUNTIME_TIMEOUTS.get(runtime.lower())


def agent_idle_timeout(runtime: str) -> int | None:
    """Idle watchdog for a runtime; None means disabled."""
    return _RUNTIME_IDLE_TIMEOUTS.get(runtime.lower())

# ── Codex model ──────────────────────────────────────────────────────────────
# Codex CLI review model. Passed as config key: -c model="<value>".
CODEX_MODEL = os.environ.get("CODEX_MODEL", phase_model("review_changes", "gpt-5.5"))


# ── AppConfig: lazy, immutable singleton (C1 fix) ────────────────────────────
# Wave 1 introduces AppConfig as the canonical config accessor. Module-level
# constants above are kept for backward compatibility; new code should call
# ``AppConfig.load()`` so config IO is deferred until first use and survives
# env-var changes between tests via ``_reset_config()``.

# Per-phase env overrides: runtime AND model can each be set independently.
# Setting only MODEL_PLAN keeps the JSON-defined runtime for that phase.
# Phase IDs and env-var stems follow ADR 0022 workflow-semantic taxonomy.
_PHASE_ENV_MAP: dict[str, dict[str, str]] = {
    "plan":              {"runtime": "RUNTIME_PLAN",              "model": "MODEL_PLAN"},
    "validate_plan":     {"runtime": "RUNTIME_VALIDATE_PLAN",     "model": "MODEL_VALIDATE_PLAN"},
    "implement":         {"runtime": "RUNTIME_IMPLEMENT",         "model": "MODEL_IMPLEMENT"},
    "review_changes":    {"runtime": "RUNTIME_REVIEW_CHANGES",    "model": "MODEL_REVIEW_CHANGES"},
    "repair_changes":    {"runtime": "RUNTIME_REPAIR_CHANGES",    "model": "MODEL_REPAIR_CHANGES"},
    "repair_escalation": {"runtime": "RUNTIME_REPAIR_ESCALATION", "model": "MODEL_REPAIR_ESCALATION"},
    "final_acceptance":  {"runtime": "RUNTIME_FINAL_ACCEPTANCE",  "model": "MODEL_FINAL_ACCEPTANCE"},
}


def _load_app_raw() -> dict:
    """Merge JSON layers and apply per-phase ``RUNTIME_<PHASE>`` /
    ``MODEL_<PHASE>`` env overrides on top."""
    cfg = _merge_json_layers()
    cfg.setdefault("phases", {})
    for phase, env_keys in _PHASE_ENV_MAP.items():
        spec = cfg["phases"].setdefault(phase, {})
        if val := os.environ.get(env_keys["runtime"]):
            spec["runtime"] = val
        if val := os.environ.get(env_keys["model"]):
            spec["model"] = val
    return cfg


_ARTIFACTS_DEFAULTS: dict[str, Any] = {
    "mirror_to_project": False,
    "mirror_patterns": ["plan.md", "todo.md", "review.md", "cross_plan.json", "cross_plan.md", "diff.patch"],
    "mirror_dir": ".orcho/artifacts",
}


@dataclass(frozen=True)
class AppConfig:
    """Immutable runtime configuration. Loaded lazily, cached forever."""

    phases:     dict[str, dict[str, str]]
    timeouts:   dict[str, int]
    session:    dict[str, Any]
    codemap:    dict[str, Any]
    hypothesis: dict[str, Any]
    language:   dict[str, str]
    artifacts:  dict[str, Any]
    pipeline:   dict[str, Any]           # change_handoff
    commit:     dict[str, Any] = field(default_factory=dict)  # commit delivery (ADR 0032/0043)
    worktree:   dict[str, Any] = field(default_factory=dict)  # per-run isolation (ADR 0033)
    pre_run_dirty: dict[str, Any] = field(default_factory=dict)  # pre-run dirty intake (ADR 0044)
    sandbox:    dict[str, Any] = field(default_factory=dict)  # process-level isolation (ADR 0034)
    cli:        dict[str, Any] = field(default_factory=dict)  # CLI defaults (e.g. output_mode)
    accounting: dict[str, Any] = field(default_factory=dict)  # opt-in dollar accounting

    @classmethod
    @cache
    def load(cls) -> "AppConfig":
        raw = _load_app_raw()
        lang = dict(raw.get("language", {}))
        # Env var overrides for language (highest priority)
        if v := os.environ.get("PLAN_LANGUAGE"):
            lang["plan_language"] = v
        if v := os.environ.get("TASK_LANGUAGE"):
            lang["task_language"] = v
        if v := os.environ.get("CONTENT_LANGUAGE"):
            lang["content_language"] = v

        # Artifacts: defaults + JSON overlay + env override.
        artifacts = dict(_ARTIFACTS_DEFAULTS)
        artifacts.update(raw.get("artifacts", {}))
        # ARTIFACTS_MIRROR=1/true включает зеркалирование без правки JSON.
        if v := os.environ.get("ARTIFACTS_MIRROR"):
            artifacts["mirror_to_project"] = v.strip().lower() in ("1", "true", "yes", "on")

        # Pipeline section: loop budgets (plan/repair) live in the active
        # profile's ``LoopStep.max_rounds``; per-phase pause semantics
        # live in profile-declared ``handoff`` policies.
        pipeline = {
            "change_handoff": "uncommitted",
            "implementation_execution": "whole_plan",
            "session_split_override": {},
        }
        pipeline.update(raw.get("pipeline", {}))
        if v := os.environ.get("ORCHO_CHANGE_HANDOFF"):
            pipeline["change_handoff"] = v.strip()
        if v := os.environ.get("ORCHO_IMPLEMENTATION_EXECUTION"):
            pipeline["implementation_execution"] = v.strip()
        pipeline["session_split_override"] = _parse_session_split_override(
            pipeline.get("session_split_override")
        )
        if v := os.environ.get("ORCHO_SESSION_SPLIT_OVERRIDE"):
            pipeline["session_split_override"].update(
                _parse_session_split_override(v)
            )

        worktree_defaults: dict[str, Any] = {
            "enabled": True,
            "isolation": "per_run",
            "retention_days": 7,
            "allow_destructive_inside": True,
        }
        worktree_defaults.update(raw.get("worktree", {}))

        pre_run_dirty_defaults: dict[str, Any] = {
            "enabled": True,
            "interactive_default": "include",
            "non_interactive_default": "halt",
            "include_untracked": "prompt",
        }
        pre_run_dirty_defaults.update(raw.get("pre_run_dirty", {}))

        commit_defaults: dict[str, Any] = {
            "enabled": True,
            "default_strategy": "release_summary",
            # ADR 0119 — delivery never auto-commits onto the repository's
            # default branch. ``worktree_branch`` (default) publishes an
            # isolated run's own branch as ``orcho/deliver/<run_id>-<slug>``;
            # an in-place run whose HEAD is the default branch gets a fresh
            # delivery branch instead of a commit on the default. ``bypass`` is
            # the explicit opt-out (prior "commit onto current HEAD" behavior).
            "branch_policy": "worktree_branch",
            # ADR 0121 — after an approved worktree_branch delivery, a
            # registered git-provider plugin may push the published branch and
            # open a pull request over the already-signed commit. ``auto``
            # (default) publishes when a provider is registered and enabled;
            # ``off`` keeps the ADR 0119 behavior (local branch only, no
            # provider ever resolved or invoked). ``publish_provider`` names one
            # provider when several are registered; ``None`` auto-selects the
            # sole registration.
            "publish": "auto",
            "publish_provider": None,
            "interactive_default": "approve",
            "auto_in_ci": "approve",
            # ADR 0100 — provider-neutral parking switch. ``auto`` keeps the
            # historical CLI/CI behavior byte-identical; ``defer`` parks a
            # non-interactive run's delivery decision as a recoverable
            # pending / correction gate for a later operator call.
            "decision_mode": "auto",
            "add_untracked": True,
            "include_pre_existing_dirty": False,
            "git_user_identity": None,
        }
        commit_defaults.update(raw.get("commit", {}))

        # ADR 0034: default to ``mode=env`` so an upgraded install
        # gets L1 protection without an explicit config change.
        # Operators opt out via ``mode=off`` in config.local.json.
        # The shape mirrors ``config.defaults.json`` — no
        # ``network`` / ``proxy`` keys, because orcho does not gate
        # network egress.
        sandbox_defaults: dict[str, Any] = {
            "mode": "env",
        }
        sandbox_defaults.update(raw.get("sandbox", {}))

        cli_defaults: dict[str, Any] = {
            "output_mode": "summary",
        }
        cli_defaults.update(raw.get("cli", {}))
        if v := os.environ.get("ORCHO_OUTPUT_MODE"):
            cli_defaults["output_mode"] = v.strip().lower()

        accounting_defaults = _resolve_accounting(raw)

        return cls(
            phases     = {p: dict(spec) for p, spec in raw.get("phases", {}).items()},
            timeouts   = dict(raw.get("timeouts", {})),
            session    = dict(raw.get("session",  {"mode": "auto"})),
            codemap    = dict(raw.get("codemap",  {"enabled": False})),
            hypothesis = dict(raw.get("hypothesis", {"enabled": False})),
            language   = lang,
            artifacts  = artifacts,
            pipeline   = pipeline,
            commit     = commit_defaults,
            worktree   = worktree_defaults,
            pre_run_dirty = pre_run_dirty_defaults,
            sandbox    = sandbox_defaults,
            cli        = cli_defaults,
            accounting = accounting_defaults,
        )

    # ── phase views (canonical) ──────────────────────────────────────────────
    @property
    def phase_model_map(self) -> dict[str, str]:
        """Per-phase model map. Read this when you need the model string
        for a single phase; for the full spec use ``self.phases[phase]``."""
        return {phase: spec.get("model", "") for phase, spec in self.phases.items()}

    @property
    def phase_runtime_map(self) -> dict[str, str]:
        """Per-phase agent-runtime map (e.g. ``{"plan": "claude", "review_changes": "codex"}``)."""
        return {phase: spec.get("runtime", "claude") for phase, spec in self.phases.items()}

    @property
    def phase_effort_map(self) -> dict[str, str | None]:
        """Per-phase reasoning-effort map. Empty/missing → ``None`` (let the
        underlying CLI keep its own default — usually picked from
        ``~/.codex/config.toml`` or a Claude profile). Set explicitly per phase
        in ``_config/config.defaults.json`` (``phases.<name>.effort``) to
        override per-call so a chatty global doesn't burn tokens on trivial
        review/QA passes.
        """
        out: dict[str, str | None] = {}
        for phase, spec in self.phases.items():
            v = spec.get("effort")
            out[phase] = str(v) if v else None
        return out

    @property
    def claude_timeout(self) -> int:
        """Hard wall-clock cap in seconds; 0 means disabled."""
        return int(self.timeouts.get("claude_seconds", 0) or 0)

    @property
    def codex_timeout(self) -> int:
        """Hard wall-clock cap in seconds; 0 means disabled."""
        return int(self.timeouts.get("codex_seconds", 0) or 0)

    @property
    def gemini_timeout(self) -> int:
        """Hard wall-clock cap in seconds; 0 means disabled."""
        return int(self.timeouts.get("gemini_seconds", 0) or 0)

    @property
    def claude_idle_timeout(self) -> int:
        return int(self.timeouts.get("claude_idle_seconds", 1800) or 0)

    @property
    def codex_idle_timeout(self) -> int:
        return int(self.timeouts.get("codex_idle_seconds", 900) or 0)

    @property
    def gemini_idle_timeout(self) -> int:
        return int(self.timeouts.get("gemini_idle_seconds", 900) or 0)

    @property
    def plan_language(self) -> str:
        """Language for plan artifacts (MD documents). Default: English.

        Workspace / project configurations may override via the JSON
        ``language.plan_language`` field or the ``PLAN_LANGUAGE`` env
        var when their team works in another natural language; the
        engine default stays English so cross-machine prompt
        rendering is reproducible.
        """
        return self.language.get("plan_language", "English")

    @property
    def task_language(self) -> str:
        """Language for task descriptions passed to agents. Default: English.

        Workspace / project configurations may override via the JSON
        ``language.task_language`` field or the ``TASK_LANGUAGE`` env
        var; the engine default stays English so cross-machine prompt
        rendering is reproducible.
        """
        return self.language.get("task_language", "English")

    @property
    def content_language(self) -> str:
        """Language for outward delivery artifacts. Default: English.

        Governs the natural language of the artifacts a run publishes
        outward — commit messages and PR title/body — independent of
        the operator-facing task language. Workspace / project
        configurations may override via the JSON
        ``language.content_language`` field or the ``CONTENT_LANGUAGE``
        env var. The engine default stays English as a fail-safe so
        public repositories receive English delivery artifacts
        regardless of the operator's working language.
        """
        return self.language.get("content_language", "English")

    @property
    def accounting_enabled(self) -> bool:
        """Whether dollar-denominated accounting is collected and rendered."""
        return _coerce_bool(self.accounting.get("enabled"), default=False)


def accounting_enabled() -> bool:
    """Global opt-in for dollar-denominated accounting surfaces."""
    app = AppConfig.load()
    if hasattr(app, "accounting_enabled"):
        return bool(app.accounting_enabled)
    if "ORCHO_ACCOUNTING" in os.environ:
        return _coerce_bool(os.environ.get("ORCHO_ACCOUNTING"), default=False)
    return _coerce_bool(
        getattr(app, "accounting", {}).get("enabled")
        if isinstance(getattr(app, "accounting", None), dict)
        else None,
        default=False,
    )


def accounting_enabled_for_workspace(workspace: Path | str | None = None) -> bool:
    """Resolve accounting with an explicit workspace-local config layer."""
    if workspace is None:
        return accounting_enabled()
    return _coerce_bool(
        _resolve_accounting(_merge_json_layers(workspace=workspace)).get("enabled"),
        default=False,
    )


def _reset_config() -> None:
    """Test helper — clears the AppConfig cache so the next load() re-reads
    JSON + env vars. Production code should never call this."""
    AppConfig.load.cache_clear()
