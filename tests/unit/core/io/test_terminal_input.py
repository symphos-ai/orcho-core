"""Unit tests for :func:`core.io.terminal_input.drain_paste_burst`.

The helper is the paste-safety primitive shared by the interactive ``--task``
prompt and the ``--resume`` follow-up-task prompt. A multi-line paste lands in
the tty buffer as one burst; without draining, a single-line read captures only
the first line and leaks the rest to the shell. These tests pin:

* a multi-line burst (incl. blank paragraph separators) is captured whole;
* a single line with nothing buffered returns immediately;
* EOF mid-burst keeps what was read;
* an unpollable stdin (``io.StringIO``, no real ``fileno``) and a fake without
  ``fileno`` both fall back to the single (newline-stripped) first line;
* a ``select`` OSError degrades to the first line rather than raising.
"""
from __future__ import annotations

import io

import pytest

from core.io.terminal_input import drain_paste_burst


class _FakeStdin:
    """Selectable stdin double: a real ``fileno`` plus a scripted
    ``readline`` queue. An exhausted queue returns ``""`` (EOF).
    """

    def __init__(self, continuation: list[str], fd: int = 0) -> None:
        self._queue = list(continuation)
        self._fd = fd

    def fileno(self) -> int:
        return self._fd

    def readline(self) -> str:
        return self._queue.pop(0) if self._queue else ""


def _patch_select(monkeypatch: pytest.MonkeyPatch, readies: list[bool]) -> None:
    """Drive ``select.select`` to report ready/not-ready per ``readies``."""
    it = iter(readies)

    def _fake_select(r, w, x, timeout):
        try:
            ready = next(it)
        except StopIteration:
            ready = False
        return (list(r) if ready else [], [], [])

    monkeypatch.setattr("core.io.terminal_input.select.select", _fake_select)


def test_multiline_burst_captured_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    stdin = _FakeStdin([
        "\n",
        "Документ построен как brief.\n",
        "\n",
        "Содержит: таблицу.\n",
    ])
    _patch_select(monkeypatch, [True, True, True, True, False])
    out = drain_paste_burst("Файл: HANDOFF.md (", stdin=stdin)
    assert out == (
        "Файл: HANDOFF.md (\n\nДокумент построен как brief.\n\nСодержит: таблицу."
    )


def test_single_line_no_buffer_returns_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdin = _FakeStdin([])
    _patch_select(monkeypatch, [False])  # nothing buffered
    assert drain_paste_burst("just one line", stdin=stdin) == "just one line"


def test_first_line_trailing_newline_normalised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # readline-style first line carries a trailing "\n"; it is stripped.
    stdin = _FakeStdin([])
    _patch_select(monkeypatch, [False])
    assert drain_paste_burst("only\n", stdin=stdin) == "only"


def test_eof_mid_burst_keeps_lines_read(monkeypatch: pytest.MonkeyPatch) -> None:
    stdin = _FakeStdin(["line two\n"])  # then EOF ("")
    _patch_select(monkeypatch, [True, True])  # ready, then ready again → EOF read
    assert drain_paste_burst("line one", stdin=stdin) == "line one\nline two"


def test_stringio_without_fileno_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # io.StringIO.fileno() raises io.UnsupportedOperation (OSError/ValueError
    # subclass) → single-line fallback, no select call.
    def _boom(*_a, **_k):  # pragma: no cover - must never run
        raise AssertionError("select must not run for unpollable stdin")

    monkeypatch.setattr("core.io.terminal_input.select.select", _boom)
    stdin = io.StringIO("ignored continuation\n")
    assert drain_paste_burst("first\n", stdin=stdin) == "first"


def test_missing_fileno_attr_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    class _NoFileno:
        def readline(self) -> str:  # pragma: no cover - never reached
            return "x\n"

    def _boom(*_a, **_k):  # pragma: no cover - must never run
        raise AssertionError("select must not run without a fileno")

    monkeypatch.setattr("core.io.terminal_input.select.select", _boom)
    assert drain_paste_burst("first", stdin=_NoFileno()) == "first"


def test_select_oserror_degrades_to_first_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdin = _FakeStdin(["should-not-be-read\n"])

    def _raise(*_a, **_k):
        raise OSError("select failed")

    monkeypatch.setattr("core.io.terminal_input.select.select", _raise)
    assert drain_paste_burst("first", stdin=stdin) == "first"


# ── stdio_interactive ──────────────────────────────────────────────────────


class _FakeStream:
    """Minimal stream double whose ``isatty`` is scripted: a bool returns
    that value; an exception type is raised (to exercise the guard)."""

    def __init__(self, isatty: bool | type[BaseException]) -> None:
        self._isatty = isatty

    def isatty(self) -> bool:
        if isinstance(self._isatty, bool):
            return self._isatty
        raise self._isatty("isatty unavailable")


def _patch_stdio(
    monkeypatch: pytest.MonkeyPatch, *, stdin: object, stdout: object,
) -> None:
    monkeypatch.setattr("core.io.terminal_input.sys.stdin", stdin)
    monkeypatch.setattr("core.io.terminal_input.sys.stdout", stdout)


def test_stdio_interactive_true_when_both_ttys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.io.terminal_input import stdio_interactive
    _patch_stdio(
        monkeypatch, stdin=_FakeStream(True), stdout=_FakeStream(True),
    )
    assert stdio_interactive() is True


def test_stdio_interactive_false_when_stdin_not_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.io.terminal_input import stdio_interactive
    _patch_stdio(
        monkeypatch, stdin=_FakeStream(False), stdout=_FakeStream(True),
    )
    assert stdio_interactive() is False


def test_stdio_interactive_false_when_stdout_not_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.io.terminal_input import stdio_interactive
    _patch_stdio(
        monkeypatch, stdin=_FakeStream(True), stdout=_FakeStream(False),
    )
    assert stdio_interactive() is False


def test_stdio_interactive_false_on_isatty_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A closed fd raises OSError from isatty — must degrade to "not
    # interactive", never propagate (an unattended run would otherwise crash
    # at the gate instead of safely skipping the prompt).
    from core.io.terminal_input import stdio_interactive
    _patch_stdio(
        monkeypatch, stdin=_FakeStream(OSError), stdout=_FakeStream(True),
    )
    assert stdio_interactive() is False


def test_stdio_interactive_false_when_isatty_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A stand-in stream without ``isatty`` (AttributeError) is also
    # uncertainty → False.
    from core.io.terminal_input import stdio_interactive
    _patch_stdio(monkeypatch, stdin=object(), stdout=_FakeStream(True))
    assert stdio_interactive() is False
