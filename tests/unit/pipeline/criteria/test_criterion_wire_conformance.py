# SPDX-License-Identifier: Apache-2.0
"""The persisted wire is the contract: canonical order and strict validation.

Two things the in-memory conformance tests cannot see:

* what the engine and SDK writers actually put on disk — a serializer that
  sorts keys silently rewrites ``CRITERION_STATE_ORDER`` in ``evidence.json``;
* whether the published SDK examples satisfy core's own schemas.
"""
from __future__ import annotations

import json

import pytest

from core.contracts.criteria import (
    CriterionSchemaError,
    validate_acceptance_criteria,
)
from pipeline.criterion_decisions import record_human_decision
from pipeline.criterion_matrix import CRITERION_STATE_ORDER
from pipeline.evidence.bundle import EVIDENCE_FILE_NAME, dumps_bundle, write_bundle
from pipeline.evidence.collector import collect_evidence
from pipeline.evidence.schema import EvidenceSchemaError, validate_bundle
from pipeline.plan_artifacts import write_parsed_plan_artifact
from pipeline.plan_parser import parse_plan
from pipeline.verification_ledger import GateLedgerRow
from pipeline.verification_ledger_store import ScheduledGateLedger, write_ledger
from sdk.criterion_examples import EXAMPLE_NAMES, criterion_matrix_example
from sdk.criterion_matrix import canonical_criterion_json, get_criterion_matrix
from sdk.errors import EvidenceInvalid

RUN_ID = "20260101_000000"

_UNIT = {"command": "unit", "hook": "after_phase", "phase": "implement"}
_LINT = {"command": "lint", "hook": "after_phase", "phase": "implement"}

_PLAN = {
    "short_summary": "s",
    "planning_context": "p",
    "acceptance_criteria": [
        {"id": "C1", "intent": "regression tested", "verify": "executable",
         "gate_refs": [dict(_UNIT)]},
        {"id": "C2", "intent": "hygiene proven", "verify": "executable",
         "gate_refs": [dict(_LINT)]},
        {"id": "C3", "intent": "docs read coherently", "verify": "agent_assertion"},
        {"id": "C4", "intent": "operator accepts migration", "verify": "human",
         "human_instructions": "Migrate once and record the outcome."},
        {"id": "C5", "intent": "operator accepts rollback", "verify": "human",
         "human_instructions": "Roll back once and record the outcome."},
    ],
    "tasks": [
        {"id": "t1", "goal": "g1", "acceptance_refs": ["C1"]},
        {"id": "t2", "goal": "g2", "acceptance_refs": ["C2"]},
    ],
}


def _row(command: str, disposition: str) -> GateLedgerRow:
    return GateLedgerRow(
        gate=command, hook="after_phase", phase="implement",
        timing="after implement", run_mode="auto", gate_sets=("smoke",),
        condition="always", disposition=disposition,
        # A passing gate only proves a criterion when a canonical receipt
        # backs it (ADR 0188 §3), so the fixture records one.
        receipt_evidence=f"verification_command_receipts/{command}.json",
    )



def _run_with_plan_event(tmp_path, plan, event_tasks=None):
    """A run dir whose ``plan.parsed`` event carries ``plan`` (and its tasks).

    ``event_tasks`` overrides only the event payload's ``subtasks``, so a test
    can corrupt the projected edge without writing an invalid plan artifact
    (which the plan schema would reject first, before the SDK reader is ever
    reached).
    """
    d = tmp_path / RUN_ID
    d.mkdir()
    (d / "meta.json").write_text(
        json.dumps({"run_id": RUN_ID, "status": "success"}), encoding="utf-8",
    )
    (d / "events.jsonl").write_text(
        json.dumps({
            "seq": 1,
            "ts": "2026-01-01T00:00:00",
            "kind": "plan.parsed",
            "phase": "PLAN",
            "payload": {
                "source": "json",
                "short_summary": plan["short_summary"],
                "planning_context": plan["planning_context"],
                "subtask_count": len(plan["tasks"]),
                "has_contract": True,
                "goal": "",
                "acceptance_criteria": plan["acceptance_criteria"],
                "owned_files": [],
                "commands_to_run": [],
                "risks": [],
                "review_focus": [],
                "mcp_context": [],
                "subtasks": plan["tasks"] if event_tasks is None else event_tasks,
            },
        }) + "\n",
        encoding="utf-8",
    )
    write_parsed_plan_artifact(d, parse_plan(json.dumps(plan)), attempt=1)
    return d

