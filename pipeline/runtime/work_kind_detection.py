"""
pipeline/runtime/work_kind_detection.py — Stage C auto-detect vocabulary.

This focused module hosts the typed value objects that describe the
*auto-detect* selector for the semantic-profile work: the detector's
successful output (``AutoDetectDecision``), the durable resolution outcome
(``AutoDetectResolution`` keyed by ``DetectionState``), the detector
contract (``WorkKindDetector`` ``Protocol``) plus deterministic
test/fake implementations, and the validated configuration
(``AutoDetectConfig``) loaded from user config over ``config.defaults.json``.

Home rationale (Stage C / T1):

- ``run_shape.py`` already owns the closed Stage B/C enums
  (``SemanticProfile`` / ``OperatingMode``) and the inert run-shape value
  objects; the Architecture Fitness Gate forbids piling a new
  responsibility — the auto-detect selector vocabulary — into that body.
- The auto-detect selector is a *distinct cohesive layer*: a recommendation
  shape, a resolution shape with its own invariant, a detector protocol,
  and a validated config. It belongs in its own focused module.

**Selector, not an enum member.** Auto-detect is deliberately *not* a member
of ``SemanticProfile`` / ``OperatingMode``: ``SemanticProfile('auto-detect')``
must keep raising ``ValueError``. This module imports those closed enums and
recommends/records members of them; it never extends them.

**Inert / side-effect-free import.** Importing this module performs no I/O,
reads no profile JSON, and runs no detector. The constructors perform
type/enum validation and coercion only (mirroring ``run_shape.py``).
``AutoDetectConfig.from_app_config`` is the single place that reads user
configuration, and it does so lazily — only when called. The provider-backed
detector (wired in a later subtask) must resolve its runtime lazily and must
not introduce provider side effects at import.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pipeline.runtime.run_shape import (
    DeliveryScope,
    OperatingMode,
    RunTopology,
    SemanticProfile,
)


def _coerce_confidence(value: Any, *, where: str) -> float:
    """Coerce/validate a confidence to a float in the closed range 0.0..1.0.

    A non-numeric value raises ``TypeError``; an out-of-range value raises
    ``ValueError``. ``bool`` is rejected explicitly (``True``/``False`` are
    ``int`` subclasses and would otherwise silently coerce to 1.0/0.0).
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(
            f"{where}.confidence must be a real number in [0.0, 1.0], got "
            f"{type(value).__name__}"
        )
    confidence = float(value)
    if not (0.0 <= confidence <= 1.0):
        raise ValueError(
            f"{where}.confidence must be within [0.0, 1.0], got {confidence}"
        )
    return confidence


def _coerce_risk_flags(value: Any, *, where: str) -> tuple[str, ...]:
    """Coerce a sequence of risk-flag strings into a ``tuple[str, ...]``.

    Accepts any non-``str`` sequence of strings (list/tuple). A bare ``str``
    is rejected to avoid the classic "iterate a string into characters"
    trap; a non-string element raises ``TypeError``.
    """

    if isinstance(value, str) or not isinstance(value, Sequence):
        raise TypeError(
            f"{where}.risk_flags must be a sequence of str, got "
            f"{type(value).__name__}"
        )
    flags: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise TypeError(
                f"{where}.risk_flags entries must be str, got "
                f"{type(item).__name__}"
            )
        flags.append(item)
    return tuple(flags)


def _coerce_projects(value: Any, *, where: str) -> tuple[str, ...]:
    """Coerce a sequence of project aliases into a ``tuple[str, ...]``.

    Same shape as :func:`_coerce_risk_flags`: a bare ``str`` is rejected to
    avoid iterating it into characters; a non-string element raises
    ``TypeError``.
    """

    if isinstance(value, str) or not isinstance(value, Sequence):
        raise TypeError(
            f"{where}.delivery_projects must be a sequence of str, got "
            f"{type(value).__name__}"
        )
    projects: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise TypeError(
                f"{where}.delivery_projects entries must be str, got "
                f"{type(item).__name__}"
            )
        projects.append(item)
    return tuple(projects)


