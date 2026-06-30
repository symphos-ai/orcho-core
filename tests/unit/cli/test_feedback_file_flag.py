"""``orcho run --feedback-file`` registers the long-feedback override.

The single-project CLI exposes ``--feedback-file`` so an operator can hand
a long phase-handoff verdict to the in-process prompt without pasting it
into the terminal. These tests pin that the flag exists on the project CLI
parser and that ``main()`` wires it into
``handoff_prompt.set_feedback_file_override`` before any real run work.
"""

from __future__ import annotations

import sys

import pytest


def test_run_cli_exposes_feedback_file_flag(capsys, monkeypatch) -> None:
    from pipeline.project import cli

    monkeypatch.setattr(sys, "argv", ["orcho-run", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert "--feedback-file" in capsys.readouterr().out


def test_feedback_file_arg_registers_override(monkeypatch, tmp_path) -> None:
    fb = tmp_path / "fb.txt"
    fb.write_text("a long operator verdict", encoding="utf-8")

    from pipeline.project import cli

    captured: dict[str, object] = {}

    class _Stop(Exception):
        pass

    def _capture_and_stop(path):
        captured["path"] = path
        raise _Stop()

    # The setter is the first thing main() does after parse_args; capturing
    # it (and stopping) proves the flag is threaded without a real run.
    monkeypatch.setattr(
        "pipeline.control.handoff_prompt.set_feedback_file_override",
        _capture_and_stop,
    )
    monkeypatch.setattr(sys, "argv", [
        "orcho-run", "--task", "T", "--project", str(tmp_path),
        "--feedback-file", str(fb),
    ])
    with pytest.raises(_Stop):
        cli.main()
    assert captured["path"] == str(fb)