@pytest.fixture()
def run_dir(tmp_path):
    """A mixed-state run: all three classes, five distinct states."""
    d = tmp_path / RUN_ID
    d.mkdir()
    (d / "meta.json").write_text(
        json.dumps({"run_id": RUN_ID, "status": "success"}), encoding="utf-8",
    )
    # A real ``plan.parsed`` event, so the bundle's plan record carries the
    # typed criteria and the matrix is cross-checked against them rather than
    # only against itself.
    (d / "events.jsonl").write_text(
        json.dumps({
            "seq": 1,
            "ts": "2026-01-01T00:00:00",
            "kind": "plan.parsed",
            "phase": "PLAN",
            "payload": {
                "source": "json",
                "short_summary": _PLAN["short_summary"],
                "planning_context": _PLAN["planning_context"],
                "subtask_count": len(_PLAN["tasks"]),
                "has_contract": True,
                "goal": "",
                "acceptance_criteria": _PLAN["acceptance_criteria"],
                "owned_files": [],
                "commands_to_run": [],
                "risks": [],
                "review_focus": [],
                "mcp_context": [],
                "subtasks": _PLAN["tasks"],
            },
        }) + "\n",
        encoding="utf-8",
    )
    write_parsed_plan_artifact(d, parse_plan(json.dumps(_PLAN)), attempt=1)
    write_ledger(d, ScheduledGateLedger(rows=(
        _row("unit", "executed_pass"),
        _row("lint", "executed_fail"),
    )))
    from pipeline.criterion_claims import record_criterion_claim

    record_criterion_claim(
        d, run_id=RUN_ID, criterion_id="C3", actor="reviewer",
        statement="The workflow reads coherently.",
    )
    record_human_decision(d, run_id=RUN_ID, criterion_id="C4", decision="accept")
    return d


def _persisted_matrix(path):
    return json.loads(path.read_text(encoding="utf-8"))["criterion_matrix"]


# ── F2: the physically written file keeps the canonical order ────────────────


