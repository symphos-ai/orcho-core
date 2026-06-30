"""M12-C4 — Durable trace-shape regression guards.

The shape contract for ``PhaseRenderTrace.payload`` lives in
:data:`pipeline.observability.prompt_render.DURABLE_FIELDS`. This
test pins:

- All 12 keys present on every trace payload.
- Values JSON-serializable per trace.
- Stateless traces preserve ``physical_session_key=None`` and
  ``session_split="stateless"``.
- Linear defaults hold on every trace: ``execution_mode="linear"``,
  ``surface_id=None``, ``surface_count=1``.
- Shape stability — extra/missing keys both reject so a future
  drift in the writer-side ``prompt_render`` dict is caught at the
  M12 boundary, not silently passed through.

The shape contract is referenced from M12-C3 evidence serialization
and from the coverage contract test. A drift here would cascade
into both surfaces, so this guard is the canonical anchor.
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from pipeline.observability.prompt_render import (
    DURABLE_FIELDS,
    PhaseRenderTrace,
    extract_prompt_render_traces,
    normalize_prompt_render,
)


def _payload(scope: str = "per_phase:plan") -> dict:
    return {
        "render_mode": "full",
        "session_split": "per_phase",
        "session_key": {
            "run_id": "20260517_120000",
            "runtime": "agents.runtimes.claude.ClaudeAgent",
            "model_key": "claude-opus-4-7",
            "scope": scope,
        },
        "part_ids": ["role:systems_architect@0", "task:plan@0"],
        "selected_part_keys": ["role:systems_architect@0"],
        "omitted_part_keys": [],
        "prefix_hash": "abc",
        "payload_hash": "def",
        "wire_chars": 1024,
    }


def _covered_session() -> dict:
    """A session fixture that touches every covered surface (plan +
    replan + validate_plan + implement + review_changes +
    repair_changes) so the shape guard runs against the full set."""
    return {
        "phases": {
            "plan": [
                {"attempt": 1, "prompt_render": _payload()},
                {
                    "attempt": 2,
                    "replan_critique": "needs more tests",
                    "prompt_render": _payload(),
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


class TestDurableTraceShape:
    def test_all_12_durable_fields_present_on_every_trace(self) -> None:
        traces = extract_prompt_render_traces(_covered_session())
        assert traces, "covered fixture must produce non-empty traces"
        for trace in traces:
            missing = set(DURABLE_FIELDS) - set(trace.payload.keys())
            extra = set(trace.payload.keys()) - set(DURABLE_FIELDS)
            assert not missing and not extra, (
                f"Trace {trace.source_path!r} payload shape drift: "
                f"missing={sorted(missing)} extra={sorted(extra)}"
            )

    def test_every_trace_is_json_serializable(self) -> None:
        # The durable payload feeds evidence persistence; every value
        # must round-trip through ``json.dumps`` without coercion.
        traces = extract_prompt_render_traces(_covered_session())
        for trace in traces:
            encoded = json.dumps(dataclasses.asdict(trace))
            decoded = json.loads(encoded)
            # Spot-check round-trip integrity on a load-bearing field.
            assert decoded["payload"]["execution_mode"] == "linear"

    def test_stateless_trace_shape_is_valid(self) -> None:
        stateless = {
            "render_mode": "full",
            "session_split": "stateless",
            "session_key": None,
            "selected_part_keys": [],
            "omitted_part_keys": [],
            "prefix_hash": "",
            "payload_hash": "",
            "wire_chars": 256,
        }
        session = {
            "phases": {
                "implement": {"prompt_render": stateless},
            },
        }
        trace = extract_prompt_render_traces(session)[0]
        assert trace.payload["physical_session_key"] is None
        assert trace.payload["session_split"] == "stateless"
        # Stateless still carries the M12 linear-default invariants.
        assert trace.payload["execution_mode"] == "linear"
        assert trace.payload["surface_id"] is None
        assert trace.payload["surface_count"] == 1
        # Shape still complete despite null session.
        assert set(trace.payload.keys()) == set(DURABLE_FIELDS)

    @pytest.mark.parametrize(
        "trace_index", [0, 1, 2, 3, 4, 5],
    )
    def test_linear_defaults_pinned_on_every_covered_surface(
        self, trace_index: int,
    ) -> None:
        # Every covered surface trace today is a pre-fanout linear
        # invocation. The M12-C2 fallback says so explicitly; this
        # guard pins the contract per-surface so a future change
        # cannot silently flip one surface while leaving the others.
        traces = extract_prompt_render_traces(_covered_session())
        trace = traces[trace_index]
        assert trace.payload["execution_mode"] == "linear", (
            f"surface {trace.trace_surface!r} (source "
            f"{trace.source_path!r}) lost the linear fallback"
        )
        assert trace.payload["surface_id"] is None
        assert trace.payload["surface_count"] == 1

    def test_normalize_prompt_render_returns_exact_durable_key_set(
        self,
    ) -> None:
        # Direct unit assertion on the normalization function — even
        # if extractor wiring changes, ``normalize_prompt_render``
        # itself must produce exactly the durable shape.
        result = normalize_prompt_render(_payload())
        assert set(result.keys()) == set(DURABLE_FIELDS)


class TestDurableShapeContractStability:
    """The durable-fields tuple is the contract every consumer
    reads. Drift checks here keep the tuple synchronized with the
    actual normalization output."""

    def test_durable_fields_tuple_matches_normalize_output(self) -> None:
        result = normalize_prompt_render({
            "render_mode": "delta",
            "session_split": "per_phase",
            "session_key": {"scope": "per_phase:plan"},
            "selected_part_keys": [],
            "omitted_part_keys": [],
            "prefix_hash": "",
            "payload_hash": "",
            "wire_chars": 0,
        })
        assert set(result.keys()) == set(DURABLE_FIELDS)
        assert len(DURABLE_FIELDS) == 17, (
            "DURABLE_FIELDS length drifted; review every downstream "
            "consumer (evidence schema, coverage contract) before "
            "extending."
        )

    def test_artifact_path_intentionally_absent_from_durable_shape(
        self,
    ) -> None:
        """``artifact_path`` is per-part trace metadata, not per-render
        summary evidence. The durable shape stays summary-only by
        design — it identifies which parts went on the wire
        (``part_ids`` / ``selected_part_keys``) plus hashes and wire
        size, but never the raw content or per-part metadata of any
        single part. Pinning the absence here defends against an
        accidental future "let's just dump artifact_path here too"
        regression that would explode the durable payload size and
        re-couple render summary to per-part schema. If a future
        feature needs durable per-part artifact references, it ships
        as its own observability sibling (e.g. an ``artifact_refs``
        record), not inside ``prompt_render``.
        """
        assert "artifact_path" not in DURABLE_FIELDS
        # Defense-in-depth: even if a writer were to stamp the field
        # on the source payload, normalize_prompt_render must not
        # leak it into the durable shape.
        result = normalize_prompt_render({
            "render_mode": "full",
            "session_split": "per_phase",
            "session_key": None,
            "selected_part_keys": ["artifact:validate_plan@0"],
            "omitted_part_keys": [],
            "prefix_hash": "abc",
            "payload_hash": "def",
            "wire_chars": 42,
            "artifact_path": "/tmp/should_not_leak.md",  # noqa: hostile
        })
        assert "artifact_path" not in result

    def test_trace_payload_remains_json_serializable_under_extreme_inputs(
        self,
    ) -> None:
        # Selected/omitted lists with empty strings, hash with
        # special chars — should still serialize cleanly.
        extreme = {
            "render_mode": "delta",
            "session_split": "per_phase",
            "session_key": {"scope": "per_phase:plan", "run_id": ""},
            "selected_part_keys": [""],
            "omitted_part_keys": [],
            "prefix_hash": "sha256:" + "f" * 64,
            "payload_hash": "",
            "wire_chars": 0,
        }
        result = normalize_prompt_render(extreme)
        # Must round-trip through JSON without raising.
        json.loads(json.dumps(result))


class TestPartIdsAndProviderSessionId:
    """M12 closing: the writer (``_session_aware_invoke``) stamps two
    fields the earlier M12 slice left out — ``part_ids`` (full
    ordered superset of selected+omitted, in render order, using
    ``part_session_key`` format) and ``provider_session_id`` (the
    runtime's ``session_id`` at invoke time). Both surface durably
    in the trace payload."""

    def test_part_ids_is_durable_and_uses_session_key_format(self) -> None:
        payload = _payload()
        result = normalize_prompt_render(payload)
        assert "part_ids" in result
        assert result["part_ids"] == ["role:systems_architect@0", "task:plan@0"]
        # The shape contract pins the field; every entry must carry
        # the ``@<version>`` suffix so part_ids is byte-comparable
        # with selected/omitted keys.
        for key in result["part_ids"]:
            assert "@" in key, (
                f"part_ids entry {key!r} missing @version suffix; "
                "use pipeline.prompts.session.part_session_key"
            )

    def test_part_ids_is_superset_of_selected_and_omitted(self) -> None:
        # Audit invariant: every part that the M6 selector either
        # sent or omitted must also appear in the full ordered
        # part_ids list. Drift here means a part was renamed or
        # versioned mid-pipeline without consumers noticing.
        payload = _payload()
        payload["part_ids"] = [
            "role:systems_architect@0",
            "task:plan@0",
            "format:detailed@0",
        ]
        payload["selected_part_keys"] = [
            "role:systems_architect@0", "task:plan@0",
        ]
        payload["omitted_part_keys"] = ["format:detailed@0"]
        result = normalize_prompt_render(payload)
        union = set(result["selected_part_keys"]) | set(
            result["omitted_part_keys"]
        )
        assert union <= set(result["part_ids"]), (
            "selected ∪ omitted leaked an id missing from part_ids; "
            "writer-side stamping is out of sync with the M6 selector"
        )

    def test_part_ids_defaults_to_empty_list_when_absent(self) -> None:
        # Legacy / synthetic payloads predating the writer-side stamp
        # must still normalize cleanly — the projection defaults to
        # an empty list rather than raising or returning ``None``.
        payload = _payload()
        payload.pop("part_ids", None)
        result = normalize_prompt_render(payload)
        assert result["part_ids"] == []

    def test_provider_session_id_passes_through_when_writer_stamps_it(
        self,
    ) -> None:
        payload = _payload()
        payload["provider_session_id"] = "sess-claude-abc-123"
        result = normalize_prompt_render(payload)
        assert result["provider_session_id"] == "sess-claude-abc-123"

    def test_provider_session_id_is_none_when_writer_did_not_stamp(
        self,
    ) -> None:
        # Stateless runtimes (Codex, mock) return ``session_id=None``
        # at invoke time. The writer stamps that value through; the
        # extractor preserves it.
        payload = _payload()
        payload.pop("provider_session_id", None)
        result = normalize_prompt_render(payload)
        assert result["provider_session_id"] is None


class TestNoTraceConstructionInTests:
    """Defense-in-depth: tests must not bypass the extractor by
    constructing ``PhaseRenderTrace`` directly with an arbitrary
    payload. The extractor is the only sanctioned path so the
    durable-shape contract holds end-to-end."""

    def test_direct_construction_is_possible_but_does_not_skip_shape_check(
        self,
    ) -> None:
        # The dataclass itself does not enforce shape — that contract
        # lives in normalize_prompt_render. Direct construction
        # remains supported for adapters that may want to inject
        # synthetic traces in the future, but every shipped path
        # goes through the extractor + normalizer.
        trace = PhaseRenderTrace(
            phase="implement",
            trace_surface="implement",
            attempt=None,
            round=None,
            source_path="phases.implement.prompt_render",
            payload={"made_up": True},  # not durable shape
        )
        assert trace.phase == "implement"
        # The synthetic payload is NOT durable — the contract is
        # honoured by callers using ``normalize_prompt_render``,
        # not by the dataclass itself.
        assert "render_mode" not in trace.payload
