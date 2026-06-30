"""tests/unit/pipeline/control/test_handoff_prompt.py — TTY prompt for
phase-handoff resolution.

The prompt module owns the keyboard surface only — reading stdin,
rendering the menu, validating action choices against the runtime's
``available_actions`` list. Audit-trail invariants (ADR 0031 § 5)
live one layer up in the orchestrator, not here.

These tests pin the prompt's input-handling contract so the
interactive path stays predictable across input shapes:

* TTY-gating helper refuses non-TTY streams + ``--no-interactive``.
* Action menu accepts numeric and alphabetic shortcuts.
* Action menu refuses canonical actions that are not in
  ``available_actions`` (runtime decides which subset is valid).
* ``retry_feedback`` requires non-empty feedback; blank → aborted.
* Default note ``f"orcho-cli tty {action}"`` is used when the
  operator presses Enter at the note prompt.
* Ctrl-D / Ctrl-C / EOF / too many invalid attempts → sentinel
  ``HANDOFF_PROMPT_ABORTED``; never silently picks a default action.

The signal fixture mirrors what the loop dispatcher produces in
real runs (advanced profile, ``human_feedback_on_reject``, round
2/2 rejected) so the assertions match the canonical pause shape.
"""

from __future__ import annotations

import io
from typing import TextIO

import pytest

from core.io.ansi import strip_ansi
from pipeline.control.handoff_prompt import (
    HANDOFF_PROMPT_ABORTED,
    AdviceActionRequest,
    AdviceFollowup,
    HandoffDecisionInput,
    prompt_advice_followup,
    prompt_confirm,
    prompt_phase_handoff_action,
    should_prompt_for_phase_handoff,
)
from pipeline.runtime.handoff import PhaseHandoffRequested
from pipeline.runtime.roles import PhaseHandoffType


def _signal(
    *,
    available_actions: tuple[str, ...] = ("continue", "retry_feedback", "halt"),
    phase: str = "validate_plan",
    handoff_id: str = "validate_plan:plan_round:2",
    round_n: int = 2,
    loop_max: int = 2,
    last_output: str = "Mock critique: missing edge case A coverage.",
    trigger: str = "rejected",
    verdict: str = "REJECTED",
    artifacts: dict | None = None,
) -> PhaseHandoffRequested:
    return PhaseHandoffRequested(
        handoff_id=handoff_id,
        phase=phase,
        type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
        trigger=trigger,
        verdict=verdict,
        approved=False,
        round_extras_key="plan_round",
        round=round_n,
        loop_max_rounds=loop_max,
        available_actions=available_actions,
        artifacts=artifacts or {},
        last_output=last_output,
    )


_WAIVER_ACTIONS: tuple[str, ...] = (
    "continue", "retry_feedback", "halt", "continue_with_waiver",
)


def _implement_incomplete_signal(
    *,
    artifacts: dict,
    last_output: str = "## subtask T1-policy (ok)",
    available_actions: tuple[str, ...] = _WAIVER_ACTIONS,
) -> PhaseHandoffRequested:
    """An implement-phase handoff paused with ``trigger='incomplete'``."""
    return _signal(
        available_actions=available_actions,
        phase="implement",
        handoff_id="implement:implement_handoff:1",
        round_n=1,
        loop_max=1,
        trigger="incomplete",
        verdict="INCOMPLETE",
        last_output=last_output,
        artifacts=artifacts,
    )


class _FakeTTY(io.StringIO):
    """``StringIO`` that claims to be a TTY for ``should_prompt_for_phase_handoff``."""

    def isatty(self) -> bool:  # noqa: D401 — protocol method
        return True


def _scripted_stdin(*lines: str) -> _FakeTTY:
    """Feed multiple lines through a fake TTY in sequence."""
    return _FakeTTY("".join(line + "\n" for line in lines))


def _new_stdout() -> _FakeTTY:
    return _FakeTTY()


# ── should_prompt_for_phase_handoff ──────────────────────────────────────────


