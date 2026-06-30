"""Pre-phase skip channel in ``run_profile`` (ADR 0086, correction routing).

The ``on_phase_pre`` callback may mark the phase it is entering as not
applicable by setting ``state.extras[PHASE_PRE_SKIP_KEY]`` to a non-empty
reason. These tests pin the consume-once contract:

 * a marked top-level phase is skipped — its handler never runs, the phase
   log gets a ``skipped`` record, and on_phase_start/on_phase_end still fire;
 * the same holds for a loop-inner phase;
 * with no marking the run is byte-unchanged and the key never lingers in
   extras;
 * F1: when on_phase_pre both marks a skip AND halts (or requests handoff),
   halt wins — the handler does not run AND no stale key survives in extras;
 * the ``on_phase_end`` fired for a skipped phase carries the skip-end
   context (``PHASE_END_SKIPPED_KEY``) so the callback can suppress
   post-phase hooks, and the key never outlives that callback.
"""

from __future__ import annotations

import pytest

from core.io.ansi import C, get_color_enabled, set_color_enabled
from pipeline.plugins import PluginConfig
from pipeline.project import profile_dispatch
from pipeline.project.profile_dispatch import emit_phase_log_end
from pipeline.runtime import (
    LoopStep,
    PhaseRegistry,
    PhaseStep,
    PipelineState,
    Profile,
    run_profile,
)
from pipeline.runtime.runner import PHASE_END_SKIPPED_KEY, PHASE_PRE_SKIP_KEY


def _state(**kw) -> PipelineState:
    return PipelineState(task="t", project_dir="/p", plugin=PluginConfig(), **kw)


def _registry_recording(seen: list[str]) -> PhaseRegistry:
    """A registry where each handler appends its name to ``seen``."""
    reg = PhaseRegistry()

    def _make(name: str):
        def handler(state: PipelineState) -> PipelineState:
            seen.append(name)
            return state

        return handler

    for n in (
        "plan", "validate_plan", "implement",
        "review_changes", "repair_changes", "final_acceptance",
    ):
        reg.register(n, _make(n))
    return reg


def _callback_recorder() -> tuple[list[str], list[str], object, object]:
    starts: list[str] = []
    ends: list[str] = []

    def on_start(phase: str, state: PipelineState) -> None:
        starts.append(phase)

    def on_end(phase: str, state: PipelineState) -> None:
        ends.append(phase)

    return starts, ends, on_start, on_end


def test_top_level_phase_skipped_when_marked() -> None:
    seen: list[str] = []
    reg = _registry_recording(seen)
    starts, ends, on_start, on_end = _callback_recorder()

    def on_pre(phase: str, state: PipelineState) -> None:
        if phase == "implement":
            state.extras[PHASE_PRE_SKIP_KEY] = "not applicable: route 'gate_rerun'"

    profile = Profile(
        name="p", kind="advanced", description="d",
        steps=(PhaseStep(phase="implement"), PhaseStep(phase="final_acceptance")),
    )
    state = run_profile(
        profile, _state(), reg,
        on_phase_start=on_start, on_phase_end=on_end, on_phase_pre=on_pre,
    )

    # Handler did not run for the skipped phase; the next phase did.
    assert "implement" not in seen
    assert "final_acceptance" in seen
    # Skip record written with the route reason.
    assert state.phase_log["implement"]["skipped"] == (
        "not applicable: route 'gate_rerun'"
    )
    # START/END callbacks fired for the skipped phase (banner/trace coherence).
    assert "implement" in starts
    assert "implement" in ends
    # Consume-once: the key never lingers.
    assert PHASE_PRE_SKIP_KEY not in state.extras


def test_loop_inner_phase_skipped_when_marked() -> None:
    seen: list[str] = []
    reg = _registry_recording(seen)
    starts, ends, on_start, on_end = _callback_recorder()

    def on_pre(phase: str, state: PipelineState) -> None:
        if phase == "implement":
            state.extras[PHASE_PRE_SKIP_KEY] = "not applicable: route 'contract_ack'"

    # A single-round loop with two inner phases; skip the first.
    profile = Profile(
        name="p", kind="advanced", description="d",
        steps=(
            LoopStep(
                steps=(PhaseStep(phase="implement"), PhaseStep(phase="review_changes")),
                until="review_changes.never",
                max_rounds=1,
            ),
        ),
    )
    state = run_profile(
        profile, _state(), reg,
        on_phase_start=on_start, on_phase_end=on_end, on_phase_pre=on_pre,
    )

    assert "implement" not in seen
    assert "review_changes" in seen
    assert state.phase_log["implement"]["skipped"] == (
        "not applicable: route 'contract_ack'"
    )
    assert "implement" in starts
    assert "implement" in ends
    assert PHASE_PRE_SKIP_KEY not in state.extras


