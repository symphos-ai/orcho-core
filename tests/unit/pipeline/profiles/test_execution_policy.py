"""ADR 0027 / M11: profile execution policy knobs.

This milestone introduces the backward-compatible execution policy
shape from ADR 0027 without implementing fanout_review. The tests
below pin:

- ``"execution": "linear"`` (string form) still loads and normalizes
  to an ``ExecutionPolicy`` with ``mode="linear"`` and no session
  split set;
- the object form is accepted for ``mode="linear"``;
- ``session_split`` accepts only the four documented values
  (``stateless`` / ``per_phase`` / ``per_role`` / ``common``);
- unknown values fail clearly;
- missing ``session_split`` defaults conservatively (``None`` on the
  policy, ``per_phase`` at the wiring layer);
- ``mode="fanout_review"`` is rejected until the later milestone;
- non-empty ``surfaces`` are rejected until the fanout runtime lands;
- the chosen ``session_split`` surfaces in
  ``state.phase_log[phase]["prompt_render"]["session_split"]`` so the
  trace makes the resolved mode obvious to M12 persistence.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from core.observability import prompt_trace
from pipeline.phases.builtin import _phase_validate_plan
from pipeline.plugins import PluginConfig
from pipeline.profiles.loader import ProfileLoadError, parse_profile
from pipeline.prompts.session import PromptSessionSplit
from pipeline.runtime import (
    ExecutionPolicy,
    ExecutionSurface,
    PhaseStep,
    PipelineState,
)

# ---------------------------------------------------------------------------
# JSON loader: backward-compat string form + new object form.
# ---------------------------------------------------------------------------


def _profile_obj(*, execution: Any) -> dict:
    """Minimal profile fixture for loader tests."""
    return {
        "kind": "custom",
        "variant": None,
        "description": "execution-policy fixture",
        "steps": [
            {"phase": "plan", "execution": execution},
        ],
    }


class TestExecutionFieldBackwardCompat:
    def test_loader_preserves_existing_string_execution_form(self) -> None:
        profile = parse_profile("p", _profile_obj(execution="linear"))
        step = profile.steps[0]
        assert step.execution == "linear"
        assert isinstance(step.execution_policy, ExecutionPolicy)
        assert step.execution_policy.mode == "linear"
        # No explicit session policy on a string-form step.
        assert step.execution_policy.session_split is None

    def test_loader_accepts_object_execution_policy(self) -> None:
        profile = parse_profile(
            "p",
            _profile_obj(execution={"mode": "linear", "session_split": "per_phase"}),
        )
        step = profile.steps[0]
        assert step.execution == "linear"
        assert step.execution_policy.mode == "linear"
        assert step.execution_policy.session_split == "per_phase"

    def test_loader_treats_string_and_explicit_mode_object_as_equivalent(
        self,
    ) -> None:
        string_form = parse_profile("p", _profile_obj(execution="linear"))
        object_form = parse_profile("p", _profile_obj(execution={"mode": "linear"}))
        assert (
            string_form.steps[0].execution_policy
            == object_form.steps[0].execution_policy
        )


# ---------------------------------------------------------------------------
# session_split value domain.
# ---------------------------------------------------------------------------


class TestSessionSplitValues:
    @pytest.mark.parametrize(
        "value", ["stateless", "per_phase", "per_role", "common"],
    )
    def test_execution_policy_accepts_documented_session_splits(
        self, value: str,
    ) -> None:
        # Note: parametrise rolls four required brief tests
        # (test_execution_policy_accepts_<stateless|per_phase|per_role|common>)
        # into one without losing coverage.
        # ``per_role`` additionally requires prompt.role on the same
        # step (ADR 0027 hardening), so the fixture supplies one.
        step_obj = {
            "phase": "plan",
            "execution": {"mode": "linear", "session_split": value},
        }
        if value == "per_role":
            step_obj["prompt"] = {
                "role": "code_reviewer",
                "task": "plan",
                "format": "terse",
            }
        profile_obj = {
            "kind": "custom",
            "variant": None,
            "description": "session_split fixture",
            "steps": [step_obj],
        }
        profile = parse_profile("p", profile_obj)
        assert profile.steps[0].execution_policy.session_split == value

    def test_execution_policy_rejects_unknown_session_split_value(self) -> None:
        with pytest.raises(ProfileLoadError, match="session_split"):
            parse_profile(
                "p",
                _profile_obj(
                    execution={"mode": "linear", "session_split": "bogus"},
                ),
            )

    def test_default_session_split_when_field_absent(self) -> None:
        # Object form without an explicit session_split.
        profile = parse_profile(
            "p",
            _profile_obj(execution={"mode": "linear"}),
        )
        # The policy itself carries ``None`` (no explicit profile-level
        # preference). The wiring helper resolves a conservative
        # per-phase default at invocation time — pinned by the trace
        # test below.
        assert profile.steps[0].execution_policy.session_split is None


# ---------------------------------------------------------------------------
# session_continuity value domain (ADR 0113) — orthogonal to session_split.
# ---------------------------------------------------------------------------


class TestSessionContinuityValues:
    @pytest.mark.parametrize(
        "value", ["fresh_only", "loop_continue", "same_zone_continue"],
    )
    def test_loader_round_trips_each_valid_continuity(self, value: str) -> None:
        profile = parse_profile(
            "p",
            _profile_obj(
                execution={"mode": "linear", "session_continuity": value},
            ),
        )
        assert profile.steps[0].execution_policy.session_continuity == value

    def test_loader_rejects_unknown_continuity_value(self) -> None:
        with pytest.raises(ProfileLoadError, match="session_continuity"):
            parse_profile(
                "p",
                _profile_obj(
                    execution={
                        "mode": "linear",
                        "session_continuity": "bogus",
                    },
                ),
            )

    def test_loader_rejects_non_string_continuity_value(self) -> None:
        with pytest.raises(ProfileLoadError, match="session_continuity"):
            parse_profile(
                "p",
                _profile_obj(
                    execution={"mode": "linear", "session_continuity": 7},
                ),
            )

    def test_continuity_defaults_to_none_when_absent(self) -> None:
        # String form and object-form-without-continuity both leave the
        # policy with no per-step continuity preference.
        string_form = parse_profile("p", _profile_obj(execution="linear"))
        object_form = parse_profile(
            "p", _profile_obj(execution={"mode": "linear"}),
        )
        assert string_form.steps[0].execution_policy.session_continuity is None
        assert object_form.steps[0].execution_policy.session_continuity is None

    def test_split_and_continuity_are_independent_axes(self) -> None:
        # ADR 0113: session_split (how a session is shared between phases)
        # and session_continuity (resume-own-session-on-repeat) coexist
        # without interfering.
        profile = parse_profile(
            "p",
            _profile_obj(
                execution={
                    "mode": "linear",
                    "session_split": "common",
                    "session_continuity": "loop_continue",
                },
            ),
        )
        policy = profile.steps[0].execution_policy
        assert policy.session_split == "common"
        assert policy.session_continuity == "loop_continue"


# ---------------------------------------------------------------------------
# Reserved future shape: fanout_review and non-empty surfaces.
# ---------------------------------------------------------------------------


class TestReservedFutureShape:
    def test_fanout_review_mode_rejected_until_m13(self) -> None:
        # ADR 0027 reserves mode="fanout_review" but M11 does not
        # implement the runtime. The loader must reject it loudly
        # rather than silently accepting and skipping execution.
        with pytest.raises(ProfileLoadError, match="fanout_review"):
            parse_profile(
                "p",
                _profile_obj(execution={"mode": "fanout_review"}),
            )

    def test_surfaces_rejected_until_fanout_is_implemented(self) -> None:
        # Non-empty surfaces are reserved shape; accept the JSON keys
        # but the policy post-init refuses to construct one.
        surface_obj = {
            "id": "correctness",
            "prompt": {
                "role": "code_reviewer",
                "task": "code_review",
                "format": "terse",
            },
        }
        with pytest.raises(ProfileLoadError, match="surfaces"):
            parse_profile(
                "p",
                _profile_obj(
                    execution={"mode": "linear", "surfaces": [surface_obj]},
                ),
            )


# ---------------------------------------------------------------------------
# Direct ExecutionPolicy / ExecutionSurface construction.
# ---------------------------------------------------------------------------


class TestExecutionPolicyConstruction:
    def test_default_execution_policy_is_linear_no_session_split(self) -> None:
        policy = ExecutionPolicy()
        assert policy.mode == "linear"
        assert policy.session_split is None
        assert policy.session_continuity is None
        assert policy.surfaces == ()

    @pytest.mark.parametrize(
        "value", ["fresh_only", "loop_continue", "same_zone_continue"],
    )
    def test_policy_accepts_each_valid_continuity_directly(
        self, value: str,
    ) -> None:
        policy = ExecutionPolicy(mode="linear", session_continuity=value)
        assert policy.session_continuity == value

    def test_policy_rejects_unknown_continuity_directly(self) -> None:
        with pytest.raises(ValueError, match="session_continuity"):
            ExecutionPolicy(mode="linear", session_continuity="bogus")

    def test_policy_rejects_empty_mode(self) -> None:
        with pytest.raises(ValueError, match="mode"):
            ExecutionPolicy(mode="")

    def test_policy_rejects_fanout_review_mode_directly(self) -> None:
        with pytest.raises(ValueError, match="fanout_review"):
            ExecutionPolicy(mode="fanout_review")

    def test_policy_rejects_non_empty_surfaces_directly(self) -> None:
        from pipeline.prompts.spec import PromptSpec

        surface = ExecutionSurface(
            id="x",
            prompt=PromptSpec(
                role="code_reviewer",
                task="code_review",
                format="terse",
            ),
        )
        with pytest.raises(ValueError, match="surfaces"):
            ExecutionPolicy(mode="linear", surfaces=(surface,))

    def test_policy_rejects_non_null_read_only(self) -> None:
        # ADR 0027 hardening: ``read_only`` is reserved profile shape
        # for the fanout milestone. The runtime does not enforce it
        # in M11, so any non-null value would let a profile look
        # constrained while no constraint applies. Fail fast.
        with pytest.raises(ValueError, match="read_only"):
            ExecutionPolicy(read_only=True)
        with pytest.raises(ValueError, match="read_only"):
            ExecutionPolicy(read_only=False)

    def test_policy_rejects_non_null_join(self) -> None:
        # Same fail-fast rule for ``join`` — reserved for the fanout
        # milestone and unenforced in M11.
        with pytest.raises(ValueError, match="join"):
            ExecutionPolicy(join="merge_findings_by_severity")

    def test_read_only_rejected_until_fanout_is_implemented(self) -> None:
        with pytest.raises(ProfileLoadError, match="read_only"):
            parse_profile(
                "p",
                _profile_obj(
                    execution={"mode": "linear", "read_only": True},
                ),
            )

    def test_join_rejected_until_fanout_is_implemented(self) -> None:
        with pytest.raises(ProfileLoadError, match="join"):
            parse_profile(
                "p",
                _profile_obj(
                    execution={
                        "mode": "linear",
                        "join": "merge_findings_by_severity",
                    },
                ),
            )


# ---------------------------------------------------------------------------
# PhaseStep ↔ ExecutionPolicy consistency.
# ---------------------------------------------------------------------------


class TestPhaseStepConsistency:
    def test_phasestep_synthesises_policy_when_omitted(self) -> None:
        step = PhaseStep(phase="plan", execution="linear")
        assert step.execution_policy.mode == "linear"

    def test_phasestep_rejects_mode_drift(self) -> None:
        # If a caller hand-constructs a step with explicit but
        # inconsistent execution and policy fields, the post-init
        # guards against silent drift.
        with pytest.raises(ValueError, match="execution_policy.mode"):
            PhaseStep(
                phase="plan",
                execution="linear",
                execution_policy=ExecutionPolicy(mode="bogus"),
            )

    def test_phasestep_rejects_per_role_without_prompt_role(self) -> None:
        # ADR 0027 hardening: ``per_role`` keys the prompt-session by
        # ``prompt.role``. Without an explicit role the wiring helper
        # would fall back to the phase name, silently degrading
        # per_role to per_phase under a different label. Reject the
        # inconsistent profile at construction time.
        policy = ExecutionPolicy(mode="linear", session_split="per_role")
        with pytest.raises(ValueError, match="per_role"):
            PhaseStep(phase="plan", execution_policy=policy)

    def test_phasestep_rejects_per_role_with_roleless_prompt(self) -> None:
        # PromptSpec allows role=None as a transitional shape; per_role
        # still must reject it for the same reason.
        from pipeline.prompts.spec import PromptSpec

        policy = ExecutionPolicy(mode="linear", session_split="per_role")
        roleless = PromptSpec(role=None, task="plan", format="terse")
        with pytest.raises(ValueError, match="per_role"):
            PhaseStep(phase="plan", execution_policy=policy, prompt=roleless)

    def test_phasestep_per_role_with_explicit_role_is_accepted(self) -> None:
        from pipeline.prompts.spec import PromptSpec

        policy = ExecutionPolicy(mode="linear", session_split="per_role")
        prompt = PromptSpec(
            role="code_reviewer", task="plan", format="terse",
        )
        step = PhaseStep(
            phase="plan", execution_policy=policy, prompt=prompt,
        )
        assert step.execution_policy.session_split == "per_role"
        assert step.prompt.role == "code_reviewer"

    def test_phasestep_loader_rejects_per_role_without_prompt_role(self) -> None:
        # End-to-end: a JSON profile with per_role on a step that
        # omits prompt.role fails at the loader, not silently at
        # invocation time.
        with pytest.raises(ProfileLoadError, match="per_role"):
            parse_profile(
                "p",
                _profile_obj(
                    execution={
                        "mode": "linear",
                        "session_split": "per_role",
                    },
                ),
            )


# ---------------------------------------------------------------------------
# Bundled profile fixture: at least one object-form policy in the
# shipped registry confirms wire shape round-trips.
# ---------------------------------------------------------------------------


def _make_minimal_advanced_profile_dict() -> dict:
    """A trimmed advanced-like profile with an object-form execution
    policy. We do not rely on the shipped fixture so this test stays
    decoupled from per-developer config edits."""
    return {
        "advanced": {
            "kind": "full_cycle",
            "variant": "advanced",
            "description": "object-form execution policy fixture",
            "steps": [
                {
                    "phase": "plan",
                    "execution": {
                        "mode": "linear",
                        "session_split": "per_phase",
                    },
                },
                {"phase": "validate_plan"},  # legacy default
            ],
        },
    }


class TestRegistryRoundTrip:
    def test_object_form_round_trips_through_parse_profiles(self) -> None:
        from pipeline.profiles.loader import parse_profiles

        loaded = parse_profiles(_make_minimal_advanced_profile_dict())
        profile = loaded["advanced"]
        plan_step = profile.steps[0]
        validate_step = profile.steps[1]
        assert plan_step.execution_policy.session_split == "per_phase"
        # Legacy step without an execution field defaults to linear /
        # no session split.
        assert validate_step.execution == "linear"
        assert validate_step.execution_policy.session_split is None


# ---------------------------------------------------------------------------
# Trace evidence: chosen session_split appears in prompt_render.
# ---------------------------------------------------------------------------


class _RecordingAgent:
    def __init__(self) -> None:
        self.model = "claude-opus-4-7"
        self.session_id = "sess-policy-1"

    def invoke(
        self,
        prompt: str,
        cwd: str,
        *,
        continue_session: bool = False,
        attachments: tuple = (),
        mutates_artifacts: bool = False,
    ) -> str:
        return json.dumps({
            "verdict": "APPROVED",
            "short_summary": "ok",
            "findings": [],
            "risks": [],
            "checks": [],
        })


def _make_state(
    *,
    policy: ExecutionPolicy | None = None,
    with_prompt_role: bool = True,
) -> PipelineState:
    from pipeline.prompts.spec import PromptSpec

    state = PipelineState(
        task="Test session_split surfacing",
        project_dir="/proj",
        plugin=PluginConfig(),
        extras={"run_id": "run-policy-1"},
    )
    # Wire a stub lifecycle_ctx whose active_step carries the policy
    # the test wants surfaced. Mirrors what the FSM populates before
    # invoking phase handlers in production.
    #
    # ``with_prompt_role`` controls whether the active step ships an
    # explicit prompt-role persona. ``per_role`` requires it (PhaseStep
    # post-init enforces; the runtime helper double-checks). The other
    # splits (per_phase / common / stateless) must still work without
    # any prompt at all — exercised by the tests below.
    # ADR 0113: validate_plan continuity is declared per-phase. These trace
    # tests vary only the orthogonal session_split axis, so the active step
    # always carries validate_plan's real continuity default (loop_continue)
    # while the split under test is preserved; the resolver requires the
    # declaration (it refuses to silently default to fresh).
    from dataclasses import replace

    if policy is None:
        effective_policy = ExecutionPolicy(
            mode="linear", session_continuity="loop_continue"
        )
    elif policy.session_continuity is None:
        effective_policy = replace(policy, session_continuity="loop_continue")
    else:
        effective_policy = policy

    if with_prompt_role:
        prompt_spec = PromptSpec(
            role="plan_reviewer",
            task="validate_plan",
            format="terse",
        )
        step = PhaseStep(
            phase="validate_plan",
            execution_policy=effective_policy,
            prompt=prompt_spec,
        )
    else:
        step = PhaseStep(
            phase="validate_plan", execution_policy=effective_policy
        )
    state.lifecycle_ctx = SimpleNamespace(
        active_step=step,
        git_helpers=SimpleNamespace(has_uncommitted=lambda _cwd: False),
        text_helpers=SimpleNamespace(critique_is_empty=lambda c: not c),
        plan_helpers=SimpleNamespace(validate_paths=lambda *_a, **_kw: ([], [])),
        run_config={},
    )
    from agents.registry import PhaseAgentConfig

    agent = _RecordingAgent()
    state.phase_config = PhaseAgentConfig(
        plan_agent=agent,
        validate_plan_agent=agent,
        implement_agent=agent,
        review_changes_agent=agent,
        repair_changes_agent=agent,
        repair_escalation_agent=agent,
        final_acceptance_agent=agent,
    )
    return state


class TestSessionSplitVisibleInTrace:
    @pytest.fixture(autouse=True)
    def _drain(self):
        prompt_trace.take_last_upper()
        yield
        prompt_trace.take_last_upper()

    def test_default_split_surfaces_as_per_phase_in_trace(self) -> None:
        # No execution_policy on the active step -> wiring helper
        # falls back to conservative per_phase. The trace surfaces the
        # resolved value so M12 persistence sees it.
        state = _make_state(policy=None)
        _phase_validate_plan(state)
        meta = state.phase_log["validate_plan"]["prompt_render"]
        assert meta["session_split"] == "per_phase"

    @pytest.mark.parametrize(
        "value", ["stateless", "per_phase", "per_role", "common"],
    )
    def test_profile_session_split_appears_in_trace(self, value: str) -> None:
        policy = ExecutionPolicy(mode="linear", session_split=value)
        state = _make_state(policy=policy)
        _phase_validate_plan(state)
        meta = state.phase_log["validate_plan"]["prompt_render"]
        assert meta["session_split"] == value

    def test_stateless_profile_produces_no_session_key(self) -> None:
        # M5 contract: STATELESS yields no reusable PhysicalSessionKey.
        # The trace exposes the chosen split even though the key is
        # absent — without this the trace would be unable to tell
        # "stateless mode" apart from "missing envelope".
        policy = ExecutionPolicy(mode="linear", session_split="stateless")
        state = _make_state(policy=policy)
        _phase_validate_plan(state)
        meta = state.phase_log["validate_plan"]["prompt_render"]
        assert meta["session_split"] == "stateless"
        assert meta["session_key"] is None

    def test_per_role_session_key_scope_uses_prompt_role(self) -> None:
        # ADR 0027 hardening evidence: when ``per_role`` is selected
        # and ``prompt.role`` is supplied, the physical session-key
        # scope must be ``per_role:<role>``. If the wiring helper
        # silently fell back to the phase name the scope would read
        # ``per_role:validate_plan`` — the test catches that
        # regression.
        policy = ExecutionPolicy(mode="linear", session_split="per_role")
        state = _make_state(policy=policy)
        _phase_validate_plan(state)
        meta = state.phase_log["validate_plan"]["prompt_render"]
        assert meta["session_split"] == "per_role"
        assert meta["session_key"] is not None
        assert meta["session_key"]["scope"] == "per_role:plan_reviewer"

    def test_per_phase_works_without_prompt_role(self) -> None:
        # ``per_phase`` keys by phase name, not by role; a profile
        # without ``prompt.role`` must keep working.
        policy = ExecutionPolicy(mode="linear", session_split="per_phase")
        state = _make_state(policy=policy, with_prompt_role=False)
        _phase_validate_plan(state)
        meta = state.phase_log["validate_plan"]["prompt_render"]
        assert meta["session_split"] == "per_phase"
        assert meta["session_key"]["scope"] == "per_phase:validate_plan"

    def test_common_works_without_prompt_role(self) -> None:
        # ``common`` collapses every phase into one shared scope; no
        # role lookup is involved.
        policy = ExecutionPolicy(mode="linear", session_split="common")
        state = _make_state(policy=policy, with_prompt_role=False)
        _phase_validate_plan(state)
        meta = state.phase_log["validate_plan"]["prompt_render"]
        assert meta["session_split"] == "common"
        assert meta["session_key"]["scope"] == "common"

    def test_stateless_works_without_prompt_role(self) -> None:
        # ``stateless`` is the most permissive split: no key, no role
        # lookup; a profile without ``prompt.role`` must keep working.
        policy = ExecutionPolicy(mode="linear", session_split="stateless")
        state = _make_state(policy=policy, with_prompt_role=False)
        _phase_validate_plan(state)
        meta = state.phase_log["validate_plan"]["prompt_render"]
        assert meta["session_split"] == "stateless"
        assert meta["session_key"] is None

    def test_per_role_runtime_rejects_missing_prompt_role(self) -> None:
        # Defense-in-depth runtime check inside ``_session_aware_invoke``:
        # PhaseStep construction already blocks ``per_role`` without
        # ``prompt.role`` (see TestPhaseStepConsistency above), so the
        # only way to reach the runtime helper in this state is to
        # bypass PhaseStep — e.g. a stubbed ``active_step`` from a
        # test, a custom embedder, or a future caller that builds a
        # SimpleNamespace step. The runtime helper must still fail
        # clearly, naming ``per_role`` and ``prompt.role``, before
        # the agent is invoked. Exercise this by calling
        # :func:`_session_aware_invoke` directly with a hand-stubbed
        # state so PhaseStep construction does not block the setup.
        from pipeline.phases.builtin import _session_aware_invoke

        # Build a stub state without going through ``_make_state``;
        # the latter constructs a real PhaseStep which would itself
        # reject ``per_role`` without ``prompt.role``. We want to
        # prove the runtime helper is the second line of defence.
        policy = ExecutionPolicy(mode="linear", session_split="per_role")
        state = PipelineState(
            task="Runtime per_role guard",
            project_dir="/proj",
            plugin=PluginConfig(),
            extras={"run_id": "run-runtime-1"},
        )
        state.lifecycle_ctx = SimpleNamespace(
            active_step=SimpleNamespace(
                phase="validate_plan",
                execution="linear",
                execution_policy=policy,
                prompt=None,
            ),
            run_config={},
        )
        agent = _RecordingAgent()
        from pipeline.prompts.turn import PromptTurnEditor
        from pipeline.prompts.types import PromptCacheScope, PromptPart, PromptStability
        _dummy_part = PromptPart(
            kind="task", name="dummy", source="core", body="dummy",
            stability=PromptStability.TURN, cache_scope=PromptCacheScope.NONE,
            volatile_reason="test",
        )
        _dummy_turn = PromptTurnEditor().append(_dummy_part).build()
        with pytest.raises(ValueError, match="per_role"):
            _session_aware_invoke(
                agent,
                state,
                phase="validate_plan",
                turn=_dummy_turn,
                cwd="/proj",
                continue_session=False,
                attachments=(),
                mutates_artifacts=False,
            )


# ---------------------------------------------------------------------------
# Loader edge cases.
# ---------------------------------------------------------------------------


class TestLoaderEdgeCases:
    def test_loader_rejects_non_string_non_object_execution(self) -> None:
        with pytest.raises(ProfileLoadError, match="execution"):
            parse_profile("p", _profile_obj(execution=42))

    def test_loader_rejects_unknown_execution_policy_keys(self) -> None:
        with pytest.raises(ProfileLoadError, match="unknown keys"):
            parse_profile(
                "p",
                _profile_obj(
                    execution={"mode": "linear", "not_a_field": "x"},
                ),
            )

def test_prompt_session_split_member_set_matches_loader_validation() -> None:
    """The loader's value domain for ``session_split`` must mirror
    the M5 :class:`PromptSessionSplit` enum exactly. Otherwise a
    profile could declare a split string the wiring layer cannot
    resolve, or vice versa.
    """
    enum_values = {member.value for member in PromptSessionSplit}
    assert enum_values == {"stateless", "per_phase", "per_role", "common"}
