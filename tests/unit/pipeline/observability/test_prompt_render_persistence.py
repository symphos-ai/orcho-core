"""M12-C1 — Durable prompt-render extractor.

Pins the read-only projection ``extract_prompt_render_traces`` over
``session["phases"]`` per the coverage contract in
``test_prompt_render_coverage.py``. Tests are table-driven over
minimal fixtures so a failure points at the exact extraction rule;
the golden-fixture path exists only as one final sanity smoke.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from pipeline.observability.prompt_render import (
    DURABLE_FIELDS,
    PhaseRenderTrace,
    extract_prompt_render_traces,
    normalize_prompt_render,
)

_GOLDEN_FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "golden"
    / "full_mode_single_round.json"
)


def _payload(scope: str = "per_phase:plan", session_id: str | None = "sid-1") -> dict:
    """Minimal source ``prompt_render`` dict (M11.5 shape — not yet normalized)."""
    return {
        "render_mode": "full",
        "session_split": "per_phase",
        "session_key": {
            "run_id": "20260517_120000",
            "runtime": "agents.runtimes.claude.ClaudeAgent",
            "model_key": "claude-opus-4-7",
            "scope": scope,
        },
        "selected_part_keys": ["role:systems_architect@0"],
        "omitted_part_keys": [],
        "prefix_hash": "abc",
        "payload_hash": "def",
        "wire_chars": 1024,
    }


# ── 1. Covered-surface extraction over minimal fixture ─────────────────────────


class TestExtractsCoveredSurfaces:
    def test_extracts_covered_surfaces_from_minimal_fixture(self) -> None:
        # One source record per covered surface (six surfaces, but plan
        # and replan share the same array — round-2 plan entry encodes
        # ``replan``). The minimal fixture exercises the full taxonomy.
        session = {
            "phases": {
                "plan": [
                    {"attempt": 1, "prompt_render": _payload("per_phase:plan")},
                    {
                        "attempt": 2,
                        "replan_critique": "missing tests",
                        "prompt_render": _payload("per_phase:plan"),
                    },
                ],
                "validate_plan": [
                    {"attempt": 1, "prompt_render": _payload("per_phase:validate_plan")},
                ],
                "implement": {"prompt_render": _payload("per_phase:implement")},
                "rounds": [
                    {
                        "round": 1,
                        "prompt_render_review": _payload("per_phase:review_changes"),
                        "prompt_render_repair": _payload("per_phase:implement"),
                    },
                ],
            },
        }
        traces = extract_prompt_render_traces(session)
        # Pin the return type — every trace must be a
        # :class:`PhaseRenderTrace` instance so M12-C2 normalization
        # and M12-C3 evidence projection can rely on the dataclass
        # surface without isinstance gymnastics.
        assert all(isinstance(t, PhaseRenderTrace) for t in traces)
        surfaces = [t.trace_surface for t in traces]
        # Order matches walk order: plan/replan first, then validate_plan,
        # then implement, then per-round review + repair.
        assert surfaces == [
            "plan",
            "replan",
            "validate_plan",
            "implement",
            "review_changes",
            "repair_changes",
        ]


# ── 2. plan-vs-replan surface discrimination ─────────────────────────────────


@pytest.mark.parametrize(
    ("attempt", "replan_critique", "expected_surface"),
    [
        (1, None, "plan"),
        (1, "", "plan"),  # empty string is falsy — still plan
        (2, None, "replan"),
        (1, "missing tests", "replan"),  # explicit critique on attempt 1
        (3, "", "replan"),
        (3, "critique text", "replan"),
    ],
)
def test_plan_vs_replan_surface_discrimination(
    attempt: int,
    replan_critique: str | None,
    expected_surface: str,
) -> None:
    entry: dict = {"attempt": attempt, "prompt_render": _payload()}
    if replan_critique is not None:
        entry["replan_critique"] = replan_critique
    session = {"phases": {"plan": [entry]}}
    traces = extract_prompt_render_traces(session)
    assert len(traces) == 1
    assert traces[0].trace_surface == expected_surface


# ── 3. Documented exceptions never synthesized ───────────────────────────────


class TestDocumentedExceptionsSkipped:
    def test_skips_documented_exceptions_even_with_stray_prompt_render(
        self,
    ) -> None:
        # ``hypothesis``, ``validate_hypothesis`` and ``final_acceptance``
        # are documented exceptions per the coverage contract: their
        # handlers do not route through ``_session_aware_invoke`` and
        # therefore should not appear in M12 traces. Even if a stray
        # ``prompt_render`` key ends up in their session entry (e.g.
        # a buggy test fixture), the extractor must not surface it.
        session = {
            "phases": {
                "hypothesis": {"prompt_render": _payload("per_phase:hypothesis")},
                "validate_hypothesis": {
                    "prompt_render": _payload("per_phase:validate_hypothesis"),
                },
                "final_acceptance": {
                    "prompt_render": _payload("per_phase:final_acceptance"),
                },
            },
        }
        traces = extract_prompt_render_traces(session)
        assert traces == []
        # Sanity: phases without exceptions still get walked.
        session["phases"]["implement"] = {
            "prompt_render": _payload("per_phase:implement"),
        }
        traces = extract_prompt_render_traces(session)
        assert [t.phase for t in traces] == ["implement"]


# ── 4. Missing prompt_render skipped gracefully ──────────────────────────────


class TestGracefulMissing:
    def test_missing_prompt_render_skipped_gracefully(self) -> None:
        # Handlers can fail mid-phase and leave entries without
        # ``prompt_render``; the extractor must not raise.
        session = {
            "phases": {
                "plan": [
                    {"attempt": 1, "output": "x"},  # no prompt_render
                    {"attempt": 2, "prompt_render": _payload()},
                ],
                "validate_plan": [{"attempt": 1, "output": "x"}],
                "implement": {"output": "x"},
                "rounds": [{"round": 1, "critique": "..."}],
            },
        }
        traces = extract_prompt_render_traces(session)
        # Only the round-2 plan entry has prompt_render.
        assert len(traces) == 1
        assert traces[0].phase == "plan"
        assert traces[0].attempt == 2

    def test_empty_session_returns_empty_list(self) -> None:
        assert extract_prompt_render_traces({}) == []
        assert extract_prompt_render_traces({"phases": {}}) == []

    def test_non_dict_session_returns_empty_list(self) -> None:
        # Defensive: malformed callers (e.g. ``None`` passed in)
        # should not crash the extractor.
        assert extract_prompt_render_traces(None) == []  # type: ignore[arg-type]
        assert extract_prompt_render_traces("not a dict") == []  # type: ignore[arg-type]


# ── 5. Stateless preserved ───────────────────────────────────────────────────


class TestStatelessPreserved:
    def test_stateless_session_key_none_preserved(self) -> None:
        # Stateless executions stamp ``session_key=None`` in the source
        # ``prompt_render`` record; the extractor must preserve that
        # null value in the payload rather than silently coercing it.
        stateless_payload = {
            "render_mode": "full",
            "session_split": "stateless",
            "session_key": None,
            "selected_part_keys": [],
            "omitted_part_keys": [],
            "prefix_hash": "",
            "payload_hash": "",
            "wire_chars": 512,
        }
        session = {
            "phases": {
                "implement": {"prompt_render": stateless_payload},
            },
        }
        traces = extract_prompt_render_traces(session)
        assert len(traces) == 1
        # M12-C2 renamed ``session_key`` to ``physical_session_key``
        # on the durable surface; the null value passes through.
        assert traces[0].payload["physical_session_key"] is None
        assert traces[0].payload["session_split"] == "stateless"


# ── 6. JSON-serializable output ──────────────────────────────────────────────


class TestJsonSerializable:
    def test_output_is_json_serializable(self) -> None:
        session = {
            "phases": {
                "plan": [{"attempt": 1, "prompt_render": _payload()}],
                "implement": {"prompt_render": _payload("per_phase:implement")},
            },
        }
        traces = extract_prompt_render_traces(session)
        as_dicts = [dataclasses.asdict(t) for t in traces]
        encoded = json.dumps(as_dicts)
        decoded = json.loads(encoded)
        # Round-trip preserves the structural shape.
        assert decoded[0]["phase"] == "plan"
        assert decoded[0]["trace_surface"] == "plan"
        assert decoded[0]["source_path"] == "phases.plan[0].prompt_render"


# ── 7. No raw prompt body field ──────────────────────────────────────────────


class TestNoRawPromptBody:
    @pytest.mark.parametrize(
        "forbidden_key",
        ["prompt_text", "prompt", "wire_prompt", "body"],
    )
    def test_payload_never_carries_raw_prompt_body_field(
        self, forbidden_key: str,
    ) -> None:
        # Defense-in-depth: the source ``prompt_render`` shape pinned
        # by ADR 0026 never includes raw text, and the M12 extractor
        # must not introduce one. The coverage contract already pins
        # this on the writer side; this test pins it on the extractor.
        session = {
            "phases": {
                "implement": {"prompt_render": _payload("per_phase:implement")},
            },
        }
        traces = extract_prompt_render_traces(session)
        for t in traces:
            assert forbidden_key not in t.payload, (
                f"Trace {t.source_path!r} leaked raw prompt body key "
                f"{forbidden_key!r} into the durable payload."
            )


# ── 8. source_path format ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("session_phases", "expected_source_paths"),
    [
        (
            {"plan": [{"attempt": 1, "prompt_render": _payload()}]},
            ["phases.plan[0].prompt_render"],
        ),
        (
            {
                "plan": [
                    {"attempt": 1, "prompt_render": _payload()},
                    {"attempt": 2, "prompt_render": _payload()},
                ],
            },
            ["phases.plan[0].prompt_render", "phases.plan[1].prompt_render"],
        ),
        (
            {"validate_plan": [{"attempt": 1, "prompt_render": _payload()}]},
            ["phases.validate_plan[0].prompt_render"],
        ),
        (
            {"implement": {"prompt_render": _payload()}},
            ["phases.implement.prompt_render"],
        ),
        (
            {
                "rounds": [
                    {
                        "round": 1,
                        "prompt_render_review": _payload(),
                        "prompt_render_repair": _payload(),
                    },
                ],
            },
            [
                "phases.rounds[0].prompt_render_review",
                "phases.rounds[0].prompt_render_repair",
            ],
        ),
    ],
)
def test_source_path_format(
    session_phases: dict,
    expected_source_paths: list[str],
) -> None:
    session = {"phases": session_phases}
    traces = extract_prompt_render_traces(session)
    assert [t.source_path for t in traces] == expected_source_paths


# ── 9. Golden-fixture smoke ─────────────────────────────────────────────────


class TestGoldenFixtureSmoke:
    def test_smoke_extraction_from_golden_fixture(self) -> None:
        # One sanity smoke against the real session shape produced by
        # the snapshot regression suite. Minimal fixtures above pin
        # the rules; this test guards against drift between the
        # writer-side shape and the extractor's expectations.
        if not _GOLDEN_FIXTURE.exists():
            pytest.skip(f"golden fixture missing: {_GOLDEN_FIXTURE}")
        session = json.loads(_GOLDEN_FIXTURE.read_text(encoding="utf-8"))
        traces = extract_prompt_render_traces(session)
        # The advanced single-round fixture covers plan + validate_plan
        # + implement at minimum; the round may or may not carry
        # prompt_render_* depending on snapshot vintage.
        surfaces = {t.trace_surface for t in traces}
        assert {"plan", "validate_plan", "implement"} <= surfaces, (
            f"golden fixture missing expected surfaces; got {surfaces}"
        )
        # Documented exceptions never appear.
        for forbidden in ("hypothesis", "validate_hypothesis", "final_acceptance"):
            assert forbidden not in {t.phase for t in traces}


# ── M12-C2 — Durable shape normalization ─────────────────────────────────────


class TestDurableShapeNormalization:
    """Pin the 12-field durable shape and the M12 fallback semantics."""

    def test_durable_shape_has_all_12_fields(self) -> None:
        session = {
            "phases": {
                "plan": [{"attempt": 1, "prompt_render": _payload()}],
                "validate_plan": [
                    {"attempt": 1, "prompt_render": _payload("per_phase:validate_plan")},
                ],
                "implement": {"prompt_render": _payload("per_phase:implement")},
                "rounds": [
                    {
                        "round": 1,
                        "prompt_render_review": _payload("per_phase:review_changes"),
                        "prompt_render_repair": _payload("per_phase:implement"),
                    },
                ],
            },
        }
        traces = extract_prompt_render_traces(session)
        assert traces
        for trace in traces:
            assert set(trace.payload.keys()) == set(DURABLE_FIELDS), (
                f"Trace {trace.source_path!r} payload keys "
                f"{sorted(trace.payload.keys())!r} != durable "
                f"{sorted(DURABLE_FIELDS)!r}"
            )

    def test_session_key_maps_to_physical_session_key(self) -> None:
        session_key_value = {
            "run_id": "20260517_120000",
            "runtime": "agents.runtimes.claude.ClaudeAgent",
            "model_key": "claude-opus-4-7",
            "scope": "per_phase:plan",
        }
        source = {
            **_payload(),
            "session_key": dict(session_key_value),
        }
        session = {
            "phases": {
                "plan": [{"attempt": 1, "prompt_render": source}],
            },
        }
        trace = extract_prompt_render_traces(session)[0]
        # Renamed key carries the same value.
        assert trace.payload["physical_session_key"] == session_key_value
        # Source key name is gone from the durable surface.
        assert "session_key" not in trace.payload

    def test_linear_defaults_pinned(self) -> None:
        session = {
            "phases": {
                "implement": {"prompt_render": _payload("per_phase:implement")},
            },
        }
        trace = extract_prompt_render_traces(session)[0]
        # Pre-fanout traces describe a single linear surface invocation.
        # M12-C2 hard-codes these defaults; ADR 0027 fanout flips them.
        assert trace.payload["execution_mode"] == "linear"
        assert trace.payload["surface_id"] is None
        assert trace.payload["surface_count"] == 1

    def test_execution_mode_is_fallback_not_runtime_derived(self) -> None:
        # The docstring on ``normalize_prompt_render`` pins the
        # semantics: M12 read-only projection cannot know the real
        # execution mode because the source ``prompt_render`` does
        # not persist it. Refuse to advertise this as runtime truth.
        doc = normalize_prompt_render.__doc__ or ""
        assert "fallback" in doc.lower()
        assert "not runtime-derived" in doc.lower() or (
            "NOT runtime-derived" in doc
        )

    def test_provider_session_id_passes_source_value_through(self) -> None:
        # M12-C5: future-safe pass-through. The source session does
        # not stamp ``provider_session_id`` today, so the durable
        # surface reports ``None`` by default. When a future
        # writer-side change starts stamping it, the same projection
        # surfaces the real value without code changes here. Pin
        # both directions so the contract is unambiguous.
        absent_session = {
            "phases": {
                "implement": {"prompt_render": _payload("per_phase:implement")},
            },
        }
        trace = extract_prompt_render_traces(absent_session)[0]
        assert trace.payload["provider_session_id"] is None

        present_session = {
            "phases": {
                "implement": {
                    "prompt_render": {
                        **_payload("per_phase:implement"),
                        "provider_session_id": "sid-from-future-writer",
                    },
                },
            },
        }
        trace = extract_prompt_render_traces(present_session)[0]
        assert trace.payload["provider_session_id"] == "sid-from-future-writer"

    def test_session_json_byte_stable_under_extraction(self) -> None:
        # The extractor is read-only: passing the same session dict
        # through extraction must not mutate it. Snapshot the JSON
        # bytes before and after to catch any in-place edit.
        session = {
            "phases": {
                "plan": [{"attempt": 1, "prompt_render": _payload()}],
                "implement": {"prompt_render": _payload("per_phase:implement")},
                "rounds": [
                    {
                        "round": 1,
                        "prompt_render_review": _payload(
                            "per_phase:review_changes",
                        ),
                        "prompt_render_repair": _payload("per_phase:implement"),
                    },
                ],
            },
        }
        before = json.dumps(session, sort_keys=True)
        extract_prompt_render_traces(session)
        after = json.dumps(session, sort_keys=True)
        assert before == after, (
            "extract_prompt_render_traces mutated the source session"
        )

    def test_normalize_passthrough_fields(self) -> None:
        # Direct ``normalize_prompt_render`` test for the seven
        # passthrough fields plus the two fallback rules.
        source = {
            "render_mode": "delta",
            "session_split": "per_phase",
            "session_key": {"scope": "per_phase:implement"},
            "selected_part_keys": ["a@0", "b@0"],
            "omitted_part_keys": ["c@0"],
            "prefix_hash": "h1",
            "payload_hash": "h2",
            "wire_chars": 1234,
        }
        result = normalize_prompt_render(source)
        assert result["render_mode"] == "delta"
        assert result["session_split"] == "per_phase"
        assert result["physical_session_key"] == {"scope": "per_phase:implement"}
        assert result["selected_part_keys"] == ["a@0", "b@0"]
        assert result["omitted_part_keys"] == ["c@0"]
        assert result["prefix_hash"] == "h1"
        assert result["payload_hash"] == "h2"
        assert result["wire_chars"] == 1234
        # M12 fallbacks.
        assert result["execution_mode"] == "linear"
        assert result["surface_id"] is None
        assert result["surface_count"] == 1
        assert result["provider_session_id"] is None


# ── M12-C5: pass-through fallbacks for writer-stamped values ─────────────────


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("execution_mode", "fanout_review"),
        ("surface_id", "correctness"),
        ("surface_count", 3),
    ],
)
def test_fallbacks_pass_source_value_through_when_writer_stamps_it(
    field: str, value: object,
) -> None:
    """When a future writer (ADR 0027 fanout) stamps the real
    value in the source ``prompt_render`` record, the durable
    surface must report it as-is — the documented fallback only
    kicks in when the source is absent.
    """
    source = {**_payload(), field: value}
    session = {
        "phases": {
            "implement": {"prompt_render": source},
        },
    }
    trace = extract_prompt_render_traces(session)[0]
    assert trace.payload[field] == value


@pytest.mark.parametrize(
    ("field", "expected_fallback"),
    [
        ("execution_mode", "linear"),
        ("surface_count", 1),
    ],
)
def test_fallbacks_apply_only_when_source_field_is_absent(
    field: str, expected_fallback: object,
) -> None:
    # The fallback is the documented value when the source record
    # does not stamp the field. Pre-fanout traces always hit this
    # path; ADR 0027 retires the fallback when the writer starts
    # stamping the real value.
    source = _payload()
    assert field not in source  # baseline: source omits the key.
    session = {
        "phases": {
            "implement": {"prompt_render": source},
        },
    }
    trace = extract_prompt_render_traces(session)[0]
    assert trace.payload[field] == expected_fallback


def test_surface_id_passes_none_through_when_absent() -> None:
    # ``surface_id`` has no documented fallback (it stays None
    # until ADR 0027 stamps a real lens id). Pin the contract.
    source = _payload()
    assert "surface_id" not in source
    session = {
        "phases": {
            "implement": {"prompt_render": source},
        },
    }
    trace = extract_prompt_render_traces(session)[0]
    assert trace.payload["surface_id"] is None
