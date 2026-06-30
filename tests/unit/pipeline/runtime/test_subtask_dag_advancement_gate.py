"""
Guard module: does an INCOMPLETE subtask wrongly advance the DAG to its
DECLARED dependents?  (T1-reproduce-guard for the subtask-advancement gate.)

Reproduce first, then name the root. These tests drive ``run_dag_sequential``
with an injected ``invoke_subtask`` that fabricates an incomplete receipt in two
shapes — (1) an unparseable/unrecoverable attestation and (2) a confirmed but
``met=false`` mandatory criterion — and observe what the runner does to a
subtask that DECLARES ``depends_on=(<incomplete>,)``.

ROOT CAUSE (reproduced on the declared dependent): GATING / WAVE GRANULARITY,
not the parser and not a fail-fast-less production call site.

  * Parser root EXCLUDED — the incomplete shape IS detected: for the unparseable
    shape ``parse_attestation_for_subtask`` returns ``attestation unparseable:``
    (pipeline/subtask_attestation_parser.py:71, surfaced via
    pipeline/subtask_attestation_repair.py:31) and the one repair turn does not
    recover it; for the unmet shape ``validate_subtask_attestation`` returns
    ``done_criteria not met (by index): [...]``
    (pipeline/subtask_attestation_parser.py:143). In both cases
    ``SubTaskResult.attestation_error`` is set and the receipt state is
    ``incomplete`` — the parser/gate did its job.
  * Fail-fast root EXCLUDED — both production call sites pass
    ``stop_on_failure=True``: the main pass at
    pipeline/phases/builtin/subtask_dag.py:682 and the ADR-0073 repair pass at
    pipeline/phases/builtin/subtask_dag.py:774.
  * Gating granularity root REPRODUCED — ``run_dag_sequential`` has NO
    dependency-scoped readiness gate. The only block is the coarse, GLOBAL
    ``if failed and stop_on_failure`` wave-skip (pipeline/dag_runner.py:454).
    An incomplete subtask is appended to ``failed`` (pipeline/dag_runner.py:536-
    537) but is never recorded as "did not satisfy its declared dependency", and
    nothing consults a subtask's own ``depends_on`` terminal states before
    invoking it. ``topological_waves`` (pipeline/plan_parser.py:465) correctly
    orders a declared dependent into a strictly later wave, so wave ORDER is
    fine — what is missing is a per-dependency BLOCK. Consequently, at the
    ``run_dag_sequential`` default ``stop_on_failure=False`` (the contract
    surface), a declared dependent of an incomplete subtask is invoked and lands
    in ``completed``. Production masks the dependent case by passing
    ``stop_on_failure=True``, but that block is over-broad (it also skips
    independent branches) and incidental (it rides on wave ordering and the
    first-failure flag), not the dependency-scoped gate the contract requires.

T2 closed this root with a dependency-scoped advancement gate in
``run_dag_sequential`` (``_unsatisfied_dependencies`` / ``_is_blocking_outcome``,
ADR 0116): a subtask is invoked only when every declared ``depends_on`` edge
points at a terminally ``done`` dependency. These tests now lock the delivered
contract — a declared dependent of an incomplete dependency is held (skipped,
never invoked, never in ``completed``) regardless of ``stop_on_failure``, while
an independent branch (no ``depends_on``) is left untouched (F1).
"""

from __future__ import annotations

import json as _json

from agents.entities import SubTask
from agents.registry import AgentRegistry
from pipeline.dag_runner import run_dag_sequential
from pipeline.plan_parser import ParsedPlan
from pipeline.plugins import PluginConfig

# ── Fakes ──────────────────────────────────────────────────────────────────


class _Agent:
    """Trivial developer agent.

    ``invoke_subtask`` is always injected in these tests, so the runner never
    calls ``agent.invoke`` — guard against a silent path change by raising.
    """

    def __init__(self, model: str):
        self.model = model
        self.session_id = None

    def invoke(self, *a, **k):  # pragma: no cover - must never run
        raise AssertionError(
            "agent.invoke must not run when invoke_subtask is injected"
        )


