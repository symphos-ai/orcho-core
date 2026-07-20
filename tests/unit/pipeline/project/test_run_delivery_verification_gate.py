# SPDX-License-Identifier: Apache-2.0
"""T3 — orchestration wiring of the Stage 6 delivery verification gate (ADR 0083).

Covers the thin glue in ``_PipelineRun._run_commit_delivery``: it builds the
assessment via the module-level ``assess_delivery_verification`` (passing only
``diff_cwd`` + the baseline — never git output), forwards it as
``verification_gate`` to ``resolve_commit_delivery``, and maps a
``verification_blocked`` decision to a ``commit_delivery_verification_blocked``
halt. Plus the finalization banner unit for the new halt reason.

Stubs mirror ``test_run_commit_delivery_order.py``: a ``SimpleNamespace``
``_PipelineRun`` and monkeypatched module attributes. ``assess_delivery_verification``
is patched on ``pipeline.project.run`` (where run.py resolves it at call time);
``resolve_commit_delivery`` / ``apply_commit_delivery`` are patched on
``pipeline.engine.commit_delivery`` (run.py imports them lazily at call time).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import pipeline.engine.commit_delivery as cd
import pipeline.project.run as run_mod
from agents.protocols import SessionMode
from core.io.ansi import C
from pipeline.engine import save_session
from pipeline.engine.commit_delivery import CommitDeliveryDecision
from pipeline.evidence.verification_receipt import (
    environment_provenance_failures,
    write_command_receipt,
    write_verification_receipt,
)
from pipeline.plugins import PluginConfig
from pipeline.project.finalization import _HALT_BANNER_LABELS, _halt_banner
from pipeline.project.run import _PipelineRun
from pipeline.project.state_setup import StateInputs, build_pipeline_state
from pipeline.project.types import PresentationPolicy
from pipeline.verification_contract import (
    PlaceholderContext,
    VerificationContract,
)
from pipeline.verification_delivery import (
    DeliveryVerificationAssessment,
    WaivedGate,
    assess_delivery_verification,
)
from pipeline.verification_dependencies import changed_files_fingerprint
from pipeline.verification_receipt_index import (
    VERIFICATION_PARENT_RUNS_EXTRAS_KEY,
)
from tests.fixtures.verification_subject import (
    fake_verification_subject_capture as fake_verification_subject_capture,
)

pytestmark = pytest.mark.usefixtures("fake_verification_subject_capture")

_INCIDENT_PARENT_RUN_ID = "20260612_213530"
_INCIDENT_CHILD_RUN_ID = "20260612_225347"

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_stub(
    tmp_path: Path,
    *,
    extras: dict,
    session: dict | None = None,
) -> SimpleNamespace:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        output_dir=run_dir,
        session=session if session is not None else {"status": "done"},
        project_path=project_dir,
        parent_run_id=None,
        project_alias=None,
        no_interactive=True,
        worktree_context=None,
        session_ts="20260610_000000",
        phase_config=None,
        state=SimpleNamespace(extras=extras),
        _commit_delivery_baseline=lambda: "SEEDBASE",
        _presentation=PresentationPolicy.SILENT,
    )


def _stub_appconfig(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "pipeline.project.run.config.AppConfig.load",
        lambda: SimpleNamespace(
            commit={"enabled": True, "add_untracked": False},
            task_language="English",
        ),
    )


def _stub_resolve_side_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub only the resolve-side git helpers (upstream of the gate check)."""
    monkeypatch.setattr(cd, "stdio_interactive", lambda: False)
    monkeypatch.setattr(
        cd,
        "_run_owned_patch",
        lambda *_a, **_kw: "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n",
    )
    monkeypatch.setattr(cd, "_changed_paths", lambda *_a, **_kw: ("a.py",))
    monkeypatch.setattr(cd, "_untracked_paths", lambda *_a, **_kw: ())


# ─────────────────────────────────────────────────────────────────────────────
# (a) require + blockers → halted with verification keys
# ─────────────────────────────────────────────────────────────────────────────


