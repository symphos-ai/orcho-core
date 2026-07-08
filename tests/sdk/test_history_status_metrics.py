"""`list_history`, `load_status`, metrics — read surface against synthetic runs."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdk import NoWorkspace, RunNotFound, get_run_metrics, list_history, list_metrics, load_status


def test_list_history_orders_newest_first(populated_runs: Path):
    rows = list_history(runs_dir=populated_runs)
    assert [r.run_id for r in rows] == [
        "20260507_120000",
        "20260506_090000",
        "20260505_080000",
    ]


def test_list_history_last_n(populated_runs: Path):
    rows = list_history(last=2, runs_dir=populated_runs)
    assert len(rows) == 2
    assert rows[0].run_id == "20260507_120000"


def test_list_history_cross_run_aliases(populated_runs: Path):
    rows = list_history(runs_dir=populated_runs)
    cross = next(r for r in rows if r.run_id == "20260505_080000")
    assert cross.cross_aliases == ("unity", "api")
    assert cross.project is None


def test_list_history_single_project(populated_runs: Path):
    rows = list_history(runs_dir=populated_runs)
    single = next(r for r in rows if r.run_id == "20260507_120000")
    assert single.project == "/tmp/projA"
    assert single.cross_aliases == ()


def test_load_status_latest(populated_runs: Path):
    status = load_status(runs_dir=populated_runs)
    assert status.run_ref.run_id == "20260507_120000"
    assert status.meta is not None
    assert status.meta.task == "Add feature X"
    assert status.total_tokens == 12000
    assert status.total_duration_s == pytest.approx(60.0)


def test_load_status_unknown(populated_runs: Path):
    with pytest.raises(RunNotFound):
        load_status("nonexistent", runs_dir=populated_runs)


def test_load_status_empty_workspace(runs_root: Path):
    with pytest.raises(RunNotFound):
        load_status(runs_dir=runs_root)


# ─────────────────────────────────────────────────────────────────────────────
# ADR 0045 — RunStatus.artefacts enrichment
# ─────────────────────────────────────────────────────────────────────────────


def _write_minimal_run(runs_root: Path, run_id: str) -> Path:
    """Create a run dir with just meta.json so load_status resolves it."""
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(
        json.dumps({"project": "/p", "task": "t", "status": "running"}),
        encoding="utf-8",
    )
    return run_dir


def test_load_status_artefacts_no_optional_files(runs_root: Path):
    """parsed_plan.json and diff.patch absent — only the always-emitted
    evidence entry shows up. size_bytes is None for evidence
    (composable resource).
    """
    _write_minimal_run(runs_root, "20260601_aonly")
    status = load_status("20260601_aonly", runs_dir=runs_root)
    kinds = [a.kind for a in status.artefacts]
    assert kinds == ["evidence"]
    ev = status.artefacts[0]
    assert ev.uri == "orcho://runs/20260601_aonly/evidence"
    assert ev.mime == "application/json"
    assert ev.size_bytes is None


def test_load_status_artefacts_parsed_plan_only(runs_root: Path):
    """parsed_plan.json present, diff.patch absent — entries for
    parsed_plan + evidence. parsed_plan carries size_bytes from stat.
    """
    run_dir = _write_minimal_run(runs_root, "20260601_planonly")
    (run_dir / "parsed_plan.json").write_text("{\"x\": 1}", encoding="utf-8")
    status = load_status("20260601_planonly", runs_dir=runs_root)
    kinds = {a.kind for a in status.artefacts}
    assert kinds == {"parsed_plan", "evidence"}
    plan = next(a for a in status.artefacts if a.kind == "parsed_plan")
    assert plan.uri == "orcho://runs/20260601_planonly/parsed_plan.json"
    assert plan.mime == "application/json"
    assert plan.size_bytes is not None
    assert plan.size_bytes > 0


def test_load_status_artefacts_diff_only(runs_root: Path):
    """diff.patch present, parsed_plan.json absent — entries for
    diff + evidence. diff carries text/x-patch mime + size_bytes.
    """
    run_dir = _write_minimal_run(runs_root, "20260601_diffonly")
    (run_dir / "diff.patch").write_text(
        "diff --git a/x b/x\n+ hello\n", encoding="utf-8",
    )
    status = load_status("20260601_diffonly", runs_dir=runs_root)
    kinds = {a.kind for a in status.artefacts}
    assert kinds == {"diff", "evidence"}
    diff = next(a for a in status.artefacts if a.kind == "diff")
    assert diff.uri == "orcho://runs/20260601_diffonly/diff.patch"
    assert diff.mime == "text/x-patch"
    assert diff.size_bytes is not None
    assert diff.size_bytes > 0


def test_load_status_artefacts_all_three(runs_root: Path):
    """Both physical artefacts present — full set surfaces, in
    insertion order (parsed_plan → diff → evidence).
    """
    run_dir = _write_minimal_run(runs_root, "20260601_full")
    (run_dir / "parsed_plan.json").write_text("{\"x\": 1}", encoding="utf-8")
    (run_dir / "diff.patch").write_text(
        "diff --git a/y b/y\n+ world\n", encoding="utf-8",
    )
    status = load_status("20260601_full", runs_dir=runs_root)
    assert [a.kind for a in status.artefacts] == [
        "parsed_plan", "diff", "evidence",
    ]
    # Spot-check the URIs (full set).
    assert all(a.uri.startswith("orcho://runs/20260601_full/")
               for a in status.artefacts)


def test_load_status_from_run_plan_action_requires_parsed_plan_file(
    runs_root: Path,
) -> None:
    """A failed child may stamp ``plan_source='run'`` before setup fails.
    Without its own ``parsed_plan.json`` it must not advertise a reusable
    ``from_run_plan`` action from the failed child run."""
    run_dir = _write_minimal_run(runs_root, "20260601_failed_child")
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    meta.update({"status": "failed", "plan_source": "run"})
    (run_dir / "meta.json").write_text(
        json.dumps(meta), encoding="utf-8",
    )

    status = load_status("20260601_failed_child", runs_dir=runs_root)

    assert any(a.tool == "orcho_run_resume" for a in status.next_actions)
    assert not any(a.tool == "orcho_run_start" for a in status.next_actions)


def test_load_status_from_run_plan_action_uses_physical_plan_artifact(
    runs_root: Path,
) -> None:
    """When ``parsed_plan.json`` exists, the plan-artifact continuation
    suggestion stays available."""
    run_dir = _write_minimal_run(runs_root, "20260601_plan_child")
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    meta.update({"status": "failed", "plan_source": "run"})
    (run_dir / "meta.json").write_text(
        json.dumps(meta), encoding="utf-8",
    )
    (run_dir / "parsed_plan.json").write_text("{\"x\": 1}", encoding="utf-8")

    status = load_status("20260601_plan_child", runs_dir=runs_root)

    start_actions = [
        a for a in status.next_actions if a.tool == "orcho_run_start"
    ]
    assert len(start_actions) == 1
    assert start_actions[0].args["from_run_plan"] == "20260601_plan_child"


# ─────────────────────────────────────────────────────────────────────────────
# ADR 0104 — merged status (launcher fallback) consistency in load_status
# ─────────────────────────────────────────────────────────────────────────────


def _write_launcher_state(run_dir: Path, supervisor: dict) -> None:
    """Thin local helper: write the optional launcher state file (ADR 0104)."""
    (run_dir / "mcp_supervisor.json").write_text(
        json.dumps(supervisor), encoding="utf-8",
    )


def test_load_status_supervisor_fallback_merges_terminal_status(
    runs_root: Path,
) -> None:
    """Empty meta.status + a launcher state that reaped an abnormal exit
    (exit_code<0) → load_status projects the merged terminal status
    ('interrupted'), while raw_meta keeps the empty status untouched."""
    run_dir = _write_minimal_run(runs_root, "20260601_supervisor_fallback")
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    meta["status"] = ""
    (run_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    _write_launcher_state(
        run_dir, {"status": "failed", "exit_code": -9, "halt_reason": "signal:SIGKILL"},
    )

    status = load_status("20260601_supervisor_fallback", runs_dir=runs_root)

    assert status.meta is not None
    assert status.meta.status == "interrupted"  # merged terminal status
    assert status.raw_meta.get("status") == ""  # raw fidelity preserved
    # Resumable terminal → resume action present.
    assert any(a.tool == "orcho_run_resume" for a in status.next_actions)


def test_load_status_terminal_meta_wins_over_launcher_negative_exit(
    runs_root: Path,
) -> None:
    """meta.status='failed' + launcher exit_code<0 → load_status keeps 'failed'
    (terminal meta wins, no remap to 'interrupted')."""
    run_dir = _write_minimal_run(runs_root, "20260601_terminal_meta_wins")
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    meta["status"] = "failed"
    (run_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    _write_launcher_state(
        run_dir, {"status": "interrupted", "exit_code": -9},
    )

    status = load_status("20260601_terminal_meta_wins", runs_dir=runs_root)

    assert status.meta is not None
    assert status.meta.status == "failed"


def test_load_status_setup_child_no_from_run_plan_with_launcher_state(
    runs_root: Path,
) -> None:
    """A failed setup-child that stamped plan_source='run' but never wrote its
    own parsed_plan.json — even with a launcher state driving the merged status —
    must NOT advertise a from_run_plan action, but must keep resume."""
    run_dir = _write_minimal_run(runs_root, "20260601_setup_child_launcher")
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    meta.update({"status": "", "plan_source": "run"})
    (run_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    _write_launcher_state(run_dir, {"status": "failed", "exit_code": 1})

    status = load_status("20260601_setup_child_launcher", runs_dir=runs_root)

    assert status.meta is not None and status.meta.status == "failed"
    assert any(a.tool == "orcho_run_resume" for a in status.next_actions)
    assert not any(a.tool == "orcho_run_start" for a in status.next_actions)


def test_load_status_excludes_run_artifact_dirs_from_sub_projects(
    runs_root: Path,
) -> None:
    """Run-owned artifact directories must not surface as child projects."""
    run_dir = _write_minimal_run(runs_root, "20260601_artifact_dirs")
    for name in (
        "commit_decisions",
        "phase_handoff_advice",
        "phase_handoff_decisions",
        "phases",
        "verification_command_receipts",
        "verification_receipts",
    ):
        (run_dir / name).mkdir()

    status = load_status("20260601_artifact_dirs", runs_dir=runs_root)

    assert status.sub_projects == ()


def test_get_run_metrics(populated_runs: Path, monkeypatch: pytest.MonkeyPatch):
    from core.infra import config
    monkeypatch.setenv("ORCHO_ACCOUNTING", "1")
    config._reset_config()
    m = get_run_metrics("20260507_120000", runs_dir=populated_runs)
    config._reset_config()
    assert m.total_tokens == 12000
    assert m.total_cost_usd_equivalent == pytest.approx(0.42)
    assert "plan" in m.phases


def test_list_metrics(populated_runs: Path):
    rows = list_metrics(last=10, runs_dir=populated_runs)
    # Cross run has no meta.json/metrics.json shape that load_historical_runs
    # accepts → only the two single-project runs come back.
    assert {r.run_id for r in rows} >= {"20260507_120000", "20260506_090000"}
    top = next(r for r in rows if r.run_id == "20260507_120000")
    assert top.total_tokens_in == 8000
    assert top.total_tokens_out == 4000


def test_list_history_no_runs_dir(tmp_path: Path):
    with pytest.raises(NoWorkspace):
        list_history(runs_dir=tmp_path / "nope")


# ─────────────────────────────────────────────────────────────────────────────
# T3 — rejected-release surfaces: delivery gate, next-actions, override-done
# ─────────────────────────────────────────────────────────────────────────────


def _write_run_meta(runs_root: Path, run_id: str, meta: dict) -> Path:
    """Write a run dir with a hand-built meta.json for surface tests."""
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8",
    )
    return run_dir


def _rejected_commit_delivery(run_id: str) -> dict:
    """Mirror the auto/non-interactive rejected-release decision (T2).

    Status ``not_applicable`` with a non-empty, non-APPROVED ``release_verdict``
    — the shape ``run.py`` persists when auto-delivery refuses a rejected
    release.
    """
    return {
        "action": "none",
        "status": "not_applicable",
        "run_id": run_id,
        "release_verdict": "REJECTED",
        "release_summary": "blocking data-loss defect",
        "release_blockers": [
            {
                "id": "RB1",
                "severity": "P1",
                "title": "Data loss on apply",
                "required_fix": "Preserve existing rows during delivery.",
                "why_blocks_release": "Shipping would destroy user data.",
            },
        ],
        "project_path": "/p",
        "source_path": "/p/checkout",
        "baseline_ref": "HEAD",
        "include_untracked": False,
    }


def test_rejected_release_gate_is_decidable_correction(runs_root: Path) -> None:
    from sdk.run_control.delivery import delivery_decision_state

    run_id = "20260610_rejected"
    _write_run_meta(
        runs_root,
        run_id,
        {
            "project": "/p",
            "task": "t",
            "status": "halted",
            "halt_reason": "final_acceptance_rejected",
            "commit_delivery": _rejected_commit_delivery(run_id),
        },
    )

    state = delivery_decision_state(run_id, runs_dir=runs_root, cwd=None)

    # Previously this returned decidable=False / 'no pending delivery gate'.
    assert state.decidable is True
    assert state.kind == "correction"
    # ADR 0111: an auto-refused rejected release (``_is_rejected_release_gate``)
    # is a dead-end whose only forward motion is a from_run_plan follow-up.
    # Repeating ``fix`` is inert, so it is blocked alongside the shipping actions
    # and ``skip`` (ADR 0106) — only ``halt`` (give up) remains available.
    assert set(state.blocked_actions) == {"fix", "approve", "apply", "skip"}
    assert state.available_actions == ("halt",)
    assert "fix" not in state.available_actions
    assert "skip" not in state.available_actions
    assert "approve" not in state.available_actions
    # No inert in-gate repeat is advertised as the actionable next step.
    assert state.default_action is None
    # The reason routes the client to a from_run_plan follow-up (no diff.patch
    # file written here, so the held-diff suffix is omitted).
    assert state.reason is not None
    assert f"from_run_plan={run_id}" in state.reason
    assert "orcho_run_start" in state.reason
    assert "inert" in state.reason
    assert "diff.patch" not in state.reason


def test_rejected_release_gate_decide_refuses_shipping_actionably(
    runs_root: Path,
) -> None:
    from sdk.run_control.delivery import decide_delivery

    run_id = "20260610_rejected_decide"
    _write_run_meta(
        runs_root,
        run_id,
        {
            "project": "/p",
            "task": "t",
            "status": "halted",
            "halt_reason": "final_acceptance_rejected",
            "commit_delivery": _rejected_commit_delivery(run_id),
        },
    )

    result = decide_delivery(run_id, "approve", runs_dir=runs_root, cwd=None)

    # A shipping action on a rejected gate is a typed *release* refusal — NOT
    # the old 'no_pending_delivery_gate' (which would read as 'nothing here').
    assert result.accepted is False
    assert result.blocker == "release_blocked"
    assert result.terminal_outcome == "halted"


def test_rejected_release_gate_decide_refuses_skip_not_clean_done(
    runs_root: Path,
) -> None:
    """``skip`` must not turn a rejected release into a silent clean ``done``.

    ``skip`` does not apply delivery, yet it would finalize to ``skipped`` ∈
    done-statuses — clearing the ``final_acceptance_rejected`` halt and writing
    no ``delivery_override`` marker. ADR 0106 forbids that: a rejected release
    refuses ``skip`` too, and the run stays halted on disk.
    """
    from sdk.run_control.delivery import decide_delivery

    run_id = "20260610_rejected_skip"
    run_dir = _write_run_meta(
        runs_root,
        run_id,
        {
            "project": "/p",
            "task": "t",
            "status": "halted",
            "halt_reason": "final_acceptance_rejected",
            "commit_delivery": _rejected_commit_delivery(run_id),
        },
    )

    result = decide_delivery(run_id, "skip", runs_dir=runs_root, cwd=None)

    # Typed release refusal — never a clean accept.
    assert result.accepted is False
    assert result.blocker == "release_blocked"
    assert result.terminal_outcome == "halted"
    assert result.status != "skipped"

    # The durable terminal stays the actionable rejected halt: not flipped to a
    # clean ``done``, halt_reason preserved, no override marker fabricated.
    meta_after = json.loads((run_dir / "meta.json").read_text())
    assert meta_after["status"] == "halted"
    assert meta_after["halt_reason"] == "final_acceptance_rejected"
    assert "delivery_override" not in meta_after
    assert meta_after["commit_delivery"]["status"] == "not_applicable"


def test_compute_next_actions_rejected_halted_is_nonempty() -> None:
    from sdk.actions import compute_next_actions

    meta = {
        "status": "halted",
        "halt_reason": "final_acceptance_rejected",
        "commit_delivery": _rejected_commit_delivery("r1"),
    }
    actions = compute_next_actions(meta, run_id="r1")
    assert actions  # non-empty, actionable
    assert any(a.tool == "orcho_run_resume" for a in actions)


def test_compute_next_actions_clean_success_is_empty() -> None:
    from sdk.actions import compute_next_actions

    assert compute_next_actions({"status": "done"}, run_id="r1") == ()


def test_override_done_surfaces_release_verdict_and_override(
    runs_root: Path,
) -> None:
    """An override-done run (status='done' + delivery_override marker) must be
    observably distinct from a clean success: the status surface exposes the
    release verdict, blockers, and override reason via meta.extra / raw_meta.
    """
    run_id = "20260610_override"
    override_marker = {
        "phase": "final_acceptance",
        "reason": "final_acceptance_rejected_override",
        "status": "done",
        "release_verdict": "REJECTED",
        "release_blockers": [
            {"severity": "medium", "detail": "flaky coverage"},
        ],
        "delivery_status": "committed",
        "message": "Operator override: delivery applied despite a rejected "
        "final acceptance.",
    }
    _write_run_meta(
        runs_root,
        run_id,
        {
            "project": "/p",
            "task": "t",
            "status": "done",
            "delivery_override": override_marker,
            "commit_delivery": {
                "status": "committed",
                "release_verdict": "REJECTED",
                "release_summary": "blocking data-loss defect",
            },
        },
    )

    status = load_status(run_id, runs_dir=runs_root)

    # Clean-success runs carry no override marker; this one does.
    assert status.meta is not None
    surfaced = status.meta.extra.get("delivery_override")
    assert surfaced == override_marker
    assert surfaced["release_verdict"] == "REJECTED"
    assert surfaced["release_blockers"] == [
        {"severity": "medium", "detail": "flaky coverage"},
    ]
    assert "override" in surfaced["reason"]
    # raw_meta keeps full fidelity too.
    assert status.raw_meta["delivery_override"]["release_verdict"] == "REJECTED"
    # next_actions stays empty (terminal done) — distinction is the marker.
    assert status.next_actions == ()
