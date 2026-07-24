"""Unit tests for :mod:`core.io.summary_lines`.

The module is the single presenter for ``--output summary`` lines. Tests
pin the visible shape of each formatter — its glyph, field order, and
separators — on the stripped output (so the palette can shift without
churning the tests), plus the two contracts that matter across the whole
grammar: free-text tails clip at 100 columns with ``…`` while structured
tokens (ids, enums, phase/gate names) never clip, and colour flows only
through :func:`core.io.ansi.paint`.
"""
from __future__ import annotations

import pytest

from core.io import summary_lines as sl
from core.io.ansi import (
    C,
    get_color_enabled,
    set_color_enabled,
    strip_ansi,
)


@pytest.fixture(autouse=True)
def _restore_color_override():
    before = get_color_enabled()
    try:
        yield
    finally:
        set_color_enabled(before)


def _strip(text: str) -> str:
    return strip_ansi(text)


# ── import purity ────────────────────────────────────────────────────────


def test_module_imports_without_side_effects(capsys):
    # Re-importing must not print or otherwise touch process state; the
    # formatters are pure. Reading captured output proves nothing leaked
    # to stdout/stderr during import + a formatter call.
    import importlib

    importlib.reload(sl)
    line = sl.phase_start("plan", color=False)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert line == "▶ plan"


# ── phase lifecycle shapes ───────────────────────────────────────────────


def test_phase_start_shape():
    assert _strip(sl.phase_start("implement", color=False)) == "▶ implement"


def test_phase_end_ok_and_fail_glyphs():
    assert _strip(sl.phase_end("plan", True, color=False)) == "✓ plan"
    assert _strip(sl.phase_end("plan", False, color=False)) == "✗ plan"


def test_phase_end_with_detail_uses_separator():
    line = _strip(sl.phase_end("review_changes", True, "2 files touched", color=False))
    assert line == "✓ review_changes · 2 files touched"


def test_plan_contract_shape():
    line = _strip(sl.plan_contract(["T1", "T2", "T3"], 5, 3, color=False))
    assert line == "✓ plan · contract: 3 tasks (T1, T2, T3) · acceptance 5 · risks 3"


def test_plan_contract_without_task_ids_drops_paren_tail():
    line = _strip(sl.plan_contract([], 2, 1, color=False))
    assert line == "✓ plan · contract: 0 tasks · acceptance 2 · risks 1"


# ── verdicts ─────────────────────────────────────────────────────────────


def test_verdict_line_approved():
    line = _strip(sl.verdict_line("validate_plan", "APPROVED", color=False))
    assert line == "✓ validate_plan · APPROVED"


def test_verdict_line_rejected_with_headline():
    line = _strip(
        sl.verdict_line("validate_plan", "REJECTED", headline="F1 missing acceptance", color=False)
    )
    assert line == "✗ validate_plan · REJECTED · F1 missing acceptance"


def test_verdict_line_round_renders_as_R_token():
    line = _strip(sl.verdict_line("review_changes", "REJECTED", round=2, color=False))
    assert line == "✗ review_changes · REJECTED · R2"


def test_verdict_line_unknown_enum_uses_warn_glyph():
    line = _strip(sl.verdict_line("final_acceptance", "PENDING", color=False))
    assert line == "⚠ final_acceptance · PENDING"


# ── subtasks ─────────────────────────────────────────────────────────────


def test_subtask_start_shape():
    line = _strip(sl.subtask_start("T1", "introduce presenter", color=False))
    assert line == "▶ T1 · introduce presenter"


def test_subtask_done_shape():
    line = _strip(sl.subtask_done("T1", 4, 4, "presenter landed", color=False))
    assert line == "✓ T1 · done · 4/4 criteria · presenter landed"


def test_subtask_done_without_summary():
    assert _strip(sl.subtask_done("T2", 3, 5, color=False)) == "✓ T2 · done · 3/5 criteria"


def test_subtask_incomplete_shape():
    line = _strip(sl.subtask_incomplete("T7", "tests still red", color=False))
    assert line == "⚠ T7 · tests still red"


def test_autofix_line_shape():
    line = _strip(sl.autofix_line("auto_repair", ["T2", "T5"], 3, "halt", color=False))
    assert line == (
        "┌ attestation auto-fix · auto_repair · repair T2,T5 · budget 3 · on_exhausted halt"
    )


def test_autofix_line_retry_mode_and_single_id():
    line = _strip(sl.autofix_line("retry_feedback", ["T1"], 1, "waive", color=False))
    assert line == (
        "┌ attestation auto-fix · retry_feedback · repair T1 · budget 1 · on_exhausted waive"
    )


def test_implement_done_shape():
    line = _strip(sl.implement_done(3, 3, 12, color=False))
    assert line == "✓ implement · 3/3 subtasks · 12 files changed"