# ── Detector output ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class AutoDetectDecision:
    """The successful *output* of a ``WorkKindDetector``.

    A detector recommends a concrete ``SemanticProfile`` + ``OperatingMode``
    with a calibrated ``confidence`` and a short ``rationale``. ``risk_flags``
    carries any short machine tags the detector wishes to surface;
    ``fallback_used`` records whether the detector itself fell back to a
    default internally (distinct from the *resolution*-level fallback states).

    ``recommended_topology`` records the recommended run topology (a closed
    ``RunTopology`` member, defaulting to ``MONO``); ``delivery_projects`` is
    the ordered tuple of project aliases a cross recommendation implicates; and
    ``topology_reason`` is a short, provider-neutral rationale for the
    topology. These three are an axis distinct from the profile + mode
    recommendation and never mutate the closed ``SemanticProfile`` enum.

    ``__post_init__`` coerces/validates the enums (a bad value such as
    ``'develop'`` raises ``ValueError``), validates ``confidence`` into the
    closed range 0.0..1.0, normalises ``risk_flags`` / ``delivery_projects`` to
    tuples, and coerces ``recommended_topology``. It performs no I/O.
    """

    recommended_profile: SemanticProfile
    recommended_mode: OperatingMode
    confidence: float
    rationale: str = ""
    risk_flags: tuple[str, ...] = ()
    fallback_used: bool = False
    recommended_topology: RunTopology = RunTopology.MONO
    delivery_projects: tuple[str, ...] = ()
    topology_reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.recommended_profile, SemanticProfile):
            object.__setattr__(
                self,
                "recommended_profile",
                SemanticProfile(self.recommended_profile),
            )
        if not isinstance(self.recommended_mode, OperatingMode):
            object.__setattr__(
                self, "recommended_mode", OperatingMode(self.recommended_mode)
            )
        object.__setattr__(
            self,
            "confidence",
            _coerce_confidence(self.confidence, where="AutoDetectDecision"),
        )
        if not isinstance(self.rationale, str):
            raise TypeError(
                "AutoDetectDecision.rationale must be str, got "
                f"{type(self.rationale).__name__}"
            )
        object.__setattr__(
            self,
            "risk_flags",
            _coerce_risk_flags(self.risk_flags, where="AutoDetectDecision"),
        )
        if not isinstance(self.fallback_used, bool):
            raise TypeError(
                "AutoDetectDecision.fallback_used must be bool, got "
                f"{type(self.fallback_used).__name__}"
            )
        if not isinstance(self.recommended_topology, RunTopology):
            object.__setattr__(
                self,
                "recommended_topology",
                RunTopology(self.recommended_topology),
            )
        object.__setattr__(
            self,
            "delivery_projects",
            _coerce_projects(
                self.delivery_projects, where="AutoDetectDecision"
            ),
        )
        if not isinstance(self.topology_reason, str):
            raise TypeError(
                "AutoDetectDecision.topology_reason must be str, got "
                f"{type(self.topology_reason).__name__}"
            )


# ── Resolution vocabulary ───────────────────────────────────────────────────


class DetectionState(StrEnum):
    """Closed set of terminal auto-detect resolution states.

    - ``RECOMMENDED`` — the detector's recommendation was accepted as-is
      (trusted auto-accept or operator accept).
    - ``LOW_CONFIDENCE_FALLBACK`` — the detector returned a decision but its
      confidence was below threshold, so the configured fallback profile was
      used instead.
    - ``DETECTOR_ERROR_FALLBACK`` — the detector raised; no recommendation
      exists, and the configured fallback profile was used.
    - ``FAILED`` — resolution failed deterministically (``on_*`` = ``fail``);
      a run does not start, so this resolution is typically not persisted.
    """

    RECOMMENDED = "recommended"
    LOW_CONFIDENCE_FALLBACK = "low_confidence_fallback"
    DETECTOR_ERROR_FALLBACK = "detector_error_fallback"
    FAILED = "failed"


# States where the detector produced no usable decision, so the
# ``recommended_*`` fields are permitted to be ``None``.
_RECOMMENDATION_OPTIONAL_STATES = frozenset(
    {DetectionState.DETECTOR_ERROR_FALLBACK, DetectionState.FAILED}
)


