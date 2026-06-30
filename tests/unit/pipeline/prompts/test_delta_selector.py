"""Unit tests for the M6 delta-selection pure function.

Covers the four decision branches of :func:`select_parts_for_turn`
(stateless / no state / fresh session / resumed delta), version-
aware send-history identity via M5's :func:`part_session_key`, and
the forced-resend rules for role / phase / contract anchor flips.

The selector is purely declarative — these tests build envelopes
and states from fixtures without touching any runtime, prompt
loader, or composer.
"""

from __future__ import annotations

import pytest

from pipeline.prompts.delta import (
    SelectionResult,
    select_parts_for_turn,
    will_render_delta,
)
from pipeline.prompts.envelope import make_render_envelope
from pipeline.prompts.session import (
    PhysicalSessionKey,
    PromptSessionSplit,
    PromptSessionState,
    part_session_key,
)
from pipeline.prompts.types import (
    PromptCacheScope,
    PromptLayer,
    PromptPart,
    PromptStability,
)

# ---------------------------------------------------------------------------
# Fixtures: tiny part / envelope / state builders. Anchor ids are bare
# PromptPart.id (semantic identity); send-history keys are the M5
# composite ``id@version`` form via part_session_key.
# ---------------------------------------------------------------------------


def _role(name: str = "code_reviewer", *, version: int | None = None) -> PromptPart:
    return PromptPart(
        kind="role",
        name=name,
        source="core",
        body=f"role:{name}",
        version=version,
        layer=PromptLayer.ROLE,
    )


def _phase(name: str = "validate_plan", *, version: int | None = None) -> PromptPart:
    return PromptPart(
        kind="task",
        name=name,
        source="core",
        body=f"phase:{name}",
        version=version,
        layer=PromptLayer.PHASE,
    )


def _contract(
    name: str = "review_json", *, version: int | None = None,
) -> PromptPart:
    return PromptPart(
        kind="system_tail",
        name=name,
        source="code-owned",
        body=f"contract:{name}",
        version=version,
        layer=PromptLayer.CONTRACT,
    )


def _turn(name: str = "task_body") -> PromptPart:
    return PromptPart(
        kind="task",
        name=name,
        source="core",
        body=f"turn:{name}",
        layer=PromptLayer.TURN,
        stability=PromptStability.TURN,
        cache_scope=PromptCacheScope.NONE,
        volatile_reason="per-turn substitution",
    )


def _none_scope_static(name: str = "always_resend") -> PromptPart:
    return PromptPart(
        kind="role",
        name=name,
        source="core",
        body=f"none:{name}",
        stability=PromptStability.STATIC,
        cache_scope=PromptCacheScope.NONE,
        volatile_reason="always resend",
    )


def _envelope(*parts: PromptPart):
    text = "\n\n".join(p.body for p in parts)
    return make_render_envelope(text=text, parts=tuple(parts))


def _physical_key(scope: str = "per_phase:validate_plan") -> PhysicalSessionKey:
    return PhysicalSessionKey(
        run_id="run-1",
        runtime="claude",
        model_key="claude-opus-4-7",
        scope=scope,
    )


def _state(
    *,
    session_id: str | None = "sess-1",
    sent: frozenset[str] = frozenset(),
    role_id: str | None = None,
    phase_id: str | None = None,
    contract_ids: frozenset[str] = frozenset(),
) -> PromptSessionState:
    return PromptSessionState(
        key=_physical_key(),
        session_id=session_id,
        sent_part_keys=sent,
        active_role_id=role_id,
        active_phase_id=phase_id,
        active_contract_ids=contract_ids,
    )


# ---------------------------------------------------------------------------
# Branch 1: STATELESS — full render, no state_update.
# ---------------------------------------------------------------------------


