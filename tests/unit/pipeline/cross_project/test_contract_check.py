"""Behavioural unit coverage for the cross contract-check flow.

Drives :func:`run_cross_contract_check` directly with a hand-built
:class:`ContractCheckContext` (fake ``codex`` + plain dicts on ``tmp_path``)
to exercise the branches that the full-pipeline tests in
``test_runner_gate_policy.py`` / ``test_cross_orchestrator.py`` do not reach at
the unit level — chiefly the **per-alias** review cycle (mode != artifact_bundle)
which currently has zero direct unit coverage.

Each test names the contract_check.py branch it closes. No real provider /
subprocess / worktree is used: the reviewer is a fake with ``.invoke``, and the
gate decision is forced through ``operator_decisions`` / ``no_interactive`` (or,
for the Ctrl-C path, a monkeypatched ``resolve_gate_decision``) so RUN / PAUSE /
ABORT are deterministic without a TTY.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pipeline.control import OperatorDecisionOverride
from pipeline.cross_project import contract_check as cc_mod
from pipeline.cross_project.contract_check import (
    ContractCheckContext,
    ContractCheckResult,
    run_cross_contract_check,
)
from pipeline.cross_project.execution_graph import CrossExecutionGraphNodeKind
from pipeline.cross_project.execution_graph_state import (
    CrossExecutionGraphNodeState,
    CrossExecutionGraphReason,
    CrossExecutionGraphState,
    CrossExecutionGraphStatus,
)
from pipeline.cross_project.gate_decisions import GateDecision
from pipeline.cross_project.planning_loop import approved_review_json
from pipeline.cross_project.project_dispatch import ProjectDispatchResult
from pipeline.runtime import (
    CrossGatePolicy,
    CrossGateRunPolicy,
    CrossGateSkipPolicy,
)


class _FakeCodex:
    """Reviewer stand-in: records ``invoke`` calls and returns a canned raw.

    A non-string ``reply`` of the sentinel ``_NEVER`` raises on invoke so the
    dry-run path can assert the provider was never consulted.
    """

    _NEVER = object()

    def __init__(self, reply: Any = _NEVER) -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []
        self.model = "fake-reviewer-model"

    def invoke(self, prompt: str, cwd: str) -> str:
        if self.reply is self._NEVER:
            raise AssertionError("codex.invoke must not run on the dry-run path")
        self.calls.append((prompt, cwd))
        return self.reply


def _policy(
    *,
    enabled: bool = True,
    run: CrossGateRunPolicy = CrossGateRunPolicy.ALWAYS,
    on_skip: CrossGateSkipPolicy = CrossGateSkipPolicy.ALLOW_WITH_GAP,
    mode: str | None = None,
) -> CrossGatePolicy:
    """A CrossGatePolicy; ``mode=None`` selects the per-alias review path."""
    return CrossGatePolicy(enabled=enabled, run=run, on_skip=on_skip, mode=mode)


_REJECTED_REVIEW = json.dumps(
    {
        "verdict": "REJECTED",
        "short_summary": "schema drift",
        "findings": [
            {
                "id": "F1",
                "title": "wire mismatch",
                "body": "core and mcp disagree",
                "severity": "P1",
                "required_fix": "regenerate the mcp schema",
            }
        ],
        "risks": [],
        "checks": [],
    }
)


def _ctx(tmp_path: Path, **overrides: Any) -> ContractCheckContext:
    """Build a ContractCheckContext with single-alias defaults on tmp_path."""
    core = tmp_path / "core"
    core.mkdir(exist_ok=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    defaults: dict[str, Any] = {
        "task": "change the sdk wire and regenerate the mcp schema",
        "projects": {"core": core},
        "session": {"phases": {}},
        "cross_ckpt": {},
        "run_dir": run_dir,
        "output_dir": None,
        "dry_run": False,
        "resume_from": None,
        "terminal": False,
        "common_cwd": str(tmp_path),
        "plan_output": "CROSS PLAN",
        "codex": _FakeCodex(),
        "contract_policy": _policy(),
        "operator_decisions": None,
        "no_interactive": True,
        "cross_phase_usage": {},
    }
    defaults.update(overrides)
    return ContractCheckContext(**defaults)


# ── _review_target: worktree.path dict branch (161-163) ──────────────────────


def test_review_target_uses_worktree_checkout_path(tmp_path: Path) -> None:
    """_review_target points at the child's worktree.path when present (161-163).

    A child phase entry carrying ``worktree.path`` makes the cross gate review
    the isolated worktree checkout, not the pristine source project.
    """
    wt = tmp_path / "wt" / "core"
    wt.mkdir(parents=True)
    session = {
        "phases": {
            "projects": {"core": {"worktree": {"path": str(wt)}}},
        }
    }
    ctx = _ctx(
        tmp_path,
        session=session,
        contract_policy=_policy(enabled=False),  # skip reviewer, only resolve target
    )
    result = run_cross_contract_check(ctx)
    assert result.review_projects["core"] == wt


# ── resume disk-fallback (193-219) ───────────────────────────────────────────


def test_resume_disk_fallback_loads_cached_rejected_results(tmp_path: Path) -> None:
    """Resume with no session cache loads contract_check from meta.json (199-216).

    A cached entry with ``approved is False`` reconstructs the local failure
    flags (contract_check_failed + artifact_bundle reason) so downstream CFA
    does not see "all clean".
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "meta.json").write_text(
        json.dumps(
            {"phases": {"contract_check": {"core": {"approved": False,
                                                     "verdict": "REJECTED"}}}}
        ),
        encoding="utf-8",
    )
    ctx = _ctx(
        tmp_path,
        run_dir=run_dir,
        resume_from="20260101_000000",
        session={"phases": {}},  # not threaded → forces the disk fallback
    )
    result = run_cross_contract_check(ctx)
    assert result.control == "continue"
    assert result.contract_check_failed is True
    assert "rejected" in result.contract_check_failure_reason
    assert ctx.session["phases"]["contract_check"]["core"]["approved"] is False


