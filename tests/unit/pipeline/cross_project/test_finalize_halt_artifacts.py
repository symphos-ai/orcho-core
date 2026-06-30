"""Unit tests for ADR 0037 — cross-project halt artifacts.

Exercises :func:`finalize_cross_terminal` directly. The full
``run_cross_pipeline`` is integration territory; here we lock the
invariant: every non-``done`` terminal cross-run carries
``meta.halt_reason`` and ``evidence.json`` next to ``meta.json``.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.cross_project.terminal import (
    finalize_cross_terminal as _finalize_cross_terminal,
)


def _read_meta(run_dir: Path) -> dict:
    return json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))


def test_done_status_writes_meta_without_halt_reason(tmp_path: Path) -> None:
    session: dict = {"run_id": "r1", "phases": {}}
    _finalize_cross_terminal(run_dir=tmp_path, session=session, status="done")

    meta = _read_meta(tmp_path)
    assert meta["status"] == "done"
    assert meta.get("halt_reason") is None
    assert (tmp_path / "evidence.json").is_file()


def test_failed_status_stamps_halt_reason_when_caller_omits(tmp_path: Path) -> None:
    session: dict = {"run_id": "r2", "phases": {}}
    _finalize_cross_terminal(run_dir=tmp_path, session=session, status="failed")

    meta = _read_meta(tmp_path)
    assert meta["status"] == "failed"
    assert meta["halt_reason"] == "cross_failed"
    assert (tmp_path / "evidence.json").is_file()


def test_explicit_halt_reason_takes_precedence(tmp_path: Path) -> None:
    session: dict = {"run_id": "r3", "phases": {}}
    _finalize_cross_terminal(
        run_dir=tmp_path,
        session=session,
        status="cancelled",
        halt_reason="cross_gate_aborted:contract_check",
    )

    meta = _read_meta(tmp_path)
    assert meta["status"] == "cancelled"
    assert meta["halt_reason"] == "cross_gate_aborted:contract_check"


def test_caller_preset_halt_reason_not_overwritten(tmp_path: Path) -> None:
    """If the caller already populated ``session['halt_reason']``,
    the helper must respect it (taxonomy might be richer than the
    helper's generic fallback)."""
    session: dict = {
        "run_id": "r4",
        "phases": {},
        "halt_reason": "cross_final_acceptance_parse_error",
    }
    _finalize_cross_terminal(run_dir=tmp_path, session=session, status="failed")

    meta = _read_meta(tmp_path)
    assert meta["halt_reason"] == "cross_final_acceptance_parse_error"


def test_evidence_bundle_lands_with_terminal_status(tmp_path: Path) -> None:
    """The ADR 0037 invariant is that ``evidence.json`` exists next
    to ``meta.json`` after every cross terminal — the schema version
    is downstream of the collector. Today the collector handles the
    empty cross-parent dir and emits a full v1 bundle (mostly empty
    slots); when the collector tightens or fails, the writer falls
    back to the ``schema_version="0-placeholder"`` stub. Both shapes
    satisfy the invariant, so we lock the file's existence + status
    rather than the schema string."""
    session: dict = {"run_id": "r5", "phases": {}}
    _finalize_cross_terminal(run_dir=tmp_path, session=session, status="failed")

    bundle_path = tmp_path / "evidence.json"
    assert bundle_path.is_file()
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert bundle["status"] == "failed"
    assert bundle["schema_version"] in {"1", "0-placeholder"}


def test_run_dir_none_mutates_session_without_writing(tmp_path: Path) -> None:
    """In-memory finalize (no output dir) still satisfies the
    halt_reason invariant on the session dict — useful for callers
    that build sessions for tests / dry-runs."""
    session: dict = {"run_id": "r6", "phases": {}}
    _finalize_cross_terminal(run_dir=None, session=session, status="failed")

    assert session["status"] == "failed"
    assert session["halt_reason"] == "cross_failed"
    assert not (tmp_path / "meta.json").is_file()


def test_run_id_falls_back_to_dir_name_when_session_missing(tmp_path: Path) -> None:
    session: dict = {"phases": {}}
    _finalize_cross_terminal(run_dir=tmp_path, session=session, status="cancelled")

    bundle = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))
    assert bundle["run_id"] == tmp_path.name
