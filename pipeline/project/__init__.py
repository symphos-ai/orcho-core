"""Project-local application boundary for the single-project pipeline.

ADR 0042 lays out the package contents and the layering rules. ADR
0046 Phase B added :class:`PresentationPolicy` at package root so
consumers can write ``from pipeline.project import PresentationPolicy``
without reaching into ``pipeline.project.types``. Other typed surfaces
(``run_project_pipeline``, ``ProjectRunRequest``, ``ProjectRunResult``)
stay accessed via their concrete modules — the import-graph rationale
in ADR 0042 still applies; only the small new enum is re-exported
here to keep the policy surface ergonomic.
"""

from pipeline.project.types import PresentationPolicy

__all__ = ["PresentationPolicy"]