class TestPersistedOrder:
    def test_the_engine_writer_preserves_the_canonical_state_order(
        self, run_dir,
    ) -> None:
        write_bundle(run_dir)
        counts = _persisted_matrix(
            run_dir / EVIDENCE_FILE_NAME,
        )["summary"]["counts_by_state"]
        assert list(counts) == [
            "proven", "failed", "advisory", "accepted", "pending",
        ]
        assert list(counts) == [s for s in CRITERION_STATE_ORDER if s in counts]
        # Alphabetical order is what a sort_keys writer would have produced.
        assert list(counts) != sorted(counts)

    def test_the_engine_file_is_byte_equivalent_to_the_sdk_canonical_json(
        self, run_dir,
    ) -> None:
        write_bundle(run_dir)
        on_disk = _persisted_matrix(run_dir / EVIDENCE_FILE_NAME)
        in_memory = collect_evidence(run_dir)["criterion_matrix"]
        assert canonical_criterion_json(on_disk) == canonical_criterion_json(
            in_memory,
        )

    def test_the_sdk_writer_preserves_the_canonical_state_order(
        self, tmp_path, run_dir,
    ) -> None:
        from sdk.evidence import collect_evidence as sdk_collect_evidence, write_evidence_bundle

        runs_dir = run_dir.parent
        bundle = sdk_collect_evidence(RUN_ID, runs_dir=runs_dir)
        out = tmp_path / "out"
        json_path, _md = write_evidence_bundle(bundle, out)

        on_disk = _persisted_matrix(json_path)
        assert list(on_disk["summary"]["counts_by_state"]) == [
            "proven", "failed", "advisory", "accepted", "pending",
        ]
        assert canonical_criterion_json(on_disk) == canonical_criterion_json(
            get_criterion_matrix(RUN_ID, runs_dir=runs_dir),
        )

    def test_narrow_sdk_reader_ignores_unrelated_corrupt_evidence_without_plan(
        self, tmp_path,
    ) -> None:
        run_dir = tmp_path / RUN_ID
        run_dir.mkdir()
        (run_dir / "meta.json").write_text(
            json.dumps({"run_id": RUN_ID, "status": "success"}),
            encoding="utf-8",
        )
        (run_dir / "scheduled_gate_ledger.json").write_text(
            '{"not":"the current ledger contract"}\n', encoding="utf-8",
        )

        assert get_criterion_matrix(RUN_ID, runs_dir=tmp_path) is None

    def test_narrow_sdk_reader_treats_pre_contract_plan_as_absent(
        self, tmp_path,
    ) -> None:
        run_dir = tmp_path / RUN_ID
        run_dir.mkdir()
        (run_dir / "meta.json").write_text(
            json.dumps({"run_id": RUN_ID, "status": "interrupted"}),
            encoding="utf-8",
        )
        (run_dir / "parsed_plan.json").write_text(
            json.dumps({
                "artifact_version": 1,
                "plan": {"tasks": [{"id": "T1", "spec": "legacy"}]},
            }),
            encoding="utf-8",
        )

        assert get_criterion_matrix(RUN_ID, runs_dir=tmp_path) is None

    def test_narrow_sdk_reader_rejects_corrupt_criterion_authority_with_plan(
        self, tmp_path,
    ) -> None:
        run_dir = _run_with_plan_event(tmp_path, _PLAN)
        (run_dir / "scheduled_gate_ledger.json").write_text(
            '{"not":"the current ledger contract"}\n', encoding="utf-8",
        )

        with pytest.raises(EvidenceInvalid, match="criterion matrix"):
            get_criterion_matrix(RUN_ID, runs_dir=tmp_path)

    def test_every_other_section_stays_key_sorted(self, run_dir) -> None:
        write_bundle(run_dir)
        body = json.loads(
            (run_dir / EVIDENCE_FILE_NAME).read_text(encoding="utf-8"),
        )
        top = [k for k in body if k != "criterion_matrix"]
        assert top == sorted(top)
        assert list(body["plan"]) == sorted(body["plan"])

    def test_a_bundle_without_a_matrix_is_byte_identical_to_sorted_dump(
        self, tmp_path,
    ) -> None:
        body = {"zz": 1, "aa": {"b": 2, "a": [{"y": 1, "x": 2}]}}
        assert dumps_bundle(body) == json.dumps(
            body, indent=2, sort_keys=True, ensure_ascii=False,
        ) + "\n"


# ── F1: the published examples pass core's own schemas ───────────────────────


class TestPublicExamplesAreSchemaValid:
    @pytest.mark.parametrize("name", EXAMPLE_NAMES)
    def test_every_example_matrix_validates(self, name) -> None:
        matrix = criterion_matrix_example(name)
        if matrix is None:
            return
        validate_bundle(_bundle_with(matrix))

    @pytest.mark.parametrize("name", EXAMPLE_NAMES)
    def test_every_example_gate_ref_validates_as_a_plan_criterion(
        self, name,
    ) -> None:
        matrix = criterion_matrix_example(name)
        if matrix is None:
            return
        payload = [
            {
                "id": row["criterion_id"],
                "intent": row["intent"],
                "verify": "executable",
                "gate_refs": row["method"]["gate_refs"],
            }
            for row in matrix["rows"]
            if row["method"]["kind"] == "gates"
        ]
        # ``multi_gate`` carries a before_delivery identity, which is exactly
        # the shape the plan schema used to reject.
        validate_acceptance_criteria(payload)

    def test_the_multi_gate_example_really_carries_a_before_delivery_identity(
        self,
    ) -> None:
        refs = criterion_matrix_example("multi_gate")["rows"][0]["method"][
            "gate_refs"
        ]
        assert {"command": "smoke", "hook": "before_delivery", "phase": ""} in refs


# ── F3: the validator rejects a contradictory matrix ─────────────────────────


def _bundle_with(matrix: dict) -> dict:
    """A bundle whose plan projection is *absent*.

    These fixtures exercise matrix shape only, so they must not also claim an
    accepted plan: ``source="absent"`` is what a bundle with no ``plan.parsed``
    event really carries, and it is the one case the plan cross-check skips.
    """
    return {
        "schema_version": "1",
        "run_id": RUN_ID,
        "run_dir": "/tmp/x",
        "status": "done",
        "created_at": "2026-01-01T00:00:00+00:00",
        "task": "t",
        "profile": "feature",
        "plan": {
            "source": "absent", "short_summary": "", "planning_context": "",
            "subtask_count": 0, "has_contract": False, "goal": None,
            "acceptance_criteria": [], "owned_files": [], "commands_to_run": [],
            "risks": [], "review_focus": [], "mcp_context": [],
        },
        "phases": [], "gates": [], "commands": [], "artifacts": [],
        "metrics": {
            "total_tokens": 0, "total_tokens_in": 0, "total_tokens_out": 0,
            "total_duration_s": 0.0, "total_rounds": 0,
        },
        "errors": [], "prompt_render": [], "raw_events_path": "/tmp/x/e.jsonl",
        "criterion_matrix": matrix,
    }


