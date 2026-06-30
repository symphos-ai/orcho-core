"""T1 companion-repo derivation + per-repo state disclosure tests.

Two layers, mirroring ``companion_scope``'s split:

* **pure** — :func:`derive_companion_aliases` (durable plan scope ∪
  delivery_projects, alias recognition, tag/primary filtering) and
  :func:`classify_companion_state` (dirty / committed / planned_requirement,
  including the load-bearing *clean-but-committed* case) with no I/O;
* **integration** — real primary + companion git repos registered in a
  workspace ``.orcho/config.local.json``, exercising the ``evaluate_delivery_scope``
  hook end to end: plan-scope-derived companion detection, first-detection base
  recording into durable meta, an observably-committed companion, and the
  strict-mono single-repo no-op.

Real-repo tests carry the same cost markers as ``test_delivery_scope.py``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agents.entities import SubTask
from core.io.git_helpers import create_worktree
from pipeline.engine.commit_delivery import resolve_commit_delivery
from pipeline.engine.companion_scope import (
    CompanionRepo,
    CompanionRepoState,
    classify_companion_state,
    derive_companion_aliases,
    derive_companion_declared_paths,
)
from pipeline.engine.delivery_scope import (
    evaluate_delivery_scope,
    record_companion_bases_at_detection,
)
from pipeline.plan_artifacts import write_parsed_plan_artifact
from pipeline.plan_parser import ParsedPlan

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


def _commit(repo: Path, rel: str, body: str, message: str) -> str:
    (repo / rel).write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)
    return _head(repo)


def _worktree_with_diff(repo: Path, run_dir: Path) -> Path:
    result = create_worktree(
        repo=repo,
        base_ref=_head(repo),
        target_path=run_dir / "checkout",
        branch_name="orcho/run/companion-test",
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


def _write_plan(run_dir: Path, *, plan_mods=(), subtask_mods=()) -> None:
    plan = ParsedPlan(
        short_summary="s",
        planning_context="ctx",
        subtasks=(
            SubTask(
                id="T1",
                goal="do work",
                spec="",
                owned_files=("pipeline/x.py",),
                allowed_modifications=tuple(subtask_mods),
            ),
        ),
        source="json",
        goal="goal here",
        acceptance_criteria=("a",),
        owned_files=("pipeline/x.py",),
        allowed_modifications=tuple(plan_mods),
    )
    write_parsed_plan_artifact(run_dir, plan, attempt=1)


def _session(*, scope: str, projects=(), bases=None) -> dict:
    auto: dict = {
        "detection_state": "recommended",
        "actual_profile": "feature",
        "actual_mode": "pro",
        "delivery_scope": scope,
        "delivery_projects": list(projects),
    }
    if bases is not None:
        auto["companion_base_revisions"] = dict(bases)
    return {
        "status": "done",
        "phases": {
            "final_acceptance": {"verdict": "APPROVED", "short_summary": "ok"},
        },
        "auto_detect": auto,
    }


def _commit_config() -> dict:
    return {
        "enabled": True,
        "auto_in_ci": "approve",
        "add_untracked": True,
        "default_strategy": "release_summary",
    }


# ── (a) pure derivation ──────────────────────────────────────────────────────


def test_derive_from_delivery_projects_only() -> None:
    aliases = derive_companion_aliases(
        plan=None,
        delivery_projects=("orcho-core", "orcho-mcp"),
        known_aliases=("orcho-core", "orcho-mcp"),
        primary_alias="orcho-core",
    )
    assert aliases == ("orcho-mcp",)


def test_derive_recognizes_registered_alias_in_plan_scope() -> None:
    plan = ParsedPlan(
        short_summary="s",
        planning_context="ctx",
        subtasks=(
            SubTask(
                id="T1",
                goal="g",
                allowed_modifications=("[orcho-mcp]/server.py", "../orcho-web/app.py"),
            ),
        ),
        source="json",
        goal="g",
        owned_files=("pipeline/x.py",),
    )
    aliases = derive_companion_aliases(
        plan=plan,
        delivery_projects=(),
        known_aliases=("orcho-core", "orcho-mcp", "orcho-web"),
        primary_alias="orcho-core",
    )
    # Both registered sibling aliases are recognised from the plan scope refs.
    assert aliases == ("orcho-mcp", "orcho-web")


def test_derive_ignores_subtask_tag_and_unregistered_tokens() -> None:
    plan = ParsedPlan(
        short_summary="s",
        planning_context="ctx",
        # ``[T6-mcp-parity]`` is a subtask tag, not an alias; ``pipeline/...`` is a
        # primary-repo path — neither names a registered alias.
        subtasks=(SubTask(id="T1", goal="g"),),
        source="json",
        goal="g",
        owned_files=("pipeline/x.py",),
        allowed_modifications=(
            "[T6-mcp-parity] ../orcho-mcp/**",
            "pipeline/engine/delivery_scope.py",
        ),
    )
    aliases = derive_companion_aliases(
        plan=plan,
        delivery_projects=(),
        known_aliases=("orcho-core", "orcho-mcp"),
        primary_alias="orcho-core",
    )
    # Only the genuine registered alias survives; the tag never leaks in.
    assert aliases == ("orcho-mcp",)


def test_derive_unions_plan_scope_and_delivery_projects() -> None:
    plan = ParsedPlan(
        short_summary="s",
        planning_context="ctx",
        subtasks=(SubTask(id="T1", goal="g"),),
        source="json",
        goal="g",
        owned_files=("pipeline/x.py",),
        allowed_modifications=("../orcho-web/app.py",),
    )
    aliases = derive_companion_aliases(
        plan=plan,
        delivery_projects=("orcho-mcp",),
        known_aliases=("orcho-core", "orcho-mcp", "orcho-web"),
        primary_alias="orcho-core",
    )
    assert aliases == ("orcho-mcp", "orcho-web")


def test_derive_declared_paths_keeps_per_alias_relative_paths() -> None:
    """Per-alias declared paths are extracted from plan-scope refs (F1)."""
    plan = ParsedPlan(
        short_summary="s",
        planning_context="ctx",
        subtasks=(
            SubTask(
                id="T1",
                goal="g",
                allowed_modifications=(
                    "[orcho-mcp]/src/server.py",
                    "../orcho-mcp/docs/schema.json",
                    "[T6-mcp-parity] ../orcho-web/**",
                ),
            ),
        ),
        source="json",
        goal="g",
        owned_files=("pipeline/x.py",),
    )
    declared = derive_companion_declared_paths(
        plan=plan,
        known_aliases=("orcho-core", "orcho-mcp", "orcho-web"),
        primary_alias="orcho-core",
    )
    assert declared["orcho-mcp"] == ("docs/schema.json", "src/server.py")
    assert declared["orcho-web"] == ("**",)
    # The subtask tag never becomes its own alias entry.
    assert "T6-mcp-parity" not in declared


# ── (b) pure classification ──────────────────────────────────────────────────


def test_classify_dirty_takes_priority() -> None:
    state = classify_companion_state(
        changed_files=("[orcho-mcp]/server.py",),
        committed_files=("[orcho-mcp]/other.py",),
        base_revision="abc123",
    )
    assert state is CompanionRepoState.DIRTY


def test_classify_planned_requirement_when_untouched() -> None:
    state = classify_companion_state(
        changed_files=(),
        committed_files=(),
        base_revision="abc123",
    )
    assert state is CompanionRepoState.PLANNED_REQUIREMENT


def test_classify_committed_via_recorded_delivery_result() -> None:
    state = classify_companion_state(
        changed_files=(),
        committed_files=(),
        recorded_delivery_commit="deadbeef",
        base_revision=None,
    )
    assert state is CompanionRepoState.COMMITTED


def test_clean_but_committed_is_committed_not_planned() -> None:
    """A clean tree whose HEAD advanced past base is ``committed``, not planned.

    This is the load-bearing discriminator: the durable base revision plus the
    commit-range paths distinguish a clean-but-committed companion from a
    declared-but-untouched ``planned_requirement`` — never a dirty-vs-clean
    heuristic.
    """
    state = classify_companion_state(
        changed_files=(),  # working tree is CLEAN
        committed_files=("[orcho-mcp]/server.py",),  # but HEAD moved past base
        base_revision="base0sha",
    )
    assert state is CompanionRepoState.COMMITTED


def test_classify_no_base_clean_is_planned_not_committed() -> None:
    # Without a recorded base there is no observable committed signal → planned.
    state = classify_companion_state(
        changed_files=(),
        committed_files=("[orcho-mcp]/server.py",),
        base_revision=None,
    )
    assert state is CompanionRepoState.PLANNED_REQUIREMENT


def test_companion_repo_to_dict_is_json_safe() -> None:
    repo = CompanionRepo(
        alias="orcho-mcp",
        path="/ws/orcho-mcp",
        state=CompanionRepoState.DIRTY,
        changed_paths=("[orcho-mcp]/server.py",),
    )
    assert repo.to_dict() == {
        "alias": "orcho-mcp",
        "path": "/ws/orcho-mcp",
        "state": "dirty",
        "changed_paths": ["[orcho-mcp]/server.py"],
    }


# ── (c) integration: dirty companion derived from durable plan scope ──────────


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
@pytest.mark.serial
def test_plan_scope_dirty_companion_disclosed_and_base_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = tmp_path / "orcho-core"
    sibling = tmp_path / "orcho-mcp"
    _init_repo(primary)
    sibling_base = _init_repo(sibling)
    ws = tmp_path / "ws"
    _write_workspace_config(ws, {
        "orcho-core": str(primary),
        "orcho-mcp": str(sibling),
    })
    monkeypatch.setenv("ORCHO_WORKSPACE", str(ws))
    # Dirty the companion's working tree.
    (sibling / "server.py").write_text("changed\n", encoding="utf-8")

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    worktree = _worktree_with_diff(primary, run_dir)
    # The companion is declared ONLY through the durable plan scope (no
    # delivery_projects) — proving plan-scope derivation, not transcript / config.
    _write_plan(run_dir, subtask_mods=("../orcho-mcp/server.py",))

    # expanded_mono so the dirty companion discloses without blocking delivery.
    session = _session(scope="expanded_mono", projects=())
    decision = resolve_commit_delivery(
        project_dir=primary,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="run-companion-dirty",
        session=session,
        commit_config=_commit_config(),
        no_interactive=True,
        baseline_ref=_head(primary),
    )

    # Per-repo companion disclosure rides on the decision + to_dict.
    payload = decision.to_dict()
    companions = payload["scope_companions"]
    assert companions == [{
        "alias": "orcho-mcp",
        "path": str(sibling.resolve()),
        "state": "dirty",
        "changed_paths": ["[orcho-mcp]/server.py"],
    }]
    # patch_text is never serialised — the durable invariant holds.
    assert "patch_text" not in payload
    # Backward-compatible string disclosure format preserved.
    assert "[orcho-mcp]/server.py" in decision.scope_disclosure

    # First-detection base revision recorded into durable meta.auto_detect.
    recorded = session["auto_detect"]["companion_base_revisions"]
    assert recorded == {"orcho-mcp": sibling_base}


# ── (d) integration: clean-but-committed companion observed as committed ──────


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
@pytest.mark.serial
def test_clean_committed_companion_observed_via_base_advance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = tmp_path / "orcho-core"
    sibling = tmp_path / "orcho-mcp"
    _init_repo(primary)
    base0 = _init_repo(sibling)
    ws = tmp_path / "ws"
    _write_workspace_config(ws, {
        "orcho-core": str(primary),
        "orcho-mcp": str(sibling),
    })
    monkeypatch.setenv("ORCHO_WORKSPACE", str(ws))
    # The companion required edit landed as a real commit — working tree is CLEAN
    # but HEAD advanced past the recorded base.
    _commit(sibling, "server.py", "committed change\n", "feat: companion edit")

    run_dir = tmp_path / "run"
    run_dir.mkdir()

    # base0 was captured at first detection (recorded in durable meta).
    session = _session(
        scope="expanded_mono",
        projects=("orcho-core", "orcho-mcp"),
        bases={"orcho-mcp": base0},
    )
    assessment = evaluate_delivery_scope(
        session=session,
        primary_project_dir=primary,
        run_dir=run_dir,
        workspace=ws,
    )
    assert assessment is not None
    companions = {c.alias: c for c in assessment.companions}
    mcp = companions["orcho-mcp"]
    # Clean tree + advanced HEAD ⇒ committed, NOT planned_requirement.
    assert mcp.state is CompanionRepoState.COMMITTED
    assert mcp.changed_paths == ("[orcho-mcp]/server.py",)
    # A committed companion does not feed the dirty-sibling blocker.
    assert assessment.blocked is False
    assert assessment.sibling_changes == {}


# ── (d2) committed over an UNDECLARED path stays planned_requirement (F1) ─────


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
@pytest.mark.serial
def test_committed_unrelated_path_stays_planned_requirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A HEAD advance that misses the declared path is NOT ``committed`` (F1).

    The plan declares ``[orcho-mcp]/server.py`` as the required companion edit.
    The companion HEAD advances, but only over an *unrelated* file
    (``readme.md``) — the required edit never landed. Committed-state must be
    measured against the *declared* paths, so this companion stays
    ``planned_requirement`` and never falsely discloses the unrelated commit as
    delivered.
    """
    primary = tmp_path / "orcho-core"
    companion = tmp_path / "orcho-mcp"
    _init_repo(primary)
    base0 = _init_repo(companion)
    ws = tmp_path / "ws"
    _write_workspace_config(ws, {
        "orcho-core": str(primary),
        "orcho-mcp": str(companion),
    })
    monkeypatch.setenv("ORCHO_WORKSPACE", str(ws))
    # Clean tree, HEAD advanced — but over an UNDECLARED path, not server.py.
    _commit(companion, "readme.md", "unrelated docs\n", "docs: tweak readme")

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    # The plan declares the companion's required edit as server.py only.
    _write_plan(run_dir, subtask_mods=("../orcho-mcp/server.py",))
    session = _session(
        scope="expanded_mono", projects=(), bases={"orcho-mcp": base0},
    )

    assessment = evaluate_delivery_scope(
        session=session, primary_project_dir=primary, run_dir=run_dir, workspace=ws,
    )
    assert assessment is not None
    mcp = {c.alias: c for c in assessment.companions}["orcho-mcp"]
    # The declared edit (server.py) never landed → still a pending requirement.
    assert mcp.state is CompanionRepoState.PLANNED_REQUIREMENT
    assert mcp.changed_paths == ()


