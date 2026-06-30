# SPDX-License-Identifier: Apache-2.0
"""Durable ``command_stalled`` evidence contract — both paths (ADR 0103 / T4).

A stalled command surfaces in the evidence bundle as a ``command_stalled``
error record, emitted once per ``agent.command_stalled`` event. The single
event kind covers BOTH stall paths, discriminated by the ``terminal`` payload
flag:

* **terminal** (``terminal=True``) — the idle-timeout escalation that failed
  the run (emitted by ``run.py`` just before ``run.end``);
* **non-terminal** (``terminal=False``) — the live unsafe-process-polling
  diagnostic written through the provider-neutral sink during a stream event,
  which does NOT fail the run.

This owns the persisted-evidence contract: the bundle must carry both records
with ``recovery_actions`` and pass schema validation.
"""

from __future__ import annotations

import json
from pathlib import Path

from agents.stall_protocol import StalledCommand, StallReason
from pipeline.evidence import collect_evidence
from pipeline.evidence.schema import (
    KNOWN_ERROR_KINDS,
    EvidenceSchemaError,
    validate_bundle,
)


def _event(seq: int, kind: str, payload: dict, *, phase: str | None = None) -> dict:
    return {
        "seq": seq,
        "ts": f"2026-06-24T00:00:{seq:02d}.000",
        "kind": kind,
        "phase": phase,
        "payload": payload,
    }


def _write_run(
    target: Path, *, meta: dict, events: list[dict],
) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    target.joinpath("meta.json").write_text(json.dumps(meta), encoding="utf-8")
    target.joinpath("events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8",
    )
    return target


def _terminal_stall_payload() -> dict:
    return StalledCommand(
        phase="implement",
        elapsed_s=300.0,
        command_preview="pytest -q -m 'not e2e'",
        output_tail="(no output for 300s)",
        reason=StallReason.SILENT_CHILD_COMMAND,
        process_group=9090,
    ).event_payload(terminal=True)


def _non_terminal_stall_payload() -> dict:
    return StalledCommand(
        phase="implement",
        elapsed_s=8.0,
        command_preview="kill -0 $(pgrep -f 'pytest -q -m')",
        output_tail="",
        reason=StallReason.UNSAFE_PROCESS_POLLING,
    ).event_payload(terminal=False)


def test_command_stalled_is_a_known_error_kind() -> None:
    assert "command_stalled" in KNOWN_ERROR_KINDS


def test_bundle_carries_both_paths_and_validates(tmp_path: Path) -> None:
    """One bundle with BOTH a terminal and a live non-terminal stall record."""
    run_dir = _write_run(
        tmp_path / "run",
        meta={"status": "failed", "task": "demo", "profile": "feature"},
        events=[
            _event(1, "run.start", {"task": "demo", "run_kind": "single_project",
                                    "project": "/p", "profile": "feature"}),
            # Live non-terminal diagnostic while the phase was running.
            _event(2, "agent.command_stalled", _non_terminal_stall_payload(),
                   phase="IMPLEMENT"),
            # Terminal idle-timeout escalation just before run.end.
            _event(3, "agent.command_stalled", _terminal_stall_payload(),
                   phase="IMPLEMENT"),
            _event(4, "run.end", {"status": "failed", "error": "stalled",
                                  "error_type": "AgentCommandStalledError"}),
        ],
    )

    bundle = collect_evidence(run_dir)

    # Schema validation passes with the new records present.
    validate_bundle(bundle)

    stalls = [e for e in bundle["errors"] if e.get("kind") == "command_stalled"]
    assert len(stalls) == 2

    non_terminal = [e for e in stalls if e["terminal"] is False]
    terminal = [e for e in stalls if e["terminal"] is True]
    assert len(non_terminal) == 1
    assert len(terminal) == 1

    # Non-terminal live diagnostic.
    nt = non_terminal[0]
    assert nt["reason"] == "unsafe_process_polling"
    assert nt["phase"] == "implement"
    assert nt["elapsed_s"] == 8.0
    assert [a["action"] for a in nt["recovery_actions"]] == [
        "interrupt", "resume_from_checkpoint", "halt",
    ]
    assert "command_preview" in nt

    # Terminal escalation.
    t = terminal[0]
    assert t["reason"] == "silent_child_command"
    assert t["elapsed_s"] == 300.0
    assert t["process_group"] == 9090
    assert [a["action"] for a in t["recovery_actions"]] == [
        "interrupt", "resume_from_checkpoint", "halt",
    ]


