"""ADR 0047 Phase F — focused tests for the cross finalization split.

Pins the load-bearing contract Phase F introduces:

1. ``finalize_cross_run(ctx)`` is the silent service. It mutates
   ``session``, emits ``run.end``, persists ``session.json`` /
   ``metrics.json`` / evidence-bundle when ``output_dir`` is set,
   mirrors artifacts, AND produces zero stdout / stderr.

2. ``finalize_cross_with_terminal_output(ctx)`` is the terminal
   wrapper. It calls the silent service exactly once, then renders
   the DONE / FAILED banner + chips from the structured result. It
   does NOT re-decide status, NOT re-emit ``run.end``, NOT re-write
   any persisted file — the invariant the wrapper exists to express.

3. The status-decision tree is owned by the silent service and
   covers the three branches that used to live inline in the cross
   app body: CFA skipped by policy with blocking contract_check
   skips → failed; CFA skipped without blocking skips → done +
   ``skipped_by_policy=True``; CFA ran and rejected → failed with
   per-source halt_reason.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pipeline.cross_project.finalization import (
    CrossFinalizationContext,
    CrossFinalizationResult,
    finalize_cross_run,
    finalize_cross_with_terminal_output,
)


def _make_cfa(*, approved: bool, source: str = "agent") -> SimpleNamespace:
    """Shape-match the CrossFinalAcceptanceResult fields the finalization
    silent service reads (``parsed.approved`` + ``source``)."""
    return SimpleNamespace(
        parsed=SimpleNamespace(approved=approved),
        source=source,
    )


def _make_context(
    *,
    run_dir: Path,
    output_dir: bool = True,
    cfa_result=None,
    contract_results: dict | None = None,
    contract_check_failed: bool = False,
    contract_check_failure_reason: str | None = None,
    cross_phase_usage: dict | None = None,
    session: dict | None = None,
    projects: dict | None = None,
) -> CrossFinalizationContext:
    return CrossFinalizationContext(
        run_dir=run_dir,
        output_dir=output_dir,
        session=session if session is not None else {
            "phases": {"projects": {}},
            "run_id": "TEST_RUN",
        },
        projects=projects if projects is not None else {
            "api": Path("/tmp/api"), "web": Path("/tmp/web"),
        },
        max_rounds=2,
        cfa_result=cfa_result,
        contract_results=contract_results or {},
        contract_check_failed=contract_check_failed,
        contract_check_failure_reason=contract_check_failure_reason,
        cross_phase_usage=cross_phase_usage or {},
    )


# ── Seam 1: silent service produces zero stdout / stderr ──────────────


def test_finalize_cross_run_silent_produces_no_stdout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """The silent service must produce ZERO stdout / stderr regardless
    of branch. ADR 0046 stop #9 inherited — the structural sinks
    (run.end event, session.json write, mirror) all happen but the
    presentation layer stays quiet."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ctx = _make_context(
        run_dir=run_dir,
        output_dir=True,
        cfa_result=_make_cfa(approved=True),
    )

    with (
        patch("core.observability.events.emit"),
        patch("pipeline.engine.artifact_mirror.mirror_to_projects",
              return_value=[Path("/tmp/api/.orcho/artifacts/plan.md")]),
    ):
        result = finalize_cross_run(ctx)

    captured = capsys.readouterr()
    assert captured.out == "", (
        f"silent finalize_cross_run leaked stdout: {captured.out!r}"
    )
    assert captured.err == "", (
        f"silent finalize_cross_run leaked stderr: {captured.err!r}"
    )
    assert isinstance(result, CrossFinalizationResult)
    assert result.status == "done"


# ── Seam 2: run.end emitted exactly once via the silent service ───────


