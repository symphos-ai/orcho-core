# SPDX-License-Identifier: Apache-2.0
"""Provider/runtime failure projection through evidence + SDK (ADR 0118, T2).

Pins the two halves of the projection wired in T2:

* the collector's ``errors`` slice
  (:func:`pipeline.evidence.collector._build_errors`) now copies
  ``provider_message`` out of the ``run.end`` payload, alongside the existing
  ``failure_kind`` family;
* the typed SDK projection
  (:func:`sdk.evidence_slices.get_errors_halt`) populates
  ``ErrorsAndHalt.provider_runtime`` with a :class:`ProviderRuntimeFailure` for
  a ``provider_runtime`` failure, and leaves it ``None`` (while still populating
  ``recovery``) for a ``provider_access`` failure — the two never overlap.
"""

from __future__ import annotations

import json
from pathlib import Path

from sdk import (
    ErrorsAndHalt,
    ProviderAccessRecovery,
    get_errors_halt,
)
from sdk.evidence_slices import ProviderRuntimeFailure


def _seed_run(
    runs_dir: Path,
    run_id: str,
    *,
    meta: dict,
    events: list[dict] | None = None,
) -> Path:
    """Minimal run dir with meta.json + events.jsonl the slices can read."""
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8",
    )
    lines = "".join(json.dumps(e) + "\n" for e in (events or []))
    (run_dir / "events.jsonl").write_text(lines, encoding="utf-8")
    return run_dir


def _provider_runtime_failure() -> dict:
    return {
        "phase": "implement",
        "error": "Agent call failed: exit=1",
        "type": "RateLimitError",
        "failure_kind": "provider_runtime",
        "recoverable": True,
        "recommended_action": "resume_or_retry_phase",
        "failed_phase": "implement",
        "runtime": "claude",
        "model": "claude-opus-4-8",
        "provider_message": "usage limit reached for this session, try again in 5h",
    }


def _run_end_event(failure: dict) -> dict:
    """A ``run.end`` event carrying the same failure_meta the run spreads."""
    return {
        "seq": 1,
        "ts": "2026-06-29T00:00:00.000",
        "kind": "run.end",
        "phase": "implement",
        "payload": {
            "status": "failed",
            "phase": failure["failed_phase"],
            "error_type": failure["type"],
            "error": failure["error"],
            "failure_kind": failure["failure_kind"],
            "recoverable": failure["recoverable"],
            "recommended_action": failure["recommended_action"],
            "failed_phase": failure["failed_phase"],
            "runtime": failure["runtime"],
            "model": failure["model"],
            "provider_message": failure["provider_message"],
        },
    }


# ── collector errors slice ────────────────────────────────────────────────────


def test_errors_slice_surfaces_provider_message(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    failure = _provider_runtime_failure()
    _seed_run(
        runs,
        "20260629_100000_aaaaaa",
        meta={
            "task": "provider runtime demo",
            "status": "failed",
            "phases": {},
            "failure": failure,
        },
        events=[_run_end_event(failure)],
    )

    info = get_errors_halt("20260629_100000_aaaaaa", runs_dir=runs, cwd=None)
    run_failed = [e for e in info.errors if e.get("kind") == "run_failed"]
    assert run_failed, "expected a run_failed error breadcrumb"
    rec = run_failed[0]
    assert rec["failure_kind"] == "provider_runtime"
    assert rec["recoverable"] is True
    assert rec["recommended_action"] == "resume_or_retry_phase"
    assert rec["provider_message"] == failure["provider_message"]


# ── typed SDK projection ──────────────────────────────────────────────────────


def test_get_errors_halt_projects_provider_runtime(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    failure = _provider_runtime_failure()
    _seed_run(
        runs,
        "20260629_110000_bbbbbb",
        meta={
            "task": "provider runtime demo",
            "status": "failed",
            "phases": {},
            "failure": failure,
        },
        events=[_run_end_event(failure)],
    )

    info = get_errors_halt("20260629_110000_bbbbbb", runs_dir=runs, cwd=None)
    assert isinstance(info, ErrorsAndHalt)
    assert isinstance(info.provider_runtime, ProviderRuntimeFailure)
    pr = info.provider_runtime
    assert pr.failure_kind == "provider_runtime"
    assert pr.recoverable is True
    assert pr.recommended_action == "resume_or_retry_phase"
    assert pr.failed_phase == "implement"
    assert pr.runtime == "claude"
    assert pr.model == "claude-opus-4-8"
    assert pr.provider_message == failure["provider_message"]
    # provider_runtime and provider_access are mutually exclusive.
    assert info.recovery is None


def test_provider_runtime_message_absent_degrades_to_empty(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    failure = _provider_runtime_failure()
    del failure["provider_message"]  # nothing readable was captured
    _seed_run(
        runs,
        "20260629_120000_cccccc",
        meta={
            "task": "provider runtime no message",
            "status": "failed",
            "phases": {},
            "failure": failure,
        },
    )
    info = get_errors_halt("20260629_120000_cccccc", runs_dir=runs, cwd=None)
    assert isinstance(info.provider_runtime, ProviderRuntimeFailure)
    assert info.provider_runtime.provider_message == ""


# ── provider_access regression ────────────────────────────────────────────────


def test_provider_access_still_projects_recovery_not_runtime(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    _seed_run(
        runs,
        "20260629_130000_dddddd",
        meta={
            "task": "provider access demo",
            "status": "failed",
            "phases": {},
            "failure": {
                "phase": "plan",
                "failure_kind": "provider_access",
                "recoverable": False,
                "recommended_action": "switch_runtime_or_restore_access",
                "failed_phase": "plan",
                "runtime": "claude",
                "model": "claude-opus-4-8",
                "recovery_actions": [
                    {"action": "retry"},
                    {"action": "halt"},
                    {"action": "replace", "runtime": "codex", "model": "gpt-5.5"},
                ],
            },
        },
    )
    info = get_errors_halt("20260629_130000_dddddd", runs_dir=runs, cwd=None)
    assert isinstance(info.recovery, ProviderAccessRecovery)
    assert info.recovery.failure_kind == "provider_access"
    assert info.provider_runtime is None


def test_non_provider_failure_has_no_runtime_projection(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    _seed_run(
        runs,
        "20260629_140000_eeeeee",
        meta={
            "task": "plain failure",
            "status": "failed",
            "phases": {},
            "failure": {"phase": "implement", "type": "RuntimeError"},
        },
    )
    info = get_errors_halt("20260629_140000_eeeeee", runs_dir=runs, cwd=None)
    assert info.provider_runtime is None
    assert info.recovery is None
