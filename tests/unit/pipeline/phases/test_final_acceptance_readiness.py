"""Stage 5 readiness wiring into the final_acceptance prompt (ADR 0082).

Covers ``_verification_readiness_text`` (dry-run short-circuit BEFORE any
receipt loader, no-contract byte-identity) and the builder/adapters slot
(``readiness_summary`` -> ``verification_readiness`` part, no collision
with ``verification_receipt``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from pipeline.evidence.verification_receipt import write_command_receipt
from pipeline.phases.builtin.review_support import _verification_readiness_text
from pipeline.plugins import PluginConfig
from pipeline.prompts.builders import runtime_review_uncommitted_prompt
from pipeline.runtime import PipelineState
from pipeline.verification_contract import (
    PlaceholderContext,
    VerificationContract,
)
from pipeline.verification_readiness import READINESS_POLICY_LINE
from pipeline.verification_subject import VerificationSubjectAvailable, capture_verification_subject

_BLOCK_MARKER = "Verification readiness — final_acceptance:"


def _contract() -> VerificationContract:
    contract = VerificationContract.from_plugin(PluginConfig(
        verification={
            "commands": {"test": {"run": "pytest -q {checkout}"}},
            "required": ["test"],
        },
    ))
    assert contract is not None
    return contract


def _state(
    *,
    output_dir: Path | None,
    contract: VerificationContract | None,
    dry_run: bool = False,
    placeholders: PlaceholderContext | None = None,
) -> PipelineState:
    extras: dict = {"run_id": "20260101_000000"}
    if contract is not None:
        extras["verification_contract"] = contract
        extras["verification_placeholders"] = (
            placeholders if placeholders is not None else PlaceholderContext()
        )
    st = PipelineState(
        task="t", project_dir="/checkout", plugin=PluginConfig(),
        extras=extras,
    )
    st.output_dir = output_dir
    st.dry_run = dry_run
    return st


def _write_missing_receipt_run(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    return run_dir


def _repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for argv in (
        ["git", "init", "-q"], ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(argv, cwd=path, check=True)
    (path / "base").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=path, check=True)


def _subject(checkout: Path):
    captured = capture_verification_subject(checkout)
    assert isinstance(captured, VerificationSubjectAvailable)
    return captured


# ── _verification_readiness_text ───────────────────────────────────────


class TestReadinessText:
    def test_renders_block_with_declared_contract(self, tmp_path: Path) -> None:
        run_dir = _write_missing_receipt_run(tmp_path)
        text = _verification_readiness_text(
            _state(output_dir=run_dir, contract=_contract()),
        )
        assert _BLOCK_MARKER in text
        # This contract has no schedule / delivery_policy → a warn boundary, so
        # the missing 'test' gate is a warn gap: surfaced with its policy and
        # shipping-allowed, not a 'missing required' blocker (T3, ADR 0097).
        assert "test (warn) — shipping allowed by policy" in text
        assert "missing required: test" not in text
        assert READINESS_POLICY_LINE in text

    def test_present_receipt_reaches_block(self, tmp_path: Path) -> None:
        run_dir = _write_missing_receipt_run(tmp_path)
        checkout = tmp_path / "checkout"
        _repo(checkout)
        write_command_receipt(output_dir=run_dir, result={
            "command": "test", "env": "", "cwd": "/cwd",
            "placeholders": {"checkout": str(checkout), "project": ""},
            "argv": ["pytest"], "assertions": [], "exit_code": 0,
            "duration_s": 0.1, "parity": "absolute", "detail": "",
            "git": {"checkout_head": None, "baseline_head": None,
                    "changed_files_fingerprint": None},
            "subject": _subject(checkout), "dependencies": [],
        })
        text = _verification_readiness_text(
            _state(output_dir=run_dir, contract=_contract(),
                   placeholders=PlaceholderContext(checkout=str(checkout))),
        )
        assert "(none — declared proof complete)" in text

    def test_empty_without_contract(self, tmp_path: Path) -> None:
        run_dir = _write_missing_receipt_run(tmp_path)
        assert _verification_readiness_text(
            _state(output_dir=run_dir, contract=None),
        ) == ""

    def test_empty_without_output_dir(self) -> None:
        assert _verification_readiness_text(
            _state(output_dir=None, contract=_contract()),
        ) == ""

    def test_dry_run_short_circuits_before_loaders(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """state.dry_run=True returns '' WITHOUT touching any receipt
        loader — the short-circuit sits before all run-dir reads."""
        import pipeline.verification_readiness as vr

        def _boom(*args, **kwargs):  # pragma: no cover — must never run
            raise AssertionError("receipt loader called under dry_run")

        monkeypatch.setattr(vr, "load_command_receipts", _boom)
        monkeypatch.setattr(vr, "load_env_assertion_receipts", _boom)
        run_dir = _write_missing_receipt_run(tmp_path)
        text = _verification_readiness_text(
            _state(output_dir=run_dir, contract=_contract(), dry_run=True),
        )
        assert text == ""


# ── cross-repo dependency staleness in the prompt (ADR 0084) ───────────


def _dep_repo(path: Path) -> str:
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
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def _dep_new_commit(path: Path) -> str:
    (path / "more.txt").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "c1"], cwd=path, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def _dep_receipt(
    run_dir: Path, checkout: Path, dep: Path, head: str, *, depends_on: bool,
) -> None:
    write_command_receipt(output_dir=run_dir, result={
        "command": "test", "env": "", "cwd": "/cwd",
        "placeholders": {"checkout": str(checkout), "project": ""},
        "argv": ["pytest"], "assertions": [], "exit_code": 0,
        "duration_s": 0.1, "parity": "absolute", "detail": "",
        "git": {"checkout_head": None, "baseline_head": None,
                "changed_files_fingerprint": None},
        "subject": _subject(checkout),
        "dependencies": [{
            "name": "shared", "path": str(dep), "head": head,
            "dirty": False, "changed_files_count": 0,
            "changed_files_fingerprint": "e", "depends_on": depends_on,
            "subject": _subject(dep),
        }],
    })


@pytest.mark.git_worktree
class TestDependencyStaleInPrompt:
    def test_dependency_head_move_surfaces_stale_reason_in_prompt(
        self, tmp_path: Path,
    ) -> None:
        dep = tmp_path / "dep"
        checkout = tmp_path / "checkout"
        old = _dep_repo(dep)
        _repo(checkout)
        run_dir = _write_missing_receipt_run(tmp_path)
        _dep_receipt(run_dir, checkout, dep, old, depends_on=True)
        _dep_new_commit(dep)

        ctx = PlaceholderContext(
            checkout=str(checkout), dependencies={"shared": str(dep)},
        )
        text = _verification_readiness_text(
            _state(output_dir=run_dir, contract=_contract(), placeholders=ctx),
        )

        assert _BLOCK_MARKER in text
        assert "dependency shared: observed_head_changed" in text

    def test_unmoved_dependency_is_not_stale_in_prompt(
        self, tmp_path: Path,
    ) -> None:
        dep = tmp_path / "dep"
        checkout = tmp_path / "checkout"
        old = _dep_repo(dep)
        _repo(checkout)
        run_dir = _write_missing_receipt_run(tmp_path)
        _dep_receipt(run_dir, checkout, dep, old, depends_on=True)  # no HEAD move

        ctx = PlaceholderContext(
            checkout=str(checkout), dependencies={"shared": str(dep)},
        )
        text = _verification_readiness_text(
            _state(output_dir=run_dir, contract=_contract(), placeholders=ctx),
        )

        assert "HEAD moved" not in text
        assert "(none — declared proof complete)" in text
        assert f"tested against shared@{old[:12]}" in text


# ── builder slot + byte-identity ───────────────────────────────────────


class TestPromptInjection:
    def test_block_lands_in_review_prompt(self) -> None:
        body = (
            f"{_BLOCK_MARKER}\n  Missing receipts:\n    test\n"
            f"  Policy: {READINESS_POLICY_LINE}"
        )
        turn = runtime_review_uncommitted_prompt(
            "focus", project_dir="/checkout",
            verification_readiness=body,
            output_contract="release",
        )
        assert _BLOCK_MARKER in turn.text
        assert READINESS_POLICY_LINE in turn.text

    def test_no_contract_prompt_byte_identical(self) -> None:
        baseline = runtime_review_uncommitted_prompt(
            "focus", project_dir="/checkout", output_contract="release",
        )
        with_empty = runtime_review_uncommitted_prompt(
            "focus", project_dir="/checkout",
            verification_readiness="",
            output_contract="release",
        )
        assert with_empty.text == baseline.text
        assert _BLOCK_MARKER not in baseline.text

    def test_readiness_part_does_not_collide_with_receipt_part(self) -> None:
        turn = runtime_review_uncommitted_prompt(
            "focus", project_dir="/checkout",
            verification_receipt="Developer-side verification environment: x",
            verification_readiness=f"{_BLOCK_MARKER}\n  data",
        )
        kinds = [p.kind for p in turn.parts]
        assert "verification_receipt" in kinds
        assert "verification_readiness" in kinds
        assert "Developer-side verification environment" in turn.text
        assert _BLOCK_MARKER in turn.text


# ── adapters.run_review proxying ───────────────────────────────────────


class TestRunReviewProxy:
    def test_readiness_summary_reaches_builder(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pipeline.phases import adapters

        captured: dict = {}

        def _fake_invoke(agent, turn, cwd, **kwargs):
            captured["text"] = turn.text
            return '{"verdict": "APPROVED", "findings": []}'

        monkeypatch.setattr(adapters, "_invoke_turn", _fake_invoke)
        body = f"{_BLOCK_MARKER}\n  Missing receipts:\n    test"
        result = adapters.run_review(
            object(), "[final_acceptance] t", "/checkout", PluginConfig(),
            label="final_acceptance",
            output_contract="release",
            readiness_summary=body,
        )
        assert result.name == "final_acceptance"
        assert _BLOCK_MARKER in captured["text"]

    def test_dry_run_skips_builder_entirely(self) -> None:
        from pipeline.phases import adapters

        result = adapters.run_review(
            object(), "t", "/checkout", PluginConfig(),
            dry_run=True,
            label="final_acceptance",
            output_contract="release",
            readiness_summary="should never land anywhere",
        )
        assert result.meta.get("dry_run") is True
        assert "should never land" not in result.output
