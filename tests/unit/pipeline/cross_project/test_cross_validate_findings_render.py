"""Cross-plan reviewer findings must surface on BOTH validate paths.

Regression: the operator-retry rejection path historically only emitted a terse
"also rejected" warning, so the operator hit the continue/retry/halt handoff with
no findings (forced to dig the critique out of events.jsonl). The shared helper
``_render_cross_validate_findings`` removes that automatic-vs-retry asymmetry by
construction — both paths render through it.
"""
import pytest

from pipeline.cross_project.planning_loop import _render_cross_validate_findings


def _capture():
    sink: list[str] = []
    return sink, sink.append


def test_renders_findings_when_review_present():
    sink, print_fn = _capture()
    review = {
        "verdict": "REJECTED",
        "short_summary": "SUMMARY_MARK",
        "findings": [
            {"id": "F1", "severity": "P1", "title": "TITLE_MARK", "body": "BODY_MARK"},
        ],
    }

    _render_cross_validate_findings(review, print_fn)

    blob = "\n".join(sink)
    assert blob.strip(), "expected a rendered findings block"
    # Surfaces the actual critique content — not just a non-empty line.
    assert "REJECTED" in blob
    assert "TITLE_MARK" in blob
    assert "SUMMARY_MARK" in blob


def test_noop_when_no_review():
    sink, print_fn = _capture()

    _render_cross_validate_findings(None, print_fn)
    _render_cross_validate_findings({}, print_fn)

    assert sink == [], "no review dict → nothing rendered (symmetric skip on both paths)"


@pytest.fixture(autouse=True)
def _live_output_mode_for_full_transcript():
    """Pin the full live transcript shape (T2 summary reconciliation).

    ``summary`` is the default run-output mode — the compact append-only
    arc that collapses phase headers to ``▶ <phase>`` and the review /
    plan / implement outcome blocks to single lines. These tests assert
    the full-fidelity transcript, so force ``live`` (rendering only; no
    echo / verbose / trace side effects) and restore afterwards.
    """
    from core.observability import logging as _logging

    _before = _logging.get_output_mode()
    _logging._output_mode = "live"
    try:
        yield
    finally:
        _logging._output_mode = _before