def test_silent_service_emits_run_end_exactly_once(
    tmp_path: Path,
) -> None:
    """``run.end`` is the structural completion signal. It must fire
    exactly once per finalization call — the silent service emits it
    unconditionally, the terminal wrapper does NOT re-emit."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ctx = _make_context(
        run_dir=run_dir,
        output_dir=False,  # skip persistence to keep the seam tight
        cfa_result=_make_cfa(approved=True),
    )

    emitted: list[dict] = []

    def _capture_emit(kind: str, **fields):
        emitted.append({"kind": kind, **fields})

    with (
        patch("core.observability.events.emit", side_effect=_capture_emit),
        patch("pipeline.engine.artifact_mirror.mirror_to_projects",
              return_value=[]),
    ):
        finalize_cross_run(ctx)

    run_end = [e for e in emitted if e["kind"] == "run.end"]
    assert len(run_end) == 1
    assert run_end[0]["status"] == "done"
    assert run_end[0]["projects"] == 2
    assert run_end[0]["rounds"] == 2


def test_terminal_wrapper_does_not_re_emit_run_end(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """The terminal wrapper renders the chips but MUST NOT call the
    silent service twice and MUST NOT re-emit ``run.end`` on its
    own. The load-bearing invariant mirroring ADR 0042 Phase G's
    project finalization wrapper."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ctx = _make_context(
        run_dir=run_dir,
        output_dir=False,
        cfa_result=_make_cfa(approved=True),
    )

    emitted: list[dict] = []

    def _capture_emit(kind: str, **fields):
        emitted.append({"kind": kind, **fields})

    with (
        patch("core.observability.events.emit", side_effect=_capture_emit),
        patch("pipeline.engine.artifact_mirror.mirror_to_projects",
              return_value=[]),
    ):
        finalize_cross_with_terminal_output(ctx)

    # Stdout proves the wrapper rendered SOMETHING (the DONE banner).
    captured = capsys.readouterr()
    assert "[DONE]" in captured.out
    # But run.end fired exactly once — the wrapper did not double-emit.
    run_end = [e for e in emitted if e["kind"] == "run.end"]
    assert len(run_end) == 1, (
        f"terminal wrapper double-emitted run.end: {run_end!r}"
    )


# ── Seam 3: status decision tree ──────────────────────────────────────


def test_cfa_skipped_no_blocking_skips_yields_done_with_policy_flag(
    tmp_path: Path,
) -> None:
    """When CFA is disabled by policy AND no contract_check entry is a
    blocking operator-source skip, the run completes with status=done
    and skipped_by_policy=True so the wrapper renders the
    ``(cross_final_acceptance skipped by policy)`` banner variant."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ctx = _make_context(
        run_dir=run_dir,
        output_dir=False,
        cfa_result=None,
        contract_results={"api": {"approved": True}},
    )

    with (
        patch("core.observability.events.emit"),
        patch("pipeline.engine.artifact_mirror.mirror_to_projects",
              return_value=[]),
    ):
        result = finalize_cross_run(ctx)

    assert result.status == "done"
    assert result.skipped_by_policy is True
    assert result.halt_reason is None
    assert result.failure_reason is None
    assert ctx.session["status"] == "done"


def test_cfa_skipped_with_blocking_skip_yields_failed(
    tmp_path: Path,
) -> None:
    """An operator-source contract_check skip with ``on_skip=block``
    while CFA is disabled is the explicit failure mode the inline
    tail used to detect."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ctx = _make_context(
        run_dir=run_dir,
        output_dir=False,
        cfa_result=None,
        contract_results={
            "web": {
                "skipped": True,
                "source": "operator",
                "on_skip": "block",
            },
        },
    )

    with (
        patch("core.observability.events.emit"),
        patch("pipeline.engine.artifact_mirror.mirror_to_projects",
              return_value=[]),
    ):
        result = finalize_cross_run(ctx)

    assert result.status == "failed"
    assert result.halt_reason == "cross_contract_check_blocking_skip"
    assert result.skipped_by_policy is False
    assert result.failure_reason is not None
    assert "web" in result.failure_reason
    assert ctx.session["status"] == "failed"
    assert ctx.session["halt_reason"] == "cross_contract_check_blocking_skip"


