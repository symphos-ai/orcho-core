"""tests/acceptance/test_no_contract_disclosure.py — the no-contract arc.

One fact — "this run declared no verification contract" — is decided once by
:mod:`pipeline.project.verification_disclosure` and must reach every operator
surface. Unit tests pin each reader in isolation against a hand-built block;
this file proves the *producer → consumer* path end to end on a real
``--mock`` run, so a break anywhere in the chain (contract projection →
``session_run`` → ``init_run_session`` → ``init_session_with_atexit`` →
``meta.json`` / ``state.extras`` → the five readers) fails here.

Two runs of the same harness, same paths, same mock provider:

* **no contract** — a plain git repo with no plugin file. The durable block
  lands in ``meta.json``; the run header prints ``HEADER_VALUE`` where the
  gate matrix would have been; the DONE tail carries exactly one
  ``tail_line()`` *after* the ``[DONE]`` banner; ``orcho status`` renders
  ``status_line()`` in its Gates section; ``delivery_decision_state`` carries
  ``verification_contract_declared=False`` (even on its ``kind="none"``
  branch); and the readiness block actually reaches the wire — asserted on
  the prompt the mock ``final_acceptance`` agent was really handed
  (``last_prompt``), never on a state object assembled by the test.
* **contract declared** — the same harness with a plugin declaring a minimal
  verification block. Not one line of the fact appears on any surface, and
  the block records ``declared: True``.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from core.io.ansi import strip_ansi
from pipeline.project.verification_disclosure import (
    HEADER_VALUE,
    META_KEY,
    delivery_gate_line,
    readiness_block,
    status_line,
    tail_line,
)

# Pinned run id (same shape as test_summary_arc) so the run-dir name is
# deterministic and both scenarios can reuse byte-identical paths.
FIXED_RUN_ID = "20260503_000000"

_FIXED_GIT_ENV = {
    "GIT_AUTHOR_DATE": "2026-05-03T00:00:00 +0000",
    "GIT_COMMITTER_DATE": "2026-05-03T00:00:00 +0000",
}

#: The prompt part id ``_verification_readiness_part`` stamps on the
#: readiness block (``pipeline/prompts/builders.py``).
READINESS_PART_ID = "verification_readiness:final_acceptance"

#: First line of ``readiness_block()`` — the header the reviewer reads.
READINESS_HEADER = readiness_block().splitlines()[0]


# ── harness (mirrors tests/acceptance/test_summary_arc.py) ────────────────


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "test@orcho.invalid"],
        ["git", "config", "user.name", "Orcho Test"],
        ["git", "config", "commit.gpgsign", "false"],
    ):
        subprocess.run(cmd, cwd=path, check=True)
    (path / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


def _reset_run_globals() -> None:
    """Reset the module-level logging / event / transcript singletons."""
    import agents.stream as _stream
    import core.io.transcript as _transcript
    import core.observability.events as _events
    import core.observability.logging as _logging

    _logging._progress_log = None
    _stream._agent_log = None
    _events.clear_phase_context()
    _events.init_event_store(None)
    _transcript.reset_phase_header_continuity()


def _contract_plugin():
    """A plugin declaring the minimal verification contract.

    Same shape as the ``test_mode_defaults_when_work_mode_unset`` fixture in
    ``tests/unit/cli/test_verification_header.py``: one named env, one
    command, **no schedule** — so the contract is genuinely declared (the
    projection returns a real object) while the engine still has no scheduled
    gate to execute inside an acceptance run.
    """
    from pipeline.plugins import PluginConfig

    return PluginConfig(
        name="No Contract Disclosure",
        language="Python",
        verification_envs={"ci": {"image": "python:3.12"}},
        verification={"commands": {"lint": "ruff check ."}},
    )


def _run_once(
    *, project: Path, ws: Path, run_dir: Path, declare_contract: bool,
) -> dict[str, Any]:
    """Run the mock ``feature`` scenario once at fixed paths.

    ``declare_contract=False`` leaves ``load_plugin`` alone — the project is a
    plain git repo with no plugin file, which is the real-world no-contract
    state. ``True`` patches the same seam ``test_summary_arc`` patches with a
    plugin carrying a minimal verification block.

    Returns the captured stdout, the persisted ``meta.json``, the evidence
    bundle, and the ``PhaseAgentConfig`` whose mock agents recorded the
    prompts they were actually handed.
    """
    import pipeline.engine.delivery_branch as _delivery_branch
    from agents.runtimes import make_mock_phase_config, make_provider
    from core.observability.logging import apply_output_mode
    from pipeline.project_orchestrator import run_pipeline

    buf = io.StringIO()
    with patch.dict(os.environ, _FIXED_GIT_ENV):
        shutil.rmtree(project, ignore_errors=True)
        _init_git_repo(project)
        shutil.rmtree(ws, ignore_errors=True)
        run_dir.mkdir(parents=True)

        _reset_run_globals()
        apply_output_mode("live")

        phase_config = make_mock_phase_config()
        provider = make_provider(True, latency=0.0)
        plugin_patch: contextlib.AbstractContextManager[Any] = (
            patch("pipeline.project.session_run.load_plugin",
                  return_value=_contract_plugin())
            if declare_contract
            else contextlib.nullcontext()
        )
        with (
            contextlib.redirect_stdout(buf),
            plugin_patch,
            patch("core.io.git_helpers.has_uncommitted", return_value=True),
            patch("core.io.git_helpers.git_diff_stat", return_value="1 file changed"),
            # ADR 0119 legacy opt-out: commit onto the checkout, as
            # test_summary_arc / test_full_mock_flow do.
            patch.object(_delivery_branch, "normalize_branch_policy",
                         lambda _raw: "bypass"),
            patch.dict(os.environ, {
                "ORCHO_RUN_ID": FIXED_RUN_ID,
                "ORCHO_WORKSPACE": str(ws),
            }),
        ):
            run_pipeline(
                task="demo no-contract disclosure",
                project_dir=str(project),
                output_dir=run_dir,
                max_rounds=2,
                profile_name="feature",
                provider=provider,
                phase_config=phase_config,
            )

    def _json(name: str) -> dict[str, Any]:
        path = run_dir / name
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    return {
        "stdout": strip_ansi(buf.getvalue()),
        "meta": _json("meta.json"),
        "evidence": _json("evidence.json"),
        "phase_config": phase_config,
        "runs_dir": run_dir.parent,
    }


@pytest.fixture(autouse=True)
def _restore_output_mode():
    """Isolate the process-level output mode / echo across tests."""
    import agents as _agents
    from core.observability.logging import apply_output_mode, get_output_mode

    before = get_output_mode()
    try:
        yield
    finally:
        apply_output_mode(before)
        _reset_run_globals()
        _agents.set_stdout_echo(False)


@pytest.fixture(scope="module")
def disclosure_runs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Run the scenario twice — without and with a declared contract."""
    root = tmp_path_factory.mktemp("no_contract_disclosure")
    project = root / "proj"
    ws = root / "ws"
    run_dir = ws / "runs" / FIXED_RUN_ID

    absent = _run_once(
        project=project, ws=ws, run_dir=run_dir, declare_contract=False,
    )
    # The status / delivery consumers read the run dir, so snapshot their
    # projections before the second run wipes the workspace.
    absent["status_text"] = _format_status(absent["runs_dir"])
    absent["delivery"] = _delivery_state(absent["runs_dir"])

    declared = _run_once(
        project=project, ws=ws, run_dir=run_dir, declare_contract=True,
    )
    declared["status_text"] = _format_status(declared["runs_dir"])
    declared["delivery"] = _delivery_state(declared["runs_dir"])

    return {"absent": absent, "declared": declared}


