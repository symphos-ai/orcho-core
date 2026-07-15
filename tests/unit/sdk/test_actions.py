"""Unit tests for :mod:`sdk.actions` — MCP UX A1 next_actions surface.

Pins the rules ``compute_next_actions`` implements:

* paused on phase handoff → one Action per ``available_actions``;
* halted / failed / interrupted → resume Action;
* persisted plan (``plan_source != "none"``) → from-run-plan Action;
* terminal-success / running / missing meta → empty tuple.

Tests are pure: synthetic meta dicts, no filesystem, no clock, no env.
"""
from __future__ import annotations

import re

import pytest

from sdk.actions import Action, compute_next_actions

# ── helpers ─────────────────────────────────────────────────────────────────


def _meta(
    *,
    status: str = "running",
    plan_source: str | None = None,
    handoff_id: str | None = None,
    available_actions: list[str] | None = None,
) -> dict:
    """Build a minimal meta.json projection for tests."""
    meta: dict = {"status": status}
    if plan_source is not None:
        meta["plan_source"] = plan_source
    if handoff_id is not None:
        payload: dict = {"id": handoff_id}
        if available_actions is not None:
            payload["available_actions"] = available_actions
        meta["phase_handoff"] = payload
    return meta


# ── Action dataclass ────────────────────────────────────────────────────────


class TestActionShape:
    def test_action_is_frozen(self) -> None:
        a = Action(intent="x", tool="y")
        with pytest.raises((AttributeError, Exception)):
            a.intent = "z"  # type: ignore[misc]

    def test_to_dict_round_trip(self) -> None:
        a = Action(
            intent="Spawn impl",
            tool="orcho_run_start",
            args={"from_run_plan": "run-1"},
            optional=True,
        )
        d = a.to_dict()
        assert d == {
            "intent": "Spawn impl",
            "tool": "orcho_run_start",
            "args": {"from_run_plan": "run-1"},
            "optional": True,
        }

    def test_to_dict_defensive_copy(self) -> None:
        """``to_dict`` returns a new dict so caller mutations cannot
        reach the Action's stored args."""
        original_args = {"k": "v"}
        a = Action(intent="x", tool="y", args=original_args)
        d = a.to_dict()
        d["args"]["mutated"] = True
        # The Action's args is unchanged (Mapping copy on serialize).
        assert "mutated" not in a.args

    def test_to_dict_includes_readiness_fields_when_input_is_required(self) -> None:
        action = Action(
            intent="Need input", tool="orcho_run_resume", args={"run_id": "r1"},
            optional=False, kind="operator_input_required",
            requires_operator_input=True, choices=("followup", "exit"),
            input_schema={"type": "object"}, context={"blocked": False},
        )
        assert action.to_dict() == {
            "intent": "Need input", "tool": "orcho_run_resume",
            "args": {"run_id": "r1"}, "optional": False,
            "kind": "operator_input_required", "requires_operator_input": True,
            "choices": ["followup", "exit"],
            "input_schema": {"type": "object"}, "context": {"blocked": False},
        }


# ── compute_next_actions: terminal / running / missing ──────────────────────


class TestTerminalAndRunning:
    def test_missing_meta_returns_empty(self) -> None:
        assert compute_next_actions(None, run_id="r1") == ()

    def test_non_mapping_meta_returns_empty(self) -> None:
        assert compute_next_actions(["not", "a", "dict"], run_id="r1") == ()  # type: ignore[arg-type]

    def test_missing_status_returns_empty(self) -> None:
        assert compute_next_actions({}, run_id="r1") == ()

    @pytest.mark.parametrize("status", ["done", "success", "completed"])
    def test_terminal_success_returns_empty(self, status: str) -> None:
        meta = _meta(status=status, plan_source="local")
        # Even with a persisted plan, terminal-success suppresses
        # suggestions — the workflow is done.
        assert compute_next_actions(meta, run_id="r1") == ()

    def test_running_returns_empty(self) -> None:
        meta = _meta(status="running", plan_source="local")
        # Running run: nothing actionable yet.
        assert compute_next_actions(meta, run_id="r1") == ()


# ── Paused on handoff ───────────────────────────────────────────────────────