@dataclass(frozen=True)
class AutoDetectResolution:
    """Durable typed outcome of resolving the auto-detect selector.

    Always carries the ``detection_state``, the ``actual_profile`` +
    ``actual_mode`` the run actually starts with, and the ``policy`` that was
    in force. The ``recommended_*`` echo of the detector's decision is present
    whenever the detector produced one, and is ``None`` only when no decision
    exists (``DETECTOR_ERROR_FALLBACK`` / ``FAILED``).

    Fields
    ------
    detection_state:
        Which resolution branch fired (see ``DetectionState``).
    actual_profile / actual_mode:
        The concrete profile + mode the run starts with. Always present.
    policy:
        The ``AutoDetectPolicy`` in force when resolving.
    recommended_profile / recommended_mode / confidence / rationale /
    risk_flags:
        Echo of the detector's ``AutoDetectDecision``. Present for
        ``RECOMMENDED`` / ``LOW_CONFIDENCE_FALLBACK``; ``None`` is permitted
        only for ``DETECTOR_ERROR_FALLBACK`` / ``FAILED``.
    fallback_used:
        Whether resolution used the configured fallback profile.
    confirmation_state:
        Optional short tag for how the recommendation was confirmed
        (e.g. ``"accepted"``, ``"override"``, ``"auto"``). ``None`` when not
        applicable.
    error_reason:
        Optional short reason recorded for a detector error.
    fallback_reason:
        Optional short reason recorded for a fallback (low-confidence or
        error fallback).
    recommended_topology:
        The recommended run topology (``RunTopology``, default ``MONO``). A
        ``cross_recommended`` value records a recommendation only; it never
        converts the run into a cross run.
    delivery_projects:
        Ordered tuple of project aliases implicated by a cross recommendation.
        Empty for a mono recommendation.
    topology_reason:
        Short, provider-neutral rationale for the topology. Default ``""``.
    delivery_scope:
        The delivery scope the run resolves to (``DeliveryScope``, default
        ``STRICT_MONO``). Independent of ``recommended_topology``: a
        ``cross_recommended`` topology never silently widens the scope. Only an
        explicit operator directive (see ``apply_topology_choice``) moves the
        scope to ``EXPANDED_MONO`` / ``CROSS``; trusted / non-interactive
        resolution keeps ``STRICT_MONO`` regardless of topology.

    Invariant (``__post_init__``): ``recommended_profile`` /
    ``recommended_mode`` may be ``None`` *only* when ``detection_state`` is a
    recommendation-optional state; for ``RECOMMENDED`` and
    ``LOW_CONFIDENCE_FALLBACK`` they are required. ``actual_profile`` /
    ``actual_mode`` are required for every state. Enums are coerced;
    ``confidence`` (when present) is range-validated. No I/O.
    """

    detection_state: DetectionState
    actual_profile: SemanticProfile
    actual_mode: OperatingMode
    policy: AutoDetectPolicy
    recommended_profile: SemanticProfile | None = None
    recommended_mode: OperatingMode | None = None
    confidence: float | None = None
    rationale: str | None = None
    risk_flags: tuple[str, ...] | None = None
    fallback_used: bool = False
    confirmation_state: str | None = None
    error_reason: str | None = None
    fallback_reason: str | None = None
    recommended_topology: RunTopology = RunTopology.MONO
    delivery_projects: tuple[str, ...] = ()
    topology_reason: str = ""
    delivery_scope: DeliveryScope = DeliveryScope.STRICT_MONO

    def __post_init__(self) -> None:
        if not isinstance(self.detection_state, DetectionState):
            object.__setattr__(
                self, "detection_state", DetectionState(self.detection_state)
            )
        # actual_* are mandatory for every persisted resolution.
        if self.actual_profile is None:
            raise ValueError(
                "AutoDetectResolution.actual_profile is required for every "
                "resolution"
            )
        if self.actual_mode is None:
            raise ValueError(
                "AutoDetectResolution.actual_mode is required for every "
                "resolution"
            )
        if not isinstance(self.actual_profile, SemanticProfile):
            object.__setattr__(
                self, "actual_profile", SemanticProfile(self.actual_profile)
            )
        if not isinstance(self.actual_mode, OperatingMode):
            object.__setattr__(
                self, "actual_mode", OperatingMode(self.actual_mode)
            )
        if not isinstance(self.policy, AutoDetectPolicy):
            object.__setattr__(self, "policy", AutoDetectPolicy(self.policy))

        # Recommendation echo: coerce when present.
        if self.recommended_profile is not None and not isinstance(
            self.recommended_profile, SemanticProfile
        ):
            object.__setattr__(
                self,
                "recommended_profile",
                SemanticProfile(self.recommended_profile),
            )
        if self.recommended_mode is not None and not isinstance(
            self.recommended_mode, OperatingMode
        ):
            object.__setattr__(
                self, "recommended_mode", OperatingMode(self.recommended_mode)
            )
        if self.confidence is not None:
            object.__setattr__(
                self,
                "confidence",
                _coerce_confidence(
                    self.confidence, where="AutoDetectResolution"
                ),
            )
        if self.risk_flags is not None:
            object.__setattr__(
                self,
                "risk_flags",
                _coerce_risk_flags(
                    self.risk_flags, where="AutoDetectResolution"
                ),
            )
        for str_field in ("rationale", "confirmation_state",
                          "error_reason", "fallback_reason"):
            val = getattr(self, str_field)
            if val is not None and not isinstance(val, str):
                raise TypeError(
                    f"AutoDetectResolution.{str_field} must be str or None, "
                    f"got {type(val).__name__}"
                )
        if not isinstance(self.fallback_used, bool):
            raise TypeError(
                "AutoDetectResolution.fallback_used must be bool, got "
                f"{type(self.fallback_used).__name__}"
            )
        if not isinstance(self.recommended_topology, RunTopology):
            object.__setattr__(
                self,
                "recommended_topology",
                RunTopology(self.recommended_topology),
            )
        object.__setattr__(
            self,
            "delivery_projects",
            _coerce_projects(
                self.delivery_projects, where="AutoDetectResolution"
            ),
        )
        if not isinstance(self.topology_reason, str):
            raise TypeError(
                "AutoDetectResolution.topology_reason must be str, got "
                f"{type(self.topology_reason).__name__}"
            )
        if not isinstance(self.delivery_scope, DeliveryScope):
            object.__setattr__(
                self, "delivery_scope", DeliveryScope(self.delivery_scope)
            )

        # Core invariant: recommended_* may be None ONLY when the detector
        # produced no decision (error/failed). For an accepted recommendation
        # or a low-confidence fallback, both must be present — a fallback must
        # never be passed off as "no recommendation existed".
        recommendation_missing = (
            self.recommended_profile is None or self.recommended_mode is None
        )
        if (
            recommendation_missing
            and self.detection_state not in _RECOMMENDATION_OPTIONAL_STATES
        ):
            raise ValueError(
                "AutoDetectResolution.recommended_profile/recommended_mode "
                "are required unless detection_state is "
                "DETECTOR_ERROR_FALLBACK/FAILED; got missing recommendation "
                f"for detection_state={self.detection_state.value}"
            )


