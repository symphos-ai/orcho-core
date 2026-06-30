"""M14.3 — Tool-result clearing event evidence (observe-only).

Tests for ``pipeline.observability.context_clearing``. The M14.3
contract is evidence-first: every record describes what *would* be
safe to clear under the M14.2 taxonomy and how many tokens /
artefacts are involved, but no actual clearing happens until a
runtime clearing API exists.

This test file pins:

- the 16-field durable shape ``DURABLE_FIELDS``;
- the observe-only defaults (``kind="eligible_tool_results"`` and
  ``cleared_tokens=0`` / ``artifact_refs=[]`` / ``cache_effect="none"``
  stay at safe defaults until a clearing primitive ships);
- the M14.2 classifier integration (clearable = RE_FETCHABLE ∪
  PERSISTED_ARTIFACT; retained = DECISION_BEARING ∪ EPHEMERAL);
- correlation with the sibling ``context_growth`` / ``prompt_render``
  records via ``prefix_hash`` / ``payload_hash`` / ``wire_chars``;
- writer-side stamping in ``_session_aware_invoke`` against a
  fake-provider stub (no real model call needed);
- the per-surface extractor taxonomy (plan / replan /
  validate_plan / implement / rounds review/repair split — the
  M14.3 catch-up of the M14.1 deferral);
- the safe-fallthrough invariant: a "clear_tool_results" kind is
  accepted (forward-compat for the future actual-clearing
  primitive) but any other kind is rewritten to
  ``"eligible_tool_results"``.
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from pipeline.observability.context_clearing import (
    DURABLE_FIELDS,
    PhaseContextClearing,
    extract_context_clearing_traces,
    normalize_context_clearing,
)
from pipeline.observability.output_class import OutputClass

# ── Synthetic payloads ───────────────────────────────────────────────────────


def _payload(
    *,
    phase: str = "plan",
    render_mode: str = "full",
    prefix_hash: str = "abc",
    payload_hash: str = "def",
    wire_chars: int = 1024,
    clearable_tokens: int = 320,
    clearable_part_ids: list[str] | None = None,
    retained_part_ids: list[str] | None = None,
    class_counts: dict[str, int] | None = None,
) -> dict:
    return {
        "kind": "eligible_tool_results",
        "trigger": "phase_invocation",
        "phase": phase,
        "round": None,
        "surface_id": None,
        "render_mode": render_mode,
        "prefix_hash": prefix_hash,
        "payload_hash": payload_hash,
        "wire_chars": wire_chars,
        "clearable_tokens": clearable_tokens,
        "clearable_part_ids": clearable_part_ids
            if clearable_part_ids is not None
            else ["role:systems_architect@0", "task:plan@0"],
        "retained_part_ids": retained_part_ids
            if retained_part_ids is not None
            else ["turn_input:plan_task@0"],
        "class_counts": class_counts if class_counts is not None else {
            "re_fetchable":       2,
            "persisted_artifact": 0,
            "ephemeral":          0,
            "decision_bearing":   1,
        },
        "cleared_tokens": 0,
        "artifact_refs": [],
        "cache_effect": "none",
    }


def _covered_session() -> dict:
    """Fixture touching every covered surface so the shape guard
    runs against the full M14.3 surface set including round-side
    review/repair split (added in M14.3, deferred from M14.1)."""
    return {
        "phases": {
            "plan": [
                {"attempt": 1, "context_clearing": _payload(phase="plan")},
                {
                    "attempt": 2,
                    "replan_critique": "needs more tests",
                    "context_clearing": _payload(phase="plan"),
                },
            ],
            "validate_plan": [
                {
                    "attempt": 1,
                    "context_clearing": _payload(phase="validate_plan"),
                },
            ],
            "implement": {
                "context_clearing": _payload(phase="implement"),
            },
            "rounds": [
                {
                    "round": 1,
                    "context_clearing_review": _payload(
                        phase="review_changes",
                    ),
                    "context_clearing_repair": _payload(
                        phase="repair_changes",
                    ),
                },
            ],
        },
    }


# ── Durable shape ────────────────────────────────────────────────────────────


class TestDurableShape:
    def test_durable_fields_count_is_sixteen(self) -> None:
        # M14.3 ships 16 durable fields: 5 attribution + 4 render
        # correlation + 4 eligibility observables + 3 reserved
        # cleared-side fields. Drift here means a downstream
        # consumer (future evidence projection, dashboard, lab
        # probe) needs review.
        assert len(DURABLE_FIELDS) == 16

    def test_normalize_returns_exactly_the_durable_key_set(self) -> None:
        result = normalize_context_clearing(_payload())
        assert set(result.keys()) == set(DURABLE_FIELDS)

    def test_all_durable_fields_present_on_every_extracted_trace(self) -> None:
        traces = extract_context_clearing_traces(_covered_session())
        assert traces, "covered fixture must produce non-empty traces"
        for trace in traces:
            missing = set(DURABLE_FIELDS) - set(trace.payload.keys())
            extra = set(trace.payload.keys()) - set(DURABLE_FIELDS)
            assert not missing and not extra, (
                f"trace {trace.source_path!r} payload shape drift: "
                f"missing={sorted(missing)} extra={sorted(extra)}"
            )

    def test_every_trace_is_json_serializable(self) -> None:
        traces = extract_context_clearing_traces(_covered_session())
        for trace in traces:
            encoded = json.dumps(dataclasses.asdict(trace))
            decoded = json.loads(encoded)
            assert decoded["payload"]["kind"] == "eligible_tool_results"


# ── Observe-only defaults ────────────────────────────────────────────────────


class TestObserveOnlyDefaults:
    """The M14.3 brief locks observe-only behaviour: nothing is
    actually cleared, so ``cleared_tokens`` stays at zero,
    ``artifact_refs`` stays empty, and ``cache_effect`` stays
    ``"none"``. ``kind`` defaults to the observe-only event name
    so a future writer can flip to ``"clear_tool_results"`` once
    a runtime clearing API exists."""

    def test_kind_defaults_to_eligible_tool_results(self) -> None:
        payload = _payload()
        payload.pop("kind", None)
        result = normalize_context_clearing(payload)
        assert result["kind"] == "eligible_tool_results"

    def test_kind_accepts_future_clear_tool_results_value(self) -> None:
        # Forward-compat: a future writer that actually clears can
        # set kind="clear_tool_results" and the normalizer passes
        # it through. Until that primitive ships, the writer in
        # M14.3 only emits "eligible_tool_results".
        payload = _payload()
        payload["kind"] = "clear_tool_results"
        result = normalize_context_clearing(payload)
        assert result["kind"] == "clear_tool_results"

    def test_unknown_kind_falls_back_to_eligible_tool_results(self) -> None:
        # Defense-in-depth: a writer drift that stamps a typo
        # ("clearing", "cleared", anything not in the two valid
        # values) must not slip a non-canonical kind through.
        payload = _payload()
        payload["kind"] = "clearing"
        result = normalize_context_clearing(payload)
        assert result["kind"] == "eligible_tool_results"

    def test_trigger_defaults_to_phase_invocation(self) -> None:
        payload = _payload()
        payload.pop("trigger", None)
        result = normalize_context_clearing(payload)
        assert result["trigger"] == "phase_invocation"

    @pytest.mark.parametrize(
        ("field", "expected_default"),
        [
            ("clearable_tokens", 0),
            ("cleared_tokens", 0),
        ],
    )
    def test_token_counter_defaults_to_zero(
        self, field: str, expected_default: int,
    ) -> None:
        payload = _payload()
        payload.pop(field, None)
        result = normalize_context_clearing(payload)
        assert result[field] == expected_default

    @pytest.mark.parametrize(
        "list_field",
        ["clearable_part_ids", "retained_part_ids", "artifact_refs"],
    )
    def test_list_fields_default_to_empty(self, list_field: str) -> None:
        payload = _payload()
        payload.pop(list_field, None)
        result = normalize_context_clearing(payload)
        assert result[list_field] == []

    @pytest.mark.parametrize(
        "list_field",
        ["clearable_part_ids", "retained_part_ids", "artifact_refs"],
    )
    def test_list_fields_are_copied_not_aliased(self, list_field: str) -> None:
        src = ["x:y@0"]
        payload = _payload()
        payload[list_field] = src
        result = normalize_context_clearing(payload)
        result[list_field].append("z:q@0")
        assert src == ["x:y@0"]

    def test_class_counts_defaults_to_zeroed_dict(self) -> None:
        payload = _payload()
        payload.pop("class_counts", None)
        result = normalize_context_clearing(payload)
        # Every OutputClass key present, all zero.
        assert set(result["class_counts"].keys()) == {
            c.value for c in OutputClass
        }
        assert all(v == 0 for v in result["class_counts"].values())

    def test_cache_effect_defaults_to_none(self) -> None:
        # Observe-only mode: nothing is cleared, so no cache
        # invalidation happens. The literal string "none" carries
        # that semantic without a Python ``None`` ambiguity in
        # JSON consumers.
        payload = _payload()
        payload.pop("cache_effect", None)
        result = normalize_context_clearing(payload)
        assert result["cache_effect"] == "none"


# ── Extractor taxonomy (M14.3 catch-up + new round-side split) ───────────────


class TestExtractorTaxonomy:
    def test_plan_attempt_one_without_critique_is_plan_surface(self) -> None:
        session = {
            "phases": {
                "plan": [{"attempt": 1, "context_clearing": _payload()}],
            },
        }
        traces = extract_context_clearing_traces(session)
        assert [(t.phase, t.trace_surface) for t in traces] == [
            ("plan", "plan"),
        ]

    def test_plan_attempt_two_with_critique_is_replan_surface(self) -> None:
        session = {
            "phases": {
                "plan": [
                    {"attempt": 1, "context_clearing": _payload()},
                    {
                        "attempt": 2,
                        "replan_critique": "redo",
                        "context_clearing": _payload(),
                    },
                ],
            },
        }
        surfaces = [
            t.trace_surface
            for t in extract_context_clearing_traces(session)
        ]
        assert surfaces == ["plan", "replan"]

    def test_implement_is_a_single_dict_not_a_list(self) -> None:
        session = {
            "phases": {
                "implement": {"context_clearing": _payload()},
            },
        }
        traces = extract_context_clearing_traces(session)
        assert [(t.phase, t.attempt, t.round) for t in traces] == [
            ("implement", None, None),
        ]

    def test_rounds_review_repair_split_emits_two_records(self) -> None:
        # M14.3 introduces the round-side context_clearing split
        # that M14.1 deferred for context_growth (the catch-up
        # promotion now also happens in session_adapters; tested
        # there). For context_clearing the extractor surfaces both
        # sides as independent trace records.
        session = {
            "phases": {
                "rounds": [
                    {
                        "round": 1,
                        "context_clearing_review": _payload(
                            phase="review_changes",
                        ),
                        "context_clearing_repair": _payload(
                            phase="repair_changes",
                        ),
                    },
                ],
            },
        }
        traces = extract_context_clearing_traces(session)
        kinds = [(t.phase, t.round) for t in traces]
        assert kinds == [
            ("review_changes", 1),
            ("repair_changes", 1),
        ]

    def test_missing_context_clearing_keys_are_skipped_silently(self) -> None:
        session = {
            "phases": {
                "plan": [{"attempt": 1}],
                "implement": {},
                "rounds": [{"round": 1}],
            },
        }
        assert extract_context_clearing_traces(session) == []

    def test_non_dict_session_returns_empty(self) -> None:
        assert extract_context_clearing_traces("not a dict") == []  # type: ignore[arg-type]
        assert extract_context_clearing_traces(None) == []  # type: ignore[arg-type]
        assert extract_context_clearing_traces([]) == []  # type: ignore[arg-type]


# ── Render correlation ───────────────────────────────────────────────────────


class TestRenderCorrelation:
    def test_correlation_keys_pass_through(self) -> None:
        payload = _payload(
            render_mode="delta",
            prefix_hash="sha256:" + "0" * 32,
            payload_hash="sha256:" + "f" * 32,
            wire_chars=4096,
        )
        result = normalize_context_clearing(payload)
        assert result["render_mode"] == "delta"
        assert result["prefix_hash"] == "sha256:" + "0" * 32
        assert result["payload_hash"] == "sha256:" + "f" * 32
        assert result["wire_chars"] == 4096


# ── Writer-side stamping (fake-provider unit test) ───────────────────────────


class TestWriterStamping:
    """The writer in ``_session_aware_invoke`` classifies every
    envelope part through the M14.2 classifier, sums per-part
    token estimates for the clearable classes, and stamps the
    eligibility evidence. Exercised here against a fake-provider
    stub so no real model call is needed."""

    def _make_state(self):
        from pipeline.plugins import PluginConfig
        from pipeline.runtime import PipelineState
        return PipelineState(
            task="Probe clearing eligibility",
            project_dir="/proj",
            plugin=PluginConfig(),
            extras={"run_id": "run-cc-1"},
        )

    def _make_turn_mixed(self):
        """Turn with one part of each clearable / retained
        class so the writer's classification + tokens sum can be
        asserted in one shot."""
        from pipeline.prompts.turn import PromptTurnEditor
        from pipeline.prompts.types import (
            PromptCacheScope,
            PromptPart,
            PromptStability,
        )

        # RE_FETCHABLE (clearable).
        role = PromptPart(
            kind="role",
            name="systems_architect",
            source="core",
            body="You are the systems architect.",
            stability=PromptStability.STATIC,
            cache_scope=PromptCacheScope.GLOBAL,
        )
        # PERSISTED_ARTIFACT (clearable). Mirrors the production
        # builder shape after the path-leak fix: body = reviewed file
        # content only; on-disk path lives in metadata-only
        # ``artifact_path`` and never in body bytes.
        artifact = PromptPart(
            kind="artifact",
            name="validate_plan",
            source="artifact",
            body="# Plan",
            artifact_path="/tmp/plan.md",
            layer=__import__(
                "pipeline.prompts.types", fromlist=["PromptLayer"],
            ).PromptLayer.TURN,
            stability=PromptStability.TURN,
            cache_scope=PromptCacheScope.NONE,
            volatile_reason="reviewed file body",
        )
        # DECISION_BEARING (retained).
        turn_input = PromptPart(
            kind="turn_input",
            name="plan_task",
            source="code-owned",
            body="TASK:\nFix the bug",
            layer=__import__(
                "pipeline.prompts.types", fromlist=["PromptLayer"],
            ).PromptLayer.TURN,
            stability=PromptStability.TURN,
            cache_scope=PromptCacheScope.NONE,
            volatile_reason="user task framing",
        )
        return (
            PromptTurnEditor()
            .append(role)
            .append(artifact)
            .append(turn_input)
            .build()
        )

    class _FakeAgent:
        model = "fake-model"
        session_id = None
        last_estimated_tokens_in = 200
        last_estimated_tokens_out = 50

        def invoke(self, prompt, cwd, *, continue_session=False,
                   attachments=(), mutates_artifacts=False):
            return "ok"

    def test_writer_classifies_each_part_and_sums_clearable_tokens(
        self,
    ) -> None:
        from pipeline.phases.builtin import _session_aware_invoke

        state = self._make_state()
        turn = self._make_turn_mixed()

        _session_aware_invoke(
            self._FakeAgent(), state,
            phase="plan",
            turn=turn,
            cwd="/proj",
        )

        cc = state.phase_log["plan"]["context_clearing"]
        # Identity slots.
        assert cc["kind"] == "eligible_tool_results"
        assert cc["trigger"] == "phase_invocation"
        assert cc["phase"] == "plan"
        # Class counts: 1 re_fetchable + 1 persisted_artifact +
        # 1 decision_bearing + 0 ephemeral.
        assert cc["class_counts"] == {
            "re_fetchable":       1,
            "persisted_artifact": 1,
            "ephemeral":          0,
            "decision_bearing":   1,
        }
        # Clearable IDs include the role (RE_FETCHABLE) and the
        # artifact (PERSISTED_ARTIFACT); retained includes the
        # turn_input (DECISION_BEARING).
        assert "role:systems_architect@0" in cc["clearable_part_ids"]
        assert "artifact:validate_plan@0" in cc["clearable_part_ids"]
        assert "turn_input:plan_task@0" in cc["retained_part_ids"]
        # clearable_tokens is the sum of estimate_tokens(body) for
        # the two clearable parts — non-zero, deterministic.
        assert cc["clearable_tokens"] > 0
        # Observe-only safe defaults preserved.
        assert cc["cleared_tokens"] == 0
        assert cc["artifact_refs"] == []
        assert cc["cache_effect"] == "none"

    def test_writer_correlation_keys_match_sibling_records(self) -> None:
        # The eligibility record must carry the same render
        # correlation keys as the sibling prompt_render and
        # context_growth records stamped in the same call.
        from pipeline.phases.builtin import _session_aware_invoke

        state = self._make_state()
        turn = self._make_turn_mixed()
        _session_aware_invoke(
            self._FakeAgent(), state,
            phase="plan",
            turn=turn,
            cwd="/proj",
        )
        cc = state.phase_log["plan"]["context_clearing"]
        pr = state.phase_log["plan"]["prompt_render"]
        cg = state.phase_log["plan"]["context_growth"]
        for key in ("prefix_hash", "payload_hash", "wire_chars", "render_mode"):
            assert cc[key] == pr[key] == cg[key], (
                f"key {key!r} drifted across the three sibling records"
            )


# ── M14.2 classifier integration ─────────────────────────────────────────────


class TestClassifierIntegration:
    """M14.3 reads M14.2's :class:`OutputClass`. The
    eligibility partition must follow:

    - clearable = RE_FETCHABLE ∪ PERSISTED_ARTIFACT
    - retained  = DECISION_BEARING ∪ EPHEMERAL

    Per ADR 0029 §"Tool-result clearing": ``EPHEMERAL`` is "not
    cleared unless first summarized or persisted" — until a
    summarizer ships in M14.4 / M14.5, EPHEMERAL stays retained
    by default.
    """

    def test_clearable_classes_pinned(self) -> None:
        from pipeline.observability.context_clearing import (
            normalize_context_clearing,
        )
        payload = _payload(
            class_counts={
                "re_fetchable":       5,
                "persisted_artifact": 3,
                "ephemeral":          2,
                "decision_bearing":   4,
            },
        )
        result = normalize_context_clearing(payload)
        # The normalizer does not recompute classifications — it
        # passes them through. The writer-side test
        # (TestWriterStamping) pins the actual classification
        # against the M14.2 rules.
        assert result["class_counts"]["re_fetchable"] == 5
        assert result["class_counts"]["persisted_artifact"] == 3
        assert result["class_counts"]["ephemeral"] == 2
        assert result["class_counts"]["decision_bearing"] == 4

    def test_class_counts_only_includes_known_output_classes(self) -> None:
        # A payload with a stray key in class_counts is normalized
        # to the canonical OutputClass key set — extras dropped,
        # missing zeroed. Defends against writer drift.
        payload = _payload(
            class_counts={
                "re_fetchable": 2,
                "ephemeral":    1,
                "unknown_class": 99,  # ← drift
            },
        )
        result = normalize_context_clearing(payload)
        assert set(result["class_counts"].keys()) == {
            c.value for c in OutputClass
        }
        assert "unknown_class" not in result["class_counts"]


# ── PhaseContextClearing dataclass surface ───────────────────────────────────


class TestDataclassSurface:
    def test_dataclass_is_frozen(self) -> None:
        trace = PhaseContextClearing(
            phase="plan",
            trace_surface="plan",
            attempt=1,
            round=None,
            source_path="phases.plan[0].context_clearing",
            payload=normalize_context_clearing(_payload()),
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            trace.phase = "implement"  # type: ignore[misc]
