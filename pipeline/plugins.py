"""
plugin_loader.py — Plugin discovery and loading.

A plugin lives in: {project_dir}/.orcho/multiagent/plugin.py
It must define a PLUGIN dict with any subset of PluginConfig fields.

The core works without any plugin (graceful degradation).
"""

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pipeline.skills import SkillPackage, SkillTrustPolicy
from pipeline.skills.discover import discover_skills as _discover_skills_canonical

PLUGIN_RELATIVE_PATH = ".orcho/multiagent/plugin.py"


# Artifact profile names recognised by the (forthcoming) artifact pipeline.
# Stage 3 adds the actual generators; for now we only carry the config so
# plugin authors can opt into "minimal" / "adr" / "docs" / "full" early and
# the field is type-stable across releases.
ARTIFACT_PROFILES = ("none", "minimal", "adr", "docs", "full")


@dataclass
class ArtifactsConfig:
    """How (and whether) to deliver project documentation artifacts.

    ``profile`` selects a built-in set of artifacts to generate after
    final_acceptance. ``"none"`` (default) keeps everything in the workspace and
    writes nothing into the project's git tree — current behaviour.
    ``output_root`` is the path inside the project where artifacts are
    written when the profile is anything other than ``"none"``.
    ``auto_commit`` controls whether orcho creates an additional commit
    with the artifacts; when False, the files are left for manual review.
    """
    profile: str = "none"
    output_root: str = ".orcho/runs"
    auto_commit: bool = False


@dataclass
class TestSuiteConfig:
    """A single named test suite within a project.

    Used when a project has multiple independent test runners
    (e.g. Unity: EditMode tests + PlayMode tests; PHP: Behat + PHPUnit).
    Each suite is run sequentially; any failure marks the overall step as failed.

    .. deprecated:: Phase 5e
        Kept as an internal coercion target for
        ``PluginConfig.quality_gates["tests"]["suites"]``. Customer
        plugins should not construct this type directly.
    """
    __test__ = False

    # Human-readable suite name (shown in pipeline output)
    name: str = ""

    # Shell command for this suite. None = suite is skipped (documented but not run).
    run_command: str | None = None

    # Where new tests for this suite should be created
    test_dir: str = ""

    # Substring that signals failure even on exit-code 0
    fail_keyword: str = "failed"

    # Hard timeout for this suite (seconds)
    timeout: int = 120


@dataclass
class TestingConfig:
    """How to run the project's test suite(s) as a shell step.

    Single suite: set ``run_command`` directly.
    Multi-suite: set ``suites`` — a list of ``TestSuiteConfig``.
    When both are set, ``suites`` takes precedence.
    When ``run_command`` is None and ``suites`` is empty, the orchestrator
    skips the step (TestResult.skipped=True).

    .. deprecated:: Phase 5e
        Kept as an internal coercion target for the built-in ``tests``
        quality gate. Customer config lives in
        ``PluginConfig.quality_gates["tests"]``; the customer-facing
        ``PluginConfig.testing`` field has been deleted.
    """
    # Tell pytest not to collect this as a test class.
    __test__ = False

    # Shell command for the default (single) suite. None = no automatic testing.
    run_command: str | None = None

    # Hint for the developer agent on how to write tests for this project
    # (e.g. "behat_gherkin", "pytest", "nunit_csharp", "xunit_csharp").
    write_style: str = ""

    # Default location for new tests (used when suites is empty)
    test_dir: str = "tests/"

    # Substring searched in stdout/stderr to detect failures even when the
    # process returns 0 (some runners exit 0 with "X failed" in output).
    fail_keyword: str = "failed"

    # Phases after which to run tests. Reserved for future use; the
    # orchestrator currently always runs after implement and repair_changes.
    run_after: list[str] = field(default_factory=lambda: ["implement", "repair_changes"])

    # Hard timeout for the test command (seconds).
    timeout: int = 120

    # Multi-suite mode: when set, each suite is run independently and results
    # are aggregated. Overrides run_command / fail_keyword / timeout.
    suites: list[TestSuiteConfig] = field(default_factory=list)