class TestAwaitingPhaseHandoff:
    def test_emits_one_action_per_available_action(self) -> None:
        meta = _meta(
            status="awaiting_phase_handoff",
            handoff_id="validate_plan:plan_round:2",
            available_actions=["continue", "retry_feedback", "halt"],
        )
        actions = compute_next_actions(meta, run_id="r1")
        verbs = [a.args.get("action") for a in actions]
        assert verbs == ["continue", "retry_feedback", "halt"]

    def test_all_decide_actions_target_phase_handoff_decide_tool(self) -> None:
        meta = _meta(
            status="awaiting_phase_handoff",
            handoff_id="h-1",
            available_actions=["continue", "halt"],
        )
        actions = compute_next_actions(meta, run_id="r1")
        for a in actions:
            assert a.tool == "orcho_phase_handoff_decide"
            assert a.args["run_id"] == "r1"
            assert a.args["handoff_id"] == "h-1"

    def test_retry_feedback_omits_feedback_from_args(self) -> None:
        meta = _meta(
            status="awaiting_phase_handoff",
            handoff_id="h-1",
            available_actions=["retry_feedback"],
        )
        actions = compute_next_actions(meta, run_id="r1")
        assert len(actions) == 1
        retry = actions[0]
        assert retry.args["action"] == "retry_feedback"
        # ``feedback`` must be real operator input, not a placeholder
        # shipped inside machine-callable args.
        assert "feedback" not in retry.args

    def test_continue_with_waiver_surfaced_without_feedback_in_args(
        self,
    ) -> None:
        """The waiver verb is a recognised decide action. Like
        ``retry_feedback`` it requires real operator input, so its
        machine-callable args must NOT embed a placeholder feedback."""
        meta = _meta(
            status="awaiting_phase_handoff",
            handoff_id="h-1",
            available_actions=[
                "continue", "retry_feedback", "halt", "continue_with_waiver",
            ],
        )
        actions = compute_next_actions(meta, run_id="r1")
        verbs = [a.args.get("action") for a in actions]
        assert verbs == [
            "continue", "retry_feedback", "halt", "continue_with_waiver",
        ]
        waiver = next(
            a for a in actions if a.args.get("action") == "continue_with_waiver"
        )
        assert waiver.tool == "orcho_phase_handoff_decide"
        assert waiver.intent  # has a human-readable intent
        assert "feedback" not in waiver.args

    def test_unknown_action_verb_is_silently_skipped(self) -> None:
        """A future verb the SDK doesn't know about must not crash
        clients holding stale payloads."""
        meta = _meta(
            status="awaiting_phase_handoff",
            handoff_id="h-1",
            available_actions=["continue", "future_verb_unknown"],
        )
        actions = compute_next_actions(meta, run_id="r1")
        verbs = [a.args.get("action") for a in actions]
        assert "continue" in verbs
        assert "future_verb_unknown" not in verbs

    def test_missing_handoff_id_returns_empty(self) -> None:
        # awaiting_phase_handoff but the active payload has no id —
        # corrupted state; we do not invent suggestions.
        meta = {
            "status": "awaiting_phase_handoff",
            "phase_handoff": {"available_actions": ["halt"]},
        }
        assert compute_next_actions(meta, run_id="r1") == ()

    def test_paused_with_persisted_plan_also_suggests_from_run_plan(
        self,
    ) -> None:
        """The plan profile's normal stop: paused on handoff AND a
        plan was persisted. Both decide actions AND from-run-plan
        should surface — the operator could decide here OR spawn a
        child run from the plan immediately."""
        meta = _meta(
            status="awaiting_phase_handoff",
            plan_source="local",
            handoff_id="h-1",
            available_actions=["continue", "halt"],
        )
        actions = compute_next_actions(meta, run_id="r1")
        tools = [a.tool for a in actions]
        assert tools.count("orcho_phase_handoff_decide") == 2
        # from-run-plan suggestion is appended after decide actions.
        assert "orcho_run_start" in tools
        from_run_plan = next(a for a in actions if a.tool == "orcho_run_start")
        assert from_run_plan.args["from_run_plan"] == "r1"


# ── Resumable terminal states ───────────────────────────────────────────────


class TestResumableTerminal:
    @pytest.mark.parametrize(
        "status", ["halted", "failed", "interrupted"],
    )
    def test_resumable_state_suggests_resume(self, status: str) -> None:
        meta = _meta(status=status)
        actions = compute_next_actions(meta, run_id="r1")
        assert len(actions) == 1
        assert actions[0].tool == "orcho_run_resume"
        assert actions[0].args == {"run_id": "r1"}

    def test_halted_with_plan_also_suggests_from_run_plan(self) -> None:
        meta = _meta(status="halted", plan_source="local")
        actions = compute_next_actions(meta, run_id="r1")
        tools = [a.tool for a in actions]
        assert "orcho_run_resume" in tools
        assert "orcho_run_start" in tools


