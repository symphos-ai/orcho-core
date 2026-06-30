# SPDX-License-Identifier: Apache-2.0
"""Stage 6 delivery-gate assessment (ADR 0083).

Covers the pure ``pipeline.verification_delivery`` layer: the effective-policy
resolver (None→off, contract-without-field→warn, explicit values, require only
explicit), the generated-garbage classifier (component-based; product paths are
never misclassified), required-receipt classification via the fixed
``assess_delivery_verification(diff_cwd)`` contract reading paths/identity from
the subject checkout itself, and the blocking matrix over policy × blockers.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from pipeline.evidence.verification_receipt import write_command_receipt
from pipeline.plugins import PluginConfig
from pipeline.verification_contract import (
    PlaceholderContext,
    VerificationContract,
    VerificationContractError,
)
from pipeline.verification_delivery import (
    DeliveryVerificationAssessment,
    WaivedGate,
    assess_delivery_verification,
    classify_generated_paths,
    resolve_delivery_policy,
)
from pipeline.verification_dependencies import changed_files_fingerprint
from pipeline.verification_readiness import (
    build_final_acceptance_readiness,
    render_readiness_block,
)
from pipeline.verification_receipt_index import (
    VERIFICATION_PARENT_RUNS_EXTRAS_KEY,
)

# Incident run ids (the real follow-up incident the parent-receipt continuity
# work targets): parent run verified the diff, the child correction follow-up
# inherits those receipts for the SAME checkout.
_INCIDENT_PARENT_RUN_ID = "20260612_213530"
_INCIDENT_CHILD_RUN_ID = "20260612_225347"

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _contract(**verification_extra) -> VerificationContract:
    verification = {
        "default_env": "ci",
        "commands": {
            "lint": {"run": "ruff check {checkout}", "env": "ci"},
            "test": {"run": "pytest -q {checkout}"},
        },
        "required": ["test"],
        "schedule": [
            {"before_delivery": True, "commands": ["test"]},
        ],
    }
    verification.update(verification_extra)
    contract = VerificationContract.from_plugin(
        PluginConfig(
            work_mode="governed",
            verification_envs={"ci": {}},
            verification=verification,
        ),
    )
    assert contract is not None
    return contract


def _ctx(
    checkout: str = "", dependencies: dict[str, str] | None = None,
) -> PlaceholderContext:
    return PlaceholderContext(checkout=checkout, dependencies=dependencies or {})


def _dep_repo(path: Path) -> str:
    """Init a git repo with one commit; return its HEAD sha."""
    path.mkdir(parents=True, exist_ok=True)
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(argv, cwd=path, check=True)
    (path / "f.txt").write_text("v0\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "c0"], cwd=path, check=True)
    return _git_head(path)


def _dep_new_commit(path: Path) -> str:
    """Add a commit to an existing repo; return the new HEAD sha."""
    (path / "more.txt").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "c1"], cwd=path, check=True)
    return _git_head(path)


def _dep_record(
    name: str, path: Path, head: str, *, depends_on: bool = True,
) -> dict:
    return {
        "name": name,
        "path": str(path),
        "head": head,
        "dirty": False,
        "changed_files_count": 0,
        "changed_files_fingerprint": "e",
        "depends_on": depends_on,
    }


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
        ["git", "rev-parse", "HEAD"], cwd=co, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def _receipt(
    run_dir: Path,
    command: str,
    *,
    exit_code: int | None = 0,
    detail: str = "",
    assertions: list | None = None,
    fingerprint: str | None = None,
    checkout_head: str | None = None,
    dependencies: list | None = None,
) -> None:
    write_command_receipt(output_dir=run_dir, result={
        "command": command,
        "env": "ci",
        "cwd": "/cwd",
        "placeholders": {"checkout": "/co", "project": "/co"},
        "argv": [command],
        "assertions": assertions or [],
        "exit_code": exit_code,
        "duration_s": 0.1,
        "parity": "absolute",
        "detail": detail,
        "git": {
            "checkout_head": checkout_head,
            "baseline_head": None,
            "changed_files_fingerprint": fingerprint,
        },
        "dependencies": dependencies or [],
    })


# ─────────────────────────────────────────────────────────────────────────────
# Policy resolution
# ─────────────────────────────────────────────────────────────────────────────


class TestResolveDeliveryPolicy:
    def test_none_contract_is_off(self) -> None:
        assert resolve_delivery_policy(None) == "off"

    def test_declared_without_field_is_warn(self) -> None:
        # work_mode=governed must NOT escalate the policy, and a schedule
        # entry without an explicit policy doesn't either — the default
        # before_delivery entry here carries policy=None.
        assert resolve_delivery_policy(_contract()) == "warn"

    @pytest.mark.parametrize("value", ["off", "suggest", "warn", "require"])
    def test_explicit_value_honoured(self, value: str) -> None:
        contract = _contract(delivery_policy=value)
        assert contract.delivery_policy == value
        assert resolve_delivery_policy(contract) == value

    def test_invalid_value_raises_on_load(self) -> None:
        with pytest.raises(VerificationContractError):
            VerificationContract.from_plugin(PluginConfig(
                verification={
                    "commands": {"test": {"run": "x"}},
                    "delivery_policy": "bogus",
                },
            ))

    def test_work_mode_never_escalates(self) -> None:
        # Governed mode + no explicit field + no scheduled require → warn.
        assert resolve_delivery_policy(_contract()) != "require"
        assert resolve_delivery_policy(_contract(delivery_policy="require")) == (
            "require"
        )

    def test_scheduled_require_at_delivery_derives_require(self) -> None:
        """ADR 0090: an explicit ``policy=require`` gate scheduled at
        ``before_delivery`` IS the delivery opt-in — letting the boundary
        stay ``warn`` would deliver unverified changes whenever the scheduled
        hook is skipped (the silent-skip incident)."""
        contract = _contract(schedule=[
            {"before_delivery": True, "policy": "require", "commands": ["test"]},
        ])
        assert resolve_delivery_policy(contract) == "require"

    def test_scheduled_require_elsewhere_does_not_escalate(self) -> None:
        # require at after_phase(implement) only — the delivery boundary
        # itself was not opted in.
        contract = _contract(schedule=[
            {"after_phase": "implement", "policy": "require", "commands": ["test"]},
        ])
        assert resolve_delivery_policy(contract) == "warn"

    def test_explicit_field_wins_over_scheduled_require(self) -> None:
        contract = _contract(
            delivery_policy="warn",
            schedule=[
                {"before_delivery": True, "policy": "require",
                 "commands": ["test"]},
            ],
        )
        assert resolve_delivery_policy(contract) == "warn"


# ─────────────────────────────────────────────────────────────────────────────
# Generated-garbage classification
# ─────────────────────────────────────────────────────────────────────────────


class TestClassifyGeneratedPaths:
    def test_positive_components(self) -> None:
        paths = [
            "venv/lib/python3.12/site-packages/x.py",
            ".venv/bin/python",
            "pkg/__pycache__/m.cpython-312.pyc",
            "a/m.pyc",
            "a/m.pyo",
            ".pytest_cache/v/cache",
            ".ruff_cache/x",
            ".mypy_cache/x",
            ".tox/py312/x",
            "node_modules/left-pad/index.js",
            "orcho.egg-info/PKG-INFO",
            "verification_receipts/implement_round1.json",
            "verification_env_receipts/verify_env_ci.json",
            "verification_command_receipts/test.json",
        ]
        assert classify_generated_paths(paths) == tuple(paths)

    def test_negative_product_paths(self) -> None:
        paths = [
            "src/venv_utils.py",
            "pipeline/verification_delivery.py",
            "docs/venv.md",
            "tests/test_node_modules_helper.py",
            "egg_info.py",
        ]
        assert classify_generated_paths(paths) == ()

    def test_mixed_returns_only_garbage(self) -> None:
        paths = ["src/app.py", ".venv/x", "README.md", "a/__pycache__/b.pyc"]
        assert classify_generated_paths(paths) == (".venv/x", "a/__pycache__/b.pyc")


# ─────────────────────────────────────────────────────────────────────────────
# Per-gate policy-aware partition + lines (T2, ADR 0097)
# ─────────────────────────────────────────────────────────────────────────────


def _ctx_with_project(checkout: str) -> PlaceholderContext:
    return PlaceholderContext(checkout=checkout, project=checkout)


@pytest.mark.git_worktree
class TestPolicyAwareDeliveryLines:
    def test_warn_missing_is_not_blocking_and_says_shipping_allowed(
        self, tmp_path: Path,
    ) -> None:
        # Boundary policy warn, no per-gate require gate → the missing receipt is
        # a warn gap: surfaced, but delivery is allowed by policy and the line
        # names the effective per-gate policy.
        co = _git_checkout(tmp_path)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        assessment = assess_delivery_verification(
            _contract(delivery_policy="warn"), run_dir,
            _ctx_with_project(str(co)), None, co,
        )
        assert assessment is not None
        assert assessment.required_missing == ("test",)
        assert assessment.blocking is False
        assert assessment.warning_gaps and not assessment.blocking_gaps
        warn_line = next(
            line for line in assessment.lines
            if line.startswith("missing receipts")
        )
        assert "shipping allowed by policy" in warn_line
        assert "(warn)" in warn_line  # effective per-gate policy named

    def test_require_missing_is_blocking_and_says_missing_required(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        assessment = assess_delivery_verification(
            _contract(delivery_policy="require"), run_dir,
            _ctx_with_project(str(co)), None, co,
        )
        assert assessment is not None
        assert assessment.blocking is True
        assert assessment.blocking_gaps and not assessment.warning_gaps
        assert any(
            line.startswith("missing required receipts")
            for line in assessment.lines
        )
        assert not any(
            "shipping allowed by policy" in line for line in assessment.lines
        )

    def test_manual_only_command_excluded_from_required_but_visible(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        contract = _contract(
            commands={
                "test": {"run": "x"},
                "audit": {"run": "audit {checkout}"},
            },
            required=["test", "audit"],
            schedule=[
                {"before_delivery": True, "commands": ["test"]},
                {"manual_only": True, "commands": ["audit"]},
            ],
            delivery_policy="require",
        )
        assessment = assess_delivery_verification(
            contract, run_dir, _ctx_with_project(str(co)), None, co,
        )
        assert assessment is not None
        # The require gap "test" is required + blocking; the manual "audit" is
        # neither in required_missing nor in any required_* bucket.
        assert "test" in assessment.required_missing
        assert "audit" not in assessment.required_missing
        assert "audit" not in assessment.required_failed
        assert "audit" not in assessment.required_stale
        # ...but it IS visible in its own field + advisory line.
        assert assessment.manual_only_commands == ("audit",)
        manual_line = assessment.manual_only_line
        assert manual_line is not None and "audit" in manual_line

    def test_manual_only_gap_alone_never_blocks(self, tmp_path: Path) -> None:
        co = _git_checkout(tmp_path)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        contract = _contract(
            commands={"audit": {"run": "audit {checkout}"}},
            required=["audit"],
            schedule=[{"manual_only": True, "commands": ["audit"]}],
            delivery_policy="warn",
        )
        assessment = assess_delivery_verification(
            contract, run_dir, _ctx_with_project(str(co)), None, co,
        )
        assert assessment is not None
        assert assessment.required_missing == ()
        assert assessment.manual_only_commands == ("audit",)
        assert assessment.has_blockers is False
        assert assessment.blocking is False

    def test_policy_by_command_records_effective_per_gate_policy(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        contract = _contract(
            commands={
                "test": {"run": "x"},
                "audit": {"run": "audit {checkout}"},
            },
            required=["test", "audit"],
            schedule=[
                {"before_delivery": True, "commands": ["test"]},
                {"manual_only": True, "commands": ["audit"]},
            ],
            delivery_policy="require",
        )
        assessment = assess_delivery_verification(
            contract, run_dir, _ctx_with_project(str(co)), None, co,
        )
        assert assessment is not None
        policy_map = dict(assessment.policy_by_command)
        assert policy_map["test"] == "require"
        assert policy_map["audit"] == "manual_only"


# ─────────────────────────────────────────────────────────────────────────────
# Blocking matrix (policy × blockers)
# ─────────────────────────────────────────────────────────────────────────────


class TestBlockingMatrix:
    def test_no_blockers_never_blocks(self) -> None:
        a = DeliveryVerificationAssessment(policy="require")
        assert not a.has_blockers
        assert not a.blocking

    @pytest.mark.parametrize("policy", ["off", "suggest", "warn"])
    def test_blockers_below_require_do_not_block(self, policy: str) -> None:
        a = DeliveryVerificationAssessment(
            policy=policy, required_missing=("test",),
        )
        assert a.has_blockers
        assert not a.blocking

    @pytest.mark.parametrize("field", [
        "required_missing", "required_failed", "required_stale", "garbage_paths",
    ])
    def test_require_with_any_blocker_blocks(self, field: str) -> None:
        a = DeliveryVerificationAssessment(policy="require", **{field: ("x",)})
        assert a.has_blockers
        assert a.blocking
        assert any("x" in line for line in a.lines)


# ─────────────────────────────────────────────────────────────────────────────
# assess_delivery_verification — fixed orchestration contract
# ─────────────────────────────────────────────────────────────────────────────


class TestAssessReturnsNone:
    def test_none_contract(self, tmp_path: Path) -> None:
        assert assess_delivery_verification(
            None, tmp_path, _ctx(), None, tmp_path,
        ) is None

    def test_policy_off(self, tmp_path: Path) -> None:
        contract = _contract(delivery_policy="off")
        assert assess_delivery_verification(
            contract, tmp_path, _ctx(), None, tmp_path,
        ) is None


@pytest.mark.git_worktree
class TestAssessReceiptClassification:
    def test_missing_receipt(self, tmp_path: Path) -> None:
        co = _git_checkout(tmp_path)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        # No receipt written for required "test" → missing.
        assessment = assess_delivery_verification(
            _contract(delivery_policy="require"), run_dir, _ctx(str(co)), None, co,
        )
        assert assessment is not None
        assert assessment.required_missing == ("test",)
        assert assessment.blocking

    def test_failed_receipt(self, tmp_path: Path) -> None:
        co = _git_checkout(tmp_path)
        head = _git_head(co)
        fingerprint = changed_files_fingerprint(str(co))
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _receipt(run_dir, "test", exit_code=1,
                 fingerprint=fingerprint, checkout_head=head)
        assessment = assess_delivery_verification(
            _contract(delivery_policy="warn"), run_dir, _ctx(str(co)), None, co,
        )
        assert assessment is not None
        assert assessment.required_failed == ("test",)
        assert assessment.has_blockers
        assert not assessment.blocking  # warn does not hard-block

    def test_failed_only_require_carries_verify_hint(self, tmp_path: Path) -> None:
        # A failed-only require blocker must still carry the actionable
        # ``orcho verify`` hint + searched dirs (parity with readiness / DONE),
        # not just a bare 'failed required receipts' line.
        co = _git_checkout(tmp_path)
        head = _git_head(co)
        fingerprint = changed_files_fingerprint(str(co))
        run_dir = tmp_path / "run_20260619_FAILED"
        run_dir.mkdir()
        _receipt(run_dir, "test", exit_code=1,
                 fingerprint=fingerprint, checkout_head=head)
        assessment = assess_delivery_verification(
            _contract(delivery_policy="require"), run_dir,
            _ctx_with_project(str(co)), None, co,
        )
        assert assessment is not None
        assert assessment.required_failed == ("test",)
        assert assessment.required_missing == ()
        assert assessment.blocking
        # The verify hint is built for failed-only too, and rides in the lines.
        assert assessment.suggested_commands
        assert any(
            line.startswith("failed required receipts") for line in assessment.lines
        )
        assert any(line.startswith("searched:") for line in assessment.lines)
        assert any("orcho verify" in line for line in assessment.lines)

    def test_stale_receipt(self, tmp_path: Path) -> None:
        co = _git_checkout(tmp_path)
        head = _git_head(co)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _receipt(run_dir, "test", fingerprint="deadbeefdeadbeef",
                 checkout_head=head)
        assessment = assess_delivery_verification(
            _contract(delivery_policy="require"), run_dir, _ctx(str(co)), None, co,
        )
        assert assessment is not None
        assert assessment.required_stale == ("test",)

    def test_receipt_written_against_diff_cwd_not_stale(self, tmp_path: Path) -> None:
        # Provenance computed against the same diff_cwd subject the assessment
        # reads identity from → present, never falsely stale on an unchanged repo.
        co = _git_checkout(tmp_path)
        head = _git_head(co)
        fingerprint = changed_files_fingerprint(str(co))
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _receipt(run_dir, "test", fingerprint=fingerprint, checkout_head=head)
        assessment = assess_delivery_verification(
            _contract(delivery_policy="require"), run_dir, _ctx(str(co)), None, co,
        )
        assert assessment is not None
        assert assessment.required_missing == ()
        assert assessment.required_failed == ()
        assert assessment.required_stale == ()
        assert not assessment.blocking

    def test_degrade_to_present_without_checkout_identity(
        self, tmp_path: Path,
    ) -> None:
        # diff_cwd is not a git repo → no head/fingerprint → staleness is not
        # asserted; a valid-looking receipt degrades to present.
        not_a_repo = tmp_path / "plain"
        not_a_repo.mkdir()
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _receipt(run_dir, "test", fingerprint="deadbeefdeadbeef",
                 checkout_head="0" * 40)
        assessment = assess_delivery_verification(
            _contract(delivery_policy="require"), run_dir, _ctx(str(not_a_repo)),
            None, not_a_repo,
        )
        assert assessment is not None
        assert assessment.required_stale == ()
        assert assessment.required_missing == ()
        assert assessment.required_failed == ()


@pytest.mark.git_worktree
class TestAssessDependencyStale:
    """A depended-on dependency's HEAD move marks the receipt stale via
    ``assess_delivery_verification``; required_stale keeps NAMES (audit keys
    unchanged) while ``lines`` carries the reason. The negatives never stale."""

    def _present_receipt_with_dep(
        self, run_dir: Path, co: Path, dep_record: dict,
    ) -> None:
        _receipt(
            run_dir, "test",
            fingerprint=changed_files_fingerprint(str(co)),
            checkout_head=_git_head(co),
            dependencies=[dep_record],
        )

    def test_dependency_head_move_is_stale_with_reason_in_lines(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        dep = tmp_path / "dep"
        old = _dep_repo(dep)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        self._present_receipt_with_dep(
            run_dir, co, _dep_record("shared", dep, old, depends_on=True),
        )
        new = _dep_new_commit(dep)

        assessment = assess_delivery_verification(
            _contract(delivery_policy="require"), run_dir,
            _ctx(str(co), {"shared": str(dep)}), None, co,
        )

        assert assessment is not None
        # Audit-facing names unchanged.
        assert assessment.required_stale == ("test",)
        # The reason rides in stale_details / lines, naming dep + both SHAs.
        assert assessment.stale_details == (
            f"test (dependency shared HEAD moved {old} -> {new})",
        )
        stale_line = next(
            line for line in assessment.lines
            if line.startswith("stale required receipts")
        )
        assert "dependency shared HEAD moved" in stale_line
        assert old in stale_line and new in stale_line
        assert assessment.blocking

    def test_depends_on_false_is_not_stale(self, tmp_path: Path) -> None:
        co = _git_checkout(tmp_path)
        dep = tmp_path / "dep"
        old = _dep_repo(dep)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        self._present_receipt_with_dep(
            run_dir, co, _dep_record("shared", dep, old, depends_on=False),
        )
        _dep_new_commit(dep)

        assessment = assess_delivery_verification(
            _contract(delivery_policy="require"), run_dir,
            _ctx(str(co), {"shared": str(dep)}), None, co,
        )

        assert assessment is not None
        assert assessment.required_stale == ()
        assert assessment.required_missing == ()

    def test_ctx_none_does_not_assert_dependency_stale(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        dep = tmp_path / "dep"
        old = _dep_repo(dep)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        self._present_receipt_with_dep(
            run_dir, co, _dep_record("shared", dep, old, depends_on=True),
        )
        _dep_new_commit(dep)

        # No ctx → no declared-dependency heads → dependency staleness silent.
        assessment = assess_delivery_verification(
            _contract(delivery_policy="require"), run_dir, None, None, co,
        )

        assert assessment is not None
        assert assessment.required_stale == ()

    def test_receipt_without_dependencies_block_is_not_stale(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        dep = tmp_path / "dep"
        _dep_repo(dep)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _receipt(
            run_dir, "test",
            fingerprint=changed_files_fingerprint(str(co)),
            checkout_head=_git_head(co),
        )  # no dependencies block
        _dep_new_commit(dep)

        assessment = assess_delivery_verification(
            _contract(delivery_policy="require"), run_dir,
            _ctx(str(co), {"shared": str(dep)}), None, co,
        )

        assert assessment is not None
        assert assessment.required_stale == ()

    def test_dependency_not_git_does_not_assert_stale(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        plain = tmp_path / "plain"
        plain.mkdir()  # not a git repo
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        self._present_receipt_with_dep(
            run_dir, co, _dep_record("shared", plain, "0" * 40, depends_on=True),
        )

        assessment = assess_delivery_verification(
            _contract(delivery_policy="require"), run_dir,
            _ctx(str(co), {"shared": str(plain)}), None, co,
        )

        assert assessment is not None
        assert assessment.required_stale == ()


@pytest.mark.git_worktree
class TestAssessReadsPathsFromDiffCwd:
    def test_untracked_and_changed_garbage_detected(self, tmp_path: Path) -> None:
        co = _git_checkout(tmp_path)
        head = _git_head(co)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _receipt(run_dir, "test",
                 fingerprint=changed_files_fingerprint(str(co)),
                 checkout_head=head)

        # An untracked generated artifact + a changed product file + a changed
        # generated file. Only the generated ones land in garbage_paths.
        (co / ".venv").mkdir()
        (co / ".venv" / "pyvenv.cfg").write_text("x\n", encoding="utf-8")
        (co / "base.txt").write_text("base changed\n", encoding="utf-8")
        (co / "src").mkdir()
        (co / "src" / "app.py").write_text("print(1)\n", encoding="utf-8")

        assessment = assess_delivery_verification(
            _contract(delivery_policy="warn"), run_dir, _ctx(str(co)), None, co,
        )
        assert assessment is not None
        # Fingerprint moved (new untracked/changed files) → the receipt is now
        # stale, proving identity is read from diff_cwd.
        assert assessment.required_stale == ("test",)
        assert ".venv/pyvenv.cfg" in assessment.garbage_paths
        assert "base.txt" not in assessment.garbage_paths
        assert "src/app.py" not in assessment.garbage_paths

    def test_no_garbage_on_clean_product_diff(self, tmp_path: Path) -> None:
        co = _git_checkout(tmp_path)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (co / "src").mkdir()
        (co / "src" / "feature.py").write_text("x = 1\n", encoding="utf-8")
        assessment = assess_delivery_verification(
            _contract(delivery_policy="warn"), run_dir, _ctx(str(co)), None, co,
        )
        assert assessment is not None
        assert assessment.garbage_paths == ()


# ─────────────────────────────────────────────────────────────────────────────
# Diagnostics: searched dirs + actionable verify hints (ADR 0089 / T3)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.git_worktree
class TestAssessDiagnostics:
    def test_missing_receipt_banner_names_searched_dirs_and_both_commands(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        run_dir = tmp_path / "run_20260613_ABCDEF"
        run_dir.mkdir()
        # No receipts anywhere → "test" is missing.
        assessment = assess_delivery_verification(
            _contract(delivery_policy="require"), run_dir,
            _ctx_with_project(str(co)), None, co,
        )
        assert assessment is not None
        assert assessment.required_missing == ("test",)

        run_id = run_dir.name
        lines = assessment.lines
        # The searched-dirs line names the actual run dir we looked in.
        searched = next(line for line in lines if line.startswith("searched:"))
        assert str(run_dir) in searched
        # Both verify commands appear, each carrying the actual run id + project.
        assert (
            f"orcho verify env --env ci --run-id {run_id} --project {co}" in lines
        )
        assert (
            f"orcho verify run --required --run-id {run_id} --project {co}" in lines
        )
        # Field carries the same searched dirs (current run only, no parents).
        assert assessment.searched_run_dirs == (str(run_dir),)

    def test_selected_delivery_gate_hint_names_exact_commands(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        run_dir = tmp_path / "run_20260613_DELIVERY"
        run_dir.mkdir()
        contract = _contract(
            commands={
                "env-provenance": {"run": "python -c 'import pipeline'", "env": "ci"},
                "lint": {"run": "ruff check {checkout}", "env": "ci"},
                "run-state-unit": {"run": "pytest -q tests/unit/pipeline/run_state"},
            },
            required=["env-provenance", "lint"],
            schedule=[
                {"before_delivery": True, "commands": ["run-state-unit"]},
            ],
            delivery_policy="require",
        )

        assessment = assess_delivery_verification(
            contract, run_dir, _ctx_with_project(str(co)), None, co,
        )

        assert assessment is not None
        assert assessment.required_missing == (
            "env-provenance", "lint", "run-state-unit",
        )
        expected = (
            f"orcho verify run env-provenance lint run-state-unit "
            f"--run-id {run_dir.name} --project {co}"
        )
        assert expected in assessment.lines
        assert not any(
            line.startswith("orcho verify run --required")
            for line in assessment.lines
        )

    def test_searched_dirs_include_parent_run_from_extras(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        run_dir = tmp_path / "child_run"
        run_dir.mkdir()
        parent_dir = tmp_path / "parent_run"
        parent_dir.mkdir()
        extras = {
            VERIFICATION_PARENT_RUNS_EXTRAS_KEY: ((parent_dir.name, str(parent_dir)),),
        }
        assessment = assess_delivery_verification(
            _contract(delivery_policy="require"), run_dir,
            _ctx_with_project(str(co)), extras, co,
        )
        assert assessment is not None
        # Current run first, then the follow-up parent — the same key readiness
        # reads, so the two surfaces searched the same dirs.
        assert assessment.searched_run_dirs == (str(run_dir), str(parent_dir))
        searched = next(
            line for line in assessment.lines if line.startswith("searched:")
        )
        assert str(parent_dir) in searched

    def test_no_missing_receipt_keeps_lines_byte_identical(
        self, tmp_path: Path,
    ) -> None:
        # A present receipt → no missing → no searched/suggestion lines appended;
        # an all-clean assessment renders no diagnostics lines at all.
        co = _git_checkout(tmp_path)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _receipt(run_dir, "test",
                 fingerprint=changed_files_fingerprint(str(co)),
                 checkout_head=_git_head(co))
        assessment = assess_delivery_verification(
            _contract(delivery_policy="require"), run_dir,
            _ctx_with_project(str(co)), None, co,
        )
        assert assessment is not None
        assert assessment.required_missing == ()
        assert assessment.lines == ()  # no blockers → no lines, unchanged


# ─────────────────────────────────────────────────────────────────────────────
# Incident: parent-receipt continuity (ADR 0089) — two run dirs + one checkout
# ─────────────────────────────────────────────────────────────────────────────


def _incident_dirs(tmp_path: Path) -> tuple[Path, Path]:
    """The incident's parent + child run dirs, named with the real run ids."""
    parent = tmp_path / _INCIDENT_PARENT_RUN_ID
    parent.mkdir()
    child = tmp_path / _INCIDENT_CHILD_RUN_ID
    child.mkdir()
    return parent, child


