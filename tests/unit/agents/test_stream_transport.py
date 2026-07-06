"""Cross-platform contract for the streamed-subprocess byte source.

Every test here drives :func:`agents.stream._stream_run` with a specific
transport pinned. :class:`PipeTransport` is the native-Windows path, but it is
pure stdlib and runs on POSIX too — so this file exercises the Windows
streaming path on *every* runner, not only ``windows-latest``. The PTY path is
POSIX-only and skipped where ``pty.openpty`` is unavailable.
"""
from __future__ import annotations

import os
import sys

import pytest

import agents.stream as stream_module
import agents.stream_transport as stream_transport
from agents.stream import _stream_run
from agents.stream_transport import PipeTransport, PtyTransport

_HAS_PTY = hasattr(os, "openpty") and sys.platform != "win32"

# Transports under test. The pipe transport (the Windows path) always runs; the
# PTY transport is POSIX-only.
_TRANSPORTS = [
    pytest.param(PipeTransport, id="pipe"),
    pytest.param(
        PtyTransport,
        id="pty",
        marks=pytest.mark.skipif(not _HAS_PTY, reason="pty.openpty is POSIX-only"),
    ),
]


@pytest.fixture(params=_TRANSPORTS)
def force_transport(request, monkeypatch):
    """Pin ``_stream_run`` to a single transport for the test's duration."""
    factory = request.param
    monkeypatch.setattr(stream_module, "select_transport", lambda: factory())
    return factory


def test_multiline_output_streams_in_order(force_transport) -> None:
    code = "import sys\nfor i in range(5):\n    print(f'line-{i}', flush=True)\n"
    stdout, rc, stderr, _dur = _stream_run([sys.executable, "-c", code])
    assert rc == 0
    assert stderr == ""
    for i in range(5):
        assert f"line-{i}" in stdout
    assert stdout.index("line-0") < stdout.index("line-4")


def test_nonzero_exit_code_propagates(force_transport) -> None:
    stdout, rc, _stderr, _dur = _stream_run(
        [sys.executable, "-c", "import sys; print('bye', flush=True); sys.exit(7)"],
    )
    assert rc == 7
    assert "bye" in stdout


def test_stderr_is_captured(force_transport) -> None:
    code = "import sys; sys.stderr.write('boom\\n'); sys.exit(1)"
    _stdout, rc, stderr, _dur = _stream_run([sys.executable, "-c", code])
    assert rc == 1
    assert "boom" in stderr


def test_final_line_without_newline_is_captured(force_transport) -> None:
    # No trailing newline exercises the post-loop tail flush on both paths.
    stdout, rc, _stderr, _dur = _stream_run(
        [sys.executable, "-c", "import sys; sys.stdout.write('no-newline-tail')"],
    )
    assert rc == 0
    assert "no-newline-tail" in stdout


def test_large_unbroken_output_is_fully_captured(force_transport) -> None:
    # More than one read chunk with no interior newline: verifies buffer
    # reassembly across reads on both transports.
    code = "import sys; sys.stdout.write('X' * 50000 + '\\nEND\\n')"
    stdout, rc, _stderr, _dur = _stream_run([sys.executable, "-c", code])
    assert rc == 0
    assert stdout.count("X") == 50000
    assert "END" in stdout


def test_idle_timeout_kills_silent_process(force_transport) -> None:
    _stdout, rc, stderr, dur = _stream_run(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        idle_timeout=1,
    )
    assert rc != 0
    assert "IDLE TIMEOUT" in stderr
    assert dur < 5


def test_on_line_callback_receives_every_line(force_transport) -> None:
    seen: list[str] = []
    code = "import sys\nfor i in range(3):\n    print(f'row-{i}', flush=True)\n"
    _stream_run([sys.executable, "-c", code], on_line=lambda line: seen.append(line.strip()))
    assert [s for s in seen if s] == ["row-0", "row-1", "row-2"]


# ── Transport selection ──────────────────────────────────────────────────────

def test_select_transport_uses_pipe_without_openpty(monkeypatch) -> None:
    monkeypatch.setattr(stream_transport, "_HAS_OPENPTY", False)
    assert isinstance(stream_transport.select_transport(), PipeTransport)


@pytest.mark.skipif(not _HAS_PTY, reason="pty.openpty is POSIX-only")
def test_select_transport_uses_pty_with_openpty(monkeypatch) -> None:
    monkeypatch.setattr(stream_transport, "_HAS_OPENPTY", True)
    assert isinstance(stream_transport.select_transport(), PtyTransport)


# ── TTY behaviour difference (documented, not incidental) ────────────────────
#
# The child's stdout is what agent CLIs probe to choose interactive vs piped
# output formatting. Under the pipe transport it is an ordinary pipe — not a
# character device — so ``isatty`` is False on POSIX *and* Windows. (stdin is
# not a reliable signal here: on Windows the ``NUL`` device behind ``DEVNULL``
# is a character device, so ``sys.stdin.isatty()`` reports True.)

def test_pipe_transport_child_stdout_is_not_a_tty(monkeypatch) -> None:
    # Windows path: the child gets no controlling terminal, so it sees a
    # non-interactive stdout stream, exactly as under any piped invocation.
    monkeypatch.setattr(stream_module, "select_transport", lambda: PipeTransport())
    stdout, rc, _stderr, _dur = _stream_run(
        [sys.executable, "-c", "import sys; print(sys.stdout.isatty(), flush=True)"],
    )
    assert rc == 0
    assert "False" in stdout


@pytest.mark.skipif(not _HAS_PTY, reason="pty.openpty is POSIX-only")
def test_pty_transport_child_stdout_is_a_tty(monkeypatch) -> None:
    monkeypatch.setattr(stream_module, "select_transport", lambda: PtyTransport())
    stdout, rc, _stderr, _dur = _stream_run(
        [sys.executable, "-c", "import sys; print(sys.stdout.isatty(), flush=True)"],
    )
    assert rc == 0
    assert "True" in stdout
