"""T3 #6a engine-cluster color-policy pin guards.

After commit #6a the following renderers route every ANSI insertion
through :func:`core.io.ansi.paint`:

* :mod:`pipeline.engine.run_diff` — file-path bold chip, ``+N -N``
  per-file stat row, hunk/added/removed line colorisation. All
  take an explicit ``color: bool`` parameter that is now threaded
  through ``paint(text, *codes, color=color)`` — the explicit
  argument wins per T2 resolution order.
* :mod:`pipeline.engine.run_logging` — two grey path chips printed
  during run header setup.
* :mod:`pipeline.phases.builtin` — Files Diff (this phase) header.
* :mod:`agents.stream_parsers.tool_invocations` — grey absolute-path
  suffix on the live transcript.

These tests focus on the ``color: bool`` propagation contract in
run_diff (a representative case for renderers that pass color
explicitly) plus run_logging's process-policy adoption (a
representative case for renderers that consult the override).
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest

from core.io.ansi import C, get_color_enabled, set_color_enabled


@pytest.fixture(autouse=True)
def _restore_color_override() -> Iterator[None]:
    before = get_color_enabled()
    try:
        yield
    finally:
        set_color_enabled(before)


# ── run_diff: explicit color=bool propagation ─────────────────────────


class TestRunDiffExplicitColor:
    """``run_diff`` renderers take ``color: bool`` from the caller.
    The explicit argument must reach ``paint()`` and win over the
    process override / auto-detect (T2 contract: explicit > override
    > auto).
    """

    def test_color_false_yields_plain_diff_line(self) -> None:
        from pipeline.engine.run_diff import _color_diff_line
        # Even with the override forced on, color=False wins.
        set_color_enabled(True)
        assert _color_diff_line("+added", color=False) == "+added"
        assert _color_diff_line("-removed", color=False) == "-removed"
        assert _color_diff_line("@@ hunk @@", color=False) == "@@ hunk @@"

    def test_color_true_wraps_diff_lines_with_palette(self) -> None:
        from pipeline.engine.run_diff import _color_diff_line
        # Override forced off; the explicit color=True wins.
        set_color_enabled(False)
        assert _color_diff_line("+a", color=True) == f"{C.GREEN}+a{C.RESET}"
        assert _color_diff_line("-b", color=True) == f"{C.RED}-b{C.RESET}"
        assert (
            _color_diff_line("@@ h @@", color=True) == f"{C.CYAN}@@ h @@{C.RESET}"
        )

    def test_unrecognised_diff_line_passes_through_unwrapped(self) -> None:
        from pipeline.engine.run_diff import _color_diff_line
        # Context lines (no +/-/@@) stay plain regardless of color.
        assert _color_diff_line(" context", color=True) == " context"
        assert _color_diff_line(" context", color=False) == " context"


# ── tool_invocations: process policy via paint() default ──────────────


class TestToolInvocationGreyHint:
    """``format_tool_call`` calls ``paint(..., C.GREY)`` without an
    explicit ``color=`` — the shared policy (override + auto-detect)
    decides. Pin the disabled / forced cases.
    """

    @staticmethod
    def _render() -> str:
        from agents.stream_parsers.tool_invocations import (
            NormalizedToolCall,
            format_tool_call,
        )
        call = NormalizedToolCall(
            event_kind="agent.tool_use",
            tool_category="file_read",
            tool_name="Read",
            display_name="Read",
            summary="api/teams.py",
            abs_hint="/checkout/api/teams.py",
        )
        return format_tool_call(call)

    def test_disabled_color_drops_grey_suffix_codes(self) -> None:
        set_color_enabled(False)
        rendered = self._render()
        # Suffix content survives; ANSI does not.
        assert "(/checkout/api/teams.py)" in rendered
        assert "\x1b[" not in rendered

    def test_forced_color_wraps_grey_suffix(self) -> None:
        set_color_enabled(True)
        rendered = self._render()
        assert C.GREY in rendered
        assert C.RESET in rendered
        assert "(/checkout/api/teams.py)" in rendered
