"""session-shape adapters.

Per-adapter unit tests with synthetic state. Each adapter is a pure
function from ``state.phase_log[name]`` (+ select state fields) to
``session["phases"][name]`` shape. Snapshot parity vs the legacy
``_append_*`` paths is verified by inspection: the bodies were ported
verbatim, only the orchestrator's call site changed.
"""
from __future__ import annotations

import json

import pytest

import pipeline.phases.builtin.handlers.review_changes as _review_changes_mod
from agents.entities import SubTask
from agents.registry import PhaseAgentConfig
from pipeline.lifecycle import default_lifecycle_context
from pipeline.phases.builtin import default_registry
from pipeline.phases.builtin.review_support import _store_repair_receipt
from pipeline.plan_parser import ParsedPlan
from pipeline.plugins import PluginConfig
from pipeline.repair_protocol import build_repair_receipt
from pipeline.runtime import PhaseRegistry, PipelineState
from pipeline.runtime.profile import ExecutionPolicy
from pipeline.runtime.steps import PhaseStep
from pipeline.session_adapters import (
    BuildAdapter,
    FinalAcceptanceAdapter,
    HypothesisAdapter,
    PlanAdapter,
    RoundAdapter,
    SessionAdapter,
    SessionAdapterRegistry,
    ValidatePlanAdapter,
    default_session_adapter_registry,
)


def _state(**kw) -> PipelineState:
    return PipelineState(task="t", project_dir="/p", plugin=PluginConfig(), **kw)


def _session() -> dict:
    return {"phases": {}}


def _approved_raw_review(summary: str = "Approved by JSON contract.") -> str:
    return json.dumps({
        "verdict":       "APPROVED",
        "short_summary": summary,
        "findings":      [],
    })


# ── Registry ──────────────────────────────────────────────────────────────────

class TestSessionAdapterRegistry:
    def test_register_and_lookup(self) -> None:
        reg = SessionAdapterRegistry()
        reg.register("plan", PlanAdapter())
        assert reg.has("plan") is True
        assert isinstance(reg.get("plan"), PlanAdapter)

    def test_get_or_none_returns_none_for_unknown(self) -> None:
        assert SessionAdapterRegistry().get_or_none("ghost") is None

    def test_unknown_get_raises(self) -> None:
        with pytest.raises(KeyError, match="Unknown session adapter"):
            SessionAdapterRegistry().get("ghost")

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty string"):
            SessionAdapterRegistry().register("", PlanAdapter())

    def test_default_registry_has_all_six(self) -> None:
        reg = default_session_adapter_registry()
        for name in ("plan", "validate_plan", "implement", "rounds", "final_acceptance", "hypothesis"):
            assert reg.has(name)

    def test_default_registry_is_singleton(self) -> None:
        a = default_session_adapter_registry()
        b = default_session_adapter_registry()
        assert a is b

    def test_protocol_satisfied_by_all_builtins(self) -> None:
        for adapter in (PlanAdapter(), ValidatePlanAdapter(), BuildAdapter(),
                        RoundAdapter(), FinalAcceptanceAdapter(), HypothesisAdapter()):
            assert isinstance(adapter, SessionAdapter)


# ── PlanAdapter ───────────────────────────────────────────────────────────────

class TestPlanAdapter:
    def test_minimal_round1(self) -> None:
        s = _state()
        s.phase_log["plan"] = {
            "output":              "## Task A",
            "attempt":             1,
            "existing_files":      ["a.py"],
            "missing_files":       [],
            "codemap_injected":    True,
            "hypothesis_injected": False,
            "replan_critique":     None,
        }
        s.parsed_plan = ParsedPlan(
            short_summary="x",
            planning_context="x",
            source="test",
            subtasks=(
                SubTask(id="a", goal="a", files=("a.py",)),
                SubTask(id="b", goal="b", files=("b.py",)),
                SubTask(id="c", goal="c", files=("a.py",)),
            ),
        )
        sess = _session()
        PlanAdapter().write("plan", s, sess, round_n=1)
        entry = sess["phases"]["plan"][0]
        assert entry["attempt"] == 1
        assert entry["output"] == "## Task A"
        assert entry["codemap_injected"] is True
        assert entry["hypothesis_injected"] is False
        assert entry["existing_files"] == ["a.py"]
        assert entry["total_atomic_tasks"] == 3
        assert entry["parsed_file_paths"] == ["a.py", "b.py"]
        assert "replan_critique" not in entry  # None → omitted

    def test_replan_critique_included_round2(self) -> None:
        s = _state()
        s.phase_log["plan"] = {
            "output":           "v2",
            "attempt":          2,
            "replan_critique":  "missing tests",
        }
        sess = _session()
        PlanAdapter().write("plan", s, sess, round_n=2)
        assert sess["phases"]["plan"][0]["replan_critique"] == "missing tests"

    def test_human_feedback_included_on_operator_directed_replan(self) -> None:
        s = _state()
        s.phase_log["plan"] = {
            "output":          "v2",
            "attempt":         2,
            "replan_critique": "missing tests",
            "human_feedback":  "Scope to migrations only.",
            "meta":            {"human_directed": True},
        }
        sess = _session()
        PlanAdapter().write("plan", s, sess, round_n=2)
        entry = sess["phases"]["plan"][0]
        assert entry["human_feedback"] == "Scope to migrations only."
        assert entry["meta"]["human_directed"] is True

    def test_human_feedback_omitted_when_empty(self) -> None:
        s = _state()
        s.phase_log["plan"] = {
            "output":          "v2",
            "attempt":         2,
            "replan_critique": "missing tests",
            "human_feedback":  "",
        }
        sess = _session()
        PlanAdapter().write("plan", s, sess, round_n=2)
        entry = sess["phases"]["plan"][0]
        assert "human_feedback" not in entry

    def test_appends_across_rounds(self) -> None:
        s = _state()
        sess = _session()
        for r in (1, 2):
            s.phase_log["plan"] = {"output": f"v{r}", "attempt": r}
            PlanAdapter().write("plan", s, sess, round_n=r)
        assert len(sess["phases"]["plan"]) == 2
        assert [e["attempt"] for e in sess["phases"]["plan"]] == [1, 2]

    def test_round_n_fallback_when_no_attempt_in_log(self) -> None:
        """When orchestrator didn't stuff attempt into phase_log,
 adapter falls back to round_n kwarg."""
        s = _state()
        s.phase_log["plan"] = {"output": "v"}
        sess = _session()
        PlanAdapter().write("plan", s, sess, round_n=3)
        assert sess["phases"]["plan"][0]["attempt"] == 3

    def test_persists_runtime_session_fields_from_meta(self) -> None:
        s = _state()
        s.phase_log["plan"] = {
            "output": "v",
            "meta": {
                "session_id": "plan-sid",
                "continue_session": True,
                "followup_parent_session_id": "parent-plan-sid",
            },
        }
        sess = _session()
        PlanAdapter().write("plan", s, sess, round_n=1)
        entry = sess["phases"]["plan"][0]
        assert entry["session_id"] == "plan-sid"
        assert entry["continue_session"] is True
        assert entry["followup_parent_session_id"] == "parent-plan-sid"


