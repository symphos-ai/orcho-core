"""Unit tests for the cross-pipeline runtime-chip mapping helper.

The Pipeline block renders a ``[Claude]`` / ``[Codex]`` chip per phase
keyed by ``step.phase``. Cross profiles route dispatch through
``step.cross.handler`` while intentionally preserving the semantic
phase name (see :mod:`pipeline.cross_project.profile_projection`),
so the chip map must be derived from ``step.cross.handler`` but keyed
by ``step.phase`` — including for custom aliases.
"""
from __future__ import annotations

from types import SimpleNamespace

from pipeline.cross_project.agent_setup import _global_phase_runtimes
from pipeline.runtime.profile import LoopStep
from pipeline.runtime.steps import CrossScope, CrossStepPolicy, PhaseStep


def _agent(rt: str) -> SimpleNamespace:
    return SimpleNamespace(runtime=rt)


def _step(phase: str, handler: str | None) -> PhaseStep:
    return PhaseStep(
        phase=phase,
        cross=CrossStepPolicy(
            scope=CrossScope.GLOBAL,
            handler=handler,
        ) if handler else None,
    )


def test_maps_semantic_phase_name_to_handler_runtime():
    steps = (
        _step("plan", "cross_plan"),
        _step("validate_plan", "cross_validate_plan"),
    )
    out = _global_phase_runtimes(
        steps,
        claude_plan=_agent("claude"),
        codex=_agent("codex"),
    )
    assert out == {"plan": "claude", "validate_plan": "codex"}


def test_custom_semantic_alias_still_resolves_via_handler():
    # A profile aliasing ``"replan"`` onto the ``cross_plan`` handler
    # — the renderer must still get a chip for the alias.
    steps = (_step("replan", "cross_plan"),)
    out = _global_phase_runtimes(
        steps,
        claude_plan=_agent("claude"),
        codex=_agent("codex"),
    )
    assert out == {"replan": "claude"}


def test_step_without_cross_annotation_is_skipped():
    # A bare PhaseStep (no ``cross`` field) is not dispatched through
    # ``claude_plan`` / ``codex``, so we omit it rather than guess.
    steps = (_step("freeform", None),)
    out = _global_phase_runtimes(
        steps,
        claude_plan=_agent("claude"),
        codex=_agent("codex"),
    )
    assert out == {}


def test_unknown_handler_is_skipped():
    steps = (_step("plan", "some_future_handler"),)
    out = _global_phase_runtimes(
        steps,
        claude_plan=_agent("claude"),
        codex=_agent("codex"),
    )
    assert out == {}


def test_loop_step_inner_phases_are_collected():
    # The cross runner can wrap ``plan`` / ``validate_plan`` in a
    # LoopStep; the chip map must reach inside the loop the same way
    # the renderer does.
    steps = (
        LoopStep(
            steps=(
                _step("plan", "cross_plan"),
                _step("validate_plan", "cross_validate_plan"),
            ),
            until="validate_plan.approved",
            max_rounds=2,
        ),
    )
    out = _global_phase_runtimes(
        steps,
        claude_plan=_agent("claude"),
        codex=_agent("codex"),
    )
    assert out == {"plan": "claude", "validate_plan": "codex"}


def test_runtime_falls_back_when_agent_missing_attribute():
    # Defensive: if an agent instance somehow lacks ``.runtime``
    # (e.g. a hand-rolled stub in a downstream test), the helper still
    # produces a sensible label using the documented default.
    steps = (_step("plan", "cross_plan"),)
    out = _global_phase_runtimes(
        steps,
        claude_plan=SimpleNamespace(),  # no runtime attr
        codex=_agent("codex"),
    )
    assert out == {"plan": "claude"}