@dataclass
class PluginConfig:
    """
    Project-specific context injected into the pipeline.
    All fields are optional — core uses generic defaults if missing.
    """
    # Human-readable project name (used in prompts)
    name: str = "Project"

    # Primary language(s) hint for Claude and Codex
    language: str = ""

    # Brief architecture description (injected into prompts)
    architecture: str = ""

    # Where plan MD artifacts should be created
    ma_artifacts_dir: str = ".orcho/artifacts"

    # Extra instructions appended to the PLAN phase prompt
    plan_prompt_extra: str = ""

    # Extra instructions appended to the implement phase prompt
    build_prompt_extra: str = ""

    # Extra instructions appended to the review_changes focus prompt
    review_focus_extra: str = ""

    # File/directory patterns to hint Claude where to look
    # e.g. ["Scripts/", "Assets/", "_docs/"]
    file_hints: list[str] = field(default_factory=list)

    # Project-wide allowed companion modifications. A flat list of
    # ``"glob — reason"`` entries describing satellite files whose
    # modification is not a scope violation in ANY task of the project —
    # lockfiles, golden snapshots, regenerable artifacts (e.g.
    # ``"package-lock.json — derived from package.json"``). Review gates
    # surface this list so changes to these files are not flagged as
    # out-of-scope; the *content* of such changes is still reviewed as
    # usual. This is informational for review agents — core performs no
    # glob matching or diff enforcement.
    allowed_modifications: list[str] = field(default_factory=list)

    # Optional: path to a custom plan prompt template file
    # If set, its content replaces the default plan prompt template
    custom_plan_prompt_file: str = ""

    # Optional: path to a custom review focus template file
    custom_review_focus_file: str = ""

    # Phase 5e step 3.5: ``testing: TestingConfig`` field DELETED.
    # Customer plugins now declare test config via
    # ``quality_gates["tests"]: dict``. ``TestingConfig`` /
    # ``TestSuiteConfig`` dataclasses below remain as internal
    # coercion targets (``_resolve_tests_config`` parses the dict
    # into them for the existing ``run_tests`` body), but are no
    # longer customer-facing API.

    # Skill registry — portable :class:`SkillPackage` instances keyed by
    # name, populated by :func:`pipeline.skills.discover.discover_skills`
    # at plugin-load time. Skills supply only instructional content;
    # runtime/model selection is the job of the runtime resolver, not the
    # skill (R9 portability invariant).
    skill_registry: dict[str, SkillPackage] = field(default_factory=dict)

    # Project artifact delivery configuration. Default profile "none" keeps
    # behaviour identical to today (workspace-only artifacts).
    artifacts: ArtifactsConfig = field(default_factory=ArtifactsConfig)

    # Optional worktree preparation steps. Used for local, gitignored
    # prerequisites that a fresh git worktree cannot materialise by itself
    # (for example copied dependency folders or package-manager installs).
    worktree_bootstrap: list[dict[str, Any]] | dict[str, Any] = field(
        default_factory=list,
    )

    # Pipeline profile selector. Phase 5d: dispatch goes through v2
    # profiles in ``_config/pipeline_profiles_v2.json`` —
    # ``ORCHO_PIPELINE`` env override picks among shipped names
    # (``lite`` / ``advanced`` / ``enterprise`` / ``plan`` / ``review`` /
    # ``task``) or any plugin-shipped custom profile (Phase 7
    # ``orcho.profiles`` entry_points). When unset, the
    # ``--mode`` CLI flag → ``_resolve_v2_profile_name_for_mode``
    # mapping picks the default. This field is currently dead-code
    # but preserved for Phase 7 plugin authors who will re-wire it
    # as a per-project default override.
    pipeline: str | list[str] = ""

    # ── Phase 1 redesign extensions (additive — wired in later phases) ────

    # Phase 4/5e — declarative per-gate config. Phase 5e step 3.5
    # deleted customer-facing ``PluginConfig.testing``; this dict is now
    # the active config source for the built-in ``tests`` gate via
    # ``project_orchestrator._resolve_tests_config()``. Empty or missing
    # ``quality_gates["tests"]`` means no tests run.
    #
    # Schema: ``{gate_name: config_dict}`` where ``gate_name`` matches
    # a registered gate handler (built-in: ``tests``; customer-shipped
    # via ``orcho.quality_gates`` entry_points in Phase 7).
    quality_gates: dict[str, dict[str, Any]] = field(default_factory=dict)

    # R9 skill trust policy (autonomous-run security). Project / compat
    # skills (.agents/skills, .claude/skills, .forge/skills) are off by default — opt-in
    # via CLI / config / ENV (Phase 7).
    skill_trust: SkillTrustPolicy = field(default_factory=SkillTrustPolicy)

    # ── Verification contract (read-only Stage 1 projection) ──────────────
    #
    # These four fields carry the declarative verification contract into the
    # pipeline as raw, normalised structures — by the same "store the dict,
    # don't coerce or validate here" rule as ``quality_gates`` and
    # ``worktree_bootstrap``. Stage 1 is strictly read-only: the contract is
    # projected into the run header and into limited per-phase prompt blocks.
    # Nothing here executes ``verification.commands``, writes a receipt,
    # blocks phase transitions, or triggers auto-repair. ``load_plugin`` keeps
    # them as-is (unknown sub-keys inside the dicts are preserved verbatim);
    # typed validation lives in ``pipeline.verification_contract`` and runs
    # later, only when a contract is actually declared.

    # Declared dependency repositories, keyed by logical name. Each value is
    # a raw dict (e.g. ``{"path": "...", "ref": "..."}``) used to resolve
    # ``{dependency:name}`` placeholders during projection.
    dependency_repos: dict[str, dict[str, Any]] = field(default_factory=dict)

    # Named verification environments, keyed by env name. Each value is a raw
    # dict describing the environment; only the names are surfaced in Stage 1.
    verification_envs: dict[str, dict[str, Any]] = field(default_factory=dict)

    # The verification contract body. Recognised keys: ``default_env``,
    # ``required``, ``commands``, ``schedule``. Stored raw; never executed.
    verification: dict[str, Any] = field(default_factory=dict)

    # Declared work mode. Expected values are ``fast`` / ``pro`` / ``governed``,
    # but validation of the value is not performed here.
    work_mode: str = ""

    # Set by ``load_plugin`` when a project-level plugin.py was actually
    # loaded. This is presentation/diagnostic metadata; plugin authors do not
    # need to declare it in PLUGIN.
    loaded_plugin_path: str = ""


