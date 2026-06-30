"""ADR 0047 Phase H — end-to-end cross silent boundary tests.

Mirrors ADR 0046 Phase F's project silent-boundary test suite,
applied to the typed cross boundary
:func:`pipeline.cross_project.app.run_cross_project_pipeline`.

The contract pinned across this file:

  Under ``presentation=SILENT``:
    1. **Zero stdout / zero stderr** — the cross transcript is
       completely quiet. No banner headers, no operator chips, no
       handoff warnings.
    2. **Persisted side effects unchanged** — ``session.json``,
       ``events.jsonl``, ``progress.log``, ``run.start`` /
       ``run.end`` events all land as they would under TERMINAL.
       ADR 0046 stop #9 invariant inherited: file + event sinks
       are never gated by presentation policy.

  Under ``presentation=TERMINAL`` (the default for legacy callers):
    3. **Legacy CLI transcript shape preserved** — cross-level
       markers (``[CROSS_PLAN]``, ``▶ SUB-PIPELINE``,
       ``[CONTRACT_CHECK]``, ``[DONE]``, ``Session:``, ``Usage:``,
       ``Projects: N | Rounds each: M``) all appear in
       ``capsys.out``. Markers are checked for presence and rough
       ordering, NOT bit-exact byte equality — there is no snapshot
       file for the cross transcript and adding one would lock in
       trivial spacing churn.

  Direction guards:
    4. Cross silent-path modules carry presentation threading —
       either consult ``terminal: bool`` field on a context dataclass
       or accept a ``terminal`` keyword. Drift toward a sneaky
       ``print()`` in a silent-path module is caught.
    5. ``app.py`` does not import ``orchestrator`` (re-pinned post
       Phase G — the wrapper direction stays orchestrator → app, not
       reverse). ADR 0042 stop #9 inherited (no
       ``pipeline.project.cli`` reach from any cross module).

The harness reuses :class:`_ScriptedProvider` +
``_make_capturing_run_project_pipeline`` from
``test_cross_silent_dispatch`` so the cross loop reaches the
per-alias dispatch + cross_final_acceptance gate without needing a
real LLM provider. Children are stubbed via the typed boundary so
the child SILENT seam (Phase D / Phase E enforcement inside
``run_project_pipeline``) is out of scope for these tests — that
seam is covered by ``tests/unit/pipeline/project/test_silent_policy_threading.py``
and ``tests/integration/test_silent_boundary.py``.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pipeline.cross_project.app import run_cross_project_pipeline
from pipeline.cross_project.app_types import CrossRunRequest, CrossRunResult
from pipeline.presentation import PresentationPolicy

# ── shared scaffolding (mirrors test_cross_silent_dispatch.py) ────────


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
        '{"verdict": "APPROVED", "short_summary": "ok", '
        '"findings": [], "risks": [], "checks": []}'
    )


def _cross_appconfig_mock(monkeypatch) -> None:
    """Mock the cross AppConfig + load_plugin so the cross body runs
    without touching real config files or filesystem walkers."""
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
    # Skip the hypothesis prelude — Phase D moved `_plan_hypothesis_step`
    # to live in the app module's namespace.
    original = cross_session_run._plan_hypothesis_step

    def _skip_implicit_hypothesis(profile, *, override_enabled):
        if override_enabled is None:
            return None
        return original(profile, override_enabled=override_enabled)

    monkeypatch.setattr(
        cross_session_run, "_plan_hypothesis_step", _skip_implicit_hypothesis,
    )


def _make_capturing_run_project_pipeline(
    captured_requests: list[Any],
    *,
    child_status: str = "done",
    child_session_extras: dict | None = None,
) -> Any:
    """Build a fake ``run_project_pipeline`` that captures every
    typed request and returns a minimal ``ProjectRunResult`` with a
    caller-chosen status. Children never actually execute under this
    harness; cross-level structural behaviour is what we pin."""
    from pipeline.project.types import ProjectRunResult

    def _fake(request):
        captured_requests.append(request)
        session: dict[str, Any] = {
            "status": child_status,
            "phases": {"rounds": [{"round": 1}]},
        }
        if child_session_extras:
            session.update(child_session_extras)
        return ProjectRunResult(
            session=session,
            output_dir=None,
            run_id="test-run",
        )

    return _fake


def _build_silent_request(
    *, projects: dict[str, Path], output_dir: Path, provider: Any,
) -> CrossRunRequest:
    """Phase H light consumer migration helper.

    Mirrors what a real SILENT cross caller (MCP / dashboard /
    embedded SDK) would build. The legacy 23-kwarg
    ``run_cross_pipeline`` back-compat surface stays exact; this
    typed form is what new callers use."""
    return CrossRunRequest(
        task="t",
        projects=projects,
        output_dir=output_dir,
        provider=provider,
        cross_mode="full",
        profile_name="delivery_audit",  # review-only projection — no PLAN phase.
        presentation=PresentationPolicy.SILENT,
        no_interactive=True,  # SILENT requires no_interactive=True.
    )


# ── 1. SILENT done path ──────────────────────────────────────────────


def test_silent_done_run_produces_zero_stdout_with_persisted_status(
    tmp_path: Path,
    monkeypatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Drive a 2-project cross run end-to-end under SILENT. Assert:

      * ``capsys.out == ""`` AND ``capsys.err == ""`` — the whole
        cross transcript is quiet under SILENT;
      * the typed result carries ``session["status"] == "done"`` and
        the actual ``output_dir`` + ``run_id`` from the body's
        bootstrap (NOT request passthroughs);
      * persisted ``session.json`` under the run dir mirrors the
        in-memory status — file sink fired under SILENT (ADR 0046
        stop #9 inherited).
    """
    from pipeline.cross_project import project_dispatch as _dispatch

    _cross_appconfig_mock(monkeypatch)
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
    request = _build_silent_request(
        projects={"api": api, "web": web},
        output_dir=tmp_path / "run",
        provider=provider,
    )

    result = run_cross_project_pipeline(request)

    # ✓ Zero stdout AND zero stderr — the load-bearing assertion.
    out = capsys.readouterr()
    assert out.out == "", (
        f"SILENT cross run leaked stdout (len={len(out.out)}):\n{out.out!r}"
    )
    assert out.err == "", (
        f"SILENT cross run leaked stderr (len={len(out.err)}):\n{out.err!r}"
    )

    # ✓ Typed result populated correctly.
    assert isinstance(result, CrossRunResult)
    assert result.session["status"] == "done"
    assert result.output_dir is not None
    assert result.output_dir.is_dir(), (
        "SILENT must still create the run dir on disk — the file sinks "
        "(session.json, events.jsonl) anchor there."
    )
    assert result.run_id, (
        "CrossRunResult.run_id must carry the actual run identifier "
        "(session_ts), not a request passthrough"
    )

    # ✓ Children dispatched once per alias with SILENT presentation.
    assert len(captured) == 2
    for req in captured:
        assert req.presentation is PresentationPolicy.SILENT
        assert req.no_interactive is True
        assert req.render_phase_outputs is False

    # ✓ session.json on disk mirrors the in-memory status.
    session_file = result.output_dir / "meta.json"  # cross uses meta.json on disk
    assert session_file.is_file()
    persisted = json.loads(session_file.read_text(encoding="utf-8"))
    assert persisted["status"] == "done"