def _pending_human_row(**overrides):
    row = {
        "criterion_id": "C1", "intent": "i", "verify": "human",
        "executors": ["human"],
        "method": {"kind": "manual", "instructions": "do it"},
        "proof_refs": [], "state": "pending", "reason": "r", "blocking": True,
    }
    row.update(overrides)
    return row


def _matrix(rows, **summary_overrides):
    tally: dict[str, int] = {}
    for row in rows:
        tally[row["state"]] = tally.get(row["state"], 0) + 1
    summary = {
        "total": len(rows),
        "blocking_open": sum(1 for r in rows if r["blocking"]),
        "ready": not any(r["blocking"] for r in rows),
        "counts_by_state": {
            s: tally[s] for s in CRITERION_STATE_ORDER if s in tally
        },
        "pending_human_ids": [
            r["criterion_id"] for r in rows
            if r["verify"] == "human" and r["state"] == "pending"
        ],
    }
    summary.update(summary_overrides)
    return {"rows": rows, "summary": summary}


class TestValidatorRejectsContradictions:
    def test_the_baseline_matrix_is_valid(self) -> None:
        validate_bundle(_bundle_with(_matrix([_pending_human_row()])))

    @pytest.mark.parametrize(
        ("overrides", "match"),
        [
            ({"total": 0}, "total is 0"),
            ({"blocking_open": -1}, "must be a non-negative integer"),
            ({"total": True}, "non-negative integer"),
            ({"blocking_open": 0, "ready": True}, "blocking_open is 0"),
            ({"ready": "yes"}, "ready must be a bool"),
            ({"counts_by_state": {"pending": 2}}, "does not match the rows"),
            ({"counts_by_state": {}}, "does not match the rows"),
            ({"pending_human_ids": []}, "pending human criteria in plan order"),
            ({"pending_human_ids": ["C1", "C1"]}, "pending human criteria"),
        ],
    )
    def test_a_summary_that_disagrees_with_its_rows_is_rejected(
        self, overrides, match,
    ) -> None:
        matrix = _matrix([_pending_human_row()], **overrides)
        with pytest.raises(EvidenceSchemaError, match=match):
            validate_bundle(_bundle_with(matrix))

    def test_pending_human_ids_must_follow_plan_order(self) -> None:
        rows = [
            _pending_human_row(criterion_id="C1"),
            _pending_human_row(criterion_id="C2"),
        ]
        matrix = _matrix(rows, pending_human_ids=["C2", "C1"])
        with pytest.raises(EvidenceSchemaError, match="plan order"):
            validate_bundle(_bundle_with(matrix))

    @pytest.mark.parametrize(
        ("overrides", "match"),
        [
            ({"executors": []}, "non-empty list"),
            ({"executors": ["  "]}, "non-empty strings"),
            ({"executors": [3]}, "non-empty strings"),
            ({"criterion_id": ""}, "criterion_id must be a non-empty string"),
            ({"intent": None}, "intent must be a non-empty string"),
            ({"reason": None}, "reason must be a string"),
            ({"blocking": "yes"}, "blocking must be a bool"),
            ({"verify": "made_up"}, "verify must be one of"),
            ({"proof_refs": [{"kind": "receipt", "id": ""}]}, "non-empty string"),
            ({"proof_refs": "nope"}, "proof_refs must be a list"),
            ({"method": {"kind": "manual", "instructions": " "}},
             "instructions must be a non-empty string"),
        ],
    )
    def test_a_malformed_row_is_rejected(self, overrides, match) -> None:
        matrix = _matrix([_pending_human_row(**overrides)])
        with pytest.raises(EvidenceSchemaError, match=match):
            validate_bundle(_bundle_with(matrix))

    def test_a_state_outside_its_verification_class_is_rejected(self) -> None:
        matrix = _matrix([_pending_human_row(state="proven", blocking=True)])
        with pytest.raises(EvidenceSchemaError, match="not valid for verify"):
            validate_bundle(_bundle_with(matrix))

    def test_a_method_kind_that_contradicts_the_class_is_rejected(self) -> None:
        matrix = _matrix([_pending_human_row(
            method={"kind": "inspection"},
        )])
        with pytest.raises(EvidenceSchemaError, match="must be 'manual'"):
            validate_bundle(_bundle_with(matrix))

    def test_a_blocking_flag_that_contradicts_the_state_is_rejected(self) -> None:
        matrix = _matrix([_pending_human_row(blocking=False)])
        with pytest.raises(EvidenceSchemaError, match="blocking must be True"):
            validate_bundle(_bundle_with(matrix))

    def test_an_advisory_row_can_never_be_marked_blocking(self) -> None:
        row = {
            "criterion_id": "C1", "intent": "i", "verify": "agent_assertion",
            "executors": ["reviewer"], "method": {"kind": "inspection"},
            "proof_refs": [], "state": "advisory", "reason": "", "blocking": True,
        }
        with pytest.raises(EvidenceSchemaError, match="blocking must be False"):
            validate_bundle(_bundle_with(_matrix([row])))

    def test_a_row_gate_ref_is_validated_by_the_criterion_schema(self) -> None:
        row = {
            "criterion_id": "C1", "intent": "i", "verify": "executable",
            "executors": ["t1"],
            "method": {"kind": "gates", "gate_refs": [
                {"command": "unit", "hook": "after_phase", "phase": ""},
            ]},
            "proof_refs": [], "state": "missing", "reason": "r",
            "blocking": True,
        }
        with pytest.raises(EvidenceSchemaError, match="gate_refs is invalid"):
            validate_bundle(_bundle_with(_matrix([row])))

    def test_a_before_delivery_row_gate_ref_is_accepted(self) -> None:
        row = {
            "criterion_id": "C1", "intent": "i", "verify": "executable",
            "executors": ["t1"],
            "method": {"kind": "gates", "gate_refs": [
                {"command": "smoke", "hook": "before_delivery", "phase": ""},
            ]},
            "proof_refs": [], "state": "missing", "reason": "r",
            "blocking": True,
        }
        validate_bundle(_bundle_with(_matrix([row])))

    def test_the_criterion_schema_error_is_reported_as_a_schema_error(
        self,
    ) -> None:
        with pytest.raises(CriterionSchemaError):
            validate_acceptance_criteria([{
                "id": "C1", "intent": "i", "verify": "executable",
                "gate_refs": [
                    {"command": "unit", "hook": "after_phase", "phase": ""},
                ],
            }])


