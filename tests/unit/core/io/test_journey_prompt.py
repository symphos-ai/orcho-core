"""Unit tests for :mod:`core.io.journey_prompt`.

Pins the visible contract of the prompt-styling helpers: each painter
respects ``color=False`` by returning plain text, ``is_color_active``
honours the NO_COLOR opt-out and any stream-level ``isatty`` quirks,
and the framing primitives (``divider``, ``default_chip``, ``title``,
``help_line``) carry the expected substrings under both color modes.
"""
from __future__ import annotations

import io
import re

import pytest

from core.io import ansi, journey_prompt
from core.io.ansi import C
from core.io.journey_prompt import (
    bold,
    cyan_bold,
    default_chip,
    divider,
    green_bold,
    grey,
    help_line,
    is_color_active,
    paint,
    title,
)

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _strip(text: str) -> str:
    return _ANSI.sub("", text)


# ── paint ──────────────────────────────────────────────────────────────


class TestPaint:
    def test_color_false_returns_plain_text(self) -> None:
        assert paint("hello", C.BOLD, color=False) == "hello"

    def test_color_true_wraps_with_codes_and_reset(self) -> None:
        out = paint("hello", C.BOLD, color=True)
        assert out.startswith(C.BOLD)
        assert out.endswith(C.RESET)
        assert _strip(out) == "hello"

    def test_multiple_codes_concatenate(self) -> None:
        out = paint("x", C.GREEN, C.BOLD, color=True)
        assert out == f"{C.GREEN}{C.BOLD}x{C.RESET}"

    def test_empty_codes_returns_plain_text(self) -> None:
        # No-op shortcut: with no codes there is nothing to wrap.
        assert paint("hello", color=True) == "hello"


# ── named painters ────────────────────────────────────────────────────


class TestNamedPainters:
    @pytest.mark.parametrize(
        ("painter", "codes"),
        [
            (bold,       (C.BOLD,)),
            (grey,       (C.GREY,)),
            (green_bold, (C.GREEN, C.BOLD)),
            (cyan_bold,  (C.CYAN, C.BOLD)),
        ],
    )
    def test_color_on_wraps_with_expected_codes(
        self, painter, codes: tuple[str, ...],
    ) -> None:
        out = painter("text", color=True)
        assert out == f"{''.join(codes)}text{C.RESET}"

    @pytest.mark.parametrize(
        "painter", [bold, grey, green_bold, cyan_bold],
    )
    def test_color_off_returns_plain_text(self, painter) -> None:
        assert painter("text", color=False) == "text"

    def test_title_is_cyan_bold(self) -> None:
        assert title("T", color=True) == cyan_bold("T", color=True)

    def test_help_line_is_grey(self) -> None:
        assert help_line("h", color=True) == grey("h", color=True)


# ── framing primitives ────────────────────────────────────────────────


class TestFraming:
    def test_default_chip_contains_literal_default(self) -> None:
        assert _strip(default_chip(color=True)) == "[default]"
        assert default_chip(color=False) == "[default]"

    def test_default_chip_uses_green_bold_when_colored(self) -> None:
        out = default_chip(color=True)
        # Same wrapping as a direct green_bold call so the visual stays
        # in lockstep with the named painter contract.
        assert out == green_bold("[default]", color=True)

    def test_divider_is_68_box_chars(self) -> None:
        plain = divider(color=False)
        assert plain == "═" * 68

    def test_divider_color_on_keeps_visible_width(self) -> None:
        out = divider(color=True)
        assert _strip(out) == "═" * 68
        assert C.GREY in out
        assert out.endswith(C.RESET)


# ── is_color_active ───────────────────────────────────────────────────