# ── Detector contract + deterministic test implementations ──────────────────


@runtime_checkable
class WorkKindDetector(Protocol):
    """Contract for a work-kind detector.

    A detector inspects the run's task text and project context and returns a
    successful ``AutoDetectDecision``. Raising is an allowed failure mode: the
    caller (wired in a later subtask) treats any exception as a
    detector-error and applies the configured ``on_error`` policy. The
    provider-backed implementation must resolve its runtime lazily and must
    not run any LLM at import time.
    """

    def detect(self, *, task: str, project: str) -> AutoDetectDecision:
        ...


@dataclass(frozen=True)
class StaticWorkKindDetector:
    """Deterministic detector returning a fixed decision — no network/LLM.

    Used by tests and as an injectable fake. The decision is validated at
    construction time via ``AutoDetectDecision``'s own ``__post_init__``.
    """

    decision: AutoDetectDecision

    def detect(self, *, task: str, project: str) -> AutoDetectDecision:
        return self.decision


@dataclass(frozen=True)
class RaisingWorkKindDetector:
    """Deterministic detector that always raises — no network/LLM.

    Used to exercise the detector-error branch. The exception instance to
    raise is supplied at construction; it defaults to a ``RuntimeError``.
    """

    error: BaseException = field(
        default_factory=lambda: RuntimeError("work-kind detection failed")
    )

    def detect(self, *, task: str, project: str) -> AutoDetectDecision:
        raise self.error


# ── Configuration ───────────────────────────────────────────────────────────


class AutoDetectPolicy(StrEnum):
    """Closed set of auto-detect dispatch policies.

    - ``CONFIRM`` — on an interactive TTY, show the recommendation and let the
      operator accept or override; non-TTY degrades to trusted threshold
      gating (handled by the dispatcher in a later subtask).
    - ``TRUST_ABOVE_THRESHOLD`` — deterministic auto-selection: accept the
      recommendation when ``confidence >= confidence_threshold``.
    """

    CONFIRM = "confirm"
    TRUST_ABOVE_THRESHOLD = "trust_above_threshold"


class FallbackAction(StrEnum):
    """What to do when the detector is low-confidence or errors."""

    FALLBACK = "fallback"
    FAIL = "fail"


