"""Repair-dependency integration (ADR 0073, T13/§6).

When an INCOMPLETE subtask depends on a DONE subtask, substance repair must
re-run ONLY the incomplete node: the done dependency rides along as read-only
context (its continuity hint is present in the repair prompt) and is never
re-invoked or re-mutated.
"""
from __future__ import annotations

from agents.entities import SubTask
from agents.registry import AgentRegistry
from agents.runtimes._strategy import _mock_subtask_attestation
from pipeline.dag_runner import PriorSubtaskContext, run_dag_sequential
from pipeline.plan_parser import ParsedPlan
from pipeline.plugins import PluginConfig
from pipeline.subtask_substance_repair import build_repair_plan, run_substance_repair


class _RecordingDev:
    """Mock developer: records every prompt it is invoked with and closes the
    attestation contract so a criteria-bearing subtask reaches ``done``."""

    def __init__(self) -> None:
        self.model = "claude-opus-4-7"
        self.session_id = None
        self.runtime = "claude"
        self.prompts: list[str] = []

    def invoke(self, prompt, cwd, *, continue_session=False, attachments=(),
               mutates_artifacts=False) -> str:
        self.prompts.append(prompt)
        return "applied changes\n" + _mock_subtask_attestation(prompt)


def _registry(agent: _RecordingDev) -> AgentRegistry:
    reg = AgentRegistry()
    reg.register("claude", lambda model, _e=None: agent)
    return reg


def _plan(*subs: SubTask) -> ParsedPlan:
    return ParsedPlan(
        short_summary="s", planning_context="c",
        subtasks=tuple(subs), source="json",
    )


def test_repair_runs_only_incomplete_with_done_dependency_context() -> None:
    agent = _RecordingDev()
    # ``a`` is already done; ``b`` is incomplete and depends on ``a``.
    plan = _plan(
        SubTask(id="a", goal="build the base module"),
        SubTask(id="b", goal="extend it", depends_on=("a",),
                done_criteria=("c1",)),
    )
    done_ctx = {
        "a": PriorSubtaskContext(
            subtask_id="a", attestation_summary="a built the base module",
        ),
    }

    def repair_pass(repair_plan, prior_results):
        return run_dag_sequential(
            repair_plan,
            PluginConfig(),
            _registry(agent),
            project_dir="/p",
            fallback_runtime="claude",
            fallback_model="m",
            prior_results=prior_results,
        )

    result = run_substance_repair(
        parsed_plan=plan,
        incomplete_ids={"b"},
        done_context=done_ctx,
        repair_attempts=1,
        repair_pass=repair_pass,
    )

    # Only the incomplete node was repaired.
    assert result.repaired_ids == ("b",)
    assert result.all_repaired is True
    # Exactly one invocation — for ``b`` only; ``a`` was never re-invoked.
    assert len(agent.prompts) == 1
    prompt = agent.prompts[0]
    assert "## Current Executable Subtask `b`" in prompt
    assert "## Current Executable Subtask `a`" not in prompt
    # The done dependency's context IS present (continuity hint).
    assert "## Upstream Completed" in prompt
    assert "a built the base module" in prompt
    # Repair receipts cover only ``b`` — ``a`` was not re-mutated.
    assert [r.subtask_id for r in result.receipts] == ["b"]


def test_build_repair_plan_excludes_done_dependency() -> None:
    # The filtered repair plan carries only the incomplete node; the done
    # dependency is intentionally absent (it becomes a prior_results id).
    plan = _plan(
        SubTask(id="a", goal="base"),
        SubTask(id="b", goal="extend", depends_on=("a",)),
    )
    repair = build_repair_plan(plan, {"b"})
    assert [s.id for s in repair.subtasks] == ["b"]
    # ``b`` keeps its dangling dep on the (now-prior) ``a``.
    assert repair.subtasks[0].depends_on == ("a",)


def test_done_dependency_context_object_not_mutated() -> None:
    agent = _RecordingDev()
    plan = _plan(
        SubTask(id="a", goal="base"),
        SubTask(id="b", goal="extend", depends_on=("a",),
                done_criteria=("c1",)),
    )
    prior = PriorSubtaskContext(subtask_id="a", attestation_summary="done a")
    done_ctx = {"a": prior}

    def repair_pass(repair_plan, prior_results):
        return run_dag_sequential(
            repair_plan, PluginConfig(), _registry(agent),
            project_dir="/p", fallback_runtime="claude", fallback_model="m",
            prior_results=prior_results,
        )

    run_substance_repair(
        parsed_plan=plan, incomplete_ids={"b"}, done_context=done_ctx,
        repair_attempts=1, repair_pass=repair_pass,
    )
    # The prior context (a frozen value object) is unchanged after repair.
    assert done_ctx["a"] is prior
    assert prior.attestation_summary == "done a"
    assert prior.summary == ""  # degraded view never gained output
