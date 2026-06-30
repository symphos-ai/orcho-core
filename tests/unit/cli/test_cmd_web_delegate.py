"""Tests for the `orcho web` CLI delegate.

Covers two regressions the delegate refactor is responsible for not
re-introducing:

1. When orcho-web is not installed, the delegate prints an actionable
 install hint to stderr and returns rc=1 (rather than crashing or
 printing a Python traceback).
2. The argparse parser exposes ``--headless``, and the delegate forwards
 it to ``orcho_web.launcher.main``.
"""
from __future__ import annotations

import builtins
import sys
from types import SimpleNamespace

from cli.orcho import build_parser, cmd_web


def test_parser_accepts_headless_flag():
    parser = build_parser()
    args = parser.parse_args(["web", "--port", "8503", "--headless"])
    assert args.port == 8503
    assert args.headless is True


def test_parser_default_headless_false():
    parser = build_parser()
    args = parser.parse_args(["web"])
    assert args.headless is False


def test_cmd_web_install_hint_when_orcho_web_missing(monkeypatch, capsys):
    """If `orcho_web` import fails, return 1 and print install hint to stderr."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "orcho_web.launcher" or name.startswith("orcho_web"):
            raise ImportError(f"No module named '{name}'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    # Strip any cached reference so the import retries through the patched hook.
    monkeypatch.delitem(sys.modules, "orcho_web", raising=False)
    monkeypatch.delitem(sys.modules, "orcho_web.launcher", raising=False)

    rc = cmd_web(SimpleNamespace(port=8501, headless=False))
    captured = capsys.readouterr()

    assert rc == 1
    assert "orcho-web is not installed" in captured.err
    assert "pip install orcho-web" in captured.err


def test_cmd_web_forwards_headless(monkeypatch):
    """When orcho_web is installed, ``--headless`` flag flows into launcher.main."""
    captured_argv: list[list[str]] = []

    def fake_main(argv):
        captured_argv.append(argv)
        return 0

    fake_module = SimpleNamespace(main=fake_main)
    monkeypatch.setitem(sys.modules, "orcho_web", SimpleNamespace(launcher=fake_module))
    monkeypatch.setitem(sys.modules, "orcho_web.launcher", fake_module)

    rc = cmd_web(SimpleNamespace(port=9001, headless=True))
    assert rc == 0
    assert captured_argv == [["--port", "9001", "--headless"]]


def test_cmd_web_omits_headless_when_false(monkeypatch):
    captured_argv: list[list[str]] = []

    def fake_main(argv):
        captured_argv.append(argv)
        return 0

    fake_module = SimpleNamespace(main=fake_main)
    monkeypatch.setitem(sys.modules, "orcho_web", SimpleNamespace(launcher=fake_module))
    monkeypatch.setitem(sys.modules, "orcho_web.launcher", fake_module)

    rc = cmd_web(SimpleNamespace(port=8501, headless=False))
    assert rc == 0
    assert captured_argv == [["--port", "8501"]]
