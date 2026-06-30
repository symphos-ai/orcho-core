"""
pipeline/runtime/state.py — ``PipelineState`` (run state model).

The state object threaded through every phase handler. The set of
fields is intentionally open: handlers may attach data via ``extras``
rather than expanding this dataclass for every new phase. Built-in
fields cover the common cross-phase needs (task, project, plugin,
plan output, last critique, halt control, lifecycle context channel).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PipelineState:
    """Threaded through every phase handler.

    The set of fields is intentionally open: handlers may attach data via
    ``extras`` rather than expanding this dataclass for every new phase.
    Built-in fields cover the common cross-phase needs (task, project,
    plugin, plan output, last critique, halt control).
    """
    task: str
    project_dir: str
    plugin: Any  # PluginConfig — typed loosely to keep runtime import-free
    registry: Any = None  # AgentRegistry — same rationale
    phase_config: Any = None  # PhaseAgentConfig — populated by orchestrator

    output_dir: Path | None = None
    dry_run: bool = False

    # Cross-phase artefacts populated as phases run.
    plan_markdown: str = ""        # rendered markdown view of ParsedPlan
    parsed_plan: Any = None        # ParsedPlan from decompose
    last_critique: str = ""        # reviewer critique from validate_plan / review / final_acceptance
    human_feedback: str = ""       # operator feedback from phase_handoff_decide(retry_feedback)
    last_test_output: str = ""
    dag_result: Any = None         # DagRunResult after execute_dag

    # Per-phase log: handler name → arbitrary dict the handler emitted.
    # Read by callers that want a structured record of the run; persisted
    # by the legacy orchestrator as session["phases"][...].
    phase_log: dict[str, Any] = field(default_factory=dict)

    # Free-form bag for handler-private state (avoid dataclass churn).
    extras: dict[str, Any] = field(default_factory=dict)

    # Phase 4.5: prompt-context attachments threaded into agent invocations.
    # Phase 1 only carries the field; Phase 4.5 wires CLI/MCP loaders +
    # per-runtime multimodal translation.
    attachments: tuple = field(default_factory=tuple)  # tuple[Attachment, ...]

    # Control flags. ``halt=True`` stops the runner before the next phase;
    # ``halt_reason`` surfaces in logs and the run summary.
    halt: bool = False
    halt_reason: str = ""

    # Generic phase-handoff request emitted by the loop runner. When a
    # phase declares a non-bypass ``handoff`` policy and the runtime
    # trigger fires, the loop driver builds a ``PhaseHandoffRequested``
    # signal, asks the active resolver, and on ``PAUSE`` writes the
    # signal here + halts. The project orchestrator (slice 3) reads this
    # after the run loop exits and owns the meta + event-emission +
    # rc=4 side. Typed loosely (``Any``) so this module avoids importing
    # ``pipeline.runtime.handoff`` at module load (the handoff module
    # imports PipelineState — would cycle).
    phase_handoff_request: Any = None

    # Phase 5e-5 substep 6c: typed lifecycle context channel. Replaces
    # the ad-hoc ``state.extras["_lifecycle_ctx"]`` string-keyed channel
    # introduced in substep 4. The FSM (``PhaseLifecycle.execute_step``)
    # populates this before each handler invocation and clears it
    # afterwards via try/finally — handlers read off it through the
    # ``pipeline.phases.builtin._ensure_lifecycle_ctx`` helper, which
    # auto-builds a default ctx for direct unit-test paths.
    #
    # Typed loosely (``Any``) to avoid importing ``LifecycleContext``
    # from ``pipeline.lifecycle`` at module load (lifecycle imports
    # PipelineState — cycle).
    lifecycle_ctx: Any = None

    # ADR 0026 / M7: per-run map from
    # :class:`pipeline.prompts.session.PhysicalSessionKey` to
    # :class:`pipeline.prompts.session.PromptSessionState`. Populated
    # by phase handlers that opt into session-aware delta rendering
    # (validate_plan first; M8/M9 add hypothesis/plan/replan/
    # review/repair). The dict is in-memory only; durable trace
    # persistence is M12's job.
    #
    # Typed loosely (``dict[Any, Any]``) to avoid importing the
    # session module at this layer — keeps PipelineState
    # framework-agnostic and avoids a runtime/prompts import cycle.
    prompt_sessions: dict[Any, Any] = field(default_factory=dict)

    def stop(self, reason: str) -> None:
        """Convenience: set halt + reason in one call."""
        self.halt = True
        self.halt_reason = reason


__all__ = ["PipelineState"]
