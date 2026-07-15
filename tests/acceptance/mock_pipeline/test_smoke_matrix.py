"""MCP-wire invariance smoke for the Stage 4 verification contract (ADR 0081).

A non-e2e mock smoke (marker ``mcp_integration``, inside the
``not e2e and not packaging`` gate) that pins the ADR 0081 falsifier: a Stage 4
contract (gate_sets / selection / work_mode / schedule actions) resolves into an
executable ``ScheduledGatePlan`` *and yet the MCP-facing wire shape is
unchanged* — the plan stays in-memory, the run-header projection is name-only,
and the evidence v1 schema gains no Stage 4 slot. If a future change leaks a gate
primitive onto either surface this smoke fails and the orcho-mcp stop condition
in ADR 0081 applies.
"""

from __future__ import annotations

import pytest

from pipeline.evidence.schema import (
    REQUIRED_COMMAND_KEYS,
    REQUIRED_TOP_LEVEL_KEYS,
)
from pipeline.plugins import PluginConfig
from pipeline.verification_contract import (
    VerificationContract,
    render_header_summary,
)
from pipeline.verification_selection import (
    SelectionContext,
    build_scheduled_gate_plan,
)

pytestmark = pytest.mark.mcp_integration


def _stage4_contract() -> VerificationContract:
    contract = VerificationContract.from_plugin(
        PluginConfig(
            work_mode="governed",
            verification_envs={"ci": {}},
            verification={
                "default_env": "ci",
                "commands": {
                    "lint": {"run": "ruff check .", "env": "ci", "cheap": True},
                    "test": {"run": "pytest -q"},
                    "smoke": {"run": "pytest -q smoke"},
                },
                "required": ["test"],
                "gate_sets": {
                    "core": {"commands": ["lint", "test"],
                             "default_policy": "warn"},
                    "delivery": {"commands": ["smoke"]},
                },
                "selection": [
                    {"always": ["core"]},
                    {"paths": ["src/**"], "include": ["delivery"]},
                    {"operator": ["delivery"]},
                ],
                "schedule": [
                    {"after_phase": "implement", "commands": ["lint", "test"]},
                    {"before_delivery": True, "policy": "require",
                     "commands": ["smoke"]},
                ],
            },
        ),
    )
    assert contract is not None
    return contract


def test_stage4_plan_is_executable() -> None:
    # The Stage 4 algebra actually resolves effective policy/action — proving the
    # contract is executable, not merely projected.
    contract = _stage4_contract()
    plan = build_scheduled_gate_plan(
        contract,
        SelectionContext(work_mode="governed", touched_paths=("src/a.py",)),
    )
    by_cmd_hook = {(e.command, e.hook): e for e in plan.entries}
    # governed + required test, after_phase(implement) => require / repair_loop.
    impl = by_cmd_hook[("test", "after_phase")]
    assert impl.policy == "require"
    assert impl.action == "repair_loop"
    # governed before_delivery + require, no explicit action => exactly handoff.
    delivery = by_cmd_hook[("smoke", "before_delivery")]
    assert delivery.policy == "require"
    assert delivery.action == "handoff"


def test_header_projection_is_name_only_and_unchanged_shape() -> None:
    summary = render_header_summary(_stage4_contract())
    assert summary is not None
    # Only the Stage 1 name-only segments exist; no Stage 4 primitive leaks.
    for leaked in (
        "gate_sets", "selection", "action=", "required=",
        "repair_loop", "handoff", "primary_gate_set", "gate_plan",
        "default_policy", "verification_command",
    ):
        assert leaked not in summary, f"{leaked!r} leaked into the run header"
    # The header still carries only the documented Stage 1 segments.
    head, _, body = summary.partition(": ")
    segments = [seg.split("=", 1)[0] for seg in body.split("; ")]
    assert set(segments) <= {"work_mode", "envs", "commands", "schedule"}


