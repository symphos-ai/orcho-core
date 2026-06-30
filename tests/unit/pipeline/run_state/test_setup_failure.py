# SPDX-License-Identifier: Apache-2.0
"""Unit tests for :mod:`pipeline.run_state.setup_failure` (ADR 0104).

Covers the two responsibilities of the module:

* the **status/halt-reason merge rule** (``merged_status`` /
  ``merged_halt_reason`` / ``supervisor_terminal_status`` /
  ``supervisor_halt_reason``) — including the three mandatory merge cases that
  pin idempotency with the launcher integration's ``merged_status_from_meta``:

    (i)   terminal ``meta.status='failed'`` + supervisor ``exit_code<0`` →
          ``'failed'`` (terminal meta WINS, NO remap to interrupted);
    (ii)  empty/``running`` meta + supervisor ``failed`` + ``exit_code<0`` →
          ``'interrupted'`` (signal-reaped remap, launcher branch only);
    (iii) same as (ii) but ``exit_code>0`` → ``'failed'``.

* the **gated setup/preflight synthesis** (``detect_setup_preflight_failure``)
  — fires for a genuine pre-phase death and stays silent (``None``) whenever a
  richer terminal cause is already on record (phase attempts, ``meta.failure``,
  a ``run.end`` error/halt event, an active ``phase_handoff``) or there is no
  concrete setup signal.

Where the launcher integration (``orcho_mcp.services.status_merge``) is
importable, its ``merged_status_from_meta`` is used as a parity ORACLE; when it
is absent the expected value is still asserted directly and the parity intent is
pinned by comment.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.run_state.setup_failure import (
    SETUP_FAILURE_KIND,
    detect_setup_preflight_failure,
    merged_halt_reason,
    merged_status,
    supervisor_halt_reason,
    supervisor_terminal_status,
)

# Optional parity oracle: the launcher integration that owns the *other* copy
# of the merge rule. Imported "по возможности" — when orcho-mcp is not installed
# the tests still assert the expected value directly (parity pinned by comment).
try:  # pragma: no cover - availability depends on workspace layout
    from orcho_mcp.services.status_merge import (  # type: ignore[import-not-found]
        merged_status_from_meta as _oracle_merged_status,
    )

    _HAS_ORACLE = True
except Exception:  # pragma: no cover - oracle simply unavailable
    _oracle_merged_status = None  # type: ignore[assignment]
    _HAS_ORACLE = False


def _run_dir(
    tmp_path: Path,
    *,
    meta: dict | None = None,
    supervisor: dict | None = None,
    runner_log: bool = False,
) -> Path:
    """Write a synthetic run dir with the durable files the projection reads.

    Thin local helper (not a shared fixture): writes ``meta.json`` and,
    optionally, the launcher state file and a ``runner.log``. No events file is
    written; callers pass ``events`` to :func:`detect_setup_preflight_failure`
    directly as plain dicts.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "meta.json").write_text(json.dumps(meta or {}), encoding="utf-8")
    if supervisor is not None:
        (run_dir / "mcp_supervisor.json").write_text(
            json.dumps(supervisor), encoding="utf-8",
        )
    if runner_log:
        (run_dir / "runner.log").write_text(
            "fatal: setup command failed\n", encoding="utf-8",
        )
    return run_dir


def _assert_parity(meta: dict, run_dir: Path, expected: str | None) -> None:
    """Assert merged_status == expected and, when available, == the oracle."""
    got = merged_status(dict(meta), run_dir)
    assert got == expected
    if _HAS_ORACLE:
        # Idempotency with the launcher integration: byte-identical resolution
        # on the same inputs.
        assert _oracle_merged_status(dict(meta), run_dir) == got


# ── supervisor_terminal_status ──────────────────────────────────────────────