def test_cfa_rejected_yields_failed_with_agent_halt_reason(
    tmp_path: Path,
) -> None:
    """CFA ran, agent verdict was REJECTED → status=failed with the
    agent-source halt_reason. Chained ``contract_check_failure_reason``
    flows into the operator-facing failure message."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ctx = _make_context(
        run_dir=run_dir,
        output_dir=False,
        cfa_result=_make_cfa(approved=False, source="agent"),
        contract_check_failed=True,
        contract_check_failure_reason="web: rejected",
    )

    with (
        patch("core.observability.events.emit"),
        patch("pipeline.engine.artifact_mirror.mirror_to_projects",
              return_value=[]),
    ):
        result = finalize_cross_run(ctx)

    assert result.status == "failed"
    assert result.halt_reason == "cross_final_acceptance_failed"
    assert "agent REJECTED" in result.failure_reason
    assert "web: rejected" in result.failure_reason


def test_cfa_parse_error_yields_failed_with_parse_error_halt_reason(
    tmp_path: Path,
) -> None:
    """``source=parse_error`` is its own halt_reason bucket — the
    silent service must not collapse it with the rejection path."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ctx = _make_context(
        run_dir=run_dir,
        output_dir=False,
        cfa_result=_make_cfa(approved=True, source="parse_error"),
    )

    with (
        patch("core.observability.events.emit"),
        patch("pipeline.engine.artifact_mirror.mirror_to_projects",
              return_value=[]),
    ):
        result = finalize_cross_run(ctx)

    assert result.status == "failed"
    assert result.halt_reason == "cross_final_acceptance_parse_error"


# ── Seam 4: persistence — session.json written when output_dir=True ───


def test_silent_service_writes_session_and_metrics_when_output_dir(
    tmp_path: Path,
) -> None:
    """When ``output_dir=True`` and the rollup has data, both
    ``session.json`` and ``metrics.json`` are persisted by the silent
    service. The terminal wrapper merely reports the paths."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    session = {
        "run_id": "TEST",
        "phases": {
            "projects": {
                "api": {"metrics": {"total_cost": 0.1, "phases": {}}},
            },
        },
    }
    ctx = _make_context(
        run_dir=run_dir,
        output_dir=True,
        cfa_result=_make_cfa(approved=True),
        session=session,
        projects={"api": tmp_path / "api"},
    )

    # save_cross_session and the metrics writer hit disk. Mock only
    # the mirror to keep the test hermetic.
    with patch(
        "pipeline.engine.artifact_mirror.mirror_to_projects",
        return_value=[],
    ):
        result = finalize_cross_run(ctx)

    assert result.session_path is not None
    assert result.session_path.is_file()
    assert result.metrics_path is not None
    assert result.metrics_path.is_file()
    # session["metrics"] mirrors the metrics.json content shape.
    assert "metrics" in session
    assert isinstance(session["metrics"], dict)


def test_silent_service_skips_persistence_when_output_dir_false(
    tmp_path: Path,
) -> None:
    """``output_dir=False`` (an in-memory dry-run) must NOT write
    ``session.json`` or ``metrics.json``. The silent service still
    emits ``run.end`` and runs the mirror — those are not gated by
    output_dir."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ctx = _make_context(
        run_dir=run_dir,
        output_dir=False,
        cfa_result=_make_cfa(approved=True),
    )

    with (
        patch("core.observability.events.emit"),
        patch("pipeline.engine.artifact_mirror.mirror_to_projects",
              return_value=[]),
    ):
        result = finalize_cross_run(ctx)

    assert result.session_path is None
    assert result.metrics_path is None


# ── Seam 5: terminal wrapper renders the right chips ──────────────────