# ── gates / handoff / resume / delivery ──────────────────────────────────


def test_delivery_line_keeps_checkout_commit_branch_and_pr() -> None:
    assert _strip(
        sl.delivery_line(
            "abcdef123456",
            "orcho/deliver/r1-feature",
            pr_url="https://example.test/pr/7",
            color=False,
        )
    ) == (
        "✓ delivery · committed abcdef123456 · branch orcho/deliver/r1-feature "
        "· PR https://example.test/pr/7"
    )


def test_gates_line_ok_uses_check_glyph_and_receipts_dir():
    ok = _strip(sl.gates_line(
        "after_phase(implement)",
        "broad-non-e2e PASS · run-state-unit PASS",
        "runs/r1/verification_command_receipts",
        True,
        color=False,
    ))
    assert ok == (
        "✓ gates after_phase(implement): "
        "broad-non-e2e PASS · run-state-unit PASS "
        "· receipts → runs/r1/verification_command_receipts"
    )


def test_gates_line_fail_uses_cross_glyph_and_specific_receipt():
    fail = _strip(sl.gates_line(
        "after_phase(implement)",
        "broad-non-e2e FAIL",
        "runs/r1/verification_command_receipts/broad-non-e2e.json",
        False,
        color=False,
    ))
    assert fail == (
        "✗ gates after_phase(implement): broad-non-e2e FAIL "
        "· receipt → runs/r1/verification_command_receipts/broad-non-e2e.json"
    )


def test_gates_line_without_receipts_path_omits_reference():
    line = _strip(sl.gates_line("after_phase(plan)", "lint PASS", "", True, color=False))
    assert line == "✓ gates after_phase(plan): lint PASS"


def test_handoff_line_shape():
    line = _strip(sl.handoff_line("h-42", "review_rejected", "REJECTED", color=False))
    assert line == "═ handoff h-42 · review_rejected · verdict REJECTED"


def test_handoff_action_line_with_feedback_and_note():
    line = _strip(sl.handoff_action_line(
        "retry_feedback", "tighten scope",
        note="run parks awaiting_phase_handoff", color=False,
    ))
    assert line == (
        'action: retry_feedback · feedback: "tighten scope" '
        "· run parks awaiting_phase_handoff"
    )


def test_handoff_action_line_action_only():
    assert _strip(sl.handoff_action_line("continue", color=False)) == "action: continue"


def test_resume_line_shape_with_and_without_replay():
    bare = _strip(sl.resume_line(4, color=False))
    assert bare == "↺ resume from checkpoint · 4 phases completed"
    full = _strip(sl.resume_line(
        4,
        decision_action="retry_feedback",
        decision_feedback="tighten the null check",
        decision_result="approved",
        color=False,
    ))
    assert full == (
        "↺ resume from checkpoint · 4 phases completed · "
        'decision replay: retry_feedback "tighten the null check" → approved'
    )


def test_delivery_line_with_and_without_branch():
    assert (
        _strip(sl.delivery_line("abc1234", color=False))
        == "✓ delivery · committed abc1234"
    )
    line = _strip(sl.delivery_line("abc1234", "feature/x", color=False))
    assert line == "✓ delivery · committed abc1234 · branch feature/x"


def test_delivery_line_published_branch_with_pr():
    line = _strip(sl.delivery_line(
        "", "orcho/deliver/r1-x", pr_url="https://example/pr/1", color=False,
    ))
    assert line == (
        "✓ delivery · PR https://example/pr/1 · branch orcho/deliver/r1-x"
    )
    assert "committed" not in line


def test_delivery_line_published_branch_without_pr():
    line = _strip(sl.delivery_line("", "orcho/deliver/r1-x", color=False))
    assert line == "✓ delivery · branch orcho/deliver/r1-x"
    assert "committed" not in line


def test_delivery_line_degraded_publish_shows_ready_branch_and_reason():
    line = _strip(sl.delivery_line(
        "abcdef1",
        "orcho/deliver/r1-x",
        publish_gate="always",
        delivery_warnings=("delivery publish provider is unavailable",),
        delivery_notices=(
            "delivery branch orcho/deliver/r1-x is ready; open a pull request",
        ),
        color=False,
    ))
    assert line == (
        "⚠ delivery · branch orcho/deliver/r1-x ready · "
        "reason: delivery publish provider is unavailable"
    )


def test_delivery_line_off_and_auto_local_paths_keep_existing_strings():
    assert _strip(sl.delivery_line(
        "abcdef1",
        "orcho/deliver/r1-x",
        publish_gate="off",
        delivery_notices=(
            "delivery branch orcho/deliver/r1-x is ready; open a pull request",
        ),
        color=False,
    )) == "✓ delivery · committed abcdef1 · branch orcho/deliver/r1-x"
    assert _strip(sl.delivery_line(
        "abcdef1", "orcho/deliver/r1-x", publish_gate="auto", color=False,
    )) == "✓ delivery · committed abcdef1 · branch orcho/deliver/r1-x"


