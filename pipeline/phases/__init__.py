"""
pipeline.phases — phase execution surface.

Phase 4 of the runtime/subdomain refactor merged the former
``pipeline/phases.py`` (adapter callables) and
``pipeline/builtin_phases.py`` (built-in handlers) into this package:

    pipeline/phases/
      adapters.py   — adapter callables (``run_plan`` / ``run_build``
                      / ``run_review`` / ``run_fix``) consumed by
                      the legacy non-profile orchestrator path.
      builtin.py    — built-in phase handler implementations and
                      ``register_builtin_phases`` /
                      ``default_registry`` factories.

The long-standing ``from pipeline import phases`` and
``from pipeline.phases import ...`` import paths keep working — this
module re-exports the adapter + registry surface. The former
``from pipeline.builtin_phases import ...`` path no longer exists;
call sites use ``from pipeline.phases.builtin import ...``.
"""

from __future__ import annotations

from pipeline.phases.adapters import (
    PhaseResult,
    run_build,
    run_fix,
    run_plan,
    run_review,
)
from pipeline.phases.builtin import (
    default_registry,
    register_builtin_phases,
)

__all__ = [
    "PhaseResult",
    "default_registry",
    "register_builtin_phases",
    "run_build",
    "run_fix",
    "run_plan",
    "run_review",
]