def test_terminal_wrapper_renders_done_banner_and_chips(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """The wrapper produces the legacy CLI tail transcript: DONE banner
    + ``Projects: ...`` chip. When the silent service persisted files
    + mirrored artifacts, those produce ``Session: ...`` /
    ``Metrics: ...`` / ``Mirrored ...`` chips off the structured
    result fields — not from re-reading disk."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    session = {
        "run_id": "TEST",
        "phases": {
            "projects": {
                "api": {"metrics": {"total_cost": 0.1, "phases": {}}},
            },
        },
    }
    ctx = _make_context(
        run_dir=run_dir,
        output_dir=True,
        cfa_result=_make_cfa(approved=True),
        session=session,
        projects={"api": tmp_path / "api"},
    )

    with (
        patch("core.observability.events.emit"),
        patch(
            "pipeline.engine.artifact_mirror.mirror_to_projects",
            return_value=[tmp_path / "api" / ".orcho" / "artifacts" / "plan.md"],
        ),
    ):
        finalize_cross_with_terminal_output(ctx)

    captured = capsys.readouterr()
    assert "[DONE]" in captured.out
    assert "Cross-project pipeline complete" in captured.out
    assert "Projects: 1 | Rounds each: 2" in captured.out
    # Session / Metrics chips fire because output_dir=True wrote both.
    assert "Session: " in captured.out
    assert "Metrics: " in captured.out
    # Mirror chip reports the single mirrored artifact.
    assert "Mirrored 1 artifacts across 1 projects" in captured.out


def test_terminal_wrapper_renders_failed_banner_with_failure_reason(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Failed path: red FAILED banner carrying the structured
    ``failure_reason`` string. No re-decision in the wrapper — the
    text comes from the silent service result."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ctx = _make_context(
        run_dir=run_dir,
        output_dir=False,
        cfa_result=_make_cfa(approved=False, source="agent"),
    )

    with (
        patch("core.observability.events.emit"),
        patch("pipeline.engine.artifact_mirror.mirror_to_projects",
              return_value=[]),
    ):
        finalize_cross_with_terminal_output(ctx)

    captured = capsys.readouterr()
    assert "[FAILED]" in captured.out
    assert "Cross-project pipeline failed" in captured.out
    assert "agent REJECTED" in captured.out


def test_terminal_wrapper_renders_policy_skip_variant(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """When CFA was skipped by policy without blocking skips, the
    wrapper renders the ``(cross_final_acceptance skipped by policy)``
    variant of the DONE banner. The text is driven by the silent
    service's ``skipped_by_policy`` flag, not by a wrapper-side
    re-inspection of ``ctx``."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ctx = _make_context(
        run_dir=run_dir,
        output_dir=False,
        cfa_result=None,
        contract_results={"api": {"approved": True}},
    )

    with (
        patch("core.observability.events.emit"),
        patch("pipeline.engine.artifact_mirror.mirror_to_projects",
              return_value=[]),
    ):
        finalize_cross_with_terminal_output(ctx)

    captured = capsys.readouterr()
    assert "[DONE]" in captured.out
    assert (
        "cross_final_acceptance skipped by policy" in captured.out
    ), (
        "policy-skip variant of the DONE banner must reach stdout when "
        "skipped_by_policy=True"
    )


# ── Seam 6: mirror error path ────────────────────────────────────────


def test_mirror_error_surfaces_in_result_and_terminal(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """When the artifact mirror raises, the silent service catches it
    and surfaces the message via ``mirror_error``. The terminal
    wrapper renders the legacy ``! mirror skipped`` line off that
    field — no separate try/except in the wrapper."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ctx = _make_context(
        run_dir=run_dir,
        output_dir=False,
        cfa_result=_make_cfa(approved=True),
    )

    with (
        patch("core.observability.events.emit"),
        patch(
            "pipeline.engine.artifact_mirror.mirror_to_projects",
            side_effect=RuntimeError("mirror disk full"),
        ),
    ):
        finalize_cross_with_terminal_output(ctx)

    captured = capsys.readouterr()
    assert "mirror skipped" in captured.out
    assert "mirror disk full" in captured.out
