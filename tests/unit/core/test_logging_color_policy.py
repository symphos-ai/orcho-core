"""T3 #4 color-policy pin guards for ``core.observability.logging``.

After commit #4 the three stdout-bound helpers exposed by
``core.observability.logging`` route their ANSI insertion through
``paint()``:

* :func:`success` — green/bold ``✓`` chip
* :func:`warn` — yellow/bold ``⚠`` chip
* :func:`preview_heading` — colored ``label:`` line

These tests verify the migration delivers the policy contract: the
process-level override decides whether ANSI is emitted; the printed
plain content is independent of the color decision.
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest

from core.io.ansi import C, get_color_enabled, set_color_enabled
from core.observability.logging import preview_heading, success, warn


@pytest.fixture(autouse=True)
def _restore_color_override() -> Iterator[None]:
    before = get_color_enabled()
    try:
        yield
    finally:
        set_color_enabled(before)


class TestSuccessChipColorPolicy:
    def test_disabled_color_emits_plain(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        set_color_enabled(False)
        success("plan approved")
        out = capsys.readouterr().out
        assert "\x1b[" not in out
        assert "✓ plan approved" in out

    def test_forced_color_emits_green_bold(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        set_color_enabled(True)
        success("plan approved")
        out = capsys.readouterr().out
        assert C.GREEN in out
        assert C.BOLD in out
        assert C.RESET in out


class TestWarnChipColorPolicy:
    def test_disabled_color_emits_plain(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        set_color_enabled(False)
        warn("checkpoint missing")
        out = capsys.readouterr().out
        assert "\x1b[" not in out
        assert "⚠ checkpoint missing" in out

    def test_forced_color_emits_yellow_bold(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        set_color_enabled(True)
        warn("checkpoint missing")
        out = capsys.readouterr().out
        assert C.YELLOW in out
        assert C.BOLD in out


class TestPreviewHeadingColorPolicy:
    def test_disabled_color_emits_plain(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        set_color_enabled(False)
        preview_heading("Plan")
        out = capsys.readouterr().out
        assert "\x1b[" not in out
        assert "Plan:" in out

    def test_forced_color_emits_supplied_palette(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        set_color_enabled(True)
        preview_heading("Plan", color=C.CYAN)
        out = capsys.readouterr().out
        # The caller-supplied color must reach the rendered string;
        # before #4 it was concatenated raw, now paint() wraps with it.
        assert C.CYAN in out
        assert C.RESET in out
