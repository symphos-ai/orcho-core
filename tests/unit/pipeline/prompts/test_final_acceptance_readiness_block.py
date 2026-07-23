"""Stage 5 final-acceptance verification-readiness summary (ADR 0082).

Covers the pure readiness module: required-command extraction
(contract.required + before_delivery + after_phase(implement)),
present / missing / failed / stale classification against real
command-receipts and a real git checkout, the authoritative delivery
gate plan (executable routing epoch or fresh selection — never the
advisory prompt preview), the exploratory-command count, and the
rendered block (sections, verbatim policy, Remaining before ready).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from pipeline.evidence.verification_receipt import (
    write_command_receipt,
    write_env_assertion_receipt,
)
from pipeline.plugins import PluginConfig
from pipeline.project.verification_timeline import (
    build_verification_timeline,
    render_verification_gate_done_block,
)
from pipeline.verification_contract import (
    PlaceholderContext,
    VerificationContract,
)
from pipeline.verification_dependencies import changed_files_fingerprint
from pipeline.verification_readiness import (
    READINESS_POLICY_LINE,
    ROUTING_PLANS_EXTRAS_KEY,
    ReadinessSummary,
    build_final_acceptance_readiness,
    delivery_gate_plan,
    render_readiness_block,
    resolve_delivery_selection,
)
from pipeline.verification_receipt_index import (
    VERIFICATION_PARENT_RUNS_EXTRAS_KEY,
    receipt_file_path,
)
from pipeline.verification_selection import (
    SelectionContext,
    build_scheduled_gate_plan,
)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _contract(**verification_extra) -> VerificationContract:
    verification = {
        "default_env": "ci",
        "commands": {
            "lint": {"run": "ruff check {checkout}", "env": "ci"},
            "test": {"run": "pytest -q {checkout}"},
            "smoke": {"run": "pytest -q {checkout}/smoke"},
        },
        "required": ["test"],
        "schedule": [
            {"after_phase": "implement", "commands": ["lint"]},
            {"before_delivery": True, "policy": "require",
             "commands": ["smoke"]},
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


def _selection_contract() -> VerificationContract:
    """Contract whose ``smoke`` gate is selected only via a paths rule."""
    return _contract(
        gate_sets={
            "core": {"commands": ["lint", "test"]},
            "pathset": {"commands": ["smoke"]},
        },
        selection=[
            {"always": ["core"]},
            {"paths": ["src/*"], "include": ["pathset"]},
        ],
        schedule=[
            {"after_phase": "implement", "commands": ["lint"]},
            {"before_delivery": True, "policy": "require",
             "gate_sets": ["pathset"]},
        ],
    )


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
    from pipeline.verification_subject import (
        VerificationSubjectAvailable,
        VerificationSubjectIdentity,
        capture_verification_subject,
    )
    candidate = run_dir.parent / "checkout"
    captured = capture_verification_subject(candidate) if candidate.is_dir() else None
    subject = captured if isinstance(captured, VerificationSubjectAvailable) else None
    if subject is not None and fingerprint is not None and fingerprint != changed_files_fingerprint(str(candidate)):
        identity = subject.identity
        tree = "0" * len(identity.tree_oid) if identity.tree_oid != "0" * len(identity.tree_oid) else "1" * len(identity.tree_oid)
        subject = VerificationSubjectAvailable(VerificationSubjectIdentity(identity.version, identity.object_format, tree, identity.observed_head_oid, identity.baseline_oid))
    if subject is not None and checkout_head is not None and checkout_head != subject.identity.observed_head_oid:
        identity = subject.identity
        subject = VerificationSubjectAvailable(VerificationSubjectIdentity(identity.version, identity.object_format, identity.tree_oid, checkout_head, identity.baseline_oid))
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
        "subject": subject,
        "dependencies": dependencies or [],
    })


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
    dirty: bool = False,
) -> dict:
    from pipeline.verification_subject import capture_verification_subject

    return {
        "name": name,
        "path": str(path),
        "head": head,
        "dirty": dirty,
        "changed_files_count": 1 if dirty else 0,
        "changed_files_fingerprint": "f" if dirty else "e",
        "depends_on": depends_on,
        "subject": capture_verification_subject(path),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Required-command extraction (pure)
# ─────────────────────────────────────────────────────────────────────────────


class TestRequiredDeliveryCommands:
    def test_schedule_fallback_unions_required_and_delivery_hooks(self) -> None:
        contract = _contract()
        # contract.required first, then after_phase(implement), then
        # before_delivery — deduped, deterministic.
        assert resolve_delivery_selection(contract, None).receipt_commands == (
            "test", "lint", "smoke",
        )

    def test_plan_entries_win_over_raw_schedule(self) -> None:
        contract = _selection_contract()
        plan = build_scheduled_gate_plan(
            contract,
            SelectionContext(touched_paths=("src/x.py",), work_mode="governed"),
        )
        commands = resolve_delivery_selection(contract, plan).receipt_commands
        assert commands[0] == "test"
        assert "smoke" in commands and "lint" in commands

    def test_plan_without_path_match_omits_path_selected_gate(self) -> None:
        contract = _selection_contract()
        plan = build_scheduled_gate_plan(
            contract, SelectionContext(work_mode="governed"),
        )
        assert "smoke" not in resolve_delivery_selection(contract, plan).receipt_commands

    def test_blanket_schedule_entry_covers_all_declared_commands(self) -> None:
        contract = VerificationContract.from_plugin(PluginConfig(
            verification={
                "commands": {
                    "b": {"run": "echo b"}, "a": {"run": "echo a"},
                },
                "schedule": [{"before_delivery": True}],
            },
        ))
        assert contract is not None
        assert resolve_delivery_selection(contract, None).receipt_commands == ("a", "b")


# ─────────────────────────────────────────────────────────────────────────────
# Classification
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.git_worktree
class TestClassification:
    def test_present_missing_failed_stale(self, tmp_path: Path) -> None:
        co = _git_checkout(tmp_path)
        head = _git_head(co)
        fingerprint = changed_files_fingerprint(str(co))
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        contract = _contract(required=["test", "lint", "smoke", "extra"],
                             commands={
                                 "lint": {"run": "x"}, "test": {"run": "x"},
                                 "smoke": {"run": "x"}, "extra": {"run": "x"},
                             })
        _receipt(run_dir, "test", fingerprint=fingerprint, checkout_head=head)
        _receipt(run_dir, "lint", exit_code=1,
                 fingerprint=fingerprint, checkout_head=head)
        _receipt(run_dir, "smoke", fingerprint="deadbeefdeadbeef",
                 checkout_head=head)
        # "extra" has no receipt → missing.

        summary = build_final_acceptance_readiness(
            contract, run_dir, PlaceholderContext(checkout=str(co)),
        )
        assert summary.required_present == ("test",)
        assert summary.required_failed == ("lint",)
        assert summary.required_stale == ("smoke",)
        assert summary.required_missing == ("extra",)

    def test_head_mismatch_is_stale(self, tmp_path: Path) -> None:
        co = _git_checkout(tmp_path)
        fingerprint = changed_files_fingerprint(str(co))
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        contract = _contract()
        _receipt(run_dir, "test", fingerprint=fingerprint,
                 checkout_head="0" * 40)
        summary = build_final_acceptance_readiness(
            contract, run_dir, PlaceholderContext(checkout=str(co)),
        )
        assert "test" in summary.required_stale

    def test_failed_assertion_and_detail_classify_failed(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        head = _git_head(co)
        fingerprint = changed_files_fingerprint(str(co))
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        contract = _contract(required=["test", "lint"])
        _receipt(run_dir, "test", fingerprint=fingerprint, checkout_head=head,
                 assertions=[{"name": "a", "kind": "k", "passed": False}])
        _receipt(run_dir, "lint", fingerprint=fingerprint, checkout_head=head,
                 detail="command timed out")
        summary = build_final_acceptance_readiness(
            contract, run_dir, PlaceholderContext(checkout=str(co)),
        )
        assert set(summary.required_failed) == {"test", "lint"}

    def test_no_checkout_is_unverifiable(
        self, tmp_path: Path,
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        contract = _contract()
        _receipt(run_dir, "test", fingerprint="deadbeefdeadbeef",
                 checkout_head="0" * 40)
        summary = build_final_acceptance_readiness(
            contract, run_dir, PlaceholderContext(checkout=""),
        )
        assert summary.required_stale == ("test",)


# ─────────────────────────────────────────────────────────────────────────────
# Cross-repo dependency staleness (ADR 0084)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.git_worktree
class TestDependencyStaleClassification:
    """A depended-on dependency's HEAD move marks the receipt stale with a
    reason. Each receipt has a usable checkout subject so these tests isolate
    the dependency dimension."""

    def test_dependency_head_move_marks_stale_with_reason(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        dep = tmp_path / "dep"
        old = _dep_repo(dep)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _receipt(
            run_dir, "test",
            dependencies=[_dep_record("shared", dep, old, depends_on=True)],
        )
        _dep_new_commit(dep)  # HEAD moves AFTER the receipt is written

        summary = build_final_acceptance_readiness(
            _contract(), run_dir,
            PlaceholderContext(
                checkout=str(co), dependencies={"shared": str(dep)},
            ),
        )

        assert summary.required_stale == ("test",)
        assert summary.stale_reasons == (
            "test: dependency shared: observed_head_changed",
        )
        rendered = render_readiness_block(summary)
        assert rendered is not None
        assert "dependency shared: observed_head_changed" in rendered

    def test_depends_on_false_is_not_stale(self, tmp_path: Path) -> None:
        co = _git_checkout(tmp_path)
        dep = tmp_path / "dep"
        old = _dep_repo(dep)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _receipt(
            run_dir, "test",
            dependencies=[_dep_record("shared", dep, old, depends_on=False)],
        )
        _dep_new_commit(dep)

        summary = build_final_acceptance_readiness(
            _contract(), run_dir,
            PlaceholderContext(checkout=str(co), dependencies={"shared": str(dep)}),
        )

        assert "test" in summary.required_present
        assert not summary.required_stale

    def test_receipt_without_dependencies_block_is_not_stale(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        dep = tmp_path / "dep"
        _dep_repo(dep)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _receipt(run_dir, "test")  # no dependencies block at all
        _dep_new_commit(dep)

        summary = build_final_acceptance_readiness(
            _contract(), run_dir,
            PlaceholderContext(checkout=str(co), dependencies={"shared": str(dep)}),
        )

        assert "test" in summary.required_present
        assert not summary.required_stale

    def test_dependency_path_not_git_is_unverifiable(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        plain = tmp_path / "plain"
        plain.mkdir()  # not a git repo → current HEAD unavailable
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _receipt(
            run_dir, "test",
            dependencies=[
                _dep_record("shared", plain, "0" * 40, depends_on=True),
            ],
        )

        summary = build_final_acceptance_readiness(
            _contract(), run_dir,
            PlaceholderContext(checkout=str(co), dependencies={"shared": str(plain)}),
        )

        assert summary.required_stale == ("test",)
        assert summary.stale_reasons == (
            "test: dependency shared: usable_subject_identity_unavailable",
        )

    def test_dirty_only_dependency_is_stale(self, tmp_path: Path) -> None:
        co = _git_checkout(tmp_path)
        dep = tmp_path / "dep"
        old = _dep_repo(dep)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _receipt(
            run_dir, "test",
            dependencies=[_dep_record("shared", dep, old, depends_on=True)],
        )
        # Dirty the dependency WITHOUT a new commit — HEAD is unchanged.
        (dep / "f.txt").write_text("dirty\n", encoding="utf-8")

        summary = build_final_acceptance_readiness(
            _contract(), run_dir,
            PlaceholderContext(checkout=str(co), dependencies={"shared": str(dep)}),
        )

        assert summary.required_stale == ("test",)
        assert summary.stale_reasons == (
            "test: dependency shared: worktree_tree_changed",
        )

    def test_present_receipt_surfaces_tested_dependency_commit(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        dep = tmp_path / "dep"
        old = _dep_repo(dep)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _receipt(
            run_dir, "test",
            dependencies=[_dep_record("shared", dep, old, depends_on=True)],
        )
        # No HEAD move → present, and the tested dependency commit is surfaced.

        summary = build_final_acceptance_readiness(
            _contract(), run_dir,
            PlaceholderContext(checkout=str(co), dependencies={"shared": str(dep)}),
        )

        assert "test" in summary.required_present
        assert summary.tested_dependency_lines == (
            f"test: tested against shared@{old[:12]}",
        )
        rendered = render_readiness_block(summary)
        assert rendered is not None
        assert "Tested dependency commits:" in rendered
        assert f"tested against shared@{old[:12]}" in rendered


# ─────────────────────────────────────────────────────────────────────────────
# Authoritative plan source (ADR 0082 / review F1)
# ─────────────────────────────────────────────────────────────────────────────


class TestDeliveryGatePlanSource:
    def test_routing_key_matches_gate_repair(self) -> None:
        from pipeline.project.gate_repair import (
            VERIFICATION_GATE_ROUTING_PLANS_KEY,
        )

        assert ROUTING_PLANS_EXTRAS_KEY == VERIFICATION_GATE_ROUTING_PLANS_KEY

    def test_prefers_executable_before_delivery_epoch(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        contract = _selection_contract()
        routed = build_scheduled_gate_plan(
            contract,
            SelectionContext(touched_paths=("src/x.py",), work_mode="governed"),
        )
        extras = {ROUTING_PLANS_EXTRAS_KEY: {"before_delivery:": routed}}
        monkeypatch.setattr(
            "pipeline.verification_selection.build_scheduled_gate_plan",
            lambda *_args: pytest.fail("readiness rebuilt recorded delivery epoch"),
        )
        plan = delivery_gate_plan(contract, extras, checkout="")
        assert plan is routed

    @pytest.mark.git_worktree
    def test_stale_prompt_preview_is_ignored(self, tmp_path: Path) -> None:
        """Regression for review F1: a prompt-preview plan cached before
        ``implement`` (no changed files → path-selected gate absent) must
        not be reused — readiness rebuilds delivery selection from the
        current checkout and reports the path-selected gate as missing."""
        contract = _selection_contract()
        co = _git_checkout(tmp_path)
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        # Early advisory preview, built before implement mutated the tree.
        stale_preview = build_scheduled_gate_plan(
            contract, SelectionContext(work_mode="governed"),
        )
        assert "smoke" not in stale_preview.selected_commands
        extras = {"verification_gate_prompt_preview": stale_preview}

        # implement later touches a path that selects the pathset gate.
        (co / "src").mkdir()
        (co / "src" / "x.py").write_text("x = 1\n", encoding="utf-8")

        summary = build_final_acceptance_readiness(
            contract, run_dir, PlaceholderContext(checkout=str(co)),
            extras=extras,
        )
        assert "smoke" in summary.required_missing
        rendered = render_readiness_block(summary)
        assert rendered is not None
        # smoke is scheduled require at before_delivery → a require gap.
        assert "missing required: smoke" in rendered

    @pytest.mark.git_worktree
    def test_recorded_epoch_wins_over_stale_prompt_preview(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A preview is advisory even when it disagrees with the durable plan."""
        contract = _selection_contract()
        co = _git_checkout(tmp_path)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        stale_preview = build_scheduled_gate_plan(
            contract, SelectionContext(work_mode="governed"),
        )
        recorded = build_scheduled_gate_plan(
            contract, SelectionContext(touched_paths=("src/x.py",), work_mode="governed"),
        )
        assert "smoke" not in stale_preview.selected_commands
        assert "smoke" in recorded.selected_commands
        extras = {
            "verification_gate_prompt_preview": stale_preview,
            ROUTING_PLANS_EXTRAS_KEY: {"before_delivery:": recorded},
        }
        monkeypatch.setattr(
            "pipeline.verification_selection.build_scheduled_gate_plan",
            lambda *_args: pytest.fail("readiness rebuilt recorded delivery epoch"),
        )

        summary = build_final_acceptance_readiness(
            contract, run_dir, PlaceholderContext(checkout=str(co)), extras=extras,
        )

        assert "smoke" in summary.required_missing