class TestMatrixMustMatchTheAcceptedPlan:
    """ADR 0188: exactly one row per criterion, in plan order.

    Validating rows only against themselves lets a bundle drop a blocking
    criterion and still report ``ready``.
    """

    PLAN_CRITERIA = [
        {"id": "C1", "intent": "operator accepts", "verify": "human",
         "human_instructions": "do it"},
        {"id": "C2", "intent": "reads coherently", "verify": "agent_assertion"},
    ]

    def _bundle(self, rows, criteria=None, source="json"):
        bundle = _bundle_with(_matrix(rows))
        bundle["plan"]["source"] = source
        bundle["plan"]["acceptance_criteria"] = (
            self.PLAN_CRITERIA if criteria is None else criteria
        )
        return bundle

    def _rows(self):
        return [
            _pending_human_row(criterion_id="C1", intent="operator accepts"),
            {
                "criterion_id": "C2", "intent": "reads coherently",
                "verify": "agent_assertion", "executors": ["reviewer"],
                "method": {"kind": "inspection"}, "proof_refs": [],
                "state": "pending", "reason": "", "blocking": False,
            },
        ]

    def test_a_matching_matrix_validates(self) -> None:
        validate_bundle(self._bundle(self._rows()))

    def test_an_empty_ready_matrix_against_a_non_empty_plan_is_rejected(
        self,
    ) -> None:
        with pytest.raises(EvidenceSchemaError, match="exactly one row per"):
            validate_bundle(self._bundle([]))

    def test_a_missing_row_is_rejected(self) -> None:
        with pytest.raises(EvidenceSchemaError, match="exactly one row per"):
            validate_bundle(self._bundle(self._rows()[:1]))

    def test_a_duplicated_row_is_rejected(self) -> None:
        rows = self._rows()
        with pytest.raises(EvidenceSchemaError, match="repeats criterion_id"):
            validate_bundle(self._bundle([rows[0], dict(rows[0])]))

    def test_a_reordered_row_is_rejected(self) -> None:
        rows = list(reversed(self._rows()))
        with pytest.raises(EvidenceSchemaError, match="rows follow plan order"):
            validate_bundle(self._bundle(rows))

    def test_a_substituted_intent_is_rejected(self) -> None:
        rows = self._rows()
        rows[0]["intent"] = "something else entirely"
        with pytest.raises(EvidenceSchemaError, match=r"\.intent is"):
            validate_bundle(self._bundle(rows))

    def test_a_substituted_verification_class_is_rejected(self) -> None:
        rows = self._rows()
        rows[0] = {
            **rows[0], "verify": "agent_assertion",
            "method": {"kind": "inspection"}, "blocking": False,
        }
        with pytest.raises(EvidenceSchemaError, match=r"\.verify is"):
            validate_bundle(self._bundle(rows))

    def test_a_method_that_does_not_project_the_plan_is_rejected(self) -> None:
        rows = self._rows()
        rows[0]["method"] = {"kind": "manual", "instructions": "something else"}
        with pytest.raises(EvidenceSchemaError, match="does not project"):
            validate_bundle(self._bundle(rows))

    def test_executable_gate_refs_must_project_the_plan_refs(self) -> None:
        criteria = [{
            "id": "C1", "intent": "regression tested", "verify": "executable",
            "gate_refs": [dict(_UNIT)],
        }]
        rows = [{
            "criterion_id": "C1", "intent": "regression tested",
            "verify": "executable", "executors": ["t1"],
            "method": {"kind": "gates", "gate_refs": [dict(_LINT)]},
            "proof_refs": [], "state": "missing", "reason": "r",
            "blocking": True,
        }]
        with pytest.raises(EvidenceSchemaError, match="does not project"):
            validate_bundle(self._bundle(rows, criteria))

    def test_only_an_absent_projection_skips_the_cross_check(self) -> None:
        """``source="absent"`` is the one case with nothing to check against.

        A bundle with no ``plan.parsed`` event carries no accepted-plan
        projection, so its rows cannot be cross-checked. This is *not* the same
        as a projected plan that declares no criteria.
        """
        validate_bundle(self._bundle(self._rows(), [], source="absent"))

    def test_an_authoritative_empty_plan_requires_an_empty_matrix(self) -> None:
        """A projected plan with ``acceptance_criteria: []`` is a real claim.

        The explicit empty matrix is the only matrix that matches it; an
        undeclared row would be a phantom criterion for SDK and MCP.
        """
        validate_bundle(self._bundle([], []))
        undeclared = [{
            "criterion_id": "C1", "intent": "never declared",
            "verify": "agent_assertion", "executors": ["reviewer"],
            "method": {"kind": "inspection"}, "proof_refs": [],
            "state": "pending", "reason": "", "blocking": False,
        }]
        with pytest.raises(EvidenceSchemaError, match="exactly one row per"):
            validate_bundle(self._bundle(undeclared, []))

    @pytest.mark.parametrize("source", ["json", "markdown"])
    def test_every_projected_source_is_authoritative(self, source) -> None:
        undeclared = [{
            "criterion_id": "C1", "intent": "never declared",
            "verify": "agent_assertion", "executors": ["reviewer"],
            "method": {"kind": "inspection"}, "proof_refs": [],
            "state": "pending", "reason": "", "blocking": False,
        }]
        with pytest.raises(EvidenceSchemaError, match="exactly one row per"):
            validate_bundle(self._bundle(undeclared, [], source=source))


