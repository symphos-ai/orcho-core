"""ADR 0046 Phase D — cross-project silent dispatch.

This module guards the win condition of ADR 0046: when the cross-project
orchestrator dispatches a per-alias child run, it must use
``run_project_pipeline(ProjectRunRequest(presentation=SILENT,
no_interactive=True))`` so the **child's** banners / success chips /
DONE block / handoff warnings stay out of the **cross transcript**,
while the **cross-level** ``▶ SUB-PIPELINE [alias]`` separator and any
cross-orchestrator-owned output is preserved.

Two complementary guards:

1. **Contract pin (``test_cross_dispatch_uses_silent_presentation``)** —
   monkeypatches ``run_project_pipeline`` at the import site in
   ``pipeline.cross_project.project_dispatch`` and captures the
   ``ProjectRunRequest`` passed in. Asserts:
     * ``request.presentation is PresentationPolicy.SILENT``
     * ``request.no_interactive is True``
     * cross-level ``▶ SUB-PIPELINE [alias]`` banner DID fire (sanity:
       we didn't accidentally suppress the cross side too)
     * for an N-alias run, the request is built N times
   This is the load-bearing contract — without it the entire ADR 0046
   refactor is decorative.

2. **Transcript guard (``test_cross_transcript_omits_child_banners``)** —
   same setup but assert the captured ``capsys.out`` does NOT contain
   strings that come **only** from inside the child run (``Codemap:``
   / ``Attachments:`` success chips, ``Handoff (`` outcome line,
   ``Resuming from checkpoint`` bootstrap banner, ``FAILED in`` red
   block, the per-phase ``[PLAN]`` / ``[VALIDATE_PLAN]`` /
   ``[IMPLEMENT]`` etc. banner headers). The list deliberately does
   NOT include strings shared with the cross orchestrator's own
   transcript (``DONE``, ``Pipeline complete``, ``Session:``,
   ``Usage:``, ``Progress log:``, ``Run dir:``) — cross-level
   transcript hygiene is the next ADR's scope, not Phase D's
   (ADR 0046 § "Out of scope"). Because ``run_project_pipeline`` is
   stubbed at the import site, the child never executes; the
   assertion proves that nothing else upstream (the cross
   orchestrator itself, the dispatch helper, the pre-render path) is
   leaking child-only patterns into the cross transcript. The actual
   SILENT enforcement inside the child run is covered by
   ``tests/unit/pipeline/project/test_silent_policy_threading.py`` +
   the upcoming Phase F ``test_silent_boundary.py``.

The setup mirrors ``test_cross_orchestrator.py``'s lightweight
``_ScriptedProvider`` + ``_cross_test_appconfig_mock`` pattern (cross_mode=
``"full"`` + ``profile_name="delivery_audit"``) so the cross loop reaches the
per-alias dispatch without needing a real LLM provider.
"""

from __future__ import annotations

import json
from dataclasses import fields
from types import SimpleNamespace
from typing import Any

# ── shared scripted provider (mirrors test_cross_orchestrator.py) ─────


class _ScriptedRuntime:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.model = "fake-model"
        self.calls: list[tuple[str, str, dict]] = []

    def invoke(self, prompt: str, cwd: str, **kwargs) -> str:
        self.calls.append((prompt, cwd, kwargs))
        if self.outputs:
            return self.outputs.pop(0)
        return ""


class _ScriptedProvider:
    def __init__(self, plan_outputs: list[str], review_outputs: list[str]) -> None:
        self.plan = _ScriptedRuntime(plan_outputs)
        self.review = _ScriptedRuntime(review_outputs)

    def resolve(self, runtime: str, _model: str, *, effort: str | None = None):  # noqa: ARG002
        return self.review if runtime == "codex" else self.plan

    def claude(self, _model: str) -> _ScriptedRuntime:
        return self.plan

    def codex(self, _model: str) -> _ScriptedRuntime:
        return self.review


def _approved_review_json() -> str:
    return (
        '{"verdict": "APPROVED", "short_summary": "Plan is coherent.", '
        '"findings": [], "risks": [], "checks": []}'
    )


def _cross_appconfig_mock(monkeypatch, cross) -> None:
    monkeypatch.setattr(
        cross.config.AppConfig,
        "load",
        classmethod(lambda _cls: SimpleNamespace(
            hypothesis={"enabled": False},
            task_language="English",
            pipeline={},
            artifacts={},
        )),
    )
    monkeypatch.setattr(
        cross, "load_plugin",
        lambda _path: SimpleNamespace(
            name="Project", language="Python", architecture="",
            file_hints=[],
        ),
    )
    # ADR 0047 Phase D — `_plan_hypothesis_step` is resolved by the run
    # body in ``pipeline.cross_project.app``'s namespace.
    from pipeline.cross_project import session_run as cross_session_run
    original = cross_session_run._plan_hypothesis_step

    def _skip_implicit_hypothesis(profile, *, override_enabled):
        if override_enabled is None:
            return None
        return original(profile, override_enabled=override_enabled)

    monkeypatch.setattr(cross_session_run, "_plan_hypothesis_step", _skip_implicit_hypothesis)