def _registry() -> AgentRegistry:
    r = AgentRegistry()
    r.register("claude", lambda model, _e=None: _Agent(model))
    return r


def _parsed(*subs: SubTask) -> ParsedPlan:
    return ParsedPlan(
        short_summary="t",
        planning_context="t",
        subtasks=tuple(subs),
        source="json",
    )


def _attestation(subtask_id: str, *mets: bool) -> str:
    """Valid attestation JSON tail over N criteria with the given ``met`` flags."""
    return _json.dumps({
        "type": "subtask_attestation",
        "subtask_id": subtask_id,
        "criteria": [
            {
                "index": i,
                "criterion": f"criterion {i}",
                "met": met,
                "evidence": "did the thing",
            }
            for i, met in enumerate(mets, start=1)
        ],
        "summary": "report",
    })


def _unparseable() -> str:
    """An attestation envelope the parser cannot recover (drift shape).

    Mirrors the unrecoverable shape used by
    ``test_failed_attestation_repair_keeps_subtask_incomplete``: the parser
    raises and the one repair turn (handed the same shape) cannot fix it.
    """
    return _json.dumps({
        "subtask_attestation": [
            {
                "index": 1,
                "criterion": "criterion 1",
                "met": True,
                "evidence": "did the thing",
            },
        ],
    })


def _dependent_pair_incomplete(t1_output) -> tuple[ParsedPlan, list, object]:
    """A 2-node DAG: incomplete ``t1`` and a declared dependent ``t2``.

    ``t1_output`` is a callable ``() -> str`` returning t1's (incomplete) body;
    t2 always returns a clean all-met attestation, so if t2 is invoked it
    advances to ``completed``. Returns ``(plan, invoked_log, invoke_fn)``.
    """
    invoked: list[tuple[str, bool]] = []

    def _invoke(agent, turn, cwd, subtask, mutates_artifacts=True):
        invoked.append((subtask.id, mutates_artifacts))
        if subtask.id == "t1":
            return t1_output(), None
        return "downstream build\n\n" + _attestation("t2", True), None

    plan = _parsed(
        SubTask(id="t1", goal="g", done_criteria=("a",)),
        SubTask(id="t2", goal="g", done_criteria=("b",), depends_on=("t1",)),
    )
    return plan, invoked, _invoke


# ── Delivered contract: incomplete dependency HOLDS its declared dependent ───


def test_unparseable_attestation_holds_declared_dependent() -> None:
    """Form 1 — unparseable/unrecoverable attestation.

    At the ``run_dag_sequential`` default (``stop_on_failure=False``, the
    contract surface), t1 is incomplete and its declared dependent t2 is held:
    never invoked, never in ``completed``, recorded as ``skipped``.
    """
    plan, invoked, invoke = _dependent_pair_incomplete(
        lambda: "build output\n\n" + _unparseable()
    )
    result = run_dag_sequential(
        plan, PluginConfig(), _registry(),
        project_dir="/p", fallback_runtime="claude", fallback_model="m",
        invoke_subtask=invoke,
    )

    # The incomplete shape is detected; a repair turn ran but did not recover it
    # -> attestation_error is set, state incomplete (in failed, not completed).
    t1 = next(r for r in result.failed if r.subtask_id == "t1")
    assert t1.attestation_error is not None
    assert "attestation unparseable" in t1.attestation_error
    assert "attestation repair failed" in t1.attestation_error
    assert [(sid, m) for sid, m in invoked if sid == "t1"] == [
        ("t1", True),   # main invoke
        ("t1", False),  # one no-mutation repair turn
    ]
    assert "t1" not in {r.subtask_id for r in result.completed}

    # CONTRACT: the declared dependent is held behind the incomplete dep.
    assert "t2" not in {sid for sid, _m in invoked}      # dependent NOT invoked
    assert "t2" not in {r.subtask_id for r in result.completed}
    assert result.skipped == ("t2",)
    t2 = next(r for r in result.receipts if r.subtask_id == "t2")
    assert t2.state == "skipped"
    assert "unsatisfied dependency" in t2.error
    assert "t1" in t2.error
    assert not result.ok