class TestProofKindsAndCardinality:
    """Which proof a ``(verify, state)`` pair may — and must — carry."""

    def _executable(self, **overrides):
        row = {
            "criterion_id": "C1", "intent": "i", "verify": "executable",
            "executors": ["t1"],
            "method": {"kind": "gates", "gate_refs": [dict(_UNIT)]},
            "proof_refs": [{"kind": "receipt", "id": "r-1"}],
            "state": "proven", "reason": "", "blocking": False,
        }
        row.update(overrides)
        return row

    def test_a_proven_executable_with_a_receipt_validates(self) -> None:
        validate_bundle(_bundle_with(_matrix([self._executable()])))

    def test_a_proven_executable_without_a_receipt_is_rejected(self) -> None:
        row = self._executable(proof_refs=[])
        with pytest.raises(EvidenceSchemaError, match="not proof"):
            validate_bundle(_bundle_with(_matrix([row])))

    def test_a_proven_multi_gate_row_needs_a_receipt_per_identity(self) -> None:
        row = self._executable(
            method={"kind": "gates", "gate_refs": [dict(_UNIT), dict(_LINT)]},
        )
        with pytest.raises(EvidenceSchemaError, match="not proof"):
            validate_bundle(_bundle_with(_matrix([row])))

    def test_an_executable_row_may_only_cite_receipts(self) -> None:
        row = self._executable(
            state="missing", blocking=True, reason="r",
            proof_refs=[{"kind": "claim", "id": "claim-1"}],
        )
        with pytest.raises(EvidenceSchemaError, match="only cite receipts"):
            validate_bundle(_bundle_with(_matrix([row])))

    def test_an_advisory_row_must_cite_a_claim_or_finding(self) -> None:
        row = {
            "criterion_id": "C1", "intent": "i", "verify": "agent_assertion",
            "executors": ["reviewer"], "method": {"kind": "inspection"},
            "proof_refs": [], "state": "advisory", "reason": "",
            "blocking": False,
        }
        with pytest.raises(EvidenceSchemaError, match="cites no claim"):
            validate_bundle(_bundle_with(_matrix([row])))

    def test_an_advisory_row_may_not_cite_a_receipt(self) -> None:
        row = {
            "criterion_id": "C1", "intent": "i", "verify": "agent_assertion",
            "executors": ["reviewer"], "method": {"kind": "inspection"},
            "proof_refs": [{"kind": "receipt", "id": "r-1"}],
            "state": "advisory", "reason": "", "blocking": False,
        }
        with pytest.raises(EvidenceSchemaError, match="claims or findings"):
            validate_bundle(_bundle_with(_matrix([row])))

    def test_a_pending_agent_row_may_not_cite_proof(self) -> None:
        row = {
            "criterion_id": "C1", "intent": "i", "verify": "agent_assertion",
            "executors": ["reviewer"], "method": {"kind": "inspection"},
            "proof_refs": [{"kind": "claim", "id": "claim-1"}],
            "state": "pending", "reason": "", "blocking": False,
        }
        with pytest.raises(EvidenceSchemaError, match="pending but cites proof"):
            validate_bundle(_bundle_with(_matrix([row])))

    @pytest.mark.parametrize("state", ["accepted", "rejected"])
    def test_a_decided_human_row_cites_exactly_one_head_decision(
        self, state,
    ) -> None:
        row = _pending_human_row(
            state=state, blocking=state != "accepted",
            proof_refs=[{"kind": "human_decision", "id": "hd-C1-2"}],
        )
        validate_bundle(_bundle_with(_matrix([row])))

    @pytest.mark.parametrize(
        "proof_refs",
        [
            [],
            [{"kind": "claim", "id": "claim-1"}],
            [
                {"kind": "human_decision", "id": "hd-C1-1"},
                {"kind": "human_decision", "id": "hd-C1-2"},
            ],
        ],
    )
    def test_a_decided_human_row_rejects_any_other_proof(self, proof_refs) -> None:
        row = _pending_human_row(
            state="accepted", blocking=False, proof_refs=proof_refs,
        )
        with pytest.raises(
            EvidenceSchemaError, match="exactly one human_decision",
        ):
            validate_bundle(_bundle_with(_matrix([row])))

    def test_a_pending_human_row_may_not_cite_a_decision(self) -> None:
        row = _pending_human_row(
            proof_refs=[{"kind": "human_decision", "id": "hd-C1-1"}],
        )
        with pytest.raises(EvidenceSchemaError, match="pending but cites proof"):
            validate_bundle(_bundle_with(_matrix([row])))


