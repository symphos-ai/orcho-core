"""T2: ADR 0108 provenance is *effective* for an isolated per-run worktree.

The ADR 0112 §3 false-green: a repo run in an isolated per-run worktree whose
verification imports the package from the canonical *sibling* tree (a clean tree
with none of the run's undelivered diff) used to pass vacuously — either via
``collect_environment_checks`` branch (c) or because ``{dependency:X}`` resolved
to the sibling. With the T1 resolver wired into ``placeholder_context_for``, the
``{dependency:X}`` of the isolated repo binds to the worktree, so:

  * ``collect_environment_checks`` enters branch (b) (declared assertions), not
    the vacuous branch (c), and
  * an import from the sibling tree fails the assertion (``passed=False``) ->
    ``verification_environment`` receipt -> ``apply_environment_provenance``
    downgrades the phase-scheduled gate to FAIL -> readiness ``required_failed``
    (the ADR 0108 terminal invariant).

Real on-disk temp dirs + a real import subprocess (no mocks), so the path math
and ``realpath`` normalisation match production.
"""
from __future__ import annotations

from pathlib import Path

from pipeline.evidence.verification_receipt import (
    collect_environment_checks,
    environment_provenance_failures,
    write_phase_verification_receipt,
)
from pipeline.plugins import PluginConfig
from pipeline.verification_contract import (
    VerificationContract,
    placeholder_context_for,
)
from pipeline.verification_readiness import (
    apply_environment_provenance,
    build_final_acceptance_readiness,
    classify_required_receipts,
)


def _isolated_widget_contract(sibling: Path) -> VerificationContract:
    """An isolated repo (``widget-repo``) whose CI env asserts its package import
    lands under the worktree-bound ``{dependency:widget-repo}``, and whose required
    ``unit`` gate is scheduled after ``implement``."""
    contract = VerificationContract.from_plugin(PluginConfig(
        dependency_repos={"widget-repo": {"path": str(sibling)}},
        verification_envs={
            "ci": {
                # PYTHONPATH points at the canonical sibling, so ``widget``
                # imports from the CLEAN tree — the wrong tree under isolation.
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


class TestEffectiveProvenanceForIsolatedRepo:
    def test_import_from_sibling_fails_provenance_to_required_failed(
        self, tmp_path: Path,
    ) -> None:
        # Synthetic isolated worktree + canonical sibling. The package lives ONLY
        # in the sibling (the clean canonical source) — the worktree carries the
        # run's diff and has no widget/ at all.
        sibling = tmp_path / "canonical" / "widget-repo"
        worktree = tmp_path / "worktrees" / "wt_run" / "checkout"
        sibling.mkdir(parents=True)
        worktree.mkdir(parents=True)
        (sibling / "widget").mkdir()
        (sibling / "widget" / "__init__.py").write_text("", encoding="utf-8")

        contract = _isolated_widget_contract(sibling)

        # checkout (worktree) != project (sibling) -> implicit isolation: the
        # widget-repo dependency binds to the worktree, NOT the canonical sibling.
        ctx = placeholder_context_for(
            contract,
            checkout=str(worktree),
            project=str(sibling),
            workspace=str(tmp_path),
            run_dir=None,
        )
        assert ctx.isolated_source is not None
        assert ctx.isolated_source.is_isolated is True
        # DC1: {dependency:X} is bound to the worktree.
        assert Path(ctx.dependencies["widget-repo"]) == worktree

        # DC1: collect_environment_checks enters branch (b) — declared assertions,
        # not the vacuous branch (c) informational check.
        checks, commands = collect_environment_checks(
            worktree, contract=contract, ctx=ctx,
        )
        names = {c["name"] for c in checks}
        assert names == {"widget"}
        assert "environment_provenance" not in names  # not branch (c)
        assert commands
        # DC2: widget imported from the sibling tree is NOT under the worktree the
        # dependency is bound to -> provenance fails.
        assert checks[0]["passed"] is False

        # Persist the implement-phase receipt; the failure must surface.
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        write_phase_verification_receipt(
            output_dir=run_dir, phase="implement", round=1, cwd=worktree,
            contract=contract, ctx=ctx,
        )
        failures = environment_provenance_failures(run_dir)
        assert any(
            f.check == "widget" and f.phase == "implement" for f in failures
        )

        # DC2: apply_environment_provenance downgrades the implement-scheduled
        # gate, and readiness reports it required_failed (ADR 0108 invariant).
        base = classify_required_receipts(
            contract, run_dir, ctx, checkout=str(worktree),
        )
        overlaid = apply_environment_provenance(base, contract, run_dir)
        assert overlaid["unit"].status == "failed"

        summary = build_final_acceptance_readiness(contract, run_dir, ctx)
        assert "unit" in summary.required_failed

    def test_import_from_worktree_passes_provenance(self, tmp_path: Path) -> None:
        # Control: when the package is present in the worktree (the bound tree),
        # the same assertion passes and no provenance failure is recorded.
        sibling = tmp_path / "canonical" / "widget-repo"
        worktree = tmp_path / "worktrees" / "wt_run" / "checkout"
        sibling.mkdir(parents=True)
        worktree.mkdir(parents=True)
        (worktree / "widget").mkdir()
        (worktree / "widget" / "__init__.py").write_text("", encoding="utf-8")

        # PYTHONPATH now points at the worktree (the bound tree).
        contract = VerificationContract.from_plugin(PluginConfig(
            dependency_repos={"widget-repo": {"path": str(sibling)}},
            verification_envs={
                "ci": {
                    "env": {"PYTHONPATH": "{dependency:widget-repo}"},
                    "assertions": [
                        {"import": "widget",
                         "path_under": "{dependency:widget-repo}"},
                    ],
                },
            },
            verification={"default_env": "ci"},
        ))
        assert contract is not None

        ctx = placeholder_context_for(
            contract,
            checkout=str(worktree),
            project=str(sibling),
            workspace=str(tmp_path),
            run_dir=None,
        )
        assert Path(ctx.dependencies["widget-repo"]) == worktree

        checks, _commands = collect_environment_checks(
            worktree, contract=contract, ctx=ctx,
        )
        assert {c["name"] for c in checks} == {"widget"}
        assert checks[0]["passed"] is True

        run_dir = tmp_path / "run"
        run_dir.mkdir()
        write_phase_verification_receipt(
            output_dir=run_dir, phase="implement", round=1, cwd=worktree,
            contract=contract, ctx=ctx,
        )
        assert environment_provenance_failures(run_dir) == ()
