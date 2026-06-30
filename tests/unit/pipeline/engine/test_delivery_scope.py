"""Stage C delivery-scope enforcement (T4) — unit + real-repo tests.

Two layers, mirroring the focused module's split:

* **(a) pure classification** — :func:`assess_delivery_scope` across every
  branch with no I/O;
* **(b–f) multi-repo collection + integration** — real primary + sibling git
  repos registered in a workspace ``.orcho/config.local.json``, exercising
  :func:`collect_sibling_changes`, the ``resolve_commit_delivery`` hook
  (expanded discloses, strict blocks reversibly), the no-scope no-op, and soft
  degradation for an unregistered alias.

Real-repo tests carry the same cost markers as ``test_commit_delivery.py``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from core.io.git_helpers import create_worktree
from pipeline.engine.commit_delivery import (
    apply_commit_delivery,
    resolve_commit_delivery,
)
from pipeline.engine.delivery_scope import (
    DELIVERY_SCOPE_VIOLATION,
    assess_delivery_scope,
    collect_sibling_changes,
    evaluate_delivery_scope,
)
from pipeline.runtime.run_shape import DeliveryScope
from sdk.run_control.delivery import decide_delivery, delivery_decision_state

# ── helpers ──────────────────────────────────────────────────────────────────


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
    return _head(repo)


def _head(repo: Path) -> str:
    r = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo,
        capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


def _worktree_with_diff(repo: Path, run_dir: Path) -> Path:
    """A run-owned worktree carrying a real diff vs the repo baseline."""
    result = create_worktree(
        repo=repo,
        base_ref=_head(repo),
        target_path=run_dir / "checkout",
        branch_name="orcho/run/scope-test",
    )
    assert result.ok, result.error
    checkout = run_dir / "checkout"
    (checkout / "app.txt").write_text("base\nrun-owned change\n", encoding="utf-8")
    return checkout


def _write_workspace_config(ws: Path, projects: dict[str, str]) -> None:
    cfg_dir = ws / ".orcho"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.local.json").write_text(
        json.dumps({"projects": projects}), encoding="utf-8",
    )


def _session(*, scope: str | None, projects: tuple[str, ...]) -> dict:
    session: dict = {
        "status": "done",
        "phases": {
            "final_acceptance": {"verdict": "APPROVED", "short_summary": "ok"},
        },
    }
    if scope is not None:
        session["auto_detect"] = {
            "detection_state": "recommended",
            "actual_profile": "feature",
            "actual_mode": "pro",
            "delivery_scope": scope,
            "delivery_projects": list(projects),
        }
    return session


def _commit_config() -> dict:
    return {
        "enabled": True,
        "auto_in_ci": "approve",
        "add_untracked": True,
        "default_strategy": "release_summary",
    }


# ── (a) pure classification, no I/O ──────────────────────────────────────────


def test_assess_no_sibling_changes_is_in_scope() -> None:
    a = assess_delivery_scope(
        scope=DeliveryScope.STRICT_MONO, sibling_changes={},
    )
    assert a.in_scope is True
    assert a.blocked is False
    assert a.blocker is None
    assert a.affected_projects == ()
    assert a.disclosure == ()


def test_assess_expanded_with_changes_discloses_and_proceeds() -> None:
    a = assess_delivery_scope(
        scope=DeliveryScope.EXPANDED_MONO,
        sibling_changes={"orcho-mcp": ("[orcho-mcp]/read.py",)},
    )
    assert a.in_scope is True
    assert a.blocked is False
    assert a.affected_projects == ("orcho-mcp",)
    assert a.disclosure == ("[orcho-mcp]/read.py",)


def test_assess_strict_with_changes_is_reversible_blocker() -> None:
    a = assess_delivery_scope(
        scope=DeliveryScope.STRICT_MONO,
        sibling_changes={"orcho-mcp": ("[orcho-mcp]/read.py",)},
    )
    assert a.in_scope is False
    assert a.blocked is True
    assert a.blocker == DELIVERY_SCOPE_VIOLATION
    assert a.disclosure == ("[orcho-mcp]/read.py",)


def test_assess_cross_is_not_applicable_to_mono() -> None:
    a = assess_delivery_scope(
        scope=DeliveryScope.CROSS,
        sibling_changes={"orcho-mcp": ("[orcho-mcp]/read.py",)},
    )
    # Cross is delivered by the cross pipeline, not this mono gate → proceeds.
    assert a.in_scope is True
    assert a.blocked is False


def test_assess_drops_primary_alias_changes() -> None:
    a = assess_delivery_scope(
        scope=DeliveryScope.STRICT_MONO,
        sibling_changes={
            "orcho-core": ("[orcho-core]/x.py",),
            "orcho-mcp": ("[orcho-mcp]/y.py",),
        },
        primary_alias="orcho-core",
    )
    # The primary's own changes are never a violation.
    assert a.affected_projects == ("orcho-mcp",)
    assert a.blocked is True


# ── (b) collect_sibling_changes against real repos ───────────────────────────


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
@pytest.mark.serial
def test_collect_sibling_changes_returns_per_alias_dirty(tmp_path: Path) -> None:
    primary = tmp_path / "orcho-core"
    sibling = tmp_path / "orcho-mcp"
    _init_repo(primary)
    _init_repo(sibling)
    ws = tmp_path / "ws"
    _write_workspace_config(ws, {
        "orcho-core": str(primary),
        "orcho-mcp": str(sibling),
    })
    # Dirty the sibling repo.
    (sibling / "read.py").write_text("changed\n", encoding="utf-8")

    out = collect_sibling_changes(
        delivery_projects=("orcho-core", "orcho-mcp"),
        primary_project_dir=primary,
        workspace=ws,
    )
    # Primary is skipped (its diff is the run-owned diff); sibling is collected.
    assert set(out) == {"orcho-mcp"}
    assert out["orcho-mcp"] == ("[orcho-mcp]/read.py",)


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
@pytest.mark.serial
def test_collect_unregistered_alias_degrades_softly(tmp_path: Path) -> None:
    primary = tmp_path / "orcho-core"
    _init_repo(primary)
    ws = tmp_path / "ws"
    _write_workspace_config(ws, {"orcho-core": str(primary)})

    # 'orcho-mcp' is not registered → soft degrade, no entry, no exception.
    out = collect_sibling_changes(
        delivery_projects=("orcho-core", "orcho-mcp"),
        primary_project_dir=primary,
        workspace=ws,
    )
    assert out == {}


# ── (c) expanded_mono: delivery proceeds + real disclosure ───────────────────


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
@pytest.mark.serial
def test_expanded_mono_delivers_with_sibling_disclosure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = tmp_path / "orcho-core"
    sibling = tmp_path / "orcho-mcp"
    _init_repo(primary)
    _init_repo(sibling)
    ws = tmp_path / "ws"
    _write_workspace_config(ws, {
        "orcho-core": str(primary),
        "orcho-mcp": str(sibling),
    })
    monkeypatch.setenv("ORCHO_WORKSPACE", str(ws))
    (sibling / "read.py").write_text("expanded change\n", encoding="utf-8")

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    worktree = _worktree_with_diff(primary, run_dir)

    decision = resolve_commit_delivery(
        project_dir=primary,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="run-expanded",
        session=_session(
            scope="expanded_mono", projects=("orcho-core", "orcho-mcp"),
        ),
        commit_config=_commit_config(),
        no_interactive=True,
        baseline_ref=_head(primary),
    )
    # Delivery proceeds (not blocked); sibling files are disclosed.
    assert decision.scope_blocker == ""
    assert decision.delivery_scope == "expanded_mono"
    assert "[orcho-mcp]/read.py" in decision.scope_disclosure
    assert decision.action == "approve"
    assert decision.status == "pending"


# ── (c2) expanded_mono real-apply: diff lands in primary, sibling stays out ──


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
@pytest.mark.serial
def test_expanded_mono_real_apply_delivers_diff_without_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """expanded_mono drives the run-owned diff through ``apply_commit_delivery``.

    Unlike the decision-only test above this one calls ``apply`` on real repos
    and proves the run-owned diff is committed into the *primary* checkout while
    the dirty *companion* sibling repo is disclosed but never shipped: it is not
    staged into the primary commit and never trips the target-dirty guard (which
    only ever inspects the primary ``project_path``).
    """
    primary = tmp_path / "orcho-core"
    sibling = tmp_path / "orcho-mcp"
    _init_repo(primary)
    _init_repo(sibling)
    ws = tmp_path / "ws"
    _write_workspace_config(ws, {
        "orcho-core": str(primary),
        "orcho-mcp": str(sibling),
    })
    monkeypatch.setenv("ORCHO_WORKSPACE", str(ws))
    # Dirty companion repo — disclosed under expanded_mono, but not delivered.
    (sibling / "read.py").write_text("expanded change\n", encoding="utf-8")

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    worktree = _worktree_with_diff(primary, run_dir)
    baseline = _head(primary)

    decision = resolve_commit_delivery(
        project_dir=primary,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="run-expanded-apply",
        session=_session(
            scope="expanded_mono", projects=("orcho-core", "orcho-mcp"),
        ),
        commit_config=_commit_config(),
        no_interactive=True,
        baseline_ref=baseline,
    )
    # Resolve discloses the sibling and lets delivery proceed (no blocker).
    assert decision.scope_blocker == ""
    assert decision.delivery_scope == "expanded_mono"
    assert "[orcho-mcp]/read.py" in decision.scope_disclosure
    assert decision.action == "approve"
    assert decision.status == "pending"

    applied = apply_commit_delivery(
        decision,
        run_dir=run_dir,
        commit_config=_commit_config(),
        no_interactive=True,
    )

    # The run-owned diff is really committed into the primary checkout.
    assert applied.status == "committed"
    assert applied.commit_sha
    assert _head(primary) != baseline
    assert _head(primary) == applied.commit_sha
    assert (primary / "app.txt").read_text(encoding="utf-8") == (
        "base\nrun-owned change\n"
    )
    assert "app.txt" in applied.files_staged

    # The companion sibling file was disclosed but never shipped: not staged
    # into the primary commit, and it did not trip the target-dirty guard.
    assert applied.status != "target_dirty"
    assert "read.py" not in applied.files_staged
    assert "[orcho-mcp]/read.py" not in applied.files_staged
    # The primary commit contains only the run-owned file.
    show = subprocess.run(
        ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
        cwd=primary, capture_output=True, text=True, check=True,
    )
    committed_files = show.stdout.split()
    assert "read.py" not in committed_files
    assert committed_files == ["app.txt"]
    # Scope disclosure / fields survive the apply + persist round-trip.
    assert applied.delivery_scope == "expanded_mono"
    assert applied.scope_blocker == ""
    assert "[orcho-mcp]/read.py" in applied.scope_disclosure
    # Sibling repo itself is untouched — still dirty, never committed.
    assert (sibling / "read.py").read_text(encoding="utf-8") == "expanded change\n"


# ── (d) strict_mono: reversible typed blocker, decidable, no exception ───────


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
@pytest.mark.serial
def test_strict_mono_yields_reversible_typed_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = tmp_path / "orcho-core"
    sibling = tmp_path / "orcho-mcp"
    _init_repo(primary)
    _init_repo(sibling)
    ws = tmp_path / "ws"
    _write_workspace_config(ws, {
        "orcho-core": str(primary),
        "orcho-mcp": str(sibling),
    })
    monkeypatch.setenv("ORCHO_WORKSPACE", str(ws))
    (sibling / "read.py").write_text("strict violation\n", encoding="utf-8")

    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-strict"
    run_dir.mkdir(parents=True)
    worktree = _worktree_with_diff(primary, run_dir)

    # No exception — a typed, parked, decidable gate.
    decision = resolve_commit_delivery(
        project_dir=primary,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="run-strict",
        session=_session(
            scope="strict_mono", projects=("orcho-core", "orcho-mcp"),
        ),
        commit_config=_commit_config(),
        no_interactive=True,
        baseline_ref=_head(primary),
    )
    assert decision.status == "pending"
    assert decision.action == "none"
    assert decision.scope_blocker == DELIVERY_SCOPE_VIOLATION
    assert "[orcho-mcp]/read.py" in decision.scope_disclosure

    # Persist the parked gate and read it back through the SDK projection.
    (run_dir / "meta.json").write_text(
        json.dumps({
            "status": "done",
            "project": str(primary),
            "commit_delivery": decision.to_dict(),
        }),
        encoding="utf-8",
    )

    state = delivery_decision_state("run-strict", runs_dir=runs_dir, cwd=None)
    assert state.decidable is True
    assert state.reason is not None
    assert DELIVERY_SCOPE_VIOLATION in state.reason
    assert "approve" in state.blocked_actions
    assert "apply" in state.blocked_actions
    assert "skip" in state.available_actions
    assert "halt" in state.available_actions
    assert "[orcho-mcp]/read.py" in state.scope_disclosure

    # Shipping is refused with the typed blocker; the gate stays reversible.
    result = decide_delivery("run-strict", "approve", runs_dir=runs_dir, cwd=None)
    assert result.accepted is False
    assert result.blocker == DELIVERY_SCOPE_VIOLATION
    assert "[orcho-mcp]/read.py" in result.scope_disclosure


# ── (d1b) strict_mono real-apply: gate stays parked, primary HEAD untouched ───


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
@pytest.mark.serial
def test_strict_mono_real_apply_ships_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """strict_mono survives ``apply_commit_delivery`` as an untouched blocker.

    Symmetric to the expanded real-apply test: same real primary + dirty
    companion sibling, but a strict scope. Calling ``apply`` on the resolved
    decision must return it unchanged — a parked, reversible, typed blocker
    (``status='pending'``, ``action='none'``, ``scope_blocker``
    ``DELIVERY_SCOPE_VIOLATION``) — and must ship nothing: the primary HEAD is
    identical to baseline.
    """
    primary = tmp_path / "orcho-core"
    sibling = tmp_path / "orcho-mcp"
    _init_repo(primary)
    _init_repo(sibling)
    ws = tmp_path / "ws"
    _write_workspace_config(ws, {
        "orcho-core": str(primary),
        "orcho-mcp": str(sibling),
    })
    monkeypatch.setenv("ORCHO_WORKSPACE", str(ws))
    (sibling / "read.py").write_text("strict violation\n", encoding="utf-8")

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    worktree = _worktree_with_diff(primary, run_dir)
    baseline = _head(primary)

    decision = resolve_commit_delivery(
        project_dir=primary,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="run-strict-apply",
        session=_session(
            scope="strict_mono", projects=("orcho-core", "orcho-mcp"),
        ),
        commit_config=_commit_config(),
        no_interactive=True,
        baseline_ref=baseline,
    )
    assert decision.status == "pending"
    assert decision.action == "none"
    assert decision.scope_blocker == DELIVERY_SCOPE_VIOLATION
    assert "[orcho-mcp]/read.py" in decision.scope_disclosure

    applied = apply_commit_delivery(
        decision,
        run_dir=run_dir,
        commit_config=_commit_config(),
        no_interactive=True,
    )

    # The decision returns untouched — nothing was shipped.
    assert applied.status == "pending"
    assert applied.action == "none"
    assert applied.scope_blocker == DELIVERY_SCOPE_VIOLATION
    assert applied.commit_sha is None
    assert applied.files_staged == ()
    assert "[orcho-mcp]/read.py" in applied.scope_disclosure
    # Primary checkout is exactly as it was — no commit, no applied diff.
    assert _head(primary) == baseline
    assert (primary / "app.txt").read_text(encoding="utf-8") == "base\n"


# ── (d2) strict_mono halts the real pipeline (not a clean DONE) ───────────────


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
@pytest.mark.serial
def test_strict_mono_halts_real_run_commit_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: ``_run_commit_delivery`` HALTS a strict-mono violation.

    A pure ``resolve_commit_delivery`` returns the parked decision, but the
    real run wiring must also flip the session to ``halted`` with a typed
    recoverable reason — otherwise a sibling-repo violation finalizes as a clean
    DONE run that hides a parked, undecided scope gate (F1).
    """
    from types import SimpleNamespace

    from pipeline.project.run import _PipelineRun
    from pipeline.project.types import PresentationPolicy

    primary = tmp_path / "orcho-core"
    sibling = tmp_path / "orcho-mcp"
    _init_repo(primary)
    _init_repo(sibling)
    ws = tmp_path / "ws"
    _write_workspace_config(ws, {
        "orcho-core": str(primary),
        "orcho-mcp": str(sibling),
    })
    monkeypatch.setenv("ORCHO_WORKSPACE", str(ws))
    (sibling / "read.py").write_text("strict violation\n", encoding="utf-8")

    run_dir = tmp_path / "runs" / "run-strict-real"
    run_dir.mkdir(parents=True)
    worktree = _worktree_with_diff(primary, run_dir)
    baseline = _head(primary)

    session = _session(scope="strict_mono", projects=("orcho-core", "orcho-mcp"))

    # A lightweight stand-in carrying exactly the attributes the method reads;
    # the strict-scope branch halts before any agent / presentation work runs.
    run = SimpleNamespace(
        output_dir=run_dir,
        session=session,
        parent_run_id=None,
        project_alias=None,
        phase_config=SimpleNamespace(final_acceptance_agent=None),
        state=SimpleNamespace(extras={}),
        project_path=primary,
        session_ts="run-strict-real",
        no_interactive=True,
        _presentation=PresentationPolicy.SILENT,
        _commit_delivery_baseline=lambda: baseline,
    )

    # No exception — the run halts at a recoverable delivery-scope pause.
    _PipelineRun._run_commit_delivery(run, worktree)

    assert session["status"] == "halted"
    assert session["halt_reason"] == "commit_delivery_scope_blocked"
    cd = session["commit_delivery"]
    assert cd["status"] == "pending"
    assert cd["scope_blocker"] == DELIVERY_SCOPE_VIOLATION
    assert "[orcho-mcp]/read.py" in cd["scope_disclosure"]