def test_resume_does_not_cache_child_readiness_precondition(tmp_path: Path) -> None:
    """A retried child must replace NOT_EVALUABLE with a real review verdict."""
    codex = _FakeCodex(reply=approved_review_json("retry succeeded"))
    ctx = _ctx(
        tmp_path,
        resume_from="20260101_000000",
        codex=codex,
        session={
            "phases": {
                "contract_check": {
                    "core": {
                        "approved": False,
                        "verdict": "NOT_EVALUABLE",
                        "not_evaluable": True,
                        "source": "precondition",
                        "reason": "child_readiness",
                    },
                },
            },
        },
    )

    result = run_cross_contract_check(ctx)

    assert len(codex.calls) == 1
    assert result.contract_results["core"]["verdict"] == "APPROVED"


def test_completed_contract_cache_predicate_accepts_real_verdicts() -> None:
    assert cc_mod._is_completed_contract_cache_entry({"verdict": "APPROVED"})
    assert cc_mod._is_completed_contract_cache_entry({"verdict": "REJECTED"})
    assert cc_mod._is_completed_contract_cache_entry(
        {"verdict": "SKIPPED", "skipped": True},
    )
    assert not cc_mod._is_completed_contract_cache_entry(
        {"verdict": "NOT_EVALUABLE", "not_evaluable": True},
    )


