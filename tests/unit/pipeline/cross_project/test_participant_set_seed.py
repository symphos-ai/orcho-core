# SPDX-License-Identifier: Apache-2.0
"""T3 — cross seeds a provisional ParticipantSet and binds editable_checkout
post-dispatch from the child's real isolated worktree (ADR 0112 §1, F1 seam).

``setup_cross_run`` seeds one PROVISIONAL participant per alias (editable_checkout
unbound). ``run_project_dispatch`` binds each alias's editable_checkout to the
child's ACTUAL ``session['worktree']['path']`` after the child returns — the
parent set then carries the real isolated paths, and no parent worktree is
created (the bind only reads the child's own worktree).
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agents.runtimes import MockAgentProvider
from pipeline.cross_project import project_dispatch
from pipeline.cross_project.project_dispatch import (
    DispatchPorts,
    ProjectDispatchContext,
    run_project_dispatch,
)
from pipeline.cross_project.run_setup import setup_cross_run
from pipeline.engine.worktree_source import resolve_isolated_repo_source
from pipeline.participants import ParticipantSet


def _profile_setup() -> SimpleNamespace:
    return SimpleNamespace(
        requested_profile=SimpleNamespace(name="feature"),
        projected_profile_name="feature#project",
    )


def _noop_ports() -> DispatchPorts:
    return DispatchPorts(
        banner=lambda *a, **k: None,
        success=lambda *a, **k: None,
        warn=lambda *a, **k: None,
    )


def test_setup_cross_run_seeds_provisional_per_alias(tmp_path: Path) -> None:
    core = tmp_path / "orcho-core"
    mcp = tmp_path / "orcho-mcp"
    core.mkdir()
    mcp.mkdir()
    run_dir = tmp_path / "runs" / "20260629_010101"

    setup = setup_cross_run(
        task="cross task",
        projects={"core": core, "mcp": mcp},
        model="fake-model",
        output_dir=run_dir,
        cross_mode="full",
        resume_from=None,
        resume_mode=None,
        followup_parent_run_id=None,
        followup_parent_run_dir=None,
        followup_parent_status=None,
        followup_base_task=None,
        resumed_meta=None,
        profile_setup=_profile_setup(),
        terminal=False,
    )

    pset = setup.participant_set
    assert isinstance(pset, ParticipantSet)
    assert len(pset) == 2
    for alias, path in (("core", core), ("mcp", mcp)):
        participant = pset.get(alias)
        assert participant is not None
        assert participant.repo == str(path)
        assert participant.delivery_target == str(path)
        # Provisional: editable_checkout unbound until post-dispatch bind.
        assert participant.editable_checkout == ""
        assert not participant.is_bound

    # The in-memory set is NOT persisted into the session / meta.json.
    assert "participant_set" not in setup.session
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert "participant_set" not in meta


def _dispatch_ctx(
    *, projects: dict[str, Path], run_dir: Path, participant_set: ParticipantSet,
) -> ProjectDispatchContext:
    return ProjectDispatchContext(
        task="cross task",
        projects=projects,
        task_plan=None,
        resume_from=None,
        dry_run=False,
        max_rounds=1,
        code_model="m",
        phase_config=None,
        child_profile=SimpleNamespace(name="feature#project"),
        requested_profile_name="feature",
        has_global_plan=False,
        provider=MockAgentProvider(),
        hypothesis_enabled=False,
        followup_session_seeds_per_alias=None,
        run_dir=run_dir,
        output_dir=None,
        plan_output="",
        plan_review_dict=None,
        cross_ckpt={"sub_status": {}},
        session={"phases": {"projects": {}}},
        cross_phase_usage={},
        ports=_noop_ports(),
        terminal=False,
        participant_set=participant_set,
    )


def test_dispatch_binds_editable_checkout_to_child_worktree(
    tmp_path: Path, monkeypatch,
) -> None:
    canonical = tmp_path / "orcho-core"
    canonical.mkdir()
    child_worktree = tmp_path / "runspace" / "wt_child" / "checkout"
    run_dir = tmp_path / "runs" / "20260629_020202"
    run_dir.mkdir(parents=True)

    pset = ParticipantSet(isolation="per_run")
    pset.add_provisional(
        alias="core", repo=str(canonical), delivery_target=str(canonical),
    )

    # The child's mono isolation_setup already created its own worktree; the cross
    # bind only READS that path (no parent worktree creation).
    child_session = {
        "status": "completed",
        "worktree": {
            "isolation": "per_run",
            "path": str(child_worktree),
            "source_repo_path": str(canonical),
        },
        "phases": {},
    }
    monkeypatch.setattr(
        project_dispatch, "run_project_pipeline",
        lambda request: SimpleNamespace(session=child_session),
    )

    ctx = _dispatch_ctx(
        projects={"core": canonical}, run_dir=run_dir, participant_set=pset,
    )
    result = run_project_dispatch(ctx)
    assert result.paused is False

    bound = pset.get("core")
    # The parent set now carries the child's REAL isolated path, distinct from the
    # canonical sibling under isolation-on.
    assert bound.editable_checkout == str(child_worktree)
    assert bound.editable_checkout != str(canonical)
    src = pset.isolated_source_for("core")
    assert src is not None and src.is_isolated
    assert src.worktree_path == str(child_worktree)
    assert src.source_repo_path == str(canonical)


def test_dispatch_degraded_isolation_off_binds_canonical(
    tmp_path: Path, monkeypatch,
) -> None:
    """Degraded isolation-off child: worktree.path == canonical → editable ==
    delivery_target (degraded contract preserved)."""
    canonical = tmp_path / "orcho-core"
    canonical.mkdir()
    run_dir = tmp_path / "runs" / "20260629_030303"
    run_dir.mkdir(parents=True)

    pset = ParticipantSet(isolation="per_run")
    pset.add_provisional(
        alias="core", repo=str(canonical), delivery_target=str(canonical),
    )

    child_session = {
        "status": "completed",
        "worktree": {
            "isolation": "off",
            "path": str(canonical),
            "source_repo_path": str(canonical),
        },
        "phases": {},
    }
    monkeypatch.setattr(
        project_dispatch, "run_project_pipeline",
        lambda request: SimpleNamespace(session=child_session),
    )

    ctx = _dispatch_ctx(
        projects={"core": canonical}, run_dir=run_dir, participant_set=pset,
    )
    run_project_dispatch(ctx)

    bound = pset.get("core")
    assert bound.editable_checkout == str(canonical) == bound.delivery_target
    # The bind threaded the child's own isolation regime onto the participant, so a
    # degraded-off child stays off even inside the per_run cross set.
    assert bound.isolation == "off"
    # Resolver invariant: a degraded-off participant resolves to NO isolated source
    # (in-place verification), NOT a fail-closed ``worktree == source`` error.
    src = pset.isolated_source_for("core")
    assert src is None
    # resolve_isolated_repo_source keeps the canonical candidate (legal in-place
    # sibling) instead of raising IsolatedSourceError for the degraded participant.
    resolved = resolve_isolated_repo_source(
        repo_name="core", candidate=str(canonical), isolated=src,
    )
    assert resolved == str(canonical)


def test_dispatch_resume_rebinds_done_alias_from_durable_session(
    tmp_path: Path, monkeypatch,
) -> None:
    """Cross resume: an alias that finished in a prior run (``sub_status == 'done'``)
    is NOT re-dispatched, but its participant must still be rebound from the durable
    child session (``worktree['path']``) so the run-scoped set carries the real
    isolated checkout instead of an unbound participant (F1)."""
    canonical = tmp_path / "orcho-core"
    canonical.mkdir()
    child_worktree = tmp_path / "runspace" / "wt_child" / "checkout"
    run_dir = tmp_path / "runs" / "20260629_050505"
    run_dir.mkdir(parents=True)

    # The set is re-seeded PROVISIONAL on resume (as ``setup_cross_run`` does), so the
    # done alias starts unbound.
    pset = ParticipantSet(isolation="per_run")
    pset.add_provisional(
        alias="core", repo=str(canonical), delivery_target=str(canonical),
    )
    assert not pset.get("core").is_bound

    # The done child was never re-dispatched — calling the child here is a hard error.
    def _must_not_run(request):  # pragma: no cover - guarded by the assert below
        raise AssertionError("done alias must not re-dispatch the child")

    monkeypatch.setattr(project_dispatch, "run_project_pipeline", _must_not_run)

    # Durable child session preserved across the resume.
    preserved_child = {
        "status": "completed",
        "worktree": {
            "isolation": "per_run",
            "path": str(child_worktree),
            "source_repo_path": str(canonical),
        },
        "phases": {},
    }
    ctx = _dispatch_ctx(
        projects={"core": canonical}, run_dir=run_dir, participant_set=pset,
    )
    ctx.cross_ckpt = {"sub_status": {"core": "done"}}
    ctx.session = {"phases": {"projects": {"core": preserved_child}}}
    (run_dir / "core").mkdir()
    (run_dir / "core" / "meta.json").write_text(
        json.dumps(preserved_child),
        encoding="utf-8",
    )

    result = run_project_dispatch(ctx)
    assert result.paused is False

    # The done child's session is preserved AND the participant is rebound to its real
    # isolated worktree without re-running the child.
    assert ctx.session["phases"]["projects"]["core"] is preserved_child
    bound = pset.get("core")
    assert bound.is_bound
    assert bound.editable_checkout == str(child_worktree)
    assert bound.editable_checkout != str(canonical)
    src = pset.isolated_source_for("core")
    assert src is not None and src.is_isolated
    assert src.worktree_path == str(child_worktree)
    assert src.source_repo_path == str(canonical)


def test_dispatch_isolated_child_resolver_redirects(
    tmp_path: Path, monkeypatch,
) -> None:
    """An isolated child's bound participant fails closed / redirects through the
    resolver — the per_run cross regime still applies when the child is isolated."""
    canonical = tmp_path / "orcho-core"
    canonical.mkdir()
    child_worktree = tmp_path / "runspace" / "wt_child" / "checkout"
    run_dir = tmp_path / "runs" / "20260629_040404"
    run_dir.mkdir(parents=True)

    pset = ParticipantSet(isolation="per_run")
    pset.add_provisional(
        alias="core", repo=str(canonical), delivery_target=str(canonical),
    )
    child_session = {
        "status": "completed",
        "worktree": {
            "isolation": "per_run",
            "path": str(child_worktree),
            "source_repo_path": str(canonical),
        },
        "phases": {},
    }
    monkeypatch.setattr(
        project_dispatch, "run_project_pipeline",
        lambda request: SimpleNamespace(session=child_session),
    )
    ctx = _dispatch_ctx(
        projects={"core": canonical}, run_dir=run_dir, participant_set=pset,
    )
    run_project_dispatch(ctx)

    src = pset.isolated_source_for("core")
    # The candidate naming the isolated repo is redirected to the child worktree.
    resolved = resolve_isolated_repo_source(
        repo_name="core", candidate=str(canonical), isolated=src,
    )
    assert resolved == str(child_worktree)
