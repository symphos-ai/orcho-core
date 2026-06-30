"""Cross-plan reviewer findings must surface on BOTH validate paths.

Regression: the operator-retry rejection path historically only emitted a terse
"also rejected" warning, so the operator hit the continue/retry/halt handoff with
no findings (forced to dig the critique out of events.jsonl). The shared helper
``_render_cross_validate_findings`` removes that automatic-vs-retry asymmetry by
construction — both paths render through it.
"""
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