class TestStatelessBranch:
    def test_stateless_full_render(self) -> None:
        env = _envelope(_role(), _turn(), _contract())
        result = select_parts_for_turn(
            env, state=None, split=PromptSessionSplit.STATELESS,
        )
        assert isinstance(result, SelectionResult)
        assert result.render_mode == "full"
        assert result.selected_parts == env.parts
        assert result.omitted_parts == ()
        assert result.state_update is None

    def test_stateless_ignores_existing_state(self) -> None:
        # Even if a stale state instance is somehow passed, STATELESS
        # must short-circuit to full render with no state_update —
        # otherwise the caller could accidentally use a sentinel
        # "stateless" state and end up reusing parts.
        env = _envelope(_role(), _turn())
        result = select_parts_for_turn(
            env,
            state=_state(sent=frozenset({"role:code_reviewer@0"})),
            split=PromptSessionSplit.STATELESS,
        )
        assert result.render_mode == "full"
        assert result.state_update is None
        assert result.selected_parts == env.parts


# ---------------------------------------------------------------------------
# Branch 2: state is None — full render, no state_update.
# ---------------------------------------------------------------------------


class TestNoneStateBranch:
    def test_none_state_full_render_without_state_update(self) -> None:
        env = _envelope(_role(), _turn(), _contract())
        result = select_parts_for_turn(
            env, state=None, split=PromptSessionSplit.PER_PHASE,
        )
        assert result.render_mode == "full"
        assert result.selected_parts == env.parts
        assert result.omitted_parts == ()
        assert result.state_update is None


# ---------------------------------------------------------------------------
# Branch 3: fresh session — full render, candidate update seeds keys.
# ---------------------------------------------------------------------------


class TestFreshSessionBranch:
    def test_fresh_session_full_render_and_candidate_update_seeds_sent_part_keys(
        self,
    ) -> None:
        env = _envelope(_role(), _phase(), _contract())
        state = _state(session_id=None)  # fresh: no provider session yet
        result = select_parts_for_turn(
            env, state=state, split=PromptSessionSplit.PER_PHASE,
        )
        assert result.render_mode == "full"
        assert result.selected_parts == env.parts
        assert result.omitted_parts == ()

        update = result.state_update
        assert update is not None
        # Every selected part's M5 composite key is present.
        assert update.sent_part_keys == frozenset(
            part_session_key(p) for p in env.parts
        )
        # PhysicalSessionKey survives untouched.
        assert update.key == state.key
        # Active anchors are derived from the envelope.
        assert update.active_role_id == "role:code_reviewer"
        assert update.active_phase_id == "task:validate_plan"
        assert update.active_contract_ids == frozenset({
            "system_tail:review_json",
        })

    def test_fresh_session_candidate_update_unions_with_existing_sent(
        self,
    ) -> None:
        # If the state was created with prior sent_part_keys (e.g. from
        # a previous interrupted run), the fresh-session candidate
        # extends the set rather than replacing it.
        env = _envelope(_role(), _phase())
        state = _state(
            session_id=None,
            sent=frozenset({"role:legacy@0"}),
        )
        result = select_parts_for_turn(
            env, state=state, split=PromptSessionSplit.PER_PHASE,
        )
        assert result.state_update is not None
        assert "role:legacy@0" in result.state_update.sent_part_keys
        assert "role:code_reviewer@0" in result.state_update.sent_part_keys


# ---------------------------------------------------------------------------
# Branch 4: resumed — delta render with omission and forced-resend rules.
# ---------------------------------------------------------------------------


