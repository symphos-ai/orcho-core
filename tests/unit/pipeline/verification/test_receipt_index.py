from __future__ import annotations

import json
import subprocess
from pathlib import Path

from pipeline.evidence.verification_receipt import write_command_receipt
from pipeline.verification_contract import PlaceholderContext, VerificationContract
from pipeline.verification_readiness import classify_required_receipts
from pipeline.verification_receipt_index import (
    VERIFICATION_PARENT_RUNS_EXTRAS_KEY,
    ReceiptSource,
    coerce_receipt_sources,
    parent_sources_from_extras,
    receipt_file_path,
)
from pipeline.verification_subject import VerificationSubjectAvailable, capture_verification_subject


def _repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "x@test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "x"], cwd=path, check=True)
    (path / "f").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)


def _contract() -> VerificationContract:
    return VerificationContract({}, {}, {"test": {"run": "true"}}, (), "", ("test",), "")


def _wire(identity) -> dict:
    return {
        "status": "available",
        "identity": {
            "version": identity.version,
            "object_format": identity.object_format,
            "tree_oid": identity.tree_oid,
            "observed_head_oid": identity.observed_head_oid,
            "baseline_oid": identity.baseline_oid,
        },
    }


def _write(run: Path, identity, *, exit_code: int = 0) -> None:
    target = run / "verification_command_receipts"
    target.mkdir(exist_ok=True)
    payload = {
        "schema_version": 3,
        "command": "test",
        "exit_code": exit_code,
        "assertions": [],
        "detail": "",
        "subject": _wire(identity),
        "dependencies": [],
    }
    (target / "test.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_unavailable_failure(run: Path) -> None:
    target = run / "verification_command_receipts"
    target.mkdir(exist_ok=True)
    payload = {
        "schema_version": 3,
        "command": "test",
        "exit_code": 1,
        "assertions": [],
        "detail": "subject unavailable",
        "subject": {"status": "unavailable", "reason": "dirty_submodule_unrepresentable"},
        "dependencies": [],
    }
    (target / "test.json").write_text(json.dumps(payload), encoding="utf-8")


class TestSources:
    def test_coerce_receipt_sources_and_parent_extras(self) -> None:
        assert coerce_receipt_sources([
            ("r1", "/run/1"), ReceiptSource("r2", "/run/2"),
            {"run_id": "r3", "run_dir": "/run/3"}, ("bad",), "junk",
        ]) == (
            ReceiptSource("r1", "/run/1"), ReceiptSource("r2", "/run/2"),
            ReceiptSource("r3", "/run/3"),
        )
        assert parent_sources_from_extras(
            {VERIFICATION_PARENT_RUNS_EXTRAS_KEY: [("parent", "/run/p")]},
        ) == (ReceiptSource("parent", "/run/p"),)

    def test_receipt_file_path_matches_schema_v3_writer_layout(self, tmp_path: Path) -> None:
        written = write_command_receipt(output_dir=tmp_path, result={
            "command": "test", "exit_code": 0, "assertions": [], "detail": "",
            "subject": {"status": "unavailable", "reason": "fixture"},
            "dependencies": [],
        })
        assert written is not None
        assert receipt_file_path(tmp_path, "test") == str(written)


def test_parent_receipt_inherits_only_for_equal_usable_subject(tmp_path: Path) -> None:
    checkout, child, parent = tmp_path / "co", tmp_path / "child", tmp_path / "parent"
    _repo(checkout)
    child.mkdir()
    parent.mkdir()
    capture = capture_verification_subject(checkout)
    assert isinstance(capture, VerificationSubjectAvailable)
    _write(parent, capture.identity)
    cls = classify_required_receipts(_contract(), child, PlaceholderContext(checkout=str(checkout)), checkout=str(checkout), parent_runs=[("parent", str(parent))])["test"]
    assert cls.status == "present"
    assert cls.source_run_id == "parent"
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "new"], cwd=checkout, check=True)
    stale = classify_required_receipts(_contract(), child, PlaceholderContext(checkout=str(checkout)), checkout=str(checkout), parent_runs=[("parent", str(parent))])["test"]
    assert stale.status == "stale"


def test_current_same_subject_failure_blocks_parent_pass(tmp_path: Path) -> None:
    checkout, child, parent = tmp_path / "co", tmp_path / "child", tmp_path / "parent"
    _repo(checkout)
    child.mkdir()
    parent.mkdir()
    capture = capture_verification_subject(checkout)
    assert isinstance(capture, VerificationSubjectAvailable)
    _write(child, capture.identity, exit_code=1)
    _write(parent, capture.identity)
    cls = classify_required_receipts(_contract(), child, PlaceholderContext(checkout=str(checkout)), checkout=str(checkout), parent_runs=[("parent", str(parent))])["test"]
    assert cls.status == "failed"


def test_failed_receipt_for_stale_subject_yields_to_current_parent_pass(tmp_path: Path) -> None:
    checkout, child, parent = tmp_path / "co", tmp_path / "child", tmp_path / "parent"
    _repo(checkout)
    child.mkdir()
    parent.mkdir()
    old_capture = capture_verification_subject(checkout)
    assert isinstance(old_capture, VerificationSubjectAvailable)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "new"], cwd=checkout, check=True)
    current_capture = capture_verification_subject(checkout)
    assert isinstance(current_capture, VerificationSubjectAvailable)
    _write(child, old_capture.identity, exit_code=1)
    _write(parent, current_capture.identity)

    cls = classify_required_receipts(
        _contract(),
        child,
        PlaceholderContext(checkout=str(checkout)),
        checkout=str(checkout),
        parent_runs=[("parent", str(parent))],
    )["test"]

    assert cls.status == "present"
    assert cls.source_run_id == "parent"


def test_current_unavailable_subject_failure_blocks_parent_pass(tmp_path: Path) -> None:
    checkout, child, parent = tmp_path / "co", tmp_path / "child", tmp_path / "parent"
    _repo(checkout)
    child.mkdir()
    parent.mkdir()
    capture = capture_verification_subject(checkout)
    assert isinstance(capture, VerificationSubjectAvailable)
    _write_unavailable_failure(child)
    _write(parent, capture.identity)

    cls = classify_required_receipts(
        _contract(),
        child,
        PlaceholderContext(checkout=str(checkout)),
        checkout=str(checkout),
        parent_runs=[("parent", str(parent))],
    )["test"]

    assert cls.status in {"failed", "unverifiable"}
    assert cls.source_run_id != "parent"


def test_first_matching_parent_is_selected_in_search_order(tmp_path: Path) -> None:
    checkout = tmp_path / "co"
    child, near, far = tmp_path / "child", tmp_path / "near", tmp_path / "far"
    _repo(checkout)
    child.mkdir()
    near.mkdir()
    far.mkdir()
    capture = capture_verification_subject(checkout)
    assert isinstance(capture, VerificationSubjectAvailable)
    _write(near, capture.identity)
    _write(far, capture.identity)

    cls = classify_required_receipts(
        _contract(),
        child,
        PlaceholderContext(checkout=str(checkout)),
        checkout=str(checkout),
        parent_runs=[("near", str(near)), ("far", str(far))],
    )["test"]

    assert cls.status == "present"
    assert cls.source_run_id == "near"
