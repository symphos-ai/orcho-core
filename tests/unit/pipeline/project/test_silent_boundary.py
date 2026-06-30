"""ADR 0046 Phase F — end-to-end zero-stdout boundary contract.

This is the load-bearing test for ADR 0046's win condition: any caller
that drives ``run_project_pipeline(ProjectRunRequest(presentation=
SILENT, no_interactive=True))`` gets **zero stdout/stderr** while
every persisted side-effect (``session.json``, ``events.jsonl``,
checkpoint, worktree teardown) stays byte-identical to the
``TERMINAL`` path.

Phase C unit tests (``test_silent_policy_threading.py``) pinned the
per-site gates in isolation; this module exercises the **whole**
``run_project_pipeline`` boundary with ``MockAgentProvider`` driving
a real child run end-to-end.

Coverage:

1. **Done path** — ``profile_name="task"`` + ``MockAgentProvider``.
   Asserts ``capsys.out == ""``, ``capsys.err == ""``, persisted
   ``session.json`` exists, ``events.jsonl`` is non-empty and carries
   the canonical ``phase.start`` / ``phase.end`` / ``run.end`` events
   that downstream consumers (orcho-web, MCP, dashboards) read.

2. **Handoff-pause path** — ``MockAgentProvider(
   validate_plan_reject_rounds=99)`` + ``profile_name="feature"`` so
   the validate_plan loop exhausts its budget and triggers a phase
   handoff. Load-bearing — site 10 in the inventory
   (``apply_phase_handoff_pause`` ``warn(...)``) and the Phase C r5
   ``log_phase`` split are the reason this path needs its own test:
   pre-r5 SILENT dropped the ``phase.start`` for HYPOTHESIS and the
   ``warn(...)`` for the handoff pause leaked to stderr. The test
   pins both: ``capsys.out == capsys.err == ""`` AND the
   ``phase.handoff_requested`` event is present in ``events.jsonl``
   AND ``session["status"] == "awaiting_phase_handoff"`` with a
   populated ``phase_handoff`` payload.

3. **Failure path** — monkeypatch ``pipeline.project.profile_dispatch.run_profile``
   to raise ``RuntimeError("test failure")``. The exception propagates
   through ``dispatch_via_v2_profile``'s exception handler → ``run._record_phase_failure``
   (site 14 — gated under SILENT for the red FAILED block + first-line
   ``warn``) → re-raise. ``run_project_pipeline`` does NOT return a
   ``ProjectRunResult`` on this path; we wrap in ``pytest.raises`` and
   read assertions from persisted state. Pins: ``capsys.out == ""``
   AND ``capsys.err == ""`` even though ``_record_phase_failure``
   normally prints the red block + ``warn(first_line)``; the
   on-disk ``session.json`` carries ``status="failed"`` +
   ``failure.type="RuntimeError"`` + ``halt_reason``; the on-disk
   ``events.jsonl`` carries the ``run.end`` event with
   ``status="failed"``.

4. **Grep guard** — cheap literal-grep pin that every silent-path
   module contains at least one occurrence of
   ``presentation is PresentationPolicy.TERMINAL`` or
   ``_presentation is PresentationPolicy.TERMINAL``. Trips on
   accidental gate removal without needing to re-run the slow
   integration tests above.

5. **Terminal-default regression** — same ``MockAgentProvider`` setup
   as the done path but with default ``presentation=TERMINAL``. Asserts
   ``capsys.out`` contains the legacy transcript shape
   (``PLAN`` / ``IMPLEMENT`` / ``DONE`` / ``Session:`` / ``Usage:``)
   so the default code path is byte-identical to pre-ADR-0046 CLI.

The five tests sit at the integration boundary; Phase C unit tests +
Phase D contract pin cover finer-grained slices.
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
)

# ── shared helpers ────────────────────────────────────────────────────


def _silent_request(
    *, task: str, project_dir: Path, output_dir: Path,
    profile_name: str = "task",
    provider: MockAgentProvider | None = None,
    max_rounds: int = 1,
    render_phase_outputs: bool = False,
) -> ProjectRunRequest:
    """Build a typed SILENT request mirroring the Phase D cross dispatch
    shape. Default ``profile_name="task"`` keeps the done-path test
    fast (no plan loop); callers override for handoff / failure paths."""
    return ProjectRunRequest(
        task=task,
        project_dir=str(project_dir),
        output_dir=output_dir,
        max_rounds=max_rounds,
        profile_name=profile_name,
        provider=provider if provider is not None else MockAgentProvider(latency=0.0),
        presentation=PresentationPolicy.SILENT,
        render_phase_outputs=render_phase_outputs,
        no_interactive=True,
    )


def _read_events(run_dir: Path) -> list[dict]:
    """Parse ``events.jsonl`` line-by-line. Returns [] if the file is
    missing (e.g. the run died before any event landed) — caller
    asserts non-empty explicitly."""
    events_path = run_dir / "events.jsonl"
    if not events_path.exists():
        return []
    out: list[dict] = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def _read_session(run_dir: Path) -> dict:
    meta_path = run_dir / "meta.json"
    if not meta_path.exists():
        return {}
    return json.loads(meta_path.read_text(encoding="utf-8"))


@pytest.fixture
def _silent_project(tmp_path: Path) -> Path:
    """Tmp project with a real git repo (needed by the dirty-tree
    detection in some profiles) and an output dir set up for the run."""
    from tests.conftest import init_git_repo
    project = tmp_path / "proj"
    init_git_repo(project)
    return project


# ── Test 1: done path ─────────────────────────────────────────────────


def test_run_project_pipeline_silent_done_path_emits_nothing(
    _silent_project: Path, tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Happy path — ``profile_name="task"`` (skips plan, goes straight
    to implement). SILENT must produce zero stdout/stderr while every
    structural side-effect (session.json on disk, events.jsonl with
    phase.start / phase.end / run.end) is preserved."""
    run_dir = tmp_path / "runs" / "silent_done"

    with patch("pipeline.project.session_run.load_plugin", return_value=PluginConfig()):
        result = run_project_pipeline(_silent_request(
            task="silent done smoke",
            project_dir=_silent_project,
            output_dir=run_dir,
        ))

    captured = capsys.readouterr()
    assert captured.out == "", (
        f"SILENT done-path leaked stdout ({len(captured.out)} chars): "
        f"{captured.out[:200]!r}"
    )
    assert captured.err == "", (
        f"SILENT done-path leaked stderr: {captured.err!r}"
    )

    # Structural side-effects: ProjectRunResult populated.
    assert result.session["status"] == "done", (
        f"expected status=done; got {result.session.get('status')!r}; "
        f"full session: {result.session!r}"
    )
    assert result.output_dir == run_dir
    assert result.run_id  # non-empty session_ts

    # Persisted session.json on disk.
    persisted = _read_session(run_dir)
    assert persisted, "meta.json must be written even under SILENT"
    assert persisted.get("status") == "done"

    # events.jsonl carries the canonical phase events. The CLI
    # transcript is gone under SILENT but the event spine is what
    # MCP / orcho-web / dashboards read. ADR 0046 stop #9: log_phase
    # writes to BOTH progress.log + events.jsonl on every phase
    # boundary regardless of presentation.
    events = _read_events(run_dir)
    assert events, (
        "events.jsonl must be non-empty under SILENT; "
        "this is the load-bearing observability guarantee"
    )
    kinds = {e.get("kind") for e in events}
    assert "phase.start" in kinds, (
        f"SILENT must still emit phase.start (ADR 0046 stop #9); "
        f"saw kinds={sorted(kinds)}"
    )
    assert "phase.end" in kinds, (
        f"SILENT must still emit phase.end; saw kinds={sorted(kinds)}"
    )
    assert "run.end" in kinds, (
        f"SILENT must still emit run.end; saw kinds={sorted(kinds)}"
    )


