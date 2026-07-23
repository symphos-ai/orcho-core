"""Durable cross-run resume coverage for XF2 child-session hydration.

The child boundary is deliberately deterministic here, while the cross
coordinator, graph scheduler, parent reducer, runner gates, disk persistence,
and SDK projection are exercised through their production entry points.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pipeline.cross_project.app import run_cross_project_pipeline
from pipeline.cross_project.app_types import CrossRunRequest
from pipeline.cross_project.execution_graph import CrossExecutionGraphNodeKind
from pipeline.cross_project.execution_graph_state_runtime import (
    reduce_runtime_cross_execution_graph_state,
)
from pipeline.cross_project.execution_graph_store import load_cross_execution_graph
from pipeline.cross_project.graph_scheduler import select_first_ready_node
from pipeline.presentation import PresentationPolicy
from pipeline.run_state.cross_parent_disk import load_cross_parent_state
from sdk import load_cross_execution_graph_state


def _approved_release() -> dict[str, Any]:
    return {
        "verdict": "APPROVED",
        "approved": True,
        "short_summary": "Ready to ship.",
        "findings": [],
        "ship_ready": True,
        "release_blockers": [],
        "verification_gaps": [],
        "contract_status": {
            "task_contract": "satisfied",
            "interfaces": "compatible",
            "persistence": "safe",
            "tests": "sufficient",
        },
    }


def _cross_plan() -> str:
    return json.dumps({
        "short_summary": "Producer precedes consumer.",
        "interface_contract": "Consumer accepts producer output.",
        "implementation_order": ["producer", "consumer"],
        "subtasks": [
            {
                "alias": "consumer", "goal": "Consume the interface.",
                "spec": "Depend on producer.", "depends_on": ["producer"],
                "files": [], "produces": "", "consumes": "producer output",
            },
            {
                "alias": "producer", "goal": "Provide the interface.",
                "spec": "Produce the shared output.", "depends_on": [],
                "files": [], "produces": "producer output", "consumes": "",
            },
        ],
    })


def _approved_review() -> str:
    return json.dumps({
        "verdict": "APPROVED", "short_summary": "Approved.",
        "findings": [], "risks": [], "checks": [],
    })


class _Runtime:
    model = "xf2-test"

    def __init__(self, provider: _Provider, role: str) -> None:
        self.provider = provider
        self.role = role

    def invoke(self, prompt: str, _cwd: str, **_kwargs: Any) -> str:
        if 'name="release_json"' in prompt:
            self.provider.cfa_calls += 1
            return json.dumps({
                "verdict": "APPROVED", "ship_ready": True,
                "short_summary": "Cross release is ready.",
                "release_blockers": [], "verification_gaps": [],
                "contract_status": {
                    "task_contract": "satisfied", "interfaces": "compatible",
                    "persistence": "safe", "tests": "sufficient",
                },
            })
        if self.role == "plan":
            self.provider.plan_calls += 1
            return _cross_plan()
        self.provider.contract_calls += 1
        return _approved_review()


class _Provider:
    def __init__(self) -> None:
        self.plan_calls = 0
        self.contract_calls = 0
        self.cfa_calls = 0
        self.plan = _Runtime(self, "plan")
        self.review = _Runtime(self, "review")

    def resolve(self, runtime: str, _model: str, *, effort: str | None = None) -> _Runtime:  # noqa: ARG002
        return self.review if runtime == "codex" else self.plan

    def claude(self, _model: str) -> _Runtime:
        return self.plan

    def codex(self, _model: str) -> _Runtime:
        return self.review


def _configure_cross(monkeypatch: pytest.MonkeyPatch) -> None:
    from pipeline.cross_project import run_setup, session_run

    monkeypatch.setattr(
        session_run.config.AppConfig, "load",
        classmethod(lambda _cls: SimpleNamespace(
            hypothesis={"enabled": False}, task_language="English",
            pipeline={}, artifacts={}, phase_effort_map={},
        )),
    )
    monkeypatch.setattr(
        run_setup, "load_plugin",
        lambda _path: SimpleNamespace(
            name="XF2 project", language="Python", architecture="", file_hints=[],
        ),
    )
    monkeypatch.setattr(session_run, "_plan_hypothesis_step", lambda *_args, **_kwargs: None)


def _child_dispatch(calls: list[str]):
    from pipeline.project.types import ProjectRunResult

    def dispatch(request):
        calls.append(Path(request.project_dir).name)
        session = {
            "status": "done",
            "worktree": {"path": request.project_dir},
            "phases": {"final_acceptance": _approved_release()},
        }
        request.output_dir.mkdir(parents=True, exist_ok=True)
        (request.output_dir / "meta.json").write_text(json.dumps(session), encoding="utf-8")
        return ProjectRunResult(session=session, output_dir=request.output_dir, run_id=request.project_alias)

    return dispatch


class _DeliveryInterrupted(RuntimeError):
    pass


def _request(
    projects: dict[str, Path], run_dir: Path, provider: _Provider, **kwargs: Any,
) -> CrossRunRequest:
    return CrossRunRequest(
        task="Hydrate producer before consumer.", projects=projects, output_dir=run_dir,
        provider=provider, profile_name="feature", presentation=PresentationPolicy.SILENT,
        no_interactive=True, **kwargs,
    )


def _runtime_and_disk_graph_match(run_dir: Path) -> None:
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    checkpoint = json.loads((run_dir / "cross_checkpoint.json").read_text(encoding="utf-8"))
    graph = load_cross_execution_graph(run_dir)
    runtime = reduce_runtime_cross_execution_graph_state(graph, meta, checkpoint, str(run_dir))
    disk = load_cross_execution_graph_state(run_dir.name, runs_dir=run_dir.parent, cwd=None)
    assert disk == runtime


def test_resume_hydrates_completed_children_and_reuses_runner_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reverse input order survives an interrupted delivery and resumes once."""
    from pipeline.cross_project import project_dispatch

    _configure_cross(monkeypatch)
    producer = tmp_path / "producer"
    consumer = tmp_path / "consumer"
    producer.mkdir()
    consumer.mkdir()
    run_dir = tmp_path / "run"
    provider = _Provider()
    child_calls: list[str] = []
    monkeypatch.setattr(project_dispatch, "run_project_pipeline", _child_dispatch(child_calls))
    monkeypatch.setattr(
        "pipeline.cross_project.cross_delivery.run_cross_delivery",
        lambda **_kwargs: (_ for _ in ()).throw(_DeliveryInterrupted()),
    )

    projects = {"consumer": consumer, "producer": producer}
    try:
        first = run_cross_project_pipeline(_request(projects, run_dir, provider))
    except _DeliveryInterrupted:
        pass
    else:
        pytest.fail(
            f"delivery was not reached: status={first.session.get('status')!r}, "
            f"children={child_calls!r}, contract={provider.contract_calls}, "
            f"cfa={provider.cfa_calls}, phases={first.session.get('phases')!r}"
        )

    assert child_calls == ["producer", "consumer"]
    # One review invocation admits the cross plan; the second is the actual
    # contract-check provider call. Resume must add neither.
    assert provider.contract_calls == 2
    assert provider.cfa_calls == 1
    before = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert set(before["phases"]["projects"]) == {"consumer", "producer"}
    _runtime_and_disk_graph_match(run_dir)

    monkeypatch.setattr(
        "pipeline.cross_project.cross_delivery.run_cross_delivery",
        lambda **_kwargs: SimpleNamespace(overall="disabled"),
    )
    resumed = run_cross_project_pipeline(_request(
        projects, run_dir, provider, resume_from=run_dir.name, resumed_meta=before,
    ))

    assert child_calls == ["producer", "consumer"]
    assert provider.contract_calls == 2
    assert provider.cfa_calls == 1
    assert resumed.session["phases"]["projects"] == before["phases"]["projects"]
    persisted = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert tuple(persisted["phases"]["projects"]) == ("consumer", "producer")
    assert persisted["phases"]["projects"] == before["phases"]["projects"]
    assert "CFA_MISSING_RELEASE_" not in json.dumps(persisted)
    assert "checkpoint_pending_without_payload" not in json.dumps(persisted)
    _runtime_and_disk_graph_match(run_dir)

    # A partial durable resume carries producer's physical outcome, not merely
    # its checkpoint cursor, so the structural scheduler selects consumer.
    partial = tmp_path / "partial"
    partial.mkdir()
    shutil.copy(run_dir / "cross_execution_graph.json", partial / "cross_execution_graph.json")
    (partial / "meta.json").write_text(json.dumps({
        "projects": {"consumer": str(consumer), "producer": str(producer)},
        "phases": {"projects": {"producer": before["phases"]["projects"]["producer"]}},
    }), encoding="utf-8")
    (partial / "cross_checkpoint.json").write_text(json.dumps({
        "phase0_done": True, "sub_status": {"producer": "done", "consumer": "pending"},
    }), encoding="utf-8")
    (partial / "producer").mkdir()
    (partial / "producer" / "meta.json").write_text(json.dumps(
        before["phases"]["projects"]["producer"],
    ), encoding="utf-8")
    partial_graph = load_cross_execution_graph(partial)
    partial_state = reduce_runtime_cross_execution_graph_state(
        partial_graph,
        json.loads((partial / "meta.json").read_text(encoding="utf-8")),
        json.loads((partial / "cross_checkpoint.json").read_text(encoding="utf-8")), str(partial),
    )
    ready = select_first_ready_node(partial_state)
    assert ready is not None
    assert ready.kind is CrossExecutionGraphNodeKind.PROJECT
    assert ready.alias == "consumer"