def test_dispatch_readiness_bypasses_contract_gate_and_all_success_runs_it(
    tmp_path: Path, monkeypatch,
) -> None:
    """The coordinator uses immutable dispatch readiness, never session rereads."""
    from pipeline.cross_project import session_run

    core = tmp_path / "core"
    core.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    request = SimpleNamespace(
        task="cross task", projects={"core": core}, task_plan=None,
        resume_from=None, dry_run=False, max_rounds=1, phase_config=None,
        hypothesis_enabled=False, followup_session_seeds_per_alias=None,
        output_dir=None, plan_output="", plan_review_dict=None,
        operator_decisions=None, no_interactive=True,
    )
    ctx = SimpleNamespace(
        r=SimpleNamespace(banner=lambda *a, **k: None, success=lambda *a, **k: None,
                          warn=lambda *a, **k: None),
        task_plan=None, code_model="test", child_profile=object(),
        requested_profile=SimpleNamespace(name="test"), has_global_plan=False,
        provider=object(), cross_ckpt={"sub_status": {}},
        session={"phases": {"projects": {
            "core": {"status": "halted", "halt_reason": "operator stop"},
        }}},
        cross_phase_usage={}, terminal=False, participant_set=None,
        run_dir=run_dir, plan_output="", plan_review_dict=None,
        common_cwd=str(tmp_path), review_agent=object(),
        profile_setup=SimpleNamespace(contract_gate_policy=_policy()),
    )
    monkeypatch.setattr(
        session_run, "_run_project_dispatch",
        lambda _dispatch_ctx: ProjectDispatchResult(False, ("core",)),
    )
    gate_calls: list[object] = []
    monkeypatch.setattr(
        session_run, "run_cross_contract_check",
        lambda _ctx: gate_calls.append(_ctx) or (_ for _ in ()).throw(
            AssertionError("contract gate must not run for blocked children"),
        ),
    )

    assert session_run._run_dispatch_and_contract(request, ctx) is False
    assert gate_calls == []
    entry = ctx.contract_results["core"]
    assert entry["verdict"] == "NOT_EVALUABLE"
    # A session snapshot cannot promote a missing durable child outcome.
    assert entry["child_status"] == "pending"
    assert entry["child_reason"] == "operator stop"
    assert ctx.contract_check_failed is False

    def _successful_dispatch(_dispatch_ctx):
        # A real successful dispatch replaces the prior halted child snapshot;
        # keeping it halted here would intentionally trip canonical admission.
        child = {"status": "done"}
        ctx.session["phases"]["projects"]["core"] = child
        child_dir = run_dir / "core"
        child_dir.mkdir()
        (child_dir / "meta.json").write_text(json.dumps(child), encoding="utf-8")
        return ProjectDispatchResult(False, ())

    monkeypatch.setattr(session_run, "_run_project_dispatch", _successful_dispatch)
    expected = ContractCheckResult(
        contract_results={"core": {"verdict": "APPROVED", "approved": True}},
        contract_check_failed=False,
        contract_check_failure_reason=None,
        control="continue",
        review_projects={"core": core},
        review_common_cwd=str(core),
    )
    monkeypatch.setattr(
        session_run, "run_cross_contract_check",
        lambda _ctx: gate_calls.append(_ctx) or expected,
    )

    assert session_run._run_dispatch_and_contract(request, ctx) is False
    assert len(gate_calls) == 1
    assert ctx.contract_results == expected.contract_results