def test_require_block_halts_run_with_verification_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_appconfig(monkeypatch)
    _stub_resolve_side_git(monkeypatch)

    assessment = DeliveryVerificationAssessment(
        policy="require",
        required_missing=("test",),
        garbage_paths=(".venv/x",),
    )
    monkeypatch.setattr(
        run_mod,
        "assess_delivery_verification",
        lambda **_kw: assessment,
    )

    stub = _make_stub(
        tmp_path,
        extras={
            "verification_contract": object(),
            "verification_placeholders": object(),
        },
    )
    _PipelineRun._run_commit_delivery(stub, diff_cwd=stub.project_path)

    # Real resolve_commit_delivery saw gate.blocking + non-interactive →
    # verification_blocked; run.py mapped it to the halt.
    assert stub.session["status"] == "halted"
    assert stub.session["halt_reason"] == "commit_delivery_verification_blocked"
    cd_dict = stub.session["commit_delivery"]
    assert cd_dict["status"] == "verification_blocked"
    assert cd_dict["verification_policy"] == "require"
    assert cd_dict["verification_missing"] == ["test"]
    assert cd_dict["generated_garbage_paths"] == [".venv/x"]


# ─────────────────────────────────────────────────────────────────────────────
# (b) no contract → resolve called with verification_gate=None, current behavior
# ─────────────────────────────────────────────────────────────────────────────


def test_no_contract_passes_gate_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_appconfig(monkeypatch)

    recorded: dict = {}

    def _recording_resolve(**kwargs):
        recorded.update(kwargs)
        # Short-circuit to a benign terminal status (no apply, no halt).
        return CommitDeliveryDecision(
            action="none",
            status="no_diff",
            run_id="r1",
            decision_id="r1",
            project_path=kwargs["project_dir"],
            source_path=kwargs["source_worktree"],
            baseline_ref=kwargs["baseline_ref"],
        )

    monkeypatch.setattr(cd, "resolve_commit_delivery", _recording_resolve)

    stub = _make_stub(tmp_path, extras={})  # no verification_contract key
    _PipelineRun._run_commit_delivery(stub, diff_cwd=stub.project_path)

    # Real assess_delivery_verification(contract=None) returns None without git.
    assert "verification_gate" in recorded
    assert recorded["verification_gate"] is None
    # Current behavior: no_diff returns early, run untouched.
    assert stub.session.get("status") == "done"
    assert "commit_delivery" not in stub.session


# ─────────────────────────────────────────────────────────────────────────────
# (c) warn policy → delivery proceeds, run not halted
# ─────────────────────────────────────────────────────────────────────────────


def test_warn_policy_does_not_halt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_appconfig(monkeypatch)
    _stub_resolve_side_git(monkeypatch)

    assessment = DeliveryVerificationAssessment(
        policy="warn",
        required_missing=("test",),
    )
    monkeypatch.setattr(
        run_mod,
        "assess_delivery_verification",
        lambda **_kw: assessment,
    )
    # Stub apply so warn-path delivery (action=approve) does not touch real git.
    monkeypatch.setattr(
        cd,
        "apply_commit_delivery",
        lambda decision, **_kw: replace(decision, status="committed"),
    )

    stub = _make_stub(
        tmp_path,
        extras={"verification_contract": object()},
    )
    _PipelineRun._run_commit_delivery(stub, diff_cwd=stub.project_path)

    assert stub.session["status"] != "halted"
    cd_dict = stub.session["commit_delivery"]
    assert cd_dict["status"] == "committed"
    # warn keys still surfaced on the delivered decision.
    assert cd_dict["verification_policy"] == "warn"
    assert cd_dict["verification_missing"] == ["test"]


# ─────────────────────────────────────────────────────────────────────────────
# (c2) final_acceptance APPROVED + waived broad-non-e2e → delivery proceeds,
#      not commit_delivery_verification_blocked, status stays done, and the
#      persisted meta.json carries the non-wire waived-evidence key.
# ─────────────────────────────────────────────────────────────────────────────


