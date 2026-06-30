"""M14.4 — Runtime context-pressure telemetry (observe-only).

Tests for ``pipeline.observability.context_pressure``. The M14.4
contract pins:

- the 12-field durable shape ``DURABLE_FIELDS``;
- the :class:`ContextSource` hierarchy ordering and the canonical
  source-label set (with unknown-input → ``"unknown"`` fallback);
- :func:`resolve_context_pressure` source-priority walking against
  a fake-agent stub (today's working branches:
  ``orcho_estimated`` → ``config_static`` → ``unknown``;
  ``runtime_reported`` branch wakes up automatically when an
  agent exposes the live attributes, no contract change);
- writer-side stamping in ``_session_aware_invoke`` so the
  ``context_pressure`` phase_log entry carries the resolver's
  source label and the matching render-correlation keys
  (``prefix_hash`` / ``payload_hash`` / ``wire_chars`` sibling of
  ``prompt_render`` / ``context_growth`` / ``context_clearing``);
- the per-surface extractor taxonomy (plan / replan / validate_plan
  / implement / rounds review/repair split — mirrors M14.3);
- the observe-only invariant: M14.4 never auto-compacts. The
  surface is evidence; the protected ``coding_agent_compaction``
  contract is the response shape.
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from pipeline.observability.context_pressure import (
    DURABLE_FIELDS,
    SOURCE_PRIORITY,
    ContextSource,
    PhaseContextPressure,
    PressureReading,
    extract_context_pressure_traces,
    format_context_summary,
    normalize_context_pressure,
    resolve_context_pressure,
)

# ── Synthetic payloads ───────────────────────────────────────────────────────

_DEFAULT_SESSION_KEY = object()


def _payload(
    *,
    phase: str = "plan",
    context_source: str = "orcho_estimated",
    trigger_source: str | None = None,
    context_window_tokens: int | None = 200_000,
    context_used_tokens: int | None = 12_000,
    context_remaining_tokens: int | None = 188_000,
    context_fill_ratio: float | None = 0.06,
    session_split: str = "per_phase",
    session_key: dict | None | object = _DEFAULT_SESSION_KEY,
    provider_session_id: str | None = "sess-1",
    runtime: str = "agents.runtimes.claude.ClaudeAgent",
    model: str = "claude-opus-4-7",
    prefix_hash: str = "abc",
    payload_hash: str = "def",
    wire_chars: int = 4096,
) -> dict:
    if session_key is _DEFAULT_SESSION_KEY:
        session_key = {
            "run_id": "run-1",
            "runtime": runtime,
            "model_key": model,
            "scope": f"{session_split}:plan",
        }
    return {
        "phase":                    phase,
        "round":                    None,
        "surface_id":               None,
        "context_source":           context_source,
        "context_window_tokens":    context_window_tokens,
        "context_used_tokens":      context_used_tokens,
        "context_remaining_tokens": context_remaining_tokens,
        "context_fill_ratio":       context_fill_ratio,
        "trigger_source":           trigger_source or context_source,
        "session_split":            session_split,
        "session_key":              session_key,
        "provider_session_id":      provider_session_id,
        "runtime":                  runtime,
        "model":                    model,
        "prefix_hash":              prefix_hash,
        "payload_hash":             payload_hash,
        "wire_chars":               wire_chars,
    }


def _covered_session() -> dict:
    """Fixture touching every covered surface so the shape guard
    runs against the full M14.4 surface set including round-side
    review/repair split."""
    return {
        "phases": {
            "plan": [
                {"attempt": 1, "context_pressure": _payload(phase="plan")},
                {
                    "attempt":          2,
                    "replan_critique":  "needs more tests",
                    "context_pressure": _payload(phase="plan"),
                },
            ],
            "validate_plan": [
                {
                    "attempt":          1,
                    "context_pressure": _payload(phase="validate_plan"),
                },
            ],
            "implement": {
                "context_pressure": _payload(phase="implement"),
            },
            "rounds": [
                {
                    "round": 1,
                    "context_pressure_review": _payload(
                        phase="review_changes",
                    ),
                    "context_pressure_repair": _payload(
                        phase="repair_changes",
                    ),
                },
            ],
        },
    }


# ── Durable shape ────────────────────────────────────────────────────────────


class TestDurableShape:
    def test_durable_fields_count_is_seventeen(self) -> None:
        # M14.4 ships 17 durable fields: 3 attribution + 1 source
        # label + 4 numeric readings + 1 trigger-source label + 5
        # session identity slots + 3 render correlation slots. Drift here
        # means a downstream
        # consumer (future evidence projection, dashboard, lab
        # probe) needs review.
        assert len(DURABLE_FIELDS) == 17

    def test_normalize_returns_exactly_the_durable_key_set(self) -> None:
        result = normalize_context_pressure(_payload())
        assert set(result.keys()) == set(DURABLE_FIELDS)

    def test_all_durable_fields_present_on_every_extracted_trace(self) -> None:
        traces = extract_context_pressure_traces(_covered_session())
        assert traces, "covered fixture must produce non-empty traces"
        for trace in traces:
            missing = set(DURABLE_FIELDS) - set(trace.payload.keys())
            extra = set(trace.payload.keys()) - set(DURABLE_FIELDS)
            assert not missing and not extra, (
                f"trace {trace.source_path!r} payload shape drift: "
                f"missing={sorted(missing)} extra={sorted(extra)}"
            )

    def test_every_trace_is_json_serializable(self) -> None:
        traces = extract_context_pressure_traces(_covered_session())
        for trace in traces:
            encoded = json.dumps(dataclasses.asdict(trace))
            decoded = json.loads(encoded)
            assert decoded["payload"]["context_source"] in {
                c.value for c in ContextSource
            }


# ── Source hierarchy ─────────────────────────────────────────────────────────


class TestSourceHierarchy:
    """ADR 0029 locks an explicit priority order. Drift in either
    the enum values or the priority tuple changes runtime
    decision-making for every future automatic-compaction trigger."""

    def test_canonical_source_labels(self) -> None:
        assert {c.value for c in ContextSource} == {
            "runtime_reported",
            "provider_usage",
            "orcho_estimated",
            "config_static",
            "unknown",
        }

    def test_priority_order_highest_to_lowest(self) -> None:
        # Position is contract: a future writer that walks
        # SOURCE_PRIORITY in tuple order must hit the highest
        # available authority first.
        assert SOURCE_PRIORITY == (
            ContextSource.RUNTIME_REPORTED,
            ContextSource.PROVIDER_USAGE,
            ContextSource.ORCHO_ESTIMATED,
            ContextSource.CONFIG_STATIC,
            ContextSource.UNKNOWN,
        )

    def test_unknown_source_label_normalizes_to_unknown(self) -> None:
        payload = _payload(context_source="bogus")
        result = normalize_context_pressure(payload)
        assert result["context_source"] == "unknown"
        # trigger_source defaulted from context_source; with the
        # source value rewritten the trigger label should also be
        # canonical (or fall back to "unknown").
        assert result["trigger_source"] in {c.value for c in ContextSource}

    def test_trigger_source_defaults_to_context_source(self) -> None:
        payload = _payload(context_source="orcho_estimated")
        payload.pop("trigger_source", None)
        result = normalize_context_pressure(payload)
        assert result["trigger_source"] == "orcho_estimated"

    def test_trigger_source_unknown_value_falls_back_to_unknown(self) -> None:
        payload = _payload(
            context_source="orcho_estimated",
            trigger_source="not-a-source",
        )
        result = normalize_context_pressure(payload)
        assert result["trigger_source"] == "unknown"

    def test_numeric_fields_pass_through_or_are_none(self) -> None:
        payload = _payload(
            context_window_tokens=None,
            context_used_tokens=None,
            context_remaining_tokens=None,
            context_fill_ratio=None,
        )
        result = normalize_context_pressure(payload)
        assert result["context_window_tokens"] is None
        assert result["context_used_tokens"] is None
        assert result["context_remaining_tokens"] is None
        assert result["context_fill_ratio"] is None


# ── Resolver branches ────────────────────────────────────────────────────────


class TestResolveContextPressure:
    """The resolver walks SOURCE_PRIORITY against agent attributes.
    M14.4 ships ORCHO_ESTIMATED / CONFIG_STATIC / UNKNOWN
    branches as today's working code; RUNTIME_REPORTED activates
    automatically the moment a runtime exposes the live attribute
    set; PROVIDER_USAGE is reserved for a future runtime."""

    class _Agent:
        """Fake-agent stub. Resolver reads only attributes."""

        def __init__(self, **attrs) -> None:
            for k, v in attrs.items():
                setattr(self, k, v)

    def test_runtime_reported_branch_when_window_and_used_present(self) -> None:
        agent = self._Agent(
            last_context_window_tokens=200_000,
            last_context_used_tokens=50_000,
            last_context_remaining_tokens=150_000,
            last_estimated_tokens_in=12_345,  # lower-authority noise
        )
        reading = resolve_context_pressure(agent)
        assert reading.context_source is ContextSource.RUNTIME_REPORTED
        assert reading.context_window_tokens == 200_000
        assert reading.context_used_tokens == 50_000
        assert reading.context_remaining_tokens == 150_000
        assert reading.context_fill_ratio == pytest.approx(0.25)
        assert reading.trigger_source is ContextSource.RUNTIME_REPORTED

    def test_runtime_reported_derives_remaining_when_missing(self) -> None:
        agent = self._Agent(
            last_context_window_tokens=100_000,
            last_context_used_tokens=20_000,
        )
        reading = resolve_context_pressure(agent)
        assert reading.context_source is ContextSource.RUNTIME_REPORTED
        assert reading.context_remaining_tokens == 80_000

    def test_orcho_estimated_branch_with_config_window(self) -> None:
        agent = self._Agent(last_estimated_tokens_in=2_500)
        reading = resolve_context_pressure(
            agent, config_window_tokens=100_000,
        )
        assert reading.context_source is ContextSource.ORCHO_ESTIMATED
        assert reading.context_window_tokens == 100_000
        assert reading.context_used_tokens == 2_500
        assert reading.context_remaining_tokens == 97_500
        assert reading.context_fill_ratio == pytest.approx(0.025)

    def test_orcho_estimated_branch_without_config_window(self) -> None:
        agent = self._Agent(last_estimated_tokens_in=2_500)
        reading = resolve_context_pressure(agent)
        assert reading.context_source is ContextSource.ORCHO_ESTIMATED
        assert reading.context_window_tokens is None
        assert reading.context_used_tokens == 2_500
        assert reading.context_remaining_tokens is None
        assert reading.context_fill_ratio is None

    def test_config_static_branch_when_only_window_known(self) -> None:
        agent = self._Agent()  # nothing useful
        reading = resolve_context_pressure(
            agent, config_window_tokens=200_000,
        )
        assert reading.context_source is ContextSource.CONFIG_STATIC
        assert reading.context_window_tokens == 200_000
        assert reading.context_used_tokens is None
        assert reading.context_remaining_tokens is None
        assert reading.context_fill_ratio is None

    def test_unknown_when_no_signal_available(self) -> None:
        agent = self._Agent()
        reading = resolve_context_pressure(agent)
        assert reading.context_source is ContextSource.UNKNOWN
        assert reading.trigger_source is ContextSource.UNKNOWN
        assert reading.context_window_tokens is None
        assert reading.context_used_tokens is None
        assert reading.context_remaining_tokens is None
        assert reading.context_fill_ratio is None

    def test_codex_runtime_reported_when_brain_telemetry_present(self) -> None:
        """CodexAgent populates the same RUNTIME_REPORTED attrs as ClaudeAgent.

        Pins the contract that rollout-sourced Codex telemetry flows through
        Branch 1 of the resolver without resolver-side changes.
        """
        agent = self._Agent(
            last_context_window_tokens=258_400,
            last_context_used_tokens=42_000,
            last_context_remaining_tokens=216_400,
        )
        reading = resolve_context_pressure(agent)
        assert reading.context_source is ContextSource.RUNTIME_REPORTED
        assert reading.context_window_tokens == 258_400
        assert reading.context_used_tokens == 42_000
        assert reading.context_remaining_tokens == 216_400
        assert reading.context_fill_ratio == pytest.approx(42_000 / 258_400)

    def test_codex_orcho_estimated_window_stays_none_without_config(
        self,
    ) -> None:
        """No hardcoded fallback window when rollout telemetry is missing.

        Even with a positive usage estimate, the resolver must not invent
        a window value — the Live card will degrade to estimate-only
        rather than show a fake "X / Y" pair.
        """
        agent = self._Agent(
            last_context_window_tokens=None,
            last_context_used_tokens=None,
            last_context_remaining_tokens=None,
            last_estimated_tokens_in=1_234,
        )
        reading = resolve_context_pressure(agent, config_window_tokens=None)
        assert reading.context_source is ContextSource.ORCHO_ESTIMATED
        assert reading.context_used_tokens == 1_234
        assert reading.context_window_tokens is None
        assert reading.context_fill_ratio is None

    def test_codex_unknown_when_no_signals_at_all(self) -> None:
        agent = self._Agent(
            last_context_window_tokens=None,
            last_context_used_tokens=None,
            last_estimated_tokens_in=None,
        )
        reading = resolve_context_pressure(agent, config_window_tokens=None)
        assert reading.context_source is ContextSource.UNKNOWN
        assert reading.context_window_tokens is None
        assert reading.context_used_tokens is None
        assert reading.context_remaining_tokens is None
        assert reading.context_fill_ratio is None

    def test_pressure_reading_is_frozen(self) -> None:
        reading = PressureReading(
            context_source=ContextSource.UNKNOWN,
            context_window_tokens=None,
            context_used_tokens=None,
            context_remaining_tokens=None,
            context_fill_ratio=None,
            trigger_source=ContextSource.UNKNOWN,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            reading.context_source = ContextSource.ORCHO_ESTIMATED  # type: ignore[misc]


# ── Extractor taxonomy ───────────────────────────────────────────────────────


class TestExtractorTaxonomy:
    def test_plan_attempt_one_without_critique_is_plan_surface(self) -> None:
        session = {
            "phases": {
                "plan": [{"attempt": 1, "context_pressure": _payload()}],
            },
        }
        traces = extract_context_pressure_traces(session)
        assert [(t.phase, t.trace_surface) for t in traces] == [
            ("plan", "plan"),
        ]

    def test_plan_attempt_two_with_critique_is_replan_surface(self) -> None:
        session = {
            "phases": {
                "plan": [
                    {"attempt": 1, "context_pressure": _payload()},
                    {
                        "attempt":          2,
                        "replan_critique":  "redo",
                        "context_pressure": _payload(),
                    },
                ],
            },
        }
        surfaces = [
            t.trace_surface
            for t in extract_context_pressure_traces(session)
        ]
        assert surfaces == ["plan", "replan"]

    def test_implement_is_a_single_dict_not_a_list(self) -> None:
        session = {
            "phases": {
                "implement": {"context_pressure": _payload()},
            },
        }
        traces = extract_context_pressure_traces(session)
        assert [(t.phase, t.attempt, t.round) for t in traces] == [
            ("implement", None, None),
        ]

    def test_rounds_review_repair_split_emits_two_records(self) -> None:
        session = {
            "phases": {
                "rounds": [
                    {
                        "round": 1,
                        "context_pressure_review": _payload(
                            phase="review_changes",
                        ),
                        "context_pressure_repair": _payload(
                            phase="repair_changes",
                        ),
                    },
                ],
            },
        }
        traces = extract_context_pressure_traces(session)
        kinds = [(t.phase, t.round) for t in traces]
        assert kinds == [
            ("review_changes", 1),
            ("repair_changes", 1),
        ]

    def test_missing_context_pressure_keys_are_skipped_silently(self) -> None:
        session = {
            "phases": {
                "plan":      [{"attempt": 1}],
                "implement": {},
                "rounds":    [{"round": 1}],
            },
        }
        assert extract_context_pressure_traces(session) == []

    def test_non_dict_session_returns_empty(self) -> None:
        assert extract_context_pressure_traces("not a dict") == []  # type: ignore[arg-type]
        assert extract_context_pressure_traces(None) == []  # type: ignore[arg-type]
        assert extract_context_pressure_traces([]) == []  # type: ignore[arg-type]


# ── Writer-side stamping (fake-provider unit test) ───────────────────────────


class TestWriterStamping:
    """The writer in ``_session_aware_invoke`` resolves a
    :class:`PressureReading` for the agent and stamps the
    ``context_pressure`` phase_log entry with the resolved source
    label + numeric values + the render-correlation keys.
    Exercised against a fake-provider stub so no real model call is
    needed."""

    def _make_state(self):
        from pipeline.plugins import PluginConfig
        from pipeline.runtime import PipelineState
        return PipelineState(
            task="Probe pressure stamp",
            project_dir="/proj",
            plugin=PluginConfig(),
            extras={"run_id": "run-cp-1"},
        )

    def _make_turn(self):
        from pipeline.prompts.turn import PromptTurnEditor
        from pipeline.prompts.types import (
            PromptCacheScope,
            PromptPart,
            PromptStability,
        )
        role = PromptPart(
            kind="role",
            name="systems_architect",
            source="core",
            body="You are the systems architect.",
            stability=PromptStability.STATIC,
            cache_scope=PromptCacheScope.GLOBAL,
        )
        return PromptTurnEditor().append(role).build()

    class _FakeAgent:
        model = "fake-model"
        session_id = None
        last_estimated_tokens_in = 200
        last_estimated_tokens_out = 50

        def invoke(self, prompt, cwd, *, continue_session=False,
                   attachments=(), mutates_artifacts=False):
            return "ok"

    def test_writer_stamps_context_pressure_with_orcho_estimated(
        self,
    ) -> None:
        from pipeline.phases.builtin import _session_aware_invoke

        state = self._make_state()
        turn = self._make_turn()

        _session_aware_invoke(
            self._FakeAgent(), state,
            phase="plan",
            turn=turn,
            cwd="/proj",
        )

        cp = state.phase_log["plan"]["context_pressure"]
        # Source label honours the resolver: the fake agent exposes
        # last_estimated_tokens_in but none of the runtime-reported
        # attributes, so the writer must label this orcho_estimated.
        assert cp["context_source"] == "orcho_estimated"
        assert cp["trigger_source"] == "orcho_estimated"
        # Numeric reading mirrors the fake agent's estimate.
        assert cp["context_used_tokens"] == 200
        # No config_window_tokens supplied today → ratio/remaining
        # stay at None.
        assert cp["context_window_tokens"] is None
        assert cp["context_remaining_tokens"] is None
        assert cp["context_fill_ratio"] is None
        # Attribution.
        assert cp["phase"] == "plan"
        assert cp["round"] is None
        assert cp["surface_id"] is None

    def test_writer_correlation_keys_match_sibling_records(self) -> None:
        from pipeline.phases.builtin import _session_aware_invoke

        state = self._make_state()
        turn = self._make_turn()
        _session_aware_invoke(
            self._FakeAgent(), state,
            phase="plan",
            turn=turn,
            cwd="/proj",
        )
        cp = state.phase_log["plan"]["context_pressure"]
        pr = state.phase_log["plan"]["prompt_render"]
        cg = state.phase_log["plan"]["context_growth"]
        cc = state.phase_log["plan"]["context_clearing"]
        for key in ("prefix_hash", "payload_hash", "wire_chars"):
            assert cp[key] == pr[key] == cg[key] == cc[key], (
                f"key {key!r} drifted across the four sibling records"
            )

    def test_writer_stamp_survives_handler_overwrite_via_carry_helper(
        self,
    ) -> None:
        # ``_carry_trace_metadata`` is the seam that keeps trace
        # metadata alive when a handler rebuilds phase_log[phase]
        # from scratch after parsing. The helper must enumerate
        # ``context_pressure`` alongside the M14.1 / M14.3 keys.
        from pipeline.phases.builtin import _carry_trace_metadata

        state = self._make_state()
        # Pretend the writer ran first and stamped all sibling
        # records.
        state.phase_log["plan"] = {
            "prompt_render":    {"x": 1},
            "context_growth":   {"y": 2},
            "context_clearing": {"z": 3},
            "context_pressure": {"w": 4},
            "output":           "...",
        }
        carried = _carry_trace_metadata(state, "plan")
        assert carried == {
            "prompt_render":    {"x": 1},
            "context_growth":   {"y": 2},
            "context_clearing": {"z": 3},
            "context_pressure": {"w": 4},
        }


# ── PhaseContextPressure dataclass surface ───────────────────────────────────


class TestFormatContextSummary:
    """M14.4.2 — CLI-facing one-liner. Picks the peak fill ratio
    across every covered phase and renders it Claude-CLI-style
    (``193.6k / 1.0M (19%)``). Suppressed when no informative
    record exists so mock runs / unknown-source-only sessions
    don't print a noisy placeholder."""

    def test_runtime_reported_peak_renders_full_line(self) -> None:
        session = {
            "phases": {
                "plan": [{
                    "attempt": 1,
                    "context_pressure": _payload(
                        phase="plan",
                        context_source="runtime_reported",
                        context_window_tokens=1_000_000,
                        context_used_tokens=193_600,
                        context_remaining_tokens=806_400,
                        context_fill_ratio=0.1936,
                    ),
                }],
            },
        }
        line = format_context_summary(session)
        assert line == (
            "Context window: 193.6k / 1.0M (19%) [runtime_reported plan]"
        )

    def test_picks_highest_fill_ratio_across_phases(self) -> None:
        # Plan at 5%, implement at 19% — implement wins.
        session = {
            "phases": {
                "plan": [{
                    "attempt": 1,
                    "context_pressure": _payload(
                        phase="plan",
                        context_window_tokens=1_000_000,
                        context_used_tokens=50_000,
                        context_fill_ratio=0.05,
                    ),
                }],
                "implement": {
                    "context_pressure": _payload(
                        phase="implement",
                        context_window_tokens=1_000_000,
                        context_used_tokens=193_600,
                        context_fill_ratio=0.1936,
                    ),
                },
            },
        }
        line = format_context_summary(session)
        assert line is not None
        assert "[orcho_estimated implement]" in line
        assert "193.6k" in line

    def test_round_side_split_participates_in_peak_pick(self) -> None:
        session = {
            "phases": {
                "rounds": [{
                    "round": 1,
                    "context_pressure_review": _payload(
                        phase="review_changes",
                        context_window_tokens=200_000,
                        context_used_tokens=20_000,
                        context_fill_ratio=0.10,
                    ),
                    "context_pressure_repair": _payload(
                        phase="repair_changes",
                        context_window_tokens=200_000,
                        context_used_tokens=80_000,
                        context_fill_ratio=0.40,
                    ),
                }],
            },
        }
        line = format_context_summary(session)
        assert line is not None
        assert "[orcho_estimated repair_changes]" in line
        assert "(40%)" in line

    def test_orcho_estimated_without_window_shows_used_only(self) -> None:
        # No config_window_tokens was plumbed in, so the resolver
        # stamps used but leaves window/ratio null. The line must
        # still surface the signal — "X used" — rather than
        # silently dropping the record.
        session = {
            "phases": {
                "implement": {
                    "context_pressure": _payload(
                        phase="implement",
                        context_source="orcho_estimated",
                        context_window_tokens=None,
                        context_used_tokens=12_345,
                        context_remaining_tokens=None,
                        context_fill_ratio=None,
                    ),
                },
            },
        }
        line = format_context_summary(session)
        assert line == (
            "Context window: 12.3k used [orcho_estimated implement]"
        )

    def test_unknown_source_suppresses_line(self) -> None:
        session = {
            "phases": {
                "plan": [{
                    "attempt": 1,
                    "context_pressure": _payload(
                        phase="plan",
                        context_source="unknown",
                        context_window_tokens=None,
                        context_used_tokens=None,
                        context_remaining_tokens=None,
                        context_fill_ratio=None,
                    ),
                }],
            },
        }
        assert format_context_summary(session) is None

    def test_no_pressure_records_returns_none(self) -> None:
        assert format_context_summary({}) is None
        assert format_context_summary({"phases": {}}) is None
        assert format_context_summary({"phases": {"plan": []}}) is None

    def test_runtime_reported_outranks_higher_ratio_orcho_estimated(
        self,
    ) -> None:
        # Tie-break: M14.4.2 picks by fill_ratio *value*, not by
        # source priority. This is intentional — the user wants to
        # know the worst observed fullness, not which source claims
        # to be best. The source label in the line stays honest.
        session = {
            "phases": {
                "plan": [{
                    "attempt": 1,
                    "context_pressure": _payload(
                        phase="plan",
                        context_source="runtime_reported",
                        context_window_tokens=1_000_000,
                        context_used_tokens=100_000,
                        context_fill_ratio=0.10,
                    ),
                }],
                "implement": {
                    "context_pressure": _payload(
                        phase="implement",
                        context_source="orcho_estimated",
                        context_window_tokens=200_000,
                        context_used_tokens=80_000,
                        context_fill_ratio=0.40,
                    ),
                },
            },
        }
        line = format_context_summary(session)
        assert line is not None
        # Implement at 40% wins over plan at 10% even though
        # plan is runtime_reported.
        assert "[orcho_estimated implement]" in line
        assert "(40%)" in line

    def test_multiple_physical_sessions_render_breakdown(self) -> None:
        session = {
            "phases": {
                "plan": [{
                    "attempt": 1,
                    "context_pressure": _payload(
                        phase="plan",
                        context_source="runtime_reported",
                        context_window_tokens=1_000_000,
                        context_used_tokens=44_600,
                        context_fill_ratio=0.0446,
                        session_split="common",
                        session_key={
                            "run_id": "run-1",
                            "runtime": "agents.runtimes.claude.ClaudeAgent",
                            "model_key": "claude-opus-4-7",
                            "scope": "common",
                        },
                        provider_session_id="claude-session",
                    ),
                }],
                "validate_plan": [{
                    "attempt": 1,
                    "context_pressure": _payload(
                        phase="validate_plan",
                        context_source="runtime_reported",
                        context_window_tokens=1_000_000,
                        context_used_tokens=49_200,
                        context_fill_ratio=0.0492,
                        session_split="stateless",
                        session_key=None,
                        provider_session_id="codex-session",
                        runtime="agents.runtimes.codex.CodexAgent",
                        model="gpt-5.5",
                    ),
                }],
                "implement": {
                    "context_pressure": _payload(
                        phase="implement",
                        context_source="runtime_reported",
                        context_window_tokens=1_000_000,
                        context_used_tokens=37_400,
                        context_fill_ratio=0.0374,
                        session_split="common",
                        session_key={
                            "run_id": "run-1",
                            "runtime": "agents.runtimes.claude.ClaudeAgent",
                            "model_key": "claude-opus-4-7",
                            "scope": "common",
                        },
                        provider_session_id="claude-session",
                    ),
                },
            },
        }
        line = format_context_summary(session)
        assert line is not None
        assert line.startswith("Context windows:")
        assert "Peak: 49.2k / 1.0M (5%) [runtime_reported validate_plan]" in line
        assert "Sessions: 2" in line
        assert (
            "common ClaudeAgent/claude-opus-4-7: current 37.4k / 1.0M "
            "(4%), peak 44.6k / 1.0M (4%), phases plan→implement"
        ) in line
        assert "stateless CodexAgent/gpt-5.5: current 49.2k / 1.0M" in line


class TestDataclassSurface:
    def test_dataclass_is_frozen(self) -> None:
        trace = PhaseContextPressure(
            phase="plan",
            trace_surface="plan",
            attempt=1,
            round=None,
            source_path="phases.plan[0].context_pressure",
            payload=normalize_context_pressure(_payload()),
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            trace.phase = "implement"  # type: ignore[misc]
