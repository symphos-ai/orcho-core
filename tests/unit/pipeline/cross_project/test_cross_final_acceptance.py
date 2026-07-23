"""Unit tests for ADR 0025 Phase 3: cross_final_acceptance gate.

Covers:
  * precondition collection (each of the five blocker classes);
  * synthesised release verdict shape + contract_status derivation;
  * agent path prompt threading (release_json only, no review_json);
  * dry-run synthesised release output round-trips parse_release;
  * usage capture on agent path vs precondition path;
  * result projection onto the dual-shape phase_log entry.

Runner-level ordering / event / acceptance tests live in
test_cross_orchestrator.py and tests/acceptance/test_full_mock_flow.py
respectively — this file pins the gate module in isolation.
"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from pipeline.cross_project.execution_graph import CrossExecutionGraphNodeKind
from pipeline.cross_project.execution_graph_state import (
    CrossExecutionGraphNodeState,
    CrossExecutionGraphReason,
    CrossExecutionGraphState,
    CrossExecutionGraphStatus,
)
from pipeline.cross_project.final_acceptance import (
    CrossFinalAcceptanceContext,
    build_context,
    result_to_phase_log_entry,
    run_cross_final_acceptance,
)
from pipeline.run_state.cross_parent import (
    ChildFacts,
    CrossParentFacts,
    Observation,
    reduce_cross_parent_state,
)
from pipeline.runtime import CrossGatePolicy, CrossGateRunPolicy, CrossGateSkipPolicy

# ── Fixtures ────────────────────────────────────────────────────────────────


def _approved_child_release(*, alias_specific: bool = True) -> dict:
    """A persisted final_acceptance dict matching the Phase 1
    FinalAcceptanceAdapter dual-shape output."""
    return {
        "verdict":         "APPROVED",
        "approved":        True,
        "short_summary":   "ok",
        "findings":        [],
        "ship_ready":      True,
        "release_blockers":  [],
        "verification_gaps": [],
        "contract_status": {
            "task_contract": "satisfied",
            "interfaces":    "compatible",
            "persistence":   "safe",
            "tests":         "sufficient",
        },
    }


def _rejected_child_release(
    *, why: str = "P1: regression in module X",
    file: str | None = "src/x.py",
    line: int | None = 42,
) -> dict:
    blocker = {
        "id": "R1", "severity": "P1",
        "title": "Release blocker",
        "body": why,
        "required_fix": "Restore prior behavior.",
        "why_blocks_release": "Production callers rely on the prior shape.",
    }
    if file is not None:
        blocker["file"] = file
    if line is not None:
        blocker["line"] = line
    return {
        "verdict":         "REJECTED",
        "approved":        False,
        "short_summary":   why,
        "findings":        [blocker.copy()],
        "ship_ready":      False,
        "release_blockers": [blocker],
        "verification_gaps": [],
        "contract_status": {
            "task_contract": "incomplete",
            "interfaces":    "broken",
            "persistence":   "safe",
            "tests":         "weak",
        },
    }


def _child_with_final(fa_entry: dict) -> dict:
    return {"phases": {"final_acceptance": fa_entry}}


def _approved_contract_check() -> dict:
    return {
        "approved": True, "verdict": "APPROVED",
        "short_summary": "Interfaces align.",
        "findings": [],
    }


def _rejected_contract_check() -> dict:
    return {
        "approved": False, "verdict": "REJECTED",
        "short_summary": "Producer/consumer field drift.",
        "findings": [],
    }


def _child_readiness_contract_check() -> dict:
    return {
        "approved": False,
        "verdict": "NOT_EVALUABLE",
        "not_evaluable": True,
        "source": "precondition",
        "reason": "child_readiness",
        "child_status": "halted",
        "child_reason": "operator requested stop",
        "findings": [],
        "risks": [],
        "checks": [],
    }


def _build(aliases, *, projects=None, contracts=None, plan="# cross plan") -> CrossFinalAcceptanceContext:
    return build_context(
        cross_plan_markdown=plan,
        aliases=tuple(aliases),
        session_phases={
            "projects": projects or {},
            "contract_check": contracts or {},
        },
        common_cwd="/tmp",
    )


# ── Recording codex stub ────────────────────────────────────────────────────


class _RecordingCodex:
    """Records prompts handed to ``invoke()`` so threading tests can
    inspect the actual final prompt."""

    model = "stub-codex"
    session_id: str | None = None

    def __init__(self, response: str | None = None) -> None:
        self._response = response or _approved_release_payload()
        self.prompts: list[str] = []
        # Mock-runtime usage tracker the orchestrator's
        # _capture_invoke_usage expects.
        self.last_estimated_tokens_in = 100
        self.last_estimated_tokens_out = 50

    def invoke(
        self, prompt: str, cwd: str, *,
        mutates_artifacts: bool = False,
        continue_session: bool = False,
        attachments: tuple = (),
    ) -> str:
        self.prompts.append(prompt)
        return self._response


def _approved_release_payload() -> str:
    return json.dumps({
        "verdict":            "APPROVED",
        "ship_ready":         True,
        "short_summary":      "Coordinated change is ship-ready.",
        "release_blockers":   [],
        "verification_gaps":  [],
        "contract_status": {
            "task_contract": "satisfied",
            "interfaces":    "compatible",
            "persistence":   "safe",
            "tests":         "sufficient",
        },
    })


# ─────────────────────────────────────────────────────────────────────────────
# Precondition path
# ─────────────────────────────────────────────────────────────────────────────


class TestPreconditionMissingChild:
    def test_missing_alias_in_projects_blocks(self) -> None:
        ctx = _build(("api", "web"), projects={})
        res = run_cross_final_acceptance(ctx, codex=None, dry_run=False)
        assert res.source == "precondition"
        assert not res.parsed.approved
        ids = [b.id for b in res.parsed.release_blockers]
        assert ids == ["CFA_MISSING_CHILD_api", "CFA_MISSING_CHILD_web"]

    def test_crashed_child_with_status_failed_blocks(self) -> None:
        ctx = _build(("api",), projects={
            "api": {"status": "failed", "error": "RuntimeError: x"},
        })
        res = run_cross_final_acceptance(ctx, codex=None, dry_run=False)
        assert res.source == "precondition"
        blocker = res.parsed.release_blockers[0]
        assert blocker.id == "CFA_MISSING_CHILD_api"
        # Error hint is folded into the body so log greppers can find it.
        assert "RuntimeError" in blocker.body

    def test_halted_readiness_is_not_a_contract_rejection(self) -> None:
        ctx = _build(
            ("api",),
            projects={
                "api": {
                    "status": "halted",
                    "halt_reason": "operator requested stop",
                    "phases": {},
                },
            },
            contracts={"api": _child_readiness_contract_check()},
        )
        res = run_cross_final_acceptance(ctx, codex=None, dry_run=False)

        assert res.source == "precondition"
        assert [b.id for b in res.parsed.release_blockers] == [
            "CFA_MISSING_CHILD_api",
        ]
        body = res.parsed.release_blockers[0].body
        assert "halted" in body
        assert "operator requested stop" in body
        assert "CFA_CONTRACT_REJECTED" not in body
        assert res.parsed.contract_status.interfaces == "not_applicable"


class TestPreconditionMissingRelease:
    def test_child_without_final_acceptance_phase_blocks(self) -> None:
        ctx = _build(("api",), projects={"api": {"phases": {}}})
        res = run_cross_final_acceptance(ctx, codex=None, dry_run=False)
        assert res.source == "precondition"
        assert [b.id for b in res.parsed.release_blockers] == [
            "CFA_MISSING_RELEASE_api",
        ]

    def test_canonical_success_without_release_entry_still_blocks(self) -> None:
        state = reduce_cross_parent_state(
            CrossParentFacts(("api",), (ChildFacts("api", Observation.PRESENT, "done"),))
        )
        ctx = _build(("api",), projects={"api": {"status": "done", "phases": {}}})
        ctx = replace(ctx, child_states={"api": state.children[0]})
        res = run_cross_final_acceptance(ctx, codex=None, dry_run=False)

        assert [b.id for b in res.parsed.release_blockers] == ["CFA_MISSING_RELEASE_api"]

    def test_child_with_partial_release_surface_blocks(self) -> None:
        # final_acceptance present but missing release fields (legacy /
        # parse-error path).
        partial = {"verdict": "APPROVED", "findings": []}
        ctx = _build(("api",), projects={
            "api": _child_with_final(partial),
        })
        res = run_cross_final_acceptance(ctx, codex=None, dry_run=False)
        assert res.source == "precondition"
        assert "CFA_MISSING_RELEASE_api" in [
            b.id for b in res.parsed.release_blockers
        ]


class TestPreconditionChildRejected:
    def test_ship_ready_false_blocks(self) -> None:
        ctx = _build(("api",), projects={
            "api": _child_with_final(_rejected_child_release()),
        })
        res = run_cross_final_acceptance(ctx, codex=None, dry_run=False)
        assert res.source == "precondition"
        blocker = res.parsed.release_blockers[0]
        assert blocker.id == "CFA_CHILD_REJECTED_api"
        assert blocker.severity == "P1"

    def test_carries_underlying_file_line(self) -> None:
        ctx = _build(("api",), projects={
            "api": _child_with_final(
                _rejected_child_release(file="src/x.py", line=42),
            ),
        })
        res = run_cross_final_acceptance(ctx, codex=None, dry_run=False)
        blocker = res.parsed.release_blockers[0]
        assert blocker.file == "src/x.py"
        assert blocker.line == 42


class TestPreconditionContractRejected:
    def test_rejected_contract_blocks(self) -> None:
        ctx = _build(
            ("api",),
            projects={
                "api": _child_with_final(_approved_child_release()),
            },
            contracts={"api": _rejected_contract_check()},
        )
        res = run_cross_final_acceptance(ctx, codex=None, dry_run=False)
        assert res.source == "precondition"
        ids = [b.id for b in res.parsed.release_blockers]
        assert "CFA_CONTRACT_REJECTED_api" in ids


def _skipped_contract_check(on_skip: str, *, reason: str = "operator_decision",
                            source: str = "operator",
                            feedback: str = "") -> dict:
    entry = {
        "approved": False,
        "verdict": "SKIPPED",
        "skipped": True,
        "skip_reason": reason,
        "on_skip": on_skip,
        "source": source,
        "short_summary": f"contract_check skipped ({reason}).",
        "findings": [],
        "risks": [],
        "checks": [],
    }
    if feedback:
        entry["operator_feedback"] = feedback
    return entry


class TestPreconditionContractSkipped:
    """``contract_check`` may be skipped (policy or operator). The
    cross_final_acceptance preconditions carry ``on_skip`` forward:
    ``block`` → release blocker, ``allow_with_gap`` → verification gap,
    ``allow`` → no entry."""

    def test_skipped_block_produces_blocker(self) -> None:
        ctx = _build(
            ("api",),
            projects={
                "api": _child_with_final(_approved_child_release()),
            },
            contracts={"api": _skipped_contract_check("block")},
        )
        res = run_cross_final_acceptance(ctx, codex=None, dry_run=False)
        assert res.source == "precondition"
        assert res.parsed.verdict == "REJECTED"
        ids = [b.id for b in res.parsed.release_blockers]
        assert "CFA_CONTRACT_SKIPPED_BLOCK_api" in ids
        # No gap on the block path.
        assert res.parsed.verification_gaps == ()

    def test_skipped_allow_with_gap_produces_gap_not_blocker(self) -> None:
        ctx = _build(
            ("api",),
            projects={
                "api": _child_with_final(_approved_child_release()),
            },
            contracts={"api": _skipped_contract_check("allow_with_gap")},
        )
        res = run_cross_final_acceptance(ctx, codex=None, dry_run=False)
        # Precondition path: no blockers + at least one gap → APPROVED
        # with the gap surfaced. The gate does not invoke Codex.
        assert res.source == "precondition"
        assert res.parsed.verdict == "APPROVED"
        assert res.parsed.ship_ready is True
        gap_risks = [g.risk for g in res.parsed.verification_gaps]
        assert any("[api]" in r for r in gap_risks), gap_risks
        # No CFA_CONTRACT_SKIPPED_BLOCK in this path.
        ids = [b.id for b in res.parsed.release_blockers]
        assert not any(
            i.startswith("CFA_CONTRACT_SKIPPED_BLOCK") for i in ids
        )

    def test_skipped_allow_produces_no_entry(self) -> None:
        ctx = _build(
            ("api",),
            projects={
                "api": _child_with_final(_approved_child_release()),
            },
            contracts={"api": _skipped_contract_check("allow")},
        )
        # All preconditions clear → gate moves to the agent path. We
        # don't need a codex stub if dry_run=True synthesises APPROVED.
        res = run_cross_final_acceptance(ctx, codex=None, dry_run=True)
        # No skipped-related blocker / gap should appear.
        ids = [b.id for b in res.parsed.release_blockers]
        assert not any(
            i.startswith("CFA_CONTRACT_SKIPPED_BLOCK") for i in ids
        )
        assert res.parsed.verification_gaps == ()

    def test_rejected_still_blocks_when_skipped_is_a_sibling(self) -> None:
        ctx = _build(
            ("api", "web"),
            projects={
                "api": _child_with_final(_approved_child_release()),
                "web": _child_with_final(_approved_child_release()),
            },
            contracts={
                "api": _skipped_contract_check("allow_with_gap"),
                "web": _rejected_contract_check(),
            },
        )
        res = run_cross_final_acceptance(ctx, codex=None, dry_run=False)
        # The REJECTED sibling still produces a blocker → release stays
        # REJECTED, the api gap is carried forward in the synthesised
        # release.
        assert res.parsed.verdict == "REJECTED"
        ids = [b.id for b in res.parsed.release_blockers]
        assert "CFA_CONTRACT_REJECTED_web" in ids
        risks = [g.risk for g in res.parsed.verification_gaps]
        assert any("[api]" in r for r in risks)


class TestPreconditionParseError:
    def test_child_final_acceptance_parse_error_blocks(self) -> None:
        broken = _approved_child_release()
        broken["parse_error"] = "ReleaseSchemaError: missing ship_ready"
        ctx = _build(("api",), projects={
            "api": _child_with_final(broken),
        })
        res = run_cross_final_acceptance(ctx, codex=None, dry_run=False)
        assert res.source == "precondition"
        assert [b.id for b in res.parsed.release_blockers] == [
            "CFA_PARSE_ERROR_api",
        ]

    def test_contract_check_parse_error_blocks(self) -> None:
        cc_broken = _approved_contract_check()
        cc_broken["parse_error"] = "ReviewParseError: bad JSON"
        ctx = _build(
            ("api",),
            projects={
                "api": _child_with_final(_approved_child_release()),
            },
            contracts={"api": cc_broken},
        )
        res = run_cross_final_acceptance(ctx, codex=None, dry_run=False)
        assert res.source == "precondition"
        assert "CFA_PARSE_ERROR_api" in [
            b.id for b in res.parsed.release_blockers
        ]


class TestPreconditionSynthesisedContractStatus:
    def test_contract_reject_marks_interfaces_broken(self) -> None:
        ctx = _build(
            ("api",),
            projects={"api": _child_with_final(_approved_child_release())},
            contracts={"api": _rejected_contract_check()},
        )
        res = run_cross_final_acceptance(ctx, codex=None, dry_run=False)
        cs = res.parsed.contract_status.to_dict()
        assert cs["interfaces"] == "broken"

    def test_child_reject_marks_task_contract_incomplete(self) -> None:
        ctx = _build(("api",), projects={
            "api": _child_with_final(_rejected_child_release()),
        })
        res = run_cross_final_acceptance(ctx, codex=None, dry_run=False)
        cs = res.parsed.contract_status.to_dict()
        assert cs["task_contract"] == "incomplete"

    def test_parse_error_marks_task_contract_unclear(self) -> None:
        broken = _approved_child_release()
        broken["parse_error"] = "ReleaseSchemaError"
        ctx = _build(("api",), projects={
            "api": _child_with_final(broken),
        })
        res = run_cross_final_acceptance(ctx, codex=None, dry_run=False)
        cs = res.parsed.contract_status.to_dict()
        assert cs["task_contract"] == "unclear"


class TestPreconditionResultShape:
    def test_synthesised_parsed_passes_schema(self) -> None:
        """Synthesised ParsedRelease must satisfy release_schema
        coherence — APPROVED/REJECTED invariants, ship_ready
        consistency, contract_status enum values."""
        from core.contracts.release_schema import validate_release_dict

        ctx = _build(("api",), projects={
            "api": _child_with_final(_rejected_child_release()),
        })
        res = run_cross_final_acceptance(ctx, codex=None, dry_run=False)
        # Round-trip the parsed result back through the schema validator.
        payload = {
            "verdict": res.parsed.verdict,
            "ship_ready": res.parsed.ship_ready,
            "short_summary": res.parsed.short_summary,
            "release_blockers": res.parsed.blockers_as_dicts(),
            "verification_gaps": res.parsed.gaps_as_dicts(),
            "contract_status": res.parsed.contract_status.to_dict(),
        }
        validate_release_dict(payload)

    def test_phase_log_entry_carries_dual_shape(self) -> None:
        ctx = _build(("api",), projects={
            "api": _child_with_final(_rejected_child_release()),
        })
        res = run_cross_final_acceptance(ctx, codex=None, dry_run=False)
        entry = result_to_phase_log_entry(res)
        # Review-shape mirror (existing consumers).
        for k in ("approved", "verdict", "short_summary", "findings"):
            assert k in entry
        # Release-shape (Phase 1 contract).
        for k in ("ship_ready", "release_blockers",
                  "verification_gaps", "contract_status"):
            assert k in entry
        # Cross-gate-only book-keeping.
        assert entry["source"] == "precondition"
        # Findings mirror projected from release blockers.
        assert [f["id"] for f in entry["findings"]] == [
            "CFA_CHILD_REJECTED_api",
        ]

    def test_precondition_markdown_uses_configured_russian_language(
        self, monkeypatch,
    ) -> None:
        from pipeline.cross_project import final_acceptance as cfa

        monkeypatch.setattr(
            cfa.config.AppConfig,
            "load",
            classmethod(lambda _cls: type("Cfg", (), {"task_language": "Russian"})()),
        )
        ctx = _build(("web",), projects={
            "web": _child_with_final(_rejected_child_release(
                why="Сборка не выполнена.",
            )),
        })

        res = run_cross_final_acceptance(ctx, codex=None, dry_run=False)

        assert "# Системная проверка релиза" in res.rendered
        assert "**Вердикт:** REJECTED" in res.rendered
        assert "**Готово к релизу:** нет" in res.rendered
        assert "## Блокеры релиза" in res.rendered
        assert "Проект [web] не готов к релизу" in res.rendered
        assert "Системный релиз заблокирован" in res.parsed.short_summary
        assert "System release blocked" not in res.rendered


# ─────────────────────────────────────────────────────────────────────────────
# Agent path — prompt threading, dry-run, parse errors
# ─────────────────────────────────────────────────────────────────────────────


class TestAgentPath:
    def test_happy_path_invokes_codex_and_parses_approved(self) -> None:
        codex = _RecordingCodex()
        ctx = _build(
            ("api",),
            projects={"api": _child_with_final(_approved_child_release())},
            contracts={"api": _approved_contract_check()},
        )
        res = run_cross_final_acceptance(ctx, codex=codex, dry_run=False)
        assert res.source == "agent"
        assert res.parsed.approved is True
        assert res.parsed.ship_ready is True
        assert len(codex.prompts) == 1

    def test_prompt_contains_exactly_one_release_json_block(self) -> None:
        """Load-bearing threading invariant: the prompt the cross
        reviewer receives must carry release_json (Phase 1 contract)
        and zero review_json blocks. Without this, the wrapper's
        strip-tail would re-attach review_json and parse_release would
        halt the gate on a shape mismatch."""
        codex = _RecordingCodex()
        ctx = _build(
            ("api",),
            projects={"api": _child_with_final(_approved_child_release())},
            contracts={"api": _approved_contract_check()},
        )
        run_cross_final_acceptance(ctx, codex=codex, dry_run=False)
        prompt = codex.prompts[0]
        assert prompt.count('name="release_json"') == 1
        assert 'name="review_json"' not in prompt
        # Prompt also references the per-project verdicts (so the agent
        # has the context to make a system-level decision).
        assert "[api]" in prompt

    def test_dry_run_synthesises_approved_release_payload(self) -> None:
        ctx = _build(
            ("api",),
            projects={"api": _child_with_final(_approved_child_release())},
            contracts={"api": _approved_contract_check()},
        )
        res = run_cross_final_acceptance(ctx, codex=None, dry_run=True)
        assert res.source == "agent"
        assert res.parsed.approved is True
        assert res.parsed.ship_ready is True
        # No agent prompt invoked — dry-run uses the synthesised payload.

    def test_parse_error_yields_parse_error_source(self) -> None:
        codex = _RecordingCodex(response="this is not JSON")
        ctx = _build(
            ("api",),
            projects={"api": _child_with_final(_approved_child_release())},
            contracts={"api": _approved_contract_check()},
        )
        res = run_cross_final_acceptance(ctx, codex=codex, dry_run=False)
        assert res.source == "parse_error"
        assert res.parsed.approved is False
        assert res.parsed.ship_ready is False
        assert res.parse_error is not None
        assert "JSON" in res.parse_error or "exactly one" in res.parse_error

    def test_parse_error_phase_entry_validates_against_release_schema(
        self,
    ) -> None:
        """The synthesised parse-error stub must satisfy
        ``validate_release_dict``: REJECTED requires at least one
        release_blockers or verification_gaps entry, so the gate
        emits a real CFA_PARSE_ERROR_cross_final_acceptance blocker
        rather than empty tuples.

        Without this, ``result_to_phase_log_entry`` would persist a
        release-shaped dict that downstream evidence collectors
        (release_summary) and any consumer that re-validates the
        release surface would treat as malformed.
        """
        from core.contracts.release_schema import validate_release_dict
        codex = _RecordingCodex(response="this is not JSON")
        ctx = _build(
            ("api",),
            projects={"api": _child_with_final(_approved_child_release())},
            contracts={"api": _approved_contract_check()},
        )
        res = run_cross_final_acceptance(ctx, codex=codex, dry_run=False)
        assert res.source == "parse_error"
        # Direct shape: blockers non-empty so REJECTED is coherent.
        assert len(res.parsed.release_blockers) >= 1
        parse_blocker = res.parsed.release_blockers[0]
        assert parse_blocker.id == "CFA_PARSE_ERROR_cross_final_acceptance"
        assert parse_blocker.severity == "P1"
        # Phase-log projection round-trips through validate_release_dict
        # using the canonical release fields. Build the validator input
        # from the projected entry's release surface only (review-shape
        # mirror keys are dropped — they belong to a different schema).
        entry = result_to_phase_log_entry(res)
        release_payload = {
            "verdict":            entry["verdict"],
            "ship_ready":         entry["ship_ready"],
            "short_summary":      entry["short_summary"],
            "release_blockers":   entry["release_blockers"],
            "verification_gaps":  entry["verification_gaps"],
            "contract_status":    entry["contract_status"],
        }
        validate_release_dict(release_payload)  # raises if malformed

    def test_agent_rejected_verdict_passes_through(self) -> None:
        """Agent emits a well-formed REJECTED payload — gate carries
        it through verbatim (no precondition synthesis)."""
        rejected = json.dumps({
            "verdict":            "REJECTED",
            "ship_ready":         False,
            "short_summary":      "System-level invariant broken.",
            "release_blockers":   [{
                "id": "AGN1", "severity": "P0",
                "title": "Cross system invariant",
                "body": "Producer/consumer drift survived per-alias gates.",
                "required_fix": "Align field sets at the system boundary.",
                "why_blocks_release": "Production data flow would corrupt.",
            }],
            "verification_gaps":  [],
            "contract_status": {
                "task_contract": "incomplete",
                "interfaces":    "broken",
                "persistence":   "safe",
                "tests":         "weak",
            },
        })
        codex = _RecordingCodex(response=rejected)
        ctx = _build(
            ("api",),
            projects={"api": _child_with_final(_approved_child_release())},
            contracts={"api": _approved_contract_check()},
        )
        res = run_cross_final_acceptance(ctx, codex=codex, dry_run=False)
        assert res.source == "agent"
        assert res.parsed.approved is False
        assert [b.id for b in res.parsed.release_blockers] == ["AGN1"]

    def test_agent_markdown_verdict_response_is_parsed(self) -> None:
        """Cross mode accepts the stable rendered release markdown as a
        fallback when a reviewer returns it instead of the JSON object."""
        markdown = """
