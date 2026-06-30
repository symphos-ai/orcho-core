"""Unit tests for the live verification-gate render model (ADR 0095 / 0106).

Pins the environment-provenance downgrade on the *live* DONE/HALTED surface
(:mod:`pipeline.project.verification_timeline`), the companion to the typed SDK
projection: a required gate scheduled at a phase whose ``verification_environment``
receipt recorded a failed check is forced into the failed residual (=>
``residual_failed`` + ``blocking_residual`` under a ``require`` policy + the shared
``fix`` hint), using the SAME shared rule the SDK projection uses so the two can
never diverge. Healthy/absent provenance keeps the prior semantics intact.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.evidence.verification_receipt import write_verification_receipt
from pipeline.plugins import PluginConfig
from pipeline.project.verification_timeline import (
    _run_level_projection,
    build_verification_timeline,
    render_verification_gate_done_block,
)
from pipeline.verification_contract import (
    VerificationContract,
    placeholder_context_for,
)


def _prov_contract() -> VerificationContract:
    """A contract whose required ``env-provenance`` gate is scheduled at
    after_phase(implement) with a ``require`` policy."""
    plugin = PluginConfig(
        verification_envs={"ci": {}},
        verification={
            "default_env": "ci",
            "required": ["env-provenance"],
            "commands": {"env-provenance": {"run": "echo prov"}},
            "delivery_policy": "require",
            "schedule": [
                {
                    "after_phase": "implement",
                    "policy": "require",
                    "commands": ["env-provenance"],
                },
            ],
        },
    )
    contract = VerificationContract.from_plugin(plugin)
    assert contract is not None
    return contract


def _manual_prov_contract() -> VerificationContract:
    """Same phase-scheduled gate, but ALSO marked manual_only — never blocking."""
    plugin = PluginConfig(
        verification_envs={"ci": {}},
        verification={
            "default_env": "ci",
            "required": ["env-provenance"],
            "commands": {"env-provenance": {"run": "echo prov"}},
            "schedule": [
                {
                    "after_phase": "implement",
                    "policy": "warn",
                    "commands": ["env-provenance"],
                },
                {"manual_only": True, "commands": ["env-provenance"]},
            ],
        },
    )
    contract = VerificationContract.from_plugin(plugin)
    assert contract is not None
    return contract


def _extras(contract: VerificationContract, checkout: Path, run_dir: Path) -> dict:
    return {
        "verification_contract": contract,
        "verification_placeholders": placeholder_context_for(
            contract,
            checkout=str(checkout),
            project=str(checkout),
            workspace=str(run_dir),
            run_dir=str(run_dir),
        ),
    }


def _write_command_receipt(run_dir: Path, command: str, *, exit_code: int = 0) -> None:
    rdir = run_dir / "verification_command_receipts"
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / f"{command}.json").write_text(
        json.dumps({
            "kind": "verification_command",
            "command": command,
            "env": "ci",
            "exit_code": exit_code,
            "assertions": [],
            "detail": "",
            "git": {
                "checkout_head": None,
                "baseline_head": None,
                "changed_files_fingerprint": None,
            },
            "dependencies": [],
        }),
        encoding="utf-8",
    )


def _write_failed_phase_receipt(run_dir: Path) -> Path:
    path = write_verification_receipt(
        output_dir=run_dir,
        phase="implement",
        round=1,
        cwd=run_dir,
        checks=[{
            "name": "pipeline_import",
            "expected": "/abs/checkout/pipeline/__init__.py",
            "actual": "/abs/install/pipeline/__init__.py",
            "passed": False,
        }],
    )
    assert path is not None
    return path


def _write_healthy_phase_receipt(run_dir: Path) -> Path:
    path = write_verification_receipt(
        output_dir=run_dir,
        phase="implement",
        round=1,
        cwd=run_dir,
        checks=[{
            "name": "pipeline_import",
            "expected": "/abs/checkout/pipeline/__init__.py",
            "actual": "/abs/checkout/pipeline/__init__.py",
            "passed": True,
        }],
    )
    assert path is not None
    return path


class TestProvenanceRunLevelProjection:
    def test_failed_provenance_is_failed_residual_and_blocking(
        self, tmp_path: Path,
    ) -> None:
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        contract = _prov_contract()
        # A passing command receipt would otherwise read present/PASS ...
        _write_command_receipt(run_dir, "env-provenance", exit_code=0)
        # ... but the implement phase's environment provenance broke.
        _write_failed_phase_receipt(run_dir)

        projection = _run_level_projection(
            contract, run_dir, _extras(contract, checkout, run_dir),
        )

        assert "env-provenance" in projection.residual_failed
        # The require policy makes it a blocker on the built timeline.
        timeline = build_verification_timeline(
            run_dir=run_dir,
            extras=_extras(contract, checkout, run_dir),
        )
        assert timeline is not None
        assert "env-provenance" in timeline.residual_failed
        assert "env-provenance" in timeline.blocking_residual

    def test_healthy_provenance_does_not_fail_present_gate(
        self, tmp_path: Path,
    ) -> None:
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        contract = _prov_contract()
        _write_command_receipt(run_dir, "env-provenance", exit_code=0)
        _write_healthy_phase_receipt(run_dir)

        projection = _run_level_projection(
            contract, run_dir, _extras(contract, checkout, run_dir),
        )

        # No provenance break -> the present command receipt is not downgraded.
        assert projection.residual_failed == ()

    def test_manual_only_provenance_gate_stays_non_blocking(
        self, tmp_path: Path,
    ) -> None:
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        contract = _manual_prov_contract()
        _write_command_receipt(run_dir, "env-provenance", exit_code=0)
        _write_failed_phase_receipt(run_dir)

        projection = _run_level_projection(
            contract, run_dir, _extras(contract, checkout, run_dir),
        )

        # A manual gate is never escalated by the overlay: it is surfaced as
        # manual-only, never as a failed/blocking residual.
        assert "env-provenance" in projection.manual_only
        assert projection.residual_failed == ()
        timeline = build_verification_timeline(
            run_dir=run_dir,
            extras=_extras(contract, checkout, run_dir),
        )
        assert timeline is not None
        assert "env-provenance" in timeline.manual_only
        assert "env-provenance" not in timeline.blocking_residual


class TestProvenanceDoneBlock:
    def test_done_block_shows_provenance_in_residual_blocking_and_fix(
        self, tmp_path: Path,
    ) -> None:
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        contract = _prov_contract()
        _write_command_receipt(run_dir, "env-provenance", exit_code=0)
        _write_failed_phase_receipt(run_dir)

        timeline = build_verification_timeline(
            run_dir=run_dir,
            extras=_extras(contract, checkout, run_dir),
        )
        block = "\n".join(render_verification_gate_done_block(timeline))

        assert "residual: " in block
        assert "failed=env-provenance" in block
        assert "blocking (require): env-provenance" in block
        # The shared fix hint is printed for the open required deficit.
        assert "fix:" in block
        assert any(
            line.strip().startswith("fix:") and "orcho verify" in line
            for line in block.splitlines()
        )


def test_no_phase_receipt_keeps_present_gate_present(tmp_path: Path) -> None:
    """With no verification_environment receipt at all, the gate keeps its prior
    semantics (a passing command receipt is present, not failed)."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    contract = _prov_contract()
    _write_command_receipt(run_dir, "env-provenance", exit_code=0)

    projection = _run_level_projection(
        contract, run_dir, _extras(contract, checkout, run_dir),
    )

    assert projection.residual_failed == ()
    assert projection.residual_missing == ()
