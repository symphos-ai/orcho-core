"""pipeline/profiles/loader.py — JSON parser for the redesigned ``Profile``
schema (Phase 5).

Phase 1 introduced the typed ``Profile`` / ``PhaseStep`` / ``LoopStep``
dataclasses with construction-time invariants. This module gives them
a JSON authoring surface so customer plugins can ship profiles without
writing Python.

Schema (top-level): ``{<profile_name>: ProfileObject}`` where
``ProfileObject`` is::

    {
      "kind":        "full_cycle" | "scoped" | "custom",
      "variant":     "lite" | "advanced" | "enterprise" |
                     "plan"  | "review"   | "task"      | <custom> | null,
      "description": "...",
      "change_handoff": "uncommitted" | "commit" | "commit_set", // optional
      "steps":       [<step>, ...]
    }

A ``<step>`` is either a ``PhaseStep`` object or a ``LoopStep`` wrapper::

    PhaseStep:
      {
        "phase":         "<registered phase name>",
        "execution":     "linear" | <plugin-mode-name>,           // optional, default "linear"
        "skill":         "<registered skill name>",               // optional
        "effort":        "low" | "medium" | "high",               // optional
        "overrides":     {...arbitrary...},                       // optional
        "prompt":        {"role": "...", "task": "...",           // optional
                          "format": "..." | null},
        "hypothesis":    {"attempts": <int>, "format": "..."},      // optional, attempts=0 disables prelude
        "quality_gates": [<gate>, ...],                           // optional
        "human_review":  {...},                                   // optional, Phase 8
      }

    LoopStep:
      {"loop": {
         "steps":                [<PhaseStep>, ...],
         "until":                "<phase>.<field>" | "not <phase>.<field>",
         "max_rounds":           <int>,                           // optional, default 1
         "round_extras_key":     "<extras key>",                  // optional, default "loop_round"
         "oscillation_halt_after": <int> | null,                  // optional, default 2
       }}

Validation reuses ``Profile.__post_init__`` invariants (Phase 1) — a
malformed profile raises ``ValueError`` at load time, not deep inside
the runtime walker.

Phase 5d: ``load_profiles_v2`` now feeds the active ``run_pipeline``
path. Remaining dispatcher gaps are deliberately explicit: top-level
``PhaseStep.execution`` and per-step ``quality_gates`` are parsed but
not yet consumed by dedicated lifecycle stages.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from pipeline.prompts.spec import PromptSpec
from pipeline.runtime import (
    ChangeHandoffMode,
    ContractCheckMode,
    CrossGatePolicy,
    CrossGateRunPolicy,
    CrossGateSkipPolicy,
    CrossScope,
    CrossStepPolicy,
    EffortLevel,
    ExecutionPolicy,
    ExecutionSurface,
    FailStrategy,
    GateKind,
    HumanAction,
    HumanReview,
    HypothesisPrelude,
    LoopStep,
    OperatingMode,
    PhaseHandoffPolicy,
    PhaseHandoffType,
    PhaseStep,
    Profile,
    ProfileKind,
    QualityGate,
    ReviewTiming,
    SemanticProfile,
)
from pipeline.runtime.profile import _VALID_RECIPE_KINDS

__all__ = [
    "ProfileLoadError",
    "parse_profile",
    "parse_profiles",
    "load_profiles_v2",
    "load_profiles_v2_with_plugins",
]


class ProfileLoadError(ValueError):
    """Raised when a Profile JSON object fails to parse / validate.

    Inherits from ValueError so callers that catch the broader category
    (per Phase 1 invariants policy) keep working.
    """


# ── Helpers ──────────────────────────────────────────────────────────────────

_PROFILE_KEYS = frozenset({
    "kind", "variant", "description", "internal", "steps", "change_handoff",
    "implementation_execution", "cross_gates", "worktree_isolation",
    "sandbox", "semantic_profile", "default_mode", "recipe_kind",
})
_CROSS_GATE_KEYS = frozenset({"enabled", "run", "on_skip", "mode"})
_PHASE_STEP_KEYS = frozenset({
    "phase", "execution", "skill", "effort", "overrides",
    "prompt", "hypothesis", "quality_gates", "human_review",
    "handoff", "cross",
})
_PHASE_HANDOFF_KEYS = frozenset({"type", "repair_attempts", "on_exhausted"})
_CROSS_POLICY_KEYS = frozenset({"scope", "handler"})
_PROMPT_SPEC_KEYS = frozenset({"role", "task", "format"})
_HYPOTHESIS_KEYS = frozenset({"attempts", "format"})
_QUALITY_GATE_KEYS = frozenset({
    "name", "kind", "on_fail", "feed_target", "config",
})
_HUMAN_REVIEW_KEYS = frozenset({
    "timing", "actions", "prompt", "retry_budget",
})
_LOOP_WRAPPER_KEYS = frozenset({"loop"})
_LOOP_BODY_KEYS = frozenset({
    "steps", "until", "max_rounds", "round_extras_key",
    "oscillation_halt_after",
})
# ADR 0027 / M11: execution-policy object-form keys. ``read_only``,
# ``join``, ``surfaces`` are reserved by ADR 0027 for the future
# fanout-review milestone but accepted by the parser so authors can
# declare them ahead of execution; M11 enforces "must be empty /
# null" downstream so reserved shape doesn't run by accident.
_EXECUTION_POLICY_KEYS = frozenset({
    "mode", "session_split", "session_continuity", "read_only", "join",
    "surfaces",
})
_EXECUTION_SURFACE_KEYS = frozenset({"id", "prompt", "model", "effort"})


def _reject_unknown_keys(obj: dict, allowed: frozenset[str], ctx: str) -> None:
    """Fail fast on schema typos instead of silently dropping config."""
    non_string = [k for k in obj if not isinstance(k, str)]
    if non_string:
        raise ProfileLoadError(f"{ctx}: keys must be strings, got {non_string!r}")
    unknown = sorted(k for k in obj if k not in allowed)
    if unknown:
        raise ProfileLoadError(
            f"{ctx}: unknown keys {unknown}; allowed: {sorted(allowed)}"
        )


def _require_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileLoadError(f"{label}: expected non-empty string, got {value!r}")
    return value.strip()


def _enum_or_raise(value: Any, enum_cls: type, label: str):
    """Coerce a JSON string into a StrEnum value or raise."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProfileLoadError(f"{label}: expected string, got {type(value).__name__}")
    try:
        return enum_cls(value)
    except ValueError:
        valid = sorted(m.value for m in enum_cls)
        raise ProfileLoadError(
            f"{label}: {value!r} is not one of {valid}"
        ) from None