Вердикт cross-final-acceptance:
# Системная проверка релиза

**Вердикт:** REJECTED

**Готово к релизу:** нет

**Кратко:** **Системный релиз заблокирован: contract mismatch.**

## Блокеры релиза

### CFA_CONTRACT_REJECTED_api [P1] Cross-project contract check отклонил [api]

Cross contract check отклонил diff [api]: producer/consumer field drift.

**Что исправить:** Исправить cross-project contract mismatch в [api].

**Почему это блокирует релиз:** Выпуск сломает callers.

## Статус контракта

- Контракт задачи: satisfied
- Интерфейсы:    broken
- Хранение:   not_applicable
- Тесты:         weak
""".strip()
        codex = _RecordingCodex(response=markdown)
        ctx = _build(
            ("api",),
            projects={"api": _child_with_final(_approved_child_release())},
            contracts={"api": _approved_contract_check()},
        )

        res = run_cross_final_acceptance(ctx, codex=codex, dry_run=False)

        assert res.source == "agent"
        assert res.parse_error is None
        assert res.parsed.source == "markdown"
        assert res.parsed.verdict == "REJECTED"
        assert res.parsed.ship_ready is False
        assert [b.id for b in res.parsed.release_blockers] == [
            "CFA_CONTRACT_REJECTED_api",
        ]
        assert res.parsed.contract_status.interfaces == "broken"


class TestAgentPathDuration:
    def test_agent_invocation_records_duration(self) -> None:
        codex = _RecordingCodex()
        ctx = _build(
            ("api",),
            projects={"api": _child_with_final(_approved_child_release())},
            contracts={"api": _approved_contract_check()},
        )
        res = run_cross_final_acceptance(ctx, codex=codex, dry_run=False)
        # Agent invocation records elapsed time (≥ 0; recording stub
        # is near-instant but the field exists).
        assert res.duration_s >= 0.0
        assert res.source == "agent"

    def test_precondition_path_has_zero_duration(self) -> None:
        ctx = _build(("api",), projects={})
        res = run_cross_final_acceptance(ctx, codex=None, dry_run=False)
        assert res.duration_s == 0.0
        assert res.source == "precondition"


# ─────────────────────────────────────────────────────────────────────────────
# Prompt text plumbing (for token-split estimation on Codex-style runtimes)
# ─────────────────────────────────────────────────────────────────────────────


class TestPromptTextPlumbing:
    """``CrossFinalAcceptanceResult.prompt_text`` carries the reviewer
    prompt back to the orchestrator so it can feed prompt+output into
    ``_capture_invoke_usage`` for token-split estimation on runtimes
    that only surface ``last_tokens_total`` (Codex). Precondition path
    leaves it empty — no model call was made."""

    def test_agent_path_populates_prompt_text(self) -> None:
        codex = _RecordingCodex()
        ctx = _build(
            ("api",),
            projects={"api": _child_with_final(_approved_child_release())},
            contracts={"api": _approved_contract_check()},
        )
        res = run_cross_final_acceptance(ctx, codex=codex, dry_run=False)
        assert res.source == "agent"
        assert res.prompt_text != ""
        # Same prompt the agent received — load-bearing for downstream
        # token estimation.
        assert res.prompt_text == codex.prompts[0]

    def test_parse_error_path_populates_prompt_text(self) -> None:
        codex = _RecordingCodex(response="this is not JSON")
        ctx = _build(
            ("api",),
            projects={"api": _child_with_final(_approved_child_release())},
            contracts={"api": _approved_contract_check()},
        )
        res = run_cross_final_acceptance(ctx, codex=codex, dry_run=False)
        assert res.source == "parse_error"
        assert res.prompt_text != ""
        assert res.prompt_text == codex.prompts[0]

    def test_precondition_path_has_empty_prompt_text(self) -> None:
        ctx = _build(("api",), projects={})
        res = run_cross_final_acceptance(ctx, codex=None, dry_run=False)
        assert res.source == "precondition"
        assert res.prompt_text == ""

    def test_dry_run_has_empty_prompt_text(self) -> None:
        ctx = _build(
            ("api",),
            projects={"api": _child_with_final(_approved_child_release())},
            contracts={"api": _approved_contract_check()},
        )
        res = run_cross_final_acceptance(ctx, codex=None, dry_run=True)
        assert res.source == "agent"  # dry-run still tagged "agent"
        # Dry-run skipped the invoke; no prompt was sent to any model.
        assert res.prompt_text == ""


# ─────────────────────────────────────────────────────────────────────────────
# Event payload contract
# ─────────────────────────────────────────────────────────────────────────────


class TestEventKindRegistration:
    def test_cross_final_acceptance_verdict_registered(self) -> None:
        from core.observability.event_kinds import (
            REQUIRED_PAYLOAD_KEYS,
            EventKind,
        )
        assert EventKind.CROSS_FINAL_ACCEPTANCE_VERDICT.value == (
            "cross_final_acceptance.verdict"
        )
        required = REQUIRED_PAYLOAD_KEYS[
            EventKind.CROSS_FINAL_ACCEPTANCE_VERDICT
        ]
        assert required == frozenset({
            "approved", "verdict", "ship_ready", "source", "short_summary",
        })

    def test_cross_validate_plan_verdict_registered(self) -> None:
        """ADR 0025 Phase 3 amendment: register both cross.* verdict
        events for consistency rather than half-doing it."""
        from core.observability.event_kinds import (
            REQUIRED_PAYLOAD_KEYS,
            EventKind,
        )
        assert EventKind.CROSS_VALIDATE_PLAN_VERDICT.value == (
            "cross_validate_plan.verdict"
        )
        required = REQUIRED_PAYLOAD_KEYS[
            EventKind.CROSS_VALIDATE_PLAN_VERDICT
        ]
        assert required == frozenset({"attempt", "approved"})

    def test_payload_validation_rejects_missing_key(self) -> None:
        from core.observability.event_kinds import (
            EventSchemaError,
            validate_payload,
        )
        # Missing 'source'.
        with pytest.raises(EventSchemaError, match="source"):
            validate_payload("cross_final_acceptance.verdict", {
                "approved": True, "verdict": "APPROVED",
                "ship_ready": True, "short_summary": "ok",
            })


# ── Variant 1: CFA focus points at the worktree review targets ────────


class TestCfaReviewTargets:
    """Variant 1 — the CFA focus must direct the reviewer at the per-alias
    worktree checkouts (where the uncommitted change lives), not the
    pristine source. Without this the cross gate reviews stale code and
    falsely REJECTS an applied fix."""

    def test_focus_renders_worktree_review_targets(self) -> None:
        from pipeline.cross_project.final_acceptance import (
            _build_agent_focus_task,
            build_context,
        )

        ctx = build_context(
            cross_plan_markdown="# plan",
            aliases=("api", "web"),
            session_phases={
                "projects": {
                    "api": _child_with_final(_approved_child_release()),
                    "web": _child_with_final(_approved_child_release()),
                },
                "contract_check": {},
            },
            common_cwd="/run/worktrees",
            review_paths={
                "api": "/run/worktrees/wt_api/checkout",
                "web": "/run/worktrees/wt_web/checkout",
            },
        )
        focus = _build_agent_focus_task(ctx)
        assert "Review targets" in focus
        assert "/run/worktrees/wt_api/checkout" in focus
        assert "/run/worktrees/wt_web/checkout" in focus
        # The instruction must steer the reviewer to the worktrees, not source.
        assert "working tree" in focus.lower()

    def test_focus_omits_section_without_review_paths(self) -> None:
        from pipeline.cross_project.final_acceptance import (
            _build_agent_focus_task,
            build_context,
        )

        ctx = build_context(
            cross_plan_markdown="# plan",
            aliases=("api",),
            session_phases={
                "projects": {"api": _child_with_final(_approved_child_release())},
                "contract_check": {},
            },
            common_cwd="/tmp",
        )
        focus = _build_agent_focus_task(ctx)
        assert "Review targets" not in focus


def test_graph_blocked_cfa_never_invokes_evaluator(tmp_path, monkeypatch) -> None:
    """CFA provider admission is owned by the re-reduced graph state."""
    from types import SimpleNamespace

    from pipeline.cross_project import cfa_gate, parent_state_runtime, session_run

    parent = reduce_cross_parent_state(CrossParentFacts(("core",), ()))
    monkeypatch.setattr(
        parent_state_runtime, "reduce_runtime_cross_parent_state", lambda *_: parent,
    )
    monkeypatch.setattr(
        session_run, "reduce_runtime_cross_execution_graph_state",
        lambda *_: CrossExecutionGraphState((
            CrossExecutionGraphNodeState(
                "cfa", CrossExecutionGraphNodeKind.CROSS_FINAL_ACCEPTANCE,
                CrossExecutionGraphStatus.BLOCKED,
                CrossExecutionGraphReason.DEPENDENCY_BLOCKED,
            ),
        )),
    )
    monkeypatch.setattr(
        cfa_gate, "evaluate_cfa_gate",
        lambda **_: pytest.fail("blocked graph must not invoke CFA evaluator"),
    )
    policy = CrossGatePolicy(
        enabled=True,
        run=CrossGateRunPolicy.ALWAYS,
        on_skip=CrossGateSkipPolicy.ALLOW_WITH_GAP,
    )
    request = SimpleNamespace(projects={"core": tmp_path}, dry_run=False)
    ctx = SimpleNamespace(
        r=SimpleNamespace(banner=lambda *_, **__: None, C=SimpleNamespace(MAGENTA="")),
        session={"projects": {"core": str(tmp_path)}, "phases": {}},
        cross_ckpt={}, run_dir=tmp_path, profile_setup=SimpleNamespace(cfa_gate_policy=policy),
        execution_graph=object(), graph_gate_blocked=False,
    )

    assert session_run._run_release_gate(request, ctx) is False
    assert ctx.graph_gate_blocked is True


def test_graph_blocked_gate_finalizes_failed_without_delivery(tmp_path) -> None:
    """The coordinator's graph denial is terminal rather than a CFA crash."""
    from types import SimpleNamespace

    from pipeline.cross_project.finalization import (
        CrossFinalizationContext,
        _decide_base_status,
    )
    from pipeline.cross_project.session_run import _cross_delivery_plan

    assert _cross_delivery_plan(SimpleNamespace(graph_gate_blocked=True)) == (False, False)
    status, halt_reason, _, _ = _decide_base_status(CrossFinalizationContext(
        run_dir=tmp_path,
        output_dir=False,
        session={"phases": {}},
        projects={},
        max_rounds=1,
        cfa_result=None,
        contract_results={},
        contract_check_failed=False,
        contract_check_failure_reason=None,
        cross_phase_usage={},
        graph_gate_blocked=True,
    ))
    assert (status, halt_reason) == ("failed", "cross_execution_graph_blocked")


