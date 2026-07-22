"""Exact-path durable adapter tests for canonical cross parent facts."""

from __future__ import annotations

import json
from pathlib import Path

from pipeline.run_state.cross import validate_cross_run_state
from pipeline.run_state.cross_parent import ParentClass, ScheduledGateIdentity
from pipeline.run_state.cross_parent_disk import (
    load_cross_parent_facts,
    load_cross_parent_state,
)


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _parent(path: Path) -> None:
    _write(path / "meta.json", {"projects": {"api": "/api", "web": "/web"}})


def test_reads_only_declared_child_paths_and_preserves_order(tmp_path: Path) -> None:
    _parent(tmp_path)
    _write(tmp_path / "api" / "meta.json", {"status": "done"})
    _write(tmp_path / "web" / "meta.json", {"status": "done"})
    _write(tmp_path / "undeclared" / "meta.json", {"status": "failed"})

    facts = load_cross_parent_facts(tmp_path)

    assert facts.declared_aliases == ("api", "web")
    assert [child.alias for child in facts.children] == ["api", "web"]


def test_malformed_child_is_not_silently_empty(tmp_path: Path) -> None:
    _parent(tmp_path)
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "meta.json").write_text("{broken", encoding="utf-8")

    state = load_cross_parent_state(tmp_path)

    assert state.parent_class is ParentClass.INCONSISTENT
    assert "physical_child_malformed" in {item.code for item in state.violations}


def test_unmatched_scheduled_gate_is_active_and_end_closes_it(tmp_path: Path) -> None:
    _parent(tmp_path)
    _write(tmp_path / "api" / "meta.json", {"status": "done"})
    _write(tmp_path / "web" / "meta.json", {"status": "done"})
    _write(
        tmp_path / "scheduled_gate_ledger.json",
        {"rows": [{"gate": "pytest", "hook": "after_phase", "phase": "implement"}]},
    )
    _write(
        tmp_path / "events.jsonl",
        {
            "seq": 1,
            "ts": "",
            "kind": "gate.start",
            "phase": None,
            "payload": {"command": "pytest", "hook": "after_phase", "phase": "implement"},
        },
    )

    active = load_cross_parent_state(tmp_path)
    assert active.parent_class is ParentClass.RUNNING
    identity = active.active_operations[0].gate
    assert identity == ScheduledGateIdentity("implement", "after_phase", ("pytest",))

    with (tmp_path / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write("\n")
        stream.write(
            json.dumps(
                {
                    "seq": 2,
                    "ts": "",
                    "kind": "gate.end",
                    "phase": None,
                    "payload": {"command": "pytest", "hook": "after_phase", "phase": "implement"},
                }
            )
            + "\n"
        )
    assert load_cross_parent_state(tmp_path).active_operations == ()


def test_child_scheduled_gate_uses_parent_stream_and_exact_alias(tmp_path: Path) -> None:
    _parent(tmp_path)
    _write(tmp_path / "api" / "meta.json", {"status": "running"})
    _write(tmp_path / "web" / "meta.json", {"status": "done"})
    for alias in ("api", "web"):
        _write(
            tmp_path / alias / "scheduled_gate_ledger.json",
            {"rows": [{"gate": "pytest", "hook": "after_phase", "phase": "implement"}]},
        )
    _write(
        tmp_path / "events.jsonl",
        {
            "seq": 1,
            "ts": "",
            "kind": "gate.start",
            "phase": None,
            "payload": {
                "command": "pytest",
                "hook": "after_phase",
                "phase": "implement",
                "project_alias": "api",
            },
        },
    )
    with (tmp_path / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write("\n")
        stream.write(
            json.dumps(
                {
                    "seq": 2,
                    "ts": "",
                    "kind": "phase.start",
                    "phase": "IMPLEMENT",
                    "payload": {"phase_key": "implement"},
                }
            )
            + "\n"
        )

    active = load_cross_parent_state(tmp_path)
    api = active.children[0]
    web = active.children[1]
    assert active.parent_class is ParentClass.RUNNING
    assert len(api.active_operations) == 1
    assert api.active_operations[0].gate == ScheduledGateIdentity(
        "implement", "after_phase", ("pytest",), "api"
    )
    assert api.blockers == ()
    assert web.active_operations == ()

    with (tmp_path / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "seq": 3,
                    "ts": "",
                    "kind": "gate.end",
                    "phase": None,
                    "payload": {
                        "command": "pytest",
                        "hook": "after_phase",
                        "phase": "implement",
                        "project_alias": "api",
                    },
                }
            )
            + "\n"
        )
    assert load_cross_parent_state(tmp_path).children[0].active_operations == ()