class TestShouldPromptGate:
    def test_no_interactive_flag_disables_prompt(self) -> None:
        assert should_prompt_for_phase_handoff(
            no_interactive=True,
            stdin=_FakeTTY(),
            stdout=_FakeTTY(),
        ) is False

    def test_non_tty_stdin_disables_prompt(self) -> None:
        non_tty = io.StringIO()  # no isatty=True override
        assert should_prompt_for_phase_handoff(
            no_interactive=False,
            stdin=non_tty,
            stdout=_FakeTTY(),
        ) is False

    def test_non_tty_stdout_disables_prompt(self) -> None:
        # ``orcho run >run.log`` is non-interactive by intent — popping
        # a prompt into a piped stdout would deadlock.
        assert should_prompt_for_phase_handoff(
            no_interactive=False,
            stdin=_FakeTTY(),
            stdout=io.StringIO(),
        ) is False

    def test_both_tty_enables_prompt(self) -> None:
        assert should_prompt_for_phase_handoff(
            no_interactive=False,
            stdin=_FakeTTY(),
            stdout=_FakeTTY(),
        ) is True

    def test_missing_isatty_attribute_disables(self) -> None:
        class _NoIsatty:
            pass
        assert should_prompt_for_phase_handoff(
            no_interactive=False,
            stdin=_NoIsatty(),  # type: ignore[arg-type]
            stdout=_FakeTTY(),
        ) is False

    def test_isatty_raises_disables(self) -> None:
        class _BrokenTTY:
            def isatty(self) -> bool:
                raise OSError("closed fd")
        assert should_prompt_for_phase_handoff(
            no_interactive=False,
            stdin=_BrokenTTY(),  # type: ignore[arg-type]
            stdout=_FakeTTY(),
        ) is False


# ── action menu ───────────────────────────────────────────────────────────────


class TestActionMenu:
    @pytest.mark.parametrize("key", ["1", "c", "continue"])
    def test_continue_aliases(self, key: str) -> None:
        result = prompt_phase_handoff_action(
            _signal(),
            stdin=_scripted_stdin(key, ""),  # action, default note
            stdout=_new_stdout(),
        )
        assert isinstance(result, HandoffDecisionInput)
        assert result.action == "continue"
        assert result.feedback is None
        assert result.note == "orcho-cli tty continue"

    @pytest.mark.parametrize("key", ["3", "h", "halt"])
    def test_halt_aliases(self, key: str) -> None:
        result = prompt_phase_handoff_action(
            _signal(),
            stdin=_scripted_stdin(key, ""),
            stdout=_new_stdout(),
        )
        assert isinstance(result, HandoffDecisionInput)
        assert result.action == "halt"
        assert result.feedback is None
        assert result.note == "orcho-cli tty halt"

    def test_unknown_action_re_prompts_then_eventually_succeeds(self) -> None:
        result = prompt_phase_handoff_action(
            _signal(),
            stdin=_scripted_stdin("typo", "garbage", "1", ""),
            stdout=_new_stdout(),
        )
        assert isinstance(result, HandoffDecisionInput)
        assert result.action == "continue"

    def test_too_many_invalid_attempts_aborts(self) -> None:
        # Default ``_MAX_INVALID_INPUT_RETRIES = 3`` — feeding three
        # invalid lines must surface ``HANDOFF_PROMPT_ABORTED``,
        # never silently pick a default. The fourth line is never
        # consumed because the prompt has already given up.
        result = prompt_phase_handoff_action(
            _signal(),
            stdin=_scripted_stdin("typo1", "typo2", "typo3", "1"),
            stdout=_new_stdout(),
        )
        assert result is HANDOFF_PROMPT_ABORTED

    def test_eof_at_action_aborts(self) -> None:
        # Empty stdin → readline returns "" → treated as EOF.
        result = prompt_phase_handoff_action(
            _signal(),
            stdin=_FakeTTY(""),
            stdout=_new_stdout(),
        )
        assert result is HANDOFF_PROMPT_ABORTED

    def test_action_not_in_available_actions_rejected(self) -> None:
        # The signal's ``available_actions`` is the source of truth — the
        # prompt must refuse any canonical action name that is not in it.
        # We synthesise a narrower set here so the negative path is
        # exercised regardless of what the active policy publishes.
        sig = _signal(available_actions=("continue", "halt"))
        result = prompt_phase_handoff_action(
            sig,
            stdin=_scripted_stdin("retry_feedback", "1", ""),
            stdout=_new_stdout(),
        )
        assert isinstance(result, HandoffDecisionInput)
        assert result.action == "continue"


# ── retry_feedback path ──────────────────────────────────────────────────────


