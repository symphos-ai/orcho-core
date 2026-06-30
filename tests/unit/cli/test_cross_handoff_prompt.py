"""Cross CLI interactive phase_handoff prompt parity with ``orcho run``.

The cross CLI used to map ``status='awaiting_phase_handoff'`` straight
to ``sys.exit(4)``. Mono ``orcho run`` first checks whether stdin is a
TTY and ``--no-interactive`` is unset; when both hold it calls
``prompt_phase_handoff_action`` and resumes the runner in-process. The
fix wires the same in-process loop into the cross CLI for the cross_plan
handoff (ADR 0038 payload shape).

These tests pin the behaviour: signal hydration, the no-interactive
fall-through, the aborted-prompt short-circuit, the live-prompt → SDK
decide → orchestrator re-enter cycle, and — for project-proxy pauses
bubbled up from a child sub-pipeline — that the cross CLI now prompts
and records the decision against the parent id (the
``phase_handoff_kind == "project"`` resume router routes it to the
child run). They monkeypatch ``run_cross_pipeline`` so no real cross run
fires.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# ── helper hydrator ─────────────────────────────────────────────────────────


def _payload(**overrides) -> dict:
    base = {
        "id":                "cross_plan:cross_plan_round:2",
        "phase":             "cross_plan",
        "type":              "human_feedback_on_reject",
        "trigger":           "rejected",
        "verdict":           "REJECTED",
        "approved":          False,
        "round_extras_key":  "cross_plan_round",
        "round":             2,
        "loop_max_rounds":   2,
        "available_actions": ["continue", "retry_feedback", "halt"],
        "artifacts":         {"short_summary": "...", "findings": []},
        "last_output":       "raw plan output",
    }
    base.update(overrides)
    return base


class TestBuildHandoffSignalFromPayload:
    def test_hydrates_complete_payload(self) -> None:
        from pipeline.cross_project.cli import (
            _build_handoff_signal_from_payload,
        )
        from pipeline.runtime.handoff import PhaseHandoffRequested
        from pipeline.runtime.roles import PhaseHandoffType

        signal = _build_handoff_signal_from_payload(_payload())

        assert isinstance(signal, PhaseHandoffRequested)
        assert signal.handoff_id == "cross_plan:cross_plan_round:2"
        assert signal.phase == "cross_plan"
        assert signal.type is PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT
        assert signal.round == 2
        assert signal.loop_max_rounds == 2
        assert "halt" in signal.available_actions

    def test_returns_none_on_missing_field(self) -> None:
        from pipeline.cross_project.cli import (
            _build_handoff_signal_from_payload,
        )
        # Drop the required ``id`` field. Malformed payloads must yield
        # None so the CLI falls back to non-interactive pause rather
        # than guessing defaults for audit-critical state.
        bad = _payload()
        del bad["id"]
        assert _build_handoff_signal_from_payload(bad) is None

    def test_returns_none_on_unknown_handoff_type(self) -> None:
        from pipeline.cross_project.cli import (
            _build_handoff_signal_from_payload,
        )
        assert _build_handoff_signal_from_payload(
            _payload(type="garbage_type"),
        ) is None


# ── CLI loop integration ────────────────────────────────────────────────────


class _CrossEnv:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        unity = tmp_path / "unity"
        unity.mkdir()
        api = tmp_path / "api"
        api.mkdir()
        monkeypatch.setenv("ORCHO_WORKSPACE", str(workspace))
        monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)
        self.workspace = workspace
        self.unity = unity
        self.api = api

    def argv(self, monkeypatch: pytest.MonkeyPatch, *extra: str) -> None:
        monkeypatch.setattr(sys, "argv", [
            "orcho-cross",
            "--task", "T",
            "--projects",
            f"unity:{self.unity}",
            f"api:{self.api}",
            "--mock",
            "--workspace", str(self.workspace),
            *extra,
        ])


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _CrossEnv:
    return _CrossEnv(tmp_path, monkeypatch)


def _install_run_mock(monkeypatch, sessions):
    """Mock ``run_cross_pipeline`` to return ``sessions[i]`` on call i."""
    from pipeline.cross_project import orchestrator as _xo

    calls = []

    def _fake(**kwargs):
        calls.append(kwargs)
        idx = min(len(calls) - 1, len(sessions) - 1)
        return sessions[idx]

    monkeypatch.setattr(_xo, "run_cross_pipeline", _fake)
    return calls


class TestCrossHandoffCliLoop:
    def test_no_interactive_falls_through_to_exit_4(
        self, env: _CrossEnv, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_run_mock(monkeypatch, [
            {"status": "awaiting_phase_handoff", "phase_handoff": _payload()},
        ])
        env.argv(monkeypatch, "--no-interactive")

        from pipeline.cross_project.cli import main
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 4

    def test_project_proxy_payload_prompts_and_routes(
        self, env: _CrossEnv, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Project-proxy payloads (``project:<alias>:...``) bubbled up
        from a child sub-pipeline are now prompted in-process. The cross
        CLI records the operator decision against the *parent* id on the
        cross run; the ``phase_handoff_kind == "project"`` resume router
        (covered by ``test_cross_project_handoff_resume_writes_child_
        decision``) routes it to the child run on re-entry."""
        proxy = _payload(id="project:api:review_changes:repair_round:1")
        sessions = [
            {"status": "awaiting_phase_handoff", "phase_handoff": proxy},
            {"status": "done"},
        ]
        calls = _install_run_mock(monkeypatch, sessions)
        env.argv(monkeypatch)

        decide_calls = []

        def _fake_decide(run_id, handoff_id, action, **kwargs):
            decide_calls.append({
                "run_id": run_id, "handoff_id": handoff_id, "action": action,
                **{k: v for k, v in kwargs.items() if k in ("feedback", "note")},
            })

        monkeypatch.setattr(
            "sdk.phase_handoff.phase_handoff_decide", _fake_decide,
        )
        from types import SimpleNamespace
        monkeypatch.setattr(
            "pipeline.control.load_resume_meta",
            lambda _path: SimpleNamespace(meta={}, path=_path / "meta.json"),
        )
        monkeypatch.setattr(
            "pipeline.control.handoff_prompt.should_prompt_for_phase_handoff",
            lambda **_: True,
        )
        from pipeline.control import handoff_prompt as _hp
        monkeypatch.setattr(
            _hp, "prompt_phase_handoff_action",
            lambda *_a, **_k: _hp.HandoffDecisionInput(
                action="continue", feedback=None, note=None,
            ),
        )

        from pipeline.cross_project.cli import main
        main()  # done is terminal — exit cleanly.

        # Re-entered exactly once after the operator decision.
        assert len(calls) == 2
        second = calls[1]
        assert second["resume_from"] is not None
        # The decision is recorded against the PARENT id on the cross run
        # (not bounced directly to the child) — the resume router owns the
        # child routing.
        assert len(decide_calls) == 1
        assert decide_calls[0]["action"] == "continue"
        assert decide_calls[0]["handoff_id"] == (
            "project:api:review_changes:repair_round:1"
        )

    def test_prompt_abort_falls_through_to_exit_4(
        self, env: _CrossEnv, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_run_mock(monkeypatch, [
            {"status": "awaiting_phase_handoff", "phase_handoff": _payload()},
        ])
        env.argv(monkeypatch)
        from pipeline.control import handoff_prompt as _hp
        monkeypatch.setattr(
            _hp, "should_prompt_for_phase_handoff", lambda **_: True,
        )
        # Force the prompt helper into the aborted-sentinel branch.
        monkeypatch.setattr(
            _hp, "prompt_phase_handoff_action",
            lambda *_a, **_k: _hp.HANDOFF_PROMPT_ABORTED,
        )

        from pipeline.cross_project.cli import main
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 4

    def test_halt_decision_drives_runner_into_terminal(
        self, env: _CrossEnv, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sessions = [
            {"status": "awaiting_phase_handoff", "phase_handoff": _payload()},
            # Second call (after operator halt) — orchestrator returns
            # the terminal halted session via _resume_handoff_decision.
            {"status": "halted", "halt_reason": "phase_handoff_halt"},
        ]
        calls = _install_run_mock(monkeypatch, sessions)
        env.argv(monkeypatch)

        # Stub the decide path so we don't touch real meta.json / SDK
        # filesystem invariants — this test focuses on the CLI loop.
        decide_calls = []

        def _fake_decide(run_id, handoff_id, action, **kwargs):
            decide_calls.append({
                "run_id": run_id, "handoff_id": handoff_id, "action": action,
                **{k: v for k, v in kwargs.items() if k in ("feedback", "note")},
            })

        monkeypatch.setattr(
            "sdk.phase_handoff.phase_handoff_decide", _fake_decide,
        )
        # Stub the resume-meta reload to a minimal stand-in. The CLI
        # only reads ``.meta`` off the returned record.
        from types import SimpleNamespace
        monkeypatch.setattr(
            "pipeline.control.load_resume_meta",
            lambda _path: SimpleNamespace(meta={}, path=_path / "meta.json"),
        )
        # Force a "TTY" — bypass the real isatty gate.
        monkeypatch.setattr(
            "pipeline.control.handoff_prompt.should_prompt_for_phase_handoff",
            lambda **_: True,
        )
        # Operator picks "halt"; helper returns the decision dataclass.
        from pipeline.control import handoff_prompt as _hp
        monkeypatch.setattr(
            _hp, "prompt_phase_handoff_action",
            lambda *_a, **_k: _hp.HandoffDecisionInput(
                action="halt", feedback=None, note="cli test",
            ),
        )

        from pipeline.cross_project.cli import main
        # ``halted`` final status is not in the awaiting/failed set, so
        # main() returns without SystemExit.
        main()

        assert len(calls) == 2, (
            "expected the cross runner to be re-entered exactly once after "
            "the operator halt decision"
        )
        # Second call must be a checkpoint resume (resume_from set, no
        # FOLLOWUP-only seeds).
        second = calls[1]
        assert second["resume_from"] is not None
        assert second.get("followup_session_seeds_per_alias") is None
        # SDK decide was invoked with the halt action.
        assert len(decide_calls) == 1
        assert decide_calls[0]["action"] == "halt"
        assert decide_calls[0]["handoff_id"].startswith("cross_plan:")

    def test_continue_decision_drives_runner_to_done(
        self, env: _CrossEnv, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``continue`` resumes the cross runner in checkpoint-resume
        mode, the planning loop reads the decision artifact, and the
        next round reaches a terminal ``done`` status. The CLI loop
        exits cleanly (no SystemExit) and the SDK decide is recorded
        exactly once with the ``continue`` action."""
        sessions = [
            {"status": "awaiting_phase_handoff", "phase_handoff": _payload()},
            # Operator picked ``continue``; the planning loop accepts
            # the last plan output and progresses to per-alias dispatch
            # + cross final acceptance, landing on terminal ``done``.
            {"status": "done"},
        ]
        calls = _install_run_mock(monkeypatch, sessions)
        env.argv(monkeypatch)

        decide_calls = []

        def _fake_decide(run_id, handoff_id, action, **kwargs):
            decide_calls.append({
                "run_id": run_id, "handoff_id": handoff_id, "action": action,
                **{k: v for k, v in kwargs.items() if k in ("feedback", "note")},
            })

        monkeypatch.setattr(
            "sdk.phase_handoff.phase_handoff_decide", _fake_decide,
        )
        from types import SimpleNamespace
        monkeypatch.setattr(
            "pipeline.control.load_resume_meta",
            lambda _path: SimpleNamespace(meta={}, path=_path / "meta.json"),
        )
        monkeypatch.setattr(
            "pipeline.control.handoff_prompt.should_prompt_for_phase_handoff",
            lambda **_: True,
        )
        from pipeline.control import handoff_prompt as _hp
        monkeypatch.setattr(
            _hp, "prompt_phase_handoff_action",
            lambda *_a, **_k: _hp.HandoffDecisionInput(
                action="continue", feedback=None, note=None,
            ),
        )

        from pipeline.cross_project.cli import main
        # ``done`` is terminal and not in the awaiting/failed set, so
        # main() returns without SystemExit.
        main()

        assert len(calls) == 2, (
            "cross runner must be re-entered exactly once after "
            "``continue`` decision"
        )
        # Re-entry is checkpoint-resume; FOLLOWUP-only seeds cleared.
        second = calls[1]
        assert second["resume_from"] is not None
        assert second.get("followup_session_seeds_per_alias") is None
        # The original FOLLOWUP-only inputs that may have been set on
        # the first entry are cleared on resume so the child sub-
        # pipelines aren't re-seeded.
        assert second.get("followup_parent_run_id") is None
        assert second.get("followup_parent_run_dir") is None
        assert second.get("followup_parent_status") is None
        assert second.get("followup_base_task") is None
        # SDK decide recorded once with continue.
        assert len(decide_calls) == 1
        assert decide_calls[0]["action"] == "continue"
        assert decide_calls[0]["handoff_id"].startswith("cross_plan:")
        # ``continue`` does not carry feedback by design.
        assert decide_calls[0].get("feedback") is None

    def test_retry_feedback_decision_passes_feedback_into_decide(
        self, env: _CrossEnv, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``retry_feedback`` records the operator's feedback string in
        the SDK decide call (so the planning loop's
        ``_resume_handoff_decision`` branch can hand it to the agent on
        the retry round), then the CLI loop re-enters the runner and
        accepts whatever terminal status the resumed run produces."""
        sessions = [
            {"status": "awaiting_phase_handoff", "phase_handoff": _payload()},
            # Operator asked for retry with feedback; resumed run
            # accepts the revised plan and reaches ``done``.
            {"status": "done"},
        ]
        calls = _install_run_mock(monkeypatch, sessions)
        env.argv(monkeypatch)

        decide_calls = []

        def _fake_decide(run_id, handoff_id, action, **kwargs):
            decide_calls.append({
                "run_id": run_id, "handoff_id": handoff_id, "action": action,
                **{k: v for k, v in kwargs.items() if k in ("feedback", "note")},
            })

        monkeypatch.setattr(
            "sdk.phase_handoff.phase_handoff_decide", _fake_decide,
        )
        from types import SimpleNamespace
        monkeypatch.setattr(
            "pipeline.control.load_resume_meta",
            lambda _path: SimpleNamespace(meta={}, path=_path / "meta.json"),
        )
        monkeypatch.setattr(
            "pipeline.control.handoff_prompt.should_prompt_for_phase_handoff",
            lambda **_: True,
        )
        from pipeline.control import handoff_prompt as _hp
        operator_feedback = "Tighten the [api] contract — drop /v1 prefix."
        monkeypatch.setattr(
            _hp, "prompt_phase_handoff_action",
            lambda *_a, **_k: _hp.HandoffDecisionInput(
                action="retry_feedback",
                feedback=operator_feedback,
                note="cli retry-feedback test",
            ),
        )

        from pipeline.cross_project.cli import main
        main()  # done is terminal — exit cleanly.

        assert len(calls) == 2
        second = calls[1]
        assert second["resume_from"] is not None
        assert second.get("followup_session_seeds_per_alias") is None
        # SDK decide must carry the feedback verbatim — the planning
        # loop's retry-feedback branch reads it from the decision
        # artifact when re-prompting the agent on the resumed round.
        assert len(decide_calls) == 1
        assert decide_calls[0]["action"] == "retry_feedback"
        assert decide_calls[0]["feedback"] == operator_feedback
        assert decide_calls[0]["handoff_id"].startswith("cross_plan:")

    def test_retry_feedback_re_pause_loops_back_to_prompt(
        self, env: _CrossEnv, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the resumed run re-pauses on a fresh rejection (round 3
        of an N=4 budget), the CLI loop must prompt again rather than
        exiting. Pins that the handoff loop is a proper while-loop on
        ``status == awaiting_phase_handoff``, not a single-shot."""
        sessions = [
            {"status": "awaiting_phase_handoff", "phase_handoff": _payload()},
            # Resumed run hit another rejection round.
            {
                "status": "awaiting_phase_handoff",
                "phase_handoff": _payload(
                    id="cross_plan:cross_plan_round:3",
                    round=3,
                    loop_max_rounds=4,
                ),
            },
            # Operator finally halts the second pause.
            {"status": "halted", "halt_reason": "phase_handoff_halt"},
        ]
        calls = _install_run_mock(monkeypatch, sessions)
        env.argv(monkeypatch)

        decide_calls = []

        def _fake_decide(run_id, handoff_id, action, **kwargs):
            decide_calls.append({
                "handoff_id": handoff_id, "action": action,
                **{k: v for k, v in kwargs.items() if k in ("feedback", "note")},
            })

        monkeypatch.setattr(
            "sdk.phase_handoff.phase_handoff_decide", _fake_decide,
        )
        from types import SimpleNamespace
        monkeypatch.setattr(
            "pipeline.control.load_resume_meta",
            lambda _path: SimpleNamespace(meta={}, path=_path / "meta.json"),
        )
        monkeypatch.setattr(
            "pipeline.control.handoff_prompt.should_prompt_for_phase_handoff",
            lambda **_: True,
        )
        from pipeline.control import handoff_prompt as _hp
        # Two decisions in order: retry_feedback then halt.
        decisions = iter([
            _hp.HandoffDecisionInput(
                action="retry_feedback",
                feedback="please redo planning",
                note=None,
            ),
            _hp.HandoffDecisionInput(
                action="halt", feedback=None, note=None,
            ),
        ])
        monkeypatch.setattr(
            _hp, "prompt_phase_handoff_action",
            lambda *_a, **_k: next(decisions),
        )

        from pipeline.cross_project.cli import main
        main()  # halted is terminal — exit cleanly.

        # Three cross_pipeline calls: initial, post-retry, post-halt.
        assert len(calls) == 3, (
            f"expected 3 cross runner entries (initial + 2 resume); "
            f"got {len(calls)}"
        )
        # Two operator decisions recorded in order.
        assert [d["action"] for d in decide_calls] == [
            "retry_feedback", "halt",
        ]
        # Second handoff id matches the round-3 payload, proving the
        # loop re-read ``phase_handoff`` after the resume rather than
        # reusing the stale round-2 id.
        assert (
            decide_calls[1]["handoff_id"]
            == "cross_plan:cross_plan_round:3"
        )


# ── Phase A3 — CFA prefix dispatch ──────────────────────────────────────────


class TestCrossOwnedHandoffIdHelper:
    """``cross_plan:`` / ``cfa:`` are cross-owned; ``project:<alias>:...``
    is a project-proxy pause. Both are promptable in-process by the cross
    CLI loop. Everything else is unknown and must be rejected."""

    def test_cross_plan_prefix_routed(self) -> None:
        from pipeline.cross_project.cli import _is_cross_owned_handoff_id
        assert _is_cross_owned_handoff_id("cross_plan:cross_plan_round:1")

    def test_cfa_prefix_routed(self) -> None:
        from pipeline.cross_project.cli import _is_cross_owned_handoff_id
        assert _is_cross_owned_handoff_id("cfa:cross_final_acceptance:1")

    def test_project_prefix_is_proxy_not_cross_owned(self) -> None:
        """A project-proxy id is NOT cross-owned (its decision routes to
        the child run) but IS promptable: the cross CLI records against
        the parent id and the resume router dispatches to the child."""
        from pipeline.cross_project.cli import (
            _is_cross_owned_handoff_id,
            _is_project_proxy_handoff_id,
            _is_promptable_handoff_id,
        )
        proxy = "project:api:validate_plan:plan_round:2"
        assert not _is_cross_owned_handoff_id(proxy)
        assert _is_project_proxy_handoff_id(proxy)
        assert _is_promptable_handoff_id(proxy)

    def test_cross_owned_ids_are_promptable(self) -> None:
        from pipeline.cross_project.cli import _is_promptable_handoff_id
        assert _is_promptable_handoff_id("cross_plan:cross_plan_round:1")
        assert _is_promptable_handoff_id("cfa:cross_final_acceptance:1")

    def test_unknown_prefix_not_promptable(self) -> None:
        from pipeline.cross_project.cli import _is_promptable_handoff_id
        assert not _is_promptable_handoff_id("future_phase:something:1")
        assert not _is_promptable_handoff_id("")


class TestCrossCfaPromptDispatch:
    """Phase A3 — when the CFA gate persists a ``cfa:`` pause, the
    cross CLI prompt loop must enter the in-process prompt branch
    (same code path as cross_plan pauses), call ``phase_handoff_decide``
    with the operator's action, and re-enter the cross runner in
    checkpoint-resume mode."""

    def test_cfa_continue_decision_drives_runner_to_done(
        self, env: _CrossEnv, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End-to-end through the prompt loop with a ``cfa:`` payload.
        The continue (override) action records via SDK decide and the
        resumed cross runner reaches terminal ``done``."""
        sessions = [
            {
                "status": "awaiting_phase_handoff",
                "phase_handoff": _payload(
                    id="cfa:cross_final_acceptance:1",
                    phase="cross_final_acceptance",
                    round_extras_key="cross_final_acceptance",
                    round=1,
                    loop_max_rounds=2,
                    available_actions=[
                        "continue", "retry_feedback", "halt",
                    ],
                ),
            },
            {"status": "done"},
        ]
        calls = _install_run_mock(monkeypatch, sessions)
        env.argv(monkeypatch)

        decide_calls = []

        def _fake_decide(run_id, handoff_id, action, **kwargs):
            decide_calls.append({
                "handoff_id": handoff_id, "action": action,
                **{k: v for k, v in kwargs.items() if k in ("feedback", "note")},
            })

        monkeypatch.setattr(
            "sdk.phase_handoff.phase_handoff_decide", _fake_decide,
        )
        from types import SimpleNamespace
        monkeypatch.setattr(
            "pipeline.control.load_resume_meta",
            lambda _path: SimpleNamespace(meta={}, path=_path / "meta.json"),
        )
        monkeypatch.setattr(
            "pipeline.control.handoff_prompt.should_prompt_for_phase_handoff",
            lambda **_: True,
        )
        from pipeline.control import handoff_prompt as _hp
        monkeypatch.setattr(
            _hp, "prompt_phase_handoff_action",
            lambda *_a, **_k: _hp.HandoffDecisionInput(
                action="continue", feedback=None, note="operator override",
            ),
        )

        from pipeline.cross_project.cli import main
        main()  # done is terminal — exit cleanly.

        assert len(calls) == 2, (
            "cross runner must be re-entered exactly once after the "
            "CFA continue decision"
        )
        assert len(decide_calls) == 1
        assert decide_calls[0]["action"] == "continue"
        # CFA prefix preserved end to end into the SDK call.
        assert decide_calls[0]["handoff_id"].startswith("cfa:")
        assert decide_calls[0].get("note") == "operator override"

    def test_cfa_no_interactive_falls_through_to_exit_4(
        self, env: _CrossEnv, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Phase A invariant — ``--no-interactive`` MUST still pause
        (status=awaiting_phase_handoff, exit 4). It must NOT demote the
        pause to ``status=failed`` just because the prompt was skipped.
        Same contract as cross_plan: the off-band caller (MCP / SDK /
        scripted resume) calls ``phase_handoff_decide`` later."""
        _install_run_mock(monkeypatch, [
            {
                "status": "awaiting_phase_handoff",
                "phase_handoff": _payload(
                    id="cfa:cross_final_acceptance:1",
                    phase="cross_final_acceptance",
                    round_extras_key="cross_final_acceptance",
                ),
            },
        ])
        env.argv(monkeypatch, "--no-interactive")

        from pipeline.cross_project.cli import main
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 4

    def test_project_proxy_payload_does_not_break_out_under_tty(
        self, env: _CrossEnv, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression pin (inverse of the historical short-circuit): a
        project-proxy payload arriving at the cross CLI with a TTY
        available MUST be prompted and resumed, NOT short-circuited to
        exit 4. The aborted-prompt sentinel is the only TTY path that
        still falls through to the resumable pause."""
        proxy = _payload(id="project:api:validate_plan:plan_round:2")
        sessions = [
            {"status": "awaiting_phase_handoff", "phase_handoff": proxy},
            {"status": "done"},
        ]
        calls = _install_run_mock(monkeypatch, sessions)
        env.argv(monkeypatch)

        decide_calls = []
        monkeypatch.setattr(
            "sdk.phase_handoff.phase_handoff_decide",
            lambda run_id, handoff_id, action, **kw: decide_calls.append(
                {"handoff_id": handoff_id, "action": action},
            ),
        )
        from types import SimpleNamespace
        monkeypatch.setattr(
            "pipeline.control.load_resume_meta",
            lambda _path: SimpleNamespace(meta={}, path=_path / "meta.json"),
        )
        monkeypatch.setattr(
            "pipeline.control.handoff_prompt.should_prompt_for_phase_handoff",
            lambda **_: True,
        )
        from pipeline.control import handoff_prompt as _hp
        monkeypatch.setattr(
            _hp, "prompt_phase_handoff_action",
            lambda *_a, **_k: _hp.HandoffDecisionInput(
                action="halt", feedback=None, note=None,
            ),
        )

        from pipeline.cross_project.cli import main
        main()  # halted is terminal — no SystemExit.

        assert len(calls) == 2  # prompted + re-entered, not exit 4
        assert decide_calls == [
            {"handoff_id": "project:api:validate_plan:plan_round:2",
             "action": "halt"},
        ]
