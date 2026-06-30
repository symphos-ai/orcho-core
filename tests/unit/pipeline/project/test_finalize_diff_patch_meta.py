# SPDX-License-Identifier: Apache-2.0
"""Durable ``diff_patch`` apply-check block in ``meta.json`` (T2).

``finalize_project_run`` captures the run-level ``diff.patch`` BEFORE delivery
and now persists the compact apply-check triad under
``session["diff_patch"]``, so the same ``meta.json`` read by status/delivery
surfaces records whether the captured patch is valid — not only the transient
``artifact.created`` evidence event.

These drive the real finalization path (real capture + real ``save_session``)
against a throwaway git repo and assert the persisted block, covering both a
valid patch (``patch_valid``) and a corrupt artifact (``patch_invalid``).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.engine.run_diff import snapshot_worktree
from pipeline.project.finalization import (
    FinalizationContext,
    finalize_project_run,
)


def _git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(path),
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "orcho@example.test")
    _git(path, "config", "user.name", "Orcho Test")
    (path / "payload.py").write_text("value = 1\n", encoding="utf-8")
    _git(path, "add", "payload.py")
    _git(path, "commit", "-qm", "initial")


def _stub_finalize_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize the non-diff finalization side-effects so the test only
    exercises capture → durable persist → meta.json."""
    monkeypatch.setattr(
        "pipeline.evidence.write_bundle_or_placeholder",
        lambda output_dir, *, run_id, status: output_dir / "evidence.json",
    )
    monkeypatch.setattr(
        "pipeline.engine.artifact_mirror.mirror_to_projects",
        lambda *_a, **_kw: [],
    )
    monkeypatch.setattr(
        "pipeline.observability.context_pressure.format_context_summary",
        lambda _session: None,
    )
    monkeypatch.setattr(
        "core.infra.config.AppConfig.load",
        lambda: SimpleNamespace(artifacts={}, commit={}, accounting={}),
    )


def _make_run(project: Path, run_dir: Path, baseline: str) -> SimpleNamespace:
    metrics = SimpleNamespace(
        save=lambda output_dir: output_dir / "metrics.json",
        summary_line=lambda: "Tokens: 0",
        as_dict=lambda: {},
        phases=[],
    )
    state = SimpleNamespace(halt=False, halt_reason=None, extras={}, phase_log={})
    return SimpleNamespace(
        output_dir=run_dir,
        task="# Orcho Task: durable diff_patch",
        session={"status": "done"},
        state=state,
        profile_name="default",
        parent_run_id=None,
        project_alias=None,
        project_path=project,
        worktree_context=None,
        no_interactive=False,
        _metrics=metrics,
        _ckpt=None,
        _done_summary_profile=None,
        session_ts="20260625_000000",
        _worktree_cvar_token=None,
        _sandbox_cvar_token=None,
        _effective_diff_cwd=lambda: project,
        _commit_delivery_baseline=lambda: baseline,
        # No-op delivery: leave the captured patch / checkout untouched so
        # the apply-check reflects the captured artifact, not a mutated tree.
        _run_commit_delivery=lambda _diff_cwd: None,
    )


def test_finalize_persists_patch_valid_block_in_meta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    _init_repo(project)
    baseline = snapshot_worktree(project)
    assert baseline is not None
    (project / "payload.py").write_text("value = 2\n", encoding="utf-8")
    _stub_finalize_side_effects(monkeypatch)

    finalize_project_run(FinalizationContext(run=_make_run(project, run_dir, baseline)))

    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    diff_patch = meta["diff_patch"]
    assert diff_patch["status"] == "patch_valid"
    assert diff_patch["reason"] == "patch_applies"
    assert diff_patch["baseline_ref"] == baseline
    assert diff_patch["patch_path"].endswith("diff.patch")


def test_finalize_persists_patch_invalid_for_corrupt_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    _init_repo(project)
    baseline = snapshot_worktree(project)
    assert baseline is not None
    _stub_finalize_side_effects(monkeypatch)

    # Force a corrupt captured artifact: a valid baseline makes read-tree
    # succeed, so the apply-check reaches git apply --check and FAILS, which
    # must project to the durable triad 'patch_invalid' (fail, not degraded).
    corrupt = run_dir / "diff.patch"

    def _corrupt_capture(_project: Path, out_dir: Path, **_kw: object) -> Path:
        corrupt.write_text("not a unified patch\n", encoding="utf-8")
        return corrupt

    monkeypatch.setattr(
        "pipeline.engine.diff_apply_check.capture_run_diff",
        _corrupt_capture,
    )

    finalize_project_run(FinalizationContext(run=_make_run(project, run_dir, baseline)))

    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["diff_patch"]["status"] == "patch_invalid"
    assert meta["diff_patch"]["reason"] == "patch_does_not_apply"