def test_resume_reuses_strict_completed_cfa_cache_without_provider(tmp_path) -> None:
    from pipeline.cross_project.cfa_gate import evaluate_cfa_gate

    result = run_cross_final_acceptance(
        _build(
            ("api",),
            projects={"api": _child_with_final(_approved_child_release())},
            contracts={"api": _approved_contract_check()},
        ),
        codex=_RecordingCodex(),
        dry_run=False,
    )
    session = {"phases": {"cross_final_acceptance": result_to_phase_log_entry(result)}}

    resume_provider = _RecordingCodex()
    outcome = evaluate_cfa_gate(
        cfa_ctx=object(),
        codex=resume_provider,
        dry_run=False,
        run_dir=tmp_path,
        session=session,
        cross_ckpt={},
        cross_phase_usage={},
        resume_from="prior",
        output_dir=False,
        terminal=False,
    )

    assert outcome.outcome == "cached_terminal"
    assert outcome.cfa_result.parsed.approved is True
    assert resume_provider.prompts == []


def test_resume_completed_cfa_reaches_delivery_without_provider(tmp_path, monkeypatch) -> None:
    """The coordinator admits a completed CFA solely to restore delivery context."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from pipeline.cross_project import parent_state_runtime, session_run

    cached_result = run_cross_final_acceptance(
        _build(
            ("api",),
            projects={"api": _child_with_final(_approved_child_release())},
            contracts={"api": _approved_contract_check()},
        ),
        codex=_RecordingCodex(),
        dry_run=False,
    )
    parent = reduce_cross_parent_state(CrossParentFacts(
        ("api",),
        (ChildFacts(
            "api", Observation.PRESENT, "done",
            release_verdict="APPROVED", release_ship_ready=True,
        ),),
    ))
    cfa_complete = CrossExecutionGraphNodeState(
        "cfa", CrossExecutionGraphNodeKind.CROSS_FINAL_ACCEPTANCE,
        CrossExecutionGraphStatus.COMPLETED,
        CrossExecutionGraphReason.RUNNER_GATE_COMPLETED,
    )
    monkeypatch.setattr(
        parent_state_runtime, "reduce_runtime_cross_parent_state", lambda *_: parent,
    )
    monkeypatch.setattr(
        session_run, "reduce_runtime_cross_execution_graph_state",
        lambda *_: CrossExecutionGraphState((cfa_complete,)),
    )

    class Provider:
        def invoke(self, *_args, **_kwargs):
            pytest.fail("a completed CFA cache must not invoke the provider")

    policy = CrossGatePolicy(
        enabled=True,
        run=CrossGateRunPolicy.ALWAYS,
        on_skip=CrossGateSkipPolicy.ALLOW_WITH_GAP,
    )
    request = SimpleNamespace(
        task="resume delivery", projects={"api": tmp_path}, dry_run=False,
        resume_from="prior", output_dir=None, max_rounds=1,
    )
    banners: list[tuple] = []
    ctx = SimpleNamespace(
        r=SimpleNamespace(
            banner=lambda *args, **_kwargs: banners.append(args),
            C=SimpleNamespace(MAGENTA=""),
        ),
        session={
            "projects": {"api": str(tmp_path)},
            "phases": {"cross_final_acceptance": result_to_phase_log_entry(cached_result)},
        },
        cross_ckpt={}, run_dir=tmp_path, profile_setup=SimpleNamespace(cfa_gate_policy=policy),
        execution_graph=object(), graph_gate_blocked=False, terminal=False,
        review_agent=Provider(), cross_phase_usage={}, plan_output="", review_common_cwd=tmp_path,
        review_projects={"api": tmp_path}, contract_results={}, contract_check_failed=False,
        contract_check_failure_reason=None, delivery_result=None, child_profile=object(),
    )

    assert session_run._run_release_gate(request, ctx) is False
    assert ctx.graph_gate_blocked is False
    assert ctx.cfa_outcome.outcome == "cached_terminal"
    assert banners == []
    assert session_run._finalize_release_verdict(request, ctx) is False
    with (
        patch("pipeline.cross_project.cross_delivery.run_cross_delivery", return_value=object()) as deliver,
        patch("pipeline.cross_project.finalization.CrossFinalizationContext"),
        patch("pipeline.cross_project.finalization.finalize_cross_run"),
    ):
        session_run._run_delivery_and_finalize(request, ctx)
    deliver.assert_called_once()
