"""M12-C3 — Evidence integration: ``evidence["prompt_render"]``
section + strict schema + raw-leak guard.

Two test layers:

1. Unit tests on synthetic ``meta.json`` fixtures pin the projection
   rules (summary shape, always-present, exceptions never appear,
   no raw text leaks).
2. One hermetic integration test runs a real mock-pipeline end to
   end and verifies the wired evidence section against the schema.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.evidence.collector import collect_evidence
from pipeline.evidence.schema import (
    REQUIRED_PROMPT_RENDER_KEYS,
    EvidenceSchemaError,
    validate_bundle,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


def _prompt_render_payload(
    scope: str = "per_phase:plan",
    *,
    phase_key: str | None = None,
    round_n: int | None = None,
    continue_session: bool = False,
) -> dict:
    """Minimal source ``prompt_render`` dict (M11.5 writer shape).

    E1 follow-up: the modern ``_session_aware_invoke`` writer always
    stamps ``phase_key`` / ``round`` / ``continue_session``. Test
    fixtures match that shape so the evidence projection can surface
    them. ``phase_key`` defaults to the surface implied by ``scope``
    when the caller doesn't pass one explicitly.
    """
    if phase_key is None:
        # Derive from scope like ``per_phase:plan`` →  ``plan``. For
        # synthetic scopes (e.g. ``common``) fall back to ``plan``.
        phase_key = scope.split(":", 1)[1] if ":" in scope else "plan"
    return {
        "render_mode": "full",
        "session_split": "per_phase",
        "session_key": {
            "run_id": "20260517_120000",
            "runtime": "agents.runtimes.claude.ClaudeAgent",
            "model_key": "claude-opus-4-7",
            "scope": scope,
        },
        "selected_part_keys": ["role:systems_architect@0", "task:plan@0"],
        "omitted_part_keys": ["format:terse@0"],
        "prefix_hash": "p" * 64,
        "payload_hash": "q" * 64,
        "wire_chars": 4321,
        "phase_key": phase_key,
        "round": round_n,
        "continue_session": continue_session,
    }


def _write_run_dir(
    tmp_path: Path, *, meta: dict, events: list[dict] | None = None,
    metrics: dict | None = None,
) -> Path:
    """Compose a minimal run_dir on disk that ``collect_evidence`` can read."""
    run = tmp_path / "run"
    run.mkdir()
    (run / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (run / "metrics.json").write_text(
        json.dumps(metrics or {}), encoding="utf-8",
    )
    lines = [json.dumps(e) for e in (events or [])]
    (run / "events.jsonl").write_text("\n".join(lines), encoding="utf-8")
    return run


def _base_meta(extra_phases: dict | None = None) -> dict:
    """Compose a meta.json dict that passes ``validate_bundle``'s
    prerequisites for the plan / phases / metrics rollups.

    The evidence collector reads ``meta.json`` for the plan record and
    findings; the bundle is built primarily from events.jsonl, but a
    valid meta keeps the plan / release-summary sections sane during
    schema validation.
    """
    meta = {
        "run_id": "20260517_120000",
        "task": "demo task",
        "profile": "feature",
        "status": "done",
        "phases": dict(extra_phases or {}),
    }
    return meta


# ── 0. Writer-stamped attribution (E1 follow-up) ─────────────────────────────


class TestWriterStampedAttribution:
    """``phase_key`` / ``round`` / ``continue_session`` are stamped on
    the source ``prompt_render`` dict by ``_session_aware_invoke``.

    The evidence projection must surface them verbatim so an operator
    can answer "which round was this and did it continue the session?"
    from ``evidence.json`` alone — no cross-reference against
    ``runner.log`` or the agent_sessions checkpoint required.
    """

    def test_continue_session_round_phase_key_for_multi_round_plan(
        self, tmp_path: Path,
    ) -> None:
        # Three plan attempts mirror a validate_plan-reject + resume
        # cycle: round 1 fresh, round 2 fresh subprocess restart,
        # round 3 continued provider session.
        meta = _base_meta(
            extra_phases={
                "plan": [
                    {
                        "attempt": 1,
                        "prompt_render": _prompt_render_payload(
                            "per_phase:plan",
                            phase_key="plan",
                            round_n=1,
                            continue_session=False,
                        ),
                    },
                    {
                        "attempt": 2,
                        "replan_critique": "round 1 rejected",
                        "prompt_render": _prompt_render_payload(
                            "per_phase:plan",
                            phase_key="plan",
                            round_n=2,
                            continue_session=False,
                        ),
                    },
                    {
                        "attempt": 3,
                        "replan_critique": "round 2 rejected",
                        "prompt_render": _prompt_render_payload(
                            "per_phase:plan",
                            phase_key="plan",
                            round_n=3,
                            continue_session=True,
                        ),
                    },
                ],
            },
        )
        run = _write_run_dir(tmp_path, meta=meta)
        bundle = collect_evidence(run)

        section = bundle["prompt_render"]
        assert len(section) == 3
        assert [e["phase_key"] for e in section] == ["plan", "plan", "plan"]
        assert [e["round"] for e in section] == [1, 2, 3]
        assert [e["continue_session"] for e in section] == [
            False, False, True,
        ]
        # phase_key is required, not optional, so the schema must
        # accept the bundle cleanly.
        validate_bundle(bundle)

    def test_phase_key_diverges_from_phase_for_chain_repair_changes(
        self, tmp_path: Path,
    ) -> None:
        # CHAIN mode: repair_changes reuses the implement physical
        # session key, so the writer stamps ``phase_key="implement"``
        # even though the trace attributes to ``phase="repair_changes"``.
        meta = _base_meta(
            extra_phases={
                "rounds": [
                    {
                        "round": 1,
                        "prompt_render_review": _prompt_render_payload(
                            "per_role:code_reviewer",
                            phase_key="review_changes",
                            round_n=1,
                            continue_session=False,
                        ),
                        "prompt_render_repair": _prompt_render_payload(
                            "per_phase:implement",
                            phase_key="implement",
                            round_n=1,
                            continue_session=True,
                        ),
                    },
                ],
            },
        )
        run = _write_run_dir(tmp_path, meta=meta)
        bundle = collect_evidence(run)

        by_phase = {e["phase"]: e for e in bundle["prompt_render"]}
        assert by_phase["review_changes"]["phase_key"] == "review_changes"
        assert by_phase["review_changes"]["continue_session"] is False
        # The diverged attribution — phase vs phase_key — that
        # justifies carrying both fields on the evidence surface.
        assert by_phase["repair_changes"]["phase"] == "repair_changes"
        assert by_phase["repair_changes"]["phase_key"] == "implement"
        assert by_phase["repair_changes"]["continue_session"] is True
        validate_bundle(bundle)

    def test_legacy_payload_without_writer_stamps_falls_back(
        self, tmp_path: Path,
    ) -> None:
        # Legacy / synthetic source payloads that predate the
        # writer-stamp contract must still produce a valid evidence
        # entry: phase_key falls back to the structural phase,
        # round falls back to the structural ``attempt`` for
        # plan/validate_plan (so the round counter still surfaces),
        # and ``continue_session`` reports ``None`` — explicit
        # "we don't know" rather than a fabricated default.
        legacy_payload = {
            "render_mode": "full",
            "session_split": "per_phase",
            "session_key": {
                "run_id": "20260517_120000",
                "runtime": "agents.runtimes.claude.ClaudeAgent",
                "model_key": "claude-opus-4-7",
                "scope": "per_phase:plan",
            },
            "selected_part_keys": ["task:plan@0"],
            "omitted_part_keys": [],
            "prefix_hash": "p" * 64,
            "payload_hash": "q" * 64,
            "wire_chars": 1024,
            # No phase_key / round / continue_session stamps.
        }
        meta = _base_meta(
            extra_phases={
                "plan": [
                    {"attempt": 2, "prompt_render": legacy_payload},
                ],
            },
        )
        run = _write_run_dir(tmp_path, meta=meta)
        bundle = collect_evidence(run)

        entry = bundle["prompt_render"][0]
        assert entry["phase_key"] == "plan"
        assert entry["round"] == 2  # structural fallback to attempt
        # Legacy / synthetic entries surface ``None`` for
        # continue_session — explicit "we don't know" rather than a
        # fabricated default. The schema declares the slot
        # Optional[bool] so the legacy composition still round-trips
        # through ``validate_bundle``.
        assert entry["continue_session"] is None
        validate_bundle(bundle)


# ── 1. Always-present section ────────────────────────────────────────────────


class TestPromptRenderSectionAlwaysPresent:
    def test_bundle_has_prompt_render_section_when_meta_has_records(
        self, tmp_path: Path,
    ) -> None:
        meta = _base_meta(
            extra_phases={
                "plan": [
                    {
                        "attempt": 1,
                        "prompt_render": _prompt_render_payload(),
                    },
                ],
                "implement": {
                    "prompt_render": _prompt_render_payload("per_phase:implement"),
                },
            },
        )
        run = _write_run_dir(tmp_path, meta=meta)
        bundle = collect_evidence(run)
        assert "prompt_render" in bundle
        assert isinstance(bundle["prompt_render"], list)
        assert len(bundle["prompt_render"]) == 2

    def test_bundle_has_empty_prompt_render_when_meta_has_no_records(
        self, tmp_path: Path,
    ) -> None:
        # Always-present contract: the key exists even when nothing
        # to summarize. Downstream consumers can treat absence as a
        # collector bug, not as "no traces".
        run = _write_run_dir(tmp_path, meta=_base_meta())
        bundle = collect_evidence(run)
        assert "prompt_render" in bundle
        assert bundle["prompt_render"] == []


# ── 2. Strict schema validation ──────────────────────────────────────────────


class TestPromptRenderStrictSchema:
    def test_each_entry_has_all_required_keys(self, tmp_path: Path) -> None:
        meta = _base_meta(
            extra_phases={
                "plan": [
                    {"attempt": 1, "prompt_render": _prompt_render_payload()},
                ],
                "implement": {
                    "prompt_render": _prompt_render_payload("per_phase:implement"),
                },
                "rounds": [
                    {
                        "round": 1,
                        "prompt_render_review": _prompt_render_payload(
                            "per_phase:review_changes",
                        ),
                        "prompt_render_repair": _prompt_render_payload(
                            "per_phase:implement",
                        ),
                    },
                ],
            },
        )
        run = _write_run_dir(tmp_path, meta=meta)
        bundle = collect_evidence(run)
        for entry in bundle["prompt_render"]:
            assert set(entry.keys()) == set(REQUIRED_PROMPT_RENDER_KEYS), (
                f"entry keys drift: extra="
                f"{set(entry.keys()) - set(REQUIRED_PROMPT_RENDER_KEYS)} "
                f"missing="
                f"{set(REQUIRED_PROMPT_RENDER_KEYS) - set(entry.keys())}"
            )

    def test_validate_bundle_rejects_missing_top_level_prompt_render(
        self,
    ) -> None:
        # Hand-build a minimal bundle that omits ``prompt_render``;
        # validate_bundle must reject it.
        bundle = {
            "schema_version": "1",
            "run_id": "r",
            "run_dir": "/tmp/run",
            "status": "done",
            "created_at": "2026-05-17T00:00:00+00:00",
            "task": "t",
            "profile": "feature",
            "plan": {
                "source": "absent",
                "short_summary": "",
                "planning_context": "",
                "subtask_count": 0,
                "has_contract": False,
                "goal": None,
                "acceptance_criteria": [],
                "owned_files": [],
                "commands_to_run": [],
                "risks": [],
                "review_focus": [],
                "mcp_context": {},
            },
            "phases": [],
            "gates": [],
            "commands": [],
            "artifacts": [],
            "metrics": {
                "total_tokens": 0, "total_tokens_in": 0, "total_tokens_out": 0,
                "total_duration_s": 0.0, "total_rounds": 0,
            },
            "errors": [],
            "raw_events_path": "/tmp/run/events.jsonl",
        }
        with pytest.raises(EvidenceSchemaError, match="prompt_render"):
            validate_bundle(bundle)

    def test_validate_bundle_rejects_entry_missing_required_key(
        self,
    ) -> None:
        # Construct a bundle whose ``prompt_render[0]`` is missing the
        # ``wire_chars`` key; strict validation must reject it.
        bundle = {
            "schema_version": "1",
            "run_id": "r",
            "run_dir": "/tmp/run",
            "status": "done",
            "created_at": "2026-05-17T00:00:00+00:00",
            "task": "t",
            "profile": "feature",
            "plan": {
                "source": "absent",
                "short_summary": "",
                "planning_context": "",
                "subtask_count": 0,
                "has_contract": False,
                "goal": None,
                "acceptance_criteria": [],
                "owned_files": [],
                "commands_to_run": [],
                "risks": [],
                "review_focus": [],
                "mcp_context": {},
            },
            "phases": [],
            "gates": [],
            "commands": [],
            "artifacts": [],
            "metrics": {
                "total_tokens": 0, "total_tokens_in": 0, "total_tokens_out": 0,
                "total_duration_s": 0.0, "total_rounds": 0,
            },
            "errors": [],
            "prompt_render": [
                {
                    "phase": "plan", "phase_key": "plan",
                    "trace_surface": "plan",
                    "attempt": 1, "round": None,
                    "continue_session": False,
                    "source_path": "phases.plan[0].prompt_render",
                    "render_mode": "full", "session_split": "per_phase",
                    "execution_mode": "linear", "surface_id": None,
                    "surface_count": 1,
                    "session_scope": "per_phase:plan",
                    "session_run_id": "r", "session_runtime": "x",
                    "session_model": "m", "provider_session_id": None,
                    "selected_count": 1, "omitted_count": 0,
                    "prefix_hash": "a", "payload_hash": "b",
                    # wire_chars missing
                },
            ],
            "raw_events_path": "/tmp/run/events.jsonl",
        }
        with pytest.raises(EvidenceSchemaError, match=r"prompt_render\[0\]"):
            validate_bundle(bundle)


# ── 3. Raw-leak guard ────────────────────────────────────────────────────────


class TestNoRawPromptText:
    @pytest.mark.parametrize(
        "forbidden_key",
        ["prompt", "prompt_text", "wire_prompt", "body",
         "selected_part_keys", "omitted_part_keys"],
    )
    def test_evidence_summary_never_carries_raw_text_or_part_keys(
        self, tmp_path: Path, forbidden_key: str,
    ) -> None:
        # The summary is counts-only. ``selected_part_keys`` /
        # ``omitted_part_keys`` arrays live in the durable trace
        # (M12-C2) but never on the evidence-facing surface — they
        # could embed user-visible identifiers and would be
        # uninterpretable without orcho-side context.
        meta = _base_meta(
            extra_phases={
                "implement": {
                    "prompt_render": _prompt_render_payload("per_phase:implement"),
                },
            },
        )
        run = _write_run_dir(tmp_path, meta=meta)
        bundle = collect_evidence(run)
        for entry in bundle["prompt_render"]:
            assert forbidden_key not in entry, (
                f"evidence entry leaked forbidden key {forbidden_key!r}"
            )


# ── 4. Exceptions never fabricated ───────────────────────────────────────────


class TestExceptionsNeverFabricated:
    def test_hypothesis_validate_hypothesis_final_acceptance_omitted(
        self, tmp_path: Path,
    ) -> None:
        meta = _base_meta(
            extra_phases={
                # All three documented exceptions.
                "hypothesis": {
                    "prompt_render": _prompt_render_payload("per_phase:hypothesis"),
                },
                "validate_hypothesis": {
                    "prompt_render": _prompt_render_payload(
                        "per_phase:validate_hypothesis",
                    ),
                },
                "final_acceptance": {
                    "prompt_render": _prompt_render_payload(
                        "per_phase:final_acceptance",
                    ),
                },
                # One covered surface alongside, to prove the filter
                # is targeted and not "drop everything".
                "implement": {
                    "prompt_render": _prompt_render_payload("per_phase:implement"),
                },
            },
        )
        run = _write_run_dir(tmp_path, meta=meta)
        bundle = collect_evidence(run)
        phases = [entry["phase"] for entry in bundle["prompt_render"]]
        assert phases == ["implement"]


# ── 5. Hermetic mock-pipeline integration test (QA P1) ───────────────────────


class TestHermeticMockPipelineIntegration:
    """End-to-end: run a mock pipeline, write a real run_dir, then
    feed it through ``collect_evidence`` and verify the wired
    ``prompt_render`` section. This is the QA-required guard against
    wiring bugs that unit tests miss.
    """

    def test_mock_run_produces_evidence_prompt_render_with_real_data(
        self, tmp_path: Path,
    ) -> None:
        # The hermetic mock infrastructure lives next to the snapshot
        # parity suite; reuse its setup utilities so this integration
        # test rides on a proven hermetic harness.
        import random

        from agents.protocols import SessionMode
        from agents.runtimes._strategy import (
            MockAgentProvider,
            make_mock_phase_config,
        )
        from core.observability import logging as _core_logging
        from pipeline.evidence.schema import validate_bundle
        from pipeline.project_orchestrator import run_pipeline

        # Reset observability globals so prior tests cannot leak
        # ``is_sub_pipeline()`` state into this hermetic run.
        _core_logging._progress_log = None

        from tests.conftest import init_git_repo
        project = tmp_path / "proj"
        init_git_repo(project)
        output_dir = tmp_path / "runs" / "m12_c3_run"
        output_dir.mkdir(parents=True)

        random.seed(0xC0FFEE)
        run_pipeline(
            task="m12-c3 integration test",
            project_dir=str(project),
            max_rounds=1,
            output_dir=output_dir,
            dry_run=False,
            provider=MockAgentProvider(),
            phase_config=make_mock_phase_config(),
            session_mode=SessionMode.STATELESS,
            profile_name="feature",
        )

        # 1. Snapshot meta.json BEFORE collect_evidence.
        meta_path = output_dir / "meta.json"
        assert meta_path.is_file()
        before = meta_path.read_bytes()

        # 2. Collect evidence.
        bundle = collect_evidence(output_dir)

        # 3. AFTER snapshot — collector must be read-only.
        after = meta_path.read_bytes()
        assert before == after, (
            "collect_evidence mutated meta.json — extractor must be "
            "read-only"
        )

        # 4. Schema passes — required key plus strict entry shape.
        validate_bundle(bundle)

        # 5. Real data, not synthetic — the mock pipeline produces
        # plan + validate_plan + implement traces under the advanced
        # profile, so the section must be non-empty.
        section = bundle["prompt_render"]
        assert section, "evidence['prompt_render'] empty after a real mock run"
        phases = {entry["phase"] for entry in section}
        # Covered surfaces present (round-level surfaces depend on
        # whether the mock review/repair loop fired; plan + implement
        # are always present in the advanced profile).
        assert {"plan", "validate_plan", "implement"} <= phases

        # 6. No exceptions sneak in.
        assert not (
            phases & {"hypothesis", "validate_hypothesis", "final_acceptance"}
        ), f"exception phases leaked into evidence: {phases}"

        # 7. No raw text anywhere in the evidence section.
        encoded = json.dumps(section)
        for forbidden in ("wire_prompt", "prompt_text", "\"body\""):
            assert forbidden not in encoded, (
                f"evidence prompt_render leaked {forbidden!r}"
            )

        # 8. M12 fallbacks/stamps present on every entry. Most surfaces still
        # rely on the linear fallback; subtask_dag implement stamps its real
        # execution mode and surface count.
        for entry in section:
            assert entry["execution_mode"] in {"linear", "subtask_dag"}
            assert entry["surface_id"] is None
            assert entry["surface_count"] >= 1
        implement_entries = [e for e in section if e["phase"] == "implement"]
        assert implement_entries
        assert implement_entries[0]["execution_mode"] == "subtask_dag"


# ── M12-C5: strict schema (extras + types) ───────────────────────────────────


def _valid_prompt_render_entry() -> dict:
    """Minimal valid entry for ``evidence["prompt_render"]``."""
    return {
        "phase": "implement",
        "phase_key": "implement",
        "trace_surface": "implement",
        "attempt": None,
        "round": None,
        "continue_session": False,
        "source_path": "phases.implement.prompt_render",
        "render_mode": "full",
        "session_split": "per_phase",
        "execution_mode": "linear",
        "surface_id": None,
        "surface_count": 1,
        "session_scope": "per_phase:implement",
        "session_run_id": "r",
        "session_runtime": "agents.runtimes.claude.ClaudeAgent",
        "session_model": "claude-opus-4-7",
        "provider_session_id": None,
        "selected_count": 1,
        "omitted_count": 0,
        "delta_dropped_count": 0,
        "prefix_hash": "abc",
        "payload_hash": "def",
        "wire_chars": 1024,
    }


def _bundle_with(entry: dict) -> dict:
    """Compose a minimal v1 bundle wrapping a single ``prompt_render`` entry."""
    return {
        "schema_version": "1",
        "run_id": "r",
        "run_dir": "/tmp/run",
        "status": "done",
        "created_at": "2026-05-17T00:00:00+00:00",
        "task": "t",
        "profile": "feature",
        "plan": {
            "source": "absent",
            "short_summary": "",
            "planning_context": "",
            "subtask_count": 0,
            "has_contract": False,
            "goal": None,
            "acceptance_criteria": [],
            "owned_files": [],
            "commands_to_run": [],
            "risks": [],
            "review_focus": [],
            "mcp_context": {},
        },
        "phases": [],
        "gates": [],
        "commands": [],
        "artifacts": [],
        "metrics": {
            "total_tokens": 0, "total_tokens_in": 0, "total_tokens_out": 0,
            "total_duration_s": 0.0, "total_rounds": 0,
        },
        "errors": [],
        "prompt_render": [entry],
        "raw_events_path": "/tmp/run/events.jsonl",
    }


class TestStrictSchemaRejectsExtras:
    """Closed schema: any key outside ``REQUIRED_PROMPT_RENDER_KEYS``
    is a writer bug or a leak attempt and must be rejected."""

    @pytest.mark.parametrize(
        "leak_key",
        [
            "prompt",
            "prompt_text",
            "wire_prompt",
            "body",
            "selected_part_keys",
            "omitted_part_keys",
            # Source-shape artifacts the projection strips.
            "session_key",
            "physical_session_key",
        ],
    )
    def test_forbidden_key_rejected(self, leak_key: str) -> None:
        entry = _valid_prompt_render_entry()
        entry[leak_key] = "any value"
        bundle = _bundle_with(entry)
        with pytest.raises(EvidenceSchemaError, match="forbidden key"):
            validate_bundle(bundle)

    @pytest.mark.parametrize(
        "extra_key",
        ["custom_field", "x_debug", "future_unknown"],
    )
    def test_benign_extra_key_still_rejected(self, extra_key: str) -> None:
        # Even benign-looking extra keys must be rejected. The
        # contract is closed; downstream consumers depend on the
        # exact key set.
        entry = _valid_prompt_render_entry()
        entry[extra_key] = "any value"
        bundle = _bundle_with(entry)
        with pytest.raises(EvidenceSchemaError, match="unexpected key"):
            validate_bundle(bundle)


class TestStrictSchemaTypeChecking:
    """Type contract — every field has a declared shape."""

    @pytest.mark.parametrize(
        ("field", "bad_value"),
        [
            ("wire_chars", None),
            ("wire_chars", "1024"),
            ("wire_chars", True),  # bool is not int for our purposes
            ("surface_count", "one"),
            ("surface_count", None),
            ("selected_count", "1"),
            ("omitted_count", 1.5),
        ],
    )
    def test_int_field_wrong_type_rejected(
        self, field: str, bad_value: object,
    ) -> None:
        entry = _valid_prompt_render_entry()
        entry[field] = bad_value
        bundle = _bundle_with(entry)
        with pytest.raises(
            EvidenceSchemaError, match=f"{field} must be int",
        ):
            validate_bundle(bundle)

    @pytest.mark.parametrize(
        ("field", "bad_value"),
        [
            ("phase", None),
            ("phase", 42),
            ("trace_surface", 0),
            ("render_mode", None),
            ("execution_mode", 1),
            ("prefix_hash", None),
        ],
    )
    def test_str_field_wrong_type_rejected(
        self, field: str, bad_value: object,
    ) -> None:
        entry = _valid_prompt_render_entry()
        entry[field] = bad_value
        bundle = _bundle_with(entry)
        with pytest.raises(
            EvidenceSchemaError, match=f"{field} must be str",
        ):
            validate_bundle(bundle)

    @pytest.mark.parametrize(
        ("field", "bad_value"),
        [
            ("attempt", "1"),
            ("attempt", 1.0),
            ("round", "two"),
            ("round", True),
        ],
    )
    def test_optional_int_field_wrong_type_rejected(
        self, field: str, bad_value: object,
    ) -> None:
        entry = _valid_prompt_render_entry()
        entry[field] = bad_value
        bundle = _bundle_with(entry)
        with pytest.raises(
            EvidenceSchemaError, match=f"{field} must be int",
        ):
            validate_bundle(bundle)

    @pytest.mark.parametrize(
        ("field", "bad_value"),
        [
            ("surface_id", 42),
            ("session_scope", 0),
            ("session_run_id", []),
            ("provider_session_id", True),
        ],
    )
    def test_optional_str_field_wrong_type_rejected(
        self, field: str, bad_value: object,
    ) -> None:
        entry = _valid_prompt_render_entry()
        entry[field] = bad_value
        bundle = _bundle_with(entry)
        with pytest.raises(
            EvidenceSchemaError, match=f"{field} must be str",
        ):
            validate_bundle(bundle)

    def test_valid_entry_accepted(self) -> None:
        # Sanity counter-test: the canonical valid entry passes
        # strict validation cleanly so the rejection tests above
        # fail for the right reason.
        bundle = _bundle_with(_valid_prompt_render_entry())
        validate_bundle(bundle)  # must not raise


# ── M12-C5: on-disk evidence.json contains prompt_render ─────────────────────


class TestEvidenceJsonOnDisk:
    """The hermetic integration test in C3 verifies the in-memory
    bundle. C5 adds a finalized-on-disk check: ``write_bundle``
    serializes the bundle to ``evidence.json`` and re-validates it
    against the strict schema. Persistence is the surface real
    consumers read, so the contract must hold there too.
    """

    def test_written_evidence_json_carries_prompt_render_section(
        self, tmp_path: Path,
    ) -> None:
        # Hermetic mock pipeline as in C3 — produces a real run_dir
        # with meta.json that ``write_bundle`` can consume.
        import random

        from agents.protocols import SessionMode
        from agents.runtimes._strategy import (
            MockAgentProvider,
            make_mock_phase_config,
        )
        from core.observability import logging as _core_logging
        from pipeline.evidence.bundle import write_bundle
        from pipeline.project_orchestrator import run_pipeline

        _core_logging._progress_log = None

        from tests.conftest import init_git_repo
        project = tmp_path / "proj"
        init_git_repo(project)
        output_dir = tmp_path / "runs" / "m12_c5_run"
        output_dir.mkdir(parents=True)

        random.seed(0xC5C5)
        run_pipeline(
            task="m12-c5 on-disk evidence",
            project_dir=str(project),
            max_rounds=1,
            output_dir=output_dir,
            dry_run=False,
            provider=MockAgentProvider(),
            phase_config=make_mock_phase_config(),
            session_mode=SessionMode.STATELESS,
            profile_name="feature",
        )

        json_path, _md_path = write_bundle(output_dir)
        assert json_path.is_file()

        # Read the serialized bundle BACK from disk — this is the
        # surface external consumers (orcho-mcp, dashboards, SDK
        # users) actually see.
        on_disk = json.loads(json_path.read_text(encoding="utf-8"))
        assert "prompt_render" in on_disk
        assert isinstance(on_disk["prompt_render"], list)
        assert on_disk["prompt_render"], (
            "on-disk evidence missing prompt_render entries after a "
            "real mock run"
        )

        # Re-validate the on-disk bundle through strict schema.
        validate_bundle(on_disk)

        # Spot-check shape on the persisted entry.
        first = on_disk["prompt_render"][0]
        assert first["execution_mode"] == "linear"
        assert first["surface_count"] == 1
        assert "wire_prompt" not in first
        assert "selected_part_keys" not in first