# ─────────────────────────────────────────────────────────────────────────────
# Env status + exploratory summary
# ─────────────────────────────────────────────────────────────────────────────


class TestEnvAndExploratory:
    def test_env_statuses_from_env_receipts(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        write_env_assertion_receipt(output_dir=run_dir, result={
            "subject": {"checkout": "/co", "project": "/co", "env": "ci"},
            "cwd": "/co", "interpreter": "3.12", "env_overrides": {},
            "assertions": [], "all_passed": True,
        })
        summary = build_final_acceptance_readiness(
            _contract(), run_dir, PlaceholderContext(),
        )
        assert summary.env_statuses == (("ci", True),)

    def test_exploratory_counts_command_end_events(
        self, tmp_path: Path,
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        events = [
            {"kind": "command.start", "payload": {}},
            {"kind": "command.end", "payload": {"exit_code": 0}},
            {"kind": "command.end", "payload": {"exit_code": 1}},
            {"kind": "phase.end", "payload": {}},
        ]
        run_dir.joinpath("events.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events) + "\nnot json\n",
            encoding="utf-8",
        )
        summary = build_final_acceptance_readiness(
            _contract(), run_dir, PlaceholderContext(),
        )
        assert summary.exploratory_count == 2
        assert "not authoritative" in summary.exploratory_note


# ─────────────────────────────────────────────────────────────────────────────
# Rendering
# ─────────────────────────────────────────────────────────────────────────────


class TestRender:
    def test_none_for_empty_summary(self) -> None:
        assert render_readiness_block(ReadinessSummary()) is None

    def test_sections_policy_and_remaining_nonempty(
        self, tmp_path: Path,
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        write_env_assertion_receipt(output_dir=run_dir, result={
            "subject": {"checkout": "/co", "project": "/co", "env": "ci"},
            "cwd": "/co", "interpreter": "3.12", "env_overrides": {},
            "assertions": [], "all_passed": False,
        })
        _receipt(run_dir, "test", exit_code=1)
        summary = build_final_acceptance_readiness(
            _contract(), run_dir, PlaceholderContext(),
        )
        rendered = render_readiness_block(summary)
        assert rendered is not None
        assert rendered.startswith("Verification readiness — final_acceptance:")
        for section in (
            "Environments:", "Scheduled gates:", "Required receipts (present):",
            "Missing receipts:", "Failed receipts:", "Stale receipts:",
            "Exploratory commands:", "Remaining before ready:",
        ):
            assert section in rendered
        assert "ci: FAIL" in rendered
        # Default contract derives a require boundary (smoke before_delivery
        # require), and with no gate_sets the plan is None so every required
        # command takes that boundary policy → all require gaps.
        assert "failed required: test" in rendered
        assert "missing required: lint" in rendered
        assert "missing required: smoke" in rendered
        # The gap sections annotate each entry with its effective policy.
        assert "lint (require)" in rendered
        assert READINESS_POLICY_LINE in rendered

    @pytest.mark.git_worktree
    def test_remaining_empty_when_all_receipts_valid(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        head = _git_head(co)
        fingerprint = changed_files_fingerprint(str(co))
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        contract = _contract()
        for command in ("test", "lint", "smoke"):
            _receipt(run_dir, command, fingerprint=fingerprint,
                     checkout_head=head)
        summary = build_final_acceptance_readiness(
            contract, run_dir, PlaceholderContext(checkout=str(co)),
        )
        rendered = render_readiness_block(summary)
        assert rendered is not None
        assert "(none — declared proof complete)" in rendered
        assert READINESS_POLICY_LINE in rendered

    def test_gate_status_lines_show_hook_and_status(
        self, tmp_path: Path,
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        summary = build_final_acceptance_readiness(
            _contract(), run_dir, PlaceholderContext(),
        )
        rendered = render_readiness_block(summary)
        assert rendered is not None
        assert "smoke [before_delivery/require]: missing" in rendered
        assert "lint [after_phase(implement)/derived]: missing" in rendered


# ─────────────────────────────────────────────────────────────────────────────
# Per-gate policy-aware rendering (T3, ADR 0097)
# ─────────────────────────────────────────────────────────────────────────────


class TestPolicyAwareReadiness:
    def test_require_missing_gate_is_missing_required_and_blocks(
        self, tmp_path: Path,
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        contract = _contract(
            commands={"test": {"run": "pytest -q {checkout}"}},
            required=["test"], schedule=[], delivery_policy="require",
        )
        summary = build_final_acceptance_readiness(
            contract, run_dir, PlaceholderContext(),
        )
        assert "test" in summary.required_missing
        rendered = render_readiness_block(summary)
        assert rendered is not None
        assert "test (require)" in rendered
        # The require gap is named 'missing required' and lands in Remaining.
        assert "missing required: test" in rendered

    def test_warn_missing_gate_is_warning_and_shipping_allowed(
        self, tmp_path: Path,
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        contract = _contract(
            commands={"test": {"run": "pytest -q {checkout}", "env": "ci"}},
            required=["test"], schedule=[], delivery_policy="warn",
        )
        summary = build_final_acceptance_readiness(
            contract, run_dir, PlaceholderContext(),
        )
        assert summary.required_missing == ("test",)
        assert dict(summary.policy_by_command)["test"] == "warn"
        rendered = render_readiness_block(summary)
        assert rendered is not None
        # The warn gap names its policy and is explicitly shipping-allowed.
        assert "test (warn) — shipping allowed by policy" in rendered
        # A warn gap is NOT a 'missing required' blocker and is not in Remaining.
        assert "missing required: test" not in rendered
        assert "(none blocking — " in rendered

    def test_manual_only_gate_is_visible_not_required_and_not_auto_run(
        self, tmp_path: Path,
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        contract = _contract(
            commands={
                "test": {"run": "pytest -q {checkout}"},
                "audit": {"run": "audit {checkout}"},
            },
            required=["test", "audit"],
            schedule=[{"manual_only": True, "commands": ["audit"]}],
            delivery_policy="require",
        )
        summary = build_final_acceptance_readiness(
            contract, run_dir, PlaceholderContext(),
        )
        # audit is excluded from every required gap bucket; test still required.
        assert "audit" not in summary.required_missing
        assert "audit" not in summary.required_failed
        assert "audit" not in summary.required_stale
        assert summary.operator_gaps == ("audit",)
        assert "test" in summary.required_missing
        rendered = render_readiness_block(summary)
        assert rendered is not None
        # Visible, marked not auto-run, and never in Remaining before ready.
        assert "audit (manual) — operator available, not auto-run" in rendered
        remaining = rendered.split("Remaining before ready:", 1)[1]
        assert "audit" not in remaining

    @pytest.mark.parametrize(
        ("policy", "expected_fragment", "operator_owned"),
        (
            ("manual", "operator available, not auto-run", True),
            ("suggest", "operator recommendation", False),
        ),
    )
    def test_selected_operator_owned_delivery_policies_are_not_required_residuals(
        self,
        tmp_path: Path,
        policy: str,
        expected_fragment: str,
        operator_owned: bool,
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        contract = _contract(
            commands={"test": {"run": "pytest -q {checkout}", "env": "ci"}},
            required=["test"],
            gate_sets={"delivery": {"commands": ["test"]}},
            selection=[{"always": ["delivery"]}],
            schedule=[{
                "before_delivery": True,
                "policy": policy,
                "commands": ["test"],
            }],
            delivery_policy="require",
        )
        plan = build_scheduled_gate_plan(contract, SelectionContext(work_mode="governed"))

        summary = build_final_acceptance_readiness(
            contract, run_dir, PlaceholderContext(), plan=plan,
        )
        rendered = render_readiness_block(summary)

        assert rendered is not None
        assert expected_fragment in rendered
        if operator_owned:
            assert summary.operator_gaps == ("test",)
        else:
            assert summary.required_missing == ("test",)
        assert "missing required: test" not in rendered

    def test_no_blocker_block_is_byte_identical_to_legacy(
        self, tmp_path: Path,
    ) -> None:
        # A fully-proven readiness block must carry NONE of the new policy
        # surfaces — no per-gate annotation, no shipping note, no manual-only
        # section, no 'missing required' wording.
        co = _git_checkout(tmp_path)
        head = _git_head(co)
        fingerprint = changed_files_fingerprint(str(co))
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        for command in ("test", "lint", "smoke"):
            _receipt(run_dir, command, fingerprint=fingerprint,
                     checkout_head=head)
        summary = build_final_acceptance_readiness(
            _contract(), run_dir, PlaceholderContext(checkout=str(co)),
        )
        rendered = render_readiness_block(summary)
        assert rendered is not None
        for token in (
            "shipping allowed by policy", "(require)", "(warn)",
            "Manual-only receipts:", "missing required:", "(none blocking",
        ):
            assert token not in rendered
        assert "  Missing receipts:\n    (none)" in rendered
        assert "  Remaining before ready:\n    (none — declared proof complete)" in (
            rendered
        )


# ─────────────────────────────────────────────────────────────────────────────
# Diagnostics: searched dirs, provenance, actionable hints (ADR 0089 / T3)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.git_worktree
class TestReadinessDiagnostics:
    def test_missing_block_names_searched_dirs_and_both_verify_commands(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        run_dir = tmp_path / "run_20260613_XYZ"
        run_dir.mkdir()
        # No receipts → all required commands missing.
        summary = build_final_acceptance_readiness(
            _contract(), run_dir,
            PlaceholderContext(checkout=str(co), project=str(co)),
        )
        assert "test" in summary.required_missing
        assert summary.searched_run_dirs == (str(run_dir),)

        rendered = render_readiness_block(summary)
        assert rendered is not None
        run_id = run_dir.name
        assert "  Searched run dirs:" in rendered
        assert str(run_dir) in rendered
        assert "  Suggested verification:" in rendered
        assert (
            f"orcho verify env --env ci --run-id {run_id} --project {co}"
            in rendered
        )
        assert (
            f"orcho verify run test lint smoke --run-id {run_id} --project {co}"
            in rendered
        )

    def test_missing_selected_gate_hint_names_exact_commands(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        (co / "src").mkdir()
        (co / "src" / "feature.py").write_text("x = 1\n", encoding="utf-8")
        run_dir = tmp_path / "run_20260613_SELECTED"
        run_dir.mkdir()

        summary = build_final_acceptance_readiness(
            _selection_contract(), run_dir,
            PlaceholderContext(checkout=str(co), project=str(co)),
        )

        assert set(summary.required_missing) == {"test", "lint", "smoke"}
        rendered = render_readiness_block(summary)
        assert rendered is not None
        expected = (
            f"orcho verify run test lint smoke --run-id {run_dir.name} "
            f"--project {co}"
        )
        assert expected in rendered
        assert "orcho verify run --required" not in rendered

    def test_inherited_parent_present_receipt_shows_provenance(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        head = _git_head(co)
        fingerprint = changed_files_fingerprint(str(co))
        child_run = tmp_path / "child_run"
        child_run.mkdir()
        parent_run = tmp_path / "parent_run_20260612"
        parent_run.mkdir()
        # Child has NO receipt; the parent has a valid present one.
        _receipt(parent_run, "test", fingerprint=fingerprint, checkout_head=head)

        summary = build_final_acceptance_readiness(
            _contract(required=["test"]), child_run,
            PlaceholderContext(checkout=str(co), project=str(co)),
            extras={
                VERIFICATION_PARENT_RUNS_EXTRAS_KEY: (
                    (parent_run.name, str(parent_run)),
                ),
            },
        )
        assert summary.required_present == ("test",)
        # Provenance names the parent run id + the inherited receipt path.
        expected = (
            f"test: present from run {parent_run.name} "
            f"({receipt_file_path(parent_run, 'test')})"
        )
        assert summary.receipt_provenance == (expected,)

        rendered = render_readiness_block(summary)
        assert rendered is not None
        assert "  Inherited receipt provenance:" in rendered
        assert expected in rendered

    def test_no_parent_present_receipt_has_no_provenance_line(
        self, tmp_path: Path,
    ) -> None:
        # A current-run present receipt must NOT add a provenance line — the
        # no-parent block stays byte-identical.
        co = _git_checkout(tmp_path)
        head = _git_head(co)
        fingerprint = changed_files_fingerprint(str(co))
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _receipt(run_dir, "test", fingerprint=fingerprint, checkout_head=head)
        # Minimal contract: only "test" required, nothing else scheduled, so the
        # single present receipt leaves zero missing/stale.
        contract = _contract(
            commands={"test": {"run": "x"}}, required=["test"], schedule=[],
        )
        summary = build_final_acceptance_readiness(
            contract, run_dir,
            PlaceholderContext(checkout=str(co), project=str(co)),
        )
        assert summary.required_present == ("test",)
        assert summary.receipt_provenance == ()
        rendered = render_readiness_block(summary)
        assert rendered is not None
        assert "Inherited receipt provenance:" not in rendered
        # No missing/stale → no diagnostics sections at all.
        assert "Searched run dirs:" not in rendered
        assert "Suggested verification:" not in rendered


# ─────────────────────────────────────────────────────────────────────────────
# Failed-only diagnostics + DONE↔readiness consistency (Stage 3 projection)
# ─────────────────────────────────────────────────────────────────────────────


def _failed_only_contract() -> VerificationContract:
    """Single required command, nothing else scheduled — so the only possible
    non-present residual is ``failed`` (no missing/stale partners)."""
    return _contract(
        commands={"test": {"run": "x"}}, required=["test"], schedule=[],
    )


@pytest.mark.git_worktree
class TestFailedOnlyDiagnostics:
    def test_failed_only_renders_searched_dirs_and_suggested(
        self, tmp_path: Path,
    ) -> None:
        """A required command whose ONLY non-present status is ``failed`` (no
        missing/stale) now drives the readiness diagnostics: ``Searched run
        dirs:`` and ``Suggested verification:`` name the failed command. This is
        the Part A widening — searched/suggested fire on missing OR stale OR
        failed."""
        co = _git_checkout(tmp_path)
        head = _git_head(co)
        fingerprint = changed_files_fingerprint(str(co))
        run_dir = tmp_path / "run_20260616_FAILED"
        run_dir.mkdir()
        # The sole required receipt is failed on the current diff (exit 1).
        _receipt(run_dir, "test", exit_code=1,
                 fingerprint=fingerprint, checkout_head=head)

        summary = build_final_acceptance_readiness(
            _failed_only_contract(), run_dir,
            PlaceholderContext(checkout=str(co), project=str(co)),
        )
        # failed-only: no missing/stale partners.
        assert summary.required_failed == ("test",)
        assert summary.required_missing == ()
        assert summary.required_stale == ()
        # The advisory surface now fires for failed alone.
        assert summary.suggested_commands  # non-empty
        assert "test" in str(summary.suggested_commands)

        rendered = render_readiness_block(summary)
        assert rendered is not None
        assert "  Searched run dirs:" in rendered
        assert str(run_dir) in rendered
        assert "  Suggested verification:" in rendered
        run_id = run_dir.name
        assert (
            f"orcho verify env --env ci --run-id {run_id} --project {co}"
            in rendered
        )

    def test_failed_only_done_and_readiness_share_identical_hint(
        self, tmp_path: Path,
    ) -> None:
        """The DONE timeline and the readiness block compute their
        ``orcho verify`` hint from the SAME ``suggested_verify_commands`` input
        (missing+stale+failed) — for a failed-only run they must be byte-equal,
        so the two operator surfaces can never contradict each other."""
        co = _git_checkout(tmp_path)
        head = _git_head(co)
        fingerprint = changed_files_fingerprint(str(co))
        run_dir = tmp_path / "run_20260616_CONSISTENCY"
        run_dir.mkdir()
        _receipt(run_dir, "test", exit_code=1,
                 fingerprint=fingerprint, checkout_head=head)
        contract = _failed_only_contract()
        ctx = PlaceholderContext(checkout=str(co), project=str(co))

        summary = build_final_acceptance_readiness(contract, run_dir, ctx)
        timeline = build_verification_timeline(
            run_dir=run_dir,
            extras={
                "verification_contract": contract,
                "verification_placeholders": ctx,
            },
        )
        assert timeline is not None
        # Identical hint set on both surfaces for the same failed-only input.
        assert summary.suggested_commands == timeline.suggested_commands
        assert summary.suggested_commands  # and non-empty

        done_block = "\n".join(render_verification_gate_done_block(timeline))
        for hint in summary.suggested_commands:
            assert hint in done_block
