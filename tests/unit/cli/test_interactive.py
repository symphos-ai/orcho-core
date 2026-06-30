"""Tests for the interactive --task prompt fallback in `cmd_run`.

When a user types `orcho run` without `--task`, `--task-file`, or
`--resume`, and stdin is a TTY, the CLI asks for a task description on
the keyboard. CI / MCP / piped invocations (no TTY, or `--no-interactive`)
fall through to the orchestrator's existing missing-task error so
automated transports don't hang.
"""
from __future__ import annotations

import argparse
from unittest.mock import patch

import pytest


def _make_args(**overrides) -> argparse.Namespace:
    """Minimal Namespace with the four flags the prompt helper inspects."""
    base = dict(
        task=None,
        task_file=None,
        resume=None,
        from_run_plan=None,
        no_interactive=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class TestPromptForTask:
    @pytest.fixture(autouse=True)
    def _import_helper(self):
        from cli._task_prompt import prompt_for_task_if_needed
        self.fn = prompt_for_task_if_needed

    def test_prompts_when_task_missing_and_tty(self) -> None:
        args = _make_args()
        with patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", return_value="Build a healthcheck"):
            self.fn(args)
        assert args.task == "Build a healthcheck"

    def test_strips_whitespace(self) -> None:
        args = _make_args()
        with patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", return_value="  fix bug  "):
            self.fn(args)
        assert args.task == "fix bug"

    def test_skipped_when_task_already_set(self) -> None:
        args = _make_args(task="already here")
        with patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", side_effect=AssertionError("must not prompt")):
            self.fn(args)
        assert args.task == "already here"

    def test_skipped_when_task_file_set(self) -> None:
        args = _make_args(task_file="/tmp/t.md")
        with patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", side_effect=AssertionError("must not prompt")):
            self.fn(args)
        assert args.task is None

    def test_skipped_when_resume_set(self) -> None:
        # --resume hydrates task from meta.json; never prompt.
        args = _make_args(resume="20260514_120000")
        with patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", side_effect=AssertionError("must not prompt")):
            self.fn(args)
        assert args.task is None

    def test_skipped_when_from_run_plan_set(self) -> None:
        # --from-run-plan inherits task from the parent run's meta.json
        # (pipeline/project/cli.py), same as --resume; never prompt.
        args = _make_args(from_run_plan="20260529_230840")
        with patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", side_effect=AssertionError("must not prompt")):
            self.fn(args)
        assert args.task is None

    def test_skipped_when_resume_latest_sentinel(self) -> None:
        # `--resume latest` sentinel must also suppress the prompt — at this
        # CLI layer the sentinel is just a truthy string.
        args = _make_args(resume="latest")
        with patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", side_effect=AssertionError("must not prompt")):
            self.fn(args)
        assert args.task is None

    def test_skipped_when_stdin_not_tty(self) -> None:
        args = _make_args()
        with patch("sys.stdin.isatty", return_value=False), \
             patch("builtins.input", side_effect=AssertionError("must not prompt")):
            self.fn(args)
        assert args.task is None  # falls through to orchestrator error

    def test_skipped_when_no_interactive(self) -> None:
        args = _make_args(no_interactive=True)
        with patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", side_effect=AssertionError("must not prompt")):
            self.fn(args)
        assert args.task is None

    def test_empty_input_leaves_task_unset(self) -> None:
        # Empty line → fall through to existing error so user sees the
        # canonical "task: provide --task or --task-file" message.
        args = _make_args()
        with patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", return_value=""):
            self.fn(args)
        assert args.task is None

    def test_whitespace_only_input_leaves_task_unset(self) -> None:
        args = _make_args()
        with patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", return_value="   \t  "):
            self.fn(args)
        assert args.task is None

    def test_eof_leaves_task_unset(self) -> None:
        # Ctrl-D at the prompt → bail cleanly, downstream error fires.
        args = _make_args()
        with patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", side_effect=EOFError):
            self.fn(args)
        assert args.task is None

    def test_keyboard_interrupt_leaves_task_unset(self) -> None:
        args = _make_args()
        with patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", side_effect=KeyboardInterrupt):
            self.fn(args)
        assert args.task is None
