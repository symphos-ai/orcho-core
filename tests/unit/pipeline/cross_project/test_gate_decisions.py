"""Tests for ``pipeline.cross_project.gate_decisions``."""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

from pipeline.cross_project.gate_decisions import (
    GateDecision,
    resolve_gate_decision,
)
from pipeline.runtime import (
    CrossGatePolicy,
    CrossGateRunPolicy,
    CrossGateSkipPolicy,
)


def _policy(run: CrossGateRunPolicy, *, enabled: bool = True) -> CrossGatePolicy:
    return CrossGatePolicy(
        enabled=enabled,
        run=run,
        on_skip=CrossGateSkipPolicy.BLOCK,
        mode=None,
    )


def _resolve(
    *,
    run: CrossGateRunPolicy,
    cli_overrides: dict | None = None,
    interactive_allowed: bool = True,
    stdin_is_tty: bool = True,
    stdout_is_tty: bool = True,
) -> GateDecision:
    return resolve_gate_decision(
        gate_name="contract_check",
        policy=_policy(run),
        cli_overrides=cli_overrides or {},
        interactive_allowed=interactive_allowed,
        stdin_is_tty=stdin_is_tty,
        stdout_is_tty=stdout_is_tty,
    )


@pytest.fixture
def fake_input() -> Iterator[list[str]]:
    """Yield a stub for ``builtins.input`` driven by a script."""
    script: list[str] = []
    def _next(_prompt: str) -> Any:
        if not script:
            raise EOFError("no more scripted answers")
        return script.pop(0)
    with patch("pipeline.cross_project.gate_decisions.input", _next):
        yield script


class TestResolveGateDecision:
    def test_always_returns_run(self) -> None:
        assert _resolve(run=CrossGateRunPolicy.ALWAYS) is GateDecision.RUN

    def test_auto_returns_run(self) -> None:
        assert _resolve(run=CrossGateRunPolicy.AUTO) is GateDecision.RUN

    def test_always_explicit_skip_override_wins(self) -> None:
        assert _resolve(
            run=CrossGateRunPolicy.ALWAYS,
            cli_overrides={"contract_check": "skip"},
        ) is GateDecision.SKIP

    def test_auto_explicit_skip_override_wins(self) -> None:
        assert _resolve(
            run=CrossGateRunPolicy.AUTO,
            cli_overrides={"contract_check": "skip"},
        ) is GateDecision.SKIP

    def test_never_returns_skip(self) -> None:
        # Upstream callers short-circuit NEVER, but the resolver still
        # gives a safe answer if it slips through.
        assert _resolve(run=CrossGateRunPolicy.NEVER) is GateDecision.SKIP

    def test_manual_confirm_explicit_run_skips_prompt(
        self, fake_input: list[str],
    ) -> None:
        decision = _resolve(
            run=CrossGateRunPolicy.MANUAL_CONFIRM,
            cli_overrides={"contract_check": "run"},
        )
        assert decision is GateDecision.RUN
        # Prompt was not invoked.
        assert fake_input == []

    def test_manual_confirm_explicit_skip_skips_prompt(
        self, fake_input: list[str],
    ) -> None:
        decision = _resolve(
            run=CrossGateRunPolicy.MANUAL_CONFIRM,
            cli_overrides={"contract_check": "skip"},
        )
        assert decision is GateDecision.SKIP
        assert fake_input == []

    def test_manual_confirm_tty_enter_runs(
        self, fake_input: list[str],
    ) -> None:
        fake_input.append("")
        assert _resolve(
            run=CrossGateRunPolicy.MANUAL_CONFIRM,
        ) is GateDecision.RUN

    def test_manual_confirm_tty_s_skips(
        self, fake_input: list[str],
    ) -> None:
        fake_input.append("s")
        assert _resolve(
            run=CrossGateRunPolicy.MANUAL_CONFIRM,
        ) is GateDecision.SKIP

    def test_manual_confirm_tty_a_aborts(
        self, fake_input: list[str],
    ) -> None:
        fake_input.append("a")
        assert _resolve(
            run=CrossGateRunPolicy.MANUAL_CONFIRM,
        ) is GateDecision.ABORT

    def test_manual_confirm_non_tty_pauses(
        self, fake_input: list[str],
    ) -> None:
        decision = _resolve(
            run=CrossGateRunPolicy.MANUAL_CONFIRM,
            stdin_is_tty=False,
        )
        assert decision is GateDecision.PAUSE
        assert fake_input == []

    def test_manual_confirm_no_interactive_pauses(
        self, fake_input: list[str],
    ) -> None:
        decision = _resolve(
            run=CrossGateRunPolicy.MANUAL_CONFIRM,
            interactive_allowed=False,
        )
        assert decision is GateDecision.PAUSE
        assert fake_input == []

    def test_manual_confirm_unknown_input_eventually_aborts(
        self, fake_input: list[str],
    ) -> None:
        # Three bad answers → abort. We append four so EOF can't be hit.
        fake_input.extend(["?", "huh", "what", "y"])
        assert _resolve(
            run=CrossGateRunPolicy.MANUAL_CONFIRM,
        ) is GateDecision.ABORT