class TestRetryFeedback:
    def test_single_line_feedback(self) -> None:
        result = prompt_phase_handoff_action(
            _signal(),
            stdin=_scripted_stdin(
                "2",                # action = retry_feedback
                "Add auth migration step",
                "",                 # blank line ends feedback
                "",                 # default note
            ),
            stdout=_new_stdout(),
        )
        assert isinstance(result, HandoffDecisionInput)
        assert result.action == "retry_feedback"
        assert result.feedback == "Add auth migration step"
        assert result.note == "orcho-cli tty retry_feedback"

    def test_multi_line_feedback(self) -> None:
        result = prompt_phase_handoff_action(
            _signal(),
            stdin=_scripted_stdin(
                "retry",
                "Line one",
                "Line two",
                "Line three",
                "",
                "",
            ),
            stdout=_new_stdout(),
        )
        assert isinstance(result, HandoffDecisionInput)
        assert result.feedback == "Line one\nLine two\nLine three"

    def test_blank_feedback_eof_aborts_run(self) -> None:
        # EOF arriving on the feedback prompt before any line is read
        # (e.g. ``< /dev/null`` closes stdin) surfaces as aborted so
        # the run stays paused rather than silently picking a default.
        result = prompt_phase_handoff_action(
            _signal(),
            stdin=_FakeTTY("2\n"),   # action only, EOF before feedback
            stdout=_new_stdout(),
        )
        assert result is HANDOFF_PROMPT_ABORTED

    def test_blank_first_line_aborts_immediately(self) -> None:
        # Live-TTY regression. In a real terminal a blank Enter on
        # the feedback prompt does *not* close stdin — the prior
        # ``continue``-on-empty-first-line implementation silently
        # swallowed the keypress and waited for more input. The
        # behaviour now must be: first empty line at the start of
        # the feedback prompt → immediate abort + visible message.
        # The trailing lines after the blank are scripted to verify
        # the prompt did NOT keep reading (otherwise they'd be
        # consumed by ``_read_feedback`` and we'd see a non-empty
        # return).
        stdin = _scripted_stdin(
            "2",          # action: retry_feedback
            "",           # blank Enter on feedback — must abort here
            "should-not", # would-be feedback if the prompt kept reading
            "be-read",
            "",
            "",           # would-be note
        )
        out = _new_stdout()
        result = prompt_phase_handoff_action(
            _signal(),
            stdin=stdin,
            stdout=out,
        )
        assert result is HANDOFF_PROMPT_ABORTED
        body = out.getvalue()
        assert "Feedback is empty" in body, (
            "blank-first-line abort must surface the same explanatory "
            "message as the EOF path so the operator sees why the run "
            "stayed paused"
        )
        # Defence-in-depth: after the abort, the remaining scripted
        # lines must still be on the buffer (we did not consume them).
        remaining = stdin.read()
        assert "should-not" in remaining, (
            "_read_feedback consumed lines past the blank — the abort "
            "must return immediately, not keep reading"
        )

    def test_feedback_whitespace_trimmed(self) -> None:
        result = prompt_phase_handoff_action(
            _signal(),
            stdin=_scripted_stdin(
                "2",
                "  feedback with leading spaces  ",
                "",
                "",
            ),
            stdout=_new_stdout(),
        )
        assert isinstance(result, HandoffDecisionInput)
        # Per-line ``rstrip("\n")`` keeps internal whitespace; the
        # combined string is then stripped end-to-end.
        assert result.feedback == "feedback with leading spaces"


