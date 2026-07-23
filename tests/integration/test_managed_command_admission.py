from __future__ import annotations

import json

import pytest

from agents.managed_command import (
    DuplicateManagedCommandError,
    ManagedCommandIdentity,
    ManagedCommandState,
    ManagedCommandStore,
)
from agents.stream_parsers.codex_jsonl import parse_codex_line
from core.observability import events


def test_provider_wrapper_completion_cannot_release_managed_command(
    tmp_path,
) -> None:
    """Falsifier for lost cell + partial completion + duplicate attempt."""
    run_dir = tmp_path / "run-1"
    checkout = tmp_path / "checkout"
    run_dir.mkdir()
    checkout.mkdir()
    events.init_event_store(run_dir)
    try:
        identity = ManagedCommandIdentity.build(
            run_dir=run_dir,
            phase="repair_changes",
            cwd=checkout,
            argv=("python", "-m", "pytest", "-q"),
        )
        store = ManagedCommandStore(run_dir)
        store.admit(identity)

        parse_codex_line(json.dumps({
            "type": "item.started",
            "item": {
                "id": "cell-16",
                "type": "command_execution",
                "command": "python -m pytest -q",
            },
        }))
        parse_codex_line(json.dumps({
            "type": "item.completed",
            "item": {
                "id": "wait-cell-16",
                "type": "command_execution",
                "command": "wait cell-16",
                "status": "completed",
                "aggregated_output": "........................",
            },
        }))
        # The provider says its wrapper completed and no longer exposes the
        # cell. Neither observation is an exact child exit.
        assert store.observe(identity).state is ManagedCommandState.UNKNOWN
        with pytest.raises(DuplicateManagedCommandError):
            store.admit(identity)
    finally:
        events.init_event_store(None)
