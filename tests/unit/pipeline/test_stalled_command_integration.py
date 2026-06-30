# SPDX-License-Identifier: Apache-2.0
"""Terminal stalled-command path, end-to-end through the run.py seam (T1).

Drives ``_PipelineRun._record_phase_failure`` with an
``AgentCommandStalledError`` (the idle-timeout escalation the agents layer
raises) and asserts the terminal contract:

* the run flips to ``failed`` with a ``session['failure']`` record whose
  ``failure_kind == 'stalled_command'`` and which carries the durable recovery
  verb set;
* a terminal ``agent.command_stalled`` (``terminal=True``) event AND a closing
  ``run.end`` event are emitted;
* ``compute_next_actions`` projects a resume recovery from that terminal
  failure;
* the terminal escalation event is NOT surfaced as a live (non-terminal)
  diagnostic — the two sources stay distinct.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from agents.stall_protocol import (
    AgentCommandStalledError,
    EventStallDiagnosticSink,
    StalledCommand,
    StallReason,
)
from agents.stream import _stream_run
from core.observability import events as _events
from pipeline.evidence import collect_evidence
from pipeline.project.run import _PipelineRun
from pipeline.project.types import PresentationPolicy
from sdk.actions import compute_next_actions
from sdk.evidence_slices import active_stall_diagnostics


def _fake_run(run_dir: Path) -> SimpleNamespace:
    """A minimal stand-in carrying only the attributes
    ``_record_phase_failure`` reads."""
    return SimpleNamespace(
        session={},
        output_dir=run_dir,
        _ckpt=None,
        _presentation=PresentationPolicy.SILENT,
        _model_for_phase=None,
    )


def test_terminal_stall_end_to_end(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _events.init_event_store(run_dir)
    try:
        fake = _fake_run(run_dir)
        stalled = StalledCommand(
            phase="implement",
            elapsed_s=300.0,
            command_preview="pytest -q -m 'not e2e'",
            output_tail="(no output for 300s)",
            reason=StallReason.SILENT_CHILD_COMMAND,
            process_group=9090,
        )
        exc = AgentCommandStalledError(stalled)

        _PipelineRun._record_phase_failure(fake, exc, "implement")

        # 1) terminal session['failure'] record
        failure = fake.session["failure"]
        assert failure["failure_kind"] == "stalled_command"
        assert failure["failed_phase"] == "implement"
        assert failure["reason"] == "silent_child_command"
        assert [a["action"] for a in failure["recovery_actions"]] == [
            "interrupt", "resume_from_checkpoint", "halt",
        ]
        assert fake.session["status"] == "failed"
        assert fake.session["halt_reason"].startswith("stalled_command:")

        # 2) events: terminal agent.command_stalled + run.end
        events = _events.read_all(run_dir)
        stall_events = [e for e in events if e.kind == "agent.command_stalled"]
        assert len(stall_events) == 1
        assert stall_events[0].payload["terminal"] is True
        assert stall_events[0].payload["reason"] == "silent_child_command"
        run_end = [e for e in events if e.kind == "run.end"]
        assert len(run_end) == 1
        assert run_end[0].payload["status"] == "failed"
        assert run_end[0].payload["failure_kind"] == "stalled_command"
    finally:
        _events.init_event_store(None)

    # 3) compute_next_actions projects a resume recovery from the terminal
    #    failure (no duplicate flat resume).
    actions = compute_next_actions(fake.session, run_id="r1")
    resumes = [a for a in actions if a.tool == "orcho_run_resume"]
    assert len(resumes) == 1
    assert resumes[0].args == {"run_id": "r1"}

    # 4) the terminal escalation event is NOT a live non-terminal diagnostic.
    assert active_stall_diagnostics(run_dir) == []


def test_stream_to_pipeline_failure_emits_exactly_one_terminal_record(
    tmp_path: Path,
) -> None:
    """End-to-end (F2): a real idle-timeout in ``_stream_run`` raises, the
    pipeline failure handler records the terminal stall, and the evidence
    bundle carries EXACTLY ONE terminal ``command_stalled`` record — the stream
    layer no longer double-emits its own terminal event."""
    run_dir = tmp_path / "run"
    _events.init_event_store(run_dir)
    try:
        _events.emit(
            "run.start",
            task="demo",
            run_kind="single_project",
            project="/p",
            profile="feature",
        )
        # Real silent subprocess → the single auto-kill trigger (idle-timeout)
        # escalates to AgentCommandStalledError. The stream emits NO terminal
        # event of its own.
        raised: AgentCommandStalledError | None = None
        try:
            _stream_run(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                idle_timeout=1,
                stall_sink=EventStallDiagnosticSink(),
                stall_phase="implement",
            )
        except AgentCommandStalledError as exc:
            raised = exc
        assert raised is not None

        # The pipeline failure handler is the single authoritative emit-site.
        fake = SimpleNamespace(
            session={"task": "demo", "profile": "feature"},
            output_dir=run_dir,
            _ckpt=None,
            _presentation=PresentationPolicy.SILENT,
            _model_for_phase=None,
        )
        _PipelineRun._record_phase_failure(fake, raised, "implement")
    finally:
        _events.init_event_store(None)

    # Exactly one terminal command_stalled record in the durable bundle.
    bundle = collect_evidence(run_dir)
    stalls = [e for e in bundle["errors"] if e.get("kind") == "command_stalled"]
    terminal = [e for e in stalls if e["terminal"] is True]
    assert len(terminal) == 1
    assert terminal[0]["reason"] == "silent_child_command"
    assert [a["action"] for a in terminal[0]["recovery_actions"]] == [
        "interrupt", "resume_from_checkpoint", "halt",
    ]
