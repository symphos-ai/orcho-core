"""P2.5 — public ``pipeline.argv.build_orch_argv`` contract tests.

External consumers (orcho-mcp supervisor today) depend on this builder.
The Namespace adapter — relocated in REA-3.8 from
``cli.orcho._build_orch_argv`` to ``sdk.runner.build_orch_argv_from_args``
must keep producing equivalent output for the legacy CLI call site.
"""
from __future__ import annotations

import argparse

from pipeline.argv import build_orch_argv
from sdk.runner import build_orch_argv_from_args as _build_orch_argv


def test_minimal_required():
    # ``--output summary`` is now always forwarded (it is no longer omitted as
    # the implicit default) so the orchestrator's own config default cannot
    # override the caller's resolved mode.
    argv = build_orch_argv(project="/p")
    assert argv == ["--project", "/p", "--output", "summary"]


def test_task_inline():
    argv = build_orch_argv(project="/p", task="ship feature")
    assert argv[:2] == ["--task", "ship feature"]
    assert "--project" in argv and "/p" in argv


def test_run_id_emits_flag():
    argv = build_orch_argv(project="/p", run_id="20260506_120000_abc123")
    # --run-id appears with value
    i = argv.index("--run-id")
    assert argv[i + 1] == "20260506_120000_abc123"


def test_run_id_omitted_when_none():
    argv = build_orch_argv(project="/p")
    assert "--run-id" not in argv


def test_mock_validate_plan_reject_zero_omitted():
    argv = build_orch_argv(project="/p", mock_validate_plan_reject=0)
    assert "--mock-validate-plan-reject" not in argv


def test_mock_validate_plan_reject_passes_count():
    argv = build_orch_argv(project="/p", mock_validate_plan_reject=2)
    i = argv.index("--mock-validate-plan-reject")
    assert argv[i + 1] == "2"


def test_profile_unset_omitted():
    """``profile=None`` (caller did not supply ``--profile``) leaves
    the flag out so the orchestrator's argparse default (``None`` →
    ``resolve_resume_profile`` resolves at runtime) takes effect.
    Resume-time inherit semantics depend on this — emitting
    ``--profile advanced`` from a None-input would silently force
    "advanced" and bypass ``meta.profile`` inherit."""
    argv = build_orch_argv(project="/p", profile=None)
    assert "--profile" not in argv


def test_profile_explicit_advanced_emitted():
    """Explicit ``profile="advanced"`` (caller supplied it) IS
    serialized: orchestrator must distinguish "explicit override" from
    "inherit from meta" on resume. Silently dropping ``--profile
    advanced`` would collapse the override into inherit."""
    argv = build_orch_argv(project="/p", profile="advanced")
    assert "--profile" in argv
    assert argv[argv.index("--profile") + 1] == "advanced"


def test_profile_non_default_emitted():
    argv = build_orch_argv(project="/p", profile="task")
    assert "--profile" in argv
    assert argv[argv.index("--profile") + 1] == "task"


def test_cross_mode_full_omitted():
    """``cross_mode`` is the cross-orchestrator-only flag; default
 "full" stays out of argv."""
    argv = build_orch_argv(project="/p", cross_mode="full")
    assert "--mode" not in argv


def test_cross_mode_plan_emitted():
    argv = build_orch_argv(project="/p", cross_mode="plan")
    assert "--mode" in argv
    assert argv[argv.index("--mode") + 1] == "plan"


def test_session_mode_auto_omitted():
    argv = build_orch_argv(project="/p", session_mode="auto")
    assert "--session-mode" not in argv


def test_session_split_overrides_emitted_in_order():
    argv = build_orch_argv(
        project="/p",
        session_split=["implement=common", "repair_changes=common"],
    )
    pairs = [
        (argv[i], argv[i + 1])
        for i, value in enumerate(argv)
        if value == "--session-split"
    ]
    assert pairs == [
        ("--session-split", "implement=common"),
        ("--session-split", "repair_changes=common"),
    ]


def test_output_live_emitted():
    argv = build_orch_argv(project="/p", output_mode="live")
    assert argv[argv.index("--output") + 1] == "live"


def test_output_summary_is_forwarded_not_omitted():
    # ``summary`` must be forwarded explicitly: omitting it let the orchestrator
    # fall back to its own ``config.cli_output_mode()`` default, so an explicit
    # ``orcho run --output summary`` was silently overridden by a workspace
    # ``cli.output_mode = live`` default.
    argv = build_orch_argv(project="/p", output_mode="summary")
    assert argv[argv.index("--output") + 1] == "summary"


def test_verbose_legacy_shim_maps_to_debug():
    argv = build_orch_argv(project="/p", verbose=True)
    assert argv[argv.index("--output") + 1] == "debug"


def test_stream_output_legacy_shim_maps_to_live():
    argv = build_orch_argv(project="/p", stream_output=True)
    assert argv[argv.index("--output") + 1] == "live"


def test_per_phase_models():
    argv = build_orch_argv(
        project="/p",
        model_plan="claude-opus-4-7",
        model_implement="claude-sonnet-4-6",
        runtime_review_changes="codex",
    )
    assert argv[argv.index("--model-plan"):argv.index("--model-plan") + 2] == ["--model-plan", "claude-opus-4-7"]
    assert argv[argv.index("--model-implement"):argv.index("--model-implement") + 2] == ["--model-implement", "claude-sonnet-4-6"]
    assert argv[argv.index("--runtime-review-changes"):argv.index("--runtime-review-changes") + 2] == ["--runtime-review-changes", "codex"]


def test_legacy_namespace_adapter_matches_kwargs_form():
    """``_build_orch_argv(Namespace)`` must produce the same argv as the
 public kwargs form for in-CLI call sites."""
    ns = argparse.Namespace(
        task="t",
        task_file=None,
        project="/p",
        workspace=None,
        resume=None,
        run_id=None,
        max_rounds=2,
        mock_validate_plan_reject=0,
        model="claude-sonnet-4-6",
        output_dir="/out",
        dry_run=False,
        mock=True,
        output="live",
        #  ``mode`` is the cross-only legacy flag (full/plan);
        # ``profile`` is the per-project v2 dispatch knob.
        mode="full",
        # profile=None mirrors CLI's new ``--profile default=None``:
        # orchestrator resolves at runtime (fresh → "advanced";
        # resume → meta.profile).
        profile=None,
        session_mode="auto",
        session_split=None,
        model_plan=None, model_implement=None, model_repair_changes=None, model_review_changes=None,
        runtime_plan=None, runtime_implement=None, runtime_repair_changes=None, runtime_review_changes=None,
    )
    legacy = _build_orch_argv(ns)
    direct = build_orch_argv(
        task="t", project="/p", max_rounds=2,
        model="claude-sonnet-4-6", output_dir="/out", mock=True,
        output_mode="live",
    )
    assert legacy == direct


def test_supervisor_minimal_call_shape():
    """Sanity: the smallest call orcho-mcp supervisor will make."""
    argv = build_orch_argv(
        project="/path/to/project",
        task="implement auth flow",
        run_id="20260506_140000_xy12ab",
        output_dir="/runs/20260506_140000_xy12ab",
        mock=True,
    )
    # All five must appear with correct values
    assert argv.index("--project") + 1 == argv.index("/path/to/project")
    assert "--task" in argv
    assert argv[argv.index("--run-id") + 1] == "20260506_140000_xy12ab"
    assert argv[argv.index("--output-dir") + 1] == "/runs/20260506_140000_xy12ab"
    assert "--mock" in argv