# ── Provider-access recovery projection (ADR 0101 / T3) ─────────────────────


def _provider_access_meta(
    *,
    status: str = "failed",
    failed_phase: str = "plan",
    recovery_actions: list[dict] | None = None,
    plan_source: str | None = None,
) -> dict:
    """A terminal provider-access failure meta with the T1 recovery record."""
    if recovery_actions is None:
        recovery_actions = [
            {"action": "retry"},
            {"action": "halt"},
            {"action": "replace", "runtime": "codex", "model": "gpt-5.5"},
        ]
    failure = {
        "phase": failed_phase,
        "failure_kind": "provider_access",
        "recoverable": False,
        "recommended_action": "switch_runtime_or_restore_access",
        "failed_phase": failed_phase,
        "runtime": "claude",
        "model": "claude-opus-4-8",
        "recovery_actions": recovery_actions,
    }
    meta: dict = {"status": status, "failure": failure}
    if plan_source is not None:
        meta["plan_source"] = plan_source
    return meta


class TestProviderAccessRecovery:
    def test_retry_action_present_with_run_id(self) -> None:
        actions = compute_next_actions(_provider_access_meta(), run_id="r1")
        resumes = [a for a in actions if a.tool == "orcho_run_resume"]
        retry = [a for a in resumes if "runtime_override" not in a.args]
        assert len(retry) == 1
        assert retry[0].args == {"run_id": "r1"}

    def test_one_replace_action_per_candidate_with_run_id_and_override(
        self,
    ) -> None:
        meta = _provider_access_meta(
            recovery_actions=[
                {"action": "retry"},
                {"action": "halt"},
                {"action": "replace", "runtime": "codex", "model": "gpt-5.5"},
                {"action": "replace", "runtime": "gemini", "model": "g-2"},
            ],
        )
        actions = compute_next_actions(meta, run_id="r1")
        replaces = [
            a for a in actions
            if a.tool == "orcho_run_resume" and "runtime_override" in a.args
        ]
        assert len(replaces) == 2
        for a in replaces:
            # run_id mandatory on every replace Action.
            assert a.args["run_id"] == "r1"
            ov = a.args["runtime_override"]
            assert set(ov) == {"phase", "runtime", "model"}
            assert ov["phase"] == "plan"
        pairs = {(a.args["runtime_override"]["runtime"],
                  a.args["runtime_override"]["model"]) for a in replaces}
        assert pairs == {("codex", "gpt-5.5"), ("gemini", "g-2")}

    def test_replace_action_args_match_resume_contract(self) -> None:
        """The replace Action args are EXACTLY {run_id, runtime_override:
        {phase, runtime, model}} — the shape RunService.resume accepts (T2)."""
        actions = compute_next_actions(_provider_access_meta(), run_id="r1")
        replace = next(
            a for a in actions
            if a.tool == "orcho_run_resume" and "runtime_override" in a.args
        )
        assert replace.args == {
            "run_id": "r1",
            "runtime_override": {
                "phase": "plan", "runtime": "codex", "model": "gpt-5.5",
            },
        }
        assert replace.optional is True

    def test_no_candidates_yields_only_retry(self) -> None:
        meta = _provider_access_meta(
            recovery_actions=[{"action": "retry"}, {"action": "halt"}],
        )
        actions = compute_next_actions(meta, run_id="r1")
        resumes = [a for a in actions if a.tool == "orcho_run_resume"]
        assert len(resumes) == 1
        assert resumes[0].args == {"run_id": "r1"}

    def test_halt_is_not_projected_as_action(self) -> None:
        """``halt`` stays a meta-only recovery option — never a SDK Action and
        never an invented halt tool."""
        meta = _provider_access_meta()
        actions = compute_next_actions(meta, run_id="r1")
        # No Action carries a halt verb or a halt-shaped tool.
        for a in actions:
            assert "halt" not in a.tool
            assert a.args.get("action") != "halt"
        # But halt remains in the durable recovery record.
        halt_opts = [
            o for o in meta["failure"]["recovery_actions"]
            if o.get("action") == "halt"
        ]
        assert len(halt_opts) == 1

    def test_no_duplicate_flat_resume_action(self) -> None:
        """The provider-access branch replaces the flat resume Action — there
        is exactly one bare ``orcho_run_resume {run_id}`` (the retry), not two."""
        actions = compute_next_actions(_provider_access_meta(), run_id="r1")
        bare = [
            a for a in actions
            if a.tool == "orcho_run_resume" and a.args == {"run_id": "r1"}
        ]
        assert len(bare) == 1

    @pytest.mark.parametrize("status", ["failed", "halted", "interrupted"])
    def test_projection_across_resumable_statuses(self, status: str) -> None:
        meta = _provider_access_meta(status=status)
        actions = compute_next_actions(meta, run_id="r1")
        tools = [a.tool for a in actions]
        # retry + one replace.
        assert tools.count("orcho_run_resume") == 2

    def test_non_provider_access_failure_uses_flat_resume(self) -> None:
        """A failure block without provider_access kind keeps the flat resume
        Action (no recovery projection)."""
        meta = {
            "status": "failed",
            "failure": {"phase": "implement", "type": "RuntimeError"},
        }
        actions = compute_next_actions(meta, run_id="r1")
        assert len(actions) == 1
        assert actions[0].args == {"run_id": "r1"}

    def test_provider_access_still_appends_from_run_plan(self) -> None:
        """Recovery actions and the from-run-plan suggestion coexist when a
        plan was persisted."""
        meta = _provider_access_meta(plan_source="local")
        actions = compute_next_actions(meta, run_id="r1")
        tools = [a.tool for a in actions]
        assert "orcho_run_start" in tools
        assert tools.count("orcho_run_resume") == 2  # retry + replace