@pytest.mark.parametrize("kind", ("missing", "malformed", "truncated"))
def test_corrupt_child_release_payloads_stay_fail_closed(tmp_path: Path, kind: str) -> None:
    """Physical payload corruption never inherits a parent-side APPROVED copy."""
    (tmp_path / "meta.json").write_text(json.dumps({
        "projects": {"api": "/api"},
        "phases": {"projects": {"api": {
            "status": "done", "phases": {"final_acceptance": _approved_release()},
        }}},
    }), encoding="utf-8")
    (tmp_path / "cross_checkpoint.json").write_text(
        json.dumps({"sub_status": {"api": "done"}}), encoding="utf-8",
    )
    if kind == "malformed":
        (tmp_path / "api").mkdir()
        (tmp_path / "api" / "meta.json").write_text("{broken", encoding="utf-8")
    elif kind == "truncated":
        (tmp_path / "api").mkdir()
        (tmp_path / "api" / "meta.json").write_text(json.dumps({
            "status": "done", "phases": {"final_acceptance": {"verdict": "APPROVED"}},
        }), encoding="utf-8")

    state = load_cross_parent_state(tmp_path)
    child = state.children[0]
    assert child.release_disposition.value != "approved"
    if kind == "missing":
        assert {blocker.code for blocker in child.blockers} == {"child_missing"}
    elif kind == "malformed":
        assert "physical_child_malformed" in {item.code for item in state.violations}
    else:
        assert child.release_disposition.value == "not_applicable"
