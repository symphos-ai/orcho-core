"""
Per-project mirror of run artifacts.

Покрывает:
mirror_to_project=False → no-op (правильное поведение по умолчанию).
single-mode mirror: matching patterns копируются с провенанс-хедером.
non-matching и низкоуровневые файлы (output.log, checkpoints.db) НЕ зеркалятся.
cross-mode: per-alias артефакты из run_dir/<alias>/ + shared из run_dir/.
best-effort: OSError на одной dst не валит остальные.
"""

from __future__ import annotations

from pathlib import Path

from pipeline.engine.artifact_mirror import mirror_to_projects

# ── helpers ──────────────────────────────────────────────────────────────────


def _setup_run_dir(tmp_path: Path) -> Path:
    """Создать минимальный workspace/runspace/runs/<ts>/ скелет с типовыми
 файлами от orchestrator-а."""
    run_dir = tmp_path / "workspace" / "runspace" / "runs" / "20260504_120000"
    run_dir.mkdir(parents=True)

    # Семантические артефакты (eligible).
    (run_dir / "plan.md").write_text("# Plan\n- step 1\n", encoding="utf-8")
    (run_dir / "todo.md").write_text("# TODO\n- [ ] do thing\n", encoding="utf-8")
    (run_dir / "review.md").write_text("# Review\nApproved\n", encoding="utf-8")
    (run_dir / "diff.patch").write_text("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@\n-1\n+2\n", encoding="utf-8")

    # Низкоуровневые артефакты (НЕ должны зеркалиться).
    (run_dir / "output.log").write_text("agent stdout\n", encoding="utf-8")
    (run_dir / "progress.log").write_text("phase ts\n", encoding="utf-8")
    (run_dir / "metrics.json").write_text('{"tokens": 100}', encoding="utf-8")
    (run_dir / "meta.json").write_text('{"task": "x"}', encoding="utf-8")
    (run_dir / "checkpoints.db").write_bytes(b"SQLite stub")
    return run_dir


def _setup_project(tmp_path: Path, name: str) -> Path:
    proj = tmp_path / name
    proj.mkdir()
    return proj


_DEFAULT_CFG = {
    "mirror_to_project": True,
    "mirror_patterns": ["plan.md", "todo.md", "review.md", "cross_plan.md", "diff.patch"],
    "mirror_dir": ".orcho/artifacts",
}


# ── tests ────────────────────────────────────────────────────────────────────


class TestMirrorDisabled:
    def test_returns_empty_when_mirror_to_project_false(self, tmp_path: Path) -> None:
        run_dir = _setup_run_dir(tmp_path)
        proj = _setup_project(tmp_path, "myproj")
        cfg = {**_DEFAULT_CFG, "mirror_to_project": False}
        result = mirror_to_projects(run_dir, {"myproj": proj}, cfg)
        assert result == []
        assert not (proj / ".orcho").exists()

    def test_returns_empty_when_no_projects(self, tmp_path: Path) -> None:
        run_dir = _setup_run_dir(tmp_path)
        assert mirror_to_projects(run_dir, None, _DEFAULT_CFG) == []
        assert mirror_to_projects(run_dir, {}, _DEFAULT_CFG) == []

    def test_returns_empty_when_patterns_missing(self, tmp_path: Path) -> None:
        run_dir = _setup_run_dir(tmp_path)
        proj = _setup_project(tmp_path, "myproj")
        cfg = {**_DEFAULT_CFG, "mirror_patterns": []}
        assert mirror_to_projects(run_dir, {"myproj": proj}, cfg) == []


