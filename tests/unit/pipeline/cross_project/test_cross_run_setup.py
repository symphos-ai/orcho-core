from __future__ import annotations

import json
from types import SimpleNamespace

from pipeline.control.resume_context import resolve_latest_run
from pipeline.cross_project.run_setup import setup_cross_run


def test_setup_cross_run_persists_parent_meta_for_resume_discovery(
    tmp_path,
) -> None:
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "20260623_090354"
    core = tmp_path / "orcho-core"
    mcp = tmp_path / "orcho-mcp"
    core.mkdir()
    mcp.mkdir()

    profile_setup = SimpleNamespace(
        requested_profile=SimpleNamespace(name="feature"),
        projected_profile_name="feature#project",
    )

    setup = setup_cross_run(
        task="cross run that may be interrupted during child dispatch",
        projects={"core": core, "mcp": mcp},
        model="fake-model",
        output_dir=run_dir,
        cross_mode="full",
        resume_from=None,
        resume_mode=None,
        followup_parent_run_id=None,
        followup_parent_run_dir=None,
        followup_parent_status=None,
        followup_base_task=None,
        resumed_meta=None,
        profile_setup=profile_setup,
        terminal=False,
    )

    meta_path = run_dir / "meta.json"
    assert meta_path.is_file()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["status"] == "running"
    assert meta["profile"] == "feature"
    assert meta["projects"] == {
        "core": str(core),
        "mcp": str(mcp),
    }
    assert setup.session == meta

    assert resolve_latest_run(
        runs_dir=runs_dir,
        kind="cross",
        prefer_incomplete=True,
        include_terminal_success=True,
        require_existing_project=True,
    ) == "20260623_090354"


def test_setup_cross_run_resume_does_not_clobber_existing_meta(
    tmp_path,
) -> None:
    run_dir = tmp_path / "runs" / "20260623_090354"
    run_dir.mkdir(parents=True)
    existing_meta = {
        "status": "awaiting_phase_handoff",
        "phases": {
            "cross_final_acceptance": {
                "verdict": "REJECTED",
                "release_blockers": [{"id": "CFA_STUB_REJECT"}],
            },
        },
    }
    meta_path = run_dir / "meta.json"
    meta_path.write_text(json.dumps(existing_meta), encoding="utf-8")
    (run_dir / "cross_checkpoint.json").write_text(
        json.dumps({
            "phase0_done": True,
            "sub_status": {},
            "phase_handoff_pending": True,
            "phase_handoff_kind": "cfa",
            "phase_handoff_id": "cfa:cross_final_acceptance:1",
        }),
        encoding="utf-8",
    )

    profile_setup = SimpleNamespace(
        requested_profile=SimpleNamespace(name="feature"),
        projected_profile_name="feature#project",
    )

    setup_cross_run(
        task="resume existing cross run",
        projects={},
        model="fake-model",
        output_dir=run_dir,
        cross_mode="full",
        resume_from=run_dir.name,
        resume_mode=None,
        followup_parent_run_id=None,
        followup_parent_run_dir=None,
        followup_parent_status=None,
        followup_base_task=None,
        resumed_meta=None,
        profile_setup=profile_setup,
        terminal=False,
    )

    assert json.loads(meta_path.read_text(encoding="utf-8")) == existing_meta
