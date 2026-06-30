"""T3 #6b color-policy pin guards for :mod:`core.observability.trace`.

trace is the third stderr-bound surface after
``pipeline/cross_project/cli.print_error`` (#3) and
``pipeline/project/app.print_error`` (#5). The migration shape is
the same — ``paint(text, *codes, stream=sys.stderr)`` — but trace is
unique in that the helpers are gated by a module-level ``_enabled``
flag (set via :func:`enable_trace`), so the policy only matters when
the gate is open.

These tests pin the disabled / forced / stream-discipline matrix
under ``enable_trace(True)`` so the auto-detect path through
``sys.stderr`` is actually exercised.
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest

from core.io.ansi import C, get_color_enabled, set_color_enabled
from core.observability.trace import enable_trace, vtrace


@pytest.fixture(autouse=True)
def _restore_color_override_and_enable_trace() -> Iterator[None]:
    color_before = get_color_enabled()
    enable_trace(True)
    try:
        yield
    finally:
        enable_trace(False)
        set_color_enabled(color_before)


class _Tty:
    """Minimal TextIO double — ``isatty`` is the policy probe; ``write``
    / ``flush`` exist so the stub can stand in for ``sys.stderr``.
    """

    def __init__(self, is_tty: bool) -> None:
        self._is_tty = is_tty
        self.chunks: list[str] = []

    def isatty(self) -> bool:
        return self._is_tty

    def write(self, text: str) -> int:
        self.chunks.append(text)
        return len(text)

    def flush(self) -> None:
        pass

    def getvalue(self) -> str:
        return "".join(self.chunks)


class TestVtraceColorPolicy:
    def test_disabled_color_yields_no_ansi_on_stderr(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        set_color_enabled(False)
        vtrace("provider", "Claude selected", extra="warm")
        captured = capsys.readouterr()
        assert "\x1b[" not in captured.err
        # Plain content survives — operators reading a log file still
        # see the trace structure.
        assert "[TRACE]" in captured.err
        assert "[provider]" in captured.err
        assert "Claude selected" in captured.err
        assert "(warm)" in captured.err

    def test_forced_color_wraps_label_and_category(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        set_color_enabled(True)
        vtrace("provider", "Claude selected")
        captured = capsys.readouterr()
        # Dim wraps [TRACE], cyan wraps [provider]; both must surface.
        assert C.DIM in captured.err
        assert C.CYAN in captured.err

    def test_stream_discipline_uses_stderr_tty_status_not_stdout(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The signature failure mode #2a guards against: sys.stdout
        non-TTY (e.g. ``orcho run > out.log``) but sys.stderr still
        on TTY → auto-detect (override=None) must consult stderr,
        not stdout, and emit color. Dropping ``stream=sys.stderr``
        from the migrated ``paint()`` calls makes this red.
        """
        import core.observability.trace as trace_mod

        monkeypatch.delenv("NO_COLOR", raising=False)
        set_color_enabled(None)
        monkeypatch.setattr(trace_mod.sys, "stdout", _Tty(False))
        stderr_stub = _Tty(True)
        monkeypatch.setattr(trace_mod.sys, "stderr", stderr_stub)

        vtrace("provider", "Claude selected")

        err_output = stderr_stub.getvalue()
        assert "\x1b[" in err_output, (
            "stream=sys.stderr discipline must apply auto-detect "
            "against stderr's TTY status, not sys.stdout's"
        )
        assert C.DIM in err_output
        assert C.CYAN in err_output

    def test_trace_disabled_emits_nothing_regardless_of_color(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        # Even with color forced on, the _enabled gate keeps trace
        # silent until enable_trace(True). The autouse fixture turned
        # it on for the suite; flip it off here to verify the gate.
        enable_trace(False)
        set_color_enabled(True)
        vtrace("provider", "should not appear")
        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out == ""
