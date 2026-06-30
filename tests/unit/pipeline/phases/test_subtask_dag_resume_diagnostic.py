"""Resume diagnostic: a torn (partial) subtask DAG must not silently continue.

Covers the early guard in ``_run_subtask_dag_implement``: on resume into a
partial subtask DAG (a subtask started but never reached a successful
DONE/ATTESTATION terminal) the IMPLEMENT phase ``state.stop``s with an
instructive message instead of marching on to the DAG / review. The supported
ADR 0073 ``implement_retry`` path and a fresh run are both inert.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agents.entities import SubTask
from agents.registry import AgentRegistry
from core.observability.events import append_event
from pipeline.phases.builtin.subtask_dag import _run_subtask_dag_implement
from pipeline.plan_parser import ParsedPlan
from pipeline.plugins import PluginConfig
from pipeline.runtime import PipelineState


class _ReachedDag(Exception):
    """Raised by the patched DAG runner to prove the diagnostic did NOT stop."""


def _agent() -> SimpleNamespace:
    return SimpleNamespace(runtime="claude", model="claude-opus-4-7")


def _registry(agent) -> AgentRegistry:
    reg = AgentRegistry()
    reg.register("claude", lambda model, _effort=None: agent)
    return reg


def _plan() -> ParsedPlan:
    return ParsedPlan(
        short_summary="p", planning_context="p",
        subtasks=(
            SubTask(id="T1", goal="first"),
            SubTask(id="T5", goal="fifth"),
        ),
        source="test",
    )


def _state(tmp_path, *, extras: dict | None = None) -> PipelineState:
    agent = _agent()
    return PipelineState(
        task="t", project_dir=str(tmp_path), plugin=PluginConfig(),
        parsed_plan=_plan(), registry=_registry(agent),
        output_dir=tmp_path,
        extras={"implementation_execution": "subtask_dag", **(extras or {})},
    )


def _write_partial_dag(tmp_path) -> None:
    """T1 fully done; T5 started but never finished (no DONE/ATTESTATION)."""
    append_event(tmp_path, "subtask.start", {"subtask_id": "T1"})
    append_event(tmp_path, "subtask.end", {"subtask_id": "T1", "ok": True})
    append_event(tmp_path, "subtask.start", {"subtask_id": "T5"})


_EXPECTED_MESSAGE = (
    "Cannot resume IMPLEMENT from partial subtask DAG state: subtask T5 "
    "started but has no DONE/ATTESTATION event. Start a follow-up or rerun "
    "implement after repair."
)


def test_partial_dag_resume_halts_before_dag(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "pipeline.dag_runner.run_dag_sequential",
        lambda *a, **k: (_ for _ in ()).throw(_ReachedDag()),
    )
    _write_partial_dag(tmp_path)
    state = _state(tmp_path)

    entry = _run_subtask_dag_implement(state, _agent(), None)

    assert state.halt is True
    assert state.halt_reason == _EXPECTED_MESSAGE
    assert entry["delivery_clean"] is False
    assert entry["output"] == ""
    assert entry["implementation_receipts"] == []


def test_implement_retry_path_does_not_trigger_diagnostic(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(
        "pipeline.dag_runner.run_dag_sequential",
        lambda *a, **k: (_ for _ in ()).throw(_ReachedDag()),
    )
    _write_partial_dag(tmp_path)
    # Truthy implement_retry skips the diagnostic; empty ids skip retry narrowing
    # so execution falls through to the (patched) DAG runner.
    state = _state(tmp_path, extras={"implement_retry": {"incomplete_ids": []}})

    with pytest.raises(_ReachedDag):
        _run_subtask_dag_implement(state, _agent(), None)

    assert state.halt is False
    assert _EXPECTED_MESSAGE not in (state.halt_reason or "")


def test_fresh_run_is_inert(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "pipeline.dag_runner.run_dag_sequential",
        lambda *a, **k: (_ for _ in ()).throw(_ReachedDag()),
    )
    # No prior subtask events at all → detector returns empty → inert.
    state = _state(tmp_path)

    with pytest.raises(_ReachedDag):
        _run_subtask_dag_implement(state, _agent(), None)

    assert state.halt is False
