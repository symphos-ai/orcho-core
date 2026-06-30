"""Unit tests for :mod:`core.io.ansi` policy surface.

Pins the color-decision contract:

* :func:`is_color_active` honours NO_COLOR and TTY status of the
  supplied stream (or ``sys.stdout`` when none is supplied).
* :func:`paint` resolves color via explicit-arg → override →
  auto-detect, in that order.
* :func:`strip_ansi` removes CSI color codes.
* :func:`set_color_enabled` / :func:`get_color_enabled` round-trip
  the process-level override and reset cleanly with ``None``.
"""
from __future__ import annotations

import io
from collections.abc import Iterator

import pytest

from core.io import ansi
from core.io.ansi import (
    C,
    get_color_enabled,
    is_color_active,
    paint,
    set_color_enabled,
    strip_ansi,
)


@pytest.fixture(autouse=True)
def _reset_override() -> Iterator[None]:
    """Process-level override is global state; reset around every test."""
    before = get_color_enabled()
    yield
    set_color_enabled(before)


def _tty(value: bool = True) -> io.StringIO:
    s = io.StringIO()
    s.isatty = lambda: value  # type: ignore[method-assign]
    return s


# ── is_color_active ───────────────────────────────────────────────────


class TestIsColorActive:
    def test_no_color_env_disables_tty_color(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NO_COLOR", "1")
        assert is_color_active(_tty(True)) is False

    def test_no_color_empty_value_still_disables(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # NO_COLOR spec — presence alone disables, value is ignored.
        monkeypatch.setenv("NO_COLOR", "")
        assert is_color_active(_tty(True)) is False

    def test_non_tty_stream_yields_false(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)
        assert is_color_active(_tty(False)) is False

    def test_tty_stream_yields_true(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)
        assert is_color_active(_tty(True)) is True

    def test_default_stream_is_sys_stdout(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)
        import sys
        monkeypatch.setattr(sys, "stdout", _tty(True))
        assert is_color_active() is True

    def test_isatty_missing_yields_false(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)

        class _Bare:
            pass

        assert is_color_active(_Bare()) is False  # type: ignore[arg-type]

    def test_isatty_oserror_yields_false(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)

        class _Closed:
            def isatty(self) -> bool:
                raise OSError("closed fd")

        assert is_color_active(_Closed()) is False  # type: ignore[arg-type]

    def test_does_not_consult_process_override(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # is_color_active is pure auto-detect — the override is
        # combined with it inside paint(), not here.
        monkeypatch.delenv("NO_COLOR", raising=False)
        set_color_enabled(False)
        assert is_color_active(_tty(True)) is True
        set_color_enabled(True)
        assert is_color_active(_tty(False)) is False


# ── paint ─────────────────────────────────────────────────────────────


class TestPaint:
    def test_paint_color_false_returns_plain(self) -> None:
        assert paint("hello", C.BOLD, color=False) == "hello"

    def test_paint_color_true_wraps_with_codes(self) -> None:
        assert paint("hello", C.BOLD, color=True) == f"{C.BOLD}hello{C.RESET}"

    def test_paint_empty_codes_returns_plain_regardless_of_color(self) -> None:
        # Short-circuit: nothing to paint, no resolution needed.
        assert paint("hello", color=True) == "hello"
        assert paint("hello", color=False) == "hello"
        assert paint("hello", color=None) == "hello"

    def test_paint_color_none_uses_override_when_set_true(self) -> None:
        set_color_enabled(True)
        assert paint("x", C.GREEN, color=None) == f"{C.GREEN}x{C.RESET}"

    def test_paint_color_none_uses_override_when_set_false(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Even though the stream looks like a TTY, the override wins.
        monkeypatch.delenv("NO_COLOR", raising=False)
        set_color_enabled(False)
        assert paint("x", C.GREEN, color=None, stream=_tty(True)) == "x"

    def test_paint_color_explicit_overrides_process_flag(self) -> None:
        # Explicit > override > auto-detect — first leg of the contract.
        set_color_enabled(False)
        assert paint("x", C.RED, color=True) == f"{C.RED}x{C.RESET}"
        set_color_enabled(True)
        assert paint("x", C.RED, color=False) == "x"

    def test_paint_color_none_no_override_uses_auto_detect_tty(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)
        set_color_enabled(None)
        assert (
            paint("x", C.CYAN, color=None, stream=_tty(True))
            == f"{C.CYAN}x{C.RESET}"
        )

    def test_paint_color_none_no_override_uses_auto_detect_non_tty(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)
        set_color_enabled(None)
        assert paint("x", C.CYAN, color=None, stream=_tty(False)) == "x"

    def test_paint_color_none_no_color_env_disables(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NO_COLOR", "1")
        set_color_enabled(None)
        assert paint("x", C.CYAN, color=None, stream=_tty(True)) == "x"

    def test_paint_concatenates_multiple_codes(self) -> None:
        assert (
            paint("x", C.GREEN, C.BOLD, color=True)
            == f"{C.GREEN}{C.BOLD}x{C.RESET}"
        )

    def test_paint_explicit_stream_overrides_sys_stdout_for_autodetect(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Stderr discipline: a stderr-bound renderer must pass
        ``stream=sys.stderr`` to ``paint()``, otherwise auto-detect
        will consult ``sys.stdout`` and an
        ``orcho run > out.log`` invocation with stderr still attached
        to a TTY would wrongly suppress color on stderr-bound output
        (or vice versa).

        Pins the contract: when ``color=None`` and the override is
        unset, the explicit ``stream=`` argument decides — independent
        of what ``sys.stdout`` looks like at the time of the call.
        """
        monkeypatch.delenv("NO_COLOR", raising=False)
        set_color_enabled(None)

        # sys.stdout looks non-TTY (piped), but stderr is still a TTY.
        monkeypatch.setattr("sys.stdout", _tty(False))
        err = _tty(True)

        # Without an explicit stream → auto-detect against sys.stdout →
        # non-TTY → plain. This is the foot-gun the discipline guards.
        assert paint("x", C.RED, color=None) == "x"
        # With explicit stream=err → auto-detect against the real
        # stderr → TTY → colored. This is the correct call shape for
        # stderr renderers.
        assert paint("x", C.RED, color=None, stream=err) == f"{C.RED}x{C.RESET}"


# ── set/get override ──────────────────────────────────────────────────


class TestOverrideAccessors:
    def test_get_returns_current_override(self) -> None:
        set_color_enabled(None)
        assert get_color_enabled() is None
        set_color_enabled(True)
        assert get_color_enabled() is True
        set_color_enabled(False)
        assert get_color_enabled() is False

    def test_set_none_clears_override(self) -> None:
        set_color_enabled(True)
        assert get_color_enabled() is True
        set_color_enabled(None)
        assert get_color_enabled() is None

    def test_override_is_module_level_state(self) -> None:
        # Pins that the override lives at the module level (not
        # thread-local or per-call). T3 callers rely on a one-shot
        # CLI-startup `set_color_enabled(...)` propagating to every
        # later paint() call across modules.
        set_color_enabled(True)
        assert ansi.get_color_enabled() is True
        # Different import binding, same state.
        from core.io.ansi import get_color_enabled as _alt
        assert _alt() is True


# ── strip_ansi ────────────────────────────────────────────────────────


class TestStripAnsi:
    def test_strip_removes_csi_color_codes(self) -> None:
        coloured = f"{C.GREEN}{C.BOLD}hello{C.RESET}"
        assert strip_ansi(coloured) == "hello"

    def test_strip_passes_plain_text_through(self) -> None:
        assert strip_ansi("plain") == "plain"

    def test_strip_handles_multiple_segments(self) -> None:
        text = (
            f"{C.CYAN}A{C.RESET}-{C.RED}B{C.RESET}-{C.GREEN}C{C.RESET}"
        )
        assert strip_ansi(text) == "A-B-C"

    def test_strip_handles_empty_string(self) -> None:
        assert strip_ansi("") == ""

    def test_strip_preserves_non_csi_escapes(self) -> None:
        # Only CSI ``\x1b[…m`` SGR sequences are colors; other
        # escapes (e.g. cursor movement) are left intact so the helper
        # cannot accidentally garble a non-color terminal stream that
        # happens to ride through it.
        text = "\x1b]0;title\x07after"  # OSC, not SGR
        assert strip_ansi(text) == text