class TestContinueWithWaiver:
    """The fourth action accepts a REJECTED verdict with a mandatory
    operator verdict (the durable waiver). It reuses the same feedback
    reader as ``retry_feedback`` — a non-empty verdict is required."""

    _WAIVER_SET = ("continue", "retry_feedback", "halt", "continue_with_waiver")

    @pytest.mark.parametrize("key", ["4", "w", "waiver", "continue_with_waiver"])
    def test_waiver_aliases_capture_verdict(self, key: str) -> None:
        result = prompt_phase_handoff_action(
            _signal(available_actions=self._WAIVER_SET),
            stdin=_scripted_stdin(
                key,
                "Accepted risk: legacy shim stays this release",
                "",   # blank line ends the verdict
                "",   # default note
            ),
            stdout=_new_stdout(),
        )
        assert isinstance(result, HandoffDecisionInput)
        assert result.action == "continue_with_waiver"
        assert result.feedback == (
            "Accepted risk: legacy shim stays this release"
        )
        assert result.note == "orcho-cli tty continue_with_waiver"

    def test_blank_verdict_aborts(self) -> None:
        # A waiver with no operator verdict is a contract violation — the
        # prompt aborts rather than recording an empty waiver.
        out = _new_stdout()
        result = prompt_phase_handoff_action(
            _signal(available_actions=self._WAIVER_SET),
            stdin=_scripted_stdin("4", "", "should-not-read", ""),
            stdout=out,
        )
        assert result is HANDOFF_PROMPT_ABORTED
        assert "Feedback is empty" in out.getvalue()

    def test_menu_shows_waiver_line_when_available(self) -> None:
        out = _new_stdout()
        prompt_phase_handoff_action(
            _signal(available_actions=self._WAIVER_SET),
            stdin=_scripted_stdin("1", ""),
            stdout=out,
        )
        body = out.getvalue()
        assert "continue_with_waiver" in body

    def test_menu_hides_waiver_when_unavailable(self) -> None:
        out = _new_stdout()
        prompt_phase_handoff_action(
            _signal(available_actions=("continue", "retry_feedback", "halt")),
            stdin=_scripted_stdin("1", ""),
            stdout=out,
        )
        assert "continue_with_waiver" not in out.getvalue()

    def test_waiver_prompt_labels_the_verdict_requirement(self) -> None:
        out = _new_stdout()
        prompt_phase_handoff_action(
            _signal(available_actions=self._WAIVER_SET),
            stdin=_scripted_stdin("4", "verdict text", "", ""),
            stdout=out,
        )
        assert "Operator verdict for the waiver" in out.getvalue()

    def test_full_set_hint_includes_waiver(self) -> None:
        from pipeline.control.handoff_prompt import _action_hint
        hint = _action_hint(set(self._WAIVER_SET))
        assert hint == (
            "  Action [1/2/3/4 or continue/retry/halt/waiver]: "
        )


# ── audit note path ──────────────────────────────────────────────────────────


class TestAuditNote:
    def test_custom_note_passed_through(self) -> None:
        result = prompt_phase_handoff_action(
            _signal(),
            stdin=_scripted_stdin("1", "operator: reviewed and approved"),
            stdout=_new_stdout(),
        )
        assert isinstance(result, HandoffDecisionInput)
        assert result.note == "operator: reviewed and approved"

    def test_blank_note_uses_default_per_action(self) -> None:
        for key, expected_action in [("1", "continue"), ("3", "halt")]:
            result = prompt_phase_handoff_action(
                _signal(),
                stdin=_scripted_stdin(key, ""),
                stdout=_new_stdout(),
            )
            assert isinstance(result, HandoffDecisionInput)
            assert result.note == f"orcho-cli tty {expected_action}"


# ── menu rendering ───────────────────────────────────────────────────────────


class TestMenuRendering:
    def test_summary_includes_handoff_id_and_round(self) -> None:
        out: TextIO = _new_stdout()
        prompt_phase_handoff_action(
            _signal(),
            stdin=_scripted_stdin("1", ""),
            stdout=out,
        )
        body = out.getvalue()  # type: ignore[attr-defined]
        assert "validate_plan:plan_round:2" in body
        assert "round 2/2" in body

    def test_summary_includes_last_output(self) -> None:
        out = _new_stdout()
        prompt_phase_handoff_action(
            _signal(last_output="Plan rejected: missing X."),
            stdin=_scripted_stdin("1", ""),
            stdout=out,
        )
        assert "Plan rejected: missing X." in out.getvalue()

    def test_menu_hides_unavailable_action(self) -> None:
        # The menu renderer must honour ``available_actions`` — any
        # canonical action absent from the signal's set must not appear
        # in the printed menu. We synthesise a narrower set here so the
        # rendering branch is exercised regardless of what the active
        # policy publishes.
        out = _new_stdout()
        prompt_phase_handoff_action(
            _signal(available_actions=("continue", "halt")),
            stdin=_scripted_stdin("1", ""),
            stdout=out,
        )
        body = out.getvalue()
        assert "continue" in body
        assert "halt" in body
        assert "retry_feedback" not in body, (
            "retry_feedback line must be hidden when not in available_actions"
        )

    def test_implement_handoff_menu_uses_implementation_retry_terms(self) -> None:
        out = _new_stdout()
        prompt_phase_handoff_action(
            _signal(
                available_actions=(
                    "continue",
                    "retry_feedback",
                    "halt",
                    "continue_with_waiver",
                ),
                phase="implement",
                handoff_id="implement:implement_handoff:1",
                round_n=1,
                loop_max=1,
                trigger="incomplete",
                verdict="INCOMPLETE",
                last_output="## subtask T1-policy-and-assessment (ok)",
            ),
            stdin=_scripted_stdin("3", ""),
            stdout=out,
        )
        body = out.getvalue()
        assert "Last implementation output" in body
        assert "retry incomplete implementation subtasks" in body
        assert "accept the INCOMPLETE verdict" in body
        assert "one extra plan round" not in body

    def test_review_handoff_menu_uses_repair_retry_terms(self) -> None:
        out = _new_stdout()
        prompt_phase_handoff_action(
            _signal(
                phase="review_changes",
                handoff_id="review_changes:repair_round:1",
                round_n=1,
                loop_max=1,
            ),
            stdin=_scripted_stdin("1", ""),
            stdout=out,
        )
        body = out.getvalue()
        assert "repair_changes → review_changes retry" in body
        assert "one extra plan round" not in body


