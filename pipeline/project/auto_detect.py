"""
pipeline/project/auto_detect.py — Stage C auto-detect dispatch (T3).

Single owner of resolving the ``auto-detect`` selector token into a concrete
``SemanticProfile`` + ``OperatingMode`` and a durable typed
``AutoDetectResolution`` (see ``pipeline.runtime.work_kind_detection``). The
CLI (`pipeline/project/cli.py:main`) calls :func:`resolve_auto_detect` once,
*after* task/project are resolved and *before* ``run_pipeline`` — covering both
``orcho run`` (via ``run_pipeline_from_args``) and the direct ``orcho-run``
entry. A manual concrete ``--profile`` never enters this path.

Algorithm (effective profile == :data:`AUTO_DETECT_PROFILE_TOKEN`):

- Read :class:`AutoDetectConfig` from user config (T1).
- ``interactive`` = TTY *and* not ``--no-interactive`` (the same signal
  ``require_profile_or_exit`` uses).
- **(a) confirm on a TTY** (``interactive`` and ``policy == CONFIRM``): run the
  detector, show the recommendation, let the operator accept it
  (``RECOMMENDED``) or pick another semantic profile (confirm-override).
- **(b) trusted / non-interactive** (any non-interactive context, *and*
  ``policy == TRUST_ABOVE_THRESHOLD`` on any context): threshold-gated
  auto-selection — never fail-fast merely for the absence of a TTY and never
  prompt. ``confidence >= threshold`` accepts the recommendation
  (``RECOMMENDED``); below threshold applies ``on_low_confidence``
  (``LOW_CONFIDENCE_FALLBACK`` or ``FAILED``); a detector exception applies
  ``on_error`` (``DETECTOR_ERROR_FALLBACK`` — ``recommended_*`` omitted,
  ``error_reason`` recorded — or ``FAILED``).
- **actual_mode** is deterministic: an explicit operator ``--mode`` always
  wins; otherwise ``recommended_mode`` is used *only* for an accepted
  recommendation (``RECOMMENDED`` without an operator profile change);
  every other branch (confirm-override / low-confidence / detector-error
  fallback) uses ``default_operating_mode(actual_profile)``.

The provider-backed detector (:class:`ProviderWorkKindDetector`) is isolated:
it resolves its runtime lazily inside :meth:`~ProviderWorkKindDetector.detect`
and performs no provider work at import. Unit tests inject a fake detector
(T1) and never touch a real LLM.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import StrEnum

from core.io.ansi import C, paint
from pipeline.runtime.run_shape import (
    DeliveryScope,
    OperatingMode,
    SemanticProfile,
)
from pipeline.runtime.semantic_mode_defaults import default_operating_mode
from pipeline.runtime.topology_detection import recommend_topology
from pipeline.runtime.work_kind_detection import (
    AutoDetectConfig,
    AutoDetectDecision,
    AutoDetectPolicy,
    AutoDetectResolution,
    DetectionState,
    FallbackAction,
    WorkKindDetector,
)

# Selector token shared with the interactive picker
# (``cli._profile_menu.AUTO_DETECT_CHOICE``). NOT a ``SemanticProfile`` member.
AUTO_DETECT_PROFILE_TOKEN = "auto-detect"

# Scoped env channel carrying the serialized resolution into ``run_pipeline``
# (its signature is locked). Set strictly around the run and restored after.
AUTODETECT_DECISION_ENV = "ORCHO_AUTODETECT_DECISION"

# Verification-strictness channel read by the default-mode projection at
# contract assembly. For an auto-detect run the resolved ``actual_mode`` is
# scoped here around the run too, so a recommended mode never leaks into a
# later manual run in the same process.
WORK_MODE_ENV = "ORCHO_WORK_MODE"

# A confirm callback returns ``None`` to accept the recommendation as-is, or a
# concrete ``SemanticProfile`` to override it (confirm-override).
ConfirmFn = Callable[[AutoDetectDecision], SemanticProfile | None]


def _explicit_mode(explicit_mode: str | None) -> OperatingMode | None:
    """Coerce an operator ``--mode`` string to ``OperatingMode`` (or ``None``)."""
    if not explicit_mode:
        return None
    return OperatingMode(explicit_mode)


def _mode_for_accept(
    explicit_mode: str | None, recommended_mode: OperatingMode
) -> OperatingMode:
    """actual_mode for an accepted recommendation.

    Explicit ``--mode`` always wins; otherwise the detector's
    ``recommended_mode`` is honoured (this is the only branch that uses it).
    """
    forced = _explicit_mode(explicit_mode)
    return forced if forced is not None else recommended_mode


def _mode_for_fallback(
    explicit_mode: str | None, profile: SemanticProfile
) -> OperatingMode:
    """actual_mode for every non-accept branch.

    Explicit ``--mode`` wins; otherwise the work kind's default mode. The
    detector's ``recommended_mode`` is never used here.
    """
    forced = _explicit_mode(explicit_mode)
    return forced if forced is not None else default_operating_mode(profile)


def _error_reason(exc: BaseException) -> str:
    """Compact one-line reason for a detector exception."""
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _topology_echo(decision: AutoDetectDecision) -> dict:
    """Topology echo kwargs carried by every decision-backed resolution.

    The topology recommendation is informational: it never mutates
    ``actual_profile``, never starts a cross run, and never widens delivery.
    Each ``AutoDetectResolution`` branch that has a ``decision`` echoes these
    three fields verbatim; the detector-error branches (no decision) keep the
    inert defaults (``MONO`` / empty / ``""``). ``delivery_scope`` is *not*
    set here — it stays ``STRICT_MONO`` until an explicit operator directive
    (:func:`apply_topology_choice`) moves it.
    """
    return {
        "recommended_topology": decision.recommended_topology,
        "delivery_projects": decision.delivery_projects,
        "topology_reason": decision.topology_reason,
    }


class TopologyChoice(StrEnum):
    """Typed model of the operator's three explicit topology choices.

    Presented by the CLI (T3) when the detector recommends a cross topology.
    The choice is an *explicit* delivery directive — it never silently converts
    the current mono run into a cross run; it only selects the
    :class:`DeliveryScope` that delivery enforcement (T4) applies.

    - ``START_CROSS`` (choice 1) → :data:`DeliveryScope.CROSS`
    - ``EXPANDED_MONO`` (choice 2) → :data:`DeliveryScope.EXPANDED_MONO`
    - ``STRICT_MONO`` (choice 3) → :data:`DeliveryScope.STRICT_MONO`
    """

    START_CROSS = "start_cross"
    EXPANDED_MONO = "expanded_mono"
    STRICT_MONO = "strict_mono"

    @classmethod
    def from_number(cls, number: int) -> TopologyChoice:
        """Map a 1-based operator choice number to a ``TopologyChoice``.

        ``1`` → start cross, ``2`` → expanded mono, ``3`` → strict mono. Any
        other value raises ``ValueError`` (fail-fast, like the enums).
        """
        mapping = {1: cls.START_CROSS, 2: cls.EXPANDED_MONO, 3: cls.STRICT_MONO}
        if number not in mapping:
            raise ValueError(
                f"topology choice must be 1, 2, or 3, got {number!r}"
            )
        return mapping[number]


_TOPOLOGY_CHOICE_SCOPE: dict[TopologyChoice, DeliveryScope] = {
    TopologyChoice.START_CROSS: DeliveryScope.CROSS,
    TopologyChoice.EXPANDED_MONO: DeliveryScope.EXPANDED_MONO,
    TopologyChoice.STRICT_MONO: DeliveryScope.STRICT_MONO,
}


def apply_topology_choice(
    resolution: AutoDetectResolution, choice: TopologyChoice
) -> AutoDetectResolution:
    """Apply an explicit operator topology choice to a resolution.

    Returns a copy of ``resolution`` with ``delivery_scope`` set per ``choice``
    and **everything else preserved** — crucially ``actual_profile``,
    ``actual_mode``, and ``recommended_topology`` are untouched. Choosing
    ``START_CROSS`` records ``DeliveryScope.CROSS`` as an explicit delivery
    directive; it does not silently convert the current mono run into a cross
    run (T3 owns the actual cross-start UX). A bad choice raises ``ValueError``.
    """
    choice = choice if isinstance(choice, TopologyChoice) else TopologyChoice(
        choice
    )
    return replace(resolution, delivery_scope=_TOPOLOGY_CHOICE_SCOPE[choice])


def resolve_auto_detect(
    *,
    task: str,
    project: str,
    interactive: bool,
    explicit_mode: str | None = None,
    config: AutoDetectConfig | None = None,
    detector: WorkKindDetector | None = None,
    confirm: ConfirmFn | None = None,
) -> AutoDetectResolution:
    """Resolve the ``auto-detect`` selector into a typed ``AutoDetectResolution``.

    ``config`` defaults to :meth:`AutoDetectConfig.from_app_config`; ``detector``
    defaults to the lazy :class:`ProviderWorkKindDetector`; ``confirm`` defaults
    to :func:`default_confirm`. Tests inject all three. Returns a resolution
    for every branch — including ``FAILED`` (the CLI turns that into a
    deterministic non-zero exit; the run does not start).
    """
    if config is None:
        config = AutoDetectConfig.from_app_config()
    if detector is None:
        detector = ProviderWorkKindDetector()
    policy = config.policy
    use_confirm = interactive and policy == AutoDetectPolicy.CONFIRM

    # Detector error is a shared failure mode for both branches: apply
    # ``on_error``. A fallback must never be passed off as a recommendation,
    # so ``recommended_*`` stay unset (None) here.
    try:
        decision = detector.detect(task=task, project=project)
    except Exception as exc:  # noqa: BLE001 — any detector failure is on_error
        reason = _error_reason(exc)
        actual_profile = config.fallback_profile
        actual_mode = _mode_for_fallback(explicit_mode, actual_profile)
        if config.on_error == FallbackAction.FAIL:
            return AutoDetectResolution(
                detection_state=DetectionState.FAILED,
                actual_profile=actual_profile,
                actual_mode=actual_mode,
                policy=policy,
                fallback_used=False,
                error_reason=reason,
                fallback_reason="detector error and on_error=fail",
            )
        return AutoDetectResolution(
            detection_state=DetectionState.DETECTOR_ERROR_FALLBACK,
            actual_profile=actual_profile,
            actual_mode=actual_mode,
            policy=policy,
            fallback_used=True,
            error_reason=reason,
            fallback_reason="detector error",
        )

    # (a) Interactive confirm: accept as-is or operator override.
    if use_confirm:
        confirm_fn = confirm if confirm is not None else default_confirm
        override = confirm_fn(decision)
        if override is None:
            return _accepted_resolution(
                decision, policy, explicit_mode,
                confirmation_state="accepted",
            )
        override_profile = (
            override if isinstance(override, SemanticProfile)
            else SemanticProfile(override)
        )
        return AutoDetectResolution(
            detection_state=DetectionState.RECOMMENDED,
            actual_profile=override_profile,
            actual_mode=_mode_for_fallback(explicit_mode, override_profile),
            policy=policy,
            recommended_profile=decision.recommended_profile,
            recommended_mode=decision.recommended_mode,
            confidence=decision.confidence,
            rationale=decision.rationale,
            risk_flags=decision.risk_flags,
            fallback_used=False,
            confirmation_state="override",
            **_topology_echo(decision),
        )

    # (b) Trusted / non-interactive threshold gating — no prompt, no fail-fast
    # merely because there is no TTY.
    if decision.confidence >= config.confidence_threshold:
        return _accepted_resolution(
            decision, policy, explicit_mode, confirmation_state="auto",
        )

    actual_profile = config.fallback_profile
    actual_mode = _mode_for_fallback(explicit_mode, actual_profile)
    reason = (
        f"confidence {decision.confidence} < threshold "
        f"{config.confidence_threshold}"
    )
    if config.on_low_confidence == FallbackAction.FAIL:
        return AutoDetectResolution(
            detection_state=DetectionState.FAILED,
            actual_profile=actual_profile,
            actual_mode=actual_mode,
            policy=policy,
            recommended_profile=decision.recommended_profile,
            recommended_mode=decision.recommended_mode,
            confidence=decision.confidence,
            rationale=decision.rationale,
            risk_flags=decision.risk_flags,
            fallback_used=False,
            fallback_reason=f"{reason} and on_low_confidence=fail",
            **_topology_echo(decision),
        )
    return AutoDetectResolution(
        detection_state=DetectionState.LOW_CONFIDENCE_FALLBACK,
        actual_profile=actual_profile,
        actual_mode=actual_mode,
        policy=policy,
        recommended_profile=decision.recommended_profile,
        recommended_mode=decision.recommended_mode,
        confidence=decision.confidence,
        rationale=decision.rationale,
        risk_flags=decision.risk_flags,
        fallback_used=True,
        confirmation_state="auto",
        fallback_reason=reason,
        **_topology_echo(decision),
    )


def _accepted_resolution(
    decision: AutoDetectDecision,
    policy: AutoDetectPolicy,
    explicit_mode: str | None,
    *,
    confirmation_state: str,
) -> AutoDetectResolution:
    """Build a ``RECOMMENDED`` resolution that accepts ``decision`` as-is."""
    return AutoDetectResolution(
        detection_state=DetectionState.RECOMMENDED,
        actual_profile=decision.recommended_profile,
        actual_mode=_mode_for_accept(explicit_mode, decision.recommended_mode),
        policy=policy,
        recommended_profile=decision.recommended_profile,
        recommended_mode=decision.recommended_mode,
        confidence=decision.confidence,
        rationale=decision.rationale,
        risk_flags=decision.risk_flags,
        fallback_used=False,
        confirmation_state=confirmation_state,
        **_topology_echo(decision),
    )


# ── Serialization + scoped env channel ──────────────────────────────────────


def resolution_to_payload(resolution: AutoDetectResolution) -> dict:
    """JSON-safe dict view of a resolution (enums → values, tuple → list).

    Hidden reasoning is never serialized — only the typed, durable fields.
    """
    return {
        "detection_state": resolution.detection_state.value,
        "actual_profile": resolution.actual_profile.value,
        "actual_mode": resolution.actual_mode.value,
        "policy": resolution.policy.value,
        "recommended_profile": (
            resolution.recommended_profile.value
            if resolution.recommended_profile is not None else None
        ),
        "recommended_mode": (
            resolution.recommended_mode.value
            if resolution.recommended_mode is not None else None
        ),
        "confidence": resolution.confidence,
        "rationale": resolution.rationale,
        "risk_flags": (
            list(resolution.risk_flags)
            if resolution.risk_flags is not None else None
        ),
        "fallback_used": resolution.fallback_used,
        "confirmation_state": resolution.confirmation_state,
        "error_reason": resolution.error_reason,
        "fallback_reason": resolution.fallback_reason,
        "recommended_topology": resolution.recommended_topology.value,
        "delivery_projects": list(resolution.delivery_projects),
        "topology_reason": resolution.topology_reason,
        "delivery_scope": resolution.delivery_scope.value,
    }


def _restore_env(key: str, previous: str | None) -> None:
    """Restore ``key`` to ``previous`` (deleting it when ``previous`` is None)."""
    if previous is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = previous


@contextmanager
def scoped_autodetect_decision_env(
    resolution: AutoDetectResolution | None,
) -> Iterator[None]:
    """Scope the auto-detect env channels around a single ``run_pipeline`` call.

    For an auto-detect run (``resolution`` is not None) both
    :data:`AUTODETECT_DECISION_ENV` (the serialized resolution evidence) and
    :data:`WORK_MODE_ENV` (the resolved ``actual_mode``) are set for the
    duration of the run and restored to their previous values (or removed) in
    ``finally`` — so neither the recommended mode (F1) nor the decision evidence
    leaks into a later manual run in the same process.

    For a manual concrete profile (``resolution`` is None) the decision channel
    is *cleared* for the duration of the run and restored afterwards (F2): a
    stale ``ORCHO_AUTODETECT_DECISION`` left in the environment (externally, or
    restored from an earlier auto-detect run) must not make run_setup persist
    ``meta.auto_detect`` for a run that never used auto-detect. The work-mode
    channel is left untouched there — manual ``--mode`` owns it. Restoration
    happens even if the body raises / ``sys.exit``s.
    """
    previous_decision = os.environ.get(AUTODETECT_DECISION_ENV)
    if resolution is None:
        # Manual concrete profile: drop any stale decision env for this run.
        os.environ.pop(AUTODETECT_DECISION_ENV, None)
        try:
            yield
        finally:
            _restore_env(AUTODETECT_DECISION_ENV, previous_decision)
        return
    previous_mode = os.environ.get(WORK_MODE_ENV)
    os.environ[AUTODETECT_DECISION_ENV] = json.dumps(
        resolution_to_payload(resolution)
    )
    os.environ[WORK_MODE_ENV] = resolution.actual_mode.value
    try:
        yield
    finally:
        _restore_env(AUTODETECT_DECISION_ENV, previous_decision)
        _restore_env(WORK_MODE_ENV, previous_mode)


# ── Default interactive confirm prompt (injectable) ──────────────────────────


def default_confirm(decision: AutoDetectDecision) -> SemanticProfile | None:
    """Interactive confirm prompt for ``policy == CONFIRM`` on a TTY.

    Prints the recommended profile + mode and lets the operator accept it
    (empty line → ``None``) or type another semantic profile name to override
    (→ that :class:`SemanticProfile`). Unknown input re-prompts a small number
    of times, then accepts the recommendation. Never imported-time side
    effecting; only invoked on a real TTY confirm flow (tests inject a fake).
    """
    reco = paint(
        f"{decision.recommended_profile.value} · {decision.recommended_mode.value}",
        C.GREEN,
        C.BOLD,
    )
    conf = paint(f"(confidence {decision.confidence:.2f})", C.GREY)
    print(f"  auto-detect {paint('→', C.GREEN)} {reco} {conf}")
    if decision.rationale:
        print(f"     {paint(decision.rationale, C.GREY)}")
    valid = {p.value for p in SemanticProfile}
    prompt = (
        "  [Enter] accept, or type a work kind "
        f"({', '.join(sorted(valid))}): "
    )
    for _ in range(3):
        try:
            raw = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not raw:
            return None
        if raw in valid:
            return SemanticProfile(raw)
        print(f"  not a work kind: {raw!r}")
    return None


# ── Provider-backed detector (isolated, lazy) ────────────────────────────────


@dataclass
class ProviderWorkKindDetector:
    """Provider-backed work-kind detector with lazy runtime resolution.

    Constructing this is side-effect free: no runtime, no LLM, no network. The
    runtime is resolved only inside :meth:`detect`, on the first real
    invocation, via the agent registry. A malformed / unparseable model reply
    raises (the dispatcher treats any exception as a detector error and applies
    the configured ``on_error`` policy). Tests never construct or call this —
    they inject a deterministic fake.
    """

    model: str | None = None
    runtime: str = "claude"
    effort: str | None = None

    def detect(self, *, task: str, project: str) -> AutoDetectDecision:
        # Lazy imports keep CLI/help/test import paths free of provider work.
        from agents.registry import AgentRegistry
        from core.infra import config as _config

        model = self.model or _config.AppConfig.load().phase_model_map.get(
            "plan", "claude-opus-4-8"
        )
        agent = AgentRegistry.default().architect(
            model, self.runtime, effort=self.effort,
        )
        reply = agent.invoke(
            _build_detection_prompt(task=task),
            project,
            mutates_artifacts=False,
        )
        decision = _parse_detection_reply(reply)
        # The semantic profile + mode come from the model; the topology axis is
        # a *deterministic* provider-neutral heuristic over the task text — no
        # LLM. Merge it onto the decision without disturbing the model's pick.
        topology = recommend_topology(task)
        return replace(
            decision,
            recommended_topology=topology.topology,
            delivery_projects=topology.projects,
            topology_reason=topology.reason,
        )


def _build_detection_prompt(*, task: str) -> str:
    """Compose the read-only work-kind detection prompt.

    Asks for a single fenced JSON object with the recommendation. Kept terse
    and provider-neutral; provider specifics live in the runtime adapter.
    """
    kinds = ", ".join(p.value for p in SemanticProfile)
    modes = ", ".join(m.value for m in OperatingMode)
    return (
        "Classify the following software task into one work kind and one "
        "operating mode. Reply with a single fenced ```json object: "
        '{"recommended_profile": <one of: ' + kinds + ">, "
        '"recommended_mode": <one of: ' + modes + ">, "
        '"confidence": <0.0..1.0>, "rationale": <one short sentence>, '
        '"risk_flags": [<short tags>]}. Do not modify any files.\n\n'
        f"TASK:\n{task}\n"
    )


def _parse_detection_reply(reply: str) -> AutoDetectDecision:
    """Parse a model reply into an ``AutoDetectDecision``.

    Extracts the first JSON object (optionally fenced) and constructs the
    decision; ``AutoDetectDecision`` validates the enums and confidence. Any
    failure raises (→ detector-error in the dispatcher).
    """
    payload = _extract_json_object(reply)
    if not isinstance(payload, Mapping):
        raise ValueError("work-kind detection reply was not a JSON object")
    return AutoDetectDecision(
        recommended_profile=payload["recommended_profile"],
        recommended_mode=payload["recommended_mode"],
        confidence=payload["confidence"],
        rationale=str(payload.get("rationale", "")),
        risk_flags=tuple(payload.get("risk_flags", ()) or ()),
    )


def _extract_json_object(text: str) -> object:
    """Return the first JSON object found in ``text`` (fenced or bare)."""
    fence = "```json"
    if fence in text:
        rest = text.split(fence, 1)[1]
        body = rest.split("```", 1)[0]
        return json.loads(body)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object in work-kind detection reply")
    return json.loads(text[start : end + 1])


__all__ = [
    "AUTODETECT_DECISION_ENV",
    "AUTO_DETECT_PROFILE_TOKEN",
    "WORK_MODE_ENV",
    "ConfirmFn",
    "ProviderWorkKindDetector",
    "TopologyChoice",
    "apply_topology_choice",
    "default_confirm",
    "resolution_to_payload",
    "resolve_auto_detect",
    "scoped_autodetect_decision_env",
]