def _parse_prompt_spec(obj: dict, ctx: str) -> PromptSpec:
    if not isinstance(obj, dict):
        raise ProfileLoadError(f"{ctx}: prompt must be an object or null")
    _reject_unknown_keys(obj, _PROMPT_SPEC_KEYS, ctx)
    task = _require_str(obj.get("task"), f"{ctx}.task")
    raw_role = obj.get("role")
    role = _require_str(raw_role, f"{ctx}.role")
    raw_format = obj.get("format")
    fmt = (
        _require_str(raw_format, f"{ctx}.format")
        if raw_format is not None else None
    )
    try:
        return PromptSpec(task=task, role=role, format=fmt)
    except (TypeError, ValueError) as e:
        raise ProfileLoadError(f"{ctx}: {e}") from e


def _parse_hypothesis(obj: Any, ctx: str) -> HypothesisPrelude | None:
    if obj is None:
        return None
    if not isinstance(obj, dict):
        raise ProfileLoadError(f"{ctx}: hypothesis must be an object or null")
    _reject_unknown_keys(obj, _HYPOTHESIS_KEYS, ctx)
    if "attempts" not in obj:
        raise ProfileLoadError(f"{ctx}.attempts: required")
    attempts = obj["attempts"]
    if (
        isinstance(attempts, bool)
        or not isinstance(attempts, int)
        or attempts < 0
    ):
        raise ProfileLoadError(
            f"{ctx}.attempts: must be a non-negative int, "
            f"got {type(attempts).__name__}"
        )
    fmt = obj.get("format")
    if fmt is not None and (not isinstance(fmt, str) or not fmt.strip()):
        raise ProfileLoadError(f"{ctx}.format: must be a non-empty string or null")
    try:
        return HypothesisPrelude(attempts=attempts, format=fmt)
    except (TypeError, ValueError) as e:
        raise ProfileLoadError(f"{ctx}: {e}") from e


def _parse_quality_gate(obj: dict, ctx: str) -> QualityGate:
    if not isinstance(obj, dict):
        raise ProfileLoadError(f"{ctx}: gate entry must be an object")
    _reject_unknown_keys(obj, _QUALITY_GATE_KEYS, ctx)
    name = _require_str(obj.get("name"), f"{ctx}.name")
    kind = _enum_or_raise(obj.get("kind", "computational"), GateKind, f"{ctx}.kind")
    on_fail = _enum_or_raise(obj.get("on_fail"), FailStrategy, f"{ctx}.on_fail")
    if on_fail is None:
        raise ProfileLoadError(f"{ctx}.on_fail: required")
    feed_target = obj.get("feed_target")
    if feed_target is not None and not isinstance(feed_target, str):
        raise ProfileLoadError(f"{ctx}.feed_target: must be string or null")
    config = obj.get("config")
    if config is not None and not isinstance(config, dict):
        raise ProfileLoadError(f"{ctx}.config: must be object or null")
    # QualityGate.__post_init__ enforces FEED_INTO_NEXT → feed_target.
    try:
        return QualityGate(
            name=name,
            kind=kind,
            on_fail=on_fail,
            feed_target=feed_target,
            config=config,
        )
    except (TypeError, ValueError) as e:
        raise ProfileLoadError(f"{ctx}: {e}") from e