def test_no_stall_events_emits_no_command_stalled(tmp_path: Path) -> None:
    """Byte-identity guard: a clean run carries no command_stalled record."""
    run_dir = _write_run(
        tmp_path / "run",
        meta={"status": "done", "task": "demo", "profile": "feature"},
        events=[
            _event(1, "run.start", {"task": "demo", "run_kind": "single_project",
                                    "project": "/p", "profile": "feature"}),
            _event(2, "run.end", {"status": "done"}),
        ],
    )
    bundle = collect_evidence(run_dir)
    validate_bundle(bundle)
    assert not any(e.get("kind") == "command_stalled" for e in bundle["errors"])


def test_event_payload_carries_recovery_actions() -> None:
    """F3: the neutral event payload embeds the shared recovery verb set, so a
    generic event consumer sees the recovery contract at write-through time."""
    payload = _non_terminal_stall_payload()
    assert [a["action"] for a in payload["recovery_actions"]] == [
        "interrupt", "resume_from_checkpoint", "halt",
    ]


def test_recovery_actions_fallback_when_payload_omits_them(tmp_path: Path) -> None:
    """The collector fills recovery_actions from the shared builder for a legacy
    event whose payload predates the embedded recovery contract."""
    legacy_payload = {
        "phase": "implement",
        "reason": "unsafe_process_polling",
        "elapsed_s": 8.0,
        "terminal": False,
        # No ``recovery_actions`` key — a pre-F3 event.
    }
    run_dir = _write_run(
        tmp_path / "run",
        meta={"status": "running", "task": "demo", "profile": "feature"},
        events=[
            _event(1, "agent.command_stalled", legacy_payload, phase="IMPLEMENT"),
        ],
    )
    bundle = collect_evidence(run_dir)
    validate_bundle(bundle)
    stall = next(e for e in bundle["errors"] if e["kind"] == "command_stalled")
    assert [a["action"] for a in stall["recovery_actions"]] == [
        "interrupt", "resume_from_checkpoint", "halt",
    ]  # non-empty, builder-filled


def test_schema_rejects_malformed_command_stalled_record() -> None:
    """A command_stalled record missing required keys fails validation."""
    bad = {
        "schema_version": "1",
        "run_id": "r", "run_dir": "/d", "status": "failed",
        "created_at": "t", "task": "x", "profile": "feature",
        "plan": {
            "source": "absent", "short_summary": "", "planning_context": "",
            "subtask_count": 0, "has_contract": False, "goal": None,
            "acceptance_criteria": [], "owned_files": [], "commands_to_run": [],
            "risks": [], "review_focus": [], "mcp_context": {},
        },
        "phases": [], "gates": [], "commands": [], "artifacts": [],
        "metrics": {
            "total_tokens": 0, "total_tokens_in": 0, "total_tokens_out": 0,
            "total_duration_s": 0.0, "total_rounds": 0,
        },
        "errors": [{"kind": "command_stalled", "phase": "implement"}],  # missing keys
        "prompt_render": [],
        "raw_events_path": "events.jsonl",
    }
    try:
        validate_bundle(bad)
    except EvidenceSchemaError as exc:
        assert "command_stalled" in str(exc)
    else:  # pragma: no cover - guard
        raise AssertionError("expected EvidenceSchemaError for malformed record")
