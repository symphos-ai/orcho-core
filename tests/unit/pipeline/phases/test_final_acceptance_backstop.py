# SPDX-License-Identifier: Apache-2.0
"""ADR 0090 — final_acceptance engine backstop for unproven required gates.

A ``require``-policy delivery gate whose receipt is missing / failed / stale
must surface as a release gap and force a REJECTED verdict, no matter what the
reviewer model emitted. Covers the pure gap builder
(``verification_readiness.required_receipt_gaps``), the handler-side guard
(``review_support._required_receipt_backstop``), and the handler integration
(forced rejection + merged ``verification_gaps``).

ADR 0192 narrows which waiver may excuse that gap. A *general* operator waiver
(a review / plan / implement-incompleteness ``continue_with_waiver``) accepts
reviewer findings; it is not proof that a required command ran. Only a waiver
that names the gate command exactly — an explicit ``gate_command`` or a
``gate:<command>:<round>`` ``handoff_id`` — excuses that command, and only when
its receipt is ``failed`` or ``missing``; ``stale`` is never waivable. This is
the same rule the Stage-6 delivery guard applies, so the closing gate and
delivery can never disagree.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
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
    DEFAULT_VERIFICATION_SUBJECT,
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


#: A general ``continue_with_waiver`` over reviewer findings: it names no gate
#: command, so it proves nothing about any required verification receipt.
_GENERIC_WAIVER: dict[str, Any] = {
    "handoff_id":  "review_changes:1",
    "phase":       "review_changes",
    "waiver_text": "operator accepted the residual risk",
    "findings":    [{"severity": "major", "title": "untested edge case"}],
    "critique":    "the reviewer wanted another test",
    "decided_by":  "operator",
}

#: The implement auto-waiver (ADR 0073/0136): still a general waiver — a
#: non-gate ``handoff_id`` decided by the engine, not gate proof.
_AUTO_IMPLEMENT_WAIVER: dict[str, Any] = {
    "handoff_id":  "implement:2",
    "phase":       "implement",
    "waiver_text": "auto-continued after the repair budget was exhausted",
    "decided_by":  "auto:on_exhausted",
}

#: The precise verification-gate waiver the gate repair loop records.
_GATE_WAIVER: dict[str, Any] = {
    "handoff_id":  "gate:test:1",
    "phase":       "verification",
    "waiver_text": "operator accepted the known test failure",
    "decided_by":  "operator",
}

#: The same precise shape, but for a *different* gate command.
_OTHER_GATE_WAIVER: dict[str, Any] = {
    "handoff_id":  "gate:lint:1",
    "phase":       "verification",
    "waiver_text": "operator accepted the lint failure",
    "decided_by":  "operator",
}


def _stale_subject() -> VerificationSubjectAvailable:
    """A recorded subject that no longer matches the current checkout."""
    return VerificationSubjectAvailable(
        replace(DEFAULT_VERIFICATION_SUBJECT, tree_oid="3" * 40),
    )


def _write_receipt(
    state: PipelineState, *, exit_code: int = 0, stale: bool = False,
) -> None:
    """Write the ``test`` receipt for ``state``'s declared checkout."""
    checkout = Path(state.extras["verification_placeholders"].checkout)
    _init_repo(checkout)
    receipt = _passing_receipt(str(checkout))
    receipt["exit_code"] = exit_code
    if stale:
        receipt["subject"] = _stale_subject()
    write_command_receipt(output_dir=state.output_dir, result=receipt)


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
        #: Every prompt this reviewer was handed, so a test can assert the
        #: operator-waiver block still reaches the model.
        self.prompts: list[str] = []

    def invoke(
        self,
        prompt: str,
        cwd: str,
        *,
        mutates_artifacts: bool = False,
        continue_session: bool = False,
        attachments: tuple = (),
    ) -> str:
        del cwd, mutates_artifacts, continue_session, attachments
        self.prompts.append(prompt)
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
    waiver: dict[str, Any] | None = None,
) -> PipelineState:
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    extras: dict = {"run_id": "20260613_000000"}
    if contract is not None:
        extras["verification_contract"] = contract
        extras["verification_placeholders"] = PlaceholderContext(
            checkout=str(tmp_path / "wt"), project=str(tmp_path),
        )
    if waiver is not None:
        extras["phase_handoff_waiver"] = dict(waiver)
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

    # ── a general operator waiver is not receipt proof (ADR 0192) ────────

    def test_generic_waiver_does_not_excuse_a_missing_receipt(
        self, tmp_path: Path,
    ) -> None:
        state = _state(tmp_path, contract=_contract(), waiver=_GENERIC_WAIVER)
        gaps = _required_receipt_backstop(state)
        assert [g["risk"] for g in gaps] == [
            "Required verification gate 'test' is unproven: receipt missing.",
        ]

    def test_generic_waiver_does_not_excuse_a_failed_receipt(
        self, tmp_path: Path,
    ) -> None:
        state = _state(tmp_path, contract=_contract(), waiver=_GENERIC_WAIVER)
        _write_receipt(state, exit_code=1)
        gaps = _required_receipt_backstop(state)
        assert len(gaps) == 1
        assert "'test'" in gaps[0]["risk"]
        assert "failed" in gaps[0]["risk"]

    def test_generic_waiver_does_not_excuse_a_stale_receipt(
        self, tmp_path: Path,
    ) -> None:
        state = _state(tmp_path, contract=_contract(), waiver=_GENERIC_WAIVER)
        _write_receipt(state, stale=True)
        gaps = _required_receipt_backstop(state)
        assert len(gaps) == 1
        assert "'test'" in gaps[0]["risk"]
        assert "stale" in gaps[0]["risk"]

    def test_implement_auto_waiver_does_not_excuse_a_missing_receipt(
        self, tmp_path: Path,
    ) -> None:
        """The ADR 0073/0136 auto-waiver is a general waiver like any other."""
        state = _state(
            tmp_path, contract=_contract(), waiver=_AUTO_IMPLEMENT_WAIVER,
        )
        assert [g["risk"] for g in _required_receipt_backstop(state)] == [
            "Required verification gate 'test' is unproven: receipt missing.",
        ]

    # ── an exact gate waiver excuses exactly its own failed/missing gate ─────

    def test_exact_gate_waiver_excuses_a_failed_receipt(
        self, tmp_path: Path,
    ) -> None:
        state = _state(tmp_path, contract=_contract(), waiver=_GATE_WAIVER)
        _write_receipt(state, exit_code=1)
        assert _required_receipt_backstop(state) == []

    def test_exact_gate_waiver_excuses_a_missing_receipt(
        self, tmp_path: Path,
    ) -> None:
        state = _state(tmp_path, contract=_contract(), waiver=_GATE_WAIVER)
        assert _required_receipt_backstop(state) == []

    def test_exact_gate_waiver_by_explicit_command_excuses_the_gate(
        self, tmp_path: Path,
    ) -> None:
        """Identity may also come from an explicit ``gate_command`` field."""
        state = _state(
            tmp_path,
            contract=_contract(),
            waiver={
                "handoff_id":   "verification:1",
                "gate_command": "test",
                "waiver_text":  "operator accepted the known test failure",
            },
        )
        assert _required_receipt_backstop(state) == []

    def test_exact_gate_waiver_does_not_excuse_a_stale_receipt(
        self, tmp_path: Path,
    ) -> None:
        """A waiver accepts a known failure, never subject drift."""
        state = _state(tmp_path, contract=_contract(), waiver=_GATE_WAIVER)
        _write_receipt(state, stale=True)
        gaps = _required_receipt_backstop(state)
        assert len(gaps) == 1
        assert "stale" in gaps[0]["risk"]

    def test_a_waiver_for_another_gate_does_not_excuse_this_one(
        self, tmp_path: Path,
    ) -> None:
        state = _state(
            tmp_path, contract=_contract(), waiver=_OTHER_GATE_WAIVER,
        )
        _write_receipt(state, exit_code=1)
        assert [g["required_check"] for g in _required_receipt_backstop(state)] == [
            "pytest -q",
        ]


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

    @pytest.mark.parametrize("receipt_status", ["missing", "failed", "stale"])
    def test_generic_waiver_does_not_buy_a_green_release(
        self, tmp_path: Path, receipt_status: str,
    ) -> None:
        """APPROVED + a general waiver is still a rejection for every way a
        required receipt can be unproven — absent, failing, or stale."""
        state = _state(tmp_path, contract=_contract(), waiver=_GENERIC_WAIVER)
        if receipt_status == "failed":
            _write_receipt(state, exit_code=1)
        elif receipt_status == "stale":
            _write_receipt(state, stale=True)

        new = default_registry().get("final_acceptance")(state)

        entry = new.phase_log["final_acceptance"]
        assert entry["approved"] is False
        assert entry["verdict"] == "REJECTED"
        assert entry["ship_ready"] is False
        assert entry["engine_backstop"]["reason"] == "required_receipts_unproven"
        assert any(
            "'test'" in str(g.get("risk", ""))
            and receipt_status in str(g.get("risk", ""))
            for g in entry["engine_backstop"]["gaps"]
        )
        assert any(
            "'test'" in str(g.get("risk", ""))
            and receipt_status in str(g.get("risk", ""))
            for g in entry["verification_gaps"]
        )

    def test_generic_waiver_still_reaches_the_reviewer_prompt(
        self, tmp_path: Path,
    ) -> None:
        """Narrowing the backstop must not drop the operator-waiver block:
        with the required receipt proven, the waived findings still ship to the
        reviewer and the model's APPROVED verdict stands."""
        state = _state(tmp_path, contract=_contract(), waiver=_GENERIC_WAIVER)
        _write_receipt(state)

        new = default_registry().get("final_acceptance")(state)

        entry = new.phase_log["final_acceptance"]
        assert entry["approved"] is True
        assert entry["verdict"] == "APPROVED"
        assert entry["ship_ready"] is True
        assert "engine_backstop" not in entry
        prompt = state.phase_config.final_acceptance_agent.prompts[-1]
        assert "Operator verdict:" in prompt
        assert _GENERIC_WAIVER["waiver_text"] in prompt

    def test_exact_gate_waiver_keeps_reviewer_verdict(
        self, tmp_path: Path,
    ) -> None:
        """Continuation over a precisely-waived failing gate is preserved."""
        state = _state(tmp_path, contract=_contract(), waiver=_GATE_WAIVER)
        _write_receipt(state, exit_code=1)

        new = default_registry().get("final_acceptance")(state)

        entry = new.phase_log["final_acceptance"]
        assert entry["approved"] is True
        assert entry["verdict"] == "APPROVED"
        assert entry["ship_ready"] is True
        assert "engine_backstop" not in entry

    def test_a_resumed_legacy_waiver_does_not_buy_a_green_release(
        self, tmp_path: Path,
    ) -> None:
        """Fresh-process resume: the waiver arrives through the session
        hydrator rather than the in-process handoff, and is still not proof."""
        from pipeline.project.state_setup import hydrate_state_extras_from_session

        state = _state(tmp_path, contract=_contract())
        hydrate_state_extras_from_session(
            state, {"phase_handoff_waiver": dict(_GENERIC_WAIVER)},
        )
        assert state.extras["phase_handoff_waiver"] == _GENERIC_WAIVER

        new = default_registry().get("final_acceptance")(state)

        entry = new.phase_log["final_acceptance"]
        assert entry["verdict"] == "REJECTED"
        assert entry["engine_backstop"]["reason"] == "required_receipts_unproven"
        assert any(
            "'test'" in str(g.get("risk", ""))
            for g in entry["verification_gaps"]
        )
        # The waiver record itself is untouched, and the backstop never
        # fabricates a receipt to close its own gap.
        assert new.extras["phase_handoff_waiver"] == _GENERIC_WAIVER
        receipts = new.output_dir / "verification_command_receipts"
        assert not receipts.exists() or list(receipts.iterdir()) == []
