"""
Adapter handlers for the linear profile.

Each handler is exercised in isolation with mock agents, then a final
end-to-end test runs the full ``linear`` profile through ``run_profile``.
The end-to-end test is the real proof that the runtime + adapters are
hooked up correctly.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agents.command_guard import ORCHO_GUARDRAIL_BLOCKED
from agents.entities import SubTask
from agents.registry import AgentRegistry
from core.infra.paths import CONFIG_DIR as _CONFIG_DIR
from pipeline.engine.declared_write_scope import DECLARED_WRITE_SCOPE_EXTRAS_KEY
from pipeline.lifecycle import default_lifecycle_context
from pipeline.phases.builtin import (
    default_registry,
    register_builtin_phases,
)
from pipeline.plan_parser import ParsedPlan
from pipeline.plugins import PluginConfig
from pipeline.profiles.loader import load_profiles_v2
from pipeline.quality_gates import (
    QualityGateRegistry,
    QualityGateResult,
)
from pipeline.runtime import (
    GateKind,
    PhaseRegistry,
    PipelineProfile,
    PipelineState,
    run_profile,
)

# ── Fakes ─────────────────────────────────────────────────────────────────────

def _approved_review(summary: str = "No blocking issues.") -> str:
    return json.dumps({
        "verdict": "APPROVED",
        "short_summary": summary,
        "findings": [],
        "risks": [],
        "checks": ["Reviewed change"],
    })


def _approved_release(summary: str = "Ship-ready.") -> str:
    """ADR 0025: release-gate APPROVED payload for final_acceptance fakes."""
    return json.dumps({
        "verdict":            "APPROVED",
        "ship_ready":         True,
        "short_summary":      summary,
        "release_blockers":   [],
        "verification_gaps":  [],
        "contract_status": {
            "task_contract": "satisfied",
            "interfaces":    "not_applicable",
            "persistence":   "not_applicable",
            "tests":         "sufficient",
        },
    })


def _rejected_release(
    summary: str,
    *,
    why_blocks: str = "Production callers depend on the prior shape.",
) -> str:
    """ADR 0025: release-gate REJECTED payload."""
    return json.dumps({
        "verdict":       "REJECTED",
        "ship_ready":    False,
        "short_summary": summary,
        "release_blockers": [{
            "id":                 "R1",
            "severity":           "P1",
            "title":              "Release blocker",
            "body":               summary,
            "required_fix":       f"Address: {summary}",
            "why_blocks_release": why_blocks,
        }],
        "verification_gaps":  [],
        "contract_status": {
            "task_contract": "incomplete",
            "interfaces":    "broken",
            "persistence":   "safe",
            "tests":         "weak",
        },
    })


def _rejected_review(body: str, *, summary: str | None = None) -> str:
    return json.dumps({
        "verdict": "REJECTED",
        "short_summary": summary or f"P2: {body}",
        "findings": [{
            "id": "F1",
            "severity": "P2",
            "title": "Review finding",
            "body": body,
            "required_fix": f"Address: {body}",
        }],
        "risks": [],
        "checks": ["Reviewed change"],
    })

class _FakeArchitect:
    """IAgentRuntime fake for plan / hypothesis paths.

    Captures ``continue_session`` per call so the round-resume policy
    tests can assert the architect's bridge gets resumed on round 2+.
    """
    def __init__(self, output: str = "PLAN MD"):
        self._output = output
        self.model = "fake-architect"
        self.session_id: str | None = None
        self.calls: list[tuple[str, str]] = []
        self.kwargs_log: list[dict] = []

    def invoke(
        self,
        prompt: str,
        cwd: str,
        *,
        mutates_artifacts: bool = False,
        continue_session: bool = False,
        attachments: tuple = (),
    ) -> str:
        del attachments
        self.calls.append((prompt, cwd))
        self.kwargs_log.append({
            "mutates_artifacts": mutates_artifacts,
            "continue_session": continue_session,
        })
        return self._output

    def reset_session(self) -> None:
        self.session_id = None


class _FakeDeveloper:
    """IAgentRuntime fake that records continue_session for the implement chain."""
    def __init__(self, output: str = "build done"):
        self._output = output
        self.model = "fake-developer"
        self.session_id: str | None = "sess-123"
        self.calls: list[tuple[str, str, dict]] = []

    def invoke(
        self,
        prompt: str,
        cwd: str,
        *,
        mutates_artifacts: bool = False,
        continue_session: bool = False,
        attachments: tuple = (),
    ) -> str:
        del attachments
        self.calls.append((
            prompt, cwd,
            {"continue_session": continue_session,
             "mutates_artifacts": mutates_artifacts},
        ))
        # P7: close the done-criteria attestation for a criteria-bearing
        # subtask_dag prompt, exactly as the production mock does. Returns the
        # bare output unchanged for whole_plan / criteria-less prompts.
        from agents.runtimes._strategy import _mock_subtask_attestation
        return self._output + _mock_subtask_attestation(prompt)

    def reset_session(self) -> None:
        self.session_id = None


class _MeteredFakeDeveloper(_FakeDeveloper):
    def __init__(self, output: str = "subtask done"):
        super().__init__(output)
        self._call_no = 0

    def invoke(
        self,
        prompt: str,
        cwd: str,
        *,
        mutates_artifacts: bool = False,
        continue_session: bool = False,
        attachments: tuple = (),
    ) -> str:
        self._call_no += 1
        self.last_tokens_in = 1_000 * self._call_no
        self.last_tokens_out = 100 * self._call_no
        self.last_tokens_total = self.last_tokens_in + self.last_tokens_out + 7
        self.last_tool_use_count = self._call_no
        self.last_cost_usd = 0.01 * self._call_no
        return super().invoke(
            prompt,
            cwd,
            mutates_artifacts=mutates_artifacts,
            continue_session=continue_session,
            attachments=attachments,
        )


def _subtask_attestation_tail(prompt: str, *, all_met: bool) -> str:
    """Build a per-subtask attestation tail with controllable met/unmet.

    Mirrors ``agents.runtimes._strategy._mock_subtask_attestation`` but lets the
    caller decide met/unmet per invocation so a fake developer can stay
    INCOMPLETE on early passes and close on a later repair pass. An unmet
    attestation is parseable (so the runner marks the subtask INCOMPLETE rather
    than firing the in-subtask "unparseable" repair turn).
    """
    from agents.runtimes._strategy import (
        _SUBTASK_HEADER_RE,
        _extract_subtask_done_criteria,
    )

    header = _SUBTASK_HEADER_RE.search(prompt)
    if header is None:
        return ""
    criteria = _extract_subtask_done_criteria(prompt)
    if not criteria:
        return ""
    subtask_id = header.group(1)
    unmet_index = None if all_met else len(criteria)
    attestation = {
        "type": "subtask_attestation",
        "subtask_id": subtask_id,
        "criteria": [
            {
                "index": i,
                "criterion": text,
                "met": i != unmet_index,
                "evidence": "left unmet" if i == unmet_index else "satisfied",
            }
            for i, text in enumerate(criteria, start=1)
        ],
        "summary": f"mock subtask {subtask_id}",
    }
    return "\n\n" + json.dumps(attestation, ensure_ascii=False)


class _RepairMeteredDeveloper(_FakeDeveloper):
    """Metered developer whose per-subtask attestation closes on a later pass.

    ``done_on_call`` maps ``subtask_id`` → the 1-based per-subtask invocation
    number on which it finally reports all criteria met. Earlier invocations
    stay INCOMPLETE, so a multi-attempt ADR-0073 substance repair drives the
    subtask to ``done`` across passes. ``session_id=None`` keeps every render
    full (no delta), so the executable-subtask header + criteria are always on
    the wire and the attestation tail can be built deterministically.
    """

    def __init__(self, done_on_call: dict[str, int]):
        super().__init__("subtask work")
        self.session_id = None
        self._done_on_call = done_on_call
        self._subtask_calls: dict[str, int] = {}
        self._call_no = 0

    def invoke(
        self,
        prompt: str,
        cwd: str,
        *,
        mutates_artifacts: bool = False,
        continue_session: bool = False,
        attachments: tuple = (),
    ) -> str:
        del attachments
        self._call_no += 1
        self.last_tokens_in = 1_000
        self.last_tokens_out = 100
        self.last_tokens_total = 1_100
        self.last_tool_use_count = 1
        self.last_cost_usd = 0.01
        self.calls.append((
            prompt, cwd,
            {"continue_session": continue_session,
             "mutates_artifacts": mutates_artifacts},
        ))
        from agents.runtimes._strategy import _SUBTASK_HEADER_RE
        header = _SUBTASK_HEADER_RE.search(prompt)
        if header is None:
            return self._output
        sid = header.group(1)
        n = self._subtask_calls.get(sid, 0) + 1
        self._subtask_calls[sid] = n
        all_met = n >= self._done_on_call.get(sid, 1)
        return self._output + _subtask_attestation_tail(prompt, all_met=all_met)


def _agent_registry(agent: Any | None = None) -> AgentRegistry:
    registry = AgentRegistry()
    registry.register("claude", lambda model, _effort=None: agent or _FakeDeveloper())
    return registry


def _parsed_plan(*subtasks: SubTask) -> ParsedPlan:
    return ParsedPlan(
        short_summary="plan",
        planning_context="plan",
        subtasks=tuple(subtasks),
        source="test",
    )


class _PassingGate:
    def execute(self, gate, state, cwd) -> QualityGateResult:
        return QualityGateResult(
            name=gate.name,
            passed=True,
            output="ok",
            duration_s=0.0,
            kind=GateKind.COMPUTATIONAL,
        )


def _passing_quality_gates() -> QualityGateRegistry:
    registry = QualityGateRegistry()
    registry.register("tests", _PassingGate())
    return registry


class _FakeReviewer:
    """IAgentRuntime fake; records review prompts and kwargs.

    ``kwargs_log`` captures ``continue_session`` per call so the
    round-resume policy tests can assert that round 2+ of a review
    loop resumes the reviewer's bridge.
    """
    def __init__(self, critique: str | None = None):
        self._critique = critique or _approved_review()
        self.model = "fake-reviewer"
        self.session_id: str | None = None
        self.calls: list[tuple[str, str]] = []
        self.kwargs_log: list[dict] = []

    def invoke(
        self,
        prompt: str,
        cwd: str,
        *,
        mutates_artifacts: bool = False,
        continue_session: bool = False,
        attachments: tuple = (),
    ) -> str:
        del attachments
        self.calls.append((prompt, cwd))
        self.kwargs_log.append({
            "mutates_artifacts": mutates_artifacts,
            "continue_session": continue_session,
        })
        return self._critique

    def reset_session(self) -> None:
        self.session_id = None

    # Back-compat accessors. Old tests unpacked ``(cwd, focus)`` from
    # ``review_uncommitted`` and ``(path, focus, cwd)`` from ``review_file``.
    # Post-collapse the runner calls ``invoke(prompt, cwd)`` and the
    # focus + path are folded into the composed prompt. We expose the
    # prompt as the second tuple element so the assertions still target it.
    @property
    def uncommitted_calls(self) -> list[tuple[str, str]]:
        return [(cwd, prompt) for (prompt, cwd) in self.calls]

    @property
    def file_calls(self) -> list[tuple[str, str, str]]:
        return [("", prompt, cwd) for (prompt, cwd) in self.calls]


class _SequenceReviewer(_FakeReviewer):
    """Reviewer fake that returns a scripted response per invocation."""

    def __init__(self, responses: list[str]):
        super().__init__(responses[-1])
        self._responses = list(responses)

    def invoke(
        self,
        prompt: str,
        cwd: str,
        *,
        mutates_artifacts: bool = False,
        continue_session: bool = False,
        attachments: tuple = (),
    ) -> str:
        self.calls.append((prompt, cwd))
        self.kwargs_log.append({
            "mutates_artifacts": mutates_artifacts,
            "continue_session": continue_session,
        })
        return self._responses.pop(0)


def _valid_plan_md(summary: str = "PLAN OUT") -> str:
    payload = {
        "short_summary": summary,
        "planning_context": summary,
        "tasks": [{"id": "t1", "goal": "Do the planned work"}],
    }
    return json.dumps(payload)


@dataclass
class _StubPhaseConfig:
    """Mirrors the shape of PhaseAgentConfig that handlers read from."""
    plan_agent:         Any = None
    validate_plan_agent:      Any = None
    implement_agent:        Any = None
    review_changes_agent:       Any = None
    repair_changes_agent:          Any = None
    repair_escalation_agent: Any = None
    final_acceptance_agent:     Any = None


def _lifecycle_ctx_with_continuity(continuity: str = "same_zone_continue"):
    """A real lifecycle context whose active step declares ``continuity``.

    ADR 0113: phase handlers resolve session continuity off the active step's
    execution policy. These direct-handler unit tests bypass the FSM (which
    seeds ``active_step`` from the profile in production), so the helper seeds it
    here. ``same_zone_continue`` is the no-raise default that matches
    implement/repair; non-edit phases (plan / validate / review) resolve fresh
    under it regardless, and the behavioural round-resume tests override it with
    the phase's real continuity (loop_continue / fresh_only).
    """
    ctx = default_lifecycle_context(phase_registry=PhaseRegistry())
    ctx.active_step = SimpleNamespace(
        prompt=None,
        execution_policy=SimpleNamespace(
            session_split=None, session_continuity=continuity
        ),
    )
    return ctx


def _state(**kw) -> PipelineState:
    pc = kw.pop("phase_config", _StubPhaseConfig(
        plan_agent     = _FakeArchitect(_valid_plan_md("PLAN OUT")),
        validate_plan_agent  = _FakeReviewer(_approved_review("looks fine")),
        implement_agent    = _FakeDeveloper("built"),
        review_changes_agent   = _FakeReviewer(_rejected_review("nits")),
        repair_changes_agent      = _FakeDeveloper("fixed"),
        final_acceptance_agent = _FakeReviewer(_approved_release("ok")),
    ))
    plugin = kw.pop("plugin", PluginConfig())
    state = PipelineState(
        task="t", project_dir="/p", plugin=plugin,
        phase_config=pc, **kw,
    )
    state.lifecycle_ctx = _lifecycle_ctx_with_continuity()
    return state


# ── Registry wiring ───────────────────────────────────────────────────────────

class TestRegistration:
    def test_register_builtin_phases_returns_same_registry(self) -> None:
        reg = PhaseRegistry()
        out = register_builtin_phases(reg)
        assert out is reg

    def test_default_registry_has_all_linear_handlers(self) -> None:
        reg = default_registry()
        for name in ("plan", "validate_plan", "implement", "review_changes", "repair_changes", "final_acceptance"):
            assert reg.has(name), f"missing built-in handler {name!r}"

    def test_handler_count_matches_export(self) -> None:
        reg = default_registry()
        assert set(reg.names()) == {
            "plan", "validate_plan", "implement", "review_changes",
            "repair_changes", "final_acceptance", "compliance_check",
            "correction_triage",
        }


# ── Plan ──────────────────────────────────────────────────────────────────────

class TestPlanHandler:
    def test_success_materializes_declared_write_scope_with_plugin_allowance(self) -> None:
        payload = json.dumps({
            "short_summary": "scope",
            "planning_context": "scope",
            "owned_files": ["src/owned.py"],
            "allowed_modifications": ["plan.lock — generated"],
            "tasks": [{
                "id": "t1", "goal": "scope", "spec": "scope",
                "files": ["src/task.py"],
                "owned_files": ["src/task_owned.py"],
                "allowed_modifications": ["task.lock — generated"],
                "done_criteria": ["done"],
            }],
        })
        state = _state(
            plugin=PluginConfig(allowed_modifications=["plugin.lock — generated"]),
            phase_config=_StubPhaseConfig(plan_agent=_FakeArchitect(payload)),
        )

        default_registry().get("plan")(state)

        scope = state.extras[DECLARED_WRITE_SCOPE_EXTRAS_KEY]
        assert scope.patterns == (
            "plan.lock", "plugin.lock", "src/owned.py", "src/task_owned.py",
            "task.lock",
        )

    def test_successful_replan_replaces_stale_declared_write_scope(self) -> None:
        payload = json.dumps({
            "short_summary": "fresh", "planning_context": "fresh",
            "owned_files": ["fresh.py"],
            "tasks": [{"id": "fresh", "goal": "fresh"}],
        })
        state = _state(phase_config=_StubPhaseConfig(plan_agent=_FakeArchitect(payload)))
        state.extras.update({"plan_round": 2, "declared_write_scope": "stale"})
        state.last_critique = "replace the old plan"

        default_registry().get("plan")(state)

        scope = state.extras[DECLARED_WRITE_SCOPE_EXTRAS_KEY]
        assert scope.patterns == ("fresh.py",)
    def test_captures_markdown_into_state(self) -> None:
        state = _state()
        new = default_registry().get("plan")(state)
        assert new is not None
        assert "PLAN OUT" in new.plan_markdown
        assert new.phase_log["plan"]["output"] == new.plan_markdown

    def test_dry_run_emits_marker(self) -> None:
        state = _state(dry_run=True)
        new = default_registry().get("plan")(state)
        assert "[DRY RUN]" in new.plan_markdown
        # Agent must not be called in dry_run.
        agent: _FakeArchitect = state.phase_config.plan_agent
        assert agent.calls == []

    def test_first_round_injects_validated_hypothesis_as_planning_context(
        self,
    ) -> None:
        state = _state()
        state.extras["validated_hypothesis"] = (
            "The producer emits email_address while the consumer expects email."
        )

        default_registry().get("plan")(state)

        agent: _FakeArchitect = state.phase_config.plan_agent
        prompt, _cwd = agent.calls[0]
        assert "VALIDATED HYPOTHESIS (QA-approved planning context):" in prompt
        assert "This is a planning input, not execution approval." in prompt
        assert "verify/falsify its riskiest assumption early" in prompt
        assert "explain why it diverges" in prompt
        assert "email_address while the consumer expects email" in prompt
        assert state.phase_log["plan"]["hypothesis_injected"] is True
        # Approved path must NOT also flag rejected-feedback injection.
        assert state.phase_log["plan"]["hypothesis_feedback_injected"] is False

    def test_first_round_injects_rejected_hypothesis_feedback_as_negative_context(
        self,
    ) -> None:
        """When QA rejected every attempt, the reviewer's findings still
        feed PLAN — but as negative context, never as approved direction.
        """
        state = _state()
        state.extras["hypothesis_attempts"] = [{
            "attempt": 1,
            "hypothesis": "HYP_X: rename email_address to email on producer side",
            "approved": False,
            "review": {
                "verdict": "REJECTED",
                "short_summary": "SUMMARY_Y: persistence gap unaddressed",
                "findings": [{
                    "id": "F1", "severity": "P1",
                    "title": "TITLE_Z",
                    "body": "BODY_Z",
                    "required_fix": "FIX_Z",
                }],
                "risks": ["RISK_Q"],
                "checks": ["CHECK_W"],
            },
        }]

        default_registry().get("plan")(state)

        agent: _FakeArchitect = state.phase_config.plan_agent
        prompt, _cwd = agent.calls[0]
        # Rejected-feedback block landed.
        assert "REJECTED HYPOTHESIS FEEDBACK" in prompt
        assert "not validated direction" in prompt
        assert "HYP_X" in prompt
        assert "SUMMARY_Y" in prompt
        assert "TITLE_Z" in prompt
        assert "FIX_Z" in prompt
        assert "RISK_Q" in prompt
        assert "CHECK_W" in prompt
        # And NOT the approved wording.
        assert "VALIDATED HYPOTHESIS" not in prompt
        # Metadata tracks the negative-context path distinctly.
        assert state.phase_log["plan"]["hypothesis_injected"] is False
        assert state.phase_log["plan"]["hypothesis_feedback_injected"] is True

    def test_approved_direction_wins_over_rejected_feedback(self) -> None:
        """If both flags are present (defensive), approved wins and no
        rejected-feedback block is appended — the two are mutually
        exclusive per attempt.
        """
        state = _state()
        state.extras["validated_hypothesis"] = "VALIDATED_TEXT"
        state.extras["hypothesis_attempts"] = [{
            "attempt": 1,
            "hypothesis": "REJECTED_TEXT",
            "approved": False,
            "review": {"verdict": "REJECTED", "short_summary": "x", "findings": []},
        }]

        default_registry().get("plan")(state)

        agent: _FakeArchitect = state.phase_config.plan_agent
        prompt, _cwd = agent.calls[0]
        assert "VALIDATED HYPOTHESIS" in prompt
        assert "REJECTED HYPOTHESIS FEEDBACK" not in prompt
        assert state.phase_log["plan"]["hypothesis_injected"] is True
        assert state.phase_log["plan"]["hypothesis_feedback_injected"] is False

    def test_plan_success_prints_structured_block(self, capsys) -> None:
        """REA-3.6 follow-up: a successful PLAN parse prints the typed
 plan as a structured block — verdict-style summary, contract
 rows, expanded acceptance/risks, task spec/done — instead of
 relying on the raw model JSON dominating stdout."""
        payload = json.dumps({
            "short_summary": "Verify that calc.add returns a + b.",
            "planning_context": "Investigation showed the bug is fixed.",
            "goal": "Confirm calc.add returns a + b.",
            "acceptance_criteria": ["pytest exits 0 with 5 passed"],
            "owned_files": ["calc.py"],
            "commands_to_run": ["python -m pytest -q"],
            "risks": ["Don't modify calc.py"],
            "review_focus": ["calc.py:2 returns a + b"],
            "tasks": [{
                "id": "T1",
                "goal": "Run tests.",
                "spec": "Run pytest from project root.",
                "files": ["calc.py"],
                "done_criteria": ["pytest passes"],
            }],
        })
        state = _state(phase_config=_StubPhaseConfig(
            plan_agent     = _FakeArchitect(payload),
            validate_plan_agent  = _FakeReviewer(_approved_review("looks fine")),
            implement_agent    = _FakeDeveloper("built"),
            review_changes_agent   = _FakeReviewer(_rejected_review("nits")),
            repair_changes_agent      = _FakeDeveloper("fixed"),
            final_acceptance_agent = _FakeReviewer(_approved_release("ok")),
        ))
        default_registry().get("plan")(state)
        out = capsys.readouterr().out
        # Structured plan block emitted alongside.
        assert "Verify that calc.add returns a + b." in out
        assert "Acceptance Criteria" in out
        assert "pytest exits 0 with 5 passed" in out
        assert "Tasks" in out
        assert "T1" in out
        assert "Run tests." in out

    def test_plan_parse_failure_keeps_raw_output_visible(self, capsys) -> None:
        """Risk #3: when parse fails the structured preview must be
 skipped, but the raw model output AND the parse error must
 appear in stdout — not only on disk. Hiding the bad JSON
 behind the suppressor would make a halt feel silent."""
        bad = "{not valid json — missing required fields"
        state = _state(phase_config=_StubPhaseConfig(
            plan_agent     = _FakeArchitect(bad),
            validate_plan_agent  = _FakeReviewer(_approved_review("looks fine")),
            implement_agent    = _FakeDeveloper("built"),
            review_changes_agent   = _FakeReviewer(_rejected_review("nits")),
            repair_changes_agent      = _FakeDeveloper("fixed"),
            final_acceptance_agent = _FakeReviewer(_approved_release("ok")),
        ))
        new = default_registry().get("plan")(state)
        out = capsys.readouterr().out
        # Run halted with parse error and raw output preserved.
        assert new.halt is True
        assert new.phase_log["plan"]["output"] == bad
        assert "parse_error" in new.phase_log["plan"]
        # No structured plan block — the parse never produced one.
        assert "Acceptance Criteria" not in out
        assert "Tasks" not in out
        # Parse failure is visible to the operator on stdout: the
        # red-headed block, the schema error, and the raw body in full.
        assert "Parse failure" in out
        assert "PLAN" in out
        assert "Raw output" in out
        assert bad in out


# ── validate_plan ─────────────────────────────────────────────────────────────

class TestValidatePlanHandler:
    def test_approved_does_not_halt(self) -> None:
        state = _state()
        state.plan_markdown = "PLAN MD"
        new = default_registry().get("validate_plan")(state)
        assert new.phase_log["validate_plan"]["approved"] is True
        assert new.halt is False

    def test_rejected_handler_records_verdict_without_halting(self) -> None:
        """Phase 3 cutover: the handler does not halt — pause semantics
        live in the loop runner via the generic phase-handoff trigger
        machinery, driven by the profile's declared ``handoff`` policy.
        The handler only records the verdict + critique. Last-round
        pause is covered end-to-end in
        ``tests/unit/pipeline/runtime/test_handoff_trigger.py``.
        """
        pc = _StubPhaseConfig(
            validate_plan_agent=_FakeReviewer(_rejected_review("missing edge case")),
        )
        state = _state(phase_config=pc)
        state.plan_markdown = "PLAN MD"
        state.lifecycle_ctx = _lifecycle_ctx_with_continuity()
        state.extras["plan_round"] = 2
        state.extras["plan_round_max"] = 2
        new = default_registry().get("validate_plan")(state)
        # Verdict recorded.
        assert new.phase_log["validate_plan"]["approved"] is False
        assert "missing edge case" in new.last_critique
        # Handler does not halt — pause is the loop runner's job.
        assert new.halt is False
        assert new.phase_handoff_request is None

    def test_rejected_mid_loop_no_halt(self) -> None:
        """Handler never halts on rejection — replan can continue."""
        pc = _StubPhaseConfig(
            validate_plan_agent=_FakeReviewer(_rejected_review("nits")),
        )
        state = _state(phase_config=pc)
        state.plan_markdown = "PLAN MD"
        state.extras["plan_round"] = 1
        state.extras["plan_round_max"] = 2  # one more round to go
        new = default_registry().get("validate_plan")(state)
        assert new.halt is False
        assert "nits" in new.last_critique  # ready for replan

    def test_parse_error_retries_same_review_contract_once(self) -> None:
        reviewer = _SequenceReviewer([
            "No substantive findings.",
            _approved_review("Plan validation recovered through JSON retry."),
        ])
        state = _state(phase_config=_StubPhaseConfig(
            validate_plan_agent=reviewer,
        ))
        state.plan_markdown = "PLAN MD"

        new = default_registry().get("validate_plan")(state)

        assert new.halt is False
        assert new.phase_log["validate_plan"]["approved"] is True
        repair = new.phase_log["validate_plan"]["contract_repair"]
        assert repair["triggered"] is True
        assert "exactly one JSON object" in repair["original_parse_error"]
        assert repair["original_raw_output"] == "No substantive findings."
        assert "session_meta" in repair
        assert len(reviewer.calls) == 2
        retry_prompt, _cwd = reviewer.calls[1]
        assert "validate_plan" in retry_prompt
        assert "Emit exactly one JSON object with this shape" in retry_prompt
        # ADR 0113: the contract re-emit is the non-edit-shaped
        # ``format_repair`` role, so the session-disposition policy resolves
        # it to a fresh session (the prior output is embedded in the prompt).
        assert reviewer.kwargs_log[1]["continue_session"] is False

    def test_parse_error_retry_failure_preserves_retry_raw_output(self) -> None:
        reviewer = _SequenceReviewer([
            "No substantive findings.",
            "Still prose, still not JSON.",
        ])
        state = _state(phase_config=_StubPhaseConfig(
            validate_plan_agent=reviewer,
        ))
        state.plan_markdown = "PLAN MD"

        new = default_registry().get("validate_plan")(state)

        assert new.halt is True
        log = new.phase_log["validate_plan"]
        repair = log["contract_repair"]
        assert repair["failed"] is True
        assert repair["original_raw_output"] == "No substantive findings."
        assert repair["retry_raw_output"] == "Still prose, still not JSON."
        assert log["raw_output"] == "Still prose, still not JSON."
        assert "Still prose" in log["output"]


# ── Build ─────────────────────────────────────────────────────────────────────

class TestBuildHandler:
    def test_records_output_and_session_meta(self) -> None:
        state = _state()
        new = default_registry().get("implement")(state)
        log = new.phase_log["implement"]
        assert log["output"] == "built"
        assert log["meta"]["session_id"] == "sess-123"

    def test_preserves_prompt_render_for_session_adapter(self) -> None:
        """The real implement handler must not drop the trace emitted by
        ``_session_aware_invoke`` when it rewrites phase_log["implement"].
        """
        from pipeline.session_adapters import BuildAdapter

        state = _state(extras={"run_id": "run-impl-trace"})
        default_registry().get("implement")(state)

        log = state.phase_log["implement"]
        assert log["prompt_render"]["session_key"]["run_id"] == "run-impl-trace"

        session = {"phases": {}}
        BuildAdapter().write("implement", state, session)
        assert session["phases"]["implement"]["prompt_render"][
            "session_key"
        ]["run_id"] == "run-impl-trace"

    def test_guardrail_blocked_build_halts(self) -> None:
        state = _state(phase_config=_StubPhaseConfig(
            implement_agent=_FakeDeveloper(f"{ORCHO_GUARDRAIL_BLOCKED}\nblocked"),
        ))

        new = default_registry().get("implement")(state)

        assert new.halt is True
        assert "guardrail" in new.halt_reason
        assert new.phase_log["implement"]["guardrail_blocked"] is True

    def test_subtask_dag_records_receipts(self) -> None:
        agent = _FakeDeveloper("subtask done")
        state = _state(
            parsed_plan=_parsed_plan(
                SubTask(id="inspect", goal="Inspect target."),
                SubTask(
                    id="patch",
                    goal="Patch target.",
                    depends_on=("inspect",),
                    done_criteria=("target patched",),
                ),
            ),
            registry=_agent_registry(agent),
            extras={"implementation_execution": "subtask_dag"},
            phase_config=_StubPhaseConfig(implement_agent=agent),
        )

        default_registry().get("implement")(state)

        log = state.phase_log["implement"]
        assert log["meta"]["execution_mode"] == "subtask_dag"
        assert log["delivery_clean"] is True
        assert [r["subtask_id"] for r in log["implementation_receipts"]] == [
            "inspect", "patch",
        ]
        assert {r["state"] for r in log["implementation_receipts"]} == {"done"}
        assert log["progress"] == {
            "kind": "subtasks",
            "completed": 2,
            "total": 2,
        }
        assert "## subtask inspect" in log["output"]

    def test_subtask_dag_records_composite_metrics_usage(self) -> None:
        agent = _MeteredFakeDeveloper("subtask done")
        state = _state(
            parsed_plan=_parsed_plan(
                SubTask(id="inspect", goal="Inspect target."),
                SubTask(id="patch", goal="Patch target.", depends_on=("inspect",)),
            ),
            registry=_agent_registry(agent),
            extras={"implementation_execution": "subtask_dag"},
            phase_config=_StubPhaseConfig(implement_agent=agent),
        )

        default_registry().get("implement")(state)

        usage = state.phase_log["implement"]["_metrics_usage"]
        assert usage["source"] == "subtask_dag"
        assert usage["invocations"] == 2
        assert usage["tokens_in"] == 3_000
        assert usage["tokens_out"] == 300
        assert usage["tokens_total"] == 3_314
        assert usage["tool_calls"] == 3
        assert round(usage["cost_usd_equivalent"], 4) == 0.03
        assert usage["tokens_exact"] is True

    def test_subtask_dag_records_per_subtask_usage(self) -> None:
        agent = _MeteredFakeDeveloper("subtask done")
        state = _state(
            parsed_plan=_parsed_plan(
                SubTask(
                    id="inspect",
                    goal="Inspect target.",
                    files=("a.py",),
                ),
                SubTask(
                    id="patch",
                    goal="Patch target.",
                    depends_on=("inspect",),
                    files=("b.py", "c.py"),
                ),
            ),
            registry=_agent_registry(agent),
            extras={"implementation_execution": "subtask_dag"},
            phase_config=_StubPhaseConfig(implement_agent=agent),
        )

        default_registry().get("implement")(state)

        log = state.phase_log["implement"]
        records = log["subtask_metrics"]
        assert [r["subtask_id"] for r in records] == ["inspect", "patch"]
        assert all(r["state"] == "done" for r in records)
        assert records[0]["declared_files"] == ["a.py"]
        assert records[1]["declared_files"] == ["b.py", "c.py"]
        assert all(r["invocations"] == 1 for r in records)
        assert all(r["tokens_exact"] is True for r in records)
        assert all("duration_s" in r for r in records)

        # Each record carries its own provider numbers (call 1 vs call 2 of the
        # metered fake): no estimate-by-division of the phase total.
        assert records[0]["tokens_in"] == 1_000
        assert records[1]["tokens_in"] == 2_000

        # Invariant: the per-subtask records EXPLAIN the phase rollup — their
        # sums equal ``_metrics_usage`` exactly (no double counting).
        usage = log["_metrics_usage"]
        assert sum(r["total_tokens"] for r in records) == usage["tokens_total"]
        assert sum(r["tokens_in"] for r in records) == usage["tokens_in"]
        assert sum(r["tokens_out"] for r in records) == usage["tokens_out"]
        assert sum(r["tool_calls"] for r in records) == usage["tool_calls"]
        assert round(
            sum(r["cost_usd_equivalent"] for r in records), 4
        ) == round(usage["cost_usd_equivalent"], 4)

    def test_subtask_dag_multi_attempt_repair_preserves_done_state(self) -> None:
        # F1 regression: with ``repair_attempts > 1`` a subtask repaired in an
        # EARLIER pass drops out of the final pass's receipts. The durable
        # per-subtask ``state`` must still reflect its final ``done`` (overlaid
        # per pass), not the stale first-pass ``incomplete``.
        from pipeline.runtime.roles import PhaseHandoffType
        from pipeline.runtime.steps import PhaseHandoffPolicy

        # ``alpha`` closes on its 2nd invocation (main pass incomplete → repair
        # pass 1 done); ``beta`` closes on its 3rd (repair pass 2 done).
        agent = _RepairMeteredDeveloper({"alpha": 2, "beta": 3})
        state = _state(
            parsed_plan=_parsed_plan(
                SubTask(
                    id="alpha",
                    goal="Do alpha.",
                    files=("alpha.py",),
                    done_criteria=("alpha done",),
                ),
                SubTask(
                    id="beta",
                    goal="Do beta.",
                    files=("beta.py",),
                    done_criteria=("beta done",),
                ),
            ),
            registry=_agent_registry(agent),
            extras={"implementation_execution": "subtask_dag"},
            phase_config=_StubPhaseConfig(implement_agent=agent),
        )
        state.lifecycle_ctx = SimpleNamespace(
            active_step=SimpleNamespace(
                handoff=PhaseHandoffPolicy(
                    type=PhaseHandoffType.HUMAN_FEEDBACK_ALWAYS,
                    repair_attempts=2,
                    on_exhausted="halt",
                ),
                execution_policy=SimpleNamespace(
                    session_split=None,
                    session_continuity="same_zone_continue",
                ),
                prompt=None,
            ),
        )

        default_registry().get("implement")(state)

        log = state.phase_log["implement"]
        records = {r["subtask_id"]: r for r in log["subtask_metrics"]}
        # Both reach ``done`` — alpha in pass 1, beta in pass 2. Without the
        # per-pass overlay, alpha would stay ``incomplete``.
        assert records["alpha"]["state"] == "done"
        assert records["beta"]["state"] == "done"
        # Aggregated across every pass that touched each subtask.
        assert records["alpha"]["invocations"] == 2
        assert records["beta"]["invocations"] == 3

        # Sums still reconcile with the phase rollup after repair.
        usage = log["_metrics_usage"]
        assert sum(
            r["total_tokens"] for r in records.values()
        ) == usage["tokens_total"]
        assert round(
            sum(r["cost_usd_equivalent"] for r in records.values()), 4
        ) == round(usage["cost_usd_equivalent"], 4)

    def test_subtask_dag_receipts_forward_to_session_adapter(self) -> None:
        from pipeline.session_adapters import BuildAdapter

        agent = _FakeDeveloper("subtask done")
        state = _state(
            parsed_plan=_parsed_plan(SubTask(id="t1", goal="Do it.")),
            registry=_agent_registry(agent),
            extras={"implementation_execution": "subtask_dag"},
            phase_config=_StubPhaseConfig(implement_agent=agent),
        )

        default_registry().get("implement")(state)
        session = {"phases": {}}
        BuildAdapter().write("implement", state, session)

        receipts = session["phases"]["implement"]["implementation_receipts"]
        assert receipts[0]["subtask_id"] == "t1"
        assert receipts[0]["state"] == "done"

    def test_advanced_profile_runs_subtask_dag_with_concurrency_one(self) -> None:
        agent = _FakeDeveloper("subtask done")
        state = _state(
            parsed_plan=_parsed_plan(SubTask(id="t1", goal="Do it.")),
            registry=_agent_registry(agent),
            phase_config=_StubPhaseConfig(implement_agent=agent),
        )
        profile = load_profiles_v2(
            _CONFIG_DIR / "pipeline_profiles_v2.json",
        )["feature"]
        ctx = default_lifecycle_context(
            phase_registry=default_registry(),
            quality_gate_registry=_passing_quality_gates(),
        )

        run_profile(
            profile,
            state,
            default_registry(),
            ctx=ctx,
            completed_phases={
                "plan",
                "validate_plan",
                "review_changes",
                "repair_changes",
                "final_acceptance",
            },
        )

        log = state.phase_log["implement"]
        assert state.extras["implementation_execution"] == "subtask_dag"
        assert log["meta"]["execution_mode"] == "subtask_dag"
        assert log["meta"]["concurrency"] == 1
        assert log["prompt_render"]["execution_mode"] == "subtask_dag"
        assert log["prompt_render"]["wire_chars"] > 0
        assert log["implementation_receipts"][0]["subtask_id"] == "t1"
        assert log["implementation_receipts"][0]["state"] == "done"

    def test_subtask_dag_failed_subtask_blocks_delivery(self) -> None:
        class FailingDeveloper(_FakeDeveloper):
            def invoke(self, *args, **kwargs) -> str:
                super().invoke(*args, **kwargs)
                raise RuntimeError("boom")

        agent = FailingDeveloper()
        state = _state(
            parsed_plan=_parsed_plan(SubTask(id="t1", goal="Do it.")),
            registry=_agent_registry(agent),
            extras={"implementation_execution": "subtask_dag"},
            phase_config=_StubPhaseConfig(implement_agent=agent),
        )

        default_registry().get("implement")(state)

        assert state.halt is True
        assert "subtask_dag delivery blocked" in state.halt_reason
        receipts = state.phase_log["implement"]["implementation_receipts"]
        assert receipts[0]["state"] == "failed"

    def test_subtask_dag_metrics_slice_is_complete_receipt_state_mirror(self) -> None:
        # R1: ``subtask_metrics`` must be a COMPLETE state slice, not a partial
        # one. A raising invoke publishes NO metered ``last_invocation_outcome``,
        # so ``_build_subtask_usage_records`` emits no record for the failed t1;
        # t2 is skipped (unsatisfied dependency) and never invokes at all. BOTH
        # must be folded into the slice as state-only markers — otherwise a
        # slice holding only ``skipped`` would still read non-empty and make
        # finalization miscount the unmetered ``failed`` as incomplete.
        class FailingDeveloper(_FakeDeveloper):
            def invoke(self, *args, **kwargs) -> str:
                super().invoke(*args, **kwargs)
                raise RuntimeError("boom")

        agent = FailingDeveloper()
        state = _state(
            parsed_plan=_parsed_plan(
                SubTask(id="t1", goal="Do it."),
                SubTask(id="t2", goal="Then this.", depends_on=("t1",)),
            ),
            registry=_agent_registry(agent),
            extras={"implementation_execution": "subtask_dag"},
            phase_config=_StubPhaseConfig(implement_agent=agent),
        )

        default_registry().get("implement")(state)

        log = state.phase_log["implement"]
        # t1 failed, t2 skipped after the unsatisfied dependency.
        receipt_states = {
            r["subtask_id"]: r["state"]
            for r in log["implementation_receipts"]
        }
        assert receipt_states == {"t1": "failed", "t2": "skipped"}

        records = {r["subtask_id"]: r for r in log["subtask_metrics"]}
        # Every receipt state is mirrored: both the unmetered failed subtask and
        # the skipped one appear as state-only markers — no invented usage fields
        # (honesty rule), just the terminal state.
        assert records["t1"]["state"] == "failed"
        assert records["t2"]["state"] == "skipped"
        for sid in ("t1", "t2"):
            assert "tokens_in" not in records[sid]
            assert "total_tokens" not in records[sid]
            assert "invocations" not in records[sid]

    def test_subtask_dag_integrated_output_preserves_incomplete_output(self) -> None:
        from pipeline.dag_runner import DagRunResult, SubTaskResult
        from pipeline.phases.builtin.subtask_dag import _integrated_subtask_output

        result = DagRunResult(
            completed=(),
            failed=(
                SubTaskResult(
                    subtask_id="T3",
                    runtime="claude",
                    model="m",
                    skill=None,
                    output="human summary survived",
                    duration=1.0,
                    attestation_error="attestation unparseable: bad envelope",
                ),
            ),
        )

        output = _integrated_subtask_output(result)

        assert "## subtask T3 (incomplete)" in output
        assert "human summary survived" in output
        assert "attestation_error: attestation unparseable: bad envelope" in output
        assert "error: None" not in output

    def test_subtask_dag_records_failed_receipt_when_runtime_resolution_fails(self) -> None:
        state = _state(
            parsed_plan=_parsed_plan(SubTask(id="t1", goal="Do it.")),
            registry=AgentRegistry(),
            extras={"implementation_execution": "subtask_dag"},
            phase_config=_StubPhaseConfig(implement_agent=_FakeDeveloper()),
        )

        default_registry().get("implement")(state)

        log = state.phase_log["implement"]
        assert state.halt is True
        assert "subtask_dag delivery blocked" in state.halt_reason
        assert log["delivery_clean"] is False
        receipts = log["implementation_receipts"]
        assert len(receipts) == 1
        assert receipts[0]["subtask_id"] == "t1"
        assert receipts[0]["state"] == "failed"
        assert receipts[0]["runtime"] == "claude"
        assert "No agent runtime registered" in receipts[0]["error"]


# ── Review ────────────────────────────────────────────────────────────────────

class TestReviewHandler:
    def test_critique_threaded_into_state(self) -> None:
        state = _state()
        new = default_registry().get("review_changes")(state)
        assert "Critique" in new.last_critique
        assert "REJECTED" in new.last_critique
        # phase_log["review_changes"]["output"] is the rendered review markdown
        # (titled "Review"), distinct from the FIX-targeted critique block.
        review_output = new.phase_log["review_changes"]["output"]
        assert review_output.startswith("# Review")
        assert "REJECTED" in review_output
        assert new.phase_log["review_changes"]["clean"] is False
        assert new.phase_log["review_changes"]["short_summary"]

    def test_parse_error_retries_same_review_contract_once(self) -> None:
        reviewer = _SequenceReviewer([
            "No substantive findings.\n\nChecks performed:\n- npm test passed.",
            _approved_review("Review recovered through JSON retry."),
        ])
        state = _state(phase_config=_StubPhaseConfig(
            review_changes_agent=reviewer,
        ))

        new = default_registry().get("review_changes")(state)

        assert new.halt is False
        assert new.phase_log["review_changes"]["clean"] is True
        repair = new.phase_log["review_changes"]["contract_repair"]
        assert repair["triggered"] is True
        assert "exactly one JSON object" in repair["original_parse_error"]
        assert repair["original_raw_output"] == (
            "No substantive findings.\n\n"
            "Checks performed:\n- npm test passed."
        )
        assert "session_meta" in repair
        assert len(reviewer.calls) == 2
        retry_prompt, _cwd = reviewer.calls[1]
        assert "review_changes" in retry_prompt
        assert "Emit exactly one JSON object with this shape" in retry_prompt
        assert "No substantive findings" in retry_prompt
        # ADR 0113: the contract re-emit is the non-edit-shaped
        # ``format_repair`` role, so the session-disposition policy resolves
        # it to a fresh session (the prior output is embedded in the prompt).
        assert reviewer.kwargs_log[1]["continue_session"] is False

    def test_parse_error_retry_failure_preserves_retry_raw_output(self) -> None:
        reviewer = _SequenceReviewer([
            "No substantive findings.",
            "Still prose, still not JSON.",
        ])
        state = _state(phase_config=_StubPhaseConfig(
            review_changes_agent=reviewer,
        ))

        new = default_registry().get("review_changes")(state)

        assert new.halt is True
        log = new.phase_log["review_changes"]
        repair = log["contract_repair"]
        assert repair["failed"] is True
        assert repair["original_raw_output"] == "No substantive findings."
        assert repair["retry_raw_output"] == "Still prose, still not JSON."
        assert log["raw_output"] == "Still prose, still not JSON."
        assert "Still prose" in log["output"]

    def test_commit_handoff_does_not_skip_on_clean_worktree(self, monkeypatch) -> None:
        """Commit-mode review targets HEAD/task commits, so clean working
 tree is not a skip signal."""
        # ADR 0042 Phase J: ``has_uncommitted`` lives in
        # ``core.io.git_helpers``; the orchestrator no longer
        # re-exports it.
        from core.io import git_helpers as _git

        monkeypatch.setattr(_git, "has_uncommitted", lambda _cwd: False)
        state = _state(extras={"change_handoff": "commit"})
        default_registry().get("review_changes")(state)

        reviewer: _FakeReviewer = state.phase_config.review_changes_agent
        assert len(reviewer.uncommitted_calls) == 1
        _cwd, focus = reviewer.uncommitted_calls[0]
        assert "Review target mode: commit" in focus
        assert "skipped" not in state.phase_log["review_changes"]

    def test_uncommitted_handoff_does_not_mark_incomplete_implement_clean(
        self, monkeypatch,
    ) -> None:
        """A clean worktree is not a clean review when implement is blocked."""
        from core.io import git_helpers as _git

        monkeypatch.setattr(_git, "has_uncommitted", lambda _cwd: False)
        state = _state()
        state.phase_log["implement"] = {
            "output": "build",
            "delivery_status": "incomplete",
            "delivery_clean": False,
            "incomplete_subtasks": ["T3"],
        }

        default_registry().get("review_changes")(state)

        log = state.phase_log["review_changes"]
        assert log["skipped"] == "implement delivery incomplete"
        assert log["clean"] is False
        assert log["approved"] is False
        assert "no uncommitted changes" not in log["skipped"]
        assert state.phase_config.review_changes_agent.calls == []

    def test_uncommitted_handoff_reviews_accidental_commit(
        self,
        tmp_path: Path,
    ) -> None:
        """A clean worktree is not a skip signal when implement committed.

        ``change_handoff=uncommitted`` asks authoring agents to leave edits in
        the working tree. If an agent violates that and commits inside the run
        checkout, review must switch to a committed target instead of skipping
        as "no uncommitted changes".
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@orcho.invalid"],
            cwd=repo,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Orcho Test"],
            cwd=repo,
            check=True,
        )
        (repo / "app.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "app.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

        from pipeline.engine.run_diff import snapshot_worktree

        baseline = snapshot_worktree(repo)
        assert baseline
        (repo / "app.txt").write_text("base\nimplemented\n", encoding="utf-8")
        subprocess.run(["git", "add", "app.txt"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "implement change"],
            cwd=repo,
            check=True,
        )
        assert subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout == ""

        state = _state(extras={"change_handoff": "uncommitted"})
        state.project_dir = str(repo)
        state.phase_log["implement"] = {
            "output": "implemented",
            "change_baseline_ref": baseline,
        }
        state.lifecycle_ctx = _lifecycle_ctx_with_continuity()
        state.lifecycle_ctx.git_helpers.has_uncommitted = lambda _cwd: False

        default_registry().get("review_changes")(state)

        reviewer: _FakeReviewer = state.phase_config.review_changes_agent
        assert len(reviewer.uncommitted_calls) == 1
        _cwd, focus = reviewer.uncommitted_calls[0]
        assert "Review target mode: commit_set" in focus
        assert "skipped" not in state.phase_log["review_changes"]


# ── Fix ───────────────────────────────────────────────────────────────────────

class TestFixHandler:
    def test_skips_when_no_critique(self) -> None:
        state = _state()
        new = default_registry().get("repair_changes")(state)
        assert new.phase_log["repair_changes"] == {
            "skipped": "review clean"
        }
        # Fix agent NOT called.
        assert state.phase_config.repair_changes_agent.calls == []

    def test_skips_when_review_verdict_approved(self) -> None:
        state = _state()
        state.last_critique = _approved_review("Acceptance criteria met.")

        new = default_registry().get("repair_changes")(state)

        assert new.phase_log["repair_changes"] == {
            "skipped": "review clean"
        }
        assert state.phase_config.repair_changes_agent.calls == []

    def test_runs_when_critique_set_and_clears_it(self) -> None:
        state = _state()
        state.last_critique = "fix me"
        # b: handler-side escalation runs
        # unconditionally now. AUTO session mode + empty build/fix
        # model strings resolves to CHAIN → round-1 CHAIN swaps
        # repair_changes_agent to implement_agent. Force STATELESS to keep the
        # repair_changes_agent in place for this output-routing assertion.
        state.extras["session_mode_initial"] = "stateless"
        new = default_registry().get("repair_changes")(state)
        assert new.phase_log["repair_changes"]["output"] == "fixed"
        assert new.last_critique == ""  # consumed

    def test_guardrail_blocked_fix_halts(self) -> None:
        state = _state(phase_config=_StubPhaseConfig(
            repair_changes_agent=_FakeDeveloper(f"{ORCHO_GUARDRAIL_BLOCKED}\nblocked"),
        ))
        state.last_critique = _rejected_review("real issue")
        state.extras["session_mode_initial"] = "stateless"

        new = default_registry().get("repair_changes")(state)

        assert new.halt is True
        assert "guardrail" in new.halt_reason
        assert new.phase_log["repair_changes"]["guardrail_blocked"] is True

# ── Final QA ──────────────────────────────────────────────────────────────────

class TestFinalQAHandler:
    def test_approved_passes_clean(self) -> None:
        state = _state()
        new = default_registry().get("final_acceptance")(state)
        assert new.phase_log["final_acceptance"]["approved"] is True
        assert new.halt is False

    def test_rejected_well_formed_does_not_halt(self) -> None:
        # ADR 0022 narrow-behavior: final_acceptance records its
        # critique on a well-formed REJECTED verdict but does not halt
        # the pipeline. Only hard contract-parse failures halt — see
        # ``test_malformed_contract_halts``.
        # ADR 0025 Phase 1: final_acceptance now uses release_json,
        # so the REJECTED payload is release-shaped.
        pc = _StubPhaseConfig(
            final_acceptance_agent=_FakeReviewer(_rejected_release("breaks tests")),
        )
        state = _state(phase_config=pc)
        new = default_registry().get("final_acceptance")(state)
        assert new.halt is False, (
            "final_acceptance must not halt on a well-formed REJECTED "
            "verdict (ADR 0022)"
        )
        assert "breaks tests" in new.last_critique

    def test_rejected_surfaces_critique(self) -> None:
        pc = _StubPhaseConfig(
            final_acceptance_agent=_FakeReviewer(_rejected_release("flaky")),
        )
        state = _state(phase_config=pc)
        new = default_registry().get("final_acceptance")(state)
        assert new.halt is False
        assert "flaky" in new.last_critique

    def test_malformed_contract_halts(self) -> None:
        """Contract failures are protocol breaks — they must halt.
        A malformed final QA otherwise lets the run finalize with QA=ok
        despite the gate having no real signal."""
        pc = _StubPhaseConfig(
            final_acceptance_agent=_FakeReviewer("{not valid json"),
        )
        state = _state(phase_config=pc)
        new = default_registry().get("final_acceptance")(state)
        assert new.halt is True
        assert "final_acceptance contract rejected" in new.halt_reason
        assert new.phase_log["final_acceptance"]["approved"] is False
        assert new.phase_log["final_acceptance"]["parse_error"]

    def test_no_diff_with_implement_evidence_is_not_applicable(self) -> None:
        reviewer = _FakeReviewer(_rejected_release("should not run"))
        state = _state(phase_config=_StubPhaseConfig(
            final_acceptance_agent=reviewer,
        ))
        state.phase_log["implement"] = {"output": "verification evidence"}
        state.phase_log["review_changes"] = {
            "clean": True,
            "skipped": "no uncommitted changes",
        }

        new = default_registry().get("final_acceptance")(state)

        assert reviewer.calls == []
        entry = new.phase_log["final_acceptance"]
        assert entry["approved"] is True
        assert entry["verdict"] == "APPROVED"
        assert entry["ship_ready"] is True
        assert entry["review_target"] == "not_applicable"
        assert entry["diff"] == "none"
        assert entry["skipped"] == "no uncommitted changes"

    def test_no_diff_without_implement_phase_is_not_applicable(self) -> None:
        reviewer = _FakeReviewer(_rejected_release("should not run"))
        state = _state(phase_config=_StubPhaseConfig(
            final_acceptance_agent=reviewer,
        ))
        state.phase_log["review_changes"] = {
            "clean": True,
            "skipped": "no uncommitted changes",
        }

        new = default_registry().get("final_acceptance")(state)

        assert reviewer.calls == []
        entry = new.phase_log["final_acceptance"]
        assert entry["approved"] is True
        assert entry["verdict"] == "APPROVED"
        assert entry["ship_ready"] is True
        assert entry["diff"] == "none"

    def test_no_diff_with_incomplete_implement_rejects_without_agent(self) -> None:
        reviewer = _FakeReviewer(_approved_release("should not run"))
        state = _state(phase_config=_StubPhaseConfig(
            final_acceptance_agent=reviewer,
        ))
        state.phase_log["implement"] = {
            "output": "build",
            "delivery_clean": False,
            "delivery_status": "incomplete",
            "incomplete_subtasks": ["T3"],
        }
        state.phase_log["review_changes"] = {
            "clean": False,
            "skipped": "implement delivery incomplete",
        }

        new = default_registry().get("final_acceptance")(state)

        assert reviewer.calls == []
        entry = new.phase_log["final_acceptance"]
        assert entry["approved"] is False
        assert entry["verdict"] == "REJECTED"
        assert entry["ship_ready"] is False
        assert entry["diff"] == "none"
        assert entry["skipped"] == "implement delivery incomplete"
        assert "missing or incomplete" in new.last_critique


# ── Final acceptance — release-gate dual-shape (ADR 0025 Phase 1) ─────────────

class TestFinalAcceptanceDualShape:
    """The handler writes both the review-shape mirror (verdict /
    short_summary / findings) and the release-shape fields
    (ship_ready / release_blockers / verification_gaps / contract_status)
    into ``phase_log["final_acceptance"]``.

    Web / MCP / evidence consumers reading review-shape fields keep
    working; release-aware consumers read the release fields.
    """

    def test_approved_populates_both_shapes(self) -> None:
        state = _state()
        new = default_registry().get("final_acceptance")(state)
        entry = new.phase_log["final_acceptance"]
        # Review-shape mirror.
        assert entry["approved"] is True
        assert entry["verdict"] == "APPROVED"
        assert entry["short_summary"] == "ok"
        assert entry["findings"] == []
        # Release fields.
        assert entry["ship_ready"] is True
        assert entry["release_blockers"] == []
        assert entry["verification_gaps"] == []
        assert entry["contract_status"]["task_contract"] == "satisfied"

    def test_rejected_projects_blockers_into_findings_mirror(self) -> None:
        pc = _StubPhaseConfig(
            final_acceptance_agent=_FakeReviewer(_rejected_release("flaky")),
        )
        state = _state(phase_config=pc)
        new = default_registry().get("final_acceptance")(state)
        entry = new.phase_log["final_acceptance"]
        # Review-shape mirror: findings projected from release blockers.
        assert entry["approved"] is False
        assert entry["verdict"] == "REJECTED"
        assert len(entry["findings"]) == 1
        f = entry["findings"][0]
        assert f["id"] == "R1"
        assert f["severity"] == "P1"
        # Release fields.
        assert entry["ship_ready"] is False
        assert len(entry["release_blockers"]) == 1
        blocker = entry["release_blockers"][0]
        assert blocker["why_blocks_release"]  # release-only field

    def test_rejected_does_not_halt_run(self) -> None:
        """ADR 0025 Phase 1 preserves ADR 0022 non-halting behaviour:
        well-formed REJECTED release verdict completes the run; only
        parse failures halt."""
        pc = _StubPhaseConfig(
            final_acceptance_agent=_FakeReviewer(_rejected_release("blocker")),
        )
        state = _state(phase_config=pc)
        new = default_registry().get("final_acceptance")(state)
        assert new.halt is False
        assert "blocker" in new.last_critique

    def test_dry_run_yields_release_shape_phase_log(self) -> None:
        """Dry-run path: ``run_review`` synthesises release-shape JSON
        when ``output_contract="release"``; ``parse_release`` accepts
        it; phase_log records the full dual-shape entry."""
        state = _state(dry_run=True)
        new = default_registry().get("final_acceptance")(state)
        entry = new.phase_log["final_acceptance"]
        assert entry["approved"] is True
        assert entry["ship_ready"] is True
        assert entry["verdict"] == "APPROVED"
        assert entry["contract_status"] is not None
        # No parse failure → run completes normally.
        assert new.halt is False

    def test_review_shape_consumer_compat_keys_present(self) -> None:
        """Lock the dual-shape mirror contract: every review-shape
        consumer (Web phase card, MCP findings slice, golden fixtures)
        reads ``verdict`` / ``short_summary`` / ``findings`` / ``approved``;
        all four must remain in the phase_log entry."""
        state = _state()
        new = default_registry().get("final_acceptance")(state)
        entry = new.phase_log["final_acceptance"]
        for key in ("approved", "verdict", "short_summary", "findings"):
            assert key in entry, f"review-shape mirror missing {key!r}"


# ── Compliance check stub ─────────────────────────────────────────────────────

class TestComplianceCheckStub:
    def test_default_is_noop(self) -> None:
        state = _state()
        new = default_registry().get("compliance_check")(state)
        assert "skipped" in new.phase_log["compliance_check"]


# ── End-to-end through run_profile ───────────────────────────────────────────

class TestLinearProfileEndToEnd:
    """The proof: full linear profile flows through run_profile with mock
 agents and produces a coherent state at the end.
 """

    def _approving_state(self) -> PipelineState:
        pc = _StubPhaseConfig(
            plan_agent     = _FakeArchitect(_valid_plan_md("PLAN MD")),
            validate_plan_agent  = _FakeReviewer(_approved_review("ok")),
            implement_agent    = _FakeDeveloper("built"),
            review_changes_agent   = _FakeReviewer(_rejected_review("nits")),
            repair_changes_agent      = _FakeDeveloper("fixed"),
            final_acceptance_agent = _FakeReviewer(_approved_release("ok")),
        )
        state = PipelineState(
            task="add endpoint", project_dir="/p", plugin=PluginConfig(),
            phase_config=pc,
        )
        # The legacy ``PipelineProfile`` driver (test-only; production v2 runs
        # seed ``active_step`` via the FSM) does not seed an active step, so
        # the phase-role continuity resolver would raise. Seed it as the FSM
        # would — these flow tests only need the no-raise default; the
        # behavioural resume cases live in TestRoundResumePolicy.
        state.lifecycle_ctx = _lifecycle_ctx_with_continuity()
        return state

    def test_full_linear_profile_runs_in_order(self) -> None:
        state = self._approving_state()
        # b: handler-side escalation runs
        # unconditionally now. AUTO session mode + empty model
        # strings resolves to CHAIN → round-1 swaps repair_changes_agent to
        # implement_agent. Force STATELESS so the repair_changes_agent's "fixed"
        # output is what shows up in phase_log["repair_changes"].
        state.extras["session_mode_initial"] = "stateless"
        registry = default_registry()
        profile = PipelineProfile(
            "linear",
            ("plan", "validate_plan", "implement", "review_changes", "repair_changes", "final_acceptance"),
        )
        result = run_profile(profile, state, registry)

        assert result.halt is False
        # Every phase has a log entry. fix handler
        # also stuffs ``rounds_pending`` for v2 dispatch RoundAdapter
        # auto-fire path — assert canonical phases are a subset, not
        # exclusive equality.
        assert {
            "plan", "validate_plan", "implement", "review_changes", "repair_changes", "final_acceptance",
        } <= set(result.phase_log)
        # Plan markdown was captured and threaded forward.
        assert "PLAN MD" in result.plan_markdown
        # Review's typed JSON critique drove fix. Output is the rendered
        # review markdown produced from the parsed contract.
        review_output = result.phase_log["review_changes"]["output"]
        assert review_output.startswith("# Review")
        assert "REJECTED" in review_output
        assert "nits" in review_output
        assert result.phase_log["repair_changes"]["output"] == "fixed"
        # last_critique was consumed by fix (cleared); final_acceptance approved without setting it again.
        assert result.last_critique == ""
        # Final verdict captured.
        assert result.phase_log["final_acceptance"]["approved"] is True

    def test_lite_profile_skips_qa_loop_entirely(self) -> None:
        """The 'small_task' profile from _config/pipeline_profiles.json: plan → build → final_acceptance."""
        state = self._approving_state()
        registry = default_registry()
        profile = PipelineProfile("small_task", ("plan", "implement", "final_acceptance"))
        result = run_profile(profile, state, registry)

        assert result.halt is False
        assert set(result.phase_log) == {"plan", "implement", "final_acceptance"}
        # validate_plan / review / fix were NOT in the profile, so no log entries.
        for skipped in ("validate_plan", "review_changes", "repair_changes"):
            assert skipped not in result.phase_log

    def test_dry_run_propagates_through_every_handler(self) -> None:
        state = self._approving_state()
        state.dry_run = True
        registry = default_registry()
        profile = PipelineProfile(
            "linear",
            ("plan", "validate_plan", "implement", "review_changes", "repair_changes", "final_acceptance"),
        )
        result = run_profile(profile, state, registry)

        # No real agent should have been called.
        assert state.phase_config.plan_agent.calls == []
        assert state.phase_config.implement_agent.calls == []
        # Plan markdown carries the dry-run marker.
        assert "[DRY RUN]" in result.plan_markdown




# ── Round-resume policy ──────────────────────────────────────────────────────
#
# After Phase 7.10, looping phase handlers pass ``continue_session=True`` on
# round 2+ so the same runtime instance resumes its captured bridge instead
# of starting fresh every iteration. These tests pin the handler-level
# policy, not the runtime mechanics (those live in
# tests/unit/pipeline/runtime/test_session_bridge.py).


class TestRoundResumePolicy:
    def test_plan_round_1_starts_fresh(self) -> None:
        state = _state()
        # Built-in plan declares loop_continue; round 1 has no prior loop
        # session yet, so it still starts fresh.
        state.lifecycle_ctx = _lifecycle_ctx_with_continuity("loop_continue")
        # plan_round defaults to 1 when extras don't carry it.
        default_registry().get("plan")(state)
        agent: _FakeArchitect = state.phase_config.plan_agent
        assert agent.kwargs_log[-1]["continue_session"] is False

    def test_plan_round_2_resumes_with_handoff(self) -> None:
        # ADR 0113 (declarative continuity): plan declares loop_continue, so
        # round 2+ RESUMES the prior loop session (the restored pre-0113
        # behaviour). The compact handoff (prior plan + reviewer critique) still
        # rides the prompt so a resumed architect revises its own attempt.
        state = _state()
        state.lifecycle_ctx = _lifecycle_ctx_with_continuity("loop_continue")
        state.extras["plan_round"] = 2
        state.plan_markdown = "PRIOR-PLAN-BODY-MARKER"
        state.last_critique = "missing edge case for null payload"
        default_registry().get("plan")(state)
        agent: _FakeArchitect = state.phase_config.plan_agent
        assert agent.kwargs_log[-1]["continue_session"] is True
        prompt = agent.calls[-1][0]
        assert "PRIOR-PLAN-BODY-MARKER" in prompt
        assert "missing edge case for null payload" in prompt

    def test_validate_plan_round_1_starts_fresh(self) -> None:
        state = _state()
        state.lifecycle_ctx = _lifecycle_ctx_with_continuity("loop_continue")
        state.plan_markdown = "PLAN MD"
        default_registry().get("validate_plan")(state)
        agent: _FakeReviewer = state.phase_config.validate_plan_agent
        assert agent.kwargs_log[-1]["continue_session"] is False

    def test_validate_plan_round_2_resumes(self) -> None:
        # ADR 0113 (declarative continuity): validate_plan declares
        # loop_continue → round 2+ RESUMES the prior loop session. This is the
        # operator-found regression fix (0113 had swept it into fresh-only).
        state = _state()
        state.lifecycle_ctx = _lifecycle_ctx_with_continuity("loop_continue")
        state.plan_markdown = "PLAN MD"
        state.extras["plan_round"] = 2
        default_registry().get("validate_plan")(state)
        agent: _FakeReviewer = state.phase_config.validate_plan_agent
        assert agent.kwargs_log[-1]["continue_session"] is True

    def test_review_changes_round_1_starts_fresh(self) -> None:
        # The handler short-circuits when no uncommitted changes — stub the
        # helper via the lifecycle_ctx (the real dependency target) so the
        # agent call lands.
        state = _state()
        # repair_round defaults to 1. review declares fresh_only.
        state.lifecycle_ctx = _lifecycle_ctx_with_continuity("fresh_only")
        state.lifecycle_ctx.git_helpers.has_uncommitted = (
            lambda *_a, **_k: True
        )
        default_registry().get("review_changes")(state)
        agent: _FakeReviewer = state.phase_config.review_changes_agent
        # Round-1 review fires (uncommitted forced) and must start fresh.
        assert agent.kwargs_log, "review_changes agent was not invoked"
        assert agent.kwargs_log[-1]["continue_session"] is False

    def test_review_changes_round_2_is_fresh(self, monkeypatch) -> None:
        # The handler short-circuits when no uncommitted changes — stub
        # the helper via the lifecycle_ctx so the agent call lands.
        state = _state()
        state.extras["repair_round"] = 2
        # Replace the GitHelpers.has_uncommitted on the lifecycle ctx so
        # the precondition doesn't kick in. review declares fresh_only.
        state.lifecycle_ctx = _lifecycle_ctx_with_continuity("fresh_only")
        state.lifecycle_ctx.git_helpers.has_uncommitted = (
            lambda *_a, **_k: True
        )
        default_registry().get("review_changes")(state)
        agent: _FakeReviewer = state.phase_config.review_changes_agent
        # ADR 0113: round-2 review stays FRESH (fresh_only policy); the
        # handoff carries the prior context instead of resuming.
        assert agent.kwargs_log, "review_changes agent was not invoked"
        assert agent.kwargs_log[-1]["continue_session"] is False

    def test_validate_round_2_resume_is_round_derived_not_session_probe(
        self,
    ) -> None:
        """ADR 0113: validate_plan round 2+ resume is derived from the loop
        round counter (loop_followon), not from whether the agent happens to
        carry a captured session id — the round counter alone drives it.
        """
        state = _state()
        state.lifecycle_ctx = _lifecycle_ctx_with_continuity("loop_continue")
        state.plan_markdown = "PLAN MD"
        state.extras["plan_round"] = 2
        agent: _FakeReviewer = state.phase_config.validate_plan_agent
        # No captured session id, yet round 2 still resumes by the counter.
        agent.session_id = None

        default_registry().get("validate_plan")(state)
        assert agent.kwargs_log[-1]["continue_session"] is True


class TestSessionDispositionFreshHandoff:
    """ADR 0113: review / plan / validate_plan go FRESH on round 2+ and carry a
    compact handoff instead of resuming; repair follows the policy (same-write-
    zone CONTINUE / non-same-zone FRESH). These pin each handler explicitly.
    """

    @staticmethod
    def _store_marker_receipt(state, *, repair_phase: str, critique: str):
        # The rendered receipt surfaces the repair *output* line as the fixed
        # item summary, so the marker rides ``repair_output``.
        from pipeline.phases.builtin.review_support import _store_repair_receipt
        from pipeline.repair_protocol import build_repair_receipt
        return _store_repair_receipt(state, build_repair_receipt(
            source_phase="review_changes",
            source_round=1,
            repair_phase=repair_phase,
            repair_round=2,
            critique="prior reviewer critique",
            repair_output=critique,
            operator_feedback="",
            changed_refs=("a.py",),
        ))

    def test_validate_plan_round2_resumes_carries_plan_and_critique(self) -> None:
        state = _state()
        # validate_plan declares loop_continue → round 2 resumes.
        state.lifecycle_ctx = _lifecycle_ctx_with_continuity("loop_continue")
        # Round 1 plan populates parsed_plan + the plan artifact so the
        # round-2 reviewer renders the typed plan views (the "план" handoff).
        default_registry().get("plan")(state)
        # A prior plan-rejection cycle stored a repair receipt (the "critique"
        # half of the handoff).
        self._store_marker_receipt(
            state, repair_phase="plan", critique="VALIDATE-HANDOFF-MARKER",
        )
        state.extras["plan_round"] = 2
        default_registry().get("validate_plan")(state)
        agent: _FakeReviewer = state.phase_config.validate_plan_agent
        assert agent.kwargs_log[-1]["continue_session"] is True
        prompt = agent.calls[-1][0]
        # Plan (typed view) + critique (receipt) both ride the prompt.
        assert "Do the planned work" in prompt
        assert "VALIDATE-HANDOFF-MARKER" in prompt

    def test_review_round2_fresh_carries_full_handoff(self, monkeypatch) -> None:
        import pipeline.phases.builtin.handlers.review_changes as rc
        state = _state()
        # parsed_plan so the plan contract (the "contract" half) is non-empty.
        default_registry().get("plan")(state)
        self._store_marker_receipt(
            state, repair_phase="repair_changes",
            critique="REVIEW-RECEIPT-MARKER",
        )
        state.extras["repair_round"] = 2
        state.lifecycle_ctx = _lifecycle_ctx_with_continuity("fresh_only")
        state.lifecycle_ctx.git_helpers.has_uncommitted = (
            lambda *_a, **_k: True
        )
        # current_review_subject derives from a real git diff; pin a marker so
        # the unit test is git-independent.
        monkeypatch.setattr(
            rc, "_current_change_review_subject",
            lambda _s: "REVIEW-SUBJECT-MARKER",
        )
        default_registry().get("review_changes")(state)
        agent: _FakeReviewer = state.phase_config.review_changes_agent
        assert agent.kwargs_log, "review_changes agent was not invoked"
        # FRESH disposition, yet the prompt carries the full compact handoff:
        # repair receipt + current review subject + plan contract.
        assert agent.kwargs_log[-1]["continue_session"] is False
        prompt = agent.calls[-1][0]
        assert "REVIEW-RECEIPT-MARKER" in prompt
        assert "REVIEW-SUBJECT-MARKER" in prompt
        assert "Do the planned work" in prompt

    def test_repair_same_write_zone_continues(self, monkeypatch) -> None:
        import pipeline.phases.builtin.handlers.repair_changes as rc
        from agents.protocols import SessionMode
        state = _state()
        state.last_critique = "fix the nits"
        # CHAIN repair → same write zone → policy CONTINUE. CHAIN round-1
        # continuation swaps to the implement agent (carries the session).
        monkeypatch.setattr(rc, "_resolve_fix_runtime_config", lambda _s: {
            "effective_mode": SessionMode.CHAIN,
            "repair_round": 1,
            "human_directed": False,
            "repair_model_for_round": "m1",
        })
        default_registry().get("repair_changes")(state)
        agent: _FakeDeveloper = state.phase_config.repair_changes_agent
        assert agent.calls, "repair agent was not invoked"
        assert agent.calls[-1][2]["continue_session"] is True

    def test_repair_non_same_write_zone_is_fresh(self, monkeypatch) -> None:
        import pipeline.phases.builtin.handlers.repair_changes as rc
        from agents.protocols import SessionMode
        state = _state()
        state.last_critique = "fix the nits"
        # STATELESS repair → not same write zone → policy FRESH.
        monkeypatch.setattr(rc, "_resolve_fix_runtime_config", lambda _s: {
            "effective_mode": SessionMode.STATELESS,
            "repair_round": 2,
            "human_directed": False,
            "repair_model_for_round": "m2",
        })
        default_registry().get("repair_changes")(state)
        agent: _FakeDeveloper = state.phase_config.repair_changes_agent
        assert agent.calls, "repair agent was not invoked"
        assert agent.calls[-1][2]["continue_session"] is False

    def test_validate_plan_phase_log_meta_reflects_policy_not_probe(self) -> None:
        # F1: the persisted phase_log meta must carry the POLICY disposition,
        # not a probe of the agent's resumed-session id. validate_plan declares
        # loop_continue, so round 2 resumes by policy even when the runtime
        # resume probe never fired (no captured resumed id) — the meta reflects
        # the policy intent, not the probe.
        state = _state()
        state.lifecycle_ctx = _lifecycle_ctx_with_continuity("loop_continue")
        default_registry().get("plan")(state)
        state.extras["plan_round"] = 2
        agent: _FakeReviewer = state.phase_config.validate_plan_agent
        agent.session_id = "sess_live_round1"
        # The runtime resume probe never captured a resumed id; the OLD meta
        # path would have reflected this probe as "not resumed".
        agent._last_resumed_session_id = None
        default_registry().get("validate_plan")(state)
        assert state.phase_log["validate_plan"]["continue_session"] is True
        assert state.phase_log["validate_plan"]["session_id"] == "sess_live_round1"

    def test_review_repair_flow_fresh_review_no_amnesia(self, monkeypatch) -> None:
        """Named flow: review(reject) → repair → re-review(round 2).

        The round-2 re-review runs FRESH (no session resume), but the compact
        handoff must still deliver the repair receipt produced by the repair
        step — otherwise the fresh reviewer would re-read the diff cold and
        lose the "what was just fixed" context the old continuity path carried.
        """
        import pipeline.phases.builtin.handlers.review_changes as rc
        state = _state()
        # The repair step in round 1 left a receipt describing its fix.
        self._store_marker_receipt(
            state, repair_phase="repair_changes",
            critique="AMNESIA-GUARD-RECEIPT",
        )
        state.extras["repair_round"] = 2
        state.lifecycle_ctx = _lifecycle_ctx_with_continuity("fresh_only")
        state.lifecycle_ctx.git_helpers.has_uncommitted = (
            lambda *_a, **_k: True
        )
        monkeypatch.setattr(
            rc, "_current_change_review_subject",
            lambda _s: "AMNESIA-GUARD-SUBJECT",
        )
        default_registry().get("review_changes")(state)
        agent: _FakeReviewer = state.phase_config.review_changes_agent
        # Fresh session, but the fixed-context handoff survived.
        assert agent.kwargs_log[-1]["continue_session"] is False
        prompt = agent.calls[-1][0]
        assert "AMNESIA-GUARD-RECEIPT" in prompt
        assert "AMNESIA-GUARD-SUBJECT" in prompt


# ── Hypothesis loop round-resume ─────────────────────────────────────────────


class TestHypothesisLoopResume:
    """attempt 1 starts fresh; attempt 2+ resumes both architect & qa."""

    def test_attempt_1_starts_fresh(self, tmp_path) -> None:
        from pipeline.engine.hypothesis import run_hypothesis_loop

        class _A:
            model = "arch"
            session_id = None
            def __init__(self):
                self.kwargs: list[dict] = []
            def invoke(self, prompt, cwd, **kw):
                self.kwargs.append(kw)
                return "hypothesis text"
            def reset_session(self):
                self.session_id = None

        class _Q:
            model = "qa"
            session_id = None
            def __init__(self):
                self.kwargs: list[dict] = []
            def invoke(self, prompt, cwd, **kw):
                self.kwargs.append(kw)
                return _approved_review()
            def reset_session(self):
                self.session_id = None

        a, q = _A(), _Q()
        run_hypothesis_loop(a, q, "task", str(tmp_path), "", max_hypotheses=1)
        assert a.kwargs[0].get("continue_session", False) is False
        assert q.kwargs[0].get("continue_session", False) is False

    def test_attempt_2_resumes_both(self, tmp_path) -> None:
        from pipeline.engine.hypothesis import run_hypothesis_loop

        class _A:
            model = "arch"
            session_id = None
            def __init__(self):
                self.kwargs: list[dict] = []
            def invoke(self, prompt, cwd, **kw):
                self.kwargs.append(kw)
                return "another guess"
            def reset_session(self):
                self.session_id = None

        # First verdict rejects so loop rolls to attempt 2.
        class _Q:
            model = "qa"
            session_id = None
            def __init__(self):
                self.kwargs: list[dict] = []
                self._verdicts = [
                    _rejected_review("missing X"),
                    _approved_review(),
                ]
            def invoke(self, prompt, cwd, **kw):
                self.kwargs.append(kw)
                return self._verdicts.pop(0) if self._verdicts else _approved_review()
            def reset_session(self):
                self.session_id = None

        a, q = _A(), _Q()
        run_hypothesis_loop(a, q, "task", str(tmp_path), "", max_hypotheses=3)
        # Attempt 1: fresh on both bridges.
        assert a.kwargs[0].get("continue_session", False) is False
        assert q.kwargs[0].get("continue_session", False) is False
        # Attempt 2: resume on both.
        assert a.kwargs[1].get("continue_session", False) is True
        assert q.kwargs[1].get("continue_session", False) is True


# ── Per-phase "Files Diff" rendering ──────────────────────────────────────────


def _strip_ansi(text: str) -> str:
    import re
    return re.sub(r"\033\[[0-9;]*m", "", text)


def _init_phase_repo(path):
    import subprocess
    path.mkdir(parents=True, exist_ok=True)
    for args in (
        ["init", "-q"],
        ["config", "user.email", "orcho@example.test"],
        ["config", "user.name", "Orcho Test"],
    ):
        subprocess.run(
            ["git", *args], cwd=str(path), check=True,
            capture_output=True, text=True, timeout=10,
        )
    (path / "payload.py").write_text("value = 1\n", encoding="utf-8")
    (path / "tests.py").write_text("answer = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "payload.py", "tests.py"], cwd=str(path),
        check=True, capture_output=True, text=True, timeout=10,
    )
    subprocess.run(
        ["git", "commit", "-qm", "initial"], cwd=str(path),
        check=True, capture_output=True, text=True, timeout=10,
    )


class TestPerPhaseFilesDiff:
    """End-to-end of the per-phase rendering contract:

    Simulates IMPLEMENT → REVIEW reject → REPAIR by mutating the real
    worktree at each step, and asserts each phase's transcript block
    shows ONLY what that phase mutated — not the cumulative diff.
    """

    def test_implement_then_repair_produce_disjoint_per_phase_patches(
        self, tmp_path, capsys,
    ) -> None:
        from pipeline.phases.builtin import (
            _capture_phase_baseline,
            _print_implement_summary,
        )

        project = tmp_path / "project"
        run_dir = tmp_path / "run"
        _init_phase_repo(project)
        state = _state()
        state.project_dir = str(project)
        state.output_dir = str(run_dir)

        # ── IMPLEMENT ────────────────────────────────────────────────
        implement_baseline = _capture_phase_baseline(state)
        assert implement_baseline is not None  # real git repo
        (project / "payload.py").write_text("value = 2\n", encoding="utf-8")
        implement_entry = {"output": "implement done", "meta": {}}
        _print_implement_summary(
            state, implement_entry, title="Implementation",
            phase_name="implement", baseline_ref=implement_baseline,
        )
        implement_out = _strip_ansi(capsys.readouterr().out)
        assert "Files Diff (this phase)" in implement_out
        assert "payload.py" in implement_out
        assert "tests.py" not in implement_out
        implement_patch = run_dir / "phases" / "implement" / "diff.patch"
        assert implement_patch.exists()
        implement_text = implement_patch.read_text(encoding="utf-8")
        assert "-value = 1" in implement_text
        assert "+value = 2" in implement_text

        # ── REPAIR ──────────────────────────────────────────────────
        repair_baseline = _capture_phase_baseline(state)
        assert repair_baseline is not None
        assert repair_baseline != implement_baseline  # tree moved
        (project / "tests.py").write_text("answer = 42\n", encoding="utf-8")
        repair_entry = {"output": "fixed", "meta": {}}
        _print_implement_summary(
            state, repair_entry, title="Repair changes",
            phase_name="repair_changes", baseline_ref=repair_baseline,
        )
        repair_out = _strip_ansi(capsys.readouterr().out)
        assert "Files Diff (this phase)" in repair_out
        assert "tests.py" in repair_out
        # The IMPLEMENT-only file must NOT show up in REPAIR's per-phase
        # block — that was the user-reported cognitive bug.
        assert "payload.py" not in repair_out
        repair_patch = run_dir / "phases" / "repair_changes" / "diff.patch"
        assert repair_patch.exists()
        repair_text = repair_patch.read_text(encoding="utf-8")
        assert "-answer = 1" in repair_text
        assert "+answer = 42" in repair_text
        assert "payload.py" not in repair_text

        # The two per-phase patches must be byte-disjoint at the
        # diff-section level.
        assert "payload.py" not in repair_text
        assert "tests.py" not in implement_text

    def test_quiet_phase_writes_no_patch_and_prints_no_changes(
        self, tmp_path, capsys,
    ) -> None:
        """A phase that mutates nothing must NOT write a per-phase patch
        file and must NOT print the cumulative diff under a per-phase
        header (load-bearing no-fallback invariant).
        """
        from pipeline.phases.builtin import (
            _capture_phase_baseline,
            _print_implement_summary,
        )

        project = tmp_path / "project"
        run_dir = tmp_path / "run"
        _init_phase_repo(project)
        # Pre-existing uncommitted change BEFORE snapshot — proves we
        # don't accidentally render the cumulative diff for a quiet
        # phase.
        (project / "payload.py").write_text("value = 99\n", encoding="utf-8")

        state = _state()
        state.project_dir = str(project)
        state.output_dir = str(run_dir)

        baseline = _capture_phase_baseline(state)
        assert baseline is not None
        # Phase invokes runtime but it changes nothing.
        entry = {"output": "no-op", "meta": {}}
        _print_implement_summary(
            state, entry, title="Repair changes",
            phase_name="repair_changes", baseline_ref=baseline,
        )

        out = _strip_ansi(capsys.readouterr().out)
        assert "Files Diff (this phase)" in out
        assert "(no changes)" in out
        # Cumulative content (the pre-existing dirty edit) must NOT leak.
        assert "value = 99" not in out
        assert not (run_dir / "phases" / "repair_changes" / "diff.patch").exists()

    def test_no_baseline_falls_back_to_cumulative_header(
        self, tmp_path, capsys,
    ) -> None:
        """When git isn't resolvable (or caller didn't pass a baseline)
        the legacy ``Files Diff`` header + cumulative preview path is
        preserved — that's the right degradation, not a regression.
        """
        from pipeline.phases.builtin import _print_implement_summary

        project = tmp_path / "project"
        run_dir = tmp_path / "run"
        _init_phase_repo(project)
        (project / "payload.py").write_text("value = 2\n", encoding="utf-8")

        state = _state()
        state.project_dir = str(project)
        state.output_dir = str(run_dir)

        entry = {"output": "implement done", "meta": {}}
        _print_implement_summary(
            state, entry, title="Implementation",
            # No phase_name / baseline_ref → legacy path.
        )

        out = _strip_ansi(capsys.readouterr().out)
        # Legacy header (no "this phase" suffix).
        assert "Files Diff" in out
        assert "Files Diff (this phase)" not in out
        # Cumulative artefact (run-level) is written via the default
        # ``diff.patch`` filename.
        assert (run_dir / "diff.patch").exists()


@pytest.fixture(autouse=True)
def _live_output_mode_for_full_transcript():
    """Pin the full live transcript shape (T2 summary reconciliation).

    ``summary`` is the default run-output mode — the compact append-only
    arc that collapses phase headers to ``▶ <phase>`` and the review /
    plan / implement outcome blocks to single lines. These tests assert
    the full-fidelity transcript, so force ``live`` (rendering only; no
    echo / verbose / trace side effects) and restore afterwards.
    """
    from core.observability import logging as _logging

    _before = _logging.get_output_mode()
    _logging._output_mode = "live"
    try:
        yield
    finally:
        _logging._output_mode = _before