class TestResumedDeltaBranch:
    def test_second_validate_plan_round_omits_unchanged_role_and_contract_parts(
        self,
    ) -> None:
        # Round 1: full render seeded sent_part_keys with role + contract
        # + phase (the stable ones). Round 2 envelope keeps the same
        # role / phase / contract but adds a fresh turn part. The
        # selector must keep volatile turn parts on the wire and omit
        # the stable parts already cached on the agent side.
        role = _role()
        phase = _phase()
        contract = _contract()
        round2_turn = _turn(name="round2_feedback")
        env = _envelope(role, phase, contract, round2_turn)

        sent = frozenset(
            part_session_key(p) for p in (role, phase, contract)
        )
        state = _state(
            sent=sent,
            role_id=role.id,
            phase_id=phase.id,
            contract_ids=frozenset({contract.id}),
        )
        result = select_parts_for_turn(
            env, state=state, split=PromptSessionSplit.PER_PHASE,
        )

        assert result.render_mode == "delta"
        # Volatile turn part stays on the wire.
        assert round2_turn in result.selected_parts
        # Stable seen role / phase / contract parts are omitted.
        assert role in result.omitted_parts
        assert phase in result.omitted_parts
        assert contract in result.omitted_parts
        # state_update unions in the new turn part's key.
        assert result.state_update is not None
        assert (
            part_session_key(round2_turn)
            in result.state_update.sent_part_keys
        )

    def test_phase_change_resends_phase_part(self) -> None:
        old_phase = _phase(name="validate_plan")
        new_phase = _phase(name="review_changes")
        sent = frozenset({part_session_key(old_phase), part_session_key(new_phase)})
        # The session previously sent both phase parts; only the active
        # anchor changed. The selector must detect the anchor flip and
        # force-resend the phase part even though its key is in
        # sent_part_keys.
        state = _state(
            sent=sent,
            phase_id=old_phase.id,
        )
        env = _envelope(new_phase)
        result = select_parts_for_turn(
            env, state=state, split=PromptSessionSplit.PER_PHASE,
        )
        assert result.render_mode == "delta"
        assert new_phase in result.selected_parts
        assert new_phase not in result.omitted_parts
        # Anchor advances on the candidate update.
        assert result.state_update is not None
        assert result.state_update.active_phase_id == new_phase.id

    def test_role_change_resends_role_part(self) -> None:
        old_role = _role(name="code_reviewer")
        new_role = _role(name="systems_architect")
        sent = frozenset({part_session_key(old_role), part_session_key(new_role)})
        state = _state(sent=sent, role_id=old_role.id)
        env = _envelope(new_role)
        result = select_parts_for_turn(
            env, state=state, split=PromptSessionSplit.PER_ROLE,
        )
        assert new_role in result.selected_parts
        assert result.state_update is not None
        assert result.state_update.active_role_id == new_role.id

    def test_contract_change_resends_contract_part(self) -> None:
        old_contract = _contract(name="review_json")
        new_contract = _contract(name="release_json")
        sent = frozenset({
            part_session_key(old_contract),
            part_session_key(new_contract),
        })
        state = _state(
            sent=sent,
            contract_ids=frozenset({old_contract.id}),
        )
        env = _envelope(new_contract)
        result = select_parts_for_turn(
            env, state=state, split=PromptSessionSplit.PER_PHASE,
        )
        assert new_contract in result.selected_parts
        assert result.state_update is not None
        assert result.state_update.active_contract_ids == frozenset({
            new_contract.id,
        })

    def test_part_id_version_change_treated_as_unseen(self) -> None:
        # Same kind/name, bumped version => different part_session_key
        # (id@version) => unseen branch fires regardless of anchor
        # comparison. The new versioned part lands on the wire.
        v1 = _contract(name="plan_json", version=1)
        v2 = _contract(name="plan_json", version=2)
        # Sent v1 only; v2 is a fresh key.
        state = _state(
            sent=frozenset({part_session_key(v1)}),
            contract_ids=frozenset({v1.id}),  # same semantic id
        )
        env = _envelope(v2)
        result = select_parts_for_turn(
            env, state=state, split=PromptSessionSplit.PER_PHASE,
        )
        assert v2 in result.selected_parts
        assert v2 not in result.omitted_parts
        # Per-key state update reflects v2.
        assert result.state_update is not None
        assert part_session_key(v2) in result.state_update.sent_part_keys

    def test_volatile_part_always_selected_even_when_key_seen(self) -> None:
        # Edge case: a turn part whose key happens to be in
        # sent_part_keys (e.g. identical body across rounds) must
        # still be sent — cache_scope=NONE / stability=TURN trumps
        # send-history.
        turn = _turn(name="task_body")
        state = _state(sent=frozenset({part_session_key(turn)}))
        env = _envelope(turn)
        result = select_parts_for_turn(
            env, state=state, split=PromptSessionSplit.PER_PHASE,
        )
        assert turn in result.selected_parts
        assert turn not in result.omitted_parts

    def test_cache_scope_none_static_part_always_selected(self) -> None:
        always = _none_scope_static()
        state = _state(sent=frozenset({part_session_key(always)}))
        env = _envelope(always)
        result = select_parts_for_turn(
            env, state=state, split=PromptSessionSplit.PER_PHASE,
        )
        assert always in result.selected_parts