def _make_capturing_run_project_pipeline(
    captured_requests: list[Any],
) -> Any:
    """Build a fake ``run_project_pipeline`` that captures each request
    object and returns a minimal ``ProjectRunResult``. Cross-level
    code reads ``.session`` to decide done / paused / failed; we
    return a ``status="done"`` session so the cross loop continues."""
    from pipeline.project.types import ProjectRunResult

    def _fake(request):
        captured_requests.append(request)
        session = {
            "status": "done",
            "phases": {"rounds": [{"round": 1}]},
        }
        request.output_dir.mkdir(parents=True, exist_ok=True)
        (request.output_dir / "meta.json").write_text(
            json.dumps(session),
            encoding="utf-8",
        )
        return ProjectRunResult(
            session=session,
            output_dir=request.output_dir,
            run_id="test-run",
        )

    return _fake


# ── tests ─────────────────────────────────────────────────────────────


def test_cross_dispatch_uses_silent_presentation(
    tmp_path, monkeypatch,
) -> None:
    """ADR 0046 Phase D contract pin.

    Cross builds ``ProjectRunRequest`` with ``presentation=SILENT`` +
    ``no_interactive=True`` for every per-alias dispatch. Drift here
    is the entire point of the refactor."""
    from pipeline.cross_project import (
        orchestrator as cross,
        project_dispatch as _dispatch,
    )
    from pipeline.project.types import PresentationPolicy, ProjectRunRequest

    _cross_appconfig_mock(monkeypatch, cross)
    api = tmp_path / "api"
    web = tmp_path / "web"
    api.mkdir()
    web.mkdir()

    captured: list[Any] = []
    monkeypatch.setattr(
        _dispatch, "run_project_pipeline",
        _make_capturing_run_project_pipeline(captured),
    )

    provider = _ScriptedProvider(
        plan_outputs=[],
        review_outputs=[_approved_review_json()],
    )
    cross.run_cross_pipeline(
        task="x",
        projects={"api": api, "web": web},
        output_dir=tmp_path / "run",
        provider=provider,
        cross_mode="full",
        profile_name="delivery_audit",  # review-only projection: no plan needed.
    )

    assert len(captured) == 2, (
        f"expected 2 per-alias dispatches; got {len(captured)}: "
        f"{[r.project_alias for r in captured]}"
    )
    for request in captured:
        assert isinstance(request, ProjectRunRequest), (
            f"cross must pass a typed ProjectRunRequest; got {type(request)}"
        )
        assert request.presentation is PresentationPolicy.SILENT, (
            f"alias={request.project_alias!r} dispatched with "
            f"{request.presentation!r}; ADR 0046 Phase D requires SILENT"
        )
        assert request.no_interactive is True, (
            f"alias={request.project_alias!r} dispatched with "
            f"no_interactive={request.no_interactive!r}; SILENT + "
            "no_interactive=False is rejected by ProjectRunRequest.__post_init__"
        )
        assert request.render_phase_outputs is True, (
            "terminal cross runs must keep child banners/finalization silent "
            "while still rendering parsed phase response blocks"
        )
        # Sanity: the request still carries the load-bearing identifiers
        # cross relies on for the parent → children timeline + resume.
        assert request.parent_run_id, (
            "parent_run_id must be set so MCP / evidence can reconstruct "
            "the parent → children timeline"
        )
        assert request.project_alias in {"api", "web"}
        assert request.plan_source == "cross"
        assert request.resume_from is None, (
            "a fresh cross child must not be marked as a checkpoint resume; "
            "project_alias already carries its cross identity"
        )
        assert request.preallocated_output_dir is True, (
            "cross prepares handoff artifacts before fresh child dispatch; "
            "that directory ownership must be explicit, not encoded as resume"
        )


