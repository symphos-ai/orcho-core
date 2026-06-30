"""Unit tests for ``_MockCodex`` invoke dispatch markers."""
from __future__ import annotations

import json

from agents.runtimes._strategy import _MockCodex


def _verdict(payload: str) -> str:
    return json.loads(payload)["verdict"]


def test_cross_validate_plan_marker_routes_through_reject_counter() -> None:
    """Cross-level validate_plan rides on the shared reject counter.

    The ``"## Cross plan review"`` marker rides on the TURN/NONE
    ``cross_validate_plan_input`` part (delta-safe across rounds).
    ``_MockCodex.invoke`` must dispatch this prompt through
    ``review_file("plan_cross_synthetic.md", ...)`` so the existing
    ``is_validate_plan = name.startswith("plan_")`` check at
    ``review_file`` ticks the shared budget.
    """
    mock = _MockCodex(validate_plan_reject_rounds=2)
    prompt = (
        "Some preamble from the static task body.\n\n"
        "## Cross plan review\n\n"
        "TASK:\nAdd telemetry across two services.\n\n"
        "PROJECTS INVOLVED: api, web\n"
    )

    assert _verdict(mock.invoke(prompt, cwd="/tmp")) == "REJECTED"
    assert _verdict(mock.invoke(prompt, cwd="/tmp")) == "REJECTED"
    assert _verdict(mock.invoke(prompt, cwd="/tmp")) == "APPROVED"


def test_cross_marker_and_project_marker_share_counter() -> None:
    """The reject budget is a single shared counter across both surfaces.

    A single ``_MockCodex`` instance must not double-count: two cross
    rejects exhaust the same budget that a project ``## Tasks`` reject
    would have used.
    """
    mock = _MockCodex(validate_plan_reject_rounds=2)
    cross_prompt = "## Cross plan review\n\nTASK:\nfoo"
    project_prompt = "## Tasks\n\n- inspect\n- apply\n- verify"

    assert _verdict(mock.invoke(cross_prompt, cwd="/tmp")) == "REJECTED"
    assert _verdict(mock.invoke(project_prompt, cwd="/tmp")) == "REJECTED"
    assert _verdict(mock.invoke(cross_prompt, cwd="/tmp")) == "APPROVED"
    assert _verdict(mock.invoke(project_prompt, cwd="/tmp")) == "APPROVED"


def test_no_marker_falls_through_to_uncommitted_review() -> None:
    """A reviewer prompt without the cross marker stays on the legacy path."""
    mock = _MockCodex(validate_plan_reject_rounds=5)
    prompt = "Review the changes in the working tree."
    # ``review_uncommitted`` always returns APPROVED in the mock.
    assert _verdict(mock.invoke(prompt, cwd="/tmp")) == "APPROVED"


def test_uncommitted_review_marker_wins_over_embedded_plan_tasks() -> None:
    """Diff-review prompts may embed the plan artifact's ``## Tasks`` section."""
    mock = _MockCodex(validate_plan_reject_rounds=5)
    prompt = (
        "<orcho:part id=\"task:review_uncommitted@0\">\n"
        "Review the uncommitted diff.\n"
        "</orcho:part>\n\n"
        "<orcho:part id=\"contract:change_handoff:mode=uncommitted@1\">\n"
        "Review working-tree changes.\n"
        "</orcho:part>\n\n"
        "# Implementation Plan\n\n"
        "## Tasks\n\n"
        "- inspect\n"
    )

    payload = json.loads(mock.invoke(prompt, cwd="/tmp"))

    assert payload["verdict"] == "APPROVED"
    assert "Plan approved" not in payload["short_summary"]
