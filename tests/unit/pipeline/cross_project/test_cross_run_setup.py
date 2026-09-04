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
        mock=True,
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
    assert meta["mock"] is True
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


def test_setup_cross_run_persists_real_provider_mode_as_boolean(tmp_path) -> None:
    run_dir = tmp_path / "runs" / "20260623_090355"
    project = tmp_path / "project"
    project.mkdir()
    profile_setup = SimpleNamespace(
        requested_profile=SimpleNamespace(name="feature"),
        projected_profile_name="feature#project",
    )

    setup_cross_run(
        task="real provider cross run",
        projects={"project": project},
        model="fake-model",
        mock=False,
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

    assert json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))["mock"] is False


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
        mock=False,
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


def test_resume_hydrates_declared_child_sessions_in_request_order(tmp_path) -> None:
    run_dir = tmp_path / "run"
    projects = {"consumer": tmp_path / "consumer", "producer": tmp_path / "producer"}
    for path in projects.values():
        path.mkdir()
    run_dir.mkdir()
    producer = {
        "status": "done",
        "phases": {"final_acceptance": {"verdict": "APPROVED", "ship_ready": True}},
    }
    (run_dir / "producer").mkdir()
    (run_dir / "producer" / "meta.json").write_text(json.dumps(producer), encoding="utf-8")
    (run_dir / "consumer").mkdir()
    (run_dir / "consumer" / "meta.json").write_text("{broken", encoding="utf-8")
    profile_setup = SimpleNamespace(
        requested_profile=SimpleNamespace(name="feature"),
        projected_profile_name="feature#project",
    )

    setup = setup_cross_run(
        task="resume",
        projects=projects,
        model="fake-model",
        mock=False,
        output_dir=run_dir,
        cross_mode="full",
        resume_from=run_dir.name,
        resume_mode=None,
        followup_parent_run_id=None,
        followup_parent_run_dir=None,
        followup_parent_status=None,
        followup_base_task=None,
        resumed_meta={"phases": {"projects": {"consumer": {"status": "done"}}}},
        profile_setup=profile_setup,
        terminal=False,
    )

    assert list(setup.session["phases"]["projects"]) == ["producer"]
    assert setup.session["phases"]["projects"]["producer"] == producer


def test_read_plan_file_warns_and_regenerates_when_file_missing(
    tmp_path, capsys,
) -> None:
    from pipeline.cross_project.run_setup import _read_plan_file

    missing = tmp_path / "no_such_plan.md"
    assert _read_plan_file(str(missing), terminal=True) is None

    out = capsys.readouterr().out
    assert "does not exist, regenerating the plan" in out


def test_read_plan_file_warns_and_regenerates_when_file_unreadable(
    tmp_path, capsys,
) -> None:
    from pipeline.cross_project.run_setup import _read_plan_file

    unreadable = tmp_path / "plan_dir.md"
    unreadable.mkdir()  # exists, but read_text raises OSError
    assert _read_plan_file(str(unreadable), terminal=True) is None

    out = capsys.readouterr().out
    assert "regenerating the plan" in out
    assert "failed" in out


def test_setup_cross_run_records_installed_orcho_versions(
    tmp_path, monkeypatch,
) -> None:
    from pipeline.cross_project import run_setup

    monkeypatch.setattr(
        run_setup, "installed_orcho_versions", lambda: {"orcho-core": "1.2.3"},
    )
    run_dir = tmp_path / "runs" / "20260623_090354"
    core = tmp_path / "orcho-core"
    core.mkdir()
    profile_setup = SimpleNamespace(
        requested_profile=SimpleNamespace(name="feature"),
        projected_profile_name="feature#project",
    )

    setup_cross_run(
        task="cross run stamped with versions",
        projects={"core": core},
        model="fake-model",
        mock=True,
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

    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["versions"] == {"orcho-core": "1.2.3"}