class TestImplementIncompleteDigest:
    """``_print_summary`` renders a decision-first digest for an implement
    handoff paused with ``trigger='incomplete'`` — and changes nothing for any
    other phase / trigger."""

    def test_subtask_and_criterion_precede_raw_output(self) -> None:
        out = _new_stdout()
        prompt_phase_handoff_action(
            _implement_incomplete_signal(
                artifacts={
                    "incomplete_subtasks": ["T2-wire"],
                    "attestation_incomplete": {
                        "T2-wire": "criterion 3: tests not added",
                    },
                    "missing_subtask_receipts": [],
                },
                last_output="## subtask T2-wire (incomplete)",
            ),
            stdin=_scripted_stdin("3", ""),
            stdout=out,
        )
        body = strip_ansi(out.getvalue())
        assert "Why paused" in body
        assert "Subtask: T2-wire" in body
        assert "criterion 3: tests not added" in body
        # Decision-first: the subtask/criterion digest precedes the raw output.
        assert body.index("Subtask: T2-wire") < body.index(
            "Last implementation output"
        )
        assert body.index("criterion 3: tests not added") < body.index(
            "Last implementation output"
        )
        # Recommends a real-work retry, no verification-exception note.
        assert "Recommended" in body
        assert "retry_feedback" in body
        assert "verification exception" not in body

    def test_baseline_marker_shows_exception_and_waiver(self) -> None:
        out = _new_stdout()
        prompt_phase_handoff_action(
            _implement_incomplete_signal(
                artifacts={
                    "incomplete_subtasks": ["T1-mod"],
                    "attestation_incomplete": {
                        "T1-mod": "suite red but baseline-identical failure",
                    },
                    "missing_subtask_receipts": [],
                },
            ),
            stdin=_scripted_stdin("3", ""),
            stdout=out,
        )
        body = strip_ansi(out.getvalue())
        assert "verification exception" in body
        assert "baseline / pre-existing, unrelated to this diff" in body
        assert "continue_with_waiver" in body
        assert "not a dirty override" in body

    def test_real_missing_work_recommends_retry(self) -> None:
        out = _new_stdout()
        prompt_phase_handoff_action(
            _implement_incomplete_signal(
                artifacts={
                    "incomplete_subtasks": ["T4-impl"],
                    "attestation_incomplete": {
                        "T4-impl": "criterion 2: handler not implemented",
                    },
                    "missing_subtask_receipts": ["T5-skip"],
                },
            ),
            stdin=_scripted_stdin("3", ""),
            stdout=out,
        )
        body = strip_ansi(out.getvalue())
        assert "Recommended" in body
        assert "retry_feedback" in body
        assert "verification exception" not in body
        # The missing receipt is surfaced in the digest.
        assert "T5-skip" in body

    def test_metadata_demoted_under_details_and_truncated(self) -> None:
        long_output = "LINE-" + "x" * 600
        out = _new_stdout()
        prompt_phase_handoff_action(
            _implement_incomplete_signal(
                artifacts={
                    "incomplete_subtasks": ["T2-wire"],
                    "attestation_incomplete": {"T2-wire": "criteria not closed"},
                    "missing_subtask_receipts": [],
                },
                last_output=long_output,
            ),
            stdin=_scripted_stdin("3", ""),
            stdout=out,
        )
        body = strip_ansi(out.getvalue())
        # handoff_id / policy / trigger / verdict survive, but under a secondary
        # Details heading that comes after the digest.
        assert "Details:" in body
        assert body.index("Why paused") < body.index("Details:")
        assert "implement:implement_handoff:1" in body
        assert "trigger" in body
        assert "verdict" in body
        assert body.index("Why paused") < body.index(
            "implement:implement_handoff:1"
        )
        # Raw output is truncated harder than the 320-char legacy budget.
        assert "..." in body
        assert "x" * 320 not in body

    def test_non_implement_signal_keeps_legacy_form(self) -> None:
        for phase, handoff_id in (
            ("validate_plan", "validate_plan:plan_round:2"),
            ("review_changes", "review_changes:repair_round:1"),
        ):
            out = _new_stdout()
            prompt_phase_handoff_action(
                _signal(phase=phase, handoff_id=handoff_id),
                stdin=_scripted_stdin("1", ""),
                stdout=out,
            )
            body = strip_ansi(out.getvalue())
            assert "Why paused" not in body
            assert "Recommended" not in body
            assert "Details:" not in body
            # Legacy metadata block (two-space indent, no Details heading).
            assert f"  handoff_id : {handoff_id}" in body
            assert "Last reviewer output" in body

    def test_implement_rejected_trigger_not_a_digest(self) -> None:
        # Branch is gated on BOTH phase=='implement' and trigger=='incomplete'.
        # An implement handoff with a different trigger keeps the legacy form.
        out = _new_stdout()
        prompt_phase_handoff_action(
            _signal(
                phase="implement",
                handoff_id="implement:implement_handoff:1",
                trigger="rejected",
                verdict="REJECTED",
                last_output="## subtask failed review",
            ),
            stdin=_scripted_stdin("1", ""),
            stdout=out,
        )
        body = strip_ansi(out.getvalue())
        assert "Why paused" not in body
        assert "Details:" not in body
        assert "  handoff_id : implement:implement_handoff:1" in body

    def test_final_acceptance_non_regression(self) -> None:
        # AC7: final_acceptance (like validate_plan / review_changes) must NOT
        # enter the digest branch — its _last_output_label is the reviewer
        # label, and the legacy metadata form is preserved byte-for-byte.
        out = _new_stdout()
        prompt_phase_handoff_action(
            _signal(
                phase="final_acceptance",
                handoff_id="final_acceptance:release_round:1",
                trigger="rejected",
                verdict="REJECTED",
                last_output="Release reviewer: gate not satisfied.",
            ),
            stdin=_scripted_stdin("1", ""),
            stdout=out,
        )
        body = strip_ansi(out.getvalue())
        assert "Why paused" not in body
        assert "Recommended" not in body
        assert "Details:" not in body
        assert "Last reviewer output" in body
        assert "Last implementation output" not in body
        assert "  handoff_id : final_acceptance:release_round:1" in body
        assert "  policy     :" in body
        assert "  trigger    : rejected" in body
        assert "  verdict    : REJECTED" in body


