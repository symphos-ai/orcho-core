"""agents/entities.py — Domain entities for the agent pipeline.

Plan parsing now lives in :mod:`pipeline.plan_parser`; the canonical PLAN
object is ``ParsedPlan``. This module keeps the agent-adjacent entities that
do not depend on pipeline runtime wiring.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SubTask:
    """A team-lead-decomposed unit of work with explicit DAG dependencies.

    Produced by the PLAN phase when the architect emits the structured
    ``## Task N`` / JSON-fence format. ``depends_on`` references other SubTask
    ``id``s — the runner topo-sorts and (later) groups into parallel waves.
    ``skill`` is an optional name resolved against the plugin's skill registry
    (auto-discovered from the plugin folder); when empty, the runner falls
    back to the per-phase build agent. ``done_criteria`` are checkable
    conditions the per-task QA gate validates before marking the subtask done.

    Phase 1 redesign extensions (additive):
      * ``owned_files`` — glob patterns the subtask writes to. Used by
        Milestone 9 wave executor for proactive conflict avoidance:
        subtasks with overlapping owned_files are scheduled in different
        waves rather than racing on merge.
      * ``architectural_decision`` — flag for the artifact pipeline. When
        ``ArtifactProfile.ADR`` (or higher) is selected, an ADR is emitted
        per subtask carrying this flag (Milestone 11).
    """
    id: str
    goal: str
    spec: str = ""
    files: tuple[str, ...] = field(default_factory=tuple)
    skill: str | None = None
    model: str | None = None
    depends_on: tuple[str, ...] = field(default_factory=tuple)
    done_criteria: tuple[str, ...] = field(default_factory=tuple)
    owned_files: tuple[str, ...] = field(default_factory=tuple)
    # Companion modifications the reviewer may accept for THIS task beyond
    # the project-wide ``allowed_modifications`` (lockfiles, regenerated
    # snapshots, derived artifacts). This is an *informational review
    # allowance* surfaced in the Plan Contract — NOT a write-scope
    # primitive for the wave planner, which schedules on ``owned_files``.
    allowed_modifications: tuple[str, ...] = field(default_factory=tuple)
    architectural_decision: bool = False


@dataclass(frozen=True)
class TestResult:
    """Result of running the project's test suite as a shell step.

    Produced by the orchestrator's ``run_tests()`` shell wrapper, not by an
    LLM. ``output`` is the combined stdout+stderr fed back into the repair_changes prompt
    when tests fail. ``skipped`` is True when the plugin defines no
    ``run_command`` — the pipeline must treat that as success.
    """
    # Tell pytest not to collect this as a test class (the leading "Test"
    # in the name would otherwise trigger PytestCollectionWarning).
    __test__ = False

    skipped: bool = False
    passed: bool = True
    output: str = ""
    duration: float = 0.0

    @property
    def failed(self) -> bool:
        return not self.skipped and not self.passed