# ── (d3) base captured at detection, committed before delivery → committed (F2) ─


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
@pytest.mark.serial
def test_base_captured_at_detection_then_committed_before_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Base recorded at first detection, companion committed before delivery (F2).

    No base is pre-injected. ``record_companion_bases_at_detection`` runs at the
    plan-end detection moment and captures the companion's *pre-work* HEAD. Only
    afterwards does the run commit the required companion edit (HEAD advances).
    The delivery-time scan then observes the advance against the captured base and
    classifies ``committed`` — the timing the dirty-vs-clean heuristic and a
    delivery-time-only capture would both miss (the latter would capture the
    already-advanced HEAD as the base and mis-read it as ``planned_requirement``).
    """
    primary = tmp_path / "orcho-core"
    companion = tmp_path / "orcho-mcp"
    _init_repo(primary)
    base0 = _head(companion) if companion.exists() else _init_repo(companion)
    ws = tmp_path / "ws"
    _write_workspace_config(ws, {
        "orcho-core": str(primary),
        "orcho-mcp": str(companion),
    })
    monkeypatch.setenv("ORCHO_WORKSPACE", str(ws))

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_plan(run_dir, subtask_mods=("../orcho-mcp/server.py",))
    # NO base pre-injected — detection must capture it.
    session = _session(scope="expanded_mono", projects=())

    # (1) First-detection capture (plan-end), BEFORE the companion advances.
    record_companion_bases_at_detection(
        session=session,
        primary_project_dir=primary,
        run_dir=run_dir,
        workspace=ws,
    )
    assert session["auto_detect"]["companion_base_revisions"] == {
        "orcho-mcp": base0,
    }

    # (2) The run now lands the required companion edit as a real commit.
    _commit(companion, "server.py", "committed edit\n", "feat: companion edit")

    # (3) Delivery-time scan observes the advance vs the captured pre-work base.
    assessment = evaluate_delivery_scope(
        session=session, primary_project_dir=primary, run_dir=run_dir, workspace=ws,
    )
    assert assessment is not None
    mcp = {c.alias: c for c in assessment.companions}["orcho-mcp"]
    assert mcp.state is CompanionRepoState.COMMITTED
    assert mcp.changed_paths == ("[orcho-mcp]/server.py",)
    # The detection-time base is unchanged — capture is idempotent, never moved
    # forward to the post-commit HEAD.
    assert session["auto_detect"]["companion_base_revisions"] == {
        "orcho-mcp": base0,
    }


# ── (e) strict-mono single-repo run stays a no-op (no companion disclosure) ───


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
@pytest.mark.serial
def test_strict_mono_single_repo_no_companion_disclosure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = tmp_path / "orcho-core"
    _init_repo(primary)
    ws = tmp_path / "ws"
    _write_workspace_config(ws, {"orcho-core": str(primary)})
    monkeypatch.setenv("ORCHO_WORKSPACE", str(ws))

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    worktree = _worktree_with_diff(primary, run_dir)
    # A real single-repo plan: only the target is touched, no companion declared.
    _write_plan(run_dir, subtask_mods=())

    session = _session(scope="strict_mono", projects=())
    decision = resolve_commit_delivery(
        project_dir=primary,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="run-mono",
        session=session,
        commit_config=_commit_config(),
        no_interactive=True,
        baseline_ref=_head(primary),
    )

    # No companions → no blocker, no disclosure noise, delivery proceeds.
    assert decision.scope_blocker == ""
    assert decision.scope_companions == ()
    assert decision.scope_disclosure == ()
    assert decision.action == "approve"
    assert decision.status == "pending"
    payload = decision.to_dict()
    assert "scope_companions" not in payload
    # No companion set was detected → no base revisions recorded.
    assert "companion_base_revisions" not in session["auto_detect"]


# ── (T4) dog-food regression: target orcho-core, dirty companion orcho-mcp ─────
#
# Models run 20260625_181331_2a392f end to end on real git repos: the target
# project (orcho-core) delivers while a mandatory companion (orcho-mcp) declared
# by the durable plan scope is dirty at delivery time. Drives the real
# ``_PipelineRun._run_commit_delivery`` so the whole chain is exercised — primary
# commit, durable ``multi_project_delivery`` propagation, evidence bundle block,
# and the finalization caveat / actionable next step. Fails on the pre-T1/T2
# baseline (no scope_companions, no multi_project_delivery, no companion caveat).


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
@pytest.mark.serial
def test_dogfood_dirty_companion_at_delivery_full_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from pipeline.evidence import collect_evidence
    from pipeline.project.finalization import build_companion_delivery_caveat
    from pipeline.project.run import _PipelineRun
    from pipeline.project.types import PresentationPolicy

    primary = tmp_path / "orcho-core"
    companion = tmp_path / "orcho-mcp"
    _init_repo(primary)
    _init_repo(companion)
    ws = tmp_path / "ws"
    _write_workspace_config(ws, {
        "orcho-core": str(primary),
        "orcho-mcp": str(companion),
    })
    monkeypatch.setenv("ORCHO_WORKSPACE", str(ws))
    # The canonical companion checkout is DIRTY at delivery time (dog-food bug).
    (companion / "server.py").write_text(
        "uncommitted companion edit\n", encoding="utf-8",
    )

    run_dir = tmp_path / "runs" / "20260625_181331_2a392f"
    run_dir.mkdir(parents=True)
    worktree = _worktree_with_diff(primary, run_dir)
    baseline = _head(primary)
    # The plan/subtask declares the mandatory companion via durable plan scope;
    # expanded_mono so the primary still ships while the companion is disclosed.
    _write_plan(run_dir, subtask_mods=("../orcho-mcp/server.py",))
    session = _session(scope="expanded_mono", projects=("orcho-core", "orcho-mcp"))

    run = SimpleNamespace(
        output_dir=run_dir,
        session=session,
        parent_run_id=None,
        project_alias=None,
        phase_config=SimpleNamespace(final_acceptance_agent=None),
        state=SimpleNamespace(extras={}),
        project_path=primary,
        session_ts="20260625_181331_2a392f",
        no_interactive=True,
        _presentation=PresentationPolicy.SILENT,
        _commit_delivery_baseline=lambda: baseline,
    )

    _PipelineRun._run_commit_delivery(run, worktree)

    # (1) Primary delivery reports as before: committed into the primary checkout.
    cd = session["commit_delivery"]
    assert cd["status"] == "committed"
    assert _head(primary) != baseline
    assert (primary / "app.txt").read_text(encoding="utf-8") == (
        "base\nrun-owned change\n"
    )

    # (2) Typed disclosure carries the dirty companion repo name/path/changed paths.
    block = session["multi_project_delivery"]
    assert block["primary_status"] == "committed"
    assert block["companions"] == [{
        "alias": "orcho-mcp",
        "path": str(companion.resolve()),
        "state": "dirty",
        "changed_paths": ["[orcho-mcp]/server.py"],
    }]
    # Backward-compatible [alias]/rel scope-disclosure string format preserved.
    assert "[orcho-mcp]/server.py" in cd.get("scope_disclosure", [])

    # (2b) The durable evidence bundle carries the multi_project_delivery block.
    (run_dir / "meta.json").write_text(
        json.dumps({**session, "project": str(primary)}), encoding="utf-8",
    )
    bundle = collect_evidence(run_dir)
    mpd = bundle["multi_project_delivery"]
    assert mpd["primary_status"] == "committed"
    assert mpd["dirty"] == ["orcho-mcp"]
    assert mpd["companions"][0]["alias"] == "orcho-mcp"
    assert mpd["companions"][0]["path"] == str(companion.resolve())
    assert mpd["companions"][0]["changed_paths"] == ["[orcho-mcp]/server.py"]

    # (3) Actionable next step: the operator must handle companion delivery.
    caveat = build_companion_delivery_caveat(session)
    assert caveat is not None
    assert caveat.primary_status == "committed"
    joined = "\n".join(caveat.lines)
    assert "orcho-mcp" in joined
    assert "[orcho-mcp]/server.py" in joined
    assert "review and commit" in joined
    assert "cross-run / follow-up" in joined

    # The companion repo itself is untouched — still dirty, never committed.
    assert (companion / "server.py").read_text(encoding="utf-8") == (
        "uncommitted companion edit\n"
    )


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
@pytest.mark.serial
def test_dogfood_control_clean_single_repo_no_disclosure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: a clean single-repo run (no companion) delivers with NO disclosure."""
    from types import SimpleNamespace

    from pipeline.project.finalization import build_companion_delivery_caveat
    from pipeline.project.run import _PipelineRun
    from pipeline.project.types import PresentationPolicy

    primary = tmp_path / "orcho-core"
    _init_repo(primary)
    ws = tmp_path / "ws"
    _write_workspace_config(ws, {"orcho-core": str(primary)})
    monkeypatch.setenv("ORCHO_WORKSPACE", str(ws))

    run_dir = tmp_path / "runs" / "run-mono"
    run_dir.mkdir(parents=True)
    worktree = _worktree_with_diff(primary, run_dir)
    baseline = _head(primary)
    # Only the target is touched; no companion declared (strict_mono single-repo).
    _write_plan(run_dir, subtask_mods=())
    session = _session(scope="strict_mono", projects=())

    run = SimpleNamespace(
        output_dir=run_dir,
        session=session,
        parent_run_id=None,
        project_alias=None,
        phase_config=SimpleNamespace(final_acceptance_agent=None),
        state=SimpleNamespace(extras={}),
        project_path=primary,
        session_ts="run-mono",
        no_interactive=True,
        _presentation=PresentationPolicy.SILENT,
        _commit_delivery_baseline=lambda: baseline,
    )

    _PipelineRun._run_commit_delivery(run, worktree)

    # Primary delivered, but no companion surface at all.
    assert session["commit_delivery"]["status"] == "committed"
    assert "multi_project_delivery" not in session
    assert "scope_companions" not in session["commit_delivery"]
    assert build_companion_delivery_caveat(session) is None


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
@pytest.mark.serial
def test_dogfood_control_clean_but_committed_companion_is_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: a clean companion whose HEAD advanced past base classifies committed.

    Synchronous with T1 — a clean-but-committed companion is observably
    ``committed`` (HEAD moved past the recorded base over declared paths), never
    a still-pending ``planned_requirement``.
    """
    primary = tmp_path / "orcho-core"
    companion = tmp_path / "orcho-mcp"
    _init_repo(primary)
    base0 = _init_repo(companion)
    ws = tmp_path / "ws"
    _write_workspace_config(ws, {
        "orcho-core": str(primary),
        "orcho-mcp": str(companion),
    })
    monkeypatch.setenv("ORCHO_WORKSPACE", str(ws))
    # The companion edit landed as a real commit: clean tree, HEAD past base0.
    _commit(companion, "server.py", "committed companion edit\n", "feat: companion")

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    session = _session(
        scope="expanded_mono",
        projects=("orcho-core", "orcho-mcp"),
        bases={"orcho-mcp": base0},
    )
    assessment = evaluate_delivery_scope(
        session=session, primary_project_dir=primary, run_dir=run_dir, workspace=ws,
    )
    assert assessment is not None
    companions = {c.alias: c for c in assessment.companions}
    assert companions["orcho-mcp"].state is CompanionRepoState.COMMITTED
    assert companions["orcho-mcp"].state is not CompanionRepoState.PLANNED_REQUIREMENT
    assert companions["orcho-mcp"].changed_paths == ("[orcho-mcp]/server.py",)