class TestSupervisorTerminalStatus:
    def test_absent_file_returns_none(self, tmp_path: Path) -> None:
        run_dir = _run_dir(tmp_path, meta={"status": "running"})
        assert supervisor_terminal_status(run_dir) is None

    def test_running_supervisor_returns_none(self, tmp_path: Path) -> None:
        run_dir = _run_dir(tmp_path, supervisor={"status": "running"})
        assert supervisor_terminal_status(run_dir) is None

    def test_unreadable_file_returns_none(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "mcp_supervisor.json").write_text("{not json", encoding="utf-8")
        assert supervisor_terminal_status(run_dir) is None

    def test_terminal_status_passthrough(self, tmp_path: Path) -> None:
        run_dir = _run_dir(tmp_path, supervisor={"status": "done"})
        assert supervisor_terminal_status(run_dir) == "done"

    def test_failed_negative_exit_remaps_to_interrupted(
        self, tmp_path: Path,
    ) -> None:
        run_dir = _run_dir(
            tmp_path, supervisor={"status": "failed", "exit_code": -9},
        )
        assert supervisor_terminal_status(run_dir) == "interrupted"

    def test_failed_positive_exit_stays_failed(self, tmp_path: Path) -> None:
        run_dir = _run_dir(
            tmp_path, supervisor={"status": "failed", "exit_code": 1},
        )
        assert supervisor_terminal_status(run_dir) == "failed"

    def test_failed_no_exit_code_stays_failed(self, tmp_path: Path) -> None:
        run_dir = _run_dir(tmp_path, supervisor={"status": "failed"})
        assert supervisor_terminal_status(run_dir) == "failed"


# ── merged_status — the three mandatory cases + idempotency ─────────────────


class TestMergedStatusMandatoryCases:
    def test_case_i_terminal_meta_failed_wins_over_negative_exit(
        self, tmp_path: Path,
    ) -> None:
        """(i) meta.status='failed' + supervisor exit_code<0 → 'failed'.

        Terminal meta WINS; the launcher is not consulted, so there is NO remap
        to 'interrupted' — this is the no-status-divergence invariant.
        """
        meta = {"status": "failed", "phases": {}}
        run_dir = _run_dir(
            tmp_path, meta=meta,
            supervisor={"status": "interrupted", "exit_code": -9},
        )
        _assert_parity(meta, run_dir, "failed")

    def test_case_ii_empty_meta_supervisor_negative_exit_interrupted(
        self, tmp_path: Path,
    ) -> None:
        """(ii) empty meta.status + supervisor failed/exit<0 → 'interrupted'."""
        meta = {"status": "", "phases": {}}
        run_dir = _run_dir(
            tmp_path, meta=meta,
            supervisor={"status": "failed", "exit_code": -15},
        )
        _assert_parity(meta, run_dir, "interrupted")

    def test_case_ii_running_meta_supervisor_negative_exit_interrupted(
        self, tmp_path: Path,
    ) -> None:
        """(ii) 'running' meta.status defers to the launcher → 'interrupted'."""
        meta = {"status": "running", "phases": {}}
        run_dir = _run_dir(
            tmp_path, meta=meta,
            supervisor={"status": "failed", "exit_code": -9},
        )
        _assert_parity(meta, run_dir, "interrupted")

    def test_case_iii_empty_meta_supervisor_positive_exit_failed(
        self, tmp_path: Path,
    ) -> None:
        """(iii) empty meta.status + supervisor failed/exit>0 → 'failed'."""
        meta = {"status": "", "phases": {}}
        run_dir = _run_dir(
            tmp_path, meta=meta,
            supervisor={"status": "failed", "exit_code": 2},
        )
        _assert_parity(meta, run_dir, "failed")

    def test_terminal_meta_wins_with_no_supervisor(self, tmp_path: Path) -> None:
        meta = {"status": "halted"}
        run_dir = _run_dir(tmp_path, meta=meta)
        _assert_parity(meta, run_dir, "halted")

    def test_running_meta_no_supervisor_surfaces_running(
        self, tmp_path: Path,
    ) -> None:
        meta = {"status": "running"}
        run_dir = _run_dir(tmp_path, meta=meta)
        _assert_parity(meta, run_dir, "running")

    def test_empty_everything_is_none(self, tmp_path: Path) -> None:
        meta: dict = {}
        run_dir = _run_dir(tmp_path, meta=meta)
        _assert_parity(meta, run_dir, None)


# ── merged_halt_reason ──────────────────────────────────────────────────────