def _parse_human_review(obj: dict, ctx: str) -> HumanReview:
    if not isinstance(obj, dict):
        raise ProfileLoadError(f"{ctx}: human_review must be an object")
    _reject_unknown_keys(obj, _HUMAN_REVIEW_KEYS, ctx)
    timing = _enum_or_raise(
        obj.get("timing", "after"), ReviewTiming, f"{ctx}.timing",
    )
    actions_raw = obj.get("actions")
    if actions_raw is None:
        actions = None  # let HumanReview default kick in
    else:
        if not isinstance(actions_raw, list) or not all(
            isinstance(a, str) for a in actions_raw
        ):
            raise ProfileLoadError(f"{ctx}.actions: must be list of strings")
        actions = tuple(
            _enum_or_raise(a, HumanAction, f"{ctx}.actions[{i}]")
            for i, a in enumerate(actions_raw)
        )
    prompt = obj.get("prompt")
    if prompt is not None and not isinstance(prompt, str):
        raise ProfileLoadError(f"{ctx}.prompt: must be string or null")
    retry_budget = obj.get("retry_budget", 5)
    if not isinstance(retry_budget, int):
        raise ProfileLoadError(f"{ctx}.retry_budget: must be int")
    kwargs: dict[str, Any] = {
        "timing":       timing,
        "prompt":       prompt,
        "retry_budget": retry_budget,
    }
    if actions is not None:
        kwargs["actions"] = actions
    try:
        return HumanReview(**kwargs)
    except (TypeError, ValueError) as e:
        raise ProfileLoadError(f"{ctx}: {e}") from e


def _parse_phase_handoff(obj: Any, ctx: str) -> PhaseHandoffPolicy:
    """Parse a ``PhaseStep.handoff`` policy.

    Loader invariants:

    * Only ``type`` is part of the profile schema. Actions are not
      configurable here; runtime forms ``available_actions`` from the
      verdict and exposes them through the active handoff payload, which
      is what the decision API validates against.
    * Unknown ``type`` values are rejected at load time.
    * Phase-name validation lives in the runtime/orchestrator support
      matrix, not here — the loader stays generic so executor support
      can widen without re-touching the parser. Unsupported policies on
      a given phase are rejected by the executor at run time.
    """
    if not isinstance(obj, dict):
        raise ProfileLoadError(f"{ctx}: handoff must be an object")
    _reject_unknown_keys(obj, _PHASE_HANDOFF_KEYS, ctx)
    type_value = _enum_or_raise(
        obj.get("type", "human_bypass"), PhaseHandoffType, f"{ctx}.type",
    )
    repair_attempts = obj.get("repair_attempts", 0)
    on_exhausted = obj.get("on_exhausted", "halt")
    try:
        return PhaseHandoffPolicy(
            type=type_value,
            repair_attempts=repair_attempts,
            on_exhausted=on_exhausted,
        )
    except (TypeError, ValueError) as e:
        raise ProfileLoadError(f"{ctx}: {e}") from e


def _parse_cross_policy(obj: dict, ctx: str) -> CrossStepPolicy:
    if not isinstance(obj, dict):
        raise ProfileLoadError(f"{ctx}: cross must be an object")
    _reject_unknown_keys(obj, _CROSS_POLICY_KEYS, ctx)
    if "scope" not in obj:
        raise ProfileLoadError(f"{ctx}.scope: required")
    scope = _enum_or_raise(obj["scope"], CrossScope, f"{ctx}.scope")
    handler = obj.get("handler")
    if handler is not None and not isinstance(handler, str):
        raise ProfileLoadError(f"{ctx}.handler: must be string or null")
    try:
        return CrossStepPolicy(scope=scope, handler=handler)
    except (TypeError, ValueError) as e:
        raise ProfileLoadError(f"{ctx}: {e}") from e


def _parse_execution_surface(obj: Any, ctx: str) -> ExecutionSurface:
    """Parse one ``ExecutionSurface`` entry (ADR 0027).

    Surfaces are accepted at the JSON layer so authors can declare
    reserved profile shape, but the M11 ``ExecutionPolicy``
    post-init rejects any non-empty surface list until the
    fanout_review runtime lands. This function only handles parsing.
    """
    if not isinstance(obj, dict):
        raise ProfileLoadError(f"{ctx}: execution surface must be an object")
    _reject_unknown_keys(obj, _EXECUTION_SURFACE_KEYS, ctx)
    surface_id = _require_str(obj.get("id"), f"{ctx}.id")
    raw_prompt = obj.get("prompt")
    if raw_prompt is None:
        raise ProfileLoadError(f"{ctx}.prompt: required")
    prompt = _parse_prompt_spec(raw_prompt, f"{ctx}.prompt")
    model = obj.get("model")
    if model is not None and not isinstance(model, str):
        raise ProfileLoadError(f"{ctx}.model: must be string or null")
    effort = _enum_or_raise(obj.get("effort"), EffortLevel, f"{ctx}.effort")
    try:
        return ExecutionSurface(
            id=surface_id, prompt=prompt, model=model, effort=effort,
        )
    except (TypeError, ValueError) as e:
        raise ProfileLoadError(f"{ctx}: {e}") from e