def test_evidence_v1_schema_has_no_stage4_slot() -> None:
    # The evidence bundle (an MCP-read surface) must gain no Stage 4 key.
    forbidden = {
        "gate_sets", "selection", "gate_plan", "scheduled_gates",
        "effective_policy", "effective_action", "verification_command",
        "work_mode",
    }
    assert forbidden.isdisjoint(REQUIRED_TOP_LEVEL_KEYS)
    assert forbidden.isdisjoint(REQUIRED_COMMAND_KEYS)


def test_plan_is_not_serialized_into_any_contract_projection() -> None:
    # The plan is an in-memory object; building it must not mutate the contract
    # or produce a wire artifact. The only place it is cached is state.extras
    # (verified in the unit suites); the contract object itself is frozen.
    contract = _stage4_contract()
    plan = build_scheduled_gate_plan(contract, SelectionContext(work_mode="governed"))
    # ScheduledGatePlan is a plain value with no persistence hook.
    assert not hasattr(plan, "to_dict")
    assert not hasattr(plan, "write")


def test_scheduled_gate_artifact_evidence_and_sdk_keep_duplicate_identities(
    tmp_path, monkeypatch,
) -> None:
    """The core-owned artifact is the shared source for evidence and SDK rows."""
    from pipeline.evidence.collector import collect_evidence
    from pipeline.verification_ledger import GateLedgerRow, GateTrailEvent
    from pipeline.verification_ledger_store import ScheduledGateLedger, write_ledger
    from sdk.verification_timeline import get_verification_timeline

    runs = tmp_path / "runs"
    run_dir = runs / "mock-ledger"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(
        '{"run_id":"mock-ledger","project":"/project","status":"done"}',
        encoding="utf-8",
    )
    rows = (
        GateLedgerRow("check", "after_phase", "implement", "after_implement", "auto", (), "always", selected=True, execution_policy="require", disposition="executed_pass"),
        GateLedgerRow("check", "before_delivery", "", "delivery", "auto", (), "on_path", selected=False, execution_policy="require", selection_reason="paths", disposition="not_selected"),
    )
    trail = (GateTrailEvent("check", "after_phase", "implement", "execution", "pass"),)
    write_ledger(run_dir, ScheduledGateLedger(rows, trail, finalized=True))
    monkeypatch.setenv("ORCHO_RUNSPACE", str(tmp_path))

    evidence = collect_evidence(run_dir)
    sdk_rows = get_verification_timeline(run_id="mock-ledger").rows

    artifact_rows = evidence["scheduled_gate_ledger"]["rows"]
    assert [(row["gate"], row["hook"], row["phase"]) for row in artifact_rows] == [
        (row.command, row.hook, row.phase) for row in sdk_rows
    ]
    assert [row["disposition"] for row in artifact_rows] == [
        row.disposition for row in sdk_rows
    ]


# ---------------------------------------------------------------------------
# small_task bypass → implement advisory-critique forwarding smoke.
#
# The shipped ``small_task`` profile rejects validate_plan under a
# ``human_bypass`` handoff (max_rounds=1) and proceeds straight to implement
# WITHOUT a replan loop. This smoke pins the end-to-end falsifier: when
# validate_plan leaves a REJECTED critique on ``state.last_critique``, the real
# implement handler forwards it into the implement wire prompt as advisory
# reviewer critique, the loop does not pause (no phase-handoff is opened), and
# the run reaches implement. An APPROVED validate (empty critique) adds nothing.
# ---------------------------------------------------------------------------

_REJECTED_FINDING = "acceptance criteria are too vague to verify"


def _small_task_profile():
    from core.infra.paths import CONFIG_DIR
    from pipeline.profiles.loader import load_profiles_v2

    return load_profiles_v2(CONFIG_DIR / "pipeline_profiles_v2.json")["small_task"]