# ── (e) no delivery_scope → no enforcement (no regression) ───────────────────


def test_evaluate_returns_none_without_scope() -> None:
    # No auto_detect at all.
    assert evaluate_delivery_scope(
        session={"status": "done"}, primary_project_dir=Path("/nope"),
    ) is None
    # auto_detect present but no delivery_scope key.
    assert evaluate_delivery_scope(
        session={"auto_detect": {"actual_profile": "feature"}},
        primary_project_dir=Path("/nope"),
    ) is None


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
@pytest.mark.serial
def test_no_scope_session_delivers_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = tmp_path / "orcho-core"
    sibling = tmp_path / "orcho-mcp"
    _init_repo(primary)
    _init_repo(sibling)
    ws = tmp_path / "ws"
    _write_workspace_config(ws, {
        "orcho-core": str(primary),
        "orcho-mcp": str(sibling),
    })
    monkeypatch.setenv("ORCHO_WORKSPACE", str(ws))
    # Sibling is dirty, but the run recorded NO delivery scope — guard no-op.
    (sibling / "read.py").write_text("ignored\n", encoding="utf-8")

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    worktree = _worktree_with_diff(primary, run_dir)

    decision = resolve_commit_delivery(
        project_dir=primary,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="run-noscope",
        session=_session(scope=None, projects=()),
        commit_config=_commit_config(),
        no_interactive=True,
        baseline_ref=_head(primary),
    )
    # Behaviour unchanged: delivers, carries no scope fields, no blocker.
    assert decision.scope_blocker == ""
    assert decision.delivery_scope == ""
    assert decision.scope_disclosure == ()
    assert decision.action == "approve"
    assert "delivery_scope" not in decision.to_dict()


