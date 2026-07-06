"""Sequencer-level cross-delivery gating tests (ADR 0128).

Proves the P0 fix at the coordinator boundary: cross delivery is decoupled
from the release-gate policy. ``_run_delivery_and_finalize`` runs
``run_cross_delivery`` on BOTH the approved path and the policy-disabled
path (gate-less profile, ``ctx.cfa_outcome is None``), the override is
null-safe, and a rejected run — which halts upstream — never reaches
delivery. These are unit tests over the module functions in
``pipeline.cross_project.session_run``; the per-alias transport matrix is
covered by ``test_cross_delivery.py``.

The forwarding tests stub ``run_cross_delivery`` to assert the override the
sequencer computes. ``test_policy_skip_end_to_end_populates_cross_delivery_
evidence`` deliberately does NOT stub it: it drives the real gate-less path
over temp git repos and asserts the acceptance invariant — the previously
absent ``session["phases"]["cross_delivery"]`` phase is now present, filled
with per-alias statuses, and threaded into finalization as a ``done`` run.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import pipeline.cross_project.session_run as session_run
from core.io.git_helpers import create_worktree
from pipeline.cross_project.session_run import (
    _cross_delivery_plan,
    _run_delivery_and_finalize,
)

pytestmark = [pytest.mark.cross_project]


# ── _cross_delivery_plan (pure classifier) ────────────────────────────


def test_plan_override_continue_yields_real_override() -> None:
    ctx = SimpleNamespace(
        cfa_outcome=SimpleNamespace(outcome="override_continue"),
    )
    assert _cross_delivery_plan(ctx) == (True, True)


def test_plan_approved_terminal_yields_no_override() -> None:
    ctx = SimpleNamespace(
        cfa_outcome=SimpleNamespace(outcome="approved_terminal"),
    )
    assert _cross_delivery_plan(ctx) == (True, False)


def test_plan_policy_skip_null_cfa_is_null_safe() -> None:
    """Regression: policy-skip path has ``cfa_outcome is None`` — the
    classifier must return ``override=False`` without ``AttributeError``."""
    ctx = SimpleNamespace(cfa_outcome=None)
    assert _cross_delivery_plan(ctx) == (True, False)


# ── _run_delivery_and_finalize (thin sequencer) ───────────────────────


def _ctx(*, cfa_outcome, terminal: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        session={"phases": {}},
        run_dir=Path("/tmp/cross-run"),
        terminal=terminal,
        cross_ckpt={},
        cfa_outcome=cfa_outcome,
        cfa_result=None,
        contract_results={},
        contract_check_failed=False,
        contract_check_failure_reason=None,
        cross_phase_usage={},
        delivery_result=None,
    )


def _request() -> SimpleNamespace:
    return SimpleNamespace(
        projects={"api": Path("/tmp/api")},
        output_dir=None,
        max_rounds=1,
    )


def _run(ctx, request):
    """Drive ``_run_delivery_and_finalize`` with delivery + finalize stubbed."""
    sentinel = object()
    with (
        patch(
            "pipeline.cross_project.cross_delivery.run_cross_delivery",
            return_value=sentinel,
        ) as m_deliver,
        patch("pipeline.cross_project.finalization.CrossFinalizationContext"),
        patch("pipeline.cross_project.finalization.finalize_cross_run") as m_fin,
        patch(
            "pipeline.cross_project.finalization.finalize_cross_with_terminal_output",
        ),
    ):
        _run_delivery_and_finalize(request, ctx)
    return m_deliver, m_fin, sentinel


def test_policy_skip_still_delivers_with_no_override() -> None:
    """Gate-less run (``cfa_outcome is None``): delivery runs — previously
    the inline ``if not release_skipped_by_policy`` skipped it entirely —
    and the override forwarded is ``False``."""
    ctx = _ctx(cfa_outcome=None)
    m_deliver, _m_fin, sentinel = _run(ctx, _request())

    m_deliver.assert_called_once()
    assert m_deliver.call_args.kwargs["override"] is False
    # Delivery result is threaded into finalization.
    assert ctx.delivery_result is sentinel


def test_approved_override_forwards_real_override() -> None:
    """Regression: CFA override-continue still forwards ``override=True``."""
    ctx = _ctx(cfa_outcome=SimpleNamespace(outcome="override_continue"))
    m_deliver, _m_fin, _sentinel = _run(ctx, _request())

    m_deliver.assert_called_once()
    assert m_deliver.call_args.kwargs["override"] is True


def test_approved_terminal_forwards_no_override() -> None:
    ctx = _ctx(cfa_outcome=SimpleNamespace(outcome="approved_terminal"))
    m_deliver, _m_fin, _sentinel = _run(ctx, _request())

    m_deliver.assert_called_once()
    assert m_deliver.call_args.kwargs["override"] is False


# ── coordinator ordering: rejected halts before delivery ──────────────


def test_rejected_release_gate_never_reaches_delivery() -> None:
    """A rejected release verdict halts in ``_run_release_gate`` (returns
    ``True``), so the coordinator returns before ``_run_delivery_and_finalize``
    — delivery must not run for a rejected run (ADR 0128 invariant preserved)."""
    fake_ctx = SimpleNamespace(
        session={"phases": {}},
        run_dir=Path("/tmp/cross-run"),
        session_ts="ts",
        cross_ckpt={},
        release_skipped_by_policy=False,
    )
    request = SimpleNamespace(resume_from=None)

    with (
        patch.object(session_run, "_setup_cross_run", return_value=fake_ctx),
        patch.object(session_run, "_run_cross_hypothesis"),
        patch.object(session_run, "_resolve_global_plan_steps"),
        patch.object(session_run, "_run_planning", return_value=False),
        patch.object(
            session_run, "_run_dispatch_and_contract", return_value=False,
        ),
        patch.object(session_run, "_run_release_gate", return_value=True),
        patch.object(session_run, "_finalize_release_verdict") as m_finver,
        patch.object(session_run, "_run_delivery_and_finalize") as m_deliver,
    ):
        session_run.run_cross_pipeline_session(request)

    m_deliver.assert_not_called()
    # Halt is terminal in the gate itself — the verdict tail never runs.
    m_finver.assert_not_called()


# ── end-to-end: policy-skip really populates cross_delivery evidence ───


def _init_repo(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@orcho.invalid"], cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Orcho Test"], cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True,
    )
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    r = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
        text=True, check=True,
    )
    return r.stdout.strip()


def _git_subjects(repo: Path) -> list[str]:
    r = subprocess.run(
        ["git", "log", "--format=%s"], cwd=repo, capture_output=True,
        text=True, check=True,
    )
    return [line for line in r.stdout.splitlines() if line.strip()]


@pytest.mark.git_worktree
def test_policy_skip_end_to_end_populates_cross_delivery_evidence(
    tmp_path: Path,
) -> None:
    """Acceptance invariant (ADR 0128): a gate-less run (``cfa_outcome is
    None``, ``release_skipped_by_policy=True``) really writes
    ``session["phases"]["cross_delivery"]`` with per-alias statuses and
    threads the delivery result into finalization as ``done`` — the phase
    that used to be ABSENT on this path.

    ``run_cross_delivery`` is NOT stubbed here (unlike the forwarding
    tests): the real per-alias transport runs over temp git repos so the
    persisted evidence is exercised, not asserted against a sentinel.
    """
    cross_run_dir = tmp_path / "run"
    cross_run_dir.mkdir()

    # Real project repo + retained run worktree carrying a change.
    repo = tmp_path / "api"
    seed = _init_repo(repo)
    wt = create_worktree(
        repo=repo,
        base_ref=seed,
        target_path=cross_run_dir / "worktrees" / "wt_api" / "checkout",
        branch_name="orcho/run/api",
    )
    assert wt.ok, wt.error
    worktree = cross_run_dir / "worktrees" / "wt_api" / "checkout"
    (worktree / "app.txt").write_text("base\napi-change\n", encoding="utf-8")

    child = {
        "pre_run_dirty": {"seed_tree_sha": seed},
        "worktree": {"path": str(worktree), "base_ref": seed},
        "phases": {"final_acceptance": {"verdict": "APPROVED"}},
    }
    session = {"phases": {"projects": {"api": child}}}

    # Gate-less context: no CFA outcome, release skipped by policy.
    ctx = SimpleNamespace(
        session=session,
        run_dir=cross_run_dir,
        terminal=False,
        cross_ckpt={},
        cfa_outcome=None,
        cfa_result=None,
        release_skipped_by_policy=True,
        contract_results={},
        contract_check_failed=False,
        contract_check_failure_reason=None,
        cross_phase_usage={},
        delivery_result=None,
    )
    request = SimpleNamespace(
        projects={"api": repo}, output_dir=None, max_rounds=1,
    )

    # Commit delivery on: the alias diff commits onto the project checkout
    # (``bypass`` — the legacy per-alias-onto-checkout contract).
    app_cfg = SimpleNamespace(
        commit={
            "enabled": True,
            "auto_in_ci": "approve",
            "add_untracked": True,
            "branch_policy": "bypass",
        },
    )

    # Spy on the REAL finalizer so it still runs (proving the ``done``
    # mapping) while recording the context it received. ``_real_finalize`` is
    # captured BEFORE the patch, so calling it is not re-routed through the
    # patched attribute — no recursion.
    from pipeline.cross_project.finalization import (
        finalize_cross_run as _real_finalize,
    )

    captured: dict = {}

    def _spy(fin_ctx):
        captured["ctx"] = fin_ctx
        captured["result"] = _real_finalize(fin_ctx)
        return captured["result"]

    with (
        patch("core.infra.config.AppConfig.load", return_value=app_cfg),
        patch("core.observability.events.emit"),
        patch("pipeline.engine.artifact_mirror.mirror_to_projects",
              return_value=[]),
        patch("pipeline.cross_project.finalization.finalize_cross_run",
              side_effect=_spy),
    ):
        _run_delivery_and_finalize(request, ctx)

    # The previously ABSENT phase is now present and populated.
    ev = session["phases"]["cross_delivery"]
    assert ev["overall"] == "ok"
    assert ev["disabled_by_config"] is False
    assert ev["per_alias"]["api"]["status"] == "committed"
    assert ev["per_alias"]["api"]["commit_sha"]
    # The change actually landed in the project checkout.
    assert any("deliver" in s for s in _git_subjects(repo))
    assert "api-change" in (repo / "app.txt").read_text()
    # The delivery aggregate reached finalization (not None / not dropped),
    # and the real finalizer mapped the ``ok`` aggregate to a ``done`` run.
    assert captured["ctx"].delivery_result is ctx.delivery_result
    assert ctx.delivery_result is not None
    assert ctx.delivery_result.overall == "ok"
    assert captured["result"].status == "done"