@dataclass(frozen=True)
class AutoDetectConfig:
    """Validated auto-detect configuration.

    Fields
    ------
    policy:
        Dispatch policy (``AutoDetectPolicy``).
    confidence_threshold:
        Trusted-accept bar, a float in the closed range 0.0..1.0.
    fallback_profile:
        Concrete ``SemanticProfile`` used on low-confidence / error fallback.
        Must be a real member — ``auto-detect`` is not a profile.
    on_low_confidence / on_error:
        ``FallbackAction`` controlling the low-confidence and detector-error
        branches.

    Construct via :meth:`parse` (pure, dict in) or :meth:`from_app_config`
    (reads ``AppConfig.load().pipeline['auto_detect']`` over
    ``config.defaults.json`` — the same ``parse`` underneath). The
    constructor coerces/validates enums and the threshold range; bad values
    raise ``ValueError`` / ``TypeError``.
    """

    policy: AutoDetectPolicy
    confidence_threshold: float
    fallback_profile: SemanticProfile
    on_low_confidence: FallbackAction = FallbackAction.FALLBACK
    on_error: FallbackAction = FallbackAction.FALLBACK

    def __post_init__(self) -> None:
        if not isinstance(self.policy, AutoDetectPolicy):
            object.__setattr__(self, "policy", AutoDetectPolicy(self.policy))
        object.__setattr__(
            self,
            "confidence_threshold",
            _coerce_threshold(self.confidence_threshold),
        )
        if not isinstance(self.fallback_profile, SemanticProfile):
            object.__setattr__(
                self,
                "fallback_profile",
                SemanticProfile(self.fallback_profile),
            )
        if not isinstance(self.on_low_confidence, FallbackAction):
            object.__setattr__(
                self,
                "on_low_confidence",
                FallbackAction(self.on_low_confidence),
            )
        if not isinstance(self.on_error, FallbackAction):
            object.__setattr__(
                self, "on_error", FallbackAction(self.on_error)
            )

    @classmethod
    def parse(cls, raw: Mapping[str, Any] | None) -> AutoDetectConfig:
        """Pure parse/validate of an ``auto_detect`` config mapping.

        Unknown keys (e.g. a leading ``_comment``) are ignored. A missing /
        empty mapping yields the shipped defaults. A bad threshold, unknown
        policy, unknown ``fallback_profile``, or unknown ``on_*`` action
        raises ``ValueError`` (``SemanticProfile('auto-detect')`` /
        ``AutoDetectPolicy('nope')`` both raise ``ValueError``).
        """

        data: Mapping[str, Any] = raw or {}
        if not isinstance(data, Mapping):
            raise TypeError(
                "AutoDetectConfig.parse expects a mapping or None, got "
                f"{type(data).__name__}"
            )
        kwargs: dict[str, Any] = {}
        if "policy" in data:
            kwargs["policy"] = data["policy"]
        else:
            kwargs["policy"] = AutoDetectPolicy.CONFIRM
        if "confidence_threshold" in data:
            kwargs["confidence_threshold"] = data["confidence_threshold"]
        else:
            kwargs["confidence_threshold"] = 0.7
        if "fallback_profile" in data:
            kwargs["fallback_profile"] = data["fallback_profile"]
        else:
            kwargs["fallback_profile"] = SemanticProfile.FEATURE
        if "on_low_confidence" in data:
            kwargs["on_low_confidence"] = data["on_low_confidence"]
        if "on_error" in data:
            kwargs["on_error"] = data["on_error"]
        return cls(**kwargs)

    @classmethod
    def from_app_config(cls) -> AutoDetectConfig:
        """Load from user config (``AppConfig.pipeline['auto_detect']``).

        Reads the merged pipeline section — ``config.defaults.json`` plus any
        local overlay — and parses the ``auto_detect`` subsection through the
        same :meth:`parse`. Imports the config module lazily so importing this
        module stays side-effect free with respect to config I/O.
        """

        from core.infra import config as _config

        pipeline_cfg = _config.AppConfig.load().pipeline
        return cls.parse(pipeline_cfg.get("auto_detect"))


def _coerce_threshold(value: Any) -> float:
    """Coerce/validate a confidence threshold to a float in 0.0..1.0."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(
            "AutoDetectConfig.confidence_threshold must be a real number in "
            f"[0.0, 1.0], got {type(value).__name__}"
        )
    threshold = float(value)
    if not (0.0 <= threshold <= 1.0):
        raise ValueError(
            "AutoDetectConfig.confidence_threshold must be within "
            f"[0.0, 1.0], got {threshold}"
        )
    return threshold


__all__ = [
    "AutoDetectConfig",
    "AutoDetectDecision",
    "AutoDetectPolicy",
    "AutoDetectResolution",
    "DetectionState",
    "FallbackAction",
    "RaisingWorkKindDetector",
    "StaticWorkKindDetector",
    "WorkKindDetector",
]
