"""Auto-reset the sandbox-policy ContextVar between tests.

A test elsewhere in the suite may call
:func:`set_active_sandbox_policy` without a paired
``reset_active_sandbox_policy`` (orchestrator tests doing this
intentionally, fixture cleanup races, etc.). The ContextVar then
leaks into our tests and breaks the default-is-None assertion.

The fixture forces the var to None *before* each sandbox test so
each test in this directory starts from a known-empty state. After
the test runs, we restore whatever it set (so a test that
deliberately leaves a policy active does not interfere with our
own reset).
"""
from __future__ import annotations

import pytest

from pipeline.sandbox.context import _active_policy


@pytest.fixture(autouse=True)
def _clean_active_sandbox_policy():
    # Force a fresh, default-None state for the sandbox tests.
    token = _active_policy.set(None)
    try:
        yield
    finally:
        _active_policy.reset(token)
