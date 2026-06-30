"""Mono/cross delivery-scope + next-action parity matrix (T3).

Locks the *parity* between the three operator topology choices and the
mono delivery-scope enforcement they feed, and the symmetry between the
engine decision and the SDK ``decide_delivery`` refusal. The individual
mechanisms are covered elsewhere (``test_delivery_scope.py``,
``test_auto_detect.py``, ``test_delivery_decision.py``); this file asserts
they stay *consistent with each other* so a future edit cannot let one
side drift:

* (1) ``strict_mono`` + companion-repo edits → a typed, reversible
  ``delivery_scope_violation`` blocker with a non-empty ``scope_disclosure``
  (never an exception); ``apply_commit_delivery`` refuses to ship and the
  decision stays ``status='pending'``.
* (2) ``expanded_mono`` + the *same* edits → not blocked, per-alias
  disclosure, the decision keeps no blocker so delivery proceeds.
* (3) a ``cross_recommended`` auto-detect resolution offers exactly three
  typed ``TopologyChoice`` next-actions (``start_cross`` / ``expanded_mono``
  / ``strict_mono``) whose machine-readable ``DeliveryScope`` args are the
  very scopes the mono enforcement in (1)/(2) consumes.
* (4) ``decide_delivery`` on a parked scope-blocked gate returns
  ``accepted=False``, ``blocker='delivery_scope_violation'``, and the
  ``scope_disclosure``.

Parity finding: no real gap was found — mono returns a *typed* blocker
(never a hard fail), and the cross-side ``TopologyChoice`` scopes map
1:1 onto the mono enforcement scopes. This file pins that invariant; no
source change was required for T3.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.engine.commit_delivery import (
    CommitDeliveryDecision,
    _scope_decision_fields,
    apply_commit_delivery,
)
from pipeline.engine.delivery_scope import (
    DELIVERY_SCOPE_VIOLATION,
    DeliveryScopeAssessment,
    assess_delivery_scope,
)
from pipeline.project.auto_detect import (
    TopologyChoice,
    apply_topology_choice,
    resolve_auto_detect,
)
from pipeline.runtime.run_shape import DeliveryScope, RunTopology
from pipeline.runtime.topology_detection import recommend_topology
from pipeline.runtime.work_kind_detection import (
    AutoDetectConfig,
    AutoDetectDecision,
    DetectionState,
    StaticWorkKindDetector,
)
from sdk.run_control.delivery import decide_delivery

# Same companion-repo edits drive both the strict and expanded branches —
# the parity is "identical input, scope decides block-vs-disclose".
_SIBLING_CHANGES: dict[str, tuple[str, ...]] = {
    "orcho-mcp": ("[orcho-mcp]/projection.py", "[orcho-mcp]/schema.json"),
}
_EXPECTED_DISCLOSURE = (
    "[orcho-mcp]/projection.py",
    "[orcho-mcp]/schema.json",
)


# ── (1)+(2) pure classification parity: identical edits, scope decides ────────


class TestMonoScopeClassificationParity:
    def test_strict_mono_companion_edits_yield_typed_reversible_blocker(self) -> None:
        a = assess_delivery_scope(
            scope=DeliveryScope.STRICT_MONO,
            sibling_changes=_SIBLING_CHANGES,
        )
        assert a.blocked is True
        assert a.in_scope is False
        assert a.blocker == DELIVERY_SCOPE_VIOLATION
        assert a.disclosure == _EXPECTED_DISCLOSURE
        assert a.affected_projects == ("orcho-mcp",)

    def test_expanded_mono_same_edits_disclose_without_blocking(self) -> None:
        a = assess_delivery_scope(
            scope=DeliveryScope.EXPANDED_MONO,
            sibling_changes=_SIBLING_CHANGES,
        )
        assert a.blocked is False
        assert a.in_scope is True
        assert a.blocker is None
        # Parity: the disclosure content is identical to the strict branch —
        # only the block/proceed verdict differs.
        assert a.disclosure == _EXPECTED_DISCLOSURE
        assert a.affected_projects == ("orcho-mcp",)

    def test_strict_and_expanded_disclose_the_same_files(self) -> None:
        strict = assess_delivery_scope(
            scope=DeliveryScope.STRICT_MONO, sibling_changes=_SIBLING_CHANGES,
        )
        expanded = assess_delivery_scope(
            scope=DeliveryScope.EXPANDED_MONO, sibling_changes=_SIBLING_CHANGES,
        )
        assert strict.disclosure == expanded.disclosure
        assert strict.affected_projects == expanded.affected_projects
        # The only divergence is the verdict.
        assert (strict.blocked, expanded.blocked) == (True, False)


# ── (1)+(2) engine decision-field + apply-guard parity ───────────────────────


class TestEngineDecisionFieldParity:
    def test_strict_assessment_projects_a_blocker_expanded_does_not(self) -> None:
        strict_fields = _scope_decision_fields(
            assess_delivery_scope(
                scope=DeliveryScope.STRICT_MONO,
                sibling_changes=_SIBLING_CHANGES,
            )
        )
        expanded_fields = _scope_decision_fields(
            assess_delivery_scope(
                scope=DeliveryScope.EXPANDED_MONO,
                sibling_changes=_SIBLING_CHANGES,
            )
        )
        # Strict parks (scope_blocker set); expanded ships (no blocker key).
        assert strict_fields["scope_blocker"] == DELIVERY_SCOPE_VIOLATION
        assert "scope_blocker" not in expanded_fields
        # Both still disclose the same sibling files + projects.
        assert strict_fields["scope_disclosure"] == _EXPECTED_DISCLOSURE
        assert expanded_fields["scope_disclosure"] == _EXPECTED_DISCLOSURE
        assert strict_fields["delivery_scope"] == "strict_mono"
        assert expanded_fields["delivery_scope"] == "expanded_mono"

    def _pending(self, **scope_fields: object) -> CommitDeliveryDecision:
        return CommitDeliveryDecision(
            action="none" if scope_fields.get("scope_blocker") else "skip",
            status="pending",
            run_id="r1",
            decision_id="d1",
            project_path=Path("/nonexistent/project"),
            source_path=Path("/nonexistent/source"),
            baseline_ref="HEAD",
            dirty=True,
            **scope_fields,  # type: ignore[arg-type]
        )

    def test_apply_refuses_scope_blocked_decision_and_stays_pending(
        self, tmp_path: Path,
    ) -> None:
        decision = self._pending(
            delivery_scope="strict_mono",
            scope_blocker=DELIVERY_SCOPE_VIOLATION,
            scope_disclosure=_EXPECTED_DISCLOSURE,
        )
        applied = apply_commit_delivery(
            decision, run_dir=tmp_path, no_interactive=True,
        )
        # Parked, untouched: never shipped, no audit artifact persisted, the
        # project checkout (a nonexistent path) is never reached.
        assert applied.status == "pending"
        assert applied.action == "none"
        assert applied.scope_blocker == DELIVERY_SCOPE_VIOLATION
        assert not list(tmp_path.glob("*.json"))

    def test_apply_does_not_park_an_expanded_decision_without_blocker(
        self, tmp_path: Path,
    ) -> None:
        # An expanded-scope decision carries a delivery_scope but NO blocker, so
        # the scope guard must not park it: a 'skip' action flows through to the
        # skipped terminal (proving delivery is allowed to proceed).
        decision = self._pending(
            delivery_scope="expanded_mono",
            scope_disclosure=_EXPECTED_DISCLOSURE,
        )
        applied = apply_commit_delivery(
            decision, run_dir=tmp_path, no_interactive=True,
        )
        assert applied.scope_blocker == ""
        assert applied.status == "skipped"


# ── (3) cross_recommended → exactly three typed next-actions ─────────────────


_SIGNALS = {
    "mcp schema": ["orcho-core", "orcho-mcp"],
    "sdk wire": ["orcho-core", "orcho-mcp"],
}


def _cross_resolution():
    """A high-confidence ``cross_recommended`` auto-detect resolution."""
    topo = recommend_topology(
        "change the sdk wire and regenerate the mcp schema", signals=_SIGNALS,
    )
    assert topo.topology is RunTopology.CROSS_RECOMMENDED  # guard the fixture
    decision = AutoDetectDecision(
        recommended_profile="feature",
        recommended_mode="pro",
        confidence=0.9,
        rationale="signal-bearing",
        risk_flags=("schema",),
        recommended_topology=topo.topology,
        delivery_projects=topo.projects,
        topology_reason=topo.reason,
    )
    config = AutoDetectConfig.parse({
        "policy": "trust_above_threshold",
        "confidence_threshold": 0.7,
        "fallback_profile": "feature",
        "on_low_confidence": "fallback",
        "on_error": "fallback",
    })
    res = resolve_auto_detect(
        task="t", project="/p", interactive=False,
        config=config, detector=StaticWorkKindDetector(decision),
    )
    assert res.detection_state is DetectionState.RECOMMENDED
    assert res.recommended_topology is RunTopology.CROSS_RECOMMENDED
    return res


class TestCrossRecommendedNextActions:
    def test_exactly_three_typed_choices_in_order(self) -> None:
        choices = list(TopologyChoice)
        assert choices == [
            TopologyChoice.START_CROSS,
            TopologyChoice.EXPANDED_MONO,
            TopologyChoice.STRICT_MONO,
        ]
        # Machine-readable 1-based selector args (the CLI/MCP next-action ids).
        assert TopologyChoice.from_number(1) is TopologyChoice.START_CROSS
        assert TopologyChoice.from_number(2) is TopologyChoice.EXPANDED_MONO
        assert TopologyChoice.from_number(3) is TopologyChoice.STRICT_MONO

    def test_each_choice_maps_to_a_machine_readable_scope(self) -> None:
        res = _cross_resolution()
        mapping = {
            TopologyChoice.START_CROSS: DeliveryScope.CROSS,
            TopologyChoice.EXPANDED_MONO: DeliveryScope.EXPANDED_MONO,
            TopologyChoice.STRICT_MONO: DeliveryScope.STRICT_MONO,
        }
        for choice, scope in mapping.items():
            applied = apply_topology_choice(res, choice)
            assert applied.delivery_scope is scope
            # An explicit topology directive never mutates the run's profile,
            # mode, or recommendation — it only selects the delivery scope.
            assert applied.actual_profile == res.actual_profile
            assert applied.actual_mode == res.actual_mode
            assert applied.recommended_topology is res.recommended_topology

    def test_choice_scopes_feed_the_same_mono_enforcement(self) -> None:
        # The parity link: the EXPANDED_MONO / STRICT_MONO scopes an operator
        # picks from the cross recommendation are the exact scopes the mono
        # delivery enforcement classifies — discloses vs blocks, symmetrically.
        res = _cross_resolution()
        expanded_scope = apply_topology_choice(
            res, TopologyChoice.EXPANDED_MONO,
        ).delivery_scope
        strict_scope = apply_topology_choice(
            res, TopologyChoice.STRICT_MONO,
        ).delivery_scope

        expanded = assess_delivery_scope(
            scope=expanded_scope, sibling_changes=_SIBLING_CHANGES,
        )
        strict = assess_delivery_scope(
            scope=strict_scope, sibling_changes=_SIBLING_CHANGES,
        )
        assert isinstance(expanded, DeliveryScopeAssessment)
        assert expanded.blocked is False and expanded.in_scope is True
        assert strict.blocked is True
        assert strict.blocker == DELIVERY_SCOPE_VIOLATION


# ── (4) SDK decide_delivery: scope-blocked typed refusal ─────────────────────


def _park_scope_blocked_gate(tmp_path: Path, *, run_id: str = "r1") -> Path:
    """Write a parked, scope-blocked delivery gate; return its runs_dir.

    No git is needed: ``decide_delivery``'s scope guard refuses a shipping
    action before it ever re-resolves a diff, so a hand-built gate context is
    a faithful and fast fixture for the refusal contract.
    """
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    ctx = {
        "action": "none",
        "status": "pending",
        "run_id": run_id,
        "decision_id": "d1",
        "project_path": str(tmp_path / "project"),
        "source_path": str(tmp_path / "source"),
        "baseline_ref": "HEAD",
        "dirty": True,
        "delivery_scope": "strict_mono",
        "scope_blocker": DELIVERY_SCOPE_VIOLATION,
        "scope_disclosure": list(_EXPECTED_DISCLOSURE),
    }
    meta = {
        "status": "halted",
        "halt_reason": "commit_delivery_scope_blocked",
        "project": str(tmp_path / "project"),
        "commit_delivery": ctx,
    }
    (run_dir / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8",
    )
    return runs_dir


class TestDecideDeliveryScopeBlocked:
    def test_approve_returns_typed_blocker_and_disclosure(
        self, tmp_path: Path,
    ) -> None:
        runs_dir = _park_scope_blocked_gate(tmp_path)
        result = decide_delivery("r1", "approve", runs_dir=runs_dir, cwd=None)
        assert result.accepted is False
        assert result.blocker == "delivery_scope_violation"
        assert result.scope_disclosure == _EXPECTED_DISCLOSURE
        assert result.terminal_outcome == "halted"

    def test_apply_is_refused_too(self, tmp_path: Path) -> None:
        runs_dir = _park_scope_blocked_gate(tmp_path)
        result = decide_delivery("r1", "apply", runs_dir=runs_dir, cwd=None)
        assert result.accepted is False
        assert result.blocker == "delivery_scope_violation"
        assert result.scope_disclosure == _EXPECTED_DISCLOSURE

    def test_skip_stays_expressible_under_scope_block(self, tmp_path: Path) -> None:
        # Parity with the reversible-gate promise: a non-shipping action is not
        # refused by the scope guard — the operator can always skip / halt.
        runs_dir = _park_scope_blocked_gate(tmp_path)
        result = decide_delivery("r1", "skip", runs_dir=runs_dir, cwd=None)
        assert result.blocker != "delivery_scope_violation"
