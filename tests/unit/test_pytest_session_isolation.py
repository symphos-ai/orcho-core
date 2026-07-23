"""Shared pytest configuration must not delete another active session's root."""

from __future__ import annotations

import tests.conftest as shared_conftest


def test_shared_conftest_has_no_global_temp_root_cleanup_hook() -> None:
    """Pytest owns retention; a session must not prune sibling sessions."""
    assert not hasattr(shared_conftest, "pytest_sessionfinish")
