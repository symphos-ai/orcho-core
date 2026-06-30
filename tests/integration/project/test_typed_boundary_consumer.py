"""Reference smoke for the single-project typed silent boundary.

This is the executable companion to
``docs/examples/typed_boundary_consumer.md``. It demonstrates — and
pins — the **canonical usage pattern** that future SDK / MCP / web /
integration consumers of ``orcho-core`` should follow when driving a
project run from code:

    result = run_project_pipeline(
        ProjectRunRequest(
            ...,
            presentation=PresentationPolicy.SILENT,
            no_interactive=True,
        )
    )

    # stdout / stderr are empty.
    # The contract surface is the structured state, not the transcript:
    #   - result.session       (in-memory snapshot of the persisted dict)
    #   - result.output_dir    (run directory on disk)
    #   - result.run_id        (session_ts identifier)
    #   - <run_dir>/meta.json  (persisted session contract)
    #   - <run_dir>/events.jsonl  (run.start / phase.* / run.end events)

ADR cross-links: ADR 0042 (typed project boundary), ADR 0046 (silent
presentation policy). The deeper *contract* coverage lives in
``tests/unit/pipeline/project/test_silent_boundary.py`` — this module
keeps the **consumer-facing** subset that documents the shape.

What this is NOT:

    * Not a CLI transcript test (use TERMINAL + capsys for that).
    * Not a substitute for the ADR 0046 contract tests.
    * Not a wire-format contract for orcho-mcp (MCP has its own E2E
      smoke; this is the in-process Python API).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agents.runtimes import MockAgentProvider
from pipeline.plugins import PluginConfig
from pipeline.project.app import run_project_pipeline
from pipeline.project.types import (
    PresentationPolicy,
    ProjectRunRequest,
    ProjectRunResult,
)

# ── fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def _tmp_project(tmp_path: Path) -> Path:
    """Minimal initialised git checkout — what a real consumer's
    project_dir would look like."""
    from tests.conftest import init_git_repo
    project = tmp_path / "proj"
    init_git_repo(project)
    return project


# ── Phase A smoke ─────────────────────────────────────────────────────


def test_consumer_drives_silent_project_run_via_typed_boundary(
    _tmp_project: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Canonical consumer pattern for a single-project run.

    Demonstrates the four-step contract a future consumer follows:

        1. Build a typed ``ProjectRunRequest`` with
           ``presentation=PresentationPolicy.SILENT`` and
           ``no_interactive=True`` (the post-init invariant rejects
           SILENT without no_interactive).
        2. Call ``run_project_pipeline(request)``; receive a typed
           ``ProjectRunResult``.
        3. Read status / completion from ``result.session`` and the
           persisted ``meta.json`` — never from stdout.
        4. Read structural progress from ``events.jsonl`` —
           ``run.start``, ``phase.start``, ``phase.end``, ``run.end``.

    No stdout parsing. No ``DONE`` banner detection. No ``Run dir:``
    chip scraping. The consumer treats the transcript as absent and
    the structured state as authoritative.
    """
    run_dir = tmp_path / "runs" / "consumer-smoke"

    # ── step 1: build the typed request ────────────────────────────
    request = ProjectRunRequest(
        task="silent consumer reference smoke",
        project_dir=str(_tmp_project),
        output_dir=run_dir,
        max_rounds=1,
        profile_name="task",  # fastest profile — no plan loop.
        provider=MockAgentProvider(latency=0.0),
        presentation=PresentationPolicy.SILENT,
        no_interactive=True,  # required by SILENT — enforced by __post_init__.
    )

    # ── step 2: invoke the typed boundary ──────────────────────────
    with patch("pipeline.project.session_run.load_plugin", return_value=PluginConfig()):
        result = run_project_pipeline(request)

    # ── contract pin: NO stdout / NO stderr ────────────────────────
    out = capsys.readouterr()
    assert out.out == "", (
        f"SILENT typed boundary leaked stdout — consumers must not "
        f"need to suppress / capture / parse it. Got "
        f"{len(out.out)} chars: {out.out[:200]!r}"
    )
    assert out.err == "", (
        f"SILENT typed boundary leaked stderr. Got: {out.err[:200]!r}"
    )

    # ── step 3: read status from the typed result ──────────────────
    assert isinstance(result, ProjectRunResult)
    assert result.session["status"] == "done", (
        f"consumer reads completion from result.session['status']; "
        f"got {result.session.get('status')!r}"
    )
    assert result.output_dir == run_dir, (
        "result.output_dir is the canonical pointer to the run "
        "directory on disk — consumers anchor here, not on stdout."
    )
    assert result.run_id, (
        "result.run_id is the session identifier — consumers use it "
        "to correlate logs, events, and downstream artifacts."
    )

    # ── step 3 (continued): read persisted meta.json ───────────────
    #
    # ``meta.json`` is the durable record of the run. It carries the
    # same shape as ``result.session`` — consumers that read after
    # process exit (web dashboards, MCP, scheduled jobs) anchor here.
    meta_path = result.output_dir / "meta.json"
    assert meta_path.is_file(), (
        "meta.json is the durable consumer contract — must exist "
        "under SILENT (ADR 0046 stop #9: file sinks are never gated)"
    )
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["status"] == "done"

    # ── step 4: read structural events from events.jsonl ───────────
    #
    # ``events.jsonl`` is the structural progress stream. Consumers
    # that want incremental updates (tail-and-react patterns) follow
    # this file rather than parsing the transcript. The canonical
    # event spine is: run.start → (phase.start / phase.end)+ → run.end.
    events_path = result.output_dir / "events.jsonl"
    assert events_path.is_file(), (
        "events.jsonl is the structural event store — never gated "
        "by presentation policy (ADR 0046 stop #9)"
    )
    events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    kinds = {e.get("kind") for e in events}

    # Canonical spine present under SILENT — the consumer's read
    # contract works regardless of presentation.
    assert "run.start" in kinds, (
        f"consumer reference smoke: run.start must be present in "
        f"events.jsonl. Saw kinds={sorted(kinds)}"
    )
    assert "phase.start" in kinds
    assert "phase.end" in kinds
    assert "run.end" in kinds

    # Phase F invariant — exactly one run.end (the silent service is
    # the only emitter; the terminal wrapper never re-emits). This
    # matters for consumers that count completions.
    run_end_events = [e for e in events if e.get("kind") == "run.end"]
    assert len(run_end_events) == 1, (
        f"expected exactly one run.end event; got {len(run_end_events)}"
    )
    assert run_end_events[0].get("payload", {}).get("status") == "done"


def test_consumer_silent_request_rejects_interactive_combo() -> None:
    """Reference for the SILENT/no_interactive invariant.

    Documents — at the consumer layer — that
    ``presentation=SILENT, no_interactive=False`` is rejected at
    request construction time. Embedders that forget the pairing get
    a loud ``ValueError`` at the API boundary, not a half-silent run
    later. See ``ProjectRunRequest.__post_init__``.
    """
    with pytest.raises(ValueError, match="no_interactive=True"):
        ProjectRunRequest(
            task="invalid",
            project_dir="/tmp",  # unused — fails before reaching the runtime
            presentation=PresentationPolicy.SILENT,
            no_interactive=False,
        )
