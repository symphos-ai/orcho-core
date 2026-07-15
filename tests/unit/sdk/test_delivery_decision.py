"""SDK post-release delivery decision surface (ADR 0100).

Exercises :func:`sdk.run_control.delivery.decide_delivery` and
:func:`~sdk.run_control.delivery.delivery_decision_state` against a hand-built
parked gate fixture (a real git worktree + ``meta.commit_delivery`` context),
independent of the C2 producer.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from pipeline.engine.commit_delivery import resolve_commit_delivery
from sdk.run_control.delivery import decide_delivery, delivery_decision_state


@pytest.fixture(autouse=True)
def _adr0119_legacy_bypass_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin delivery to the ADR 0119 ``bypass`` opt-out for this legacy slice.

    ADR 0119 shipped ``branch_policy=worktree_branch`` as the delivery default,
    which publishes an isolated run's own branch instead of committing onto the
    target checkout; ``decide_delivery`` replays under the live config. These
    tests predate that policy and assert the prior "commit onto the checkout"
    behavior, so they run under ``bypass`` (the ADR's explicit legacy opt-out).
    Projecting ``delivery_branch`` / ``pr_intent`` onto the SDK decision surface
    is T3's work; the new branch-policy behavior is covered by
    ``tests/unit/pipeline/engine/test_commit_delivery.py`` and
    ``test_delivery_branch.py``.
    """
    import pipeline.engine.delivery_branch as _db

    monkeypatch.setattr(_db, "normalize_branch_policy", lambda _raw: "bypass")


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@orcho.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Orcho Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def _worktree(repo: Path, run_dir: Path) -> Path:
    from core.io.git_helpers import create_worktree

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()
    result = create_worktree(
        repo=repo,
        base_ref=head,
        target_path=run_dir / "checkout",
        branch_name="orcho/run/r1",
    )
    assert result.ok, result.error
    return run_dir / "checkout"


