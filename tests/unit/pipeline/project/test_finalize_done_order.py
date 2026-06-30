"""Narrow wrapper-test: outcome line precedes the DONE banner.

``finalize_with_terminal_output`` calls the silent finalization
service (which itself runs ``_run_commit_delivery`` and prints the
delivery outcome via ``render_delivery_outcome``) and then prints the
``[DONE] Pipeline complete`` header. This test pins that order on the
skip branch.

Prompt is intentionally NOT exercised here — that contract is the job
of ``test_run_commit_delivery_order.py`` (T6a). T6b only watches the
outcome→DONE ordering, so ``resolve_commit_delivery`` is replaced
with a tiny factory returning a ready-made
``CommitDeliveryDecision(action='skip', status='pending', ...)``. The
real ``apply_commit_delivery`` then short-circuits on the skip branch,
and the real ``render_delivery_outcome`` produces the "⏭ Delivery
skipped" line.

Finalize side-effects are stubbed explicitly so the test does not
depend on real disk writes / event store / config / mirror / worktree
teardown:

1. ``pipeline.engine.diff_apply_check.capture_run_diff_with_apply_check`` —
   returns a fake Path.
2. ``pipeline.project.finalization.save_session`` — no-op writer.
3. ``stub._metrics`` — SimpleNamespace with save/summary_line/as_dict/phases.
4. ``stub._ckpt`` — None (the silent service guards on truthiness).
5. ``pipeline.evidence.write_bundle_or_placeholder`` — returns a fake Path.
6. ``pipeline.engine.artifact_mirror.mirror_to_projects`` — returns [].
7. ``stub._effective_diff_cwd`` — returns project_path.
8. ``stub._done_summary_profile`` — None (no profile chips).
9. ``core.infra.config.AppConfig.load`` — deterministic SimpleNamespace.
10. ``pipeline.observability.context_pressure.format_context_summary``
    — returns None (no context-pressure line).

If ``finalize_project_run`` ever picks up a new required dependency,
this test breaks honestly — that is the intended trade-off (explicit
fixture wiring vs. silent drift).

``render_delivery_outcome``, ``render_phase_header``, ``success``,
and ``finalize_with_terminal_output`` are NOT monkeypatched — their
real output is what this test inspects.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import pipeline.engine.commit_delivery as cd
from core.io.ansi import C, get_color_enabled, set_color_enabled, strip_ansi
from pipeline.engine.diff_apply_check import CapturedRunDiff
from pipeline.project.bootstrap import init_session_with_atexit
from pipeline.project.finalization import (
    FinalizationContext,
    _apply_no_diff_final_acceptance_outcome,
    _apply_rejected_release_terminal_outcome,
    _resolve_terminal_status,
    finalize_project_run,
    finalize_with_terminal_output,
)
from pipeline.project.run import _PipelineRun
from pipeline.project.types import PresentationPolicy


def test_outcome_line_precedes_done_banner_on_skip(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)

    # ── 1) Stub resolve_commit_delivery: factory returns a ready-made
    # pending skip decision, so _prompt_action is NEVER reached.
    def _resolve_skip(**kwargs: object) -> cd.CommitDeliveryDecision:
        return cd.CommitDeliveryDecision(
            action="skip",
            status="pending",
            run_id=str(kwargs.get("run_id", "r")),
            decision_id="d",
            project_path=kwargs.get("project_dir"),  # type: ignore[arg-type]
            source_path=kwargs.get("source_worktree"),  # type: ignore[arg-type]
            baseline_ref=str(kwargs.get("baseline_ref", "HEAD")),
        )

    monkeypatch.setattr(cd, "resolve_commit_delivery", _resolve_skip)

    # ── 2) Stub finalization side-effects (the explicit list above).
    # capture_run_diff_with_apply_check (silent service step 2).
    monkeypatch.setattr(
        "pipeline.engine.diff_apply_check.capture_run_diff_with_apply_check",
        lambda *_a, **_kw: CapturedRunDiff(
            path=run_dir / "diff.patch", apply_check=None
        ),
    )
    # save_session (silent service step 6, module-level import in
    # finalization). The file must exist on disk because vtrace's
    # arguments are eagerly evaluated (``session_path.stat().st_size``)
    # before the verbose-mode guard inside vtrace decides to no-op.
    def _save_session(output_dir: Path, _session: object) -> Path:
        path = output_dir / "session.json"
        path.write_text("{}", encoding="utf-8")
        return path

    monkeypatch.setattr(
        "pipeline.project.finalization.save_session",
        _save_session,
    )
    # write_bundle_or_placeholder (silent service step 6, local import).
    monkeypatch.setattr(
        "pipeline.evidence.write_bundle_or_placeholder",
        lambda output_dir, *, run_id, status: output_dir / "evidence.json",
    )
    # mirror_to_projects (silent service step 8, local import).
    monkeypatch.setattr(
        "pipeline.engine.artifact_mirror.mirror_to_projects",
        lambda *_a, **_kw: [],
    )
    # format_context_summary (silent service step 6, local import).
    monkeypatch.setattr(
        "pipeline.observability.context_pressure.format_context_summary",
        lambda _session: None,
    )
    # AppConfig.load — both _run_commit_delivery (for commit cfg) and
    # silent service step 8 (for artifacts cfg) call this; return a
    # deterministic SimpleNamespace so neither hits the developer's
    # ~/.config/orcho.json.
    monkeypatch.setattr(
        "core.infra.config.AppConfig.load",
        lambda: SimpleNamespace(
            artifacts={},
            commit={"enabled": True, "add_untracked": False},
            accounting={},
        ),
    )

    # ── 3) Minimal _PipelineRun stub. Every attribute the silent
    # service and the wrapper read is set explicitly.
    metrics = SimpleNamespace(
        save=lambda output_dir: output_dir / "metrics.json",
        summary_line=lambda: "Tokens: 0",
        as_dict=lambda: {},
        phases=[],
    )
    state = SimpleNamespace(
        halt=False,
        halt_reason=None,
        extras={},
        phase_log={},
    )
    stub = SimpleNamespace(
        # _PipelineRun shape for finalize_project_run + wrapper.
        output_dir=run_dir,
        task="# Orcho Task: Final summary identity",
        session={"status": "done"},
        state=state,
        profile_name="default",
        parent_run_id=None,
        project_alias=None,
        project_path=project_dir,
        worktree_context=None,
        no_interactive=False,
        _metrics=metrics,
        _ckpt=None,
        _done_summary_profile=None,
        session_ts="20260603_000000",
        _worktree_cvar_token=None,
        _sandbox_cvar_token=None,
        # _commit_delivery_baseline is read inside _run_commit_delivery.
        _commit_delivery_baseline=lambda: "HEAD",
    )
    # Bind the real _PipelineRun methods we want to exercise.
    stub._effective_diff_cwd = lambda: project_dir
    stub._run_commit_delivery = (
        lambda diff_cwd: _PipelineRun._run_commit_delivery(stub, diff_cwd)
    )

    # ── 4) Drive the wrapper and pin the ordering.
    finalize_with_terminal_output(FinalizationContext(run=stub))

    out = strip_ansi(capsys.readouterr().out)

    assert "Delivery skipped" in out, (
        f"outcome line missing from stdout: {out!r}"
    )
    assert "Pipeline complete" in out, (
        f"DONE banner missing from stdout: {out!r}"
    )
    assert "Run:     20260603_000000" in out
    assert "Task:    Orcho Task: Final summary identity" in out
    assert out.index("Delivery skipped") < out.index("Pipeline complete"), (
        "DONE banner printed before delivery outcome — ordering contract "
        f"violated:\n{out!r}"
    )


def test_no_diff_final_acceptance_reject_prints_halted_not_done(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)

    calls: list[tuple[str, object]] = []

    def _capture_run_diff_with_apply_check(
        *args: object, **kwargs: object
    ) -> None:
        calls.append(("capture", {"args": args, "kwargs": kwargs}))
        return None

    monkeypatch.setattr(
        "pipeline.engine.diff_apply_check.capture_run_diff_with_apply_check",
        _capture_run_diff_with_apply_check,
    )

    def _save_session(output_dir: Path, _session: object) -> Path:
        path = output_dir / "session.json"
        path.write_text("{}", encoding="utf-8")
        return path

    monkeypatch.setattr(
        "pipeline.project.finalization.save_session",
        _save_session,
    )
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

    metrics = SimpleNamespace(
        save=lambda output_dir: output_dir / "metrics.json",
        summary_line=lambda: "Tokens: 0",
        as_dict=lambda: {},
        phases=[],
    )
    state = SimpleNamespace(halt=False, halt_reason=None, extras={}, phase_log={})
    stub = SimpleNamespace(
        output_dir=run_dir,
        task="# Orcho Task: verify-only",
        session={
            "status": "done",
            "phases": {
                "review_changes": {
                    "clean": True,
                    "skipped": "no uncommitted changes",
                },
                "final_acceptance": {
                    "approved": False,
                    "verdict": "REJECTED",
                    "ship_ready": False,
                },
            },
        },
        state=state,
        profile_name="default",
        parent_run_id=None,
        project_alias=None,
        project_path=project_dir,
        worktree_context=None,
        no_interactive=False,
        _metrics=metrics,
        _ckpt=None,
        _done_summary_profile=None,
        session_ts="20260608_000000",
        _worktree_cvar_token=None,
        _sandbox_cvar_token=None,
        _effective_diff_cwd=lambda: project_dir,
        _commit_delivery_baseline=lambda: "run-baseline-tree",
        _run_commit_delivery=lambda _diff_cwd: calls.append(("delivery", _diff_cwd)),
    )

    result = finalize_with_terminal_output(FinalizationContext(run=stub))

    out = strip_ansi(capsys.readouterr().out)
    assert result.status == "halted"
    assert stub.session["halt_reason"] == "final_acceptance_no_diff"
    assert len(calls) >= 2
    assert calls[0][0] == "capture"
    assert calls[1] == ("delivery", project_dir)
    assert calls[0][1]["kwargs"] == {
        "baseline_ref": "run-baseline-tree",
    }
    assert "HALTED" in out
    assert "final acceptance found no diff" in out
    assert "Pipeline complete" not in out


# ── rejected real-contract final_acceptance backstop (real diff) ───────────
#
# These pin the single-project finalization backstop: a GENUINE release-
# contract ``final_acceptance`` REJECTION over a *real* diff must not persist
# as green ``done`` — finalize flips it to a resumable ``halted`` non-success
# (``halt_reason='final_acceptance_rejected'``). The negative cases pin that
# the backstop is a strict no-op for dry-run, a non-contract verdict, and an
# APPROVED verdict. All four drive the real ``finalize_with_terminal_output``
# path; only the silent-service side-effects are stubbed.


def _real_contract_final_acceptance(*, verdict: str, ship_ready: bool,
                                    approved: bool,
                                    contract_status: object) -> dict:
    """A dual-shape final_acceptance entry as the handler writes it."""
    return {
        "approved": approved,
        "verdict": verdict,
        "ship_ready": ship_ready,
        "release_blockers": [
            {"title": "Tests missing for the new branch"},
            {"title": "Interface contract drift unresolved"},
        ],
        "verification_gaps": [],
        "contract_status": contract_status,
    }


def _real_diff_reject_stub(
    run_dir: Path,
    project_dir: Path,
    *,
    final_acceptance: dict,
    dry_run: bool,
    phase_log_verdict: str = "REJECTED",
) -> SimpleNamespace:
    """A _PipelineRun stub whose diff capture yields a non-None diff_path.

    ``_patch_finalization_side_effects`` stubs
    ``capture_run_diff_with_apply_check`` to return a ``CapturedRunDiff`` with
    a real ``path`` (apply_check=None), so ``_record_diff_patch_block`` returns
    a non-None ``diff_path`` — the precondition the rejected backstop gates on.
    ``state.dry_run`` is set explicitly (the production state object carries it;
    the older stubs omitted it).
    """
    metrics = SimpleNamespace(
        save=lambda output_dir: output_dir / "metrics.json",
        summary_line=lambda: "Tokens: 0",
        as_dict=lambda: {},
        phases=[],
    )
    state = SimpleNamespace(
        halt=False,
        halt_reason=None,
        extras={},
        # The DONE summary chip is rendered from state.phase_log, not the
        # session phases the backstop reads — so the chip needs its own entry.
        phase_log={"final_acceptance": {"verdict": phase_log_verdict}},
        dry_run=dry_run,
    )
    return SimpleNamespace(
        output_dir=run_dir,
        task="# Orcho Task: rejected release with a real diff",
        session={
            "status": "done",
            "phases": {
                # No "no uncommitted changes" skip marker: a real diff exists.
                "review_changes": {"clean": False, "verdict": "APPROVED"},
                "final_acceptance": final_acceptance,
            },
        },
        state=state,
        profile_name="default",
        parent_run_id=None,
        project_alias=None,
        project_path=project_dir,
        worktree_context=None,
        no_interactive=False,
        _metrics=metrics,
        _ckpt=None,
        _done_summary_profile=None,
        session_ts="20260620_000000",
        _worktree_cvar_token=None,
        _sandbox_cvar_token=None,
        _effective_diff_cwd=lambda: project_dir,
        _commit_delivery_baseline=lambda: "run-baseline-tree",
        # Delivery is a no-op here (simulates skipped/inapplicable delivery
        # that leaves status on "done" for the backstop to act on).
        _run_commit_delivery=lambda _diff_cwd: None,
    )


def test_rejected_contract_with_real_diff_becomes_halted_not_done(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    _patch_finalization_side_effects(monkeypatch, run_dir)

    stub = _real_diff_reject_stub(
        run_dir,
        project_dir,
        final_acceptance=_real_contract_final_acceptance(
            verdict="REJECTED",
            ship_ready=False,
            approved=False,
            contract_status={"task_contract": "incomplete", "tests": "missing"},
        ),
        dry_run=False,
    )

    # Capture the durable event stream at the single module emit() seam so the
    # run.end payload — what events_summary/diagnose project from — is pinned,
    # not just the in-memory session. If a future edit moves the rejected
    # backstop after emit('run.end') (or otherwise breaks ordering), this
    # assertion fails instead of silently re-emitting the done+reject lie.
    run_end_payloads: list[dict[str, object]] = []

    def _emit(kind: str, **payload: object) -> None:
        if kind == "run.end":
            run_end_payloads.append(payload)

    monkeypatch.setattr("core.observability.events.emit", _emit)

    result = finalize_with_terminal_output(FinalizationContext(run=stub))

    out = strip_ansi(capsys.readouterr().out)
    assert result.status == "halted"
    assert stub.session["status"] == "halted"
    assert stub.session["halt_reason"] == "final_acceptance_rejected"
    # The durable rejected-outcome block records the rejection + blockers.
    rejected = stub.session["rejected_outcome"]
    assert rejected["reason"] == "final_acceptance_rejected"
    assert rejected["status"] == "halted"
    assert rejected["release_blockers"][0]["title"] == (
        "Tests missing for the new branch"
    )
    # Summary still carries the honest per-phase chip; no done+reject lie.
    assert "final_acceptance=reject" in result.summary_text
    assert "release rejected" in out
    assert "Pipeline complete" not in out
    # run.end must carry the honest non-success state: exactly one run.end,
    # status flipped off 'done', the rejected halt_reason, and the per-phase
    # reject chip — so events_summary/diagnose never see done+reject.
    assert len(run_end_payloads) == 1, run_end_payloads
    run_end = run_end_payloads[0]
    assert run_end["status"] == "halted"
    assert run_end["status"] != "done"
    assert run_end["halt_reason"] == "final_acceptance_rejected"
    assert "final_acceptance=reject" in str(run_end["summary"])


def test_rejected_contract_dry_run_is_backstop_noop(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    _patch_finalization_side_effects(monkeypatch, run_dir)

    stub = _real_diff_reject_stub(
        run_dir,
        project_dir,
        final_acceptance=_real_contract_final_acceptance(
            verdict="REJECTED",
            ship_ready=False,
            approved=False,
            contract_status={"task_contract": "incomplete", "tests": "missing"},
        ),
        dry_run=True,
    )

    result = finalize_with_terminal_output(FinalizationContext(run=stub))

    capsys.readouterr()
    # Backstop is a strict no-op under dry-run: status stays done, and it must
    # never stamp the rejected halt_reason.
    assert result.status == "done"
    assert stub.session["status"] == "done"
    assert stub.session.get("halt_reason") != "final_acceptance_rejected"
    assert "no_op_outcome" not in stub.session


def test_rejected_non_contract_verdict_is_backstop_noop(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    _patch_finalization_side_effects(monkeypatch, run_dir)

    # Parse-error shape: REJECTED verdict but contract_status is None — a hard
    # schema halt, not a real release verdict. The backstop must not fire.
    stub = _real_diff_reject_stub(
        run_dir,
        project_dir,
        final_acceptance=_real_contract_final_acceptance(
            verdict="REJECTED",
            ship_ready=False,
            approved=False,
            contract_status=None,
        ),
        dry_run=False,
    )

    result = finalize_with_terminal_output(FinalizationContext(run=stub))

    capsys.readouterr()
    assert result.status == "done"
    assert stub.session["status"] == "done"
    assert stub.session.get("halt_reason") != "final_acceptance_rejected"
    assert "no_op_outcome" not in stub.session


def test_no_diff_synth_stub_reject_is_rejected_backstop_noop(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    _patch_finalization_side_effects(monkeypatch, run_dir)

    # No-diff synth stub shape (_write_no_diff_final_acceptance): a non-empty
    # contract_status BUT skipped / diff='none' / no_change_outcome markers.
    # That path is owned by the no-diff helper; the rejected backstop must
    # treat it as non-contract and leave its decision untouched.
    synth = _real_contract_final_acceptance(
        verdict="REJECTED",
        ship_ready=False,
        approved=False,
        contract_status={"task_contract": "incomplete", "tests": "missing"},
    )
    synth["skipped"] = "implement delivery incomplete"
    synth["diff"] = "none"
    synth["no_change_outcome"] = False
    stub = _real_diff_reject_stub(
        run_dir, project_dir, final_acceptance=synth, dry_run=False,
    )

    result = finalize_with_terminal_output(FinalizationContext(run=stub))

    capsys.readouterr()
    # The rejected (real-diff) backstop does not fire on the synth stub, so it
    # never stamps the rejected halt_reason.
    assert stub.session.get("halt_reason") != "final_acceptance_rejected"
    no_op = stub.session.get("no_op_outcome")
    if isinstance(no_op, dict):
        assert no_op.get("reason") != "final_acceptance_rejected"
    assert result is not None


def test_approved_contract_with_real_diff_stays_done(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    _patch_finalization_side_effects(monkeypatch, run_dir)

    approved = _real_contract_final_acceptance(
        verdict="APPROVED",
        ship_ready=True,
        approved=True,
        contract_status={"task_contract": "satisfied", "tests": "sufficient"},
    )
    approved["release_blockers"] = []
    stub = _real_diff_reject_stub(
        run_dir,
        project_dir,
        final_acceptance=approved,
        dry_run=False,
        phase_log_verdict="APPROVED",
    )

    result = finalize_with_terminal_output(FinalizationContext(run=stub))

    out = strip_ansi(capsys.readouterr().out)
    assert result.status == "done"
    assert stub.session["status"] == "done"
    assert "halt_reason" not in stub.session
    assert "final_acceptance=ok" in result.summary_text
    assert "Pipeline complete" in out


def test_durable_meta_never_done_with_real_contract_reject(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Consistency invariant: after finalization a run must never carry the
    # contradictory combination status=='done' AND a genuine release-contract
    # final_acceptance verdict=='REJECTED'.
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    _patch_finalization_side_effects(monkeypatch, run_dir)

    stub = _real_diff_reject_stub(
        run_dir,
        project_dir,
        final_acceptance=_real_contract_final_acceptance(
            verdict="REJECTED",
            ship_ready=False,
            approved=False,
            contract_status={"task_contract": "incomplete", "tests": "missing"},
        ),
        dry_run=False,
    )

    result = finalize_with_terminal_output(FinalizationContext(run=stub))

    capsys.readouterr()
    fa = stub.session["phases"]["final_acceptance"]
    real_contract_reject = (
        str(fa.get("verdict", "")).upper() == "REJECTED"
        and isinstance(fa.get("contract_status"), dict)
        and bool(fa.get("contract_status"))
    )
    assert real_contract_reject
    assert not (stub.session["status"] == "done" and real_contract_reject)
    assert result.status == "halted"


# ── terminal-status consolidation contract (T1 helpers, real paths) ────────
#
# These pin the *observable* lifecycle contract at the real call sites
# (not the helper unit tests in tests/unit/pipeline/run_state): the
# stale-handoff policy must hold through ``_resolve_terminal_status``,
# ``_PipelineRun._record_phase_failure``, and the atexit-interrupted hook.


def test_resolve_terminal_status_halted_clears_phase_handoff() -> None:
    # A state.halt termination (e.g. quality-gate HALT) must flip to
    # halted, stamp the top-level halt_reason, clear any stale active
    # phase_handoff, and keep the nested ``halt`` compat block.
    state = SimpleNamespace(
        halt=True,
        halt_reason="quality_gate_halt",
        extras={"_current_phase": "review_changes"},
    )
    run = SimpleNamespace(
        state=state,
        profile_name="default",
        session={
            "status": "running",
            "phase_handoff": {"id": "h1", "phase": "review_changes"},
        },
    )
    _resolve_terminal_status(run)
    assert run.session["status"] == "halted"
    assert run.session["halt_reason"] == "quality_gate_halt"
    assert "phase_handoff" not in run.session
    assert run.session["halt"] == {
        "reason": "quality_gate_halt",
        "phase": "review_changes",
    }


def test_resolve_terminal_status_done_clears_stale_phase_handoff() -> None:
    # The done leaf defensively clears a stale phase_handoff (it should
    # already be gone after a handoff continue, but done is a settled
    # terminal). No halt_reason is introduced on success.
    state = SimpleNamespace(halt=False, halt_reason=None, extras={})
    run = SimpleNamespace(
        state=state,
        profile_name="default",
        session={
            "status": "running",
            "phase_handoff": {"id": "h1", "phase": "validate_plan"},
        },
    )
    _resolve_terminal_status(run)
    assert run.session["status"] == "done"
    assert "phase_handoff" not in run.session
    assert "halt_reason" not in run.session


def test_no_diff_final_acceptance_reject_becomes_explicit_no_op_halt() -> None:
    run = SimpleNamespace(
        session={
            "status": "done",
            "phases": {
                "review_changes": {
                    "clean": True,
                    "skipped": "no uncommitted changes",
                },
                "final_acceptance": {
                    "approved": False,
                    "verdict": "REJECTED",
                    "ship_ready": False,
                    "verification_gaps": [{
                        "risk": "No review target",
                        "missing": "No uncommitted diff",
                    }],
                },
            },
        },
    )

    _apply_no_diff_final_acceptance_outcome(run, diff_path=None)

    assert run.session["status"] == "halted"
    assert run.session["halt_reason"] == "final_acceptance_no_diff"
    assert run.session["no_op_outcome"] == {
        "phase": "final_acceptance",
        "review_target": "uncommitted",
        "diff": "none",
        "reason": "final_acceptance_no_diff",
        "status": "halted",
        "message": (
            "Final acceptance rejected the run, but review_changes skipped "
            "because there was no uncommitted diff to review or deliver."
        ),
    }


def test_no_diff_final_acceptance_approved_records_no_change_outcome() -> None:
    run = SimpleNamespace(
        session={
            "status": "done",
            "phases": {
                "review_changes": {
                    "clean": True,
                    "skipped": "no uncommitted changes",
                },
                "final_acceptance": {
                    "approved": True,
                    "verdict": "APPROVED",
                    "ship_ready": True,
                },
            },
        },
    )

    _apply_no_diff_final_acceptance_outcome(run, diff_path=None)

    assert run.session["status"] == "done"
    assert "halt_reason" not in run.session
    assert run.session["no_change_outcome"] == {
        "phase": "final_acceptance",
        "review_target": "uncommitted",
        "diff": "none",
        "reason": "verification_no_changes",
        "status": "done",
        "message": (
            "Final acceptance approved a verification-only run that "
            "produced no file changes to review or deliver."
        ),
    }


def test_no_diff_final_acceptance_marker_records_no_change_without_review_entry() -> None:
    run = SimpleNamespace(
        session={
            "status": "done",
            "phases": {
                "final_acceptance": {
                    "approved": True,
                    "verdict": "APPROVED",
                    "ship_ready": True,
                    "review_target": "not_applicable",
                    "diff": "none",
                },
            },
        },
    )

    _apply_no_diff_final_acceptance_outcome(run, diff_path=None)

    assert run.session["status"] == "done"
    assert run.session["no_change_outcome"]["reason"] == (
        "verification_no_changes"
    )


def test_no_diff_final_acceptance_marker_halts_without_review_entry() -> None:
    run = SimpleNamespace(
        session={
            "status": "done",
            "phases": {
                "final_acceptance": {
                    "approved": False,
                    "verdict": "REJECTED",
                    "ship_ready": False,
                    "review_target": "not_applicable",
                    "diff": "none",
                },
            },
        },
    )

    _apply_no_diff_final_acceptance_outcome(run, diff_path=None)

    assert run.session["status"] == "halted"
    assert run.session["halt_reason"] == "final_acceptance_no_diff"


def test_no_diff_final_acceptance_guard_does_not_override_real_diff() -> None:
    run = SimpleNamespace(
        session={
            "status": "done",
            "phases": {
                "review_changes": {
                    "clean": True,
                    "skipped": "no uncommitted changes",
                },
                "final_acceptance": {
                    "approved": False,
                    "verdict": "REJECTED",
                    "ship_ready": False,
                },
            },
        },
    )

    _apply_no_diff_final_acceptance_outcome(
        run, diff_path=Path("/tmp/run/diff.patch"),
    )

    assert run.session["status"] == "done"
    assert "halt_reason" not in run.session
    assert "no_op_outcome" not in run.session


def test_rejected_release_with_diff_and_no_delivery_halts() -> None:
    # A rejected final acceptance with a real diff but no applied delivery
    # must not finish as a silent 'done': it flips to halted with an explicit
    # reason and a structured outcome carrying the visible blockers.
    run = SimpleNamespace(
        session={
            "status": "done",
            "phases": {
                "final_acceptance": {
                    "approved": False,
                    "verdict": "REJECTED",
                    "ship_ready": False,
                    "short_summary": "blocking defect in delivery path",
                    "release_blockers": [
                        {"severity": "high", "detail": "data loss on apply"},
                    ],
                },
            },
        },
    )

    _apply_rejected_release_terminal_outcome(run)

    assert run.session["status"] == "halted"
    assert run.session["halt_reason"] == "final_acceptance_rejected"
    outcome = run.session["rejected_outcome"]
    assert outcome["reason"] == "final_acceptance_rejected"
    assert outcome["status"] == "halted"
    assert outcome["release_verdict"] == "REJECTED"
    assert outcome["release_blockers"] == [
        {"severity": "high", "detail": "data loss on apply"},
    ]
    assert outcome["short_summary"] == "blocking defect in delivery path"
    assert "delivery_override" not in run.session


def test_rejected_release_blockers_only_record_halts() -> None:
    # Defense-in-depth: even if verdict/ship_ready slipped past as non-reject
    # shapes, a non-empty release_blockers list alone still drives the
    # rejected terminal.
    run = SimpleNamespace(
        session={
            "status": "done",
            "phases": {
                "final_acceptance": {
                    "release_blockers": [
                        {"severity": "high", "detail": "unverified change"},
                    ],
                },
            },
        },
    )

    _apply_rejected_release_terminal_outcome(run)

    assert run.session["status"] == "halted"
    assert run.session["halt_reason"] == "final_acceptance_rejected"
    assert run.session["rejected_outcome"]["release_blockers"] == [
        {"severity": "high", "detail": "unverified change"},
    ]


def test_release_schema_forbids_blockers_on_approved_verdict() -> None:
    # The blockers-bearing rejected terminal is backed by a schema invariant:
    # an APPROVED verdict carrying release_blockers is impossible to persist.
    from core.contracts.release_schema import (
        ReleaseSchemaError,
        validate_release_dict,
    )

    approved_with_blockers = {
        "verdict": "APPROVED",
        "ship_ready": True,
        "short_summary": "ok",
        "release_blockers": [
            {"severity": "high", "detail": "should be impossible"},
        ],
        "verification_gaps": [],
        "contract_status": {},
    }

    with pytest.raises(ReleaseSchemaError):
        validate_release_dict(approved_with_blockers)


def test_approved_release_stays_clean_done_without_override_marker() -> None:
    run = SimpleNamespace(
        session={
            "status": "done",
            "phases": {
                "final_acceptance": {
                    "approved": True,
                    "verdict": "APPROVED",
                    "ship_ready": True,
                },
            },
        },
    )

    _apply_rejected_release_terminal_outcome(run)

    assert run.session["status"] == "done"
    assert "halt_reason" not in run.session
    assert "rejected_outcome" not in run.session
    assert "delivery_override" not in run.session


def test_approved_retry_supersedes_stale_rejection_residue() -> None:
    # Dogfood shape: an earlier REJECTED attempt left terminal-rejection
    # markers AND a phantom rejected commit_delivery gate. A later APPROVED
    # final acceptance on the still-'done' run must evict ALL of that stale
    # residue so meta/run.end/SDK reconcile to the authoritative APPROVED
    # verdict.
    run = SimpleNamespace(
        session={
            "status": "done",
            "halt_reason": "final_acceptance_rejected",
            "halted_at": "2026-06-26T00:00:00+00:00",
            "rejected_outcome": {
                "phase": "final_acceptance",
                "reason": "final_acceptance_rejected",
                "status": "halted",
                "release_verdict": "REJECTED",
                "release_blockers": [
                    {"severity": "high", "detail": "data loss on apply"},
                ],
            },
            "halt": {
                "reason": "final_acceptance_rejected",
                "phase": "final_acceptance",
            },
            "commit_delivery": {
                "status": "not_applicable",
                "release_verdict": "REJECTED",
            },
            "multi_project_delivery": {
                "primary_status": "not_applicable",
                "companions": [],
            },
            "phases": {
                "final_acceptance": {
                    "approved": True,
                    "verdict": "APPROVED",
                    "ship_ready": True,
                },
            },
        },
    )

    _apply_rejected_release_terminal_outcome(run)

    assert run.session["status"] == "done"
    for key in (
        "halt_reason",
        "halted_at",
        "rejected_outcome",
        "delivery_override",
        "halt",
    ):
        assert key not in run.session
    # Phantom rejected delivery-gate (and its mirrored companion block) gone.
    assert "commit_delivery" not in run.session
    assert "multi_project_delivery" not in run.session


def test_approved_retry_preserves_legitimate_approved_delivery() -> None:
    # Counter-test: on the same approved branch, a legitimate current
    # commit_delivery (APPROVED verdict, or an empty/parked verdict) is NOT a
    # phantom rejected gate and must survive untouched (ADR 0099/0100).
    for verdict in ("APPROVED", ""):
        run = SimpleNamespace(
            session={
                "status": "done",
                "commit_delivery": {
                    "status": "pending",
                    "release_verdict": verdict,
                },
                "phases": {
                    "final_acceptance": {
                        "approved": True,
                        "verdict": "APPROVED",
                        "ship_ready": True,
                    },
                },
            },
        )

        _apply_rejected_release_terminal_outcome(run)

        assert run.session["status"] == "done"
        assert run.session["commit_delivery"] == {
            "status": "pending",
            "release_verdict": verdict,
        }


def test_rejected_release_with_applied_delivery_records_override() -> None:
    # Operator override: a rejected release whose delivery was actually
    # applied stays 'done', but carries a durable, observable override marker
    # so it is never mistaken for a clean success.
    run = SimpleNamespace(
        session={
            "status": "done",
            "commit_delivery": {
                "status": "committed",
                "commit_sha": "abc123",
            },
            "phases": {
                "final_acceptance": {
                    "approved": False,
                    "verdict": "REJECTED",
                    "ship_ready": False,
                    "short_summary": "shipped over a known blocker",
                    "release_blockers": [
                        {"severity": "medium", "detail": "flaky coverage"},
                    ],
                },
            },
        },
    )

    _apply_rejected_release_terminal_outcome(run)

    assert run.session["status"] == "done"
    assert "halt_reason" not in run.session
    assert "rejected_outcome" not in run.session
    override = run.session["delivery_override"]
    assert override["reason"] == "final_acceptance_rejected_override"
    assert override["status"] == "done"
    assert override["release_verdict"] == "REJECTED"
    assert override["release_blockers"] == [
        {"severity": "medium", "detail": "flaky coverage"},
    ]
    assert override["delivery_status"] == "committed"
    assert override["short_summary"] == "shipped over a known blocker"


def test_rejected_release_no_diff_path_takes_precedence() -> None:
    # The more specific no-diff reject must win: once it has halted with
    # 'final_acceptance_no_diff', the general helper leaves it untouched.
    run = SimpleNamespace(
        session={
            "status": "done",
            "phases": {
                "review_changes": {
                    "clean": True,
                    "skipped": "no uncommitted changes",
                },
                "final_acceptance": {
                    "approved": False,
                    "verdict": "REJECTED",
                    "ship_ready": False,
                    "verification_gaps": [{
                        "risk": "No review target",
                        "missing": "No uncommitted diff",
                    }],
                },
            },
        },
    )

    _apply_no_diff_final_acceptance_outcome(run, diff_path=None)
    _apply_rejected_release_terminal_outcome(run)

    assert run.session["status"] == "halted"
    assert run.session["halt_reason"] == "final_acceptance_no_diff"
    assert "rejected_outcome" not in run.session


def test_record_phase_failure_preserves_phase_handoff() -> None:
    # A failure stamps status=failed + a phase_failure halt_reason but
    # must NOT resolve an outstanding handoff — phase_handoff survives.
    stub = SimpleNamespace(
        session={
            "status": "running",
            "phase_handoff": {"id": "h1", "phase": "validate_plan"},
        },
        output_dir=None,
        _ckpt=None,
        _presentation=PresentationPolicy.SILENT,
    )
    _PipelineRun._record_phase_failure(stub, RuntimeError("boom"), "implement")
    assert stub.session["status"] == "failed"
    assert stub.session["halt_reason"] == "phase_failure:RuntimeError"
    assert stub.session["phase_handoff"] == {
        "id": "h1", "phase": "validate_plan",
    }
    assert stub.session["failure"]["phase"] == "implement"


# ── correction-route DONE/HALTED line (T3) ─────────────────────────────────


@pytest.fixture
def _force_color():
    """Force the colored render path on, restoring the process global after.

    ``paint`` auto-detection is off under capsys (non-TTY); a test that needs
    to assert the ANSI tone must opt in. ``set_color_enabled`` is a process
    global — save and restore it so the override never leaks into other tests.
    """
    prev = get_color_enabled()
    set_color_enabled(True)
    try:
        yield
    finally:
        set_color_enabled(prev)


def _patch_finalization_side_effects(
    monkeypatch: pytest.MonkeyPatch, run_dir: Path
) -> None:
    """Stub the silent-service side-effects (disk / evidence / mirror /
    context / config) so the correction-route tests exercise only the
    finalization flow and terminal render."""
    monkeypatch.setattr(
        "pipeline.engine.diff_apply_check.capture_run_diff_with_apply_check",
        lambda *_a, **_kw: CapturedRunDiff(
            path=run_dir / "diff.patch", apply_check=None
        ),
    )

    def _save_session(output_dir: Path, _session: object) -> Path:
        path = output_dir / "session.json"
        path.write_text("{}", encoding="utf-8")
        return path

    monkeypatch.setattr(
        "pipeline.project.finalization.save_session", _save_session
    )
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


def _correction_stub(
    run_dir: Path, project_dir: Path, *, session: dict, state: object
) -> SimpleNamespace:
    metrics = SimpleNamespace(
        save=lambda output_dir: output_dir / "metrics.json",
        summary_line=lambda: "Tokens: 0",
        as_dict=lambda: {},
        phases=[],
    )
    return SimpleNamespace(
        output_dir=run_dir,
        task="# Orcho Task: correction follow-up",
        session=session,
        state=state,
        profile_name="correction",
        parent_run_id=None,
        project_alias=None,
        project_path=project_dir,
        worktree_context=None,
        no_interactive=False,
        _metrics=metrics,
        _ckpt=None,
        _done_summary_profile=None,
        session_ts="20260611_000000",
        _worktree_cvar_token=None,
        _sandbox_cvar_token=None,
        _effective_diff_cwd=lambda: project_dir,
        _commit_delivery_baseline=lambda: "HEAD",
        _run_commit_delivery=lambda _diff_cwd: None,
    )


def _route_line(out: str) -> str:
    for line in out.splitlines():
        if "Correction route" in line:
            return line
    raise AssertionError(f"no 'Correction route' line in output:\n{out!r}")


def test_shortcut_route_line_is_neutral_in_done_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    _force_color: None,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    _patch_finalization_side_effects(monkeypatch, run_dir)

    session = {
        "status": "done",
        "phases": {
            "correction_triage": {
                "kind": "gate_rerun",
                "summary": "gates stale; rerun is enough",
                "route": {
                    "kind": "gate_rerun",
                    "skip_phases": [
                        "implement", "repair_changes", "review_changes",
                    ],
                    "halt": False,
                    "reason": "not applicable for correction route 'gate_rerun'",
                },
            },
            "final_acceptance": {"verdict": "APPROVED", "ship_ready": True},
        },
    }
    state = SimpleNamespace(halt=False, halt_reason=None, extras={}, phase_log={})
    stub = _correction_stub(run_dir, project_dir, session=session, state=state)

    result = finalize_with_terminal_output(FinalizationContext(run=stub))

    out = capsys.readouterr().out
    stripped = strip_ansi(out)
    assert "Correction route: gate_rerun" in stripped
    assert "final_acceptance=ok" in stripped
    # The route line is neutral (cyan), never green — a skip is not an approve.
    line = _route_line(out)
    assert C.CYAN in line
    assert C.GREEN not in line
    assert "⚠" not in line
    # summary_text chips are untouched: route text is a separate line, not
    # folded into the green summary banner.
    assert "Correction route" not in result.summary_text
    assert result.correction_route_halted is False


def test_blocked_route_line_is_amber_in_halted_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    _force_color: None,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    _patch_finalization_side_effects(monkeypatch, run_dir)

    # Blocked triage halts in-state; the route dict is NOT stamped, so the
    # summary must derive it from the raw halted record.
    session = {
        "status": "done",  # _resolve_terminal_status flips this to halted
        "phases": {
            "correction_triage": {
                "kind": "blocked",
                "halted": True,
                "summary": "release blocked: contract drift unresolved",
                "blockers": ["missing API token"],
            },
        },
    }
    state = SimpleNamespace(
        halt=True,
        halt_reason="correction_triage_blocked",
        extras={"_current_phase": "correction_triage"},
        phase_log={},
    )
    stub = _correction_stub(run_dir, project_dir, session=session, state=state)

    result = finalize_with_terminal_output(FinalizationContext(run=stub))

    assert result.status == "halted"
    out = capsys.readouterr().out
    stripped = strip_ansi(out)
    assert "Correction route: blocked" in stripped
    assert "halted before implement" in stripped
    assert "missing API token" in stripped
    line = _route_line(out)
    assert "⚠" in line
    assert C.YELLOW in line
    assert C.GREEN not in line
    assert result.correction_route_halted is True


def test_non_correction_run_has_no_route_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    _patch_finalization_side_effects(monkeypatch, run_dir)

    session = {
        "status": "done",
        "phases": {"final_acceptance": {"verdict": "APPROVED"}},
    }
    state = SimpleNamespace(halt=False, halt_reason=None, extras={}, phase_log={})
    stub = _correction_stub(run_dir, project_dir, session=session, state=state)

    result = finalize_with_terminal_output(FinalizationContext(run=stub))

    out = strip_ansi(capsys.readouterr().out)
    assert "Correction route" not in out
    assert result.correction_route_line is None
    assert result.correction_route_halted is False


def test_route_done_entry_precedes_run_end_no_phase_after(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    _patch_finalization_side_effects(monkeypatch, run_dir)

    # Capture the event stream at the single module emit() seam used by both
    # log_phase (phase.start/phase.end) and finalize_project_run (run.end).
    events: list[tuple[str, str]] = []

    def _emit(kind: str, **payload: object) -> None:
        events.append((kind, str(payload.get("title", ""))))

    monkeypatch.setattr("core.observability.events.emit", _emit)

    session = {
        "status": "done",
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
            "final_acceptance": {"verdict": "APPROVED", "ship_ready": True},
        },
    }
    state = SimpleNamespace(halt=False, halt_reason=None, extras={}, phase_log={})
    stub = _correction_stub(run_dir, project_dir, session=session, state=state)

    finalize_project_run(FinalizationContext(run=stub))

    kinds = [k for k, _ in events]
    assert "run.end" in kinds
    run_end_idx = kinds.index("run.end")
    # The route DONE entry (a phase.end carrying the route text) is emitted
    # strictly before run.end.
    route_idx = next(
        i for i, (k, title) in enumerate(events)
        if k == "phase.end" and "Correction route: gate_rerun" in title
    )
    assert route_idx < run_end_idx
    # No phase.* event follows run.end.
    assert not any(
        k.startswith("phase.") for k in kinds[run_end_idx + 1:]
    ), f"phase event emitted after run.end: {events[run_end_idx + 1:]!r}"


def test_atexit_interrupted_follows_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The atexit hook marks a still-``running`` run interrupted with
    # halt_reason='interrupted' and an interrupted_at stamp, and — per
    # policy — preserves any active phase_handoff (an interrupted run
    # with an undecided handoff needs an operator decision).
    captured: list = []
    monkeypatch.setattr(
        "pipeline.project.bootstrap.atexit.register",
        lambda fn: captured.append(fn),
    )
    output_dir = tmp_path / "run"
    output_dir.mkdir(parents=True)
    session = init_session_with_atexit(
        task="t",
        project_path=tmp_path / "proj",
        plugin=SimpleNamespace(name="p"),
        model="m",
        profile_name="default",
        session_mode=SimpleNamespace(value="auto"),
        change_handoff="uncommitted",
        output_dir=output_dir,
    )
    assert captured, "atexit hook was not registered"
    # Simulate a run interrupted mid-flight with an active handoff.
    session["status"] = "running"
    session["phase_handoff"] = {"id": "h1", "phase": "validate_plan"}

    captured[0]()

    assert session["status"] == "interrupted"
    assert session["halt_reason"] == "interrupted"
    assert "interrupted_at" in session
    assert session["phase_handoff"] == {"id": "h1", "phase": "validate_plan"}