# ---------------------------------------------------------------------------
# State update payload assertions
# ---------------------------------------------------------------------------


class TestStateUpdateShape:
    def test_state_update_records_selected_part_keys_not_bare_ids(self) -> None:
        # M5 stores composite ``id@version`` keys in sent_part_keys.
        # The selector must use the same form when seeding /
        # extending the set.
        v3 = _contract(name="plan_json", version=3)
        env = _envelope(v3)
        state = _state(session_id=None)
        result = select_parts_for_turn(
            env, state=state, split=PromptSessionSplit.PER_PHASE,
        )
        assert result.state_update is not None
        # Composite key is present.
        assert "system_tail:plan_json@3" in result.state_update.sent_part_keys
        # Bare id is not — that would silently treat a v4 bump as
        # already-sent.
        assert "system_tail:plan_json" not in result.state_update.sent_part_keys

    def test_state_update_only_reflects_actually_selected_parts(self) -> None:
        # Resumed delta: an omitted stable part's key must NOT appear
        # in the candidate update's sent_part_keys (it was already
        # there, so the union is a no-op for it; the assertion is
        # that newly-omitted parts do not get re-recorded under a
        # new identity).
        role = _role()
        contract = _contract()
        turn = _turn()
        env = _envelope(role, contract, turn)
        sent = frozenset({part_session_key(role), part_session_key(contract)})
        state = _state(
            sent=sent,
            role_id=role.id,
            contract_ids=frozenset({contract.id}),
        )
        result = select_parts_for_turn(
            env, state=state, split=PromptSessionSplit.PER_PHASE,
        )
        # The turn part is the only newly-selected one; role and
        # contract are omitted.
        assert role in result.omitted_parts
        assert contract in result.omitted_parts
        assert turn in result.selected_parts
        # selected_part_keys mirrors selected_parts exactly — no
        # phantom keys for omitted parts.
        assert result.selected_part_keys == (part_session_key(turn),)
        # And the union grows by exactly the turn part's key.
        assert result.state_update is not None
        delta = result.state_update.sent_part_keys - state.sent_part_keys
        assert delta == {part_session_key(turn)}


# ---------------------------------------------------------------------------
# Result shape sanity — both key tuples mirror their part tuples.
# ---------------------------------------------------------------------------


class TestResultShape:
    def test_selected_and_omitted_keys_mirror_part_tuples(self) -> None:
        role = _role()
        contract = _contract()
        turn = _turn()
        env = _envelope(role, contract, turn)
        sent = frozenset({part_session_key(role), part_session_key(contract)})
        state = _state(
            sent=sent,
            role_id=role.id,
            contract_ids=frozenset({contract.id}),
        )
        result = select_parts_for_turn(
            env, state=state, split=PromptSessionSplit.PER_PHASE,
        )
        assert result.selected_part_keys == tuple(
            part_session_key(p) for p in result.selected_parts
        )
        assert result.omitted_part_keys == tuple(
            part_session_key(p) for p in result.omitted_parts
        )

    @pytest.mark.parametrize(
        ("split", "state"),
        [
            (PromptSessionSplit.STATELESS, None),
            (PromptSessionSplit.STATELESS, _state()),
            (PromptSessionSplit.PER_PHASE, None),
        ],
    )
    def test_full_render_branches_have_no_state_update(
        self, split: PromptSessionSplit, state: PromptSessionState | None,
    ) -> None:
        env = _envelope(_role(), _turn())
        result = select_parts_for_turn(env, state=state, split=split)
        assert result.render_mode == "full"
        assert result.state_update is None