def _discover_skills_for_plugin(
    project_dir: str, trust_policy: SkillTrustPolicy,
) -> dict[str, SkillPackage]:
    """Run the canonical multi-source discovery for plugin loading.

    Workspace dir comes from ``$ORCHO_WORKSPACE`` when set, else falls
    back to the project dir (single-project mode — workspace layer is
    just another scan of the project root, which is fine because the
    project layer already covers that path).
    """
    from core.infra.platform import workspace_dir as _resolve_workspace
    workspace = _resolve_workspace() or Path(project_dir)
    return _discover_skills_canonical(
        project_dir=project_dir,
        workspace_dir=workspace,
        trust_policy=trust_policy,
    )


def load_plugin(project_dir: str) -> PluginConfig:
    """
    Attempt to load plugin from {project_dir}/.orcho/multiagent/plugin.py.

    Returns a default :class:`PluginConfig` if no plugin found or on
    error. Never raises — core should always be able to run without
    a plugin.

    The loader is a **normalising validator**, not a strict one. Any
    key in ``PLUGIN`` that is NOT a field of :class:`PluginConfig`
    is silently dropped with a yellow warning line so a plugin
    written for an older core version keeps loading after a field
    rename or deletion. Plugin authors that need strict validation
    must check the dict themselves before exporting ``PLUGIN``.

    The ``artifacts`` key, when present as a ``dict``, is coerced
    into :class:`ArtifactsConfig` with ``profile`` validated against
    :data:`ARTIFACT_PROFILES` (unknown profiles fall back to
    ``"none"`` with a warning).
    """
    plugin_path = Path(project_dir) / PLUGIN_RELATIVE_PATH

    if not plugin_path.exists():
        # No plugin.py, but skills may still exist on their own — discover
        # them so a project can opt into team-lead routing without a full
        # plugin definition.
        config = PluginConfig()
        config.skill_registry = _discover_skills_for_plugin(
            project_dir, config.skill_trust,
        )
        return config

    try:
        spec = importlib.util.spec_from_file_location("multiagent_plugin", plugin_path)
        module = importlib.util.module_from_spec(spec)
        old_dont_write_bytecode = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            spec.loader.exec_module(module)
        finally:
            sys.dont_write_bytecode = old_dont_write_bytecode

        plugin_dict: dict = getattr(module, "PLUGIN", {})
        if not isinstance(plugin_dict, dict):
            raise TypeError(f"PLUGIN must be a dict, got {type(plugin_dict)}")

        # Filter only known fields — see ``load_plugin`` docstring for
        # the "normalising validator, not strict" contract.
        known = {f for f in PluginConfig.__dataclass_fields__}
        filtered = {k: v for k, v in plugin_dict.items() if k in known}
        unknown = set(plugin_dict) - known
        if unknown:
            from core.observability.logging import warn as _warn
            _warn(
                f"plugin: ignoring unknown PLUGIN keys "
                f"{sorted(unknown)!r} (known fields: "
                f"{sorted(known)!r})",
            )

        # Phase 5e step 3.5: ``PLUGIN["testing"]`` coercion deleted.
        # Customer plugins now declare test config via the dict-based
        # ``PLUGIN["quality_gates"]["tests"] = {...}`` shape; coercion
        # to internal ``TestingConfig`` happens at gate-firing time
        # in ``_resolve_tests_config()``.

        # Coerce artifacts dict if present, with profile validation.
        if "artifacts" in filtered and isinstance(filtered["artifacts"], dict):
            art_known = {f for f in ArtifactsConfig.__dataclass_fields__}
            art_dict = filtered["artifacts"]
            art_filtered = {k: v for k, v in art_dict.items() if k in art_known}
            profile = art_filtered.get("profile", "none")
            if profile not in ARTIFACT_PROFILES:
                from core.observability.logging import warn as _warn
                _warn(
                    f"plugin: unknown artifact profile {profile!r}; "
                    f"falling back to 'none'. Allowed: "
                    f"{ARTIFACT_PROFILES!r}",
                )
                art_filtered["profile"] = "none"
            filtered["artifacts"] = ArtifactsConfig(**art_filtered)

        # Normalise allowed_modifications: a non-list value is dropped
        # entirely (falls back to the empty-list default); a list keeps
        # only its string elements, dropping non-strings. Both cases emit
        # a yellow warning. load_plugin never raises — garbage values are
        # discarded, not surfaced as exceptions.
        if "allowed_modifications" in filtered:
            value = filtered["allowed_modifications"]
            if not isinstance(value, list):
                from core.observability.logging import warn as _warn
                _warn(
                    f"plugin: allowed_modifications must be a list, got "
                    f"{type(value).__name__}; ignoring the field.",
                )
                del filtered["allowed_modifications"]
            else:
                strings = [item for item in value if isinstance(item, str)]
                if len(strings) != len(value):
                    from core.observability.logging import warn as _warn
                    _warn(
                        "plugin: allowed_modifications entries must be "
                        "strings; dropping non-string entries.",
                    )
                filtered["allowed_modifications"] = strings

        config = PluginConfig(**filtered)
        config.loaded_plugin_path = str(plugin_path)

        # Auto-discover skills via the canonical multi-source chain
        # (project / compat / workspace / user / entry_points). Failures
        # inside discover never raise; an empty dict means the runner
        # stays in flat developer-agent mode.
        config.skill_registry = _discover_skills_for_plugin(
            project_dir, config.skill_trust,
        )

        return config

    except Exception as e:
        from core.observability.logging import warn as _warn
        _warn(
            f"plugin: failed to load from {plugin_path}: {e}. "
            "Running with default (no-plugin) configuration.",
        )
        config = PluginConfig()
        config.skill_registry = _discover_skills_for_plugin(
            project_dir, config.skill_trust,
        )
        return config