class TestIsColorActive:
    def _tty_stream(self) -> io.StringIO:
        s = io.StringIO()
        s.isatty = lambda: True  # type: ignore[method-assign]
        return s

    def _non_tty_stream(self) -> io.StringIO:
        s = io.StringIO()
        s.isatty = lambda: False  # type: ignore[method-assign]
        return s

    def test_tty_stream_yields_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)
        assert is_color_active(self._tty_stream()) is True

    def test_non_tty_stream_yields_false(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)
        assert is_color_active(self._non_tty_stream()) is False

    def test_no_color_env_overrides_tty(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NO_COLOR", "1")
        assert is_color_active(self._tty_stream()) is False

    def test_no_color_takes_any_value(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # NO_COLOR spec: presence alone disables; value is ignored.
        monkeypatch.setenv("NO_COLOR", "")
        assert is_color_active(self._tty_stream()) is False

    def test_stream_without_isatty_attribute_yields_false(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)

        class _Bare:
            pass

        assert is_color_active(_Bare()) is False  # type: ignore[arg-type]

    def test_stream_isatty_raising_oserror_yields_false(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)

        class _Closed:
            def isatty(self) -> bool:
                raise OSError("closed fd")

        assert is_color_active(_Closed()) is False  # type: ignore[arg-type]

    def test_default_stream_is_sys_stdout(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # No explicit stream → reads from sys.stdout. Stub it to a
        # TTY-like and verify the helper picks it up.
        monkeypatch.delenv("NO_COLOR", raising=False)
        import sys
        stub = self._tty_stream()
        monkeypatch.setattr(sys, "stdout", stub)
        assert is_color_active() is True


# ── re-export compatibility ───────────────────────────────────────────


class TestReExports:
    """Pin the compat-surface contract: ``journey_prompt`` re-exports
    the policy primitives from ``core.io.ansi`` so existing prompt
    callers (commit_delivery, pre_run_dirty, resume_prompt) keep
    their import paths working after T2."""

    def test_is_color_active_is_same_object_as_ansi(self) -> None:
        assert journey_prompt.is_color_active is ansi.is_color_active

    def test_paint_is_same_object_as_ansi(self) -> None:
        assert journey_prompt.paint is ansi.paint

    def test_strip_ansi_is_re_exported(self) -> None:
        assert journey_prompt.strip_ansi is ansi.strip_ansi

    def test_journey_prompt_is_color_active_matches_ansi_behaviour(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NO_COLOR", "1")
        assert is_color_active(self._tty_stream()) is False
        monkeypatch.delenv("NO_COLOR")
        assert is_color_active(self._tty_stream()) is True

    def _tty_stream(self) -> io.StringIO:
        s = io.StringIO()
        s.isatty = lambda: True  # type: ignore[method-assign]
        return s


# ── ask_yn ────────────────────────────────────────────────────────────


class TestAskYn:
    """The shared yes/no prompt used by `orcho workspace init` flows."""

    def _ask(self, text: str, *, default_yes: bool = True):
        stdin = io.StringIO(text)
        return journey_prompt.ask_yn(
            "Proceed?", default_yes=default_yes,
            stdin=stdin, stdout=io.StringIO(), color=False,
        )

    def test_yes_variants(self) -> None:
        assert self._ask("y\n") is True
        assert self._ask("YES\n") is True

    def test_no(self) -> None:
        assert self._ask("n\n") is False

    def test_empty_line_returns_default(self) -> None:
        assert self._ask("\n", default_yes=True) is True
        assert self._ask("\n", default_yes=False) is False

    def test_eof_returns_none(self) -> None:
        assert self._ask("") is None

    def test_keyboard_interrupt_returns_none(self) -> None:
        class _CtrlCStdin:
            def readline(self):
                raise KeyboardInterrupt

        out = io.StringIO()
        result = journey_prompt.ask_yn(
            "Proceed?", default_yes=True,
            stdin=_CtrlCStdin(), stdout=out, color=False,
        )
        assert result is None
        assert out.getvalue().endswith("\n")

    def test_hint_reflects_default(self) -> None:
        out = io.StringIO()
        journey_prompt.ask_yn(
            "Proceed?", default_yes=False,
            stdin=io.StringIO("\n"), stdout=out, color=False,
        )
        assert "[y/N]" in out.getvalue()
