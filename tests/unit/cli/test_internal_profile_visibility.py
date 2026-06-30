"""Internal-profile visibility (ADR 0085, T2).

Two surfaces:

* the interactive fresh-run picker
  (:func:`cli._profile_prompt.prompt_for_profile_if_needed`) must never
  offer an ``internal=True`` profile — not in the menu, not by number, not
  by name, not in the ``?N`` detail view;
* the ``orcho profiles`` catalog
  (:func:`cli.orcho._format_profile_catalog`) keeps internal profiles
  visible but tags them with an ``[internal]`` chip.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator

import pytest

from cli._profile_prompt import (
    ProfilePromptResult,
    prompt_for_profile_if_needed,
)
from core.io.ansi import get_color_enabled, set_color_enabled, strip_ansi


@pytest.fixture(autouse=True)
def _restore_color_override() -> Iterator[None]:
    before = get_color_enabled()
    try:
        yield
    finally:
        set_color_enabled(before)


class _FakeProfile:
    def __init__(
        self,
        description: str = "",
        *,
        kind: str | None = None,
        internal: bool = False,
    ) -> None:
        self.description = description
        self.kind = kind
        self.internal = internal


def _ns(**kwargs) -> argparse.Namespace:
    defaults = {
        "profile": None,
        "resume": None,
        "from_run_plan": None,
        "no_interactive": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _force_stdin_tty(monkeypatch: pytest.MonkeyPatch, is_tty: bool) -> None:
    class _Stdin:
        def isatty(self) -> bool:
            return is_tty

    monkeypatch.setattr(sys, "stdin", _Stdin())


def _patch_input(
    monkeypatch: pytest.MonkeyPatch, replies: list[str],
) -> list[str]:
    captured: list[str] = []
    queue = list(replies)

    def _fake_input(prompt: str = "") -> str:
        captured.append(prompt)
        if not queue:
            raise EOFError
        return queue.pop(0)

    monkeypatch.setattr("builtins.input", _fake_input)
    return captured


def _patch_catalog(monkeypatch: pytest.MonkeyPatch, profiles: dict) -> None:
    monkeypatch.setattr(
        "pipeline.profiles.loader.load_profiles_v2_with_plugins",
        lambda _path: profiles,
    )


def _catalog_with_internal() -> dict:
    # sorted() over keys → advanced, correction, review. ``correction`` is
    # internal and must be filtered out of the menu entirely.
    return {
        "advanced": _FakeProfile("Full dev cycle profile."),
        "correction": _FakeProfile(
            "Internal correction follow-up profile.", internal=True,
        ),
        "review": _FakeProfile("Review-only profile."),
    }


# ── interactive picker hides internal profiles ─────────────────────────


class TestPickerHidesInternal:
    def test_menu_omits_internal_profile(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _catalog_with_internal())
        # Abort immediately so we only inspect the rendered menu.
        _patch_input(monkeypatch, [""])

        args = _ns()
        result = prompt_for_profile_if_needed(args)

        out = strip_ansi(capsys.readouterr().out)
        assert result is ProfilePromptResult.ABORTED
        assert "advanced" in out
        assert "review" in out
        assert "correction" not in out

    def test_internal_name_is_not_accepted(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _catalog_with_internal())
        # Typing the internal name is treated as invalid input; the queue
        # then exhausts → ABORTED with profile unset.
        _patch_input(monkeypatch, ["correction"])

        args = _ns()
        result = prompt_for_profile_if_needed(args)

        assert result is ProfilePromptResult.ABORTED
        assert args.profile is None

    def test_only_visible_profiles_are_numbered(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _catalog_with_internal())
        # Two visible profiles (advanced=1, review=2). "2" selects review;
        # the hidden internal profile never takes a slot.
        _patch_input(monkeypatch, ["2"])

        args = _ns()
        result = prompt_for_profile_if_needed(args)

        assert result is ProfilePromptResult.SELECTED
        assert args.profile == "review"

    def test_detail_view_cannot_reveal_internal(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _catalog_with_internal())
        # ``?correction`` is an unrecognised token (it was filtered from the
        # ordered menu) → "No such profile" hint, never the description.
        _patch_input(monkeypatch, ["?correction", ""])

        args = _ns()
        prompt_for_profile_if_needed(args)

        out = strip_ansi(capsys.readouterr().out)
        assert "No such profile" in out
        assert "Internal correction follow-up profile." not in out


# ── catalog keeps internal profiles visible with a chip ────────────────


class TestCatalogShowsInternalChip:
    def test_real_catalog_tags_correction_internal(self) -> None:
        from cli.orcho import _format_profile_catalog, _load_profile_catalog

        text = _format_profile_catalog(_load_profile_catalog())
        lines = text.splitlines()

        correction_line = next(
            ln for ln in lines if ln.strip().startswith("correction")
        )
        assert "[internal]" in correction_line

        # Operator-facing profiles do not carry the chip.
        feature_line = next(
            ln for ln in lines if ln.strip().startswith("feature")
        )
        assert "[internal]" not in feature_line

    def test_fake_catalog_chip_only_on_internal(self) -> None:
        from cli.orcho import _format_profile_catalog

        text = _format_profile_catalog(_catalog_with_internal())
        lines = {
            ln.strip().split()[0]: ln
            for ln in text.splitlines()
            if ln.strip() and not ln.startswith("Profiles")
            and not ln.startswith("    ")
        }
        assert "[internal]" in lines["correction"]
        assert "[internal]" not in lines["advanced"]
        assert "[internal]" not in lines["review"]