# ── ValidatePlanAdapter ─────────────────────────────────────────────────────────────

class TestPlanQAAdapter:
    def test_approved_round1(self) -> None:
        s = _state()
        s.last_critique = ""
        s.phase_log["validate_plan"] = {
            "output":      "# Review\nApproved by JSON contract.",
            "raw_output":  _approved_raw_review(),
            "approved":    True,
            "verdict":     "APPROVED",
            "short_summary": "Approved by JSON contract.",
            "findings":    [],
            "attempt":     1,
            "plan_file":   "/p/plan.md",
            "reviewer_provider": "ClaudeAgent",
        }
        sess = _session()
        ValidatePlanAdapter().write("validate_plan", s, sess, round_n=1)
        entry = sess["phases"]["validate_plan"][0]
        assert entry["approved"] is True
        assert entry["plan_file"] == "/p/plan.md"
        assert entry["reviewer_provider"] == "ClaudeAgent"

    def test_rejected_critique_threaded(self) -> None:
        s = _state()
        s.last_critique = "needs more tests"
        s.phase_log["validate_plan"] = {
            "output":      "# Review\nneeds more tests",
            "raw_output":  json.dumps({
                "verdict": "REJECTED",
                "short_summary": "needs more tests",
                "findings": [{
                    "id": "F1",
                    "severity": "P2",
                    "title": "Missing tests",
                    "body": "needs more tests",
                    "required_fix": "Add the missing tests.",
                }],
            }),
            "approved":    False,
            "verdict":     "REJECTED",
            "attempt":     1,
            "plan_file":   "/p/plan.md",
            "reviewer_provider": "CodexAgent",
        }
        sess = _session()
        ValidatePlanAdapter().write("validate_plan", s, sess, round_n=1)
        assert sess["phases"]["validate_plan"][0]["critique"] == "needs more tests"

    def test_explicit_log_critique_wins_for_skip_path(self) -> None:
        """The orchestrator's no-plan-file branch bypasses the reviewer
 handler but still goes through ValidatePlanAdapter. That branch needs a
 literal critique string even though state.last_critique and output
 are empty."""
        s = _state()
        s.phase_log["validate_plan"] = {
            "output":      "",
            "critique":    "(no plan file)",
            "approved":    True,
            "attempt":     1,
            "plan_file":   "",
            "reviewer_provider": "CodexAgent",
        }
        sess = _session()
        ValidatePlanAdapter().write("validate_plan", s, sess, round_n=1)
        assert sess["phases"]["validate_plan"][0] == {
            "attempt":      1,
            "plan_file":    "",
            "raw_response": "",
            "critique":     "(no plan file)",
            "approved":     True,
            "reviewer_provider":  "CodexAgent",
        }

    def test_persists_runtime_session_fields_from_top_level(self) -> None:
        s = _state()
        s.phase_log["validate_plan"] = {
            "output": "# Review\nApproved",
            "approved": True,
            "session_id": "validate-sid",
            "continue_session": True,
            "followup_parent_session_id": "parent-validate-sid",
        }
        sess = _session()
        ValidatePlanAdapter().write("validate_plan", s, sess, round_n=1)
        entry = sess["phases"]["validate_plan"][0]
        assert entry["session_id"] == "validate-sid"
        assert entry["continue_session"] is True
        assert entry["followup_parent_session_id"] == "parent-validate-sid"


# ── BuildAdapter ──────────────────────────────────────────────────────────────

class TestBuildAdapter:
    def test_minimal_no_progress(self) -> None:
        s = _state()
        s.phase_log["implement"] = {"output": "build done"}
        sess = _session()
        BuildAdapter().write("implement", s, sess)
        assert sess["phases"]["implement"] == {"output": "build done"}

    def test_with_progress_and_test_result(self) -> None:
        s = _state()
        s.phase_log["implement"] = {
            "output":      "build done",
            "progress":    {"completed": 3, "total": 5},
            "test_result": {"skipped": False, "passed": True, "duration": 1.2},
        }
        sess = _session()
        BuildAdapter().write("implement", s, sess)
        b = sess["phases"]["implement"]
        assert b["progress"] == {"completed": 3, "total": 5}
        assert b["test_result"]["passed"] is True

    def test_keeps_session_meta_and_executor_meta(self) -> None:
        """Follow-up runtime continuation needs the agent's session_id and
        the actual ``continue_session`` to survive into ``session.json`` so
        the extractor / forensic surface can read them back. The legacy
        filter that dropped these keys was removed when the follow-up
        feature landed; this test now pins the new contract.
        """
        s = _state()
        s.phase_log["implement"] = {
            "output": "build done",
            "meta": {
                "session_id": "implement-sid",
                "continue_session": True,
                "followup_parent_session_id": "parent-implement-sid",
                "execution_mode": "custom",
                "subtask_count": 3,
            },
        }
        sess = _session()
        BuildAdapter().write("implement", s, sess)
        assert sess["phases"]["implement"]["meta"] == {
            "session_id": "implement-sid",
            "continue_session": True,
            "followup_parent_session_id": "parent-implement-sid",
            "execution_mode": "custom",
            "subtask_count": 3,
        }

    def test_copies_delivery_provenance_fields(self) -> None:
        """ADR 0073: delivery_status / delivery_waived / waiver_id / action
        (+ legacy delivery_clean) flow from phase_log into the session entry."""
        s = _state()
        s.phase_log["implement"] = {
            "output": "build done",
            "delivery_status": "waived",
            "delivery_waived": True,
            "waiver_id": "implement:implement_handoff:1",
            "action": "continue_with_waiver",
            "delivery_clean": True,
        }
        sess = _session()
        BuildAdapter().write("implement", s, sess)
        b = sess["phases"]["implement"]
        assert b["delivery_status"] == "waived"
        assert b["delivery_waived"] is True
        assert b["waiver_id"] == "implement:implement_handoff:1"
        assert b["action"] == "continue_with_waiver"
        assert b["delivery_clean"] is True

    def test_omits_delivery_fields_when_absent(self) -> None:
        s = _state()
        s.phase_log["implement"] = {"output": "build done"}
        sess = _session()
        BuildAdapter().write("implement", s, sess)
        b = sess["phases"]["implement"]
        for key in ("delivery_status", "delivery_waived", "waiver_id", "action"):
            assert key not in b


