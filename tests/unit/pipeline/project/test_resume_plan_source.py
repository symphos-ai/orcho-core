"""Real session/plan writers feed restored request and profile readers."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from agents.entities import SubTask
from pipeline.engine.session import init_session, save_session
from pipeline.plan_artifacts import ParsedPlanArtifactError, write_parsed_plan_artifact
from pipeline.plan_parser import ParsedPlan
from pipeline.project.profile_setup import setup_profile
from pipeline.project.resume_plan_source import restore_inherited_plan_request
from pipeline.project.types import PresentationPolicy, ProjectRunRequest


def inherited(tmp_path: Path):
    parent = tmp_path / 'runs/plan1'
    child = tmp_path / 'runs/child1'
    parent.mkdir(parents=True)
    plan = ParsedPlan(short_summary='accepted plan', planning_context='context',
                      subtasks=(SubTask(id='T1', goal='Bounded change', spec='spec'),), source='json')
    write_parsed_plan_artifact(parent, plan, attempt=1)
    session = init_session(plan_source='run', plan_source_run_id=parent.name,
                           parent_run_id=parent.name, parent_run_dir=str(parent),
                           profile='feature', projected_profile='feature#from_run_plan')
    session['status'] = 'awaiting_phase_handoff'
    save_session(child, session)
    request = ProjectRunRequest(task='bounded change', project_dir=str(tmp_path),
                                output_dir=child, resume_from=child.name, profile_name='feature',
                                presentation=PresentationPolicy.SILENT, no_interactive=True)
    return request, parent, plan, session


def resolve(request: ProjectRunRequest):
    return setup_profile(profile_name=request.profile_name, profile_obj=request.profile_obj,
                         from_run_plan_parent_dir=request.from_run_plan_parent_dir,
                         plan_source=request.plan_source, handoff_path=request.handoff_path,
                         max_rounds=request.max_rounds, presentation=request.presentation,
                         allow_env_override=False)


def test_real_writer_to_request_to_projected_profile(tmp_path: Path) -> None:
    request, parent, plan, _ = inherited(tmp_path)
    before = (request.output_dir / 'meta.json').read_bytes()
    restored = restore_inherited_plan_request(request)
    assert restored.from_run_plan_parent_dir == parent
    assert restored.followup_parent_run_id == parent.name
    assert restored.followup_parent_run_dir == str(parent)
    profile = resolve(restored)
    assert profile.from_run_plan_loaded == dataclasses.replace(plan, source='artifact')
    assert profile.from_run_plan_stripped == ('plan', 'validate_plan')
    assert profile.v2_profile.name == 'feature#from_run_plan'
    assert (request.output_dir / 'meta.json').read_bytes() == before
    assert restore_inherited_plan_request(restored) == restored


@pytest.mark.parametrize('field', ['plan_source_run_id', 'parent_run_dir', 'parent_run_id'])
def test_missing_or_conflicting_provenance_refuses_before_rewrite(tmp_path: Path, field: str) -> None:
    request, _, _, session = inherited(tmp_path)
    session.pop(field)
    save_session(request.output_dir, session)
    before = (request.output_dir / 'meta.json').read_bytes()
    with pytest.raises(ValueError, match='inherited-plan checkpoint'):
        restore_inherited_plan_request(request)
    assert (request.output_dir / 'meta.json').read_bytes() == before


def test_cannot_replace_checkpoint_plan(tmp_path: Path) -> None:
    request, _, _, _ = inherited(tmp_path)
    changed = dataclasses.replace(request, from_run_plan_parent_dir=tmp_path / 'other')
    with pytest.raises(ValueError, match='cannot replace'):
        restore_inherited_plan_request(changed)


def test_missing_parent_artifact_does_not_replan(tmp_path: Path) -> None:
    request, parent, _, _ = inherited(tmp_path)
    for path in parent.glob('parsed_plan*.json'):
        path.unlink()
    with pytest.raises(ParsedPlanArtifactError):
        resolve(restore_inherited_plan_request(request))


def test_fresh_and_ordinary_checkpoint_unchanged(tmp_path: Path) -> None:
    request, _, _, session = inherited(tmp_path)
    assert restore_inherited_plan_request(dataclasses.replace(request, resume_from=None)) \
        == dataclasses.replace(request, resume_from=None)
    session['plan_source'] = 'local'
    save_session(request.output_dir, session)
    assert restore_inherited_plan_request(request) is request