# ---------------------------------------------------------------------------
# M11.5 Fix 6 — prefix-contiguity defense
# ---------------------------------------------------------------------------


def _static_contract(name: str = "static_contract") -> PromptPart:
    """A genuinely STATIC/GLOBAL contract part — prefix-eligible by
    its own metadata, but whether it lands in the envelope's
    contiguous prefix depends on render-order context.
    """
    return PromptPart(
        kind="system_tail",
        name=name,
        source="code-owned",
        body=f"static_contract:{name}",
        layer=PromptLayer.CONTRACT,
        stability=PromptStability.STATIC,
        cache_scope=PromptCacheScope.GLOBAL,
    )


class TestPrefixContiguityDefenseM11_5:
    """M6 selector must only omit parts that live in the envelope's
    contiguous ``stable_prefix_parts``. A prefix-eligible part rendered
    AFTER a payload part is moved into ``turn_payload_parts`` by the
    M2 partitioner; the M6 selector must respect that and keep the
    part on the wire on a resumed turn instead of restitching a
    payload-position part the agent never saw at the start of the
    prompt.
    """

    def test_stable_contract_after_payload_not_omitted_on_delta(
        self,
    ) -> None:
        # Render order: STATIC role -> TURN body -> STATIC contract.
        # M2 cuts at the TURN payload, so the trailing STATIC contract
        # sits in turn_payload_parts even though it is prefix-eligible
        # by its own metadata. The selector must NOT omit it on
        # delta even when its part key is in sent_part_keys.
        role = _role()
        payload = _turn("body_v1")
        contract = _static_contract()
        env = _envelope(role, payload, contract)
        assert role in env.stable_prefix_parts
        assert contract not in env.stable_prefix_parts, (
            "contract sits after payload — M2 must demote it to payload"
        )

        sent = frozenset({
            part_session_key(role),
            part_session_key(contract),
        })
        state = _state(sent=sent, role_id=role.id)
        result = select_parts_for_turn(
            env, state=state, split=PromptSessionSplit.PER_PHASE,
        )
        # Role is in stable_prefix and is already sent → omittable.
        assert role in result.omitted_parts
        # Contract is NOT in stable_prefix → must stay on the wire
        # despite being already-sent and STATIC/GLOBAL.
        assert contract in result.selected_parts
        assert contract not in result.omitted_parts

    def test_prefix_part_still_omittable_on_delta(self) -> None:
        # Sanity counter-test: a STATIC part in the contiguous prefix
        # IS omitted on delta when already sent. The Fix 6 defense
        # tightens the rule but must not break the basic omission path.
        role = _role()
        contract = _static_contract()
        payload = _turn("body_v1")
        env = _envelope(role, contract, payload)
        assert role in env.stable_prefix_parts
        assert contract in env.stable_prefix_parts

        sent = frozenset({
            part_session_key(role),
            part_session_key(contract),
        })
        # Anchor ids must already be active to avoid the
        # role/contract-changed force-resend branches.
        state = _state(
            sent=sent,
            role_id=role.id,
            contract_ids=frozenset({contract.id}),
        )
        result = select_parts_for_turn(
            env, state=state, split=PromptSessionSplit.PER_PHASE,
        )
        assert role in result.omitted_parts
        assert contract in result.omitted_parts
        assert payload in result.selected_parts


# ---------------------------------------------------------------------------
# Resume-delta task drop (ADR 0026): a caller may ask to omit named parts
# from the wire on a resumed turn because the runtime already holds them in
# conversation history (e.g. the original task on a replan round). The drop
# fires ONLY on the delta path; full renders keep the part.
# ---------------------------------------------------------------------------