def _park(
    tmp_path: Path,
    *,
    verdict: str = "APPROVED",
    run_id: str = "r1",
    verification: dict | None = None,
    add_untracked: bool = True,
    untracked_file: str | None = None,
) -> tuple[Path, Path, Path]:
    """Build a runs_dir with a parked delivery gate. Returns (runs_dir, repo, wt)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    wt = _worktree(repo, run_dir)
    (wt / "app.txt").write_text("base\nrun\n", encoding="utf-8")
    if untracked_file is not None:
        (wt / untracked_file).write_text("generated\n", encoding="utf-8")

    release_entry = {"verdict": verdict, "short_summary": "feat: x"}
    if verdict == "REJECTED":
        release_entry["release_blockers"] = [
            {
                "id": "RB1",
                "severity": "P1",
                "title": "Data loss on apply",
                "body": "The delivery path drops user rows.",
                "required_fix": "Preserve existing rows during delivery.",
                "why_blocks_release": "Shipping would destroy user data.",
            },
        ]
    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=wt,
        run_dir=run_dir,
        run_id=run_id,
        session={
            "status": "done",
            "phases": {
                "final_acceptance": release_entry,
            },
        },
        commit_config={
            "enabled": True,
            "auto_in_ci": "approve",
            "add_untracked": add_untracked,
        },
        no_interactive=True,
        decision_mode="defer",
    )
    ctx = decision.to_dict()
    if verification:
        ctx.update(verification)
    meta = {
        "status": "halted",
        "halt_reason": "commit_delivery_pending",
        "commit_delivery": ctx,
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return runs_dir, repo, wt


def _meta(runs_dir: Path, run_id: str = "r1") -> dict:
    return json.loads((runs_dir / run_id / "meta.json").read_text(encoding="utf-8"))


# ── decide_delivery: allowed actions ─────────────────────────────────────────


def test_approve_commits_and_marks_done(tmp_path: Path) -> None:
    runs_dir, repo, _ = _park(tmp_path)
    result = decide_delivery("r1", "approve", runs_dir=runs_dir, cwd=None)

    assert result.accepted is True
    assert result.status == "committed"
    assert result.terminal_outcome == "done"
    assert result.commit_sha
    assert result.blocker is None
    assert (repo / "app.txt").read_text(encoding="utf-8") == "base\nrun\n"
    assert _meta(runs_dir)["status"] == "done"
    assert "halt_reason" not in _meta(runs_dir)


def test_apply_leaves_uncommitted_and_marks_done(tmp_path: Path) -> None:
    runs_dir, repo, _ = _park(tmp_path)
    result = decide_delivery("r1", "apply", runs_dir=runs_dir, cwd=None)

    assert result.accepted is True
    assert result.status == "applied_uncommitted"
    assert result.terminal_outcome == "done"
    assert result.commit_sha is None
    assert _meta(runs_dir)["status"] == "done"


def test_skip_marks_done(tmp_path: Path) -> None:
    runs_dir, _, _ = _park(tmp_path)
    result = decide_delivery("r1", "skip", runs_dir=runs_dir, cwd=None)

    assert result.accepted is True
    assert result.status == "skipped"
    assert result.terminal_outcome == "done"
    assert _meta(runs_dir)["status"] == "done"


def test_halt_marks_halted(tmp_path: Path) -> None:
    runs_dir, _, _ = _park(tmp_path)
    result = decide_delivery("r1", "halt", runs_dir=runs_dir, cwd=None)

    assert result.accepted is True
    assert result.status == "halted"
    assert result.terminal_outcome == "halted"
    assert result.halt_reason == "commit_decision_halt"
    assert _meta(runs_dir)["halt_reason"] == "commit_decision_halt"


def test_fix_marks_correction_without_followup(tmp_path: Path) -> None:
    """F1: a fix that does not start a follow-up is terminal_outcome='halted'.

    The 'correction marked' state is expressed by the combination
    status='fix_requested' + halt_reason='commit_decision_fix' +
    followup_run_id=None — never by a 'correction marked' terminal_outcome.
    """
    runs_dir, _, _ = _park(tmp_path)
    result = decide_delivery("r1", "fix", runs_dir=runs_dir, cwd=None)

    assert result.accepted is True
    assert result.status == "fix_requested"
    assert result.terminal_outcome == "halted"
    assert result.halt_reason == "commit_decision_fix"
    assert result.followup_run_id is None
    assert _meta(runs_dir)["halt_reason"] == "commit_decision_fix"


# ── decide_delivery: reducer settle parity (ADR 0115 slice 3b) ───────────────
#
# These pin that ``_finalize`` settles done/halted through the single
# ``terminal_outcome.settle_delivery_terminal`` reducer rather than an
# open-coded terminal patch: the done branch runs the canonical residue eviction
# (and keeps the just-applied ``commit_delivery``), and the halt branch stamps
# the SDK-resolved ``halted_at`` without evicting.


def test_approve_settles_done_via_reducer_evicting_residue(tmp_path: Path) -> None:
    """approve → done evicts every transient residue key but keeps commit_delivery.

    Seeds a prior-attempt halt/rejection residue onto the parked meta. The done
    settle must route through the reducer's canonical eviction, clearing all
    ``TRANSIENT_SETTLE_KEYS`` while leaving the freshly-applied ``commit_delivery``
    record intact.
    """
    from pipeline.run_state.terminal import TRANSIENT_SETTLE_KEYS

    runs_dir, _, _ = _park(tmp_path)
    meta = _meta(runs_dir)
    # Seed stale residue a prior REJECTED attempt of the same run could leave.
    meta["halt"] = {"reason": "x", "phase": "final_acceptance"}
    meta["halted_at"] = "2020-01-01T00:00:00+00:00"
    meta["rejected_outcome"] = {"status": "halted"}
    meta["delivery_override"] = {"status": "done"}
    meta["no_op_outcome"] = {"status": "halted"}
    meta["correction_fixed_point"] = {"k": "v"}
    (runs_dir / "r1" / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8",
    )

    result = decide_delivery("r1", "approve", runs_dir=runs_dir, cwd=None)

    assert result.terminal_outcome == "done"
    settled = _meta(runs_dir)
    assert settled["status"] == "done"
    for key in TRANSIENT_SETTLE_KEYS:
        assert key not in settled, f"residue {key!r} survived the done settle"
    # The just-applied decision record is preserved (canonical eviction never
    # touches the delivery keys).
    assert settled["commit_delivery"]["status"] == "committed"


def test_halt_settles_halted_via_reducer_stamping_halted_at(tmp_path: Path) -> None:
    """halt → halted stamps the SDK-resolved ISO ``halted_at`` and does not evict.

    The halt branch must persist the ``halted_at`` timestamp the SDK formats and
    leave the halt residue in place (no canonical eviction on a halt settle).
    """
    runs_dir, _, _ = _park(tmp_path)
    result = decide_delivery("r1", "halt", runs_dir=runs_dir, cwd=None)

    assert result.terminal_outcome == "halted"
    settled = _meta(runs_dir)
    assert settled["status"] == "halted"
    assert settled["halt_reason"] == "commit_decision_halt"
    # halted_at is the SDK-stamped ISO-8601 second-precision timestamp.
    halted_at = settled["halted_at"]
    assert isinstance(halted_at, str) and halted_at.endswith("+00:00")


# ── decide_delivery: typed refusals ──────────────────────────────────────────


def _commit_delivery_statuses() -> frozenset[str]:
    from typing import get_args

    from pipeline.engine.commit_delivery import CommitDeliveryStatus

    return frozenset(get_args(CommitDeliveryStatus))


def test_no_pending_gate_is_typed_refusal(tmp_path: Path) -> None:
    runs_dir, _, _ = _park(tmp_path)
    # Wipe the gate context: a done run has nothing to decide.
    (runs_dir / "r1" / "meta.json").write_text(
        json.dumps({"status": "done"}) + "\n", encoding="utf-8",
    )
    result = decide_delivery("r1", "approve", runs_dir=runs_dir, cwd=None)

    assert result.accepted is False
    assert result.blocker == "no_pending_delivery_gate"
    assert result.terminal_outcome == "done"
    # status must stay within the CommitDeliveryStatus contract (never 'none'):
    # an MCP mirror switching on the delivery-status enum would not recognise
    # an out-of-contract value.
    assert result.status == "not_applicable"
    assert result.status in _commit_delivery_statuses()


def test_already_decided_gate_keeps_valid_status(tmp_path: Path) -> None:
    """A non-decidable but already-decided gate keeps its informative terminal
    status (still a valid CommitDeliveryStatus), not 'none'."""
    runs_dir, _, _ = _park(tmp_path)
    meta = _meta(runs_dir)
    meta["commit_delivery"]["status"] = "committed"
    (runs_dir / "r1" / "meta.json").write_text(
        json.dumps(meta) + "\n", encoding="utf-8",
    )
    result = decide_delivery("r1", "approve", runs_dir=runs_dir, cwd=None)

    assert result.accepted is False
    assert result.blocker == "no_pending_delivery_gate"
    assert result.status == "committed"
    assert result.status in _commit_delivery_statuses()


def test_rejected_release_blocks_approve(tmp_path: Path) -> None:
    runs_dir, _, _ = _park(tmp_path, verdict="REJECTED")
    result = decide_delivery("r1", "approve", runs_dir=runs_dir, cwd=None)

    assert result.accepted is False
    assert result.blocker == "release_blocked"
    assert result.terminal_outcome == "halted"
    # Run is left untouched — still parked.
    assert _meta(runs_dir)["halt_reason"] == "commit_delivery_pending"


def test_rejected_release_allows_fix(tmp_path: Path) -> None:
    runs_dir, _, _ = _park(tmp_path, verdict="REJECTED")
    result = decide_delivery("r1", "fix", runs_dir=runs_dir, cwd=None)

    assert result.accepted is True
    assert result.status == "fix_requested"
    assert result.halt_reason == "commit_decision_fix"
    persisted = _meta(runs_dir)["commit_delivery"]
    assert persisted["release_blockers"][0]["id"] == "RB1"
    assert persisted["release_blockers"][0]["title"] == "Data loss on apply"


def test_rejected_release_blocks_skip(tmp_path: Path) -> None:
    # ``skip`` settles to ``skipped`` ∈ done-statuses without applying delivery;
    # on a rejected release that would clear the halt and present a clean
    # ``done`` with no override marker — refused like the shipping actions so a
    # rejected release never settles successful via skip (ADR 0106).
    runs_dir, _, _ = _park(tmp_path, verdict="REJECTED")
    result = decide_delivery("r1", "skip", runs_dir=runs_dir, cwd=None)

    assert result.accepted is False
    assert result.blocker == "release_blocked"
    assert result.terminal_outcome == "halted"
    assert result.status != "skipped"
    # Run is left untouched — still parked, never flipped to done.
    assert _meta(runs_dir)["status"] != "done"
    assert _meta(runs_dir)["halt_reason"] == "commit_delivery_pending"


def test_required_verification_blocks_approve(tmp_path: Path) -> None:
    runs_dir, _, _ = _park(
        tmp_path,
        verification={
            "verification_policy": "require",
            "verification_missing": ["pytest"],
        },
    )
    result = decide_delivery("r1", "approve", runs_dir=runs_dir, cwd=None)

    assert result.accepted is False
    assert result.blocker == "verification_blocked"
    assert result.status == "verification_blocked"
    # Still parked — receipts can land later and the gate stays decidable.
    assert _meta(runs_dir)["halt_reason"] == "commit_delivery_pending"


def test_invalid_action_raises(tmp_path: Path) -> None:
    runs_dir, _, _ = _park(tmp_path)
    with pytest.raises(ValueError):
        decide_delivery("r1", "bogus", runs_dir=runs_dir, cwd=None)  # type: ignore[arg-type]


# ── delivery_decision_state ──────────────────────────────────────────────────


def test_state_delivery_gate(tmp_path: Path) -> None:
    runs_dir, _, _ = _park(tmp_path)
    state = delivery_decision_state("r1", runs_dir=runs_dir, cwd=None)

    assert state.decidable is True
    assert state.kind == "delivery"
    assert set(state.available_actions) == {"approve", "apply", "skip", "halt"}
    assert state.blocked_actions == ()
    assert state.default_action == "approve"


def test_state_correction_gate_blocks_shipping(tmp_path: Path) -> None:
    runs_dir, _, _ = _park(tmp_path, verdict="REJECTED")
    state = delivery_decision_state("r1", runs_dir=runs_dir, cwd=None)

    assert state.decidable is True
    assert state.kind == "correction"
    # ``skip`` is blocked alongside the shipping actions on a rejected release:
    # skipping would settle a clean ``done`` without applied delivery (ADR 0106).
    assert set(state.blocked_actions) == {"approve", "apply", "skip"}
    assert "fix" in state.available_actions
    assert "skip" not in state.available_actions
    assert state.default_action == "fix"
    # The reason must stay consistent with the action model — never advertise the
    # blocked ``skip`` as an available path (ADR 0106).
    assert state.reason is not None
    assert "skip" not in state.reason
    assert "RB1" in state.reason
    assert "Data loss on apply" in state.reason
    assert "Preserve existing rows" in state.reason


def test_state_none_when_no_gate(tmp_path: Path) -> None:
    runs_dir, _, _ = _park(tmp_path)
    (runs_dir / "r1" / "meta.json").write_text(
        json.dumps({"status": "done"}) + "\n", encoding="utf-8",
    )
    state = delivery_decision_state("r1", runs_dir=runs_dir, cwd=None)

    assert state.decidable is False
    assert state.kind == "none"


def test_state_verification_block_removes_shipping(tmp_path: Path) -> None:
    runs_dir, _, _ = _park(
        tmp_path,
        verification={
            "verification_policy": "require",
            "verification_failed": ["pytest"],
        },
    )
    state = delivery_decision_state("r1", runs_dir=runs_dir, cwd=None)

    assert state.decidable is True
    assert set(state.blocked_actions) == {"approve", "apply"}


# ── T3: durable patch_invalid blocks shipping (captured patch is corrupt) ────


def _set_diff_patch(runs_dir: Path, block: dict, run_id: str = "r1") -> None:
    """Stamp a durable ``meta.diff_patch`` apply-check block (as T2 finalize writes)."""
    meta = _meta(runs_dir, run_id)
    meta["diff_patch"] = block
    (runs_dir / run_id / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8",
    )


def _invalid_block() -> dict:
    return {
        "status": "patch_invalid",
        "reason": "patch_does_not_apply",
        "patch_path": "/runs/r1/diff.patch",
        "baseline_ref": "base-tree",
        "detail": "git apply --check exited with 1",
    }


def test_state_patch_invalid_blocks_shipping_keeps_skip_halt(tmp_path: Path) -> None:
    runs_dir, _, _ = _park(tmp_path)
    _set_diff_patch(runs_dir, _invalid_block())
    state = delivery_decision_state("r1", runs_dir=runs_dir, cwd=None)

    assert state.decidable is True
    assert set(state.blocked_actions) == {"approve", "apply"}
    # The non-shipping actions stay open so the operator can recover.
    assert "skip" in state.available_actions
    assert "halt" in state.available_actions
    assert "approve" not in state.available_actions
    assert "apply" not in state.available_actions
    assert state.reason is not None
    assert "patch_invalid" in state.reason
    assert "/runs/r1/diff.patch" in state.reason
    assert "recover from worktree or rerun" in state.reason


def test_decide_approve_refused_on_patch_invalid(tmp_path: Path) -> None:
    runs_dir, repo, _ = _park(tmp_path)
    _set_diff_patch(runs_dir, _invalid_block())
    result = decide_delivery("r1", "approve", runs_dir=runs_dir, cwd=None)

    assert result.accepted is False
    assert result.blocker == "patch_invalid"
    assert result.terminal_outcome == "halted"
    assert result.status in _commit_delivery_statuses()
    # Run is left untouched — still parked, nothing shipped into the project.
    assert _meta(runs_dir)["halt_reason"] == "commit_delivery_pending"
    assert (repo / "app.txt").read_text(encoding="utf-8") == "base\n"


def test_decide_apply_refused_on_patch_invalid(tmp_path: Path) -> None:
    runs_dir, _, _ = _park(tmp_path)
    _set_diff_patch(runs_dir, _invalid_block())
    result = decide_delivery("r1", "apply", runs_dir=runs_dir, cwd=None)

    assert result.accepted is False
    assert result.blocker == "patch_invalid"


def test_patch_invalid_allows_skip(tmp_path: Path) -> None:
    runs_dir, _, _ = _park(tmp_path)
    _set_diff_patch(runs_dir, _invalid_block())
    result = decide_delivery("r1", "skip", runs_dir=runs_dir, cwd=None)

    assert result.accepted is True
    assert result.status == "skipped"
    assert result.terminal_outcome == "done"


def test_valid_patch_does_not_change_delivery(tmp_path: Path) -> None:
    runs_dir, repo, _ = _park(tmp_path)
    _set_diff_patch(
        runs_dir,
        {
            "status": "patch_valid",
            "reason": "patch_applies",
            "patch_path": "/runs/r1/diff.patch",
            "baseline_ref": "base-tree",
            "detail": "",
        },
    )
    result = decide_delivery("r1", "approve", runs_dir=runs_dir, cwd=None)

    assert result.accepted is True
    assert result.status == "committed"
    assert result.blocker is None
    assert (repo / "app.txt").read_text(encoding="utf-8") == "base\nrun\n"


def test_patch_missing_does_not_block_no_diff_run(tmp_path: Path) -> None:
    # patch_missing is a legitimate no-diff / absent-artifact signal and must
    # NOT refuse a shipping action.
    runs_dir, repo, _ = _park(tmp_path)
    _set_diff_patch(
        runs_dir,
        {
            "status": "patch_missing",
            "reason": "patch_unavailable",
            "patch_path": "/runs/r1/diff.patch",
            "baseline_ref": "base-tree",
            "detail": "patch file is missing or is not a regular file",
        },
    )
    result = decide_delivery("r1", "approve", runs_dir=runs_dir, cwd=None)

    assert result.accepted is True
    assert result.status == "committed"
    assert result.blocker is None


# ── F1: persisted include_untracked is honored on replay ─────────────────────


def test_approve_honors_persisted_add_untracked_false(tmp_path: Path) -> None:
    """A gate parked with add_untracked=false must not deliver untracked files
    on approve, even if the live AppConfig default flips add_untracked to true.

    Reconstructing delivery config from the current AppConfig (default
    add_untracked=True) would silently fold the excluded untracked file into the
    commit — the persisted ``include_untracked`` is the contract.
    """
    runs_dir, repo, _ = _park(
        tmp_path, add_untracked=False, untracked_file="generated.log",
    )
    ctx = _meta(runs_dir)["commit_delivery"]
    assert ctx["include_untracked"] is False
    assert "generated.log" in ctx["untracked_paths"]

    # Sanity: the live config default would have included untracked.
    from core.infra import config

    assert config.AppConfig.load().commit.get("add_untracked") is True

    result = decide_delivery("r1", "approve", runs_dir=runs_dir, cwd=None)

    assert result.accepted is True
    assert result.status == "committed"
    # The tracked change shipped; the excluded untracked file did NOT.
    assert (repo / "app.txt").read_text(encoding="utf-8") == "base\nrun\n"
    assert not (repo / "generated.log").exists()
    committed = _meta(runs_dir)["commit_delivery"]
    assert "generated.log" not in committed.get("untracked_delivered", [])


# ── F2: verification guard is re-evaluated fresh, not from stale fields ───────


def _assessment(*, blocking: bool):
    from pipeline.verification_delivery import DeliveryVerificationAssessment

    return DeliveryVerificationAssessment(
        policy="require",
        required_missing=("pytest",) if blocking else (),
    )


def test_approve_unblocked_when_fresh_assessment_clears_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A require-block parked with stale missing-receipt fields must lift once a
    fresh re-assessment (operator materialized the receipt / recorded a waiver)
    reports no blocking gap — approve then proceeds."""
    runs_dir, repo, _ = _park(
        tmp_path,
        verification={
            "verification_policy": "require",
            "verification_missing": ["pytest"],
        },
    )
    import sdk.run_control.delivery as deliv

    # Simulate the receipt/waiver now present: the contract reconstructs and the
    # fresh assessment no longer blocks.
    monkeypatch.setattr(
        deliv,
        "_reassess_delivery_verification",
        lambda *_a, **_kw: (_assessment(blocking=False), True),
    )

    result = decide_delivery("r1", "approve", runs_dir=runs_dir, cwd=None)

    assert result.accepted is True
    assert result.status == "committed"
    assert (repo / "app.txt").read_text(encoding="utf-8") == "base\nrun\n"