def test_parent_consistency_violation_uses_parent_readiness_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An evaluable child does not become non-evaluable because its parent is inconsistent."""
    from pipeline.cross_project import session_run

    core = tmp_path / "core"
    core.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "core").mkdir()
    (run_dir / "core" / "meta.json").write_text(
        json.dumps({"status": "done"}), encoding="utf-8"
    )
    request = SimpleNamespace(
        task="cross task", projects={"core": core}, task_plan=None,
        resume_from=None, dry_run=False, max_rounds=1, phase_config=None,
        hypothesis_enabled=False, followup_session_seeds_per_alias=None,
        output_dir=None, plan_output="", plan_review_dict=None,
        operator_decisions=None, no_interactive=True,
    )
    ctx = SimpleNamespace(
        r=SimpleNamespace(banner=lambda *a, **k: None, success=lambda *a, **k: None,
                          warn=lambda *a, **k: None),
        task_plan=None, code_model="test", child_profile=object(),
        requested_profile=SimpleNamespace(name="test"), has_global_plan=False,
        provider=object(),
        cross_ckpt={"phase_handoff_pending": True, "sub_status": {"core": "done"}},
        session={"phases": {"projects": {"core": {"status": "done"}}}},
        cross_phase_usage={}, terminal=False, participant_set=None,
        run_dir=run_dir, plan_output="", plan_review_dict=None,
        common_cwd=str(core), review_agent=object(),
        profile_setup=SimpleNamespace(contract_gate_policy=_policy()),
    )
    monkeypatch.setattr(
        session_run, "_run_project_dispatch",
        lambda _dispatch_ctx: ProjectDispatchResult(False, ()),
    )
    monkeypatch.setattr(
        session_run,
        "run_cross_contract_check",
        lambda _ctx: (_ for _ in ()).throw(
            AssertionError("contract gate must not run for an inconsistent parent")
        ),
    )

    assert session_run._run_dispatch_and_contract(request, ctx) is False
    entry = ctx.contract_results["core"]
    assert entry["verdict"] == "NOT_EVALUABLE"
    assert entry["child_status"] == "done"
    assert entry["child_reason"] == "parent_inconsistent:checkpoint_pending_without_payload"


def test_graph_blocked_contract_never_invokes_contract_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A graph-denied contract gate stops before the provider boundary."""
    from pipeline.cross_project import session_run

    core = tmp_path / "core"
    core.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    child_dir = run_dir / "core"
    child_dir.mkdir()
    (child_dir / "meta.json").write_text('{"status": "done"}', encoding="utf-8")
    request = SimpleNamespace(
        task="cross task", projects={"core": core}, task_plan=None,
        resume_from=None, dry_run=False, max_rounds=1, phase_config=None,
        hypothesis_enabled=False, followup_session_seeds_per_alias=None,
        output_dir=None, plan_output="", plan_review_dict=None,
        operator_decisions=None, no_interactive=True,
    )
    ctx = SimpleNamespace(
        r=SimpleNamespace(banner=lambda *a, **k: None, success=lambda *a, **k: None,
                          warn=lambda *a, **k: None),
        task_plan=None, code_model="test", child_profile=object(),
        requested_profile=SimpleNamespace(name="test"), has_global_plan=False,
        provider=object(), cross_ckpt={}, session={
            "projects": {"core": str(core)},
            "phases": {"projects": {"core": {"status": "done"}}},
        },
        cross_phase_usage={}, terminal=False, participant_set=None,
        run_dir=run_dir, plan_output="", plan_review_dict=None,
        common_cwd=str(core), review_agent=object(), execution_graph=object(),
        profile_setup=SimpleNamespace(contract_gate_policy=_policy()),
        graph_gate_blocked=False,
    )
    monkeypatch.setattr(
        session_run, "_run_project_dispatch", lambda _: ProjectDispatchResult(False, ()),
    )
    monkeypatch.setattr(
        session_run, "reduce_runtime_cross_execution_graph_state",
        lambda *_: CrossExecutionGraphState((
            CrossExecutionGraphNodeState(
                "contract", CrossExecutionGraphNodeKind.CONTRACT_CHECK,
                CrossExecutionGraphStatus.BLOCKED,
                CrossExecutionGraphReason.DEPENDENCY_BLOCKED,
            ),
        )),
    )
    monkeypatch.setattr(
        session_run, "run_cross_contract_check",
        lambda _: pytest.fail("blocked graph must not invoke contract provider"),
    )

    assert session_run._run_dispatch_and_contract(request, ctx) is False
    assert ctx.graph_gate_blocked is True
    assert ctx.contract_results == {}


