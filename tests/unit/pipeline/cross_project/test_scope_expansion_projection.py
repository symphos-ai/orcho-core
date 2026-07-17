"""Cross-child declared-write scope survives a cold handoff resume."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from agents.protocols import SessionMode
from pipeline.cross_project.handoff import Handoff, write_handoff
from pipeline.cross_project.project_dispatch import _normalize_unit_declared_files
from pipeline.cross_project.task_plan import CrossTaskUnit
from pipeline.engine.declared_write_scope import DECLARED_WRITE_SCOPE_EXTRAS_KEY
from pipeline.phases.builtin import default_registry
from pipeline.phases.builtin.review_support import _scope_expansion_assessment
from pipeline.plugins import PluginConfig
from pipeline.project.profile_setup import setup_profile
from pipeline.project.state_setup import StateInputs, build_pipeline_state
from pipeline.project.types import PresentationPolicy
from pipeline.runtime import CrossScope, CrossStepPolicy, PhaseStep, Profile, ProfileKind
from pipeline.verification_contract import VerificationContract


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _child_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "child"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "baseline.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "baseline.py")
    _git(repo, "commit", "-qm", "baseline")
    for rel in ("src/generated.py", "tests/test_generated.py"):
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n", encoding="utf-8")
    return repo


class _ApprovedReleaseAgent:
    model = "test-release"
    session_id = None

    def __init__(self) -> None:
        self.prompt = ""

    def invoke(self, prompt: str, cwd: str, **_kwargs) -> str:
        del cwd
        self.prompt = prompt
        return json.dumps({
            "verdict": "APPROVED", "ship_ready": True,
            "short_summary": "scope is clean", "release_blockers": [],
            "verification_gaps": [],
            "contract_status": {
                "task_contract": "satisfied", "interfaces": "not_applicable",
                "persistence": "not_applicable", "tests": "sufficient",
            },
        })


def _profile() -> Profile:
    cross = CrossStepPolicy(scope=CrossScope.PROJECT)
    return Profile(
        name="cross-child", kind=ProfileKind.CUSTOM,
        steps=(PhaseStep(phase="implement", cross=cross), PhaseStep(phase="final_acceptance", cross=cross)),
    )


def _handoff(alias_dir: Path, repo: Path) -> Path:
    alias_dir.mkdir(parents=True)
    unit = CrossTaskUnit(
        unit_id="api", alias="api", goal="g", spec="s",
        depends_on=(), files=("[api]/src/generated.py", "[api]/tests/test_generated.py"),
        produces="", consumes="",
    )
    declared_files = _normalize_unit_declared_files(unit.alias, unit.files)
    return write_handoff(Handoff(
        parent_run_id="parent", profile="cross-child", alias="api",
        project_path=str(repo), approved_cross_plan_path="/cross-plan.md",
        full_cross_plan_path="/cross-plan.md", full_cross_plan_markdown="cross plan",
        cross_validation_summary="approved", cross_validation_verdict={"verdict": "APPROVED"},
        # Deliberately misleading prose: control ownership comes only from the
        # JSON declared_files field, never from prompt-like text.
        project_subtask="Do not infer ownership from this prose: sibling.py",
        declared_files=declared_files, sibling_aliases=(),
    ), alias_dir)


def _state_from_handoff(*, repo: Path, handoff_path: Path, output_dir: Path):
    profile_setup = setup_profile(
        profile_name="cross-child", profile_obj=_profile(),
        from_run_plan_parent_dir=None, plan_source="cross",
        handoff_path=str(handoff_path), max_rounds=1,
        presentation=PresentationPolicy.SILENT, allow_env_override=False,
    )
    plugin = PluginConfig(work_mode="pro", verification={})
    contract = VerificationContract.from_plugin(plugin)
    assert contract is not None
    agent = _ApprovedReleaseAgent()
    state = build_pipeline_state(StateInputs(
        task="child task", project_path=repo, plugin=plugin,
        phase_config=SimpleNamespace(final_acceptance_agent=agent), agent_registry=None,
        output_dir=output_dir, dry_run=False, session={}, session_ts="child-run",
        git_cwd=str(repo), change_handoff=profile_setup.change_handoff,
        cross_handoff_text=profile_setup.cross_handoff_text,
        cross_declared_files=profile_setup.cross_declared_files,
        plan_source=profile_setup.plan_source, handoff_path=str(handoff_path),
        auto_waiver_allowed=False, followup_seed_count=0, ckpt=None, attachments=(),
        session_mode=SessionMode.AUTO, implement_model="test", repair_model="test",
        repair_escalation_model="test", chain_same_model_only=False,
        presentation=PresentationPolicy.SILENT, render_phase_outputs=False,
        from_run_plan_loaded=None, followup_parent_run_id=None,
        from_run_plan_parent_dir=None, from_run_plan_stripped=(),
        verification_contract=contract,
    )).state
    return profile_setup, state, agent


def _assert_no_scope_projection(state, agent: _ApprovedReleaseAgent) -> None:
    assessment = _scope_expansion_assessment(state)
    assert not assessment.items
    default_registry().get("final_acceptance")(state)
    entry = state.phase_log["final_acceptance"]
    assert "scope_expansion" not in entry
    assert "scope_expansion_sanction" not in entry
    assert state.phase_handoff_request is None
    assert agent.prompt
    assert "Scope expansion" not in agent.prompt
    assert "Scope expansion" not in entry["output"]


def test_cross_child_scope_is_rehydrated_only_from_canonical_handoff(
    tmp_path: Path,
) -> None:
    repo = _child_repo(tmp_path)
    handoff_path = _handoff(tmp_path / "cross-run" / "api", repo)

    first_setup, first, first_agent = _state_from_handoff(
        repo=repo, handoff_path=handoff_path, output_dir=tmp_path / "first",
    )
    assert first_setup.cross_declared_files == (
        "src/generated.py", "tests/test_generated.py",
    )
    assert first.extras[DECLARED_WRITE_SCOPE_EXTRAS_KEY].patterns == (
        "src/generated.py", "tests/test_generated.py",
    )
    _assert_no_scope_projection(first, first_agent)

    # Build a wholly fresh process-shaped state from the JSON sidecar: neither
    # ParsedPlan nor any extras object from the initial state crosses this seam.
    cold_setup, cold, cold_agent = _state_from_handoff(
        repo=repo, handoff_path=handoff_path, output_dir=tmp_path / "cold",
    )
    assert cold.parsed_plan is None
    assert cold_setup.cross_declared_files == first_setup.cross_declared_files
    assert cold.extras[DECLARED_WRITE_SCOPE_EXTRAS_KEY] == (
        first.extras[DECLARED_WRITE_SCOPE_EXTRAS_KEY]
    )
    _assert_no_scope_projection(cold, cold_agent)
