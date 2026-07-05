"""Pin guards for the operator-facade profile enforcement (T1/T2).

The ``orcho`` facade no longer silently falls back to ``advanced`` when a
fresh run is started without ``--profile``. Instead
:func:`cli._profile_prompt.require_profile_or_exit` runs the interactive
picker and then enforces a decision:

* the operator declined the interactive menu → clean exit ``0``;
* the menu could not be shown (non-TTY / ``--no-interactive``) and no
  profile is set → error on stderr, exit ``2``;
* a profile is set (picked, explicit, or inherited via ``--resume`` /
  ``--from-run-plan``) → return ``None`` so the caller proceeds.

``cmd_run`` / ``cmd_cross`` return the helper's code immediately and never
start the pipeline on a non-``None`` code. ``cmd_cross`` forwards the
``_cross_eligible`` predicate as ``profile_filter``.

stdin TTY state, ``builtins.input`` and the profile catalog are
monkeypatched, so tests never touch the real ``CONFIG_DIR``.
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator

import pytest

import cli.orcho
from cli._profile_prompt import require_profile_or_exit
from core.io.ansi import get_color_enabled, set_color_enabled


@pytest.fixture(autouse=True)
def _restore_color_override() -> Iterator[None]:
    before = get_color_enabled()
    try:
        yield
    finally:
        set_color_enabled(before)


class _FakeProfile:
    def __init__(self, description: str = "") -> None:
        self.description = description


def _ns(**kwargs) -> argparse.Namespace:
    """Namespace with the fields the facade path reads."""
    defaults = {
        "profile": None,
        "resume": None,
        "from_run_plan": None,
        "no_interactive": False,
        "task": "do the thing",  # short-circuits prompt_for_task_if_needed
        "task_file": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _force_stdin_tty(monkeypatch: pytest.MonkeyPatch, is_tty: bool) -> None:
    class _Stdin:
        def isatty(self) -> bool:
            return is_tty
    monkeypatch.setattr(sys, "stdin", _Stdin())


def _patch_input(
    monkeypatch: pytest.MonkeyPatch,
    replies: str | list[str] | None,
) -> list[str]:
    captured: list[str] = []
    queue = list(replies) if isinstance(replies, list) else None

    def _fake_input(prompt: str = "") -> str:
        captured.append(prompt)
        if replies is None:
            raise EOFError
        if queue is not None:
            if not queue:
                raise EOFError
            return queue.pop(0)
        return replies  # type: ignore[return-value]

    monkeypatch.setattr("builtins.input", _fake_input)
    return captured


def _patch_catalog(
    monkeypatch: pytest.MonkeyPatch,
    profiles: dict | Exception,
) -> None:
    def _loader(_path):
        if isinstance(profiles, Exception):
            raise profiles
        return profiles

    monkeypatch.setattr(
        "pipeline.profiles.loader.load_profiles_v2_with_plugins",
        _loader,
    )


def _catalog() -> dict:
    # sorted() over keys → menu order: advanced, lite, review.
    return {
        "lite": _FakeProfile("Fast single-pass profile."),
        "advanced": _FakeProfile("Full plan/implement/review profile."),
        "review": _FakeProfile("Review-only profile."),
    }


# ── require_profile_or_exit (direct) ──────────────────────────────────


class TestRequireProfileOrExit:
    def test_interactive_abort_returns_zero(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _catalog())
        _patch_input(monkeypatch, "")  # empty line → ABORTED
        args = _ns()
        code = require_profile_or_exit(args)
        assert code == 0
        assert args.profile is None

    def test_non_tty_fresh_returns_two_on_stderr(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _force_stdin_tty(monkeypatch, False)
        _patch_catalog(monkeypatch, _catalog())
        captured = _patch_input(monkeypatch, "ignored")
        args = _ns()
        code = require_profile_or_exit(args)
        assert code == 2
        assert args.profile is None
        assert captured == []  # no menu, no prompt
        err = capsys.readouterr().err
        assert "--profile" in err

    def test_no_interactive_fresh_returns_two(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _catalog())
        args = _ns(no_interactive=True)
        code = require_profile_or_exit(args)
        assert code == 2
        assert capsys.readouterr().err.strip() != ""

    def test_interactive_selection_returns_none(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _catalog())
        _patch_input(monkeypatch, "1")  # sorted → advanced
        args = _ns()
        code = require_profile_or_exit(args)
        assert code is None
        assert args.profile == "advanced"

    def test_resume_skips_enforcement(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Non-TTY + fresh would be code 2, but --resume must inherit the
        # profile downstream, so enforcement returns None.
        _force_stdin_tty(monkeypatch, False)
        _patch_catalog(monkeypatch, _catalog())
        args = _ns(resume="latest")
        assert require_profile_or_exit(args) is None
        assert args.profile is None

    def test_from_run_plan_skips_enforcement(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, False)
        _patch_catalog(monkeypatch, _catalog())
        args = _ns(from_run_plan="20260529_230840")
        assert require_profile_or_exit(args) is None
        assert args.profile is None

    def test_explicit_profile_returns_none_without_menu(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _catalog())
        captured = _patch_input(monkeypatch, "ignored")
        args = _ns(profile="lite")
        assert require_profile_or_exit(args) is None
        assert args.profile == "lite"
        assert captured == []

    @pytest.mark.parametrize("empty", ["", "   "])
    def test_empty_profile_on_tty_shows_menu_and_abort_returns_zero(
        self, monkeypatch: pytest.MonkeyPatch, empty: str,
    ) -> None:
        # An empty / whitespace-only --profile must be treated as "not set":
        # the menu is shown, and cancelling it is a clean exit 0 — not a
        # silent fall-through to the downstream advanced default.
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _catalog())
        captured = _patch_input(monkeypatch, "")  # menu shown, then cancel
        args = _ns(profile=empty)
        code = require_profile_or_exit(args)
        assert code == 0
        assert args.profile is None
        assert captured != []  # the picker actually prompted

    @pytest.mark.parametrize("empty", ["", "   "])
    def test_empty_profile_non_tty_returns_two(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture, empty: str,
    ) -> None:
        _force_stdin_tty(monkeypatch, False)
        _patch_catalog(monkeypatch, _catalog())
        args = _ns(profile=empty)
        code = require_profile_or_exit(args)
        assert code == 2
        assert args.profile is None
        assert "--profile" in capsys.readouterr().err


# ── cmd_run integration ───────────────────────────────────────────────


class TestCmdRunEnforcement:
    @pytest.fixture(autouse=True)
    def _stub_pipeline(self, monkeypatch: pytest.MonkeyPatch):
        self.calls: list[argparse.Namespace] = []

        def _run(args: argparse.Namespace) -> int:
            self.calls.append(args)
            return 0
        monkeypatch.setattr(cli.orcho, "run_pipeline_from_args", _run)
        # Task prompt is orthogonal here; neutralize it.
        monkeypatch.setattr(cli.orcho, "prompt_for_task_if_needed", lambda a: None)

    def test_abort_returns_zero_and_skips_pipeline(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _catalog())
        _patch_input(monkeypatch, None)  # ^D / ^C at the picker → ABORTED
        code = cli.orcho.cmd_run(_ns())
        assert code == 0
        assert self.calls == []

    def test_empty_selects_auto_detect_and_runs(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The orcho run picker offers auto-detect as the default choice: a
        # bare Enter selects it and the pipeline proceeds with the
        # ``auto-detect`` selector (resolved downstream), not an abort.
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _catalog())
        _patch_input(monkeypatch, "")  # bare Enter → auto-detect
        code = cli.orcho.cmd_run(_ns())
        assert code == 0
        assert len(self.calls) == 1
        assert self.calls[0].profile == "auto-detect"

    def test_non_tty_returns_two_and_skips_pipeline(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _force_stdin_tty(monkeypatch, False)
        _patch_catalog(monkeypatch, _catalog())
        code = cli.orcho.cmd_run(_ns())
        assert code == 2
        assert self.calls == []
        assert "--profile" in capsys.readouterr().err

    def test_empty_profile_non_tty_returns_two_and_skips_pipeline(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        # `--profile ""` must not slip through as a "set" profile and reach
        # the pipeline (where the downstream advanced default would apply).
        _force_stdin_tty(monkeypatch, False)
        _patch_catalog(monkeypatch, _catalog())
        code = cli.orcho.cmd_run(_ns(profile=""))
        assert code == 2
        assert self.calls == []
        assert "--profile" in capsys.readouterr().err

    def test_selection_runs_pipeline_with_profile(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _catalog())
        _patch_input(monkeypatch, "review")
        code = cli.orcho.cmd_run(_ns())
        assert code == 0
        assert len(self.calls) == 1
        assert self.calls[0].profile == "review"

    def test_resume_runs_pipeline_without_profile(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, False)
        _patch_catalog(monkeypatch, _catalog())
        code = cli.orcho.cmd_run(_ns(resume="latest"))
        assert code == 0
        assert len(self.calls) == 1
        assert self.calls[0].profile is None

    def test_from_run_plan_runs_pipeline_without_profile(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, False)
        _patch_catalog(monkeypatch, _catalog())
        code = cli.orcho.cmd_run(_ns(from_run_plan="20260529_230840"))
        assert code == 0
        assert len(self.calls) == 1
        assert self.calls[0].profile is None

    def test_explicit_profile_runs_pipeline_without_menu(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _catalog())
        captured = _patch_input(monkeypatch, "ignored")
        code = cli.orcho.cmd_run(_ns(profile="advanced"))
        assert code == 0
        assert len(self.calls) == 1
        assert self.calls[0].profile == "advanced"
        assert captured == []


# ── cmd_cross integration ─────────────────────────────────────────────


class TestCmdCrossEnforcement:
    @pytest.fixture(autouse=True)
    def _stub_pipeline(self, monkeypatch: pytest.MonkeyPatch):
        self.calls: list[argparse.Namespace] = []

        def _run(args: argparse.Namespace) -> int:
            self.calls.append(args)
            return 0
        monkeypatch.setattr(cli.orcho, "run_cross_from_args", _run)

    def test_cross_forwards_cross_eligible_filter(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pipeline.cross_project.profile_projection import (
            CrossProjectionError,
        )

        captured_filter: dict = {}

        def _capture(args, *, profile_filter=None):
            captured_filter["fn"] = profile_filter
            return None

        monkeypatch.setattr(cli.orcho, "require_profile_or_exit", _capture)

        ineligible = _FakeProfile("raises")

        def _project(profile):
            if profile is ineligible:
                raise CrossProjectionError("no per-step cross policy")
            return object()

        monkeypatch.setattr(
            "pipeline.cross_project.profile_projection.project_cross_profile",
            _project,
        )

        cli.orcho.cmd_cross(_ns())
        predicate = captured_filter["fn"]
        assert predicate is not None
        assert predicate(_FakeProfile("ok")) is True
        assert predicate(ineligible) is False

    def test_non_tty_returns_two_and_skips_pipeline(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _force_stdin_tty(monkeypatch, False)
        _patch_catalog(monkeypatch, _catalog())
        code = cli.orcho.cmd_cross(_ns())
        assert code == 2
        assert self.calls == []
        assert "--profile" in capsys.readouterr().err

    def test_abort_returns_zero_and_skips_pipeline(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _catalog())
        # All catalog profiles are cross-eligible here.
        monkeypatch.setattr(
            "pipeline.cross_project.profile_projection.project_cross_profile",
            lambda profile: object(),
        )
        _patch_input(monkeypatch, "")  # ABORTED
        code = cli.orcho.cmd_cross(_ns())
        assert code == 0
        assert self.calls == []

    def test_resume_runs_pipeline_without_profile(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, False)
        _patch_catalog(monkeypatch, _catalog())
        code = cli.orcho.cmd_cross(_ns(resume="latest"))
        assert code == 0
        assert len(self.calls) == 1
        assert self.calls[0].profile is None
