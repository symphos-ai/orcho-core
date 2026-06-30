"""Presentation policy enum — neutral home shared by project + cross.

ADR 0047 D1 promoted :class:`PresentationPolicy` out of
:mod:`pipeline.project.types` once cross-project became a second real
caller. The "shared primitive only after two real callers" rule is
satisfied: ADR 0046 wired the project pipeline through it, and
ADR 0047 wires cross-project through it.

**Import discipline.** This module imports only from :mod:`enum`.
Any module under :mod:`pipeline.project.*` or
:mod:`pipeline.cross_project.*` may import it directly; no cycle
risk. :mod:`pipeline.project.types` re-exports the enum so the 7
existing ``from pipeline.project.types import PresentationPolicy``
importers continue working byte-identical — the **identity invariant**
``pipeline.project.types.PresentationPolicy is
pipeline.presentation.PresentationPolicy`` is pinned by Phase C tests.
"""

from __future__ import annotations

from enum import StrEnum


class PresentationPolicy(StrEnum):
    """Presentation policy for the typed pipeline app boundaries.

    Two values shipped (ADR 0046 + 0047):

    * ``TERMINAL`` (default) — banners / success / warn / print calls
      reachable from ``run_project_pipeline`` (and after ADR 0047
      Phase E, ``run_cross_project_pipeline``) fire to stdout/stderr.
      CLI + SDK + integration tests + every existing wide-kwarg
      back-compat call gets it by default → byte-identical to legacy
      transcript.

    * ``SILENT`` — zero stdout/stderr. Side effects still happen
      (files written, session mutated, events emitted, checkpoint
      closed, mirror done, worktree torn down). Library callers
      (cross-project per-alias children, future direct-library UI,
      MCP if it ever stops subprocess-spawning) consume this to
      drive the pipeline structurally without terminal pollution.

    Hard invariant (enforced at each request's ``__post_init__``):
    ``SILENT`` implies ``no_interactive=True``. Interactive prompts
    are terminal-by-definition.
    """

    TERMINAL = "terminal"
    SILENT = "silent"


__all__ = ["PresentationPolicy"]