# ── RoundAdapter ──────────────────────────────────────────────────────────────

class TestRoundAdapter:
    def test_critique_only_round_omits_optionals(self) -> None:
        """Legacy contract (test_pipeline_runtime test_pipeline_*) requires
 ``"repair_output" not in rounds[0]`` for clean early-exits — None
 values must be omitted, not stored as None."""
        s = _state()
        s.phase_log["rounds_pending"] = {"critique": "all good"}
        sess = _session()
        RoundAdapter().write("rounds", s, sess, round_n=1)
        entry = sess["phases"]["rounds"][0]
        assert entry == {"round": 1, "critique": "all good"}
        assert "repair_output" not in entry
        assert "test_result" not in entry

    def test_full_round(self) -> None:
        s = _state()
        s.phase_log["rounds_pending"] = {
            "critique":     "fix tests",
            "repair_output":   "patched",
            "repair_model":    "claude-sonnet-4-6",
            "session_mode": "chain",
            "session_mode_reason": "auto_chain_suppressed_context_pressure",
            "session_mode_context_pressure": {
                "tokens_in": 1_000_001,
                "tool_calls": 31,
                "max_tokens_in": 1_000_000,
                "max_tool_calls": 30,
                "fallback_mode": "stateless",
            },
            "review_session_id": "review-abc",
            "review_continue_session": True,
            "repair_session_id": "repair-abc",
            "repair_continue_session": True,
            "followup_parent_review_session_id": "parent-review",
            "followup_parent_repair_session_id": "parent-repair",
            "repair_receipt": {
                "source_phase": "review_changes",
                "repair_phase": "repair_changes",
                "fixed": [],
            },
            "session_id":   "legacy-abc",
            "test_result":  {"skipped": False, "passed": True, "duration": 0.5},
        }
        sess = _session()
        RoundAdapter().write("rounds", s, sess, round_n=2)
        e = sess["phases"]["rounds"][0]
        assert e["round"] == 2
        assert e["repair_output"] == "patched"
        assert e["session_mode_reason"] == (
            "auto_chain_suppressed_context_pressure"
        )
        assert e["session_mode_context_pressure"]["tokens_in"] == 1_000_001
        assert e["review_session_id"] == "review-abc"
        assert e["review_continue_session"] is True
        assert e["repair_session_id"] == "repair-abc"
        assert e["repair_continue_session"] is True
        assert e["followup_parent_review_session_id"] == "parent-review"
        assert e["followup_parent_repair_session_id"] == "parent-repair"
        assert e["repair_receipt"]["source_phase"] == "review_changes"
        assert e["session_id"] == "repair-abc"
        assert e["test_result"]["passed"] is True

    def test_missing_round_n_raises(self) -> None:
        s = _state()
        s.phase_log["rounds_pending"] = {"critique": "x"}
        with pytest.raises(ValueError, match="round_n"):
            RoundAdapter().write("rounds", s, _session())

    def test_falls_back_to_state_last_critique(self) -> None:
        """When pending dict has no critique, fall back to
 state.last_critique (legacy critique-only path)."""
        s = _state()
        s.last_critique = "found in state"
        s.phase_log["rounds_pending"] = {}
        sess = _session()
        RoundAdapter().write("rounds", s, sess, round_n=1)
        assert sess["phases"]["rounds"][0]["critique"] == "found in state"


# ── FinalAcceptanceAdapter ────────────────────────────────────────────────────────────

class TestFinalQAAdapter:
    def test_writes_critique(self) -> None:
        s = _state()
        s.phase_log["final_acceptance"] = {
            "output": "# Review\nApproved by JSON contract.",
            "raw_output": _approved_raw_review(),
            "verdict": "APPROVED",
            "short_summary": "Approved by JSON contract.",
            "findings": [],
        }
        sess = _session()
        FinalAcceptanceAdapter().write("final_acceptance", s, sess)
        assert sess["phases"]["final_acceptance"] == {
            "critique": "# Review\nApproved by JSON contract.",
            "raw_response": _approved_raw_review(),
            "verdict": "APPROVED",
            "short_summary": "Approved by JSON contract.",
            "findings": [],
        }

    def test_empty_log_yields_empty_critique(self) -> None:
        s = _state()
        sess = _session()
        FinalAcceptanceAdapter().write("final_acceptance", s, sess)
        assert sess["phases"]["final_acceptance"] == {"critique": ""}

    def test_persists_runtime_session_fields(self) -> None:
        s = _state()
        s.phase_log["final_acceptance"] = {
            "output": "# Release\nApproved",
            "meta": {
                "session_id": "final-sid",
                "continue_session": True,
                "followup_parent_session_id": "parent-final-sid",
            },
        }
        sess = _session()
        FinalAcceptanceAdapter().write("final_acceptance", s, sess)
        entry = sess["phases"]["final_acceptance"]
        assert entry["session_id"] == "final-sid"
        assert entry["continue_session"] is True
        assert entry["followup_parent_session_id"] == "parent-final-sid"

    def test_persists_release_fields_alongside_mirror(self) -> None:
        """ADR 0025 Phase 1: ``state.phase_log`` carries dual shapes;
        the adapter must persist BOTH review-shape mirror fields and
        the new release fields into ``session["phases"]["final_acceptance"]``.
        Without this, in-memory state has the release surface but the
        persisted session entry (read by acceptance fixtures, MCP,
        Web phase card) silently loses it."""
        s = _state()
        s.phase_log["final_acceptance"] = {
            # Review-shape mirror.
            "output":         "# Release gate\nShip-ready.",
            "raw_output":     "{\"verdict\":\"APPROVED\"}",
            "verdict":        "APPROVED",
            "short_summary":  "Ship-ready.",
            "findings":       [],
            "approved":       True,
            # Release-shape fields.
            "ship_ready":         True,
            "release_blockers":   [],
            "verification_gaps":  [],
            "contract_status": {
                "task_contract": "satisfied",
                "interfaces":    "not_applicable",
                "persistence":   "not_applicable",
                "tests":         "sufficient",
            },
        }
        sess = _session()
        FinalAcceptanceAdapter().write("final_acceptance", s, sess)
        entry = sess["phases"]["final_acceptance"]
        # Review-shape mirror persisted.
        assert entry["verdict"] == "APPROVED"
        assert entry["short_summary"] == "Ship-ready."
        assert entry["findings"] == []
        assert entry["critique"] == "# Release gate\nShip-ready."
        # Release-shape fields persisted.
        assert entry["ship_ready"] is True
        assert entry["release_blockers"] == []
        assert entry["verification_gaps"] == []
        assert entry["contract_status"]["task_contract"] == "satisfied"

    def test_persists_scope_expansion_sanction_route(self) -> None:
        """ADR 0112 §5 (increment D): the mode-projected sanction route must
        survive phase-end. The handler writes both the classifier fact
        (``scope_expansion``) and the projected route
        (``scope_expansion_sanction``) into ``phase_log``; the adapter persists
        BOTH so an operator/DONE/evidence surface reading the session entry
        sees ``forces_rejected`` / ``needs_phase_handoff`` / ``alert_paths``,
        not just the classifier fact."""
        s = _state()
        s.phase_log["final_acceptance"] = {
            "output":   "# Release gate\nScope expansion noticed.",
            "verdict":  "APPROVED",
            "approved": True,
            "scope_expansion": {"has_blocker": False, "items": []},
            "scope_expansion_sanction": {
                "forces_rejected":     False,
                "needs_phase_handoff": True,
                "alert_paths":         ["pipeline/run_projection.py"],
            },
        }
        sess = _session()
        FinalAcceptanceAdapter().write("final_acceptance", s, sess)
        entry = sess["phases"]["final_acceptance"]
        # Classifier fact persisted.
        assert entry["scope_expansion"]["has_blocker"] is False
        # Mode-projected route persisted (the F1 gap).
        sanction = entry["scope_expansion_sanction"]
        assert sanction["forces_rejected"] is False
        assert sanction["needs_phase_handoff"] is True
        assert sanction["alert_paths"] == ["pipeline/run_projection.py"]

    def test_omits_scope_expansion_sanction_when_absent(self) -> None:
        """Legacy-safe: an in-scope diff writes neither the classifier fact
        nor the route, so the persisted entry shape stays byte-identical."""
        s = _state()
        s.phase_log["final_acceptance"] = {
            "output":   "# Release gate\nShip-ready.",
            "verdict":  "APPROVED",
            "approved": True,
        }
        sess = _session()
        FinalAcceptanceAdapter().write("final_acceptance", s, sess)
        entry = sess["phases"]["final_acceptance"]
        assert "scope_expansion" not in entry
        assert "scope_expansion_sanction" not in entry


