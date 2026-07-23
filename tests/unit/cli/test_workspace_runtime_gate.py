"""Unit tests for cli._workspace_runtime_gate — the `workspace init`
runtime availability gate (zero-runtime stop + config-switch offer)."""
from __future__ import annotations

import io

import pytest

from cli._workspace_runtime_gate import (
    RuntimeGateDecision,
    workspace_runtime_gate,
)
from sdk.errors import WorkspaceInitError


@pytest.fixture(autouse=True)
def _planned_runtimes(monkeypatch):
    """Pin the configured phase map so tests don't depend on config layers."""
    monkeypatch.setattr(
        "cli._workspace_runtime_gate.planned_phase_runtimes",
        lambda root: {"plan": "claude", "review_changes": "codex"},
    )


def _which(*installed: str):
    return lambda cmd: f"/usr/bin/{cmd}" if cmd in installed else None


def _tty_stdin(text: str) -> io.StringIO:
    stdin = io.StringIO(text)
    stdin.isatty = lambda: True  # type: ignore[method-assign]
    return stdin


def test_zero_runtimes_raises_with_install_hint(monkeypatch) -> None:
    monkeypatch.setattr("sdk.runtimes.shutil.which", _which())

    with pytest.raises(WorkspaceInitError) as exc:
        workspace_runtime_gate(
            "/g", no_interactive=False, dry_run=False, force=False,
        )

    message = str(exc.value)
    assert "no CLI agent runtime found on PATH" in message
    assert "--force" in message


@pytest.mark.parametrize("force,dry_run", [(True, False), (False, True)])
def test_zero_runtimes_force_or_dry_run_passes(
    monkeypatch, force: bool, dry_run: bool,
) -> None:
    monkeypatch.setattr("sdk.runtimes.shutil.which", _which())

    decision = workspace_runtime_gate(
        "/g", no_interactive=False, dry_run=dry_run, force=force,
    )

    assert decision == RuntimeGateDecision(runtime_override=None)


def test_all_installed_no_prompt(monkeypatch) -> None:
    monkeypatch.setattr(
        "sdk.runtimes.shutil.which", _which("claude", "codex", "gemini"),
    )
    stdin = _tty_stdin("y\n")

    decision = workspace_runtime_gate(
        "/g", no_interactive=False, dry_run=False, force=False,
        stdin=stdin, stdout=io.StringIO(),
    )

    assert decision.runtime_override is None
    assert stdin.tell() == 0, "nothing may be read when nothing is missing"


def test_partial_gap_yes_returns_fallback_override(monkeypatch) -> None:
    monkeypatch.setattr("sdk.runtimes.shutil.which", _which("claude"))
    stdout = io.StringIO()

    decision = workspace_runtime_gate(
        "/g", no_interactive=False, dry_run=False, force=False,
        stdin=_tty_stdin("y\n"), stdout=stdout,
    )

    assert decision.runtime_override == "claude"
    output = stdout.getvalue()
    assert "'codex'" in output
    assert "Switch those phases to 'claude'" in output


def test_partial_gap_default_answer_is_yes(monkeypatch) -> None:
    monkeypatch.setattr("sdk.runtimes.shutil.which", _which("claude"))

    decision = workspace_runtime_gate(
        "/g", no_interactive=False, dry_run=False, force=False,
        stdin=_tty_stdin("\n"), stdout=io.StringIO(),
    )

    assert decision.runtime_override == "claude"


def test_partial_gap_no_keeps_config(monkeypatch) -> None:
    monkeypatch.setattr("sdk.runtimes.shutil.which", _which("claude"))
    stdout = io.StringIO()

    decision = workspace_runtime_gate(
        "/g", no_interactive=False, dry_run=False, force=False,
        stdin=_tty_stdin("n\n"), stdout=stdout,
    )

    assert decision.runtime_override is None
    assert "Keeping the configured runtimes" in stdout.getvalue()


def test_partial_gap_eof_treated_as_no_switch(monkeypatch) -> None:
    monkeypatch.setattr("sdk.runtimes.shutil.which", _which("claude"))

    decision = workspace_runtime_gate(
        "/g", no_interactive=False, dry_run=False, force=False,
        stdin=_tty_stdin(""), stdout=io.StringIO(),
    )

    assert decision.runtime_override is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"no_interactive": True, "dry_run": False},
        {"no_interactive": False, "dry_run": True},
    ],
)
def test_partial_gap_never_prompts_non_interactively(
    monkeypatch, kwargs: dict,
) -> None:
    monkeypatch.setattr("sdk.runtimes.shutil.which", _which("claude"))
    stdin = _tty_stdin("y\n")

    decision = workspace_runtime_gate(
        "/g", force=False, stdin=stdin, stdout=io.StringIO(), **kwargs,
    )

    assert decision.runtime_override is None
    assert stdin.tell() == 0


def test_partial_gap_no_tty_no_prompt(monkeypatch) -> None:
    monkeypatch.setattr("sdk.runtimes.shutil.which", _which("claude"))
    stdin = io.StringIO("y\n")  # isatty() → False

    decision = workspace_runtime_gate(
        "/g", no_interactive=False, dry_run=False, force=False,
        stdin=stdin, stdout=io.StringIO(),
    )

    assert decision.runtime_override is None
    assert stdin.tell() == 0