def test_cross_transcript_omits_child_banners(
    tmp_path, monkeypatch, capsys,
) -> None:
    """ADR 0046 Phase D — cross transcript hygiene.

    The cross-level ``▶ SUB-PIPELINE [alias]`` separator MUST still
    appear in the operator's terminal (it's the only visual cue for
    which child is running). Per-project DONE banners / Session lines /
    Usage rollups / handoff warnings MUST NOT appear.

    This test stubs ``run_project_pipeline`` at the import site so the
    child never executes — the assertion proves nothing upstream
    (cross orchestrator, dispatch helper, banner pre-render code) is
    leaking the patterns we promise to suppress under SILENT. The
    actual SILENT enforcement inside ``_PipelineRun`` is covered by
    ``test_silent_policy_threading.py`` (Phase C) and
    ``test_silent_boundary.py`` (Phase F)."""
    from pipeline.cross_project import (
        orchestrator as cross,
        project_dispatch as _dispatch,
    )

    _cross_appconfig_mock(monkeypatch, cross)
    api = tmp_path / "api"
    web = tmp_path / "web"
    api.mkdir()
    web.mkdir()

    captured: list[Any] = []
    monkeypatch.setattr(
        _dispatch, "run_project_pipeline",
        _make_capturing_run_project_pipeline(captured),
    )

    provider = _ScriptedProvider(
        plan_outputs=[],
        review_outputs=[_approved_review_json()],
    )
    cross.run_cross_pipeline(
        task="x",
        projects={"api": api, "web": web},
        output_dir=tmp_path / "run",
        provider=provider,
        cross_mode="full",
        profile_name="delivery_audit",
    )

    out = capsys.readouterr().out

    # Cross-level banners: MUST still appear.
    assert "▶ SUB-PIPELINE [api]" in out, (
        "cross-level per-alias separator must survive — it's the operator's "
        "only visual cue distinguishing which child is running"
    )
    assert "▶ SUB-PIPELINE [web]" in out

    # Per-project child terminal lines: MUST NOT appear in cross
    # transcript. These are leak patterns ADR 0046 Phase D was designed
    # to suppress — strings emitted ONLY from inside the child run
    # (``_PipelineRun`` + ``finalize_with_terminal_output`` +
    # ``_print_pipeline_header`` + ``success(...)`` chips on the
    # silent-path modules). Strings that ALSO appear in the cross
    # orchestrator's own transcript ("Pipeline complete", "Session:",
    # "Usage:", "Progress log:", "Run dir:") are deliberately NOT in
    # this list — Phase D leaves the cross-level transcript untouched
    # per the ADR's "Out of scope" section ("full cross-project app
    # boundary" is a separate follow-up ADR). This guard only catches
    # leaks attributable to the child seam.
    child_only_leak_patterns = [
        "Codemap:",                 # site 3 — child PLAN-prompt codemap chip
        "Attachments:",             # site 4 — child attachment threading chip
        "Handoff (",                # site 11 — child print_handoff_outcome
        "Resuming from checkpoint", # site 15 — child bootstrap resume
        "FAILED in",                # site 14 — child _record_phase_failure
        # Per-phase START/END banner headers — child-only because the
        # cross orchestrator uses ``═══`` banners with [CROSS_*] /
        # [CONTRACT_CHECK] / [CROSS_FINAL_ACCEPTANCE] / [DONE] tags
        # (not the bare child phase names below).
        "[PLAN]",
        "[VALIDATE_PLAN]",
        "[IMPLEMENT]",
        "[REVIEW_CHANGES]",
        "[REPAIR_CHANGES]",
    ]
    for pattern in child_only_leak_patterns:
        assert pattern not in out, (
            f"cross transcript contains per-child leak {pattern!r}; "
            f"the child run under PresentationPolicy.SILENT should not "
            f"surface this string into the parent transcript"
        )