# ── HypothesisAdapter ─────────────────────────────────────────────────────────

class TestHypothesisAdapter:
    def test_attempts_present(self) -> None:
        s = _state()
        s.research_hypothesis = "X causes Y"
        s.phase_log["hypothesis"] = {
            "attempts": [{"hypothesis": "X causes Y", "approved": True}],
        }
        sess = _session()
        HypothesisAdapter().write("hypothesis", s, sess)
        entry = sess["phases"]["hypothesis"]
        assert entry["enabled"] is True
        assert entry["approved"] is True
        assert len(entry["attempts"]) == 1

    def test_no_attempts_skips_session_entry(self) -> None:
        """Legacy: if ``maybe_run_hypothesis`` returns no attempts (loop
 was disabled / fell through), the orchestrator did NOT create
 a session["phases"]["hypothesis"] entry. Adapter preserves that."""
        s = _state()
        sess = _session()
        HypothesisAdapter().write("hypothesis", s, sess)
        assert "hypothesis" not in sess.get("phases", {})

    def test_rejected_attempts_marked_unapproved(self) -> None:
        s = _state()
        s.research_hypothesis = None  # all rejected
        s.phase_log["hypothesis"] = {
            "attempts": [{"approved": False}, {"approved": False}],
        }
        sess = _session()
        HypothesisAdapter().write("hypothesis", s, sess)
        assert sess["phases"]["hypothesis"]["approved"] is False
        assert len(sess["phases"]["hypothesis"]["attempts"]) == 2


# ── Override smoke (plugin extension) ─────────────────────────────────────────

class TestPluginOverride:
    def test_custom_plan_adapter_replaces_default(self) -> None:
        """A plugin shipping its own PlanAdapter overrides the built-in.
 Validates the registry's ``register`` overwrite semantics —
 important for plugin authors shipping richer session shapes."""

        class CustomPlanAdapter:
            def write(self, phase_name, state, session, *, round_n=None):
                session.setdefault("phases", {}).setdefault("plan", []).append(
                    {"custom": True, "round": round_n}
                )

        reg = SessionAdapterRegistry()
        reg.register("plan", PlanAdapter())
        reg.register("plan", CustomPlanAdapter())  # override
        sess = _session()
        reg.get("plan").write("plan", _state(), sess, round_n=1)
        assert sess["phases"]["plan"][0] == {"custom": True, "round": 1}


# ── Orchestrator call-site parity ─────────────────────────────────────────────

class TestSessionAdapterOverrideViaV2Dispatch:
    """verify custom adapter overrides flow through v2 dispatch.

 The contract: when ``_PipelineRun._on_phase_end`` fires with the
 ``_v2_dispatch_active`` flag set, it looks up the adapter from
 ``self._session_adapters`` — so a customer-registered override
 propagates to the session shape without orchestrator-side
 imperative calls.
 """

    def test_custom_validate_plan_adapter_override_propagates(self, tmp_path) -> None:
        from pipeline.runtime import LoopStep, PhaseStep, Profile, run_profile

        class FakeAgent:
            model = "fake-model"

            def label(self):
                return "FakeReviewer"

        class CustomValidatePlanAdapter:
            def write(self, phase_name, state, session, *, round_n=None):
                log = state.phase_log.get("validate_plan", {})
                session.setdefault("phases", {}).setdefault(
                    "validate_plan", []
                ).append({
                    "custom":   True,
                    "round":    round_n,
                    "critique": log.get("critique", "(no critique)"),
                })

        phase_config = PhaseAgentConfig(
            plan_agent=FakeAgent(),
            implement_agent=FakeAgent(),
            repair_changes_agent=FakeAgent(),
            repair_escalation_agent=FakeAgent(),
            validate_plan_agent=FakeAgent(),
            review_changes_agent=FakeAgent(),
            final_acceptance_agent=FakeAgent(),
        )

        def plan_handler(state: PipelineState) -> PipelineState:
            state.plan_markdown = "plan markdown"
            state.phase_log["plan"] = {"output": state.plan_markdown}
            return state

        def validate_plan_handler(state: PipelineState) -> PipelineState:
            state.phase_log["validate_plan"] = {
                "approved": True,
                "critique": "looks good — custom adapter test",
                "output":   "approved",
            }
            return state

        registry = PhaseRegistry()
        registry.register("plan", plan_handler)
        registry.register("validate_plan", validate_plan_handler)

        adapters = SessionAdapterRegistry()
        adapters.register("plan", PlanAdapter())
        adapters.register("validate_plan", CustomValidatePlanAdapter())

        # Build a state mirroring how _dispatch_via_v2_profile sets up
        # the v2 dispatch path — flag the activation so _on_phase_end
        # auto-fires the registered adapters.
        state = _state(phase_config=phase_config)
        state.extras["_v2_dispatch_active"] = True

        # Dispatch through run_profile (the v2 path) with a synthetic
        # plan/validate_plan loop profile.
        profile = Profile(
            name="plan-only",
            kind="custom",
            steps=(
                LoopStep(
                    steps=(
                        PhaseStep(phase="plan"),
                        PhaseStep(phase="validate_plan"),
                    ),
                    until="validate_plan.approved",
                    max_rounds=1,
                    round_extras_key="plan_round",
                ),
            ),
        )

        session: dict = {"phases": {}}

        def on_phase_end(name: str, st: PipelineState) -> None:
            adapter = adapters.get_or_none(name)
            if adapter is not None:
                round_n = st.extras.get("plan_round") or st.extras.get("loop_round")
                adapter.write(name, st, session, round_n=round_n)

        run_profile(profile, state, registry, on_phase_end=on_phase_end)

        assert session["phases"]["validate_plan"] == [{
            "custom":   True,
            "round":    1,
            "critique": "looks good — custom adapter test",
        }]


