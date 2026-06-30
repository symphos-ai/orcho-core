# SPDX-License-Identifier: Apache-2.0
"""Stage 6 verification delivery gate in the engine (ADR 0083).

Covers ``resolve_commit_delivery``'s consumption of a ``DeliveryVerificationAssessment``:
the non-interactive ``require``-block returning ``verification_blocked`` before
any transport git op, the warn path proceeding while surfacing keys, the
``verification_gate=None`` byte-identity guard, and the interactive prompt's
distinct garbage section + fix default. Assessments are constructed directly
from the T1 dataclass — no git, no contract resolution in these tests.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from core.io.git_helpers import create_worktree
from pipeline.engine.commit_delivery import (
    apply_commit_delivery,
    resolve_commit_delivery,
)
from pipeline.verification_delivery import (
    DeliveryVerificationAssessment,
    WaivedGate,
)
from pipeline.verification_policy import GapEntry

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _init_repo(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    for argv in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@orcho.invalid"],
        ["git", "config", "user.name", "Orcho Test"],
        ["git", "config", "commit.gpgsign", "false"],
    ):
        subprocess.run(argv, cwd=repo, check=True)
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return _head(repo)


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
        text=True, check=True,
    ).stdout.strip()


def _status(repo: Path) -> str:
    return subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True,
        text=True, check=True,
    ).stdout.strip()


def _new_worktree(repo: Path, run_dir: Path) -> Path:
    result = create_worktree(
        repo=repo, base_ref=_head(repo),
        target_path=run_dir / "checkout", branch_name="orcho/run/r1",
    )
    assert result.ok, result.error
    return run_dir / "checkout"


def _session(verdict: str = "APPROVED") -> dict:
    return {
        "phases": {
            "final_acceptance": {
                "verdict": verdict,
                "short_summary": "feat: update app",
            },
        },
    }


def _resolve(
    *,
    repo: Path,
    worktree: Path,
    run_dir: Path,
    verification_gate: DeliveryVerificationAssessment | None,
    no_interactive: bool = True,
    session: dict | None = None,
    input_fn=input,
    output_fn=print,
):
    return resolve_commit_delivery(
        project_dir=repo,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="r1",
        session=session or _session(),
        commit_config={"enabled": True, "auto_in_ci": "approve",
                       "add_untracked": True},
        no_interactive=no_interactive,
        verification_gate=verification_gate,
        input_fn=input_fn,
        output_fn=output_fn,
    )


def _with_diff(worktree: Path) -> None:
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")


# Realistic policy-aware assessments — the partition fields (blocking_gaps /
# warning_gaps + policy_by_command) are exactly what ``assess_delivery_verification``
# produces, so the policy-aware lines render as they do in production (T2/T4).


def _require_gate(
    missing: tuple[str, ...] = (),
    *,
    garbage: tuple[str, ...] = (),
    searched_run_dirs: tuple[str, ...] = (),
    suggested_commands: tuple[str, ...] = (),
) -> DeliveryVerificationAssessment:
    return DeliveryVerificationAssessment(
        policy="require",
        required_missing=missing,
        garbage_paths=garbage,
        blocking_gaps=tuple(GapEntry(c, "missing", "require") for c in missing),
        policy_by_command=tuple((c, "require") for c in missing),
        searched_run_dirs=searched_run_dirs,
        suggested_commands=suggested_commands,
    )


def _warn_gate(
    missing: tuple[str, ...] = (),
    *,
    garbage: tuple[str, ...] = (),
    searched_run_dirs: tuple[str, ...] = (),
    suggested_commands: tuple[str, ...] = (),
) -> DeliveryVerificationAssessment:
    return DeliveryVerificationAssessment(
        policy="warn",
        required_missing=missing,
        garbage_paths=garbage,
        warning_gaps=tuple(GapEntry(c, "missing", "warn") for c in missing),
        policy_by_command=tuple((c, "warn") for c in missing),
        searched_run_dirs=searched_run_dirs,
        suggested_commands=suggested_commands,
    )


def _suggest_gate(
    missing: tuple[str, ...] = (),
) -> DeliveryVerificationAssessment:
    return DeliveryVerificationAssessment(
        policy="suggest",
        required_missing=missing,
        warning_gaps=tuple(GapEntry(c, "missing", "suggest") for c in missing),
        policy_by_command=tuple((c, "suggest") for c in missing),
    )


def _manual_only_gate(
    manual: tuple[str, ...] = (),
    *,
    boundary_policy: str = "warn",
) -> DeliveryVerificationAssessment:
    # A gap whose ONLY open commands are manual/operator-only: never a blocker
    # and never a missing-required receipt, but it must stay visible. The boundary
    # policy is irrelevant to the manual-only line (default ``warn`` here).
    return DeliveryVerificationAssessment(
        policy=boundary_policy,
        manual_only_gaps=tuple(
            GapEntry(c, "missing", "manual_only") for c in manual
        ),
        policy_by_command=tuple((c, "manual_only") for c in manual),
    )


def _waived_gate(
    command: str = "broad-non-e2e",
    *,
    status: str = "failed",
    handoff_id: str | None = None,
    waiver_text: str = "accepted: pre-existing failure on this checkout",
) -> DeliveryVerificationAssessment:
    """A require gate whose only required gap was excused by an exact waiver.

    Mirrors what ``assess_delivery_verification`` returns post-T2: the waived
    command is NOT in ``required_*`` / ``blocking_gaps`` (so ``blocking`` is
    False), only in the waived sets + ``waived_gates``.
    """
    return DeliveryVerificationAssessment(
        policy="require",
        waived_failed=(command,) if status == "failed" else (),
        waived_missing=(command,) if status == "missing" else (),
        waived_gates=(
            WaivedGate(
                gate_command=command,
                status=status,
                handoff_id=handoff_id or f"gate:{command}:1",
                waiver_text=waiver_text,
            ),
        ),
        policy_by_command=((command, "require"),),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Non-interactive require-block — no transport side effects
# ─────────────────────────────────────────────────────────────────────────────


class TestRequireBlockNonInteractive:
    def test_missing_receipt_blocks_before_transport(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        old_head = _init_repo(repo)
        run_dir = tmp_path / "run"
        worktree = _new_worktree(repo, run_dir)
        _with_diff(worktree)

        gate = DeliveryVerificationAssessment(
            policy="require", required_missing=("test",),
        )
        decision = _resolve(
            repo=repo, worktree=worktree, run_dir=run_dir, verification_gate=gate,
        )

        assert decision.status == "verification_blocked"
        assert decision.action == "none"
        assert decision.error and "test" in decision.error
        # No transport happened: project checkout HEAD unmoved and clean.
        assert _head(repo) == old_head
        assert _status(repo) == ""
        # apply is a no-op on a non-'pending' decision — still no side effects.
        applied = apply_commit_delivery(decision, run_dir=run_dir)
        assert applied.status == "verification_blocked"
        assert _head(repo) == old_head
        assert _status(repo) == ""
        # No persisted artifact was written for the block.
        assert not (run_dir / "commit_decisions" / "r1.json").exists()

    def test_missing_receipt_blocks_even_with_clean_diff(
        self, tmp_path: Path,
    ) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        worktree = _new_worktree(repo, run_dir)
        # No diff in the worktree on purpose: the require-block must fire
        # BEFORE the no-diff early return, so a clean worktree cannot
        # downgrade missing required receipts to a silent 'no_diff'.

        gate = DeliveryVerificationAssessment(
            policy="require", required_missing=("test",),
        )
        decision = _resolve(
            repo=repo, worktree=worktree, run_dir=run_dir, verification_gate=gate,
        )
        assert decision.status == "verification_blocked"
        assert decision.action == "none"

    def test_blocked_decision_to_dict_has_verification_keys(
        self, tmp_path: Path,
    ) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        worktree = _new_worktree(repo, run_dir)
        _with_diff(worktree)

        gate = DeliveryVerificationAssessment(
            policy="require",
            required_failed=("lint",),
            required_stale=("smoke",),
            garbage_paths=(".venv/x",),
        )
        decision = _resolve(
            repo=repo, worktree=worktree, run_dir=run_dir, verification_gate=gate,
        )
        d = decision.to_dict()
        assert d["status"] == "verification_blocked"
        assert d["verification_policy"] == "require"
        assert d["verification_failed"] == ["lint"]
        assert d["verification_stale"] == ["smoke"]
        assert d["generated_garbage_paths"] == [".venv/x"]

    def test_require_without_blockers_delivers(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        worktree = _new_worktree(repo, run_dir)
        _with_diff(worktree)

        # require policy but nothing missing/failed/stale and no garbage → not
        # blocking → delivery proceeds.
        gate = DeliveryVerificationAssessment(policy="require")
        decision = _resolve(
            repo=repo, worktree=worktree, run_dir=run_dir, verification_gate=gate,
        )
        assert decision.status == "pending"
        assert decision.action == "approve"

    def test_failed_only_require_block_error_carries_verify_hint(
        self, tmp_path: Path,
    ) -> None:
        # A failed-only require block must still name the searched dirs + the
        # exact ``orcho verify`` command — the failed-only banner must not diverge
        # from the readiness / DONE surfaces.
        repo = tmp_path / "repo"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        worktree = _new_worktree(repo, run_dir)
        _with_diff(worktree)

        verify_run = "orcho verify run run-state-unit --run-id r1 --project /proj"
        gate = DeliveryVerificationAssessment(
            policy="require",
            required_failed=("run-state-unit",),
            blocking_gaps=(GapEntry("run-state-unit", "failed", "require"),),
            policy_by_command=(("run-state-unit", "require"),),
            searched_run_dirs=(str(run_dir),),
            suggested_commands=(verify_run,),
        )
        decision = _resolve(
            repo=repo, worktree=worktree, run_dir=run_dir, verification_gate=gate,
        )
        assert decision.status == "verification_blocked"
        err = decision.error or ""
        assert "failed required receipts: run-state-unit" in err
        assert "searched:" in err
        assert verify_run in err

    def test_release_blocked_keeps_priority(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        worktree = _new_worktree(repo, run_dir)
        _with_diff(worktree)

        # Non-interactive + release REJECTED → release path returns first
        # (not_applicable), never reaching the verification block.
        gate = DeliveryVerificationAssessment(
            policy="require", required_missing=("test",),
        )
        decision = _resolve(
            repo=repo, worktree=worktree, run_dir=run_dir, verification_gate=gate,
            session=_session(verdict="REJECTED"),
        )
        assert decision.status == "not_applicable"
        assert decision.error == "release verdict is not APPROVED"


# ─────────────────────────────────────────────────────────────────────────────
# warn / suggest — delivery proceeds, keys surfaced
# ─────────────────────────────────────────────────────────────────────────────


class TestWarnProceeds:
    def test_warn_delivers_and_surfaces_keys(self, tmp_path: Path, capsys) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        worktree = _new_worktree(repo, run_dir)
        _with_diff(worktree)

        printed: list[str] = []
        gate = _warn_gate(("test",), garbage=(".venv/x",))
        decision = _resolve(
            repo=repo, worktree=worktree, run_dir=run_dir, verification_gate=gate,
            output_fn=printed.append,
        )
        # warn never hard-blocks: delivery proceeds.
        assert decision.status == "pending"
        assert decision.action == "approve"
        # Non-interactive warn emits a warning via the observability logger,
        # worded as shipping-allowed-by-policy — never 'missing required'.
        logged = capsys.readouterr().out
        assert "Verification incomplete" in logged
        assert "shipping allowed by policy" in logged
        assert "missing required" not in logged

        delivered = apply_commit_delivery(
            decision, run_dir=run_dir, commit_config={"add_untracked": True},
        )
        assert delivered.status == "committed"
        # Keys travel through _persist onto the delivered decision.
        d = delivered.to_dict()
        assert d["verification_policy"] == "warn"
        assert d["verification_missing"] == ["test"]
        assert d["generated_garbage_paths"] == [".venv/x"]
        # ... and into the persisted audit artifact, not only the returned
        # decision object.
        artifact = json.loads(
            (run_dir / "commit_decisions" / "r1.json").read_text(
                encoding="utf-8",
            ),
        )
        assert artifact["commit_status"] == "committed"
        assert artifact["verification_policy"] == "warn"
        assert artifact["verification_missing"] == ["test"]
        assert artifact["generated_garbage_paths"] == [".venv/x"]
        assert "verification_failed" not in artifact  # empty → omitted
        assert "verification_stale" not in artifact

    def test_require_boundary_warn_gate_surfaces_shipping_allowed(
        self, tmp_path: Path, capsys,
    ) -> None:
        # Boundary policy is ``require``, but THIS gate's effective policy is
        # ``warn`` → not blocking. The non-interactive path must still surface it
        # as shipping-allowed-by-policy, never as a 'missing required' blocker,
        # and delivery proceeds (gating is per-gate, not boundary-policy).
        repo = tmp_path / "repo"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        worktree = _new_worktree(repo, run_dir)
        _with_diff(worktree)

        gate = DeliveryVerificationAssessment(
            policy="require",
            required_missing=("lint",),
            warning_gaps=(GapEntry("lint", "missing", "warn"),),
            policy_by_command=(("lint", "warn"),),
        )
        decision = _resolve(
            repo=repo, worktree=worktree, run_dir=run_dir, verification_gate=gate,
        )
        assert decision.status == "pending"
        assert decision.action == "approve"
        logged = capsys.readouterr().out
        assert "shipping allowed by policy" in logged
        assert "missing required" not in logged


# ─────────────────────────────────────────────────────────────────────────────
# Waived require gate (T3): a precise durable waiver unblocks delivery, but the
# waived gate stays visible in the non-interactive surfacing.
# ─────────────────────────────────────────────────────────────────────────────


class TestWaivedGateUnblocks:
    def test_waived_failed_require_does_not_block(
        self, tmp_path: Path, capsys,
    ) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        worktree = _new_worktree(repo, run_dir)
        _with_diff(worktree)

        gate = _waived_gate("broad-non-e2e", status="failed")
        # Sanity: the assessment itself reports not-blocking (T2 contract).
        assert gate.blocking is False

        decision = _resolve(
            repo=repo, worktree=worktree, run_dir=run_dir, verification_gate=gate,
        )
        # Delivery proceeds — never verification_blocked.
        assert decision.status == "pending"
        assert decision.action == "approve"
        # The waived gate stays visible in the non-interactive surfacing:
        # gate command, the durable handoff id, and the failed/missing status.
        logged = capsys.readouterr().out
        assert "waived required receipts: broad-non-e2e" in logged
        assert "gate:broad-non-e2e:1" in logged
        assert "failed" in logged
        assert "missing required" not in logged

    def test_waived_missing_require_does_not_block(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        worktree = _new_worktree(repo, run_dir)
        _with_diff(worktree)

        gate = _waived_gate("broad-non-e2e", status="missing")
        decision = _resolve(
            repo=repo, worktree=worktree, run_dir=run_dir, verification_gate=gate,
        )
        assert decision.status == "pending"
        assert decision.action == "approve"

    def test_wrong_gate_waiver_still_blocks(self, tmp_path: Path) -> None:
        # A waiver covering a DIFFERENT command must not unblock the real require
        # gap: 'test' is still blocking even though 'broad-non-e2e' is waived.
        repo = tmp_path / "repo"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        worktree = _new_worktree(repo, run_dir)
        _with_diff(worktree)

        gate = DeliveryVerificationAssessment(
            policy="require",
            required_missing=("test",),
            blocking_gaps=(GapEntry("test", "missing", "require"),),
            waived_failed=("broad-non-e2e",),
            waived_gates=(
                WaivedGate(
                    gate_command="broad-non-e2e",
                    status="failed",
                    handoff_id="gate:broad-non-e2e:1",
                    waiver_text="accepted",
                ),
            ),
            policy_by_command=(("test", "require"), ("broad-non-e2e", "require")),
        )
        assert gate.blocking is True
        decision = _resolve(
            repo=repo, worktree=worktree, run_dir=run_dir, verification_gate=gate,
        )
        assert decision.status == "verification_blocked"
        # The real blocker is named; the unrelated waived gate did not excuse it.
        assert "test" in (decision.error or "")

    def test_to_dict_carries_no_waived_keys(self, tmp_path: Path) -> None:
        # Wire/MCP/SDK guard: the waived-gate awareness must NOT leak into the
        # CommitDeliveryDecision wire shape.
        repo = tmp_path / "repo"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        worktree = _new_worktree(repo, run_dir)
        _with_diff(worktree)

        gate = _waived_gate("broad-non-e2e", status="failed")
        decision = _resolve(
            repo=repo, worktree=worktree, run_dir=run_dir, verification_gate=gate,
        )
        d = decision.to_dict()
        for key in (
            "waived_failed", "waived_missing", "waived_gates",
            "commit_delivery_verification_waived",
        ):
            assert key not in d


# ─────────────────────────────────────────────────────────────────────────────
# verification_gate=None — byte-identity guard
# ─────────────────────────────────────────────────────────────────────────────


class TestGateNoneByteIdentical:
    def test_no_new_output_and_no_verification_keys(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        worktree = _new_worktree(repo, run_dir)
        _with_diff(worktree)

        printed: list[str] = []
        decision = _resolve(
            repo=repo, worktree=worktree, run_dir=run_dir, verification_gate=None,
            output_fn=printed.append,
        )
        assert decision.status == "pending"
        # Non-interactive path prints nothing at all (unchanged behavior).
        assert printed == []
        d = decision.to_dict()
        for key in (
            "verification_policy", "verification_missing", "verification_failed",
            "verification_stale", "generated_garbage_paths",
        ):
            assert key not in d


# ─────────────────────────────────────────────────────────────────────────────
# Interactive prompt — garbage section + fix default
# ─────────────────────────────────────────────────────────────────────────────


class _ScriptedInput:
    """Returns queued answers; a bare '' models a plain Enter on the default."""

    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)

    def __call__(self, _prompt: str) -> str:
        return self._answers.pop(0) if self._answers else ""


class TestInteractiveRequireBlock:
    def test_prompt_has_garbage_section_and_fix_default(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        # Force the interactive branch (stdio_interactive() is otherwise False
        # under pytest's captured stdio).
        monkeypatch.setattr(
            "pipeline.engine.commit_delivery.stdio_interactive", lambda: True,
        )
        repo = tmp_path / "repo"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        worktree = _new_worktree(repo, run_dir)
        _with_diff(worktree)

        printed: list[str] = []
        gate = _require_gate(
            ("test",),
            garbage=(".venv/pyvenv.cfg", "__pycache__/m.pyc"),
        )
        # Bare Enter → take the default action.
        decision = _resolve(
            repo=repo, worktree=worktree, run_dir=run_dir, verification_gate=gate,
            no_interactive=False,
            input_fn=_ScriptedInput([""]),
            output_fn=printed.append,
        )
        joined = "\n".join(printed)
        # Distinct garbage section, separate from product M/?? lines.
        assert "Generated environment garbage (not product diff):" in joined
        assert ".venv/pyvenv.cfg" in joined
        assert "__pycache__/m.pyc" in joined
        # require reads as a hard block: title + 'blocked until receipt or waiver'.
        assert "Delivery gate — blocked by required verification" in joined
        assert "delivery blocked until receipt or waiver" in joined
        # Receipt warning surfaced too.
        assert "missing required receipts: test" in joined
        # Bare Enter defaulted to fix — never delivers.
        assert decision.action == "fix"

    def test_operator_override_at_require_block_is_marked_waiver(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """Choosing to deliver despite a require-block is an explicit operator
        override/waiver — the confirming output says so, and delivery proceeds."""
        monkeypatch.setattr(
            "pipeline.engine.commit_delivery.stdio_interactive", lambda: True,
        )
        repo = tmp_path / "repo"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        worktree = _new_worktree(repo, run_dir)
        _with_diff(worktree)

        printed: list[str] = []
        gate = _require_gate(("test",))
        # Operator explicitly chooses approve over the default fix.
        decision = _resolve(
            repo=repo, worktree=worktree, run_dir=run_dir, verification_gate=gate,
            no_interactive=False,
            input_fn=_ScriptedInput(["approve"]),
            output_fn=printed.append,
        )
        joined = "\n".join(printed)
        assert decision.action == "approve"
        assert "override" in joined.lower()
        assert "waiver" in joined.lower()


class TestSuggestProceeds:
    def test_suggest_delivers_default_apply_approve(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """A ``suggest`` gap is informational: the interactive prompt shows a
        shipping-allowed hint and a bare Enter takes the apply/approve default."""
        monkeypatch.setattr(
            "pipeline.engine.commit_delivery.stdio_interactive", lambda: True,
        )
        repo = tmp_path / "repo"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        worktree = _new_worktree(repo, run_dir)
        _with_diff(worktree)

        printed: list[str] = []
        gate = _suggest_gate(("test",))
        decision = _resolve(
            repo=repo, worktree=worktree, run_dir=run_dir, verification_gate=gate,
            no_interactive=False,
            input_fn=_ScriptedInput([""]),
            output_fn=printed.append,
        )
        joined = "\n".join(printed)
        assert decision.action == "approve"
        assert "shipping allowed by policy" in joined
        assert "missing required" not in joined

    def test_prompt_warning_carries_searched_dirs_and_verify_hints(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        # The interactive prompt must carry the SAME actionable diagnostics as
        # the non-interactive banner: searched run dirs + both verify commands,
        # not a bare "missing required receipts" (ADR 0089 / F2).
        monkeypatch.setattr(
            "pipeline.engine.commit_delivery.stdio_interactive", lambda: True,
        )
        repo = tmp_path / "repo"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        worktree = _new_worktree(repo, run_dir)
        _with_diff(worktree)

        child_dir = tmp_path / _INCIDENT_CHILD_RUN_ID
        parent_dir = tmp_path / _INCIDENT_PARENT_RUN_ID
        verify_env = (
            f"orcho verify env --env core-local --run-id {_INCIDENT_CHILD_RUN_ID} "
            "--project /proj"
        )
        verify_run = (
            f"orcho verify run --required --run-id {_INCIDENT_CHILD_RUN_ID} "
            "--project /proj"
        )
        printed: list[str] = []
        gate = _warn_gate(
            ("env-provenance", "lint"),
            garbage=(".venv/pyvenv.cfg",),
            searched_run_dirs=(str(child_dir), str(parent_dir)),
            suggested_commands=(verify_env, verify_run),
        )
        decision = _resolve(
            repo=repo, worktree=worktree, run_dir=run_dir, verification_gate=gate,
            no_interactive=False,
            input_fn=_ScriptedInput([""]),
            output_fn=printed.append,
        )
        joined = "\n".join(printed)
        # warn is shipping-allowed-by-policy, NOT a 'missing required' blocker.
        assert "shipping allowed by policy" in joined
        assert "missing required" not in joined
        # The open commands + their warn policy are still named.
        assert "env-provenance (warn)" in joined and "lint (warn)" in joined
        # Searched dirs + both verify hints still travel with the warning.
        assert "searched:" in joined
        assert str(child_dir) in joined and str(parent_dir) in joined
        assert verify_env in joined
        assert verify_run in joined
        # Garbage keeps its own distinct section and is NOT duplicated inside the
        # verification-warning block.
        assert "Generated environment garbage (not product diff):" in joined
        assert joined.count(".venv/pyvenv.cfg") == 1
        # warn never routes to fix: a bare Enter takes the apply/approve default.
        assert decision.action == "approve"

    def test_release_and_verification_both_block_default_stays_fix(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            "pipeline.engine.commit_delivery.stdio_interactive", lambda: True,
        )
        repo = tmp_path / "repo"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        worktree = _new_worktree(repo, run_dir)
        _with_diff(worktree)

        printed: list[str] = []
        gate = DeliveryVerificationAssessment(
            policy="require", required_missing=("test",),
        )
        decision = _resolve(
            repo=repo, worktree=worktree, run_dir=run_dir, verification_gate=gate,
            session=_session(verdict="REJECTED"),
            no_interactive=False,
            input_fn=_ScriptedInput([""]),
            output_fn=printed.append,
        )
        joined = "\n".join(printed)
        # Release keeps title priority; status of the release path is not
        # replaced by a verification block in interactive mode.
        assert "Correction gate — acceptance rejected" in joined
        assert decision.action == "fix"
        assert decision.status == "pending"


# ─────────────────────────────────────────────────────────────────────────────
# Manual/operator-only-only gap — visible, never blocking, never missing-required
# ─────────────────────────────────────────────────────────────────────────────


class TestManualOnlyVisible:
    def test_manual_only_only_gap_visible_non_interactive(
        self, tmp_path: Path, capsys,
    ) -> None:
        # The only open gap is a manual/operator-only command: delivery proceeds
        # (apply/approve), but the non-interactive log must still surface it as
        # not-auto-run — never as a 'missing required' blocker.
        repo = tmp_path / "repo"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        worktree = _new_worktree(repo, run_dir)
        _with_diff(worktree)

        gate = _manual_only_gate(("e2e",))
        decision = _resolve(
            repo=repo, worktree=worktree, run_dir=run_dir, verification_gate=gate,
        )
        assert decision.status == "pending"
        assert decision.action == "approve"
        assert not gate.blocking
        assert not gate.has_blockers
        logged = capsys.readouterr().out
        assert "manual-only" in logged
        assert "not auto-run" in logged
        assert "e2e" in logged
        assert "missing required" not in logged

    def test_manual_only_only_gap_visible_in_interactive_prompt(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        # Same case, interactive: the prompt shows the manual-only advisory and a
        # bare Enter takes the apply/approve default (never routed to fix).
        monkeypatch.setattr(
            "pipeline.engine.commit_delivery.stdio_interactive", lambda: True,
        )
        repo = tmp_path / "repo"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        worktree = _new_worktree(repo, run_dir)
        _with_diff(worktree)

        printed: list[str] = []
        gate = _manual_only_gate(("e2e",))
        decision = _resolve(
            repo=repo, worktree=worktree, run_dir=run_dir, verification_gate=gate,
            no_interactive=False,
            input_fn=_ScriptedInput([""]),
            output_fn=printed.append,
        )
        joined = "\n".join(printed)
        assert decision.action == "approve"
        assert "manual-only" in joined
        assert "not auto-run" in joined
        assert "e2e" in joined
        assert "missing required" not in joined


# ─────────────────────────────────────────────────────────────────────────────
# Incident banner: a missing-receipt require-block names the searched dirs and
# the exact verify commands (gate.lines carries them) (ADR 0089 / T4)
# ─────────────────────────────────────────────────────────────────────────────


_INCIDENT_PARENT_RUN_ID = "20260612_213530"
_INCIDENT_CHILD_RUN_ID = "20260612_225347"


class TestIncidentBlockBannerCarriesDiagnostics:
    def test_require_block_error_names_searched_dirs_and_verify_commands(
        self, tmp_path: Path,
    ) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        worktree = _new_worktree(repo, run_dir)
        _with_diff(worktree)

        # The incident shape: child empty, parent searched too, no receipt found.
        parent_dir = tmp_path / _INCIDENT_PARENT_RUN_ID
        child_dir = tmp_path / _INCIDENT_CHILD_RUN_ID
        verify_run = (
            f"orcho verify run --required --run-id {_INCIDENT_CHILD_RUN_ID} "
            "--project /proj"
        )
        verify_env = (
            f"orcho verify env --env ci --run-id {_INCIDENT_CHILD_RUN_ID} "
            "--project /proj"
        )
        gate = _require_gate(
            ("test",),
            searched_run_dirs=(str(child_dir), str(parent_dir)),
            suggested_commands=(verify_env, verify_run),
        )
        decision = _resolve(
            repo=repo, worktree=worktree, run_dir=run_dir, verification_gate=gate,
        )

        assert decision.status == "verification_blocked"
        # The non-interactive block error renders gate.lines, so the searched
        # dirs + both verify hints travel with the blocker (no bare banner).
        err = decision.error or ""
        # require reads as a hard block until a receipt or operator waiver.
        assert "blocked by required verification" in err
        assert "delivery blocked until receipt or waiver" in err
        assert "missing required receipts: test" in err
        assert "searched:" in err
        assert str(parent_dir) in err and str(child_dir) in err
        assert verify_env in err
        assert verify_run in err
