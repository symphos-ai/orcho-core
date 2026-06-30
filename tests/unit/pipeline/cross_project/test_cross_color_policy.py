"""T3 cross-cluster color-policy pin guards.

After commit #3 the cross renderers
(:mod:`pipeline.cross_project.planning_loop`,
:mod:`pipeline.cross_project.app`, :mod:`pipeline.cross_project.cli`,
:mod:`pipeline.cross_project.finalization`) route every ANSI insertion
through :func:`core.io.ansi.paint`. These tests verify the contract
the migration is supposed to deliver:

* A representative stdout-bound site honours the process-level color
  override.
* The stderr-bound :func:`pipeline.cross_project.cli.print_error`
  passes ``stream=sys.stderr`` so auto-detect consults stderr's TTY
  status — without that discipline an ``orcho cross > out.log`` run
  with stderr still on a TTY would wrongly suppress the colored
  Error line (or its mirror would leak ANSI into a stdout file).
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


class _Tty:
    """Minimal TextIO double — ``isatty`` is the policy probe; ``write``
    / ``flush`` exist so the stub can stand in for ``sys.stderr`` when
    ``print(..., file=stub)`` runs against it.
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


# ── stdout path (cross app._print_usage_snapshot) ─────────────────────


class TestCrossStdoutPolicy:
    """``_print_usage_snapshot`` is a single-line ``paint(..., C.GREY)``
    call inside the cross app — a representative stdout-bound site
    after #3 migration.
    """

    @staticmethod
    def _snapshot(capsys: pytest.CaptureFixture) -> str:
        from pipeline.cross_project.usage import _print_usage_snapshot
        _print_usage_snapshot("CROSS_PLAN", {"in": 100, "out": 50})
        return capsys.readouterr().out

    def test_disabled_color_yields_no_ansi_on_stdout(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        set_color_enabled(False)
        out = self._snapshot(capsys)
        assert "\x1b[" not in out
        # Plain content still surfaces — the snapshot must not vanish.
        assert "CROSS_PLAN" in out

    def test_forced_color_emits_palette_on_stdout(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        set_color_enabled(True)
        out = self._snapshot(capsys)
        # Snapshot is grey-painted; presence of the grey escape proves
        # the migrated paint() call actually wrapped the string.
        assert C.GREY in out
        assert C.RESET in out


# ── stderr path (cross cli.print_error) ───────────────────────────────


class TestCrossStderrPolicy:
    """``print_error`` is the cross CLI's stderr-bound error printer
    and the first cross-cluster site that passes ``stream=sys.stderr``
    to ``paint()``. These pin guards lock the contract in place.
    """

    def test_disabled_color_yields_no_ansi_on_stderr(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        from pipeline.cross_project.cli import print_error

        set_color_enabled(False)
        print_error("boom")
        captured = capsys.readouterr()
        assert "\x1b[" not in captured.err
        # Plain message still surfaces; this is an error printer.
        assert "Error:" in captured.err
        assert "boom" in captured.err

    def test_forced_color_emits_palette_on_stderr(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        from pipeline.cross_project.cli import print_error

        set_color_enabled(True)
        print_error("boom")
        captured = capsys.readouterr()
        # Red palette anchors the label and the body.
        assert C.RED in captured.err
        assert C.BOLD in captured.err

    def test_stream_discipline_uses_stderr_tty_status_not_stdout(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Critical migration guard: ``print_error`` passes
        ``stream=sys.stderr`` so when stdout is non-TTY but stderr is
        TTY-like, auto-detect (override=None) decides against the
        stderr stream and emits color. Without that discipline the
        cli would consult ``sys.stdout`` and silently suppress the
        red Error line on a piped-stdout invocation.
        """
        import pipeline.cross_project.cli as cli_mod

        monkeypatch.delenv("NO_COLOR", raising=False)
        set_color_enabled(None)
        # sys.stdout looks non-TTY (e.g. piped to a file).
        monkeypatch.setattr(cli_mod.sys, "stdout", _Tty(False))
        # sys.stderr stays TTY-like (operator still watches the terminal).
        stderr_stub = _Tty(True)
        monkeypatch.setattr(cli_mod.sys, "stderr", stderr_stub)

        cli_mod.print_error("boom")

        err_output = stderr_stub.getvalue()
        # ANSI emitted because stream=sys.stderr saw a TTY stderr,
        # even though sys.stdout is non-TTY. Drop the discipline
        # (remove ``stream=sys.stderr``) and this assertion goes red.
        assert "\x1b[" in err_output
        assert C.RED in err_output
        assert "boom" in err_output