class TestSingleModeMirror:
    def test_copies_matching_patterns_only(self, tmp_path: Path) -> None:
        run_dir = _setup_run_dir(tmp_path)
        proj = _setup_project(tmp_path, "myproj")

        result = mirror_to_projects(run_dir, {"myproj": proj}, _DEFAULT_CFG)

        mirror_dir = proj / ".orcho" / "artifacts"
        # Должны появиться semantic-артефакты.
        assert (mirror_dir / "plan.md").exists()
        assert (mirror_dir / "todo.md").exists()
        assert (mirror_dir / "review.md").exists()
        assert (mirror_dir / "diff.patch").exists()
        assert len(result) == 4
        # cross_plan.md в run_dir нет (single-mode), его и не должно быть.
        assert not (mirror_dir / "cross_plan.md").exists()

    def test_low_level_files_never_mirrored(self, tmp_path: Path) -> None:
        run_dir = _setup_run_dir(tmp_path)
        proj = _setup_project(tmp_path, "myproj")
        mirror_to_projects(run_dir, {"myproj": proj}, _DEFAULT_CFG)

        mirror_dir = proj / ".orcho" / "artifacts"
        for low_level in ("output.log", "progress.log", "metrics.json",
                          "meta.json", "checkpoints.db"):
            assert not (mirror_dir / low_level).exists(), (
                f"{low_level} попал в mirror, хотя не должен — он не в mirror_patterns"
            )

    def test_markdown_gets_provenance_header(self, tmp_path: Path) -> None:
        run_dir = _setup_run_dir(tmp_path)
        proj = _setup_project(tmp_path, "myproj")
        mirror_to_projects(run_dir, {"myproj": proj}, _DEFAULT_CFG)

        plan_copy = (proj / ".orcho" / "artifacts" / "plan.md").read_text(encoding="utf-8")
        assert plan_copy.startswith("<!-- mirrored from "), (
            "Markdown должен получать header с провенансом для аудита"
        )
        # Оригинальный контент должен сохраниться после header'а.
        assert "# Plan" in plan_copy
        assert "- step 1" in plan_copy

    def test_diff_patch_no_header_inject(self, tmp_path: Path) -> None:
        run_dir = _setup_run_dir(tmp_path)
        proj = _setup_project(tmp_path, "myproj")
        mirror_to_projects(run_dir, {"myproj": proj}, _DEFAULT_CFG)

        diff_copy = (proj / ".orcho" / "artifacts" / "diff.patch").read_text(encoding="utf-8")
        # .patch — не markdown, header не инжектится (иначе git apply сломается).
        assert not diff_copy.startswith("<!--")
        assert diff_copy.startswith("diff --git")


class TestCrossModeMirror:
    def test_per_alias_subdir_artifacts_routed_correctly(self, tmp_path: Path) -> None:
        run_dir = _setup_run_dir(tmp_path)

        # Cross orchestrator кладёт per-project артефакты в run_dir/<alias>/.
        for alias in ("unity", "api"):
            (run_dir / alias).mkdir()
            (run_dir / alias / "plan.md").write_text(
                f"# {alias} subplan\n", encoding="utf-8",
            )

        # Shared cross_plan.md в самом run_dir/.
        (run_dir / "cross_plan.md").write_text("# Cross\n", encoding="utf-8")

        unity = _setup_project(tmp_path, "unity")
        api   = _setup_project(tmp_path, "api")

        mirror_to_projects(run_dir, {"unity": unity, "api": api}, _DEFAULT_CFG)

        # У каждого проекта свой план из subdir (не unity-плана в api).
        assert (unity / ".orcho/artifacts/plan.md").read_text().endswith("# unity subplan\n")
        assert (api   / ".orcho/artifacts/plan.md").read_text().endswith("# api subplan\n")
        # Shared cross_plan.md — у обоих.
        assert (unity / ".orcho/artifacts/cross_plan.md").exists()
        assert (api   / ".orcho/artifacts/cross_plan.md").exists()


class TestMirrorRobustness:
    def test_missing_run_dir_does_not_crash(self, tmp_path: Path) -> None:
        proj = _setup_project(tmp_path, "myproj")
        # run_dir вообще не существует.
        result = mirror_to_projects(
            tmp_path / "ghost_run", {"myproj": proj}, _DEFAULT_CFG,
        )
        assert result == []

    def test_custom_mirror_dir_respected(self, tmp_path: Path) -> None:
        run_dir = _setup_run_dir(tmp_path)
        proj = _setup_project(tmp_path, "myproj")
        cfg = {**_DEFAULT_CFG, "mirror_dir": "docs/orcho"}
        mirror_to_projects(run_dir, {"myproj": proj}, cfg)

        assert (proj / "docs" / "orcho" / "plan.md").exists()
        assert not (proj / ".orcho" / "artifacts" / "plan.md").exists()