@pytest.mark.parametrize(
    ("cached_entry", "expected_failed", "expect_provider"),
    [
        pytest.param({"verdict": "REJECTED"}, True, False, id="rejected-cache"),
        pytest.param(
            {"verdict": "NOT_EVALUABLE", "not_evaluable": True},
            True,
            True,
            id="stale-readiness-entry",
        ),
    ],
)
def test_resume_graph_contract_cache_preserves_existing_cache_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cached_entry: dict[str, object],
    expected_failed: bool,
    expect_provider: bool,
) -> None:
    """Graph admission delegates completed/resumed cache handling to the gate."""
    from pipeline.cross_project import session_run

    core = tmp_path / "core"
    core.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    child_dir = run_dir / "core"
    child_dir.mkdir()
    (child_dir / "meta.json").write_text('{"status": "done"}', encoding="utf-8")
    reviewer = _FakeCodex(_REJECTED_REVIEW)
    request = SimpleNamespace(
        task="cross task", projects={"core": core}, task_plan=None,
        resume_from="prior", dry_run=False, max_rounds=1, phase_config=None,
        hypothesis_enabled=False, followup_session_seeds_per_alias=None,
        output_dir=None, plan_output="", plan_review_dict=None,
        operator_decisions=None, no_interactive=True,
    )
    status = CrossExecutionGraphStatus.READY if expect_provider else CrossExecutionGraphStatus.COMPLETED
    ctx = SimpleNamespace(
        r=SimpleNamespace(banner=lambda *a, **k: None, success=lambda *a, **k: None,
                          warn=lambda *a, **k: None),
        task_plan=None, code_model="test", child_profile=object(),
        requested_profile=SimpleNamespace(name="test"), has_global_plan=False,
        provider=object(), cross_ckpt={},
        session={
            "projects": {"core": str(core)},
            "phases": {
                "projects": {"core": {"status": "done"}},
                "contract_check": {"core": cached_entry},
            },
        },
        cross_phase_usage={}, terminal=False, participant_set=None,
        run_dir=run_dir, plan_output="", plan_review_dict=None,
        common_cwd=str(core), review_agent=reviewer, execution_graph=object(),
        profile_setup=SimpleNamespace(contract_gate_policy=_policy()),
        graph_gate_blocked=False,
    )
    monkeypatch.setattr(
        session_run, "_run_project_dispatch", lambda _: ProjectDispatchResult(False, ()),
    )
    monkeypatch.setattr(
        session_run, "reduce_runtime_cross_execution_graph_state",
        lambda *_: CrossExecutionGraphState((
            CrossExecutionGraphNodeState(
                "contract", CrossExecutionGraphNodeKind.CONTRACT_CHECK, status,
                CrossExecutionGraphReason.RUNNER_GATE_COMPLETED
                if status is CrossExecutionGraphStatus.COMPLETED
                else CrossExecutionGraphReason.DEPENDENCY_PENDING,
            ),
        )),
    )

    assert session_run._run_dispatch_and_contract(request, ctx) is False
    assert ctx.contract_check_failed is expected_failed
    assert bool(reviewer.calls) is expect_provider


