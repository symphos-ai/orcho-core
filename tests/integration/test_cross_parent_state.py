"""Integration falsifiers for the durable canonical cross-parent state."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.cross_project.final_acceptance import (
    build_context,
    run_cross_final_acceptance,
)
from pipeline.cross_project.parent_state_runtime import reduce_runtime_cross_parent_state
from sdk import load_cross_parent_state


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def durable_cross_run(tmp_path: Path) -> tuple[Path, dict, dict]:
    """Materialize only declared paths plus typed state artifacts."""
    run = tmp_path / "cross-parent"
    parent = {
        "projects": {"api": "/src/api", "web": "/src/web"},
        "phase_handoff": {
            "id": "parent-handoff",
            "project_alias": "api",
            "child_handoff_id": "child-handoff",
            "available_actions": ["continue", "halt"],
        },
        "phases": {"projects": {"api": {"status": "done"}, "web": {"status": "done"}}},
    }
    checkpoint = {
        "phase_handoff_pending": True,
        "phase_handoff_kind": "project",
        "phase_handoff_id": "parent-handoff",
        "phase_handoff_project_alias": "api",
        "phase_handoff_child_id": "child-handoff",
        "sub_status": {"api": "done", "web": "done"},
    }
    _write(run / "meta.json", parent)
    _write(run / "cross_checkpoint.json", checkpoint)
    _write(run / "api" / "meta.json", {"status": "done"})
    _write(run / "web" / "meta.json", {"status": "done"})
    _write(run / "scheduled_gate_ledger.json", {
        "rows": [{"command": "pytest\u0000-q", "hook": "after_phase", "phase": "implement"}],
    })
    _write(run / "events.jsonl", {
        "seq": 1,
        "ts": "",
        "kind": "gate.start",
        "phase": None,
        "payload": {"command": "pytest\u0000-q", "hook": "after_phase", "phase": "implement"},
    })
    # These are deliberate falsifiers: no transcript parser or directory
    # discovery may consult either undeclared child-like data or prose.
    _write(run / "undeclared" / "meta.json", {"status": "failed"})
    (run / "transcript.md").write_text("not a state artifact", encoding="utf-8")
    return run, parent, checkpoint


class _ProviderMustNotRun:
    def invoke(self, *args, **kwargs):  # pragma: no cover - assertion is the call count
        raise AssertionError("CFA provider must not run for reduced preconditions")


def test_cross_parent_state_end_to_end_falsifiers(
    durable_cross_run: tuple[Path, dict, dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, parent, checkpoint = durable_cross_run

    # 1. Exact declaration order excludes undeclared directories and ignores prose.
    initial = load_cross_parent_state("cross-parent", runs_dir=run.parent, cwd=None)
    assert tuple(child.alias for child in initial.children) == ("api", "web")

    # 2. An engine-owned gate remains fully addressable by phase/hook/command.
    gate = initial.active_operations[0].gate
    assert gate is not None and (gate.phase, gate.hook, gate.command) == (
        "implement", "after_phase", ("pytest", "-q"),
    )

    # 3. The checkpoint routes the active parent handoff without id-prefix inference.
    pending = initial.pending_decision
    assert pending is not None and (
        pending.kind, pending.alias, pending.parent_handoff_id, pending.child_handoff_id,
        pending.available_actions,
    ) == ("project", "api", "parent-handoff", "child-handoff", ("continue", "halt"))

    # 4. An active/pending reduced state is a precondition, so no provider call occurs.
    cfa = run_cross_final_acceptance(
        build_context(
            cross_plan_markdown="# plan",
            aliases=("api", "web"),
            session_phases=parent["phases"],
            common_cwd=str(run),
            child_states={child.alias: child for child in initial.children},
            parent_blocked=True,
        ),
        codex=_ProviderMustNotRun(),
        dry_run=False,
    )
    assert cfa.source == "precondition"

    # 5. The next boundary sees a physical child update immediately.
    _write(run / "api" / "meta.json", {"status": "failed"})
    session = {
        **parent,
        "phases": {"projects": {"api": {"status": "failed"}, "web": {"status": "done"}}},
    }
    runtime = reduce_runtime_cross_parent_state(session, checkpoint, run)
    sdk_state = load_cross_parent_state("cross-parent", runs_dir=run.parent, cwd=None)
    assert next(child for child in sdk_state.children if child.alias == "api").status == "failed"

    # 6. Runtime and SDK share the exact reducer result and no directory scan is needed.
    monkeypatch.setattr(Path, "iterdir", lambda self: (_ for _ in ()).throw(AssertionError("scan")))
    assert runtime == load_cross_parent_state("cross-parent", runs_dir=run.parent, cwd=None)
