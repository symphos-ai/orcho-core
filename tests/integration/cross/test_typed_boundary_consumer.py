"""Reference smoke for the cross-project typed silent boundary.

Mirrors ``tests/integration/project/test_typed_boundary_consumer.py``
applied to the cross boundary. Demonstrates — and pins — the
canonical usage pattern that future SDK / MCP / web / orchestration
consumers should follow when driving a cross-project run from code:

    result = run_cross_project_pipeline(
        CrossRunRequest(
            ...,
            presentation=PresentationPolicy.SILENT,
            no_interactive=True,
        )
    )

    # stdout / stderr are empty.
    # The contract surface is the structured state, not the transcript:
    #   - result.session       (in-memory snapshot of the persisted dict)
    #   - result.output_dir    (cross run directory on disk)
    #   - result.run_id        (cross session_ts identifier)
    #   - <run_dir>/meta.json     (persisted cross session contract)
    #   - <run_dir>/events.jsonl  (run.start / phase.* / run.end events)

Inherited invariant from ADR 0046 Phase D: every per-alias child the
cross body dispatches goes out as
``ProjectRunRequest(presentation=SILENT, no_interactive=True)``.
Cross consumers do not have to thread this manually — the cross body
owns it. This smoke pins that contract from the consumer side: child
requests captured at the seam are inspected and verified.

ADR cross-links: ADR 0047 (typed cross boundary) + ADR 0046 (silent
presentation policy) + ADR 0042 (project boundary it sits over). The
deeper *contract* coverage lives in
``tests/integration/cross/test_cross_silent_boundary.py``; this
module keeps the **consumer-facing** subset that documents the shape.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pipeline.cross_project.app import run_cross_project_pipeline
from pipeline.cross_project.app_types import (
    CrossRunRequest,
    CrossRunResult,
)
from pipeline.presentation import PresentationPolicy

# ── scripted runtime + provider (reference scaffolding) ───────────────


class _ScriptedRuntime:
    """Stand-in agent runtime: returns canned strings, never spawns a
    real LLM. Reference scaffolding only — real consumers wire their
    own ``AgentProvider`` here."""

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.model = "fake-model"

    def invoke(self, prompt: str, cwd: str, **kwargs) -> str:
        if self.outputs:
            return self.outputs.pop(0)
        return ""


class _ScriptedProvider:
    """Stand-in :class:`agents.runtimes.AgentProvider` returning the
    same scripted runtime for both ``claude(...)`` and ``codex(...)``
    accessors. Keeps the cross body's review gate happy without
    needing real backends."""

    def __init__(
        self, plan_outputs: list[str], review_outputs: list[str],
    ) -> None:
        self._plan = _ScriptedRuntime(plan_outputs)
        self._review = _ScriptedRuntime(review_outputs)

    def resolve(self, runtime: str, _model: str, *, effort: str | None = None):  # noqa: ARG002
        return self._review if runtime == "codex" else self._plan

    def claude(self, _model: str) -> _ScriptedRuntime:
        return self._plan

    def codex(self, _model: str) -> _ScriptedRuntime:
        return self._review


def _approved_review_json() -> str:
    """The single JSON shape the reviewer gate accepts. See
    ``pipeline.review_parser.parse_review`` — there is no prose
    verdict path."""
    return (
        '{"verdict": "APPROVED", "short_summary": "ok", '
        '"findings": [], "risks": [], "checks": []}'
    )


def _stub_cross_appconfig(monkeypatch) -> None:
    """Skip real AppConfig + plugin discovery so the cross body runs
    without touching the host filesystem layout. Real consumers point
    the request's ``projects=`` at real checkouts and don't need any
    of this — but the reference smoke has no real projects to load."""
    from pipeline.cross_project import (
        run_setup as cross_run_setup,
        session_run as cross_session_run,
    )

    monkeypatch.setattr(
        cross_session_run.config.AppConfig,
        "load",
        classmethod(lambda _cls: SimpleNamespace(
            hypothesis={"enabled": False},
            task_language="English",
            pipeline={},
            artifacts={},
        )),
    )
    monkeypatch.setattr(
        cross_run_setup, "load_plugin",
        lambda _path: SimpleNamespace(
            name="Project", language="Python", architecture="",
            file_hints=[],
        ),
    )
    # Hypothesis prelude isn't part of the consumer reference shape.
    original = cross_session_run._plan_hypothesis_step

    def _skip_implicit_hypothesis(profile, *, override_enabled):
        if override_enabled is None:
            return None
        return original(profile, override_enabled=override_enabled)

    monkeypatch.setattr(
        cross_session_run, "_plan_hypothesis_step", _skip_implicit_hypothesis,
    )


def _make_capturing_child_dispatch(
    captured: list[Any],
) -> Any:
    """Fake ``run_project_pipeline`` for the per-alias seam. Captures
    every typed ``ProjectRunRequest`` and returns a minimal ``done``
    child result. Real consumers never touch this — the cross body
    builds child requests internally."""
    from pipeline.project.types import ProjectRunResult

    def _fake(request):
        captured.append(request)
        return ProjectRunResult(
            session={
                "status": "done",
                "phases": {"rounds": [{"round": 1}]},
            },
            output_dir=None,
            run_id=f"test-child-{request.project_alias}",
        )

    return _fake


# ── Phase B smoke ─────────────────────────────────────────────────────


def test_consumer_drives_silent_cross_run_via_typed_boundary(
    tmp_path: Path,
    monkeypatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Canonical consumer pattern for a cross-project run.

    Demonstrates the four-step contract a future cross consumer
    follows:

        1. Build a typed ``CrossRunRequest`` with
           ``presentation=PresentationPolicy.SILENT`` and
           ``no_interactive=True`` (the post-init invariant rejects
           SILENT without no_interactive).
        2. Call ``run_cross_project_pipeline(request)``; receive a
           typed ``CrossRunResult``.
        3. Read status / completion from ``result.session`` and the
           persisted ``meta.json`` — never from stdout.
        4. Read structural progress from ``events.jsonl`` —
           ``run.start``, ``phase.*``, ``run.end``.

    Plus one cross-only invariant: every per-alias child the cross
    body dispatches goes out as
    ``ProjectRunRequest(presentation=SILENT, no_interactive=True)``.
    Cross consumers do not thread the child seam manually — it's owned
    by the cross body. ADR 0046 Phase D.
    """
    from pipeline.cross_project import project_dispatch as _dispatch

    _stub_cross_appconfig(monkeypatch)
    api = tmp_path / "api"
    web = tmp_path / "web"
    api.mkdir()
    web.mkdir()

    captured_children: list[Any] = []
    monkeypatch.setattr(
        _dispatch, "run_project_pipeline",
        _make_capturing_child_dispatch(captured_children),
    )

    # ── step 1: build the typed cross request ──────────────────────
    request = CrossRunRequest(
        task="silent cross consumer reference smoke",
        projects={"api": api, "web": web},
        output_dir=tmp_path / "run",
        provider=_ScriptedProvider(
            plan_outputs=[],
            review_outputs=[_approved_review_json()],
        ),
        cross_mode="full",
        profile_name="delivery_audit",  # review-only — no PLAN phase needed.
        presentation=PresentationPolicy.SILENT,
        no_interactive=True,  # required by SILENT (post-init invariant).
    )

    # ── step 2: invoke the typed boundary ──────────────────────────
    result = run_cross_project_pipeline(request)

    # ── contract pin: NO stdout / NO stderr ────────────────────────
    out = capsys.readouterr()
    assert out.out == "", (
        f"SILENT cross typed boundary leaked stdout. Consumers must "
        f"not need to suppress / capture / parse it. Got "
        f"{len(out.out)} chars: {out.out[:200]!r}"
    )
    assert out.err == "", (
        f"SILENT cross typed boundary leaked stderr: {out.err[:200]!r}"
    )

    # ── step 3: read status from the typed result ──────────────────
    assert isinstance(result, CrossRunResult)
    assert result.session["status"] == "done", (
        f"consumer reads completion from result.session['status']; "
        f"got {result.session.get('status')!r}"
    )
    assert result.output_dir is not None
    assert result.output_dir.is_dir(), (
        "result.output_dir points at the on-disk cross run directory; "
        "consumers anchor here for any post-run inspection"
    )
    assert result.run_id, (
        "result.run_id is the cross session identifier — consumers "
        "use it to correlate cross-level logs with per-alias children"
    )

    # ── step 3 (continued): read persisted meta.json ───────────────
    meta_path = result.output_dir / "meta.json"
    assert meta_path.is_file(), (
        "meta.json is the durable cross consumer contract — must "
        "exist under SILENT (ADR 0046 stop #9 inherited: file sinks "
        "are never gated by presentation)"
    )
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["status"] == "done"

    # ── step 4: read structural events from events.jsonl ───────────
    events_path = result.output_dir / "events.jsonl"
    assert events_path.is_file(), (
        "events.jsonl is the cross-level structural event store — "
        "never gated by presentation (ADR 0046 stop #9 inherited)"
    )
    events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    kinds = {e.get("kind") for e in events}

    assert "run.start" in kinds, (
        f"consumer reference: run.start must appear in cross "
        f"events.jsonl. Saw kinds={sorted(kinds)}"
    )
    assert "run.end" in kinds

    # Phase F invariant — exactly one run.end (the silent service is
    # the only emitter under SILENT).
    run_end_events = [e for e in events if e.get("kind") == "run.end"]
    assert len(run_end_events) == 1, (
        f"expected exactly one run.end event; got {len(run_end_events)}"
    )
    assert run_end_events[0].get("payload", {}).get("status") == "done"

    # ── cross-only invariant: child SILENT seam is automatic ───────
    #
    # The cross body fans out per-alias dispatch. Each child request
    # carries ``presentation=SILENT, no_interactive=True`` so child
    # transcripts never leak into the cross transcript. ADR 0046
    # Phase D. Cross consumers do not have to enforce this — they
    # just need to know it's true.
    assert len(captured_children) == 2, (
        f"expected one child request per alias; got "
        f"{len(captured_children)}"
    )
    for child_req in captured_children:
        assert child_req.presentation is PresentationPolicy.SILENT, (
            "cross body must dispatch children under SILENT regardless "
            "of the cross-level presentation policy — they are not "
            "operator-facing"
        )
        assert child_req.no_interactive is True, (
            "child SILENT requests must also be no_interactive=True "
            "(post-init invariant on ProjectRunRequest)"
        )
        assert child_req.render_phase_outputs is False, (
            "SILENT cross consumers must not receive child phase output "
            "previews on stdout"
        )


def test_consumer_cross_request_rejects_interactive_combo() -> None:
    """Reference for the cross SILENT/no_interactive invariant.

    Mirrors the project-side check. ``presentation=SILENT,
    no_interactive=False`` is rejected at ``CrossRunRequest``
    construction time, so embedders get a loud ``ValueError`` at the
    API boundary instead of a half-silent cross run later.
    """
    with pytest.raises(ValueError, match="no_interactive=True"):
        CrossRunRequest(
            task="invalid",
            projects={"a": Path("/tmp/a")},  # unused — fails at __post_init__
            presentation=PresentationPolicy.SILENT,
            no_interactive=False,
        )