def test_state_unblocks_when_fresh_assessment_clears_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs_dir, _, _ = _park(
        tmp_path,
        verification={
            "verification_policy": "require",
            "verification_missing": ["pytest"],
        },
    )
    import sdk.run_control.delivery as deliv

    monkeypatch.setattr(
        deliv,
        "_reassess_delivery_verification",
        lambda *_a, **_kw: (_assessment(blocking=False), True),
    )

    state = delivery_decision_state("r1", runs_dir=runs_dir, cwd=None)

    assert state.blocked_actions == ()
    assert "approve" in state.available_actions


def test_approve_still_blocked_when_fresh_assessment_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirror image: a fresh require-block (receipt still missing) keeps approve
    refused — the guard tracks current state, not just the parked snapshot."""
    runs_dir, _, _ = _park(tmp_path)  # no persisted verification fields
    import sdk.run_control.delivery as deliv

    monkeypatch.setattr(
        deliv,
        "_reassess_delivery_verification",
        lambda *_a, **_kw: (_assessment(blocking=True), True),
    )

    result = decide_delivery("r1", "approve", runs_dir=runs_dir, cwd=None)

    assert result.accepted is False
    assert result.blocker == "verification_blocked"
    assert _meta(runs_dir)["halt_reason"] == "commit_delivery_pending"


# ── T3: companion delivery disclosure projected into scope_disclosure ────────
#
# ADR 0107 extends the scope_disclosure semantics — backward-compatibly, same
# ``[alias]/rel`` string format — to surface every declared companion repo's
# changed paths from the durable ``scope_companions`` block, not only the
# strict-mono violation siblings. Per-repo typed state lives in the core-durable
# ``multi_project_delivery`` evidence block, never in these strings, so the
# MCP-visible shape (``list[str]``) is unchanged.

_DIRTY_COMPANION = {
    "alias": "orcho-mcp",
    "path": "/ws/orcho-mcp",
    "state": "dirty",
    "changed_paths": ["[orcho-mcp]/read.py"],
}
_COMMITTED_COMPANION = {
    "alias": "orcho-web",
    "path": "/ws/orcho-web",
    "state": "committed",
    "changed_paths": ["[orcho-web]/app.py"],
}


def test_state_projects_companion_disclosure_including_committed(
    tmp_path: Path,
) -> None:
    """A decidable gate surfaces dirty AND observably-committed companion paths."""
    runs_dir, _, _ = _park(
        tmp_path,
        verification={"scope_companions": [_DIRTY_COMPANION, _COMMITTED_COMPANION]},
    )
    state = delivery_decision_state("r1", runs_dir=runs_dir, cwd=None)

    assert state.decidable is True
    # Both companions disclosed, in the backward-compatible [alias]/rel format.
    assert "[orcho-mcp]/read.py" in state.scope_disclosure
    assert "[orcho-web]/app.py" in state.scope_disclosure
    # No scope_blocker → shipping stays available (disclosure is not a block).
    assert "approve" in state.available_actions
    assert state.blocked_actions == ()


def test_state_scope_block_preserves_legacy_prefix_then_appends_companions(
    tmp_path: Path,
) -> None:
    """The legacy violation siblings stay FIRST; companion paths append after."""
    runs_dir, _, _ = _park(
        tmp_path,
        verification={
            "delivery_scope": "strict_mono",
            "scope_blocker": "delivery_scope_violation",
            "scope_disclosure": ["[orcho-mcp]/read.py"],
            "scope_companions": [_DIRTY_COMPANION, _COMMITTED_COMPANION],
        },
    )
    state = delivery_decision_state("r1", runs_dir=runs_dir, cwd=None)

    # Legacy disclosure prefix preserved byte-for-byte at the head.
    assert state.scope_disclosure[0] == "[orcho-mcp]/read.py"
    # The committed companion is appended (full companion file set disclosed).
    assert "[orcho-web]/app.py" in state.scope_disclosure
    # The dirty sibling appears exactly once (deduped against scope_companions).
    assert state.scope_disclosure.count("[orcho-mcp]/read.py") == 1
    # Strict-mono violation still blocks shipping; skip/halt stay open.
    assert "approve" in state.blocked_actions
    assert "apply" in state.blocked_actions
    assert "skip" in state.available_actions


def test_decide_scope_block_refusal_surfaces_full_companion_disclosure(
    tmp_path: Path,
) -> None:
    """decide_delivery projects the enriched disclosure on the scope refusal."""
    runs_dir, _, _ = _park(
        tmp_path,
        verification={
            "delivery_scope": "strict_mono",
            "scope_blocker": "delivery_scope_violation",
            "scope_disclosure": ["[orcho-mcp]/read.py"],
            "scope_companions": [_DIRTY_COMPANION, _COMMITTED_COMPANION],
        },
    )
    result = decide_delivery("r1", "approve", runs_dir=runs_dir, cwd=None)

    assert result.accepted is False
    assert result.blocker == "delivery_scope_violation"
    assert "[orcho-mcp]/read.py" in result.scope_disclosure
    assert "[orcho-web]/app.py" in result.scope_disclosure


def test_decide_accepted_result_carries_companion_disclosure(
    tmp_path: Path,
) -> None:
    """An accepted approve still names a companion repo left behind (from ctx)."""
    runs_dir, repo, _ = _park(
        tmp_path,
        verification={"scope_companions": [_DIRTY_COMPANION]},
    )
    result = decide_delivery("r1", "approve", runs_dir=runs_dir, cwd=None)

    # Primary delivered (no scope_blocker) ...
    assert result.accepted is True
    assert result.status == "committed"
    # ... and the still-dirty companion is disclosed on the result.
    assert "[orcho-mcp]/read.py" in result.scope_disclosure


def test_scope_disclosure_backward_compatible_without_companions(
    tmp_path: Path,
) -> None:
    """No scope_companions → scope_disclosure is the legacy list, order intact."""
    runs_dir, _, _ = _park(
        tmp_path,
        verification={
            "scope_blocker": "delivery_scope_violation",
            "scope_disclosure": ["[orcho-mcp]/b.py", "[orcho-mcp]/a.py"],
        },
    )
    state = delivery_decision_state("r1", runs_dir=runs_dir, cwd=None)

    # Byte-identical to the persisted legacy list (order preserved, no sort).
    assert state.scope_disclosure == ("[orcho-mcp]/b.py", "[orcho-mcp]/a.py")
    # Every entry is a plain [alias]/rel string (MCP-visible format unchanged).
    assert all(
        isinstance(p, str) and p.startswith("[") and "]/" in p
        for p in state.scope_disclosure
    )


# ── F1 (review retry): SDK approve syncs the durable multi_project_delivery ───
#
# The defer flow parks ``meta['multi_project_delivery']`` with
# ``primary_status='pending'`` and a dirty companion. After an operator approve
# the primary really commits, so the durable companion block must flip to the
# terminal status — not read stale ``pending`` — while preserving the
# still-dirty companion disclosure so finalization/evidence keep the caveat.


def _inject_multi_project_delivery(
    runs_dir: Path,
    *,
    primary_status: str,
    companions: list[dict],
    run_id: str = "r1",
) -> None:
    """Add a parked durable ``multi_project_delivery`` block to the run meta."""
    meta = _meta(runs_dir, run_id)
    meta["multi_project_delivery"] = {
        "primary_status": primary_status,
        "companions": companions,
    }
    (runs_dir / run_id / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8",
    )


def test_approve_syncs_durable_multi_project_delivery_to_committed(
    tmp_path: Path,
) -> None:
    """Approve flips primary_status pending→committed, keeps the dirty companion."""
    runs_dir, repo, _ = _park(
        tmp_path,
        verification={"scope_companions": [_DIRTY_COMPANION]},
    )
    _inject_multi_project_delivery(
        runs_dir, primary_status="pending", companions=[_DIRTY_COMPANION],
    )

    result = decide_delivery("r1", "approve", runs_dir=runs_dir, cwd=None)
    assert result.accepted is True
    assert result.status == "committed"

    meta = _meta(runs_dir)
    # commit_delivery stays the authoritative applied result ...
    assert meta["commit_delivery"]["status"] == "committed"
    # ... and the durable companion block is no longer stale ``pending``.
    block = meta["multi_project_delivery"]
    assert block["primary_status"] == "committed"
    # The still-dirty companion disclosure is preserved verbatim (caveat survives).
    assert block["companions"] == [_DIRTY_COMPANION]


def test_apply_syncs_durable_multi_project_delivery_to_applied(
    tmp_path: Path,
) -> None:
    """Apply mirrors its terminal status into the durable companion block."""
    runs_dir, _, _ = _park(
        tmp_path,
        verification={"scope_companions": [_DIRTY_COMPANION]},
    )
    _inject_multi_project_delivery(
        runs_dir, primary_status="pending", companions=[_DIRTY_COMPANION],
    )

    result = decide_delivery("r1", "apply", runs_dir=runs_dir, cwd=None)
    assert result.status == "applied_uncommitted"

    block = _meta(runs_dir)["multi_project_delivery"]
    assert block["primary_status"] == "applied_uncommitted"
    assert block["companions"] == [_DIRTY_COMPANION]


def test_approve_reconstructs_block_from_gate_context_companions(
    tmp_path: Path,
) -> None:
    """No parked block, but ctx carries companions → reconstruct it as committed."""
    runs_dir, _, _ = _park(
        tmp_path,
        verification={"scope_companions": [_DIRTY_COMPANION]},
    )
    # Note: no _inject_multi_project_delivery — only commit_delivery.scope_companions.

    decide_delivery("r1", "approve", runs_dir=runs_dir, cwd=None)

    block = _meta(runs_dir)["multi_project_delivery"]
    assert block["primary_status"] == "committed"
    assert block["companions"] == [_DIRTY_COMPANION]


def test_approve_single_repo_leaves_no_multi_project_block(tmp_path: Path) -> None:
    """A clean single-repo run never grows a companion block (no-op preserved)."""
    runs_dir, _, _ = _park(tmp_path)

    decide_delivery("r1", "approve", runs_dir=runs_dir, cwd=None)

    assert "multi_project_delivery" not in _meta(runs_dir)


# ── approved-retry reconciliation: phantom rejected gate is gone ──────────────
#
# Dogfood shape: an early final_acceptance REJECTED → repair → late APPROVED.
# Finalization (T1) supersedes the phantom rejected ``commit_delivery`` gate, so
# the persisted clean-done meta carries NO ``commit_delivery`` at all. These pin
# that the SDK delivery-gate / status projections read that reconciled meta as a
# settled clean success — not a lingering correction / release_blocked gate.


def _write_clean_done_retry_meta(runs_dir: Path, run_id: str = "r1") -> None:
    """Overwrite the run meta with the reconciled clean-done retry shape.

    Mirrors what ``_supersede_stale_rejection_residue`` leaves behind on the
    approved branch: ``status='done'`` with an APPROVED final acceptance and
    none of the stale rejection markers (``halt_reason`` / ``rejected_outcome``
    / ``delivery_override`` / ``halt``) nor a phantom rejected ``commit_delivery``.
    """
    meta = {
        "status": "done",
        "phases": {
            "final_acceptance": {
                "verdict": "APPROVED",
                "ship_ready": True,
                "short_summary": "feat: x",
            },
        },
    }
    (runs_dir / run_id / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8",
    )


def test_state_clean_done_retry_has_no_phantom_gate(tmp_path: Path) -> None:
    runs_dir, _, _ = _park(tmp_path, verdict="REJECTED")
    # The approved retry reconciled the meta: no rejected commit_delivery left.
    _write_clean_done_retry_meta(runs_dir)

    state = delivery_decision_state("r1", runs_dir=runs_dir, cwd=None)

    assert state.decidable is False
    assert state.kind == "none"
    assert state.reason == "no pending delivery gate"


def test_decide_clean_done_retry_not_release_blocked(tmp_path: Path) -> None:
    runs_dir, _, _ = _park(tmp_path, verdict="REJECTED")
    _write_clean_done_retry_meta(runs_dir)

    for action in ("approve", "apply"):
        result = decide_delivery("r1", action, runs_dir=runs_dir, cwd=None)
        # No phantom rejected gate → the stale 'release_blocked' refusal is gone.
        assert result.blocker != "release_blocked"
        assert result.blocker == "no_pending_delivery_gate"
        assert result.terminal_outcome == "done"


def test_state_current_rejected_still_correction_fix_or_halt(tmp_path: Path) -> None:
    # ADR 0106 guard (counter-test): a run parked on a CURRENT REJECTED
    # commit_delivery still reads as a decidable fix-or-halt correction gate.
    # The approved-retry reconciliation must NOT weaken this.
    runs_dir, _, _ = _park(tmp_path, verdict="REJECTED")

    state = delivery_decision_state("r1", runs_dir=runs_dir, cwd=None)

    assert state.decidable is True
    assert state.kind == "correction"
    assert state.default_action == "fix"
    assert set(state.blocked_actions) == {"approve", "apply", "skip"}
    assert state.reason is not None
    assert "fix or halt only" in state.reason

    for action in ("approve", "apply"):
        result = decide_delivery("r1", action, runs_dir=runs_dir, cwd=None)
        assert result.accepted is False
        assert result.blocker == "release_blocked"


# ── T1: rejected / fix-marked correction directs to an ordinary follow-up ────
#
# After ``fix`` is accepted (status='fix_requested') — or when the run dead-ended
# on an auto-refused rejected release (_is_rejected_release_gate) — repeating
# ``fix`` is inert and a bare resume cannot advance the run. The state advertises
    # no inert repeat (only ``halt`` stays) and routes the client to an ordinary
# follow-up via the already-mapped ``reason`` field, naming the held diff.patch.


def test_state_fix_requested_directs_to_followup(tmp_path: Path) -> None:
    runs_dir, _, _ = _park(tmp_path, verdict="REJECTED")
    fix = decide_delivery("r1", "fix", runs_dir=runs_dir, cwd=None)
    assert fix.status == "fix_requested"
    # An artifact diff must not alter the ordinary follow-up instruction.
    (runs_dir / "r1" / "diff.patch").write_text("patch\n", encoding="utf-8")

    state = delivery_decision_state("r1", runs_dir=runs_dir, cwd=None)

    assert state.decidable is True
    assert state.kind == "correction"
    # The inert ``fix`` repeat is NOT advertised as an actionable next step ...
    assert "fix" not in state.available_actions
    assert state.default_action is None
    # ... only ``halt`` (give up) remains available.
    assert state.available_actions == ("halt",)
    assert "fix" in state.blocked_actions
    # ADR 0106: shipping (approve/apply) and skip stay blocked on the rejected
    # release alongside the now-inert fix — none is offered as an actionable step.
    assert set(state.blocked_actions) == {"fix", "approve", "apply", "skip"}
    for inert in ("approve", "apply", "skip"):
        assert inert not in state.available_actions
    # The reason routes the client to an ordinary follow-up; diff artifacts are
    # not replayed as correction input.
    assert state.reason is not None
    assert "orcho_run_resume run_id=r1" in state.reason


def test_state_fix_requested_without_held_diff_still_points_to_followup(
    tmp_path: Path,
) -> None:
    runs_dir, _, _ = _park(tmp_path, verdict="REJECTED")
    decide_delivery("r1", "fix", runs_dir=runs_dir, cwd=None)
    patch = runs_dir / "r1" / "diff.patch"
    if patch.exists():
        patch.unlink()

    state = delivery_decision_state("r1", runs_dir=runs_dir, cwd=None)

    assert state.reason is not None
    assert "orcho_run_resume run_id=r1" in state.reason
    # Artifact presence is never part of the correction launch request.
    assert "diff.patch" not in state.reason


def test_state_rejected_dead_end_directs_to_followup(tmp_path: Path) -> None:
    runs_dir, _, _ = _park(tmp_path, verdict="REJECTED")
    meta = _meta(runs_dir)
    # Reshape the parked gate into the auto rejected-release dead-end shape: a
    # ``not_applicable`` decision still carrying the non-APPROVED verdict.
    meta["commit_delivery"]["status"] = "not_applicable"
    (runs_dir / "r1" / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8",
    )

    state = delivery_decision_state("r1", runs_dir=runs_dir, cwd=None)

    assert state.decidable is True
    assert state.kind == "correction"
    assert state.available_actions == ("halt",)
    assert state.default_action is None
    assert "fix" not in state.available_actions
    assert state.reason is not None
    assert "orcho_run_resume run_id=r1" in state.reason


def test_state_approved_pending_serialization_is_byte_identical(
    tmp_path: Path,
) -> None:
    """The non-rejected (approved-pending) gate projection stays byte/structure-
    identical to the baseline — the follow-up routing never touches this path."""
    from sdk._jsonable import to_jsonable

    runs_dir, _, _ = _park(tmp_path)  # verdict defaults to APPROVED
    state = delivery_decision_state("r1", runs_dir=runs_dir, cwd=None)

    assert to_jsonable(state) == {
        "run_id": "r1",
        "decidable": True,
        "kind": "delivery",
        "available_actions": ["approve", "apply", "skip", "halt"],
        "blocked_actions": [],
        "default_action": "approve",
        "reason": None,
        "scope_disclosure": [],
    }


def test_decide_approve_result_serialization_is_structurally_identical(
    tmp_path: Path,
) -> None:
    """The DeliveryDecisionResult of an approve on a non-rejected gate keeps its
    exact field set and approved-path values. ADR 0119 (T3) adds two additive
    fields — ``delivery_branch`` and ``pr_intent`` — which are ``None`` on this
    ``bypass`` commit-onto-checkout path, so the approved path stays otherwise
    structure-identical to the baseline.

    ``commit_sha`` and ``artifact_paths`` are intrinsically volatile (a fresh sha
    / absolute run-dir paths), so they are asserted for shape/presence only; every
    other field is pinned to its baseline approved value.
    """
    from sdk._jsonable import to_jsonable

    runs_dir, _, _ = _park(tmp_path)  # verdict defaults to APPROVED
    result = decide_delivery("r1", "approve", runs_dir=runs_dir, cwd=None)
    payload = to_jsonable(result)

    # Exact key set — the additive ADR 0119 fields are present, nothing dropped.
    assert set(payload) == {
        "run_id",
        "action",
        "accepted",
        "status",
        "terminal_outcome",
        "halt_reason",
        "artifact_paths",
        "commit_sha",
        "delivery_branch",
        "pr_intent",
        "pr_url",
        "blocker",
        "followup_run_id",
        "scope_disclosure",
    }
    # Deterministic approved-path values.
    assert payload["run_id"] == "r1"
    assert payload["action"] == "approve"
    assert payload["accepted"] is True
    assert payload["status"] == "committed"
    assert payload["terminal_outcome"] == "done"
    assert payload["halt_reason"] is None
    assert payload["blocker"] is None
    assert payload["followup_run_id"] is None
    assert payload["scope_disclosure"] == []
    # bypass commits onto the checkout: commit_sha carries the outcome, the
    # branch-publish fields stay null.
    assert payload["delivery_branch"] is None
    assert payload["pr_intent"] is None
    # No PR was opened on the commit-onto-checkout path.
    assert payload["pr_url"] is None
    # Volatile but structurally pinned.
    assert isinstance(payload["commit_sha"], str) and payload["commit_sha"]
    assert isinstance(payload["artifact_paths"], list)


def test_load_status_clean_done_retry_drops_stale_rejection(tmp_path: Path) -> None:
    # SDK status projection: a finalized clean-done retry dir must not surface a
    # stale halt_reason='final_acceptance_rejected' or stale rejected residue in
    # the projected meta.extra.
    from sdk.status import load_status

    runs_dir, _, _ = _park(tmp_path, verdict="REJECTED")
    _write_clean_done_retry_meta(runs_dir)

    status = load_status("r1", runs_dir=runs_dir, cwd=None)

    assert status.meta is not None
    assert status.meta.status == "done"
    extra = status.meta.extra
    assert extra.get("halt_reason") != "final_acceptance_rejected"
    assert "halt_reason" not in extra
    assert "rejected_outcome" not in extra
    assert "delivery_override" not in extra
    assert "halt" not in extra
    assert "commit_delivery" not in extra


# ── ADR 0119 delivery-branch projection ──────────────────────────────────────
#
# The autouse ``_adr0119_legacy_bypass_delivery`` fixture pins delivery to
# ``bypass`` for the legacy slice; the worktree_branch case below overrides that
# fixture for its own test to exercise the publish projection.


def test_worktree_branch_publish_projects_delivery_branch_and_pr_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under ``branch_policy=worktree_branch`` an approve publishes the run
    branch: the SDK projection carries ``delivery_branch`` + ``pr_intent`` and
    leaves ``commit_sha`` None (nothing landed on the target checkout)."""
    import pipeline.engine.delivery_branch as _db

    # Override the module-level legacy-bypass fixture for this case.
    monkeypatch.setattr(_db, "normalize_branch_policy", lambda _raw: "worktree_branch")

    runs_dir, repo, _ = _park(tmp_path)
    old_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    result = decide_delivery("r1", "approve", runs_dir=runs_dir, cwd=None)

    assert result.accepted is True
    assert result.status == "committed"
    assert result.terminal_outcome == "done"
    # Pure publish: no commit landed on the canonical checkout.
    assert result.commit_sha is None
    assert subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip() == old_head
    # delivery_branch + pr_intent carry the outcome.
    assert result.delivery_branch is not None
    assert result.delivery_branch.startswith("orcho/deliver/")
    intent = result.pr_intent
    assert intent is not None
    assert intent.branch == result.delivery_branch
    assert intent.base == "main"
    assert intent.title
    # Provider-neutral: plain git, never gh/glab.
    assert intent.suggested_command.startswith("git push")
    assert "gh " not in intent.suggested_command
    assert "glab" not in intent.suggested_command
    # Serialized projection round-trips the nested pr_intent record.
    from sdk._jsonable import to_jsonable

    payload = to_jsonable(result)
    assert payload["commit_sha"] is None
    assert payload["delivery_branch"] == result.delivery_branch
    assert payload["pr_intent"] == {
        "branch": result.delivery_branch,
        "base": "main",
        "title": intent.title,
        "suggested_command": intent.suggested_command,
    }
    # No git-provider is registered in the test env, so no PR was opened.
    assert result.pr_url is None
    assert payload["pr_url"] is None