def test_checkpoint_routing_uses_fields_not_handoff_id_prefix(tmp_path: Path) -> None:
    _parent(tmp_path)
    _write(
        tmp_path / "api" / "meta.json",
        {
            "status": "awaiting_phase_handoff",
            "phase_handoff": {
                "id": "not-a-prefix",
                "available_actions": ["continue"],
                "artifacts": {"project_alias": "api", "child_handoff_id": "child-1"},
            },
        },
    )
    _write(tmp_path / "web" / "meta.json", {"status": "done"})
    _write(
        tmp_path / "cross_checkpoint.json",
        {
            "phase_handoff_pending": True,
            "phase_handoff_kind": "project",
            "phase_handoff_id": "not-a-prefix",
            "phase_handoff_project_alias": "api",
            "phase_handoff_child_id": "child-1",
        },
    )

    state = load_cross_parent_state(tmp_path)
    assert state.pending_decision is not None
    assert state.pending_decision.kind == "project"
    assert state.pending_decision.alias == "api"


def test_project_proxy_and_child_payload_are_one_durable_pending_decision(tmp_path: Path) -> None:
    _parent(tmp_path)
    _write(
        tmp_path / "meta.json",
        {
            "projects": {"api": "/api", "web": "/web"},
            "phase_handoff": {
                "id": "parent-handoff",
                "project_alias": "api",
                "child_handoff_id": "child-handoff",
                "available_actions": ["continue", "halt"],
            },
        },
    )
    _write(
        tmp_path / "api" / "meta.json",
        {
            "status": "awaiting_phase_handoff",
            "phase_handoff": {
                "id": "child-handoff",
                "available_actions": ["continue", "halt"],
            },
        },
    )
    _write(tmp_path / "web" / "meta.json", {"status": "done"})
    _write(
        tmp_path / "cross_checkpoint.json",
        {
            "phase_handoff_pending": True,
            "phase_handoff_kind": "project",
            "phase_handoff_id": "parent-handoff",
            "phase_handoff_project_alias": "api",
            "phase_handoff_child_id": "child-handoff",
        },
    )

    state = load_cross_parent_state(tmp_path)

    assert state.parent_class is ParentClass.AWAITING_OPERATOR
    assert state.violations == ()
    assert not any(
        issue.code == "cross_parent_multiple_pending_decisions"
        for issue in validate_cross_run_state(tmp_path)
    )


def test_checkpoint_routing_conflict_with_payload_is_inconsistent(tmp_path: Path) -> None:
    _parent(tmp_path)
    _write(
        tmp_path / "api" / "meta.json",
        {
            "status": "awaiting_phase_handoff",
            "phase_handoff": {
                "id": "project:api:child-1",
                "kind": "project",
                "project_alias": "api",
                "child_handoff_id": "child-1",
                "available_actions": ["continue"],
            },
        },
    )
    _write(tmp_path / "web" / "meta.json", {"status": "done"})
    _write(
        tmp_path / "cross_checkpoint.json",
        {
            "phase_handoff_pending": True,
            "phase_handoff_kind": "cross_plan",
            "phase_handoff_project_alias": "web",
            "phase_handoff_id": "project:api:child-1",
            "phase_handoff_child_id": "child-1",
        },
    )

    state = load_cross_parent_state(tmp_path)

    assert state.parent_class is ParentClass.INCONSISTENT
    assert {item.code for item in state.violations} >= {
        "checkpoint_kind_conflict",
        "checkpoint_alias_conflict",
    }
