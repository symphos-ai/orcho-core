"""M9 wiring tests: review / implement / repair_changes session policy.

The reviewer and implementer prompt-session states are intentionally
isolated. Implement seeds ``phase="implement"`` so CHAIN repair can
reuse the implementer-side stable prefix; non-CHAIN repair gets its
own ``phase="repair_changes"`` key. Reviewer phases run under
``phase="review_changes"`` and never touch the implementer state.

Final acceptance stays on full render — verdict isolation outweighs
prompt-size win on the closing gate. The test for that is structural
(no helper invocation, no prompt_session_state mutation).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from agents.protocols import SessionMode
from core.observability import prompt_trace
from pipeline.phases.builtin import (
    _phase_implement,
    _phase_repair_changes,
    _phase_review_changes,
)
from pipeline.plugins import PluginConfig
from pipeline.runtime import PipelineState

# ---------------------------------------------------------------------------
# Mock surface.
# ---------------------------------------------------------------------------


def _approved_review_json(summary: str = "No blocking issues.") -> str:
    return json.dumps({
        "verdict": "APPROVED",
        "short_summary": summary,
        "findings": [],
        "risks": [],
        "checks": ["Reviewed change"],
    })


def _rejected_review_json(body: str) -> str:
    return json.dumps({
        "verdict": "REJECTED",
        "short_summary": f"P2: {body}",
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


class _RecordingAgent:
    """Records every wire prompt + invoke kwargs."""

    def __init__(
        self,
        *,
        responses: list[str] | None = None,
        model: str = "claude-opus-4-7",
        session_id: str | None = "sess-1",
        cls_name: str | None = None,
    ) -> None:
        self.model = model
        self.session_id = session_id
        self.calls: list[dict[str, Any]] = []
        self._responses = list(responses or ["impl output"])
        # Optional class-name override for runtime-mismatch tests.
        if cls_name is not None:
            self.__class__ = type(cls_name, (_RecordingAgent,), {})

    def invoke(
        self,
        prompt: str,
        cwd: str,
        *,
        continue_session: bool = False,
        attachments: tuple = (),
        mutates_artifacts: bool = False,
    ) -> str:
        self.calls.append({
            "prompt": prompt,
            "cwd": cwd,
            "continue_session": continue_session,
            "attachments": attachments,
            "mutates_artifacts": mutates_artifacts,
        })
        head, *rest = self._responses
        self._responses = rest or [head]
        return head


def _make_state(
    *,
    plugin: PluginConfig | None = None,
    repair_round: int = 1,
    last_critique: str = "",
    session_mode_initial: str = "auto",
    implement_model: str = "claude-opus-4-7",
    repair_model: str = "claude-opus-4-7",
) -> PipelineState:
    plugin = plugin or PluginConfig()
    extras: dict[str, Any] = {
        "run_id": "run-rev-rep-1",
        "repair_round": repair_round,
        "session_mode_initial": session_mode_initial,
        "implement_model": implement_model,
        "repair_model": repair_model,
    }
    state = PipelineState(
        task="Add structured logging",
        project_dir="/proj",
        plugin=plugin,
        extras=extras,
        last_critique=last_critique,
    )
    # ADR 0113: implement / repair_changes declare same_zone_continue. These
    # direct-handler tests bypass the FSM, so seed the active step's declaration
    # the way the FSM would in production (so CHAIN repair resolves a same-write-
    # zone CONTINUE instead of the no-context fresh fallback). session_split is
    # left at the conservative per_phase default.
    from pipeline.lifecycle import default_lifecycle_context
    from pipeline.runtime import PhaseRegistry

    state.lifecycle_ctx = default_lifecycle_context(phase_registry=PhaseRegistry())
    state.lifecycle_ctx.active_step = SimpleNamespace(
        prompt=None,
        execution_policy=SimpleNamespace(
            session_split=None, session_continuity="same_zone_continue"
        ),
    )
    return state


def _install_agents(
    state: PipelineState,
    *,
    implement: _RecordingAgent | None = None,
    review: _RecordingAgent | None = None,
    repair: _RecordingAgent | None = None,
    final_acceptance: _RecordingAgent | None = None,
    repair_escalation: _RecordingAgent | None = None,
) -> None:
    """Wire mock agents into state.phase_config.

    The repair handler relies on ``dataclasses.replace`` to swap
    ``repair_changes_agent`` between rounds, so ``phase_config``
    must be a real dataclass — :class:`agents.registry.PhaseAgentConfig`.
    """
    from agents.registry import PhaseAgentConfig

    state.phase_config = PhaseAgentConfig(
        plan_agent=implement or _RecordingAgent(),
        validate_plan_agent=review or _RecordingAgent(),
        implement_agent=implement or _RecordingAgent(),
        review_changes_agent=review or _RecordingAgent(),
        repair_changes_agent=repair or _RecordingAgent(),
        repair_escalation_agent=repair_escalation or _RecordingAgent(),
        final_acceptance_agent=final_acceptance or _RecordingAgent(),
    )


def _force_uncommitted_in_ctx(state: PipelineState) -> None:
    """Override has_uncommitted so the review handler skips its
    no-uncommitted short-circuit.

    The reviewer skips agent invocation entirely when the working
    tree is clean — that's the legacy ``run_review_fix_loop`` parity
    behavior. For M9 wiring tests we want the agent path exercised,
    so we install a ctx whose ``git_helpers.has_uncommitted`` always
    returns True.
    """
    from pipeline.phases.builtin import _ensure_lifecycle_ctx

    ctx = _ensure_lifecycle_ctx(state)
    ctx.git_helpers = SimpleNamespace(
        has_uncommitted=lambda _cwd: True,
    )


@pytest.fixture(autouse=True)
def _drain_render_envelope():
    """Ensure each test starts with a clean prompt_trace slot."""
    prompt_trace.take_last_upper()
    yield
    prompt_trace.take_last_upper()


# ---------------------------------------------------------------------------
# Implement seeds prompt-session state for CHAIN repair to reuse.
# ---------------------------------------------------------------------------


class TestImplementSeedsState:
    def test_implement_seeds_prompt_session_state(self) -> None:
        impl = _RecordingAgent(responses=["implement output"])
        state = _make_state()
        _install_agents(state, implement=impl)
        _phase_implement(state)

        # Implement committed a per_phase:implement session record.
        assert state.prompt_sessions, "implement must seed prompt_sessions"
        keys = list(state.prompt_sessions.keys())
        assert keys[0].scope == "per_phase:implement"

    def test_implement_passes_mutates_artifacts_true(self) -> None:
        impl = _RecordingAgent(responses=["impl"])
        state = _make_state()
        _install_agents(state, implement=impl)
        _phase_implement(state)
        assert impl.calls[0]["mutates_artifacts"] is True


# ---------------------------------------------------------------------------
# Reviewer state stays isolated from implementer state.
# ---------------------------------------------------------------------------


class TestStateIsolation:
    def test_review_state_isolated_from_implementation_state(self) -> None:
        impl = _RecordingAgent(responses=["impl"])
        review = _RecordingAgent(responses=[_approved_review_json()])
        state = _make_state()
        _install_agents(state, implement=impl, review=review)

        # implement first to seed phase="implement" state.
        _phase_implement(state)
        # review next under its own phase="review_changes" key.
        # Force the no-uncommitted skip OFF by claiming uncommitted.
        _force_uncommitted_in_ctx(state)
        _phase_review_changes(state)

        scopes = sorted(k.scope for k in state.prompt_sessions)
        assert scopes == [
            "per_phase:implement",
            "per_phase:review_changes",
        ], (
            "implement and review_changes must occupy distinct "
            "PhysicalSessionKeys, never share state"
        )


# ---------------------------------------------------------------------------
# CHAIN repair reuses implementer key; non-CHAIN repair gets own key.
# ---------------------------------------------------------------------------


class TestRepairChainRouting:
    def _seed_implement(self, state, agent) -> None:
        # Round 1 implement seeds phase="implement" state.
        _install_agents(state, implement=agent, repair=agent)
        _phase_implement(state)

    def test_chain_repair_reuses_implement_prompt_session_key(self) -> None:
        agent = _RecordingAgent(responses=["impl", "repair"])
        state = _make_state(
            session_mode_initial="chain", last_critique="Fix the thing.",
        )
        # In CHAIN mode the helper resolves continue_session=True and
        # phase_config.repair_changes_agent gets swapped to
        # implement_agent — so both calls run on `agent`.
        self._seed_implement(state, agent)

        impl_key = list(state.prompt_sessions.keys())[0]
        assert impl_key.scope == "per_phase:implement"

        # Now run repair_changes; cfg resolves CHAIN, phase_config
        # swap points repair_changes_agent at implement_agent
        # (already same agent here).
        _phase_repair_changes(state)
        keys = list(state.prompt_sessions.keys())
        # Repair must NOT add a per_phase:repair_changes key when
        # CHAIN — it must hit the same per_phase:implement key.
        assert all(
            k.scope == "per_phase:implement" for k in keys
        ), (
            "CHAIN repair must reuse the implement PhysicalSessionKey, "
            f"got scopes: {[k.scope for k in keys]}"
        )

    def test_chain_repair_runs_delta_under_shared_implement_key(
        self,
    ) -> None:
        # M11.5: CHAIN repair must (a) hit the implement session key
        # (so the M6 selector sees prior sent_part_keys) and (b)
        # report ``render_mode="delta"`` on the repair-side trace.
        # Whether the wire prompt actually shrinks depends on
        # whether the implement upper parts are prefix-eligible.
        # The implementation_engineer role template references
        # ``$project_dir`` / ``$context`` so the M11.5 composer
        # classifies it RUN/WORKSPACE — not prefix-eligible. Under
        # the M11.5 prefix-contiguity defense the role body stays on
        # the wire on every round. The contract this test pins is
        # the session-reuse routing, not a wire-size bound.
        agent = _RecordingAgent(responses=["impl", "repair"])
        state = _make_state(
            session_mode_initial="chain", last_critique="Fix.",
        )
        self._seed_implement(state, agent)
        impl_prompt = agent.calls[0]["prompt"]
        assert "implementation engineer" in impl_prompt.lower()

        _phase_repair_changes(state)
        repair_trace = state.phase_log["repair_changes"]["prompt_render"]
        # M11.5 Fix 2: CHAIN repair attributes its trace to
        # ``repair_changes`` while reusing the ``implement``
        # session key under the hood.
        assert repair_trace["render_mode"] == "delta"
        assert repair_trace["session_key"]["scope"] == "per_phase:implement"

    def test_repair_uses_full_render_when_not_chain(self) -> None:
        # STATELESS mode: repair gets its own per_phase:repair_changes
        # key and renders full because that key is fresh.
        agent = _RecordingAgent(responses=["impl", "repair"])
        state = _make_state(
            session_mode_initial="stateless",
            last_critique="Fix.",
        )
        self._seed_implement(state, agent)
        _phase_repair_changes(state)

        scopes = sorted(k.scope for k in state.prompt_sessions)
        assert "per_phase:repair_changes" in scopes
        assert "per_phase:implement" in scopes
        # Distinct keys means distinct states means full render for
        # repair on round 1.
        repair_meta = state.phase_log["repair_changes"].get("prompt_render")
        assert repair_meta is not None
        assert repair_meta["render_mode"] == "full"

    def test_repair_uses_full_render_on_runtime_mismatch(self) -> None:
        # Non-CHAIN repair gets its own per_phase:repair_changes key
        # (no swap). With distinct runtime classes for implement and
        # repair, the M5 PhysicalSessionKey carries different
        # ``runtime`` ids and the M6 selector starts from empty
        # state for repair_changes. M9's contract: a runtime
        # boundary is honored at the wire level — repair never
        # silently reads implementer state when runtimes differ.
        class _OtherRuntime(_RecordingAgent):
            pass

        impl = _RecordingAgent(responses=["impl"])
        repair = _OtherRuntime(responses=["repair"])
        state = _make_state(
            session_mode_initial="stateless", last_critique="Fix.",
        )
        _install_agents(state, implement=impl, repair=repair)
        _phase_implement(state)
        _phase_repair_changes(state)

        # implement and repair landed in distinct keys (different
        # runtime ids + different scopes).
        keys = list(state.prompt_sessions.keys())
        runtime_ids = {k.runtime for k in keys}
        scopes = {k.scope for k in keys}
        assert len(runtime_ids) == 2, (
            "implement and non-CHAIN repair must use distinct runtime ids"
        )
        assert "per_phase:implement" in scopes
        assert "per_phase:repair_changes" in scopes

        repair_meta = state.phase_log["repair_changes"].get("prompt_render")
        assert repair_meta is not None
        # Repair's key is fresh -> full render. The M9 wire honors
        # the runtime boundary at the prompt-session layer.
        assert repair_meta["render_mode"] == "full"

    def test_repair_uses_full_render_on_model_mismatch(self) -> None:
        # Same shape as the runtime-mismatch test but under model
        # boundary. Non-CHAIN repair gets its own scope, distinct
        # model_keys produce distinct PhysicalSessionKeys.
        impl = _RecordingAgent(responses=["impl"], model="opus")
        repair = _RecordingAgent(responses=["repair"], model="sonnet")
        state = _make_state(
            session_mode_initial="stateless", last_critique="Fix.",
        )
        _install_agents(state, implement=impl, repair=repair)
        _phase_implement(state)
        _phase_repair_changes(state)

        keys = list(state.prompt_sessions.keys())
        models = {k.model_key for k in keys}
        assert len(models) == 2, (
            "implement and non-CHAIN repair must carry distinct model_keys"
        )
        repair_meta = state.phase_log["repair_changes"].get("prompt_render")
        assert repair_meta is not None
        assert repair_meta["render_mode"] == "full"


# ---------------------------------------------------------------------------
# continue_with_waiver: the durable operator waiver is injected into the
# review_changes wire prompt so the reviewer does not reopen waived findings.
# ---------------------------------------------------------------------------


class TestOperatorWaiverInjection:
    def test_review_changes_wire_prompt_carries_waiver(self) -> None:
        review = _RecordingAgent(responses=[_approved_review_json()])
        state = _make_state()
        state.extras["phase_handoff_waiver"] = {
            "handoff_id": "review_changes:repair_round:1",
            "phase": "review_changes",
            "waiver_text": "Accepted risk: legacy shim stays this release",
            "findings": [
                {"id": "F1", "severity": "P2", "title": "legacy shim"},
            ],
            "critique": "Reviewer flagged the shim.",
        }
        _install_agents(state, review=review)
        _force_uncommitted_in_ctx(state)
        _phase_review_changes(state)

        wire = review.calls[0]["prompt"]
        # Code-owned reconciliation policy + the operator verdict reach the
        # reviewer on the wire.
        assert "OPERATOR WAIVER" in wire
        assert "MUST NOT reopen the waived" in wire
        assert "Operator verdict: Accepted risk: legacy shim stays" in wire
        assert "legacy shim" in wire

    def test_no_waiver_leaves_review_prompt_clean(self) -> None:
        review = _RecordingAgent(responses=[_approved_review_json()])
        state = _make_state()
        _install_agents(state, review=review)
        _force_uncommitted_in_ctx(state)
        _phase_review_changes(state)

        wire = review.calls[0]["prompt"]
        assert "OPERATOR WAIVER" not in wire


# ---------------------------------------------------------------------------
# mutates_artifacts is forwarded for repair invocations.
# ---------------------------------------------------------------------------


class TestRepairMutatesArtifacts:
    def test_repair_passes_mutates_artifacts_true(self) -> None:
        agent = _RecordingAgent(responses=["impl", "repair"])
        state = _make_state(
            session_mode_initial="chain", last_critique="Fix.",
        )
        _install_agents(state, implement=agent, repair=agent)
        _phase_implement(state)
        _phase_repair_changes(state)
        repair_call = agent.calls[1]
        assert repair_call["mutates_artifacts"] is True


# ---------------------------------------------------------------------------
# Reviewer round 2 uses review_changes scope.
# ---------------------------------------------------------------------------


class TestReviewerRoundTwo:
    def test_review_round2_uses_review_changes_delta_scope(self) -> None:
        review = _RecordingAgent(
            responses=[_approved_review_json(), _approved_review_json()],
        )
        state = _make_state()
        _install_agents(state, review=review)
        # Force has_uncommitted True so the no-op skip doesn't fire.
        _force_uncommitted_in_ctx(state)
        _phase_review_changes(state)
        # Bump round counter and re-run reviewer.
        state.extras["repair_round"] = 2
        _phase_review_changes(state)

        meta = state.phase_log["review_changes"]["prompt_render"]
        assert meta["session_key"]["scope"] == "per_phase:review_changes"
        assert meta["render_mode"] in {"delta", "full"}

    def test_post_repair_reverify_receives_receipt_and_current_subject(
        self,
    ) -> None:
        repair = _RecordingAgent(responses=["Edited file.py and checked status."])
        review = _RecordingAgent(responses=[_approved_review_json()])
        state = _make_state(
            session_mode_initial="stateless",
            last_critique="F1: stale wording remains.",
        )
        _install_agents(state, repair=repair, review=review)
        _phase_repair_changes(state)

        state.extras["_review_reverify_resume"] = True
        _force_uncommitted_in_ctx(state)
        _phase_review_changes(state)

        prompt = review.calls[-1]["prompt"]
        assert "## Repair Receipt" in prompt
        assert "## Current Review Subject" in prompt
        meta = state.phase_log["review_changes"]["prompt_render"]
        keys = set(meta["selected_part_keys"])
        assert "repair_receipt:latest@0" in keys
        assert "current_review_subject:latest@0" in keys

    def test_round2_leading_review_after_repair_receives_receipt(
        self,
    ) -> None:
        review = _RecordingAgent(
            responses=[
                _rejected_review_json("F1 still has stale wording."),
                _approved_review_json(),
            ],
        )
        repair = _RecordingAgent(responses=["Removed stale wording."])
        state = _make_state(session_mode_initial="stateless")
        _install_agents(state, review=review, repair=repair)
        _force_uncommitted_in_ctx(state)

        _phase_review_changes(state)
        _phase_repair_changes(state)

        state.extras["repair_round"] = 2
        _phase_review_changes(state)

        prompt = review.calls[-1]["prompt"]
        assert "## Repair Receipt" in prompt
        assert "## Current Review Subject" in prompt
        meta = state.phase_log["review_changes"]["prompt_render"]
        keys = set(meta["selected_part_keys"])
        assert "repair_receipt:latest@0" in keys
        assert "current_review_subject:latest@0" in keys


# ---------------------------------------------------------------------------
# Findings / diffs / repair instructions stay dynamic turn parts.
# ---------------------------------------------------------------------------


class TestDynamicTurnParts:
    def test_findings_remain_dynamic_turn_part(self) -> None:
        review = _RecordingAgent(
            responses=[_rejected_review_json("Edge case missed.")],
        )
        state = _make_state()
        _install_agents(state, review=review)
        _force_uncommitted_in_ctx(state)
        _phase_review_changes(state)

        # Review surface declared findings: they live in
        # state.last_critique (dynamic per round). The review prompt
        # itself does not pre-bake findings (the reviewer agent
        # generates them). Sanity-check that last_critique is the
        # dynamic per-round payload.
        assert state.last_critique
        assert "Edge case missed" in state.last_critique

    def test_repair_instructions_remain_dynamic_turn_part(self) -> None:
        agent = _RecordingAgent(responses=["impl", "repair"])
        critique = "Reviewer says: handle empty input."
        state = _make_state(
            session_mode_initial="chain", last_critique=critique,
        )
        _install_agents(state, implement=agent, repair=agent)
        _phase_implement(state)
        _phase_repair_changes(state)

        repair_prompt = agent.calls[1]["prompt"]
        # Critique text reaches the wire; M6 selector cannot omit it
        # because the fix_prompt task body is TURN/NONE per M2's
        # composer classification.
        assert critique in repair_prompt


# ---------------------------------------------------------------------------
# Final acceptance keeps full render — the M9 policy decision.
# ---------------------------------------------------------------------------


class TestFinalAcceptanceFullOnly:
    def test_final_acceptance_remains_full_render_without_prompt_session_state(
        self,
    ) -> None:
        # Importing the helper inline so the test can grep for the
        # absence of session-aware wiring at the handler level.
        from pathlib import Path

        from pipeline.phases.builtin.handlers import (
            final_acceptance as builtin_module,
        )

        source = Path(builtin_module.__file__).read_text(encoding="utf-8")
        # The final_acceptance handler must NOT call _session_aware_invoke.
        # Find the function block and assert the helper name is absent.
        start = source.index("def _phase_final_acceptance")
        # Find the next top-level def.
        rest = source[start:]
        # The function ends at the next "\ndef _" at column 0.
        end_off = rest.find("\ndef _", 1)
        body = rest if end_off < 0 else rest[:end_off]
        assert "_session_aware_invoke" not in body, (
            "final_acceptance must stay on full render in M9; "
            "_session_aware_invoke wiring belongs to a later milestone"
        )

    def test_final_acceptance_handler_does_not_seed_prompt_sessions(
        self,
    ) -> None:
        # End-to-end: run the final_acceptance handler against a
        # mock agent and assert it never wrote into
        # state.prompt_sessions.
        from pipeline.phases.builtin import _phase_final_acceptance

        # Final acceptance uses release_json contract; emit a minimal
        # release-shape JSON.
        release_json = json.dumps({
            "verdict": "APPROVED",
            "ship_ready": True,
            "short_summary": "OK",
            "release_blockers": [],
            "verification_gaps": [],
            "contract_status": {
                "task_contract": "satisfied",
                "interfaces":    "not_applicable",
                "persistence":   "not_applicable",
                "tests":         "sufficient",
            },
        })
        agent = _RecordingAgent(responses=[release_json])
        state = _make_state()
        _install_agents(state, final_acceptance=agent)
        _force_uncommitted_in_ctx(state)
        _phase_final_acceptance(state)

        # Helper untouched: prompt_sessions stays empty.
        assert state.prompt_sessions == {}


# ---------------------------------------------------------------------------
# Enum boundary: SessionMode and PromptSessionSplit must not be
# treated as interchangeable.
# ---------------------------------------------------------------------------


class TestEnumBoundary:
    def test_session_mode_and_prompt_session_split_are_distinct(self) -> None:
        from pipeline.prompts.session import PromptSessionSplit

        assert SessionMode is not PromptSessionSplit
        assert not issubclass(PromptSessionSplit, SessionMode)
        assert not issubclass(SessionMode, PromptSessionSplit)
        # CHAIN exists only on SessionMode; PER_PHASE only on
        # PromptSessionSplit. Crossing the type boundary at the
        # phase-handler layer is a bug.
        assert "CHAIN" in {m.name for m in SessionMode}
        assert "PER_PHASE" in {m.name for m in PromptSessionSplit}
        assert "CHAIN" not in {m.name for m in PromptSessionSplit}


# ---------------------------------------------------------------------------
# M11.5 Fix 2 — CHAIN repair: trace_phase decouples session-key phase
# from the phase_log slot the prompt_render metadata lands in.
# ---------------------------------------------------------------------------


class TestRepairTracePhaseM11_5:
    """The helper's ``trace_phase`` parameter writes prompt_render
    under a separate slot from the session-key ``phase``. Before
    M11.5 CHAIN repair attributed its trace under ``implement`` (the
    session-key phase), so the repair handler's overwrite of
    ``phase_log["repair_changes"]`` could not preserve the metadata.
    """

    def test_chain_repair_records_prompt_render_under_repair_changes(
        self,
    ) -> None:
        agent = _RecordingAgent(responses=["impl", "repair"])
        state = _make_state(
            session_mode_initial="chain", last_critique="Fix.",
        )
        _install_agents(state, implement=agent, repair=agent)
        _phase_implement(state)
        _phase_repair_changes(state)

        # M11.5 Fix 2: trace attribution under repair_changes even
        # when the session key reuses implement.
        assert "prompt_render" in state.phase_log["repair_changes"]
        repair_meta = state.phase_log["repair_changes"]["prompt_render"]
        assert repair_meta["render_mode"] == "delta"
        # Session key still uses the implement phase scope — that is
        # the key-reuse contract.
        assert repair_meta["session_key"]["scope"] == "per_phase:implement"

    def test_non_chain_repair_uses_repair_changes_session_key(
        self,
    ) -> None:
        # Sanity counter-test: non-CHAIN repair has its own
        # per_phase:repair_changes key and still writes its trace
        # under ``repair_changes`` (trace_phase=phase fall-through).
        agent = _RecordingAgent(responses=["impl", "repair"])
        state = _make_state(
            session_mode_initial="stateless", last_critique="Fix.",
        )
        _install_agents(state, implement=agent, repair=agent)
        _phase_implement(state)
        _phase_repair_changes(state)

        repair_meta = state.phase_log["repair_changes"]["prompt_render"]
        assert (
            repair_meta["session_key"]["scope"] == "per_phase:repair_changes"
        )
