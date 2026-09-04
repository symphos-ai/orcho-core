# SPDX-License-Identifier: Apache-2.0
"""Branch + parity coverage for the core recovery-lineage read-model (ADR 0114).

Exercises :func:`sdk.run_control.recovery_lineage.recovery_lineage` over the same
filesystem-light synthetic ``meta.json`` fixtures as the diagnosis test, one
scenario per ladder branch of MCP's ``project_recovery_lineage``:

    (1) active_child → active_child_run / resume_active_child
    (2) gate_pending → delivery_gate / delivery_decision
    (3a) terminal + resumable source → source_run_checkpoint / resume_source_run
    (3b) terminal plan-only → plan_artifact / plan_artifact_continuation
    (3c) clean terminal-success → none / start_followup
    (3d) terminal + source not resumable in place but plan-launchable →
         plan_artifact / plan_artifact_continuation with the SOURCE as the
         recommended ``from_run_plan`` parent
    (3e) terminal dead-end → unknown / stop_unknown (+ missing_facts)
    (4/5) non-terminal → none / None *with* source/plan enrichment

Plus the defensive contract (unreadable meta → unknown/stop_unknown, never
raises), the empty-``run_id`` → ``ValueError`` guard, the ``source_meta``
provider seam (an overlay that settles the source as terminal suppresses the
source-run resume), and the single-owner contract for ``source_resumable``: it
is exactly what the canonical launch preflight would accept as a same-run
checkpoint resume (a finalized scheduled-gate ledger, a paused handoff, or a
live ``running`` source are all refused there, so none may be recommended).

orcho-mcp is NEITHER imported NOR modified: the MCP closed sets are mirrored as
plain literals so the test fails loudly if the core read-model ever drifts from
the wire contract the P1-mcp migration will consume.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from pipeline.control.continuation import ContinuationRequest
from pipeline.control.resume_context import is_terminal_resume_parent
from pipeline.verification_ledger_store import ScheduledGateLedger, write_ledger
from sdk import RecoveryLineage, recovery_lineage
from sdk.run_control import (
    RecoveryLineage as RecoveryLineage_rc,
    recovery_lineage as recovery_lineage_rc,
)
from sdk.run_control.continuation import preflight_continuation

pytestmark = [pytest.mark.sdk, pytest.mark.filesystem_light]

# The public ``recovery_lineage`` function shadows the same-named submodule in
# the ``sdk.run_control`` package namespace; reach the module (for its closed
# SUBJECT_* / ACTION_* / _MISSING_* vocabulary) through importlib, not attribute
# access on the package.
rlm = importlib.import_module("sdk.run_control.recovery_lineage")


# ── fixture helpers ───────────────────────────────────────────────────────────


def _mk(
    runs_dir: Path, run_id: str, meta: dict, files: dict[str, str] | None = None,
) -> str:
    """Write a synthetic run dir (``meta.json`` + optional artifacts)."""
    d = runs_dir / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    for name, content in (files or {}).items():
        (d / name).write_text(content, encoding="utf-8")
    return run_id


def _lineage(runs_dir: Path, run_id: str, **kw) -> RecoveryLineage:
    """Resolve ``run_id`` from an explicit runs_dir (no ambient walk-up)."""
    return recovery_lineage(run_id, runs_dir=runs_dir, cwd=None, **kw)


def _finalize_ledger(runs_dir: Path, run_id: str) -> None:
    """Write the finalized ``scheduled_gate_ledger.json`` finalization leaves.

    Uses the store API (not a hand-written body) so the artifact is exactly
    what ``pipeline/project/finalization.py`` persists at every runner-side
    ``run.end`` — the durable fact that blocks a same-run resume in preflight.
    """
    write_ledger(runs_dir / run_id, ScheduledGateLedger(rows=(), finalized=True))


_FAILED_SOURCE_WITH_PLAN = {
    "status": "failed", "plan_source": "local", "profile": "feature",
    "worktree": {"isolation": "per_run", "path": "/tmp/wt-parent"},
    "project": "/x",
}
_TERMINAL_CHILD = {
    "status": "halted", "halt_reason": "final_acceptance_rejected",
    "parent_run_id": "parent", "project": "/x",
}


# ── export wiring ─────────────────────────────────────────────────────────────


def test_symbols_exported_from_both_surfaces() -> None:
    # The public function/type resolve the same object from sdk and sdk.run_control.
    assert recovery_lineage is recovery_lineage_rc
    assert RecoveryLineage is RecoveryLineage_rc
    assert callable(recovery_lineage)


# ── per-branch coverage ───────────────────────────────────────────────────────


def test_active_child(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _mk(runs, "20260101_000001", {
        "status": "halted", "halt_reason": "final_acceptance_rejected",
        "project": "/x",
    })
    _mk(runs, "20260101_000002", {
        "status": "awaiting_phase_handoff", "resume_mode": "followup",
        "parent_run_id": "20260101_000001",
        "phase_handoff": {"id": "hc", "available_actions": ["continue"]},
        "project": "/x",
    })
    rl = _lineage(runs, "20260101_000001")
    assert rl.continuation_subject == rlm.SUBJECT_ACTIVE_CHILD_RUN
    assert rl.recommended_next_action == rlm.ACTION_RESUME_ACTIVE_CHILD
    assert rl.recommended_run_id == "20260101_000002"
    assert rl.active_child_run_id == "20260101_000002"


def test_gate_pending(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _mk(runs, "r", {
        "status": "awaiting_commit_decision",
        "commit_delivery": {
            "status": "pending", "run_id": "r",
            "project_path": "/x", "source_path": "/x", "baseline_ref": "HEAD",
        },
        "project": "/x",
    })
    rl = _lineage(runs, "r")
    assert rl.continuation_subject == rlm.SUBJECT_DELIVERY_GATE
    assert rl.recommended_next_action == rlm.ACTION_DELIVERY_DECISION
    assert rl.recommended_run_id == "r"


def test_terminal_resumable_source(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    # Resumable source: a failed (non-terminal) parent that kept its worktree.
    _mk(runs, "parent", {
        "status": "failed",
        "worktree": {"isolation": "per_run", "path": "/tmp/wt-parent"},
        "project": "/x",
    })
    _mk(runs, "child", {
        "status": "halted", "halt_reason": "final_acceptance_rejected",
        "parent_run_id": "parent", "project": "/x",
    })
    rl = _lineage(runs, "child")
    assert rl.is_terminal_or_rejected is True
    assert rl.continuation_subject == rlm.SUBJECT_SOURCE_RUN_CHECKPOINT
    assert rl.recommended_next_action == rlm.ACTION_RESUME_SOURCE_RUN
    assert rl.recommended_run_id == "parent"
    assert rl.source_run_id == "parent"
    assert rl.source_status == "failed"
    assert rl.source_resumable is True
    assert rl.source_worktree_preserved is True


def test_terminal_plan_only(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _mk(
        runs, "r",
        {
            "status": "halted", "halt_reason": "final_acceptance_rejected",
            "plan_source": "local", "profile": "planning", "project": "/x",
        },
        files={"parsed_plan.json": "{}"},
    )
    rl = _lineage(runs, "r")
    assert rl.is_terminal_or_rejected is True
    assert rl.continuation_subject == rlm.SUBJECT_PLAN_ARTIFACT
    assert rl.recommended_next_action == rlm.ACTION_PLAN_ARTIFACT_CONTINUATION
    assert rl.recommended_run_id == "r"
    assert rl.plan_subject_available is True


def test_clean_terminal_success(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _mk(runs, "r", {"status": "done", "project": "/x"})
    rl = _lineage(runs, "r")
    assert rl.is_terminal_or_rejected is True
    assert rl.continuation_subject == rlm.SUBJECT_NONE
    assert rl.recommended_next_action == rlm.ACTION_START_FOLLOWUP
    assert rl.plan_subject_available is False
    assert rl.missing_facts == ()


def test_terminal_deadend_unknown(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _mk(runs, "r", {
        "status": "halted", "halt_reason": "final_acceptance_rejected",
        "project": "/x",
    })
    rl = _lineage(runs, "r")
    assert rl.is_terminal_or_rejected is True
    assert rl.continuation_subject == rlm.SUBJECT_UNKNOWN
    assert rl.recommended_next_action == rlm.ACTION_STOP_UNKNOWN
    # No source, no plan, no gate, no child → all four facts enumerated.
    assert rl.missing_facts == (
        rlm._MISSING_SOURCE, rlm._MISSING_PLAN, rlm._MISSING_GATE,
        rlm._MISSING_CHILD,
    )


def test_non_terminal_enriched(tmp_path: Path) -> None:
    # A non-terminal stop continues itself (subject none / no action), but the
    # source facts are still enriched (MCP branch 4/5) — never a bare ``none``.
    runs = tmp_path / "runs"
    _mk(runs, "parent", {
        "status": "failed",
        "worktree": {"isolation": "per_run", "path": "/tmp/wt-parent"},
        "project": "/x",
    })
    _mk(runs, "r", {
        "status": "failed", "parent_run_id": "parent", "project": "/x",
    })
    rl = _lineage(runs, "r")
    assert rl.is_terminal_or_rejected is False
    assert rl.continuation_subject == rlm.SUBJECT_NONE
    assert rl.recommended_next_action is None
    # Enrichment: the source facts are resolved even on the non-terminal path.
    assert rl.source_run_id == "parent"
    assert rl.source_status == "failed"
    assert rl.source_resumable is True
    assert rl.source_worktree_preserved is True


# ── defensive + guard contract ────────────────────────────────────────────────


def test_corrupt_meta_degrades_to_unknown(tmp_path: Path) -> None:
    # A corrupt inspected ``meta.json`` is a genuine read failure: the recovery
    # read-model reads it strictly (not via the tolerant ``load_meta`` → ``{}``),
    # so it degrades to the unknown/stop_unknown dead-end with all four missing
    # facts and a fact-built reason — never a bare non-terminal ``none``. It must
    # still not raise.
    runs = tmp_path / "runs"
    d = runs / "r"
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text("{ this is not json", encoding="utf-8")
    rl = _lineage(runs, "r")
    assert rl.continuation_subject == rlm.SUBJECT_UNKNOWN
    assert rl.recommended_next_action == rlm.ACTION_STOP_UNKNOWN
    assert rl.is_terminal_or_rejected is False
    assert rl.missing_facts == (
        rlm._MISSING_SOURCE, rlm._MISSING_PLAN, rlm._MISSING_GATE,
        rlm._MISSING_CHILD,
    )
    assert rl.reason.startswith("could not read run meta:")


def test_missing_meta_degrades_to_unknown(tmp_path: Path) -> None:
    # A run directory with no ``meta.json`` at all is likewise an inspected-meta
    # read failure → unknown/stop_unknown, not an empty non-terminal run.
    runs = tmp_path / "runs"
    d = runs / "r"
    d.mkdir(parents=True, exist_ok=True)
    rl = _lineage(runs, "r")
    assert rl.continuation_subject == rlm.SUBJECT_UNKNOWN
    assert rl.recommended_next_action == rlm.ACTION_STOP_UNKNOWN
    assert rl.is_terminal_or_rejected is False
    assert rl.missing_facts == (
        rlm._MISSING_SOURCE, rlm._MISSING_PLAN, rlm._MISSING_GATE,
        rlm._MISSING_CHILD,
    )
    assert rl.reason.startswith("could not read run meta:")


def test_unreadable_run_degrades_to_unknown(tmp_path: Path) -> None:
    # A run that ``find_run`` cannot resolve is a genuine read failure: the outer
    # guard degrades it to the unknown/stop_unknown dead-end with a fact-built
    # reason, rather than propagating the lookup error (unlike ``run_diagnosis``).
    runs = tmp_path / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    rl = _lineage(runs, "does_not_exist")
    assert rl.continuation_subject == rlm.SUBJECT_UNKNOWN
    assert rl.recommended_next_action == rlm.ACTION_STOP_UNKNOWN
    assert rl.is_terminal_or_rejected is False
    assert rl.missing_facts == (
        rlm._MISSING_SOURCE, rlm._MISSING_PLAN, rlm._MISSING_GATE,
        rlm._MISSING_CHILD,
    )
    assert rl.reason.startswith("could not read run meta:")


def test_empty_run_id_raises() -> None:
    with pytest.raises(ValueError):
        recovery_lineage("", runs_dir=Path("/nowhere"), cwd=None)
    with pytest.raises(ValueError):
        recovery_lineage(None, runs_dir=Path("/nowhere"), cwd=None)  # type: ignore[arg-type]


def test_source_meta_seam_suppresses_blind_resume(tmp_path: Path) -> None:
    # The source's on-disk meta is stale (``failed`` + retained worktree → it
    # reads as a resumable checkpoint), but the embedder's supervisor-merge has
    # settled it to a clean terminal ``done``. Feeding the resolved meta via
    # ``source_meta`` must suppress the blind source-run resume — the terminal
    # child becomes an unknown dead-end rather than a redirect.
    runs = tmp_path / "runs"
    _mk(runs, "parent", {
        "status": "failed",
        "worktree": {"isolation": "per_run", "path": "/tmp/wt-parent"},
        "project": "/x",
    })
    _mk(runs, "child", {
        "status": "halted", "halt_reason": "final_acceptance_rejected",
        "parent_run_id": "parent", "project": "/x",
    })

    # Without injection: the stale on-disk source reads as a resumable checkpoint.
    stale = _lineage(runs, "child")
    assert stale.continuation_subject == rlm.SUBJECT_SOURCE_RUN_CHECKPOINT
    assert stale.recommended_run_id == "parent"

    # With the provider-resolved source overlay: the source is terminal-done, so
    # it is no longer a resumable checkpoint → the child stops as unknown.
    resolved = recovery_lineage(
        "child", runs_dir=runs, cwd=None,
        source_meta={"parent": {"status": "done", "project": "/x"}},
    )
    assert resolved.continuation_subject == rlm.SUBJECT_UNKNOWN
    assert resolved.recommended_next_action == rlm.ACTION_STOP_UNKNOWN
    assert rlm._MISSING_SOURCE in resolved.missing_facts


def test_inspected_meta_seam_drives_delivery_gate(tmp_path: Path) -> None:
    # The inspected run's on-disk meta carries NO delivery gate (it would read as
    # a terminal dead-end → unknown). The embedder feeds a supervisor-merged meta
    # with a pending ``commit_delivery`` gate via ``meta=``. The gate must be
    # classified from the passed meta — not re-read from the stale on-disk file —
    # so recovery picks the delivery_gate/delivery_decision branch.
    runs = tmp_path / "runs"
    _mk(runs, "r", {
        "status": "halted", "halt_reason": "final_acceptance_rejected",
        "project": "/x",
    })

    # On disk: a terminal dead-end with no gate → unknown/stop_unknown.
    on_disk = _lineage(runs, "r")
    assert on_disk.continuation_subject == rlm.SUBJECT_UNKNOWN

    # With the provider-merged meta carrying a pending gate → delivery_gate.
    merged = {
        "status": "awaiting_commit_decision",
        "commit_delivery": {
            "status": "pending", "run_id": "r",
            "project_path": "/x", "source_path": "/x", "baseline_ref": "HEAD",
        },
        "project": "/x",
    }
    seamed = recovery_lineage("r", runs_dir=runs, cwd=None, meta=merged)
    assert seamed.continuation_subject == rlm.SUBJECT_DELIVERY_GATE
    assert seamed.recommended_next_action == rlm.ACTION_DELIVERY_DECISION
    assert seamed.recommended_run_id == "r"


# ── source resumability is owned by the launch preflight ─────────────────────


def test_finalized_source_ledger_recommends_from_run_plan(tmp_path: Path) -> None:
    # Field shape: the child dead-ended (phase_handoff_halt / rejected) and its
    # source is a non-terminal ``failed``/``halted`` stop with a retained
    # worktree AND a persisted plan — but the source's scheduled-gate ledger was
    # finalized at run.end, so the canonical preflight refuses a same-run
    # resume. Recommending "resume the source" would be a dead recommendation;
    # the durable exit is a NEW run from the source's plan artifact.
    runs = tmp_path / "runs"
    _mk(runs, "parent", _FAILED_SOURCE_WITH_PLAN, files={"parsed_plan.json": "{}"})
    _finalize_ledger(runs, "parent")
    _mk(runs, "child", _TERMINAL_CHILD)
    rl = _lineage(runs, "child")
    assert rl.is_terminal_or_rejected is True
    assert rl.continuation_subject == rlm.SUBJECT_PLAN_ARTIFACT
    assert rl.recommended_next_action == rlm.ACTION_PLAN_ARTIFACT_CONTINUATION
    assert rl.recommended_run_id == "parent"
    assert rl.source_run_id == "parent"
    assert rl.source_status == "failed"
    assert rl.source_resumable is False
    # Reported facts stay reported; they no longer decide resumability.
    assert rl.source_worktree_preserved is True
    assert rl.plan_subject_available is False
    assert "finalized scheduled-gate ledger" in rl.reason
    assert "from_run_plan=parent" in rl.reason
    assert rl.missing_facts == ()


def test_finalized_source_ledger_without_plan_is_unknown(tmp_path: Path) -> None:
    # Same finalized source, but no persisted plan artifact: neither via-source
    # operation passes preflight, so the child stops as an explicit unknown
    # (missing the resumable source) rather than being pointed at a dead resume.
    runs = tmp_path / "runs"
    _mk(runs, "parent", _FAILED_SOURCE_WITH_PLAN)
    _finalize_ledger(runs, "parent")
    _mk(runs, "child", _TERMINAL_CHILD)
    rl = _lineage(runs, "child")
    assert rl.continuation_subject == rlm.SUBJECT_UNKNOWN
    assert rl.recommended_next_action == rlm.ACTION_STOP_UNKNOWN
    assert rl.recommended_run_id is None
    assert rl.source_run_id == "parent"
    assert rl.source_resumable is False
    assert rlm._MISSING_SOURCE in rl.missing_facts


def test_paused_source_is_not_a_resume_target(tmp_path: Path) -> None:
    # A source parked at ``awaiting_phase_handoff`` with no recorded decision is
    # refused by preflight ("same-run resume is only available for a
    # checkpoint-resumable state"); with its plan persisted the child is routed
    # to a from_run_plan launch off the source, never to a resume of it.
    runs = tmp_path / "runs"
    _mk(
        runs, "parent",
        {
            "status": "awaiting_phase_handoff", "plan_source": "local",
            "profile": "planning", "project": "/x",
            "phase_handoff": {"id": "h1", "available_actions": ["continue"]},
            "worktree": {"isolation": "per_run", "path": "/tmp/wt-parent"},
        },
        files={"parsed_plan.json": "{}"},
    )
    # The child is a non-success terminal dead-end (operator halt at delivery).
    _mk(runs, "child", {
        "status": "halted", "halt_reason": "commit_decision_halt",
        "plan_source": "run", "plan_source_run_id": "parent", "project": "/x",
    })
    rl = _lineage(runs, "child")
    assert rl.source_resumable is False
    assert rl.continuation_subject == rlm.SUBJECT_PLAN_ARTIFACT
    assert rl.recommended_next_action == rlm.ACTION_PLAN_ARTIFACT_CONTINUATION
    assert rl.recommended_run_id == "parent"


def test_running_source_is_not_a_resume_target(tmp_path: Path) -> None:
    # A live ``running`` source is not checkpoint-resumable (preflight refuses
    # it), so a terminal child must never be told to resume it; with no plan
    # artifact the child stops as unknown.
    runs = tmp_path / "runs"
    _mk(runs, "parent", {
        "status": "running",
        "worktree": {"isolation": "per_run", "path": "/tmp/wt-parent"},
        "project": "/x",
    })
    _mk(runs, "child", _TERMINAL_CHILD)
    rl = _lineage(runs, "child")
    assert rl.source_run_id == "parent"
    assert rl.source_resumable is False
    assert rl.continuation_subject == rlm.SUBJECT_UNKNOWN
    assert rl.recommended_next_action == rlm.ACTION_STOP_UNKNOWN


def test_clean_success_child_keeps_start_followup_over_source_plan(
    tmp_path: Path,
) -> None:
    # A child that completed cleanly is not a recovery case: even though its
    # source cannot be resumed and holds a launchable plan, re-implementing that
    # plan is wrong advice — the clean terminal-success branch (start a fresh
    # follow-up) keeps priority over the via-source plan launch.
    runs = tmp_path / "runs"
    _mk(runs, "parent", _FAILED_SOURCE_WITH_PLAN, files={"parsed_plan.json": "{}"})
    _finalize_ledger(runs, "parent")
    _mk(runs, "child", {
        "status": "done", "plan_source": "run", "plan_source_run_id": "parent",
        "project": "/x",
    })
    rl = _lineage(runs, "child")
    assert rl.continuation_subject == rlm.SUBJECT_NONE
    assert rl.recommended_next_action == rlm.ACTION_START_FOLLOWUP
    assert rl.source_run_id == "parent"
    assert rl.source_resumable is False


@pytest.mark.parametrize(
    ("source_meta", "finalized"),
    [
        ({"status": "failed", "project": "/x"}, False),
        ({"status": "failed", "project": "/x"}, True),
        ({"status": "halted", "halt_reason": "x", "project": "/x"}, False),
        ({"status": "interrupted", "project": "/x"}, False),
        ({"status": "running", "project": "/x"}, False),
        ({"status": "done", "project": "/x"}, False),
        ({"status": "halted", "halt_reason": "phase_handoff_halt", "project": "/x"}, False),
        (
            {
                "status": "awaiting_phase_handoff", "project": "/x",
                "phase_handoff": {"id": "h1", "available_actions": ["continue"]},
            },
            False,
        ),
    ],
)
def test_source_resumable_matches_launch_preflight(
    tmp_path: Path, source_meta: dict, finalized: bool,
) -> None:
    # DRIFT INVARIANT (single owner): ``source_resumable`` is exactly "the
    # canonical launch preflight selects resume_checkpoint for the source AND the
    # source is not a terminal resume parent" — the two gates every resume
    # surface applies before spawning. Re-deriving it from meta facts alone is
    # what produced the dead "resume the source" recommendation.
    runs = tmp_path / "runs"
    _mk(runs, "parent", {**source_meta, "worktree": {"isolation": "per_run", "path": "/tmp/wt-parent"}})
    if finalized:
        _finalize_ledger(runs, "parent")
    _mk(runs, "child", _TERMINAL_CHILD)
    rl = _lineage(runs, "child")
    preflight = preflight_continuation(
        ContinuationRequest(run_id="parent", intent="resume"),
        parent_run_dir=runs / "parent",
    )
    persisted_source = json.loads((runs / "parent" / "meta.json").read_text())
    expected = (
        preflight.resolution.operation == "resume_checkpoint"
        and not is_terminal_resume_parent(persisted_source)
    )
    assert rl.source_resumable is expected, (source_meta, finalized, preflight.resolution)
    if rl.source_resumable:
        assert rl.recommended_next_action == rlm.ACTION_RESUME_SOURCE_RUN
    else:
        assert rl.recommended_next_action != rlm.ACTION_RESUME_SOURCE_RUN


# ── field-for-field parity table (living documentation; MCP not imported) ──────

# Mirror of MCP ``project_recovery_lineage`` → ``RecoveryLineageProjection``
# (orcho_mcp.services.run_lineage). Every field name + the closed
# continuation_subject / recommended_next_action vocabularies are pinned so a
# drift in the core read-model fails loudly without importing orcho-mcp.
_MCP_PROJECTION_FIELDS = frozenset({
    "run_id",
    "is_terminal_or_rejected",
    "continuation_subject",
    "recommended_next_action",
    "recommended_run_id",
    "source_run_id",
    "source_status",
    "source_resumable",
    "source_worktree_preserved",
    "plan_subject_available",
    "active_child_run_id",
    "missing_facts",
    "reason",
})

_MCP_CONTINUATION_SUBJECTS = frozenset({
    "active_child_run",
    "delivery_gate",
    "source_run_checkpoint",
    "plan_artifact",
    "none",
    "unknown",
})

_MCP_NEXT_ACTIONS = frozenset({
    "resume_active_child",
    "delivery_decision",
    "resume_source_run",
    "plan_artifact_continuation",
    "start_followup",
    "stop_unknown",
})


def test_field_parity_with_mcp_projection() -> None:
    import dataclasses

    names = {f.name for f in dataclasses.fields(RecoveryLineage)}
    assert names == _MCP_PROJECTION_FIELDS
    # The closed vocabularies the core module declares match MCP's exactly.
    assert {
        rlm.SUBJECT_ACTIVE_CHILD_RUN, rlm.SUBJECT_DELIVERY_GATE,
        rlm.SUBJECT_SOURCE_RUN_CHECKPOINT, rlm.SUBJECT_PLAN_ARTIFACT,
        rlm.SUBJECT_NONE, rlm.SUBJECT_UNKNOWN,
    } == _MCP_CONTINUATION_SUBJECTS
    assert {
        rlm.ACTION_RESUME_ACTIVE_CHILD, rlm.ACTION_DELIVERY_DECISION,
        rlm.ACTION_RESUME_SOURCE_RUN, rlm.ACTION_PLAN_ARTIFACT_CONTINUATION,
        rlm.ACTION_START_FOLLOWUP, rlm.ACTION_STOP_UNKNOWN,
    } == _MCP_NEXT_ACTIONS


def test_missing_facts_is_tuple(tmp_path: Path) -> None:
    # frozen+slots convention: missing_facts is a tuple, not a list (the lone
    # shape difference from MCP's list[str], same values/order).
    runs = tmp_path / "runs"
    _mk(runs, "r", {
        "status": "halted", "halt_reason": "final_acceptance_rejected",
        "project": "/x",
    })
    rl = _lineage(runs, "r")
    assert isinstance(rl.missing_facts, tuple)


def test_reclaimed_worktree_is_not_projected_as_preserved_source(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _mk(runs, "r", {
        "status": "halted", "halt_reason": "commit_decision_fix", "project": "/x",
        "worktree": {"path": "/historical", "isolation": "per_run", "reclaimed": {"at": "2026-07-29T00:00:00Z"}},
    })
    lineage = _lineage(runs, "r")
    assert lineage.source_worktree_preserved is False
    assert lineage.recommended_run_id is None
    assert "historical" in lineage.reason