# ── from-run-plan: plan_source variants ────────────────────────────────────


class TestFromRunPlanRule:
    @pytest.mark.parametrize(
        "plan_source", ["local", "run", "cross"],
    )
    def test_persisted_plan_sources_suggest_from_run_plan(
        self, plan_source: str,
    ) -> None:
        """Any non-none plan_source means the run produced a durable
        parsed_plan.json suitable for follow-up."""
        meta = _meta(status="halted", plan_source=plan_source)
        actions = compute_next_actions(meta, run_id="r1")
        from_run_plans = [a for a in actions if a.tool == "orcho_run_start"]
        assert len(from_run_plans) == 1
        assert from_run_plans[0].args["from_run_plan"] == "r1"
        assert from_run_plans[0].args["profile"] == "feature"

    def test_plan_source_none_does_not_suggest_from_run_plan(self) -> None:
        meta = _meta(status="halted", plan_source="none")
        actions = compute_next_actions(meta, run_id="r1")
        tools = [a.tool for a in actions]
        assert "orcho_run_start" not in tools

    def test_missing_plan_source_does_not_suggest_from_run_plan(self) -> None:
        """Defensive: missing key treated as no-persisted-plan."""
        meta = _meta(status="halted")
        actions = compute_next_actions(meta, run_id="r1")
        tools = [a.tool for a in actions]
        assert "orcho_run_start" not in tools

    def test_missing_parent_task_requires_operator_input(self) -> None:
        action = next(
            action for action in compute_next_actions(
                _meta(status="halted", plan_source="local"), run_id="r1",
            ) if action.tool == "orcho_run_start"
        )
        assert action.kind == "operator_input_required"
        assert action.requires_operator_input is True
        assert action.input_schema == {
            "type": "object",
            "required": ["task"],
            "properties": {"task": {"type": "string", "minLength": 1}},
        }

    def test_parent_task_makes_plan_action_ready(self) -> None:
        meta = _meta(status="halted", plan_source="local")
        meta["task"] = "Implement the approved plan"
        action = next(
            action for action in compute_next_actions(meta, run_id="r1")
            if action.tool == "orcho_run_start"
        )
        assert action.kind == "ready_call"
        assert action.args["task"] == "Implement the approved plan"


# ── plan-artifact continuation semantics + legacy-name regression ───────────


# Legacy profile names retired for the semantic work-kind scheme. None of
# them may appear in any follow-up / from-run-plan operator diagnostic or
# suggested action (intent text or the ``profile`` arg value).
_LEGACY_PROFILE_NAMES: frozenset[str] = frozenset({
    "advanced",
    "lite",
    "enterprise",
    "plan",
    "review",
})