class TestActionHint:
    """The ``Action [...]`` input hint must reflect the LIVE
    ``available_actions`` — when an action is narrowed out of the
    payload (e.g. ``retry_feedback`` before A2c) the hint must not
    advertise a phantom ``2/retry`` option (the bugfix; the old hint
    was a hardcoded ``[1/2/3 or continue/retry/halt]`` string)."""

    def test_full_set_hint(self) -> None:
        from pipeline.control.handoff_prompt import _action_hint
        assert _action_hint({"continue", "retry_feedback", "halt"}) == (
            "  Action [1/2/3 or continue/retry/halt]: "
        )

    def test_narrowed_hint_omits_retry(self) -> None:
        from pipeline.control.handoff_prompt import _action_hint
        hint = _action_hint({"continue", "halt"})
        assert hint == "  Action [1/3 or continue/halt]: "
        assert "retry" not in hint
        assert "2" not in hint


# ── advisory menu items (5 / 6) ───────────────────────────────────────────────


class TestAdvisoryMenu:
    """advisory_available gates the 5) advice / 6) retry_with_advice items,
    aliases and hint — and changes nothing when False."""

    def test_advisory_items_hidden_by_default(self) -> None:
        out = _new_stdout()
        prompt_phase_handoff_action(
            _signal(),
            stdin=_scripted_stdin("1", ""),
            stdout=out,
        )
        body = out.getvalue()
        assert "advice" not in body
        assert "retry_with_advice" not in body
        assert "5)" not in body
        assert "6)" not in body

    def test_menu_byte_for_byte_identical_when_disabled(self) -> None:
        # Passing advisory_available=False must produce the exact same stdout
        # as not passing it at all (the legacy default).
        out_default = _new_stdout()
        prompt_phase_handoff_action(
            _signal(available_actions=(
                "continue", "retry_feedback", "halt", "continue_with_waiver",
            )),
            stdin=_scripted_stdin("1", ""),
            stdout=out_default,
        )
        out_explicit = _new_stdout()
        prompt_phase_handoff_action(
            _signal(available_actions=(
                "continue", "retry_feedback", "halt", "continue_with_waiver",
            )),
            advisory_available=False,
            stdin=_scripted_stdin("1", ""),
            stdout=out_explicit,
        )
        assert out_explicit.getvalue() == out_default.getvalue()

    def test_advisory_items_shown_when_available(self) -> None:
        out = _new_stdout()
        prompt_phase_handoff_action(
            _signal(),
            advisory_available=True,
            stdin=_scripted_stdin("1", ""),
            stdout=out,
        )
        body = out.getvalue()
        assert "5) 💡 advice" in body
        assert "6) 🤖 retry_with_advice" in body

    def test_hint_unchanged_without_advisory(self) -> None:
        # Regression: the input hint with advisory disabled is byte-for-byte
        # the canonical string.
        from pipeline.control.handoff_prompt import _action_hint
        assert _action_hint(
            {"continue", "retry_feedback", "halt"}, advisory_available=False,
        ) == "  Action [1/2/3 or continue/retry/halt]: "

    def test_hint_extended_in_advisory_mode(self) -> None:
        from pipeline.control.handoff_prompt import _action_hint
        hint = _action_hint(
            {"continue", "retry_feedback", "halt"}, advisory_available=True,
        )
        assert hint == (
            "  Action [1/2/3/5/6 or "
            "continue/retry/halt/advice/retry_with_advice]: "
        )

    @pytest.mark.parametrize("key", ["5", "a", "advice"])
    def test_select_advice_returns_request(self, key: str) -> None:
        result = prompt_phase_handoff_action(
            _signal(),
            advisory_available=True,
            stdin=_scripted_stdin(key, "leftover-not-read"),
            stdout=_new_stdout(),
        )
        assert isinstance(result, AdviceActionRequest)
        assert result.kind == "advice"

    @pytest.mark.parametrize("key", ["6", "ra", "retry_with_advice"])
    def test_select_retry_with_advice_returns_request(self, key: str) -> None:
        result = prompt_phase_handoff_action(
            _signal(),
            advisory_available=True,
            stdin=_scripted_stdin(key, "leftover-not-read"),
            stdout=_new_stdout(),
        )
        assert isinstance(result, AdviceActionRequest)
        assert result.kind == "retry_with_advice"

    def test_advisory_selection_reads_no_feedback_or_note(self) -> None:
        # After returning the pseudo-action request, the remaining scripted
        # lines must be untouched (no feedback / note consumed).
        stdin = _scripted_stdin("5", "still-here-1", "still-here-2")
        result = prompt_phase_handoff_action(
            _signal(),
            advisory_available=True,
            stdin=stdin,
            stdout=_new_stdout(),
        )
        assert isinstance(result, AdviceActionRequest)
        assert "still-here-1" in stdin.read()

    def test_advisory_keys_inert_when_not_available(self) -> None:
        # '5' is not a valid action without advisory mode → treated as an
        # unknown action, re-prompted, then a canonical action succeeds.
        result = prompt_phase_handoff_action(
            _signal(),
            stdin=_scripted_stdin("5", "advice", "1", ""),
            stdout=_new_stdout(),
        )
        assert isinstance(result, HandoffDecisionInput)
        assert result.action == "continue"


