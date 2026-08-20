"""Unit tests for ``_MockClaude`` invoke-surface dispatch (ADR 0054).

The mock Claude runtime classifies a read-only prompt by surface so that
``--mock`` runs exercise the real contracts:

* cross architect plan / replan → typed ``cross_plan_json`` object;
* reviewer gates (validate_plan / review_changes / validate_cross_plan /
  contract_check) → ``review_json`` verdict;
* release gate (final_acceptance) → ``release_json``.

Regression guard: the cross_validate_plan focus prompt embeds the rendered
"# Cross-Project Plan" artifact and its task prose says "Validate the
cross-project plan", so a naive cross-plan detector would emit a plan object
to a review parser. The default green path hides this because the mock
reviewer is codex; this test pins the behavior when Claude is the reviewer.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from agents.runtimes._strategy import _MockClaude


def test_mock_claude_roundtrips_large_unicode_prompt_without_provider_process(
    tmp_path: Path,
) -> None:
    prompt = "Проверка stdin ��\n" * 6000
    mock = _MockClaude()
    with patch("subprocess.Popen", side_effect=AssertionError("provider started")):
        output = mock.invoke(prompt, str(tmp_path))

    assert output
    assert mock.last_input_prompt == prompt


def test_mock_claude_records_byte_identical_stdin_prompt_and_session(tmp_path: Path) -> None:
    prompt = "Unicode prompt: ž ��\n"
    mock = _MockClaude()
    output = mock.invoke(prompt, str(tmp_path), continue_session=True)

    assert output
    assert mock.last_input_prompt == prompt
    assert mock.session_id == "mock-claude-1"


def _projects(tmp: Path) -> dict[str, Path]:
    return {"api": tmp / "api", "web": tmp / "web"}


def test_cross_plan_prompt_emits_typed_cross_plan_json(tmp_path: Path) -> None:
    from pipeline.cross_project.prompts import cross_plan_prompt

    out = _MockClaude().invoke(
        cross_plan_prompt("Align email", _projects(tmp_path), tmp_path / "c").text,
        str(tmp_path),
    )
    obj = json.loads(out)
    assert "subtasks" in obj and "verdict" not in obj
    assert {st["alias"] for st in obj["subtasks"]} == {"api", "web"}


def test_cross_replan_prompt_emits_typed_cross_plan_json(tmp_path: Path) -> None:
    from pipeline.cross_project.prompts import cross_replan_prompt

    out = _MockClaude().invoke(
        cross_replan_prompt(
            "Align email", "fix the contract", _projects(tmp_path), tmp_path / "c",
        ).text,
        str(tmp_path),
    )
    obj = json.loads(out)
    assert "subtasks" in obj and "verdict" not in obj
    assert {st["alias"] for st in obj["subtasks"]} == {"api", "web"}


def test_cross_validate_plan_focus_emits_review_verdict_not_plan(
    tmp_path: Path,
) -> None:
    """The cross_validate_plan reviewer surface must emit a ``review_json``
    verdict even though it embeds the rendered cross-plan artifact and the
    task prose mentions "cross-project plan"."""
    from pipeline.cross_project.prompts import cross_plan_review_focus

    rendered_plan = (
        "# Cross-Project Plan\n\n"
        "## Interface Contract\nshared surface\n\n"
        "## Per-Project Subtasks\n### [api]\nGoal: x\n"
    )
    out = _MockClaude().invoke(
        cross_plan_review_focus(
            "Align email", ["api", "web"], plan_artifact=rendered_plan,
        ).text,
        str(tmp_path),
    )
    obj = json.loads(out)
    assert "verdict" in obj, "reviewer surface must emit a review_json verdict"
    assert "subtasks" not in obj, "reviewer must NOT emit a cross-plan object"


def test_contract_check_focus_emits_review_verdict(tmp_path: Path) -> None:
    from pipeline.cross_project.prompts import contract_review_focus

    out = _MockClaude().invoke(
        contract_review_focus("Align email", _projects(tmp_path)).text,
        str(tmp_path),
    )
    obj = json.loads(out)
    assert "verdict" in obj and "subtasks" not in obj