class TestPlanArtifactContinuationSemantics:
    def test_from_run_plan_action_reflects_plan_artifact_continuation(
        self,
    ) -> None:
        """The plan-only success path suggests a 'plan artifact continuation',
        not a 'parent worktree missing' recovery, and uses a semantic profile."""
        meta = _meta(status="awaiting_phase_handoff", plan_source="local")
        actions = compute_next_actions(meta, run_id="r1")
        from_run_plan = [a for a in actions if a.tool == "orcho_run_start"]
        assert len(from_run_plan) == 1
        action = from_run_plan[0]
        assert "plan artifact continuation" in action.intent.lower()
        # The success-path suggestion never frames itself as a missing-worktree
        # recovery — that wording belongs only to the genuine block path.
        assert "worktree missing" not in action.intent.lower()
        assert "parent worktree" not in action.intent.lower()
        assert action.args["profile"] == "feature"

    @pytest.mark.parametrize("plan_source", ["local", "run", "cross"])
    def test_no_legacy_profile_names_in_from_run_plan_diagnostics(
        self, plan_source: str,
    ) -> None:
        """No retired profile name leaks into the suggested action's text or
        its ``profile`` arg, across every persisted-plan source."""
        meta = _meta(status="halted", plan_source=plan_source)
        actions = compute_next_actions(meta, run_id="r1")
        from_run_plan = [a for a in actions if a.tool == "orcho_run_start"]
        assert len(from_run_plan) == 1
        action = from_run_plan[0]
        # The ``profile`` arg must be a semantic work kind, never a legacy name.
        assert action.args.get("profile") not in _LEGACY_PROFILE_NAMES
        # The unambiguous legacy tokens must not appear in the intent prose.
        intent_words = set(re.findall(r"[a-z_]+", action.intent.lower()))
        assert intent_words.isdisjoint({"advanced", "lite", "enterprise"})


# ── stalled-command recovery: dual source (T1) ──────────────────────────────


def _stalled_failure_meta(
    *,
    status: str = "failed",
    failed_phase: str = "implement",
    reason: str = "silent_child_command",
) -> dict:
    """A terminal stalled-command failure meta with the durable recovery record."""
    return {
        "status": status,
        "failure": {
            "phase": failed_phase,
            "failure_kind": "stalled_command",
            "recoverable": True,
            "recommended_action": "interrupt_resume_or_halt",
            "failed_phase": failed_phase,
            "reason": reason,
            "elapsed_s": 120.0,
            "recovery_actions": [
                {"action": "interrupt"},
                {"action": "resume_from_checkpoint"},
                {"action": "halt"},
            ],
        },
    }


class _LiveStallDiag:
    """Duck-typed stand-in for ``StalledCommandRecovery`` (only the fields
    ``compute_next_actions`` reads)."""

    def __init__(self, recovery_actions: tuple[str, ...]) -> None:
        self.recovery_actions = recovery_actions


class TestTerminalStallRecovery:
    def test_terminal_stall_projects_single_resume(self) -> None:
        actions = compute_next_actions(_stalled_failure_meta(), run_id="r1")
        resumes = [a for a in actions if a.tool == "orcho_run_resume"]
        assert len(resumes) == 1
        assert resumes[0].args == {"run_id": "r1"}
        assert "stall" in resumes[0].intent.lower()

    def test_terminal_stall_no_duplicate_flat_resume(self) -> None:
        """The stall branch replaces the flat resume — never both."""
        actions = compute_next_actions(_stalled_failure_meta(), run_id="r1")
        resumes = [a for a in actions if a.tool == "orcho_run_resume"]
        assert len(resumes) == 1

    def test_terminal_stall_does_not_project_halt_or_cancel(self) -> None:
        actions = compute_next_actions(_stalled_failure_meta(), run_id="r1")
        tools = {a.tool for a in actions}
        assert "orcho_run_cancel" not in tools


