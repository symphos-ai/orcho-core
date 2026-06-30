"""
pipeline/runtime/profile.py — declarative profile shapes.

``Profile`` is the v2 production shape consumed by ``run_profile``:
ordered steps (each ``PhaseStep`` or ``LoopStep``), kind × variant
typology, and an optional change-handoff strategy.

``LoopStep`` is the declarative retry loop entry — replaces inline
imperative orchestrator loops with a profile-driven recipe
(``[plan, validate_plan]`` rerun until QA approves or N rounds elapse).

``PipelineProfile`` is the legacy in-process helper shape retained
for direct dispatcher tests / inline helper calls. The v1 JSON loader
was removed in Phase 5d-5; this dataclass is no longer the runtime
production shape.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from pipeline.prompts.spec import PromptSpec
from pipeline.runtime.results import PhaseRegistry
from pipeline.runtime.roles import (
    ChangeHandoffMode,
    EffortLevel,
    ExecutionMode,
    FullCycleDepth,
    ImplementationExecution,
    ProfileKind,
    ScopedTarget,
)
from pipeline.runtime.run_shape import OperatingMode, SemanticProfile
from pipeline.runtime.steps import CrossScope, CrossStepPolicy, PhaseStep

# Stage C: the closed set of recipe-kind tags a built-in profile may carry.
# ``full_cycle`` / ``focused`` describe how broad the recipe is; ``internal``
# marks a profile the interactive picker never offers (e.g. ``correction`` /
# ``task``). This is the semantic *recipe* tag, distinct from the legacy
# ``kind`` × ``variant`` typology which stays for plugin/custom compatibility.
_VALID_RECIPE_KINDS: frozenset[str] = frozenset({
    "full_cycle", "focused", "internal",
})

# ADR 0027 / M11: profile-visible prompt-session split values.
# Mirrors the M5 ``PromptSessionSplit`` member set on the wire-shape
# side without importing the enum here — ``pipeline.runtime`` must
# stay framework-agnostic and avoid a runtime→prompts coupling.
# Validation in :class:`ExecutionPolicy.__post_init__` uses this set
# as the authority; the M5 enum is a separate concern that the
# wiring layer (M7/M8/M9) resolves at invocation time.
_VALID_SESSION_SPLITS: frozenset[str] = frozenset({
    "stateless", "per_phase", "per_role", "common",
})

# ADR 0113: profile-visible per-phase session-continuity values. Mirrors the
# member set of :class:`pipeline.runtime.roles.SessionContinuity` without
# importing the enum here — ``pipeline.runtime.profile`` stays a pure data
# shape and avoids coupling the dataclass to the policy enum. Validation in
# :class:`ExecutionPolicy.__post_init__` uses this set as the authority; the
# resolver above the stack (T3) maps the parsed string onto the enum.
_VALID_SESSION_CONTINUITY: frozenset[str] = frozenset({
    "fresh_only", "loop_continue", "same_zone_continue",
})

# ADR 0027: execution modes reserved for a future runtime milestone but
# rejected today (no implemented dispatcher yet).
_RESERVED_FUTURE_MODES: frozenset[str] = frozenset({"fanout_review"})


@dataclass(frozen=True)
class ExecutionSurface:
    """ADR 0027 read-only execution surface (e.g. correctness, test
    gaps, security review lens).

    Surfaces are reserved profile shape — M11 parses and validates
    them but the runtime does not execute fanout in this milestone.
    A later milestone wires the actual fanout review dispatcher.
    """
    id: str
    prompt: PromptSpec
    model: str | None = None
    effort: EffortLevel | None = None

    def __post_init__(self) -> None:
        if not self.id or not self.id.strip():
            raise ValueError("ExecutionSurface.id is empty")
        if not isinstance(self.prompt, PromptSpec):
            raise TypeError(
                f"ExecutionSurface {self.id!r}: prompt must be PromptSpec, "
                f"got {type(self.prompt).__name__}"
            )


@dataclass(frozen=True)
class ExecutionPolicy:
    """ADR 0027 phase-step execution policy.

    ``mode`` matches the existing ``PhaseStep.execution`` string so
    profiles authored as ``"execution": "linear"`` normalize to
    ``ExecutionPolicy(mode="linear")`` without losing the existing
    string accessor at the runtime layer.

    ``session_split`` is the M5/M6 prompt-session split policy on
    the profile side. It lives on execution policy, not on a
    top-level profile field and not under ``overrides`` (ADR 0027:
    "do not hide topology under overrides"). M7/M8/M9 wiring reads
    this through the active ``PhaseStep`` and passes it into
    ``_session_aware_invoke``. ``None`` means "use the per-phase
    default the wiring helpers ship with" (currently ``per_phase``);
    explicit values override.

    ``session_continuity`` is the ADR 0113 per-phase continuity policy.
    It lives here next to ``session_split`` but is an **orthogonal axis**:
    ``session_split`` decides *how* a session is shared *between phases*
    (the physical-session key scope), while ``session_continuity`` decides
    *whether* an invocation resumes *its own* prior session on a repeat
    call / loop round (``fresh_only`` / ``loop_continue`` /
    ``same_zone_continue``). The two are set independently. The resolver
    above the stack (T3) maps this string onto
    :class:`~pipeline.runtime.roles.SessionContinuity` and feeds it to the
    session-disposition projection. ``None`` means "no per-step preference
    declared"; the resolver supplies the role default.

    ``read_only`` / ``join`` / ``surfaces`` are reserved profile
    shape for the future fanout_review milestone. M11 parses them
    but fails fast on any non-default value: a profile must not
    look constrained (``read_only: true``, ``join: "..."``,
    non-empty surfaces) while the runtime silently ignores those
    constraints. ``mode="fanout_review"`` is rejected for the same
    reason. Until the fanout milestone wires execution, all four
    fields must stay at their defaults.
    """
    mode: str = ExecutionMode.LINEAR.value
    session_split: str | None = None
    session_continuity: str | None = None
    read_only: bool | None = None
    join: str | None = None
    surfaces: tuple[ExecutionSurface, ...] = ()

    def __post_init__(self) -> None:
        if not self.mode or not self.mode.strip():
            raise ValueError("ExecutionPolicy.mode is empty")
        if self.mode in _RESERVED_FUTURE_MODES:
            raise ValueError(
                f"ExecutionPolicy.mode={self.mode!r} is reserved by "
                "ADR 0027 but runtime execution is not implemented in "
                "this milestone; remove the field or wait for the "
                "later milestone."
            )
        if (
            self.session_split is not None
            and self.session_split not in _VALID_SESSION_SPLITS
        ):
            valid = sorted(_VALID_SESSION_SPLITS)
            raise ValueError(
                f"ExecutionPolicy.session_split={self.session_split!r} "
                f"is not one of {valid}"
            )
        if (
            self.session_continuity is not None
            and self.session_continuity not in _VALID_SESSION_CONTINUITY
        ):
            valid = sorted(_VALID_SESSION_CONTINUITY)
            raise ValueError(
                f"ExecutionPolicy.session_continuity="
                f"{self.session_continuity!r} is not one of {valid}"
            )
        if self.surfaces:
            raise ValueError(
                "ExecutionPolicy.surfaces are reserved by ADR 0027 for "
                "the fanout_review milestone; non-empty surfaces are "
                "not yet supported"
            )
        # ADR 0027 hardening: ``read_only`` and ``join`` are reserved
        # but otherwise unenforced by the runtime in this milestone.
        # Accepting them silently lets a profile look constrained
        # while no constraint is actually applied. Reject any non-null
        # value until the fanout milestone wires execution.
        if self.read_only is not None:
            raise ValueError(
                "ExecutionPolicy.read_only is reserved by ADR 0027 for "
                "the fanout_review milestone; the runtime does not "
                "enforce it yet, so any non-null value is rejected to "
                "avoid a profile that looks constrained while no "
                "constraint applies"
            )
        if self.join is not None:
            raise ValueError(
                "ExecutionPolicy.join is reserved by ADR 0027 for the "
                "fanout_review milestone; the runtime does not "
                "consume it yet, so any non-null value is rejected to "
                "avoid a profile that looks constrained while no "
                "constraint applies"
            )


class CrossGateRunPolicy(StrEnum):
    """When a runner-owned cross gate runs.

    ``always``         — always run the gate when enabled.
    ``auto``           — runner decides based on context (treated as
                          ``always`` until heuristics exist).
    ``manual_confirm`` — operator decision resolved by the current
                          transport (CLI prompt / pending state).
    ``never``          — skip the gate by policy.
    """
    ALWAYS = "always"
    AUTO = "auto"
    MANUAL_CONFIRM = "manual_confirm"
    NEVER = "never"


class CrossGateSkipPolicy(StrEnum):
    """How a skipped runner-owned cross gate affects system release.

    ``block``          — skipped gate blocks system release.
    ``allow_with_gap`` — continue, but record a verification gap.
    ``allow``          — continue without blocker/gap.
    """
    BLOCK = "block"
    ALLOW_WITH_GAP = "allow_with_gap"
    ALLOW = "allow"


class ContractCheckMode(StrEnum):
    """Execution mode for the cross-contract check gate.

    ``artifact_bundle`` — review the compact cross-contract bundle.
    """
    ARTIFACT_BUNDLE = "artifact_bundle"


@dataclass(frozen=True)
class CrossGatePolicy:
    """Profile-level policy for a runner-owned cross gate.

    Each known gate (``contract_check`` / ``cross_final_acceptance``)
    accepts a policy block on the profile. Missing blocks fall back to
    documented defaults at consumption time (see
    ``pipeline.cross_project.profile_projection.get_cross_gate_policy``).
    """
    enabled: bool = True
    run: CrossGateRunPolicy = CrossGateRunPolicy.AUTO
    on_skip: CrossGateSkipPolicy = CrossGateSkipPolicy.BLOCK
    mode: str | None = None


@dataclass(frozen=True)
class LoopStep:
    """A retry-loop entry inside a profile.

    Declarative form for things like ``[plan, validate_plan]`` rerun until QA approves
    or N rounds elapsed. Without this, a profile would have to leave such
    loops to imperative orchestrator code.

    R2 clean break (see docs/adr/0001): ``inner_phases: tuple[str, ...]`` is
    replaced by ``steps: tuple[PhaseStep, ...]`` so each inner step can carry
    full per-step richness (execution mode, skill, effort, quality_gates,
    human_review). Phase 5d makes this shape the active dispatcher input;
    dedicated hooks for non-linear ``execution`` / per-step gates /
    HumanReview are still separate lifecycle work.

    Fields
    ------
    steps:
        The PhaseStep sequence to run **per round**. Each ``step.phase`` name
        must be registered in the same ``PhaseRegistry`` as plain phases.
        Nested ``LoopStep`` is NOT supported in Phase 1-8 (steps must be
        PhaseStep-only).
    until:
        Predicate evaluated after the inner phases complete. Format
        ``"<phase>.<field>"`` (truthy → exit loop) or
        ``"not <phase>.<field>"`` (falsy → exit loop). The phase log entry is
        read via ``state.phase_log[phase][field]``. Use the ``"approved"``
        convention for QA gates and ``"clean"`` for empty-critique signals.
    max_rounds:
        Hard cap. The loop exits after this many rounds even if ``until``
        was never satisfied — surface in the log as "max rounds reached".
    round_extras_key:
        ``state.extras[<key>]`` is set to the current 1-based round number
        before each iteration. Defaults to ``"loop_round"``; handlers can
        consult it to vary behaviour per round (e.g. ``_phase_plan`` reads
        ``plan_round`` to switch to replan prompt on round ≥ 2).
    oscillation_halt_after:
        Hash-based "agent stuck" early halt. When set (≥2), the runtime
        compares phase outputs across rounds; if the same handler emits the
        same hash N rounds in a row, halt the loop. ``None`` disables.
        Currently stored only; runtime hookup is pending.
    """
    steps: tuple[PhaseStep, ...]
    until: str
    max_rounds: int = 1
    round_extras_key: str = "loop_round"
    oscillation_halt_after: int | None = 2

    def __post_init__(self) -> None:
        if not self.steps:
            raise ValueError("LoopStep.steps is empty")
        for i, s in enumerate(self.steps):
            if not isinstance(s, PhaseStep):
                raise TypeError(
                    f"LoopStep.steps[{i}] must be PhaseStep (nested LoopStep "
                    f"not supported in Phase 1-8), got {type(s).__name__}"
                )
        if self.max_rounds < 1:
            raise ValueError(f"LoopStep.max_rounds must be ≥1, got {self.max_rounds}")
        if not self.until or not self.until.strip():
            raise ValueError("LoopStep.until is empty")
        if self.oscillation_halt_after is not None and \
                self.oscillation_halt_after < 2:
            raise ValueError(
                "LoopStep.oscillation_halt_after must be ≥2 or None"
            )

    @property
    def inner_phases(self) -> tuple[str, ...]:
        """Read-only view of phase names for legacy tests / callers that
        still inspect LoopStep through the old string-list lens.
        """
        return tuple(s.phase for s in self.steps)


@dataclass(frozen=True)
class Profile:
    """Goal-oriented recipe. Two-axis kind × variant typology enforces
    semantic split: FULL_CYCLE varies by depth, SCOPED varies by target.

    This is the active Phase 5d runtime shape. The legacy
    ``PipelineProfile`` below remains only for direct in-process helper
    dispatch and tests.
    """
    name: str
    kind: ProfileKind = ProfileKind.CUSTOM
    variant: str | None = None
    description: str = ""
    # First-class internal/system flag (ADR 0085). ``True`` marks a profile
    # the interactive fresh-run picker must never offer (e.g. the
    # ``correction`` follow-up profile, which fail-fasts without correction
    # context). It stays visible in the `orcho profiles` catalog and is
    # selectable explicitly via ``--profile``. Default ``False`` so existing
    # profiles parse unchanged.
    internal: bool = False
    steps: tuple = ()  # tuple[PhaseStep | LoopStep, ...]
    change_handoff: ChangeHandoffMode | None = None
    # Profile-level policy for how the built-in implement phase consumes a
    # parsed plan. None means "use global pipeline config".
    implementation_execution: ImplementationExecution | None = None
    # Profile-level policy for runner-owned cross gates
    # (``contract_check`` / ``cross_final_acceptance``). Empty mapping
    # means "use documented defaults at consumption time" — see
    # ``pipeline.cross_project.profile_projection.get_cross_gate_policy``.
    cross_gates: Mapping[str, CrossGatePolicy] = field(default_factory=dict)
    # Optional worktree isolation mode override (ADR 0033). ``per_run``
    # and ``per_phase`` are schema-valid; v1 rejects ``per_phase`` at
    # runtime. None means "use the global config setting". DAG-2 profiles
    # may declare ``per_phase`` to signal intent ahead of v1 implementation.
    worktree_isolation: str | None = None
    # Optional sandbox isolation override (ADR 0034). Raw mapping stored
    # verbatim — the resolver in ``pipeline.sandbox.resolver`` merges this
    # with the global ``sandbox`` config block and validates structure.
    # Storing as a mapping (not a typed dataclass) keeps this module's
    # imports unchanged: ``pipeline.runtime.profile`` stays
    # framework-agnostic and ``pipeline.sandbox`` depends on it, not the
    # other way around.
    sandbox: Mapping[str, object] | None = None
    # Stage C semantic identity fields (ADR semantic-profiles alignment).
    # These are the explicit source of a built-in profile's semantic
    # identity — ``variant`` is NOT. They are optional so plugin/custom
    # profiles that don't declare them parse unchanged.
    #   semantic_profile — the goal-shaped work kind this recipe targets.
    #   default_mode      — the default strictness posture; ``None`` means
    #                        "no declared default" (the runtime projects one
    #                        from ``semantic_profile`` via the T2 helper).
    #   recipe_kind       — recipe breadth tag in {full_cycle, focused,
    #                        internal}; ``None`` means "untagged".
    semantic_profile: SemanticProfile | None = None
    default_mode: OperatingMode | None = None
    recipe_kind: str | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("Profile.name is empty")
        if self.change_handoff is not None and not isinstance(
            self.change_handoff, ChangeHandoffMode
        ):
            raise TypeError(
                f"Profile {self.name!r}: change_handoff must be "
                f"ChangeHandoffMode or None, got "
                f"{type(self.change_handoff).__name__}"
            )
        if self.implementation_execution is not None and not isinstance(
            self.implementation_execution, ImplementationExecution
        ):
            raise TypeError(
                f"Profile {self.name!r}: implementation_execution must be "
                f"ImplementationExecution or None, got "
                f"{type(self.implementation_execution).__name__}"
            )
        # Stage C semantic identity: coerce/validate the enums and the
        # recipe-kind tag. Bad values fail fast here (e.g. an unknown
        # semantic_profile or default_mode string), matching the loader's
        # fail-fast policy.
        if self.semantic_profile is not None and not isinstance(
            self.semantic_profile, SemanticProfile
        ):
            object.__setattr__(
                self, "semantic_profile", SemanticProfile(self.semantic_profile)
            )
        if self.default_mode is not None and not isinstance(
            self.default_mode, OperatingMode
        ):
            object.__setattr__(
                self, "default_mode", OperatingMode(self.default_mode)
            )
        if self.recipe_kind is not None:
            if not isinstance(self.recipe_kind, str):
                raise TypeError(
                    f"Profile {self.name!r}: recipe_kind must be str or None, "
                    f"got {type(self.recipe_kind).__name__}"
                )
            if self.recipe_kind not in _VALID_RECIPE_KINDS:
                raise ValueError(
                    f"Profile {self.name!r}: recipe_kind="
                    f"{self.recipe_kind!r} is not one of "
                    f"{sorted(_VALID_RECIPE_KINDS)}"
                )
        if not self.steps:
            raise ValueError(f"Profile {self.name!r} has no steps")
        # Frozen dataclass: snapshot cross_gates into an immutable view
        # so callers cannot mutate the policy map through Profile.
        if not isinstance(self.cross_gates, Mapping):
            raise TypeError(
                f"Profile {self.name!r}: cross_gates must be a Mapping, "
                f"got {type(self.cross_gates).__name__}"
            )
        for gate_name, policy in self.cross_gates.items():
            if not isinstance(policy, CrossGatePolicy):
                raise TypeError(
                    f"Profile {self.name!r}: cross_gates[{gate_name!r}] "
                    f"must be CrossGatePolicy, got "
                    f"{type(policy).__name__}"
                )
        object.__setattr__(
            self,
            "cross_gates",
            MappingProxyType(dict(self.cross_gates)),
        )
        if self.sandbox is not None:
            if not isinstance(self.sandbox, Mapping):
                raise TypeError(
                    f"Profile {self.name!r}: sandbox must be a Mapping or None, "
                    f"got {type(self.sandbox).__name__}"
                )
            object.__setattr__(
                self,
                "sandbox",
                MappingProxyType(dict(self.sandbox)),
            )
        for i, entry in enumerate(self.steps):
            if not isinstance(entry, (PhaseStep, LoopStep)):
                raise TypeError(
                    f"Profile {self.name!r} step[{i}] must be PhaseStep or "
                    f"LoopStep, got {type(entry).__name__}"
                )
        match self.kind:
            case ProfileKind.FULL_CYCLE:
                valid = {d.value for d in FullCycleDepth}
                if self.variant not in valid:
                    raise ValueError(
                        f"Profile {self.name!r}: kind=FULL_CYCLE requires "
                        f"variant in {valid}, got {self.variant!r}"
                    )
            case ProfileKind.SCOPED:
                valid = {t.value for t in ScopedTarget}
                if self.variant not in valid:
                    raise ValueError(
                        f"Profile {self.name!r}: kind=SCOPED requires "
                        f"variant in {valid}, got {self.variant!r}"
                    )
            # CUSTOM: variant arbitrary or None


# Legacy helper entry: either a single phase name (str) or a LoopStep.
PhaseEntry = "str | LoopStep"


@dataclass(frozen=True)
class PipelineProfile:
    """Legacy in-process helper shape.

    The v1 JSON loader was removed in Phase 5d-5. This dataclass remains
    only for direct unit tests and the private ``_run_one_phase`` helper
    that dispatches an inline tuple of phase names.
    """
    name: str
    phases: tuple  # tuple[str | LoopStep, ...] — Python's typing too narrow for frozen dataclass

    def validate(self, registry: PhaseRegistry) -> None:
        """Raise ValueError if any phase name (top-level or nested in a
        LoopStep) is not registered in ``PhaseRegistry``.

        Phase 5e-5 substep 6: legacy ``modes_registry`` parameter
        removed. Entry-name composite dispatch (``"dag"`` →
        ``DagExecutionMode``) was deleted with v1 ``pipeline_profiles.json``
        in 5d-5; the registry it consulted (``pipeline.execution_modes``)
        is gone in substep 6. v2 ``PhaseStep.execution`` dispatch lives
        on ``LifecycleContext.execution_mode_registry`` and is checked
        by ``_validate_v2_entries`` for ``Profile`` shapes.
        """
        missing: list[str] = []
        for entry in self.phases:
            if isinstance(entry, str):
                if not registry.has(entry):
                    missing.append(entry)
            elif isinstance(entry, LoopStep):
                missing.extend(
                    s.phase for s in entry.steps
                    if not registry.has(s.phase)
                )
            else:
                raise TypeError(
                    f"profile {self.name!r}: entry {entry!r} is neither a "
                    f"phase name (str) nor a LoopStep"
                )
        if missing:
            raise ValueError(
                f"profile {self.name!r} references unknown phases: {missing}. "
                f"Registered: {registry.names()}"
            )


__all__ = [
    "ContractCheckMode",
    "CrossGatePolicy",
    "CrossGateRunPolicy",
    "CrossGateSkipPolicy",
    "CrossScope",
    "CrossStepPolicy",
    "ExecutionPolicy",
    "ExecutionSurface",
    "LoopStep",
    "PhaseEntry",
    "PipelineProfile",
    "Profile",
]