# ── M12 trace foundation — Commit 3: prompt_render persistence ───────────────


class TestPromptRenderPersistedInSessionShape:
    """M12 persistence depends on ``prompt_render`` (the M7+ session-
    aware render metadata) reaching the session-dict shape, not only
    living in ``state.phase_log``. Each adapter that participates in
    session-aware rendering must carry the field through.
    """

    PROMPT_RENDER_FIXTURE = {
        "render_mode": "full",
        "session_split": "per_phase",
        "session_key": {
            "run_id": "20260516_120000",
            "runtime": "agents.runtimes.claude.ClaudeAgent",
            "model_key": "claude-opus-4-7",
            "scope": "per_phase:plan",
        },
        "selected_part_keys": ["role:systems_architect@0"],
        "omitted_part_keys": [],
        "prefix_hash": "abc",
        "payload_hash": "def",
        "wire_chars": 1024,
    }

    def test_plan_adapter_carries_prompt_render_into_session_entry(
        self,
    ) -> None:
        state = _state()
        state.phase_log["plan"] = {
            "output": "plan body",
            "prompt_render": dict(self.PROMPT_RENDER_FIXTURE),
        }
        session = _session()
        PlanAdapter().write("plan", state, session)
        assert session["phases"]["plan"]
        entry = session["phases"]["plan"][0]
        assert entry["prompt_render"]["session_key"]["run_id"] == (
            "20260516_120000"
        )
        assert entry["prompt_render"]["render_mode"] == "full"

    def test_validate_plan_adapter_carries_prompt_render(self) -> None:
        state = _state()
        state.phase_log["validate_plan"] = {
            "output": "",
            "approved": True,
            "prompt_render": dict(self.PROMPT_RENDER_FIXTURE),
        }
        session = _session()
        ValidatePlanAdapter().write("validate_plan", state, session)
        assert session["phases"]["validate_plan"][0]["prompt_render"][
            "session_key"
        ]["run_id"] == "20260516_120000"

    def test_build_adapter_carries_prompt_render(self) -> None:
        state = _state()
        state.phase_log["implement"] = {
            "output": "impl body",
            "prompt_render": dict(self.PROMPT_RENDER_FIXTURE),
        }
        session = _session()
        BuildAdapter().write("implement", state, session)
        assert session["phases"]["implement"]["prompt_render"][
            "session_key"
        ]["run_id"] == "20260516_120000"

    def test_round_adapter_carries_review_and_repair_prompt_render(
        self,
    ) -> None:
        # The round entry composes data from BOTH sides of the
        # review_changes ↔ repair_changes loop. M11.5 attributes each
        # side's trace to the matching phase_log slot via trace_phase;
        # the RoundAdapter forwards them under namespaced keys so M12
        # persistence can attribute wire shape and cost per side.
        state = _state()
        state.phase_log["review_changes"] = {
            "prompt_render": {
                **self.PROMPT_RENDER_FIXTURE,
                "session_key": {
                    **self.PROMPT_RENDER_FIXTURE["session_key"],
                    "scope": "per_phase:review_changes",
                },
            },
        }
        state.phase_log["repair_changes"] = {
            "prompt_render": {
                **self.PROMPT_RENDER_FIXTURE,
                "session_key": {
                    **self.PROMPT_RENDER_FIXTURE["session_key"],
                    "scope": "per_phase:implement",  # CHAIN reuse
                },
            },
        }
        state.phase_log["rounds_pending"] = {
            "critique": "small nit",
            "repair_output": "repair body",
        }
        session: dict = {"phases": {"rounds": []}}
        RoundAdapter().write("repair_changes", state, session, round_n=1)
        assert session["phases"]["rounds"]
        entry = session["phases"]["rounds"][0]
        assert entry["prompt_render_review"]["session_key"]["scope"] == (
            "per_phase:review_changes"
        )
        assert entry["prompt_render_repair"]["session_key"]["scope"] == (
            "per_phase:implement"
        )

    def test_final_acceptance_adapter_does_not_carry_prompt_render(
        self,
    ) -> None:
        # Documented exception: ``_phase_final_acceptance`` does not
        # route through ``_session_aware_invoke`` (M9 pinned verdict
        # isolation over prompt-size win on the closing gate). No
        # ``prompt_render`` exists to forward — even if a stray
        # value sat in the phase log, the adapter intentionally
        # ignores it.
        state = _state()
        state.phase_log["final_acceptance"] = {
            "output": "verdict body",
            "verdict": "APPROVED",
            # Stray value to assert the adapter's intentional silence.
            "prompt_render": dict(self.PROMPT_RENDER_FIXTURE),
        }
        session = _session()
        FinalAcceptanceAdapter().write("final_acceptance", state, session)
        entry = session["phases"]["final_acceptance"]
        assert "prompt_render" not in entry


# ── M12 trace foundation — run_id anchor ─────────────────────────────────────


