"""Phase 7 bridge mechanics — the load-bearing architectural invariant.

Per ADR 0023: the session bridge is THE orchestration primitive. Each
runtime instance owns its ``session_id``; that id persists across
arbitrary phase transitions when callers pass ``continue_session=True``,
and burns cleanly when callers call ``reset_session()`` before the
next ``invoke()``.

This file pins two scenarios that the rest of the suite covers only
glancingly:

1. **Bridge persistence через циклы** — multi-phase pipeline on two
   runtimes (X-Y-X-Y-X) where each runtime accumulates context on its
   own bridge; cross-runtime data flow happens through prompt text, not
   session merge.

2. **Human-resume control** — when an auto-cycle pauses on a human,
   the operator picks ``preserve`` (continue_session=True, same id) or
   ``burn`` (reset_session() → fresh id). Both must work without any
   special runtime-side state.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import agents as agents_module


def _stream_with_session(session_id: str, text: str = "ok") -> tuple:
    """Return a ``_stream_run`` result that emits ``session_id`` in
    stream-json output. Tuple shape matches ``_stream_run``'s return."""
    stdout = (
        f'{{"type":"init","session_id":"{session_id}"}}\n'
        f'{{"type":"text","content":{text!r}}}\n'
    )
    return (stdout, 0, "", 0.1)


# ── Multi-phase bridge persistence ──────────────────────────────────────────


class TestBridgePersistsAcrossPhases:
    """Architectural invariant: a runtime instance's session_id survives
    every ``invoke(continue_session=True)`` regardless of which phases
    happen in between on OTHER runtimes."""

    @pytest.fixture
    def claude_x(self, mock_claude_bin: None):
        return agents_module.ClaudeAgent(model="claude-sonnet-4-6")

    @pytest.fixture
    def claude_y(self, mock_claude_bin: None):
        return agents_module.ClaudeAgent(model="claude-opus-4-7")

    @pytest.fixture
    def mock_stream(self, monkeypatch):
        mock = MagicMock()
        monkeypatch.setattr(agents_module, "_stream_run", mock)
        return mock

    def test_two_runtimes_keep_independent_bridges_across_five_phases(
        self, claude_x, claude_y, mock_stream,
    ) -> None:
        """Simulate plan-X → implement-X → review-Y → repair-X → review-Y
        on two distinct ClaudeAgent instances. After the run:

        * X has the same session_id across all three X-invocations
          (captured on call 1, resumed on calls 2 and 3).
        * Y has the same session_id across both Y-invocations.
        * The two bridges never alias each other.
        """
        sid_x = "sess_X_abc"
        sid_y = "sess_Y_xyz"

        # Phase 1 — plan on X: captures sid_x.
        mock_stream.return_value = _stream_with_session(sid_x)
        claude_x.invoke("plan prompt", "/proj")
        assert claude_x.session_id == sid_x
        assert claude_y.session_id is None

        # Phase 2 — implement on X (continue): output keeps emitting the
        # same id; ``--resume`` must appear on the CLI.
        mock_stream.return_value = _stream_with_session(sid_x)
        claude_x.invoke(
            "implement prompt", "/proj",
            mutates_artifacts=True, continue_session=True,
        )
        cmd = mock_stream.call_args[0][0]
        assert "--resume" in cmd and cmd[cmd.index("--resume") + 1] == sid_x

        # Phase 3 — review on Y: captures sid_y. X's bridge is untouched.
        mock_stream.return_value = _stream_with_session(sid_y)
        claude_y.invoke("review prompt", "/proj")
        assert claude_y.session_id == sid_y
        assert claude_x.session_id == sid_x, "Y invoke must not leak into X"

        # Phase 4 — repair on X (continue): X resumes its own bridge,
        # NOT Y's.
        mock_stream.return_value = _stream_with_session(sid_x)
        claude_x.invoke(
            "repair prompt", "/proj",
            mutates_artifacts=True, continue_session=True,
        )
        cmd = mock_stream.call_args[0][0]
        assert cmd[cmd.index("--resume") + 1] == sid_x

        # Phase 5 — review on Y (continue): Y resumes its own bridge.
        mock_stream.return_value = _stream_with_session(sid_y)
        claude_y.invoke("review-2 prompt", "/proj", continue_session=True)
        cmd = mock_stream.call_args[0][0]
        assert cmd[cmd.index("--resume") + 1] == sid_y

        # End state: both bridges intact and distinct.
        assert claude_x.session_id == sid_x
        assert claude_y.session_id == sid_y

    def test_cross_runtime_data_flow_is_prompt_text_only(
        self, claude_x, claude_y, mock_stream,
    ) -> None:
        """Y's output of phase N feeds into X's prompt of phase N+1 as
        plain text. There is no session-id sharing — bridges stay
        separate even when context crosses runtimes."""
        # Phase 1: Y produces a critique.
        critique = "Missing null check on line 42"
        mock_stream.return_value = (
            f'{{"type":"init","session_id":"sess_Y_1"}}\n'
            f'{{"type":"result","result":{critique!r}}}\n',
            0, "", 0.1,
        )
        y_output = claude_y.invoke("review prompt", "/proj")
        assert claude_y.session_id == "sess_Y_1"

        # Phase 2: X picks up Y's text in its prompt; X's bridge starts
        # fresh (no implicit reset, but no shared id either).
        mock_stream.return_value = _stream_with_session("sess_X_1")
        x_prompt = f"Apply this critique:\n{y_output}"
        claude_x.invoke(x_prompt, "/proj", mutates_artifacts=True)

        # The X invoke saw the critique as part of its prompt arg
        # (last positional in the cmd list).
        cmd = mock_stream.call_args[0][0]
        assert critique in cmd[-1], "Y's output must travel as prompt text"
        # And the two bridges never crossed.
        assert claude_x.session_id == "sess_X_1"
        assert claude_y.session_id == "sess_Y_1"