@pytest.mark.parametrize(
    "meta_writer",
    [
        pytest.param(lambda p: None, id="missing-file-oserror"),
        pytest.param(
            lambda p: (p / "meta.json").write_text("{not json", encoding="utf-8"),
            id="bad-json-valueerror",
        ),
    ],
)
def test_resume_disk_fallback_swallows_read_errors(
    tmp_path: Path, meta_writer
) -> None:
    """A missing / unparseable meta.json is swallowed (except OSError/ValueError, 201-202).

    The flow must not treat the run as cached; with the gate policy disabled it
    falls through to policy-skipped entries instead of crashing.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    meta_writer(run_dir)
    ctx = _ctx(
        tmp_path,
        run_dir=run_dir,
        resume_from="20260101_000000",
        session={"phases": {}},
        contract_policy=_policy(enabled=False),
    )
    result = run_cross_contract_check(ctx)
    assert result.control == "continue"
    # Not cached → policy-disabled skip entries were written instead.
    assert result.contract_results["core"]["skip_reason"] == "policy_disabled"


# ── policy disabled / never skips (220-241) ──────────────────────────────────


@pytest.mark.parametrize(
    ("policy", "expected_reason"),
    [
        pytest.param(_policy(enabled=False), "policy_disabled", id="disabled"),
        pytest.param(
            _policy(enabled=True, run=CrossGateRunPolicy.NEVER),
            "policy_never",
            id="never",
        ),
    ],
)
def test_policy_skip_writes_skipped_entries(
    tmp_path: Path, policy: CrossGatePolicy, expected_reason: str
) -> None:
    """Disabled / run=never policy skips the reviewer with a policy entry (220-241)."""
    codex = _FakeCodex(reply="UNUSED")
    ctx = _ctx(tmp_path, contract_policy=policy, codex=codex)
    result = run_cross_contract_check(ctx)
    assert result.control == "continue"
    entry = result.contract_results["core"]
    assert entry["skip_reason"] == expected_reason
    assert entry["source"] == "policy"
    assert codex.calls == []  # reviewer never consulted
    assert ctx.session["phases"]["contract_check"] == result.contract_results


# ── gate PAUSE (255-279) ─────────────────────────────────────────────────────


def test_gate_pause_persists_pending_gate(tmp_path: Path) -> None:
    """manual_confirm + no_interactive → PAUSE: pending_gate persisted (255-279).

    Returns ``control == 'paused'`` with the session marked
    ``awaiting_gate_decision`` and the pending block mirrored into the
    checkpoint, so the coordinator can early-return for a later resume.
    """
    ctx = _ctx(
        tmp_path,
        contract_policy=_policy(run=CrossGateRunPolicy.MANUAL_CONFIRM),
        no_interactive=True,
    )
    result = run_cross_contract_check(ctx)
    assert result.control == "paused"
    assert ctx.session["status"] == "awaiting_gate_decision"
    pending = ctx.session["pending_gate"]
    assert pending["name"] == "contract_check"
    assert pending["choices"] == ["run", "skip"]
    assert ctx.cross_ckpt["pending_gate"] == pending


# ── gate ABORT via KeyboardInterrupt (252-253, 281-304) ──────────────────────


def test_gate_abort_on_keyboard_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C out of resolve_gate_decision maps to ABORT (252-253 → 281-304).

    The flow finalizes the cross terminal (status cancelled, failure_reason set)
    and returns ``control == 'aborted'`` without invoking the reviewer.
    """
    def _raise_kbd(**_kwargs: Any) -> GateDecision:
        raise KeyboardInterrupt

    monkeypatch.setattr(cc_mod, "resolve_gate_decision", _raise_kbd)
    codex = _FakeCodex(reply="UNUSED")
    ctx = _ctx(
        tmp_path,
        contract_policy=_policy(run=CrossGateRunPolicy.MANUAL_CONFIRM),
        codex=codex,
    )
    result = run_cross_contract_check(ctx)
    assert result.control == "aborted"
    assert ctx.session["failure_reason"] == "contract_check aborted by operator"
    assert ctx.session["status"] == "cancelled"
    assert codex.calls == []


# ── gate SKIP via operator decision (306-324) ────────────────────────────────


def test_gate_skip_by_operator_writes_operator_entries(tmp_path: Path) -> None:
    """An operator SKIP decision writes per-alias skip entries (306-324).

    A ``skip`` override on a manual_confirm gate resolves to SKIP: the reviewer
    is never consulted, each alias gets an ``operator_decision`` / ``operator``
    entry carrying the operator feedback, and the flow still ``continue``s.
    """
    overrides = (
        OperatorDecisionOverride(
            target="contract_check", decision="skip", feedback="not needed here",
        ),
    )
    codex = _FakeCodex(reply="UNUSED")
    ctx = _ctx(
        tmp_path,
        contract_policy=_policy(run=CrossGateRunPolicy.MANUAL_CONFIRM),
        operator_decisions=overrides,
        no_interactive=True,
        codex=codex,
    )
    result = run_cross_contract_check(ctx)
    assert result.control == "continue"
    entry = result.contract_results["core"]
    assert entry["skip_reason"] == "operator_decision"
    assert entry["source"] == "operator"
    assert entry["operator_feedback"] == "not needed here"
    assert codex.calls == []  # reviewer never consulted on the SKIP branch
    assert ctx.session["phases"]["contract_check"] == result.contract_results