def _parse_execution_policy(raw: Any, ctx: str) -> tuple[str, ExecutionPolicy]:
    """Normalize the JSON ``execution`` field into ``(mode, policy)``.

    ADR 0027 / M11 backward compatibility: ``"execution": "linear"``
    and ``"execution": {"mode": "linear"}`` are equivalent. The
    string form synthesises ``ExecutionPolicy(mode="linear")``; the
    object form parses the policy explicitly.

    ``ExecutionPolicy.__post_init__`` enforces the M11 deferrals
    (mode='fanout_review' and non-empty surfaces are rejected
    until the later milestone) and the ``session_split`` value
    domain.

    Object-form keys (all optional)::

        {
          "mode":               "linear" | <plugin-mode>,   // default "linear"
          "session_split":      "stateless" | "per_phase" |
                                "per_role" | "common" | null,
          "session_continuity": "fresh_only" | "loop_continue" |
                                "same_zone_continue" | null,
          // read_only / join / surfaces — reserved (ADR 0027)
        }

    ``session_split`` and ``session_continuity`` are ORTHOGONAL axes
    (ADR 0113): ``session_split`` controls how a session is shared
    *between phases*; ``session_continuity`` controls whether an
    invocation resumes *its own* prior session on a repeat call / loop
    round. They are read and validated independently. ``null`` /
    omitted ``session_continuity`` means "no per-step preference"; the
    T3 resolver supplies the role default.
    """
    if isinstance(raw, str):
        mode = _require_str(raw, f"{ctx}.execution")
        try:
            return mode, ExecutionPolicy(mode=mode)
        except (TypeError, ValueError) as e:
            raise ProfileLoadError(f"{ctx}.execution: {e}") from e
    if not isinstance(raw, dict):
        raise ProfileLoadError(
            f"{ctx}.execution: must be a string or object, "
            f"got {type(raw).__name__}"
        )
    _reject_unknown_keys(raw, _EXECUTION_POLICY_KEYS, f"{ctx}.execution")
    mode_raw = raw.get("mode", "linear")
    mode = _require_str(mode_raw, f"{ctx}.execution.mode")
    session_split = raw.get("session_split")
    if session_split is not None and not isinstance(session_split, str):
        raise ProfileLoadError(
            f"{ctx}.execution.session_split: must be string or null"
        )
    session_continuity = raw.get("session_continuity")
    if session_continuity is not None and not isinstance(session_continuity, str):
        raise ProfileLoadError(
            f"{ctx}.execution.session_continuity: must be string or null"
        )
    read_only = raw.get("read_only")
    if read_only is not None and not isinstance(read_only, bool):
        raise ProfileLoadError(
            f"{ctx}.execution.read_only: must be bool or null"
        )
    join = raw.get("join")
    if join is not None and not isinstance(join, str):
        raise ProfileLoadError(
            f"{ctx}.execution.join: must be string or null"
        )
    raw_surfaces = raw.get("surfaces", [])
    if not isinstance(raw_surfaces, list):
        raise ProfileLoadError(
            f"{ctx}.execution.surfaces: must be a list"
        )
    surfaces = tuple(
        _parse_execution_surface(s, f"{ctx}.execution.surfaces[{i}]")
        for i, s in enumerate(raw_surfaces)
    )
    try:
        policy = ExecutionPolicy(
            mode=mode,
            session_split=session_split,
            session_continuity=session_continuity,
            read_only=read_only,
            join=join,
            surfaces=surfaces,
        )
    except (TypeError, ValueError) as e:
        raise ProfileLoadError(f"{ctx}.execution: {e}") from e
    return mode, policy


def _parse_phase_step(obj: dict, ctx: str) -> PhaseStep:
    if not isinstance(obj, dict):
        raise ProfileLoadError(f"{ctx}: PhaseStep entry must be an object")
    _reject_unknown_keys(obj, _PHASE_STEP_KEYS, ctx)
    if "phase" not in obj:
        raise ProfileLoadError(f"{ctx}.phase: required")
    phase = _require_str(obj["phase"], f"{ctx}.phase")
    execution, execution_policy = _parse_execution_policy(
        obj.get("execution", "linear"), ctx,
    )
    skill = obj.get("skill")
    if skill is not None and not isinstance(skill, str):
        raise ProfileLoadError(f"{ctx}.skill: must be string or null")
    effort = _enum_or_raise(obj.get("effort"), EffortLevel, f"{ctx}.effort")
    overrides = obj.get("overrides")
    if overrides is not None and not isinstance(overrides, dict):
        raise ProfileLoadError(f"{ctx}.overrides: must be object or null")
    raw_prompt = obj.get("prompt")
    prompt = (
        _parse_prompt_spec(raw_prompt, f"{ctx}.prompt")
        if raw_prompt is not None else None
    )
    hypothesis = _parse_hypothesis(obj.get("hypothesis"), f"{ctx}.hypothesis")
    raw_gates = obj.get("quality_gates", [])
    if not isinstance(raw_gates, list):
        raise ProfileLoadError(f"{ctx}.quality_gates: must be list")
    gates = tuple(
        _parse_quality_gate(g, f"{ctx}.quality_gates[{i}]")
        for i, g in enumerate(raw_gates)
    )
    raw_review = obj.get("human_review")
    review = (
        _parse_human_review(raw_review, f"{ctx}.human_review")
        if raw_review is not None else None
    )
    raw_handoff = obj.get("handoff")
    handoff = (
        _parse_phase_handoff(raw_handoff, f"{ctx}.handoff")
        if raw_handoff is not None else None
    )
    if handoff is not None and review is not None:
        raise ProfileLoadError(
            f"{ctx}: handoff and human_review are mutually exclusive — "
            "both declare a human-control surface on the same step"
        )
    raw_cross = obj.get("cross")
    cross = (
        _parse_cross_policy(raw_cross, f"{ctx}.cross")
        if raw_cross is not None else None
    )
    try:
        return PhaseStep(
            phase=phase,
            execution=execution,
            skill=skill,
            effort=effort,
            overrides=overrides,
            prompt=prompt,
            hypothesis=hypothesis,
            quality_gates=gates,
            human_review=review,
            handoff=handoff,
            cross=cross,
            execution_policy=execution_policy,
        )
    except (TypeError, ValueError) as e:
        raise ProfileLoadError(f"{ctx}: {e}") from e