def test_unmet_criterion_holds_declared_dependent() -> None:
    """Form 2 — confirmed attestation with an unmet mandatory criterion.

    ``validate_subtask_attestation`` returns ``done_criteria not met (by index):
    [1]`` so t1 is incomplete; its declared dependent t2 is held back.
    """
    plan, invoked, invoke = _dependent_pair_incomplete(
        lambda: "partial build\n\n" + _attestation("t1", False)
    )
    result = run_dag_sequential(
        plan, PluginConfig(), _registry(),
        project_dir="/p", fallback_runtime="claude", fallback_model="m",
        invoke_subtask=invoke,
    )

    # The unmet criterion is detected by index; no repair turn fires (repair
    # only triggers on the unparseable shape).
    t1 = next(r for r in result.failed if r.subtask_id == "t1")
    assert t1.attestation_error is not None
    assert "done_criteria not met (by index): [1]" in t1.attestation_error
    assert [(sid, m) for sid, m in invoked if sid == "t1"] == [("t1", True)]
    assert "t1" not in {r.subtask_id for r in result.completed}

    # CONTRACT: the declared dependent is held behind the incomplete dep.
    assert "t2" not in {sid for sid, _m in invoked}
    assert "t2" not in {r.subtask_id for r in result.completed}
    assert result.skipped == ("t2",)
    assert not result.ok


def test_block_cascades_transitively_down_dependency_chain() -> None:
    """A transitive dependent (t3 depends on the held t2) is also held.

    t1 incomplete -> t2 (depends t1) held -> t3 (depends t2) held. Only t1's
    agent is invoked; t2 and t3 are both skipped.
    """
    invoked: list[str] = []

    def _invoke(agent, turn, cwd, subtask, mutates_artifacts=True):
        invoked.append(subtask.id)
        if subtask.id == "t1":
            return "out\n\n" + _attestation("t1", False), None
        return "out\n\n" + _attestation(subtask.id, True), None

    plan = _parsed(
        SubTask(id="t1", goal="g", done_criteria=("a",)),
        SubTask(id="t2", goal="g", done_criteria=("b",), depends_on=("t1",)),
        SubTask(id="t3", goal="g", done_criteria=("c",), depends_on=("t2",)),
    )
    result = run_dag_sequential(
        plan, PluginConfig(), _registry(),
        project_dir="/p", fallback_runtime="claude", fallback_model="m",
        invoke_subtask=_invoke,
    )

    assert invoked == ["t1"]
    assert {r.subtask_id for r in result.completed} == set()
    assert set(result.skipped) == {"t2", "t3"}


# ── F1 guard: independent branches keep their existing semantics ─────────────


def test_independent_branch_not_blocked_by_incomplete_default() -> None:
    """An independent subtask (no ``depends_on``) is NOT held when another
    subtask is incomplete — the gate touches only declared dependents.

    ``z_indep`` is id-sorted after ``t1`` so it is scheduled later in the same
    pass, yet under the default (``stop_on_failure=False``) it still runs and
    completes, while the declared dependent ``dep`` is held. This is the F1
    requirement: no new restriction on independent branches.
    """
    invoked: list[str] = []

    def _invoke(agent, turn, cwd, subtask, mutates_artifacts=True):
        invoked.append(subtask.id)
        if subtask.id == "t1":
            return "out\n\n" + _attestation("t1", False), None
        return "out\n\n" + _attestation(subtask.id, True), None

    plan = _parsed(
        SubTask(id="t1", goal="g", done_criteria=("a",)),
        SubTask(id="dep", goal="g", done_criteria=("b",), depends_on=("t1",)),
        SubTask(id="z_indep", goal="g", done_criteria=("c",)),
    )
    result = run_dag_sequential(
        plan, PluginConfig(), _registry(),
        project_dir="/p", fallback_runtime="claude", fallback_model="m",
        invoke_subtask=_invoke,
    )

    # Independent branch ran and completed; declared dependent was held.
    assert "z_indep" in invoked
    assert "z_indep" in {r.subtask_id for r in result.completed}
    assert "dep" not in invoked
    assert "dep" in result.skipped
