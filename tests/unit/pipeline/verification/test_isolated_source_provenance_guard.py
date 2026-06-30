# SPDX-License-Identifier: Apache-2.0
"""Guard: fail-closed isolated-source resolution + provenance preflight (ADR 0112 §3).

End-to-end guard on a synthetic but real git-backed setup — an isolated per-run
worktree carrying a dirty diff and the canonical sibling checkout of the same
repo. It pins the three invariants the fail-closed source resolution (ADR 0112
§3, building on ADR 0108) must hold:

  (1) a verify run that resolves an isolated repo's source to the CANONICAL
      SIBLING fails on provenance (FAIL via ``apply_environment_provenance``),
      rather than passing vacuously against the clean tree;
  (2) the ``before_phase(implement)`` preflight aborts the run BEFORE implement
      when the isolated source is sibling-pointing / unbound; and
  (3) an ordinary single-checkout run (no isolation) is unaffected — the
      ambient/sibling fallback still resolves and no new failure appears.

The package ``widget`` is placed ONLY in the canonical sibling (untracked), so a
``git worktree add`` checkout from HEAD does not carry it; an env whose
``PYTHONPATH`` points at the sibling therefore imports ``widget`` from the clean
tree — exactly the "verify ran against the canonical sibling" false-green — while
the declared assertion binds ``{dependency:X}`` to the worktree. No ``orcho-mcp``
involvement.

Markers: ``git_worktree`` (creates a real worktree) + ``serial`` (shared git /
filesystem state — xdist-unsafe).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.evidence.verification_receipt import (
    collect_environment_checks,
    environment_provenance_failures,
    write_phase_verification_receipt,
)
from pipeline.plugins import PluginConfig
from pipeline.project import gate_repair
from pipeline.verification_contract import (
    VerificationContract,
    placeholder_context_for,
)
from pipeline.verification_readiness import (
    apply_environment_provenance,
    build_final_acceptance_readiness,
    classify_required_receipts,
)

pytestmark = [pytest.mark.git_worktree, pytest.mark.serial]


# ── git-backed setup helpers (mirrors test_retry_subject_guard / _init_repo) ──


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
    (repo / "f.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    r = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo,
        capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


def _add_dirty_worktree(repo: Path, worktree: Path) -> None:
    """Create a real linked worktree from HEAD and dirty it (an undelivered diff)."""
    worktree.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "orcho/run/guard",
         str(worktree), "HEAD"],
        cwd=repo, check=True,
    )
    # The run's undelivered change lives only in the worktree.
    (worktree / "f.txt").write_text("base\nworktree-change\n", encoding="utf-8")


def _meta(repo: Path, worktree: Path) -> dict:
    """The ``session['worktree']`` / ``meta.worktree`` block for the isolated run."""
    return {
        "isolation": "per_run",
        "path": str(worktree),
        "source_repo_path": str(repo),
    }


def _contract(sibling: Path) -> VerificationContract:
    """An isolated repo whose CI env imports ``widget`` from the canonical sibling
    (literal PYTHONPATH) but asserts the import lands under the worktree-bound
    ``{dependency:widget-repo}``; a required ``unit`` gate is scheduled after
    ``implement``."""
    contract = VerificationContract.from_plugin(PluginConfig(
        dependency_repos={"widget-repo": {"path": str(sibling)}},
        verification_envs={
            "ci": {
                # Deliberately the canonical sibling: simulates a verify that
                # resolved its source to the clean tree (the ADR 0112 false-green).
                "env": {"PYTHONPATH": str(sibling)},
                "assertions": [
                    {"import": "widget", "path_under": "{dependency:widget-repo}"},
                ],
            },
        },
        verification={
            "default_env": "ci",
            "required": ["unit"],
            "commands": {"unit": {"run": "true"}},
            "schedule": [
                {"after_phase": "implement", "policy": "require",
                 "commands": ["unit"]},
            ],
        },
    ))
    assert contract is not None
    return contract


def _isolated_setup(tmp_path: Path) -> tuple[Path, Path, dict]:
    """Build canonical sibling repo + dirty isolated worktree + the meta block.

    ``widget`` lives ONLY in the sibling (untracked) so the worktree checkout from
    HEAD does not carry it — an import via ``PYTHONPATH=<sibling>`` then resolves
    to the clean tree.
    """
    sibling = tmp_path / "canonical" / "widget-repo"
    worktree = tmp_path / "worktrees" / "wt_guard" / "checkout"
    _init_repo(sibling)
    _add_dirty_worktree(sibling, worktree)
    # untracked package, present only in the canonical tree:
    (sibling / "widget").mkdir()
    (sibling / "widget" / "__init__.py").write_text("", encoding="utf-8")
    return sibling, worktree, _meta(sibling, worktree)


def _run(*, contract, ctx, git_cwd: str, meta: dict) -> SimpleNamespace:
    state = SimpleNamespace(
        extras={
            "verification_contract": contract,
            "verification_placeholders": ctx,
            "git_cwd": git_cwd,
        },
        project_dir=git_cwd,
        halt=False,
        halt_reason="",
        phase_handoff_request=None,
    )
    state.stop = lambda reason: (
        setattr(state, "halt", True), setattr(state, "halt_reason", reason),
    )
    return SimpleNamespace(state=state, session={"worktree": meta}, _in_gate_hook=False)


# ── (1) sibling source for an isolated repo FAILS provenance ─────────────────


def test_isolated_repo_verified_against_sibling_fails_provenance(
    tmp_path: Path,
) -> None:
    sibling, worktree, meta = _isolated_setup(tmp_path)
    contract = _contract(sibling)

    # The dependency binds to the worktree (NOT the canonical sibling).
    ctx = placeholder_context_for(
        contract,
        checkout=str(worktree),
        project=str(sibling),
        workspace=str(tmp_path),
        run_dir=None,
        worktree=meta,
    )
    assert ctx.isolated_source is not None and ctx.isolated_source.is_isolated
    assert Path(ctx.dependencies["widget-repo"]) == worktree

    # collect_environment_checks enters branch (b); widget imported from the
    # sibling tree is NOT under the worktree -> the provenance check fails (not a
    # vacuous branch-(c) pass).
    checks, _commands = collect_environment_checks(
        worktree, contract=contract, ctx=ctx,
    )
    names = {c["name"] for c in checks}
    assert names == {"widget"}
    assert "environment_provenance" not in names
    assert checks[0]["passed"] is False

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    write_phase_verification_receipt(
        output_dir=run_dir, phase="implement", round=1, cwd=worktree,
        contract=contract, ctx=ctx,
    )
    assert any(
        f.check == "widget" and f.phase == "implement"
        for f in environment_provenance_failures(run_dir)
    )

    base = classify_required_receipts(
        contract, run_dir, ctx, checkout=str(worktree),
    )
    overlaid = apply_environment_provenance(base, contract, run_dir)
    assert overlaid["unit"].status == "failed"

    summary = build_final_acceptance_readiness(contract, run_dir, ctx)
    assert "unit" in summary.required_failed


# ── (1b) CORE checkout verified against the sibling FAILS provenance ─────────
#
# The original ADR 0112 core false-green: a core repo (carrying ``pipeline/``)
# whose verification receipt was written with ``cwd`` at the canonical sibling
# (a clean tree) took branch (a) and matched the sibling's OWN ``pipeline`` —
# passing vacuously. With the fail-closed expected-tree binding, branch (a)
# expects the worktree's ``pipeline`` so a sibling cwd mismatches and FAILS.


def _core_contract() -> VerificationContract:
    """A minimal declared contract with no dependency/env, so
    ``collect_environment_checks`` takes branch (a) purely on the presence of a
    local ``pipeline/__init__.py``."""
    contract = VerificationContract.from_plugin(PluginConfig(work_mode="fast"))
    assert contract is not None
    return contract


def _core_setup(tmp_path: Path) -> tuple[Path, Path, dict]:
    """Canonical sibling + isolated worktree, EACH carrying ``pipeline/__init__.py``.

    Mirrors an orcho-core checkout: branch (a) fires on the local ``pipeline/``
    in either tree, so only the fail-closed expected-tree binding tells the
    worktree apart from the clean sibling.
    """
    sibling = tmp_path / "canonical" / "orcho-core"
    worktree = tmp_path / "worktrees" / "wt_core" / "checkout"
    for root in (sibling, worktree):
        (root / "pipeline").mkdir(parents=True)
        (root / "pipeline" / "__init__.py").write_text("", encoding="utf-8")
    return sibling, worktree, _meta(sibling, worktree)


def test_core_checkout_verified_against_sibling_fails_provenance(
    tmp_path: Path,
) -> None:
    sibling, worktree, meta = _core_setup(tmp_path)
    contract = _core_contract()
    ctx = placeholder_context_for(
        contract,
        checkout=str(worktree),
        project=str(sibling),
        workspace=str(tmp_path),
        run_dir=None,
        worktree=meta,
    )
    assert ctx.isolated_source is not None and ctx.isolated_source.is_isolated

    # A receipt cwd at the CANONICAL SIBLING imports the sibling's pipeline, which
    # is NOT the worktree's expected tree -> provenance FAIL, not a self-match.
    checks, _commands = collect_environment_checks(
        sibling, contract=contract, ctx=ctx,
    )
    assert {c["name"] for c in checks} == {"pipeline_import"}
    assert checks[0]["passed"] is False
    assert Path(str(checks[0]["expected"])) == worktree / "pipeline" / "__init__.py"

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    write_phase_verification_receipt(
        output_dir=run_dir, phase="implement", round=1, cwd=sibling,
        contract=contract, ctx=ctx,
    )
    assert any(
        f.check == "pipeline_import" and f.phase == "implement"
        for f in environment_provenance_failures(run_dir)
    )


def test_core_checkout_verified_against_worktree_passes(tmp_path: Path) -> None:
    sibling, worktree, meta = _core_setup(tmp_path)
    contract = _core_contract()
    ctx = placeholder_context_for(
        contract,
        checkout=str(worktree),
        project=str(sibling),
        workspace=str(tmp_path),
        run_dir=None,
        worktree=meta,
    )
    # Control: a receipt cwd at the worktree (the bound tree) matches expected.
    checks, _commands = collect_environment_checks(
        worktree, contract=contract, ctx=ctx,
    )
    assert {c["name"] for c in checks} == {"pipeline_import"}
    assert checks[0]["passed"] is True

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    write_phase_verification_receipt(
        output_dir=run_dir, phase="implement", round=1, cwd=worktree,
        contract=contract, ctx=ctx,
    )
    assert environment_provenance_failures(run_dir) == ()


# ── (2) preflight aborts BEFORE implement on a sibling source ────────────────


def test_preflight_aborts_before_implement_on_sibling_source(
    tmp_path: Path,
) -> None:
    sibling, worktree, meta = _isolated_setup(tmp_path)
    contract = _contract(sibling)
    ctx = placeholder_context_for(
        contract,
        checkout=str(worktree),
        project=str(sibling),
        workspace=str(tmp_path),
        run_dir=None,
        worktree=meta,
    )
    run = _run(contract=contract, ctx=ctx, git_cwd=str(worktree), meta=meta)

    gate_repair.evaluate_isolated_source_preflight(run, "implement")

    assert run.state.halt is True
    assert gate_repair.ISOLATED_SOURCE_PREFLIGHT_HALT in run.state.halt_reason


# ── (3) ordinary single-checkout run is unaffected ──────────────────────────


def test_single_checkout_run_is_unaffected(tmp_path: Path) -> None:
    # No isolation: checkout == project == the canonical repo with widget present.
    sibling = tmp_path / "canonical" / "widget-repo"
    _init_repo(sibling)
    (sibling / "widget").mkdir()
    (sibling / "widget" / "__init__.py").write_text("", encoding="utf-8")
    contract = _contract(sibling)

    ctx = placeholder_context_for(
        contract,
        checkout=str(sibling),
        project=str(sibling),
        workspace=str(tmp_path),
        run_dir=None,
    )
    # Ambient fallback: no isolated source, dependency stays the sibling.
    assert ctx.isolated_source is None
    assert Path(ctx.dependencies["widget-repo"]) == sibling

    # widget imports from the sibling, which IS the bound dependency -> passes.
    checks, _commands = collect_environment_checks(
        sibling, contract=contract, ctx=ctx,
    )
    assert {c["name"] for c in checks} == {"widget"}
    assert checks[0]["passed"] is True

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    write_phase_verification_receipt(
        output_dir=run_dir, phase="implement", round=1, cwd=sibling,
        contract=contract, ctx=ctx,
    )
    assert environment_provenance_failures(run_dir) == ()

    # The preflight does not interfere with a single-checkout run.
    run = _run(
        contract=contract, ctx=ctx, git_cwd=str(sibling),
        meta={"isolation": "off", "path": str(sibling),
              "source_repo_path": str(sibling)},
    )
    gate_repair.evaluate_isolated_source_preflight(run, "implement")
    assert run.state.halt is False


# ── (T2) resolver reads the participant set ─────────────────────────────────


def test_participant_sibling_source_fails_closed(tmp_path: Path) -> None:
    """A participant READ FROM THE SET whose isolated checkout collapses onto the
    canonical sibling fails closed (IsolatedSourceError) — never a silent fallback
    to the clean sibling tree (ADR 0112 §3, fail-closed contract A)."""
    from pipeline.engine.worktree_source import (
        IsolatedSourceError,
        resolve_isolated_repo_source,
    )
    from pipeline.participants import ParticipantSet

    sibling = tmp_path / "canonical" / "widget-repo"
    sibling.mkdir(parents=True)

    pset = ParticipantSet(isolation="per_run")
    pset.add_provisional(
        alias="primary", repo=str(sibling), delivery_target=str(sibling),
    )
    # Degenerate bind: the bound editable checkout points back at the sibling.
    pset.bind_editable_checkout("primary", str(sibling))

    isolated = pset.isolated_source_for("primary")
    assert isolated is not None and isolated.is_declared
    with pytest.raises(IsolatedSourceError) as exc:
        resolve_isolated_repo_source(
            repo_name="widget-repo", candidate=str(sibling), isolated=isolated,
        )
    assert "widget-repo" in str(exc.value)
    assert str(sibling) in str(exc.value)


def test_multi_participant_set_resolves_by_repo_identity(tmp_path: Path) -> None:
    """A threaded multi-participant set must redirect THIS call's repo through its
    OWN participant — not the set's first element. The target repo is the SECOND
    member: its dependency must bind to the second participant's isolated checkout
    and its isolated source must carry the second worktree, never the first
    (the false-green the repo-identity lookup closes, ADR 0112 §3)."""
    from pipeline.participants import ParticipantSet

    first_repo = tmp_path / "canonical" / "first-repo"
    first_wt = tmp_path / "worktrees" / "wt_first" / "checkout"
    second_repo = tmp_path / "canonical" / "second-repo"
    second_wt = tmp_path / "worktrees" / "wt_second" / "checkout"
    for p in (first_repo, first_wt, second_repo, second_wt):
        p.mkdir(parents=True)

    pset = ParticipantSet(isolation="per_run")
    pset.add_provisional(
        alias="first", repo=str(first_repo), delivery_target=str(first_repo),
    )
    pset.add_provisional(
        alias="second", repo=str(second_repo), delivery_target=str(second_repo),
    )
    pset.bind_editable_checkout("first", str(first_wt))
    pset.bind_editable_checkout("second", str(second_wt))

    # The verify call targets the SECOND repo (not the set's first element); its
    # declared dependency names that same canonical repo.
    contract = VerificationContract.from_plugin(PluginConfig(
        dependency_repos={"dep": {"path": str(second_repo)}},
    ))
    assert contract is not None
    ctx = placeholder_context_for(
        contract,
        checkout=str(second_wt),
        project=str(second_repo),
        workspace=str(tmp_path),
        run_dir=None,
        participant_set=pset,
    )

    # Resolved through the SECOND participant: isolated source + dependency bind
    # land on the second worktree, never leaking the first participant's source.
    assert ctx.isolated_source is not None and ctx.isolated_source.is_isolated
    assert Path(ctx.isolated_source.worktree_path) == second_wt
    assert Path(ctx.isolated_source.source_repo_path) == second_repo
    assert Path(ctx.dependencies["dep"]) == second_wt


def test_single_checkout_parity_no_isolated_source(tmp_path: Path) -> None:
    """Parity: checkout == project derives no isolated source through the set —
    byte-identical to the pre-migration single-checkout resolution."""
    repo = tmp_path / "repo"
    repo.mkdir()
    contract = _core_contract()
    ctx = placeholder_context_for(
        contract,
        checkout=str(repo),
        project=str(repo),
        workspace=str(tmp_path),
        run_dir=None,
    )
    assert ctx.isolated_source is None