def test_no_marking_leaves_behavior_unchanged() -> None:
    seen: list[str] = []
    reg = _registry_recording(seen)

    pre_seen: list[str] = []

    def on_pre(phase: str, state: PipelineState) -> None:
        # A no-op callback that inspects but never marks a skip.
        pre_seen.append(phase)

    profile = Profile(
        name="p", kind="advanced", description="d",
        steps=(PhaseStep(phase="implement"), PhaseStep(phase="final_acceptance")),
    )
    state = run_profile(profile, _state(), reg, on_phase_pre=on_pre)

    # Both handlers ran in order; nothing skipped.
    assert seen == ["implement", "final_acceptance"]
    assert pre_seen == ["implement", "final_acceptance"]
    assert "skipped" not in state.phase_log.get("implement", {})
    # The channel key was never introduced.
    assert PHASE_PRE_SKIP_KEY not in state.extras


def test_halt_in_pre_outranks_skip_and_leaves_no_stale_key() -> None:
    # F1: on_phase_pre both marks a skip AND halts. Halt wins, the handler
    # never runs, and the popped skip key must not survive in extras.
    seen: list[str] = []
    reg = _registry_recording(seen)
    starts, ends, on_start, on_end = _callback_recorder()

    def on_pre(phase: str, state: PipelineState) -> None:
        if phase == "implement":
            state.extras[PHASE_PRE_SKIP_KEY] = "would-skip-but-halting"
            state.stop("correction_triage_blocked")

    profile = Profile(
        name="p", kind="advanced", description="d",
        steps=(PhaseStep(phase="implement"), PhaseStep(phase="final_acceptance")),
    )
    state = run_profile(
        profile, _state(), reg,
        on_phase_start=on_start, on_phase_end=on_end, on_phase_pre=on_pre,
    )

    assert state.halt
    assert state.halt_reason == "correction_triage_blocked"
    # Halt pre-empts the phase entirely: neither the skipped phase nor the
    # next one ran, and no skip record was written.
    assert seen == []
    assert "skipped" not in state.phase_log.get("implement", {})
    assert "implement" not in starts
    # The skip key was popped before the halt break — no stale key remains.
    assert PHASE_PRE_SKIP_KEY not in state.extras


def test_skip_end_callback_carries_skip_context() -> None:
    # Reviewer F1 (gates on skip): the on_phase_end fired for a skipped
    # phase must be distinguishable from a real phase-end, so the
    # orchestrator can suppress after_phase gates. The runner exposes the
    # skip-end context via PHASE_END_SKIPPED_KEY for exactly the duration
    # of the callback.
    seen: list[str] = []
    reg = _registry_recording(seen)
    end_contexts: dict[str, object] = {}

    def on_end(phase: str, state: PipelineState) -> None:
        end_contexts[phase] = state.extras.get(PHASE_END_SKIPPED_KEY)

    def on_pre(phase: str, state: PipelineState) -> None:
        if phase == "implement":
            state.extras[PHASE_PRE_SKIP_KEY] = "not applicable: route 'gate_rerun'"

    profile = Profile(
        name="p", kind="advanced", description="d",
        steps=(PhaseStep(phase="implement"), PhaseStep(phase="final_acceptance")),
    )
    state = run_profile(
        profile, _state(), reg, on_phase_end=on_end, on_phase_pre=on_pre,
    )

    # Skipped phase: callback saw its own name as the skip-end context.
    assert end_contexts["implement"] == "implement"
    # Executed phase: no skip-end context.
    assert end_contexts["final_acceptance"] is None
    # The key never outlives the callback.
    assert PHASE_END_SKIPPED_KEY not in state.extras


def test_handoff_in_pre_outranks_skip_and_leaves_no_stale_key() -> None:
    # F1 variant: on_phase_pre marks a skip AND requests a phase handoff.
    seen: list[str] = []
    reg = _registry_recording(seen)

    def on_pre(phase: str, state: PipelineState) -> None:
        if phase == "implement":
            state.extras[PHASE_PRE_SKIP_KEY] = "would-skip-but-handoff"
            state.phase_handoff_request = {"id": "x", "phase": phase}

    profile = Profile(
        name="p", kind="advanced", description="d",
        steps=(PhaseStep(phase="implement"), PhaseStep(phase="final_acceptance")),
    )
    state = run_profile(profile, _state(), reg, on_phase_pre=on_pre)

    assert state.phase_handoff_request is not None
    assert seen == []
    assert "skipped" not in state.phase_log.get("implement", {})
    assert PHASE_PRE_SKIP_KEY not in state.extras


# ── correction-route decision line at the correction_triage END (T2) ───────


