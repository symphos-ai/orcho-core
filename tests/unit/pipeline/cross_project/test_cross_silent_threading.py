"""ADR 0047 Phase E — focused unit tests for the three SILENT seams.

Phase E threads ``request.presentation`` through cross runtime via:

1. **`silent_renderers` factory** (``pipeline.cross_project.rendering``) —
   returns ``(banner, success, warn, preview,
   _render_cross_plan_preview, print_fn, C)`` per ``terminal``. Under
   ``terminal=True`` byte-identical to the canonical helpers; under
   ``terminal=False`` stdout no-ops + ``banner`` forwards
   ``terminal=False`` to the implementation (``log_phase`` keeps
   firing — ADR 0046 stop #9 invariant).

2. **`CrossPlanningContext.terminal` field** — planning_loop's six
   gated functions destructure ``silent_renderers(ctx.terminal)`` at
   their top so all internal calls obey the policy with no per-call
   ``if`` gates.

3. **`ProjectDispatchContext.terminal` field** + Phase E threading of
   ``apply_cross_phase_handoff_pause(..., terminal=...)`` so the
   parent-side handoff-pause ``warn(...)`` suppresses under SILENT.

Phase F's end-to-end test (`test_cross_silent_boundary.py`) covers
the full ``run_cross_project_pipeline(SILENT)`` driver with capsys
+ persisted state assertions. These tests sit one level lower and
lock the three threading seams in isolation.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

# ── Seam 1: silent_renderers factory contract ─────────────────────────


def test_silent_renderers_terminal_returns_canonical_helpers() -> None:
    """Under ``terminal=True`` the factory returns the canonical
    rendering helpers byte-identical to the pre-Phase-E imports."""
    from pipeline.cross_project import rendering
    from pipeline.cross_project.rendering import silent_renderers

    banner, success, warn, preview, render_preview, print_fn, C = (
        silent_renderers(terminal=True)
    )

    assert banner is rendering.banner
    assert success is rendering.success
    assert warn is rendering.warn
    assert preview is rendering.preview
    assert render_preview is rendering._render_cross_plan_preview
    assert print_fn is print
    assert C is rendering.C


def test_silent_renderers_silent_suppresses_stdout(
    capsys: pytest.CaptureFixture,
) -> None:
    """Under ``terminal=False`` ``success`` / ``warn`` / ``preview`` /
    ``_render_cross_plan_preview`` / ``print_fn`` are pure stdout
    no-ops. ``banner`` is the load-bearing one (next test)."""
    from pipeline.cross_project.rendering import silent_renderers

    _, success, warn, preview, render_preview, print_fn, _ = (
        silent_renderers(terminal=False)
    )

    success("would normally print")
    warn("would normally print")
    preview("label", "body", "color")
    render_preview("plan markdown", ["api", "web"])
    print_fn("would normally print")

    captured = capsys.readouterr()
    assert captured.out == "", (
        f"silent helpers leaked stdout: {captured.out!r}"
    )
    assert captured.err == ""


def test_silent_renderers_banner_silent_suppresses_print_but_calls_log_phase(
    capsys: pytest.CaptureFixture,
) -> None:
    """ADR 0046 stop #9 invariant under cross: silent ``banner``
    suppresses the stdout header BUT ``log_phase`` STILL fires so
    ``events.jsonl`` + ``progress.log`` carry the matched
    ``phase.start`` event. The factory's silent ``banner`` forwards
    ``terminal=False`` to the implementation; the implementation gates
    its print half on that flag but logs unconditionally."""
    from pipeline.cross_project.rendering import silent_renderers

    silent_banner, *_ = silent_renderers(terminal=False)

    with patch(
        "pipeline.cross_project.rendering.log_phase",
    ) as log_phase_mock:
        silent_banner("CROSS_PLAN", "round 1")

    captured = capsys.readouterr()
    assert captured.out == "", (
        f"silent banner leaked stdout: {captured.out!r}"
    )
    assert log_phase_mock.called, (
        "silent banner MUST still call log_phase — file + event sinks "
        "are never gated by presentation (ADR 0046 stop #9 inherited)"
    )
    args, _ = log_phase_mock.call_args
    assert args[0] == "CROSS_PLAN"
    assert args[1] == "round 1"


def test_silent_renderers_banner_terminal_prints_and_logs(
    capsys: pytest.CaptureFixture,
) -> None:
    """Polarity sanity: ``terminal=True`` banner prints header +
    calls log_phase. Guards against the conditional logic being
    flipped accidentally."""
    from pipeline.cross_project.rendering import silent_renderers

    terminal_banner, *_ = silent_renderers(terminal=True)

    with patch(
        "pipeline.cross_project.rendering.log_phase",
    ) as log_phase_mock:
        terminal_banner("CROSS_PLAN", "round 1")

    captured = capsys.readouterr()
    assert "[CROSS_PLAN]" in captured.out
    assert "round 1" in captured.out
    assert log_phase_mock.called


# ── Seam 2: CrossPlanningContext.terminal field ───────────────────────


def test_cross_planning_context_carries_terminal_field() -> None:
    """The Phase E field. ``True`` default preserves legacy CLI/SDK
    behaviour; the cross app body sets it from
    ``request.presentation``."""
    import dataclasses

    from pipeline.cross_project.planning_loop import CrossPlanningContext

    fields = {f.name: f for f in dataclasses.fields(CrossPlanningContext)}
    assert "terminal" in fields, (
        "CrossPlanningContext must carry a `terminal: bool` field "
        "(ADR 0047 Phase E seam 1)"
    )
    assert fields["terminal"].default is True, (
        "`terminal` default must be True so existing test/SDK callers "
        "that construct CrossPlanningContext without specifying it get "
        "byte-identical pre-Phase-E behaviour"
    )


def test_cross_planning_context_terminal_threads_into_silent_renderers() -> None:
    """A planning-loop function uses ``silent_renderers(ctx.terminal)``
    to destructure presentation-aware helpers. Pin that the unpack
    returns no-ops under ``terminal=False`` so subsequent
    ``success(...)`` / ``warn(...)`` calls suppress."""
    from pipeline.cross_project.rendering import silent_renderers

    # Mirrors what each planning_loop function does at its top.
    _, success_sink, warn_sink, *_ = silent_renderers(terminal=False)
    # Calls should not raise + should not print.
    success_sink("anything")
    warn_sink("anything")


# ── Seam 3: ProjectDispatchContext.terminal field + handoff threading ─


def test_project_dispatch_context_carries_terminal_field() -> None:
    """Cross-level presentation reaches per-alias dispatch via the
    context field. ``apply_cross_phase_handoff_pause`` callers read
    ``ctx.terminal`` and forward to the helper."""
    import dataclasses

    from pipeline.cross_project.project_dispatch import (
        ProjectDispatchContext,
    )

    fields = {f.name: f for f in dataclasses.fields(ProjectDispatchContext)}
    assert "terminal" in fields, (
        "ProjectDispatchContext must carry a `terminal: bool` field "
        "(ADR 0047 Phase E seam 3)"
    )
    assert fields["terminal"].default is True


def test_apply_cross_phase_handoff_pause_silent_suppresses_warn(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Under ``terminal=False`` the parent-side pause-banner
    ``warn(...)`` suppresses; the structural side-effects (session
    status, checkpoint marker, ``phase.handoff_requested`` event)
    fire unconditionally."""
    from pipeline.cross_project.handoff_payloads import (
        apply_cross_phase_handoff_pause,
    )

    session: dict = {"status": "running"}
    cross_ckpt: dict = {}
    payload = {
        "id": "cross_plan:rejection:1/2",
        "phase": "cross_plan",
        "type": "human_feedback_on_reject",
        "trigger": "rejected",
        "round": 1,
        "loop_max_rounds": 2,
        "verdict": "REJECTED",
        "approved": False,
        "available_actions": ["continue", "retry_feedback", "halt"],
        "artifacts": {},
        "last_output": "",
    }

    emitted: list[dict] = []

    def _capture_emit(kind: str, **fields):
        emitted.append({"kind": kind, **fields})

    with patch("core.observability.events.emit", side_effect=_capture_emit):
        apply_cross_phase_handoff_pause(
            run_dir=None,  # in-memory; no disk side-effects
            session=session,
            cross_ckpt=cross_ckpt,
            payload=payload,
            terminal=False,
        )

    # ✓ stdout/stderr suppressed under terminal=False
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "", (
        f"silent handoff-pause leaked stderr (the warn(...) call): "
        f"{captured.err!r}"
    )

    # ✓ structural mutations + event still fired
    assert session["status"] == "awaiting_phase_handoff"
    assert session["phase_handoff"] == payload
    assert cross_ckpt.get("phase_handoff_pending") is True
    assert cross_ckpt.get("phase_handoff_id") == payload["id"]

    handoff_events = [e for e in emitted if e["kind"] == "phase.handoff_requested"]
    assert len(handoff_events) == 1, (
        "phase.handoff_requested MUST fire under SILENT — it's the "
        "structural signal MCP / dashboards consume when the warn is "
        "suppressed"
    )
    assert handoff_events[0]["phase"] == "cross_plan"