def _parse_loop_step(obj: dict, ctx: str) -> LoopStep:
    """A LoopStep entry is wrapped in ``{"loop": {...}}`` to disambiguate
    from PhaseStep at the JSON layer (no shared keys → unambiguous, but
    the wrapper is explicit + future-proof for nested loop support)."""
    if not isinstance(obj, dict) or "loop" not in obj:
        raise ProfileLoadError(f"{ctx}: loop entry must be {{\"loop\": {{...}}}}")
    _reject_unknown_keys(obj, _LOOP_WRAPPER_KEYS, ctx)
    body = obj["loop"]
    if not isinstance(body, dict):
        raise ProfileLoadError(f"{ctx}.loop: body must be an object")
    _reject_unknown_keys(body, _LOOP_BODY_KEYS, f"{ctx}.loop")

    raw_steps = body.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ProfileLoadError(f"{ctx}.loop.steps: required non-empty list")
    steps = tuple(
        _parse_phase_step(s, f"{ctx}.loop.steps[{i}]")
        for i, s in enumerate(raw_steps)
    )

    until = _require_str(body.get("until"), f"{ctx}.loop.until")
    max_rounds = body.get("max_rounds", 1)
    if not isinstance(max_rounds, int):
        raise ProfileLoadError(f"{ctx}.loop.max_rounds: must be int")
    round_key = body.get("round_extras_key", "loop_round")
    if not isinstance(round_key, str):
        raise ProfileLoadError(f"{ctx}.loop.round_extras_key: must be string")
    osc = body.get("oscillation_halt_after", 2)
    if osc is not None and not isinstance(osc, int):
        raise ProfileLoadError(f"{ctx}.loop.oscillation_halt_after: must be int or null")

    try:
        return LoopStep(
            steps=steps,
            until=until,
            max_rounds=max_rounds,
            round_extras_key=round_key,
            oscillation_halt_after=osc,
        )
    except (TypeError, ValueError) as e:
        raise ProfileLoadError(f"{ctx}.loop: {e}") from e


def _parse_step(obj: Any, ctx: str):
    """Discriminate PhaseStep vs LoopStep on the ``loop`` key."""
    if isinstance(obj, dict) and "loop" in obj:
        return _parse_loop_step(obj, ctx)
    if isinstance(obj, dict):
        return _parse_phase_step(obj, ctx)
    raise ProfileLoadError(
        f"{ctx}: step must be an object (PhaseStep or {{\"loop\": ...}}); "
        f"got {type(obj).__name__}"
    )


def _parse_one_cross_gate(
    gate_name: str, obj: Any, ctx: str,
) -> CrossGatePolicy:
    """Parse one ``cross_gates`` entry. ``gate_name`` is the known gate
    key (``contract_check`` / ``cross_final_acceptance``); the caller is
    responsible for rejecting unknown keys before calling.
    """
    if not isinstance(obj, dict):
        raise ProfileLoadError(
            f"{ctx}: cross_gates[{gate_name!r}] must be an object"
        )
    _reject_unknown_keys(obj, _CROSS_GATE_KEYS, f"{ctx}.{gate_name}")

    enabled = obj.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ProfileLoadError(
            f"{ctx}.{gate_name}.enabled: must be bool, "
            f"got {type(enabled).__name__}"
        )

    run_raw = obj.get("run")
    if run_raw is None:
        run = CrossGateRunPolicy.AUTO
    else:
        run = _enum_or_raise(
            run_raw, CrossGateRunPolicy, f"{ctx}.{gate_name}.run",
        )

    on_skip_raw = obj.get("on_skip")
    if on_skip_raw is None:
        on_skip = CrossGateSkipPolicy.BLOCK
    else:
        on_skip = _enum_or_raise(
            on_skip_raw,
            CrossGateSkipPolicy,
            f"{ctx}.{gate_name}.on_skip",
        )

    mode_raw = obj.get("mode")
    if mode_raw is None:
        mode = None
    elif gate_name == "contract_check":
        # Coerce + validate against ContractCheckMode so repo_review and
        # other future-deprecated names fail at load time.
        mode_enum = _enum_or_raise(
            mode_raw, ContractCheckMode, f"{ctx}.{gate_name}.mode",
        )
        mode = mode_enum.value
    else:
        raise ProfileLoadError(
            f"{ctx}.{gate_name}.mode: not applicable to gate "
            f"{gate_name!r}; remove the field"
        )

    if (
        gate_name == "cross_final_acceptance"
        and run is CrossGateRunPolicy.MANUAL_CONFIRM
    ):
        raise ProfileLoadError(
            f"{ctx}.{gate_name}.run: 'manual_confirm' is not yet "
            f"supported for cross_final_acceptance; supported values "
            f"are 'always', 'auto', 'never'"
        )

    try:
        return CrossGatePolicy(
            enabled=enabled,
            run=run,
            on_skip=on_skip,
            mode=mode,
        )
    except (TypeError, ValueError) as e:
        raise ProfileLoadError(f"{ctx}.{gate_name}: {e}") from e


