"""Reviewer prompt carries the verification-receipt summary (ADR 0076 / T7).

The reviewer prompt includes a brief verification-environment receipt
summary when the developer-side phases wrote one; with no receipts it adds
no block and the prompt still builds. Named with "review" so the targeted
slice ``pytest -q tests/unit/pipeline/prompts -k "review or evidence"``
catches it.
"""

from __future__ import annotations

from pipeline.phases.builtin.review_support import _verification_receipt_text
from pipeline.plugins import PluginConfig
from pipeline.prompts.builders import (
    _verification_receipt_part,
    runtime_review_uncommitted_prompt,
)
from pipeline.runtime import PipelineState

_BLOCK_MARKER = "Developer-side verification environment"


def _state(*, output_dir) -> PipelineState:
    st = PipelineState(
        task="t", project_dir="/checkout", plugin=PluginConfig(),
        extras={"run_id": "20260101_000000"},
    )
    st.output_dir = output_dir
    return st


# ── builder part ───────────────────────────────────────────────────────


class TestVerificationReceiptPart:
    def test_part_built_when_body_present(self) -> None:
        part = _verification_receipt_part("some receipt summary")
        assert part is not None
        assert part.kind == "verification_receipt"
        assert part.body == "some receipt summary"

    def test_no_part_when_empty(self) -> None:
        assert _verification_receipt_part("") is None
        assert _verification_receipt_part("   \n ") is None


# ── review prompt inclusion ────────────────────────────────────────────


class TestReviewPromptInclusion:
    def test_prompt_includes_receipt_when_provided(self) -> None:
        body = (
            "Developer-side verification environment:\n"
            "- implement round 1: python=3.12; cwd=/checkout\n"
            "    check ruff: pass"
        )
        turn = runtime_review_uncommitted_prompt(
            "focus area", project_dir="/checkout",
            verification_receipt=body,
        )
        assert "verification environment" in turn.text
        assert "check ruff: pass" in turn.text

    def test_prompt_builds_without_receipt_and_adds_no_block(self) -> None:
        turn = runtime_review_uncommitted_prompt(
            "focus area", project_dir="/checkout",
        )
        assert turn.text  # builds fine
        assert _BLOCK_MARKER not in turn.text

    def test_empty_receipt_adds_no_block(self) -> None:
        turn = runtime_review_uncommitted_prompt(
            "focus area", project_dir="/checkout",
            verification_receipt="",
        )
        assert _BLOCK_MARKER not in turn.text


# ── text builder from on-disk receipts (T6 reader) ─────────────────────


class TestVerificationReceiptText:
    def test_renders_receipts_under_run_dir(self, tmp_path) -> None:
        from pipeline.evidence.verification_receipt import (
            write_verification_receipt,
        )

        run_dir = tmp_path / "run"
        run_dir.mkdir()
        write_verification_receipt(
            output_dir=run_dir, phase="implement", round=1, cwd="/checkout",
            python="3.12.4 (/venv/bin/python)",
            checks=[{"name": "import_invariant", "expected": "ok",
                     "actual": "ok", "passed": True}],
            commands=[{"argv": ["python", "-c", "import pkg"], "exit_code": 0}],
        )
        text = _verification_receipt_text(_state(output_dir=run_dir))
        assert text
        assert "implement round 1" in text
        assert "3.12.4 (/venv/bin/python)" in text
        assert "/checkout" in text
        assert "import_invariant" in text
        assert "python -c import pkg" in text
        assert "exit=0" in text
        assert "temp venv outside checkout: True" in text

    def test_empty_when_no_receipts(self, tmp_path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        assert _verification_receipt_text(_state(output_dir=run_dir)) == ""

    def test_empty_when_no_output_dir(self) -> None:
        assert _verification_receipt_text(_state(output_dir=None)) == ""

    def test_review_prompt_end_to_end_with_disk_receipts(self, tmp_path) -> None:
        from pipeline.evidence.verification_receipt import (
            write_verification_receipt,
        )

        run_dir = tmp_path / "run"
        run_dir.mkdir()
        write_verification_receipt(
            output_dir=run_dir, phase="repair_changes", round=2,
            cwd="/checkout",
        )
        text = _verification_receipt_text(_state(output_dir=run_dir))
        turn = runtime_review_uncommitted_prompt(
            "focus", project_dir="/checkout", verification_receipt=text,
        )
        assert "repair_changes round 2" in turn.text
