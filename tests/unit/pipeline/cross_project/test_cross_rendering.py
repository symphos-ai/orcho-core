"""ADR 0047 Phase B — guard the cross-project render helpers extracted
to :mod:`pipeline.cross_project.rendering`.

Three load-bearing invariants:

1. **`banner()` render/log split (D4).** ``banner(terminal=True)`` (the
   CLI/SDK default) prints the banner header AND calls
   :func:`core.observability.logging.log_phase`. ``banner(terminal=False)``
   suppresses the print surface BUT ``log_phase`` STILL FIRES — the
   ADR 0046 Phase C r5 lesson applied prospectively: gating the helper
   as a whole would suppress the structural ``phase.start`` /
   ``progress.log`` writes too, which violates ADR 0046 stop #9.

2. **Pure-stdout helpers (`success`, `warn`, `preview`) have no event
   side-effect.** They emit color-coded chips and nothing else.
   Mocking ``log_phase`` and asserting it was NOT called pins this.

3. **Re-export surface.** ``from pipeline.cross_project.rendering
   import C, banner, success, warn, preview, _render_cross_plan_preview``
   resolves. Non-CLI cross peers (``planning_loop``, future ``app.py``)
   target this module, not ``orchestrator.py``.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from unittest.mock import patch

import pytest

from core.io.ansi import C, get_color_enabled, set_color_enabled


@pytest.fixture(autouse=True)
def _restore_color_override() -> Iterator[None]:
    before = get_color_enabled()
    try:
        yield
    finally:
        set_color_enabled(before)


class _Stdout:
    """Minimal stdout double for color-policy tests."""

    def __init__(self, *, is_tty: bool) -> None:
        self._is_tty = is_tty
        self._chunks: list[str] = []

    def write(self, text: str) -> int:
        self._chunks.append(text)
        return len(text)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return self._is_tty

    def getvalue(self) -> str:
        return "".join(self._chunks)


# ── Test 1: banner() render/log split ─────────────────────────────────


def test_banner_terminal_true_prints_and_logs(
    capsys: pytest.CaptureFixture,
) -> None:
    """``banner(terminal=True)`` (default) is byte-identical to the
    pre-Phase-B behaviour: ``print`` + ``log_phase``."""
    from pipeline.cross_project.rendering import banner

    with patch(
        "pipeline.cross_project.rendering.log_phase",
    ) as log_phase_mock:
        banner("CROSS_PLAN", "smoke", C.MAGENTA)

    captured = capsys.readouterr()
    assert "[CROSS_PLAN]" in captured.out, (
        "TERMINAL banner must print the bracketed phase tag"
    )
    assert "smoke" in captured.out
    assert log_phase_mock.called, (
        "TERMINAL banner must call log_phase for events.jsonl + progress.log"
    )
    # phase + title forwarded; keyword-only phase_kind/attempt default.
    args, kwargs = log_phase_mock.call_args
    assert args[0] == "CROSS_PLAN"
    assert args[1] == "smoke"


def test_banner_terminal_false_suppresses_print_but_logs(
    capsys: pytest.CaptureFixture,
) -> None:
    """ADR 0047 D4 — the load-bearing split. ``terminal=False`` MUST
    skip the print half but ``log_phase`` MUST still fire. This is
    the contract that ADR 0046 Phase C r5 established for project; we
    apply it prospectively to cross.

    Pre-Phase-B this combination didn't exist — ``banner()`` always
    printed. The new keyword-only parameter is the silent-callers
    seam Phase E will consume via wrapped ``_DispatchPorts``."""
    from pipeline.cross_project.rendering import banner

    with patch(
        "pipeline.cross_project.rendering.log_phase",
    ) as log_phase_mock:
        banner("CROSS_PLAN", "silent smoke", C.MAGENTA, terminal=False)

    captured = capsys.readouterr()
    assert captured.out == "", (
        f"banner(terminal=False) leaked stdout: {captured.out!r}"
    )
    assert captured.err == ""

    # The load-bearing assertion: log_phase STILL fires under
    # terminal=False. ADR 0046 stop #9 — file + event sinks are never
    # gated by presentation. Mocking the whole banner() helper (the
    # ADR 0046 Phase C r5 antipattern) would have silently dropped
    # this call; the split-render-from-log discipline is what makes
    # cross SILENT in Phase E observability-equivalent to TERMINAL.
    assert log_phase_mock.called, (
        "banner(terminal=False) MUST still call log_phase — "
        "ADR 0046 stop #9 forbids gating log_phase under any "
        "presentation policy"
    )
    args, kwargs = log_phase_mock.call_args
    assert args[0] == "CROSS_PLAN"
    assert args[1] == "silent smoke"


def test_banner_threads_phase_kind_and_attempt() -> None:
    """``phase_kind`` and ``attempt`` keyword-only args reach
    ``log_phase``. Acceptance tests + dashboard consume these for
    grouping-by-attempt; they're load-bearing for the event spine."""
    from pipeline.cross_project.rendering import banner

    with patch(
        "pipeline.cross_project.rendering.log_phase",
    ) as log_phase_mock:
        banner(
            "CROSS_PLAN", "round 2", C.MAGENTA,
            phase_kind="PLAN", attempt=2, terminal=False,
        )

    args, kwargs = log_phase_mock.call_args
    assert kwargs.get("phase_kind") == "PLAN"
    assert kwargs.get("attempt") == 2


# ── Test 2: pure-stdout helpers (no event side-effect) ───────────────