class TestLiveNonTerminalStall:
    def test_running_with_live_diag_projects_interrupt(self) -> None:
        meta = {"status": "running"}
        diags = [_LiveStallDiag(("interrupt", "resume_from_checkpoint", "halt"))]
        actions = compute_next_actions(
            meta, run_id="r1", live_stall_diagnostics=diags,
        )
        cancels = [a for a in actions if a.tool == "orcho_run_cancel"]
        assert len(cancels) == 1
        assert cancels[0].args == {"run_id": "r1"}
        # Non-empty, bounded recovery set for the non-terminal case.
        assert actions

    def test_running_without_diag_stays_empty(self) -> None:
        assert compute_next_actions({"status": "running"}, run_id="r1") == ()

    def test_live_diag_does_not_make_run_resumable(self) -> None:
        """A live non-terminal diagnostic never adds a resume / from-plan
        action — the run is still running, not terminal."""
        meta = {"status": "running"}
        diags = [_LiveStallDiag(("interrupt", "resume_from_checkpoint", "halt"))]
        actions = compute_next_actions(
            meta, run_id="r1", live_stall_diagnostics=diags,
        )
        tools = {a.tool for a in actions}
        assert tools == {"orcho_run_cancel"}

    def test_terminal_run_ignores_live_diags(self) -> None:
        """Live diagnostics are only projected while running — a failed run
        does not gain an interrupt action from stale live events."""
        diags = [_LiveStallDiag(("interrupt",))]
        actions = compute_next_actions(
            _stalled_failure_meta(), run_id="r1", live_stall_diagnostics=diags,
        )
        assert "orcho_run_cancel" not in {a.tool for a in actions}


# ── ADR 0104: merged status override + conservative from_run_plan ───────────


class TestMergedStatusOverrideAndFromRunPlan:
    def test_failed_setup_child_no_from_run_plan_without_artifact(self) -> None:
        """A failed setup-child stamped plan_source='run' but with no physical
        parsed_plan.json (has_parsed_plan_artifact=False) gets resume but NOT a
        false from_run_plan suggestion."""
        meta = {"status": "failed", "plan_source": "run"}
        actions = compute_next_actions(
            meta, run_id="r1", has_parsed_plan_artifact=False,
        )
        tools = {a.tool for a in actions}
        assert "orcho_run_resume" in tools
        assert "orcho_run_start" not in tools

    def test_from_run_plan_present_when_artifact_exists(self) -> None:
        """Positive control: with the physical artifact present, from_run_plan
        stays available alongside resume."""
        meta = {"status": "failed", "plan_source": "run"}
        actions = compute_next_actions(
            meta, run_id="r1", has_parsed_plan_artifact=True,
        )
        starts = [a for a in actions if a.tool == "orcho_run_start"]
        assert len(starts) == 1
        assert starts[0].args["from_run_plan"] == "r1"
        assert any(a.tool == "orcho_run_resume" for a in actions)

    def test_status_override_drives_resumable_branch(self) -> None:
        """A merged status override (launcher resolved the terminal state while
        meta.status was empty) drives the resumable branch — resume present, no
        from_run_plan without the artifact."""
        meta = {"status": "", "plan_source": "run"}
        actions = compute_next_actions(
            meta, run_id="r1", has_parsed_plan_artifact=False,
            status="interrupted",
        )
        tools = {a.tool for a in actions}
        assert "orcho_run_resume" in tools
        assert "orcho_run_start" not in tools

    def test_status_override_none_falls_back_to_meta_status(self) -> None:
        """status=None preserves the legacy behaviour (derive from meta)."""
        meta = {"status": "failed", "plan_source": "run"}
        with_override = compute_next_actions(
            meta, run_id="r1", has_parsed_plan_artifact=True, status=None,
        )
        without = compute_next_actions(
            meta, run_id="r1", has_parsed_plan_artifact=True,
        )
        assert [a.to_dict() for a in with_override] == [
            a.to_dict() for a in without
        ]

    def test_status_override_is_noop_for_provider_access(self) -> None:
        """Passing the merged status (== terminal meta.status) leaves the
        provider-access projection unchanged (retry + one replace)."""
        meta = _provider_access_meta(status="failed")
        overridden = compute_next_actions(meta, run_id="r1", status="failed")
        plain = compute_next_actions(meta, run_id="r1")
        assert [a.to_dict() for a in overridden] == [
            a.to_dict() for a in plain
        ]

    def test_status_override_is_noop_for_stalled_command(self) -> None:
        """Same no-op guarantee for the stalled-command recovery projection."""
        meta = _stalled_failure_meta()
        overridden = compute_next_actions(meta, run_id="r1", status="failed")
        plain = compute_next_actions(meta, run_id="r1")
        assert [a.to_dict() for a in overridden] == [
            a.to_dict() for a in plain
        ]