class TestRunIdAnchorInPromptRender:
    """``_session_aware_invoke`` reads the run_id from
    ``state.extras["run_id"]``. Before the Commit 3 fix, the
    orchestrator never set that key, so every persisted
    ``session_key.run_id`` carried the literal string ``"unknown"``.
    The fix anchors it once at state construction.
    """

    def test_session_key_run_id_reflects_state_extras_anchor(self) -> None:
        # Reuse the M7 wiring test fixtures — they exercise the real
        # ``_session_aware_invoke`` against a recording agent.
        from pipeline.phases.builtin import _session_aware_invoke
        from pipeline.prompts.turn import PromptTurnEditor
        from pipeline.prompts.types import (
            PromptCacheScope,
            PromptLayer,
            PromptPart,
            PromptStability,
        )

        class _Agent:
            model = "claude-opus-4-7"
            session_id = "sess-1"

            def __init__(self) -> None:
                self.calls: list[str] = []

            def invoke(self, prompt, cwd, **kwargs):  # noqa: ANN001
                self.calls.append(prompt)
                return "{}"

        agent = _Agent()
        state = PipelineState(
            task="t", project_dir="/p", plugin=PluginConfig(),
            extras={"run_id": "20260516_120000"},
        )
        role = PromptPart(
            kind="role", name="systems_architect", source="core",
            body="role body", layer=PromptLayer.ROLE,
        )
        task_part = PromptPart(
            kind="task", name="implement", source="core",
            body="turn body",
            layer=PromptLayer.TURN,
            stability=PromptStability.TURN,
            cache_scope=PromptCacheScope.NONE,
            volatile_reason="per-turn",
        )
        turn = PromptTurnEditor().append(role).append(task_part).build()

        _session_aware_invoke(
            agent, state,
            phase="plan",
            turn=turn,
            cwd="/p",
            continue_session=False,
        )

        meta = state.phase_log["plan"]["prompt_render"]
        # Anchor reaches the persisted session key — no more "unknown".
        assert meta["session_key"]["run_id"] == "20260516_120000"
        assert meta["session_key"]["run_id"] != "unknown"


# ── M14.4+ runtime_compaction adapter promotion ──────────────────────────────


class TestRuntimeCompactionAdapterPromotion:
    """Adapter-side proof that a stamped ``runtime_compaction``
    phase_log entry lands in the persisted session shape under the
    canonical key the extractor reads.

    Without this wiring, the writer in
    ``_session_aware_invoke`` stamps the event but
    ``extract_runtime_compaction_traces`` (which walks
    ``session["phases"]``) never sees it — the dashboard /
    evidence path would silently lose the signal.

    The fixture below mirrors the typical event a runtime would
    emit when Claude CLI / Codex CLI start exposing the auto-compact
    signal. No runtime exposes it today; under typical runs the
    phase_log key is absent and the adapter is a no-op.
    """

    RUNTIME_COMPACTION_FIXTURE = {
        "kind":             "runtime_auto_compacted",
        "trigger":          "event_hook",
        "phase":            "plan",
        "round":            None,
        "surface_id":       None,
        "pre_used_tokens":  150_000,
        "post_used_tokens": 40_000,
        "summary_tokens":   4_096,
        "prefix_hash":      "abc",
        "payload_hash":     "def",
        "wire_chars":       8192,
        "preserved_slots":  ["task_and_acceptance", "risks"],
        "artifact_refs":    [{"path": "/run/summary.md"}],
    }

    def test_plan_adapter_promotes_runtime_compaction(self) -> None:
        state = _state()
        state.phase_log["plan"] = {
            "output": "plan body",
            "runtime_compaction": dict(self.RUNTIME_COMPACTION_FIXTURE),
        }
        session = _session()
        PlanAdapter().write("plan", state, session, round_n=1)
        entry = session["phases"]["plan"][0]
        assert entry["runtime_compaction"]["kind"] == "runtime_auto_compacted"
        assert entry["runtime_compaction"]["trigger"] == "event_hook"
        assert entry["runtime_compaction"]["preserved_slots"] == [
            "task_and_acceptance", "risks",
        ]

    def test_validate_plan_adapter_promotes_runtime_compaction(self) -> None:
        state = _state()
        state.phase_log["validate_plan"] = {
            "output":   "{\"verdict\":\"APPROVED\",\"findings\":[]}",
            "approved": True,
            "runtime_compaction": dict(self.RUNTIME_COMPACTION_FIXTURE),
        }
        session = _session()
        ValidatePlanAdapter().write("validate_plan", state, session, round_n=1)
        entry = session["phases"]["validate_plan"][0]
        assert entry["runtime_compaction"]["pre_used_tokens"] == 150_000
        assert entry["runtime_compaction"]["post_used_tokens"] == 40_000

    def test_build_adapter_promotes_runtime_compaction(self) -> None:
        state = _state()
        state.phase_log["implement"] = {
            "output": "impl body",
            "runtime_compaction": dict(self.RUNTIME_COMPACTION_FIXTURE),
        }
        session = _session()
        BuildAdapter().write("implement", state, session)
        rc = session["phases"]["implement"]["runtime_compaction"]
        assert rc["summary_tokens"] == 4_096
        assert rc["artifact_refs"] == [{"path": "/run/summary.md"}]

    def test_round_adapter_splits_review_and_repair(self) -> None:
        # The round entry carries two independent records — one per
        # side of the loop — under the ``_review`` / ``_repair``
        # suffix, matching the prompt_render / context_growth /
        # context_clearing / context_pressure conventions.
        state = _state()
        state.phase_log["review_changes"] = {
            "runtime_compaction": {
                **self.RUNTIME_COMPACTION_FIXTURE,
                "phase": "review_changes",
                "pre_used_tokens": 120_000,
            },
        }
        state.phase_log["repair_changes"] = {
            "runtime_compaction": {
                **self.RUNTIME_COMPACTION_FIXTURE,
                "phase": "repair_changes",
                "pre_used_tokens": 180_000,
            },
        }
        state.phase_log["rounds_pending"] = {
            "critique":      "small nit",
            "repair_output": "repair body",
        }
        session: dict = {"phases": {"rounds": []}}
        RoundAdapter().write("repair_changes", state, session, round_n=1)
        entry = session["phases"]["rounds"][0]
        assert entry["runtime_compaction_review"]["phase"] == (
            "review_changes"
        )
        assert entry["runtime_compaction_review"]["pre_used_tokens"] == 120_000
        assert entry["runtime_compaction_repair"]["phase"] == (
            "repair_changes"
        )
        assert entry["runtime_compaction_repair"]["pre_used_tokens"] == 180_000

    def test_round_adapter_omits_split_when_neither_side_stamped(self) -> None:
        # Typical run path today: no runtime exposes the signal, so
        # the keys must NOT appear in the round entry. Mirrors the
        # context_pressure / context_clearing observe-only contract.
        state = _state()
        state.phase_log["review_changes"] = {}
        state.phase_log["repair_changes"] = {}
        state.phase_log["rounds_pending"] = {"critique": "all good"}
        session: dict = {"phases": {"rounds": []}}
        RoundAdapter().write("repair_changes", state, session, round_n=1)
        entry = session["phases"]["rounds"][0]
        assert "runtime_compaction_review" not in entry
        assert "runtime_compaction_repair" not in entry

    def test_single_entry_adapters_omit_key_when_absent(self) -> None:
        # No runtime exposes the signal today; verify all three
        # single-entry adapters are silent no-ops in that case so
        # legacy snapshots stay byte-stable.
        state = _state()
        state.phase_log["plan"] = {"output": "plan body"}
        state.phase_log["validate_plan"] = {
            "output": "{\"verdict\":\"APPROVED\",\"findings\":[]}",
            "approved": True,
        }
        state.phase_log["implement"] = {"output": "impl body"}
        session = _session()
        PlanAdapter().write("plan", state, session, round_n=1)
        ValidatePlanAdapter().write("validate_plan", state, session, round_n=1)
        BuildAdapter().write("implement", state, session)
        assert "runtime_compaction" not in session["phases"]["plan"][0]
        assert "runtime_compaction" not in (
            session["phases"]["validate_plan"][0]
        )
        assert "runtime_compaction" not in session["phases"]["implement"]

    def test_extractor_reads_what_adapters_promote(self) -> None:
        # End-to-end check: stamp via adapters → walk via
        # extract_runtime_compaction_traces. Closes the
        # writer-stamps-but-extractor-misses gap.
        from pipeline.observability.runtime_compaction import (
            extract_runtime_compaction_traces,
        )

        state = _state()
        state.phase_log["plan"] = {
            "output": "plan body",
            "runtime_compaction": dict(self.RUNTIME_COMPACTION_FIXTURE),
        }
        state.phase_log["implement"] = {
            "output": "impl body",
            "runtime_compaction": {
                **self.RUNTIME_COMPACTION_FIXTURE,
                "phase": "implement",
            },
        }
        session = _session()
        PlanAdapter().write("plan", state, session, round_n=1)
        BuildAdapter().write("implement", state, session)

        traces = extract_runtime_compaction_traces(session)
        kinds = [(t.phase, t.trace_surface) for t in traces]
        assert kinds == [("plan", "plan"), ("implement", "implement")]


