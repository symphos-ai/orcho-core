"""Multi-run command-receipt inheritance (ADR 0089 / T1).

Covers :func:`pipeline.verification_readiness.classify_required_receipts` once it
searches a follow-up's parent run for a required command's receipt: the five
acceptance conditions for a parent candidate, the candidate-selection priority
(current present wins; a fresh same-diff failure blocks inheritance; an absent /
stale / foreign-diff current receipt degrades to a valid parent), and the
provenance carried onto every classification. The no-parent path is asserted to
be byte-identical to the single-run classification.
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
)
from pipeline.verification_dependencies import changed_files_fingerprint
from pipeline.verification_readiness import classify_required_receipts
from pipeline.verification_receipt_index import (
    VERIFICATION_PARENT_RUNS_EXTRAS_KEY,
    ReceiptSource,
    coerce_receipt_sources,
    parent_sources_from_extras,
    receipt_file_path,
)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _contract(**verification_extra) -> VerificationContract:
    verification = {
        "default_env": "ci",
        "commands": {
            "test": {"run": "pytest -q {checkout}"},
            "lint": {"run": "ruff check {checkout}", "env": "ci"},
        },
        "required": ["test"],
    }
    verification.update(verification_extra)
    contract = VerificationContract.from_plugin(
        PluginConfig(
            work_mode="governed",
            verification_envs={"ci": {}, "other": {}},
            verification=verification,
        ),
    )
    assert contract is not None
    return contract


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
    env: str = "ci",
    exit_code: int | None = 0,
    detail: str = "",
    fingerprint: str | None = None,
    checkout_head: str | None = None,
) -> None:
    write_command_receipt(output_dir=run_dir, result={
        "command": command,
        "env": env,
        "cwd": "/cwd",
        "placeholders": {"checkout": "/co", "project": "/co"},
        "argv": [command],
        "assertions": [],
        "exit_code": exit_code,
        "duration_s": 0.1,
        "parity": "absolute",
        "detail": detail,
        "git": {
            "checkout_head": checkout_head,
            "baseline_head": None,
            "changed_files_fingerprint": fingerprint,
        },
        "dependencies": [],
    })


def _classify(
    contract: VerificationContract,
    child_run: Path,
    co: Path,
    *,
    parent_runs=None,
    extras=None,
):
    return classify_required_receipts(
        contract, child_run, PlaceholderContext(checkout=str(co)),
        checkout=str(co), extras=extras, parent_runs=parent_runs,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Source coercion / extras key (pure)
# ─────────────────────────────────────────────────────────────────────────────


class TestSources:
    def test_extras_key_is_documented_constant(self) -> None:
        assert VERIFICATION_PARENT_RUNS_EXTRAS_KEY == "verification_parent_runs"

    def test_coerce_accepts_pairs_and_sources_and_skips_junk(self) -> None:
        sources = coerce_receipt_sources([
            ("r1", "/run/1"),
            ReceiptSource(run_id="r2", run_dir="/run/2"),
            {"run_id": "r3", "run_dir": "/run/3"},
            ("missing-dir",),          # too short → skipped
            ("r4", ""),                # empty run_dir → skipped
            "nonsense",                # wrong type → skipped
        ])
        assert sources == (
            ReceiptSource("r1", "/run/1"),
            ReceiptSource("r2", "/run/2"),
            ReceiptSource("r3", "/run/3"),
        )

    def test_coerce_tolerates_non_iterable_and_empty(self) -> None:
        assert coerce_receipt_sources(None) == ()
        assert coerce_receipt_sources("a-string") == ()
        assert coerce_receipt_sources(123) == ()

    def test_parent_sources_from_extras_reads_only_the_key(self) -> None:
        assert parent_sources_from_extras(None) == ()
        assert parent_sources_from_extras({"other": [("r", "/d")]}) == ()
        assert parent_sources_from_extras(
            {VERIFICATION_PARENT_RUNS_EXTRAS_KEY: [("r", "/d")]},
        ) == (ReceiptSource("r", "/d"),)

    def test_receipt_file_path_matches_writer_layout(self, tmp_path: Path) -> None:
        run = tmp_path / "run"
        run.mkdir()
        _receipt(run, "test")
        expected = run / "verification_command_receipts" / "test.json"
        assert receipt_file_path(run, "test") == str(expected)
        assert Path(receipt_file_path(run, "test")).is_file()


# ─────────────────────────────────────────────────────────────────────────────
# Parent inheritance + selection priority
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.git_worktree
class TestParentInheritance:
    def test_valid_parent_with_empty_child_is_present_from_parent(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        head = _git_head(co)
        fp = changed_files_fingerprint(str(co))
        child = tmp_path / "child"
        child.mkdir()
        parent = tmp_path / "parent"
        parent.mkdir()
        _receipt(parent, "test", fingerprint=fp, checkout_head=head)
        # child has NO receipt at all.

        result = _classify(
            _contract(), child, co,
            parent_runs=[("parent-run", str(parent))],
        )
        cls = result["test"]
        assert cls.status == "present"
        assert cls.source_run_id == "parent-run"
        assert cls.path == receipt_file_path(parent, "test")

    def test_current_stale_degrades_to_valid_parent(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        head = _git_head(co)
        fp = changed_files_fingerprint(str(co))
        child = tmp_path / "child"
        child.mkdir()
        parent = tmp_path / "parent"
        parent.mkdir()
        # child receipt is stale: foreign fingerprint (different diff).
        _receipt(child, "test", fingerprint="deadbeefdeadbeef", checkout_head=head)
        _receipt(parent, "test", fingerprint=fp, checkout_head=head)

        cls = _classify(
            _contract(), child, co,
            parent_runs=[("parent-run", str(parent))],
        )["test"]
        assert cls.status == "present"
        assert cls.source_run_id == "parent-run"

    def test_current_failed_same_fingerprint_blocks_parent(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        head = _git_head(co)
        fp = changed_files_fingerprint(str(co))
        child = tmp_path / "child"
        child.mkdir()
        parent = tmp_path / "parent"
        parent.mkdir()
        # child FAILED against the SAME diff → fresh failure, must win.
        _receipt(child, "test", exit_code=1, fingerprint=fp, checkout_head=head)
        _receipt(parent, "test", fingerprint=fp, checkout_head=head)

        cls = _classify(
            _contract(), child, co,
            parent_runs=[("parent-run", str(parent))],
        )["test"]
        assert cls.status == "failed"
        assert cls.source_run_id == child.name
        assert cls.path == receipt_file_path(child, "test")

    def test_current_failed_foreign_fingerprint_yields_to_parent(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        head = _git_head(co)
        fp = changed_files_fingerprint(str(co))
        child = tmp_path / "child"
        child.mkdir()
        parent = tmp_path / "parent"
        parent.mkdir()
        # child failed against a DIFFERENT diff → not a fresh same-diff failure.
        _receipt(child, "test", exit_code=1,
                 fingerprint="deadbeefdeadbeef", checkout_head=head)
        _receipt(parent, "test", fingerprint=fp, checkout_head=head)

        cls = _classify(
            _contract(), child, co,
            parent_runs=[("parent-run", str(parent))],
        )["test"]
        assert cls.status == "present"
        assert cls.source_run_id == "parent-run"

    def test_parent_fingerprint_mismatch_is_stale_with_reason(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        head = _git_head(co)
        child = tmp_path / "child"
        child.mkdir()
        parent = tmp_path / "parent"
        parent.mkdir()
        # No child receipt; the parent's fingerprint does not match the subject.
        _receipt(parent, "test", fingerprint="deadbeefdeadbeef", checkout_head=head)

        cls = _classify(
            _contract(), child, co,
            parent_runs=[("parent-run", str(parent))],
        )["test"]
        assert cls.status == "stale"
        assert "fingerprint moved" in cls.reason
        assert cls.source_run_id == "parent-run"

    def test_parent_without_fingerprint_is_not_inherited(
        self, tmp_path: Path,
    ) -> None:
        # A parent receipt that recorded NO changed-files fingerprint cannot
        # prove it covers the current diff. The base classifier would call it
        # present (staleness not asserted), but inheritance requires a matched
        # fingerprint → it must be reported stale, never silently inherited.
        co = _git_checkout(tmp_path)
        head = _git_head(co)
        child = tmp_path / "child"
        child.mkdir()
        parent = tmp_path / "parent"
        parent.mkdir()
        _receipt(parent, "test", fingerprint=None, checkout_head=head)

        cls = _classify(
            _contract(), child, co,
            parent_runs=[("parent-run", str(parent))],
        )["test"]
        assert cls.status == "stale"
        assert "fingerprint" in cls.reason
        assert cls.source_run_id == "parent-run"

    def test_parent_not_inherited_when_current_fingerprint_unavailable(
        self, tmp_path: Path,
    ) -> None:
        # The current subject fingerprint is unavailable (empty checkout → git
        # identity degrades to None). Even a fingerprinted parent receipt cannot
        # be confirmed against the current diff, so it is not inherited.
        co = _git_checkout(tmp_path)
        head = _git_head(co)
        fp = changed_files_fingerprint(str(co))
        child = tmp_path / "child"
        child.mkdir()
        parent = tmp_path / "parent"
        parent.mkdir()
        _receipt(parent, "test", fingerprint=fp, checkout_head=head)

        result = classify_required_receipts(
            _contract(), child, PlaceholderContext(checkout=""),
            checkout="", parent_runs=[("parent-run", str(parent))],
        )
        cls = result["test"]
        assert cls.status == "stale"
        assert "fingerprint" in cls.reason
        assert cls.source_run_id == "parent-run"

    def test_env_mismatch_rejects_parent(self, tmp_path: Path) -> None:
        co = _git_checkout(tmp_path)
        head = _git_head(co)
        fp = changed_files_fingerprint(str(co))
        child = tmp_path / "child"
        child.mkdir()
        parent = tmp_path / "parent"
        parent.mkdir()
        # 'lint' declares env "ci"; the parent receipt ran in "other".
        _receipt(parent, "lint", env="other", fingerprint=fp, checkout_head=head)

        cls = _classify(
            _contract(required=["lint"]), child, co,
            parent_runs=[("parent-run", str(parent))],
        )["lint"]
        # Parent rejected on env; no child receipt → missing.
        assert cls.status == "missing"

    def test_failed_parent_is_not_inherited(self, tmp_path: Path) -> None:
        co = _git_checkout(tmp_path)
        head = _git_head(co)
        fp = changed_files_fingerprint(str(co))
        child = tmp_path / "child"
        child.mkdir()
        parent = tmp_path / "parent"
        parent.mkdir()
        _receipt(parent, "test", exit_code=1, fingerprint=fp, checkout_head=head)

        cls = _classify(
            _contract(), child, co,
            parent_runs=[("parent-run", str(parent))],
        )["test"]
        # The failed parent cannot be accepted as present; no current receipt and
        # the only parent classifies failed → most-informative is that failure.
        assert cls.status == "failed"
        assert cls.source_run_id == "parent-run"

    def test_current_present_wins_over_parent(self, tmp_path: Path) -> None:
        co = _git_checkout(tmp_path)
        head = _git_head(co)
        fp = changed_files_fingerprint(str(co))
        child = tmp_path / "child"
        child.mkdir()
        parent = tmp_path / "parent"
        parent.mkdir()
        _receipt(child, "test", fingerprint=fp, checkout_head=head)
        _receipt(parent, "test", fingerprint=fp, checkout_head=head)

        cls = _classify(
            _contract(), child, co,
            parent_runs=[("parent-run", str(parent))],
        )["test"]
        assert cls.status == "present"
        assert cls.source_run_id == child.name
        assert cls.path == receipt_file_path(child, "test")

    def test_first_qualifying_parent_in_search_order_is_chosen(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        head = _git_head(co)
        fp = changed_files_fingerprint(str(co))
        child = tmp_path / "child"
        child.mkdir()
        near = tmp_path / "near"
        near.mkdir()
        far = tmp_path / "far"
        far.mkdir()
        _receipt(near, "test", fingerprint=fp, checkout_head=head)
        _receipt(far, "test", fingerprint=fp, checkout_head=head)

        cls = _classify(
            _contract(), child, co,
            parent_runs=[("near-run", str(near)), ("far-run", str(far))],
        )["test"]
        assert cls.status == "present"
        assert cls.source_run_id == "near-run"

    def test_parent_sources_read_from_extras_when_no_override(
        self, tmp_path: Path,
    ) -> None:
        co = _git_checkout(tmp_path)
        head = _git_head(co)
        fp = changed_files_fingerprint(str(co))
        child = tmp_path / "child"
        child.mkdir()
        parent = tmp_path / "parent"
        parent.mkdir()
        _receipt(parent, "test", fingerprint=fp, checkout_head=head)

        cls = _classify(
            _contract(), child, co,
            extras={
                VERIFICATION_PARENT_RUNS_EXTRAS_KEY: [
                    ("parent-run", str(parent)),
                ],
            },
        )["test"]
        assert cls.status == "present"
        assert cls.source_run_id == "parent-run"


# ─────────────────────────────────────────────────────────────────────────────
# No-parent path is unchanged
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.git_worktree
class TestNoParentUnchanged:
    def test_present_and_missing_without_parents(self, tmp_path: Path) -> None:
        co = _git_checkout(tmp_path)
        head = _git_head(co)
        fp = changed_files_fingerprint(str(co))
        child = tmp_path / "child"
        child.mkdir()
        _receipt(child, "test", fingerprint=fp, checkout_head=head)

        result = _classify(
            _contract(required=["test", "lint"]), child, co,
        )
        assert result["test"].status == "present"
        assert result["test"].source_run_id == child.name
        assert result["lint"].status == "missing"
        assert result["lint"].source_run_id == ""
        assert result["lint"].path == ""