def test_a_real_run_bundle_still_validates(run_dir) -> None:
    bundle = collect_evidence(run_dir)
    # The cross-check is really engaged on this bundle, not skipped.
    assert [c["id"] for c in bundle["plan"]["acceptance_criteria"]] == [
        "C1", "C2", "C3", "C4", "C5",
    ]
    validate_bundle(bundle)


def test_a_real_bundle_that_drops_a_blocking_row_is_rejected(run_dir) -> None:
    """The scenario the row-only validator used to accept."""
    bundle = collect_evidence(run_dir)
    bundle["criterion_matrix"] = {
        "rows": [],
        "summary": {
            "total": 0, "blocking_open": 0, "ready": True,
            "counts_by_state": {}, "pending_human_ids": [],
        },
    }
    with pytest.raises(EvidenceSchemaError, match="exactly one row per"):
        validate_bundle(bundle)


# ── the per-task half of the traceability contract (SDK projection) ──────────


def test_plan_summary_projects_the_full_task_reference_graph(run_dir) -> None:
    """``task_acceptance_refs`` is the public reader for the plan's edges.

    Consumers (notably the MCP plan slice) must be able to answer "which task
    owns C1?" from the SDK alone. Reading the durable ``parsed_plan.json``
    themselves is exactly the compensation the contract forbids: it splits one
    contract across two readers that can disagree.
    """
    from sdk.evidence_slices import get_plan_summary

    summary = get_plan_summary(
        RUN_ID, runs_dir=run_dir.parent, cwd=None,
    )

    assert summary.task_acceptance_refs == (
        {"task_id": "t1", "acceptance_refs": ["C1"]},
        {"task_id": "t2", "acceptance_refs": ["C2"]},
    )
    # Every referenced id resolves against the criteria in the same summary —
    # one plan, one graph.
    declared = {c["id"] for c in summary.acceptance_criteria}
    for task in summary.task_acceptance_refs:
        assert set(task["acceptance_refs"]) <= declared