def test_apply_cross_phase_handoff_pause_terminal_warns(
    capsys: pytest.CaptureFixture,
) -> None:
    """Polarity sanity: ``terminal=True`` (default) emits the
    operator-facing pause-banner warn line."""
    from pipeline.cross_project.handoff_payloads import (
        apply_cross_phase_handoff_pause,
    )

    payload = {
        "id": "cross_plan:rejection:1/2",
        "phase": "cross_plan",
        "type": "human_feedback_on_reject",
        "trigger": "rejected",
        "round": 1,
        "loop_max_rounds": 2,
        "verdict": "REJECTED",
        "approved": False,
        "available_actions": ["continue"],
        "artifacts": {},
        "last_output": "",
    }

    apply_cross_phase_handoff_pause(
        run_dir=None,
        session={"status": "running"},
        cross_ckpt={},
        payload=payload,
        # ``terminal`` defaults to True
    )

    captured = capsys.readouterr()
    # warn writes to stderr (via core.observability.logging.warn)
    full_output = captured.out + captured.err
    assert "Pausing for operator decision" in full_output, (
        "TERMINAL handoff-pause must emit the operator pause-banner warn line"
    )


# ── Seam 3 bonus: presentation flows app → planning → dispatch ────────


def test_silent_request_presentation_resolves_to_terminal_false() -> None:
    """End-to-end check that the typed boundary computes
    ``terminal = (request.presentation is PresentationPolicy.TERMINAL)``
    correctly. Both ``SILENT`` cases (enum + string-coerced) must
    yield ``terminal=False``."""
    from pathlib import Path

    from pipeline.cross_project.app_types import CrossRunRequest
    from pipeline.presentation import PresentationPolicy

    silent_enum = CrossRunRequest(
        task="t", projects={"a": Path("/tmp")},
        presentation=PresentationPolicy.SILENT, no_interactive=True,
    )
    assert silent_enum.presentation is PresentationPolicy.SILENT
    assert (silent_enum.presentation is PresentationPolicy.TERMINAL) is False

    silent_str = CrossRunRequest(
        task="t", projects={"a": Path("/tmp")},
        presentation="silent", no_interactive=True,
    )
    # __post_init__ coerced the string to the enum, so the same
    # identity check yields the same flag.
    assert silent_str.presentation is PresentationPolicy.SILENT
    assert (silent_str.presentation is PresentationPolicy.TERMINAL) is False


def test_default_request_resolves_to_terminal_true() -> None:
    """Sanity: a CrossRunRequest without an explicit ``presentation``
    yields ``terminal=True`` — preserves byte-identical CLI / SDK
    behaviour for every existing caller."""
    from pathlib import Path

    from pipeline.cross_project.app_types import CrossRunRequest
    from pipeline.presentation import PresentationPolicy

    req = CrossRunRequest(task="t", projects={"a": Path("/tmp")})
    assert req.presentation is PresentationPolicy.TERMINAL
    assert (req.presentation is PresentationPolicy.TERMINAL) is True