# ── Human-resume control: preserve vs burn ──────────────────────────────────


class TestHumanResumePreserveAndBurn:
    """When an auto-cycle pauses on a human, the operator picks one of
    two explicit modes:

    * **preserve** → next ``invoke(continue_session=True)`` resumes the
      same bridge; all prior context kept.
    * **burn** → ``reset_session()`` first, then ``invoke()`` starts
      fresh; new session_id captured.

    Both must work via the same bridge primitive — no separate
    runtime-level state.
    """

    @pytest.fixture
    def claude(self, mock_claude_bin: None):
        return agents_module.ClaudeAgent(model="claude-sonnet-4-6")

    @pytest.fixture
    def mock_stream(self, monkeypatch):
        mock = MagicMock()
        monkeypatch.setattr(agents_module, "_stream_run", mock)
        return mock

    def test_preserve_resumes_same_session_id(
        self, claude, mock_stream,
    ) -> None:
        """Captured id is preserved across the pause; resume goes back
        to the same conversation."""
        mock_stream.return_value = _stream_with_session("sess_preserve")
        claude.invoke("phase 1", "/proj", mutates_artifacts=True)
        assert claude.session_id == "sess_preserve"

        # Pause boundary (operator inspects). Same instance, same id.
        captured_id_at_pause = claude.session_id

        # Resume: same instance, continue_session=True. The CLI must
        # carry --resume <captured_id>.
        mock_stream.return_value = _stream_with_session("sess_preserve")
        claude.invoke(
            "phase 2 after human review", "/proj",
            mutates_artifacts=True, continue_session=True,
        )
        cmd = mock_stream.call_args[0][0]
        assert "--resume" in cmd
        assert cmd[cmd.index("--resume") + 1] == captured_id_at_pause
        assert claude.session_id == "sess_preserve"

    def test_burn_resets_then_captures_fresh_id(
        self, claude, mock_stream,
    ) -> None:
        """The operator wants a clean slate. ``reset_session()`` first;
        the next ``invoke()`` runs without ``--resume`` and captures a
        NEW session_id from the stream-json output."""
        mock_stream.return_value = _stream_with_session("sess_old")
        claude.invoke("phase 1", "/proj", mutates_artifacts=True)
        assert claude.session_id == "sess_old"

        # Operator burns the bridge.
        claude.reset_session()
        assert claude.session_id is None

        # Next invoke: even with continue_session=True, no --resume
        # (nothing to resume), and a fresh id is captured.
        mock_stream.return_value = _stream_with_session("sess_fresh")
        claude.invoke(
            "phase 2 with fresh context", "/proj",
            mutates_artifacts=True, continue_session=True,
        )
        cmd = mock_stream.call_args[0][0]
        assert "--resume" not in cmd, (
            "reset_session() must drop the id so --resume isn't injected"
        )
        assert claude.session_id == "sess_fresh"
        assert claude.session_id != "sess_old"
