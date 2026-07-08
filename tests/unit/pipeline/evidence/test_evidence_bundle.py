"""REA-3 evidence schema + collector + render.

Three layers:

1. **Schema** — :func:`validate_bundle` rejects malformed bundles and
 accepts placeholder + v1 shapes.
2. **Collector** — composes a v1 bundle from a synthetic run dir
 (events.jsonl + meta.json + metrics.json). Asserts every required
 slot is populated and rollups (phases / gates / commands /
 artifacts / errors) reflect the input events.
3. **Renderer** — markdown render is deterministic and contains the
 key bundle facts.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import pytest

from pipeline.engine.diff_apply_check import DiffApplyCheckResult
from pipeline.evidence import (
    EVIDENCE_FILE_NAME,
    EVIDENCE_MD_FILE_NAME,
    EVIDENCE_SCHEMA_VERSION,
    EVIDENCE_SCHEMA_VERSION_PLACEHOLDER,
    EvidenceSchemaError,
    collect_evidence,
    render_evidence_md,
    validate_bundle,
    write_bundle,
    write_bundle_or_placeholder,
    write_placeholder,
)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _write_run_dir(
    target: Path,
    *,
    events: list[dict],
    meta: dict,
    metrics: dict | None = None,
) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    target.joinpath("events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n",
        encoding="utf-8",
    )
    target.joinpath("meta.json").write_text(
        json.dumps(meta), encoding="utf-8",
    )
    if metrics is not None:
        target.joinpath("metrics.json").write_text(
            json.dumps(metrics), encoding="utf-8",
        )
    return target


def _ev(seq: int, kind: str, **payload) -> dict:
    return {
        "seq": seq,
        "ts": f"2026-05-08T10:00:{seq:02d}.000",
        "kind": kind,
        "phase": payload.pop("phase", None),
        "payload": payload,
    }


def _apply_check(
    status: Literal["pass", "fail", "degraded"],
    reason: str,
) -> dict:
    return DiffApplyCheckResult(
        status=status,
        reason=reason,
        cwd="/p",
        patch_path="/run/diff.patch",
        baseline_ref="abc123",
        command=("git", "apply", "--check", "--cached", "/run/diff.patch"),
    ).to_metadata()


def _golden_run(target: Path) -> Path:
    """A canonical, schema-valid run-dir snapshot.

 Mirrors what the live golden scenario produces — used as the
 happy-path fixture for collector + renderer tests.
 """
    events = [
        _ev(1, "run.start", task="fix bug", run_kind="single_project",
            project="/p", profile="advanced"),
        _ev(2, "phase.start", phase="PLAN", title="PLAN",
            phase_kind="PLAN", attempt=1),
        _ev(3, "plan.parsed", phase="PLAN",
            source="json", subtask_count=3, has_contract=True,
            short_summary="Reject invalid payloads.",
            planning_context="Implementation Plan body...",
            goal="Reject invalid payloads",
            acceptance_criteria=["invalid payload is rejected", "tests pass"],
            acceptance_criteria_count=2, owned_files_count=1,
            owned_files=["calc.py"],
            commands_to_run=["pytest -q"],
            commands_to_run_count=1,
            subtasks=[
                {
                    "id": "t1",
                    "goal": "Add payload validation",
                    "owned_files": ["calc.py"],
                    "done_criteria": ["invalid payload is rejected"],
                },
                {
                    "id": "t2",
                    "goal": "Add regression coverage",
                    "depends_on": ["t1"],
                    "files": ["tests/test_calc.py"],
                    "done_criteria": ["tests pass"],
                },
            ]),
        _ev(4, "phase.end", phase="PLAN", title="PLAN",
            outcome="ok", attempt=1),
        _ev(5, "phase.start", phase="IMPLEMENT", title="BUILD",
            phase_kind="BUILD", attempt=1),
        _ev(6, "gate.start", phase="IMPLEMENT",
            name="tests", gate_kind="computational"),
        _ev(7, "command.start", phase="IMPLEMENT",
            argv_summary="pytest -q", cwd="/p",
            command_kind="tests"),
        _ev(8, "command.end", phase="IMPLEMENT",
            exit_code=0, duration_s=0.42, outcome="ok"),
        _ev(9, "gate.end", phase="IMPLEMENT",
            name="tests", outcome="passed", duration_s=0.42),
        _ev(10, "artifact.created", phase="IMPLEMENT",
            path="/p/.orcho/artifacts/plan.md",
            artifact_kind="plan", size_bytes=1024, attempt=1),
        _ev(11, "phase.end", phase="IMPLEMENT", title="BUILD",
            outcome="ok", attempt=1),
        _ev(12, "run.end", status="done", summary="ok"),
    ]
    meta = {
        "run_id": "GOLDEN_TEST",
        "task": "fix bug",
        "profile": "advanced",
        "status": "done",
        "phases": {
            "plan": [{
                "attempt": 1,
                "output": "Implementation Plan body...",
                "parsed_file_paths": ["calc.py"],
            }],
            "implement": {"output": "BUILD output"},
        },
    }
    metrics = {
        "total_tokens": 1000,
        "total_tokens_in": 800,
        "total_tokens_out": 200,
        "total_duration_s": 1.5,
        "total_rounds": 1,
        "total_retries": 0,
    }
    return _write_run_dir(target, events=events, meta=meta, metrics=metrics)


# ─────────────────────────────────────────────────────────────────────────────
# Schema validator
# ─────────────────────────────────────────────────────────────────────────────


class TestSchemaValidator:
    def test_accepts_v1_bundle(self, tmp_path: Path) -> None:
        bundle = collect_evidence(_golden_run(tmp_path))
        validate_bundle(bundle)        # must not raise

    def test_accepts_placeholder(self) -> None:
        validate_bundle({
            "schema_version": EVIDENCE_SCHEMA_VERSION_PLACEHOLDER,
            "run_id": "x",
            "status": "done",
        })

    def test_rejects_unknown_schema_version(self) -> None:
        with pytest.raises(EvidenceSchemaError, match="schema_version"):
            validate_bundle({"schema_version": "999"})

    def test_rejects_missing_top_level_key(self, tmp_path: Path) -> None:
        bundle = collect_evidence(_golden_run(tmp_path))
        del bundle["plan"]
        with pytest.raises(EvidenceSchemaError, match="plan"):
            validate_bundle(bundle)

    def test_rejects_malformed_phases(self, tmp_path: Path) -> None:
        bundle = collect_evidence(_golden_run(tmp_path))
        bundle["phases"] = [{"name": "x"}]   # missing required keys
        with pytest.raises(EvidenceSchemaError, match=r"phases\[0\]"):
            validate_bundle(bundle)

    def test_validates_optional_artifact_apply_check_status(
        self, tmp_path: Path,
    ) -> None:
        bundle = collect_evidence(_golden_run(tmp_path))
        bundle["artifacts"][0]["apply_check"] = _apply_check(
            "pass", "patch_applies",
        )
        validate_bundle(bundle)
        assert bundle["schema_version"] == EVIDENCE_SCHEMA_VERSION

        bundle["artifacts"][0]["apply_check"]["status"] = "ok"
        with pytest.raises(EvidenceSchemaError, match="apply_check.status"):
            validate_bundle(bundle)

    def test_rejects_non_dict(self) -> None:
        with pytest.raises(EvidenceSchemaError, match="object"):
            validate_bundle([])         # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────────────
# Collector
# ─────────────────────────────────────────────────────────────────────────────


class TestCollector:
    def test_collect_full_v1_bundle(self, tmp_path: Path) -> None:
        bundle = collect_evidence(_golden_run(tmp_path))

        assert bundle["schema_version"] == EVIDENCE_SCHEMA_VERSION
        assert bundle["run_id"] == "GOLDEN_TEST"
        assert bundle["status"] == "done"
        assert bundle["task"] == "fix bug"
        assert bundle["profile"] == "advanced"
        assert bundle["raw_events_path"].endswith("events.jsonl")

    def test_plan_record_reflects_plan_parsed_event(
        self, tmp_path: Path,
    ) -> None:
        bundle = collect_evidence(_golden_run(tmp_path))
        plan = bundle["plan"]

        assert plan["source"] == "json"
        assert plan["short_summary"] == "Reject invalid payloads."
        assert plan["planning_context"] == "Implementation Plan body..."
        assert plan["subtask_count"] == 3
        assert plan["has_contract"] is True
        assert plan["goal"] == "Reject invalid payloads"
        assert plan["acceptance_criteria"] == [
            "invalid payload is rejected",
            "tests pass",
        ]
        assert plan["owned_files"] == ["calc.py"]
        assert plan["commands_to_run"] == ["pytest -q"]
        assert len(plan["acceptance_criteria"]) == 2
        assert len(plan["owned_files"]) == 1
        assert len(plan["commands_to_run"]) == 1
        assert "Implementation Plan body" in plan["planning_context"]
        assert plan["subtasks"] == [
            {
                "id": "t1",
                "goal": "Add payload validation",
                "owned_files": ["calc.py"],
                "done_criteria": ["invalid payload is rejected"],
            },
            {
                "id": "t2",
                "goal": "Add regression coverage",
                "depends_on": ["t1"],
                "files": ["tests/test_calc.py"],
                "done_criteria": ["tests pass"],
            },
        ]

    def test_phases_paired_by_attempt(self, tmp_path: Path) -> None:
        bundle = collect_evidence(_golden_run(tmp_path))
        phase_names = [p["name"] for p in bundle["phases"]]
        assert phase_names == ["PLAN", "IMPLEMENT"]
        for entry in bundle["phases"]:
            assert entry["outcome"] == "ok"
            assert entry["started_at"]
            assert entry["ended_at"]

    def test_gate_start_end_paired(self, tmp_path: Path) -> None:
        bundle = collect_evidence(_golden_run(tmp_path))
        gates = bundle["gates"]
        assert len(gates) == 1
        assert gates[0]["name"] == "tests"
        assert gates[0]["outcome"] == "passed"
        assert gates[0]["kind"] == "computational"
        assert gates[0]["duration_s"] == pytest.approx(0.42)

    def test_command_start_end_paired(self, tmp_path: Path) -> None:
        bundle = collect_evidence(_golden_run(tmp_path))
        cmds = bundle["commands"]
        assert len(cmds) == 1
        assert cmds[0]["argv_summary"] == "pytest -q"
        assert cmds[0]["exit_code"] == 0
        assert cmds[0]["outcome"] == "ok"

    def test_artifact_events_surface_in_artifacts(
        self, tmp_path: Path,
    ) -> None:
        bundle = collect_evidence(_golden_run(tmp_path))
        assert any(a["kind"] == "plan" for a in bundle["artifacts"])

    def test_artifact_events_carry_apply_check_metadata(
        self, tmp_path: Path,
    ) -> None:
        target = tmp_path / "apply_check_artifacts"
        events = [
            _ev(1, "run.start", task="x", run_kind="single_project",
                project="/p", profile="advanced"),
            _ev(2, "artifact.created", path="/run/pass.patch",
                artifact_kind="diff", size_bytes=12,
                apply_check=_apply_check("pass", "patch_applies")),
            _ev(3, "artifact.created", path="/run/fail.patch",
                artifact_kind="diff", size_bytes=13,
                apply_check=_apply_check("fail", "patch_does_not_apply")),
            _ev(4, "artifact.created", path="/run/degraded.patch",
                artifact_kind="diff", size_bytes=14,
                apply_check=_apply_check("degraded", "baseline_unavailable")),
            _ev(5, "run.end", status="done", summary="ok"),
        ]
        meta = {"run_id": "APPLY_CHECK", "task": "x", "profile": "advanced",
                "status": "done", "phases": {}}
        _write_run_dir(target, events=events, meta=meta, metrics={})

        bundle = collect_evidence(target)
        by_path = {a["path"]: a["apply_check"] for a in bundle["artifacts"]}

        assert by_path["/run/pass.patch"]["status"] == "pass"
        assert by_path["/run/fail.patch"]["status"] == "fail"
        assert by_path["/run/degraded.patch"]["status"] == "degraded"
        validate_bundle(bundle)

    def test_metrics_rollup_projects_metrics_json(
        self, tmp_path: Path,
    ) -> None:
        bundle = collect_evidence(_golden_run(tmp_path))
        m = bundle["metrics"]
        assert m["total_tokens"] == 1000
        assert m["total_tokens_in"] == 800
        assert m["total_tokens_out"] == 200
        assert m["total_duration_s"] == pytest.approx(1.5)
        assert m["total_rounds"] == 1

    def test_metrics_rollup_carries_subtask_breakdown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from core.infra import config
        monkeypatch.setenv("ORCHO_ACCOUNTING", "1")
        config._reset_config()
        try:
            target = tmp_path / "with_subtasks"
            events = [
                _ev(1, "run.start", task="x", run_kind="single_project",
                    project="/p", profile="advanced"),
                _ev(2, "run.end", status="done", summary="ok"),
            ]
            meta = {"run_id": "S", "task": "x", "profile": "advanced",
                    "status": "done", "phases": {}}
            metrics = {
                "total_tokens": 3300, "total_tokens_in": 3000,
                "total_tokens_out": 300, "total_duration_s": 3.0,
                "total_rounds": 0,
                "subtasks": {
                    "implement": [
                        {"subtask_id": "T1", "total_tokens": 1100,
                         "cost_usd_equivalent": 0.02},
                        {"subtask_id": "T2", "total_tokens": 2200,
                         "cost_usd_equivalent": 0.04},
                    ],
                },
            }
            _write_run_dir(target, events=events, meta=meta, metrics=metrics)

            bundle = collect_evidence(target)
            validate_bundle(bundle)  # additive key must not break validation
            rows = bundle["metrics"]["subtasks"]["implement"]
            # The durable bundle alone answers "which subtask was most
            # expensive?".
            assert max(rows, key=lambda r: r["cost_usd_equivalent"])[
                "subtask_id"
            ] == "T2"
        finally:
            config._reset_config()

    def test_metrics_rollup_omits_subtasks_for_old_runs(
        self, tmp_path: Path,
    ) -> None:
        # A pre-feature metrics.json (no ``subtasks``) yields no key and stays
        # schema-valid.
        bundle = collect_evidence(_golden_run(tmp_path))
        validate_bundle(bundle)
        assert "subtasks" not in bundle["metrics"]

    def test_errors_surface_plan_parse_breadcrumb(
        self, tmp_path: Path,
    ) -> None:
        target = tmp_path / "halted"
        events = [
            _ev(1, "run.start", task="x", run_kind="single_project",
                project="/p", profile="advanced"),
            _ev(2, "run.end", status="halted",
                error="acceptance_criteria must be a list",
                error_type="PlanSchemaError"),
        ]
        meta = {
            "run_id": "HALTED",
            "task": "x",
            "profile": "advanced",
            "status": "halted",
            "phases": {
                "plan": [{
                    "attempt": 1,
                    "output": "...",
                    "parse_error": "acceptance_criteria must be a list of strings",
                }],
            },
        }
        _write_run_dir(target, events=events, meta=meta, metrics={})

        bundle = collect_evidence(target)
        kinds = [e["kind"] for e in bundle["errors"]]
        assert "plan_parse_error" in kinds
        assert "run_failed" in kinds

    def test_errors_preserve_provider_access_metadata(
        self, tmp_path: Path,
    ) -> None:
        target = tmp_path / "provider_access"
        events = [
            _ev(1, "run.start", task="x", run_kind="single_project",
                project="/p", profile="feature"),
            _ev(2, "run.end", status="failed",
                error="Provider access unavailable: subscription access disabled",
                error_type="AgentAccessError",
                failure_kind="provider_access",
                recoverable=False,
                recommended_action="switch_runtime_or_restore_access"),
        ]
        meta = {
            "run_id": "PROVIDER_ACCESS",
            "task": "x",
            "profile": "feature",
            "status": "failed",
            "phases": {},
        }
        _write_run_dir(target, events=events, meta=meta, metrics={})

        bundle = collect_evidence(target)
        [error] = [e for e in bundle["errors"] if e["kind"] == "run_failed"]

        assert error["error_type"] == "AgentAccessError"
        assert error["failure_kind"] == "provider_access"
        assert error["recoverable"] is False
        assert error["recommended_action"] == "switch_runtime_or_restore_access"

    def test_errors_surface_phase_handoff_waiver(
        self, tmp_path: Path,
    ) -> None:
        """A ``continue_with_waiver`` decision persists
        ``meta.phase_handoff_waiver``; the collector projects it as a
        distinct ``phase_handoff_waiver`` error so a post-mortem shows the
        run shipped under an operator waiver, with the verdict text and
        waived findings preserved."""
        target = tmp_path / "waived"
        meta = {
            "run_id": "WAIVED",
            "task": "x",
            "profile": "advanced",
            "status": "done",
            "phases": {},
            "phase_handoff_waiver": {
                "handoff_id": "review_changes:repair_round:1",
                "phase": "review_changes",
                "waiver_text": "accepted risk: legacy shim stays",
                "note": "operator waiver",
                "decided_at": "2026-06-03T12:00:00+00:00",
                "findings": [{"id": "F1", "title": "legacy shim"}],
            },
        }
        _write_run_dir(target, events=[], meta=meta, metrics={})

        bundle = collect_evidence(target)
        waivers = [
            e for e in bundle["errors"]
            if e["kind"] == "phase_handoff_waiver"
        ]
        assert len(waivers) == 1
        w = waivers[0]
        # Contract key (interface item 5 / T3b): consumers read the operator
        # verdict off ``waiver_text``; ``message`` mirrors it for renderers.
        assert w["waiver_text"] == "accepted risk: legacy shim stays"
        assert w["message"] == "accepted risk: legacy shim stays"
        assert w["phase"] == "review_changes"
        assert w["handoff_id"] == "review_changes:repair_round:1"
        assert w["decided_at"] == "2026-06-03T12:00:00+00:00"
        assert w["findings"] == [{"id": "F1", "title": "legacy shim"}]

        # Renderer surfaces it without raising.
        md = render_evidence_md(bundle)
        assert "accepted risk: legacy shim stays" in md

    def test_collect_tolerant_of_missing_metrics(
        self, tmp_path: Path,
    ) -> None:
        target = _golden_run(tmp_path)
        target.joinpath("metrics.json").unlink()
        bundle = collect_evidence(target)
        # Lower-bound: every required metrics key still present, defaulted.
        m = bundle["metrics"]
        assert m["total_tokens"] == 0
        assert m["total_duration_s"] == 0.0

    def test_collect_raises_on_missing_run_dir(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            collect_evidence(tmp_path / "nope")


# ─────────────────────────────────────────────────────────────────────────────
# Findings (collector + renderer surface — DEMO-1A)
# ─────────────────────────────────────────────────────────────────────────────


def _findings_run(target: Path) -> Path:
    """Run dir whose meta carries reviewer findings on validate_plan + final_acceptance.

 Mirrors the on-disk shape produced by the live mock pipeline when
 `--mock-validate-plan-reject 1` forces a rejected validate_plan attempt:
 findings live at ``meta.phases.<phase>[i].findings`` with
 ``{id, severity, title, body, required_fix}`` records.
 """
    target.mkdir(parents=True, exist_ok=True)
    target.joinpath("events.jsonl").write_text("", encoding="utf-8")
    meta = {
        "run_id": "FINDINGS_TEST",
        "task": "demo",
        "profile": "advanced",
        "status": "done",
        "phases": {
            "validate_plan": [
                {
                    "attempt": 1,
                    "verdict": "REJECTED",
                    "approved": False,
                    "findings": [
                        {
                            "id": "F1",
                            "severity": "P2",
                            "title": "Missing test coverage",
                            "body": "Edge case A is not exercised.",
                            "required_fix": "Add a test for edge case A.",
                        },
                        {
                            "id": "F2",
                            "severity": "P3",
                            "title": "Module boundary unclear",
                            "body": "Owner module not stated.",
                            "required_fix": "Name the owning module.",
                            "file": "src/foo.py",
                            "line": 42,
                        },
                    ],
                },
                {
                    "attempt": 2,
                    "verdict": "APPROVED",
                    "approved": True,
                    "findings": [],
                },
            ],
            "final_acceptance": [
                {
                    "attempt": 1,
                    "verdict": "APPROVED",
                    "findings": [
                        {
                            "id": "Q1",
                            "severity": "P0",
                            "title": "Critical regression",
                            "body": "Tests fail on edge path.",
                            "required_fix": "Restore guard clause.",
                        },
                    ],
                },
            ],
            # build is a dict, not a list — must not crash the collector.
            "implement": {"output": "ok"},
        },
    }
    target.joinpath("meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return target


class TestFindings:
    def test_bundle_carries_top_level_findings_in_source_order(
        self, tmp_path: Path,
    ) -> None:
        bundle = collect_evidence(_findings_run(tmp_path))

        assert "findings" in bundle
        # validate_plan attempt 1 (F1, F2) before final_acceptance attempt 1 (Q1) — phase
        # order matches FINDING_BEARING_PHASES; within-attempt order
        # preserved; the empty validate_plan attempt 2 contributes nothing.
        assert [f["id"] for f in bundle["findings"]] == ["F1", "F2", "Q1"]
        assert [f["phase"] for f in bundle["findings"]] == [
            "validate_plan", "validate_plan", "final_acceptance",
        ]
        assert [f["attempt"] for f in bundle["findings"]] == [1, 1, 1]

    def test_bundle_annotates_finding_lifecycle_status(
        self, tmp_path: Path,
    ) -> None:
        bundle = collect_evidence(_findings_run(tmp_path))
        by_id = {f["id"]: f for f in bundle["findings"]}

        assert by_id["F1"]["status"] == "fixed"
        assert by_id["F1"]["status_reason"] == "later validate_plan attempt approved"
        assert by_id["F2"]["status"] == "fixed"
        assert by_id["Q1"]["status"] == "accepted"
        assert by_id["Q1"]["status_reason"] == (
            "source phase approved with this finding present"
        )

    def test_finding_record_preserves_optional_location(
        self, tmp_path: Path,
    ) -> None:
        bundle = collect_evidence(_findings_run(tmp_path))
        f1, f2, q1 = bundle["findings"]

        # Mock validate_plan records lack file/line — surfaced as None.
        assert f1["file"] is None and f1["line"] is None
        # Real reviewer records carry file:line — passthrough.
        assert f2["file"] == "src/foo.py" and f2["line"] == 42
        assert q1["severity"] == "P0"
        assert q1["required_fix"] == "Restore guard clause."

    def test_findings_default_to_empty_when_no_reviewer_phases(
        self, tmp_path: Path,
    ) -> None:
        # The golden fixture has plan/build but no validate_plan/review/final_acceptance.
        bundle = collect_evidence(_golden_run(tmp_path))
        assert bundle["findings"] == []

    def test_render_findings_section_carries_severity_and_required_fix(
        self, tmp_path: Path,
    ) -> None:
        bundle = collect_evidence(_findings_run(tmp_path))
        md = render_evidence_md(bundle)

        assert "## Findings" in md
        assert "**Lifecycle:** `fixed` x2 (P2 x1, P3 x1), `accepted` x1 (P0 x1)" in md
        # Severity, phase, attempt, title all visible.
        assert "`FIXED` `P2`" in md and "`ACCEPTED` `P0`" in md
        assert "Missing test coverage" in md
        assert "**Status:** `fixed` (later validate_plan attempt approved)" in md
        assert "**Phase:** `validate_plan`" in md
        assert "**Phase:** `final_acceptance`" in md
        # required_fix surfaced for every finding.
        assert "Add a test for edge case A." in md
        assert "Restore guard clause." in md
        # Optional location renders only when present.
        assert "`src/foo.py:42`" in md
        # ## Findings sits between Quality gates and Commands.
        gates_idx = md.index("## Quality gates")
        findings_idx = md.index("## Findings")
        commands_idx = md.index("## Commands")
        assert gates_idx < findings_idx < commands_idx

    def test_render_findings_empty_state(self, tmp_path: Path) -> None:
        # Approved-clean run: bundle.findings == []; the section still
        # renders so the reader knows reviewers ran and produced nothing.
        bundle = collect_evidence(_golden_run(tmp_path))
        md = render_evidence_md(bundle)

        assert "## Findings" in md
        assert "_No review findings recorded._" in md

    def test_render_empty_commands_names_mock_recording_gap(
        self, tmp_path: Path,
    ) -> None:
        bundle = collect_evidence(_golden_run(tmp_path))
        bundle["commands"] = []
        md = render_evidence_md(bundle)

        assert (
            "_No shell commands were recorded in the evidence event stream._"
            in md
        )
        assert "_No external commands ran._" not in md

    def test_collector_tolerates_malformed_findings_shapes(
        self, tmp_path: Path,
    ) -> None:
        target = tmp_path / "messy"
        target.mkdir()
        target.joinpath("events.jsonl").write_text("", encoding="utf-8")
        target.joinpath("meta.json").write_text(json.dumps({
            "run_id": "MESSY",
            "status": "done",
            "phases": {
                "validate_plan": "not-a-list",                 # wrong outer type
                "review_changes": [{"findings": "not-a-list"}],  # wrong inner type
                "final_acceptance": [{"findings": [42, "x"]}],   # non-dict findings
                "compliance_check": [None],              # non-dict attempt
            },
        }), encoding="utf-8")
        bundle = collect_evidence(target)
        # Every malformed shape contributes zero findings rather than
        # crashing the bundle.
        assert bundle["findings"] == []

    def test_existing_schema_validation_unchanged(
        self, tmp_path: Path,
    ) -> None:
        # Adding the additive `findings` key must not affect bundles
        # that pre-date it: validate_bundle still passes on bundles
        # produced by collect_evidence on either fixture.
        validate_bundle(collect_evidence(_golden_run(tmp_path / "g")))
        validate_bundle(collect_evidence(_findings_run(tmp_path / "f")))


# ─────────────────────────────────────────────────────────────────────────────
# Release-gate persisted shape (ADR 0025 Phase 1)
#
# ``FinalAcceptanceAdapter`` writes ``session["phases"]["final_acceptance"]``
# as a singleton ``dict``, not a list of attempts. Both the evidence
# collector and the SDK findings slice must normalize that shape so
# release blockers projected into the review-shape ``findings`` mirror
# stay visible to consumers, and so the new ``release_summary`` section
# carries the release-only ``why_blocks_release`` field.
# ─────────────────────────────────────────────────────────────────────────────


def _release_rejected_run(target: Path) -> Path:
    """Run dir whose final_acceptance is persisted as the real dual-shape
    singleton ``dict`` (mirroring ``FinalAcceptanceAdapter.write``):
    REJECTED release verdict with a projected blocker on ``findings``
    and the full release surface on ``release_blockers`` etc.
    """
    target.mkdir(parents=True, exist_ok=True)
    target.joinpath("events.jsonl").write_text("", encoding="utf-8")
    meta = {
        "run_id": "RELEASE_REJECTED",
        "task": "demo",
        "profile": "advanced",
        "status": "done",
        "phases": {
            "final_acceptance": {
                # Review-shape mirror (FinalAcceptanceAdapter writes
                # findings projected from release_blockers).
                "verdict": "REJECTED",
                "approved": False,
                "short_summary": "Release blocked.",
                "findings": [{
                    "id": "R1",
                    "severity": "P1",
                    "title": "Release blocker",
                    "body": "Breaks caller contract.",
                    "required_fix": "Restore compatibility.",
                }],
                # Release-shape (new fields).
                "ship_ready": False,
                "release_blockers": [{
                    "id": "R1",
                    "severity": "P1",
                    "title": "Release blocker",
                    "body": "Breaks caller contract.",
                    "required_fix": "Restore compatibility.",
                    "why_blocks_release": "Production callers would fail.",
                }],
                "verification_gaps": [],
                "contract_status": {
                    "task_contract": "incomplete",
                    "interfaces":    "broken",
                    "persistence":   "safe",
                    "tests":         "weak",
                },
            },
        },
    }
    target.joinpath("meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return target


class TestReleaseDictShape:
    def test_findings_slice_normalizes_singleton_dict_shape(
        self, tmp_path: Path,
    ) -> None:
        """``final_acceptance`` persisted as a singleton dict must still
        surface its review-shape findings — the dual-shape mirror is
        only useful if downstream finding readers can see it."""
        bundle = collect_evidence(_release_rejected_run(tmp_path))
        ids = [f["id"] for f in bundle["findings"]]
        assert ids == ["R1"], (
            "release blocker projected into final_acceptance.findings "
            "must be visible in the evidence findings slice"
        )
        entry = bundle["findings"][0]
        assert entry["phase"] == "final_acceptance"
        assert entry["attempt"] == 1
        assert entry["severity"] == "P1"
        assert entry["required_fix"] == "Restore compatibility."
        assert entry["status"] == "final_rejected"
        assert entry["status_reason"] == "final acceptance rejected this finding"

    def test_release_summary_preserves_why_blocks_release(
        self, tmp_path: Path,
    ) -> None:
        """``release_summary`` is the only place where the release-only
        ``why_blocks_release`` field survives — the review-shape mirror
        drops it by design. Verify it round-trips through evidence."""
        bundle = collect_evidence(_release_rejected_run(tmp_path))
        rs = bundle["release_summary"]
        assert len(rs) == 1
        entry = rs[0]
        assert entry["phase"] == "final_acceptance"
        assert entry["verdict"] == "REJECTED"
        assert entry["ship_ready"] is False
        assert entry["release_blockers"][0]["why_blocks_release"] == (
            "Production callers would fail."
        )
        assert entry["contract_status"]["interfaces"] == "broken"

    def test_release_summary_skips_phase_without_release_fields(
        self, tmp_path: Path,
    ) -> None:
        """Legacy / parse-error paths that didn't write ``ship_ready``
        must not crash and must not produce a summary entry."""
        target = tmp_path / "legacy"
        target.mkdir()
        target.joinpath("events.jsonl").write_text("", encoding="utf-8")
        target.joinpath("meta.json").write_text(json.dumps({
            "run_id": "LEGACY",
            "status": "done",
            "phases": {
                "final_acceptance": {
                    "verdict": "APPROVED",
                    "findings": [],
                    # No ship_ready / release_blockers / etc.
                },
            },
        }), encoding="utf-8")
        bundle = collect_evidence(target)
        assert bundle["release_summary"] == []
        # Findings slice still works (empty findings list, but the
        # phase is read without crash).
        assert bundle["findings"] == []


# ─────────────────────────────────────────────────────────────────────────────
# Renderer
# ─────────────────────────────────────────────────────────────────────────────


class TestRenderer:
    def test_render_full_bundle_carries_canonical_sections(
        self, tmp_path: Path,
    ) -> None:
        bundle = collect_evidence(_golden_run(tmp_path))
        md = render_evidence_md(bundle)

        # Headings are stable section anchors REA-3 readers can rely on.
        for heading in (
            "# Run evidence",
            "## Summary",
            "## Plan",
            "## Phase timeline",
            "## Quality gates",
            "## Commands",
            "## Artifacts",
            "## Metrics",
            "## Errors",
        ):
            assert heading in md, f"missing section heading: {heading!r}"

        # Spot-check rolled values landed in the human-readable view.
        assert "GOLDEN_TEST" in md
        assert "Reject invalid payloads" in md
        assert "pytest -q" in md
        assert "1,000" in md or "1000" in md   # token formatting
        assert "**Repair rounds:** 1" in md
        assert "**Rounds:**" not in md

    def test_render_artifact_apply_check_status_when_present(
        self, tmp_path: Path,
    ) -> None:
        clean = render_evidence_md(collect_evidence(_golden_run(tmp_path / "clean")))
        assert "Apply check" not in clean

        bundle = collect_evidence(_golden_run(tmp_path / "with_apply_check"))
        bundle["artifacts"][0]["apply_check"] = _apply_check(
            "fail", "patch_does_not_apply",
        )
        md = render_evidence_md(bundle)

        assert "Apply check" in md
        assert "`fail` patch_does_not_apply" in md

    def test_render_is_deterministic(self, tmp_path: Path) -> None:
        bundle = collect_evidence(_golden_run(tmp_path))
        a = render_evidence_md(bundle)
        b = render_evidence_md(bundle)
        assert a == b

    def test_render_placeholder_bundle(self) -> None:
        md = render_evidence_md({
            "schema_version": EVIDENCE_SCHEMA_VERSION_PLACEHOLDER,
            "run_id": "X",
            "run_dir": "/r",
            "status": "interrupted",
        })
        assert "(placeholder)" in md
        assert "interrupted" in md

    def test_running_summary_names_active_phase_and_attention(
        self, tmp_path: Path,
    ) -> None:
        target = tmp_path / "running"
        events = [
            _ev(1, "run.start", task="x", run_kind="single_project",
                project="/p", profile="feature"),
            {
                "seq": 2,
                "ts": "2026-05-08T10:00:02.000",
                "kind": "phase.handoff_requested",
                "phase": None,
                "payload": {
                    "phase": "review_changes",
                    "trigger": "rejected",
                    "handoff_id": "review_changes:repair_round:3",
                },
            },
            _ev(3, "phase.start", phase="REPAIR_CHANGES",
                title="REPAIR CHANGES -- Round 1",
                phase_kind="REPAIR_CHANGES", attempt=1),
            _ev(4, "gate.start", phase="REPAIR_CHANGES",
                name="tests", gate_kind="computational"),
            _ev(5, "gate.end", phase="REPAIR_CHANGES",
                name="tests", outcome="skipped", duration_s=0.0),
            _ev(6, "agent.command_stalled", phase="REPAIR_CHANGES",
                reason="unsafe process polling", elapsed_s=91.25,
                terminal=False, command_preview="pytest -q"),
        ]
        meta = {
            "run_id": "RUNNING_STATUS",
            "task": "x",
            "profile": "feature",
            "status": "running",
            "phases": {},
        }
        _write_run_dir(target, events=events, meta=meta, metrics={})

        bundle = collect_evidence(target)
        md = render_evidence_md(bundle)

        assert "- **Status:** `running`" in md
        assert "Pending handoff" not in md
        assert (
            "- **Active phase:** `REPAIR_CHANGES` attempt 1 "
            "— REPAIR CHANGES -- Round 1"
        ) in md
        assert "- **Gate attention:** 1 skipped (`tests`)" in md
        assert "Stall diagnostics" not in md
        assert (
            "- `command_stalled`: 1 live diagnostic event hidden; "
            "rerun with `--debug` for details"
        ) in md
        assert "unsafe process polling — `pytest -q`" not in md

        debug_md = render_evidence_md(bundle, debug=True)

        assert (
            "- `command_stalled` (live, phase `REPAIR_CHANGES`, 91.25s): "
            "unsafe process polling — `pytest -q`"
        ) in debug_md

    def test_awaiting_handoff_summary_names_pending_handoff(
        self, tmp_path: Path,
    ) -> None:
        target = tmp_path / "handoff"
        events = [
            _ev(1, "run.start", task="x", run_kind="single_project",
                project="/p", profile="feature"),
            _ev(2, "phase.start", phase="REVIEW_CHANGES",
                title="review_changes -- Round 3",
                phase_kind="REVIEW_CHANGES", attempt=3),
            _ev(3, "phase.end", phase="REVIEW_CHANGES",
                title="review_changes -- Round 3", outcome="ok",
                phase_kind="REVIEW_CHANGES", attempt=3),
            {
                "seq": 4,
                "ts": "2026-05-08T10:00:04.000",
                "kind": "phase.handoff_requested",
                "phase": None,
                "payload": {
                    "phase": "review_changes",
                    "handoff_type": "human_feedback_on_reject",
                    "trigger": "rejected",
                    "round": 3,
                    "handoff_id": "review_changes:repair_round:3",
                },
            },
        ]
        meta = {
            "run_id": "HANDOFF_STATUS",
            "task": "x",
            "profile": "feature",
            "status": "awaiting_phase_handoff",
            "phases": {},
        }
        _write_run_dir(target, events=events, meta=meta, metrics={})

        md = render_evidence_md(collect_evidence(target))

        assert "- **Status:** `awaiting_phase_handoff`" in md
        assert (
            "- **Pending handoff:** `review_changes:repair_round:3` "
            "(phase `review_changes`, trigger `rejected`)"
        ) in md
        assert "- **Last phase:** `REVIEW_CHANGES` attempt 3" in md

    def test_halted_summary_names_terminal_reason(
        self, tmp_path: Path,
    ) -> None:
        target = tmp_path / "halted_status"
        events = [
            _ev(1, "run.start", task="x", run_kind="single_project",
                project="/p", profile="feature"),
            _ev(2, "run.end", status="halted",
                halt_reason="final_acceptance_rejected"),
        ]
        meta = {
            "run_id": "HALTED_STATUS",
            "task": "x",
            "profile": "feature",
            "status": "halted",
            "phases": {},
        }
        _write_run_dir(target, events=events, meta=meta, metrics={})

        md = render_evidence_md(collect_evidence(target))

        assert "- **Status:** `halted`" in md
        assert "- **Terminal reason:** `final_acceptance_rejected`" in md


# ─────────────────────────────────────────────────────────────────────────────
# write_bundle / write_bundle_or_placeholder
# ─────────────────────────────────────────────────────────────────────────────


class TestWriteBundle:
    def test_write_bundle_emits_json_and_md(self, tmp_path: Path) -> None:
        target = _golden_run(tmp_path)
        json_path, md_path = write_bundle(target)

        assert json_path.name == EVIDENCE_FILE_NAME
        assert md_path.name == EVIDENCE_MD_FILE_NAME
        assert json_path.is_file() and md_path.is_file()

        bundle = json.loads(json_path.read_text())
        validate_bundle(bundle)
        assert "## Plan" in md_path.read_text()

    def test_write_bundle_or_placeholder_falls_back_on_missing_dir(
        self, tmp_path: Path,
    ) -> None:
        """Non-existent run dir → collector raises FileNotFoundError;
 helper falls back to the REA-0 placeholder so finalize never
 crashes."""
        nonexistent = tmp_path / "missing"
        # Don't mkdir — the collector should raise FileNotFoundError.
        out = write_bundle_or_placeholder(
            nonexistent, run_id="EMPTY", status="interrupted",
        )
        bundle = json.loads(out.read_text())
        assert bundle["schema_version"] == EVIDENCE_SCHEMA_VERSION_PLACEHOLDER
        assert bundle["run_id"] == "EMPTY"

    def test_write_bundle_or_placeholder_succeeds_on_empty_dir(
        self, tmp_path: Path,
    ) -> None:
        """An *existing* but empty run dir produces a degenerate v1
 bundle (every rollup empty) rather than the placeholder. The
 collector is tolerant of missing companion files; the
 placeholder is the failsafe for actively broken runs."""
        empty = tmp_path / "empty"
        empty.mkdir()
        out = write_bundle_or_placeholder(
            empty, run_id="EMPTY", status="done",
        )
        bundle = json.loads(out.read_text())
        assert bundle["schema_version"] == EVIDENCE_SCHEMA_VERSION
        assert bundle["phases"] == []
        assert bundle["gates"] == []
        assert bundle["artifacts"] == []

    def test_write_placeholder_directly(self, tmp_path: Path) -> None:
        out = write_placeholder(
            tmp_path, run_id="STUB", status="awaiting_human_review",
        )
        bundle = json.loads(out.read_text())
        assert bundle["schema_version"] == EVIDENCE_SCHEMA_VERSION_PLACEHOLDER
        assert bundle["status"] == "awaiting_human_review"


# ─────────────────────────────────────────────────────────────────────────────
# GWT-1 Worktree evidence projection
# ─────────────────────────────────────────────────────────────────────────────


class TestWorktreeEvidenceProjection:
    def _run_dir_with_worktree(self, target: Path, worktree: dict | None) -> Path:
        meta: dict = {"run_id": "WT_TEST", "task": "t", "status": "done"}
        if worktree is not None:
            meta["worktree"] = worktree
        return _write_run_dir(target, events=[], meta=meta)

    def test_worktree_block_emitted_when_present(self, tmp_path: Path) -> None:
        # Wire shape uses ``isolation`` + ``branch_ref`` (ADR 0033 to_dict).
        ctx = {
            "isolation": "per_run",
            "path": "/run/checkout",
            "branch_ref": "orcho/run/20260522_001",
            "base_ref": "abc123",
            "retention_until": "2026-05-29T00:00:00",
        }
        run_dir = self._run_dir_with_worktree(tmp_path / "r1", ctx)
        bundle = collect_evidence(run_dir)
        assert bundle["worktree"] == ctx

    def test_worktree_block_none_when_absent(self, tmp_path: Path) -> None:
        run_dir = self._run_dir_with_worktree(tmp_path / "r2", None)
        bundle = collect_evidence(run_dir)
        assert bundle["worktree"] is None

    def test_worktree_off_isolation(self, tmp_path: Path) -> None:
        ctx = {"isolation": "off", "path": "/proj", "branch_ref": None,
               "base_ref": "", "degraded_reason": "disabled"}
        run_dir = self._run_dir_with_worktree(tmp_path / "r3", ctx)
        bundle = collect_evidence(run_dir)
        assert bundle["worktree"]["isolation"] == "off"
        assert bundle["worktree"]["degraded_reason"] == "disabled"

    def test_worktree_projects_empty_for_single_run(self, tmp_path: Path) -> None:
        run_dir = self._run_dir_with_worktree(tmp_path / "r4", {"isolation": "per_run"})
        bundle = collect_evidence(run_dir)
        assert bundle["worktree_projects"] == {}

    def test_worktree_projects_populated_for_cross_run(self, tmp_path: Path) -> None:
        meta = {
            "run_id": "CROSS_WT",
            "task": "t",
            "status": "done",
            "projects": {
                "alpha": {"worktree": {"isolation": "per_run", "path": "/run/alpha/checkout"}},
                "beta": {"worktree": {"isolation": "off", "path": "/proj/beta"}},
                "gamma": {},  # no worktree key — should be excluded
            },
        }
        run_dir = tmp_path / "r5"
        _write_run_dir(run_dir, events=[], meta=meta)
        bundle = collect_evidence(run_dir)
        assert "alpha" in bundle["worktree_projects"]
        assert "beta" in bundle["worktree_projects"]
        assert "gamma" not in bundle["worktree_projects"]

    def test_render_worktree_per_run_section(self, tmp_path: Path) -> None:
        ctx = {
            "isolation": "per_run",
            "path": "/run/checkout",
            "branch_ref": "orcho/run/r1",
            "base_ref": "abc123",
            "retention_until": "2026-05-29T00:00:00",
        }
        run_dir = self._run_dir_with_worktree(tmp_path / "r6", ctx)
        bundle = collect_evidence(run_dir)
        md = render_evidence_md(bundle)
        assert "## Worktree" in md
        assert "per_run" in md
        assert "/run/checkout" in md

    def test_render_worktree_off_shows_reason(self, tmp_path: Path) -> None:
        ctx = {"isolation": "off", "path": "/proj", "branch_ref": None,
               "base_ref": "", "degraded_reason": "disabled"}
        run_dir = self._run_dir_with_worktree(tmp_path / "r7", ctx)
        bundle = collect_evidence(run_dir)
        md = render_evidence_md(bundle)
        assert "## Worktree" in md
        assert "disabled" in md

    def test_render_worktree_absent(self, tmp_path: Path) -> None:
        run_dir = self._run_dir_with_worktree(tmp_path / "r8", None)
        bundle = collect_evidence(run_dir)
        md = render_evidence_md(bundle)
        assert "## Worktree" in md
        assert "No worktree context recorded" in md


# ─────────────────────────────────────────────────────────────────────────────
# ADR 0082 — additive verification-readiness digest
# ─────────────────────────────────────────────────────────────────────────────


class TestVerificationReadinessDigest:
    """The bundle's ``verification_readiness`` key is strictly additive:
    observed command/env receipt facts only — never a stale/missing
    verdict (the collector has neither the declared contract nor the
    live checkout, so that classification is owned by the prompt layer).
    """

    def test_digest_empty_without_receipt_dirs(self, tmp_path: Path) -> None:
        run_dir = _golden_run(tmp_path / "vr_empty")
        bundle = collect_evidence(run_dir)
        assert bundle["verification_readiness"] == {
            "commands": [], "envs": [],
        }
        validate_bundle(bundle)

    def test_digest_carries_passed_failed_and_env(self, tmp_path: Path) -> None:
        from pipeline.evidence.verification_receipt import (
            write_command_receipt,
            write_env_assertion_receipt,
        )

        run_dir = _golden_run(tmp_path / "vr_full")
        base = {
            "env": "ci", "cwd": "/cwd",
            "placeholders": {"checkout": "/co", "project": "/co"},
            "argv": ["x"], "assertions": [], "duration_s": 0.1,
            "parity": "absolute", "detail": "",
            "git": {"checkout_head": "h", "baseline_head": None,
                    "changed_files_fingerprint": "f"},
        }
        write_command_receipt(
            output_dir=run_dir, result={**base, "command": "test", "exit_code": 0},
        )
        write_command_receipt(
            output_dir=run_dir, result={**base, "command": "lint", "exit_code": 1},
        )
        write_env_assertion_receipt(output_dir=run_dir, result={
            "subject": {"checkout": "/co", "project": "/co", "env": "ci"},
            "cwd": "/co", "interpreter": "3.12", "env_overrides": {},
            "assertions": [], "all_passed": True,
        })

        bundle = collect_evidence(run_dir)
        digest = bundle["verification_readiness"]
        by_command = {c["command"]: c for c in digest["commands"]}
        assert by_command["test"]["passed"] is True
        assert by_command["lint"]["passed"] is False
        assert digest["envs"] == [{"env": "ci", "all_passed": True}]
        # Observed facts only — no verdicts the collector cannot make.
        for entry in digest["commands"]:
            assert "stale" not in entry
            assert "missing" not in entry
        validate_bundle(bundle)

    def test_digest_does_not_disturb_existing_keys(self, tmp_path: Path) -> None:
        run_dir = _golden_run(tmp_path / "vr_keys")
        bundle = collect_evidence(run_dir)
        # The ADR 0076 key keeps its meaning and source directory.
        assert bundle["verification_receipts"] == []
        assert bundle["schema_version"] == EVIDENCE_SCHEMA_VERSION
        validate_bundle(bundle)


# ─────────────────────────────────────────────────────────────────────────────
# ADR 0093 — additive handoff-advice digest (T2-collector)
#
# ``collect_handoff_advice`` (T1) normalizes Stage 0/1 advice artifacts into a
# ``{calls, summary}`` digest. The collector folds it into the bundle as a new
# OPTIONAL top-level ``handoff_advice`` key — present only when ≥1 advice call
# exists, so a run that never paused for advice shows no misleading section.
# ─────────────────────────────────────────────────────────────────────────────


def _write_advice_artifact(
    run_dir: Path,
    name: str,
    *,
    handoff_id: str = "h1",
    phase: str = "review_changes",
    recommended_action: str = "retry_feedback",
    confidence: str = "high",
    usage: dict | None = None,
    created_at: str = "2026-06-13T10:00:00+00:00",
) -> str:
    advice_dir = run_dir / "phase_handoff_advice"
    advice_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": "r1",
        "handoff_id": handoff_id,
        "phase": phase,
        "created_at": created_at,
        "response_language": "",
        "advice": {
            "recommended_action": recommended_action,
            "confidence": confidence,
            "rationale": "because",
            "retry_feedback": "fix it",
            "risks": [],
            "expected_files": [],
            "operator_note": "",
            "parse_warnings": [],
        },
        "raw_output": "",
        "usage": usage if usage is not None else {},
    }
    advice_dir.joinpath(name).write_text(json.dumps(payload), encoding="utf-8")
    return f"phase_handoff_advice/{name}"


def _write_advice_decision(
    run_dir: Path,
    name: str,
    *,
    action: str = "retry_feedback",
    advice_relpath: str,
    feedback_source: str = "agent_advice",
    phase: str = "review_changes",
    handoff_id: str = "h1",
) -> None:
    decisions_dir = run_dir / "phase_handoff_decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    note = f"feedback_source={feedback_source}; advice_artifact={advice_relpath}"
    payload = {
        "run_id": "r1",
        "handoff_id": handoff_id,
        "phase": phase,
        "action": action,
        "feedback": "fix it",
        "note": note,
        "decided_at": "2026-06-13T10:05:00+00:00",
    }
    decisions_dir.joinpath(name).write_text(json.dumps(payload), encoding="utf-8")


class TestHandoffAdviceDigest:
    def test_no_advice_surface_omits_key(self, tmp_path: Path) -> None:
        """A run with no Stage 0/1 advice surface gets no handoff_advice key —
        no misleading empty section (criterion 1)."""
        bundle = collect_evidence(_golden_run(tmp_path))
        assert "handoff_advice" not in bundle
        validate_bundle(bundle)
        # Top-level required keys and schema version untouched.
        assert bundle["schema_version"] == EVIDENCE_SCHEMA_VERSION

    def test_unapplied_advice_surfaces_call(self, tmp_path: Path) -> None:
        run_dir = _golden_run(tmp_path)
        _write_advice_artifact(run_dir, "h1.json")
        bundle = collect_evidence(run_dir)

        assert "handoff_advice" in bundle
        advice = bundle["handoff_advice"]
        assert len(advice["calls"]) == 1
        call = advice["calls"][0]
        assert call["applied_action"] is None
        assert call["outcome"] == "stopped"
        assert advice["summary"]["calls"] == 1
        validate_bundle(bundle)

    def test_resolved_after_approved_review(self, tmp_path: Path) -> None:
        target = tmp_path / "resolved"
        target.mkdir()
        target.joinpath("events.jsonl").write_text("", encoding="utf-8")
        target.joinpath("meta.json").write_text(json.dumps({
            "run_id": "ADV_RESOLVED",
            "status": "done",
            "phases": {
                "review_changes": [
                    {"attempt": 1, "approved": False, "verdict": "REJECTED",
                     "findings": [{"id": "F1", "severity": "P1", "title": "bug"}]},
                    {"attempt": 2, "approved": True, "verdict": "APPROVED",
                     "findings": []},
                ],
            },
        }), encoding="utf-8")
        relpath = _write_advice_artifact(target, "h1.json", phase="review_changes")
        _write_advice_decision(
            target, "d1.json", advice_relpath=relpath, feedback_source="agent_advice",
        )

        bundle = collect_evidence(target)
        advice = bundle["handoff_advice"]
        call = advice["calls"][0]
        assert call["feedback_source"] == "agent_advice"
        assert call["applied_action"] == "retry_feedback"
        assert call["outcome"] == "resolved"
        assert advice["summary"]["resolved_retries"] == 1
        validate_bundle(bundle)

    def test_repeated_finding_after_retry(self, tmp_path: Path) -> None:
        target = tmp_path / "repeated"
        target.mkdir()
        target.joinpath("events.jsonl").write_text("", encoding="utf-8")
        same = {"id": "F1", "severity": "P1", "title": "still broken"}
        target.joinpath("meta.json").write_text(json.dumps({
            "run_id": "ADV_REPEATED",
            "status": "done",
            "phases": {
                "review_changes": [
                    {"attempt": 1, "approved": False, "verdict": "REJECTED",
                     "findings": [same]},
                    {"attempt": 2, "approved": False, "verdict": "REJECTED",
                     "findings": [dict(same)]},
                ],
            },
        }), encoding="utf-8")
        relpath = _write_advice_artifact(target, "h1.json", phase="review_changes")
        _write_advice_decision(target, "d1.json", advice_relpath=relpath)

        bundle = collect_evidence(target)
        advice = bundle["handoff_advice"]
        assert advice["calls"][0]["outcome"] == "repeated"
        assert advice["summary"]["repeated"] == 1
        assert advice["summary"]["resolved_retries"] == 0
        validate_bundle(bundle)

    def test_validate_bundle_tolerates_handoff_advice_key(
        self, tmp_path: Path,
    ) -> None:
        """validate_bundle accepts a bundle carrying the additive
        handoff_advice key, and rejects a malformed one — proving the key is
        optional but shape-checked."""
        bundle = collect_evidence(_golden_run(tmp_path))
        # Inject a well-formed digest by hand: validation must pass.
        bundle["handoff_advice"] = {
            "calls": [{"phase": "review_changes", "outcome": "resolved"}],
            "summary": {"calls": 1},
        }
        validate_bundle(bundle)
        # Malformed outer shape is rejected.
        bundle["handoff_advice"] = {"calls": "not-a-list", "summary": {}}
        with pytest.raises(EvidenceSchemaError, match="handoff_advice"):
            validate_bundle(bundle)

    def test_render_prints_section_only_with_advice(self, tmp_path: Path) -> None:
        # Without advice: section omitted entirely.
        clean = collect_evidence(_golden_run(tmp_path / "clean"))
        md_clean = render_evidence_md(clean)
        assert "## Agent advice" not in md_clean

        # With advice: section rendered, covering both sources + outcome.
        run_dir = _golden_run(tmp_path / "advised")
        relpath = _write_advice_artifact(run_dir, "h1.json", phase="review_changes")
        _write_advice_decision(
            run_dir, "d1.json", advice_relpath=relpath, feedback_source="ci_agent",
        )
        bundle = collect_evidence(run_dir)
        md = render_evidence_md(bundle)
        assert "## Agent advice" in md
        assert "ci_agent" in md
        assert "review_changes" in md
        # Sits between Findings and Commands.
        assert md.index("## Findings") < md.index("## Agent advice")
        assert md.index("## Agent advice") < md.index("## Commands")


# ─────────────────────────────────────────────────────────────────────────────
# T2 — multi_project_delivery companion-disclosure block
# ─────────────────────────────────────────────────────────────────────────────


class TestMultiProjectDeliveryBlock:
    """The additive ``multi_project_delivery`` evidence block (T2).

    Built from the durable ``meta.multi_project_delivery`` block ``run.py``
    propagates from the T1 disclosure. Present only for a multi-repo run; a
    single-repo run records no block, so the bundle key stays absent.
    """

    def _meta(self, *, multi: dict | None) -> dict:
        meta: dict = {"status": "done", "run_id": "r-mpd", "profile": "feature"}
        if multi is not None:
            meta["multi_project_delivery"] = multi
        return meta

    def test_block_emitted_with_per_repo_disclosure(self, tmp_path: Path) -> None:
        run_dir = _write_run_dir(
            tmp_path / "mpd",
            events=[_ev(0, "run.start"), _ev(1, "run.end", status="done")],
            meta=self._meta(multi={
                "primary_status": "committed",
                "companions": [
                    {
                        "alias": "orcho-mcp",
                        "path": "/ws/orcho-mcp",
                        "state": "dirty",
                        "changed_paths": ["[orcho-mcp]/server.py"],
                    },
                    {
                        "alias": "orcho-web",
                        "path": "/ws/orcho-web",
                        "state": "committed",
                        "changed_paths": ["[orcho-web]/app.py"],
                    },
                ],
            }),
        )
        bundle = collect_evidence(run_dir)
        validate_bundle(bundle)  # additive top-level key never breaks v1 schema

        block = bundle["multi_project_delivery"]
        assert block["primary_status"] == "committed"
        assert block["dirty"] == ["orcho-mcp"]
        assert block["companions"] == [
            {
                "alias": "orcho-mcp",
                "path": "/ws/orcho-mcp",
                "state": "dirty",
                "changed_paths": ["[orcho-mcp]/server.py"],
            },
            {
                "alias": "orcho-web",
                "path": "/ws/orcho-web",
                "state": "committed",
                "changed_paths": ["[orcho-web]/app.py"],
            },
        ]

    def test_block_absent_for_single_repo_run(self, tmp_path: Path) -> None:
        run_dir = _write_run_dir(
            tmp_path / "single",
            events=[_ev(0, "run.start"), _ev(1, "run.end", status="done")],
            meta=self._meta(multi=None),
        )
        bundle = collect_evidence(run_dir)
        validate_bundle(bundle)
        assert "multi_project_delivery" not in bundle