def test_waived_required_gate_delivers_and_persists_meta_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_appconfig(monkeypatch)
    _stub_resolve_side_git(monkeypatch)
    # Stub apply so the (non-blocking) waived path delivers without real git.
    monkeypatch.setattr(
        cd,
        "apply_commit_delivery",
        lambda decision, **_kw: replace(decision, status="committed"),
    )

    # Mirrors assess_delivery_verification's post-T2 output: the waived command
    # is excused (blocking False), present only in the waived sets / waived_gates.
    assessment = DeliveryVerificationAssessment(
        policy="require",
        waived_failed=("broad-non-e2e",),
        waived_gates=(
            WaivedGate(
                gate_command="broad-non-e2e",
                status="failed",
                handoff_id="gate:broad-non-e2e:1",
                waiver_text="accepted: pre-existing failure on this checkout",
            ),
        ),
    )
    assert assessment.blocking is False
    monkeypatch.setattr(
        run_mod,
        "assess_delivery_verification",
        lambda **_kw: assessment,
    )

    session = {
        "status": "done",
        "phases": {
            "final_acceptance": {
                "verdict": "APPROVED",
                "short_summary": "feat: deliver",
            },
        },
    }
    stub = _make_stub(
        tmp_path,
        session=session,
        extras={
            "verification_contract": object(),
            "verification_placeholders": object(),
            # The durable waiver both resume paths leave on extras by delivery.
            "phase_handoff_waiver": {
                "handoff_id": "gate:broad-non-e2e:1",
                "phase": "final_acceptance",
                "waiver_text": "accepted: pre-existing failure on this checkout",
            },
        },
    )
    _PipelineRun._run_commit_delivery(stub, diff_cwd=stub.project_path)

    # Not blocked: no verification halt, status stays done.
    assert stub.session["status"] == "done"
    assert stub.session.get("halt_reason") != "commit_delivery_verification_blocked"
    assert stub.session["commit_delivery"]["status"] == "committed"

    # Persisted (non-wire) evidence: the key lands in meta.json via save_session.
    save_session(stub.output_dir, stub.session)
    meta = json.loads(
        (stub.output_dir / "meta.json").read_text(encoding="utf-8"),
    )
    waived = meta["commit_delivery_verification_waived"]
    assert isinstance(waived, list) and len(waived) == 1
    entry = waived[0]
    assert entry["command"] == "broad-non-e2e"
    assert entry["gate_name"] == "broad-non-e2e"
    assert entry["handoff_id"] == "gate:broad-non-e2e:1"
    assert entry["status"] == "failed"
    assert "pre-existing" in entry["waiver_preview"]
    # The non-wire key never leaks onto the CommitDeliveryDecision wire shape.
    assert "commit_delivery_verification_waived" not in stub.session["commit_delivery"]


def test_no_waiver_omits_meta_evidence_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Byte-identity guard: with no waived gate the evidence key is absent.
    _stub_appconfig(monkeypatch)
    _stub_resolve_side_git(monkeypatch)
    monkeypatch.setattr(
        cd,
        "apply_commit_delivery",
        lambda decision, **_kw: replace(decision, status="committed"),
    )
    assessment = DeliveryVerificationAssessment(policy="warn")
    monkeypatch.setattr(
        run_mod,
        "assess_delivery_verification",
        lambda **_kw: assessment,
    )
    stub = _make_stub(tmp_path, extras={"verification_contract": object()})
    _PipelineRun._run_commit_delivery(stub, diff_cwd=stub.project_path)
    assert "commit_delivery_verification_waived" not in stub.session