def _parse_cross_gates(obj: Any, ctx: str) -> dict[str, CrossGatePolicy]:
    """Parse the top-level ``cross_gates`` block.

    Only known gate names are accepted. Unknown keys (typos like
    ``contract_chek``) fail at load time so authors find the mistake
    instantly.
    """
    # Imported here to avoid a circular import at module load time:
    # profile_projection imports from pipeline.runtime, and loader is
    # itself a runtime sibling consumer.
    from pipeline.cross_project.profile_projection import KNOWN_CROSS_GATES

    if not isinstance(obj, dict):
        raise ProfileLoadError(
            f"{ctx}.cross_gates: must be an object, got "
            f"{type(obj).__name__}"
        )
    unknown = sorted(set(obj) - KNOWN_CROSS_GATES)
    if unknown:
        raise ProfileLoadError(
            f"{ctx}.cross_gates: unknown gate keys {unknown}; "
            f"known: {sorted(KNOWN_CROSS_GATES)}"
        )
    out: dict[str, CrossGatePolicy] = {}
    for gate_name, gate_obj in obj.items():
        out[gate_name] = _parse_one_cross_gate(gate_name, gate_obj, ctx)
    return out


# ── Public API ───────────────────────────────────────────────────────────────

def parse_profile(name: str, obj: dict) -> Profile:
    """Parse one named profile dict into a ``Profile``.

    Raises ``ProfileLoadError`` on malformed input. ``Profile.__post_init__``
    enforces kind+variant invariants on top of this parser's structural
    validation.
    """
    if not isinstance(obj, dict):
        raise ProfileLoadError(f"profile {name!r}: expected object, got {type(obj).__name__}")
    _reject_unknown_keys(obj, _PROFILE_KEYS, name)

    kind = _enum_or_raise(obj.get("kind", "custom"), ProfileKind, f"{name}.kind")
    if kind is None:
        kind = ProfileKind.CUSTOM
    variant = obj.get("variant")
    if variant is not None and not isinstance(variant, str):
        raise ProfileLoadError(f"{name}.variant: must be string or null")

    description = obj.get("description", "")
    if not isinstance(description, str):
        raise ProfileLoadError(f"{name}.description: must be string")
    internal = obj.get("internal", False)
    if not isinstance(internal, bool):
        raise ProfileLoadError(
            f"{name}.internal: must be a bool, got {type(internal).__name__}"
        )
    change_handoff = _enum_or_raise(
        obj.get("change_handoff"), ChangeHandoffMode, f"{name}.change_handoff",
    )
    from pipeline.runtime import ImplementationExecution
    implementation_execution = _enum_or_raise(
        obj.get("implementation_execution"),
        ImplementationExecution,
        f"{name}.implementation_execution",
    )

    raw_steps = obj.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ProfileLoadError(f"{name}.steps: required non-empty list")
    steps = tuple(
        _parse_step(s, f"{name}.steps[{i}]") for i, s in enumerate(raw_steps)
    )

    raw_cross_gates = obj.get("cross_gates")
    if raw_cross_gates is None:
        cross_gates: dict = {}
    else:
        cross_gates = _parse_cross_gates(raw_cross_gates, name)

    raw_worktree_isolation = obj.get("worktree_isolation")
    if raw_worktree_isolation is not None and not isinstance(raw_worktree_isolation, str):
        raise ProfileLoadError(
            f"{name}.worktree_isolation: must be a string or null, "
            f"got {type(raw_worktree_isolation).__name__}"
        )
    worktree_isolation: str | None = raw_worktree_isolation

    raw_sandbox = obj.get("sandbox")
    if raw_sandbox is not None and not isinstance(raw_sandbox, dict):
        raise ProfileLoadError(
            f"{name}.sandbox: must be an object or null, "
            f"got {type(raw_sandbox).__name__}"
        )
    # Validate structure via the sandbox resolver — surfaces unknown
    # keys, bad enums, malformed regex at profile-load time rather
    # than at run init. ADR 0034: schema validation gated up-front.
    if raw_sandbox is not None:
        from pipeline.sandbox.resolver import (
            SandboxConfigError,
            _parse_section,
        )
        try:
            _parse_section(raw_sandbox, f"{name}.sandbox")
        except SandboxConfigError as e:
            raise ProfileLoadError(str(e)) from e
    sandbox: dict | None = raw_sandbox

    # Stage C semantic identity fields. These are the explicit source of a
    # built-in profile's semantic identity (``variant`` is not). All three
    # are optional so plugin/custom profiles parse unchanged.
    semantic_profile = _enum_or_raise(
        obj.get("semantic_profile"), SemanticProfile, f"{name}.semantic_profile",
    )
    default_mode = _enum_or_raise(
        obj.get("default_mode"), OperatingMode, f"{name}.default_mode",
    )
    raw_recipe_kind = obj.get("recipe_kind")
    if raw_recipe_kind is not None:
        if not isinstance(raw_recipe_kind, str):
            raise ProfileLoadError(
                f"{name}.recipe_kind: must be a string or null, "
                f"got {type(raw_recipe_kind).__name__}"
            )
        if raw_recipe_kind not in _VALID_RECIPE_KINDS:
            raise ProfileLoadError(
                f"{name}.recipe_kind: {raw_recipe_kind!r} is not one of "
                f"{sorted(_VALID_RECIPE_KINDS)}"
            )
    recipe_kind: str | None = raw_recipe_kind

    try:
        return Profile(
            name=name,
            kind=kind,
            variant=variant,
            description=description,
            internal=internal,
            steps=steps,
            change_handoff=change_handoff,
            implementation_execution=implementation_execution,
            cross_gates=cross_gates,
            worktree_isolation=worktree_isolation,
            sandbox=sandbox,
            semantic_profile=semantic_profile,
            default_mode=default_mode,
            recipe_kind=recipe_kind,
        )
    except (TypeError, ValueError) as e:
        raise ProfileLoadError(f"{name}: {e}") from e


