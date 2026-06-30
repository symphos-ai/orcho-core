"""Cross-project local constants.

Module-level constants that used to live inside
:mod:`pipeline.cross_project.orchestrator` but have no other natural
home and need to be importable from
:mod:`pipeline.cross_project.app_types` (and any future sibling)
without dragging the whole orchestrator into the import graph.
Keeping this module stdlib-only is load-bearing: ``app_types.py``
imports :data:`CROSS_DEFAULT_PROFILE` from here for the
``CrossRunRequest.profile_name`` default, and ADR 0047 Phase D wires
``orchestrator → app → app_types``; a transitive import back into
``orchestrator`` (the prior shape) would cycle.

Mirrors :mod:`pipeline.project.constants`.
"""

from __future__ import annotations

#: Default profile name for cross-project runs when none is supplied
#: via CLI / SDK request. Single source of truth — ``orchestrator.py``
#: re-imports it, ``app_types.py`` imports it for the request default,
#: and every other consumer should follow the same path. ``feature`` is
#: the default semantic work kind; it reuses the former ``advanced``
#: recipe and keeps both terminal cross gates strict (contract_check +
#: cross_final_acceptance, run=always / on_skip=block), so the cross
#: fresh-run default behaviour is unchanged.
CROSS_DEFAULT_PROFILE: str = "feature"
