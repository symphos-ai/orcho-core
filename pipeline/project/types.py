"""Typed request / result types for the project-pipeline application boundary.

ADR 0042 Phase B established the dataclass. ADR 0046 Phase B added
:class:`PresentationPolicy` (TERMINAL / SILENT) and the
:attr:`ProjectRunRequest.presentation` field — request-only, lives
exclusively on the typed surface so the legacy
``pipeline.project.app.run_pipeline`` 28-kwarg wrapper preserves its
frozen ADR 0042 Phase J signature.

The fields of :class:`ProjectRunRequest` **mirror the current
signature of** ``pipeline.project.app.run_pipeline`` modulo
``_REQUEST_ONLY_FIELDS`` (currently ``{"presentation",
"render_phase_outputs"}``) — see
``tests/unit/pipeline/test_project_run_request.py`` for the contract
test. The dataclass is NOT generated from ``inspect.signature`` at
runtime; ``inspect.signature`` is the verification tool, not a
code-generation seam.

**Import discipline (ADR 0042).** This module is allowed to import from
``pipeline.project.constants`` and from common stdlib / package roots
(``agents.protocols``, ``agents.registry``, ``agents.runtimes``,
``core.infra.config``) — but it must NOT import from
``pipeline.project_orchestrator``. The orchestrator currently imports
``run_pipeline``'s defaults at module load (e.g. ``config.phase_model(
"implement", "claude-opus-4-8[1m]")``); the dataclass below preserves
that legacy import-time evaluation by calling the same function at
this module's import time so the default value matches.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from agents.protocols import SessionMode
from agents.registry import PhaseAgentConfig
from agents.runtimes import AgentProvider
from core.infra import config
from pipeline.presentation import PresentationPolicy
from pipeline.project.constants import DEFAULT_PROFILE_NAME

# ADR 0047 Phase C — ``PresentationPolicy`` promoted to a neutral
# ``pipeline.presentation`` module (project + cross are both real
# callers now). The enum is re-exported here for back-compat: the 7
# existing ``from pipeline.project.types import PresentationPolicy``
# importers (ADR 0046 sites) keep resolving the SAME enum object.
# Identity is the invariant — pinned by the Phase C tests.
__all_presentation__ = ["PresentationPolicy"]


@dataclass(frozen=True, slots=True)
class ProjectRunRequest:
    """Headless request DTO for the project pipeline.

    Mirrors the current ``run_pipeline`` signature **modulo
    ``_REQUEST_ONLY_FIELDS``** (see
    ``tests/unit/pipeline/test_project_run_request.py``). Today
    ``_REQUEST_ONLY_FIELDS = {"presentation", "render_phase_outputs"}``
    — these presentation knobs live on the typed request only because
    the wide-kwarg ``run_pipeline`` back-compat surface is frozen by the
    ADR 0042 Phase J signature lock and must not grow.

    Notes on defaults:

    * ``model`` is set via ``config.phase_model("implement",
      "claude-opus-4-8[1m]")`` at module load time. ``run_pipeline``
      uses the same expression at its function definition time. As
      long as both modules import in the same process, the strings
      match — locked by the Phase B signature test.
    * ``session_mode`` defaults to ``SessionMode.AUTO`` — the same
      enum member ``run_pipeline`` uses.
    * ``attachments`` is the legacy positional ``tuple`` default;
      embedders should pass a tuple of attachment specs, not a list.
    * ``presentation`` defaults to ``PresentationPolicy.TERMINAL``.
      Setting it to ``SILENT`` requires ``no_interactive=True``;
      enforced by ``__post_init__`` (see ADR 0046 § Hard invariant).
    """

    # ── intent ──
    task: str
    project_dir: str
    max_rounds: int = 1
    model: str = config.phase_model("implement", "claude-opus-4-8[1m]")
    output_dir: Path | None = None
    dry_run: bool = False
    phase_config: PhaseAgentConfig | None = None
    session_mode: SessionMode = SessionMode.AUTO
    profile_name: str = DEFAULT_PROFILE_NAME
    ma_artifacts_dir_override: str | None = None
    provider: AgentProvider | None = None
    resume_from: str | None = None
    attachments: tuple = ()
    parent_run_id: str | None = None
    project_alias: str | None = None
    hypothesis_enabled: bool | None = None
    # ``profile_obj`` is a Profile but typed as ``Any`` to avoid the
    # heavy ``pipeline.runtime`` import at this layer (matches
    # run_pipeline's forward-quoted annotation).
    profile_obj: Any | None = None
    plan_source: str = "local"
    handoff_path: str | None = None
    resume_mode: str | None = None
    followup_parent_run_id: str | None = None
    followup_parent_run_dir: str | None = None
    followup_parent_status: str | None = None
    followup_base_task: str | None = None
    followup_session_seeds: dict[str, str] | None = None
    # Display-only lineage for a CHECKPOINT resume of an existing
    # follow-up child (header rendering only — not meta-recording).
    followup_child_status: str | None = None
    followup_active_handoff_id: str | None = None
    no_interactive: bool = False
    from_run_plan_parent_dir: Path | None = None
    worktree_config_override: dict[str, Any] | None = None
    # ── request-only (NOT on the run_pipeline back-compat surface) ──
    presentation: PresentationPolicy = PresentationPolicy.TERMINAL
    render_phase_outputs: bool = False
    auto_waiver_allowed: bool = False

    def __post_init__(self) -> None:
        """Coerce + validate the presentation field (ADR 0046).

        Two jobs:

        1. **Coerce** string → enum. ``from_kwargs(presentation="silent")``
           callers store the raw string; without coercion the runtime
           check ``self.presentation is PresentationPolicy.SILENT`` is
           a silent ``False`` and the SILENT path never fires. The
           dataclass is frozen, so the coercion uses
           ``object.__setattr__``.
        2. **Reject** ``SILENT`` + ``no_interactive=False``. Interactive
           prompts are terminal-by-definition; a SILENT run cannot
           legitimately prompt the operator. Embedders must be explicit
           — no silent widening of ``no_interactive``.
        """
        if not isinstance(self.presentation, PresentationPolicy):
            try:
                normalised = PresentationPolicy(self.presentation)
            except ValueError as exc:
                raise ValueError(
                    f"invalid presentation policy: {self.presentation!r} "
                    f"(expected one of "
                    f"{[p.value for p in PresentationPolicy]})"
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
    def from_kwargs(cls, **kwargs: Any) -> ProjectRunRequest:
        """Build a request from the kwargs ``run_pipeline`` accepts.

        Integration helper used by the ``run_pipeline`` back-compat
        wrapper (ADR 0042 Phase I) and by any external caller that has
        kwargs in hand. Validates that every kwarg maps to a declared
        field; unknown kwargs raise ``TypeError`` so a renamed
        parameter cannot silently drop on the floor.

        Accepts ``presentation`` even though that field is not on
        ``run_pipeline``'s signature — request-only fields are
        permitted at construction; the parity test
        (``_REQUEST_ONLY_FIELDS``) is what locks the *signature*
        contract between the wrapper and the request.
        """
        declared = {f.name for f in fields(cls)}
        unknown = set(kwargs) - declared
        if unknown:
            raise TypeError(
                "ProjectRunRequest.from_kwargs got unexpected keyword "
                f"argument(s): {sorted(unknown)}. The declared field "
                "set is locked against run_pipeline's signature modulo "
                "_REQUEST_ONLY_FIELDS by the Phase B test."
            )
        return cls(**kwargs)


@dataclass(frozen=True, slots=True)
class ProjectRunResult:
    """Outcome of :func:`run_project_pipeline` (Phase I).

    Phase B keeps the shape minimal — just enough that Phase I has a
    return type to construct. The wider lifecycle surface (event
    stream, structured artifact paths) is deferred to whichever later
    phase introduces a truly silent application boundary.
    """

    session: dict
    output_dir: Path | None
    run_id: str


# ``ProjectRunDeps`` retired in ADR 0042 Phase J per r4 P2 — Phase I
# never consumed the empty seam, so the placeholder doesn't survive
# past the refactor series. A later ADR re-introduces a typed
# injection point (presentation policy / silent-finalize switch) at
# ``run_project_pipeline``'s signature when it has a concrete
# contract to ship; the Phase B docstring noted this trajectory.


__all__ = [
    "PresentationPolicy",
    "ProjectRunRequest",
    "ProjectRunResult",
]
