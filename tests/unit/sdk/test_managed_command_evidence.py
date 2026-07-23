from __future__ import annotations

import json
from pathlib import Path

from agents.managed_command import ManagedCommandIdentity, ManagedCommandStore
from sdk.evidence_slices import list_commands


def test_list_commands_projects_managed_receipt_as_typed_bounded_record(
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    checkout = tmp_path / "checkout"
    run_dir.mkdir(parents=True)
    checkout.mkdir()
    (run_dir / "meta.json").write_text(
        json.dumps({"run_id": "run-1", "status": "done"}),
        encoding="utf-8",
    )
    (run_dir / "events.jsonl").write_text("", encoding="utf-8")
    identity = ManagedCommandIdentity.build(
        run_dir=run_dir,
        phase="repair_changes",
        cwd=checkout,
        argv=("/usr/bin/python3", "-c", "sentinel-sdk-secret"),
    )
    store = ManagedCommandStore(run_dir)
    store.settle(store.admit(identity), exit_code=0)

    records = list_commands("run-1", runs_dir=runs_dir, cwd=None)

    assert len(records) == 1
    record = records[0]
    assert record.source == "managed"
    assert record.identity_digest == identity.key
    assert record.phase == "repair_changes"
    assert record.state == "exited"
    assert record.exit_code == 0
    assert record.argv_summary == "python3"
    assert record.executable == "python3"
    assert record.artifact_path is not None
    assert record.artifact_path.startswith("managed_commands/receipts/")
    assert "sentinel-sdk-secret" not in repr(record)