def parse_profiles(raw: dict) -> dict[str, Profile]:
    """Parse a top-level ``{name: ProfileObject}`` dict.

    Underscore-prefixed keys (``_comment``) are silently ignored —
    the JSON file may carry comment-shaped fields the schema doesn't
    define.
    """
    if not isinstance(raw, dict):
        raise ProfileLoadError("top-level: expected object")
    out: dict[str, Profile] = {}
    for name, obj in raw.items():
        if not isinstance(name, str):
            continue
        if name.startswith("_"):
            continue  # comment-shaped key
        out[name] = parse_profile(name, obj)
    return out


def _find_phase_steps_in_raw(
    raw_profile: dict, phase_name: str,
) -> list[dict]:
    """Return every raw step dict in ``raw_profile`` whose ``phase``
    equals ``phase_name``.

    Walks the top-level ``steps`` array and descends into ``loop.steps``
    so phases nested in loops are reachable. Returns the actual step
    dicts (by reference) so callers can mutate them in place — that's
    how the local-config overlay deep-merges patches into the JSON
    tree before ``parse_profile`` parses it.

    The list lets callers detect ambiguous overlays: in built-in
    profiles every phase name appears at most once anywhere in the
    tree, but a custom JSON could repeat a phase. Two matches → loader
    raises so the operator disambiguates instead of guessing.
    """
    matches: list[dict] = []
    steps = raw_profile.get("steps", []) or []
    for entry in steps:
        if not isinstance(entry, dict):
            continue
        loop_block = entry.get("loop")
        if isinstance(loop_block, dict):
            inner_steps = loop_block.get("steps") or []
            for inner in inner_steps:
                if (
                    isinstance(inner, dict)
                    and inner.get("phase") == phase_name
                ):
                    matches.append(inner)
            continue
        if entry.get("phase") == phase_name:
            matches.append(entry)
    return matches


def _deep_merge_into(dst: dict, src: dict) -> None:
    """Recursive dict merge: ``src`` values win at leaf collisions;
    nested dicts merge field-by-field. Lists / scalars are replaced
    wholesale — overlay authors who want to *append* to a list (e.g.
    quality_gates) have to restate the full list."""
    for key, value in src.items():
        if (
            isinstance(value, dict)
            and isinstance(dst.get(key), dict)
        ):
            _deep_merge_into(dst[key], value)
        else:
            dst[key] = value


def _apply_profile_overlays(
    raw: dict, overlays: dict[str, dict[str, dict]],
) -> None:
    """Patch the raw JSON profile dict in place with operator overlays.

    Overlay shape (consumed from ``profiles_v2`` in any JSON config layer,
    including workspace ``config.json`` and ``config.local.json``)::

        {"<profile_name>": {"<phase_name>": <patch_dict>, ...}, ...}

    Phase keys deep-merge ``<patch_dict>`` into the matching phase step's dict
    inside the built-in profile. The reserved ``"_profile"`` key deep-merges
    into the top-level profile object itself. Examples of legitimate patches:
    switching ``handoff.type``, tweaking ``effort``, changing
    ``execution.mode``, or setting ``_profile.worktree_isolation``.

    Errors (raised before ``parse_profiles`` so they surface as
    ``ProfileLoadError`` at startup, not deep in the runtime):

    * profile named in overlay does not exist in the built-in JSON;
    * phase named in overlay does not appear in that profile's tree;
    * phase name appears more than once (ambiguous patch target).
    """
    if not overlays:
        return
    for profile_name, phase_patches in overlays.items():
        raw_profile = raw.get(profile_name)
        if not isinstance(raw_profile, dict):
            raise ProfileLoadError(
                f"profiles_v2 overlay: profile {profile_name!r} is not "
                "defined in the built-in JSON; cannot apply overlay. "
                "Check ``config.local.json`` for a typo or remove the "
                "stale entry."
            )
        profile_patch = phase_patches.get("_profile")
        if isinstance(profile_patch, dict):
            _deep_merge_into(raw_profile, profile_patch)
        for phase_name, patch in phase_patches.items():
            if phase_name == "_profile":
                continue
            matches = _find_phase_steps_in_raw(raw_profile, phase_name)
            if not matches:
                raise ProfileLoadError(
                    f"profiles_v2 overlay: profile {profile_name!r} "
                    f"has no PhaseStep with phase={phase_name!r}; cannot "
                    "apply overlay. Either add the phase to the profile "
                    "or remove the overlay entry."
                )
            if len(matches) > 1:
                raise ProfileLoadError(
                    f"profiles_v2 overlay: profile {profile_name!r} has "
                    f"{len(matches)} PhaseSteps with phase={phase_name!r}; "
                    "this overlay format only patches by phase name and "
                    "needs a single match. Either rename one occurrence "
                    "or stop patching this phase from local config."
                )
            _deep_merge_into(matches[0], patch)