def test_redelivery_without_waiver_clears_stale_meta_evidence_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A first waived delivery left commit_delivery_verification_waived on the
    # session; the operator re-ran the gate and re-delivers with no waived gaps.
    # The stale key must be cleared so persisted evidence reflects THIS delivery.
    _stub_appconfig(monkeypatch)
    _stub_resolve_side_git(monkeypatch)
    monkeypatch.setattr(
        cd,
        "apply_commit_delivery",
        lambda decision, **_kw: replace(decision, status="committed"),
    )
    assessment = DeliveryVerificationAssessment(policy="require")
    assert assessment.blocking is False
    assert assessment.waived_gates == ()
    monkeypatch.setattr(
        run_mod,
        "assess_delivery_verification",
        lambda **_kw: assessment,
    )
    stub = _make_stub(tmp_path, extras={"verification_contract": object()})
    # Pre-seed the stale evidence key from a prior waived attempt.
    stub.session["commit_delivery_verification_waived"] = [
        {
            "command": "broad-non-e2e",
            "gate_name": "broad-non-e2e",
            "handoff_id": "gate:broad-non-e2e:1",
            "waiver_preview": "accepted earlier",
            "status": "failed",
        },
    ]
    _PipelineRun._run_commit_delivery(stub, diff_cwd=stub.project_path)

    assert "commit_delivery_verification_waived" not in stub.session
    save_session(stub.output_dir, stub.session)
    meta = json.loads(
        (stub.output_dir / "meta.json").read_text(encoding="utf-8"),
    )
    assert "commit_delivery_verification_waived" not in meta


# ─────────────────────────────────────────────────────────────────────────────
# (d) run.py passes diff_cwd + baseline to assess_delivery_verification
# ─────────────────────────────────────────────────────────────────────────────


def test_assess_called_with_diff_cwd_and_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_appconfig(monkeypatch)

    recorded: dict = {}

    def _recording_assess(**kwargs):
        recorded.update(kwargs)
        return None

    monkeypatch.setattr(run_mod, "assess_delivery_verification", _recording_assess)
    # Keep resolve git-free and terminal.
    monkeypatch.setattr(
        cd,
        "resolve_commit_delivery",
        lambda **kwargs: CommitDeliveryDecision(
            action="none",
            status="no_diff",
            run_id="r1",
            decision_id="r1",
            project_path=kwargs["project_dir"],
            source_path=kwargs["source_worktree"],
            baseline_ref=kwargs["baseline_ref"],
        ),
    )

    stub = _make_stub(
        tmp_path,
        extras={"verification_contract": object()},
    )
    diff_cwd = stub.project_path
    _PipelineRun._run_commit_delivery(stub, diff_cwd=diff_cwd)

    assert recorded["diff_cwd"] == diff_cwd
    assert recorded["baseline_ref"] == "SEEDBASE"
    assert recorded["run_dir"] == stub.output_dir
    assert recorded["extras"] is stub.state.extras


# ─────────────────────────────────────────────────────────────────────────────
# (e) finalization banner for the new halt reason
# ─────────────────────────────────────────────────────────────────────────────


def test_halt_banner_label_present_and_amber() -> None:
    assert "commit_delivery_verification_blocked" in _HALT_BANNER_LABELS
    label, color = _halt_banner("commit_delivery_verification_blocked")
    assert label == "Run halted — verification receipts incomplete"
    assert color == C.YELLOW


# ─────────────────────────────────────────────────────────────────────────────
# (f) state_setup threads the correction follow-up's parent run into extras
#     under the single verification-receipt search key (ADR 0089 / T2)
# ─────────────────────────────────────────────────────────────────────────────


def _state_inputs(tmp_path: Path, **overrides) -> StateInputs:
    base: dict = {
        "task": "do the thing",
        "project_path": tmp_path,
        "plugin": PluginConfig(),
        "phase_config": None,
        "agent_registry": None,
        "output_dir": tmp_path / "run",
        "dry_run": True,
        "session": {},
        "session_ts": "20260613_000000",
        "git_cwd": str(tmp_path),
        "change_handoff": "uncommitted",
        "cross_handoff_text": "",
        "plan_source": "local",
        "handoff_path": None,
        "auto_waiver_allowed": False,
        "followup_seed_count": 0,
        "ckpt": None,
        "attachments": None,
        "session_mode": SessionMode.AUTO,
        "implement_model": "m",
        "repair_model": "m",
        "repair_escalation_model": "m",
        "chain_same_model_only": False,
        "presentation": PresentationPolicy.SILENT,
        "render_phase_outputs": False,
        "from_run_plan_loaded": None,
        "followup_parent_run_id": None,
        "from_run_plan_parent_dir": None,
        "from_run_plan_stripped": (),
    }
    base.update(overrides)
    return StateInputs(**base)


