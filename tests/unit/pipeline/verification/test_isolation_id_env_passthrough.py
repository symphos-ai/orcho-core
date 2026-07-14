"""ADR 0131: gate commands must inherit ``ORCHO_ISOLATION_ID``.

The isolation namespace is only useful if BOTH ``worktree_bootstrap`` (which
inherits the full ``os.environ``) and the verification gate commands see the
same value — otherwise bootstrap brings up ``orcho_<id>`` and the gate targets a
different stack. Gate env is ``os.environ`` minus ``RUN_SCOPED_ENV_CHANNELS``, so
the contract is simply that the isolation id is not on that strip list.
"""

from __future__ import annotations

from pipeline.verification_env import RUN_SCOPED_ENV_CHANNELS


def test_isolation_id_is_not_stripped_from_gate_env() -> None:
    assert "ORCHO_ISOLATION_ID" not in RUN_SCOPED_ENV_CHANNELS
    # Sanity: the run-id it mirrors is likewise inherited by gates.
    assert "ORCHO_RUN_ID" not in RUN_SCOPED_ENV_CHANNELS
