from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pipeline.engine.workspace_run_retention import (
    RETENTION_UNSET,
    RunRootFacts,
    evaluate_run_root,
    resolve_retention_deadline,
)

NOW = datetime(2026, 7, 29, tzinfo=UTC)


def _facts(**changes: object) -> RunRootFacts:
    values: dict[str, object] = {
        "root_id": "20260601_000000_legacy",
        "run_dir": Path("/runs/root"),
        "meta": {"status": "done"},
        "meta_exists": True,
        "status": "done",
        "retention_until": "2026-07-28T00:00:00Z",
        "active_handoff": False,
        "checkpoint_handoff": False,
        "active_gate": False,
        "path_safe": True,
        "nested_checkout_unidentified": False,
        "checkout_blocker": None,
        "dependency_paths": (),
    }
    values.update(changes)
    return RunRootFacts(**values)  # type: ignore[arg-type]


def test_deadline_uses_stamp_then_legacy_prefix_without_mtime():
    deadline, error = resolve_retention_deadline(
        "20260601_000000_suffix",
        RETENTION_UNSET,
        older_than=timedelta(days=30),
    )
    assert (deadline, error) == (datetime(2026, 7, 1, tzinfo=UTC), None)
    assert (
        resolve_retention_deadline("bad", RETENTION_UNSET, older_than=timedelta(days=30))[1]
        == "root_id_timestamp_invalid"
    )
    assert (
        resolve_retention_deadline("20260601_000000", "nope", older_than=timedelta(days=30))[1]
        == "retention_invalid"
    )


@pytest.mark.parametrize(
    ("stamp", "reason"),
    [
        ("2026-07-29T00:00:01Z", "retention_active"),
        ("2026-07-29T00:00:00Z", "retention_expired"),
        ("2026-07-28T23:59:59Z", "retention_expired"),
    ],
)
def test_deadline_boundary_is_absolute(stamp: str, reason: str):
    assert (
        evaluate_run_root(
            _facts(retention_until=stamp), now=NOW, older_than=timedelta(days=30)
        ).reason
        == reason
    )


@pytest.mark.parametrize(
    ("changes", "selected", "reason"),
    [
        ({"meta": None, "meta_exists": False, "retention_until": RETENTION_UNSET}, True, "retention_expired"),
        ({"retention_until": RETENTION_UNSET}, True, "retention_expired"),
        ({"dependency_paths": ()}, True, "retention_expired"),  # checkout-gone is inert
        ({"dependency_paths": ()}, True, "retention_expired"),  # already-reclaimed is inert
        (
            {"status": "awaiting_phase_handoff", "active_handoff": True},
            True,
            "pause_retention_expired",
        ),
        ({"retention_until": "2026-08-01T00:00:00Z"}, False, "retention_active"),
        ({"checkout_blocker": "uncommitted_changes"}, False, "uncommitted_changes"),
        ({"checkout_blocker": "unpushed_commits"}, False, "unpushed_commits"),
    ],
)
def test_eight_form_root_matrix(changes: dict[str, object], selected: bool, reason: str):
    verdict = evaluate_run_root(_facts(**changes), now=NOW, older_than=timedelta(days=30))
    assert (verdict.selected, verdict.reason) == (selected, reason)


def test_checkpoint_only_and_real_gate_remain_blocking():
    assert (
        evaluate_run_root(
            _facts(checkpoint_handoff=True), now=NOW, older_than=timedelta(days=30)
        ).reason
        == "checkpoint_handoff_active"
    )
    assert evaluate_run_root(
        _facts(active_gate=True, retention_until="2026-08-01T00:00:00Z"),
        now=NOW,
        older_than=timedelta(days=30),
    ).reason == "active_handoff_or_gate"


def test_expired_real_gate_stays_fail_closed_but_stale_handoff_expires():
    # A real active gate is a live coordination point: it stays protected even
    # after its retention deadline has passed. Age-gating is only for a stale
    # open pause/handoff.
    gated = evaluate_run_root(
        _facts(active_gate=True, retention_until="2026-07-28T00:00:00Z"),
        now=NOW,
        older_than=timedelta(days=30),
    )
    assert (gated.selected, gated.reason) == (False, "active_handoff_or_gate")

    # Same age, a stale open pause with an active handoff and no real gate: the
    # dead pause no longer holds its run root once the deadline has passed.
    stale = evaluate_run_root(
        _facts(
            status="awaiting_phase_handoff",
            active_handoff=True,
            retention_until="2026-07-28T00:00:00Z",
        ),
        now=NOW,
        older_than=timedelta(days=30),
    )
    assert (stale.selected, stale.reason) == (True, "pause_retention_expired")

    # A young pause (deadline not yet reached) still protects, exactly as before.
    young = evaluate_run_root(
        _facts(
            status="awaiting_phase_handoff",
            active_handoff=True,
            retention_until="2026-08-15T00:00:00Z",
        ),
        now=NOW,
        older_than=timedelta(days=30),
    )
    assert (young.selected, young.reason) == (False, "active_handoff_or_gate")


def test_absent_meta_still_fails_closed_for_a_checkpoint_gate():
    verdict = evaluate_run_root(
        _facts(meta=None, meta_exists=False, active_gate=True, retention_until=RETENTION_UNSET),
        now=NOW,
        older_than=timedelta(days=30),
    )
    assert (verdict.selected, verdict.reason) == (False, "active_handoff_or_gate")
