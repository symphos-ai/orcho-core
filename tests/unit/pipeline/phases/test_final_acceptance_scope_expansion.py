# SPDX-License-Identifier: Apache-2.0
"""F2 — final_acceptance scope-expansion gate (deterministic out-of-plan classifier).

Covers the I/O-facing facade
(:func:`pipeline.phases.builtin.review_support._scope_expansion_assessment`,
which gathers durable artefacts and calls the pure T1 classifier) and the
handler integration:

* a small verified out-of-plan companion (build / fixture) → ``notice``, no
  forced rejection;
* public wire without paired alignment, persistence, no-explanation,
  no-verification, destructive/large diffs → ``risk`` / ``blocker`` (never
  ``notice``); blockers force REJECTED;
* SDK reconciliation of an already-public invariant under a green guard →
  ``notice`` with evidence, no forced rejection;
* the assessment is written to the canonical durable path
  ``phase_log['final_acceptance']['scope_expansion']`` only, and only when
  there are out-of-plan files (byte-identical entry otherwise).

These exercise a real throwaway git repo + receipts, so they carry the same
filesystem cost as the neighbouring backstop tests.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from pipeline.evidence.verification_receipt import write_command_receipt
from pipeline.phases.builtin import default_registry
from pipeline.phases.builtin.review_support import _scope_expansion_assessment
from pipeline.plan_parser import ParsedPlan
from pipeline.plugins import PluginConfig
from pipeline.runtime import PipelineState
from pipeline.verification_contract import (
    PlaceholderContext,
    VerificationContract,
)

# ── git + contract + state harness ───────────────────────────────────────────


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "baseline.txt").write_text("seed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "baseline")


def _commit_file(repo: Path, rel: str, content: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"add {rel}")


def _write_untracked(repo: Path, rel: str, content: str = "x\n") -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    # Intent-to-add so a new file under a fresh subdirectory is reported by git
    # at its full path (``git status`` collapses a wholly-untracked directory to
    # ``dir/``) and its content surfaces in ``git diff``.
    _git(repo, "add", "-N", rel)


def _contract() -> VerificationContract:
    contract = VerificationContract.from_plugin(PluginConfig(
        work_mode="pro",
        verification={
            "commands": {
                "test": {"run": ["pytest", "-q"]},
                "lint": {"run": ["ruff", "check", "."]},
                "schema": {"run": ["python", "check_schema.py"]},
            },
            "required": ["test", "lint", "schema"],
            "schedule": [
                {"before_delivery": True, "policy": "require",
                 "commands": ["test", "lint", "schema"]},
            ],
        },
    ))
    assert contract is not None
    return contract


def _passing_receipt(command: str, argv: list[str], checkout: str) -> dict[str, Any]:
    return {
        "kind": "verification_command",
        "command": command,
        "env": "",
        "cwd": checkout,
        "placeholders": {"checkout": checkout, "project": checkout},
        "argv": argv,
        "env_overrides": {},
        "assertions": [],
        "exit_code": 0,
        "duration_s": 0.1,
        "stdout_tail": "",
        "stderr_tail": "",
        "log_path": None,
        "parity": "absolute",
        "detail": "",
        "git": {
            "checkout_head": None,
            "baseline_head": None,
            "changed_files_fingerprint": None,
        },
        "dependencies": [],
    }


_ARGV = {"test": ["pytest", "-q"], "lint": ["ruff", "check", "."],
         "schema": ["python", "check_schema.py"]}


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
    def __init__(self, payload: str | None = None):
        self._payload = payload or _approved_release()
        self.model = "fake-release-reviewer"
        self.session_id: str | None = None
        self.captured = ""

    def invoke(self, prompt: str, cwd: str, **kwargs) -> str:
        self.captured = prompt
        return self._payload


class _StubPhaseConfig:
    def __init__(self, final_acceptance_agent: Any) -> None:
        self.final_acceptance_agent = final_acceptance_agent


def _state(
    tmp_path: Path,
    *,
    repo: Path,
    present: tuple[str, ...] = (),
    plan_owned: tuple[str, ...] = ("src/in_scope.py",),
    implement_output: str = "",
    reviewer: _FakeReleaseReviewer | None = None,
    operating_mode: str | None = None,
) -> PipelineState:
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    for command in present:
        write_command_receipt(
            output_dir=run_dir,
            result=_passing_receipt(command, _ARGV[command], str(repo)),
        )
    extras: dict[str, Any] = {
        "run_id": "20260627_000000",
        "git_cwd": str(repo),
        "verification_contract": _contract(),
        "verification_placeholders": PlaceholderContext(
            checkout=str(repo), project=str(repo),
        ),
    }
    # ADR 0112 §5: the sanction route reads the run's OperatingMode from the
    # existing runtime substrate (state.extras['operating_mode']). Absent → the
    # conservative FAST posture, matching _operating_mode_for_state.
    if operating_mode is not None:
        extras["operating_mode"] = operating_mode
    st = PipelineState(
        task="t", project_dir=str(repo), plugin=PluginConfig(),
        phase_config=_StubPhaseConfig(reviewer or _FakeReleaseReviewer()),
        extras=extras,
    )
    st.output_dir = run_dir
    st.dry_run = False
    st.parsed_plan = ParsedPlan(subtasks=(), source="json", owned_files=plan_owned)
    if implement_output:
        st.phase_log["implement"] = {"output": implement_output}
    return st


def _run(state: PipelineState) -> PipelineState:
    return default_registry().get("final_acceptance")(state)


# ── facade: the status matrix (a–д) ──────────────────────────────────────────


class TestScopeExpansionFacade:
    def test_public_wire_without_alignment_is_blocker(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        _write_untracked(repo, "sdk/new_wire.py", "X = 1\n")
        state = _state(tmp_path, repo=repo, present=("test", "lint", "schema"))

        assessment = _scope_expansion_assessment(state)

        assert assessment.has_blocker is True
        blocker = assessment.blockers[0]
        assert blocker.path == "sdk/new_wire.py"
        assert blocker.category == "public_wire"
        assert "no-paired-alignment" in blocker.evidence

    def test_persistence_out_of_plan_is_blocker(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        _write_untracked(repo, "storage/cache.py", "y = 2\n")
        state = _state(tmp_path, repo=repo, present=("test", "lint", "schema"))

        assessment = _scope_expansion_assessment(state)

        assert [b.category for b in assessment.blockers] == ["persistence"]

    def test_no_explanation_is_risk_not_notice(self, tmp_path: Path) -> None:
        # build gate green, but the file is not mentioned in implement evidence.
        repo = tmp_path / "repo"
        _init_repo(repo)
        _write_untracked(repo, "package-lock.json", "{}\n")
        state = _state(tmp_path, repo=repo, present=("lint",))

        assessment = _scope_expansion_assessment(state)

        assert not assessment.notices
        assert [r.path for r in assessment.risks] == ["package-lock.json"]
        assert assessment.has_blocker is False

    def test_no_verification_is_not_notice(self, tmp_path: Path) -> None:
        # explained, but the build gate is NOT green (no lint receipt).
        repo = tmp_path / "repo"
        _init_repo(repo)
        _write_untracked(repo, "package-lock.json", "{}\n")
        state = _state(
            tmp_path, repo=repo, present=(),
            implement_output="Regenerated package-lock.json after dependency bump.",
        )

        assessment = _scope_expansion_assessment(state)

        assert not assessment.notices
        assert [r.category for r in assessment.risks] == ["build"]

    def test_verified_explained_build_companion_is_notice(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        _write_untracked(repo, "package-lock.json", "{}\n")
        state = _state(
            tmp_path, repo=repo, present=("lint",),
            implement_output="Regenerated package-lock.json after dependency bump.",
        )

        assessment = _scope_expansion_assessment(state)

        assert [n.path for n in assessment.notices] == ["package-lock.json"]
        assert assessment.has_blocker is False

    def test_sdk_reconciliation_already_public_is_notice(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        _commit_file(
            repo, "sdk/payloads.py",
            "from dataclasses import dataclass\n\n\n@dataclass\nclass P:\n    x: int\n",
        )
        # Restore the frozen/slots invariant on the already-exported dataclass.
        (repo / "sdk/payloads.py").write_text(
            "from dataclasses import dataclass\n\n\n"
            "@dataclass(frozen=True, slots=True)\nclass P:\n    x: int\n",
        )
        state = _state(tmp_path, repo=repo, present=("schema",))

        assessment = _scope_expansion_assessment(state)

        assert not assessment.has_blocker
        notices = [n.path for n in assessment.notices]
        assert "sdk/payloads.py" in notices
        item = next(n for n in assessment.notices if n.path == "sdk/payloads.py")
        assert any("sdk-reconciliation" in e for e in item.evidence)


# ── handler integration: verdict + canonical durable write ───────────────────


class TestScopeExpansionHandler:
    @pytest.mark.parametrize("mode", ["fast", "pro", "governed"])
    def test_genuine_safety_halts_in_every_mode(
        self, tmp_path: Path, mode: str,
    ) -> None:
        # ADR 0112 §5: a genuine-safety class (here persistence) is HALT_WAIVER in
        # EVERY mode, including fast — it forces REJECTED and is never silently
        # auto-sanctioned. All required receipts present → the required-receipt
        # backstop is inert, so the REJECTED verdict is purely the genuine-safety
        # scope-expansion halt.
        repo = tmp_path / "repo"
        _init_repo(repo)
        _write_untracked(repo, "storage/cache.py", "y = 2\n")
        state = _state(
            tmp_path, repo=repo, present=("test", "lint", "schema"),
            operating_mode=mode,
        )

        entry = _run(state).phase_log["final_acceptance"]

        assert entry["approved"] is False
        assert entry["verdict"] == "REJECTED"
        assert entry["ship_ready"] is False
        assert any(
            "storage/cache.py" in str(g.get("risk", ""))
            for g in entry["verification_gaps"]
        )
        # canonical durable evidence on the single source-of-truth path.
        scope = entry["scope_expansion"]
        assert scope["has_blocker"] is True
        assert scope["counts"]["blocker"] == 1
        assert scope["items"][0]["status"] == "scope_expansion_blocker"
        # the mode-projected sanction is recorded and forces rejection.
        sanction = entry["scope_expansion_sanction"]
        assert sanction["operating_mode"] == mode
        assert sanction["forces_rejected"] is True
        assert "storage/cache.py" in sanction["halt_paths"]
        # scope evidence reached the reviewer prompt too.
        assert "Scope expansion blocker:" in state.phase_config.final_acceptance_agent.captured

    @pytest.mark.parametrize(
        ("mode", "expect_handoff"),
        [("fast", False), ("pro", True), ("governed", True)],
    )
    def test_benign_blocker_routes_by_mode_not_rejected(
        self, tmp_path: Path, mode: str, expect_handoff: bool,
    ) -> None:
        # A benign (non-genuine-safety) blocker — an unaligned public-wire change —
        # must NOT hard-REJECT: fast auto-continues, pro/governed route through a
        # phase-handoff (recorded), but the verdict stays the reviewer's APPROVED
        # in every mode (no silent reject).
        repo = tmp_path / "repo"
        _init_repo(repo)
        _write_untracked(repo, "sdk/new_wire.py", "X = 1\n")
        state = _state(
            tmp_path, repo=repo, present=("test", "lint", "schema"),
            operating_mode=mode,
        )

        entry = _run(state).phase_log["final_acceptance"]

        assert entry["verdict"] == "APPROVED"
        assert entry["approved"] is True
        # the classifier still records the blocker fact …
        assert entry["scope_expansion"]["has_blocker"] is True
        # … but the route is mode-projected, not a forced rejection.
        sanction = entry["scope_expansion_sanction"]
        assert sanction["operating_mode"] == mode
        assert sanction["forces_rejected"] is False
        assert sanction["needs_phase_handoff"] is expect_handoff
        if expect_handoff:
            assert "sdk/new_wire.py" in sanction["handoff_paths"]

    def test_notice_does_not_force_rejection(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        _write_untracked(repo, "package-lock.json", "{}\n")
        state = _state(
            tmp_path, repo=repo, present=("test", "lint", "schema"),
            implement_output="Regenerated package-lock.json after dependency bump.",
        )

        entry = _run(state).phase_log["final_acceptance"]

        assert entry["approved"] is True
        assert entry["verdict"] == "APPROVED"
        scope = entry["scope_expansion"]
        assert scope["has_blocker"] is False
        assert scope["counts"]["notice"] == 1
        assert scope["items"][0]["status"] == "scope_expansion_notice"

    def test_sdk_reconciliation_notice_keeps_approval(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        _commit_file(
            repo, "sdk/payloads.py",
            "from dataclasses import dataclass\n\n\n@dataclass\nclass P:\n    x: int\n",
        )
        (repo / "sdk/payloads.py").write_text(
            "from dataclasses import dataclass\n\n\n"
            "@dataclass(frozen=True, slots=True)\nclass P:\n    x: int\n",
        )
        state = _state(tmp_path, repo=repo, present=("test", "lint", "schema"))

        entry = _run(state).phase_log["final_acceptance"]

        assert entry["verdict"] == "APPROVED"
        assert entry["scope_expansion"]["has_blocker"] is False

    def test_in_scope_only_diff_writes_no_scope_evidence(self, tmp_path: Path) -> None:
        # The only uncommitted change is an in-plan owned file → empty assessment
        # → the entry shape stays byte-identical (no scope_expansion key).
        repo = tmp_path / "repo"
        _init_repo(repo)
        _commit_file(repo, "src/in_scope.py", "a = 1\n")
        (repo / "src/in_scope.py").write_text("a = 2\n")
        state = _state(
            tmp_path, repo=repo, present=("test", "lint", "schema"),
            plan_owned=("src/in_scope.py",),
        )

        entry = _run(state).phase_log["final_acceptance"]

        assert entry["verdict"] == "APPROVED"
        assert "scope_expansion" not in entry
        assert "Scope expand" not in state.phase_config.final_acceptance_agent.captured

    @pytest.mark.parametrize("mode", ["fast", "pro", "governed"])
    def test_operator_waiver_gates_scope_expansion_entirely(
        self, tmp_path: Path, mode: str,
    ) -> None:
        # R1 + ADR 0112 §5: an active continue_with_waiver fully disarms the
        # scope-expansion gate in EVERY mode (fast/pro/governed). Even with a
        # genuine-safety out-of-plan file that would otherwise HALT_WAIVER, the
        # prompt/readiness must carry no scope-expansion block and no durable
        # evidence may be written — byte-identical to pre-feature waiver
        # behaviour. The waiver is the single operator escape hatch.
        repo = tmp_path / "repo"
        _init_repo(repo)
        _write_untracked(repo, "storage/cache.py", "y = 2\n")
        state = _state(
            tmp_path, repo=repo, present=("test", "lint", "schema"),
            operating_mode=mode,
        )
        state.extras["phase_handoff_waiver"] = {
            "waiver_text": "operator accepted the residual scope risk",
        }

        entry = _run(state).phase_log["final_acceptance"]

        # Waiver respects the reviewer's APPROVED verdict, with no halt forced.
        assert entry["verdict"] == "APPROVED"
        # No durable evidence on the canonical phase_log path.
        assert "scope_expansion" not in entry
        assert "scope_expansion_sanction" not in entry
        # No scope-expansion block reached the reviewer prompt/readiness.
        assert "Scope expand" not in state.phase_config.final_acceptance_agent.captured


# ── dogfood reproducers: the two forms that previously hard-REJECTed ─────────


class TestScopeExpansionDogfood:
    """The two dogfood forms that the old fixed blocker→REJECTED coupling
    hard-rejected must now continue (or route to handoff) under fast/pro,
    never a silent hard reject."""

    @pytest.mark.parametrize("mode", ["fast", "pro"])
    def test_benign_sdk_init_export_not_hard_reject(
        self, tmp_path: Path, mode: str,
    ) -> None:
        # Form 1: a benign export added to an sdk package __init__ (public_wire
        # category → classified blocker without paired alignment) must not
        # hard-REJECT under fast/pro.
        repo = tmp_path / "repo"
        _init_repo(repo)
        _commit_file(repo, "sdk/__init__.py", '__all__ = ["a"]\n')
        (repo / "sdk/__init__.py").write_text('__all__ = ["a", "b"]\n')
        state = _state(
            tmp_path, repo=repo, present=("test", "lint", "schema"),
            operating_mode=mode,
        )

        entry = _run(state).phase_log["final_acceptance"]

        assert entry["verdict"] == "APPROVED"
        assert entry["scope_expansion_sanction"]["forces_rejected"] is False

    @pytest.mark.parametrize("mode", ["fast", "pro"])
    def test_companion_large_diff_not_hard_reject(
        self, tmp_path: Path, mode: str,
    ) -> None:
        # Form 2: a large companion diff (>= LARGE_DIFF_LINES) on a non
        # genuine-safety file (run_projection.py → "other") is a benign blocker;
        # under fast/pro it continues / routes to handoff, never a hard REJECT.
        repo = tmp_path / "repo"
        _init_repo(repo)
        big = "".join(f"row_{i} = {i}\n" for i in range(260))
        _write_untracked(repo, "pipeline/run_projection.py", big)
        state = _state(
            tmp_path, repo=repo, present=("test", "lint", "schema"),
            operating_mode=mode,
        )

        entry = _run(state).phase_log["final_acceptance"]

        assert entry["verdict"] == "APPROVED"
        sanction = entry["scope_expansion_sanction"]
        assert sanction["forces_rejected"] is False
        # the large diff is classified a blocker fact, just not a forced reject.
        assert entry["scope_expansion"]["has_blocker"] is True


# ── F1 fix: HANDOFF route actually opens a phase-handoff pause ────────────────


class _FakeMetrics:
    def save(self, _output_dir: Path) -> None:  # noqa: D401 — no-op test double
        return None


def _pause_run(state: PipelineState, *, output_dir: Path) -> Any:
    """Minimal real run wrapper for the generic phase-handoff pause tail.

    Carries exactly what ``apply_phase_handoff_pause`` reads (``state`` /
    ``session`` / ``output_dir`` / ``_ckpt`` / ``_metrics`` / ``_presentation``).
    SILENT presentation suppresses the operator warn line; ``_ckpt=None`` keeps
    the test off the checkpoint while ``save_session`` still persists meta.json
    under ``output_dir`` (a ``runs/<run_id>`` dir decide/advice can read).
    """
    from types import SimpleNamespace

    from pipeline.presentation import PresentationPolicy

    return SimpleNamespace(
        state=state,
        output_dir=output_dir,
        session={
            "task": "t",
            "project": state.project_dir,
            "model": "claude-opus-4-8",
            "profile": "feature",
            "status": "running",
            "phases": {},
        },
        _ckpt=None,
        _metrics=_FakeMetrics(),
        _presentation=PresentationPolicy.SILENT,
    )


class TestScopeExpansionHandoffEndToEnd:
    """The HANDOFF route must open a real phase-handoff pause, not just record
    the route in phase_log (review finding F1).

    Drives the REAL final_acceptance handler → REAL generic pause tail
    (``apply_phase_handoff_pause``) → REAL ``phase_handoff_decide`` /
    ``request_handoff_advice``, with no hand-built signal and no manually seeded
    paused run: the handler is the seam that raises the signal.
    """

    @pytest.mark.parametrize("mode", ["pro", "governed"])
    def test_handler_raises_out_of_plan_handoff_request(
        self, tmp_path: Path, mode: str,
    ) -> None:
        # A benign blocker under pro/governed: the handler must set
        # state.phase_handoff_request with the out_of_plan trigger so the runner
        # breaks and the orchestrator pauses (not just record needs_phase_handoff).
        repo = tmp_path / "repo"
        _init_repo(repo)
        _write_untracked(repo, "sdk/new_wire.py", "X = 1\n")
        state = _state(
            tmp_path, repo=repo, present=("test", "lint", "schema"),
            operating_mode=mode,
        )

        out = _run(state)

        signal = out.phase_handoff_request
        assert signal is not None, "HANDOFF route did not open a pause"
        assert signal.trigger == "scope_expansion:out_of_plan"
        assert signal.phase == "final_acceptance"
        # The terminal final_acceptance seam offers no retry: continue /
        # continue_with_waiver (the durable escape hatch) / halt only.
        assert signal.available_actions == (
            "continue", "halt", "continue_with_waiver",
        )
        assert "retry_feedback" not in signal.available_actions

    def test_fast_benign_blocker_raises_no_handoff(self, tmp_path: Path) -> None:
        # fast auto-continues a benign blocker → no pause request raised.
        repo = tmp_path / "repo"
        _init_repo(repo)
        _write_untracked(repo, "sdk/new_wire.py", "X = 1\n")
        state = _state(
            tmp_path, repo=repo, present=("test", "lint", "schema"),
            operating_mode="fast",
        )

        out = _run(state)

        assert out.phase_handoff_request is None

    def test_genuine_safety_rejects_without_raising_handoff(
        self, tmp_path: Path,
    ) -> None:
        # A genuine-safety class halts via the release-gap (REJECTED) path; it
        # must NOT also open a phase-handoff pause (no handoff item, only halt).
        repo = tmp_path / "repo"
        _init_repo(repo)
        _write_untracked(repo, "storage/cache.py", "y = 2\n")
        state = _state(
            tmp_path, repo=repo, present=("test", "lint", "schema"),
            operating_mode="governed",
        )

        out = _run(state)

        assert out.phase_log["final_acceptance"]["verdict"] == "REJECTED"
        assert out.phase_handoff_request is None

    def test_end_to_end_pause_persists_meta_and_accepts_decide_advice(
        self, tmp_path: Path,
    ) -> None:
        from pipeline.project.handoff import apply_phase_handoff_pause
        from sdk import (
            HandoffAdviceResult,
            phase_handoff_decide,
            request_handoff_advice,
        )

        # 1) REAL handler raises the pause for a pro benign blocker.
        repo = tmp_path / "repo"
        _init_repo(repo)
        _write_untracked(repo, "sdk/new_wire.py", "X = 1\n")
        state = _state(
            tmp_path, repo=repo, present=("test", "lint", "schema"),
            operating_mode="pro",
        )
        out = _run(state)
        assert out.phase_handoff_request is not None

        # 2) REAL generic pause tail persists meta.phase_handoff + status into a
        # runs/<run_id> dir decide/advice can address (distinct from the
        # handler's receipts dir).
        runs = tmp_path / "runs"
        runs.mkdir(exist_ok=True)
        run_id = "20260629_120000_scopeexp"
        run_dir = runs / run_id
        run_dir.mkdir()
        run = _pause_run(out, output_dir=run_dir)
        apply_phase_handoff_pause(run)

        meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        assert meta["status"] == "awaiting_phase_handoff"
        assert meta["phase_handoff"]["trigger"] == "scope_expansion:out_of_plan"
        assert meta["phase_handoff"]["phase"] == "final_acceptance"
        handoff_id = meta["phase_handoff"]["id"]

        # 3) REAL advice accepts the opaque trigger and returns a typed result.
        from agents.runtimes._strategy import MockAgentProvider

        advice = request_handoff_advice(
            run_id, handoff_id, runs_dir=runs, cwd=None,
            provider=MockAgentProvider(),
        )
        assert isinstance(advice, HandoffAdviceResult)
        assert advice.recommended_action

        # 4) REAL decide applies the operator's sanction.
        decision = phase_handoff_decide(
            run_id, handoff_id, "continue",
            note="operator sanctioned the scope expansion",
            runs_dir=runs, cwd=None,
        )
        assert decision.action == "continue"
        meta_after = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        assert meta_after["phase_handoff"]["trigger"] == "scope_expansion:out_of_plan"