# ── (f) unregistered alias under a scope → soft degrade, no crash ────────────


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
@pytest.mark.serial
def test_unregistered_alias_under_scope_does_not_crash_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = tmp_path / "orcho-core"
    _init_repo(primary)
    ws = tmp_path / "ws"
    # Only the primary is registered; 'orcho-mcp' alias is unknown.
    _write_workspace_config(ws, {"orcho-core": str(primary)})
    monkeypatch.setenv("ORCHO_WORKSPACE", str(ws))

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    worktree = _worktree_with_diff(primary, run_dir)

    decision = resolve_commit_delivery(
        project_dir=primary,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="run-unreg",
        session=_session(
            scope="strict_mono", projects=("orcho-core", "orcho-mcp"),
        ),
        commit_config=_commit_config(),
        no_interactive=True,
        baseline_ref=_head(primary),
    )
    # No sibling changes were collectable → no violation, delivery proceeds.
    assert decision.scope_blocker == ""
    assert decision.action == "approve"
    assert decision.status == "pending"


# ── (g) companion enrichment attaches on the delivery_projects path (T1) ──────


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
@pytest.mark.serial
def test_companions_attach_for_delivery_projects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dirty companion declared via ``delivery_projects`` rides on the decision.

    The existing expanded/strict tests assert the backward-compatible string
    disclosure; this one asserts the additive T1 per-repo companion enrichment is
    attached too, and the first-detection base revision is recorded in meta.
    """
    primary = tmp_path / "orcho-core"
    sibling = tmp_path / "orcho-mcp"
    _init_repo(primary)
    sibling_base = _head(sibling) if sibling.exists() else _init_repo(sibling)
    ws = tmp_path / "ws"
    _write_workspace_config(ws, {
        "orcho-core": str(primary),
        "orcho-mcp": str(sibling),
    })
    monkeypatch.setenv("ORCHO_WORKSPACE", str(ws))
    (sibling / "read.py").write_text("dirty companion\n", encoding="utf-8")

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    worktree = _worktree_with_diff(primary, run_dir)
    session = _session(scope="expanded_mono", projects=("orcho-core", "orcho-mcp"))

    decision = resolve_commit_delivery(
        project_dir=primary,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="run-companion-attach",
        session=session,
        commit_config=_commit_config(),
        no_interactive=True,
        baseline_ref=_head(primary),
    )

    companions = decision.to_dict()["scope_companions"]
    assert companions == [{
        "alias": "orcho-mcp",
        "path": str(sibling.resolve()),
        "state": "dirty",
        "changed_paths": ["[orcho-mcp]/read.py"],
    }]
    assert session["auto_detect"]["companion_base_revisions"] == {
        "orcho-mcp": sibling_base,
    }
