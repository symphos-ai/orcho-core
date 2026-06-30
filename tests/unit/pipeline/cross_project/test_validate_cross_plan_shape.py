"""Tests for ``validate_cross_plan``'s return shape.

The helper returns a ``CrossValidateResult`` dataclass that surfaces the
reviewer prompt and raw output so the cross orchestrator can feed them
into ``_capture_invoke_usage`` for token-split estimation on runtimes
that only surface ``last_tokens_total`` (Codex).
"""
from __future__ import annotations

import json

import pytest

from pipeline.cross_project.planning_loop import (
    CrossValidateResult as _CrossValidateResult,
    validate_cross_plan as _validate_cross_plan,
)


class _FakeQA:
    model = "fake-codex"

    def __init__(self, response: str | Exception) -> None:
        self._response = response
        self.prompts: list[str] = []

    def invoke(self, prompt: str, cwd: str, **_kw) -> str:
        self.prompts.append(prompt)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _approved_review() -> str:
    return json.dumps({
        "verdict": "APPROVED",
        "short_summary": "Plan is coherent across projects.",
        "findings": [],
    })


def test_approve_path_returns_dataclass_with_prompt_and_raw():
    qa = _FakeQA(_approved_review())
    res = _validate_cross_plan(
        qa, "# plan body", "Some task", ["api", "web"], "/tmp",
    )
    assert isinstance(res, _CrossValidateResult)
    assert res.approved is True
    assert res.prompt_text != ""
    assert res.prompt_text == qa.prompts[0]
    assert res.raw_output == _approved_review()
    assert res.review_dict["verdict"] == "APPROVED"


def test_parse_error_path_still_carries_prompt_and_raw():
    qa = _FakeQA("not json at all")
    res = _validate_cross_plan(
        qa, "# plan body", "Some task", ["api"], "/tmp",
    )
    assert res.approved is False
    assert res.prompt_text != ""
    assert res.raw_output == "not json at all"
    assert res.review_dict["verdict"] == "REJECTED"
    assert "parse_error" in res.review_dict


def test_invoke_runtime_error_keeps_prompt_drops_raw():
    qa = _FakeQA(RuntimeError("codex CLI exited 1"))
    res = _validate_cross_plan(
        qa, "# plan body", "Some task", ["api"], "/tmp",
    )
    assert res.approved is False
    assert res.prompt_text != ""
    assert res.raw_output == ""
    assert "codex CLI exited 1" in res.review_dict["parse_error"]


def test_dataclass_is_frozen():
    qa = _FakeQA(_approved_review())
    res = _validate_cross_plan(
        qa, "# plan body", "Some task", ["api"], "/tmp",
    )
    with pytest.raises((AttributeError, Exception)):
        res.approved = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Path-leak invariant for cross_validate_plan.
#
# Symmetric to the single-project ``test_dynamic_artifact_parts.py``
# ``TestArtifactWirePresence`` block. ``cross_plan_review_focus`` used
# to embed ``f"Plan artifact under review: {plan_artifact_path}\n\n
# {plan_artifact}"`` directly into the wire body of the artifact part
# (and into the minimal-mode intent). After the path-separation fix
# the wire body carries only the plan content; the on-disk location
# lives on the PromptPart as metadata-only ``artifact_path``.
# ---------------------------------------------------------------------------


# Sentinel path chosen so it cannot collide with task / aliases /
# plan body / role+format prose. Any leak into wire bytes is then a
# real leak, not a false-positive substring.
_CROSS_PATH_SENTINEL = "/tmp/ZZSENTINELPATH_cross_plan_42.md"


def test_cross_plan_review_focus_strips_path_from_wire_body_full_mode() -> None:
    from pipeline.cross_project.orchestrator import cross_plan_review_focus

    plan_body = "## Cross plan\n\nReviewed plan body sentinel."
    turn = cross_plan_review_focus(
        task="Demo cross task",
        aliases=["api", "web"],
        plan_artifact=plan_body,
        plan_artifact_path=_CROSS_PATH_SENTINEL,
    )
    out = turn.text if hasattr(turn, "text") else turn
    # Plan body still on the wire — once, in the artifact part.
    assert out.count("Reviewed plan body sentinel.") == 1
    # The on-disk path NEVER appears in the wire bytes — it travels as
    # metadata on the artifact PromptPart only.
    assert _CROSS_PATH_SENTINEL not in out
    # The legacy "Plan artifact under review: <path>" preamble is gone.
    assert "Plan artifact under review:" not in out


def test_cross_plan_review_focus_strips_path_from_wire_body_minimal_mode() -> None:
    from pipeline.cross_project.orchestrator import cross_plan_review_focus

    plan_body = "## Cross plan\n\nMinimal-mode body sentinel."
    turn = cross_plan_review_focus(
        task="Demo cross task",
        aliases=["api", "web"],
        plan_artifact=plan_body,
        plan_artifact_path=_CROSS_PATH_SENTINEL,
        professional_prompt_mode="minimal",
    )
    out = turn.text if hasattr(turn, "text") else turn
    assert out.count("Minimal-mode body sentinel.") == 1
    # Minimal mode does not have a PromptPart envelope for metadata,
    # so the path simply never reaches the wire at all (it stays in
    # the caller's evidence/trace surface).
    assert _CROSS_PATH_SENTINEL not in out
    assert "Plan artifact under review:" not in out


def test_cross_plan_review_focus_publishes_artifact_path_as_metadata() -> None:
    from pipeline.cross_project.orchestrator import cross_plan_review_focus

    turn = cross_plan_review_focus(
        task="Demo cross task",
        aliases=["api", "web"],
        plan_artifact="## Cross plan\n\nBody.",
        plan_artifact_path=_CROSS_PATH_SENTINEL,
    )
    env = turn.envelope() if hasattr(turn, "envelope") else None
    assert env is not None
    artifact_parts = [
        p for p in env.parts
        if p.kind == "artifact" and p.name == "cross_validate_plan"
    ]
    assert len(artifact_parts) == 1, (
        "expected exactly one artifact:cross_validate_plan part"
    )
    artifact = artifact_parts[0]
    # The path travels as PromptPart metadata, not as wire body.
    assert artifact.artifact_path == _CROSS_PATH_SENTINEL
    # Body carries only the reviewed content — no path prefix.
    assert _CROSS_PATH_SENTINEL not in artifact.body
    assert "Plan artifact under review:" not in artifact.body
