"""SDK contract tests for the REA-4.3 evidence slice helpers.

Pin the typed projection: each slice over a synthetic run dir
returns the expected dataclass shape, severity filtering works,
empty inputs degrade to empty lists (not crashes), cross-run sub-run
listing surfaces all aliases.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdk import (
    ErrorsAndHalt,
    PlanSummary,
    ProviderAccessRecovery,
    SubtaskReceipt,
    get_errors_halt,
    get_plan_summary,
    list_evidence_artifacts,
    list_evidence_commands,
    list_findings,
    list_sub_runs,
    list_subtask_receipts,
    load_status,
)


def _write_launcher_state(run_dir: Path, supervisor: dict) -> None:
    """Thin local helper: write the optional launcher state file (ADR 0104).

    Provider-neutral file contract read by the terminal-state projection; not a
    shared fixture, so no conftest consumers are affected.
    """
    (run_dir / "mcp_supervisor.json").write_text(
        json.dumps(supervisor), encoding="utf-8",
    )


def _write_runner_log(run_dir: Path) -> None:
    """Thin local helper: write the conventional runtime log the synth points at."""
    (run_dir / "runner.log").write_text(
        "fatal: setup command failed\n", encoding="utf-8",
    )


def _seed_run(
    runs_dir: Path,
    run_id: str,
    *,
    meta: dict | None = None,
) -> Path:
    """Create a minimal run dir with meta.json + events.jsonl + metrics.json."""
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(
        json.dumps(meta or {}, indent=2) + "\n", encoding="utf-8",
    )
    # Empty events.jsonl + metrics.json keep the evidence collector happy.
    (run_dir / "events.jsonl").write_text("", encoding="utf-8")
    (run_dir / "metrics.json").write_text(
        json.dumps({
            "total_tokens": 0,
            "total_tokens_in": 0,
            "total_tokens_out": 0,
            "total_duration_s": 0.0,
            "total_rounds": 0,
        }) + "\n", encoding="utf-8",
    )
    return run_dir


# ── findings ────────────────────────────────────────────────────────────────


def test_list_findings_flattens_across_phase_attempts(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    _seed_run(runs, "20260510_220000_aaaaaa", meta={
        "task": "demo", "status": "done",
        "phases": {
            "validate_plan": [{
                "attempt": 1, "approved": False, "verdict": "REJECTED",
                "findings": [
                    {"id": "F1", "severity": "P0", "title": "Critical issue",
                     "body": "must fix"},
                    {"id": "F2", "severity": "P2", "title": "Minor",
                     "body": "nice to have"},
                ],
            }],
            "review_changes": [{
                "attempt": 1, "approved": True, "verdict": "APPROVED",
                "findings": [
                    {"id": "R1", "severity": "P1", "title": "Style nit",
                     "body": "code style", "file": "x.py", "line": 42},
                ],
            }],
        },
    })

    findings = list_findings(
        "20260510_220000_aaaaaa", runs_dir=runs, cwd=None,
    )
    assert len(findings) == 3
    by_id = {f.id: f for f in findings}
    assert by_id["F1"].phase == "validate_plan"
    assert by_id["F1"].severity == "P0"
    assert by_id["F2"].severity == "P2"
    assert by_id["R1"].phase == "review_changes"
    assert by_id["R1"].file == "x.py"
    assert by_id["R1"].line == 42


def test_list_findings_severity_min_filters_lower_severity(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    _seed_run(runs, "20260510_230000_bbbbbb", meta={
        "task": "demo", "status": "done",
        "phases": {
            "review_changes": [{
                "attempt": 1, "findings": [
                    {"id": "A", "severity": "P0", "title": "p0", "body": ""},
                    {"id": "B", "severity": "P1", "title": "p1", "body": ""},
                    {"id": "C", "severity": "P2", "title": "p2", "body": ""},
                    {"id": "D", "severity": "P3", "title": "p3", "body": ""},
                ],
            }],
        },
    })

    only_p0 = list_findings(
        "20260510_230000_bbbbbb", severity_min="P0", runs_dir=runs, cwd=None,
    )
    assert {f.id for f in only_p0} == {"A"}

    p0_p1 = list_findings(
        "20260510_230000_bbbbbb", severity_min="P1", runs_dir=runs, cwd=None,
    )
    assert {f.id for f in p0_p1} == {"A", "B"}

    p0_p2 = list_findings(
        "20260510_230000_bbbbbb", severity_min="P2", runs_dir=runs, cwd=None,
    )
    assert {f.id for f in p0_p2} == {"A", "B", "C"}


def test_list_findings_phase_filter(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    _seed_run(runs, "20260510_240000_cccccc", meta={
        "task": "demo", "status": "done",
        "phases": {
            "validate_plan": [{"attempt": 1, "findings":
                [{"id": "Q1", "severity": "P1", "title": "q1", "body": ""}]}],
            "review_changes":  [{"attempt": 1, "findings":
                [{"id": "R1", "severity": "P1", "title": "r1", "body": ""}]}],
        },
    })

    only_review = list_findings(
        "20260510_240000_cccccc",
        phases=("review_changes",),
        runs_dir=runs,
        cwd=None,
    )
    assert {f.id for f in only_review} == {"R1"}


def test_list_findings_invalid_severity_raises(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    _seed_run(runs, "20260510_250000_dddddd")
    with pytest.raises(ValueError) as exc:
        list_findings(
            "20260510_250000_dddddd",
            severity_min="critical",  # type: ignore[arg-type]
            runs_dir=runs,
            cwd=None,
        )
    assert "P0" in str(exc.value)


def test_list_findings_empty_meta_returns_empty_list(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    _seed_run(runs, "20260510_260000_eeeeee")
    result = list_findings(
        "20260510_260000_eeeeee", runs_dir=runs, cwd=None,
    )
    assert result == []


def test_list_findings_normalizes_singleton_dict_final_acceptance(
    tmp_path: Path,
) -> None:
    """ADR 0025 Phase 1: ``FinalAcceptanceAdapter`` persists
    ``session["phases"]["final_acceptance"]`` as a singleton dict
    (the closing gate runs once, not as a loop). The SDK
    ``list_findings`` slice must normalize this shape so release
    blockers projected into the review-shape ``findings`` mirror stay
    visible. Without normalization, MCP ``orcho_run_evidence(slice=
    "findings")`` (which delegates here) would silently drop them.
    """
    runs = tmp_path / "runs"
    runs.mkdir()
    _seed_run(runs, "20260514_100000_aaaaaa", meta={
        "task": "demo", "status": "done",
        "phases": {
            "final_acceptance": {
                "verdict": "REJECTED",
                "approved": False,
                "short_summary": "Release blocked.",
                "findings": [{
                    "id": "R1",
                    "severity": "P1",
                    "title": "Release blocker",
                    "body": "Breaks caller contract.",
                    "required_fix": "Restore compatibility.",
                }],
                "ship_ready": False,
                "release_blockers": [{
                    "id": "R1",
                    "severity": "P1",
                    "title": "Release blocker",
                    "body": "Breaks caller contract.",
                    "required_fix": "Restore compatibility.",
                    "why_blocks_release": "Production callers would fail.",
                }],
                "verification_gaps": [],
                "contract_status": {
                    "task_contract": "incomplete",
                    "interfaces":    "broken",
                    "persistence":   "safe",
                    "tests":         "weak",
                },
            },
        },
    })
    findings = list_findings(
        "20260514_100000_aaaaaa",
        phases=("final_acceptance",),
        runs_dir=runs, cwd=None,
    )
    assert [f.id for f in findings] == ["R1"]
    assert findings[0].phase == "final_acceptance"
    assert findings[0].attempt == 1
    assert findings[0].severity == "P1"


# ── plan summary ────────────────────────────────────────────────────────────


def test_get_plan_summary_returns_typed_projection(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    run_dir = _seed_run(runs, "20260510_270000_ffffff", meta={
        "task": "demo", "status": "done",
        "phases": {"plan": [{"attempt": 1}]},
    })
    # Drop a minimal plan event so the collector's plan record is populated.
    (run_dir / "events.jsonl").write_text(
        json.dumps({
            "seq": 1, "ts": "2026-05-10T22:00:00Z", "kind": "plan.parsed",
            "phase": "plan", "payload": {
                "source": "json", "short_summary": "demo plan",
                "planning_context": "test ctx", "subtask_count": 2,
                "has_contract": True, "goal": "deliver demo",
                "acceptance_criteria": ["ac1"], "owned_files": ["a.py"],
                "commands_to_run": ["pytest"], "risks": ["r1"],
                "review_focus": ["rf1"], "mcp_context": [],
            },
        }) + "\n",
        encoding="utf-8",
    )

    summary = get_plan_summary(
        "20260510_270000_ffffff", runs_dir=runs, cwd=None,
    )
    assert isinstance(summary, PlanSummary)
    # Minimal assertion — the exact shape depends on the collector's plan
    # extraction; we check the call doesn't raise and returns the dataclass.


# ── errors / halt ──────────────────────────────────────────────────────────


def test_get_errors_halt_surfaces_halt_reason_from_meta(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    _seed_run(runs, "20260510_280000_gggggg", meta={
        "task": "rejected demo",
        "status": "halted",
        "halt_reason": "plan_rejected",
        "halted_at": "2026-05-10T22:00:00+00:00",
        "phases": {},
    })

    info = get_errors_halt(
        "20260510_280000_gggggg", runs_dir=runs, cwd=None,
    )
    assert isinstance(info, ErrorsAndHalt)
    assert info.status == "halted"
    assert info.halt_reason == "plan_rejected"
    assert info.halted_at == "2026-05-10T22:00:00+00:00"
    assert info.errors == ()  # no errors emitted in this run


def test_get_errors_halt_projects_provider_access_recovery(tmp_path: Path) -> None:
    """ADR 0101 / T3 — a terminal provider-access failure populates the typed
    ``recovery`` field; ``halt`` stays meta-only (not in ``replacements``)."""
    runs = tmp_path / "runs"
    runs.mkdir()
    _seed_run(runs, "20260524_300000_iiiiii", meta={
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
    })

    info = get_errors_halt("20260524_300000_iiiiii", runs_dir=runs, cwd=None)
    assert isinstance(info.recovery, ProviderAccessRecovery)
    rec = info.recovery
    assert rec.failure_kind == "provider_access"
    assert rec.recoverable is False
    assert rec.failed_phase == "plan"
    assert rec.runtime == "claude"
    assert rec.model == "claude-opus-4-8"
    # Only replace candidates are promoted; retry/halt are not replacements.
    assert len(rec.replacements) == 1
    assert rec.replacements[0].runtime == "codex"
    assert rec.replacements[0].model == "gpt-5.5"


def test_get_errors_halt_no_recovery_for_non_provider_access(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    _seed_run(runs, "20260524_310000_jjjjjj", meta={
        "task": "plain failure",
        "status": "failed",
        "phases": {},
        "failure": {"phase": "implement", "type": "RuntimeError"},
    })
    info = get_errors_halt("20260524_310000_jjjjjj", runs_dir=runs, cwd=None)
    assert info.recovery is None


# ── ADR 0104: setup/preflight terminal-state projection ─────────────────────


def test_get_errors_halt_synthesizes_setup_failure_for_bootstrap_halt(
    tmp_path: Path,
) -> None:
    """A run that died in setup (worktree bootstrap) before any phase, with no
    parsed_plan.json and no other terminal breadcrumb, surfaces a non-empty
    errors slice naming the actionable cause + the runner.log pointer."""
    runs = tmp_path / "runs"
    runs.mkdir()
    run_dir = _seed_run(runs, "20260625_400000_aaaaaa", meta={
        "task": "setup death",
        "status": "halted",
        "halt_reason": "worktree_bootstrap_failed",
        "halted_at": "2026-06-25T09:00:00+00:00",
        "phases": {},
        "worktree_bootstrap": {"status": "failed", "error": "git checkout boom"},
    })
    _write_runner_log(run_dir)

    info = get_errors_halt("20260625_400000_aaaaaa", runs_dir=runs, cwd=None)
    assert info.errors  # non-empty
    setup = next(e for e in info.errors if e.get("kind") == "setup_failed")
    assert "worktree_bootstrap_failed" in setup["message"]
    assert "git checkout boom" in setup["message"]
    assert "runner.log" in setup["message"]
    # status/halt_reason come from the merge rule and stay self-consistent.
    assert info.status == "halted"
    assert info.halt_reason == "worktree_bootstrap_failed"


def test_setup_failure_single_status_meta_failed_plus_negative_exit(
    tmp_path: Path,
) -> None:
    """meta.status='failed' + supervisor exit_code<0 → get_errors_halt and
    load_status agree on ONE status ('failed'); terminal meta wins, NO remap to
    'interrupted' (no status divergence), in parity with the MCP merge rule."""
    runs = tmp_path / "runs"
    runs.mkdir()
    run_dir = _seed_run(runs, "20260625_410000_bbbbbb", meta={
        "task": "status divergence guard",
        "status": "failed",
        "halt_reason": "worktree_bootstrap_failed",
        "phases": {},
        "worktree_bootstrap": {"status": "failed", "error": "boom"},
    })
    _write_runner_log(run_dir)
    _write_launcher_state(run_dir, {"status": "interrupted", "exit_code": -9})

    eh = get_errors_halt("20260625_410000_bbbbbb", runs_dir=runs, cwd=None)
    st = load_status("20260625_410000_bbbbbb", runs_dir=runs, cwd=None)
    assert eh.status == "failed"
    assert st.meta is not None and st.meta.status == "failed"
    assert eh.status == st.meta.status  # single status on both surfaces


def test_setup_failure_supervisor_fallback_single_status(
    tmp_path: Path,
) -> None:
    """Empty meta.status + launcher abnormal exit (exit<0) → both get_errors_halt
    and load_status resolve the SAME terminal status ('interrupted')."""
    runs = tmp_path / "runs"
    runs.mkdir()
    run_dir = _seed_run(runs, "20260625_420000_cccccc", meta={
        "task": "supervisor fallback",
        "status": "",
        "phases": {},
    })
    _write_runner_log(run_dir)
    _write_launcher_state(
        run_dir, {"status": "failed", "exit_code": -15, "halt_reason": "signal:SIGTERM"},
    )

    eh = get_errors_halt("20260625_420000_cccccc", runs_dir=runs, cwd=None)
    st = load_status("20260625_420000_cccccc", runs_dir=runs, cwd=None)
    assert eh.status == "interrupted"
    assert st.meta is not None and st.meta.status == "interrupted"
    assert eh.status == st.meta.status
    # raw meta stays raw (fidelity): the empty status is preserved untouched.
    assert st.raw_meta.get("status") == ""
    # The errors slice names the actionable launcher cause.
    assert any(
        e.get("kind") == "setup_failed" and "signal:SIGTERM" in e["message"]
        for e in eh.errors
    )


def test_setup_failure_bare_failed_run_end_without_error_still_surfaces(
    tmp_path: Path,
) -> None:
    """A bare ``run.end`` ``{'status': 'failed'}`` with no ``error`` writes NO
    collector breadcrumb, so it must not suppress the synthesis. The errors slice
    must still name the actionable launcher cause (regression: empty errors)."""
    runs = tmp_path / "runs"
    runs.mkdir()
    run_dir = _seed_run(runs, "20260625_430000_dddddd", meta={
        "task": "bare failed run.end",
        "status": "failed",
        "phases": {},
    })
    _write_runner_log(run_dir)
    _write_launcher_state(
        run_dir, {"status": "failed", "exit_code": 1, "halt_reason": "abnormal_exit:1"},
    )
    # Only a bare run.end with status='failed' (no error / no halt status) — the
    # collector emits neither run_failed nor run_halted for it.
    (run_dir / "events.jsonl").write_text(
        json.dumps({"kind": "run.end", "payload": {"status": "failed"}}) + "\n",
        encoding="utf-8",
    )

    info = get_errors_halt("20260625_430000_dddddd", runs_dir=runs, cwd=None)
    assert info.errors  # non-empty despite the bare run.end
    setup = next(e for e in info.errors if e.get("kind") == "setup_failed")
    assert "abnormal_exit:1" in setup["message"]
    assert "runner.log" in setup["message"]
    assert info.status == "failed"
    assert info.halt_reason == "abnormal_exit:1"


def test_get_errors_halt_surfaces_phase_handoff_from_meta(tmp_path: Path) -> None:
    """Evidence reads the canonical ``meta.phase_handoff`` payload and
    surfaces a ``phase_handoff_requested`` finding.
    """
    runs = tmp_path / "runs"
    runs.mkdir()
    _seed_run(runs, "20260520_290000_hhhhhh", meta={
        "task": "paused demo",
        "status": "awaiting_phase_handoff",
        "phases": {
            "plan": [{"approved": False, "attempt": 1}],
            "validate_plan": [{"approved": False, "attempt": 1}],
        },
        "phase_handoff": {
            "id":         "validate_plan:plan_round:2",
            "phase":      "validate_plan",
            "type":       "human_feedback_on_reject",
            "trigger":    "rejected",
            "round":      2,
            "last_output": "needs rollback plan",
        },
    })

    info = get_errors_halt(
        "20260520_290000_hhhhhh", runs_dir=runs, cwd=None,
    )
    assert isinstance(info, ErrorsAndHalt)
    assert info.status == "awaiting_phase_handoff"
    handoff_errors = [
        e for e in info.errors if e.get("kind") == "phase_handoff_requested"
    ]
    assert len(handoff_errors) == 1
    err = handoff_errors[0]
    assert err["message"] == "needs rollback plan"
    assert err["phase"] == "validate_plan"
    assert err["handoff_type"] == "human_feedback_on_reject"
    assert err["handoff_id"] == "validate_plan:plan_round:2"
    assert err["round"] == 2


def test_get_errors_halt_phase_handoff_event_carries_handoff_id(
    tmp_path: Path,
) -> None:
    """A ``phase.handoff_requested`` event in events.jsonl also
    surfaces a finding, carrying phase / handoff_type / handoff_id /
    round so audit consumers can correlate the event with the
    persisted payload."""
    runs = tmp_path / "runs"
    runs.mkdir()
    run_dir = _seed_run(runs, "20260520_2a0000_iiiiii", meta={
        "task": "event demo",
        "status": "awaiting_phase_handoff",
        "phases": {},
        # Active payload absent (event-only surface).
    })
    (run_dir / "events.jsonl").write_text(
        json.dumps({
            "kind":      "phase.handoff_requested",
            "ts":        "2026-05-20T10:00:00+00:00",
            "seq":       1,
            "phase":     "validate_plan",
            "payload":   {
                "phase":        "validate_plan",
                "handoff_type": "human_feedback_on_reject",
                "trigger":      "rejected",
                "round":        2,
                "handoff_id":   "validate_plan:plan_round:2",
            },
        }) + "\n",
        encoding="utf-8",
    )

    info = get_errors_halt(
        "20260520_2a0000_iiiiii", runs_dir=runs, cwd=None,
    )
    handoff_errors = [
        e for e in info.errors if e.get("kind") == "phase_handoff_requested"
    ]
    assert len(handoff_errors) == 1
    err = handoff_errors[0]
    assert err["handoff_id"] == "validate_plan:plan_round:2"
    assert err["phase"] == "validate_plan"
    assert err["handoff_type"] == "human_feedback_on_reject"


# ── sub_runs (cross-run linkage) ───────────────────────────────────────────


def test_list_sub_runs_returns_all_aliases(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    parent = _seed_run(runs, "20260510_290000_hhhhhh", meta={
        "task": "cross demo", "status": "done",
        "projects": ["unity", "api"],
    })
    # Sub-run with meta.
    unity_dir = parent / "unity"
    unity_dir.mkdir()
    (unity_dir / "meta.json").write_text(
        json.dumps({"status": "done"}) + "\n", encoding="utf-8",
    )
    # Sub-run without meta (early state — alias dir exists but pipeline
    # didn't write its own meta yet).
    api_dir = parent / "api"
    api_dir.mkdir()

    links = list_sub_runs(
        "20260510_290000_hhhhhh", runs_dir=runs, cwd=None,
    )
    by_name = {link.name: link for link in links}
    assert by_name["unity"].status == "done"
    # api alias exists but has no meta — status surfaces as None, not crash.
    assert by_name["api"].status is None


def test_list_sub_runs_empty_for_single_project(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    _seed_run(runs, "20260510_300000_iiiiii")
    assert list_sub_runs(
        "20260510_300000_iiiiii", runs_dir=runs, cwd=None,
    ) == []


def test_list_sub_runs_excludes_run_artifact_dirs(tmp_path: Path) -> None:
    """Run-owned artifact dirs (``commit_decisions`` / ``worktrees`` /
    ``phase_handoff_decisions``) are direct children of run_dir but
    are NOT per-alias sub-runs. Once worktree isolation +
    commit-delivery are active, a single-project run creates
    ``commit_decisions`` — it must not surface as a spurious sub-run
    (regression: this broke the L4 MCP ``sub_runs`` slice expectation
    for single-project runs)."""
    runs = tmp_path / "runs"
    runs.mkdir()
    parent = _seed_run(runs, "20260510_310001_kkkkkk")
    # Real artifact dirs a single-project run leaves behind.
    (parent / "commit_decisions").mkdir()
    (parent / "worktrees").mkdir()
    (parent / "phase_handoff_decisions").mkdir()

    assert list_sub_runs(
        "20260510_310001_kkkkkk", runs_dir=runs, cwd=None,
    ) == [], "artifact dirs must not be reported as sub-runs"


def test_list_sub_runs_keeps_aliases_alongside_artifact_dirs(
    tmp_path: Path,
) -> None:
    """A cross run has BOTH per-alias sub-run dirs AND artifact dirs
    under run_dir. Only the alias dirs are sub-runs; the artifact dirs
    are filtered out."""
    runs = tmp_path / "runs"
    runs.mkdir()
    parent = _seed_run(runs, "20260510_310002_llllll", meta={
        "task": "cross demo", "status": "done",
        "projects": ["api", "web"],
    })
    (parent / "api").mkdir()
    (parent / "api" / "meta.json").write_text(
        json.dumps({"status": "done"}) + "\n", encoding="utf-8",
    )
    (parent / "web").mkdir()
    # Artifact dirs alongside the aliases.
    (parent / "commit_decisions").mkdir()
    (parent / "worktrees").mkdir()

    names = {
        link.name
        for link in list_sub_runs(
            "20260510_310002_llllll", runs_dir=runs, cwd=None,
        )
    }
    assert names == {"api", "web"}, (
        f"only alias dirs are sub-runs; got {names}"
    )


def test_list_sub_runs_positive_detection_beyond_denylist(
    tmp_path: Path,
) -> None:
    """Classification is by positive detection (run marker OR declared
    parent alias), not a name denylist alone. Pins three cases:

    * a declared-but-empty alias dir → included (alias membership);
    * an early-state sub-run carrying only ``output.log`` and NOT in
      the parent alias list → included (run marker);
    * a *future* artifact dir not in ``_NON_SUBRUN_DIR_NAMES``, with no
      run marker and not a declared alias → excluded (the denylist
      would have leaked it; positive detection does not).
    """
    runs = tmp_path / "runs"
    runs.mkdir()
    parent = _seed_run(runs, "20260510_310003_mmmmmm", meta={
        "task": "cross demo", "status": "done", "projects": ["api"],
    })
    # Declared alias, empty dir → included via alias membership.
    (parent / "api").mkdir()
    # Early-state sub-run: only output.log, NOT a declared alias →
    # included via run marker.
    early = parent / "early_alias"
    early.mkdir()
    (early / "output.log").write_text("", encoding="utf-8")
    # Future artifact dir: not in the denylist, no run marker, not a
    # declared alias → excluded by positive detection.
    future_artifacts = parent / "some_future_artifacts"
    future_artifacts.mkdir()
    (future_artifacts / "data.json").write_text("{}", encoding="utf-8")

    names = {
        link.name
        for link in list_sub_runs(
            "20260510_310003_mmmmmm", runs_dir=runs, cwd=None,
        )
    }
    assert names == {"api", "early_alias"}, (
        f"positive detection should include declared aliases + run-marked "
        f"dirs and exclude markerless artifact dirs; got {names}"
    )


# ── commands / artifacts (smoke — collector owns the heavy lifting) ────────


def test_list_evidence_commands_returns_empty_for_no_commands(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    _seed_run(runs, "20260510_310000_jjjjjj")
    assert list_evidence_commands(
        "20260510_310000_jjjjjj", runs_dir=runs, cwd=None,
    ) == []


def test_list_evidence_artifacts_returns_empty_for_no_artifacts(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    _seed_run(runs, "20260510_320000_kkkkkk")
    assert list_evidence_artifacts(
        "20260510_320000_kkkkkk", runs_dir=runs, cwd=None,
    ) == []


# ── subtask receipts (P7 / ADR 0068) ────────────────────────────────────────


def _write_events(run_dir: Path, *payloads: dict) -> None:
    """Overwrite events.jsonl with one subtask.receipt event per payload."""
    lines = [
        json.dumps({
            "seq": i, "ts": "2026-06-03T00:00:00Z",
            "kind": "subtask.receipt", "phase": "implement",
            "payload": p,
        })
        for i, p in enumerate(payloads, start=1)
    ]
    (run_dir / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_list_subtask_receipts_empty_when_no_receipt_events(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    _seed_run(runs, "20260603_000000_aaaaaa", meta={"task": "t", "status": "done"})
    assert list_subtask_receipts(
        "20260603_000000_aaaaaa", runs_dir=runs, cwd=None,
    ) == []


def test_list_subtask_receipts_projects_done_with_attestation(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    run_dir = _seed_run(
        runs, "20260603_000000_bbbbbb", meta={"task": "t", "status": "done"},
    )
    _write_events(run_dir, {
        "subtask_id": "t1", "state": "done", "runtime": "claude",
        "model": "m", "skill": None, "depends_on": [],
        "done_criteria": ["a", "b"], "duration": 1.5,
        "attestation_repaired": True,
        "criteria_report": [
            {"index": 1, "criterion": "a", "met": True, "evidence": "did a"},
            {"index": 2, "criterion": "b", "met": True, "evidence": "did b"},
        ],
        "attestation_summary": "all met",
    })

    receipts = list_subtask_receipts(
        "20260603_000000_bbbbbb", runs_dir=runs, cwd=None,
    )
    assert len(receipts) == 1
    r = receipts[0]
    assert isinstance(r, SubtaskReceipt)
    assert r.subtask_id == "t1"
    assert r.state == "done"
    assert r.done_criteria == ("a", "b")
    assert r.attestation_summary == "all met"
    assert r.attestation_error is None
    assert r.attestation_repaired is True
    assert [c.index for c in r.criteria_report] == [1, 2]
    assert r.criteria_report[0].met is True
    assert r.criteria_report[0].evidence == "did a"


def test_list_subtask_receipts_surfaces_incomplete_with_reason(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    run_dir = _seed_run(
        runs, "20260603_000000_cccccc", meta={"task": "t", "status": "halted"},
    )
    _write_events(
        run_dir,
        {
            "subtask_id": "t1", "state": "done", "runtime": "claude",
            "model": "m", "skill": None, "depends_on": [],
            "done_criteria": ["a"], "duration": 1.0,
            "criteria_report": [
                {"index": 1, "criterion": "a", "met": True, "evidence": "ok"},
            ],
            "attestation_summary": "met",
        },
        {
            "subtask_id": "t2", "state": "incomplete", "runtime": "claude",
            "model": "m", "skill": None, "depends_on": ["t1"],
            "done_criteria": ["b"], "duration": 0.5,
            "attestation_error": "done_criteria not met (by index): [1]",
        },
    )

    receipts = list_subtask_receipts(
        "20260603_000000_cccccc", runs_dir=runs, cwd=None,
    )
    by_id = {r.subtask_id: r for r in receipts}
    assert by_id["t1"].state == "done"
    incomplete = by_id["t2"]
    assert incomplete.state == "incomplete"
    assert incomplete.depends_on == ("t1",)
    assert incomplete.attestation_error == "done_criteria not met (by index): [1]"
    assert incomplete.criteria_report == ()