def _format_status(runs_dir: Path) -> str:
    from cli._formatters import format_status
    from sdk.status import load_status

    return strip_ansi(format_status(load_status(FIXED_RUN_ID, runs_dir=runs_dir)))


def _delivery_state(runs_dir: Path):
    from sdk.run_control.delivery import delivery_decision_state

    return delivery_decision_state(FIXED_RUN_ID, runs_dir=runs_dir)


def _gate_report(run: dict[str, Any]) -> str:
    """Render ``orcho delivery gate`` for a finished run, as the CLI does."""
    from cli._delivery_cli import format_delivery_gate

    meta = run["meta"]
    return strip_ansi(format_delivery_gate(
        run["delivery"],
        gate_facts=meta.get("commit_delivery") or {},
        current_status=meta.get("status"),
    ))


def _final_acceptance_prompt(run: dict[str, Any]) -> str:
    """The prompt the mock ``final_acceptance`` agent was actually handed."""
    prompt = run["phase_config"].final_acceptance_agent.last_prompt
    assert prompt, "final_acceptance agent was never invoked"
    return prompt


# ── scenario 1: no contract → the fact on every surface ───────────────────


def test_meta_records_the_fact(disclosure_runs) -> None:
    """(a) The durable block is in ``meta.json`` — the one writer ran."""
    assert disclosure_runs["absent"]["meta"].get(META_KEY) == {"declared": False}


def test_run_header_discloses_in_place_of_the_gate_matrix(disclosure_runs) -> None:
    """(b) The header prints the fact exactly once."""
    out = disclosure_runs["absent"]["stdout"]
    assert out.count(HEADER_VALUE) == 1, "header disclosure missing or duplicated"


def test_done_tail_carries_exactly_one_fact_line_after_the_banner(
    disclosure_runs,
) -> None:
    """(c) One tail line, and it lands in the DONE tail, not earlier."""
    out = disclosure_runs["absent"]["stdout"]
    assert out.count(tail_line()) == 1, "tail disclosure missing or duplicated"

    lines = out.splitlines()
    banner_idx = next(
        (i for i, ln in enumerate(lines) if "[DONE]" in ln), None,
    )
    tail_idx = next(
        (i for i, ln in enumerate(lines) if tail_line() in ln), None,
    )
    assert banner_idx is not None, "run did not reach the [DONE] banner"
    assert tail_idx is not None
    assert banner_idx < tail_idx, (
        "the fact must be part of the DONE tail, not printed before the banner"
    )