# ── advice follow-up sub-menu ─────────────────────────────────────────────────


def _advice_kwargs(**over: object) -> dict:
    base = dict(
        recommended_action="retry_feedback",
        confidence="high",
        rationale="The reviewer flagged a missing test for edge case A.",
        retry_feedback_preview="Add a test for edge case A and re-run pytest.",
        risks=("scope creep",),
        expected_files=("a.py",),
        operator_note="",
    )
    base.update(over)
    return base


class TestAdviceFollowup:
    def test_apply_returns_apply_no_feedback(self) -> None:
        result = prompt_advice_followup(
            **_advice_kwargs(),
            stdin=_scripted_stdin("1"),
            stdout=_new_stdout(),
        )
        assert isinstance(result, AdviceFollowup)
        assert result.action == "apply"
        assert result.feedback is None

    @pytest.mark.parametrize(
        "key,expected", [("apply", "apply"), ("3", "back"), ("back", "back"),
                         ("4", "halt"), ("halt", "halt")],
    )
    def test_simple_choices(self, key: str, expected: str) -> None:
        result = prompt_advice_followup(
            **_advice_kwargs(),
            stdin=_scripted_stdin(key),
            stdout=_new_stdout(),
        )
        assert isinstance(result, AdviceFollowup)
        assert result.action == expected
        assert result.feedback is None

    def test_edit_replaces_feedback(self) -> None:
        out = _new_stdout()
        result = prompt_advice_followup(
            **_advice_kwargs(),
            stdin=_scripted_stdin("2", "Operator-edited feedback line", ""),
            stdout=out,
        )
        assert isinstance(result, AdviceFollowup)
        assert result.action == "apply"
        assert result.feedback == "Operator-edited feedback line"
        # The generated feedback is printed in full so the operator can edit it.
        assert "Add a test for edge case A and re-run pytest." in out.getvalue()

    def test_edit_multiline_feedback(self) -> None:
        result = prompt_advice_followup(
            **_advice_kwargs(),
            stdin=_scripted_stdin("edit", "Line one", "Line two", ""),
            stdout=_new_stdout(),
        )
        assert isinstance(result, AdviceFollowup)
        assert result.feedback == "Line one\nLine two"

    def test_empty_edit_feedback_aborts(self) -> None:
        # Blank first line on the edit feedback prompt aborts exactly like
        # _read_feedback elsewhere.
        out = _new_stdout()
        result = prompt_advice_followup(
            **_advice_kwargs(),
            stdin=_scripted_stdin("2", "", "should-not-read", ""),
            stdout=out,
        )
        assert result is HANDOFF_PROMPT_ABORTED
        assert "Feedback is empty" in out.getvalue()

    def test_summary_renders_recommendation(self) -> None:
        out = _new_stdout()
        prompt_advice_followup(
            **_advice_kwargs(),
            stdin=_scripted_stdin("3"),
            stdout=out,
        )
        # ``_new_stdout`` is a ``_FakeTTY`` (isatty()==True), so when the run
        # environment leaves ``NO_COLOR`` unset the summary is legitimately
        # painted. Strip ANSI before the substring assertions — the same
        # env-robust pattern the sibling render tests above use — so the exact
        # text contract is verified without depending on ambient color state.
        body = strip_ansi(out.getvalue())
        assert "recommended : retry_feedback" in body
        assert "confidence: high" in body

    def test_unknown_choice_reprompts_then_succeeds(self) -> None:
        result = prompt_advice_followup(
            **_advice_kwargs(),
            stdin=_scripted_stdin("nope", "1"),
            stdout=_new_stdout(),
        )
        assert isinstance(result, AdviceFollowup)
        assert result.action == "apply"

    def test_too_many_invalid_aborts(self) -> None:
        result = prompt_advice_followup(
            **_advice_kwargs(),
            stdin=_scripted_stdin("x", "y", "z", "1"),
            stdout=_new_stdout(),
        )
        assert result is HANDOFF_PROMPT_ABORTED

    def test_eof_aborts(self) -> None:
        result = prompt_advice_followup(
            **_advice_kwargs(),
            stdin=_FakeTTY(""),
            stdout=_new_stdout(),
        )
        assert result is HANDOFF_PROMPT_ABORTED


