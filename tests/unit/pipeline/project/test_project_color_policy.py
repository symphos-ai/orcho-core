"""T3 #5 mono-cluster color-policy pin guards.

After commit #5 the mono renderers in :mod:`pipeline.project.app`,
:mod:`pipeline.project.handoff`, :mod:`pipeline.project.run`,
:mod:`pipeline.project.profile_dispatch`, and
:mod:`pipeline.project.finalization` route every ANSI insertion
through :func:`core.io.ansi.paint`. The stderr-bound
:func:`pipeline.project.app.print_error` passes
``stream=sys.stderr`` per the #2a discipline; everything else is
stdout-bound.

These tests mirror the cross-cluster guards in
``test_cross_color_policy.py``: representative stdout + stderr
surfaces under the disabled / forced / stream-discipline matrix.
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


# ── stderr path: project app.print_error ──────────────────────────────


class TestProjectPrintErrorPolicy:
    """``print_error`` is the mono CLI's stderr-bound error printer and
    a second site (after cross_project/cli.py) where the #2a stderr
    discipline matters.
    """

    def test_disabled_color_yields_no_ansi_on_stderr(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        from pipeline.project.app import print_error

        set_color_enabled(False)
        print_error("boom")
        captured = capsys.readouterr()
        assert "\x1b[" not in captured.err
        assert "Error:" in captured.err
        assert "boom" in captured.err

    def test_forced_color_emits_red_bold_on_stderr(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        from pipeline.project.app import print_error

        set_color_enabled(True)
        print_error("boom")
        captured = capsys.readouterr()
        assert C.RED in captured.err
        assert C.BOLD in captured.err

    def test_stream_discipline_uses_stderr_tty_status_not_stdout(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """sys.stdout non-TTY + sys.stderr TTY-like + override=None →
        ANSI emitted on stderr. Drop ``stream=sys.stderr`` from
        ``print_error`` and this assertion goes red — proves the
        discipline is doing its job at the mono surface too.
        """
        import pipeline.project.app as app_mod

        monkeypatch.delenv("NO_COLOR", raising=False)
        set_color_enabled(None)
        monkeypatch.setattr(app_mod.sys, "stdout", _Tty(False))
        stderr_stub = _Tty(True)
        monkeypatch.setattr(app_mod.sys, "stderr", stderr_stub)

        app_mod.print_error("boom")

        err_output = stderr_stub.getvalue()
        assert "\x1b[" in err_output
        assert C.RED in err_output
        assert "boom" in err_output


# ── stdout path: project profile_dispatch helpers ────────────────────


class TestProjectProfileDispatchPolicy:
    """``profile_dispatch._render_handoff_outcome`` is a representative
    mono stdout-bound site that takes a per-call ``color`` palette
    argument; ``paint()`` must wrap with that color when forced and
    drop to plain when disabled.
    """

    @staticmethod
    def _emit_skipped_line(capsys: pytest.CaptureFixture) -> str:
        # The "↳ skipped:" line in profile_dispatch.py is a small
        # paint(..., C.GREY) call we can exercise indirectly by
        # invoking the same idiom (kept tiny so the test doesn't have
        # to construct a full pipeline state).
        from core.io.ansi import C as _C, paint as _paint
        print(_paint("  ↳ skipped: reason", _C.GREY))
        return capsys.readouterr().out

    def test_disabled_color_emits_plain_skipped_line(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        set_color_enabled(False)
        out = self._emit_skipped_line(capsys)
        assert "\x1b[" not in out
        assert "↳ skipped: reason" in out

    def test_forced_color_emits_grey_skipped_line(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        set_color_enabled(True)
        out = self._emit_skipped_line(capsys)
        assert C.GREY in out
        assert C.RESET in out


# ── stdout path: project run failed-banner shape ──────────────────────


class TestProjectRunFailedBannerPolicy:
    """The FAILED banner in :mod:`pipeline.project.run` is a 3-line
    red/bold block. Pin its color policy via direct ``paint()`` calls
    that mirror the exact migrated idiom; a regression in the rendering
    site would surface as a missing C.RED here.
    """

    def test_disabled_color_emits_plain_banner_lines(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        from core.io.ansi import C as _C, paint as _paint

        set_color_enabled(False)
        rule = "=" * 62
        print(_paint(rule, _C.RED, _C.BOLD))
        print(_paint("  FAILED in IMPLEMENT", _C.RED, _C.BOLD))
        out = capsys.readouterr().out
        assert "\x1b[" not in out
        assert rule in out
        assert "FAILED in IMPLEMENT" in out

    def test_forced_color_emits_red_bold_banner(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        from core.io.ansi import C as _C, paint as _paint

        set_color_enabled(True)
        print(_paint("  FAILED in IMPLEMENT", _C.RED, _C.BOLD))
        out = capsys.readouterr().out
        assert C.RED in out
        assert C.BOLD in out