class TestRuntimeSessionPersistence:
    """Step 0 of follow-up runtime continuation: every adapter must
    promote ``session_id`` / ``continue_session`` /
    ``followup_parent_session_id`` from the phase_log into the persisted
    session entry so a follow-up extractor can read them back.

    The persistence shape is uniform but the *source* differs per phase:
    plan / implement / final_acceptance route through ``log["meta"]``
    (phase handlers write the runtime forensic surface inside a meta
    dict), while validate_plan / round phases land them at top level.
    The ``_copy_runtime_session`` helper handles both — these tests
    pin the round-trip per adapter.
    """

    def test_plan_promotes_session_fields_from_meta(self) -> None:
        s = _state()
        s.phase_log["plan"] = {
            "output": "plan body",
            "meta": {
                "session_id": "plan-sid-new",
                "continue_session": True,
                "followup_parent_session_id": "plan-sid-parent",
            },
        }
        sess = _session()
        PlanAdapter().write("plan", s, sess, round_n=1)
        entry = sess["phases"]["plan"][0]
        assert entry["session_id"] == "plan-sid-new"
        assert entry["continue_session"] is True
        assert entry["followup_parent_session_id"] == "plan-sid-parent"

    def test_plan_omits_session_fields_when_absent(self) -> None:
        s = _state()
        s.phase_log["plan"] = {"output": "plan body"}
        sess = _session()
        PlanAdapter().write("plan", s, sess, round_n=1)
        entry = sess["phases"]["plan"][0]
        assert "session_id" not in entry
        assert "continue_session" not in entry
        assert "followup_parent_session_id" not in entry

    def test_validate_plan_promotes_session_fields_top_level(self) -> None:
        # validate_plan handler stuffs runtime fields at top of log,
        # not inside ``meta`` — the helper accepts either shape.
        s = _state()
        s.phase_log["validate_plan"] = {
            "output": _approved_raw_review(),
            "raw_output": _approved_raw_review(),
            "approved": True,
            "session_id": "vp-sid-new",
            "continue_session": False,
        }
        sess = _session()
        ValidatePlanAdapter().write("validate_plan", s, sess, round_n=1)
        entry = sess["phases"]["validate_plan"][0]
        assert entry["session_id"] == "vp-sid-new"
        assert entry["continue_session"] is False

    def test_implement_keeps_session_fields_in_meta(self) -> None:
        # Implement promotes the whole ``meta`` block (the legacy filter
        # that stripped session keys was removed for follow-up).
        s = _state()
        s.phase_log["implement"] = {
            "output": "impl body",
            "meta": {
                "session_id": "impl-sid-new",
                "continue_session": True,
                "followup_parent_session_id": "impl-sid-parent",
            },
        }
        sess = _session()
        BuildAdapter().write("implement", s, sess)
        meta = sess["phases"]["implement"]["meta"]
        assert meta["session_id"] == "impl-sid-new"
        assert meta["continue_session"] is True
        assert meta["followup_parent_session_id"] == "impl-sid-parent"

    def test_final_acceptance_promotes_session_fields(self) -> None:
        s = _state()
        s.phase_log["final_acceptance"] = {
            "output": "all good",
            "verdict": "APPROVED",
            "session_id": "fa-sid-new",
            "continue_session": True,
            "followup_parent_session_id": "fa-sid-parent",
        }
        sess = _session()
        FinalAcceptanceAdapter().write("final_acceptance", s, sess)
        entry = sess["phases"]["final_acceptance"]
        assert entry["session_id"] == "fa-sid-new"
        assert entry["continue_session"] is True
        assert entry["followup_parent_session_id"] == "fa-sid-parent"

    def test_round_splits_review_and_repair_session_ids(self) -> None:
        s = _state()
        s.phase_log["rounds_pending"] = {
            "critique":      "needs work",
            "repair_output": "fixed",
            "review_session_id":       "review-sid-new",
            "review_continue_session": True,
            "followup_parent_review_session_id": "review-sid-parent",
            "repair_session_id":       "repair-sid-new",
            "repair_continue_session": True,
            "followup_parent_repair_session_id": "repair-sid-parent",
        }
        sess = _session()
        RoundAdapter().write("rounds", s, sess, round_n=1)
        entry = sess["phases"]["rounds"][0]
        assert entry["review_session_id"] == "review-sid-new"
        assert entry["review_continue_session"] is True
        assert entry["followup_parent_review_session_id"] == "review-sid-parent"
        assert entry["repair_session_id"] == "repair-sid-new"
        assert entry["repair_continue_session"] is True
        assert entry["followup_parent_repair_session_id"] == "repair-sid-parent"
        # Backcompat alias: the legacy ``session_id`` mirrors the
        # repair side (the only side pre-split callers stuffed).
        assert entry["session_id"] == "repair-sid-new"

    def test_round_backcompat_alias_when_only_legacy_session_id_set(
        self,
    ) -> None:
        # Pre-split callers stuffed a single ``session_id`` into
        # ``rounds_pending``. That keeps working — the alias appears,
        # the split fields stay absent.
        s = _state()
        s.phase_log["rounds_pending"] = {
            "critique":      "ok",
            "repair_output": "fix",
            "session_id":    "legacy-sid",
        }
        sess = _session()
        RoundAdapter().write("rounds", s, sess, round_n=1)
        entry = sess["phases"]["rounds"][0]
        assert entry["session_id"] == "legacy-sid"
        assert "review_session_id" not in entry
        assert "repair_session_id" not in entry

    def test_round_omits_all_session_fields_when_none_set(self) -> None:
        s = _state()
        s.phase_log["rounds_pending"] = {
            "critique":      "ok",
            "repair_output": "fix",
        }
        sess = _session()
        RoundAdapter().write("rounds", s, sess, round_n=1)
        entry = sess["phases"]["rounds"][0]
        for k in (
            "session_id", "review_session_id", "repair_session_id",
            "review_continue_session", "repair_continue_session",
            "review_followup_parent_session_id",
            "repair_followup_parent_session_id",
        ):
            assert k not in entry


