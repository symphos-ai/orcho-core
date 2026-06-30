"""Unit tests for the env-gated incomplete-delivery mock trigger.

Covers :func:`_mock_subtask_attestation` and its gate
:func:`_mock_implement_incomplete_enabled`. The trigger exists to drive the
ADR-0073 incomplete-implement-delivery path deterministically in --mock E2E
smokes. The default (unset env) all-met behaviour must stay unchanged.
"""
from __future__ import annotations

import json

import pytest

from agents.runtimes._strategy import (
    _mock_implement_incomplete_enabled,
    _mock_subtask_attestation,
)

_ENV = "ORCHO_MOCK_IMPLEMENT_INCOMPLETE"

_PROMPT = (
    "Some implement preamble.\n\n"
    "## Current Executable Subtask `implement:t-1`\n\n"
    "**Done criteria (the work is not finished until each is true):**\n"
    "- First criterion holds.\n"
    "- Second criterion holds.\n"
    "- Third criterion holds.\n\n"
    "Other prose.\n"
)


def _parse(prompt: str) -> dict:
    suffix = _mock_subtask_attestation(prompt)
    assert suffix.startswith("\n\n"), "attestation must be appended as JSON tail"
    return json.loads(suffix.strip())


def test_unset_env_keeps_all_criteria_met(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression guard: default behaviour reports every criterion met."""
    monkeypatch.delenv(_ENV, raising=False)
    assert _mock_implement_incomplete_enabled() is False

    att = _parse(_PROMPT)
    assert att["type"] == "subtask_attestation"
    assert att["subtask_id"] == "implement:t-1"
    assert [c["met"] for c in att["criteria"]] == [True, True, True]
    assert all(c["met"] for c in att["criteria"])
    assert "all 3 criteria met" in att["summary"]


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_truthy_env_marks_one_criterion_unmet(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Armed trigger emits a valid attestation with >=1 ``met: false``."""
    monkeypatch.setenv(_ENV, value)
    assert _mock_implement_incomplete_enabled() is True

    att = _parse(_PROMPT)
    unmet = [c for c in att["criteria"] if not c["met"]]
    assert len(unmet) >= 1
    assert unmet[-1]["index"] == len(att["criteria"])  # last criterion unmet
    assert "ADR-0073" in unmet[0]["evidence"]
    assert "incomplete" in att["summary"]


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "  "])
def test_falsy_env_keeps_all_criteria_met(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Falsy env values leave the default all-met behaviour intact."""
    monkeypatch.setenv(_ENV, value)
    assert _mock_implement_incomplete_enabled() is False

    att = _parse(_PROMPT)
    assert all(c["met"] for c in att["criteria"])


def test_no_header_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """whole_plan builds (no executable-subtask header) append nothing."""
    monkeypatch.setenv(_ENV, "1")
    assert _mock_subtask_attestation("no subtask header here") == ""


def test_no_criteria_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """A subtask with no done-criteria carries no contract to close."""
    monkeypatch.setenv(_ENV, "1")
    prompt = "## Current Executable Subtask `implement:t-2`\n\nNo criteria block.\n"
    assert _mock_subtask_attestation(prompt) == ""
