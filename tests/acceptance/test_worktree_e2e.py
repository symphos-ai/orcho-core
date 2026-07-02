"""GWT-1 acceptance tests: per-run worktree isolation end-to-end.

Uses the ``--mock`` pipeline (MockAgentProvider, zero LLM calls) to verify
that the worktree integration is wired correctly through the orchestrator.

Coverage:
  W1. ``meta.json`` carries a ``worktree`` block after a run with isolation on.
  W2. ``meta.worktree.mode`` is ``"per_run"`` when a real git repo is present.
  W3. ``worktree_config_override={"enabled": False}`` produces ``mode="off"``
      in ``meta.json`` and does not materialise a checkout directory.
  W4. User source checkout is byte-identical before and after an isolated run.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from agents.runtimes import MockAgentProvider
from pipeline.plugins import PluginConfig
from pipeline.project_orchestrator import run_pipeline  # noqa: E402

# ── Helpers ────────────────────────────────────────────────────────────────


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@orcho.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Orcho Test"], cwd=path, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=path, check=True)
    (path / "src.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


PLUGIN = PluginConfig(
    name="Worktree E2E Project",
    language="Python",
    architecture="CLI",
    file_hints=["src.py"],
)


def _patches():
    lp = patch("pipeline.project.session_run.load_plugin", return_value=PLUGIN)
    hu = patch("core.io.git_helpers.has_uncommitted", return_value=False)
    gd = patch("core.io.git_helpers.git_diff_stat", return_value="0 files changed")
    return lp, hu, gd


@pytest.fixture(autouse=True)
def _reset_logging():
    import agents.stream as _stream
    import core.observability.logging as _log
    yield
    _log._progress_log = None
    _stream._agent_log = None


@pytest.fixture(autouse=True)
def _adr0119_legacy_bypass_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin delivery to the ADR 0119 ``bypass`` opt-out for this legacy slice.

    ADR 0119 shipped ``branch_policy=worktree_branch`` as the delivery default,
    which publishes an isolated run's own branch instead of committing onto the
    target checkout. These end-to-end tests predate that policy and assert the
    prior "diff delivered into the project checkout on approve" behavior, so they
    run under ``bypass`` (the ADR's explicit legacy opt-out). The new
    branch-policy behavior is covered by
    ``tests/unit/pipeline/engine/test_commit_delivery.py`` and
    ``test_delivery_branch.py``.
    """
    import pipeline.engine.delivery_branch as _db

    monkeypatch.setattr(_db, "normalize_branch_policy", lambda _raw: "bypass")


# ── W3: isolation disabled via override ────────────────────────────────────