def describe_plugin(plugin: PluginConfig) -> str:
    """Human-readable summary of loaded plugin config."""
    if (
        not plugin.loaded_plugin_path
        and plugin.name == "Project"
        and not plugin.language
        and not plugin.skill_registry
    ):
        return "  (no plugin — generic mode)"
    parts = [f"  Plugin: {plugin.name}"]
    if plugin.loaded_plugin_path:
        parts.append(f"  Plugin file: {plugin.loaded_plugin_path}")
    if plugin.language:
        parts.append(f"  Language: {plugin.language}")
    if plugin.architecture:
        parts.append(f"  Architecture: {plugin.architecture}")
    if plugin.file_hints:
        parts.append(f"  File hints: {', '.join(plugin.file_hints)}")
    if plugin.artifacts.profile != "none":
        parts.append(f"  Artifacts profile: {plugin.artifacts.profile} → {plugin.artifacts.output_root}")
    if plugin.work_mode or plugin.verification:
        commands = plugin.verification.get("commands") or {}
        parts.append(
            f"  Verification contract: work_mode="
            f"{plugin.work_mode or '(unset)'}, "
            f"{len(commands)} command(s)",
        )
    trust = _describe_skill_trust(plugin.skill_trust)
    if trust:
        parts.append(f"  Skill trust: {trust}")
    if plugin.skill_registry:
        parts.append(_describe_skills(plugin.skill_registry))
    return "\n".join(parts)


def _describe_skill_trust(policy: SkillTrustPolicy) -> str:
    """Describe only trust knobs that differ from the secure defaults."""
    default = SkillTrustPolicy()
    enabled: list[str] = []
    disabled: list[str] = []
    for field_name, label in (
        ("trust_packages", "packages"),
        ("trust_user", "user"),
        ("trust_workspace", "workspace"),
        ("trust_project", "project"),
        ("trust_compat_claude", "claude-compat"),
        ("trust_compat_forge", "forge-compat"),
    ):
        value = bool(getattr(policy, field_name))
        if value == bool(getattr(default, field_name)):
            continue
        if value:
            enabled.append(label)
        else:
            disabled.append(label)

    bits: list[str] = []
    if enabled:
        bits.append("enabled " + ", ".join(enabled))
    if disabled:
        bits.append("disabled " + ", ".join(disabled))
    return "; ".join(bits)


def _describe_skills(registry: dict[str, SkillPackage]) -> str:
    """Compact human-readable summary of a discovered skill registry."""
    if not registry:
        return "  (no skills discovered — flat developer-agent mode)"
    lines = [f"  Skills: {len(registry)} discovered"]
    for name, pkg in sorted(registry.items()):
        desc = pkg.description.strip() or "(no description)"
        lines.append(f"    - {name} [{pkg.source}]: {desc}")
    return "\n".join(lines)