def test_a_task_owning_no_criterion_is_present_with_an_empty_list(
    tmp_path,
) -> None:
    """"Owns nothing" and "missing from the projection" must stay different."""
    from sdk.evidence_slices import get_plan_summary

    plan = json.loads(json.dumps(_PLAN))
    plan["tasks"] = [
        {"id": "t1", "goal": "g1", "acceptance_refs": ["C1"]},
        {"id": "t2", "goal": "g2", "acceptance_refs": ["C2"]},
        {"id": "t3", "goal": "documentation only"},
    ]
    run_dir = _run_with_plan_event(tmp_path, plan)

    summary = get_plan_summary(RUN_ID, runs_dir=run_dir.parent, cwd=None)

    assert summary.task_acceptance_refs == (
        {"task_id": "t1", "acceptance_refs": ["C1"]},
        {"task_id": "t2", "acceptance_refs": ["C2"]},
        {"task_id": "t3", "acceptance_refs": []},
    )


@pytest.mark.parametrize(
    ("tasks", "fragment"),
    [
        ([{"goal": "no id"}], "must be a non-empty string"),
        ([{"id": "", "goal": "blank id"}], "must be a non-empty string"),
        ([{"id": "t1", "acceptance_refs": "C1"}], "must be a list of criterion ids"),
        ([{"id": "t1", "acceptance_refs": [1]}], "must be a list of criterion ids"),
        ([{"id": "t1", "acceptance_refs": ["C1", "C1"]}], "repeats criterion id"),
        # The evidence plan record keeps only object-shaped subtasks, so a
        # non-object entry never reaches this reader at all — it simply
        # disappears. The shortfall against the plan's own ``subtask_count``
        # is what makes that drop loud instead of silent.
        (
            [{"id": "t1", "acceptance_refs": ["C1"]}, "not an object"],
            "reference graph would be partial",
        ),
    ],
)
def test_a_malformed_task_edge_fails_closed(tmp_path, tasks, fragment) -> None:
    """A corrupt edge raises — it never degrades to an empty graph.

    Silence would be the worst outcome available: the criteria stay visible,
    so a consumer renders a fully typed plan while the edges saying who owns
    what have quietly vanished.
    """
    from sdk.errors import EvidenceInvalid
    from sdk.evidence_slices import get_plan_summary

    plan = json.loads(json.dumps(_PLAN))
    run_dir = _run_with_plan_event(tmp_path, plan, event_tasks=tasks)

    with pytest.raises(EvidenceInvalid, match=fragment):
        get_plan_summary(RUN_ID, runs_dir=run_dir.parent, cwd=None)
