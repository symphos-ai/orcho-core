# SPDX-License-Identifier: Apache-2.0
"""Phase-registry construction for built-in and extension handlers.

Registers local built-ins first, then discovers extension handlers via the
``orcho.phases`` entry-point group and overlays them into a
:class:`~pipeline.runtime.PhaseRegistry`. Entry-point loading is deferred to
call time (never at module import), so importing this submodule never triggers
``ep.load()`` and therefore never re-enters the ``pipeline.phases.builtin``
package mid-initialisation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pipeline.runtime import PhaseRegistry

if TYPE_CHECKING:
    from pipeline.runtime import PipelineState

_LOCAL_BUILTIN_PHASES = {
    "plan": "pipeline.phases.builtin.handlers.plan:_phase_plan",
    "validate_plan": "pipeline.phases.builtin.handlers.validate_plan:_phase_validate_plan",
    "implement": "pipeline.phases.builtin.handlers.implement:_phase_implement",
    "review_changes": "pipeline.phases.builtin.handlers.review_changes:_phase_review_changes",
    "repair_changes": "pipeline.phases.builtin.handlers.repair_changes:_phase_repair_changes",
    "final_acceptance": (
        "pipeline.phases.builtin.handlers.final_acceptance:_phase_final_acceptance"
    ),
    "compliance_check": (
        "pipeline.phases.builtin.handlers.compliance_check:_phase_compliance_check"
    ),
    "correction_triage": (
        "pipeline.phases.builtin.handlers.correction_triage:_phase_correction_triage"
    ),
}


def _require_agent(state: PipelineState, attr: str) -> Any:
    """Pull a phase-specific agent off ``state.phase_config`` or fail loudly.

    Failing here surfaces orchestrator wiring bugs immediately rather than
    silently substituting a wrong agent.
    """
    pc = state.phase_config
    if pc is None:
        raise RuntimeError(
            f"PipelineState.phase_config is None; cannot resolve {attr!r}. "
            "The orchestrator must populate phase_config before run_profile()."
        )
    agent = getattr(pc, attr, None)
    if agent is None:
        raise RuntimeError(
            f"phase_config.{attr} is not set. Available attrs: "
            f"{[a for a in dir(pc) if a.endswith('_agent')]}"
        )
    return agent


def register_builtin_phases(registry: PhaseRegistry) -> PhaseRegistry:
    """Register built-in phase handlers plus ``orcho.phases`` extensions.

    Built-ins are registered from the local source tree first so editable
    development cannot lose a newly added phase when installed metadata is
    stale. Entry points are loaded afterwards; any third-party package may
    declare entries under the same group and they get picked up automatically
    — no core edits, no manual registration calls.

    Re-registration is allowed: a third-party entry with name ``"plan"``
    overwrites the built-in. This is the supported override mechanism for
    plugin authors who need to swap a built-in handler for one with
    richer behaviour.

    Failures loading any single entry (broken third-party plugin) surface
    as warnings — one bad entry must not break discovery for the rest.
    """
    from importlib import import_module

    for name, target in _LOCAL_BUILTIN_PHASES.items():
        module_name, attr_name = target.split(":", 1)
        handler = getattr(import_module(module_name), attr_name)
        registry.register(name, handler)

    from importlib.metadata import entry_points
    for ep in entry_points(group="orcho.phases"):
        try:
            handler = ep.load()
        except Exception as exc:
            print(f"  ! orcho.phases: failed to load '{ep.name}': {exc}")
            continue
        registry.register(ep.name, handler)
    if not registry.names():
        raise RuntimeError(
            "register_builtin_phases(): no phase handlers registered. "
            "Check pipeline.phases.builtin.handlers imports."
        )
    return registry


def default_registry() -> PhaseRegistry:
    """Convenience: empty registry pre-populated via ``orcho.phases``
    entry_points."""
    return register_builtin_phases(PhaseRegistry())