def load_profiles_v2(path: Path) -> dict[str, Profile]:
    """Read a profile JSON file and return ``{name: Profile}``.

    Phase 5d: this is the only JSON profile loader used by the active
    pipeline dispatch path. The legacy v1 loader was removed in Phase
    5d-5.

    Phase 7c: customer plugins shipping custom profiles via the
    ``orcho.profiles`` entry_points group are merged in via
    ``load_profiles_v2_with_plugins`` (separate function). This bare
    loader stays JSON-only for tests and tools that need to read the
    shipped registry without plugin discovery.

    Operator overlays: before parsing, the raw JSON dict is patched
    with any ``profiles_v2`` block declared in layered local config
    (``core/_config/config.local.json`` < ``~/.orcho/config.local.json``
    < ``$ORCHO_WORKSPACE/.orcho/config.json``
    < ``$ORCHO_WORKSPACE/.orcho/config.local.json``). The overlay
    format is flat-by-phase-name — see
    :func:`core.infra.config.load_profile_overlays` for the shape and
    :func:`_apply_profile_overlays` for merge semantics. Patching
    happens in place on the raw dict so that ``parse_profile`` sees
    a fully-resolved profile and runs the same invariants on operator
    overrides that it does on built-in JSON.
    """
    if not path.exists():
        raise FileNotFoundError(f"profile file not found: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    # Lazy import — ``core.infra.config`` reads environment + filesystem
    # at module import, and pulling it in at the top of this loader
    # would force every plain ``import pipeline.profiles.loader`` to
    # also load the layered config stack. The overlay path is opt-in
    # via ``config.local.json``; for the common no-overlay case the
    # call returns ``{}`` cheaply.
    from core.infra.config import load_profile_overlays
    overlays = load_profile_overlays()
    if overlays:
        _apply_profile_overlays(raw, overlays)
    return parse_profiles(raw)


def load_profiles_v2_with_plugins(path: Path) -> dict[str, Profile]:
    """Phase 7c: ``load_profiles_v2`` + plugin discovery.

    Reads the JSON profile registry at ``path`` first, then loads any
    plugin-shipped profiles registered via the ``orcho.profiles``
    entry_points group. Plugin entries with names matching shipped
    profiles win — supported plugin-override mechanism (e.g. shipping
    an ``advanced_with_compliance`` profile, or overriding ``"task"``
    to add a project-specific review step).

    Entry contract: each entry resolves to a ``Profile`` instance,
    a zero-arg callable returning a ``Profile``, or any object that
    duck-types as a ``Profile`` (passes ``isinstance(obj, Profile)``).
    Non-Profile entries are skipped with a diagnostic — wrong shape
    means the plugin author misconfigured pyproject.toml.

    The entry point name is the CLI / registry key. If it differs from
    ``Profile.name`` the loader warns and normalizes the frozen Profile
    instance with ``dataclasses.replace``. This keeps plugin override
    semantics deterministic: an entry named ``task`` overrides the
    shipped ``task`` profile even if the object was authored with a
    stale internal name.

    Failure isolation: one broken plugin's load failure does not
    block discovery for the rest. Production callers
    (``_resolve_v2_profile``) use this function; bare
    ``load_profiles_v2`` is kept for tests that pin only the shipped
    JSON registry.
    """
    profiles = load_profiles_v2(path)

    from pipeline.entry_points import discover_entry_points
    plugin_entries = discover_entry_points("orcho.profiles")
    for name, obj in plugin_entries.items():
        if not isinstance(obj, Profile):
            print(
                f"  ! orcho.profiles/{name!r}: expected Profile instance "
                f"or zero-arg callable returning Profile, got "
                f"{type(obj).__name__} — skipping"
            )
            continue
        if obj.name != name:
            # Allow but warn — entry point name vs. ``Profile.name`` can drift
            # if plugin author renames one without the other. Entry point name
            # is the public CLI key, so normalize the frozen Profile to match.
            print(
                f"  ! orcho.profiles entry {name!r} ships Profile with "
                f"name={obj.name!r}; using entry point name as registry key"
            )
            obj = replace(obj, name=name)
        profiles[name] = obj
    return profiles