def test_followup_parent_threaded_into_extras_key(tmp_path: Path) -> None:
    parent_dir = tmp_path / "parent_run"
    inputs = _state_inputs(
        tmp_path,
        followup_parent_run_id="20260613_PARENT",
        followup_parent_run_dir=parent_dir,
    )

    state = build_pipeline_state(inputs).state

    assert state.extras[VERIFICATION_PARENT_RUNS_EXTRAS_KEY] == (
        ("20260613_PARENT", str(parent_dir)),
    )


def test_fresh_run_has_no_parent_runs_key(tmp_path: Path) -> None:
    state = build_pipeline_state(_state_inputs(tmp_path)).state
    assert VERIFICATION_PARENT_RUNS_EXTRAS_KEY not in state.extras


def test_parent_id_without_dir_does_not_stamp_key(tmp_path: Path) -> None:
    # Only an id (no run dir) is insufficient — the search source needs a dir.
    inputs = _state_inputs(tmp_path, followup_parent_run_id="20260613_PARENT")
    state = build_pipeline_state(inputs).state
    assert VERIFICATION_PARENT_RUNS_EXTRAS_KEY not in state.extras


# ─────────────────────────────────────────────────────────────────────────────
# (g) incident end-to-end through run.py: the parent key threaded in state.extras
#     reaches the REAL assess_delivery_verification, which inherits the parent's
#     receipts so the child follow-up's gate carries no missing (ADR 0089 / T4)
# ─────────────────────────────────────────────────────────────────────────────


def _git_checkout(tmp_path: Path) -> Path:
    co = tmp_path / "checkout"
    co.mkdir()
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(argv, cwd=co, check=True)
    (co / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=co, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=co, check=True)
    return co


