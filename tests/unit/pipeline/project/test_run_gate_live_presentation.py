"""The pre-phase gate announcement seam respects the presentation policy."""
from __future__ import annotations

from types import SimpleNamespace

import pytest


def test_gate_targets_not_announced_without_terminal_presentation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pipeline.project.gate_repair as gate_repair
    import pipeline.project.run as run_mod
    import pipeline.project.verification_autorun as autorun_mod

    def _fake_autorun(
        run: object,
        phase: str,
        *,
        reason: str,
        delivery_plan: object | None = None,
        on_targets_resolved=None,
    ) -> SimpleNamespace:
        assert on_targets_resolved is not None
        on_targets_resolved(("python -m pytest -q",))
        return SimpleNamespace()

    monkeypatch.setattr(
        autorun_mod, "auto_run_required_receipts", _fake_autorun,
    )

    def _explode(*_a: object, **_k: object) -> None:
        raise AssertionError(
            "non-terminal presentation must not render gate output",
        )

    monkeypatch.setattr(gate_repair, "_render_gate_section_header", _explode)
    monkeypatch.setattr(gate_repair, "_render_gate_command_start", _explode)

    run = SimpleNamespace(_presentation=None)
    run_mod._auto_run_required_receipts_live(
        run, "implement", reason="pre-phase", hook_label="before implement",
    )