# ── per-alias review cycle (467-571) ─────────────────────────────────────────


def test_per_alias_dry_run_approves_without_provider(tmp_path: Path) -> None:
    """dry_run builds an approved per-alias entry without calling the reviewer (477-499).

    The default ``_FakeCodex`` raises if invoked, so reaching ``continue`` proves
    the provider was skipped on the dry-run branch.
    """
    ctx = _ctx(tmp_path, dry_run=True)  # codex sentinel raises on invoke
    result = run_cross_contract_check(ctx)
    assert result.control == "continue"
    assert result.contract_check_failed is False
    entry = result.contract_results["core"]
    assert entry["approved"] is True
    assert entry["verdict"] == "APPROVED"
    assert ctx.session["phases"]["contract_check"]["core"] == entry  # 569-570


def test_per_alias_real_invoke_approved(tmp_path: Path) -> None:
    """A valid approved review JSON yields an approved per-alias entry (500-561).

    The reviewer is invoked once at the alias's review target and the usage is
    accumulated under the ``contract_check`` phase.
    """
    codex = _FakeCodex(reply=approved_review_json("looks consistent"))
    ctx = _ctx(tmp_path, codex=codex)
    result = run_cross_contract_check(ctx)
    assert result.control == "continue"
    assert result.contract_check_failed is False
    entry = result.contract_results["core"]
    assert entry["approved"] is True
    assert entry["verdict"] == "APPROVED"
    assert len(codex.calls) == 1
    assert codex.calls[0][1] == str(ctx.projects["core"])
    assert "contract_check" in ctx.cross_phase_usage  # 571-575


def test_per_alias_real_invoke_rejected_sets_failure(tmp_path: Path) -> None:
    """A REJECTED review marks contract_check_failed with a per-alias reason (562-567)."""
    codex = _FakeCodex(reply=_REJECTED_REVIEW)
    ctx = _ctx(tmp_path, codex=codex)
    result = run_cross_contract_check(ctx)
    entry = result.contract_results["core"]
    assert entry["approved"] is False
    assert entry["verdict"] == "REJECTED"
    assert result.contract_check_failed is True
    assert result.contract_check_failure_reason == (
        "contract_check rejected for core"
    )


def test_per_alias_parse_error_shapes_rejected_entry(tmp_path: Path) -> None:
    """Unparseable reviewer output → approved=False/REJECTED + parse_error (520-545).

    The flow records the parse failure as a P1 rejection rather than crashing,
    and surfaces a parse-error failure reason.
    """
    codex = _FakeCodex(reply="this is not a json review object at all")
    ctx = _ctx(tmp_path, codex=codex)
    result = run_cross_contract_check(ctx)
    entry = result.contract_results["core"]
    assert entry["approved"] is False
    assert entry["verdict"] == "REJECTED"
    assert "parse_error" in entry
    assert result.contract_check_failed is True
    assert result.contract_check_failure_reason == (
        "contract_check parse error for core"
    )


def test_operator_run_override_threads_decisions(tmp_path: Path) -> None:
    """An explicit run override is consumed from operator_decisions (132-139).

    Builds the per-target override maps and resolves the manual_confirm gate to
    RUN without a TTY, so the per-alias reviewer executes.
    """
    overrides = (
        OperatorDecisionOverride(
            target="contract_check", decision="run", feedback="proceed",
        ),
    )
    codex = _FakeCodex(reply=approved_review_json("ok"))
    ctx = _ctx(
        tmp_path,
        contract_policy=_policy(run=CrossGateRunPolicy.MANUAL_CONFIRM),
        operator_decisions=overrides,
        no_interactive=True,
        codex=codex,
    )
    result = run_cross_contract_check(ctx)
    assert result.control == "continue"
    assert result.contract_results["core"]["approved"] is True
    assert len(codex.calls) == 1
