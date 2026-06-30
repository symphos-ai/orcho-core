"""``correction_triage`` phase handler + session adapter (ADR 0085, T1).

Covers the read-only triage handler: structured phase_log record,
invalid-kind normalization, the context-less fail-fast guard, the dry-run
path, the MockAgentProvider triage branch, and the session adapter that
promotes the record into ``session['phases']['correction_triage']``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agents.runtimes._strategy import MockAgentProvider
from pipeline.phases.builtin.handlers.correction_triage import (
    _VALID_KINDS,
    _extract_json_object,
    _normalize_triage,
    _phase_correction_triage,
)
from pipeline.plugins import PluginConfig
from pipeline.runtime import PipelineState
from pipeline.session_adapters import CorrectionTriageAdapter


class _StubAgent:
    """Minimal read-only agent: returns a canned response from ``invoke``."""

    runtime = "codex"
    model = "gpt-test"

    def __init__(self, response: str) -> None:
        self._response = response
        self.session_id = "stub-sid"
        self._last_resumed_session_id = None
        self._last_followup_parent_session_id = None
        self.last_prompt = ""
        self.last_tokens_in = None
        self.last_tokens_out = None
        self.last_tokens_total = None
        self.last_tokens_in_cache_read = None
        self.last_cost_usd = None

    def invoke(
        self,
        prompt: str,
        cwd: str,
        *,
        mutates_artifacts: bool = False,
        continue_session: bool = False,
        attachments: tuple = (),
    ) -> str:
        self.last_prompt = prompt
        return self._response


def _state(
    *,
    output_dir: Path | None,
    agent: _StubAgent | None = None,
    dry_run: bool = False,
    extras: dict | None = None,
) -> PipelineState:
    st = PipelineState(
        task="resolve the recorded release blockers",
        project_dir="/checkout",
        plugin=PluginConfig(),
        extras=extras or {},
    )
    st.output_dir = output_dir
    st.dry_run = dry_run
    if agent is not None:
        st.phase_config = SimpleNamespace(review_changes_agent=agent)
    return st


def _write_context(output_dir: Path, body: str = "## blocker\n\nmissing test") -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "correction_context.md").write_text(body, encoding="utf-8")


# ── handler: structured record ─────────────────────────────────────────


def test_handler_writes_structured_record(tmp_path: Path) -> None:
    _write_context(tmp_path)
    agent = _StubAgent(
        '{"kind": "code_fix", "summary": "narrow fix", '
        '"allowed_scope": ["a.py"], "required_checks": ["pytest -q"], '
        '"blockers": []}'
    )
    state = _phase_correction_triage(_state(output_dir=tmp_path, agent=agent))

    rec = state.phase_log["correction_triage"]
    assert rec["kind"] == "code_fix"
    assert rec["summary"] == "narrow fix"
    assert rec["allowed_scope"] == ["a.py"]
    assert rec["required_checks"] == ["pytest -q"]
    assert rec["blockers"] == []
    assert not state.halt
    # The composed prompt carries the marker the mock keys on.
    assert "[correction_triage]" in agent.last_prompt


def test_handler_records_session_observability_for_metrics(tmp_path: Path) -> None:
    """Correction triage must not be invisible in DONE usage by phase.

    The phase uses a reviewer agent, so provider usage has to flow through
    the same session-aware invoke boundary as review/repair. A regression
    here prints ``correction_triage tokens=0`` despite a real model call.
    """
    from core.observability.metrics import MetricsCollector
    from pipeline.project.run import _PipelineRun

    _write_context(tmp_path)
    agent = _StubAgent(
        '{"kind": "code_fix", "summary": "narrow fix", '
        '"allowed_scope": [], "required_checks": [], "blockers": []}'
    )
    agent.last_tokens_in = 123
    agent.last_tokens_out = 45
    agent.last_tokens_total = 168
    state = _phase_correction_triage(_state(output_dir=tmp_path, agent=agent))

    rec = state.phase_log["correction_triage"]
    assert "prompt_render" in rec
    assert "context_growth" in rec
    assert agent.last_invocation_outcome.tokens_in == 123
    assert agent.last_invocation_outcome.tokens_out == 45

    metrics = MetricsCollector(default_model="gpt-test")
    fake_run = SimpleNamespace(
        _agent_for_phase=lambda name: agent,
        _model_for_phase=lambda name: "gpt-test",
        _metrics=metrics,
    )
    _PipelineRun._fsm_metrics(fake_run, "correction_triage", state)
    phase = metrics.phases[-1]
    assert phase.phase == "correction_triage"
    assert phase.tokens_in == 123
    assert phase.tokens_out == 45
    assert phase.total_tokens == 168
    assert phase.tokens_exact is True


def test_invalid_kind_normalizes_to_blocked(tmp_path: Path) -> None:
    _write_context(tmp_path)
    agent = _StubAgent(
        '{"kind": "totally_unknown", "summary": "weird", '
        '"allowed_scope": [], "required_checks": [], "blockers": []}'
    )
    state = _phase_correction_triage(_state(output_dir=tmp_path, agent=agent))

    rec = state.phase_log["correction_triage"]
    assert rec["kind"] == "blocked"
    assert rec["blockers"], "blocked kind must carry an explanatory blocker"
    assert any("totally_unknown" in w for w in rec["parse_warnings"])
    # Stage 1: a normalized-to-blocked verdict halts before implement.
    assert state.halt
    assert state.halt_reason == "correction_triage_blocked"
    assert rec["halted"] is True
    assert rec["reason"] == "correction_triage_blocked"


def test_unparseable_response_normalizes_to_blocked(tmp_path: Path) -> None:
    _write_context(tmp_path)
    agent = _StubAgent("not json at all, sorry")
    state = _phase_correction_triage(_state(output_dir=tmp_path, agent=agent))

    rec = state.phase_log["correction_triage"]
    assert rec["kind"] == "blocked"
    assert rec["blockers"]
    assert "triage_unparseable" in rec["parse_warnings"]
    # Stage 1: an unparseable triage must not burn tokens on implement.
    assert state.halt
    assert state.halt_reason == "correction_triage_blocked"
    assert rec["halted"] is True


def test_blocked_verdict_halts_before_implement(tmp_path: Path) -> None:
    # An explicit ``blocked`` verdict from the model halts the run in triage
    # with the blocked halt reason and retains the named blockers.
    _write_context(tmp_path)
    agent = _StubAgent(
        '{"kind": "blocked", "summary": "no safe path", '
        '"allowed_scope": [], "required_checks": [], '
        '"blockers": ["upstream contract missing"]}'
    )
    state = _phase_correction_triage(_state(output_dir=tmp_path, agent=agent))

    assert state.halt
    assert state.halt_reason == "correction_triage_blocked"
    rec = state.phase_log["correction_triage"]
    assert rec["kind"] == "blocked"
    assert rec["halted"] is True
    assert rec["reason"] == "correction_triage_blocked"
    assert rec["blockers"] == ["upstream contract missing"]
    # This unit harness builds PipelineState directly (no checkpoint), so the
    # state.halt assertion above is the sufficient guarantee that triage stops
    # the run before any completed-checkpoint is written for it.


# ── handler: fail-fast ─────────────────────────────────────────────────


def test_fail_fast_without_context(tmp_path: Path) -> None:
    # No correction_context.md → fail-fast halt.
    agent = _StubAgent('{"kind": "code_fix", "summary": "x"}')
    state = _phase_correction_triage(_state(output_dir=tmp_path, agent=agent))

    assert state.halt
    assert state.halt_reason == "correction_triage_missing_context"
    rec = state.phase_log["correction_triage"]
    assert rec["kind"] == "blocked"
    assert rec["halted"] is True
    assert agent.last_prompt == "", "agent must not be invoked on fail-fast"


def test_plan_source_lineage_does_not_bypass_fail_fast(tmp_path: Path) -> None:
    # ``plan_source_run_id`` (--from-run-plan) carries no rejection blockers
    # and must not stand in for correction_context.md.
    agent = _StubAgent('{"kind": "gate_rerun", "summary": "stale blockers"}')
    state = _phase_correction_triage(
        _state(
            output_dir=tmp_path,
            agent=agent,
            extras={"plan_source_run_id": "20260101_000000"},
        )
    )
    assert state.halt
    assert state.halt_reason == "correction_triage_missing_context"
    assert state.phase_log["correction_triage"]["kind"] == "blocked"
    assert agent.last_prompt == "", "agent must not be invoked on fail-fast"


def test_empty_context_file_fails_fast(tmp_path: Path) -> None:
    # A present-but-empty correction_context.md is treated as missing.
    _write_context(tmp_path, body="   \n")
    agent = _StubAgent('{"kind": "code_fix", "summary": "x"}')
    state = _phase_correction_triage(_state(output_dir=tmp_path, agent=agent))

    assert state.halt
    assert state.halt_reason == "correction_triage_missing_context"
    assert agent.last_prompt == ""


# ── handler: dry run ───────────────────────────────────────────────────


def test_dry_run_skips_agent(tmp_path: Path) -> None:
    _write_context(tmp_path)
    agent = _StubAgent("SHOULD NOT BE CALLED")
    state = _phase_correction_triage(
        _state(output_dir=tmp_path, agent=agent, dry_run=True)
    )
    rec = state.phase_log["correction_triage"]
    assert rec["kind"] in _VALID_KINDS
    assert rec["meta"]["dry_run"] is True
    assert agent.last_prompt == ""


# ── mock provider triage branch ────────────────────────────────────────


def test_mock_provider_returns_parseable_triage() -> None:
    provider = MockAgentProvider()
    agent = provider.codex("mock")
    raw = agent.invoke(
        "[correction_triage] resolve blockers\n\n# Recorded correction context\n\nx",
        "/tmp",
    )
    parsed = _normalize_triage(_extract_json_object(raw), raw=raw)
    assert parsed["kind"] in _VALID_KINDS
    assert parsed["kind"] == "code_fix"
    assert parsed["summary"]


def test_mock_claude_also_serves_triage() -> None:
    provider = MockAgentProvider()
    agent = provider.claude("mock")
    raw = agent.invoke("[correction_triage] resolve blockers", "/tmp")
    parsed = _normalize_triage(_extract_json_object(raw), raw=raw)
    assert parsed["kind"] == "code_fix"


def test_mock_triage_kind_directive_drives_each_kind() -> None:
    # ADR 0086: a ``orcho-mock-triage-kind`` directive in the embedded
    # correction context pins the mock's triage classification.
    provider = MockAgentProvider()
    agent = provider.codex("mock")
    for kind in _VALID_KINDS:
        raw = agent.invoke(
            "[correction_triage] resolve blockers\n\n"
            "# Recorded correction context\n\n"
            f"orcho-mock-triage-kind: {kind}\n",
            "/tmp",
        )
        parsed = _normalize_triage(_extract_json_object(raw), raw=raw)
        assert parsed["kind"] == kind
        if kind == "blocked":
            assert parsed["blockers"], "blocked directive must carry a blocker"


def test_mock_triage_unknown_directive_falls_back_to_code_fix() -> None:
    provider = MockAgentProvider()
    agent = provider.codex("mock")
    raw = agent.invoke(
        "[correction_triage] resolve blockers\n\norcho-mock-triage-kind: bogus\n",
        "/tmp",
    )
    parsed = _normalize_triage(_extract_json_object(raw), raw=raw)
    assert parsed["kind"] == "code_fix"


# ── session adapter ────────────────────────────────────────────────────


def test_adapter_persists_record_into_session(tmp_path: Path) -> None:
    _write_context(tmp_path)
    agent = _StubAgent(
        '{"kind": "blocked", "summary": "cannot proceed", '
        '"allowed_scope": [], "required_checks": [], '
        '"blockers": ["missing parent diff"]}'
    )
    state = _phase_correction_triage(_state(output_dir=tmp_path, agent=agent))

    session: dict = {}
    CorrectionTriageAdapter().write("correction_triage", state, session)
    entry = session["phases"]["correction_triage"]
    assert entry["kind"] == "blocked"
    assert entry["summary"] == "cannot proceed"
    assert entry["blockers"] == ["missing parent diff"]


def test_adapter_persists_fail_fast_markers(tmp_path: Path) -> None:
    agent = _StubAgent("unused")
    state = _phase_correction_triage(_state(output_dir=tmp_path, agent=agent))

    session: dict = {}
    CorrectionTriageAdapter().write("correction_triage", state, session)
    entry = session["phases"]["correction_triage"]
    assert entry["halted"] is True
    assert entry["reason"] == "correction_triage_missing_context"
