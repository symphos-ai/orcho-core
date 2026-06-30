"""Unit tests for the pure scope-expansion classifier (T1).

Covers, with no I/O:

* :func:`derive_in_plan_patterns` — folding plan + project allowed-modifications
  into a glob set, including ``"glob — reason"`` and ``[tag]``-prefixed entries.
* :func:`categorize_file` — deterministic category per signal family.
* :func:`build_scope_expansion_signals` — in-plan skip + conservative defaults +
  per-category ``verified`` binding.
* :func:`classify_file_signals` — the full status matrix (every category and
  every status) plus the SDK-reconciliation and notice→risk downgrade branches.
* ``to_dict`` / :func:`render_scope_expansion_lines` stability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pipeline.engine.scope_expansion import (
    CATEGORY_BUILD,
    CATEGORY_FIXTURE,
    CATEGORY_IMPORT_WIRING,
    CATEGORY_OTHER,
    CATEGORY_PERSISTENCE,
    CATEGORY_PROJECT_CONFIG,
    CATEGORY_PUBLIC_WIRE,
    CATEGORY_SCHEMA,
    CATEGORY_SECURITY,
    FileScopeSignals,
    ScopeExpansionAssessment,
    ScopeExpansionItem,
    ScopeExpansionStatus,
    build_scope_expansion_assessment,
    build_scope_expansion_signals,
    categorize_file,
    classify_file_signals,
    derive_in_plan_patterns,
    render_scope_expansion_lines,
)

# ── tiny plan stand-ins (only the attributes the deriver reads) ──────────────


@dataclass
class _Sub:
    owned_files: tuple[str, ...] = ()
    allowed_modifications: tuple[str, ...] = ()


@dataclass
class _Plan:
    owned_files: tuple[str, ...] = ()
    allowed_modifications: tuple[str, ...] = ()
    subtasks: tuple[_Sub, ...] = field(default_factory=tuple)


def _signals(**overrides) -> FileScopeSignals:
    base = {"path": "x.txt", "category": CATEGORY_OTHER}
    base.update(overrides)
    return FileScopeSignals(**base)


# ── derive_in_plan_patterns ──────────────────────────────────────────────────


def test_derive_in_plan_patterns_unions_all_levels_and_strips_reason():
    plan = _Plan(
        owned_files=("pipeline/engine/scope_expansion.py",),
        allowed_modifications=("docs/architecture/phase_lifecycle.md — link",),
        subtasks=(
            _Sub(
                owned_files=("tests/unit/pipeline/engine/test_scope_expansion.py",),
                allowed_modifications=("[T2] tests/unit/pipeline/phases/** — fixtures",),
            ),
        ),
    )
    patterns = derive_in_plan_patterns(plan, ["uv.lock — lockfile"])
    assert patterns == (
        "docs/architecture/phase_lifecycle.md",
        "pipeline/engine/scope_expansion.py",
        "tests/unit/pipeline/engine/test_scope_expansion.py",
        "tests/unit/pipeline/phases/**",
        "uv.lock",
    )


def test_derive_in_plan_patterns_keeps_in_path_hyphens():
    plan = _Plan(allowed_modifications=("package-lock.json",))
    assert derive_in_plan_patterns(plan, None) == ("package-lock.json",)


def test_derive_in_plan_patterns_handles_none_plan():
    assert derive_in_plan_patterns(None, None) == ()


# ── categorize_file ──────────────────────────────────────────────────────────


def test_categorize_file_each_family():
    assert categorize_file("app/secret_store.py") == CATEGORY_SECURITY
    assert categorize_file("services/auth_client.py") == CATEGORY_SECURITY
    assert categorize_file("storage/run_store.py") == CATEGORY_PERSISTENCE
    assert categorize_file("pipeline/engine/run_state.py") == CATEGORY_PERSISTENCE
    assert categorize_file("db/migrations/0007_add.py") == CATEGORY_PERSISTENCE
    assert categorize_file("docs/sdk_schema.json") == CATEGORY_SCHEMA
    assert categorize_file("tests/data/plan_schema.json") == CATEGORY_SCHEMA
    assert categorize_file("sdk/payloads.py") == CATEGORY_PUBLIC_WIRE
    assert categorize_file("proto/run.proto") == CATEGORY_PUBLIC_WIRE
    assert categorize_file("tests/fixtures/run.json") == CATEGORY_FIXTURE
    assert categorize_file("tests/golden/done_summary.txt") == CATEGORY_FIXTURE
    assert categorize_file("ui/Theme.csproj") == CATEGORY_BUILD
    assert categorize_file("package-lock.json") == CATEGORY_BUILD
    assert categorize_file("pkg/__init__.py") == CATEGORY_IMPORT_WIRING
    assert categorize_file("project.config.yaml") == CATEGORY_PROJECT_CONFIG
    assert categorize_file("src/widget.py") == CATEGORY_OTHER
    assert categorize_file("") == CATEGORY_OTHER


# ── build_scope_expansion_signals ────────────────────────────────────────────


def test_build_signals_skips_in_plan_and_is_conservative():
    signals = build_scope_expansion_signals(
        changed_files=["pipeline/engine/scope_expansion.py", "package-lock.json"],
        in_plan_patterns=["pipeline/engine/scope_expansion.py"],
    )
    # Only the out-of-plan file survives; in-plan file is dropped.
    assert [s.path for s in signals] == ["package-lock.json"]
    sig = signals[0]
    # No artefacts supplied → every signal stays conservatively False.
    assert sig.category == CATEGORY_BUILD
    assert sig.verified is False
    assert sig.has_explanation is False
    assert sig.large_diff is False
    assert sig.destructive_delete is False
    assert sig.repeated_across_corrections is False


def test_build_signals_verified_is_per_category():
    signals = build_scope_expansion_signals(
        changed_files=["package-lock.json", "sdk/payloads.py"],
        in_plan_patterns=[],
        gate_status_by_category={CATEGORY_BUILD: "green", CATEGORY_PUBLIC_WIRE: "red"},
        explained_files=["package-lock.json"],
    )
    by_path = {s.path: s for s in signals}
    # The green gate is bound to the build category only.
    assert by_path["package-lock.json"].verified is True
    assert by_path["package-lock.json"].has_explanation is True
    assert by_path["sdk/payloads.py"].verified is False
    assert by_path["sdk/payloads.py"].is_public_wire is True


def test_build_signals_diff_stats_and_sdk_flags():
    signals = build_scope_expansion_signals(
        changed_files=["tests/fixtures/big.json", "docs/sdk_schema.json"],
        in_plan_patterns=[],
        diff_stats_by_file={
            "tests/fixtures/big.json": {"added": 300, "removed": 10},
        },
        repeated_paths=["tests/fixtures/big.json"],
        sdk_flags_by_file={
            "docs/sdk_schema.json": {
                "already_public": True,
                "no_new_exports": True,
                "restores_invariant": True,
            }
        },
    )
    by_path = {s.path: s for s in signals}
    assert by_path["tests/fixtures/big.json"].large_diff is True
    assert by_path["tests/fixtures/big.json"].repeated_across_corrections is True
    schema = by_path["docs/sdk_schema.json"]
    assert schema.sdk_already_public and schema.sdk_no_new_exports
    assert schema.sdk_restores_invariant


# ── classify_file_signals: the status matrix ─────────────────────────────────


def test_build_companion_with_green_gate_is_notice():
    sig = _signals(
        path="package-lock.json",
        category=CATEGORY_BUILD,
        verified=True,
        has_explanation=True,
    )
    item = classify_file_signals(sig)
    assert item.status is ScopeExpansionStatus.NOTICE
    assert "verified" in item.evidence
    assert "explained" in item.evidence


def test_fixture_snapshot_with_green_gate_is_notice():
    sig = _signals(
        path="tests/fixtures/run.json",
        category=CATEGORY_FIXTURE,
        verified=True,
        has_explanation=True,
    )
    assert classify_file_signals(sig).status is ScopeExpansionStatus.NOTICE


def test_sdk_reconciliation_verified_is_notice_with_evidence():
    sig = _signals(
        path="docs/sdk_schema.json",
        category=CATEGORY_SCHEMA,
        verified=True,
        sdk_already_public=True,
        sdk_no_new_exports=True,
        sdk_restores_invariant=True,
    )
    item = classify_file_signals(sig)
    assert item.status is ScopeExpansionStatus.NOTICE
    assert any("sdk-reconciliation" in e for e in item.evidence)


def test_sdk_reconciliation_unverified_is_risk_not_blocker():
    sig = _signals(
        path="docs/sdk_schema.json",
        category=CATEGORY_SCHEMA,
        verified=False,
        sdk_already_public=True,
        sdk_no_new_exports=True,
        sdk_restores_invariant=True,
    )
    item = classify_file_signals(sig)
    assert item.status is ScopeExpansionStatus.RISK
    assert any("sdk-reconciliation" in e for e in item.evidence)


def test_public_wire_without_paired_alignment_is_blocker():
    sig = _signals(
        path="sdk/payloads.py",
        category=CATEGORY_PUBLIC_WIRE,
        verified=True,
        has_explanation=True,
        is_public_wire=True,
        paired_alignment=False,
    )
    item = classify_file_signals(sig)
    assert item.status is ScopeExpansionStatus.BLOCKER
    assert "no-paired-alignment" in item.evidence


def test_public_wire_with_alignment_but_no_reconciliation_is_risk():
    sig = _signals(
        path="sdk/payloads.py",
        category=CATEGORY_PUBLIC_WIRE,
        verified=True,
        has_explanation=True,
        is_public_wire=True,
        paired_alignment=True,
    )
    assert classify_file_signals(sig).status is ScopeExpansionStatus.RISK


def test_persistence_out_of_plan_is_blocker():
    sig = _signals(
        path="storage/run_store.py",
        category=CATEGORY_PERSISTENCE,
        verified=True,
        has_explanation=True,
        is_persistence=True,
    )
    assert classify_file_signals(sig).status is ScopeExpansionStatus.BLOCKER


def test_security_out_of_plan_is_blocker():
    sig = _signals(
        path="app/secret_store.py",
        category=CATEGORY_SECURITY,
        verified=True,
        has_explanation=True,
        is_security=True,
    )
    assert classify_file_signals(sig).status is ScopeExpansionStatus.BLOCKER


def test_no_explanation_benign_is_risk_not_notice():
    sig = _signals(
        path="package-lock.json",
        category=CATEGORY_BUILD,
        verified=True,
        has_explanation=False,
    )
    assert classify_file_signals(sig).status is ScopeExpansionStatus.RISK


def test_unverified_benign_explained_is_risk_not_notice():
    # Downgrade: notice-eligible but verified=False → at least RISK.
    sig = _signals(
        path="package-lock.json",
        category=CATEGORY_BUILD,
        verified=False,
        has_explanation=True,
    )
    assert classify_file_signals(sig).status is ScopeExpansionStatus.RISK


def test_destructive_delete_is_blocker_even_for_benign():
    sig = _signals(
        path="tests/fixtures/run.json",
        category=CATEGORY_FIXTURE,
        verified=True,
        has_explanation=True,
        destructive_delete=True,
    )
    item = classify_file_signals(sig)
    assert item.status is ScopeExpansionStatus.BLOCKER
    assert "destructive-delete" in item.evidence


def test_large_diff_is_blocker_even_for_benign():
    sig = _signals(
        path="tests/fixtures/run.json",
        category=CATEGORY_FIXTURE,
        verified=True,
        has_explanation=True,
        large_diff=True,
    )
    item = classify_file_signals(sig)
    assert item.status is ScopeExpansionStatus.BLOCKER
    assert "large-diff" in item.evidence


def test_repeated_across_corrections_is_blocker():
    sig = _signals(
        path="package-lock.json",
        category=CATEGORY_BUILD,
        verified=True,
        has_explanation=True,
        repeated_across_corrections=True,
    )
    item = classify_file_signals(sig)
    assert item.status is ScopeExpansionStatus.BLOCKER
    assert "repeated-across-corrections" in item.evidence


def test_sdk_reconciliation_with_destructive_delete_is_blocker():
    # A reconciliation flag must not rescue a destructive change.
    sig = _signals(
        path="docs/sdk_schema.json",
        category=CATEGORY_SCHEMA,
        verified=True,
        sdk_already_public=True,
        sdk_no_new_exports=True,
        sdk_restores_invariant=True,
        destructive_delete=True,
    )
    assert classify_file_signals(sig).status is ScopeExpansionStatus.BLOCKER


# ── assessment + to_dict + render stability ──────────────────────────────────


def _mixed_assessment() -> ScopeExpansionAssessment:
    signals = [
        _signals(
            path="package-lock.json",
            category=CATEGORY_BUILD,
            verified=True,
            has_explanation=True,
        ),
        _signals(
            path="docs/notes.md",
            category=CATEGORY_OTHER,
            verified=False,
        ),
        _signals(
            path="storage/run_store.py",
            category=CATEGORY_PERSISTENCE,
            is_persistence=True,
        ),
    ]
    return build_scope_expansion_assessment(signals)


def test_assessment_projections_and_has_blocker():
    assessment = _mixed_assessment()
    assert len(assessment.notices) == 1
    assert len(assessment.risks) == 1
    assert len(assessment.blockers) == 1
    assert assessment.has_blocker is True


def test_assessment_to_dict_is_json_safe_and_stable():
    assessment = _mixed_assessment()
    d = assessment.to_dict()
    assert d["has_blocker"] is True
    assert d["counts"] == {"notice": 1, "risk": 1, "blocker": 1}
    # enum projected to its string value; evidence is a plain list.
    statuses = {item["status"] for item in d["items"]}
    assert statuses == {
        "scope_expansion_notice",
        "scope_expansion_risk",
        "scope_expansion_blocker",
    }
    for item in d["items"]:
        assert isinstance(item["evidence"], list)
    # Stable across repeated calls.
    assert assessment.to_dict() == d


def test_item_and_signals_to_dict_round_trip():
    item = ScopeExpansionItem(
        path="a.json",
        category=CATEGORY_FIXTURE,
        status=ScopeExpansionStatus.NOTICE,
        evidence=("verified", "explained"),
    )
    assert item.to_dict() == {
        "path": "a.json",
        "category": CATEGORY_FIXTURE,
        "status": "scope_expansion_notice",
        "evidence": ["verified", "explained"],
    }
    sig = _signals(path="a.json", category=CATEGORY_FIXTURE, verified=True)
    sig_dict = sig.to_dict()
    assert sig_dict["path"] == "a.json"
    assert sig_dict["verified"] is True
    assert sig.to_dict() == sig_dict


def test_render_scope_expansion_lines_is_grouped_and_stable():
    assessment = _mixed_assessment()
    lines = render_scope_expansion_lines(assessment.to_dict())
    assert lines[0] == "Scope expanded:"
    assert "Scope expansion risk:" in lines
    assert "Scope expansion blocker:" in lines
    # Each item line carries "<path> — <category>; <evidence>".
    assert any(line.strip().startswith("package-lock.json — build;") for line in lines)
    assert any("storage/run_store.py — persistence" in line for line in lines)
    # Pure: same input → identical output.
    assert render_scope_expansion_lines(assessment.to_dict()) == lines


def test_render_empty_assessment_is_empty():
    assert render_scope_expansion_lines(ScopeExpansionAssessment().to_dict()) == ()


# ── seam: the classifier owns no status→verdict coupling (ADR 0112 §5) ───────


def test_classifier_module_has_no_status_to_verdict_coupling():
    """The classifier stays a pure fact producer: its executable code names no
    release verdict (``REJECTED`` / reject coupling) and never imports the
    OperatingMode sanction projection. The route a status takes is the
    consumer's concern. (Docstrings may *describe* the seam; only code/imports
    are asserted on, via AST, so the seam comment does not trip the guard.)"""
    import ast

    import pipeline.engine.scope_expansion as mod

    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    # No import of the runtime sanction projection (one-way: engine ⇏ runtime).
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any("scope_expansion_sanction" in m for m in imported)
    assert not any("pipeline.runtime" in m for m in imported)
    # No verdict term appears in executable code (strings/identifiers), only in
    # docstrings — strip docstrings and string constants, then scan.
    code_terms: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            code_terms.append(node.id)
        elif isinstance(node, ast.Attribute):
            code_terms.append(node.attr)
    joined = " ".join(code_terms).lower()
    assert "reject" not in joined
    # has_blocker remains a pure fact, not a verdict.
    assessment = build_scope_expansion_assessment(
        build_scope_expansion_signals(
            changed_files=["storage/state.py"],
            in_plan_patterns=(),
            diff_stats_by_file={},
        )
    )
    assert isinstance(assessment.has_blocker, bool)
