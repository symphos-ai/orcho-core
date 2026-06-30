"""Project-local constants.

Module-level constants that used to live inside
``pipeline.project_orchestrator`` but have no other natural home and
need to be importable from ``pipeline.project.types`` (and any future
sibling) without dragging the whole orchestrator into the import
graph. Keeping this module stdlib-only is load-bearing: ``types.py``
imports from here, and a transitive import back into
``project_orchestrator`` would defeat the layering rule.
"""

from __future__ import annotations

#: Default profile name when none is supplied via CLI / SDK request.
#: Single source of truth — ``project_orchestrator.py`` re-imports it,
#: every other consumer should follow the same path. ``feature`` is the
#: default semantic work kind; it reuses the former ``advanced`` recipe
#: (subtask_dag delivery + both terminal cross gates strict), so the
#: fresh-run default behaviour is unchanged.
DEFAULT_PROFILE_NAME: str = "feature"
