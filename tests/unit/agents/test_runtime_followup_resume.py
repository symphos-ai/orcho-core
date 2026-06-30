"""Runtime follow-up resume seed contracts."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import agents as agents_module
from agents.runtimes._strategy import make_mock_phase_config
from agents.runtimes.claude import ClaudeAgent
from agents.runtimes.codex import CodexAgent
from agents.runtimes.gemini import GeminiAgent


def _stream_result(
    stdout: str = "done",
    returncode: int = 0,
    stderr: str = "",
    duration: float = 0.1,
):
    return (stdout, returncode, stderr, duration)


@pytest.fixture
def mock_stream_run(monkeypatch) -> MagicMock:
    mock = MagicMock(return_value=_stream_result("done"))
    monkeypatch.setattr(agents_module, "_stream_run", mock)
    return mock


def test_claude_followup_seed_forces_first_resume(
    mock_claude_bin: None,
    mock_stream_run: MagicMock,
) -> None:
    agent = ClaudeAgent(model="claude-test")
    agent.session_id = "parent-claude"
    agent._followup_resume_pending = True

    agent.invoke("task", "/project", continue_session=False)

    cmd = mock_stream_run.call_args[0][0]
    assert cmd[cmd.index("--resume") + 1] == "parent-claude"
    assert agent._last_resumed_session_id == "parent-claude"
    assert agent._last_followup_parent_session_id == "parent-claude"
    assert agent._followup_resume_pending is False


def test_claude_second_call_without_pending_does_not_resume(
    mock_claude_bin: None,
    mock_stream_run: MagicMock,
) -> None:
    agent = ClaudeAgent(model="claude-test")
    agent.session_id = "parent-claude"
    agent._followup_resume_pending = True
    agent.invoke("first", "/project")
    agent.invoke("second", "/project", continue_session=False)

    cmd = mock_stream_run.call_args[0][0]
    assert "--resume" not in cmd
    assert agent._last_resumed_session_id is None
    assert agent._last_followup_parent_session_id is None


def test_codex_followup_seed_forces_first_resume(
    mock_codex_bin: None,
    mock_stream_run: MagicMock,
) -> None:
    agent = CodexAgent(model="gpt-test")
    agent.session_id = "parent-codex"
    agent._followup_resume_pending = True

    agent.invoke("task", "/project", mutates_artifacts=True, continue_session=False)

    cmd = mock_stream_run.call_args[0][0]
    assert cmd[:3] == [agent.bin, "exec", "resume"]
    assert cmd[-2:] == ["parent-codex", "task"]
    assert agent._last_resumed_session_id == "parent-codex"
    assert agent._last_followup_parent_session_id == "parent-codex"
    assert agent._followup_resume_pending is False


def test_codex_second_call_without_pending_does_not_resume(
    mock_codex_bin: None,
    mock_stream_run: MagicMock,
) -> None:
    agent = CodexAgent(model="gpt-test")
    agent.session_id = "parent-codex"
    agent._followup_resume_pending = True
    agent.invoke("first", "/project", mutates_artifacts=True)
    agent.invoke("second", "/project", mutates_artifacts=True, continue_session=False)

    cmd = mock_stream_run.call_args[0][0]
    assert cmd[:2] == [agent.bin, "exec"]
    assert cmd[2] != "resume"
    assert agent._last_resumed_session_id is None
    assert agent._last_followup_parent_session_id is None


def test_gemini_followup_seed_forces_first_resume(
    mock_gemini_bin: None,
    mock_stream_run: MagicMock,
) -> None:
    agent = GeminiAgent(model="gemini-test")
    agent.session_id = "parent-gemini"
    agent._followup_resume_pending = True

    agent.invoke("task", "/project", continue_session=False)

    cmd = mock_stream_run.call_args[0][0]
    assert cmd[cmd.index("-r") + 1] == "parent-gemini"
    assert agent._last_resumed_session_id == "parent-gemini"
    assert agent._last_followup_parent_session_id == "parent-gemini"
    assert agent._followup_resume_pending is False


def test_gemini_second_call_without_pending_does_not_resume(
    mock_gemini_bin: None,
    mock_stream_run: MagicMock,
) -> None:
    agent = GeminiAgent(model="gemini-test")
    agent.session_id = "parent-gemini"
    agent._followup_resume_pending = True
    agent.invoke("first", "/project")
    agent.invoke("second", "/project", continue_session=False)

    cmd = mock_stream_run.call_args[0][0]
    assert "-r" not in cmd
    assert agent._last_resumed_session_id is None
    assert agent._last_followup_parent_session_id is None


def test_mock_runtimes_follow_resume_forensic_contract() -> None:
    cfg = make_mock_phase_config()

    cfg.plan_agent.session_id = "parent-mock-claude"
    cfg.plan_agent._followup_resume_pending = True
    cfg.plan_agent.invoke("plan", "/project", continue_session=False)
    assert cfg.plan_agent._last_continue_session is True
    assert cfg.plan_agent._last_resumed_session_id == "parent-mock-claude"
    assert (
        cfg.plan_agent._last_followup_parent_session_id
        == "parent-mock-claude"
    )

    cfg.review_changes_agent.session_id = "parent-mock-codex"
    cfg.review_changes_agent._followup_resume_pending = True
    cfg.review_changes_agent.invoke("review", "/project", continue_session=False)
    assert cfg.review_changes_agent._last_continue_session is True
    assert cfg.review_changes_agent._last_resumed_session_id == "parent-mock-codex"
    assert (
        cfg.review_changes_agent._last_followup_parent_session_id
        == "parent-mock-codex"
    )