def _incident_extras(parent: Path) -> dict:
    return {VERIFICATION_PARENT_RUNS_EXTRAS_KEY: ((parent.name, str(parent)),)}


@pytest.mark.git_worktree
class TestIncidentParentContinuity:
    """The 20260612_213530 -> 20260612_225347 incident, end-to-end on the pure
    Stage 5/6 layer: the child follow-up inherits the parent's receipts for the
    same checkout, foreign-fingerprint parents are stale, no receipts yield
    actionable diagnostics, and the two surfaces agree on every verdict set."""

    def test_parent_receipts_for_current_checkout_are_inherited_present(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        head, fp = _git_head(co), changed_files_fingerprint(str(co))
        parent, child = _incident_dirs(tmp_path)
        # Parent verified BOTH required commands against this very checkout.
        _receipt(parent, "test", fingerprint=fp, checkout_head=head)
        _receipt(parent, "lint", fingerprint=fp, checkout_head=head)
        contract = _contract(required=["test", "lint"], schedule=[],
                             delivery_policy="require")
        extras = _incident_extras(parent)

        summary = build_final_acceptance_readiness(
            contract, child, _ctx(str(co)), extras=extras,
        )
        # Readiness: both required receipts present (inherited), none missing.
        assert set(summary.required_present) == {"test", "lint"}
        assert summary.required_missing == ()
        # Provenance points at the parent run id for both.
        assert len(summary.receipt_provenance) == 2
        assert all(
            _INCIDENT_PARENT_RUN_ID in line and "present from run" in line
            for line in summary.receipt_provenance
        )

        # Delivery agrees: nothing missing → no "missing required receipts".
        assessment = assess_delivery_verification(
            contract, child, _ctx(str(co)), extras, co,
        )
        assert assessment is not None
        assert assessment.required_missing == ()
        assert assessment.required_stale == ()
        assert not any(
            line.startswith("missing required receipts")
            for line in assessment.lines
        )

    def test_parent_receipt_with_foreign_fingerprint_is_stale_with_reason(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        head = _git_head(co)
        parent, child = _incident_dirs(tmp_path)
        # Parent verified a DIFFERENT change set → fingerprint will not match.
        _receipt(parent, "test", fingerprint="deadbeefdeadbeef",
                 checkout_head=head)
        contract = _contract(
            commands={"test": {"run": "x"}}, required=["test"], schedule=[],
            delivery_policy="require",
        )
        extras = _incident_extras(parent)

        summary = build_final_acceptance_readiness(
            contract, child, _ctx(str(co)), extras=extras,
        )
        assert summary.required_stale == ("test",)
        assert any("fingerprint" in r for r in summary.stale_reasons)

        assessment = assess_delivery_verification(
            contract, child, _ctx(str(co)), extras, co,
        )
        assert assessment is not None
        assert assessment.required_stale == ("test",)
        assert any("fingerprint" in d for d in assessment.stale_details)
        assert assessment.required_missing == ()

    def test_no_receipts_anywhere_yields_dirs_and_hints(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        parent, child = _incident_dirs(tmp_path)
        contract = _contract(
            commands={"test": {"run": "x"}}, required=["test"], schedule=[],
            delivery_policy="require",
        )
        extras = _incident_extras(parent)
        run_id = child.name

        summary = build_final_acceptance_readiness(
            contract, child, _ctx_with_project(str(co)), extras=extras,
        )
        assert summary.required_missing == ("test",)
        # Both searched dirs (child first, then parent) are named.
        assert summary.searched_run_dirs == (str(child), str(parent))
        assert any(
            f"--run-id {run_id}" in c for c in summary.suggested_commands
        )
        assert any(
            "orcho verify run --required" in c for c in summary.suggested_commands
        )
        rendered = render_readiness_block(summary)
        assert rendered is not None
        assert "Searched run dirs:" in rendered
        assert str(parent) in rendered and str(child) in rendered

        assessment = assess_delivery_verification(
            contract, child, _ctx_with_project(str(co)), extras, co,
        )
        assert assessment is not None
        assert assessment.required_missing == ("test",)
        assert assessment.searched_run_dirs == (str(child), str(parent))
        searched = next(
            line for line in assessment.lines if line.startswith("searched:")
        )
        assert str(parent) in searched and str(child) in searched
        assert any(f"--run-id {run_id}" in line for line in assessment.lines)

    def test_readiness_and_delivery_agree_on_every_verdict_set(
        self, tmp_path: Path,
    ) -> None:
        # Single-source contract: same (contract, run_dir, ctx, extras, checkout)
        # MUST give identical missing/failed/stale partitions on both surfaces.
        co = _git_checkout(tmp_path)
        head, fp = _git_head(co), changed_files_fingerprint(str(co))
        parent, child = _incident_dirs(tmp_path)
        # test: present (inherited from parent); lint: stale (foreign parent);
        # smoke: failed (fresh child failure on the same diff).
        _receipt(parent, "test", fingerprint=fp, checkout_head=head)
        _receipt(parent, "lint", fingerprint="deadbeefdeadbeef",
                 checkout_head=head)
        _receipt(child, "smoke", exit_code=1, fingerprint=fp, checkout_head=head)
        contract = _contract(
            commands={
                "test": {"run": "x"}, "lint": {"run": "y"},
                "smoke": {"run": "z"},
            },
            required=["test", "lint", "smoke"], schedule=[],
            delivery_policy="require",
        )
        ctx = _ctx(str(co))
        extras = _incident_extras(parent)

        summary = build_final_acceptance_readiness(
            contract, child, ctx, extras=extras,
        )
        assessment = assess_delivery_verification(contract, child, ctx, extras, co)
        assert assessment is not None

        # The single-source invariant: the three verdict sets match exactly.
        assert set(summary.required_missing) == set(assessment.required_missing)
        assert set(summary.required_failed) == set(assessment.required_failed)
        assert set(summary.required_stale) == set(assessment.required_stale)
        # ... and they are the non-trivial partition we constructed.
        assert set(summary.required_present) == {"test"}
        assert set(summary.required_stale) == {"lint"}
        assert set(summary.required_failed) == {"smoke"}
        assert summary.required_missing == ()

    def test_invalid_child_receipt_yields_to_valid_parent(
        self, tmp_path: Path,
    ) -> None:
        # Incident variant: the child re-ran lint against a now-different diff
        # (its receipt is stale), but the parent's lint for THIS checkout is
        # valid → delivery inherits it: no missing/stale lint.
        co = _git_checkout(tmp_path)
        head, fp = _git_head(co), changed_files_fingerprint(str(co))
        parent, child = _incident_dirs(tmp_path)
        _receipt(child, "lint", fingerprint="deadbeefdeadbeef",
                 checkout_head=head)            # child stale (foreign diff)
        _receipt(parent, "lint", fingerprint=fp, checkout_head=head)  # valid parent
        contract = _contract(
            commands={"lint": {"run": "y", "env": "ci"}}, required=["lint"],
            schedule=[], delivery_policy="require",
        )
        extras = _incident_extras(parent)

        assessment = assess_delivery_verification(
            contract, child, _ctx(str(co)), extras, co,
        )
        assert assessment is not None
        assert assessment.required_missing == ()
        assert assessment.required_stale == ()
        assert assessment.lines == ()  # nothing to warn about — fully inherited

        summary = build_final_acceptance_readiness(
            contract, child, _ctx(str(co)), extras=extras,
        )
        assert summary.required_present == ("lint",)
        assert summary.required_stale == ()


# ─────────────────────────────────────────────────────────────────────────────
# Verification-gate waivers (T2): a precise durable waiver removes a failed /
# missing require gap from blocking and records it as waived, without ever
# re-counting it in required_* or unblocking a neighbouring gate.
# ─────────────────────────────────────────────────────────────────────────────


def _gate_waiver_extras(
    *,
    command: str = "test",
    round_n: int = 1,
    waiver_text: str = "accepted: pre-existing failure on this checkout",
    note: str | None = None,
    handoff_id: str | None = None,
) -> dict:
    """A durable ``phase_handoff_waiver`` covering ``gate:<command>:<round>``."""
    return {
        "phase_handoff_waiver": {
            "handoff_id": handoff_id or f"gate:{command}:{round_n}",
            "phase": "final_acceptance",
            "waiver_text": waiver_text,
            "note": note,
            "decided_by": "operator",
        },
    }


@pytest.mark.git_worktree
class TestDeliveryVerificationWaiver:
    def test_failed_required_with_exact_waiver_is_not_blocking(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        head = _git_head(co)
        fingerprint = changed_files_fingerprint(str(co))
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _receipt(run_dir, "test", exit_code=1,
                 fingerprint=fingerprint, checkout_head=head)
        assessment = assess_delivery_verification(
            _contract(delivery_policy="require"), run_dir,
            _ctx_with_project(str(co)), _gate_waiver_extras(), co,
        )
        assert assessment is not None
        # The failed require gap is excused: not blocking, recorded as waived,
        # and never re-counted in required_failed / blocking_gaps.
        assert assessment.blocking is False
        assert assessment.waived_failed == ("test",)
        assert assessment.required_failed == ()
        assert not assessment.blocking_gaps
        waived = assessment.waived_gates
        assert len(waived) == 1
        gate = waived[0]
        assert isinstance(gate, WaivedGate)
        assert gate.gate_command == "test"
        assert gate.status == "failed"
        assert gate.handoff_id == "gate:test:1"
        assert gate.waiver_text and "pre-existing" in gate.waiver_text

    def test_missing_required_with_exact_waiver_is_not_blocking(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        # No receipt → missing; the waiver excuses the missing require gap.
        assessment = assess_delivery_verification(
            _contract(delivery_policy="require"), run_dir,
            _ctx_with_project(str(co)), _gate_waiver_extras(), co,
        )
        assert assessment is not None
        assert assessment.blocking is False
        assert assessment.waived_missing == ("test",)
        assert assessment.required_missing == ()
        assert not assessment.blocking_gaps
        assert assessment.waived_gates[0].status == "missing"

    def test_waiver_for_another_gate_does_not_unblock(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        # Waiver names a different command → the real "test" gap stays blocking.
        assessment = assess_delivery_verification(
            _contract(delivery_policy="require"), run_dir,
            _ctx_with_project(str(co)),
            _gate_waiver_extras(command="some-other-gate"), co,
        )
        assert assessment is not None
        assert assessment.blocking is True
        assert assessment.required_missing == ("test",)
        assert assessment.waived_missing == ()
        assert assessment.waived_failed == ()
        assert assessment.waived_gates == ()
        assert assessment.blocking_gaps  # the unwaived gap remains

    def test_no_waiver_keeps_prior_blocking_behaviour(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        assessment = assess_delivery_verification(
            _contract(delivery_policy="require"), run_dir,
            _ctx_with_project(str(co)), None, co,
        )
        assert assessment is not None
        assert assessment.blocking is True
        assert assessment.required_missing == ("test",)
        # Additive waived fields stay empty when no waiver is present.
        assert assessment.waived_failed == ()
        assert assessment.waived_missing == ()
        assert assessment.waived_gates == ()

    def test_malformed_waiver_input_never_raises_and_is_ignored(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        # A non-gate handoff id (review waiver) and garbage shapes must not
        # excuse the gap, and must not raise.
        for extras in (
            {"phase_handoff_waiver": {"handoff_id": "review:1",
                                      "waiver_text": "x"}},
            {"phase_handoff_waiver": "not-a-mapping"},
            {"phase_handoff_waiver": 12345},
        ):
            assessment = assess_delivery_verification(
                _contract(delivery_policy="require"), run_dir,
                _ctx_with_project(str(co)), extras, co,
            )
            assert assessment is not None
            assert assessment.blocking is True
            assert assessment.required_missing == ("test",)
            assert assessment.waived_gates == ()


# ─────────────────────────────────────────────────────────────────────────────
# Environment-provenance overlay (ADR 0108 T1)
# ─────────────────────────────────────────────────────────────────────────────


def _prov_delivery_contract(*, manual: bool = False) -> VerificationContract:
    """A required ``env-provenance`` gate scheduled at after_phase(implement).

    When ``manual`` is set, the gate also carries a ``manual_only`` schedule
    entry so its effective policy is manual (never blocking) while keeping the
    phase link the overlay needs.
    """
    schedule: list = [
        {"after_phase": "implement", "policy": "require",
         "commands": ["env-provenance"]},
    ]
    if manual:
        schedule.append({"manual_only": True, "commands": ["env-provenance"]})
    contract = VerificationContract.from_plugin(
        PluginConfig(
            work_mode="governed",
            verification_envs={"ci": {}},
            verification={
                "default_env": "ci",
                "required": ["env-provenance"],
                "commands": {"env-provenance": {"run": "echo prov", "env": "ci"}},
                "delivery_policy": "require",
                "schedule": schedule,
            },
        ),
    )
    assert contract is not None
    return contract


def _write_failed_prov_phase_receipt(run_dir: Path) -> Path:
    from pipeline.evidence.verification_receipt import write_verification_receipt

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


class TestEnvironmentProvenanceOverlay:
    """A passing command receipt + a failed phase provenance receipt for a
    phase-scheduled require gate blocks delivery (the gap is failed/blocking)."""

    def test_failed_provenance_blocks_delivery(self, tmp_path: Path) -> None:
        co = _git_checkout(tmp_path)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        contract = _prov_delivery_contract()
        # Passing command receipt would otherwise read present ...
        _receipt(run_dir, "env-provenance")
        # ... but the implement phase's environment provenance broke.
        _write_failed_prov_phase_receipt(run_dir)

        assessment = assess_delivery_verification(
            contract, run_dir, _ctx(str(co)), None, co,
        )

        assert assessment is not None
        assert assessment.blocking is True
        assert assessment.required_failed == ("env-provenance",)
        assert "env-provenance" in {e.command for e in assessment.blocking_gaps}

    def test_healthy_provenance_does_not_block(self, tmp_path: Path) -> None:
        co = _git_checkout(tmp_path)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        contract = _prov_delivery_contract()
        _receipt(run_dir, "env-provenance")
        # No phase receipt -> no provenance failure -> the present gate stands.

        assessment = assess_delivery_verification(
            contract, run_dir, _ctx(str(co)), None, co,
        )

        assert assessment is not None
        assert assessment.required_failed == ()
        assert assessment.blocking is False

    def test_manual_only_provenance_gate_stays_non_blocking(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        contract = _prov_delivery_contract(manual=True)
        _receipt(run_dir, "env-provenance")
        _write_failed_prov_phase_receipt(run_dir)

        assessment = assess_delivery_verification(
            contract, run_dir, _ctx(str(co)), None, co,
        )

        assert assessment is not None
        # Downgraded by the overlay, but routed to manual-only — never blocking.
        assert assessment.blocking is False
        assert assessment.required_failed == ()
        assert "env-provenance" in assessment.manual_only_commands
