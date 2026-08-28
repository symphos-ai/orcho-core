"""Unit tests for the gate-progress UX (``pipeline/project/gate_repair.py``).

A gate hook runs the declared checks (tests, linters) as blocking subprocesses
that can take minutes. These renderers surface the gate so the terminal never
looks hung: a VERIFICATION GATE header, a ``▶ running…`` line before the
blocking call, and a ``✓/✗`` result after. Everything is gated on TERMINAL
presentation — sub-pipelines / SILENT stay silent.
"""
from __future__ import annotations

import contextlib
import io
from types import SimpleNamespace

import pipeline.verification_command as vc
from core.io.ansi import strip_ansi
from pipeline.project import gate_repair
from pipeline.project.types import PresentationPolicy


def _cap(fn) -> list[str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return [strip_ansi(line) for line in buf.getvalue().splitlines()]


# ── duration formatting ──────────────────────────────────────────────────


def test_fmt_gate_duration_minutes_seconds_and_unparseable() -> None:
    assert gate_repair._fmt_gate_duration(134.2) == "2m14s"
    assert gate_repair._fmt_gate_duration(8.1) == "8s"
    assert gate_repair._fmt_gate_duration(0) == "0s"
    assert gate_repair._fmt_gate_duration(None) == ""
    assert gate_repair._fmt_gate_duration("nope") == ""


# ── terminal gating ──────────────────────────────────────────────────────


def test_gate_progress_on_terminal_only() -> None:
    assert gate_repair._gate_progress_on(
        SimpleNamespace(_presentation=PresentationPolicy.TERMINAL),
    )
    # Any non-TERMINAL presentation (SILENT sub-pipeline) → off.
    assert not gate_repair._gate_progress_on(
        SimpleNamespace(_presentation=object()),
    )
    # Missing attribute (duck-typed stub) → off, never raises.
    assert not gate_repair._gate_progress_on(SimpleNamespace())


# ── header + per-command rendering ───────────────────────────────────────


def test_section_header_names_the_gate_and_warns_of_duration() -> None:
    lines = _cap(
        lambda: gate_repair._render_gate_section_header(
            2, hook="after_phase", phase="implement",
        ),
    )
    joined = "\n".join(lines)
    assert "🔎  VERIFICATION GATE · after implement — running 2 checks" in joined
    assert "tests can take a few minutes" in joined
    # framed by a rule top and bottom.
    assert lines[1] == "═" * 68
    assert lines[-1] == "═" * 68


def test_section_header_singular_check() -> None:
    lines = _cap(
        lambda: gate_repair._render_gate_section_header(
            1, hook="before_phase", phase="final_acceptance",
        ),
    )
    joined = "\n".join(lines)
    assert "· before final_acceptance — running 1 check" in joined


def test_command_start_line_announces_running() -> None:
    lines = _cap(lambda: gate_repair._render_gate_command_start("broad-non-e2e"))
    assert lines == ["   ▶ broad-non-e2e   running…"]


def _classified(status: str, *, reason: str = "", kind: str | None = None):
    """Minimal stand-in for ``ReceiptClassification`` (status + reason only)."""
    return SimpleNamespace(status=status, failure_kind=kind, reason=reason)


def test_command_result_pass_shows_duration() -> None:
    lines = _cap(
        lambda: gate_repair._render_gate_command_result(
            "broad-non-e2e",
            {"exit_code": 0, "assertions": [], "detail": "", "duration_s": 134.0},
            _classified("present"),
        ),
    )
    assert lines == ["   ✓ broad-non-e2e   passed  (2m14s)"]


def test_command_result_fail_shows_duration() -> None:
    lines = _cap(
        lambda: gate_repair._render_gate_command_result(
            "unit", {"exit_code": 1, "detail": "2 failed", "duration_s": 8.0},
            _classified("failed", kind="test_failure", reason="command exited 1"),
        ),
    )
    assert lines == ["   ✗ unit   failed  (8s)"]


def test_command_result_unverifiable_does_not_claim_the_tests_passed() -> None:
    """An exit-0 command whose proof is unusable is neither ✓ nor ✗.

    Reporting ``✓ passed`` and then parking the run on a REJECTED handoff
    forces the operator to reverse-engineer which of the two happened.
    """
    lines = _cap(
        lambda: gate_repair._render_gate_command_result(
            "quant_tests",
            {"exit_code": 0, "assertions": [], "detail": "", "duration_s": 25.7},
            _classified(
                "unverifiable",
                kind="unverifiable",
                reason="usable_subject_identity_unavailable",
            ),
        ),
    )
    assert lines == [
        "   ⚠ quant_tests   unverifiable  (26s)",
        "     command exited 0; its verification proof is unverifiable "
        "(usable_subject_identity_unavailable)",
    ]


# ── wiring: _run_and_classify_gate renders around the blocking call ──────


def _fake_run(*, terminal: bool) -> SimpleNamespace:
    presentation = PresentationPolicy.TERMINAL if terminal else object()
    return SimpleNamespace(_presentation=presentation)


def _stub_gate_execution(monkeypatch, *, status: str = "present") -> None:
    monkeypatch.setattr(
        vc, "run_command",
        lambda *a, **k: {
            "exit_code": 0, "assertions": [], "detail": "", "duration_s": 12.0,
        },
    )
    monkeypatch.setattr(gate_repair, "_placeholders", lambda run: None)
    monkeypatch.setattr(
        gate_repair, "_persist_gate_receipt", lambda run, entry, receipt: None,
    )
    monkeypatch.setattr(
        gate_repair, "_classify_gate_receipt",
        lambda receipt, ctx=None: _classified(status),
    )


def test_run_and_classify_gate_terminal_renders_start_and_result(
    monkeypatch,
) -> None:
    _stub_gate_execution(monkeypatch)

    lines = _cap(lambda: gate_repair._run_and_classify_gate(
        _fake_run(terminal=True),
        SimpleNamespace(commands={"test": {}}),
        SimpleNamespace(command="test"),
    ))

    assert lines == [
        "   ▶ test   running…",
        "   ✓ test   passed  (12s)",
    ]


def test_run_and_classify_gate_result_line_follows_the_classification(
    monkeypatch,
) -> None:
    """The result line is rendered from the classification, not the exit code.

    Same exit-0 receipt as the passing case; only the classification differs,
    and the operator must see that difference at the moment it is decided.
    """
    _stub_gate_execution(monkeypatch, status="stale")

    lines = _cap(lambda: gate_repair._run_and_classify_gate(
        _fake_run(terminal=True),
        SimpleNamespace(commands={"test": {}}),
        SimpleNamespace(command="test"),
    ))

    assert lines == [
        "   ▶ test   running…",
        "   ⚠ test   stale  (12s)",
        "     command exited 0; its verification proof is stale",
    ]


def test_run_and_classify_gate_silent_run_prints_nothing(monkeypatch) -> None:
    _stub_gate_execution(monkeypatch)

    lines = _cap(lambda: gate_repair._run_and_classify_gate(
        _fake_run(terminal=False),
        SimpleNamespace(commands={"test": {}}),
        SimpleNamespace(command="test"),
    ))

    assert lines == []
