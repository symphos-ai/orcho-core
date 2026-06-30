"""Markdown renderers emphasize short summaries consistently."""
from __future__ import annotations

from agents.entities import SubTask
from pipeline.plan_markdown import render_plan_markdown
from pipeline.plan_parser import ParsedPlan
from pipeline.release_markdown import render_release_markdown
from pipeline.release_parser import ContractStatus, ParsedRelease
from pipeline.review_markdown import render_review_markdown
from pipeline.review_parser import ParsedReview


def test_review_markdown_bolds_short_summary_value() -> None:
    out = render_review_markdown(
        ParsedReview(
            verdict="APPROVED",
            short_summary="Interfaces align.",
        )
    )

    assert "**Short summary:** **Interfaces align.**" in out


def test_release_markdown_bolds_short_summary_value() -> None:
    out = render_release_markdown(
        ParsedRelease(
            verdict="APPROVED",
            ship_ready=True,
            short_summary="Coordinated change ships.",
            release_blockers=(),
            verification_gaps=(),
            contract_status=ContractStatus(
                task_contract="satisfied",
                interfaces="compatible",
                persistence="safe",
                tests="sufficient",
            ),
        )
    )

    assert "**Short summary:** **Coordinated change ships.**" in out


def test_plan_markdown_bolds_short_summary_value() -> None:
    out = render_plan_markdown(
        ParsedPlan(
            subtasks=(SubTask(id="T1", goal="Implement the change."),),
            source="json",
            short_summary="Implement safely.",
        )
    )

    assert "## Short Summary\n\n**Implement safely.**" in out
