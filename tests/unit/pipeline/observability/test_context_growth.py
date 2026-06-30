"""M14.1 — Context-growth durable trace shape + writer-stamping invariants.

ADR 0029 introduces a context-lifecycle layer that records how the
context window grows, gets cleared, gets compacted, and gets
persisted. M14.1 lands the **observe-only** thin slice:
``_session_aware_invoke`` stamps a ``context_growth`` payload per
phase invocation, ``session_adapters._copy_context_growth`` promotes
it into the per-phase session entry, and
``pipeline.observability.context_growth`` provides the read-only
extractor + normalizer mirror of M12's ``prompt_render`` surface.

This test file pins:

- the 15-field durable shape ``DURABLE_FIELDS``;
- the M14.1 fallback semantics (``kind`` and ``trigger`` default
  to ``"phase_invocation"``; tool / cleared / summary counters
  default to ``0``; ``artifact_refs`` defaults to ``[]``);
- the JSON-serializability of every normalized payload;
- the per-surface attribution taxonomy (``plan`` / ``replan`` /
  ``validate_plan`` / ``implement``);
- correlation with the sibling ``prompt_render`` record via
  ``render_mode`` / ``prefix_hash`` / ``payload_hash`` / ``wire_chars``;
- writer-side stamping in ``_session_aware_invoke`` against a
  fake-provider stub, so no real model call is needed.
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from pipeline.observability.context_growth import (
    DURABLE_FIELDS,
    PhaseContextGrowth,
    extract_context_growth_traces,
    normalize_context_growth,
)

# ── Synthetic payloads ───────────────────────────────────────────────────────


def _payload(
    *,
    phase: str = "plan",
    render_mode: str = "full",
    prefix_hash: str = "abc",
    payload_hash: str = "def",
    wire_chars: int = 1024,
    input_tokens_estimate: int | None = 256,
    output_tokens_estimate: int | None = 64,
) -> dict:
    return {
        "kind": "phase_invocation",
        "trigger": "phase_invocation",
        "phase": phase,
        "round": None,
        "surface_id": None,
        "render_mode": render_mode,
        "prefix_hash": prefix_hash,
        "payload_hash": payload_hash,
        "wire_chars": wire_chars,
        "input_tokens_estimate": input_tokens_estimate,
        "output_tokens_estimate": output_tokens_estimate,
        "tool_use_count": 0,
        "cleared_tokens": 0,
        "summary_tokens": 0,
        "artifact_refs": [],
    }


def _covered_session() -> dict:
    """Fixture that touches every covered surface (plan + replan +
    validate_plan + implement) so the shape guard runs against the
    full M14.1 surface set."""
    return {
        "phases": {
            "plan": [
                {"attempt": 1, "context_growth": _payload(phase="plan")},
                {
                    "attempt": 2,
                    "replan_critique": "needs more tests",
                    "context_growth": _payload(phase="plan"),
                },
            ],
            "validate_plan": [
                {
                    "attempt": 1,
                    "context_growth": _payload(phase="validate_plan"),
                },
            ],
            "implement": {"context_growth": _payload(phase="implement")},
        },
    }


# ── Durable shape ────────────────────────────────────────────────────────────


class TestDurableShape:
    def test_durable_fields_count_is_fifteen(self) -> None:
        # M14.1 ships with 15 durable fields: 5 attribution +
        # 4 render correlation + 2 token observables + 4 reserved
        # lifecycle placeholders. Drift here means a downstream
        # consumer (future evidence projection, dashboard, lab
        # probe) needs review.
        assert len(DURABLE_FIELDS) == 15

    def test_normalize_returns_exactly_the_durable_key_set(self) -> None:
        result = normalize_context_growth(_payload())
        assert set(result.keys()) == set(DURABLE_FIELDS)

    def test_all_durable_fields_present_on_every_extracted_trace(self) -> None:
        traces = extract_context_growth_traces(_covered_session())
        assert traces, "covered fixture must produce non-empty traces"
        for trace in traces:
            missing = set(DURABLE_FIELDS) - set(trace.payload.keys())
            extra = set(trace.payload.keys()) - set(DURABLE_FIELDS)
            assert not missing and not extra, (
                f"trace {trace.source_path!r} payload shape drift: "
                f"missing={sorted(missing)} extra={sorted(extra)}"
            )

    def test_every_trace_is_json_serializable(self) -> None:
        traces = extract_context_growth_traces(_covered_session())
        for trace in traces:
            encoded = json.dumps(dataclasses.asdict(trace))
            decoded = json.loads(encoded)
            # Spot-check round-trip integrity on a load-bearing field.
            assert decoded["payload"]["kind"] == "phase_invocation"


# ── M14.1 fallback semantics ─────────────────────────────────────────────────


class TestM14p1ObserveOnlyFallbacks:
    """The reserved lifecycle placeholders stay at safe defaults
    until M14.3+ enable the matching primitive. The fallbacks must
    not drift — a downstream consumer that sees a non-zero
    ``cleared_tokens`` today would believe Orcho cleared tool
    results, which it does not until M14.3."""

    def test_kind_defaults_to_phase_invocation(self) -> None:
        payload = _payload()
        payload.pop("kind", None)
        result = normalize_context_growth(payload)
        assert result["kind"] == "phase_invocation"

    def test_trigger_defaults_to_phase_invocation(self) -> None:
        payload = _payload()
        payload.pop("trigger", None)
        result = normalize_context_growth(payload)
        assert result["trigger"] == "phase_invocation"

    @pytest.mark.parametrize(
        ("field", "expected_default"),
        [
            ("tool_use_count", 0),
            ("cleared_tokens", 0),
            ("summary_tokens", 0),
        ],
    )
    def test_reserved_counter_defaults_to_zero(
        self, field: str, expected_default: int,
    ) -> None:
        payload = _payload()
        payload.pop(field, None)
        result = normalize_context_growth(payload)
        assert result[field] == expected_default, (
            f"{field} drifted from M14.1 safe default; review whether the "
            "matching M14.3+ primitive actually shipped before changing it"
        )

    def test_artifact_refs_defaults_to_empty_list(self) -> None:
        payload = _payload()
        payload.pop("artifact_refs", None)
        result = normalize_context_growth(payload)
        assert result["artifact_refs"] == []

    def test_artifact_refs_is_copied_not_aliased(self) -> None:
        # Pure-function rule: mutating the returned list must not
        # mutate the source payload.
        src = ["a/path.md"]
        payload = _payload()
        payload["artifact_refs"] = src
        result = normalize_context_growth(payload)
        result["artifact_refs"].append("b/path.md")
        assert src == ["a/path.md"]

    def test_token_estimates_pass_through_or_default_to_none(self) -> None:
        # The runtime exposes ``last_estimated_tokens_in/out`` —
        # the writer passes them through when present, None when
        # the runtime does not instrument them.
        payload = _payload(input_tokens_estimate=None, output_tokens_estimate=None)
        result = normalize_context_growth(payload)
        assert result["input_tokens_estimate"] is None
        assert result["output_tokens_estimate"] is None

    def test_surface_id_defaults_none_until_adr_0027_lands(self) -> None:
        payload = _payload()
        payload.pop("surface_id", None)
        result = normalize_context_growth(payload)
        assert result["surface_id"] is None


# ── Extractor taxonomy ───────────────────────────────────────────────────────


class TestExtractorTaxonomy:
    def test_plan_attempt_one_without_critique_is_plan_surface(self) -> None:
        session = {
            "phases": {
                "plan": [{"attempt": 1, "context_growth": _payload()}],
            },
        }
        traces = extract_context_growth_traces(session)
        assert [(t.phase, t.trace_surface) for t in traces] == [
            ("plan", "plan"),
        ]

    def test_plan_attempt_two_with_critique_is_replan_surface(self) -> None:
        session = {
            "phases": {
                "plan": [
                    {"attempt": 1, "context_growth": _payload()},
                    {
                        "attempt": 2,
                        "replan_critique": "needs more tests",
                        "context_growth": _payload(),
                    },
                ],
            },
        }
        surfaces = [t.trace_surface for t in extract_context_growth_traces(session)]
        assert surfaces == ["plan", "replan"]

    def test_validate_plan_attempts_each_produce_a_trace(self) -> None:
        session = {
            "phases": {
                "validate_plan": [
                    {"attempt": 1, "context_growth": _payload()},
                    {"attempt": 2, "context_growth": _payload()},
                ],
            },
        }
        traces = extract_context_growth_traces(session)
        assert [t.attempt for t in traces] == [1, 2]
        assert all(t.trace_surface == "validate_plan" for t in traces)

    def test_implement_is_a_single_dict_not_a_list(self) -> None:
        session = {
            "phases": {
                "implement": {"context_growth": _payload()},
            },
        }
        traces = extract_context_growth_traces(session)
        assert [(t.phase, t.attempt, t.round) for t in traces] == [
            ("implement", None, None),
        ]

    def test_missing_context_growth_keys_are_skipped_silently(self) -> None:
        # Phases that did not stamp context_growth (e.g. dry-run,
        # MINIMAL ablation paths) must not crash the extractor.
        session = {
            "phases": {
                "plan": [{"attempt": 1}],
                "implement": {},
            },
        }
        assert extract_context_growth_traces(session) == []

    def test_round_surface_review_repair_is_reserved_for_m14p3(self) -> None:
        # M14.1 keeps the round entry shape stable but does not yet
        # split context_growth per side. M14.3 will populate
        # ``context_growth_review`` / ``context_growth_repair``.
        session = {
            "phases": {
                "rounds": [
                    {
                        "round": 1,
                        "context_growth_review": _payload(),
                        "context_growth_repair": _payload(),
                    },
                ],
            },
        }
        # M14.1 extractor ignores rounds-side payloads.
        assert extract_context_growth_traces(session) == []


# ── Render correlation ───────────────────────────────────────────────────────


class TestRenderCorrelation:
    def test_render_mode_passes_through_for_full_and_delta(self) -> None:
        full = normalize_context_growth(_payload(render_mode="full"))
        delta = normalize_context_growth(_payload(render_mode="delta"))
        assert full["render_mode"] == "full"
        assert delta["render_mode"] == "delta"

    def test_prefix_and_payload_hashes_correlate_to_prompt_render(
        self,
    ) -> None:
        # The writer copies the M12 ``prompt_render`` hashes directly so
        # a downstream join (``context_growth.prefix_hash`` ==
        # ``prompt_render.prefix_hash``) recovers the matching
        # render event without re-walking part lists.
        payload = _payload(
            prefix_hash="sha256:" + "0" * 32,
            payload_hash="sha256:" + "f" * 32,
        )
        result = normalize_context_growth(payload)
        assert result["prefix_hash"] == "sha256:" + "0" * 32
        assert result["payload_hash"] == "sha256:" + "f" * 32

    def test_wire_chars_matches_render_record(self) -> None:
        result = normalize_context_growth(_payload(wire_chars=4096))
        assert result["wire_chars"] == 4096


# ── Writer-side stamping (fake-provider unit test) ───────────────────────────


class TestWriterStamping:
    """``_session_aware_invoke`` stamps ``context_growth`` alongside
    ``prompt_render``. This test exercises the writer against a fake
    provider — no real model call — so the M14.1 "fake-provider tests
    can assert context-growth evidence without real model calls"
    acceptance is honored.
    """

    def _make_state(self):
        from pipeline.plugins import PluginConfig
        from pipeline.runtime import PipelineState
        return PipelineState(
            task="Probe context growth",
            project_dir="/proj",
            plugin=PluginConfig(),
            extras={"run_id": "run-cg-1"},
        )

    def _make_turn(self):
        # A minimal STATIC/GLOBAL part is enough — the M2 envelope
        # partitioner treats it as the stable prefix; the writer
        # then records its hashes / wire_chars in the trace.
        from pipeline.prompts.turn import PromptTurnEditor
        from pipeline.prompts.types import (
            PromptCacheScope,
            PromptPart,
            PromptStability,
        )
        part = PromptPart(
            kind="role",
            name="systems_architect",
            source="core",
            body="You are the systems architect.",
            stability=PromptStability.STATIC,
            cache_scope=PromptCacheScope.GLOBAL,
        )
        return PromptTurnEditor().append(part).build()

    class _FakeAgent:
        model = "fake-model"
        session_id = None
        last_estimated_tokens_in = 123
        last_estimated_tokens_out = 45

        def invoke(self, prompt, cwd, *, continue_session=False,
                   attachments=(), mutates_artifacts=False):
            return "ok"

    def test_writer_stamps_context_growth_with_token_estimates(self) -> None:
        from pipeline.phases.builtin import _session_aware_invoke

        state = self._make_state()
        turn = self._make_turn()

        agent = self._FakeAgent()
        _session_aware_invoke(
            agent, state,
            phase="plan",
            turn=turn,
            cwd="/proj",
        )

        cg = state.phase_log["plan"]["context_growth"]
        # Identity slots.
        assert cg["kind"] == "phase_invocation"
        assert cg["trigger"] == "phase_invocation"
        assert cg["phase"] == "plan"
        assert cg["surface_id"] is None
        # Token observables read straight from the fake agent.
        assert cg["input_tokens_estimate"] == 123
        assert cg["output_tokens_estimate"] == 45
        # Lifecycle placeholders stay at M14.1 safe defaults.
        assert cg["tool_use_count"] == 0
        assert cg["cleared_tokens"] == 0
        assert cg["summary_tokens"] == 0
        assert cg["artifact_refs"] == []
        # Correlation back to the sibling prompt_render record.
        pr = state.phase_log["plan"]["prompt_render"]
        assert cg["prefix_hash"] == pr["prefix_hash"]
        assert cg["payload_hash"] == pr["payload_hash"]
        assert cg["wire_chars"] == pr["wire_chars"]
        assert cg["render_mode"] == pr["render_mode"]


# ── PhaseContextGrowth dataclass surface ─────────────────────────────────────


class TestDataclassSurface:
    def test_dataclass_is_frozen(self) -> None:
        trace = PhaseContextGrowth(
            phase="plan",
            trace_surface="plan",
            attempt=1,
            round=None,
            source_path="phases.plan[0].context_growth",
            payload=normalize_context_growth(_payload()),
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            trace.phase = "implement"  # type: ignore[misc]
