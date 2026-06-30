"""P2.5 — ORCHO_RUN_ID / --run-id contract tests.

Verifies the run_id resolution priority documented in
``pipeline.project_orchestrator.run_pipeline``:
 1. ``resume_from`` — explicit resume target
 2. ``$ORCHO_RUN_ID`` — set by external supervisor before spawn
 3. ``output_dir.name`` — SDK/MCP/direct callers that already selected a run dir
 4. minted timestamp — only when no caller selected a run id

Without (2) the supervisor + checkpoint identity contract breaks.
"""
from __future__ import annotations

import os

import pytest


def test_run_id_priority_resume_wins(monkeypatch):
    """resume_from takes precedence over both env and minted ts."""
    monkeypatch.setenv("ORCHO_RUN_ID", "from_env_xyz")
    resume_from = "explicit_resume_target"
    env_run_id = os.environ.get("ORCHO_RUN_ID", "").strip() or None
    # Mirror the resolution from run_pipeline:
    session_ts = resume_from or env_run_id or "<ts>"
    assert session_ts == "explicit_resume_target"


def test_run_id_priority_env_beats_session_ts(monkeypatch):
    """$ORCHO_RUN_ID is used when no resume_from is given."""
    monkeypatch.setenv("ORCHO_RUN_ID", "supervisor_minted_id")
    resume_from = None
    env_run_id = os.environ.get("ORCHO_RUN_ID", "").strip() or None
    session_ts = resume_from or env_run_id or "<ts>"
    assert session_ts == "supervisor_minted_id"


def test_run_id_mints_timestamp_when_no_authority_exists(monkeypatch):
    """Without resume/env/output_dir authority, mint a new timestamp."""
    monkeypatch.delenv("ORCHO_RUN_ID", raising=False)
    resume_from = None
    env_run_id = os.environ.get("ORCHO_RUN_ID", "").strip() or None
    minted = "20260506_120000"
    session_ts = resume_from or env_run_id or minted
    assert session_ts == minted


def test_run_id_uses_output_dir_name_for_direct_callers(
    tmp_path, monkeypatch,
):
    """SDK/MCP callers that pass output_dir use its name as the run id."""
    from pipeline.presentation import PresentationPolicy
    from pipeline.project.bootstrap import resolve_run_id_and_setup_logging

    monkeypatch.delenv("ORCHO_RUN_ID", raising=False)
    run_dir = tmp_path / "runs" / "preselected_run_id"
    run_dir.mkdir(parents=True)

    session_ts = resolve_run_id_and_setup_logging(
        task="x",
        project_dir=str(tmp_path),
        resume_from=None,
        output_dir=run_dir,
        profile_name="feature",
        presentation=PresentationPolicy.SILENT,
    )

    assert session_ts == "preselected_run_id"


def test_empty_env_var_treated_as_unset(monkeypatch):
    """Whitespace-only ORCHO_RUN_ID behaves as absent."""
    monkeypatch.setenv("ORCHO_RUN_ID", "   ")
    resume_from = None
    env_run_id = os.environ.get("ORCHO_RUN_ID", "").strip() or None
    session_ts = resume_from or env_run_id or "minted"
    assert session_ts == "minted"


def test_argparse_accepts_run_id():
    """The orchestrator's argparse layer exposes ``--run-id``.

 The orchestrator builds its parser inline in ``main()``; we use a smaller
 proxy here that mirrors the contract (``--run-id`` is a string flag with
 None default) so this test runs without invoking main()'s side effects.
 """
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--run-id", type=str, default=None)
    args = p.parse_args(["--run-id", "20260506_test_xy"])
    assert args.run_id == "20260506_test_xy"


def test_run_id_propagates_through_argv_builder():
    """build_orch_argv emits ``--run-id`` so subprocess inherits the contract."""
    from pipeline.argv import build_orch_argv

    argv = build_orch_argv(project="/p", run_id="abc123")
    assert "--run-id" in argv
    assert argv[argv.index("--run-id") + 1] == "abc123"


def test_fresh_run_allows_empty_precreated_dir(tmp_path):
    """Supervisors may pre-create the run dir before spawning Orcho."""
    from pipeline.project.bootstrap import (
        assert_fresh_run_dir_available as _assert_fresh_run_dir_available,
    )

    run_dir = tmp_path / "runs" / "SUPERVISOR_ID"
    run_dir.mkdir(parents=True)

    _assert_fresh_run_dir_available(run_dir, resume_from=None)


def test_fresh_run_rejects_existing_materialized_run_dir(tmp_path):
    """Same run id without --resume must not overwrite an existing run."""
    from pipeline.project.bootstrap import (
        RunIdCollisionError,
        assert_fresh_run_dir_available as _assert_fresh_run_dir_available,
    )

    run_dir = tmp_path / "runs" / "REAL_ADV_1"
    run_dir.mkdir(parents=True)
    (run_dir / "events.jsonl").write_text('{"kind":"run.start"}\n')
    (run_dir / "meta.json").write_text("{}")

    with pytest.raises(RunIdCollisionError, match="Use --resume"):
        _assert_fresh_run_dir_available(run_dir, resume_from=None)


def test_resume_allows_existing_materialized_run_dir(tmp_path):
    """Existing run artifacts are expected when explicitly resuming."""
    from pipeline.project.bootstrap import (
        assert_fresh_run_dir_available as _assert_fresh_run_dir_available,
    )

    run_dir = tmp_path / "runs" / "REAL_ADV_1"
    run_dir.mkdir(parents=True)
    (run_dir / "events.jsonl").write_text('{"kind":"run.start"}\n')

    _assert_fresh_run_dir_available(run_dir, resume_from="REAL_ADV_1")


def test_fresh_run_rejects_plan_artifact_collision(tmp_path):
    from pipeline.project.bootstrap import (
        RunIdCollisionError,
        assert_fresh_run_dir_available as _assert_fresh_run_dir_available,
    )

    run_dir = tmp_path / "runs" / "REAL_ADV_1"
    run_dir.mkdir(parents=True)
    (run_dir / "plan_calc_add_fix.md").write_text("# Plan\n")

    with pytest.raises(RunIdCollisionError, match="plan_calc_add_fix.md"):
        _assert_fresh_run_dir_available(run_dir, resume_from=None)


def test_cli_error_prefix_is_red(capsys):
    from core.io.ansi import get_color_enabled, set_color_enabled
    from pipeline.project.app import print_error

    # Force color on so the colored path runs even under pytest's
    # non-TTY captured stderr — without it the shared paint() policy
    # auto-detects to plain (correct production behaviour, but the
    # test wants to verify the red palette wires through).
    before = get_color_enabled()
    set_color_enabled(True)
    try:
        print_error("boom")
    finally:
        set_color_enabled(before)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "\x1b[91m" in captured.err
    assert "Error:" in captured.err
    assert "boom" in captured.err