def test_worktree_branch_publish_projects_pr_url_when_pr_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a git-provider opens a PR during the publish, the SDK projection
    carries its ``pr_url`` — sourced from the applied decision's ``pr_url``, not
    re-parsed from the human-readable delivery notice."""
    import pipeline.engine.commit_delivery as _cd
    import pipeline.engine.delivery_branch as _db
    from pipeline.engine.delivery_publish import PublishResult

    monkeypatch.setattr(_db, "normalize_branch_policy", lambda _raw: "worktree_branch")
    pr_url = "https://example.invalid/pr/7"
    monkeypatch.setattr(
        _cd, "publish_delivery",
        lambda *a, **k: PublishResult(pushed=True, pr_url=pr_url),
    )

    runs_dir, _, _ = _park(tmp_path)

    result = decide_delivery("r1", "approve", runs_dir=runs_dir, cwd=None)

    assert result.accepted is True
    assert result.status == "committed"
    assert result.pr_url == pr_url
    from sdk._jsonable import to_jsonable

    assert to_jsonable(result)["pr_url"] == pr_url


def test_bypass_projection_carries_commit_sha_not_branch(tmp_path: Path) -> None:
    """The documented fill rule on the legacy commit-onto-checkout path: the
    projection carries ``commit_sha`` and leaves the branch-publish fields null."""
    runs_dir, _, _ = _park(tmp_path)

    result = decide_delivery("r1", "approve", runs_dir=runs_dir, cwd=None)

    assert result.status == "committed"
    # bypass committed onto the checkout: commit_sha populated.
    assert result.commit_sha
    # No branch was published, so both additive fields stay None.
    assert result.delivery_branch is None
    assert result.pr_intent is None
    # No PR opened on the commit-onto-checkout path.
    assert result.pr_url is None
