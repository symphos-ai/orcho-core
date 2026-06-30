"""`collect_evidence` / `render_evidence_md` / `write_evidence_bundle`."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdk import collect_evidence, render_evidence_md, write_evidence_bundle


@pytest.fixture
def evidence_run(runs_root: Path) -> Path:
    """A minimal run with the artifacts `pipeline.evidence` looks for.

    The exact schema the collector accepts varies; this fixture provides
    enough of `meta.json`, `metrics.json`, and `events.jsonl` for the
    happy path. If pipeline.evidence rejects the bundle we just check
    the SDK reports `valid=False` rather than crashing.
    """
    rid = "20260507_180000"
    run_dir = runs_root / rid
    run_dir.mkdir()
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "project": "/tmp/projA",
                "task": "Sample",
                "status": "success",
                "profile": "advanced",
                "timestamp": "2026-05-07T18:00:00",
                "phases": {"plan": {}, "implement": {}},
            }
        )
    )
    (run_dir / "metrics.json").write_text(
        json.dumps(
            {
                "total_tokens": 1000,
                "total_duration_s": 5.0,
                "total_rounds": 1,
                "phases": {
                    "plan": {"model": "claude-sonnet-4-6", "total_tokens": 1000, "tokens_exact": True},
                },
            }
        )
    )
    (run_dir / "events.jsonl").write_text("")
    return run_dir


def test_collect_evidence_returns_bundle(evidence_run: Path):
    runs_dir = evidence_run.parent
    bundle = collect_evidence(evidence_run.name, runs_dir=runs_dir)
    assert bundle.run_ref.run_id == evidence_run.name
    assert isinstance(bundle.body, dict)
    assert bundle.markdown  # non-empty rendered string
    # `valid` may be True or False depending on schema strictness; either
    # way `validation_errors` is a tuple.
    assert isinstance(bundle.validation_errors, tuple)


def test_render_evidence_md_uses_body(evidence_run: Path):
    bundle = collect_evidence(evidence_run.name, runs_dir=evidence_run.parent)
    rendered = render_evidence_md(bundle)
    assert isinstance(rendered, str)
    assert rendered  # non-empty


def test_write_evidence_bundle(evidence_run: Path, tmp_path: Path):
    bundle = collect_evidence(evidence_run.name, runs_dir=evidence_run.parent)
    out = tmp_path / "out"
    paths = write_evidence_bundle(bundle, out)
    assert {p.name for p in paths} == {"evidence.json", "evidence.md"}
    assert all(p.exists() for p in paths)
    payload = json.loads((out / evidence_run.name / "evidence.json").read_text())
    assert isinstance(payload, dict)


def test_projection_tolerates_additive_handoff_advice_key(evidence_run: Path):
    """ADR 0093: the SDK evidence projection (sdk.evidence.collect_evidence →
    pipeline.evidence.collect_evidence + validate_bundle) must tolerate the
    additive, optional ``handoff_advice`` top-level key — adding a key never
    breaks the v1 lower-bound contract, so no ``orcho-mcp`` change is needed.

    Mirrors the unit-level guarantee in
    ``tests/unit/pipeline/evidence/test_evidence_bundle.py::test_existing_schema_validation_unchanged``,
    but here through the public SDK surface a transport (e.g. MCP) would use.
    """
    # Drop one Stage 0 advice artifact (with usage) into the run dir so the
    # collector attaches the additive ``handoff_advice`` section. No matching
    # decision → an unapplied-advice call, which is enough to populate the key.
    advice_dir = evidence_run / "phase_handoff_advice"
    advice_dir.mkdir()
    (advice_dir / "h1.json").write_text(
        json.dumps({
            "run_id": "r1",
            "handoff_id": "h1",
            "phase": "review_changes",
            "created_at": "2026-05-07T18:00:00+00:00",
            "response_language": "",
            "advice": {
                "recommended_action": "retry_feedback",
                "confidence": "high",
                "rationale": "because",
                "retry_feedback": "fix it",
                "risks": [],
                "expected_files": [],
                "operator_note": "",
                "parse_warnings": [],
            },
            "raw_output": "",
            "usage": {"tokens_in": 100, "tokens_out": 50},
        }),
        encoding="utf-8",
    )

    bundle = collect_evidence(evidence_run.name, runs_dir=evidence_run.parent)

    # The additive key is present in the projected bundle ...
    assert "handoff_advice" in bundle.body
    assert bundle.body["handoff_advice"]["calls"], "≥1 advice call expected"
    # ... and the projection still considers the bundle valid (the v1
    # validator tolerates the extra top-level key — no schema bump, no
    # orcho-mcp change).
    assert bundle.valid is True
    assert bundle.validation_errors == ()
