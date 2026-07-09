# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ``pipeline.control.scope_handoff_digest``.

The digest is the decision surface for an engine-issued ``scope_expansion:*``
pause: it must show the scope delta (out-of-plan changes vs declared in-plan
patterns) and disambiguate the engine REJECTED from the reviewer's own verdict.
Both the classifier and the renderer are pure — primitives in, strings out.
"""

from __future__ import annotations

from core.io.ansi import strip_ansi
from pipeline.control.scope_handoff_digest import (
    ScopeExpansionDigest,
    ScopeExpansionFinding,
    classify_scope_expansion,
    is_scope_expansion_trigger,
    render_scope_expansion_digest,
)

_OUT_OF_PLAN_ARTIFACTS = {
    "operating_mode": "governed",
    "handoff_paths": ["engine/verify/runner.py"],
    "findings": [
        {
            "path": "engine/verify/runner.py",
            "category": "test",
            "status": "scope_expansion_risk",
            "evidence": ["gate-not-verified", "large-diff"],
        },
    ],
    "in_plan_patterns": ["bin/session-key.mjs", "engine/verify/paths.py"],
}


class TestTriggerFamily:
    def test_out_of_plan_and_participant_add_match(self) -> None:
        assert is_scope_expansion_trigger("scope_expansion:out_of_plan")
        assert is_scope_expansion_trigger(
            "scope_expansion:participant_add:orcho-mcp"
        )

    def test_other_triggers_and_none_do_not_match(self) -> None:
        assert not is_scope_expansion_trigger("rejected")
        assert not is_scope_expansion_trigger("incomplete")
        assert not is_scope_expansion_trigger("")
        assert not is_scope_expansion_trigger(None)


class TestClassify:
    def test_out_of_plan_artifacts_fully_classified(self) -> None:
        digest = classify_scope_expansion(_OUT_OF_PLAN_ARTIFACTS)
        assert digest.operating_mode == "governed"
        assert digest.handoff_paths == ("engine/verify/runner.py",)
        assert digest.in_plan_patterns == (
            "bin/session-key.mjs", "engine/verify/paths.py",
        )
        assert digest.participant_repo == ""
        assert digest.findings == (
            ScopeExpansionFinding(
                path="engine/verify/runner.py",
                category="test",
                status="scope_expansion_risk",
                evidence=("gate-not-verified", "large-diff"),
            ),
        )

    def test_participant_add_artifacts(self) -> None:
        digest = classify_scope_expansion(
            {"operating_mode": "governed", "participant_repo": "orcho-mcp"},
        )
        assert digest.participant_repo == "orcho-mcp"
        assert digest.findings == ()
        assert digest.in_plan_patterns == ()

    def test_missing_and_malformed_fields_degrade(self) -> None:
        digest = classify_scope_expansion(
            {
                # A findings entry without a path (e.g. the older reviewer
                # finding shape) drops instead of raising.
                "findings": [
                    {"id": "S1", "severity": "P2", "title": "out-of-plan"},
                    "not-a-mapping",
                ],
                "handoff_paths": "not-a-list",
                "in_plan_patterns": None,
            },
        )
        assert digest == ScopeExpansionDigest(
            operating_mode="",
            findings=(),
            handoff_paths=(),
            in_plan_patterns=(),
            participant_repo="",
        )


class TestRender:
    def test_scope_delta_and_verdict_provenance(self) -> None:
        digest = classify_scope_expansion(_OUT_OF_PLAN_ARTIFACTS)
        body = strip_ansi(render_scope_expansion_digest(digest))
        assert "Why paused" in body
        assert "out-of-plan scope expansion (governed mode)" in body
        # The single most confusing byte of the legacy layout: an engine
        # REJECTED above an APPROVED reviewer transcript.
        assert "issued by the engine scope-expansion sanction" in body
        assert "reviewer transcript below may say APPROVED" in body
        # The full scope delta: offending file with classification + evidence,
        # and the declared scope it was judged against.
        assert (
            "Out of plan: engine/verify/runner.py"
            "  [test · scope_expansion_risk]" in body
        )
        assert "evidence: gate-not-verified; large-diff" in body
        assert (
            "Declared scope: bin/session-key.mjs, engine/verify/paths.py"
            in body
        )

    def test_bare_paths_render_without_findings(self) -> None:
        # An older persisted payload (or a malformed findings artifact) still
        # shows the offending paths from handoff_paths.
        digest = classify_scope_expansion(
            {"handoff_paths": ["sdk/new_wire.py"]},
        )
        body = strip_ansi(render_scope_expansion_digest(digest))
        assert "Out of plan: sdk/new_wire.py" in body
        assert "evidence:" not in body
        assert "Declared scope: (not recorded on this signal)" in body

    def test_participant_add_renders_out_of_set_repo(self) -> None:
        digest = classify_scope_expansion(
            {"operating_mode": "governed", "participant_repo": "orcho-mcp"},
        )
        body = strip_ansi(render_scope_expansion_digest(digest))
        assert "out-of-set repository discovered mid-run (governed mode)" in body
        assert "Out of set: orcho-mcp" in body
        assert "Out of plan:" not in body

    def test_long_scope_collapses_with_more_marker(self) -> None:
        digest = classify_scope_expansion(
            {
                "handoff_paths": ["x.py"],
                "in_plan_patterns": [f"src/mod_{i}.py" for i in range(14)],
            },
        )
        body = strip_ansi(render_scope_expansion_digest(digest))
        assert "src/mod_9.py" in body
        assert "src/mod_10.py" not in body
        assert "(+4 more)" in body