# ── prompt_confirm ────────────────────────────────────────────────────────────


class TestPromptConfirm:
    @pytest.mark.parametrize("key", ["y", "yes"])
    def test_yes(self, key: str) -> None:
        assert prompt_confirm(
            "Apply low-confidence advice?",
            stdin=_scripted_stdin(key),
            stdout=_new_stdout(),
        ) is True

    @pytest.mark.parametrize("key", ["n", "no"])
    def test_no(self, key: str) -> None:
        assert prompt_confirm(
            "Apply low-confidence advice?",
            stdin=_scripted_stdin(key),
            stdout=_new_stdout(),
        ) is False

    def test_invalid_then_yes(self) -> None:
        assert prompt_confirm(
            "Proceed?",
            stdin=_scripted_stdin("maybe", "y"),
            stdout=_new_stdout(),
        ) is True

    def test_eof_aborts(self) -> None:
        assert prompt_confirm(
            "Proceed?",
            stdin=_FakeTTY(""),
            stdout=_new_stdout(),
        ) is HANDOFF_PROMPT_ABORTED

    def test_too_many_invalid_aborts(self) -> None:
        assert prompt_confirm(
            "Proceed?",
            stdin=_scripted_stdin("a", "b", "c"),
            stdout=_new_stdout(),
        ) is HANDOFF_PROMPT_ABORTED
