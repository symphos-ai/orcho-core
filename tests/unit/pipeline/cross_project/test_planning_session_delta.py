"""ADR 0055 — session-aware delta rendering for the cross planning loop.

:func:`pipeline.cross_project.session_invoke.session_aware_invoke` sits
between the cross-plan / cross_replan prompt builder and the plan agent.
It receives a :class:`~pipeline.prompts.turn.PromptTurn`, runs the M6
selector under :data:`PromptSessionSplit.PER_PHASE`, and either sends
the full prompt (round 1 / cold session) or a delta wire prompt (resumed
round 2) that omits the stable parts the architect already saw.

Tests are scoped to the helper: a plain ``prompt_sessions`` dict plus a
recording agent pin the contract without spinning up the cross loop.
"""

from __future__ import annotations

from typing import Any

from pipeline.cross_project.session_invoke import session_aware_invoke
from pipeline.prompts.session import PromptSessionSplit
from pipeline.prompts.turn import PromptTurn, PromptTurnEditor
from pipeline.prompts.types import (
    PromptCacheScope,
    PromptLayer,
    PromptPart,
    PromptStability,
)


class _RecordingAgent:
    """Plan-agent stub recording every wire prompt it received."""

    def __init__(
        self,
        *,
        model: str = "claude-opus-4-7",
        responses: list[str] | None = None,
        session_id: str | None = "sess-1",
    ) -> None:
        self.model = model
        self.session_id = session_id
        self.calls: list[dict[str, Any]] = []
        self._responses = list(responses or ["plan"])

    def invoke(
        self, prompt: str, cwd: str, *, continue_session: bool = False,
    ) -> str:
        self.calls.append({"prompt": prompt, "continue_session": continue_session})
        head, *rest = self._responses
        self._responses = rest or [head]
        return head


def _role() -> PromptPart:
    return PromptPart(
        kind="role", name="systems_architect", source="core",
        body="role:systems_architect", layer=PromptLayer.ROLE,
    )


def _contract(name: str = "cross_subtask_blocks") -> PromptPart:
    return PromptPart(
        kind="system_tail", name=name, source="code-owned",
        body=f"<orcho:system-block>{name}</orcho:system-block>",
        layer=PromptLayer.CONTRACT,
    )


def _phase_part(name: str) -> PromptPart:
    # The task framing (cross_plan vs cross_replan) sits on the PHASE
    # layer; a different id on round 2 is "unseen" and forced onto the
    # wire by the selector.
    return PromptPart(
        kind="task", name=name, source="core",
        body=f"task:{name}", layer=PromptLayer.PHASE, id=f"task:{name}",
    )


def _turn_input(body: str, name: str = "cross_replan_input") -> PromptPart:
    return PromptPart(
        kind="turn_input", name=name, source="code-owned", body=body,
        layer=PromptLayer.TURN, stability=PromptStability.TURN,
        cache_scope=PromptCacheScope.NONE, volatile_reason="turn input",
        id=name,
    )


def _make_turn(*parts: PromptPart) -> PromptTurn:
    """Build a PromptTurn from parts in order."""
    editor = PromptTurnEditor()
    for p in parts:
        editor.append(p)
    return editor.build()


def _kwargs(store: dict, **over):
    base = dict(
        prompt_sessions=store, run_id="20260529_005304", phase="cross_plan",
        cwd="/repo", continue_session=False,
    )
    base.update(over)
    return base


# ── round 1 full → round 2 delta ──────────────────────────────────────


def test_round1_sends_full_prompt_and_seeds_store() -> None:
    agent = _RecordingAgent()
    store: dict = {}
    turn = _make_turn(_role(), _contract(), _phase_part("cross_plan"))
    text = turn.text

    out = session_aware_invoke(
        agent, turn=turn, **_kwargs(store, continue_session=False),
    )
    assert out == "plan"
    assert agent.calls[0]["prompt"] == text
    assert store, "round 1 must seed a reusable prompt-session state"