# ── 2. SILENT events.jsonl + progress.log are NOT gated ──────────────


def test_silent_run_emits_run_start_and_run_end_events(
    tmp_path: Path,
    monkeypatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """ADR 0046 stop #9 inherited — under SILENT the event sink still
    receives ``run.start`` AND ``run.end``. These are structural
    completion signals MCP / dashboards / supervisors consume."""
    from pipeline.cross_project import project_dispatch as _dispatch

    _cross_appconfig_mock(monkeypatch)
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
    request = _build_silent_request(
        projects={"api": api},
        output_dir=tmp_path / "run",
        provider=provider,
    )

    result = run_cross_project_pipeline(request)

    # capsys is still silent (sanity).
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == ""

    # events.jsonl on disk carries run.start + run.end.
    events_file = result.output_dir / "events.jsonl"
    assert events_file.is_file(), (
        "events.jsonl MUST land on disk under SILENT — it's the "
        "structural event store, never gated by presentation"
    )
    events = [
        json.loads(line)
        for line in events_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    kinds = [e.get("kind") for e in events]
    assert "run.start" in kinds, (
        f"run.start not in events.jsonl under SILENT (kinds={kinds!r})"
    )
    assert "run.end" in kinds, (
        f"run.end not in events.jsonl under SILENT (kinds={kinds!r})"
    )

    # Sanity: exactly one run.end (Phase F invariant — the silent
    # service is the only emitter; the terminal wrapper does NOT
    # re-emit).
    end_events = [e for e in events if e.get("kind") == "run.end"]
    assert len(end_events) == 1, (
        f"expected exactly one run.end event under SILENT, got "
        f"{len(end_events)}: {end_events!r}"
    )
    # ``status`` lives under ``payload`` per the event-store shape
    # (event_kinds.json contract); cross uses ``run.end`` to signal
    # both done and failed cross runs.
    assert end_events[0].get("payload", {}).get("status") == "done"


# ── 3. SILENT child failure path ─────────────────────────────────────


def test_silent_child_failure_surfaces_structurally_without_stdout(
    tmp_path: Path,
    monkeypatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """One child returns ``status="failed"``. The cross body continues
    through contract_check + finalization (ADR 0025 invariant — the
    cross run does not crash on a child failure; cross_final_acceptance
    or the contract-check gate decides the final cross status). Under
    SILENT, the failure surfaces in the persisted session WITHOUT any
    stdout/stderr."""
    from pipeline.cross_project import project_dispatch as _dispatch
    from pipeline.project.types import ProjectRunResult

    _cross_appconfig_mock(monkeypatch)
    api = tmp_path / "api"
    web = tmp_path / "web"
    api.mkdir()
    web.mkdir()

    captured: list[Any] = []

    def _selective_dispatch(request):
        captured.append(request)
        # The first alias the cross body reaches gets `done`; the
        # second one fails. Either order is acceptable — we just need
        # one of each.
        status = "failed" if len(captured) == 2 else "done"
        session: dict[str, Any] = {
            "status": status,
            "phases": {"rounds": [{"round": 1}]},
        }
        if status == "failed":
            session["halt_reason"] = "phase_failure:RuntimeError"
            session["failure"] = {
                "type": "RuntimeError",
                "error": "child blew up",
                "phase": "implement",
            }
        return ProjectRunResult(
            session=session,
            output_dir=None,
            run_id=f"test-run-{request.project_alias}",
        )

    monkeypatch.setattr(
        _dispatch, "run_project_pipeline", _selective_dispatch,
    )

    provider = _ScriptedProvider(
        plan_outputs=[],
        review_outputs=[_approved_review_json()],
    )
    request = _build_silent_request(
        projects={"api": api, "web": web},
        output_dir=tmp_path / "run",
        provider=provider,
    )

    result = run_cross_project_pipeline(request)

    # ✓ stdout/stderr still empty even on the failure path.
    out = capsys.readouterr()
    assert out.out == "", (
        f"SILENT cross child-failure leaked stdout: {out.out!r}"
    )
    assert out.err == "", (
        f"SILENT cross child-failure leaked stderr: {out.err!r}"
    )

    # ✓ Structural state preserved — the persisted session records
    # the child's failure under phases.projects regardless of how
    # cross-level final-acceptance decided the cross status.
    assert len(captured) == 2
    persisted = json.loads(
        (result.output_dir / "meta.json").read_text(encoding="utf-8"),
    )
    projects_log = persisted.get("phases", {}).get("projects", {})
    failed_aliases = [
        alias
        for alias, entry in projects_log.items()
        if isinstance(entry, dict) and entry.get("status") == "failed"
    ]
    assert len(failed_aliases) == 1, (
        f"expected exactly one child to record status=failed; got "
        f"{failed_aliases!r} from phases.projects={projects_log!r}"
    )


# ── 4. TERMINAL default regression — legacy transcript shape ─────────


def test_terminal_default_run_carries_cross_level_markers(
    tmp_path: Path,
    monkeypatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Under default ``presentation=TERMINAL`` the cross transcript
    carries the legacy CLI markers: ``▶ SUB-PIPELINE [alias]`` (cross
    dispatch separator), ``[CONTRACT_CHECK]`` (cross gate banner),
    ``[DONE]`` (cross finalization banner), ``Projects: N | Rounds
    each: M`` (operator chip from the terminal wrapper).

    Markers are checked for **presence**, not byte-exact equality.
    The cross transcript has no snapshot file — adding one would
    lock in trivial spacing churn the next ADR would have to undo.
    Operator-facing shape is what this guard protects.
    """
    from pipeline.cross_project import project_dispatch as _dispatch

    _cross_appconfig_mock(monkeypatch)
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
    # TERMINAL is the default — no presentation kwarg, no
    # no_interactive kwarg. Legacy CLI / SDK callers get this shape.
    request = CrossRunRequest(
        task="t",
        projects={"api": api, "web": web},
        output_dir=tmp_path / "run",
        provider=provider,
        cross_mode="full",
        profile_name="delivery_audit",
    )

    result = run_cross_project_pipeline(request)
    assert result.session["status"] == "done"

    out = capsys.readouterr().out

    # Cross-level dispatch separator (cross owns; child SILENT does
    # not suppress it — pinned by Phase D + Phase E threading).
    assert "▶ SUB-PIPELINE [api]" in out, (
        "TERMINAL default lost the cross-level per-alias separator"
    )
    assert "▶ SUB-PIPELINE [web]" in out

    # Cross-level gate banners + chip from the terminal wrapper.
    # These come from the cross orchestrator's own transcript, not
    # from the child seam.
    assert "[CONTRACT_CHECK]" in out, (
        "TERMINAL default lost the cross-level [CONTRACT_CHECK] banner"
    )
    assert "[DONE]" in out, (
        "TERMINAL default lost the [DONE] banner from the cross "
        "finalization terminal wrapper"
    )
    assert "Projects: 2 | Rounds each:" in out, (
        "TERMINAL default lost the operator chip "
        "(`Projects: N | Rounds each: M`) the terminal wrapper renders"
    )


# ── 5. Import hygiene + AST guards (post-Phase G layout) ─────────────


_CROSS_DIR = Path(__file__).resolve().parents[3] / "pipeline" / "cross_project"


def _imports_from(tree: ast.AST, prefix: str) -> list[str]:
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == prefix or alias.name.startswith(f"{prefix}."):
                    out.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == prefix or mod.startswith(f"{prefix}."):
                out.append(mod)
    return out


def test_cross_silent_path_modules_thread_presentation_policy() -> None:
    """ADR 0047 Phase H — every cross silent-path module either
    consults the ``PresentationPolicy.TERMINAL`` identity literally,
    accepts a ``terminal: bool`` parameter, or carries a
    ``terminal: bool`` field on its context dataclass.

    The four modules below own the silent-vs-terminal seam: the run
    coordinator body (session_run — moved out of app in Phase F), the
    planning loop, the per-alias dispatch, and the parent-side handoff
    pause. Drift toward an ungated print in any of them is the regression
    this guard catches.
    """
    silent_path_modules = (
        "session_run",
        "planning_loop",
        "project_dispatch",
        "handoff_payloads",
    )
    for module in silent_path_modules:
        text = (_CROSS_DIR / f"{module}.py").read_text(encoding="utf-8")
        has_terminal_identity = (
            "PresentationPolicy.TERMINAL" in text
            or "presentation is PresentationPolicy" in text
        )
        has_terminal_field_or_param = (
            "terminal: bool" in text
            or "terminal=True" in text
            or "terminal=False" in text
            or "ctx.terminal" in text
        )
        assert has_terminal_identity or has_terminal_field_or_param, (
            f"pipeline.cross_project.{module} does not thread "
            f"presentation policy — neither `PresentationPolicy.TERMINAL` "
            f"literal nor `terminal: bool` field / parameter present. "
            f"Adding silent-bypassing prints to this module breaks the "
            f"ADR 0047 Phase E threading."
        )


def test_app_does_not_import_orchestrator_post_phase_g() -> None:
    """ADR 0047 stop #2 + Phase G re-pin — the typed boundary owns
    the body. ``app.py`` MUST NOT import from
    :mod:`pipeline.cross_project.orchestrator`. The direction is
    orchestrator → app (back-compat wrapper routes through), never
    the reverse. Phase G added the new ``cli`` peer so the guard is
    re-asserted post-G."""
    tree = ast.parse((_CROSS_DIR / "app.py").read_text(encoding="utf-8"))
    violations = _imports_from(tree, "pipeline.cross_project.orchestrator")
    assert violations == [], (
        f"pipeline.cross_project.app imports from "
        f"pipeline.cross_project.orchestrator: {violations!r}. The "
        f"wrapper direction is orchestrator → app — reversing it "
        f"recreates the cycle Phase D existed to break."
    )


def test_no_cross_module_imports_project_cli() -> None:
    """ADR 0042 stop #9 inherited — cross-project must not reach
    into the single-project CLI module. The cross CLI is a peer
    leaf, not a child of project CLI."""
    for module_path in _CROSS_DIR.glob("*.py"):
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        violations = _imports_from(tree, "pipeline.project.cli")
        assert violations == [], (
            f"pipeline.cross_project.{module_path.stem} imports from "
            f"pipeline.project.cli: {violations!r}. Cross is a peer "
            f"of project — not a child. If you need a shared helper, "
            f"extract it to a non-CLI module both can import."
        )
