"""``ensure_utf8_stdio`` makes non-UTF-8 standard streams safe for emoji output.

Reproduces the native-Windows failure mode on any platform: a legacy code-page
(cp1252) stdout crashes on the ``📄``/``📡`` run-header glyphs. The helper must
reconfigure such a stream to UTF-8, and leave already-UTF-8 streams untouched.
"""
from __future__ import annotations

import io

import pytest

from core.io.encoding import ensure_utf8_stdio


def test_reconfigures_cp1252_stream_and_encodes_emoji(monkeypatch) -> None:
    buf = io.BytesIO()
    legacy = io.TextIOWrapper(buf, encoding="cp1252", line_buffering=True)
    monkeypatch.setattr("sys.stdout", legacy)

    # Precondition: the legacy stream cannot encode the run-header glyph.
    with pytest.raises(UnicodeEncodeError):
        legacy.write("📄")
        legacy.flush()

    ensure_utf8_stdio()

    import sys

    assert sys.stdout.encoding.lower().replace("-", "") == "utf8"
    print("  📄 Live output → ok")  # must not raise
    sys.stdout.flush()
    assert "📄".encode() in buf.getvalue()


def test_leaves_utf8_stream_untouched(monkeypatch) -> None:
    buf = io.BytesIO()
    utf8_stream = io.TextIOWrapper(buf, encoding="utf-8")
    monkeypatch.setattr("sys.stdout", utf8_stream)

    ensure_utf8_stdio()

    import sys

    # Same object — no needless reconfigure when already UTF-8.
    assert sys.stdout is utf8_stream
    assert sys.stdout.encoding.lower().replace("-", "") == "utf8"


def test_tolerates_stream_without_reconfigure(monkeypatch) -> None:
    # A plain buffer (no ``reconfigure``) must be left alone, not crash.
    monkeypatch.setattr("sys.stdout", io.BytesIO())
    monkeypatch.setattr("sys.stderr", io.BytesIO())
    ensure_utf8_stdio()  # no exception
