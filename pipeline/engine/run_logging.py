"""
pipeline/engine/run_logging.py — Logging setup for a pipeline run.

Named run_logging (not logging) to avoid shadowing the stdlib logging module.

Provides setup_run_logging() — shared between orchestrator and cross_orchestrator.
Previously duplicated inline in run_pipeline() and run_cross_pipeline().
"""

from __future__ import annotations

from pathlib import Path

import agents as _agent_module
from core.io.ansi import C, paint
from core.observability import events as _events, logging as _core_logging
from core.observability.logging import set_progress_log


def setup_run_logging(
    output_dir: Path | None,
    session_ts: str,
    *,
    is_sub_pipeline: bool = False,
    is_resume: bool = False,
    terminal: bool = True,
) -> Path | None:
    """Configure progress.log and output.log for a pipeline run.

    Args:
        output_dir:      The runs/{ts}/ directory. If None, logging is skipped
                         (standalone CLI invocation without --output-dir).
        session_ts:      Timestamp string used as the progress.log key.
        is_sub_pipeline: If True, this is a per-project call from cross_pipeline;
                         skip progress.log init (parent already owns it) but
                         still write per-project output.log.
        is_resume:       If True, the event-store appends to the existing
                         events.jsonl and seq continues from the last
                         recorded event. progress.log is already
                         append-mode so no extra flag is needed there.
        terminal:        ADR 0046 Phase C — split render from setup.
                         When ``False`` (SILENT path from ``run_project_pipeline``),
                         the two grey ``📄 Live output`` / ``📡 Events``
                         courtesy chips are suppressed. The
                         ``set_progress_log`` / ``init_event_store`` /
                         ``set_agent_log`` calls always fire regardless
                         (file + event sinks are never gated by
                         presentation — ADR 0046 stop #9).

    Returns:
        The path to the output.log file, or None if output_dir is None.
    """
    if output_dir is None:
        return None

    if not is_sub_pipeline:
        set_progress_log(output_dir, session_ts)
        # Initialize the canonical event-store. Sub-pipelines (cross-runs
        # forking per project) inherit the parent's store — the events all
        # land in one events.jsonl at the cross-run dir, tagged by phase.
        _events.init_event_store(output_dir, resume=is_resume)

    agent_log_path = output_dir / "output.log"
    # The CLI advertises this path before the first provider invocation. Some
    # resume paths finish entirely from cached durable state, so no stream
    # writer ever opens it; materialize the promised sink during setup.
    agent_log_path.touch(exist_ok=True)
    _agent_module.set_agent_log(agent_log_path)

    if not is_sub_pipeline and terminal:
        # ADR 0046 Phase C (site 18 — inventory miss caught by Phase F
        # test 3 failure): the two grey path chips are CLI courtesy
        # only; the structural ``output.log`` + ``events.jsonl`` paths
        # are already recoverable from ``output_dir``. Silent callers
        # suppress.
        _events_path = output_dir / "events.jsonl"
        print(paint(f"  📄 Live output → tail -f {agent_log_path}", C.GREY))
        print(paint(f"  📡 Events     → {_events_path}", C.GREY))

    return agent_log_path


def is_sub_pipeline() -> bool:
    """Return True if a progress.log is already active (we are inside a cross-run)."""
    return bool(_core_logging._progress_log)
