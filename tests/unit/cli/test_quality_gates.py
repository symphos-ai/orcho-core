# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the read-only ``orcho quality-gates`` command.

Covers the three modes over ONE ledger projection + ONE shared renderer:

* ``--profile`` prints the three-axis matrix (when / policy / activation) with
  ``when`` resolved against the profile's phases (require -> its hook, warn ->
  pre-final under a final-phase profile);
* ``--paths`` resolves on-path activation to active / dormant / manual by
  identity through the same ``build_gate_ledger``;
* no ``--profile`` marks warn/off gates ``profile-dependent`` (the profile is
  never guessed) while required gates stay on their hook;
* every mode is strictly read-only — nothing is executed, nothing is written.
"""

from __future__ import annotations

import pipeline.plugins as plugins_mod
from cli import _quality_gates as qg
from cli.orcho import build_parser
from core.io.ansi import strip_ansi
from pipeline.plugins import PluginConfig
from pipeline.runtime import PhaseStep, Profile


def _reference_plugin() -> PluginConfig:
    """A plugin whose declared contract mirrors the repo's own multiagent shape.

    baseline (warn/cheap) is always; run-state/verification/cli-sdk (require) are
    path-gated; broad (require) is always; e2e (suggest) is operator/manual — the
    mix that exercises when / policy / activation together.
    """
    verification = {
        "default_env": "ci",
        "commands": {
            "env-provenance": {"env": "ci", "cheap": True, "run": "prov"},
            "lint": {"env": "ci", "cheap": True, "run": "ruff check ."},
            "run-state-unit": {"env": "ci", "run": "pytest run_state"},
            "verification-unit": {"env": "ci", "run": "pytest verification"},
            "cli-sdk-unit": {"env": "ci", "run": "pytest cli sdk"},
            "broad-non-e2e": {"env": "ci", "run": "pytest -m 'not e2e'"},
            "e2e": {"env": "ci", "run": "pytest -m e2e"},
        },
        "gate_sets": {
            "baseline": {
                "commands": ["env-provenance", "lint"],
                "default_policy": "warn",
                "default_cheap": True,
            },
            "run-state": {"commands": ["run-state-unit"], "default_policy": "require"},
            "verification": {
                "commands": ["verification-unit"], "default_policy": "require",
            },
            "cli-sdk": {"commands": ["cli-sdk-unit"], "default_policy": "require"},
            "broad": {"commands": ["broad-non-e2e"], "default_policy": "require"},
            "e2e": {"commands": ["e2e"], "default_policy": "suggest"},
        },
        "selection": [
            {"always": ["baseline", "broad"]},
            {"paths": ["pipeline/verification*.py"], "include": ["verification"]},
            {"paths": ["cli/**", "sdk/**"], "include": ["cli-sdk"]},
            {"paths": ["pipeline/run_state/**"], "include": ["run-state"]},
            {"operator": ["e2e"]},
        ],
        "schedule": [
            {"after_phase": "implement", "gate_sets": ["baseline"], "policy": "warn"},
            {
                "after_phase": "implement",
                "gate_sets": ["run-state", "verification", "cli-sdk"],
                "policy": "require",
            },
            {"after_phase": "implement", "gate_sets": ["broad"], "policy": "require"},
            {"manual_only": True, "gate_sets": ["e2e"], "policy": "suggest"},
        ],
    }
    return PluginConfig(
        work_mode="pro",
        verification_envs={"ci": {"image": "python:3.12"}},
        verification=verification,
    )


def _profiles() -> dict[str, Profile]:
    """Two fake profiles: one with a final delivery phase, one without."""
    with_final = Profile(
        name="withfinal", kind=None, variant=None, description="", internal=False,
        steps=(PhaseStep(phase="implement"), PhaseStep(phase="final_acceptance")),
    )
    no_final = Profile(
        name="nofinal", kind=None, variant=None, description="", internal=False,
        steps=(PhaseStep(phase="implement"),),
    )
    return {"withfinal": with_final, "nofinal": no_final}


def _install(monkeypatch, *, plugin: PluginConfig | None = None) -> None:
    """Point the command at our reference plugin + fake profile catalogue."""
    resolved = _reference_plugin() if plugin is None else plugin
    monkeypatch.setattr(plugins_mod, "load_plugin", lambda _project: resolved)
    monkeypatch.setattr(qg, "_load_profiles", _profiles)


def _run(monkeypatch, argv: list[str]) -> tuple[int, str, str]:
    """Parse ``argv`` through the real parser and invoke the routed command."""
    import io
    import sys

    _install(monkeypatch)
    parser = build_parser()
    args = parser.parse_args(argv)
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    rc = args.func(args)
    return rc, strip_ansi(out.getvalue()), strip_ansi(err.getvalue())


# ── parser wiring ──────────────────────────────────────────────────────────


def test_parser_routes_to_command_body() -> None:
    parser = build_parser()
    args = parser.parse_args(["quality-gates", "--profile", "withfinal"])
    assert args.func is qg.cmd_quality_gates
    assert args.profile == "withfinal"
    assert args.paths is None


# ── (a) --profile prints the three-axis matrix ─────────────────────────────


def test_profile_prints_three_axis_matrix(monkeypatch) -> None:
    rc, out, err = _run(monkeypatch, ["quality-gates", "--profile", "withfinal"])
    assert rc == 0, err
    # Column header names all three axes plus identity/run/kind.
    header = next(ln for ln in out.splitlines() if "activation" in ln)
    assert header.split() == ["gate", "when", "run", "policy", "kind", "activation"]
    # require gate reads its hook; warn gate defers to pre-final under a
    # profile that HAS a final phase; e2e is operator.
    ver_line = next(ln for ln in out.splitlines() if "verification-unit" in ln)
    assert "after_implement" in ver_line
    assert "require" in ver_line
    lint_line = next(ln for ln in out.splitlines() if ln.strip().startswith("lint"))
    assert "pre-final" in lint_line
    assert "warn" in lint_line
    e2e_line = next(ln for ln in out.splitlines() if ln.strip().startswith("e2e"))
    assert "operator" in e2e_line


def test_profile_without_final_phase_marks_warn_not_auto_run(monkeypatch) -> None:
    rc, out, err = _run(monkeypatch, ["quality-gates", "--profile", "nofinal"])
    assert rc == 0, err
    lint_line = next(ln for ln in out.splitlines() if ln.strip().startswith("lint"))
    assert "not auto-run" in lint_line
    # A required gate still reads its own timing hook regardless of the profile.
    ver_line = next(ln for ln in out.splitlines() if "verification-unit" in ln)
    assert "after_implement" in ver_line


def test_unknown_profile_errors_without_guessing(monkeypatch) -> None:
    rc, out, err = _run(monkeypatch, ["quality-gates", "--profile", "nope"])
    assert rc == 2
    assert "unknown profile" in err
    assert out.strip() == ""


# ── (b) --paths resolves on-path activation (active/dormant/manual) ─────────


def test_paths_resolve_active_vs_dormant(monkeypatch) -> None:
    rc, out, err = _run(
        monkeypatch,
        ["quality-gates", "--profile", "withfinal",
         "--paths", "pipeline/verification_ledger.py"],
    )
    assert rc == 0, err
    resolution = out.split("Resolution for given paths:", 1)
    assert len(resolution) == 2, out
    body = resolution[1]
    # The verification subsystem gate is on-path -> active; the other narrow
    # gates stay dormant; always gates active; the operator gate manual.
    assert _disposition(body, "verification-unit") == "active"
    assert _disposition(body, "run-state-unit") == "dormant"
    assert _disposition(body, "cli-sdk-unit") == "dormant"
    assert _disposition(body, "broad-non-e2e") == "active"
    assert _disposition(body, "lint") == "active"
    assert _disposition(body, "e2e") == "manual"


def test_non_subsystem_paths_leave_narrow_gates_dormant(monkeypatch) -> None:
    rc, out, err = _run(
        monkeypatch,
        ["quality-gates", "--profile", "withfinal", "--paths", "README.md"],
    )
    assert rc == 0, err
    body = out.split("Resolution for given paths:", 1)[1]
    for narrow in ("verification-unit", "run-state-unit", "cli-sdk-unit"):
        assert _disposition(body, narrow) == "dormant", narrow
    assert _disposition(body, "broad-non-e2e") == "active"


def test_no_paths_has_no_resolution_section(monkeypatch) -> None:
    rc, out, err = _run(monkeypatch, ["quality-gates", "--profile", "withfinal"])
    assert rc == 0, err
    assert "Resolution for given paths:" not in out


def _disposition(body: str, gate: str) -> str:
    # Resolution rows are ``  <gate padded>  <disposition>`` — width padding
    # guarantees at least the two-space column gap after the gate name.
    line = next(
        ln for ln in body.splitlines() if ln.strip().startswith(gate + " ")
    )
    return line.strip().split()[-1]


# ── (c) no --profile: warn/off profile-dependent, require on its hook ───────


def test_no_profile_marks_warn_profile_dependent(monkeypatch) -> None:
    rc, out, err = _run(monkeypatch, ["quality-gates"])
    assert rc == 0, err
    assert "no profile" in out  # the title says the profile was not guessed
    lint_line = next(ln for ln in out.splitlines() if ln.strip().startswith("lint"))
    assert "profile-dependent" in lint_line
    # require gate is unaffected — it still reads its own hook.
    ver_line = next(ln for ln in out.splitlines() if "verification-unit" in ln)
    assert "after_implement" in ver_line


def test_no_contract_reports_cleanly(monkeypatch) -> None:
    # A bare plugin declares no contract at all -> from_plugin returns None; the
    # command reports cleanly and exits 0 (absent-but-worked).
    rc, out, err = _run_with_plugin(monkeypatch, PluginConfig())
    assert rc == 0
    assert "no verification contract declared" in out


def _run_with_plugin(monkeypatch, plugin: PluginConfig) -> tuple[int, str, str]:
    import io
    import sys

    _install(monkeypatch, plugin=plugin)
    parser = build_parser()
    args = parser.parse_args(["quality-gates"])
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    rc = args.func(args)
    return rc, strip_ansi(out.getvalue()), strip_ansi(err.getvalue())


# ── (d) strictly read-only in every mode ────────────────────────────────────


def test_command_never_executes_or_writes(monkeypatch) -> None:
    # Fail loudly if the command reaches for any command-execution surface in
    # any mode. The ledger's identity resolve is pure path-matching (no
    # subprocess), so trapping process spawns is sufficient to prove read-only.
    import os
    import subprocess

    def _boom(*_a, **_k):  # pragma: no cover - only fires on a read-only breach
        raise AssertionError("quality-gates must not execute a command")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(os, "system", _boom)

    for argv in (
        ["quality-gates", "--profile", "withfinal"],
        ["quality-gates", "--profile", "withfinal", "--paths", "cli/x.py"],
        ["quality-gates"],
    ):
        rc, out, err = _run(monkeypatch, argv)
        assert rc == 0, err
        assert out.strip()
