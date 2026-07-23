# SPDX-License-Identifier: Apache-2.0
"""ADR 0090 — final_acceptance engine backstop for unproven required gates.

A ``require``-policy delivery gate whose receipt is missing / failed / stale
must surface as a release gap and force a REJECTED verdict, no matter what the
reviewer model emitted. Covers the pure gap builder
(``verification_readiness.required_receipt_gaps``), the handler-side guard
(``review_support._required_receipt_backstop``), and the handler integration
(forced rejection + merged ``verification_gaps``).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from pipeline.evidence.verification_receipt import write_command_receipt
from pipeline.phases.builtin import default_registry
from pipeline.phases.builtin.review_support import _required_receipt_backstop
from pipeline.plugins import PluginConfig
from pipeline.runtime import PipelineState
from pipeline.verification_contract import (
    PlaceholderContext,
    VerificationContract,
)
from pipeline.verification_readiness import required_receipt_gaps
from pipeline.verification_subject import VerificationSubjectAvailable, capture_verification_subject
from tests.fixtures.verification_subject import (
    fake_verification_subject_capture as fake_verification_subject_capture,
)

pytestmark = pytest.mark.usefixtures("fake_verification_subject_capture")


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for argv in (
        ["git", "init", "-q"], ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(argv, cwd=path, check=True)
    (path / "base").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=path, check=True)


def _contract() -> VerificationContract:
    contract = VerificationContract.from_plugin(PluginConfig(
        work_mode="pro",
        verification={
            "commands": {"test": {"run": ["pytest", "-q"]}},
            "required": ["test"],
            "schedule": [
                {"before_delivery": True, "policy": "require",
                 "commands": ["test"]},
            ],
        },
    ))
    assert contract is not None
    return contract


def _passing_receipt(checkout: str) -> dict[str, Any]:
    captured = capture_verification_subject(Path(checkout))
    assert isinstance(captured, VerificationSubjectAvailable)
    return {
        "kind": "verification_command",
        "command": "test",
        "env": "",
        "cwd": checkout,
        "placeholders": {"checkout": checkout, "project": checkout},
        "argv": ["pytest", "-q"],
        "env_overrides": {},
        "assertions": [],
        "exit_code": 0,
        "duration_s": 0.1,
        "stdout_tail": "",
        "stderr_tail": "",
        "log_path": None,
        "parity": "absolute",
        "detail": "",
        "subject": captured,
        "git": {
            "checkout_head": None,
            "baseline_head": None,
            "changed_files_fingerprint": None,
        },
        "dependencies": [],
    }


def _approved_release(summary: str = "Ship-ready.") -> str:
    return json.dumps({
        "verdict":            "APPROVED",
        "ship_ready":         True,
        "short_summary":      summary,
        "release_blockers":   [],
        "verification_gaps":  [],
        "contract_status": {
            "task_contract": "satisfied",
            "interfaces":    "not_applicable",
            "persistence":   "not_applicable",
            "tests":         "sufficient",
        },
    })


class _FakeReleaseReviewer:
    """final_acceptance_agent fake emitting a fixed release payload."""

    def __init__(self, payload: str | None = None):
        self._payload = payload or _approved_release()
        self.model = "fake-release-reviewer"
        self.session_id: str | None = None

    def invoke(
        self,
        prompt: str,
        cwd: str,
        *,
        mutates_artifacts: bool = False,
        continue_session: bool = False,
        attachments: tuple = (),
    ) -> str:
        del prompt, cwd, mutates_artifacts, continue_session, attachments
        return self._payload


class _StubPhaseConfig:
    final_acceptance_agent: Any = None

    def __init__(self, final_acceptance_agent: Any) -> None:
        self.final_acceptance_agent = final_acceptance_agent


def _state(
    tmp_path: Path,
    *,
    contract: VerificationContract | None,
    dry_run: bool = False,
    waiver: bool = False,
) -> PipelineState:
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    extras: dict = {"run_id": "20260613_000000"}
    if contract is not None:
        extras["verification_contract"] = contract
        extras["verification_placeholders"] = PlaceholderContext(
            checkout=str(tmp_path / "wt"), project=str(tmp_path),
        )
    if waiver:
        extras["phase_handoff_waiver"] = {
            "waiver_text": "operator accepted the residual risk",
        }
    st = PipelineState(
        task="t", project_dir="/p", plugin=PluginConfig(),
        phase_config=_StubPhaseConfig(_FakeReleaseReviewer()),
        extras=extras,
    )
    st.output_dir = run_dir
    st.dry_run = dry_run
    return st


# ── required_receipt_gaps (pure) ─────────────────────────────────────────────


class TestRequiredReceiptGaps:
    def test_missing_receipt_yields_gap(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        gaps = required_receipt_gaps(
            _contract(), run_dir,
            PlaceholderContext(checkout=str(tmp_path)),
        )
        assert len(gaps) == 1
        gap = gaps[0]
        assert "'test'" in gap["risk"]
        assert "missing" in gap["risk"]
        assert gap["required_check"] == "pytest -q"
        assert set(gap) == {"risk", "missing_evidence", "required_check"}

    def test_missing_receipt_yields_russian_gap_when_requested(
        self, tmp_path: Path,
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        gaps = required_receipt_gaps(
            _contract(), run_dir,
            PlaceholderContext(checkout=str(tmp_path)),
            language="Russian",
        )
        assert len(gaps) == 1
        gap = gaps[0]
        assert "Обязательный verification gate 'test' не доказан" in gap["risk"]
        assert "receipt отсутствует" in gap["risk"]
        assert "Нет проходящего command receipt" in gap["missing_evidence"]
        assert gap["required_check"] == "pytest -q"

    def test_passing_receipt_yields_no_gap(self, tmp_path: Path) -> None:
        checkout = tmp_path / "checkout"
        _init_repo(checkout)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        write_command_receipt(
            output_dir=run_dir, result=_passing_receipt(str(checkout)),
        )
        gaps = required_receipt_gaps(
            _contract(), run_dir,
            PlaceholderContext(checkout=str(checkout)),
        )
        assert gaps == []

    def test_failed_receipt_yields_gap(self, tmp_path: Path) -> None:
        checkout = tmp_path / "checkout"
        _init_repo(checkout)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        receipt = _passing_receipt(str(checkout))
        receipt["exit_code"] = 1
        write_command_receipt(output_dir=run_dir, result=receipt)
        gaps = required_receipt_gaps(
            _contract(), run_dir,
            PlaceholderContext(checkout=str(checkout)),
        )
        assert len(gaps) == 1
        assert "failed" in gaps[0]["risk"]


# ── _required_receipt_backstop (handler guard) ───────────────────────────────


class TestBackstopGuard:
    def test_missing_receipts_produce_gaps(self, tmp_path: Path) -> None:
        state = _state(tmp_path, contract=_contract())
        assert _required_receipt_backstop(state) != []

    def test_dry_run_is_inert(self, tmp_path: Path) -> None:
        state = _state(tmp_path, contract=_contract(), dry_run=True)
        assert _required_receipt_backstop(state) == []

    def test_no_contract_is_inert(self, tmp_path: Path) -> None:
        state = _state(tmp_path, contract=None)
        assert _required_receipt_backstop(state) == []

    def test_operator_waiver_disarms_backstop(self, tmp_path: Path) -> None:
        state = _state(tmp_path, contract=_contract(), waiver=True)
        assert _required_receipt_backstop(state) == []


# ── handler integration ──────────────────────────────────────────────────────


class TestFinalAcceptanceBackstop:
    def test_unproven_required_gate_forces_rejection(
        self, tmp_path: Path,
    ) -> None:
        """Reviewer said APPROVED, but the required receipt is missing and no
        waiver is active — the engine must reject and surface the gap."""
        state = _state(tmp_path, contract=_contract())

        new = default_registry().get("final_acceptance")(state)

        entry = new.phase_log["final_acceptance"]
        assert entry["approved"] is False
        assert entry["verdict"] == "REJECTED"
        assert entry["ship_ready"] is False
        assert any(
            "'test'" in str(g.get("risk", ""))
            for g in entry["verification_gaps"]
        )
        assert entry["engine_backstop"]["reason"] == "required_receipts_unproven"
        # The run is NOT halted here — blocking is owned by the delivery
        # gate / handoff machinery; the handler records the rejection.
        assert new.last_critique  # critique recorded for downstream surfacing

    def test_engine_backstop_uses_configured_russian_language(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        from types import SimpleNamespace

        from pipeline.phases.builtin.handlers import final_acceptance

        monkeypatch.setattr(
            final_acceptance.AppConfig,
            "load",
            classmethod(lambda _cls: SimpleNamespace(task_language="Russian")),
        )
        state = _state(tmp_path, contract=_contract())

        new = default_registry().get("final_acceptance")(state)

        gap = new.phase_log["final_acceptance"]["verification_gaps"][0]
        assert "Обязательный verification gate 'test' не доказан" in gap["risk"]
        assert "Нет проходящего command receipt" in gap["missing_evidence"]
        assert "Required verification gate" not in new.last_critique

    def test_passing_receipt_keeps_approval(self, tmp_path: Path) -> None:
        state = _state(tmp_path, contract=_contract())
        _init_repo(tmp_path / "wt")
        write_command_receipt(
            output_dir=state.output_dir,
            result=_passing_receipt(str(tmp_path / "wt")),
        )

        new = default_registry().get("final_acceptance")(state)

        entry = new.phase_log["final_acceptance"]
        assert entry["approved"] is True
        assert entry["verdict"] == "APPROVED"
        assert entry["ship_ready"] is True
        assert "engine_backstop" not in entry

    def test_provenance_receipt_is_a_hygiene_warning_not_a_backstop_gap(
        self, tmp_path: Path,
    ) -> None:
        """An exit-0 provenance assertion does not override APPROVED status."""
        state = _state(tmp_path, contract=_contract())
        _init_repo(tmp_path / "wt")
        receipt = _passing_receipt(str(tmp_path / "wt"))
        receipt["assertions"] = [
            {
                "name": "pipeline",
                "kind": "import_path_equals",
                "expected": "/work/pipeline/__init__.py",
                "actual": "/installed/pipeline/__init__.py",
                "passed": False,
            }
        ]
        write_command_receipt(output_dir=state.output_dir, result=receipt)

        new = default_registry().get("final_acceptance")(state)

        entry = new.phase_log["final_acceptance"]
        assert entry["approved"] is True
        assert entry["verdict"] == "APPROVED"
        assert entry["verification_gaps"] == []
        assert "engine_backstop" not in entry

    def test_waiver_keeps_reviewer_verdict(self, tmp_path: Path) -> None:
        state = _state(tmp_path, contract=_contract(), waiver=True)

        new = default_registry().get("final_acceptance")(state)

        entry = new.phase_log["final_acceptance"]
        assert entry["approved"] is True
        assert "engine_backstop" not in entry