class TestWorktreeOff:
    def test_meta_worktree_mode_is_off(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        run_dir = tmp_path / "run" / "20260522_001"
        run_dir.mkdir(parents=True)
        provider = MockAgentProvider(latency=0.0, test_pass_rate=1.0)

        lp, hu, gd = _patches()
        with lp, hu, gd:
            session = run_pipeline(
                task="add a comment",
                project_dir=str(project),
                output_dir=run_dir,
                provider=provider,
                profile_name="task",
                worktree_config_override={"enabled": False},
            )

        assert session.get("worktree", {}).get("isolation") == "off"
        meta = json.loads((run_dir / "meta.json").read_text())
        assert meta.get("worktree", {}).get("isolation") == "off"

    def test_no_checkout_dir_when_disabled(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        run_dir = tmp_path / "run" / "20260522_002"
        run_dir.mkdir(parents=True)
        provider = MockAgentProvider(latency=0.0, test_pass_rate=1.0)

        lp, hu, gd = _patches()
        with lp, hu, gd:
            run_pipeline(
                task="add a comment",
                project_dir=str(project),
                output_dir=run_dir,
                provider=provider,
                profile_name="task",
                worktree_config_override={"enabled": False},
            )

        assert not (run_dir / "checkout").exists()


# ── W1–W2: isolation enabled with real git repo ────────────────────────────


class TestWorktreeEnabled:
    def test_dirty_checkout_halts_before_worktree_creation_by_default(
        self, tmp_path: Path,
    ) -> None:
        project = tmp_path / "src"
        _init_git_repo(project)
        (project / "src.py").write_text("x = 2\n", encoding="utf-8")
        run_dir = tmp_path / "run" / "20260522_dirty_halt"
        run_dir.mkdir(parents=True)
        provider = MockAgentProvider(latency=0.0, test_pass_rate=1.0)

        lp, hu, gd = _patches()
        with lp, hu, gd:
            session = run_pipeline(
                task="continue current change",
                project_dir=str(project),
                output_dir=run_dir,
                provider=provider,
                profile_name="feature",
                no_interactive=True,
                worktree_config_override={
                    "enabled": True,
                    "isolation": "per_run",
                    "retention_days": 7,
                    "allow_destructive_inside": True,
                },
            )

        assert session["status"] == "halted"
        assert session["halt_reason"] == "pre_run_dirty_halt"
        assert session["pre_run_dirty"]["action"] == "halt"
        assert not (run_dir / "checkout").exists()

    def test_include_halts_if_worktree_resolution_degrades(
        self, tmp_path: Path,
    ) -> None:
        project = tmp_path / "src"
        _init_git_repo(project)
        run_dir = tmp_path / "run" / "20260522_dirty_degraded"
        run_dir.mkdir(parents=True)
        provider = MockAgentProvider(latency=0.0, test_pass_rate=1.0)

        from pipeline.engine.pre_run_dirty import PreRunDirtyIntake
        from pipeline.engine.worktree import WorktreeContext

        dirty_include = PreRunDirtyIntake(
            action="include",
            status="seed_pending",
            dirty=True,
            source_head="HEAD",
            changed_paths=("src.py",),
        )
        degraded = WorktreeContext(
            mode="off",
            project_dir=project,
            path=project,
            base_ref="HEAD",
            degraded_reason="forced test degradation",
        )

        lp, hu, gd = _patches()
        with lp, hu, gd, patch(
            "pipeline.engine.pre_run_dirty.resolve_pre_run_dirty_intake",
            return_value=dirty_include,
        ), patch(
            "pipeline.engine.worktree.resolve_worktree_for_run",
            return_value=degraded,
        ):
            session = run_pipeline(
                task="continue current change",
                project_dir=str(project),
                output_dir=run_dir,
                provider=provider,
                profile_name="feature",
                no_interactive=True,
                worktree_config_override={
                    "enabled": True,
                    "isolation": "per_run",
                    "retention_days": 7,
                    "allow_destructive_inside": True,
                },
            )

        assert session["status"] == "halted"
        assert session["halt_reason"] == "pre_run_dirty_seed_failed"
        assert session["pre_run_dirty"]["status"] == "seed_failed"
        assert not (run_dir / "checkout").exists()

    def test_meta_worktree_mode_is_per_run(self, tmp_path: Path) -> None:
        project = tmp_path / "src"
        _init_git_repo(project)
        run_dir = tmp_path / "run" / "20260522_003"
        run_dir.mkdir(parents=True)
        provider = MockAgentProvider(latency=0.0, test_pass_rate=1.0)

        lp, hu, gd = _patches()
        with lp, hu, gd:
            session = run_pipeline(
                task="add a comment",
                project_dir=str(project),
                output_dir=run_dir,
                provider=provider,
                profile_name="feature",
                worktree_config_override={
                    "enabled": True,
                    "isolation": "per_run",
                    "retention_days": 7,
                    "allow_destructive_inside": True,
                },
            )

        wt = session.get("worktree", {})
        assert wt.get("isolation") == "per_run"
        assert wt.get("path") is not None

    def test_meta_json_worktree_block_present(self, tmp_path: Path) -> None:
        project = tmp_path / "src"
        _init_git_repo(project)
        run_dir = tmp_path / "run" / "20260522_004"
        run_dir.mkdir(parents=True)
        provider = MockAgentProvider(latency=0.0, test_pass_rate=1.0)

        lp, hu, gd = _patches()
        with lp, hu, gd:
            run_pipeline(
                task="add a comment",
                project_dir=str(project),
                output_dir=run_dir,
                provider=provider,
                profile_name="feature",
                worktree_config_override={
                    "enabled": True,
                    "isolation": "per_run",
                    "retention_days": 7,
                    "allow_destructive_inside": True,
                },
            )

        meta = json.loads((run_dir / "meta.json").read_text())
        assert "worktree" in meta
        assert meta["worktree"]["isolation"] == "per_run"

    # W5 (ADR 0062): workspace-config git_dir — nested git root is respected
    def test_workspace_git_dir_creates_worktree_from_nested_root(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """When workspace config registers a project with {path, git_dir}, the
        actual git repo is a subdirectory. The worktree must be created from
        that nested root, not from the outer project_path.

        This test exercises the REAL read path through config.local.json and
        load_workspace_project_git_dir without mocking that function — any
        regression in config parsing or path matching will make this red.

        ADR 0062: git_dir lives in workspace config, not plugin.py.
        """
        project = tmp_path / "mono"
        project.mkdir()
        git_subdir = project / "src"
        _init_git_repo(git_subdir)  # git repo lives under project/src

        # Write a real workspace config with the object-form {path, git_dir}.
        workspace_dir = tmp_path / "workspace-orchestrator"
        config_dir = workspace_dir / ".orcho"
        config_dir.mkdir(parents=True)
        (config_dir / "config.local.json").write_text(
            json.dumps({"projects": {
                "mono": {"path": str(project.resolve()), "git_dir": "src"},
            }}),
            encoding="utf-8",
        )
        # Point the workspace resolver at our test workspace.
        monkeypatch.setenv("ORCHO_WORKSPACE", str(workspace_dir))

        run_dir = tmp_path / "run" / "20260522_006"
        run_dir.mkdir(parents=True)
        provider = MockAgentProvider(latency=0.0, test_pass_rate=1.0)

        lp = patch("pipeline.project.session_run.load_plugin", return_value=PLUGIN)
        hu = patch("core.io.git_helpers.has_uncommitted", return_value=False)
        gd = patch("core.io.git_helpers.git_diff_stat", return_value="0 files")
        with lp, hu, gd:
            session = run_pipeline(
                task="add a comment",
                project_dir=str(project),
                output_dir=run_dir,
                provider=provider,
                profile_name="feature",
                worktree_config_override={
                    "enabled": True,
                    "isolation": "per_run",
                    "retention_days": 7,
                    "allow_destructive_inside": True,
                },
            )

        wt = session.get("worktree", {})
        # Must be isolated — not degraded to off because project_path (mono/)
        # is not a git repo itself; the fix routes the probe to project/src
        # via the real workspace config.
        assert wt.get("isolation") == "per_run", (
            f"Expected per_run isolation; got {wt}. "
            "The workspace git_dir fix may not be wiring the nested root correctly."
        )
        checkout_path = wt.get("path", "")
        assert checkout_path.endswith("checkout"), (
            f"Expected checkout path to end with 'checkout'; got {checkout_path}"
        )

    # R1 regression: diff captured from worktree, not project_path
    def test_real_implement_change_captured_and_delivered_on_approve(
        self, tmp_path: Path,
    ) -> None:
        """finalize() must read diff from the isolated checkout, not project_path.

        Injects a file mutation into the worktree right after it is created
        (before finalize runs capture_run_diff). Asserts that diff.patch in
        the run dir contains the mutation — proving capture_run_diff was
        called with the worktree path, not the original source dir. The
        default commit delivery then approves that same run-owned diff into
        the project checkout and commits it.
        """
        project = tmp_path / "src"
        _init_git_repo(project)
        old_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        run_dir = tmp_path / "run" / "20260522_r1"
        run_dir.mkdir(parents=True)
        provider = MockAgentProvider(latency=0.0, test_pass_rate=1.0)

        from pipeline.engine.worktree import resolve_worktree_for_run as _real_resolve

        def _resolve_and_mutate(**kwargs):
            ctx = _real_resolve(**kwargs)
            if ctx.is_isolated:
                # Modify a tracked file so git diff --no-color picks it up.
                (ctx.path / "src.py").write_text(
                    "x = 1\n# R1-regression-marker\n", encoding="utf-8",
                )
            return ctx

        lp, hu, gd = _patches()
        with lp, hu, gd, patch(
            "pipeline.engine.worktree.resolve_worktree_for_run",
            side_effect=_resolve_and_mutate,
        ):
            run_pipeline(
                task="add a comment",
                project_dir=str(project),
                output_dir=run_dir,
                provider=provider,
                profile_name="feature",
                worktree_config_override={
                    "enabled": True,
                    "isolation": "per_run",
                    "retention_days": 7,
                    "allow_destructive_inside": True,
                },
            )

        patch_file = run_dir / "diff.patch"
        assert patch_file.exists(), "diff.patch was not written to run_dir"
        diff_text = patch_file.read_text(encoding="utf-8")
        assert "R1-regression-marker" in diff_text, (
            f"Expected worktree file change in diff.patch; got:\n{diff_text[:800]}"
        )
        new_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert new_head != old_head
        assert status == ""
        assert (project / "src.py").read_text(encoding="utf-8") == (
            "x = 1\n# R1-regression-marker\n"
        )
        artifacts = list((run_dir / "commit_decisions").glob("*.json"))
        assert len(artifacts) == 1
        artifact = json.loads(artifacts[0].read_text(encoding="utf-8"))
        assert artifact["commit_status"] == "committed"

    def test_untracked_implement_file_delivered_on_approve(
        self, tmp_path: Path,
    ) -> None:
        project = tmp_path / "src"
        _init_git_repo(project)
        run_dir = tmp_path / "run" / "20260522_untracked"
        run_dir.mkdir(parents=True)
        provider = MockAgentProvider(latency=0.0, test_pass_rate=1.0)

        from pipeline.engine.worktree import resolve_worktree_for_run as _real_resolve

        def _resolve_and_create_untracked(**kwargs):
            ctx = _real_resolve(**kwargs)
            if ctx.is_isolated:
                (ctx.path / "created.py").write_text(
                    "created = True\n", encoding="utf-8",
                )
            return ctx

        lp, hu, gd = _patches()
        with lp, hu, gd, patch(
            "pipeline.engine.worktree.resolve_worktree_for_run",
            side_effect=_resolve_and_create_untracked,
        ):
            run_pipeline(
                task="create a file",
                project_dir=str(project),
                output_dir=run_dir,
                provider=provider,
                profile_name="feature",
                worktree_config_override={
                    "enabled": True,
                    "isolation": "per_run",
                    "retention_days": 7,
                    "allow_destructive_inside": True,
                },
            )

        assert (project / "created.py").read_text(encoding="utf-8") == (
            "created = True\n"
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert status == ""
        files = subprocess.run(
            ["git", "show", "--name-only", "--format=", "HEAD"],
            cwd=project,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        assert "created.py" in files
        artifact = json.loads(
            next((run_dir / "commit_decisions").glob("*.json")).read_text(
                encoding="utf-8",
            )
        )
        assert artifact["commit_status"] == "committed"
        assert "created.py" in artifact["untracked_delivered"]

    def test_agent_prompts_anchor_to_isolated_checkout(self, tmp_path: Path) -> None:
        """Prompt-visible project path must match the isolated agent cwd.

        A real runtime may use absolute paths from the plan. If prompt builders
        keep advertising the user's source checkout while agent ``cwd`` points at
        the run worktree, implement can mutate the source tree and leave the
        review target clean.
        """
        project = tmp_path / "src"
        _init_git_repo(project)
        run_dir = tmp_path / "run" / "20260522_prompt_anchor"
        run_dir.mkdir(parents=True)
        provider = MockAgentProvider(latency=0.0, test_pass_rate=1.0)
        captured: list[tuple[str, str, str]] = []

        from agents.runtimes._strategy import _MockClaude, _MockCodex

        real_claude_invoke = _MockClaude.invoke
        real_codex_invoke = _MockCodex.invoke

        def spy_claude_invoke(self, prompt, cwd="", **kw):
            captured.append(("claude", prompt, cwd))
            return real_claude_invoke(self, prompt, cwd, **kw)

        def spy_codex_invoke(self, prompt, cwd="", **kw):
            captured.append(("codex", prompt, cwd))
            return real_codex_invoke(self, prompt, cwd, **kw)

        lp, hu, gd = _patches()
        with lp, hu, gd, patch.object(_MockClaude, "invoke", spy_claude_invoke), patch.object(
            _MockCodex, "invoke", spy_codex_invoke,
        ):
            run_pipeline(
                task="add a comment",
                project_dir=str(project),
                output_dir=run_dir,
                provider=provider,
                profile_name="feature",
                worktree_config_override={
                    "enabled": True,
                    "isolation": "per_run",
                    "retention_days": 7,
                    "allow_destructive_inside": True,
                },
            )

        session_path = run_dir / "meta.json"
        session = json.loads(session_path.read_text(encoding="utf-8"))
        checkout = session["worktree"]["path"]
        anchored_prompts = [
            (runtime, prompt, cwd)
            for runtime, prompt, cwd in captured
            if "You are working in an isolated git worktree checkout at:" in prompt
        ]
        assert anchored_prompts, "Expected at least one project-anchored prompt"
        for runtime, prompt, cwd in anchored_prompts:
            assert cwd == checkout, f"{runtime} invoked outside checkout: {cwd}"
            assert (
                f"You are working in an isolated git worktree checkout at: {checkout}"
                in prompt
            )
            assert (
                f"You are working directly in the project directory at: {project}"
                not in prompt
            )

    # R2 regression: resume reuses existing worktree instead of degrading to off
    def test_resume_retained_worktree_stays_isolated(self, tmp_path: Path) -> None:
        """resolve_worktree_for_run must detect an existing valid worktree on resume.

        First run creates and retains the worktree (retain=True is the finalize
        default). Second run resumes via resume_from; the resolver detects the
        existing checkout, skips create_worktree, and returns mode='per_run' so
        meta.json shows isolation='per_run' — not the degraded 'off' that happened
        before the R2 fix.
        """
        project = tmp_path / "src"
        _init_git_repo(project)
        run_dir = tmp_path / "run" / "20260522_r2"
        run_dir.mkdir(parents=True)
        provider = MockAgentProvider(latency=0.0, test_pass_rate=1.0)

        run_id = "20260522_r2_resume_test"
        wt_cfg = {
            "enabled": True,
            "isolation": "per_run",
            "retention_days": 7,
            "allow_destructive_inside": True,
        }

        lp, hu, gd = _patches()
        with lp, hu, gd, patch.dict(os.environ, {"ORCHO_RUN_ID": run_id}):
            session1 = run_pipeline(
                task="add a comment",
                project_dir=str(project),
                output_dir=run_dir,
                provider=provider,
                profile_name="feature",
                worktree_config_override=wt_cfg,
            )

        checkout = Path(session1["worktree"]["path"])
        assert checkout.exists(), "First run must retain worktree checkout"
        assert session1.get("worktree", {}).get("isolation") == "per_run"

        with lp, hu, gd:
            session2 = run_pipeline(
                task="add a comment",
                project_dir=str(project),
                output_dir=run_dir,
                provider=provider,
                profile_name="feature",
                resume_from=run_id,
                worktree_config_override=wt_cfg,
            )

        wt2 = session2.get("worktree", {})
        assert wt2.get("isolation") == "per_run", (
            f"Resumed run degraded to '{wt2.get('isolation')}'; "
            f"expected 'per_run'. degraded_reason={wt2.get('degraded_reason')}"
        )
        assert wt2.get("degraded_reason") is None, (
            f"Resumed run should not have degraded_reason; got {wt2.get('degraded_reason')}"
        )

    # W4: user source checkout untouched
    def test_user_checkout_bytes_unchanged(self, tmp_path: Path) -> None:
        project = tmp_path / "src"
        _init_git_repo(project)
        src_bytes_before = (project / "src.py").read_bytes()

        run_dir = tmp_path / "run" / "20260522_005"
        run_dir.mkdir(parents=True)
        provider = MockAgentProvider(latency=0.0, test_pass_rate=1.0)

        lp, hu, gd = _patches()
        with lp, hu, gd:
            run_pipeline(
                task="add a comment",
                project_dir=str(project),
                output_dir=run_dir,
                provider=provider,
                profile_name="feature",
                worktree_config_override={
                    "enabled": True,
                    "isolation": "per_run",
                    "retention_days": 7,
                    "allow_destructive_inside": True,
                },
            )

        assert (project / "src.py").read_bytes() == src_bytes_before

    def test_dirty_target_pauses_delivery_non_interactive(
        self, tmp_path: Path,
    ) -> None:
        """B1.2 e2e: project_dir starts clean (no pre_run_dirty trip).
        After the worktree resolver runs, an unrelated dirty file
        appears in project_dir. By the time finalize calls commit
        delivery, the target-dirty guard fires and the session halts
        with the dedicated ``commit_delivery_target_dirty`` reason —
        distinct from the operator-chosen ``commit_decision_halt``
        and the executor-error ``commit_delivery_failed``.
        """
        project = tmp_path / "src"
        _init_git_repo(project)
        run_dir = tmp_path / "run" / "20260525_target_dirty"
        run_dir.mkdir(parents=True)
        provider = MockAgentProvider(latency=0.0, test_pass_rate=1.0)

        from pipeline.engine import worktree as worktree_mod

        real_resolve = worktree_mod.resolve_worktree_for_run

        def resolve_then_dirty(**kwargs):
            ctx = real_resolve(**kwargs)
            # pre_run_dirty has already finished (saw a clean checkout
            # and returned ``clean``). Inject the parallel dirty work
            # *now*, so finalize → _run_commit_delivery sees it.
            (project / "parallel.txt").write_text(
                "operator-owned change\n", encoding="utf-8",
            )
            return ctx

        lp, hu, gd = _patches()
        with lp, hu, gd, patch(
            "pipeline.engine.worktree.resolve_worktree_for_run",
            side_effect=resolve_then_dirty,
        ):
            session = run_pipeline(
                task="add a comment",
                project_dir=str(project),
                output_dir=run_dir,
                provider=provider,
                profile_name="feature",
                no_interactive=True,
                worktree_config_override={
                    "enabled": True,
                    "isolation": "per_run",
                    "retention_days": 7,
                    "allow_destructive_inside": True,
                },
            )

        assert session["status"] == "halted"
        assert session["halt_reason"] == "commit_delivery_target_dirty"
        delivery = session["commit_delivery"]
        assert delivery["status"] == "target_dirty"
        assert any(
            "parallel.txt" in line for line in delivery["target_dirty_paths"]
        )
        # The operator-owned dirty file survives untouched.
        assert (project / "parallel.txt").read_text(encoding="utf-8") == (
            "operator-owned change\n"
        )
        # Project's tracked files were not mutated by delivery.
        head_files = subprocess.run(
            ["git", "show", "--name-only", "--format=", "HEAD"],
            cwd=project, capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        assert "parallel.txt" not in head_files
        # Audit artifact persisted with the right shape.
        artifacts = list((run_dir / "commit_decisions").glob("*.json"))
        assert len(artifacts) == 1
        artifact = json.loads(artifacts[0].read_text(encoding="utf-8"))
        assert artifact["commit_status"] == "target_dirty"
        assert artifact["commit_sha"] is None
        assert artifact["commit_error"] is None
        assert any(
            "parallel.txt" in line for line in artifact["target_dirty_paths"]
        )


# ── ADR 0058: cross worktree degrade is loud; enabled=false stays legal ─────


class TestCrossWorktreeDegradeIsLoud:
    """A cross child (``parent_run_id`` set) must NOT silently run in the
    source checkout when isolation was requested but degraded — it raises so
    the cross dispatch records the alias as failed. A *mono* run keeps the
    in-place fallback (operator-tolerable). An explicit
    ``worktree.enabled=false`` is a clean off and stays legal for cross.
    """

    def _degraded_ctx(self, project: Path):
        from pipeline.engine.worktree import WorktreeContext
        return WorktreeContext(
            mode="off",
            project_dir=project,
            path=project,
            base_ref="HEAD",
            degraded_reason="git worktree add failed (branch already in use)",
        )

    def test_cross_child_degrade_raises_loudly(self, tmp_path: Path) -> None:
        from pipeline.engine.worktree import WorktreeConfigError

        project = tmp_path / "src"
        _init_git_repo(project)
        run_dir = tmp_path / "runs" / "cross_ts" / "web"
        run_dir.mkdir(parents=True)
        provider = MockAgentProvider(latency=0.0, test_pass_rate=1.0)

        lp, hu, gd = _patches()
        with lp, hu, gd, patch(
            "pipeline.engine.worktree.resolve_worktree_for_run",
            return_value=self._degraded_ctx(project),
        ), pytest.raises(WorktreeConfigError) as exc:
            run_pipeline(
                task="edit users",
                project_dir=str(project),
                output_dir=run_dir,
                provider=provider,
                profile_name="task",
                no_interactive=True,
                # Cross child markers — this is what flips degrade to fatal.
                parent_run_id="cross_ts",
                project_alias="web",
                worktree_config_override={
                    "enabled": True, "isolation": "per_run", "retention_days": 7,
                },
            )
        # The failure names the alias and carries the degraded reason — the
        # cross dispatch surfaces this as a failed alias, never source edits.
        msg = str(exc.value)
        assert "web" in msg
        assert "branch already in use" in msg

    def test_mono_run_keeps_in_place_fallback_on_degrade(self, tmp_path: Path) -> None:
        """No ``parent_run_id`` → mono. A degraded worktree falls back to
        in-place (the pre-existing operator-tolerable behavior); it must NOT
        raise the cross-only guard."""
        project = tmp_path / "src"
        _init_git_repo(project)
        run_dir = tmp_path / "run" / "mono_ts"
        run_dir.mkdir(parents=True)
        provider = MockAgentProvider(latency=0.0, test_pass_rate=1.0)

        lp, hu, gd = _patches()
        with lp, hu, gd, patch(
            "pipeline.engine.worktree.resolve_worktree_for_run",
            return_value=self._degraded_ctx(project),
        ):
            session = run_pipeline(
                task="edit users",
                project_dir=str(project),
                output_dir=run_dir,
                provider=provider,
                profile_name="task",
                no_interactive=True,
                worktree_config_override={
                    "enabled": True, "isolation": "per_run", "retention_days": 7,
                },
            )
        # Mono proceeded (no cross guard); it did not raise.
        assert isinstance(session, dict)

    def test_cross_child_enabled_false_is_legal(self, tmp_path: Path) -> None:
        """``worktree.enabled=false`` is a clean off (no degraded_reason), so
        the cross guard does NOT fire — explicit off stays a legal mode even
        for a cross child. Real resolver (no patch) returns off cleanly."""
        project = tmp_path / "src"
        _init_git_repo(project)
        run_dir = tmp_path / "runs" / "cross_ts2" / "web"
        run_dir.mkdir(parents=True)
        provider = MockAgentProvider(latency=0.0, test_pass_rate=1.0)

        lp, hu, gd = _patches()
        with lp, hu, gd:
            session = run_pipeline(
                task="edit users",
                project_dir=str(project),
                output_dir=run_dir,
                provider=provider,
                profile_name="task",
                no_interactive=True,
                parent_run_id="cross_ts2",
                project_alias="web",
                worktree_config_override={"enabled": False},
            )
        # Clean off — no raise, isolation reported off.
        assert session.get("worktree", {}).get("isolation") == "off"
