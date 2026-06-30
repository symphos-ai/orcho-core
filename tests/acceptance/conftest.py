"""Test-only suppression of the cross-handoff fail-fast for acceptance.

Mirror of ``tests/unit/pipeline/cross_project/conftest.py``: Phase 5
declared non-bypass ``handoff`` on the built-in ``advanced`` /
``enterprise`` / ``plan`` profiles, and Phase 6 added a
projection-time fail-fast in ``pipeline.cross_project.profile_projection``
that refuses such profiles in cross mode (cross handoff support is a
later slice).

Acceptance tests in this directory exercise both single-project and
cross-project flows against the built-in catalogue. The suppression is
narrowly scoped to ``_reject_non_bypass_handoff`` — single-project
runs use ``pipeline.runtime.runner._validate_handoff_support`` which
remains in force, so this fixture does not weaken single-project
contract coverage.
"""
from __future__ import annotations

import pytest

from pipeline.cross_project import profile_projection as _projection


@pytest.fixture(autouse=True)
def _suppress_cross_handoff_fail_fast(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Mirror of the unit/cross_project conftest: acceptance tests that
    # want to verify production cross fail-fast behaviour opt out via
    # ``@pytest.mark.preserve_handoff_fail_fast``. Without this opt-out
    # the suppression would silently downgrade future regression tests
    # for the production guard.
    if request.node.get_closest_marker("preserve_handoff_fail_fast"):
        return
    # Patch both the projected-output guard (production path) and the
    # legacy raw-profile shim so suppression covers both code paths.
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