def test_silent_run_can_render_phase_output_previews(
    _silent_project: Path, tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Cross terminal dispatch uses this shape: child run shell stays
    SILENT, but parsed phase response blocks retain mono-run parity."""
    run_dir = tmp_path / "runs" / "silent_previews"

    with patch("pipeline.project.session_run.load_plugin", return_value=PluginConfig()):
        result = run_project_pipeline(_silent_request(
            task="silent preview smoke",
            project_dir=_silent_project,
            output_dir=run_dir,
            render_phase_outputs=True,
        ))

    captured = capsys.readouterr()
    assert result.session["status"] == "done"
    assert "Implementation" in captured.out
    assert "[IMPLEMENT]" not in captured.out
    assert "[DONE]" not in captured.out


# ── Test 2: handoff-pause path ────────────────────────────────────────


def test_run_project_pipeline_silent_handoff_pause_emits_nothing(
    _silent_project: Path, tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """``MockAgentProvider(validate_plan_reject_rounds=99)`` +
    advanced profile drives the validate_plan loop to exhaust its
    budget and trigger a phase handoff pause. Pre-ADR-0046:

      * ``apply_phase_handoff_pause`` ``warn(...)`` (site 10) leaks
        to stderr.
      * The HYPOTHESIS banner ``log_phase("START", ...)`` was
        previously gated alongside the print (Phase C r5 fix split
        them); pre-fix SILENT produced an unpaired ``phase.end``.

    Post-Phase-C-r5 both leaks are gone. This test pins the contract:
    zero stdout/stderr AND the handoff state lands structurally."""
    run_dir = tmp_path / "runs" / "silent_handoff"

    provider = MockAgentProvider(latency=0.0, validate_plan_reject_rounds=99)

    with (
        patch("pipeline.project.session_run.load_plugin", return_value=PluginConfig()),
        patch("core.io.git_helpers.has_uncommitted", return_value=True),
        # Hypothesis prelude is unrelated to the validate_plan loop;
        # skip it so the scripted rejection counter doesn't burn
        # rounds on hypothesis QA review.
        patch(
            "pipeline.project.profile_dispatch.maybe_run_hypothesis",
            return_value=(None, []),
        ),
    ):
        result = run_project_pipeline(_silent_request(
            task="silent handoff smoke",
            project_dir=_silent_project,
            output_dir=run_dir,
            profile_name="feature",
            provider=provider,
            max_rounds=1,
        ))

    captured = capsys.readouterr()
    assert captured.out == "", (
        f"SILENT handoff-pause leaked stdout: {captured.out[:200]!r}"
    )
    # Load-bearing: site 10 ``warn(...)`` would leak here without
    # the Phase C handoff.py gate; the r5 fix kept ``log_phase``
    # unconditional but moved the ``warn`` under the TERMINAL gate.
    assert captured.err == "", (
        f"SILENT handoff-pause leaked stderr (site 10 regression?): "
        f"{captured.err!r}"
    )

    # Structural handoff state on the session.
    assert result.session["status"] == "awaiting_phase_handoff", (
        f"expected handoff pause; got status="
        f"{result.session.get('status')!r}"
    )
    handoff = result.session.get("phase_handoff")
    assert isinstance(handoff, dict) and handoff, (
        f"phase_handoff payload missing; session={result.session!r}"
    )
    assert handoff.get("phase") == "validate_plan", (
        f"expected validate_plan handoff; got phase={handoff.get('phase')!r}"
    )

    # Persisted session.json mirrors the in-memory state.
    persisted = _read_session(run_dir)
    assert persisted.get("status") == "awaiting_phase_handoff"

    # phase.handoff_requested event landed in events.jsonl — the
    # structural signal that orcho-web / MCP / dashboards consume to
    # surface the pause to a human operator.
    events = _read_events(run_dir)
    kinds = [e.get("kind") for e in events]
    assert "phase.handoff_requested" in kinds, (
        f"phase.handoff_requested must fire under SILENT — it's the "
        f"structural signal replacing the suppressed warn(...). "
        f"Saw event kinds: {sorted(set(kinds))}"
    )


# ── Test 3: failure path ──────────────────────────────────────────────


def test_run_project_pipeline_silent_failure_path_emits_nothing(
    _silent_project: Path, tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Monkeypatch ``run_profile`` to raise ``RuntimeError``. The
    exception propagates through ``dispatch_via_v2_profile``'s
    ``except Exception as exc`` → ``run._record_phase_failure(exc, ...)``
    → re-raise. Under SILENT, ``_record_phase_failure`` skips the red
    FAILED block + first-line ``warn(...)`` (site 14).

    ``run_project_pipeline`` does NOT return on this path — it
    re-raises. Read assertions from persisted state, not the
    (non-existent) ``ProjectRunResult``."""
    run_dir = tmp_path / "runs" / "silent_failure"

    def _raise_run_profile(*args, **kwargs):
        raise RuntimeError("test failure boom")

    # ``run_profile`` is locally imported inside
    # ``dispatch_via_v2_profile`` (``from pipeline.runtime import
    # run_profile``), so patch the source module rather than the
    # consumer.
    monkeypatch.setattr(
        "pipeline.runtime.runner.run_profile",
        _raise_run_profile,
    )
    monkeypatch.setattr(
        "pipeline.runtime.run_profile",
        _raise_run_profile,
    )

    with (
        patch("pipeline.project.session_run.load_plugin", return_value=PluginConfig()),
        pytest.raises(RuntimeError, match="test failure boom"),
    ):
        run_project_pipeline(_silent_request(
            task="silent failure smoke",
            project_dir=_silent_project,
            output_dir=run_dir,
        ))

    captured = capsys.readouterr()
    assert captured.out == "", (
        f"SILENT failure-path leaked stdout (site 14 regression?): "
        f"{captured.out[:200]!r}"
    )
    assert captured.err == "", (
        f"SILENT failure-path leaked stderr (site 14 _record_phase_failure "
        f"first-line warn regression?): {captured.err!r}"
    )

    # Persisted session.json carries the structured failure block.
    persisted = _read_session(run_dir)
    assert persisted, "meta.json must be written even on failure under SILENT"
    assert persisted.get("status") == "failed", (
        f"expected status=failed; got {persisted.get('status')!r}"
    )
    failure = persisted.get("failure")
    assert isinstance(failure, dict) and failure
    assert failure.get("type") == "RuntimeError"
    assert "test failure boom" in failure.get("error", "")
    # ADR 0035 invariant — every non-done terminal status carries halt_reason.
    assert persisted.get("halt_reason", "").startswith("phase_failure:"), (
        f"halt_reason must start with phase_failure: for failed runs; "
        f"got {persisted.get('halt_reason')!r}"
    )

    # run.end event with status=failed landed in events.jsonl.
    # NB: ``status`` lives inside the event's ``payload`` dict, not at
    # the top level — see ``core.observability.events.Event``.
    events = _read_events(run_dir)
    end_events = [
        e for e in events
        if (
            e.get("kind") == "run.end"
            and (e.get("payload") or {}).get("status") == "failed"
        )
    ]
    assert end_events, (
        f"expected run.end event with payload.status=failed; "
        f"saw events: "
        f"{[(e.get('kind'), (e.get('payload') or {}).get('status')) for e in events]}"
    )
    # The error_type chip also lands in the payload so downstream
    # consumers (MCP, dashboards) can surface the exception class
    # without parsing free-text.
    assert end_events[0]["payload"].get("error_type") == "RuntimeError"


# ── Test 4: grep guard (cheap pin) ────────────────────────────────────


def test_silent_policy_threads_through_every_silent_path_module() -> None:
    """Literal-grep guard: every silent-path module contains at least
    one ``presentation is PresentationPolicy.TERMINAL`` or
    ``_presentation is PresentationPolicy.TERMINAL`` literal. Trips on
    accidental gate removal at refactor time without needing to re-run
    the slow integration tests above.

    The list of modules is the ADR 0046 Phase C pre-flight scan target;
    excludes ``pipeline/project/cli.py`` per ADR 0046 stop #11 —
    terminal-by-definition leaf.

    ADR 0042 setup-module split: the silent-path gates that lived in the
    monolithic ``app.py`` body moved into the focused setup modules
    (``profile_setup`` / ``run_setup`` / ``isolation_setup`` /
    ``state_setup``). ``app.py`` is now a thin coordinator that threads
    ``presentation`` into those helpers, so the gate literals are scanned
    in their new homes instead of in ``app.py``."""
    silent_path_modules = [
        "pipeline/project/run.py",
        "pipeline/project/profile_dispatch.py",
        "pipeline/project/handoff.py",
        "pipeline/project/bootstrap.py",
        "pipeline/project/profile_setup.py",
        "pipeline/project/run_setup.py",
        "pipeline/project/isolation_setup.py",
        "pipeline/project/state_setup.py",
    ]
    for module_path in silent_path_modules:
        source = Path(module_path).read_text(encoding="utf-8")
        # Substring-level check so multi-line gates (e.g. the
        # ``getattr(run, "_presentation", PresentationPolicy.TERMINAL)\n
        # is PresentationPolicy.TERMINAL`` shape inside
        # ``profile_dispatch.py``) still match. The unique fingerprint is
        # an identity-check against an enum constant — accept the
        # ``is TERMINAL`` chip/banner gate, the ``is not TERMINAL`` early
        # return (``run_setup.print_pipeline_header``), and the
        # ``is SILENT`` error-rendering gate (``isolation_setup``).
        assert (
            "is PresentationPolicy.TERMINAL" in source
            or "is not PresentationPolicy.TERMINAL" in source
            or "is PresentationPolicy.SILENT" in source
        ), (
            f"{module_path} does not contain a "
            f"`PresentationPolicy` identity-check gate — the "
            f"silent-path policy gate is missing. ADR 0046 Phase C "
            f"requires every silent-path module to consult the "
            f"presentation policy at least once."
        )


# ── Test 5: terminal-default regression ───────────────────────────────


def test_run_project_pipeline_terminal_default_preserves_transcript(
    _silent_project: Path, tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Default ``presentation=TERMINAL`` (the CLI / SDK back-compat
    path) must emit the legacy transcript shape byte-identical to the
    pre-ADR-0046 ``run_pipeline`` output. ADR 0046 stop #1 — any drift
    of the default away from TERMINAL trips here."""
    run_dir = tmp_path / "runs" / "terminal_default"

    with patch("pipeline.project.session_run.load_plugin", return_value=PluginConfig()):
        request = ProjectRunRequest(
            task="terminal default smoke",
            project_dir=str(_silent_project),
            output_dir=run_dir,
            max_rounds=1,
            profile_name="task",
            provider=MockAgentProvider(latency=0.0),
            # NB: ``presentation`` deliberately omitted — relying on
            # the default. If the default ever flips away from
            # TERMINAL, this test trips before any other.
        )
        # Sanity that the default is what we think it is.
        assert request.presentation is PresentationPolicy.TERMINAL, (
            f"default presentation must be TERMINAL; got {request.presentation!r}"
        )
        result = run_project_pipeline(request)

    captured = capsys.readouterr()
    assert result.session["status"] == "done"

    # Legacy transcript markers. ``IMPLEMENT`` banner fires from the
    # implement phase, ``DONE`` from finalize_with_terminal_output's
    # DONE block, ``Session:`` / ``Usage:`` are success chips on the
    # DONE block. The ``Run dir:`` chip fires from
    # ``_print_pipeline_header``. None of these survive under SILENT
    # (covered by test 1 above).
    for marker in ["IMPLEMENT", "DONE", "Session:", "Usage:", "Run dir:"]:
        assert marker in captured.out, (
            f"TERMINAL transcript must contain legacy marker {marker!r}; "
            f"got stdout: {captured.out[:500]!r}"
        )
