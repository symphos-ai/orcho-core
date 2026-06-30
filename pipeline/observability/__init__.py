"""pipeline.observability — read-only projections over the canonical
session shape.

M12 introduces durable per-phase prompt-render traces. The extractor
in :mod:`pipeline.observability.prompt_render` walks the
``session["phases"]`` tree per the coverage contract pinned in
``tests/unit/pipeline/observability/test_prompt_render_coverage.py``.
It does not mutate sessions, does not synthesize records for
documented exceptions, and does not surface raw prompt bodies.
"""
