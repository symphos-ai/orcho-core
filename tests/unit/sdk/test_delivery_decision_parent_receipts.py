"""An out-of-band delivery decision inherits the parent's receipts (ADR 0089).

A correction follow-up child shares its parent's retained worktree. In-run,
``state_setup`` stamps the parent as a receipt source under
``verification_parent_runs`` and both readiness and the delivery gate inherit
the parent's valid receipts for the identical subject. The SDK re-check that
``decide_delivery`` / ``delivery_decision_state`` run out of band rebuilt the
contract with empty extras, searched only the child's run dir, and refused a
child that changed no code with "required verification incomplete" — the
operator then had to re-run every gate by hand. The re-check now rebuilds the
same single source from the persisted ``parent_run_id`` / ``parent_run_dir``,
and a refusal names the gap commands plus the exact ``orcho verify run`` line.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from core.io.git_helpers import create_worktree
from pipeline.engine.commit_delivery import resolve_commit_delivery
from pipeline.evidence.verification_receipt import write_command_receipt
from pipeline.verification_receipt_index import (
    VERIFICATION_PARENT_RUNS_EXTRAS_KEY,
    parent_sources_from_meta,
)
from pipeline.verification_subject import capture_verification_subject
from sdk.run_control.delivery import decide_delivery, delivery_decision_state

pytestmark = [pytest.mark.sdk, pytest.mark.git_worktree]

_PLUGIN = '''\
PLUGIN = {
    "verification_envs": {"ci": {}},
    "verification": {
        "default_env": "ci",
        "delivery_policy": "require",
        "required": ["req"],
        "commands": {
            "req": {"run": "python -c \\"pass\\"", "parity": "differential"},
        },
    },
}
'''


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _init_project(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@orcho.invalid")
    _git(repo, "config", "user.name", "Orcho Test")
    _git(repo, "config", "commit.gpgsign", "false")
    plugin_dir = repo / ".orcho" / "multiagent"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text(_PLUGIN, encoding="utf-8")
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return _git(repo, "rev-parse", "HEAD")


def _park_child(
    tmp_path: Path, *, link_parent: bool,
) -> tuple[Path, Path, Path, Path]:
    """Parent run verified the diff; child parks on the SAME retained worktree.

    Returns ``(runs_dir, repo, worktree, parent_run_dir)``. The parent receipt
    is captured against the worktree's current subject, so it is ``present``
    for the child only through inheritance.
    """
    repo = tmp_path / "repo"
    head = _init_project(repo)
    runs = tmp_path / "runs"
    parent_dir = runs / "parent"
    child_dir = runs / "child"
    parent_dir.mkdir(parents=True)
    child_dir.mkdir(parents=True)
    result = create_worktree(
        repo=repo, base_ref=head, target_path=parent_dir / "checkout",
        branch_name="orcho/run/parent",
    )
    assert result.ok, result.error
    wt = parent_dir / "checkout"
    (wt / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    write_command_receipt(
        output_dir=parent_dir,
        result={
            "command": "req",
            "env": "ci",
            "cwd": str(wt),
            "placeholders": {"checkout": str(wt), "project": str(repo)},
            "argv": ["python", "-c", "pass"],
            "assertions": [],
            "exit_code": 0,
            "duration_s": 0.1,
            "parity": "differential",
            "detail": "",
            "git": {"checkout_head": head, "baseline_head": head},
            "subject": capture_verification_subject(wt),
            "dependencies": [],
        },
    )
    (parent_dir / "meta.json").write_text(
        json.dumps({"status": "halted", "project": str(repo)}), encoding="utf-8",
    )

    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=wt,
        run_dir=child_dir,
        run_id="child",
        session={
            "status": "done",
            "phases": {"final_acceptance": {"verdict": "APPROVED", "short_summary": "ok"}},
        },
        commit_config={"enabled": True, "add_untracked": True, "branch_policy": "bypass"},
        no_interactive=True,
        decision_mode="defer",
    )
    assert decision.status == "pending" and decision.action == "none"
    meta = {
        "status": "halted",
        "halt_reason": "commit_delivery_pending",
        "project": str(repo),
        "profile": "correction",
        "resume_mode": "followup",
        "commit_delivery": decision.to_dict(),
    }
    if link_parent:
        meta["parent_run_id"] = "parent"
        meta["parent_run_dir"] = str(parent_dir)
    (child_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return runs, repo, wt, parent_dir


# ── parent_sources_from_meta: the durable lineage → receipt source ────────────


def test_parent_sources_from_meta_reads_the_persisted_lineage() -> None:
    sources = parent_sources_from_meta(
        {"parent_run_id": "p1", "parent_run_dir": "/runs/p1"},
    )
    assert [(s.run_id, s.run_dir) for s in sources] == [("p1", "/runs/p1")]


@pytest.mark.parametrize(
    "meta",
    [
        None,
        {},
        {"parent_run_id": "p1"},
        {"parent_run_dir": "/runs/p1"},
        {"parent_run_id": "", "parent_run_dir": "/runs/p1"},
        {"parent_run_id": "p1", "parent_run_dir": 7},
    ],
    ids=["none", "empty", "id-only", "dir-only", "empty-id", "non-str-dir"],
)
def test_parent_sources_from_meta_degrades_to_empty(meta) -> None:
    assert parent_sources_from_meta(meta) == ()


# ── the SDK re-check threads the parent source into the assessment ───────────


def test_reassessment_passes_parent_sources_in_extras(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs, _repo, _wt, parent_dir = _park_child(tmp_path, link_parent=True)
    seen: list[dict] = []

    import pipeline.verification_delivery as vd

    real = vd.assess_delivery_verification

    def spy(contract, run_dir, ctx, extras, **kw):
        seen.append(dict(extras))
        return real(contract, run_dir, ctx, extras, **kw)

    monkeypatch.setattr(vd, "assess_delivery_verification", spy)

    delivery_decision_state("child", runs_dir=runs, cwd=None)

    assert seen, "the fresh re-check did not run the assessment"
    sources = seen[0][VERIFICATION_PARENT_RUNS_EXTRAS_KEY]
    assert [(s.run_id, s.run_dir) for s in sources] == [("parent", str(parent_dir))]


# ── producer → consumer: parent receipt unblocks the child's decision ─────────


def test_child_without_code_changes_inherits_parent_receipt_and_ships(
    tmp_path: Path,
) -> None:
    runs, repo, _wt, _parent_dir = _park_child(tmp_path, link_parent=True)

    state = delivery_decision_state("child", runs_dir=runs, cwd=None)
    assert "approve" in state.available_actions, state
    assert state.blocked_actions == ()

    result = decide_delivery("child", "approve", runs_dir=runs, cwd=None)
    assert result.accepted, result
    assert result.status == "committed"
    assert (repo / "app.txt").read_text(encoding="utf-8") == "base\nrun\n"


def test_unlinked_child_is_refused_and_the_reason_names_the_gap(
    tmp_path: Path,
) -> None:
    runs, repo, _wt, _parent_dir = _park_child(tmp_path, link_parent=False)

    state = delivery_decision_state("child", runs_dir=runs, cwd=None)
    assert "approve" in state.blocked_actions
    assert state.reason is not None
    assert state.reason.startswith("required verification incomplete")
    assert "missing: req" in state.reason
    assert "run: orcho verify run --required --run-id child" in state.reason

    result = decide_delivery("child", "approve", runs_dir=runs, cwd=None)
    assert result.accepted is False
    assert result.blocker == "verification_blocked"
    assert result.reason == state.reason
    assert (repo / "app.txt").read_text(encoding="utf-8") == "base\n"


# ── the refusal reason follows the assessment, not a fixed sentence ──────────


def test_reason_lists_path_selected_commands_positionally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When delivery selected gates beyond ``verification.required`` the hint
    must name them (``--required`` would rerun too little, ADR 0094)."""
    from pipeline.verification_delivery import DeliveryVerificationAssessment

    runs, _repo, _wt, _parent_dir = _park_child(tmp_path, link_parent=False)
    import sdk.run_control.delivery as deliv

    assessment = DeliveryVerificationAssessment(
        policy="require",
        required_missing=("run-state-unit", "cli-sdk-unit"),
        required_stale=("lint",),
        suggested_commands=(
            "orcho verify env --env ci --run-id child --project /p",
            "orcho verify run run-state-unit cli-sdk-unit lint --run-id child --project /p",
        ),
    )
    monkeypatch.setattr(
        deliv, "_reassess_delivery_verification", lambda *_a, **_kw: (assessment, True),
    )

    state = delivery_decision_state("child", runs_dir=runs, cwd=None)

    assert state.reason == (
        "required verification incomplete — receipt or waiver needed; "
        "missing: run-state-unit, cli-sdk-unit; stale: lint; "
        "run: orcho verify run run-state-unit cli-sdk-unit lint --run-id child --project /p"
    )


def test_persisted_fallback_reason_names_the_recorded_gaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs, _repo, _wt, _parent_dir = _park_child(tmp_path, link_parent=False)
    meta_path = runs / "child" / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["commit_delivery"].update({
        "verification_policy": "require",
        "verification_failed": ["req"],
    })
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    import sdk.run_control.delivery as deliv

    monkeypatch.setattr(
        deliv, "_reassess_delivery_verification", lambda *_a, **_kw: (None, False),
    )

    state = delivery_decision_state("child", runs_dir=runs, cwd=None)

    assert state.reason == (
        "required verification incomplete — receipt or waiver needed; failed: req"
    )