def test_round2_delta_omits_already_sent_stable_parts() -> None:
    agent = _RecordingAgent(responses=["round1", "round2"])
    store: dict = {}
    role, contract = _role(), _contract()

    turn_r1 = _make_turn(
        role, contract, _phase_part("cross_plan"), _turn_input("critique A"),
    )
    session_aware_invoke(
        agent, turn=turn_r1, **_kwargs(store, continue_session=False),
    )

    # Round 2: same role + contract (already sent), new replan task
    # framing + new critique.
    turn_r2 = _make_turn(
        role, contract, _phase_part("cross_replan"), _turn_input("critique B"),
    )
    session_aware_invoke(
        agent, turn=turn_r2, **_kwargs(store, continue_session=True),
    )

    wire2 = agent.calls[1]["prompt"]
    # Decision-bearing delta is present...
    assert "critique B" in wire2
    assert "task:cross_replan" in wire2
    # ...but the stable role / contract bodies the architect already has
    # are omitted from the resumed turn.
    assert "role:systems_architect" not in wire2
    assert "cross_subtask_blocks" not in wire2
    assert agent.calls[1]["continue_session"] is True


def test_round2_wire_strictly_smaller_than_round1() -> None:
    agent = _RecordingAgent(responses=["r1", "r2"])
    store: dict = {}
    role, contract = _role(), _contract()

    turn_r1 = _make_turn(
        role, contract, _phase_part("cross_plan"), _turn_input("c1"),
    )
    session_aware_invoke(
        agent, turn=turn_r1, **_kwargs(store, continue_session=False),
    )
    turn_r2 = _make_turn(
        role, contract, _phase_part("cross_plan"), _turn_input("c2"),
    )
    session_aware_invoke(
        agent, turn=turn_r2, **_kwargs(store, continue_session=True),
    )
    assert len(agent.calls[1]["prompt"]) < len(agent.calls[0]["prompt"])


# ── degraded paths: stateless / cold session / no envelope ────────────


def test_stateless_split_keeps_full_prompt_every_round() -> None:
    agent = _RecordingAgent(responses=["r1", "r2"])
    store: dict = {}
    role, contract = _role(), _contract()
    for _ in range(2):
        turn = _make_turn(
            role, contract, _phase_part("cross_plan"), _turn_input("c"),
        )
        session_aware_invoke(
            agent, turn=turn,
            **_kwargs(store, split=PromptSessionSplit.STATELESS),
        )
    assert agent.calls[0]["prompt"] == agent.calls[1]["prompt"]
    assert store == {}, "stateless leaves no reusable state"


def test_continue_session_false_forces_full_even_with_prior_state() -> None:
    agent = _RecordingAgent(responses=["r1", "r2"])
    store: dict = {}
    role, contract = _role(), _contract()

    turn_r1 = _make_turn(
        role, contract, _phase_part("cross_plan"), _turn_input("c1"),
    )
    session_aware_invoke(
        agent, turn=turn_r1, **_kwargs(store, continue_session=False),
    )
    # Cold restart: store may carry prior state, but continue_session is
    # False so the agent context is fresh — must render full again.
    turn_r2 = _make_turn(
        role, contract, _phase_part("cross_plan"), _turn_input("c2"),
    )
    session_aware_invoke(
        agent, turn=turn_r2, **_kwargs(store, continue_session=False),
    )
    assert agent.calls[1]["prompt"] == turn_r2.text


def test_missing_stable_parts_falls_back_to_full_prompt() -> None:
    # A turn with only TURN/NONE parts (no prefix-eligible parts)
    # means the selector has nothing to omit — full render.
    agent = _RecordingAgent()
    store: dict = {}
    part = PromptPart(
        kind="task", name="test", source="core",
        body="raw body",
        stability=PromptStability.TURN,
        cache_scope=PromptCacheScope.NONE,
        volatile_reason="test",
    )
    editor = PromptTurnEditor()
    editor.append(part)
    turn = editor.build()
    out = session_aware_invoke(
        agent, turn=turn, **_kwargs(store, continue_session=False),
    )
    assert out == "plan"
    # Full prompt is sent — no stable parts to omit.
    assert agent.calls[0]["prompt"] == "raw body"
    # Session state may be stored but the render was full (no delta omissions).
