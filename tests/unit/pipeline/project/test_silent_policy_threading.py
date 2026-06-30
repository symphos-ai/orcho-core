"""ADR 0046 Phase C — focused unit tests pinning that the policy is wired
through the three load-bearing sites inside ``_PipelineRun``.

Phase F covers the end-to-end zero-stdout boundary contract through
``run_project_pipeline``. These tests sit one layer lower and lock the
per-site behaviour so a refactor that *thinks* it preserves the boundary
but quietly drops a gate trips here before Phase F's broader integration
tests.

Three sites are covered:
  1. ``_PipelineRun.finalize()`` branches on ``_presentation`` and calls
     exactly one of ``finalize_project_run`` (silent) /
     ``finalize_with_terminal_output`` (terminal).
  2. ``_PipelineRun._on_phase_start`` suppresses the phase banner under
     ``SILENT`` even when ``_dispatch_active=True``.
  3. ``_PipelineRun._record_phase_failure`` produces no stdout/stderr
     under ``SILENT`` while still mutating ``self.session`` and emitting
     the ``run.end`` event.

The tests build minimal stand-ins (duck-typed against the attributes /
methods actually touched) rather than constructing a full ``_PipelineRun``
dataclass — same pattern as ``tests/unit/pipeline/test_finalization_silent.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from core.io.retry import AgentAccessError
from pipeline.project.run import _PipelineRun
from pipeline.project.types import PresentationPolicy


class _FakeState:
    """Minimal PipelineState stand-in. ``_on_phase_start`` only needs an
    ``.extras`` dict to record the timer + current-phase entries.
    ``_on_phase_end`` additionally reads ``phase_log``, ``halt``,
    ``halt_reason``, ``dry_run`` (mock-plan-write fallback)."""

    def __init__(self) -> None:
        self.extras: dict[str, Any] = {}
        self.phase_log: dict[str, Any] = {}
        self.halt: bool = False
        self.halt_reason: str | None = None
        self.dry_run: bool = True  # disables the mock-plan-write fallback
        # ``followup_banner_suffix`` reads ``st.phase_config``; ``None``
        # short-circuits to empty-suffix (the non-followup path).
        self.phase_config: Any = None


def _make_run_stub(presentation: PresentationPolicy) -> Any:
    """Build a duck-typed stand-in carrying just the attributes the three
    sites under test touch. We deliberately avoid instantiating the real
    ``_PipelineRun`` dataclass here — its field shape is ~25 required
    args, and the methods under test only read a handful of them.
    """
    run = type("_RunStub", (), {})()
    run._presentation = presentation
    run._dispatch_active = True
    run.session = {}
    run.output_dir = None
    run._ckpt = None
    return run


# ── Site 16: finalize() branches on _presentation ─────────────────────


def test_pipeline_run_finalize_branches_on_silent() -> None:
    """SILENT routes to ``finalize_project_run``; TERMINAL routes to
    ``finalize_with_terminal_output``. Default is TERMINAL."""
    silent_run = _make_run_stub(PresentationPolicy.SILENT)
    with (
        patch("pipeline.project.finalization.finalize_project_run") as silent_fn,
        patch(
            "pipeline.project.finalization.finalize_with_terminal_output",
        ) as terminal_fn,
    ):
        result = _PipelineRun.finalize(silent_run)
        assert silent_fn.called, "SILENT must call finalize_project_run"
        assert not terminal_fn.called, (
            "SILENT must NOT call finalize_with_terminal_output"
        )
        assert result is silent_run.session

    terminal_run = _make_run_stub(PresentationPolicy.TERMINAL)
    with (
        patch("pipeline.project.finalization.finalize_project_run") as silent_fn,
        patch(
            "pipeline.project.finalization.finalize_with_terminal_output",
        ) as terminal_fn,
    ):
        result = _PipelineRun.finalize(terminal_run)
        assert terminal_fn.called, (
            "TERMINAL must call finalize_with_terminal_output"
        )
        assert not silent_fn.called, (
            "TERMINAL must NOT call finalize_project_run"
        )
        assert result is terminal_run.session


# ── Site 7: phase banner — stdout suppressed, log_phase preserved ─────


def test_phase_banner_silent_suppresses_stdout_but_logs_event(
    capsys: pytest.CaptureFixture,
) -> None:
    """ADR 0046 Phase C contract — split render from log:
      * SILENT must NOT print the banner header.
      * SILENT MUST still call ``log_phase(...)`` so ``events.jsonl``
        carries a matched ``phase.start`` event and ``progress.log``
        sees the START line. ADR 0046 stop #9 — ``log_phase`` is a
        file + event writer and never gated by presentation policy.

    The reviewer caught the previous wording of this test: gating the
    whole helper dropped the observability side-effects with the
    courtesy print, leaving SILENT runs with unpaired ``phase.end`` in
    the event spine. This test now pins the split."""
    run = _make_run_stub(PresentationPolicy.SILENT)
    state = _FakeState()

    # Patch the underlying ``log_phase`` (file + event sink), NOT the
    # whole ``emit_phase_banner`` helper. Under SILENT the helper still
    # runs; it just skips the ``print(render_phase_header(...))`` half.
    with patch(
        "pipeline.project.profile_dispatch.log_phase",
    ) as log_phase_mock:
        _PipelineRun._on_phase_start(run, "plan", state)

    captured = capsys.readouterr()
    assert captured.out == "", (
        f"SILENT _on_phase_start leaked stdout: {captured.out!r}"
    )
    assert captured.err == ""

    assert log_phase_mock.called, (
        "ADR 0046 stop #9: log_phase MUST fire under SILENT — it is the "
        "file + event sink that downstream consumers (events.jsonl, "
        "progress.log) read; gating it breaks observability parity."
    )
    # Sanity: the START log call carries the canonical phase header
    # + the lowercase phase_key for event-store context.
    args, kwargs = log_phase_mock.call_args
    assert args[0] == "PLAN", (
        f"log_phase called with unexpected header {args!r}"
    )
    assert kwargs.get("phase_key") == "plan"

    # Structural side-effects (timer + current-phase) unchanged.
    assert "_phase_t0_plan" in state.extras
    assert state.extras["_current_phase"] == "plan"


def test_phase_banner_terminal_prints_header_and_logs(
    capsys: pytest.CaptureFixture,
) -> None:
    """Sanity: TERMINAL prints the banner header AND fires log_phase.
    Guards the polarity of the ``terminal=`` parameter and the existing
    byte-identical CLI behaviour."""
    run = _make_run_stub(PresentationPolicy.TERMINAL)
    state = _FakeState()
    with patch(
        "pipeline.project.profile_dispatch.log_phase",
    ) as log_phase_mock:
        _PipelineRun._on_phase_start(run, "plan", state)

    captured = capsys.readouterr()
    assert captured.out != "", (
        "TERMINAL _on_phase_start must print the banner header"
    )
    assert log_phase_mock.called


def test_phase_log_end_silent_suppresses_stdout_but_logs_event(
    capsys: pytest.CaptureFixture,
) -> None:
    """Companion to the START test: ``_on_phase_end`` under SILENT must
    fire ``log_phase("END", ...)`` so each SILENT run produces matched
    START / END pairs in ``events.jsonl``. Without this, the silent
    boundary leaks an asymmetric event spine.

    Note: the helper also takes a ``print("↳ skipped: …")`` path when
    the phase was skipped; this test uses an empty ``phase_log`` so no
    skip line would render either way. The SILENT gate inside the
    helper is for the skip-print specifically; ``log_phase`` itself is
    unconditional under both presentations."""
    run = _make_run_stub(PresentationPolicy.SILENT)
    state = _FakeState()

    with patch(
        "pipeline.project.profile_dispatch.log_phase",
    ) as log_phase_mock:
        _PipelineRun._on_phase_end(run, "plan", state)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""

    assert log_phase_mock.called, (
        "ADR 0046 stop #9: log_phase('END', ...) MUST fire under SILENT"
    )
    args, kwargs = log_phase_mock.call_args
    # log_phase signature: (phase, title, status, outcome, **kwargs)
    assert args[0] == "PLAN"
    assert args[2] == "END"


# ── Site 14: _record_phase_failure silent path ────────────────────────


def test_record_phase_failure_silent_path(
    capsys: pytest.CaptureFixture,
) -> None:
    """Under SILENT, ``_record_phase_failure`` mutates ``session`` and
    emits ``run.end`` without printing the red FAILED block or the
    ``warn(...)`` first-line summary.

    The structured ``failure`` block + ``halt_reason`` + ``run.end``
    event are the signal for silent callers; the prints are CLI-only
    courtesy."""
    run = _make_run_stub(PresentationPolicy.SILENT)
    exc = RuntimeError("test failure boom")

    emitted: list[dict[str, Any]] = []

    def _capture_emit(kind: str, **fields: Any) -> None:
        emitted.append({"kind": kind, **fields})

    with patch("core.observability.events.emit", side_effect=_capture_emit):
        _PipelineRun._record_phase_failure(run, exc, fallback_phase="plan")

    # Stdout / stderr both empty under SILENT.
    captured = capsys.readouterr()
    assert captured.out == "", (
        f"SILENT _record_phase_failure leaked stdout: {captured.out!r}"
    )
    assert captured.err == "", (
        f"SILENT _record_phase_failure leaked stderr: {captured.err!r}"
    )

    # Structural session mutations unchanged.
    assert run.session["status"] == "failed"
    assert run.session["halt_reason"] == "phase_failure:RuntimeError"
    assert run.session["failure"]["type"] == "RuntimeError"
    assert run.session["failure"]["phase"] == "plan"
    assert "test failure boom" in run.session["failure"]["error"]

    # ``run.end`` event still emitted.
    end_events = [e for e in emitted if e["kind"] == "run.end"]
    assert len(end_events) == 1
    assert end_events[0]["status"] == "failed"
    assert end_events[0]["error_type"] == "RuntimeError"


def test_record_phase_failure_projects_provider_access_metadata() -> None:
    run = _make_run_stub(PresentationPolicy.SILENT)
    exc = AgentAccessError("subscription access disabled")

    emitted: list[dict[str, Any]] = []

    def _capture_emit(kind: str, **fields: Any) -> None:
        emitted.append({"kind": kind, **fields})

    with patch("core.observability.events.emit", side_effect=_capture_emit):
        _PipelineRun._record_phase_failure(run, exc, fallback_phase="plan")

    assert run.session["status"] == "failed"
    assert run.session["halt_reason"] == "phase_failure:AgentAccessError"
    assert run.session["failure"]["type"] == "AgentAccessError"
    assert run.session["failure"]["failure_kind"] == "provider_access"
    assert run.session["failure"]["recoverable"] is False
    assert (
        run.session["failure"]["recommended_action"]
        == "switch_runtime_or_restore_access"
    )

    [end_event] = [e for e in emitted if e["kind"] == "run.end"]
    assert end_event["error_type"] == "AgentAccessError"
    assert end_event["failure_kind"] == "provider_access"
    assert end_event["recoverable"] is False
    assert end_event["recommended_action"] == "switch_runtime_or_restore_access"


class _FakeAppConfig:
    """Controlled stand-in for AppConfig exposing only the two phase views the
    recovery projection reads."""

    def __init__(
        self,
        runtime_map: dict[str, str],
        model_map: dict[str, str],
    ) -> None:
        self._runtime_map = runtime_map
        self._model_map = model_map

    @property
    def phase_runtime_map(self) -> dict[str, str]:
        return self._runtime_map

    @property
    def phase_model_map(self) -> dict[str, str]:
        return self._model_map


def _patch_app_config(runtime_map: dict[str, str], model_map: dict[str, str]):
    """Patch the AppConfig.load used inside _record_phase_failure so the
    recovery candidates are deterministic in the unit test."""
    fake = _FakeAppConfig(runtime_map, model_map)
    return patch(
        "pipeline.project.run.config.AppConfig.load",
        return_value=fake,
    )


def test_record_phase_failure_projects_recovery_actions_with_candidates() -> None:
    """ADR 0101: durable recovery record carries failed phase/runtime/model
    plus retry+halt and one replace per configured alternative pair."""
    run = _make_run_stub(PresentationPolicy.SILENT)
    exc = AgentAccessError("subscription access disabled")
    emitted: list[dict[str, Any]] = []

    with (
        _patch_app_config(
            {"plan": "claude", "implement": "codex"},
            {"plan": "sonnet", "implement": "gpt5"},
        ),
        _patch_registry_names(["claude", "codex"]),
        patch(
            "core.observability.events.emit",
            side_effect=lambda kind, **f: emitted.append({"kind": kind, **f}),
        ),
    ):
        _PipelineRun._record_phase_failure(run, exc, fallback_phase="plan")

    failure = run.session["failure"]
    assert failure["failed_phase"] == "plan"
    assert failure["runtime"] == "claude"
    # ``_model_for_phase`` is absent on the duck-typed stub → defensive
    # fallback to the configured model map.
    assert failure["model"] == "sonnet"
    assert failure["recovery_actions"] == [
        {"action": "retry"},
        {"action": "halt"},
        {"action": "replace", "runtime": "codex", "model": "gpt5"},
    ]

    [end_event] = [e for e in emitted if e["kind"] == "run.end"]
    assert end_event["failed_phase"] == "plan"
    assert end_event["runtime"] == "claude"
    assert end_event["recovery_actions"][0] == {"action": "retry"}


def test_record_phase_failure_recovery_actions_retry_halt_only() -> None:
    """When the only configured pair is the failed one, no replace option is
    offered — just retry/halt."""
    run = _make_run_stub(PresentationPolicy.SILENT)
    exc = AgentAccessError("subscription access disabled")

    with (
        _patch_app_config(
            {"plan": "claude", "validate_plan": "claude"},
            {"plan": "sonnet", "validate_plan": "sonnet"},
        ),
        patch("core.observability.events.emit"),
    ):
        _PipelineRun._record_phase_failure(run, exc, fallback_phase="plan")

    assert run.session["failure"]["recovery_actions"] == [
        {"action": "retry"},
        {"action": "halt"},
    ]


def _patch_registry_names(names: list[str]):
    """Patch ``AgentRegistry.default()`` so the validation-only registry set the
    recovery projection consults is deterministic in the unit test."""
    from types import SimpleNamespace

    fake = SimpleNamespace(names=lambda: list(names))
    return patch("agents.registry.AgentRegistry.default", return_value=fake)


def test_record_phase_failure_prunes_unregistered_runtime_candidate() -> None:
    """Fix F3: a configured ``(runtime, model)`` pair whose runtime is NOT
    registered in the ``AgentRegistry`` must not surface as a ``replace`` option.

    The configured phase maps are the sole *source* of candidate pairs, but
    ``AgentRegistry.names()`` is a validation-only filter: an unregistered
    runtime is not executable, so a ``replace`` for it would later be rejected
    by ``persist_runtime_override`` — it must be pruned from the durable
    ``recovery_actions`` here, leaving only retry/halt.
    """
    run = _make_run_stub(PresentationPolicy.SILENT)
    exc = AgentAccessError("subscription access disabled")
    emitted: list[dict[str, Any]] = []

    with (
        _patch_app_config(
            {"plan": "claude", "implement": "codex"},
            {"plan": "sonnet", "implement": "gpt5"},
        ),
        # ``codex`` is configured for ``implement`` but NOT registered here.
        _patch_registry_names(["claude"]),
        patch(
            "core.observability.events.emit",
            side_effect=lambda kind, **f: emitted.append({"kind": kind, **f}),
        ),
    ):
        _PipelineRun._record_phase_failure(run, exc, fallback_phase="plan")

    failure = run.session["failure"]
    # The unregistered codex/gpt5 pair is pruned → only retry/halt remain.
    assert failure["recovery_actions"] == [
        {"action": "retry"},
        {"action": "halt"},
    ]
    # The failed phase/runtime/model are still projected (only the candidate
    # surface is filtered, not the rest of the record).
    assert failure["failed_phase"] == "plan"
    assert failure["runtime"] == "claude"

    [end_event] = [e for e in emitted if e["kind"] == "run.end"]
    assert end_event["recovery_actions"] == [
        {"action": "retry"},
        {"action": "halt"},
    ]


def test_record_phase_failure_keeps_registered_runtime_candidate() -> None:
    """The companion to the prune test: a configured pair whose runtime IS
    registered survives as a ``replace`` option."""
    run = _make_run_stub(PresentationPolicy.SILENT)
    exc = AgentAccessError("subscription access disabled")

    with (
        _patch_app_config(
            {"plan": "claude", "implement": "codex"},
            {"plan": "sonnet", "implement": "gpt5"},
        ),
        _patch_registry_names(["claude", "codex"]),
        patch("core.observability.events.emit"),
    ):
        _PipelineRun._record_phase_failure(run, exc, fallback_phase="plan")

    assert run.session["failure"]["recovery_actions"] == [
        {"action": "retry"},
        {"action": "halt"},
        {"action": "replace", "runtime": "codex", "model": "gpt5"},
    ]


def test_record_phase_failure_sanitizes_provider_access_error_text() -> None:
    """ADR 0101 sanitary boundary: raw provider init JSON must not appear in
    any operator-visible text field (failure['error'], run.end error)."""
    run = _make_run_stub(PresentationPolicy.SILENT)
    raw = (
        '{"type":"system","subtype":"init","cwd":"/repo/secret"}\n'
        "Your organization has disabled Claude subscription access for "
        "Claude Code. Use an Anthropic API key instead."
    )
    exc = AgentAccessError(raw, stderr=raw)
    emitted: list[dict[str, Any]] = []

    with (
        _patch_app_config({"plan": "claude"}, {"plan": "sonnet"}),
        patch(
            "core.observability.events.emit",
            side_effect=lambda kind, **f: emitted.append({"kind": kind, **f}),
        ),
    ):
        _PipelineRun._record_phase_failure(run, exc, fallback_phase="plan")

    failure_error = run.session["failure"]["error"]
    [end_event] = [e for e in emitted if e["kind"] == "run.end"]
    run_end_error = end_event["error"]

    for visible in (failure_error, run_end_error):
        assert '{"type":"system"' not in visible
        assert "/repo/secret" not in visible
        assert '"subtype":"init"' not in visible
    # The sanitized human-readable line is preserved.
    assert "disabled Claude subscription access" in failure_error


def test_record_phase_failure_non_access_leaves_error_unchanged() -> None:
    """Non-provider-access failures get empty failure_meta: no recovery fields,
    raw error text preserved verbatim."""
    run = _make_run_stub(PresentationPolicy.SILENT)
    exc = RuntimeError("boom {\"type\":\"system\"} detail")
    emitted: list[dict[str, Any]] = []

    with patch(
        "core.observability.events.emit",
        side_effect=lambda kind, **f: emitted.append({"kind": kind, **f}),
    ):
        _PipelineRun._record_phase_failure(run, exc, fallback_phase="plan")

    failure = run.session["failure"]
    assert "failure_kind" not in failure
    assert "recovery_actions" not in failure
    assert "failed_phase" not in failure
    # Raw text is untouched for non-access exceptions.
    assert failure["error"] == 'boom {"type":"system"} detail'
    [end_event] = [e for e in emitted if e["kind"] == "run.end"]
    assert "failure_kind" not in end_event
    assert "recovery_actions" not in end_event
