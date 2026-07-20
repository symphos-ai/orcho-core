"""ADR 0042 Phase G guard — :func:`finalize_project_run` is silent.

"Silent" means the function writes files / mutates session / emits
events / sets checkpoint final status / mirrors artifacts / tears
down the worktree, but produces **no terminal output**. UI clients
can drive the project pipeline without banners / success chips /
Session/Usage lines crossing the terminal boundary.

This test pins the rule so a future edit to the silent service
cannot reintroduce a stray ``print()`` / ``banner()`` / ``success()``
/ ``warn()`` call. The terminal-wrapper variant
(:func:`finalize_with_terminal_output`) is allowed to print — that
is exactly what it exists for and is intentionally not covered
here.

The test uses a minimal stand-in ``_Run`` rather than a real
:class:`pipeline.project.run._PipelineRun` because the dataclass
takes ~25 required fields. The silent service is duck-typed
(``ctx.run`` is annotated ``Any``) precisely so callers can build
the minimal attribute surface needed for a focused unit test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pipeline.project.finalization import (
    FinalizationContext,
    FinalizationResult,
    finalize_project_run,
)
from pipeline.project.terminal_delivery import TerminalDeliveryDisposition


class _FakeState:
    """Minimal PipelineState stand-in covering what the silent service
    reads. ``halt = False`` + non-``plan`` profile → "done" status
    path; phase_log is empty so the summary string is empty."""

    halt: bool = False
    halt_reason: str | None = None
    phase_log: dict[str, Any] = {}  # noqa: RUF012

    def __init__(self) -> None:
        self.extras: dict[str, Any] = {}


class _Run:
    """Minimal _PipelineRun stand-in. Only the attributes / methods
    the silent service actually touches are wired."""

    # ── inputs / identity ──────────────────────────────────────────
    output_dir: Any = None         # silent path: short-circuits artifact writes
    profile_name: str = "advanced"
    session_ts: str = "test-run"
    parent_run_id: str | None = None
    project_alias: str | None = None
    no_interactive: bool = True

    # ── resources ──────────────────────────────────────────────────
    _ckpt: Any = None              # no checkpoint side-effects in this fixture
    _metrics: Any = None           # unused when output_dir is None
    _done_summary_profile: Any = None
    worktree_context: Any = None   # no teardown
    _worktree_cvar_token: Any = None
    _sandbox_cvar_token: Any = None

    def __init__(self) -> None:
        self.state = _FakeState()
        self.session: dict[str, Any] = {}

    # ── methods the silent service may call ────────────────────────
    def _effective_diff_cwd(self):  # pragma: no cover - never reached
        # output_dir is None → silent service never calls this method.
        raise AssertionError(
            "diff cwd should not be queried when output_dir is None"
        )

    def _run_commit_delivery(self, diff_cwd):  # pragma: no cover
        raise AssertionError(
            "commit delivery should not run when output_dir is None"
        )


class TestFinalizeProjectRunSilent:
    def test_finalize_project_run_emits_no_stdout_or_stderr(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Pin: the silent service must produce zero terminal output.

        Captures stdout AND stderr. A regression that reintroduces
        ``banner()`` / ``success()`` / ``warn()`` / ``print()`` in
        :func:`finalize_project_run` (or any helper it calls) will
        flunk this assertion immediately.
        """
        run = _Run()
        ctx = FinalizationContext(run=run)

        result = finalize_project_run(ctx)

        captured = capsys.readouterr()
        assert captured.out == "", (
            f"finalize_project_run leaked stdout:\n{captured.out!r}"
        )
        assert captured.err == "", (
            f"finalize_project_run leaked stderr:\n{captured.err!r}"
        )

        # Sanity: the silent service did its job (status was set to
        # ``done`` because state.halt is False and profile_name is
        # non-``plan``). The summary is empty because phase_log is
        # empty — that's correct.
        assert isinstance(result, FinalizationResult)
        assert result.status == "done"
        assert result.halt_reason is None
        assert result.summary_text == ""
        assert result.session_path is None
        assert result.metrics_path is None
        assert result.diff_path is None
        assert result.evidence_path is None
        assert result.mirrored_artifacts == []
        assert result.mirror_error is None
        assert result.worktree_teardown_message is None
        assert result.terminal_delivery.disposition is TerminalDeliveryDisposition.UNKNOWN

    def test_session_status_mutated_in_place(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Pin: even on the no-output_dir path, the silent service
        still mutates ``session["status"]``. This is what makes
        ``run_pipeline`` return the post-finalize session shape to
        callers — the value would be missing if a future edit gated
        status resolution on ``output_dir``.
        """
        run = _Run()
        ctx = FinalizationContext(run=run)
        assert "status" not in run.session

        finalize_project_run(ctx)

        assert run.session["status"] == "done"
        # Belt-and-suspenders: no terminal output even on this path.
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_plan_profile_routes_to_awaiting_human_review(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Pin: ``planning`` work kind + no phase_handoff_override = the
        plan-only ``awaiting_human_review`` tail. Still silent."""
        run = _Run()
        run.profile_name = "planning"
        ctx = FinalizationContext(run=run)

        result = finalize_project_run(ctx)

        assert result.status == "awaiting_human_review"
        assert run.session["status"] == "awaiting_human_review"
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_correction_route_evidence_stays_silent(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Pin: a correction run with stamped route evidence emits the
        extra DONE route entry (file + event side only) but still leaks
        no terminal output from the silent service.
        """
        run = _Run()
        run.session = {
            "phases": {
                "correction_triage": {
                    "kind": "gate_rerun",
                    "summary": "gates stale",
                    "route": {
                        "kind": "gate_rerun",
                        "skip_phases": ["implement", "review_changes"],
                        "halt": False,
                        "reason": "not applicable",
                    },
                },
                "final_acceptance": {"verdict": "APPROVED"},
            }
        }
        ctx = FinalizationContext(run=run)

        result = finalize_project_run(ctx)

        # The route line was surfaced on the result (file/event channel),
        # not the terminal.
        assert result.correction_route_line is not None
        assert "Correction route: gate_rerun" in result.correction_route_line
        assert result.correction_route_halted is False
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_companion_caveat_built_when_primary_committed_companion_dirty(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Pin: a committed primary + dirty companion yields a silent caveat.

        The silent service must surface the companion-delivery caveat on the
        ``FinalizationResult`` (so UI/SDK clients see the incomplete companion)
        WITHOUT printing anything — the durable ``multi_project_delivery`` block
        ``run.py`` propagated from the T1 disclosure is the only input.
        """
        run = _Run()
        run.session = {
            "multi_project_delivery": {
                "primary_status": "committed",
                "companions": [
                    {
                        "alias": "orcho-mcp",
                        "path": "/ws/orcho-mcp",
                        "state": "dirty",
                        "changed_paths": ["[orcho-mcp]/server.py"],
                    },
                ],
            },
        }
        ctx = FinalizationContext(run=run)

        result = finalize_project_run(ctx)

        assert result.status == "done"
        caveat = result.companion_caveat
        assert caveat is not None
        assert caveat.primary_status == "committed"
        assert [c["alias"] for c in caveat.dirty_companions] == ["orcho-mcp"]
        joined = "\n".join(caveat.lines)
        assert "Companion delivery incomplete" in joined
        assert "orcho-mcp" in joined
        assert "[orcho-mcp]/server.py" in joined
        # Actionable next step is present.
        assert "review and commit" in joined
        # Still silent.
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_no_companion_caveat_for_clean_single_repo(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Pin: a clean single-repo run (no block) carries no caveat."""
        run = _Run()
        ctx = FinalizationContext(run=run)

        result = finalize_project_run(ctx)

        assert result.companion_caveat is None
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_no_companion_caveat_when_all_companions_committed(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Pin: a fully-delivered multi-repo run (no dirty companion) → no caveat."""
        run = _Run()
        run.session = {
            "multi_project_delivery": {
                "primary_status": "committed",
                "companions": [
                    {
                        "alias": "orcho-mcp",
                        "path": "/ws/orcho-mcp",
                        "state": "committed",
                        "changed_paths": ["[orcho-mcp]/server.py"],
                    },
                ],
            },
        }
        ctx = FinalizationContext(run=run)

        result = finalize_project_run(ctx)

        assert result.companion_caveat is None
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_halt_path_records_halt_reason_and_block(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Pin: ``state.halt = True`` makes the silent service stamp
        ``session["halt_reason"]`` + the nested ``session["halt"]``
        block (the ADR 0035 invariant for downstream consumers).
        """
        run = _Run()
        run.state.halt = True
        run.state.halt_reason = "quality_gate_block"
        run.state.extras["_current_phase"] = "review_changes"
        ctx = FinalizationContext(run=run)

        result = finalize_project_run(ctx)

        assert result.status == "halted"
        assert result.halt_reason == "quality_gate_block"
        assert run.session["status"] == "halted"
        assert run.session["halt_reason"] == "quality_gate_block"
        assert run.session["halt"] == {
            "reason": "quality_gate_block",
            "phase": "review_changes",
        }
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""


def _git(path: Path, *args: str) -> None:
    import subprocess

    subprocess.run(
        ["git", *args],
        cwd=str(path),
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "orcho@example.test")
    _git(path, "config", "user.name", "Orcho Test")
    (path / "payload.py").write_text("value = 1\n", encoding="utf-8")
    _git(path, "add", "payload.py")
    _git(path, "commit", "-qm", "initial")


def _stub_finalize_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(
        "pipeline.evidence.write_bundle_or_placeholder",
        lambda output_dir, *, run_id, status: output_dir / "evidence.json",
    )
    monkeypatch.setattr(
        "pipeline.engine.artifact_mirror.mirror_to_projects",
        lambda *_a, **_kw: [],
    )
    monkeypatch.setattr(
        "pipeline.observability.context_pressure.format_context_summary",
        lambda _session: None,
    )
    monkeypatch.setattr(
        "core.infra.config.AppConfig.load",
        lambda: SimpleNamespace(artifacts={}, commit={}, accounting={}),
    )


def _make_done_run(project: Path, run_dir: Path, baseline: str, session: dict[str, Any]):
    from types import SimpleNamespace

    metrics = SimpleNamespace(
        save=lambda output_dir: output_dir / "metrics.json",
        summary_line=lambda: "Tokens: 0",
        as_dict=lambda: {},
        phases=[],
    )
    state = SimpleNamespace(halt=False, halt_reason=None, extras={}, phase_log={})
    return SimpleNamespace(
        output_dir=run_dir,
        task="# Orcho Task: approved retry reconciliation",
        session=session,
        state=state,
        profile_name="default",
        parent_run_id=None,
        project_alias=None,
        project_path=project,
        worktree_context=None,
        no_interactive=False,
        _metrics=metrics,
        _ckpt=None,
        _done_summary_profile=None,
        session_ts="20260626_000000",
        _worktree_cvar_token=None,
        _sandbox_cvar_token=None,
        _effective_diff_cwd=lambda: project,
        _commit_delivery_baseline=lambda: baseline,
        # No-op delivery: the now-APPROVED retry resolves to nothing-to-ship and
        # does NOT overwrite the stale rejected commit_delivery, exactly the
        # dogfood condition the reconciliation must repair.
        _run_commit_delivery=lambda _diff_cwd: None,
    )


def test_approved_retry_reconciles_run_end_and_meta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """finalize_project_run on an approved retry must reconcile run.end + meta.

    Dogfood shape: an earlier REJECTED attempt left top-level terminal-rejection
    markers and a phantom rejected ``commit_delivery`` gate. The later APPROVED
    final acceptance settles ``done``; the ``run.end`` payload must read
    ``status='done'`` with NO ``halt_reason``, and the persisted ``meta.json``
    must carry neither the stale terminal markers nor the rejected
    ``commit_delivery`` (run.end + meta consistent — plan requirement 5).
    """
    import json

    from pipeline.engine.run_diff import snapshot_worktree

    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    _init_repo(project)
    baseline = snapshot_worktree(project)
    assert baseline is not None
    (project / "payload.py").write_text("value = 2\n", encoding="utf-8")
    _stub_finalize_side_effects(monkeypatch)

    # Capture the run.end event payload off the real emit boundary.
    run_end: dict[str, Any] = {}

    real_emit = __import__(
        "core.observability.events", fromlist=["emit"]
    ).emit

    def _spy_emit(name: str, **payload: Any) -> Any:
        if name == "run.end":
            run_end.clear()
            run_end.update(payload)
        return real_emit(name, **payload)

    monkeypatch.setattr("core.observability.events.emit", _spy_emit)

    session = {
        "status": "done",
        "halt_reason": "final_acceptance_rejected",
        "halted_at": "2026-06-26T00:00:00+00:00",
        "rejected_outcome": {
            "phase": "final_acceptance",
            "reason": "final_acceptance_rejected",
            "status": "halted",
            "release_verdict": "REJECTED",
            "release_blockers": [{"severity": "high", "detail": "stale"}],
        },
        "halt": {
            "reason": "final_acceptance_rejected",
            "phase": "final_acceptance",
        },
        "commit_delivery": {
            "status": "not_applicable",
            "release_verdict": "REJECTED",
        },
        "phases": {
            "final_acceptance": {
                "approved": True,
                "verdict": "APPROVED",
                "ship_ready": True,
            },
        },
    }

    run = _make_done_run(project, run_dir, baseline, session)
    finalize_project_run(FinalizationContext(run=run))

    # run.end: clean done, no stale halt_reason.
    assert run_end["status"] == "done"
    assert "halt_reason" not in run_end

    # In-place session reconciled.
    assert run.session["status"] == "done"
    for key in (
        "halt_reason",
        "halted_at",
        "rejected_outcome",
        "delivery_override",
        "halt",
    ):
        assert key not in run.session
    assert "commit_delivery" not in run.session

    # Persisted meta.json is the same reconciled shape (run.end ↔ meta consistent).
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "done"
    for key in (
        "halt_reason",
        "halted_at",
        "rejected_outcome",
        "delivery_override",
        "halt",
        "commit_delivery",
    ):
        assert key not in meta

    # Still silent.
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_rejected_terminal_seam_surfaces_engine_backstop_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Seam: a forced engine-backstop REJECT surfaces the engine cause + halts.

    Dogfood shape via the finalization seam (not the reducer directly): the
    persisted ``final_acceptance`` reports ``ship_ready=True`` with an empty
    ``release_blockers`` and a positive ``short_summary``, but the engine receipt
    backstop forced ``verdict=REJECTED`` with an ``engine_backstop`` +
    ``verification_gaps`` record. Delivery was NOT applied. The seam must read
    those facts off the SAME record, flip the stale ``done`` to ``halted``, and
    persist a ``rejected_outcome`` whose marker names the engine reason and does
    not surface the positive agent summary as the headline.
    """
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    _init_repo(project)
    _stub_finalize_side_effects(monkeypatch)

    backstop_gap = {
        "risk": "required receipts unproven",
        "missing_evidence": "no passing pytest receipt for the touched slice",
        "required_check": "python -m pytest -q tests/unit/pipeline",
    }
    session = {
        "status": "done",
        # Engine backstop forced REJECTED despite the agent's positive view.
        "phases": {
            "final_acceptance": {
                "verdict": "REJECTED",
                "ship_ready": True,
                "release_blockers": [],
                "short_summary": "All acceptance criteria met; ship it.",
                "engine_backstop": {
                    "reason": "required_receipts_unproven",
                    "gaps": [backstop_gap],
                },
                "verification_gaps": [backstop_gap],
            },
        },
    }

    # No diff captured, delivery not applied: a verify-only style done that the
    # seam must reconcile to halted via the rejected-release terminal.
    run = _make_done_run(project, run_dir, baseline="HEAD", session=session)
    finalize_project_run(FinalizationContext(run=run))

    # The stale done was flipped to halted by the rejected-release terminal.
    assert run.session["status"] == "halted"
    marker = run.session["rejected_outcome"]
    assert marker["engine_backstop"] == {
        "reason": "required_receipts_unproven",
        "gaps": [backstop_gap],
    }
    assert marker["verification_gaps"] == [backstop_gap]
    assert marker["message"].startswith(
        "Engine backstop rejected the release: required_receipts_unproven."
    )
    # The positive agent summary is demoted, not the headline.
    assert marker["short_summary"] == (
        "(superseded agent view) All acceptance criteria met; ship it."
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


# ── T2: from_run_plan follow-up supersedes a rejected-FA / fix parent ─────────
#
# When a ``--from-run-plan`` child actually delivers, the rejected-FA /
# correction parent it was launched to fix must stop reading as an active
# correction candidate: its phantom rejected ``commit_delivery`` gate and
# rejected residue are evicted and the parent is settled ``done`` with a
# ``superseded_by_followup`` marker — across every read surface
# (``delivery_decision_state`` here stands in for the shared diagnosis source).


def _rejected_fa_parent_meta(parent_run_id: str) -> dict[str, Any]:
    return {
        "status": "halted",
        "halt_reason": "final_acceptance_rejected",
        "halted_at": "2026-06-26T00:00:00+00:00",
        "project": "/p",
        "halt": {"reason": "final_acceptance_rejected", "phase": "final_acceptance"},
        "rejected_outcome": {
            "phase": "final_acceptance",
            "reason": "final_acceptance_rejected",
            "status": "halted",
            "release_verdict": "REJECTED",
            "release_blockers": [{"id": "RB1", "detail": "data loss"}],
        },
        "commit_delivery": {
            "run_id": parent_run_id,
            "status": "not_applicable",
            "release_verdict": "REJECTED",
            "release_blockers": [{"id": "RB1", "detail": "data loss"}],
        },
    }


def _fix_marked_parent_meta(parent_run_id: str) -> dict[str, Any]:
    return {
        "status": "halted",
        "halt_reason": "commit_decision_fix",
        "project": "/p",
        "commit_delivery": {
            "run_id": parent_run_id,
            "status": "fix_requested",
            "release_verdict": "REJECTED",
            "release_blockers": [{"id": "RB1", "detail": "data loss"}],
        },
    }


def _write_meta(run_dir: Path, meta: dict[str, Any]) -> None:
    import json

    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8",
    )


def _followup_child(
    *,
    output_dir: Path,
    parent_run_id: str | None,
    delivery_status: str | None,
    session_ts: str,
) -> Any:
    from types import SimpleNamespace

    extras: dict[str, Any] = {}
    session: dict[str, Any] = {
        "status": "done",
        "resume_mode": "followup",
        "profile": "correction",
    }
    if parent_run_id is not None:
        session["parent_run_id"] = parent_run_id
        (output_dir / "correction_context.md").write_text(
            "# Correction Context\n", encoding="utf-8",
        )
    if delivery_status is not None:
        session["commit_delivery"] = {"status": delivery_status}
    return SimpleNamespace(
        output_dir=output_dir,
        state=SimpleNamespace(extras=extras),
        session=session,
        session_ts=session_ts,
    )


def _parent_meta_after(runs_dir: Path, parent_run_id: str) -> dict[str, Any]:
    import json

    return json.loads(
        (runs_dir / parent_run_id / "meta.json").read_text(encoding="utf-8"),
    )


class TestSupersedeParentCorrectionAfterFollowup:
    @pytest.mark.parametrize("extras", [None, "not-a-mapping"])
    def test_non_mapping_state_extras_is_a_safe_noop(
        self, tmp_path: Path, extras: Any,
    ) -> None:
        from pipeline.project.finalization import (
            _supersede_parent_correction_after_followup,
        )

        child_dir = tmp_path / "runs" / "child"
        child_dir.mkdir(parents=True)
        child = _followup_child(
            output_dir=child_dir,
            parent_run_id=None,
            delivery_status="committed",
            session_ts="child",
        )
        child.state.extras = extras

        _supersede_parent_correction_after_followup(child)

        assert child.session["status"] == "done"

    @pytest.mark.parametrize(
        "meta_factory",
        [_rejected_fa_parent_meta, _fix_marked_parent_meta],
    )
    def test_successful_child_closes_parent_correction(
        self, tmp_path: Path, meta_factory: Any,
    ) -> None:
        from pipeline.project.finalization import (
            _supersede_parent_correction_after_followup,
        )
        from sdk.run_control.delivery import delivery_decision_state

        runs_dir = tmp_path / "runs"
        parent_id = "20260101_000000"
        _write_meta(runs_dir / parent_id, meta_factory(parent_id))
        child_dir = runs_dir / "20260102_000000"
        child_dir.mkdir(parents=True)
        child = _followup_child(
            output_dir=child_dir,
            parent_run_id=parent_id,
            delivery_status="committed",
            session_ts="20260102_000000",
        )

        _supersede_parent_correction_after_followup(child)

        # delivery_decision_state(parent) is no longer a decidable correction.
        state = delivery_decision_state(parent_id, runs_dir=runs_dir, cwd=None)
        assert state.decidable is False
        assert state.kind == "none"

        meta = _parent_meta_after(runs_dir, parent_id)
        assert meta["status"] == "done"
        # The phantom gate + rejected residue (both carrying release_blockers) gone.
        for key in (
            "commit_delivery",
            "rejected_outcome",
            "halt_reason",
            "halted_at",
            "halt",
            "delivery_override",
            "multi_project_delivery",
        ):
            assert key not in meta
        marker = meta["superseded_by_followup"]
        assert marker["child_run_id"] == "20260102_000000"
        assert marker["delivery_status"] == "committed"

    def test_skipped_delivery_also_supersedes(self, tmp_path: Path) -> None:
        from pipeline.project.finalization import (
            _supersede_parent_correction_after_followup,
        )

        runs_dir = tmp_path / "runs"
        parent_id = "20260101_000000"
        _write_meta(runs_dir / parent_id, _rejected_fa_parent_meta(parent_id))
        child_dir = runs_dir / "20260102_000000"
        child_dir.mkdir(parents=True)
        child = _followup_child(
            output_dir=child_dir,
            parent_run_id=parent_id,
            delivery_status="skipped",
            session_ts="20260102_000000",
        )

        _supersede_parent_correction_after_followup(child)

        meta = _parent_meta_after(runs_dir, parent_id)
        assert meta["status"] == "done"
        assert meta["superseded_by_followup"]["delivery_status"] == "skipped"

    def test_unsuccessful_child_delivery_is_noop(self, tmp_path: Path) -> None:
        from pipeline.project.finalization import (
            _supersede_parent_correction_after_followup,
        )

        runs_dir = tmp_path / "runs"
        parent_id = "20260101_000000"
        original = _rejected_fa_parent_meta(parent_id)
        _write_meta(runs_dir / parent_id, original)
        child_dir = runs_dir / "20260102_000000"
        child_dir.mkdir(parents=True)
        child = _followup_child(
            output_dir=child_dir,
            parent_run_id=parent_id,
            delivery_status="pending",  # parked, not delivered
            session_ts="20260102_000000",
        )

        _supersede_parent_correction_after_followup(child)

        # Parent untouched — still an active correction terminal.
        meta = _parent_meta_after(runs_dir, parent_id)
        assert meta["status"] == "halted"
        assert meta["halt_reason"] == "final_acceptance_rejected"
        assert "superseded_by_followup" not in meta
        assert meta["commit_delivery"]["status"] == "not_applicable"

    def test_missing_from_run_plan_link_is_noop(self, tmp_path: Path) -> None:
        from pipeline.project.finalization import (
            _supersede_parent_correction_after_followup,
        )

        runs_dir = tmp_path / "runs"
        parent_id = "20260101_000000"
        _write_meta(runs_dir / parent_id, _rejected_fa_parent_meta(parent_id))
        child_dir = runs_dir / "20260102_000000"
        child_dir.mkdir(parents=True)
        # No plan_source_run_id on extras → not a from_run_plan child.
        child = _followup_child(
            output_dir=child_dir,
            parent_run_id=None,
            delivery_status="committed",
            session_ts="20260102_000000",
        )

        _supersede_parent_correction_after_followup(child)

        meta = _parent_meta_after(runs_dir, parent_id)
        assert meta["status"] == "halted"
        assert "superseded_by_followup" not in meta

    def test_parent_not_correction_terminal_is_noop(self, tmp_path: Path) -> None:
        from pipeline.project.finalization import (
            _supersede_parent_correction_after_followup,
        )

        runs_dir = tmp_path / "runs"
        parent_id = "20260101_000000"
        # A non-correction halt (parse_error) must never be superseded.
        _write_meta(
            runs_dir / parent_id,
            {"status": "halted", "halt_reason": "parse_error", "project": "/p"},
        )
        child_dir = runs_dir / "20260102_000000"
        child_dir.mkdir(parents=True)
        child = _followup_child(
            output_dir=child_dir,
            parent_run_id=parent_id,
            delivery_status="committed",
            session_ts="20260102_000000",
        )

        _supersede_parent_correction_after_followup(child)

        meta = _parent_meta_after(runs_dir, parent_id)
        assert meta["status"] == "halted"
        assert meta["halt_reason"] == "parse_error"
        assert "superseded_by_followup" not in meta

    def test_idempotent_on_rerun(self, tmp_path: Path) -> None:
        from pipeline.project.finalization import (
            _supersede_parent_correction_after_followup,
        )

        runs_dir = tmp_path / "runs"
        parent_id = "20260101_000000"
        _write_meta(runs_dir / parent_id, _rejected_fa_parent_meta(parent_id))
        child_dir = runs_dir / "20260102_000000"
        child_dir.mkdir(parents=True)
        child = _followup_child(
            output_dir=child_dir,
            parent_run_id=parent_id,
            delivery_status="committed",
            session_ts="20260102_000000",
        )

        _supersede_parent_correction_after_followup(child)
        first = _parent_meta_after(runs_dir, parent_id)
        _supersede_parent_correction_after_followup(child)
        second = _parent_meta_after(runs_dir, parent_id)

        assert first == second