class TestDeltaDroppablePartIds:
    def test_drop_fires_only_on_delta_path(self) -> None:
        # Resumed delta: the droppable task is excluded from the wire and
        # surfaced in delta_dropped_part_keys.
        # Explicit id so the droppable set is unambiguous.
        task = PromptPart(
            kind="turn_input", name="replan_task", source="code-owned",
            body="TASK:\n<full task>", layer=PromptLayer.TURN,
            stability=PromptStability.TURN, cache_scope=PromptCacheScope.NONE,
            volatile_reason="per-turn", id="turn_input:replan_task",
        )
        critique = PromptPart(
            kind="reviewer_critique", name="critique", source="artifact",
            body="findings...", layer=PromptLayer.TURN,
            stability=PromptStability.TURN, cache_scope=PromptCacheScope.NONE,
            volatile_reason="per-turn", id="reviewer_critique:critique",
        )
        env = _envelope(task, critique)
        # Round-1 already sent the task key under this session.
        state = _state(sent=frozenset({part_session_key(task)}))

        result = select_parts_for_turn(
            env, state=state, split=PromptSessionSplit.PER_PHASE,
            delta_droppable_part_ids=frozenset({"turn_input:replan_task"}),
        )

        assert result.render_mode == "delta"
        # Task dropped from the wire, critique kept.
        assert task not in result.selected_parts
        assert critique in result.selected_parts
        assert part_session_key(task) not in result.selected_part_keys
        # Dropped, traceable.
        assert task in result.delta_dropped_parts
        assert part_session_key(task) in result.delta_dropped_part_keys

    def test_full_render_ignores_droppable_set(self) -> None:
        # Fresh session (session_id=None) → full render → droppable ignored,
        # task stays on the wire (round-1 fullness guarantee).
        task = PromptPart(
            kind="turn_input", name="replan_task", source="code-owned",
            body="TASK:\n<full task>", layer=PromptLayer.TURN,
            stability=PromptStability.TURN, cache_scope=PromptCacheScope.NONE,
            volatile_reason="per-turn", id="turn_input:replan_task",
        )
        env = _envelope(_role(), task)
        state = _state(session_id=None)  # fresh physical session

        result = select_parts_for_turn(
            env, state=state, split=PromptSessionSplit.PER_PHASE,
            delta_droppable_part_ids=frozenset({"turn_input:replan_task"}),
        )

        assert result.render_mode == "full"
        assert task in result.selected_parts
        assert result.delta_dropped_part_keys == ()

    def test_stateless_ignores_droppable_set(self) -> None:
        task = PromptPart(
            kind="turn_input", name="replan_task", source="code-owned",
            body="TASK:\n<full task>", layer=PromptLayer.TURN,
            stability=PromptStability.TURN, cache_scope=PromptCacheScope.NONE,
            volatile_reason="per-turn", id="turn_input:replan_task",
        )
        env = _envelope(_role(), task)
        result = select_parts_for_turn(
            env, state=None, split=PromptSessionSplit.STATELESS,
            delta_droppable_part_ids=frozenset({"turn_input:replan_task"}),
        )
        assert result.render_mode == "full"
        assert task in result.selected_parts
        assert result.delta_dropped_part_keys == ()

    def test_source_envelope_unchanged_by_drop(self) -> None:
        # The drop must never touch the source envelope — part_ids / hashes
        # describe the canonical full prompt regardless of droppable set.
        task = PromptPart(
            kind="turn_input", name="replan_task", source="code-owned",
            body="TASK:\n<full task>", layer=PromptLayer.TURN,
            stability=PromptStability.TURN, cache_scope=PromptCacheScope.NONE,
            volatile_reason="per-turn", id="turn_input:replan_task",
        )
        env = _envelope(_role(), task)
        # Same envelope object; the selector is pure and must not mutate it.
        before_ids = [p.id for p in env.parts]
        before_prefix, before_payload = env.prefix_hash, env.payload_hash
        state = _state(sent=frozenset({part_session_key(task)}))
        select_parts_for_turn(
            env, state=state, split=PromptSessionSplit.PER_PHASE,
            delta_droppable_part_ids=frozenset({"turn_input:replan_task"}),
        )
        assert [p.id for p in env.parts] == before_ids
        assert env.prefix_hash == before_prefix
        assert env.payload_hash == before_payload

    def test_dropped_key_not_added_but_prior_keys_preserved(self) -> None:
        # sent_part_keys is cumulative session memory: the dropped task key
        # was added on round 1 and MUST remain; the drop only means "this
        # turn adds no NEW sent key for it" (and never removes an old one).
        task = PromptPart(
            kind="turn_input", name="validate_plan_task", source="code-owned",
            body="TASK:\n<full task>", layer=PromptLayer.TURN,
            stability=PromptStability.TURN, cache_scope=PromptCacheScope.NONE,
            volatile_reason="per-turn", id="turn_input:validate_plan_task",
        )
        new_plan = PromptPart(
            kind="plan_tasks", name="execution_plan", source="artifact",
            body="## Tasks ...", layer=PromptLayer.TURN,
            stability=PromptStability.TURN, cache_scope=PromptCacheScope.NONE,
            volatile_reason="per-turn", id="plan_tasks:execution_plan",
            version=2,
        )
        env = _envelope(task, new_plan)
        task_key = part_session_key(task)
        # Round-1 already recorded the task key.
        state = _state(sent=frozenset({task_key}))
        result = select_parts_for_turn(
            env, state=state, split=PromptSessionSplit.PER_PHASE,
            delta_droppable_part_ids=frozenset({"turn_input:validate_plan_task"}),
        )
        assert result.state_update is not None
        # Prior task key preserved (cumulative), even though dropped this turn.
        assert task_key in result.state_update.sent_part_keys
        # New plan key newly recorded.
        assert part_session_key(new_plan) in result.state_update.sent_part_keys

    def test_completeness_invariant(self) -> None:
        # Every selected/omitted/dropped key traces back to a source part_id.
        role = _role()
        task = PromptPart(
            kind="turn_input", name="replan_task", source="code-owned",
            body="TASK:\n<full task>", layer=PromptLayer.TURN,
            stability=PromptStability.TURN, cache_scope=PromptCacheScope.NONE,
            volatile_reason="per-turn", id="turn_input:replan_task",
        )
        critique = PromptPart(
            kind="reviewer_critique", name="critique", source="artifact",
            body="findings", layer=PromptLayer.TURN,
            stability=PromptStability.TURN, cache_scope=PromptCacheScope.NONE,
            volatile_reason="per-turn", id="reviewer_critique:critique",
        )
        env = _envelope(role, task, critique)
        source_keys = {part_session_key(p) for p in env.parts}
        state = _state(
            sent=frozenset({part_session_key(role), part_session_key(task)}),
            role_id=role.id,
        )
        result = select_parts_for_turn(
            env, state=state, split=PromptSessionSplit.PER_PHASE,
            delta_droppable_part_ids=frozenset({"turn_input:replan_task"}),
        )
        union = (
            set(result.selected_part_keys)
            | set(result.omitted_part_keys)
            | set(result.delta_dropped_part_keys)
        )
        assert union <= source_keys  # no key absent from source


class TestWillRenderDelta:
    @pytest.mark.parametrize("split", list(PromptSessionSplit))
    @pytest.mark.parametrize(
        "state_factory",
        [
            ("none", lambda: None),
            ("fresh", lambda: _state(session_id=None)),
            ("resumed", lambda: _state(session_id="sess-1")),
        ],
        ids=lambda v: v[0] if isinstance(v, tuple) else str(v),
    )
    def test_predicate_matches_selector_mode(self, split, state_factory) -> None:
        _label, factory = state_factory
        state = factory()
        env = _envelope(_role(), _turn())
        predicted = will_render_delta(state, split)
        actual = select_parts_for_turn(env, state=state, split=split)
        assert predicted == (actual.render_mode == "delta")
