# SPDX-License-Identifier: Apache-2.0
"""Collector breadcrumb for Stage 6 delivery-gate waivers (T4).

``run.py`` persists one record per required gate excused by an exact durable
``phase_handoff_waiver`` under the NON-wire ``meta.commit_delivery_verification_waived``
key. The collector must surface a distinct ``verification_gate_waived`` error
breadcrumb per record (command / gate_name / handoff_id / waiver_preview /
status) so the persisted ``evidence.json`` carries durable delivery evidence
separate from the SDK/MCP wire.
"""

from __future__ import annotations

import json
from pathlib import Path

from pipeline.evidence import collect_evidence


def _write_meta(target: Path, meta: dict) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    target.joinpath("meta.json").write_text(json.dumps(meta), encoding="utf-8")
    target.joinpath("events.jsonl").write_text("", encoding="utf-8")
    return target


def test_waived_gate_breadcrumb_has_four_fields(tmp_path: Path) -> None:
    run_dir = _write_meta(
        tmp_path / "run",
        {
            "status": "done",
            "commit_delivery_verification_waived": [
                {
                    "command": "broad-non-e2e",
                    "gate_name": "broad-non-e2e",
                    "handoff_id": "gate:broad-non-e2e:1",
                    "waiver_preview": "accepted: pre-existing failure on this checkout",
                    "status": "failed",
                },
            ],
        },
    )

    bundle = collect_evidence(run_dir)

    waived = [
        e for e in bundle["errors"] if e.get("kind") == "verification_gate_waived"
    ]
    assert len(waived) == 1
    entry = waived[0]
    assert entry["command"] == "broad-non-e2e"
    assert entry["gate_name"] == "broad-non-e2e"
    assert entry["handoff_id"] == "gate:broad-non-e2e:1"
    assert entry["waiver_preview"] == (
        "accepted: pre-existing failure on this checkout"
    )
    assert entry["status"] == "failed"


def test_multiple_waived_gates_each_breadcrumb(tmp_path: Path) -> None:
    run_dir = _write_meta(
        tmp_path / "run",
        {
            "status": "done",
            "commit_delivery_verification_waived": [
                {
                    "command": "broad-non-e2e",
                    "gate_name": "broad-non-e2e",
                    "handoff_id": "gate:broad-non-e2e:1",
                    "waiver_preview": "accepted A",
                    "status": "failed",
                },
                {
                    "command": "smoke",
                    "gate_name": "smoke",
                    "handoff_id": "gate:smoke:2",
                    "waiver_preview": "accepted B",
                    "status": "missing",
                },
            ],
        },
    )

    bundle = collect_evidence(run_dir)
    waived = [
        e for e in bundle["errors"] if e.get("kind") == "verification_gate_waived"
    ]
    assert [e["command"] for e in waived] == ["broad-non-e2e", "smoke"]
    assert [e["status"] for e in waived] == ["failed", "missing"]


def test_no_waived_key_emits_no_breadcrumb(tmp_path: Path) -> None:
    # Byte-identity guard: a run without the key produces no waived breadcrumb.
    run_dir = _write_meta(tmp_path / "run", {"status": "done"})
    bundle = collect_evidence(run_dir)
    assert not any(
        e.get("kind") == "verification_gate_waived" for e in bundle["errors"]
    )


def test_malformed_waived_entries_are_skipped(tmp_path: Path) -> None:
    run_dir = _write_meta(
        tmp_path / "run",
        {
            "status": "done",
            "commit_delivery_verification_waived": [
                "not-a-dict",
                {"handoff_id": "gate:x:1"},  # no command/gate_name → skipped
                {
                    "command": "lint",
                    "handoff_id": "gate:lint:1",
                    "waiver_preview": "ok",
                    "status": "failed",
                },
            ],
        },
    )
    bundle = collect_evidence(run_dir)
    waived = [
        e for e in bundle["errors"] if e.get("kind") == "verification_gate_waived"
    ]
    assert [e["command"] for e in waived] == ["lint"]
    # gate_name falls back to command when not provided.
    assert waived[0]["gate_name"] == "lint"