class _RecordingAgent:
    """Mock agent shared across phases — records implement wire prompts."""

    def __init__(self) -> None:
        self.model = "claude-opus-4-7"
        self.session_id = "sess-smoke-1"
        self.implement_prompts: list[str] = []
        self._in_implement = False

    def invoke(
        self,
        prompt: str,
        cwd: str,
        *,
        continue_session: bool = False,
        attachments: tuple = (),
        mutates_artifacts: bool = False,
    ) -> str:
        if self._in_implement:
            self.implement_prompts.append(prompt)
        return "Implemented the change."


class _PassingGate:
    """No-op ``tests`` quality gate so the smoke never spawns pytest."""

    def execute(self, gate, state, cwd):
        from pipeline.quality_gates import QualityGateResult

        return QualityGateResult(
            name=gate.name, passed=True, output="ok",
            duration_s=0.0, kind=gate.kind,
        )


def _run_small_task(monkeypatch, *, critique: str):
    from types import SimpleNamespace

    import pipeline.phases.builtin.handlers.implement as impl_mod
    from pipeline.phases.builtin.handlers.implement import _phase_implement
    from pipeline.quality_gates import QualityGateRegistry
    from pipeline.runtime import PhaseRegistry, PipelineState, run_profile

    # Avoid the post-implement environment-probe subprocess.
    monkeypatch.setattr(
        impl_mod, "_write_implement_verification_receipt", lambda state: None,
    )

    agent = _RecordingAgent()
    state = PipelineState(
        task="Add structured logging",
        project_dir="/proj",
        plugin=PluginConfig(),
        extras={"run_id": "run-smoke-1", "loop_round": 1, "plan_round": 1},
    )
    state.phase_config = SimpleNamespace(
        plan_agent=agent,
        validate_plan_agent=agent,
        implement_agent=agent,
        review_agent=agent,
        repair_agent=agent,
        final_acceptance_agent=agent,
    )

    reg = PhaseRegistry()
    reg.register("plan", lambda s: s)

    def _validate_plan(s: PipelineState) -> PipelineState:
        # Mock validate verdict: REJECTED (critique set) or APPROVED (empty).
        approved = not critique
        s.phase_log["validate_plan"] = {
            "approved": approved,
            "verdict": "APPROVED" if approved else "REJECTED",
            "critique": critique,
        }
        # validate_plan writes the critique onto last_critique on REJECTED,
        # and clears it on APPROVED — mirror that contract here.
        s.last_critique = critique
        return s

    def _implement(s: PipelineState) -> PipelineState:
        agent._in_implement = True
        try:
            return _phase_implement(s)
        finally:
            agent._in_implement = False

    reg.register("validate_plan", _validate_plan)
    reg.register("implement", _implement)

    gates = QualityGateRegistry()
    gates.register("tests", _PassingGate())

    run_profile(
        _small_task_profile(), state, reg, quality_gate_registry=gates,
    )
    return state, agent


def test_small_task_bypass_forwards_critique_to_implement(monkeypatch) -> None:
    state, agent = _run_small_task(monkeypatch, critique=(
        f"REJECTED. Findings:\n- {_REJECTED_FINDING}"
    ))
    # The run reached implement exactly once.
    assert len(agent.implement_prompts) == 1
    prompt = agent.implement_prompts[0]
    # The rejected finding text rides into the implement prompt, framed as
    # advisory reviewer critique (not a replan command, not a blocking gate).
    assert _REJECTED_FINDING in prompt
    assert "Do not replan" in prompt
    assert "reviewer advisory feedback" in prompt
    # human_bypass never opens a phase-handoff: the loop did not pause.
    assert state.phase_handoff_request is None


def test_small_task_approved_plan_adds_no_advisory(monkeypatch) -> None:
    state, agent = _run_small_task(monkeypatch, critique="")
    assert len(agent.implement_prompts) == 1
    prompt = agent.implement_prompts[0]
    assert "Do not replan" not in prompt
    assert "reviewer advisory feedback" not in prompt
    assert state.phase_handoff_request is None