def test_orcho_status_gates_section_discloses(disclosure_runs) -> None:
    """(d) ``orcho status`` names the fact under Gates."""
    text = disclosure_runs["absent"]["status_text"]
    assert status_line() in text
    assert "Gates:" in text, "the Gates section must exist to carry the fact"


def test_delivery_decision_state_publishes_the_fact(disclosure_runs) -> None:
    """(e) The SDK field is False — including on the ``none`` branch."""
    state = disclosure_runs["absent"]["delivery"]
    assert state.verification_contract_declared is False, (
        f"kind={state.kind!r} dropped the additive field"
    )


def test_delivery_gate_cli_renders_the_fact(disclosure_runs) -> None:
    """(e, cont.) The gate renderer turns that field into operator text."""
    from cli._delivery_cli import delivery_gate_to_json

    run = disclosure_runs["absent"]
    assert delivery_gate_line() in _gate_report(run)
    payload = delivery_gate_to_json(run["delivery"], {})
    assert payload["verification_contract_declared"] is False


def test_final_acceptance_wire_prompt_carries_the_readiness_block(
    disclosure_runs,
) -> None:
    """(f) The block reached the wire — asserted on the real prompt.

    ``last_prompt`` is what ``_MockCodex`` was handed, so this fails if the
    readiness text is computed but never composed into the final_acceptance
    prompt. No state object is assembled by the test.
    """
    prompt = _final_acceptance_prompt(disclosure_runs["absent"])
    assert "[final_acceptance]" in prompt, "not the closing-gate prompt"
    assert READINESS_HEADER in prompt
    assert "0 receipts" in prompt


def test_final_acceptance_prompt_render_names_the_readiness_part(
    disclosure_runs,
) -> None:
    """Corroboration from the durable side, when the engine records it.

    ``FinalAcceptanceAdapter`` deliberately carries no ``prompt_render``
    record (the closing gate does not route through ``_session_aware_invoke``
    — see ``pipeline/session_adapters.py``), so the evidence bundle has no
    final_acceptance entry today and the wire proof above is the authority.
    Should the closing gate ever join session-aware rendering, this asserts
    the readiness part is named there rather than silently omitted.
    """
    records = disclosure_runs["absent"]["evidence"].get("prompt_render") or []
    final_records = [r for r in records if r.get("phase") == "final_acceptance"]
    if not final_records:
        pytest.skip(
            "final_acceptance emits no prompt_render record by design; the "
            "wire proof is test_final_acceptance_wire_prompt_carries_the_"
            "readiness_block"
        )
    selected = [
        key
        for record in final_records
        for key in (record.get("selected_part_keys") or [])
    ]
    assert any(READINESS_PART_ID in str(key) for key in selected), (
        f"readiness part missing from the render record: {selected!r}"
    )


# ── scenario 2: contract declared → total silence ─────────────────────────


def test_declared_contract_run_is_a_real_completed_run(disclosure_runs) -> None:
    """Guard: the silence below must come from the contract, not a dead run.

    A halted / empty run would satisfy every "fact not in output" assertion
    vacuously, so pin that this run reached DONE and that the declared
    contract actually rendered its own header block.
    """
    run = disclosure_runs["declared"]
    assert run["meta"].get("status") == "done"
    assert "[DONE]" in run["stdout"]
    # The declared contract owns the header slot the fact would have taken.
    assert "Verification" in run["stdout"]


def test_declared_contract_records_declared_true(disclosure_runs) -> None:
    assert disclosure_runs["declared"]["meta"].get(META_KEY) == {"declared": True}


@pytest.mark.parametrize(
    "fact",
    [HEADER_VALUE, tail_line(), status_line(), delivery_gate_line()],
    ids=["header", "tail", "status", "delivery_gate"],
)
def test_declared_contract_prints_no_fact_anywhere(disclosure_runs, fact: str) -> None:
    """Not one disclosure line survives on any surface once a contract exists."""
    run = disclosure_runs["declared"]
    assert fact not in run["stdout"], "stdout leaked the no-contract fact"
    assert fact not in run["status_text"], "orcho status leaked the fact"
    assert fact not in _gate_report(run), "the delivery gate leaked the fact"


def test_declared_contract_keeps_the_fact_out_of_the_wire_prompt(
    disclosure_runs,
) -> None:
    prompt = _final_acceptance_prompt(disclosure_runs["declared"])
    assert "[final_acceptance]" in prompt
    assert "No verification contract declared" not in prompt


def test_declared_contract_publishes_declared_true_on_the_gate(
    disclosure_runs,
) -> None:
    assert (
        disclosure_runs["declared"]["delivery"].verification_contract_declared
        is True
    )