def _git_head(co: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=co,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _parent_receipt(
    run_dir: Path,
    command: str,
    *,
    fingerprint: str,
    head: str,
    exit_code: int = 0,
) -> None:
    from pipeline.verification_subject import capture_verification_subject

    write_command_receipt(
        output_dir=run_dir,
        result={
            "command": command,
            "env": "ci",
            "cwd": "/cwd",
            "placeholders": {"checkout": "/co", "project": "/co"},
            "argv": [command],
            "assertions": [],
            "exit_code": exit_code,
            "duration_s": 0.1,
            "parity": "absolute",
            "detail": "",
            "git": {
                "checkout_head": head,
                "baseline_head": None,
                "changed_files_fingerprint": fingerprint,
            },
            "subject": capture_verification_subject(run_dir.parent / "checkout"),
            "dependencies": [],
        },
    )


def _incident_contract() -> VerificationContract:
    contract = VerificationContract.from_plugin(
        PluginConfig(
            work_mode="governed",
            verification_envs={"ci": {}},
            verification={
                "default_env": "ci",
                "commands": {"test": {"run": "pytest -q {checkout}"}},
                "required": ["test"],
                "delivery_policy": "require",
            },
        )
    )
    assert contract is not None
    return contract


@pytest.mark.git_worktree
def test_incident_parent_receipts_inherited_through_run_py(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_appconfig(monkeypatch)

    co = _git_checkout(tmp_path)
    head, fp = _git_head(co), changed_files_fingerprint(str(co))
    parent_dir = tmp_path / _INCIDENT_PARENT_RUN_ID
    parent_dir.mkdir()
    child_dir = tmp_path / _INCIDENT_CHILD_RUN_ID
    child_dir.mkdir()
    # The parent verified "test" against THIS checkout; the child run is empty.
    _parent_receipt(parent_dir, "test", fingerprint=fp, head=head)

    contract = _incident_contract()
    ctx = PlaceholderContext(checkout=str(co), project=str(co))
    extras = {
        "verification_contract": contract,
        "verification_placeholders": ctx,
        VERIFICATION_PARENT_RUNS_EXTRAS_KEY: ((parent_dir.name, str(parent_dir)),),
    }

    # Let the REAL assess run; capture the gate run.py forwards to resolve.
    captured: dict = {}

    def _recording_resolve(**kwargs):
        captured["gate"] = kwargs["verification_gate"]
        return CommitDeliveryDecision(
            action="none",
            status="no_diff",
            run_id="r1",
            decision_id="r1",
            project_path=kwargs["project_dir"],
            source_path=kwargs["source_worktree"],
            baseline_ref=kwargs["baseline_ref"],
        )

    monkeypatch.setattr(cd, "resolve_commit_delivery", _recording_resolve)

    stub = SimpleNamespace(
        output_dir=child_dir,
        session={"status": "done"},
        project_path=co,
        parent_run_id=None,
        project_alias=None,
        no_interactive=True,
        worktree_context=None,
        session_ts=_INCIDENT_CHILD_RUN_ID,
        phase_config=None,
        state=SimpleNamespace(extras=extras),
        _commit_delivery_baseline=lambda: "HEAD",
        _presentation=PresentationPolicy.SILENT,
    )

    _PipelineRun._run_commit_delivery(stub, diff_cwd=co)

    gate = captured["gate"]
    assert gate is not None
    # The parent key threaded through run.py reached assess: its receipt was
    # inherited for the same checkout, so nothing is missing/stale...
    assert gate.required_missing == ()
    assert gate.required_stale == ()
    assert not gate.blocking
    # ... and the parent run dir is among the searched sources (proving the
    # extras key was actually consulted via run.py, not just the child).
    assert str(parent_dir) in gate.searched_run_dirs


# ─────────────────────────────────────────────────────────────────────────────
# (h) terminal invariant: a non-interactive run whose final_acceptance is
#     APPROVED but whose required gate has a provenance break surfaces that break
#     as a hygiene warning, never as commit-delivery verification blocking.
# ─────────────────────────────────────────────────────────────────────────────


def _phase_scheduled_require_contract() -> VerificationContract:
    """A require gate ``test`` scheduled at ``after_phase(implement)`` — so the
    overlay can link its provenance to the implement phase receipt."""
    contract = VerificationContract.from_plugin(
        PluginConfig(
            work_mode="governed",
            verification_envs={"ci": {}},
            verification={
                "default_env": "ci",
                "commands": {"test": {"run": "pytest -q {checkout}"}},
                "required": ["test"],
                "delivery_policy": "require",
                "schedule": [
                    {"after_phase": "implement", "policy": "require", "commands": ["test"]},
                ],
            },
        )
    )
    assert contract is not None
    return contract


def _write_failed_implement_phase_receipt(run_dir: Path) -> Path:
    """A verification_environment receipt for ``implement`` with a failed check."""
    path = write_verification_receipt(
        output_dir=run_dir,
        phase="implement",
        round=1,
        cwd=run_dir,
        checks=[
            {
                "name": "pipeline_import",
                "expected": "/abs/checkout/pipeline/__init__.py",
                "actual": "/abs/install/pipeline/__init__.py",
                "passed": False,
            }
        ],
    )
    assert path is not None
    return path


@pytest.mark.git_worktree
def test_approved_with_provenance_failed_required_gate_warns_at_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_appconfig(monkeypatch)

    co = _git_checkout(tmp_path)
    head, fp = _git_head(co), changed_files_fingerprint(str(co))
    run_dir = tmp_path / "20260626_000000"
    run_dir.mkdir()
    # A PASSING command receipt for the required gate, valid against THIS
    # checkout (present, not stale) — on its own it would read green ...
    _parent_receipt(run_dir, "test", fingerprint=fp, head=head)
    # ... but the implement phase's environment provenance broke.
    phase_path = _write_failed_implement_phase_receipt(run_dir)

    contract = _phase_scheduled_require_contract()
    ctx = PlaceholderContext(checkout=str(co), project=str(co))
    extras = {
        "verification_contract": contract,
        "verification_placeholders": ctx,
    }

    # Reuse the real assessment helper (no duplicated git logic): the passing
    # command receipt is downgraded by the provenance break → hygiene warning.
    assessment = assess_delivery_verification(
        contract=contract,
        run_dir=run_dir,
        ctx=ctx,
        extras=extras,
        diff_cwd=co,
        baseline_ref="HEAD",
    )
    assert assessment is not None
    assert assessment.required_failed == ("test",)
    assert assessment.blocking is False
    assert assessment.warning_gaps[0].command == "test"

    # final_acceptance APPROVED — the release gate alone would let this ship.
    session = {
        "status": "done",
        "phases": {
            "final_acceptance": {
                "verdict": "APPROVED",
                "short_summary": "feat: deliver",
            },
        },
    }
    stub = SimpleNamespace(
        output_dir=run_dir,
        session=session,
        project_path=co,
        parent_run_id=None,
        project_alias=None,
        no_interactive=True,
        worktree_context=None,
        session_ts="20260626_000000",
        phase_config=None,
        state=SimpleNamespace(extras=extras),
        _commit_delivery_baseline=lambda: "HEAD",
        _presentation=PresentationPolicy.SILENT,
    )

    # Drive the REAL terminal path through run.py.
    _PipelineRun._run_commit_delivery(stub, diff_cwd=co)

    # APPROVED is not halted by a hygiene warning.
    assert stub.session.get("halt_reason") != "commit_delivery_verification_blocked"
    assert stub.session["status"] != "halted"
    if "commit_delivery" in stub.session:
        assert stub.session["commit_delivery"]["status"] != "verification_blocked"

    # Typed provenance evidence is present on disk: the failing check, its
    # expected/actual, and the receipt_path of the verification_environment
    # phase receipt — read via the typed reader, never a terminal banner.
    failures = environment_provenance_failures(run_dir)
    assert len(failures) == 1
    failure = failures[0]
    assert failure.phase == "implement"
    assert failure.check == "pipeline_import"
    assert failure.receipt_path == str(phase_path)
    assert failure.expected == "/abs/checkout/pipeline/__init__.py"
    assert failure.actual == "/abs/install/pipeline/__init__.py"


@pytest.mark.git_worktree
def test_approved_with_test_failure_and_phase_provenance_halts_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_appconfig(monkeypatch)

    co = _git_checkout(tmp_path)
    head, fp = _git_head(co), changed_files_fingerprint(str(co))
    run_dir = tmp_path / "20260626_000001"
    run_dir.mkdir()
    _parent_receipt(run_dir, "test", fingerprint=fp, head=head, exit_code=1)
    _write_failed_implement_phase_receipt(run_dir)

    contract = _phase_scheduled_require_contract()
    ctx = PlaceholderContext(checkout=str(co), project=str(co))
    extras = {
        "verification_contract": contract,
        "verification_placeholders": ctx,
    }
    assessment = assess_delivery_verification(
        contract=contract,
        run_dir=run_dir,
        ctx=ctx,
        extras=extras,
        diff_cwd=co,
        baseline_ref="HEAD",
    )
    assert assessment is not None
    assert assessment.blocking is True
    assert assessment.warning_gaps == ()
    assert assessment.blocking_gaps[0].command == "test"

    stub = SimpleNamespace(
        output_dir=run_dir,
        session={
            "status": "done",
            "phases": {
                "final_acceptance": {
                    "verdict": "APPROVED",
                    "short_summary": "feat: deliver",
                },
            },
        },
        project_path=co,
        parent_run_id=None,
        project_alias=None,
        no_interactive=True,
        worktree_context=None,
        session_ts="20260626_000001",
        phase_config=None,
        state=SimpleNamespace(extras=extras),
        _commit_delivery_baseline=lambda: "HEAD",
        _presentation=PresentationPolicy.SILENT,
    )

    _PipelineRun._run_commit_delivery(stub, diff_cwd=co)

    assert stub.session["status"] == "halted"
    assert stub.session["halt_reason"] == "commit_delivery_verification_blocked"
    assert stub.session["commit_delivery"]["status"] == "verification_blocked"
