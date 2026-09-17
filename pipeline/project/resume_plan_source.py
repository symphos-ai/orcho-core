"""Restore inherited plan provenance before checkpoint setup rewrites metadata."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from pipeline.project.types import ProjectRunRequest


def restore_inherited_plan_request(request: ProjectRunRequest) -> ProjectRunRequest:
    """Reuse the recorded parent plan and existing profile projection on resume.

    A checkpoint continuation of a from-run-plan child must keep its accepted
    plan, rather than dispatching the requested profile's planning block again.
    Fresh runs and ordinary checkpoint resumes remain unchanged.
    """
    if not request.resume_from or request.output_dir is None:
        return request
    try:
        meta = json.loads((request.output_dir / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return request
    if not isinstance(meta, dict) or meta.get("plan_source") != "run":
        return request
    source_id = meta.get("plan_source_run_id")
    source_path = meta.get("parent_run_dir")
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("inherited-plan checkpoint has no plan_source_run_id")
    if not isinstance(source_path, str) or not source_path.strip():
        raise ValueError("inherited-plan checkpoint has no parent_run_dir")
    parent = Path(source_path)
    if parent.name != source_id or meta.get("parent_run_id") != source_id:
        raise ValueError("inherited-plan checkpoint has conflicting parent identities")
    if request.from_run_plan_parent_dir is not None and request.from_run_plan_parent_dir != parent:
        raise ValueError("checkpoint cannot replace its recorded inherited plan")
    return dataclasses.replace(
        request,
        from_run_plan_parent_dir=parent,
        followup_parent_run_id=source_id,
        followup_parent_run_dir=str(parent),
    )