# ── truncation contract ──────────────────────────────────────────────────


def test_truncate_tail_leaves_short_text_untouched():
    text = "x" * 100
    assert sl._truncate_tail(text) == text


def test_truncate_tail_clips_long_text_to_100_visible_columns():
    text = "y" * 250
    out = sl._truncate_tail(text)
    assert out.endswith("…")
    assert len(out) == 100
    assert out == "y" * 99 + "…"


def test_truncate_tail_collapses_internal_whitespace():
    assert sl._truncate_tail("a\n  b\tc") == "a b c"


def test_truncate_tail_measures_colored_text_by_visible_width():
    # A coloured free-text tail whose VISIBLE width exceeds the budget must
    # clip on rendered length, not byte length: the ANSI escapes must not eat
    # into the 100-column budget, so exactly 99 visible chars + ``…`` survive.
    red, reset = C.RED, C.RESET
    text = f"{red}{'z' * 250}{reset}"
    out = sl._truncate_tail(text)
    assert out.endswith("…")
    assert out == "z" * 99 + "…"


def test_truncate_tail_strips_ansi_so_a_clipped_tail_cannot_color_leak():
    # A coloured tail clipped mid-span must NOT leave an open SGR code after
    # ``…`` — that would tint the following separators / lines. Stripping the
    # tail up front guarantees no escape (opening OR reset) survives, so there
    # is nothing to leak. Regression guard for the reset-loss finding.
    red, reset = C.RED, C.RESET
    out = sl._truncate_tail(f"{red}{'z' * 250}{reset}")
    assert "\x1b[" not in out          # no CSI escape survives at all
    assert out == strip_ansi(out)      # plain by construction → cannot leak


def test_truncate_tail_strips_ansi_from_a_short_colored_tail_too():
    # Even when no clipping is needed, a coloured tail is normalised to plain
    # text so the single-line grammar never carries stray escapes.
    red, reset = C.RED, C.RESET
    out = sl._truncate_tail(f"{red}hello{reset}")
    assert out == "hello"


def test_free_text_tail_over_100_columns_is_truncated_in_a_line():
    goal = "g" * 150
    line = _strip(sl.subtask_start("T1", goal, color=False))
    # Prefix + separator are intact; only the goal tail carries the ellipsis.
    assert line.startswith("▶ T1 · ")
    assert line.endswith("…")
    tail = line.split(" · ", 1)[1]
    assert len(tail) == 100


def test_structured_tokens_of_the_same_length_are_never_truncated():
    # An id / enum / phase / gate name at the very length that would clip a
    # free-text tail must survive whole — truncation is tail-only.
    long_id = "T" + "9" * 150            # 151-char subtask id
    long_enum = "R" + "E" * 150          # 151-char verdict enum
    long_phase = "p" * 151              # 151-char phase name
    long_gate_path = "g" * 151          # 151-char receipts path

    assert long_id in _strip(sl.subtask_done(long_id, 1, 1, color=False))
    assert long_enum in _strip(sl.verdict_line("validate_plan", long_enum, color=False))
    assert long_phase in _strip(sl.phase_start(long_phase, color=False))
    assert long_gate_path in _strip(
        sl.gates_line("1s", "1/1", long_gate_path, True, color=False)
    )


# ── colour discipline ────────────────────────────────────────────────────


def test_color_false_is_plain_text():
    line = sl.phase_start("plan", color=False)
    assert "\033[" not in line
    assert line == strip_ansi(line)


def test_color_true_paints_through_shared_palette():
    # ▶ start glyph is yellow+bold; the separator is grey. Both come from
    # the shared ansi palette via paint().
    start = sl.phase_start("plan", color=True)
    assert C.YELLOW in start
    assert C.RESET in start

    contract = sl.plan_contract(["T1"], 1, 0, color=True)
    assert C.GREY in contract           # the dim `·` separators
    assert strip_ansi(contract) == "✓ plan · contract: 1 tasks (T1) · acceptance 1 · risks 0"


def test_stderr_stream_auto_detect_stays_plain_when_not_a_tty(monkeypatch):
    # With color=None the formatter defers to paint()'s auto-detect against
    # the supplied stream. A non-TTY stderr must yield plain text.
    class _Stream:
        def isatty(self) -> bool:
            return False

    monkeypatch.delenv("NO_COLOR", raising=False)
    set_color_enabled(None)
    line = sl.phase_start("plan", stream=_Stream())
    assert "\033[" not in line
