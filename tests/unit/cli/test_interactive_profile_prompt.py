"""Pin guards for the ``--profile`` keyboard picker (T1/T2).

:func:`cli._profile_prompt.prompt_for_profile_if_needed` shows a numbered
menu of available profiles when none was supplied on an interactive TTY,
and mutates ``args.profile`` in place from the operator's choice. It is a
silent no-op (no menu, no ``input()``) when the profile is already set,
on ``--resume`` / ``--from-run-plan``, under ``--no-interactive``, when
stdin is not a TTY, or when the profile catalog is empty / fails to load.

Tests cover:

* the silent no-op branches;
* number / exact-name selection;
* invalid-then-valid retry;
* empty line / EOF / Ctrl-C / retry exhaustion leaving ``profile=None``;
* empty-catalog and load-failure no-ops;
* the ``profile_filter`` (``orcho cross``) contract: eligibility is
  driven by cross-projection, not by ``cross_gates``;
* the ``cmd_cross`` predicate returning False on ``CrossProjectionError``.

The profile catalog is monkeypatched, so tests never touch the real
``CONFIG_DIR``.
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator

import pytest

from cli._profile_menu import _profile_summary
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
    """Minimal stand-in for a loaded ``Profile``.

    The picker reads ``.description`` (tagline + detail view), the semantic
    ``.default_mode`` and ``.worktree_isolation`` metadata (row subtitle), and
    ``.internal`` (picker filtering). Curated Common/Focused grouping keys off
    the *catalog name* (the dict key), not these attributes; a name outside the
    curated work kinds falls into the trailing ``Other`` fallback group.
    """

    def __init__(
        self,
        description: str = "",
        kind: str | None = None,
        *,
        default_mode: str | None = None,
        worktree_isolation: str | None = None,
        internal: bool = False,
    ) -> None:
        self.description = description
        self.kind = kind
        self.default_mode = default_mode
        self.worktree_isolation = worktree_isolation
        self.internal = internal


def _ns(**kwargs) -> argparse.Namespace:
    """Namespace with the fields the picker reads:
    ``profile`` / ``resume`` / ``from_run_plan`` / ``no_interactive``.
    """
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
    monkeypatch: pytest.MonkeyPatch,
    replies: str | list[str] | None,
) -> list[str]:
    """Patch ``builtins.input``.

    * ``replies is None`` → every call raises ``EOFError``.
    * a ``str`` → every call returns that string.
    * a ``list`` → calls pop successive replies; an exhausted queue
      raises ``EOFError`` (models the operator giving up).

    Captures the prompt argument of every call into the returned list.
    """
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
    """Monkeypatch the lazily-imported catalog loader.

    The picker does ``from pipeline.profiles.loader import
    load_profiles_v2_with_plugins`` inside the function body, so patch the
    attribute on that module. Pass an ``Exception`` instance to simulate a
    load failure.
    """
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


# ── no-op branches ────────────────────────────────────────────────────


class TestSilentNoOps:
    def test_existing_profile_short_circuits(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _catalog())
        captured = _patch_input(monkeypatch, "ignored")
        args = _ns(profile="advanced")
        result = prompt_for_profile_if_needed(args)
        assert result is ProfilePromptResult.SKIPPED
        assert args.profile == "advanced"
        assert captured == []

    def test_resume_short_circuits(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _catalog())
        captured = _patch_input(monkeypatch, "ignored")
        args = _ns(resume="latest")
        result = prompt_for_profile_if_needed(args)
        assert result is ProfilePromptResult.SKIPPED
        assert args.profile is None
        assert captured == []

    def test_from_run_plan_short_circuits(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _catalog())
        captured = _patch_input(monkeypatch, "ignored")
        args = _ns(from_run_plan="run-plan.json")
        result = prompt_for_profile_if_needed(args)
        assert result is ProfilePromptResult.SKIPPED
        assert args.profile is None
        assert captured == []

    def test_no_interactive_short_circuits(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _catalog())
        captured = _patch_input(monkeypatch, "ignored")
        args = _ns(no_interactive=True)
        result = prompt_for_profile_if_needed(args)
        assert result is ProfilePromptResult.SKIPPED
        assert args.profile is None
        assert captured == []

    def test_non_tty_stdin_short_circuits(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, False)
        _patch_catalog(monkeypatch, _catalog())
        captured = _patch_input(monkeypatch, "ignored")
        args = _ns()
        result = prompt_for_profile_if_needed(args)
        assert result is ProfilePromptResult.SKIPPED
        assert args.profile is None
        assert captured == []


# ── selection semantics ───────────────────────────────────────────────


class TestSelectionSemantics:
    def test_valid_number_selects_name(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _catalog())
        _patch_input(monkeypatch, "1")  # sorted order → advanced
        args = _ns()
        result = prompt_for_profile_if_needed(args)
        assert result is ProfilePromptResult.SELECTED
        assert args.profile == "advanced"

    def test_valid_name_selects_profile(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _catalog())
        _patch_input(monkeypatch, "review")
        args = _ns()
        result = prompt_for_profile_if_needed(args)
        assert result is ProfilePromptResult.SELECTED
        assert args.profile == "review"

    def test_invalid_then_valid_retries(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _catalog())
        captured = _patch_input(monkeypatch, ["99", "nope", "lite"])
        args = _ns()
        result = prompt_for_profile_if_needed(args)
        assert result is ProfilePromptResult.SELECTED
        assert args.profile == "lite"
        assert len(captured) == 3  # two rejects, then the valid pick

    def test_empty_input_leaves_profile_none(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _catalog())
        _patch_input(monkeypatch, "")
        args = _ns()
        result = prompt_for_profile_if_needed(args)
        assert result is ProfilePromptResult.ABORTED
        assert args.profile is None

    def test_eof_leaves_profile_none(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _catalog())
        _patch_input(monkeypatch, None)  # raises EOFError
        args = _ns()
        result = prompt_for_profile_if_needed(args)
        assert result is ProfilePromptResult.ABORTED
        assert args.profile is None
        assert capsys.readouterr().out.endswith("\n")

    def test_keyboard_interrupt_leaves_profile_none(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _catalog())

        def _fake_input(prompt: str = "") -> str:
            raise KeyboardInterrupt

        monkeypatch.setattr("builtins.input", _fake_input)
        args = _ns()
        result = prompt_for_profile_if_needed(args)
        assert result is ProfilePromptResult.ABORTED
        assert args.profile is None

    def test_retry_exhaustion_leaves_profile_none(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _catalog())
        captured = _patch_input(monkeypatch, "still-not-a-profile")
        args = _ns()
        result = prompt_for_profile_if_needed(args)
        assert result is ProfilePromptResult.ABORTED
        assert args.profile is None
        assert len(captured) == 3  # capped at three attempts


# ── catalog edge cases ────────────────────────────────────────────────


class TestCatalogEdgeCases:
    def test_empty_catalog_is_silent_noop(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, {})
        captured = _patch_input(monkeypatch, "1")
        args = _ns()
        result = prompt_for_profile_if_needed(args)
        assert result is ProfilePromptResult.SKIPPED
        assert args.profile is None
        assert captured == []

    def test_load_failure_is_silent_noop(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, RuntimeError("boom"))
        captured = _patch_input(monkeypatch, "1")
        args = _ns()
        result = prompt_for_profile_if_needed(args)
        assert result is ProfilePromptResult.SKIPPED
        assert args.profile is None
        assert captured == []


# ── cross profile_filter contract ─────────────────────────────────────


class TestCrossProfileFilter:
    def test_filter_excludes_ineligible_profile(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _catalog())
        # Only "review" is cross-eligible → menu shows it as #1.
        _patch_input(monkeypatch, "1")
        args = _ns()
        result = prompt_for_profile_if_needed(
            args, profile_filter=lambda p: p.description == "Review-only profile."
        )
        assert result is ProfilePromptResult.SELECTED
        assert args.profile == "review"
        out = capsys.readouterr().out
        assert "review" in out
        assert "advanced" not in out
        assert "lite" not in out

    def test_eligible_without_cross_gates_is_offered(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A profile with a valid per-step cross policy but no cross_gates
        # is eligible: eligibility is decided by the projection-backed
        # predicate (modeled here as True), not by cross_gates presence.
        catalog = {"with_policy": _FakeProfile("has per-step cross policy")}
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, catalog)
        _patch_input(monkeypatch, "1")
        args = _ns()
        result = prompt_for_profile_if_needed(args, profile_filter=lambda p: True)
        assert result is ProfilePromptResult.SELECTED
        assert args.profile == "with_policy"

    def test_cross_gates_without_policy_is_hidden(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A profile that carries cross_gates but lacks per-step cross
        # policy is ineligible (project_cross_profile would raise), so the
        # projection-backed predicate is False and the menu stays empty →
        # silent no-op, never picking the gated-but-unprojectable profile.
        catalog = {"gates_only": _FakeProfile("cross_gates but no policy")}
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, catalog)
        captured = _patch_input(monkeypatch, "1")
        args = _ns()
        result = prompt_for_profile_if_needed(args, profile_filter=lambda p: False)
        assert result is ProfilePromptResult.SKIPPED
        assert args.profile is None
        assert captured == []


# ── cmd_cross eligibility predicate ───────────────────────────────────


class TestCmdCrossPredicate:
    """The predicate built inside ``cli.orcho.cmd_cross`` must treat a
    profile as eligible iff ``project_cross_profile`` does not raise
    ``CrossProjectionError`` — never by inspecting ``cross_gates``.
    """

    def test_predicate_reflects_cross_projection(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import cli.orcho
        from pipeline.cross_project.profile_projection import (
            CrossProjectionError,
        )

        captured_filter: dict = {}

        def _capture(args, *, profile_filter=None):
            captured_filter["fn"] = profile_filter
            return None

        monkeypatch.setattr(cli.orcho, "require_profile_or_exit", _capture)
        monkeypatch.setattr(cli.orcho, "run_cross_from_args", lambda args: 0)

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
        assert predicate(_FakeProfile("ok")) is True
        assert predicate(ineligible) is False


# ── _profile_summary tagline helper ───────────────────────────────────


class TestProfileSummary:
    def test_empty_description_is_empty(self) -> None:
        assert _profile_summary("") == ""
        assert _profile_summary("   ") == ""

    def test_takes_first_sentence(self) -> None:
        out = _profile_summary("First sentence. Second sentence is dropped.")
        assert out == "First sentence"

    def test_clause_dash_boundary(self) -> None:
        out = _profile_summary("Quick dev cycle — skips the review loop entirely.")
        assert out == "Quick dev cycle"

    def test_long_description_is_shortened_with_ellipsis(self) -> None:
        # A 600-char single-sentence paragraph (no ". " boundary) must be
        # word-truncated to a compact one-liner ending in an ellipsis.
        long_desc = "word " * 120  # 600 chars, no sentence break
        out = _profile_summary(long_desc)
        assert out.endswith("…")
        assert len(out) <= 100
        # Truncation lands on a word boundary: no dangling partial word.
        assert set(out.rstrip("…").strip().split()) == {"word"}


# ── new UX: framing, [default] chip, kind grouping ────────────────────


def _visible(capsys: pytest.CaptureFixture) -> str:
    return strip_ansi(capsys.readouterr().out)


def _semantic_catalog() -> dict:
    """A catalog keyed by the semantic work kinds (a subset is enough to
    exercise curated Common/Focused grouping + the feature [default] chip +
    the {mode · isolation · tagline} subtitle). Internal task/correction are
    included to assert the picker hides them."""
    return {
        "feature": _FakeProfile(
            "Production-grade dev cycle for a feature.",
            default_mode="fast", worktree_isolation=None,
        ),
        "small_task": _FakeProfile(
            "Quick dev cycle for a small change.",
            default_mode="fast", worktree_isolation="off",
        ),
        "complex_feature": _FakeProfile(
            "More comprehensive development cycle.",
            default_mode="pro", worktree_isolation=None,
        ),
        "planning": _FakeProfile(
            "Produce a plan artifact and stop.",
            default_mode="pro", worktree_isolation="off",
        ),
        "delivery_audit": _FakeProfile(
            "Audit uncommitted changes for delivery.",
            default_mode="pro", worktree_isolation="off",
        ),
        "code_review": _FakeProfile(
            "Review uncommitted changes.",
            default_mode="pro", worktree_isolation="off",
        ),
        "research": _FakeProfile(
            "Investigate and produce a plan.",
            default_mode="fast", worktree_isolation="off",
        ),
        "refactor": _FakeProfile(
            "Production-grade dev cycle for a refactor.",
            default_mode="pro", worktree_isolation=None,
        ),
        "migration": _FakeProfile(
            "More comprehensive cycle for a migration.",
            default_mode="pro", worktree_isolation=None,
        ),
        "task": _FakeProfile("Internal build-only profile.", internal=True),
        "correction": _FakeProfile("Internal follow-up.", internal=True),
    }


class TestMenuRendering:
    def test_default_chip_present_on_feature(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _semantic_catalog())
        _patch_input(monkeypatch, "")  # abort after the menu renders
        args = _ns()
        prompt_for_profile_if_needed(args)
        out = _visible(capsys)
        assert "[default]" in out
        # feature and its chip share a line.
        chip_line = next(ln for ln in out.splitlines() if "[default]" in ln)
        assert "feature" in chip_line

    def test_default_chip_absent_when_feature_filtered(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _semantic_catalog())
        _patch_input(monkeypatch, "")
        args = _ns()
        # Hide feature → no [default] chip, and the name must not leak as a
        # numbered row (it stays out of the menu entirely).
        prompt_for_profile_if_needed(
            args,
            profile_filter=lambda p: not p.description.startswith(
                "Production-grade dev cycle for a feature"
            ),
        )
        out = _visible(capsys)
        assert "[default]" not in out
        # No "N) feature" row rendered.
        assert not any(
            ln.strip().split(") ", 1)[-1].startswith("feature")
            for ln in out.splitlines() if ")" in ln
        )

    def test_title_is_select_work_kind(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _semantic_catalog())
        _patch_input(monkeypatch, "")
        args = _ns()
        prompt_for_profile_if_needed(args)
        out = _visible(capsys)
        assert "Select work kind" in out

    def test_internal_profiles_hidden(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _semantic_catalog())
        _patch_input(monkeypatch, "")
        args = _ns()
        prompt_for_profile_if_needed(args)
        out = _visible(capsys)
        # task / correction are internal → never offered.
        assert not any(
            ln.strip().split(") ", 1)[-1].startswith(("task", "correction"))
            for ln in out.splitlines() if ")" in ln
        )

    def test_subtitle_carries_mode_and_isolation(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _semantic_catalog())
        _patch_input(monkeypatch, "")
        args = _ns()
        prompt_for_profile_if_needed(args)
        out = _visible(capsys)
        # feature: fast default mode, global per_run default → "isolated".
        assert "fast · isolated · Production-grade dev cycle for a feature" in out
        # small_task: worktree off → "direct checkout".
        assert "fast · direct checkout · Quick dev cycle for a small change" in out


class TestCuratedGrouping:
    def test_common_focused_headers_and_feature_first(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _semantic_catalog())
        # feature is row #1 (first in Common).
        _patch_input(monkeypatch, "1")
        args = _ns()
        result = prompt_for_profile_if_needed(args)
        assert result is ProfilePromptResult.SELECTED
        assert args.profile == "feature"
        out = _visible(capsys)
        assert "Common" in out
        assert "Focused" in out
        # Common precedes Focused.
        assert out.index("Common") < out.index("Focused")
        # The retired flat-kind headers are gone.
        assert "Full cycle" not in out
        assert "Scoped" not in out

    def test_flat_headerless_for_plugin_only_catalog(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        # A catalog with no curated work-kind names degrades to a flat,
        # headerless, sorted menu.
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _catalog())  # advanced/lite/review
        _patch_input(monkeypatch, "1")
        args = _ns()
        result = prompt_for_profile_if_needed(args)
        assert result is ProfilePromptResult.SELECTED
        assert args.profile == "advanced"  # flat sorted order
        out = _visible(capsys)
        assert "Common" not in out
        assert "Focused" not in out


# ── details-on-demand: '?N' does not spend the invalid budget ─────────


class TestAutoDetectEntry:
    """``include_auto_detect`` adds a first-class ``auto-detect`` selector
    above the manual Common/Focused profiles without hiding any of them
    (Stage C / T2). Picking it (by number or name) sets
    ``args.profile == "auto-detect"``; the manual profiles still render.
    """

    def test_auto_detect_rendered_first_and_manual_kept(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _semantic_catalog())
        _patch_input(monkeypatch, "")  # abort after the menu renders
        args = _ns()
        prompt_for_profile_if_needed(args, include_auto_detect=True)
        out = _visible(capsys)
        # auto-detect is the very first numbered row.
        numbered = [ln for ln in out.splitlines() if ") " in ln]
        assert numbered, "expected numbered rows"
        assert numbered[0].strip().split(") ", 1)[1].startswith("auto-detect")
        assert "recommend work kind & mode" in out
        # Manual groups and profiles are still present, not hidden.
        assert "Common" in out
        assert "Focused" in out
        assert any(
            ln.strip().split(") ", 1)[-1].startswith("feature")
            for ln in numbered
        )

    def test_number_one_selects_auto_detect(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _semantic_catalog())
        _patch_input(monkeypatch, "1")  # auto-detect is row #1
        args = _ns()
        result = prompt_for_profile_if_needed(args, include_auto_detect=True)
        assert result is ProfilePromptResult.SELECTED
        assert args.profile == "auto-detect"

    def test_name_selects_auto_detect(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _semantic_catalog())
        _patch_input(monkeypatch, "auto-detect")
        args = _ns()
        result = prompt_for_profile_if_needed(args, include_auto_detect=True)
        assert result is ProfilePromptResult.SELECTED
        assert args.profile == "auto-detect"

    def test_feature_still_selectable_after_shift(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # auto-detect pushes feature to row #2; feature must still be pickable.
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _semantic_catalog())
        _patch_input(monkeypatch, "2")
        args = _ns()
        result = prompt_for_profile_if_needed(args, include_auto_detect=True)
        assert result is ProfilePromptResult.SELECTED
        assert args.profile == "feature"

    def test_legacy_ids_absent_from_public_picker(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        # The legacy flat ids must never resurface as picker rows; internal
        # task/correction stay hidden too.
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _semantic_catalog())
        _patch_input(monkeypatch, "")
        args = _ns()
        prompt_for_profile_if_needed(args, include_auto_detect=True)
        out = _visible(capsys)
        rows = [
            ln.strip().split(") ", 1)[-1]
            for ln in out.splitlines() if ") " in ln
        ]
        for legacy in ("advanced", "lite", "task", "review", "correction"):
            assert not any(r.startswith(legacy) for r in rows), legacy

    def test_auto_detect_absent_without_flag(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        # Default (cross / non-run callers): no synthetic auto-detect row.
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _semantic_catalog())
        _patch_input(monkeypatch, "1")
        args = _ns()
        result = prompt_for_profile_if_needed(args)
        assert result is ProfilePromptResult.SELECTED
        assert args.profile == "feature"  # row #1 is feature, not auto-detect
        assert "auto-detect" not in _visible(capsys)

    def test_question_on_auto_detect_shows_detail_no_crash(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        # ``?1`` for the synthetic row prints its blurb (not a KeyError on the
        # catalog) and does not spend the invalid budget; a later pick works.
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _semantic_catalog())
        _patch_input(monkeypatch, ["?1", "auto-detect"])
        args = _ns()
        result = prompt_for_profile_if_needed(args, include_auto_detect=True)
        assert result is ProfilePromptResult.SELECTED
        assert args.profile == "auto-detect"
        assert "recommend" in _visible(capsys).lower()


class TestAutoDetectArgParsing:
    def test_run_profile_auto_detect_parses(self) -> None:
        import cli.orcho

        parser = cli.orcho.build_parser()
        ns = parser.parse_args(
            ["run", "--task", "x", "--profile", "auto-detect"]
        )
        assert ns.profile == "auto-detect"


class TestDetailsOnDemand:
    def test_question_prints_details_then_selects(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _catalog())  # sorted: advanced, lite, review
        _patch_input(monkeypatch, ["?1", "1"])
        args = _ns()
        result = prompt_for_profile_if_needed(args)
        assert result is ProfilePromptResult.SELECTED
        assert args.profile == "advanced"
        out = _visible(capsys)
        # The full description of profile #1 was printed on the '?1' query.
        assert "Full plan/implement/review profile." in out

    def test_repeated_question_does_not_exhaust_budget(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _catalog())
        # Four detail queries (more than the 3-invalid budget) then a valid
        # pick: details must not count as invalid attempts.
        captured = _patch_input(monkeypatch, ["?1", "?1", "?1", "?1", "2"])
        args = _ns()
        result = prompt_for_profile_if_needed(args)
        assert result is ProfilePromptResult.SELECTED
        assert args.profile == "lite"  # #2 in sorted order
        assert len(captured) == 5  # all five reads consumed, none aborted

    def test_unknown_question_token_is_not_invalid(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _force_stdin_tty(monkeypatch, True)
        _patch_catalog(monkeypatch, _catalog())
        # An unrecognised '?token' prints a hint but does not spend budget;
        # a later valid pick still succeeds.
        _patch_input(monkeypatch, ["?nope", "review"])
        args = _ns()
        result = prompt_for_profile_if_needed(args)
        assert result is ProfilePromptResult.SELECTED
        assert args.profile == "review"
        assert "No such profile" in _visible(capsys)
