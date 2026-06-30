"""M14.4+ — Runtime auto-compaction evidence (observe-only).

Tests for ``pipeline.observability.runtime_compaction``. The contract
this module pins:

- the 13-field durable shape ``DURABLE_FIELDS``;
- forward-compat normaliser (unknown / missing inputs project to
  safe defaults; canonical preserve-slot filter; defensive
  numeric coercion);
- :func:`resolve_runtime_compaction_event` reads
  ``agent.last_runtime_compaction_event`` only, returns ``None``
  when absent or shaped unexpectedly;
- :func:`validate_compaction_recovery` reports missing
  :data:`REQUIRED_COMPACTION_PRESERVE_FIELDS` slots — does not
  raise; satisfaction can come from either the event's
  ``preserved_slots`` list (artifact-side) or a truthy session
  field;
- writer-side stamping in ``_session_aware_invoke`` only fires
  when the fake agent exposes ``last_runtime_compaction_event``;
  correlation keys (prefix_hash / payload_hash / wire_chars) match
  the sibling records stamped in the same call;
- ``_carry_trace_metadata`` enumerates ``runtime_compaction`` so
  the stamp survives handler-driven ``phase_log[phase]``
  overwrites.

Observe-only invariants:

- No auto-compaction is triggered by Orcho.
- No runtime mutation — the resolver reads attributes only.
- No prompt-wire change.
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from pipeline.observability.runtime_compaction import (
    DURABLE_FIELDS,
    PhaseRuntimeCompaction,
    RecoveryValidationResult,
    RuntimeCompactionEvent,
    extract_runtime_compaction_traces,
    latest_runtime_compaction_event,
    normalize_runtime_compaction_event,
    resolve_runtime_compaction_event,
    validate_compaction_recovery,
)
from pipeline.prompts.contract_templates import (
    REQUIRED_COMPACTION_PRESERVE_FIELDS,
)

# ── Synthetic payloads ───────────────────────────────────────────────────────


def _event(
    *,
    kind: str = "runtime_auto_compacted",
    trigger: str = "event_hook",
    phase: str = "plan",
    round_n: int | None = None,
    surface_id: str | None = None,
    pre_used_tokens: int | None = 150_000,
    post_used_tokens: int | None = 40_000,
    summary_tokens: int | None = 4_096,
    prefix_hash: str = "abc",
    payload_hash: str = "def",
    wire_chars: int = 8192,
    preserved_slots: list[str] | None = None,
    artifact_refs: list | None = None,
) -> dict:
    return {
        "kind":             kind,
        "trigger":          trigger,
        "phase":            phase,
        "round":            round_n,
        "surface_id":       surface_id,
        "pre_used_tokens":  pre_used_tokens,
        "post_used_tokens": post_used_tokens,
        "summary_tokens":   summary_tokens,
        "prefix_hash":      prefix_hash,
        "payload_hash":     payload_hash,
        "wire_chars":       wire_chars,
        "preserved_slots":  preserved_slots or [],
        "artifact_refs":    artifact_refs or [],
    }


def _covered_session() -> dict:
    """Fixture touching every covered surface (plan / replan /
    validate_plan / implement / rounds review+repair)."""
    return {
        "phases": {
            "plan": [
                {"attempt": 1, "runtime_compaction": _event(phase="plan")},
                {
                    "attempt": 2,
                    "replan_critique": "needs more tests",
                    "runtime_compaction": _event(phase="plan"),
                },
            ],
            "validate_plan": [
                {
                    "attempt": 1,
                    "runtime_compaction": _event(phase="validate_plan"),
                },
            ],
            "implement": {
                "runtime_compaction": _event(phase="implement"),
            },
            "rounds": [
                {
                    "round": 1,
                    "runtime_compaction_review": _event(
                        phase="review_changes",
                    ),
                    "runtime_compaction_repair": _event(
                        phase="repair_changes",
                    ),
                },
            ],
        },
    }


# ── Durable shape ────────────────────────────────────────────────────────────


class TestDurableShape:
    def test_durable_fields_count_is_thirteen(self) -> None:
        # 13 fields: 2 identity + 3 attribution + 3 numeric +
        # 3 correlation + 2 recovery. Drift here means a downstream
        # consumer needs review.
        assert len(DURABLE_FIELDS) == 13

    def test_normalize_returns_exactly_the_durable_key_set(self) -> None:
        result = normalize_runtime_compaction_event(_event())
        assert set(result.keys()) == set(DURABLE_FIELDS)

    def test_all_durable_fields_present_on_every_extracted_trace(self) -> None:
        traces = extract_runtime_compaction_traces(_covered_session())
        assert traces, "covered fixture must produce non-empty traces"
        for trace in traces:
            missing = set(DURABLE_FIELDS) - set(trace.payload.keys())
            extra = set(trace.payload.keys()) - set(DURABLE_FIELDS)
            assert not missing and not extra, (
                f"trace {trace.source_path!r} payload shape drift: "
                f"missing={sorted(missing)} extra={sorted(extra)}"
            )

    def test_every_trace_is_json_serializable(self) -> None:
        traces = extract_runtime_compaction_traces(_covered_session())
        for trace in traces:
            encoded = json.dumps(dataclasses.asdict(trace))
            decoded = json.loads(encoded)
            assert decoded["payload"]["kind"] == "runtime_auto_compacted"


# ── Normalizer ───────────────────────────────────────────────────────────────


class TestNormalizer:
    def test_empty_payload_projects_to_defaults(self) -> None:
        result = normalize_runtime_compaction_event({})
        assert result["kind"] == "runtime_auto_compacted"
        assert result["trigger"] == "unknown"
        assert result["preserved_slots"] == []
        assert result["artifact_refs"] == []
        assert result["pre_used_tokens"] is None
        assert result["post_used_tokens"] is None
        assert result["summary_tokens"] is None
        # Attribution + correlation slots default to None.
        for k in (
            "phase", "round", "surface_id",
            "prefix_hash", "payload_hash", "wire_chars",
        ):
            assert result[k] is None

    def test_unknown_trigger_falls_back_to_unknown(self) -> None:
        result = normalize_runtime_compaction_event(
            _event(trigger="provider-bespoke"),
        )
        assert result["trigger"] == "unknown"

    def test_canonical_triggers_pass_through(self) -> None:
        for t in ("event_hook", "response_header", "log_line", "unknown"):
            result = normalize_runtime_compaction_event(_event(trigger=t))
            assert result["trigger"] == t

    def test_unknown_kind_passes_through_verbatim(self) -> None:
        # A future runtime may introduce sub-kinds; the normaliser
        # must not clobber them. Only non-string / empty falls back.
        result = normalize_runtime_compaction_event(
            _event(kind="runtime_manual_compacted"),
        )
        assert result["kind"] == "runtime_manual_compacted"

    def test_non_string_kind_falls_back_to_default(self) -> None:
        result = normalize_runtime_compaction_event({"kind": 42})
        assert result["kind"] == "runtime_auto_compacted"

    def test_empty_string_kind_falls_back_to_default(self) -> None:
        result = normalize_runtime_compaction_event({"kind": ""})
        assert result["kind"] == "runtime_auto_compacted"

    def test_negative_token_count_clamps_to_none(self) -> None:
        result = normalize_runtime_compaction_event(
            _event(pre_used_tokens=-1),
        )
        assert result["pre_used_tokens"] is None

    def test_boolean_token_count_clamps_to_none(self) -> None:
        # ``True`` is an int in Python; defensive — a stray bool
        # must not project to 1.
        result = normalize_runtime_compaction_event(
            {"pre_used_tokens": True},
        )
        assert result["pre_used_tokens"] is None

    def test_non_int_token_count_clamps_to_none(self) -> None:
        result = normalize_runtime_compaction_event(
            {"post_used_tokens": "many"},
        )
        assert result["post_used_tokens"] is None

    def test_preserved_slots_filtered_to_canonical_names(self) -> None:
        result = normalize_runtime_compaction_event(_event(
            preserved_slots=[
                "task_and_acceptance",   # canonical
                "phantom_slot",          # invalid — dropped
                "risks",                 # canonical
                42,                      # non-string — dropped
            ],
        ))
        assert result["preserved_slots"] == [
            "task_and_acceptance",
            "risks",
        ]

    def test_artifact_refs_copied_defensively(self) -> None:
        refs_in = [{"path": "/a"}, "ref-2"]
        payload = _event(artifact_refs=refs_in)
        result = normalize_runtime_compaction_event(payload)
        result["artifact_refs"].append("smuggled")
        assert payload["artifact_refs"] == refs_in  # source untouched

    def test_normalize_does_not_mutate_input(self) -> None:
        payload = _event(trigger="bogus", preserved_slots=["phantom"])
        before = json.dumps(payload, sort_keys=True)
        normalize_runtime_compaction_event(payload)
        after = json.dumps(payload, sort_keys=True)
        assert before == after


# ── Resolver ─────────────────────────────────────────────────────────────────


class _Agent:
    """Fake-agent stub. Resolver reads only attributes."""

    def __init__(self, **attrs) -> None:
        for k, v in attrs.items():
            setattr(self, k, v)


class TestResolveRuntimeCompactionEvent:
    def test_returns_none_when_attribute_missing(self) -> None:
        assert resolve_runtime_compaction_event(_Agent()) is None

    def test_returns_none_when_attribute_is_none(self) -> None:
        agent = _Agent(last_runtime_compaction_event=None)
        assert resolve_runtime_compaction_event(agent) is None

    def test_returns_none_when_attribute_is_not_a_dict(self) -> None:
        agent = _Agent(last_runtime_compaction_event="auto-compacted")
        assert resolve_runtime_compaction_event(agent) is None

    def test_returns_event_when_attribute_is_dict(self) -> None:
        agent = _Agent(last_runtime_compaction_event=_event(
            preserved_slots=["task_and_acceptance", "risks"],
            artifact_refs=[{"path": "/summary.md"}],
        ))
        ev = resolve_runtime_compaction_event(agent)
        assert isinstance(ev, RuntimeCompactionEvent)
        assert ev.kind == "runtime_auto_compacted"
        assert ev.trigger == "event_hook"
        assert ev.pre_used_tokens == 150_000
        assert ev.post_used_tokens == 40_000
        assert ev.summary_tokens == 4_096
        assert ev.preserved_slots == (
            "task_and_acceptance", "risks",
        )
        assert ev.artifact_refs == ({"path": "/summary.md"},)

    def test_event_is_frozen(self) -> None:
        ev = RuntimeCompactionEvent(
            kind="runtime_auto_compacted",
            trigger="event_hook",
            pre_used_tokens=None,
            post_used_tokens=None,
            summary_tokens=None,
            preserved_slots=(),
            artifact_refs=(),
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            ev.kind = "other"  # type: ignore[misc]


# ── Recovery-contract validator ──────────────────────────────────────────────


class TestValidateCompactionRecovery:
    def test_no_event_no_session_every_slot_missing(self) -> None:
        result = validate_compaction_recovery({}, None)
        assert isinstance(result, RecoveryValidationResult)
        assert result.satisfied == ()
        assert set(result.missing) == set(REQUIRED_COMPACTION_PRESERVE_FIELDS)
        assert result.by_artifact == ()
        assert result.by_session == ()

    def test_full_event_artifact_coverage_satisfies_every_slot(self) -> None:
        event = _event(
            preserved_slots=list(REQUIRED_COMPACTION_PRESERVE_FIELDS),
        )
        result = validate_compaction_recovery({}, event)
        assert result.missing == ()
        assert set(result.satisfied) == set(
            REQUIRED_COMPACTION_PRESERVE_FIELDS,
        )
        # Every slot satisfied via artifact (since the session has
        # no fields).
        assert set(result.by_artifact) == set(
            REQUIRED_COMPACTION_PRESERVE_FIELDS,
        )
        assert result.by_session == ()

    def test_session_field_satisfies_slot_without_event(self) -> None:
        session = {
            "task": "Add runtime compaction surface",
            "plan_markdown": "## Plan\n...",
            "risks": ["regression in extractor"],
        }
        result = validate_compaction_recovery(session, None)
        assert "task_and_acceptance" in result.satisfied
        assert "approved_plan_and_non_goals" in result.satisfied
        assert "risks" in result.satisfied
        # The truthy match path counts as session-derived.
        assert "task_and_acceptance" in result.by_session
        # Slots without session evidence stay missing.
        assert "verification_gaps" in result.missing

    def test_event_artifact_outranks_session_field(self) -> None:
        # When both sources cover a slot, by_artifact wins — the
        # runtime claimed it explicitly.
        session = {"task": "redundant", "risks": ["redundant"]}
        event = _event(preserved_slots=["task_and_acceptance"])
        result = validate_compaction_recovery(session, event)
        assert "task_and_acceptance" in result.by_artifact
        assert "task_and_acceptance" not in result.by_session

    def test_empty_session_field_does_not_count(self) -> None:
        session = {
            "task": "",            # falsy → not satisfying
            "files_changed": [],   # falsy → not satisfying
            "risks": None,         # falsy → not satisfying
        }
        result = validate_compaction_recovery(session, None)
        assert "task_and_acceptance" in result.missing
        assert "files_read_and_changed" in result.missing
        assert "risks" in result.missing

    def test_accepts_resolved_event_dataclass(self) -> None:
        agent = _Agent(last_runtime_compaction_event=_event(
            preserved_slots=["risks"],
        ))
        resolved = resolve_runtime_compaction_event(agent)
        result = validate_compaction_recovery({}, resolved)
        assert "risks" in result.by_artifact

    def test_non_dict_session_treated_as_empty(self) -> None:
        result = validate_compaction_recovery("not-a-dict", None)  # type: ignore[arg-type]
        assert set(result.missing) == set(REQUIRED_COMPACTION_PRESERVE_FIELDS)

    def test_validator_does_not_raise_on_garbage_event(self) -> None:
        # Defensive — the validator is observe-only and must not
        # raise when handed unexpected shapes.
        result = validate_compaction_recovery({}, {"kind": 42, "trigger": []})
        assert isinstance(result, RecoveryValidationResult)
        assert set(result.missing) == set(REQUIRED_COMPACTION_PRESERVE_FIELDS)


# ── Extractor taxonomy ──────────────────────────────────────────────────────


class TestExtractorTaxonomy:
    def test_covered_fixture_emits_six_traces(self) -> None:
        traces = extract_runtime_compaction_traces(_covered_session())
        kinds = [(t.phase, t.trace_surface, t.attempt, t.round) for t in traces]
        assert kinds == [
            ("plan",            "plan",            1,    None),
            ("plan",            "replan",          2,    None),
            ("validate_plan",   "validate_plan",   1,    None),
            ("implement",       "implement",       None, None),
            ("review_changes",  "review_changes",  None, 1),
            ("repair_changes",  "repair_changes",  None, 1),
        ]

    def test_missing_runtime_compaction_keys_are_skipped(self) -> None:
        session = {
            "phases": {
                "plan":      [{"attempt": 1}],
                "implement": {},
                "rounds":    [{"round": 1}],
            },
        }
        assert extract_runtime_compaction_traces(session) == []

    def test_non_dict_session_returns_empty(self) -> None:
        assert extract_runtime_compaction_traces("nope") == []  # type: ignore[arg-type]
        assert extract_runtime_compaction_traces(None) == []  # type: ignore[arg-type]

    def test_latest_event_returns_last_in_walk_order(self) -> None:
        session = _covered_session()
        # Tag the repair round's event so we can identify it.
        session["phases"]["rounds"][0]["runtime_compaction_repair"][
            "summary_tokens"
        ] = 9999
        latest = latest_runtime_compaction_event(session)
        assert latest is not None
        assert latest["summary_tokens"] == 9999

    def test_latest_event_none_when_no_event_stamped(self) -> None:
        assert latest_runtime_compaction_event({}) is None
        assert latest_runtime_compaction_event(
            {"phases": {"plan": []}},
        ) is None


# ── Writer-side stamping (fake-provider unit test) ───────────────────────────


class TestWriterStamping:
    """The writer in ``_session_aware_invoke`` stamps a
    ``runtime_compaction`` phase_log entry only when the agent
    exposes ``last_runtime_compaction_event``. Exercised against a
    fake-provider stub so no real model call is needed."""

    def _make_state(self):
        from pipeline.plugins import PluginConfig
        from pipeline.runtime import PipelineState
        return PipelineState(
            task="Probe runtime compaction stamp",
            project_dir="/proj",
            plugin=PluginConfig(),
            extras={"run_id": "run-rc-1"},
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

    class _SilentAgent:
        """Fake agent that does NOT expose
        ``last_runtime_compaction_event`` — writer must not stamp."""

        model = "fake-model"
        session_id = None
        last_estimated_tokens_in = 200
        last_estimated_tokens_out = 50

        def invoke(self, prompt, cwd, *, continue_session=False,
                   attachments=(), mutates_artifacts=False):
            return "ok"

    class _CompactingAgent:
        """Fake agent exposing a runtime compaction event."""

        model = "fake-model"
        session_id = None
        last_estimated_tokens_in = 200
        last_estimated_tokens_out = 50
        last_runtime_compaction_event = {
            "kind":             "runtime_auto_compacted",
            "trigger":          "event_hook",
            "pre_used_tokens":  150_000,
            "post_used_tokens": 40_000,
            "summary_tokens":   4_096,
            "preserved_slots":  ["task_and_acceptance", "risks"],
            "artifact_refs":    [{"path": "/run/summary.md"}],
        }

        def invoke(self, prompt, cwd, *, continue_session=False,
                   attachments=(), mutates_artifacts=False):
            return "ok"

    def test_writer_skips_stamp_when_agent_exposes_no_event(self) -> None:
        from pipeline.phases.builtin import _session_aware_invoke

        state = self._make_state()
        turn = self._make_turn()

        _session_aware_invoke(
            self._SilentAgent(), state,
            phase="plan",
            turn=turn,
            cwd="/proj",
        )

        assert "runtime_compaction" not in state.phase_log["plan"]

    def test_writer_stamps_runtime_compaction_when_event_present(self) -> None:
        from pipeline.phases.builtin import _session_aware_invoke

        state = self._make_state()
        turn = self._make_turn()

        _session_aware_invoke(
            self._CompactingAgent(), state,
            phase="plan",
            turn=turn,
            cwd="/proj",
        )

        rc = state.phase_log["plan"]["runtime_compaction"]
        assert rc["kind"] == "runtime_auto_compacted"
        assert rc["trigger"] == "event_hook"
        assert rc["pre_used_tokens"] == 150_000
        assert rc["post_used_tokens"] == 40_000
        assert rc["summary_tokens"] == 4_096
        assert rc["preserved_slots"] == [
            "task_and_acceptance", "risks",
        ]
        assert rc["artifact_refs"] == [{"path": "/run/summary.md"}]
        # Attribution slots come from the writer, not the agent.
        assert rc["phase"] == "plan"
        assert rc["round"] is None
        assert rc["surface_id"] is None

    def test_writer_correlation_keys_match_sibling_records(self) -> None:
        from pipeline.phases.builtin import _session_aware_invoke

        state = self._make_state()
        turn = self._make_turn()

        _session_aware_invoke(
            self._CompactingAgent(), state,
            phase="plan",
            turn=turn,
            cwd="/proj",
        )

        log = state.phase_log["plan"]
        rc = log["runtime_compaction"]
        pr = log["prompt_render"]
        cg = log["context_growth"]
        cc = log["context_clearing"]
        cp = log["context_pressure"]
        for key in ("prefix_hash", "payload_hash", "wire_chars"):
            assert rc[key] == pr[key] == cg[key] == cc[key] == cp[key], (
                f"key {key!r} drifted across the five sibling records"
            )

    def test_writer_stamp_survives_handler_overwrite_via_carry_helper(
        self,
    ) -> None:
        # ``_carry_trace_metadata`` must enumerate
        # ``runtime_compaction`` alongside the M14.1 / M14.3 / M14.4
        # keys so a handler-driven phase_log overwrite doesn't drop
        # the event.
        from pipeline.phases.builtin import _carry_trace_metadata

        state = self._make_state()
        state.phase_log["plan"] = {
            "prompt_render":      {"x": 1},
            "context_growth":     {"y": 2},
            "context_clearing":   {"z": 3},
            "context_pressure":   {"w": 4},
            "runtime_compaction": {"v": 5},
            "output":             "...",
        }
        carried = _carry_trace_metadata(state, "plan")
        assert carried == {
            "prompt_render":      {"x": 1},
            "context_growth":     {"y": 2},
            "context_clearing":   {"z": 3},
            "context_pressure":   {"w": 4},
            "runtime_compaction": {"v": 5},
        }


# ── PhaseRuntimeCompaction dataclass surface ─────────────────────────────────


class TestDataclassSurface:
    def test_dataclass_is_frozen(self) -> None:
        trace = PhaseRuntimeCompaction(
            phase="plan",
            trace_surface="plan",
            attempt=1,
            round=None,
            source_path="phases.plan[0].runtime_compaction",
            payload=normalize_runtime_compaction_event(_event()),
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            trace.phase = "implement"  # type: ignore[misc]
