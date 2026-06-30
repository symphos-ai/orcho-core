"""T3: before_phase(implement) isolated-source provenance preflight (ADR 0112 §3).

Drives ``gate_repair.evaluate_isolated_source_preflight`` directly with a
duck-typed run (the same shape ``_on_phase_pre`` passes). The three guarantees:

  * isolated worktree + a verify source that imports from the canonical sibling
    tree -> the run aborts BEFORE implement, with a clear reason;
  * isolated worktree whose source is correctly bound to the worktree -> the
    preflight passes (no halt);
  * a single-checkout run (no isolated worktree) is never given the interrupting
    preflight, and a non-implement phase is a no-op.

Real on-disk temp dirs + a real import subprocess (no mocks), so the path math
and the reused T1/T2 machinery behave as in production.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pipeline.plugins import PluginConfig
from pipeline.project import gate_repair
from pipeline.verification_contract import (
    VerificationContract,
    placeholder_context_for,
)


def _contract(sibling: Path, *, pythonpath: str) -> VerificationContract:
    contract = VerificationContract.from_plugin(PluginConfig(
        dependency_repos={"widget-repo": {"path": str(sibling)}},
        verification_envs={
            "ci": {
                "env": {"PYTHONPATH": pythonpath},
                "assertions": [
                    {"import": "widget", "path_under": "{dependency:widget-repo}"},
                ],
            },
        },
        verification={"default_env": "ci"},
    ))
    assert contract is not None
    return contract


class _State:
    def __init__(self, *, contract, ctx, git_cwd: str) -> None:
        self.extras = {
            "verification_contract": contract,
            "verification_placeholders": ctx,
            "git_cwd": git_cwd,
        }
        self.project_dir = git_cwd
        self.halt = False
        self.halt_reason = ""
        self.phase_handoff_request = None

    def stop(self, reason: str) -> None:
        self.halt = True
        self.halt_reason = reason


def _run(*, contract, ctx, git_cwd: str) -> SimpleNamespace:
    return SimpleNamespace(
        state=_State(contract=contract, ctx=ctx, git_cwd=git_cwd),
        session={},
        _in_gate_hook=False,
    )


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    sibling = tmp_path / "canonical" / "widget-repo"
    worktree = tmp_path / "worktrees" / "wt_run" / "checkout"
    sibling.mkdir(parents=True)
    worktree.mkdir(parents=True)
    return sibling, worktree


def test_sibling_source_aborts_before_implement(tmp_path: Path) -> None:
    sibling, worktree = _dirs(tmp_path)
    # The package lives only in the clean sibling tree; PYTHONPATH points there.
    (sibling / "widget").mkdir()
    (sibling / "widget" / "__init__.py").write_text("", encoding="utf-8")

    contract = _contract(sibling, pythonpath=str(sibling))
    ctx = placeholder_context_for(
        contract, checkout=str(worktree), project=str(sibling),
        workspace=str(tmp_path), run_dir=None,
    )
    assert ctx.isolated_source is not None and ctx.isolated_source.is_isolated
    run = _run(contract=contract, ctx=ctx, git_cwd=str(worktree))

    gate_repair.evaluate_isolated_source_preflight(run, "implement")

    assert run.state.halt is True
    assert gate_repair.ISOLATED_SOURCE_PREFLIGHT_HALT in run.state.halt_reason
    notes = run.state.extras.get("gate_repair_notes") or []
    assert any(
        n["kind"] == gate_repair.ISOLATED_SOURCE_PREFLIGHT_NOTE for n in notes
    )


def test_correct_worktree_source_passes(tmp_path: Path) -> None:
    sibling, worktree = _dirs(tmp_path)
    # The package lives in the worktree (the bound tree); PYTHONPATH points there.
    (worktree / "widget").mkdir()
    (worktree / "widget" / "__init__.py").write_text("", encoding="utf-8")

    contract = _contract(sibling, pythonpath="{dependency:widget-repo}")
    ctx = placeholder_context_for(
        contract, checkout=str(worktree), project=str(sibling),
        workspace=str(tmp_path), run_dir=None,
    )
    assert Path(ctx.dependencies["widget-repo"]) == worktree
    run = _run(contract=contract, ctx=ctx, git_cwd=str(worktree))

    gate_repair.evaluate_isolated_source_preflight(run, "implement")

    assert run.state.halt is False


def test_single_checkout_no_preflight(tmp_path: Path) -> None:
    sibling, _worktree = _dirs(tmp_path)
    (sibling / "widget").mkdir()
    (sibling / "widget" / "__init__.py").write_text("", encoding="utf-8")

    # checkout == project -> no isolated source derived; PYTHONPATH=sibling would
    # otherwise fail the assertion, proving the preflight simply does not run.
    contract = _contract(sibling, pythonpath=str(sibling))
    ctx = placeholder_context_for(
        contract, checkout=str(sibling), project=str(sibling),
        workspace=str(tmp_path), run_dir=None,
    )
    assert ctx.isolated_source is None
    run = _run(contract=contract, ctx=ctx, git_cwd=str(sibling))

    gate_repair.evaluate_isolated_source_preflight(run, "implement")

    assert run.state.halt is False


def test_non_implement_phase_is_noop(tmp_path: Path) -> None:
    sibling, worktree = _dirs(tmp_path)
    (sibling / "widget").mkdir()
    (sibling / "widget" / "__init__.py").write_text("", encoding="utf-8")

    contract = _contract(sibling, pythonpath=str(sibling))
    ctx = placeholder_context_for(
        contract, checkout=str(worktree), project=str(sibling),
        workspace=str(tmp_path), run_dir=None,
    )
    run = _run(contract=contract, ctx=ctx, git_cwd=str(worktree))

    # Even though the source is wrong, a non-implement phase must not abort.
    gate_repair.evaluate_isolated_source_preflight(run, "review_changes")

    assert run.state.halt is False


def test_unbound_worktree_aborts(tmp_path: Path) -> None:
    # Declared isolation (``per_run``) whose worktree path is unresolved (empty):
    # isolation is in force but the verify source cannot bind to a worktree. The
    # preflight must activate on ``is_declared`` and abort fail-closed (review F2),
    # NOT slip past as a single-checkout run.
    from pipeline.engine.worktree_source import IsolatedSource

    sibling, _worktree = _dirs(tmp_path)
    contract = _contract(sibling, pythonpath=str(sibling))
    ctx = placeholder_context_for(
        contract, checkout=str(sibling), project=str(sibling),
        workspace=str(tmp_path), run_dir=None,
    )
    object.__setattr__(
        ctx, "isolated_source",
        IsolatedSource(
            isolation="per_run", worktree_path="", source_repo_path=str(sibling),
        ),
    )
    assert ctx.isolated_source.is_declared is True
    assert ctx.isolated_source.is_isolated is False
    run = _run(contract=contract, ctx=ctx, git_cwd=str(sibling))

    gate_repair.evaluate_isolated_source_preflight(run, "implement")

    assert run.state.halt is True
    assert gate_repair.ISOLATED_SOURCE_PREFLIGHT_HALT in run.state.halt_reason


def test_degenerate_worktree_aborts(tmp_path: Path) -> None:
    # Worktree path degenerates to the canonical sibling (isolation recorded but
    # the checkout points back at the clean source): the T1 resolver branch fails
    # the preflight even with no declared assertion to run.
    from pipeline.engine.worktree_source import IsolatedSource

    sibling, _worktree = _dirs(tmp_path)
    contract = _contract(sibling, pythonpath=str(sibling))
    ctx = placeholder_context_for(
        contract, checkout=str(sibling), project=str(sibling),
        workspace=str(tmp_path), run_dir=None,
    )
    # Force a degenerate isolated source onto the ctx (worktree == sibling).
    object.__setattr__(
        ctx, "isolated_source",
        IsolatedSource(
            isolation="per_run",
            worktree_path=str(sibling),
            source_repo_path=str(sibling),
        ),
    )
    run = _run(contract=contract, ctx=ctx, git_cwd=str(sibling))

    gate_repair.evaluate_isolated_source_preflight(run, "implement")

    assert run.state.halt is True
    assert gate_repair.ISOLATED_SOURCE_PREFLIGHT_HALT in run.state.halt_reason
