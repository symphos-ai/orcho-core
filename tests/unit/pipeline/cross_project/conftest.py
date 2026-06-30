"""Test-only suppression of unsupported cross-handoff fail-fast cases.

Phase 5 cutover declared non-bypass ``handoff`` on the built-in
``advanced`` / ``enterprise`` / ``plan`` profiles. Phase 6 added a
projection-time fail-fast that refuses projected non-bypass shapes the
cross orchestrator cannot honour. ADR 0038 and ADR 0039 later opened
specific supported shapes; other shapes still stay guarded.

These cross tests pre-date Phase 5; they exercise legitimate cross
runner mechanics (gate policies, projection scope, metrics, …) and
expect the legacy cross dispatch to succeed against the built-in
catalogue. They are not exercising the handoff contract — that's the
single-project test surface's job.

This autouse fixture monkey-patches the projection-side check to a
no-op for legacy cross tests that are not about handoff behaviour.
Production code paths (CLI, SDK, MCP) keep the guard intact; handoff
contract tests opt out of this fixture with
``preserve_handoff_fail_fast``.
"""
from __future__ import annotations

import pytest

from pipeline.cross_project import profile_projection as _projection


@pytest.fixture(autouse=True)
def _suppress_cross_handoff_fail_fast(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The handoff-fail-fast tests in ``test_profile_projection.py``
    # exercise the contract directly — they need the guard intact. Opt
    # them out via the ``preserve_handoff_fail_fast`` marker rather
    # than carrying the suppression class-wide.
    if request.node.get_closest_marker("preserve_handoff_fail_fast"):
        return
    # Patch both the projected-output guard (production path) and the
    # legacy raw-profile shim (used in earlier test passes / direct
    # callers) so suppression covers both code paths.
    monkeypatch.setattr(
        _projection,
        "_reject_non_bypass_handoff_in_projection",
        lambda profile_name, global_steps, project_steps: None,
    )
    monkeypatch.setattr(
        _projection,
        "_reject_non_bypass_handoff",
        lambda profile: None,
    )
