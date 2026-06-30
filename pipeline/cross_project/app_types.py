"""Typed request / result types for the cross-project application boundary.

ADR 0047 Phase C established these. Parallel shape to
:class:`pipeline.project.types.ProjectRunRequest` /
:class:`~pipeline.project.types.ProjectRunResult`:

* :class:`CrossRunRequest` — headless DTO mirroring the 23-param
  signature of :func:`pipeline.cross_project.orchestrator.run_cross_pipeline`
  modulo ``_REQUEST_ONLY_FIELDS`` (currently ``{"presentation"}``).
* :class:`CrossRunResult` — return type for the typed boundary
  :func:`pipeline.cross_project.app.run_cross_project_pipeline`
  (lands in Phase D). Carries the persisted ``session`` dict plus
  the **actual** ``output_dir`` and ``run_id`` from run bootstrap
  (not request passthroughs).

**Separate file rationale.** ``pipeline.cross_project.types`` is the
home of M13 Phase 1 domain types (``ProjectStep``, ``CrossPlanStep``,
``ContractValidation``, ``ProjectRunRef``, ``CrossProjectProfile``,
status / when / blocked enums) — validation-heavy contract shapes.
The cross app boundary types here are run-shape DTOs (closer to the
project-side ``pipeline.project.types``). Splitting keeps each module
single-concept; ADR 0047 Phase C plan allowed the escape hatch.

**Import discipline (ADR 0047).** This module imports only from
:mod:`pipeline.presentation`, :mod:`pipeline.cross_project.constants`
(for ``CROSS_DEFAULT_PROFILE``), ``agents.protocols`` /
``agents.registry`` / ``agents.runtimes``, and ``core.infra.config``
— the same surface ``pipeline.project.types`` uses for project-side
defaults. It must NOT import :mod:`pipeline.cross_project.orchestrator`
(prevents the ``orchestrator → app → app_types → orchestrator`` cycle
Phase D would otherwise hit), the cross run function, or any
rendering / CLI module.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from agents.protocols import SessionMode  # noqa: F401  # imported for shape consistency
from agents.registry import PhaseAgentConfig
from agents.runtimes import AgentProvider
from core.infra import config
from pipeline.cross_project.constants import CROSS_DEFAULT_PROFILE
from pipeline.presentation import PresentationPolicy


@dataclass(frozen=True, slots=True)
class CrossRunRequest:
    """Headless request DTO for the cross-project pipeline.

    Mirrors the current ``run_cross_pipeline`` signature **modulo
    ``_REQUEST_ONLY_FIELDS``** (see
    ``tests/unit/pipeline/cross_project/test_cross_run_request.py``).
    Today ``_REQUEST_ONLY_FIELDS = {"presentation"}`` — the
    :class:`pipeline.presentation.PresentationPolicy` knob lives on
    the typed request only because the wide-kwarg
    ``run_cross_pipeline`` back-compat surface stays frozen by the
    Phase C signature lock and must not grow.

    Notes on defaults:

    * ``model`` is set via ``config.phase_model("implement",
      "claude-opus-4-8[1m]")`` at module load time. ``run_cross_pipeline``
      uses the same expression at its function definition time. As
      long as both modules import in the same process the strings
      match — locked by the Phase C signature test.
    * ``profile_name`` defaults to
      :data:`pipeline.cross_project.constants.CROSS_DEFAULT_PROFILE`
      (``"feature"``). Orchestrator + app_types both import this
      constant from the neutral module to keep the future
      ``orchestrator → app → app_types`` graph acyclic.
    * ``presentation`` defaults to ``PresentationPolicy.TERMINAL``.
      CLI + SDK + integration tests + the existing
      ``run_cross_pipeline(...)`` back-compat surface get TERMINAL by
      default → legacy transcript byte-identical.
    """

    # ── 23 fields mirroring run_cross_pipeline(...) — ORDER MATCHES ──
    task: str
    projects: dict[str, Path]
    max_rounds: int = 1
    model: str = config.phase_model("implement", "claude-opus-4-8[1m]")
    output_dir: Path | None = None
    dry_run: bool = False
    mock: bool = False
    provider: AgentProvider | None = None
    phase_config: PhaseAgentConfig | None = None
    cross_mode: str = "full"
    plan_file: str | None = None
    resume_from: str | None = None
    hypothesis_enabled: bool | None = None
    profile_name: str = CROSS_DEFAULT_PROFILE
    operator_decisions: tuple | None = None
    no_interactive: bool = False
    resumed_meta: dict | None = None
    resume_mode: str | None = None
    followup_parent_run_id: str | None = None
    followup_parent_run_dir: str | None = None
    followup_parent_status: str | None = None
    followup_base_task: str | None = None
    followup_session_seeds_per_alias: dict[str, dict[str, str]] | None = None

    # ── Request-only field (NOT on run_cross_pipeline's signature) ──
    presentation: PresentationPolicy = PresentationPolicy.TERMINAL

    def __post_init__(self) -> None:
        """Coerce + reject. Mirrors ProjectRunRequest semantics:

        1. **String coercion.** ``from_kwargs(presentation="silent")``
           normalises to the enum via ``object.__setattr__`` (the
           dataclass is frozen). Invalid strings raise ``ValueError``.
        2. **SILENT implies no_interactive=True.** Interactive prompts
           are terminal-by-definition; the hard invariant rejects the
           mismatched combination.
        """
        if not isinstance(self.presentation, PresentationPolicy):
            try:
                normalised = PresentationPolicy(self.presentation)
            except ValueError as exc:
                raise ValueError(
                    f"invalid presentation policy: {self.presentation!r} "
                    f"(expected one of {[p.value for p in PresentationPolicy]})"
                ) from exc
            object.__setattr__(self, "presentation", normalised)
        if (
            self.presentation is PresentationPolicy.SILENT
            and not self.no_interactive
        ):
            raise ValueError(
                "PresentationPolicy.SILENT requires no_interactive=True "
                "(interactive prompts are terminal-by-definition)"
            )

    @classmethod
    def from_kwargs(cls, **kwargs: Any) -> CrossRunRequest:
        """Build a request from the kwargs ``run_cross_pipeline`` accepts.

        Integration helper for the future ``run_cross_pipeline``
        back-compat wrapper (ADR 0047 Phase D) and for external callers
        that have kwargs in hand. Validates that every kwarg maps to a
        declared field; unknown kwargs raise ``TypeError`` so a renamed
        parameter cannot silently drop on the floor.

        Accepts ``presentation`` even though that field is not on
        ``run_cross_pipeline``'s signature — request-only fields are
        permitted at construction; the parity test
        (``_REQUEST_ONLY_FIELDS``) is what locks the *signature*
        contract between the wrapper and the request.
        """
        declared = {f.name for f in fields(cls)}
        unknown = set(kwargs) - declared
        if unknown:
            raise TypeError(
                "CrossRunRequest.from_kwargs got unexpected keyword "
                f"argument(s): {sorted(unknown)}. The declared field "
                "set is locked against run_cross_pipeline's signature "
                "modulo _REQUEST_ONLY_FIELDS by the Phase C test."
            )
        return cls(**kwargs)


@dataclass(frozen=True, slots=True)
class CrossRunResult:
    """Outcome of :func:`pipeline.cross_project.app.run_cross_project_pipeline`
    (Phase D).

    ``session`` is the persisted session dict — same shape callers of
    the legacy ``run_cross_pipeline`` already consume. ``output_dir``
    and ``run_id`` are the **actual** locals from run bootstrap (not
    request passthroughs); they may differ from
    ``request.output_dir`` when the bootstrap synthesises a
    timestamped sub-directory or when ``$ORCHO_RUN_ID`` overrides the
    minted run id.
    """

    session: dict
    output_dir: Path | None
    run_id: str | None


__all__ = ["CrossRunRequest", "CrossRunResult"]