def _capture_log_phase(monkeypatch) -> list[tuple]:
    """Record (header, label, kind, outcome) for each log_phase call."""
    calls: list[tuple] = []

    def fake(header, label, kind, outcome, **kw):
        calls.append((header, label, kind, outcome))

    monkeypatch.setattr(profile_dispatch, "log_phase", fake)
    return calls


def _last_outcome(calls: list[tuple]) -> str:
    assert calls, "expected a log_phase END call"
    return calls[-1][3]


@pytest.fixture
def force_color():
    """Force the colored render path on, restoring the process global after.

    ``paint`` auto-detection is off under capsys (non-TTY), so a test that
    needs to see the ANSI code must opt in explicitly. ``set_color_enabled``
    is a process global — save and restore it (see the lineage color-state
    leak fix) so the override never bleeds into other tests.
    """
    prev = get_color_enabled()
    set_color_enabled(True)
    try:
        yield
    finally:
        set_color_enabled(prev)


def test_triage_decision_shortcut_line_terminal_and_log(monkeypatch, capsys) -> None:
    calls = _capture_log_phase(monkeypatch)
    st = _state()
    st.phase_log["correction_triage"] = {
        "kind": "gate_rerun",
        "summary": "gates were stale; rerun is enough",
    }

    emit_phase_log_end("correction_triage", st, terminal=True)

    # Terminal: neutral chip (•), the full decision, NOT halted / not amber.
    out = capsys.readouterr().out
    assert "Correction route: gate_rerun" in out
    assert "implement/repair_changes/review_changes" in out
    assert "•" in out
    assert "⚠" not in out
    # progress.log END outcome carries the whole decision, folded after "ok".
    outcome = _last_outcome(calls)
    assert outcome.startswith("ok | ")
    assert "Correction route: gate_rerun → skipping " in outcome
    assert "implement/repair_changes/review_changes" in outcome


def test_triage_decision_blocked_line_amber_and_log(
    monkeypatch, capsys, force_color
) -> None:
    calls = _capture_log_phase(monkeypatch)
    st = _state()
    st.phase_log["correction_triage"] = {
        "kind": "blocked",
        "summary": "release blocked: contract drift unresolved",
        "blockers": ["missing API token"],
    }
    st.stop("correction_triage_blocked")
    st.extras["_current_phase"] = "correction_triage"

    emit_phase_log_end("correction_triage", st, terminal=True)

    out = capsys.readouterr().out
    # Amber tone (⚠ + yellow code), no neutral bullet, no green anywhere.
    assert "⚠" in out
    assert C.YELLOW in out
    assert C.GREEN not in out
    assert "Correction route: blocked" in out
    assert "halting before implement" in out
    assert "contract drift unresolved" in out
    assert "missing API token" in out
    # Outcome keeps the halted base and folds the full decision after it.
    outcome = _last_outcome(calls)
    assert outcome.startswith("halted: correction_triage_blocked | ")
    assert "Correction route: blocked → halting before implement;" in outcome
    assert "missing API token" in outcome


def test_triage_decision_silent_branch_logs_but_prints_nothing(
    monkeypatch, capsys
) -> None:
    calls = _capture_log_phase(monkeypatch)
    st = _state()
    st.phase_log["correction_triage"] = {
        "kind": "contract_ack",
        "summary": "ack the contract; no code change",
    }

    emit_phase_log_end("correction_triage", st, terminal=False)

    # terminal=False: no route line printed at all.
    assert capsys.readouterr().out == ""
    # ...but the END outcome still carries the full decision text.
    outcome = _last_outcome(calls)
    assert outcome.startswith("ok | ")
    assert "Correction route: contract_ack → skipping " in outcome
    assert "implement/repair_changes/review_changes" in outcome


@pytest.mark.parametrize("terminal", [True, False])
def test_non_correction_phase_output_byte_identical(
    monkeypatch, capsys, terminal
) -> None:
    # A profile without a correction_triage record must be untouched: the
    # END outcome stays the bare "ok" and nothing extra is printed.
    calls = _capture_log_phase(monkeypatch)
    st = _state()

    emit_phase_log_end("final_acceptance", st, terminal=terminal)

    assert capsys.readouterr().out == ""
    outcome = _last_outcome(calls)
    assert outcome == "ok"
    assert "Correction route" not in outcome


def test_correction_triage_without_record_is_unchanged(monkeypatch, capsys) -> None:
    # correction_triage banner with no triage evidence in the phase log: no
    # route line, bare "ok" outcome, byte-identical to the pre-T2 behavior.
    calls = _capture_log_phase(monkeypatch)
    st = _state()

    emit_phase_log_end("correction_triage", st, terminal=True)

    assert capsys.readouterr().out == ""
    outcome = _last_outcome(calls)
    assert outcome == "ok"
    assert "Correction route" not in outcome
