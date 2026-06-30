"""Provider-session fallback is a first-class warning, not a phase failure.

When a runtime rejects a stale local conversation id, ``_session_aware_invoke``
burns the stale bridge, recomputes a full prompt, and retries in a fresh
provider session. These tests pin that the *recovered* fallback surfaces as
exactly one compact operator warning plus a structural
``phase.provider_session_fallback`` event — without the swallowed
missing-session exception leaking out as a failed phase — and that
non-matching errors still propagate.

Scoped to ``_session_aware_invoke`` with a stub agent (no real provider CLI),
mirroring ``test_common_session_continuation.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.io.retry import AgentCallError
from core.observability import events as _events, prompt_trace
from pipeline.phases.builtin import _session_aware_invoke
from pipeline.plugins import PluginConfig
from pipeline.prompts.session import PromptSessionSplit
from pipeline.prompts.turn import PromptTurn, PromptTurnEditor
from pipeline.prompts.types import (
    PromptCacheScope,
    PromptLayer,
    PromptPart,
    PromptStability,
)
from pipeline.runtime import PipelineState

# Error stderr texts modelling the two main provider runtimes whose
# missing-session rejection must be recognised by
# ``_is_missing_provider_session_error``.
_CLAUDE_MISSING = "No conversation found with session ID: sess-plan-1"
_CODEX_MISSING = "Error: session not found"


class _RecordingAgent:
    def __init__(
        self,
        *,
        model: str = "claude-opus-4-7",
        session_id: str | None = "sess-plan-1",
        fail_text: str | None = None,
        fail_once: bool = False,
        success_session_id: str | None = "sess-fresh-2",
    ) -> None:
        self.model = model
        self.session_id = session_id
        self.calls: list[dict[str, Any]] = []
        self.fail_text = fail_text
        self.fail_once = fail_once
        self.success_session_id = success_session_id

    def invoke(
        self,
        prompt: str,
        cwd: str,
        *,
        continue_session: bool = False,
        attachments: tuple = (),
        mutates_artifacts: bool = False,
    ) -> str:
        self.calls.append(
            {"prompt": prompt, "cwd": cwd, "continue_session": continue_session}
        )
        if self.fail_text is not None and (not self.fail_once or len(self.calls) == 1):
            raise AgentCallError(
                "Agent call failed: exit=1",
                exit_code=1,
                stderr=self.fail_text,
            )
        if self.success_session_id:
            self.session_id = self.success_session_id
        return "ok"


def _role() -> PromptPart:
    return PromptPart(
        kind="role", name="implementation_engineer", source="core",
        body="role:implementation_engineer", layer=PromptLayer.ROLE,
    )


def _turn_input(body: str, name: str = "implement_task") -> PromptPart:
    return PromptPart(
        kind="turn_input", name=name, source="code-owned", body=body,
        layer=PromptLayer.TURN, stability=PromptStability.TURN,
        cache_scope=PromptCacheScope.NONE, volatile_reason="turn input",
        id=name,
    )


def _turn(body: str = "do the work") -> PromptTurn:
    editor = PromptTurnEditor()
    editor.append(_role())
    editor.append(_turn_input(body))
    return editor.build()


def _state(*, silent: bool = False) -> PipelineState:
    extras: dict[str, Any] = {"run_id": "run-fallback-1"}
    if silent:
        extras["_silent"] = True
    return PipelineState(
        task="implement the plan",
        project_dir="/proj",
        plugin=PluginConfig(),
        extras=extras,
    )


def _seed_common_session(state: PipelineState) -> None:
    """Land a committed COMMON session id so a later continue resumes it."""
    seed_agent = _RecordingAgent(session_id="sess-plan-1", success_session_id=None)
    _session_aware_invoke(
        seed_agent, state, phase="plan", turn=_turn("seed"),
        cwd="/proj", split=PromptSessionSplit.COMMON, continue_session=False,
    )


@pytest.fixture(autouse=True)
def _drain_render_envelope():
    prompt_trace.take_last_upper()
    yield
    prompt_trace.take_last_upper()


@pytest.fixture()
def _event_store(tmp_path):
    """Activate a JSONL event store so emit() is observable, then disable."""
    _events.init_event_store(tmp_path)
    try:
        yield tmp_path
    finally:
        _events.init_event_store(None)


def _invoke_with_failing_resume(
    state: PipelineState, fail_text: str,
) -> tuple[str, _RecordingAgent]:
    _seed_common_session(state)
    agent = _RecordingAgent(
        session_id=None, fail_text=fail_text, fail_once=True,
        success_session_id="sess-fresh-2",
    )
    raw = _session_aware_invoke(
        agent, state, phase="implement", turn=_turn(),
        cwd="/proj", split=PromptSessionSplit.COMMON, continue_session=True,
    )
    return raw, agent


class TestFallbackIsFirstClassWarning:
    @pytest.mark.parametrize(
        "fail_text", [_CLAUDE_MISSING, _CODEX_MISSING],
        ids=["claude", "codex"],
    )
    def test_missing_session_recovery_warns_and_emits(
        self, fail_text, capsys, _event_store,
    ) -> None:
        state = _state()
        raw, agent = _invoke_with_failing_resume(state, fail_text)

        # The fresh-session retry succeeded.
        assert raw == "ok"
        assert [c["continue_session"] for c in agent.calls] == [True, False]
        assert agent.session_id == "sess-fresh-2"

        # Exactly one operator warning line of the specified format.
        out = capsys.readouterr().out
        warning_lines = [
            ln for ln in out.splitlines()
            if "Provider session resume unavailable" in ln
        ]
        assert len(warning_lines) == 1
        assert "Provider session resume unavailable for implement" in warning_lines[0]
        assert "Continuing with a fresh provider session" in warning_lines[0]
        # The fallback keeps the run on the same worktree — pinned by text.
        assert "the same run worktree" in warning_lines[0]

        # Structural event recorded for debug / progress observability.
        kinds = [e.kind for e in _events.read_all(_event_store)]
        assert "phase.provider_session_fallback" in kinds

    def test_fresh_session_retry_reuses_same_run_worktree(
        self, capsys, _event_store,
    ) -> None:
        """The fresh-session retry never reallocates a worktree.

        A missing-provider-session fallback resets only the stale local
        session bridge and re-invokes in a fresh provider session. It must
        not move the run off its worktree: the ``cwd`` passed to the second
        ``agent.invoke`` is identical to the first, and the retry switches
        ``continue_session`` from True to False without changing the path.
        """
        state = _state()
        raw, agent = _invoke_with_failing_resume(state, _CLAUDE_MISSING)

        assert raw == "ok"
        assert len(agent.calls) == 2
        # continue_session flipped True -> False on the fresh-session retry.
        assert [c["continue_session"] for c in agent.calls] == [True, False]
        # cwd unchanged across the retry: no new worktree was allocated.
        first_cwd, second_cwd = agent.calls[0]["cwd"], agent.calls[1]["cwd"]
        assert first_cwd == second_cwd == "/proj"

    def test_phase_not_marked_failed_on_recovery(self, capsys, _event_store) -> None:
        state = _state()
        _invoke_with_failing_resume(state, _CLAUDE_MISSING)
        # The recovered fallback rendered a fresh full prompt; no failed
        # marker leaks into the phase log for the swallowed exception.
        meta = state.phase_log["implement"]["prompt_render"]
        assert meta["render_mode"] == "full"
        assert meta["continue_session"] is False
        assert "failed" not in state.phase_log["implement"]

    def test_silent_suppresses_warning_but_still_emits_event(
        self, capsys, _event_store,
    ) -> None:
        state = _state(silent=True)
        raw, _agent = _invoke_with_failing_resume(state, _CLAUDE_MISSING)
        assert raw == "ok"

        out = capsys.readouterr().out
        assert "Provider session resume unavailable" not in out

        kinds = [e.kind for e in _events.read_all(_event_store)]
        assert "phase.provider_session_fallback" in kinds

    def test_non_matching_error_propagates(self, capsys, _event_store) -> None:
        state = _state()
        _seed_common_session(state)
        agent = _RecordingAgent(
            session_id=None, fail_text="boom: unrelated failure",
            fail_once=False,
        )
        with pytest.raises(AgentCallError):
            _session_aware_invoke(
                agent, state, phase="implement", turn=_turn(),
                cwd="/proj", split=PromptSessionSplit.COMMON,
                continue_session=True,
            )
        # No fallback warning / event for a genuine, unrecognised failure.
        out = capsys.readouterr().out
        assert "Provider session resume unavailable" not in out
        kinds = [e.kind for e in _events.read_all(_event_store)]
        assert "phase.provider_session_fallback" not in kinds
