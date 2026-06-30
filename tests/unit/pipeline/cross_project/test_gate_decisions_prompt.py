"""Pin guards for the manual-confirm cross-gate prompt (T3 #7).

After #7 :func:`pipeline.cross_project.gate_decisions._prompt_inline`
routes its styling through :mod:`core.io.journey_prompt`. The
prompt's choice / default semantics are unchanged from the
pre-migration shape and must stay byte-identical — the migration is
a pure UX-styling pass.

Tests cover:

* default action semantics (empty / y / Y / s / S / a / A);
* repeat-and-abort behaviour after three unrecognised answers;
* EOFError → PAUSE (operator-decides-later path);
* color policy on the prompt string passed to ``input()``;
* stderr-bound unrecognised-answer reminder routes through
  ``stream=sys.stderr`` so it obeys stderr's TTY status.
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest

from core.io.ansi import C, get_color_enabled, set_color_enabled
from pipeline.cross_project.gate_decisions import (
    GateDecision,
    _prompt_inline,
)


@pytest.fixture(autouse=True)
def _restore_color_override() -> Iterator[None]:
    before = get_color_enabled()
    try:
        yield
    finally:
        set_color_enabled(before)


def _patch_input(monkeypatch, replies: list[str]) -> list[str]:
    """Patch :func:`input` to return successive replies; record prompts.

    The prompt argument passed to ``input`` is captured into the
    returned list so tests can assert on its styling without scraping
    stdout.
    """
    captured: list[str] = []
    iter_replies = iter(replies)

    def _fake_input(prompt: str = "") -> str:
        captured.append(prompt)
        try:
            return next(iter_replies)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", _fake_input)
    return captured


# ── default-action semantics (unchanged from pre-#7) ──────────────────


class TestDefaultSemantics:
    @pytest.mark.parametrize("reply", ["", "y", "Y"])
    def test_default_and_yes_inputs_map_to_run(
        self, monkeypatch: pytest.MonkeyPatch, reply: str,
    ) -> None:
        _patch_input(monkeypatch, [reply])
        assert _prompt_inline("cross_qa") is GateDecision.RUN

    @pytest.mark.parametrize("reply", ["s", "S"])
    def test_lowercase_and_upper_s_map_to_skip(
        self, monkeypatch: pytest.MonkeyPatch, reply: str,
    ) -> None:
        _patch_input(monkeypatch, [reply])
        assert _prompt_inline("cross_qa") is GateDecision.SKIP

    @pytest.mark.parametrize("reply", ["a", "A"])
    def test_lowercase_and_upper_a_map_to_abort(
        self, monkeypatch: pytest.MonkeyPatch, reply: str,
    ) -> None:
        _patch_input(monkeypatch, [reply])
        assert _prompt_inline("cross_qa") is GateDecision.ABORT


class TestRetryAndAbort:
    def test_three_unrecognised_answers_yield_abort(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _patch_input(monkeypatch, ["x", "?", "what"])
        result = _prompt_inline("cross_qa")
        assert result is GateDecision.ABORT
        # All three unrecognised reminders surface on stderr.
        err = capsys.readouterr().err
        assert err.count("unrecognised answer") == 3

    def test_eof_during_prompt_yields_pause(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_input(monkeypatch, [])  # No replies → EOFError on first call.
        assert _prompt_inline("cross_qa") is GateDecision.PAUSE

    def test_recovers_after_one_unrecognised_then_valid(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_input(monkeypatch, ["wat", "s"])
        assert _prompt_inline("cross_qa") is GateDecision.SKIP


# ── color policy (T3 #7) ──────────────────────────────────────────────


class TestPromptColorPolicy:
    def test_disabled_color_prompt_is_plain(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        set_color_enabled(False)
        captured = _patch_input(monkeypatch, [""])
        _prompt_inline("cross_qa")
        prompt = captured[0]
        assert "\x1b[" not in prompt
        # Semantics: gate name, default chip, alternatives still visible.
        assert "cross_qa" in prompt
        assert "[Y]" in prompt
        assert "[s] skip" in prompt
        assert "[a] abort" in prompt

    def test_forced_color_prompt_uses_journey_palette(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        set_color_enabled(True)
        captured = _patch_input(monkeypatch, [""])
        _prompt_inline("cross_qa")
        prompt = captured[0]
        # Gate name is bold; default chip is green+bold; alternatives
        # rendered in grey — same painter set the journey prompts use.
        assert C.BOLD in prompt
        assert C.GREEN in prompt
        assert C.GREY in prompt
        assert C.RESET in prompt


class TestUnrecognisedReminderStreamDiscipline:
    """The "  unrecognised answer …" reminder writes to ``sys.stderr``.
    The migration passes ``stream=sys.stderr`` to ``paint()`` so the
    shared policy auto-detects against stderr's TTY status, not
    stdout's — same discipline as :func:`pipeline.project.app.print_error`
    (#5) and :func:`core.observability.trace.vtrace` (#6b).
    """

    def test_disabled_color_reminder_is_plain(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        set_color_enabled(False)
        _patch_input(monkeypatch, ["x", "y"])
        _prompt_inline("cross_qa")
        err = capsys.readouterr().err
        assert "\x1b[" not in err
        assert "unrecognised answer 'x'" in err

    def test_forced_color_reminder_wraps_grey(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        set_color_enabled(True)
        _patch_input(monkeypatch, ["x", "y"])
        _prompt_inline("cross_qa")
        err = capsys.readouterr().err
        assert C.GREY in err
