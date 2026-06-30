"""
pipeline/runtime/run_shape.py — Stage B inert run-shape vocabulary.

This module hosts the Stage B *value objects* for the semantic-profile
alignment work: the ``SemanticProfile`` and ``OperatingMode`` enums plus
the ``OperatingModePolicy`` and ``RunShape`` frozen dataclasses.

Home rationale (decided in Stage B / T1):

- ``profile.py`` (~466 lines: ``Profile``, ``ExecutionPolicy``, cross-gate
  policy, ``LoopStep``, ``PipelineProfile``) is already wide and the
  Architecture Fitness Gate forbids adding a new responsibility to its
  body.
- ``roles.py`` is an I/O-free ``StrEnum``-only layer; the run-shape value
  objects (``OperatingModePolicy`` / ``RunShape`` frozen dataclasses, plus
  the ``operating_mode == policy.operating_mode`` consistency invariant)
  are a distinct cohesive layer that should not be split across files.

Hence this focused module owns the full Stage B vocabulary — both enums and
both dataclasses — together.

**Inert by construction.** Every type here is an inert Stage B value
object; there is no resolver. The shipped flat profiles
(``lite`` / ``advanced`` / ``enterprise`` / ``plan`` / ``review`` /
``task`` / ``correction``) remain the executable surface. This module
deliberately contains no ``resolve_run_shape()`` and no helper that maps a
flat profile onto a ``SemanticProfile``. Importing it is side-effect free
with respect to the profile loader, profile JSON, git, and the
environment: it must not import ``pipeline.profiles.loader`` or read
``core/_config/pipeline_profiles_v2.json``, and its constructors perform
type/enum validation only — never I/O.

A later stage wires the resolver, CLI/SDK/MCP surfaces, auto-selection,
and the policy knobs that would change runtime behaviour; all of that is
explicitly deferred and out of scope here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pipeline.runtime.roles import ImplementationExecution


class SemanticProfile(StrEnum):
    """Inert Stage B value object: the goal-shaped semantic profile name.

    No resolver maps the shipped flat profiles onto these members; the
    shipped flat profiles (``lite`` / ``advanced`` / ``enterprise`` /
    ``plan`` / ``review`` / ``task`` / ``correction``) remain the
    executable surface. These names describe *what kind of work* a future
    resolver would shape a run around.

    The member set is the closed Stage C vocabulary of nine goal-shaped
    work kinds: ``small_task``, ``feature``, ``complex_feature``,
    ``planning``, ``code_review``, ``delivery_audit``, ``research``,
    ``refactor``, and ``migration``. It intentionally excludes ``develop``
    — ``SemanticProfile('develop')`` raises ``ValueError`` — and the
    historical draft name ``heavy_feature`` (now ``complex_feature``) is
    not retained, even as an alias: ``SemanticProfile('heavy_feature')``
    raises ``ValueError``.
    """

    SMALL_TASK = "small_task"
    FEATURE = "feature"
    COMPLEX_FEATURE = "complex_feature"
    PLANNING = "planning"
    CODE_REVIEW = "code_review"
    DELIVERY_AUDIT = "delivery_audit"
    RESEARCH = "research"
    REFACTOR = "refactor"
    MIGRATION = "migration"


class RunTopology(StrEnum):
    """Inert value object: the recommended *topology* of a run.

    Topology is a distinct axis from the semantic profile and the operating
    mode. It records whether a run is best executed as a single-repo run
    (``mono``) or whether a multi-repo cross-project run is *recommended*
    (``cross_recommended``).

    This is a closed, inert enum. It is deliberately **not** a member of
    ``SemanticProfile`` — ``SemanticProfile('cross')`` keeps raising
    ``ValueError``. A ``cross_recommended`` topology only records a
    recommendation; it never silently converts a mono run into a cross run.
    """

    MONO = "mono"
    CROSS_RECOMMENDED = "cross_recommended"


class DeliveryScope(StrEnum):
    """Inert value object: the delivery scope applied at final delivery.

    A third axis, independent of both the semantic profile and the run
    topology. It records how changes are collected and validated at delivery
    time:

    - ``strict_mono`` — only the primary repo is in scope; changes in sibling
      repos are a typed, reversible delivery-scope violation.
    - ``expanded_mono`` — the run stays mono, but sibling-repo changes are
      disclosed per alias instead of being treated as a violation.
    - ``cross`` — a genuine multi-repo cross-project delivery.

    Closed and inert: naming a scope, not enforcing it. Enforcement is wired
    by a later subtask.
    """

    STRICT_MONO = "strict_mono"
    EXPANDED_MONO = "expanded_mono"
    CROSS = "cross"


class OperatingMode(StrEnum):
    """Inert Stage B value object: strictness posture of a run.

    Three closed members: ``fast``, ``pro``, ``governed``. This is an inert
    value object — naming a posture, not enforcing it; the shipped flat
    profiles remain the executable surface and no resolver consumes this.

    Historical note: an earlier draft used ``team`` for what is now
    ``pro``. ``team`` is *not* a live member — ``OperatingMode('team')``
    raises ``ValueError``.
    """

    FAST = "fast"
    PRO = "pro"
    GOVERNED = "governed"


@dataclass(frozen=True)
class OperatingModePolicy:
    """Inert Stage B value object: strictness posture, described not enforced.

    Carries the knobs a future resolver would attach to an
    ``OperatingMode``. Every field is inert: it records intent, it does not
    execute it. The shipped flat profiles remain the executable surface and
    no resolver reads this today.

    Any knob whose non-default value would require changing current runtime
    behaviour to honour it is modelled as an optional field with a
    documented empty/false default (or deferred entirely), so constructing
    a policy never implies a behaviour change.

    Fields
    ------
    operating_mode:
        The posture this policy describes.
    require_proof_before_transitions:
        Inert intent flag. Default ``False`` — current behaviour does not
        require proof before phase transitions; flipping this is deferred
        to the resolver stage.
    repair_on_gate_failure:
        Inert intent flag. Default ``False`` — current behaviour does not
        auto-repair on gate failure; flipping this is deferred.
    notes:
        Free-text rationale for a future resolver to surface. Default
        ``""``.

    ``__post_init__`` validates types / coerces the enum only — no I/O.
    """

    operating_mode: OperatingMode
    require_proof_before_transitions: bool = False
    repair_on_gate_failure: bool = False
    notes: str = ""

    def __post_init__(self) -> None:
        # Coerce/validate the enum: a bad value (e.g. "team") raises
        # ValueError here, matching the fail-fast style of Profile.
        if not isinstance(self.operating_mode, OperatingMode):
            object.__setattr__(
                self, "operating_mode", OperatingMode(self.operating_mode)
            )
        if not isinstance(self.require_proof_before_transitions, bool):
            raise TypeError(
                "OperatingModePolicy.require_proof_before_transitions must "
                f"be bool, got "
                f"{type(self.require_proof_before_transitions).__name__}"
            )
        if not isinstance(self.repair_on_gate_failure, bool):
            raise TypeError(
                "OperatingModePolicy.repair_on_gate_failure must be bool, "
                f"got {type(self.repair_on_gate_failure).__name__}"
            )
        if not isinstance(self.notes, str):
            raise TypeError(
                "OperatingModePolicy.notes must be str, got "
                f"{type(self.notes).__name__}"
            )


@dataclass(frozen=True)
class ScopeExpansionSanctionPolicy:
    """Inert policy carrier for the scope-expansion sanction projection (§5).

    The ADR 0112 §5 sanction *policy* a future resolver projects from an
    :class:`OperatingMode` and attaches to a :class:`RunShape`. It is a
    **projection carrier, not a baked outcome**: it records only the strictness
    posture the sanction decision keys on. The actual routing
    (:class:`~pipeline.runtime.roles.ScopeExpansionSanction`) is always computed
    by :func:`pipeline.runtime.scope_expansion_sanction.decide` from this
    posture *plus* the per-file status / genuine-safety / waiver signals — it is
    never stored here. Storing a single outcome enum on the run shape would
    collapse the whole status/category/waiver matrix into one fixed cell and
    re-introduce the "prison rule" this knob exists to remove; carrying the
    posture keeps the full matrix expressible through ``decide``.

    The posture is the only knob the §5 matrix needs — the routing is a total
    function of ``operating_mode`` × status × genuine-safety × waiver — so this
    carrier deliberately holds just the mode plus free-text ``notes``, mirroring
    :class:`OperatingModePolicy`. A richer per-status override table would be a
    future resolver concern and is intentionally not modelled here; the
    projection table that maps each mode to one of these carriers lives in
    :mod:`pipeline.runtime.scope_expansion_sanction`.

    ``__post_init__`` validates types / coerces the enum only — no I/O.
    """

    operating_mode: OperatingMode
    notes: str = ""

    def __post_init__(self) -> None:
        # Coerce/validate the enum: a bad value (e.g. "team") raises ValueError
        # here, matching the fail-fast style of OperatingModePolicy.
        if not isinstance(self.operating_mode, OperatingMode):
            object.__setattr__(
                self, "operating_mode", OperatingMode(self.operating_mode)
            )
        if not isinstance(self.notes, str):
            raise TypeError(
                "ScopeExpansionSanctionPolicy.notes must be str, got "
                f"{type(self.notes).__name__}"
            )


@dataclass(frozen=True)
class RunShape:
    """Inert Stage B value object: the shape a future resolver would emit.

    Maps onto concepts already shipped today (semantic profile, operating
    mode, worktree-isolation intent, implementation-execution intent, and
    which phases a run includes). It is inert: constructing a ``RunShape``
    models the *output* of a resolver that does not exist yet. The shipped
    flat profiles remain the executable surface; nothing here is executed.

    Fields
    ------
    semantic_profile:
        The goal-shaped profile this run targets.
    operating_mode:
        The strictness posture of this run.
    worktree_isolation_intent:
        Optional declared isolation intent (e.g. ``"per_run"`` /
        ``"per_phase"``), mirroring ``Profile.worktree_isolation``.
        ``None`` means "unspecified — use the global default". Stored as a
        string intent, not enforced.
    implementation_execution_intent:
        Optional reuse of ``ImplementationExecution`` from ``roles.py`` as
        a declared intent. ``ImplementationExecution`` is **not** modified
        by this module. ``None`` means "unspecified".
    includes_planning / includes_review / includes_repair /
    includes_final_acceptance:
        Inert booleans recording which phases this shape would contain.
        Documented defaults: planning/review/repair ``False`` (a bare run
        is implement-only), final acceptance ``False``. A resolver would
        set these; today they only describe intent.
    reason:
        Free-text rationale a future resolver would emit. Default ``""``.
    notes:
        Additional free-text. Default ``""``.
    policy:
        Optional carrier for an ``OperatingModePolicy``. ``None`` means "no
        attached policy". This is a carrier field only — no policy is
        computed here (the resolver is not implemented). When present, its
        ``operating_mode`` must match this shape's ``operating_mode`` (see
        the consistency invariant below).
    scope_expansion_sanction:
        Optional carrier for a ``ScopeExpansionSanctionPolicy`` — the ADR 0112
        §5 sanction posture this run would route out-of-plan scope expansions
        with. ``None`` means "no attached sanction policy". Like ``policy`` it
        is a *projection carrier, not a baked outcome*: the actual
        ``ScopeExpansionSanction`` route is computed by
        :func:`pipeline.runtime.scope_expansion_sanction.decide` from the
        carried posture plus status/genuine-safety/waiver signals, never stored
        here. When present, its ``operating_mode`` must match this shape's
        ``operating_mode`` (same consistency invariant as ``policy``).

    Consistency invariant (Stage B F2): a non-``None`` ``policy`` —and likewise
    a non-``None`` ``scope_expansion_sanction``— must agree with
    ``operating_mode``. This rejects a formally valid but contradictory shape,
    which matters because ``RunShape`` models a future resolver's output.

    ``__post_init__`` validates types / coerces enums only — no I/O.
    """

    semantic_profile: SemanticProfile
    operating_mode: OperatingMode
    worktree_isolation_intent: str | None = None
    implementation_execution_intent: ImplementationExecution | None = None
    includes_planning: bool = False
    includes_review: bool = False
    includes_repair: bool = False
    includes_final_acceptance: bool = False
    reason: str = ""
    notes: str = ""
    policy: OperatingModePolicy | None = None
    scope_expansion_sanction: ScopeExpansionSanctionPolicy | None = None

    def __post_init__(self) -> None:
        # Coerce/validate the enums first: bad values fail fast with
        # ValueError (e.g. SemanticProfile('develop') / OperatingMode('team')).
        if not isinstance(self.semantic_profile, SemanticProfile):
            object.__setattr__(
                self, "semantic_profile", SemanticProfile(self.semantic_profile)
            )
        if not isinstance(self.operating_mode, OperatingMode):
            object.__setattr__(
                self, "operating_mode", OperatingMode(self.operating_mode)
            )
        if self.worktree_isolation_intent is not None and not isinstance(
            self.worktree_isolation_intent, str
        ):
            raise TypeError(
                "RunShape.worktree_isolation_intent must be str or None, got "
                f"{type(self.worktree_isolation_intent).__name__}"
            )
        if self.implementation_execution_intent is not None and not isinstance(
            self.implementation_execution_intent, ImplementationExecution
        ):
            raise TypeError(
                "RunShape.implementation_execution_intent must be "
                "ImplementationExecution or None, got "
                f"{type(self.implementation_execution_intent).__name__}"
            )
        for flag_name in (
            "includes_planning",
            "includes_review",
            "includes_repair",
            "includes_final_acceptance",
        ):
            value = getattr(self, flag_name)
            if not isinstance(value, bool):
                raise TypeError(
                    f"RunShape.{flag_name} must be bool, got "
                    f"{type(value).__name__}"
                )
        if not isinstance(self.reason, str):
            raise TypeError(
                f"RunShape.reason must be str, got {type(self.reason).__name__}"
            )
        if not isinstance(self.notes, str):
            raise TypeError(
                f"RunShape.notes must be str, got {type(self.notes).__name__}"
            )
        if self.policy is not None:
            if not isinstance(self.policy, OperatingModePolicy):
                raise TypeError(
                    "RunShape.policy must be OperatingModePolicy or None, got "
                    f"{type(self.policy).__name__}"
                )
            # Consistency invariant (F2): a carried policy must agree with
            # this shape's operating mode.
            if self.policy.operating_mode != self.operating_mode:
                raise ValueError(
                    "RunShape.operating_mode="
                    f"{self.operating_mode.value} does not match "
                    "policy.operating_mode="
                    f"{self.policy.operating_mode.value}"
                )
        if self.scope_expansion_sanction is not None:
            if not isinstance(
                self.scope_expansion_sanction, ScopeExpansionSanctionPolicy
            ):
                raise TypeError(
                    "RunShape.scope_expansion_sanction must be "
                    "ScopeExpansionSanctionPolicy or None, got "
                    f"{type(self.scope_expansion_sanction).__name__}"
                )
            # Consistency invariant (F2): a carried sanction policy must agree
            # with this shape's operating mode, same as ``policy``.
            if self.scope_expansion_sanction.operating_mode != self.operating_mode:
                raise ValueError(
                    "RunShape.operating_mode="
                    f"{self.operating_mode.value} does not match "
                    "scope_expansion_sanction.operating_mode="
                    f"{self.scope_expansion_sanction.operating_mode.value}"
                )


def coerce_operating_mode(raw: Any) -> OperatingMode | None:
    """Coerce a raw posture (``OperatingMode`` / member string) to the enum.

    Pure value-object coercion — no I/O, no resolver: it never maps a flat
    profile onto a posture, it only narrows an already-resolved value. Returns
    ``None`` (rather than raising) for an absent / blank / unknown value so
    callers can apply their own conservative default.
    """
    if isinstance(raw, OperatingMode):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return OperatingMode(raw.strip())
        except ValueError:
            return None
    return None


def operating_mode_from_state(state: Any) -> OperatingMode:
    """Resolve a run's :class:`OperatingMode` from its projected state stamp.

    The SINGLE reader of the run's strictness posture for the in-process
    sanction sites — the ``final_acceptance`` scope gate (via
    :mod:`pipeline.phases.builtin.session_keys`) and the participant-promotion
    governed route (:mod:`pipeline.participant_promotion`). Both read the one
    posture that :func:`pipeline.project.state_setup.build_pipeline_state`
    projects ONCE onto ``state.extras['operating_mode']`` from the resolved
    verification work-mode / auto-detect ``actual_mode`` / profile default — so
    the two sites can never diverge on the run's mode. Falls back to the
    conservative ``fast`` posture when no posture is projected (mode unresolved).
    Pure read — no I/O.
    """
    extras = getattr(state, "extras", None) or {}
    return coerce_operating_mode(extras.get("operating_mode")) or OperatingMode.FAST


__all__ = [
    "DeliveryScope",
    "OperatingMode",
    "OperatingModePolicy",
    "RunShape",
    "RunTopology",
    "ScopeExpansionSanctionPolicy",
    "SemanticProfile",
    "coerce_operating_mode",
    "operating_mode_from_state",
]