def test_success_prints_only(capsys: pytest.CaptureFixture) -> None:
    """``success`` emits a green chip on stdout. No event side-effect
    — mocking log_phase confirms it's never reached."""
    from pipeline.cross_project import rendering
    from pipeline.cross_project.rendering import success

    with patch.object(rendering, "log_phase") as log_phase_mock:
        success("Run dir: /tmp/run")

    out = capsys.readouterr().out
    assert "✓" in out
    assert "Run dir: /tmp/run" in out
    assert not log_phase_mock.called, (
        "success() must not call log_phase — pure-stdout chip"
    )


def test_warn_prints_only(capsys: pytest.CaptureFixture) -> None:
    """``warn`` emits a yellow chip on stdout. No event side-effect."""
    from pipeline.cross_project import rendering
    from pipeline.cross_project.rendering import warn

    with patch.object(rendering, "log_phase") as log_phase_mock:
        warn("something looks off")

    out = capsys.readouterr().out
    assert "⚠" in out
    assert "something looks off" in out
    assert not log_phase_mock.called


def test_preview_prints_only_no_truncation_by_default(
    capsys: pytest.CaptureFixture,
) -> None:
    """``preview`` defaults to ``n=None`` (no truncation). Important:
    cross plan bodies + Codex review renders are high-value — an
    operator needs to see the full text, not a 400/800-char teaser."""
    from pipeline.cross_project import rendering
    from pipeline.cross_project.rendering import preview

    long_text = "x" * 10_000
    with patch.object(rendering, "log_phase") as log_phase_mock:
        preview("Cross-project plan", long_text, C.MAGENTA)

    out = capsys.readouterr().out
    assert "Cross-project plan:" in out
    assert len(long_text) <= len(out), (
        "preview() default must not truncate; the full body must reach stdout"
    )
    assert not log_phase_mock.called


def test_preview_respects_explicit_truncation(
    capsys: pytest.CaptureFixture,
) -> None:
    """Callers that intentionally want a teaser can pass ``n=N``."""
    from pipeline.cross_project.rendering import preview

    preview("teaser", "x" * 100, C.WHITE, n=10)
    out = capsys.readouterr().out
    assert "…" in out, (
        "preview(n=10) over a 100-char body must add the '…' trailer"
    )


# ── Test 2b: color policy ─────────────────────────────────────────────


def test_render_helpers_emit_plain_text_under_no_color_on_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pipeline.cross_project.rendering import banner, preview, success, warn

    stdout = _Stdout(is_tty=True)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setenv("NO_COLOR", "1")
    set_color_enabled(None)

    with patch("pipeline.cross_project.rendering.log_phase"):
        banner("CROSS_PLAN", "smoke", C.MAGENTA)
    success("ok")
    warn("careful")
    preview("label", "body", C.WHITE)

    out = stdout.getvalue()
    assert "\033[" not in out
    assert "[CROSS_PLAN] smoke" in out
    assert "✓ ok" in out
    assert "⚠ careful" in out
    assert "label:" in out


def test_render_helpers_emit_plain_text_under_override_false_on_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pipeline.cross_project.rendering import banner, preview, success, warn

    stdout = _Stdout(is_tty=True)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.delenv("NO_COLOR", raising=False)
    set_color_enabled(False)

    with patch("pipeline.cross_project.rendering.log_phase"):
        banner("CROSS_PLAN", "smoke", C.MAGENTA)
    success("ok")
    warn("careful")
    preview("label", "body", C.WHITE)

    assert "\033[" not in stdout.getvalue()


def test_render_helpers_emit_color_under_force_color(
    capsys: pytest.CaptureFixture,
) -> None:
    from pipeline.cross_project.rendering import banner, preview, success, warn

    set_color_enabled(True)

    with patch("pipeline.cross_project.rendering.log_phase"):
        banner("CROSS_PLAN", "smoke", C.MAGENTA)
    success("ok")
    warn("careful")
    preview("label", "body", C.WHITE)

    out = capsys.readouterr().out
    assert C.MAGENTA in out
    assert C.GREEN in out
    assert C.YELLOW in out
    assert C.WHITE in out
    assert C.RESET in out


# ── Test 3: re-export surface ─────────────────────────────────────────


def test_rendering_module_exports_canonical_surface() -> None:
    """All render helpers are importable from
    ``pipeline.cross_project.rendering``. Non-CLI cross peers consume
    them from this module after ADR 0047 Phase B."""
    from pipeline.cross_project.rendering import (
        C,
        _render_cross_plan_preview,
        banner,
        preview,
        success,
        warn,
    )

    # Basic shape checks — no accidental swap with other helpers.
    assert callable(banner)
    assert callable(success)
    assert callable(warn)
    assert callable(preview)
    assert callable(_render_cross_plan_preview)
    assert isinstance(C.GREEN, str)
    assert isinstance(C.MAGENTA, str)


def test_orchestrator_re_exports_only_banner_for_test_patch_surface() -> None:
    """``orchestrator.py`` re-exports only ``banner`` from ``rendering``,
    and only because a few tests reach it through the orchestrator
    namespace (``orchestrator.banner(...)``). Every other render helper
    (``C``, ``success``, ``warn``, ``preview``,
    ``_render_cross_plan_preview``) is consumed from ``rendering``
    directly — the back-compat re-export surface for those names was
    removed as internal ceremony."""
    from pipeline.cross_project import orchestrator, rendering

    assert orchestrator.banner is rendering.banner

    for name in ("C", "success", "warn", "preview",
                 "_render_cross_plan_preview"):
        assert not hasattr(orchestrator, name), (
            f"orchestrator.{name} should no longer be re-exported; "
            f"import it from pipeline.cross_project.rendering instead"
        )