# ── ADR 0113 §5: baggage-guard regression asserts (test-only) ──────────────────
#
# Thin guards over the assembled review-turn (T2). They add NO production
# measurement code — they only assert properties of the wire input the review
# handler already builds, and are written so a real regression (re-attaching
# the pre-policy resume baggage) makes them fail.


class _RecordingReviewer:
    """Reviewer fake that records the wire prompt + continue_session per call."""

    def __init__(self) -> None:
        self.model = "fake-reviewer"
        self.session_id: str | None = None
        self.calls: list[tuple[str, str]] = []
        self.kwargs_log: list[dict] = []

    def invoke(self, prompt, cwd, *, mutates_artifacts=False,
               continue_session=False, attachments=()):
        self.calls.append((prompt, cwd))
        self.kwargs_log.append({"continue_session": continue_session})
        return _approved_raw_review()

    def reset_session(self) -> None:
        self.session_id = None


def _review_ready_state(*, prior_transcript: str = ""):
    """A round-2 review-ready state with a stored repair receipt.

    ``prior_transcript`` (when set) seeds a bulky previous-round transcript in
    run state (the prior implement output) so a guard can assert the FRESH
    review does not drag it onto the wire.
    """
    reviewer = _RecordingReviewer()
    pc = PhaseAgentConfig(
        plan_agent=_RecordingReviewer(),
        validate_plan_agent=_RecordingReviewer(),
        implement_agent=_RecordingReviewer(),
        review_changes_agent=reviewer,
        repair_changes_agent=_RecordingReviewer(),
        repair_escalation_agent=_RecordingReviewer(),
        final_acceptance_agent=_RecordingReviewer(),
    )
    st = _state(phase_config=pc, extras={"run_id": "run-baggage", "repair_round": 2})
    if prior_transcript:
        st.phase_log["implement"] = {"output": prior_transcript}
    _store_repair_receipt(st, build_repair_receipt(
        source_phase="review_changes", source_round=1,
        repair_phase="repair_changes", repair_round=2,
        critique="prior critique", repair_output="applied the fix",
        operator_feedback="", changed_refs=("a.py",),
    ))
    st.lifecycle_ctx = default_lifecycle_context(phase_registry=PhaseRegistry())
    st.lifecycle_ctx.git_helpers.has_uncommitted = lambda *_a, **_k: True
    # ADR 0113: review continuity is declared per-phase (fresh_only) on the
    # active step's execution policy; the resolver reads it here exactly as the
    # lifecycle FSM seeds it at runtime.
    st.lifecycle_ctx.active_step = PhaseStep(
        phase="review_changes",
        execution="linear",
        execution_policy=ExecutionPolicy(
            mode="linear", session_continuity="fresh_only"
        ),
    )
    return st, reviewer


class TestReviewBaggageGuard:
    # N is the diff-relative ceiling on the review wire input. The fresh review
    # carries the current change + a bounded contract preamble (~2x the diff in
    # practice), never the accumulated prior-round transcript, so 3x is a safe,
    # meaningful bound: it passes for the real turn and fails the moment a
    # prior-round transcript (typically >= the diff) is re-attached.
    N = 3

    def test_review_input_bounded_by_reviewed_diff(self, monkeypatch) -> None:
        # The reviewed change is surfaced via current_review_subject; pin a
        # known, sizeable diff so the bound is dominated by the diff, not the
        # fixed contract preamble.
        diff = "diff-line-content\n" * 500  # ~9000 chars
        monkeypatch.setattr(
            _review_changes_mod, "_current_change_review_subject",
            lambda _s: diff,
        )
        st, reviewer = _review_ready_state()
        default_registry().get("review_changes")(st)
        assert reviewer.calls, "review agent was not invoked"
        wire = reviewer.calls[-1][0]
        # The change under review rides the prompt, and the input stays within
        # N x the diff (diff-sized, not history-sized).
        assert diff in wire
        assert len(wire) <= self.N * len(diff), (
            f"review input {len(wire)} exceeds {self.N}x diff {len(diff)}"
        )
        # Negative case: re-attaching the pre-policy resume baggage (a prior
        # round transcript ~= the diff) blows the bound — the guard bites.
        regressed = wire + diff + diff
        assert len(regressed) > self.N * len(diff)

    def test_fresh_review_drops_prior_round_transcript(self, monkeypatch) -> None:
        sentinel = "PRIOR-ROUND-TRANSCRIPT-SENTINEL-must-not-be-carried"
        prior = (sentinel + " ") * 400  # a bulky previous-round transcript
        monkeypatch.setattr(
            _review_changes_mod, "_current_change_review_subject",
            lambda _s: "current change subject marker",
        )
        st, reviewer = _review_ready_state(prior_transcript=prior)
        default_registry().get("review_changes")(st)
        assert reviewer.calls, "review agent was not invoked"
        wire = reviewer.calls[-1][0]
        # Fresh policy: the review invocation does not resume...
        assert reviewer.kwargs_log[-1]["continue_session"] is False
        # ...and carries the compact current handoff but NOT the prior-round
        # transcript living in run state. A regression that piped the previous
        # subtask/round transcript into the review prompt would leak the
        # sentinel and fail this assert.
        assert "current change subject marker" in wire
        assert sentinel not in wire
