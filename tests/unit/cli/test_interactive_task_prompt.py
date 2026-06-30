"""Pin guards for the ``--task`` keyboard prompt (T3 #7).

After #7 :func:`cli._task_prompt.prompt_for_task_if_needed` routes
its styling through :mod:`core.io.journey_prompt`. The
no-op-vs-prompt branching and the empty-line-abort semantics stay
byte-identical — the migration is a UX-styling pass only.

Tests cover:

* silent no-op when ``--task`` / ``--task-file`` / ``--resume`` are set;
* silent no-op when ``--no-interactive`` is set;
* silent no-op when stdin is not a TTY;
* empty input leaves ``args.task`` unchanged (the canonical
  downstream-error path);
* non-empty input populates ``args.task``;
* EOFError / KeyboardInterrupt abort cleanly;
* color policy: prompt header + ``Task:`` input prompt obey the
  shared color decision.
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator

import pytest

from cli._task_prompt import prompt_for_task_if_needed
from core.io.ansi import C, get_color_enabled, set_color_enabled


@pytest.fixture(autouse=True)
def _restore_color_override() -> Iterator[None]:
    before = get_color_enabled()
    try:
        yield
    finally:
        set_color_enabled(before)


def _ns(**kwargs) -> argparse.Namespace:
    """Make an argparse.Namespace with sensible defaults for the prompt
    helper. The helper only reads ``task`` / ``task_file`` / ``resume``
    / ``no_interactive``.
    """
    defaults = {
        "task": None,
        "task_file": None,
        "resume": None,
        "no_interactive": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _force_stdin_tty(monkeypatch: pytest.MonkeyPatch, is_tty: bool) -> None:
    """Replace ``sys.stdin.isatty`` so the helper's TTY gate sees
    ``is_tty`` regardless of how the test process was launched.
    """
    class _Stdin:
        def isatty(self) -> bool:
            return is_tty
    monkeypatch.setattr(sys, "stdin", _Stdin())


def _force_stdin_tty_with_fileno(
    monkeypatch: pytest.MonkeyPatch,
    continuation: list[str] | None = None,
    fd: int = 0,
) -> None:
    """Like :func:`_force_stdin_tty` but the fake stdin exposes a ``fileno``
    (so the paste-drain ``select`` path is reachable) and a ``readline`` that
    pops from ``continuation`` (the lines drained *after* the first ``input()``
    line). An exhausted queue returns ``""`` to model EOF.
    """
    queue = list(continuation or [])

    class _Stdin:
        def isatty(self) -> bool:
            return True

        def fileno(self) -> int:
            return fd

        def readline(self) -> str:
            return queue.pop(0) if queue else ""
    monkeypatch.setattr(sys, "stdin", _Stdin())


def _patch_input(monkeypatch: pytest.MonkeyPatch, reply: str | None) -> list[str]:
    """Patch ``builtins.input`` to return ``reply`` (or raise EOFError
    when ``reply is None``). Captures the prompt argument into a list.
    """
    captured: list[str] = []

    def _fake_input(prompt: str = "") -> str:
        captured.append(prompt)
        if reply is None:
            raise EOFError
        return reply

    monkeypatch.setattr("builtins.input", _fake_input)
    return captured


# ── no-op branches ────────────────────────────────────────────────────


class TestSilentNoOps:
    def test_existing_task_short_circuits(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Even on a TTY, ``args.task`` already set means no prompt.
        _force_stdin_tty(monkeypatch, True)
        captured = _patch_input(monkeypatch, "ignored")
        args = _ns(task="already-here")
        prompt_for_task_if_needed(args)
        assert args.task == "already-here"
        assert captured == []  # input() never called.

    def test_existing_task_file_short_circuits(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        captured = _patch_input(monkeypatch, "ignored")
        args = _ns(task_file="task.md")
        prompt_for_task_if_needed(args)
        assert args.task is None
        assert captured == []

    def test_resume_short_circuits(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        captured = _patch_input(monkeypatch, "ignored")
        args = _ns(resume="latest")
        prompt_for_task_if_needed(args)
        assert args.task is None
        assert captured == []

    def test_no_interactive_short_circuits(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        captured = _patch_input(monkeypatch, "ignored")
        args = _ns(no_interactive=True)
        prompt_for_task_if_needed(args)
        assert args.task is None
        assert captured == []

    def test_non_tty_stdin_short_circuits(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, False)
        captured = _patch_input(monkeypatch, "ignored")
        args = _ns()
        prompt_for_task_if_needed(args)
        assert args.task is None
        assert captured == []


# ── input semantics ───────────────────────────────────────────────────


class TestInputSemantics:
    def test_non_empty_input_sets_task(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_input(monkeypatch, "Add a health endpoint")
        args = _ns()
        prompt_for_task_if_needed(args)
        assert args.task == "Add a health endpoint"

    def test_empty_input_leaves_task_none(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_input(monkeypatch, "")
        args = _ns()
        prompt_for_task_if_needed(args)
        assert args.task is None

    def test_whitespace_only_input_leaves_task_none(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ``.strip()`` collapses to empty → unchanged ``task``.
        _force_stdin_tty(monkeypatch, True)
        _patch_input(monkeypatch, "   \t  ")
        args = _ns()
        prompt_for_task_if_needed(args)
        assert args.task is None

    def test_eof_aborts_cleanly(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_input(monkeypatch, None)  # raises EOFError
        args = _ns()
        prompt_for_task_if_needed(args)
        assert args.task is None
        # A trailing blank line keeps the shell prompt clean.
        out = capsys.readouterr().out
        assert out.endswith("\n\n") or out.endswith("\n")

    def test_keyboard_interrupt_aborts_cleanly(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)

        def _fake_input(prompt: str = "") -> str:
            raise KeyboardInterrupt

        monkeypatch.setattr("builtins.input", _fake_input)
        args = _ns()
        prompt_for_task_if_needed(args)
        assert args.task is None


# ── paste-drain semantics ─────────────────────────────────────────────


class TestPasteDrain:
    """A multi-line / multi-paragraph paste lands in the tty buffer as one
    burst. ``input()`` only consumes the first line; the prompt must drain the
    remaining buffered lines — including the blank lines between paragraphs —
    via ``core.io.terminal_input.drain_paste_burst`` rather than truncating at
    the first newline (the reported bug). Drain mechanics are unit-tested in
    ``tests/unit/core/io/test_terminal_input.py``; here we pin the wiring.
    """

    def test_multiline_paste_is_captured_in_full(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # First line via input(); the rest arrive as a paste burst that the
        # fake stdin's readline() drains, with blank paragraph separators.
        _force_stdin_tty_with_fileno(monkeypatch, continuation=[
            "\n",
            "Документ построен как brief.\n",
            "\n",
            "Содержит: таблицу и команды.\n",
        ])
        monkeypatch.setattr("builtins.input", lambda prompt="": "Файл: HANDOFF.md (")
        # select reports the four continuation lines ready, then drained.
        readies = iter([([0], [], [])] * 4 + [([], [], [])])
        monkeypatch.setattr(
            "core.io.terminal_input.select.select",
            lambda r, w, x, timeout: next(readies),
        )
        args = _ns()
        prompt_for_task_if_needed(args)
        assert args.task == (
            "Файл: HANDOFF.md (\n\n"
            "Документ построен как brief.\n\n"
            "Содержит: таблицу и команды."
        )

    def test_single_typed_line_returns_without_draining(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # No burst buffered → select immediately reports nothing ready, so the
        # normal "type one line + Enter" case is unchanged.
        _force_stdin_tty_with_fileno(monkeypatch)
        monkeypatch.setattr("builtins.input", lambda prompt="": "just one line")
        monkeypatch.setattr(
            "core.io.terminal_input.select.select",
            lambda r, w, x, timeout: ([], [], []),
        )
        args = _ns()
        prompt_for_task_if_needed(args)
        assert args.task == "just one line"

    def test_unselectable_stdin_falls_back_to_single_line(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Fake stdin from _force_stdin_tty has no ``fileno`` → the drain path
        # short-circuits to single-line behaviour (also the path piped / non-tty
        # callers take). No select call should happen.
        _force_stdin_tty(monkeypatch, True)
        _patch_input(monkeypatch, "single line via fallback")

        def _boom(*_a, **_k):  # pragma: no cover - must never be called
            raise AssertionError("select must not run without a fileno")

        monkeypatch.setattr("core.io.terminal_input.select.select", _boom)
        args = _ns()
        prompt_for_task_if_needed(args)
        assert args.task == "single line via fallback"


# ── color policy ──────────────────────────────────────────────────────


class TestPromptColorPolicy:
    def test_disabled_color_header_and_prompt_are_plain(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        set_color_enabled(False)
        _force_stdin_tty(monkeypatch, True)
        captured = _patch_input(monkeypatch, "ok")
        prompt_for_task_if_needed(_ns())
        header = capsys.readouterr().out
        prompt = captured[0]
        assert "\x1b[" not in header
        assert "\x1b[" not in prompt
        # Plain content survives.
        assert "No --task provided." in header
        assert "Task:" in prompt

    def test_forced_color_styles_header_and_prompt(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        set_color_enabled(True)
        _force_stdin_tty(monkeypatch, True)
        captured = _patch_input(monkeypatch, "ok")
        prompt_for_task_if_needed(_ns())
        header = capsys.readouterr().out
        prompt = captured[0]
        # Header has bold (No --task provided.) + grey (help text).
        assert C.BOLD in header
        assert C.GREY in header
        # The "Task: " input prompt is bolded.
        assert C.BOLD in prompt
