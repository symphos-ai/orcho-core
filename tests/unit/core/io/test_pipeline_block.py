"""Unit tests for :func:`core.io.pipeline_block.render_pipeline_block`.

The renderer is the static pipeline-progress block printed under the
run header. Tests pin the visible shape — glyphs, ordering, loop
grouping, and resume highlighting — without locking down ANSI codes
(those are asserted via stripped output to keep the tests robust to
palette tweaks).
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from core.io.ansi import C, get_color_enabled, set_color_enabled, strip_ansi
from core.io.pipeline_block import (
    render_pipeline_block,
    render_pipeline_sections,
)
from pipeline.runtime.profile import LoopStep, Profile
from pipeline.runtime.roles import FullCycleDepth, ProfileKind
from pipeline.runtime.steps import PhaseStep


@pytest.fixture(autouse=True)
def _restore_color_override():
    before = get_color_enabled()
    try:
        yield
    finally:
        set_color_enabled(before)


class _Stdout:
    def __init__(self, is_tty: bool) -> None:
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


def _strip(text: str) -> str:
    return strip_ansi(text)


def _lite_profile() -> Profile:
    return Profile(
        name="lite",
        kind=ProfileKind.FULL_CYCLE,
        variant=FullCycleDepth.LITE.value,
        steps=(
            PhaseStep(phase="plan"),
            PhaseStep(phase="validate_plan"),
            PhaseStep(phase="implement"),
            LoopStep(
                steps=(
                    PhaseStep(phase="review_changes"),
                    PhaseStep(phase="repair_changes"),
                ),
                until="review_changes.approved",
                max_rounds=2,
            ),
            PhaseStep(phase="final_acceptance"),
        ),
    )


def test_fresh_run_first_phase_is_current():
    block = _strip(render_pipeline_block(_lite_profile()))
    assert "Pipeline" in block
    # First phase carries the ▶ chevron, the rest carry · pending dots.
    assert "▶ plan" in block
    assert "· validate_plan" in block
    assert "· implement" in block
    assert "· final_acceptance" in block


def test_completed_phases_show_check_marks():
    block = _strip(render_pipeline_block(
        _lite_profile(),
        completed=("plan", "validate_plan"),
        current="implement",
    ))
    assert "✓ plan" in block
    assert "✓ validate_plan" in block
    assert "▶ implement" in block
    assert "· final_acceptance" in block
    # No leftover pending glyph on the completed phases.
    assert "· plan" not in block
    assert "· validate_plan" not in block


def test_loop_step_renders_with_prefix_glyph_and_parens():
    block = _strip(render_pipeline_block(_lite_profile()))
    # ⟳ sits *before* the parens as a group marker; inside the group
    # steps are joined by → because they execute sequentially within
    # one round.
    assert "⟳² (· review_changes → · repair_changes)" in block


def test_loop_inner_phase_can_be_current():
    block = _strip(render_pipeline_block(
        _lite_profile(),
        completed=("plan", "validate_plan", "implement"),
        current="review_changes",
    ))
    assert "⟳² (▶ review_changes → · repair_changes)" in block


def test_loop_chip_uses_superscript_for_small_max_rounds():
    profile = Profile(
        name="t", kind=ProfileKind.CUSTOM,
        steps=(LoopStep(
            steps=(PhaseStep(phase="a"), PhaseStep(phase="b")),
            until="a.ok", max_rounds=3,
        ),),
    )
    block = _strip(render_pipeline_block(profile))
    assert "⟳³" in block
    assert "⟳×" not in block


def test_loop_chip_falls_back_to_times_for_large_max_rounds():
    profile = Profile(
        name="t", kind=ProfileKind.CUSTOM,
        steps=(LoopStep(
            steps=(PhaseStep(phase="a"), PhaseStep(phase="b")),
            until="a.ok", max_rounds=12,
        ),),
    )
    block = _strip(render_pipeline_block(profile))
    assert "⟳×12" in block


def test_loop_chip_omitted_for_single_round():
    profile = Profile(
        name="t", kind=ProfileKind.CUSTOM,
        steps=(LoopStep(
            steps=(PhaseStep(phase="a"),),
            until="a.ok", max_rounds=1,
        ),),
    )
    block = _strip(render_pipeline_block(profile))
    # Plain ⟳ marker, no digit chip.
    assert "⟳ (" in block
    assert "⟳²" not in block
    assert "⟳×" not in block


def test_current_inferred_when_omitted():
    # When ``current`` is None, the first phase missing from ``completed``
    # is highlighted.
    block = _strip(render_pipeline_block(
        _lite_profile(),
        completed=("plan",),
    ))
    assert "✓ plan" in block
    assert "▶ validate_plan" in block


def test_duck_typed_profile_via_simple_namespace():
    # Cross-run uses a ``SimpleNamespace(steps=...)`` view rather than a
    # full ``Profile`` dataclass. The renderer must accept anything that
    # exposes ``.steps``.
    view = SimpleNamespace(
        steps=(PhaseStep(phase="alpha"), PhaseStep(phase="beta")),
    )
    block = _strip(render_pipeline_block(view))
    assert "▶ alpha" in block
    assert "· beta" in block


def test_no_trailing_newline():
    block = render_pipeline_block(_lite_profile())
    assert not block.endswith("\n"), (
        "render_pipeline_block must not append a trailing newline; "
        "callers wrap framing themselves"
    )


def test_arrow_separator_between_top_level_steps():
    block = _strip(render_pipeline_block(_lite_profile()))
    # Adjacent top-level phases are joined by ``→`` (with surrounding
    # whitespace). Don't assert exact column positions — the wrap path
    # can reflow the chain — but the arrow must be present.
    assert "→" in block


def test_color_false_returns_plain_text():
    block = render_pipeline_block(_lite_profile(), color=False)

    assert "\033[" not in block
    assert block == strip_ansi(block)


def test_color_true_applies_shared_ansi_palette():
    block = render_pipeline_block(_lite_profile(), color=True)

    assert C.CYAN in block
    assert C.YELLOW in block
    assert C.GREY in block
    assert strip_ansi(block).startswith("Pipeline\n  ▶ plan")


def test_auto_color_uses_stdout_tty_policy(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys, "stdout", _Stdout(is_tty=False))
    set_color_enabled(None)

    block = render_pipeline_block(_lite_profile())

    assert "\033[" not in block


def test_auto_color_honours_no_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(sys, "stdout", _Stdout(is_tty=True))
    set_color_enabled(None)

    block = render_pipeline_block(_lite_profile())

    assert "\033[" not in block


def test_auto_color_honours_process_override(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys, "stdout", _Stdout(is_tty=True))
    set_color_enabled(False)

    block = render_pipeline_block(_lite_profile())

    assert "\033[" not in block


def test_phase_runtimes_chip_for_known_phases():
    block = strip_ansi(render_pipeline_block(
        _lite_profile(),
        phase_runtimes={
            "plan": "claude",
            "validate_plan": "codex",
            "implement": "claude",
            "review_changes": "codex",
            "repair_changes": "claude",
            "final_acceptance": "codex",
        },
    ))
    # Capitalized runtime label sits right after each phase name.
    assert "▶ plan [Claude]" in block
    assert "· validate_plan [Codex]" in block
    assert "· implement [Claude]" in block
    assert "· final_acceptance [Codex]" in block


def test_phase_runtimes_chip_appears_inside_loop():
    block = strip_ansi(render_pipeline_block(
        _lite_profile(),
        completed=("plan", "validate_plan", "implement"),
        current="review_changes",
        phase_runtimes={"review_changes": "codex", "repair_changes": "claude"},
    ))
    assert "▶ review_changes [Codex]" in block
    assert "· repair_changes [Claude]" in block


def test_phase_runtimes_unknown_phase_renders_bare():
    # ``phase_runtimes`` supplied but ``final_acceptance`` is omitted —
    # that phase must render without a chip so the renderer doesn't
    # invent a label.
    block = strip_ansi(render_pipeline_block(
        _lite_profile(),
        phase_runtimes={"plan": "claude"},
    ))
    assert "▶ plan [Claude]" in block
    assert "[Claude]" not in block.replace("▶ plan [Claude]", "")
    assert "[Codex]" not in block


def test_phase_runtimes_none_keeps_renderer_unchanged():
    # Default path — no chips at all when ``phase_runtimes`` is omitted.
    block = strip_ansi(render_pipeline_block(_lite_profile()))
    assert "[" not in block.replace("Pipeline", "")


# ── render_pipeline_sections ─────────────────────────────────────────────────


def _cross_sections():
    """Global plan loop + per-project chain + terminal gates — the shape
    the cross-project header builds from a projected ``advanced`` profile.
    """
    return [
        ("Global", (
            LoopStep(
                steps=(
                    PhaseStep(phase="plan"),
                    PhaseStep(phase="validate_plan"),
                ),
                until="validate_plan.approved",
                max_rounds=2,
            ),
        )),
        ("Per project (×2)", (
            PhaseStep(phase="implement"),
            LoopStep(
                steps=(
                    PhaseStep(phase="review_changes"),
                    PhaseStep(phase="repair_changes"),
                ),
                until="review_changes.clean",
                max_rounds=2,
            ),
            PhaseStep(phase="final_acceptance"),
        )),
        ("Cross gates", (
            PhaseStep(phase="contract_check"),
            PhaseStep(phase="cross_final_acceptance"),
        )),
    ]


def test_sections_render_all_labels_and_phases():
    block = _strip(render_pipeline_sections(_cross_sections()))
    # One Pipeline header, then each section label, then its chain.
    assert block.count("Pipeline") == 1
    assert "Global" in block
    assert "Per project (×2)" in block
    assert "Cross gates" in block
    # Every section's phases are present — the per-project sub-pipeline
    # and terminal gates must not be dropped from the visualization.
    for phase in (
        "plan", "validate_plan", "implement", "review_changes",
        "repair_changes", "final_acceptance", "contract_check",
        "cross_final_acceptance",
    ):
        assert phase in block


def test_sections_current_marker_lands_on_first_phase_only():
    block = _strip(render_pipeline_sections(_cross_sections()))
    # ▶ marks the first phase of the first section; everything else is
    # pending so the operator sees where the run starts.
    assert "▶ plan" in block
    assert block.count("▶") == 1
    assert "· implement" in block
    assert "· contract_check" in block


def test_sections_skip_empty_sections():
    block = _strip(render_pipeline_sections([
        ("Global", (PhaseStep(phase="plan"),)),
        ("Per project", ()),
    ]))
    assert "Global" in block
    # An empty section emits neither its label nor a blank chain line.
    assert "Per project" not in block


def test_sections_runtime_chips_shared_across_sections():
    block = strip_ansi(render_pipeline_sections(
        _cross_sections(),
        phase_runtimes={
            "plan": "claude",
            "validate_plan": "codex",
            "implement": "claude",
            "final_acceptance": "codex",
        },
    ))
    assert "▶ plan [Claude]" in block
    assert "· validate_plan [Codex]" in block
    assert "· implement [Claude]" in block
    assert "· final_acceptance [Codex]" in block


def test_sections_no_trailing_newline():
    block = render_pipeline_sections(_cross_sections())
    assert not block.endswith("\n")


def test_sections_color_false_is_plain():
    block = render_pipeline_sections(_cross_sections(), color=False)
    assert "\033[" not in block
    assert block == strip_ansi(block)
