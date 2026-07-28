"""Unhandled cross-stage failures settle the durable parent before escaping."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import pipeline.cross_project.session_run as session_run

pytestmark = [pytest.mark.cross_project]


class _ProviderFailure(RuntimeError):
    pass


def test_provider_failure_marks_parent_failed_and_preserves_exception(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "cross-run"
    run_dir.mkdir()
    session = {
        "run_id": run_dir.name,
        "status": "running",
        "phases": {},
    }
    ctx = SimpleNamespace(
        run_dir=run_dir,
        session=session,
        session_ts=run_dir.name,
        cross_ckpt={"phase0_done": False, "sub_status": {}},
    )
    request = SimpleNamespace(resume_from=None)
    provider_failure = _ProviderFailure("provider unavailable")

    with (
        patch.object(session_run, "_setup_cross_run", return_value=ctx),
        patch.object(session_run, "_run_cross_hypothesis"),
        patch.object(session_run, "_resolve_global_plan_steps"),
        patch.object(
            session_run,
            "_run_planning",
            side_effect=provider_failure,
        ),
        pytest.raises(_ProviderFailure) as raised,
    ):
        session_run.run_cross_pipeline_session(request)

    assert raised.value is provider_failure
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "failed"
    assert meta["halt_reason"] == "cross_unhandled_exception"
    assert meta["failure_reason"] == (
        "_ProviderFailure: provider unavailable"
    )


def test_settlement_failure_does_not_replace_provider_failure(
    tmp_path: Path,
) -> None:
    ctx = SimpleNamespace(
        run_dir=tmp_path,
        session={"status": "running", "phases": {}},
        session_ts="cross-run",
        cross_ckpt={},
    )
    request = SimpleNamespace(resume_from=None)
    provider_failure = _ProviderFailure("provider unavailable")

    with (
        patch.object(session_run, "_setup_cross_run", return_value=ctx),
        patch.object(session_run, "_run_cross_hypothesis"),
        patch.object(session_run, "_resolve_global_plan_steps"),
        patch.object(
            session_run,
            "_run_planning",
            side_effect=provider_failure,
        ),
        patch.object(
            session_run,
            "finalize_cross_terminal",
            side_effect=OSError("disk unavailable"),
        ),
        pytest.raises(_ProviderFailure) as raised,
    ):
        session_run.run_cross_pipeline_session(request)

    assert raised.value is provider_failure
    assert raised.value.__notes__ == [
        "Cross parent terminal settlement also failed: "
        "OSError: disk unavailable"
    ]