class TestMergedHaltReason:
    def test_meta_reason_wins(self, tmp_path: Path) -> None:
        meta = {"status": "halted", "halt_reason": "worktree_bootstrap_failed"}
        run_dir = _run_dir(
            tmp_path, meta=meta,
            supervisor={"halt_reason": "signal:SIGKILL"},
        )
        assert merged_halt_reason(meta, run_dir) == "worktree_bootstrap_failed"

    def test_supervisor_reason_fills_in(self, tmp_path: Path) -> None:
        meta = {"status": ""}
        run_dir = _run_dir(
            tmp_path, meta=meta,
            supervisor={"status": "failed", "halt_reason": "abnormal_exit:1"},
        )
        assert merged_halt_reason(meta, run_dir) == "abnormal_exit:1"

    def test_no_reason_anywhere_is_none(self, tmp_path: Path) -> None:
        meta = {"status": "failed"}
        run_dir = _run_dir(tmp_path, meta=meta)
        assert merged_halt_reason(meta, run_dir) is None
        assert supervisor_halt_reason(run_dir) is None


# ── detect_setup_preflight_failure — synthesis + non-interference gates ─────


class TestDetectSetupPreflightFailure:
    def test_worktree_bootstrap_halt_synthesizes_actionable_record(
        self, tmp_path: Path,
    ) -> None:
        meta = {
            "status": "halted",
            "halt_reason": "worktree_bootstrap_failed",
            "halted_at": "2026-06-25T09:00:00+00:00",
            "phases": {},
            "worktree_bootstrap": {
                "status": "failed", "error": "git checkout exploded",
            },
        }
        run_dir = _run_dir(tmp_path, meta=meta, runner_log=True)
        rec = detect_setup_preflight_failure(meta, run_dir, [])
        assert rec is not None
        assert rec["kind"] == SETUP_FAILURE_KIND
        # Names the actionable cause + the runner.log pointer.
        assert "worktree_bootstrap_failed" in rec["message"]
        assert "git checkout exploded" in rec["message"]
        assert "runner.log" in rec["message"]
        assert rec["halt_reason"] == "worktree_bootstrap_failed"
        assert rec["at"] == "2026-06-25T09:00:00+00:00"
        assert rec["runtime_log_hint"] == "runner.log"

    def test_supervisor_abnormal_exit_synthesizes_record(
        self, tmp_path: Path,
    ) -> None:
        meta = {"status": "", "phases": {}}
        run_dir = _run_dir(
            tmp_path, meta=meta, runner_log=True,
            supervisor={
                "status": "failed", "exit_code": -9,
                "halt_reason": "signal:SIGKILL",
            },
        )
        rec = detect_setup_preflight_failure(meta, run_dir, [])
        assert rec is not None
        assert rec["kind"] == SETUP_FAILURE_KIND
        assert rec["halt_reason"] == "signal:SIGKILL"
        assert "signal:SIGKILL" in rec["message"]
        assert "runner.log" in rec["message"]

    # ── non-interference gates ──────────────────────────────────────────────

    def test_phase_attempts_present_returns_none(self, tmp_path: Path) -> None:
        meta = {
            "status": "failed",
            "phases": {"plan": [{"attempt": 1}]},
            "worktree_bootstrap": {"status": "failed", "error": "x"},
        }
        run_dir = _run_dir(tmp_path, meta=meta)
        assert detect_setup_preflight_failure(meta, run_dir, []) is None

    def test_release_summary_present_returns_none(self, tmp_path: Path) -> None:
        meta = {
            "status": "failed",
            "phases": {},
            "release_summary": {"ship_ready": False},
            "worktree_bootstrap": {"status": "failed", "error": "x"},
        }
        run_dir = _run_dir(tmp_path, meta=meta)
        assert detect_setup_preflight_failure(meta, run_dir, []) is None

    def test_meta_failure_present_returns_none(self, tmp_path: Path) -> None:
        """A provider-access / stalled-command failure already owns the surface."""
        meta = {
            "status": "failed",
            "phases": {},
            "failure": {"failure_kind": "provider_access"},
            "worktree_bootstrap": {"status": "failed", "error": "x"},
        }
        run_dir = _run_dir(tmp_path, meta=meta)
        assert detect_setup_preflight_failure(meta, run_dir, []) is None

    def test_active_phase_handoff_returns_none(self, tmp_path: Path) -> None:
        meta = {
            "status": "interrupted",
            "phases": {},
            "phase_handoff": {"id": "h1"},
            "worktree_bootstrap": {"status": "failed", "error": "x"},
        }
        run_dir = _run_dir(tmp_path, meta=meta)
        assert detect_setup_preflight_failure(meta, run_dir, []) is None

    def test_run_end_error_event_returns_none(self, tmp_path: Path) -> None:
        meta = {
            "status": "failed",
            "phases": {},
            "worktree_bootstrap": {"status": "failed", "error": "x"},
        }
        run_dir = _run_dir(tmp_path, meta=meta)
        events = [{"kind": "run.end", "payload": {"error": "boom"}}]
        assert detect_setup_preflight_failure(meta, run_dir, events) is None

    def test_run_end_halted_status_event_returns_none(
        self, tmp_path: Path,
    ) -> None:
        meta = {
            "status": "halted",
            "phases": {},
            "worktree_bootstrap": {"status": "failed", "error": "x"},
        }
        run_dir = _run_dir(tmp_path, meta=meta)
        events = [{"kind": "run.end", "payload": {"status": "halted"}}]
        assert detect_setup_preflight_failure(meta, run_dir, events) is None

    def test_bare_failed_run_end_without_error_still_synthesizes(
        self, tmp_path: Path,
    ) -> None:
        """A bare ``run.end`` ``{'status': 'failed'}`` with no ``error`` produces
        NO collector breadcrumb, so it must NOT suppress the synthesis — else the
        errors slice stays empty for the silent setup/preflight death."""
        meta = {"status": "failed", "phases": {}}
        run_dir = _run_dir(
            tmp_path, meta=meta, runner_log=True,
            supervisor={
                "status": "failed", "exit_code": 1,
                "halt_reason": "abnormal_exit:1",
            },
        )
        events = [{"kind": "run.end", "payload": {"status": "failed"}}]
        rec = detect_setup_preflight_failure(meta, run_dir, events)
        assert rec is not None
        assert rec["kind"] == SETUP_FAILURE_KIND
        assert rec["halt_reason"] == "abnormal_exit:1"
        assert "abnormal_exit:1" in rec["message"]
        assert "runner.log" in rec["message"]

    def test_bare_interrupted_run_end_without_error_still_synthesizes(
        self, tmp_path: Path,
    ) -> None:
        """A bare ``run.end`` ``{'status': 'interrupted'}`` likewise yields no
        collector breadcrumb, so the signal-induced synthesis still fires."""
        meta = {"status": "", "phases": {}}
        run_dir = _run_dir(
            tmp_path, meta=meta, runner_log=True,
            supervisor={
                "status": "failed", "exit_code": -15,
                "halt_reason": "signal:SIGTERM",
            },
        )
        events = [{"kind": "run.end", "payload": {"status": "interrupted"}}]
        rec = detect_setup_preflight_failure(meta, run_dir, events)
        assert rec is not None
        assert rec["halt_reason"] == "signal:SIGTERM"
        assert "signal:SIGTERM" in rec["message"]

    def test_benign_meta_halt_without_signal_returns_none(
        self, tmp_path: Path,
    ) -> None:
        """A clean operator/gate halt (``plan_rejected``) carries no setup signal —
        no worktree-bootstrap record, no launcher state — so it must NOT
        synthesize (Gate 4)."""
        meta = {"status": "halted", "halt_reason": "plan_rejected", "phases": {}}
        run_dir = _run_dir(tmp_path, meta=meta)
        assert detect_setup_preflight_failure(meta, run_dir, []) is None

    def test_done_status_returns_none(self, tmp_path: Path) -> None:
        meta = {"status": "done", "phases": {}}
        run_dir = _run_dir(
            tmp_path, meta=meta,
            supervisor={"status": "done"},
        )
        assert detect_setup_preflight_failure(meta, run_dir, []) is None