def test_cross_dispatch_import_does_not_regain_cli_dependency() -> None:
    """ADR 0042 stop #9 — cross must not depend on
    ``pipeline.project.cli``. Phase D switches the library call from
    ``run_pipeline`` (legacy wrapper in ``pipeline.project.app``) to
    ``run_project_pipeline`` (typed boundary, same module) +
    ``ProjectRunRequest`` / ``PresentationPolicy`` from
    ``pipeline.project.types``. None of these are CLI leafs."""
    import ast
    from pathlib import Path

    source = Path(
        "pipeline/cross_project/project_dispatch.py",
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    banned_modules = {"pipeline.project.cli"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module not in banned_modules, (
                f"cross_project/project_dispatch.py imports from "
                f"{node.module} — ADR 0042 stop #9 forbids cross "
                "depending on the project CLI leaf"
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in banned_modules


def test_cross_dispatch_imports_silent_boundary_names() -> None:
    """Positive smoke: cross imports the typed silent boundary
    (``run_project_pipeline``, ``ProjectRunRequest``,
    ``PresentationPolicy``) — i.e. the Phase D wire actually shipped."""
    import pipeline.cross_project.project_dispatch as _dispatch

    assert hasattr(_dispatch, "run_project_pipeline")
    assert hasattr(_dispatch, "ProjectRunRequest")
    assert hasattr(_dispatch, "PresentationPolicy")

    # And the legacy 28-kwarg wrapper is no longer imported here —
    # the swap is the whole point of Phase D.
    assert not hasattr(_dispatch, "run_pipeline"), (
        "cross dispatch should no longer import the legacy 28-kwarg "
        "``run_pipeline`` wrapper; ADR 0046 Phase D switched the library "
        "call to ``run_project_pipeline(ProjectRunRequest(...))``"
    )


# Reference avoidance: ``fields`` was imported but used only as a
# documentation hook in this file; assert it remains usable for any
# future tests that want to introspect ``ProjectRunRequest`` shape.
def test_project_run_request_field_set_resolves() -> None:
    from pipeline.project.types import ProjectRunRequest
    names = {f.name for f in fields(ProjectRunRequest)}
    assert "presentation" in names
    assert "no_interactive" in names


# ── ADR 0047 Phase H — light library consumer migration ──────────────


def test_cross_dispatch_uses_silent_presentation_via_typed_boundary(
    tmp_path, monkeypatch,
) -> None:
    """ADR 0047 Phase H — typed boundary as drop-in for new callers.

    Re-runs the ADR 0046 Phase D contract pin
    (``test_cross_dispatch_uses_silent_presentation``) but drives
    through the typed boundary
    :func:`pipeline.cross_project.app.run_cross_project_pipeline`
    with a :class:`CrossRunRequest` instead of the legacy 23-kwarg
    ``run_cross_pipeline`` wrapper.

    Locks two things at once:
      1. The Phase D child-SILENT contract still holds when the
         parent is driven via the typed boundary — i.e. the boundary
         is a real drop-in, not a partial shim.
      2. The legacy ``run_cross_pipeline`` wrapper (covered by the
         original Phase D test above) and the new typed boundary
         resolve to the same per-alias dispatch behaviour.

    The legacy wrapper test stays — both forms are expected to keep
    working through ADR 0047 Phase I and beyond.
    """
    from pipeline.cross_project import (
        orchestrator as cross,
        project_dispatch as _dispatch,
    )
    from pipeline.cross_project.app import run_cross_project_pipeline
    from pipeline.cross_project.app_types import CrossRunRequest
    from pipeline.project.types import PresentationPolicy, ProjectRunRequest

    _cross_appconfig_mock(monkeypatch, cross)
    api = tmp_path / "api"
    web = tmp_path / "web"
    api.mkdir()
    web.mkdir()

    captured: list[Any] = []
    monkeypatch.setattr(
        _dispatch, "run_project_pipeline",
        _make_capturing_run_project_pipeline(captured),
    )

    provider = _ScriptedProvider(
        plan_outputs=[],
        review_outputs=[_approved_review_json()],
    )
    request = CrossRunRequest(
        task="x",
        projects={"api": api, "web": web},
        output_dir=tmp_path / "run",
        provider=provider,
        cross_mode="full",
        profile_name="delivery_audit",
    )
    result = run_cross_project_pipeline(request)

    # Cross-level done status threads through the typed result.
    assert result.session["status"] == "done"

    # Same per-alias contract as the legacy-wrapper test: every child
    # gets SILENT + no_interactive=True.
    assert len(captured) == 2
    for child_req in captured:
        assert isinstance(child_req, ProjectRunRequest)
        assert child_req.presentation is PresentationPolicy.SILENT
        assert child_req.no_interactive is True
        assert child_req.render_phase_outputs is True


def test_typed_boundary_accepts_request_builder_helper(
    tmp_path, monkeypatch,
) -> None:
    """ADR 0047 Phase H — `CrossRunRequest.from_kwargs(...)` is the
    documented integration helper for callers that have kwargs in
    hand (MCP SDK, future orcho-web cross bridge). Drives a 1-project
    cross run via the kwarg-helper form to verify the wiring."""
    from pipeline.cross_project import (
        orchestrator as cross,
        project_dispatch as _dispatch,
    )
    from pipeline.cross_project.app import run_cross_project_pipeline
    from pipeline.cross_project.app_types import CrossRunRequest

    _cross_appconfig_mock(monkeypatch, cross)
    api = tmp_path / "api"
    api.mkdir()

    captured: list[Any] = []
    monkeypatch.setattr(
        _dispatch, "run_project_pipeline",
        _make_capturing_run_project_pipeline(captured),
    )

    provider = _ScriptedProvider(
        plan_outputs=[],
        review_outputs=[_approved_review_json()],
    )
    # Kwarg-helper form — the call shape callers with raw arguments
    # use. Validates the typed boundary at its widest entry surface.
    request = CrossRunRequest.from_kwargs(
        task="x",
        projects={"api": api},
        output_dir=tmp_path / "run",
        provider=provider,
        cross_mode="full",
        profile_name="delivery_audit",
    )
    result = run_cross_project_pipeline(request)
    assert result.session["status"] == "done"
    assert len(captured) == 1
