# SPDX-License-Identifier: Apache-2.0
"""Incident 20260612_213530 regression: review-retry resume worktree subject.

End-to-end (real git worktrees, mock provider) reproductions of the incident
where a checkpoint-resume after a ``review_changes`` rejection re-minted a
clean ``wt_<run_id>`` checkout instead of reusing the retained worktree that
held the rejected diff — losing the diff and "repairing" an empty tree.

The incident's defining detail is the **run-dir / worktree-id drift**: the
resumed run directory name (``20260612_213530``) does not match the original
run's worktree id (``wt_20260612_213531``), so the resolver's ``wt_<run_id>``
reuse branch could not find the retained worktree.

A genuine ``review_changes`` rejection is not reachable through the stock mock
provider (it approves reviews), so these tests reconstruct the paused durable
state the original run left behind — the retained isolated worktree carrying an
uncommitted diff, plus a drifted run dir whose ``meta.json`` records the active
``review_changes`` handoff — record the ``retry_feedback`` decision through the
SDK, and then drive the **resume** with the mock provider. The resume's
worktree selection is exactly what the incident got wrong.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agents.runtimes import MockAgentProvider
from pipeline.checkpoint import CheckpointStore
from pipeline.engine.worktree import WorktreeConfigError, resolve_worktree_for_run
from pipeline.project.app import run_project_pipeline
from pipeline.project.retry_subject import RepairSubjectUnproven
from pipeline.project.types import PresentationPolicy, ProjectRunRequest
from sdk.errors import InvalidPhaseHandoffState
from sdk.phase_handoff import phase_handoff_decide
from sdk.run_control.snapshots import load_run_snapshot

pytestmark = [pytest.mark.git_worktree, pytest.mark.filesystem_heavy]

_ORIG_WT_ID = "20260612_213531"   # original run's worktree id (wt_<id>)
_RESUME_RUN = "20260612_213530"   # resumed run-dir name — DRIFTS from above
_HANDOFF_ID = "review_changes:repair_round:1"
_DIFF_MARKER = "rejected_change.txt"


def _init_repo(repo: Path) -> None:
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


def _make_retained_worktree(
    project: Path, runs_dir: Path, run_id: str, *, with_diff: bool = True,
) -> dict:
    """Materialise a real registered isolated worktree.

    With ``with_diff`` (default) it carries the rejected uncommitted diff; with
    ``with_diff=False`` it is registered but clean at the recorded base — the
    clean-HEAD guard case (worktree present, rejected diff gone).
    """
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    ctx = resolve_worktree_for_run(
        run_id=run_id,
        project_dir=project,
        run_dir=run_dir,
        worktree_config={"enabled": True, "isolation": "per_run"},
    )
    assert ctx.is_isolated
    if with_diff:
        # The rejected diff: an uncommitted change living only in this worktree.
        (Path(ctx.path) / _DIFF_MARKER).write_text(
            "the rejected change under review\n", encoding="utf-8",
        )
        assert _git_status_short(Path(ctx.path))  # non-empty
    else:
        assert not _git_status_short(Path(ctx.path))  # clean at recorded base
    return ctx.to_dict()


def _git_status_short(cwd: Path) -> str:
    r = subprocess.run(
        ["git", "status", "--short"], cwd=cwd, capture_output=True, text=True,
    )
    return r.stdout.strip()


def _diff_survived(cwd: Path) -> bool:
    """True when the rejected diff is present in the (reused) retained worktree.

    The incident these scenarios guard against silently mints a FRESH clean
    checkout at a drifted path, so the rejected file is absent there. Presence in
    the reused worktree is the surviving invariant — regardless of whether it is
    still an uncommitted working-tree diff, or was committed onto the run branch
    by the ADR 0119 ``worktree_branch`` delivery that a full mock resume now runs
    at the end (the file stays on disk either way; only ``git status`` clears
    once it is committed).
    """
    return (cwd / _DIFF_MARKER).exists()


def _active_review_payload(*, round_n: int = 1) -> dict:
    handoff_id = f"review_changes:repair_round:{round_n}"
    return {
        "id": handoff_id,
        "phase": "review_changes",
        "type": "human_feedback_on_reject",
        "trigger": "rejected",
        "verdict": "REJECTED",
        "approved": False,
        "round_extras_key": "repair_round",
        "round": round_n,
        "loop_max_rounds": 1,
        "available_actions": ["continue", "retry_feedback", "halt"],
        "artifacts": {},
        "last_output": "Mock review: blocking issue in the change.",
    }


def _seed_paused_run(
    run_dir: Path, *, project: Path, worktree_block: dict, payload: dict,
) -> None:
    """Write the paused meta.json the original run left behind on the drifted
    resume run dir."""
    run_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": run_dir.name,
        "task": "harden the retry path",
        "project": str(project),
        "profile": "feature",
        "status": "awaiting_phase_handoff",
        "phases": {"implement": [{"ok": True}]},
        "worktree": worktree_block,
        "phase_handoff": payload,
    }
    (run_dir / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8",
    )


def _resume_request(
    *, project: Path, run_dir: Path, provider: MockAgentProvider,
) -> ProjectRunRequest:
    return ProjectRunRequest(
        task="harden the retry path",
        project_dir=str(project),
        output_dir=run_dir,
        profile_name="feature",
        provider=provider,
        presentation=PresentationPolicy.SILENT,
        no_interactive=True,
        resume_from=run_dir.name,
    )


class _Incident:
    """The durable state the original run left behind, on the drifted run dir."""

    def __init__(
        self, *, project: Path, runs_dir: Path, worktrees_dir: Path,
        retained_path: Path, worktree_block: dict, resume_run_dir: Path,
    ) -> None:
        self.project = project
        self.runs_dir = runs_dir
        self.worktrees_dir = worktrees_dir
        self.retained_path = retained_path
        self.worktree_block = worktree_block
        self.resume_run_dir = resume_run_dir


def _build_incident(
    tmp_path: Path, *, round_n: int = 1, record_decision: bool = True,
    with_diff: bool = True,
) -> _Incident:
    """Materialise the incident: a retained isolated worktree carrying the
    rejected diff, plus a drifted resume run dir whose meta records the active
    ``review_changes`` handoff and (optionally) a recorded ``retry_feedback``
    decision."""
    project = tmp_path / "api"
    _init_repo(project)
    runs_dir = tmp_path / "workspace-orchestrator" / "runspace" / "runs"
    worktrees_dir = runs_dir.parent / "worktrees"

    worktree_block = _make_retained_worktree(
        project, runs_dir, _ORIG_WT_ID, with_diff=with_diff,
    )
    retained_path = Path(worktree_block["path"])
    # Incident invariant: run-dir name != original worktree id.
    assert _RESUME_RUN != _ORIG_WT_ID
    assert retained_path.parent.name == f"wt_{_ORIG_WT_ID}"

    resume_run_dir = runs_dir / _RESUME_RUN
    payload = _active_review_payload(round_n=round_n)
    _seed_paused_run(
        resume_run_dir, project=project, worktree_block=worktree_block,
        payload=payload,
    )
    if record_decision:
        phase_handoff_decide(
            _RESUME_RUN, payload["id"], "retry_feedback",
            feedback="address the blocking issue", runs_dir=runs_dir, cwd=None,
        )
    return _Incident(
        project=project, runs_dir=runs_dir, worktrees_dir=worktrees_dir,
        retained_path=retained_path, worktree_block=worktree_block,
        resume_run_dir=resume_run_dir,
    )


def test_scenario1_resume_reuses_retained_worktree_incident_drift(
    tmp_path: Path, monkeypatch,
) -> None:
    """(1) Resume after review reject + retry_feedback reuses the retained
    worktree even though the run-dir name drifts from the worktree id."""
    inc = _build_incident(tmp_path)

    monkeypatch.setenv("ORCHO_RUN_ID", _RESUME_RUN)
    session = run_project_pipeline(
        _resume_request(
            project=inc.project, run_dir=inc.resume_run_dir,
            provider=MockAgentProvider(latency=0.0),
        ),
    ).session

    # Repair cwd is the retained worktree, not a fresh wt_<resume_run>.
    assert Path(session["worktree"]["path"]) == inc.retained_path
    # No second clean worktree was materialised.
    existing = sorted(p.name for p in inc.worktrees_dir.iterdir() if p.is_dir())
    assert existing == [f"wt_{_ORIG_WT_ID}"]
    assert not (inc.worktrees_dir / f"wt_{_RESUME_RUN}").exists()
    # The original rejected diff still lives in the retained worktree.
    assert _diff_survived(inc.retained_path)
    # The reuse decision is recorded additively for inspectability.
    assert session["worktree"]["resume_continuity"]["path"] == str(inc.retained_path)


def test_prefix_resolver_choice_reproduces_the_incident(tmp_path: Path) -> None:
    """Pre-fix reproduction: scenario (1) fails on code before the fix.

    The resolver call the resume made BEFORE the fix derived the checkout from
    ``wt_<resume_run_id>`` with no retained subject, minting a FRESH, clean
    worktree at a DIFFERENT path with no rejected diff — the silent
    clean-checkout the incident produced. With the fix (passing the retained
    subject as ``resume_prior_worktree``) the resolver attaches to the exact
    retained path. Asserting both directions pins that the fix flips the
    worktree choice, so scenario (1)'s ``path == retained`` assertion would
    fail on pre-fix code.
    """
    inc = _build_incident(tmp_path, record_decision=False)

    # Pre-fix: no retained subject -> fresh wt_<resume_run>, clean, diff lost.
    prefix_ctx = resolve_worktree_for_run(
        run_id=_RESUME_RUN,
        project_dir=inc.project,
        run_dir=inc.resume_run_dir,
        worktree_config={"enabled": True, "isolation": "per_run"},
    )
    assert prefix_ctx.path != inc.retained_path
    assert prefix_ctx.path.parent.name == f"wt_{_RESUME_RUN}"
    assert not _git_status_short(prefix_ctx.path)  # clean — the diff is gone

    # With the fix: the retained subject is reused at the exact recorded path.
    fixed_ctx = resolve_worktree_for_run(
        run_id=_RESUME_RUN,
        project_dir=inc.project,
        run_dir=inc.resume_run_dir,
        worktree_config={"enabled": True, "isolation": "per_run"},
        resume_prior_worktree=inc.worktree_block,
    )
    assert fixed_ctx.path == inc.retained_path
    assert _DIFF_MARKER in _git_status_short(fixed_ctx.path)


class _FallbackOnceProvider(MockAgentProvider):
    """Mock provider whose claude agent rejects the first ``continue_session``
    resume with a missing-session error, then succeeds fresh — driving exactly
    one real ``phase.provider_session_fallback``."""

    def __init__(self) -> None:
        super().__init__(latency=0.0)
        self.fell_back = False

    def _wrap(self, agent):
        real_invoke = agent.invoke
        provider = self

        def invoke(*args, **kwargs):
            if kwargs.get("continue_session") and not provider.fell_back:
                provider.fell_back = True
                raise RuntimeError("no conversation found with session id abc")
            return real_invoke(*args, **kwargs)

        agent.invoke = invoke
        return agent

    def claude(self, model: str, *, effort: str | None = None):
        return self._wrap(super().claude(model, effort=effort))


def test_scenario2_provider_session_fallback_keeps_same_worktree_subject(
    tmp_path: Path, monkeypatch,
) -> None:
    """(2) The provider-session resume misses and falls back to a fresh
    session, but the worktree / diff subject is unchanged."""
    inc = _build_incident(tmp_path)

    # Seed a stale provider session so the repair invoke resumes it (and then
    # misses). The role slot for the repair (CHAIN) agent is ``implement_agent``.
    ckpt = CheckpointStore(inc.resume_run_dir / "checkpoints.db", run_id=_RESUME_RUN)
    ckpt.set_agent_session("implement_agent", "stale-session-id")
    ckpt.close()

    provider = _FallbackOnceProvider()
    monkeypatch.setenv("ORCHO_RUN_ID", _RESUME_RUN)
    session = run_project_pipeline(
        _resume_request(
            project=inc.project, run_dir=inc.resume_run_dir, provider=provider,
        ),
    ).session

    # The fresh-session fallback actually fired...
    assert provider.fell_back is True
    events = [
        json.loads(line)
        for line in (inc.resume_run_dir / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert any(e["kind"] == "phase.provider_session_fallback" for e in events)
    # ...yet the worktree / diff subject is the SAME retained worktree.
    assert Path(session["worktree"]["path"]) == inc.retained_path
    assert not (inc.worktrees_dir / f"wt_{_RESUME_RUN}").exists()
    assert _diff_survived(inc.retained_path)


def test_scenario3_deleted_retained_subject_blocks_repair(
    tmp_path: Path, monkeypatch,
) -> None:
    """(3) Retained subject deleted while a review-retry is active -> repair
    does not start; a recoverable error names the path and the recovery."""
    inc = _build_incident(tmp_path)
    decision_artifact = (
        inc.resume_run_dir / "phase_handoff_decisions"
    )
    assert any(decision_artifact.iterdir())  # decision recorded

    # The operator (or a sweep) removed the retained worktree before resuming.
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(inc.retained_path)],
        cwd=inc.project, check=True,
    )
    assert not inc.retained_path.exists()

    monkeypatch.setenv("ORCHO_RUN_ID", _RESUME_RUN)
    with pytest.raises(WorktreeConfigError) as exc:
        run_project_pipeline(
            _resume_request(
                project=inc.project, run_dir=inc.resume_run_dir,
                provider=MockAgentProvider(latency=0.0),
            ),
        )
    msg = str(exc.value)
    assert str(inc.retained_path) in msg
    assert "review retry" in msg
    assert "restoring the retained worktree" in msg or "halt the run" in msg

    # No fresh clean checkout was materialised for the resume run.
    assert not (inc.worktrees_dir / f"wt_{_RESUME_RUN}").exists()

    # The run stays DECIDABLE / not torn: status is the decidable
    # awaiting_phase_handoff (never a torn running+handoff), the active handoff
    # payload and the recorded decision both survive, and the retained worktree
    # subject (path / isolation / base_ref) is PRESERVED so a later resume can
    # still name and reuse it after recovery.
    meta = json.loads((inc.resume_run_dir / "meta.json").read_text())
    assert meta["status"] == "awaiting_phase_handoff"
    assert meta["phase_handoff"]["id"] == _HANDOFF_ID
    assert meta["worktree"]["path"] == str(inc.retained_path)
    assert meta["worktree"]["isolation"] == inc.worktree_block["isolation"]
    assert meta["worktree"]["base_ref"] == inc.worktree_block["base_ref"]
    assert any(decision_artifact.iterdir())

    # The decidable shape is visible to the SDK surfaces MCP relays.
    snap = load_run_snapshot(_RESUME_RUN, runs_dir=inc.runs_dir, cwd=None)
    assert snap.pending_action is not None
    assert snap.pending_action.handoff_id == _HANDOFF_ID

    # Recovery: the operator restores the retained worktree + its rejected diff,
    # then re-resumes. The preserved meta.worktree subject lets the resume find
    # and reuse it; repair now runs against the retained diff, no fresh wt_.
    subprocess.run(
        ["git", "worktree", "add", "--force", str(inc.retained_path),
         inc.worktree_block["branch_ref"]],
        cwd=inc.project, check=True,
    )
    (inc.retained_path / _DIFF_MARKER).write_text(
        "the rejected change under review\n", encoding="utf-8",
    )
    session = run_project_pipeline(
        _resume_request(
            project=inc.project, run_dir=inc.resume_run_dir,
            provider=MockAgentProvider(latency=0.0),
        ),
    ).session
    assert Path(session["worktree"]["path"]) == inc.retained_path
    assert not (inc.worktrees_dir / f"wt_{_RESUME_RUN}").exists()
    assert _diff_survived(inc.retained_path)


def test_scenario3b_clean_head_guard_persists_decidable_state(
    tmp_path: Path, monkeypatch,
) -> None:
    """(3b) Retained worktree is registered but CLEAN at the recorded base —
    the rejected diff vanished. The clean-HEAD guard aborts before the write
    phase, and the persisted run-state stays decidable (not torn): status,
    active handoff, and the full retained worktree subject survive, so a resume
    after the operator restores the diff reuses the same worktree."""
    # Worktree present + registered, but no rejected diff -> clean at base.
    inc = _build_incident(tmp_path, with_diff=False)

    monkeypatch.setenv("ORCHO_RUN_ID", _RESUME_RUN)
    with pytest.raises(RepairSubjectUnproven) as exc:
        run_project_pipeline(
            _resume_request(
                project=inc.project, run_dir=inc.resume_run_dir,
                provider=MockAgentProvider(latency=0.0),
            ),
        )
    # Recoverable clean-HEAD error, before any write-phase dispatch.
    assert "Cannot run repair_changes against clean HEAD" in str(exc.value)
    # The retained worktree was the resolved subject (reused, not re-minted).
    assert not (inc.worktrees_dir / f"wt_{_RESUME_RUN}").exists()

    # Persisted run-state is DECIDABLE / not torn: awaiting_phase_handoff with
    # the active handoff payload and the full retained worktree subject intact.
    meta = json.loads((inc.resume_run_dir / "meta.json").read_text())
    assert meta["status"] == "awaiting_phase_handoff"
    assert meta["phase_handoff"]["id"] == _HANDOFF_ID
    assert meta["worktree"]["path"] == str(inc.retained_path)
    assert meta["worktree"]["isolation"] == inc.worktree_block["isolation"]
    assert meta["worktree"]["base_ref"] == inc.worktree_block["base_ref"]

    # The pending handoff id stays visible to the SDK surfaces MCP relays.
    snap = load_run_snapshot(_RESUME_RUN, runs_dir=inc.runs_dir, cwd=None)
    assert snap.pending_action is not None
    assert snap.pending_action.handoff_id == _HANDOFF_ID

    # Recovery: the operator restores the rejected diff into the retained
    # worktree, then re-resumes. Repair now finds the subject and runs in the
    # same retained worktree; no fresh wt_<resume_run> is minted.
    (inc.retained_path / _DIFF_MARKER).write_text(
        "the restored rejected change\n", encoding="utf-8",
    )
    session = run_project_pipeline(
        _resume_request(
            project=inc.project, run_dir=inc.resume_run_dir,
            provider=MockAgentProvider(latency=0.0),
        ),
    ).session
    assert Path(session["worktree"]["path"]) == inc.retained_path
    assert not (inc.worktrees_dir / f"wt_{_RESUME_RUN}").exists()
    assert _diff_survived(inc.retained_path)


def test_scenario4_round_progression_snapshot_and_idempotent_decide(
    tmp_path: Path,
) -> None:
    """(4) Two reject rounds: the snapshot shows the CURRENT (round:2) id and
    a per-id decide is idempotent; an old round:1 decision does not block it."""
    inc = _build_incident(tmp_path, round_n=2, record_decision=False)
    runs_dir = inc.runs_dir
    id1 = "review_changes:repair_round:1"
    id2 = "review_changes:repair_round:2"

    # A decision for the PRIOR round is already on disk (drop it in directly,
    # mirroring a completed round:1 that progressed the handoff to round:2).
    decisions = inc.resume_run_dir / "phase_handoff_decisions"
    decisions.mkdir(parents=True, exist_ok=True)
    from sdk.phase_handoff import safe_handoff_id
    (decisions / f"{safe_handoff_id(id1)}.json").write_text(
        json.dumps({
            "run_id": _RESUME_RUN, "handoff_id": id1, "phase": "review_changes",
            "action": "retry_feedback", "feedback": "round one",
            "note": None, "decided_at": "2026-06-12T00:00:00+00:00",
        }),
        encoding="utf-8",
    )

    # The snapshot (the surface MCP relays) shows the CURRENT round id.
    snap = load_run_snapshot(_RESUME_RUN, runs_dir=runs_dir, cwd=None)
    assert snap.pending_action is not None
    assert snap.pending_action.handoff_id == id2

    # Deciding the current round is not blocked by the round:1 decision.
    first = phase_handoff_decide(
        _RESUME_RUN, id2, "retry_feedback", feedback="round two",
        runs_dir=runs_dir, cwd=None,
    )
    assert first.handoff_id == id2
    # Exact-payload idempotency per id.
    again = phase_handoff_decide(
        _RESUME_RUN, id2, "retry_feedback", feedback="round two",
        runs_dir=runs_dir, cwd=None,
    )
    assert again.decided_at == first.decided_at
    # A divergent payload for the same id is a conflict.
    with pytest.raises(InvalidPhaseHandoffState, match="already decided"):
        phase_handoff_decide(
            _RESUME_RUN, id2, "retry_feedback", feedback="different",
            runs_dir=runs_dir, cwd=None,
        )
