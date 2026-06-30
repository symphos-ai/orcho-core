"""
pipeline/runtime/results.py — handler dispatch surface.

``PhaseHandler`` declares the callable signature every phase handler
implements. ``PhaseRegistry`` is the name → handler mapping the
runner dispatches through.

The richer ``StepOutcome`` typed lifecycle result lives in
``pipeline.lifecycle`` today (it depends on the FSM and is consumed by
``PhaseLifecycle.execute_step``). When ``StepOutcome`` migrates into
the runtime subdomain in a future refactor, it lands here alongside
the dispatch types.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pipeline.runtime.state import PipelineState


@runtime_checkable
class PhaseHandler(Protocol):
    """A callable that consumes a state and returns a (possibly new) state.

    Returning None is allowed; the runner treats it as "I mutated in place".
    """
    def __call__(self, state: PipelineState) -> PipelineState | None: ...


class PhaseRegistry:
    """Name → handler mapping with simple validation.

    Registered names are case-sensitive and should be lowercase tokens
    matching what appears in profile JSON (``"plan"``, ``"validate_plan"``,
    ``"execute_dag"``, ...). Re-registration overwrites silently — that
    is intentional: plugins may want to swap out a built-in handler
    (e.g. a custom compliance_check that calls an internal SaaS).
    """

    def __init__(self) -> None:
        self._handlers: dict[str, PhaseHandler] = {}

    def register(self, name: str, handler: PhaseHandler) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("phase name must be a non-empty string")
        self._handlers[name.strip()] = handler

    def get(self, name: str) -> PhaseHandler:
        if name not in self._handlers:
            raise KeyError(
                f"Unknown phase {name!r}. Registered phases: {sorted(self._handlers)}"
            )
        return self._handlers[name]

    def has(self, name: str) -> bool:
        return name in self._handlers

    def names(self) -> list[str]:
        return sorted(self._handlers)


__all__ = ["PhaseHandler", "PhaseRegistry"]
