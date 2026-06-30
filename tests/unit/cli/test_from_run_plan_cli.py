"""CLI surface for ``--from-run-plan``.

Covers the user-facing slice of PR4: the ``orcho run`` parser must
accept ``--from-run-plan RUN_ID_OR_DIR``; ``build_orch_argv`` must
forward it as a single ``--from-run-plan <spec>`` pair; the orchestrator
``main()`` must reject the combination of ``--resume`` and
``--from-run-plan`` with a clear diagnostic.

The actual hydration / projection behaviour (state.parsed_plan,
profile projection, plan_source="run") is covered by the integration
test ``tests/integration/test_from_run_plan.py`` and the unit tests
in ``tests/unit/pipeline/`` — this file pins only the CLI plumbing.
"""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path
from typing import Any

import pytest

from pipeline.argv import build_orch_argv

# ── argv builder pass-through ───────────────────────────────────────────────


class TestArgvBuilder:
    """``build_orch_argv`` must emit ``--from-run-plan <spec>`` exactly
    once and only when the kwarg is set."""

    def test_emits_flag_with_run_id(self) -> None:
        argv = build_orch_argv(
            project="/tmp/p",
            task="t",
            from_run_plan="20260523_120000",
        )
        assert "--from-run-plan" in argv
        assert argv[argv.index("--from-run-plan") + 1] == "20260523_120000"

    def test_emits_flag_with_explicit_path(self) -> None:
        argv = build_orch_argv(
            project="/tmp/p",
            task="t",
            from_run_plan="/abs/path/to/run",
        )
        assert "--from-run-plan" in argv
        assert argv[argv.index("--from-run-plan") + 1] == "/abs/path/to/run"

    def test_default_omits_flag(self) -> None:
        argv = build_orch_argv(project="/tmp/p", task="t")
        assert "--from-run-plan" not in argv

    def test_empty_string_omits_flag(self) -> None:
        """Defensive: an empty string should not produce a phantom
        ``--from-run-plan ""`` pair on the wire."""
        argv = build_orch_argv(
            project="/tmp/p", task="t", from_run_plan="",
        )
        assert "--from-run-plan" not in argv


# ── orchestrator argparse: --resume / --from-run-plan mutex ─────────────────


class TestOrchestratorMutex:
    """``pipeline.project_orchestrator.main`` must reject combinations
    of ``--resume`` and ``--from-run-plan`` before any IO happens.
    Otherwise we'd be ambiguous about whether to continue the same
    run from a checkpoint or to spawn a new run from a parent's
    plan."""

    def _run_main_capture(
        self, argv: list[str],
    ) -> tuple[int, str, str]:
        """Invoke ``main()`` with synthetic argv; capture stdout/stderr
        and the SystemExit code. Returns ``(rc, stdout, stderr)``."""
        from pipeline.project.cli import main as _cli_main
        saved_argv = sys.argv
        sys.argv = ["orchestrator", *argv]
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(
                err_buf,
            ):
                try:
                    _cli_main()
                except SystemExit as e:
                    rc = int(e.code or 0)
                else:
                    rc = 0
        finally:
            sys.argv = saved_argv
        return rc, out_buf.getvalue(), err_buf.getvalue()

    def test_resume_plus_from_run_plan_rejected_with_clear_message(
        self,
    ) -> None:
        rc, _, err = self._run_main_capture([
            "--resume", "20260101_000000",
            "--from-run-plan", "20260102_000000",
            "--task", "demo",
            "--project", "/tmp/nonexistent",
        ])
        # Must exit with rc=2 (operator error) and surface a message
        # that names both flags so the operator can fix the call.
        assert rc == 2
        assert "--resume" in err and "--from-run-plan" in err
        assert "mutually exclusive" in err

    def test_from_run_plan_plus_profile_plan_rejected_up_front(
        self,
    ) -> None:
        """``--profile planning`` is the plan-only profile; --from-run-plan
        already inherits a plan, so the two combined leave the child
        run with nothing to execute. The orchestrator must reject this
        BEFORE workspace resolve / task prompt so the operator does
        not have to type a task only to hit a downstream
        ValueError("profile consists entirely of planning phases").
        """
        rc, _, err = self._run_main_capture([
            "--from-run-plan", "20260102_000000",
            "--profile", "planning",
            "--task", "demo",
            "--project", "/tmp/nonexistent",
        ])
        assert rc == 2
        assert "--from-run-plan" in err and "--profile planning" in err
        assert "contradictory" in err
        # Message must name an actionable alternative.
        assert "feature" in err or "complex_feature" in err

    def test_from_run_plan_plus_profile_review_rejected_up_front(
        self,
    ) -> None:
        """``--profile delivery_audit`` reviews the working tree and has no
        planning / implementation phases — there is nothing for the
        inherited plan to feed into. Same fail-fast policy as the
        plan-only-profile case."""
        rc, _, err = self._run_main_capture([
            "--from-run-plan", "20260102_000000",
            "--profile", "delivery_audit",
            "--task", "demo",
            "--project", "/tmp/nonexistent",
        ])
        assert rc == 2
        assert "--from-run-plan" in err and "--profile delivery_audit" in err
        assert "contradictory" in err

    def test_from_run_plan_plus_profile_advanced_passes_guard(
        self,
    ) -> None:
        """The fail-fast guard must NOT trip for compatible profiles —
        feature / complex_feature / task all have phases downstream of
        the planning block. The run will still fail later (the
        parent path is bogus), but it must fail with the parent
        resolver's diagnostic, not the contradictory-combo guard.
        """
        rc, _, err = self._run_main_capture([
            "--from-run-plan", "/tmp/does/not/exist",
            "--profile", "feature",
            "--task", "demo",
            "--project", "/tmp/nonexistent",
        ])
        # Still exits with operator error rc=2, but the message must
        # come from the parent resolver (not the combo guard).
        assert rc == 2
        assert "contradictory" not in err

    def test_env_override_to_plan_caught_by_guard(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``ORCHO_PIPELINE=planning`` env override + ``--profile feature``
        + ``--from-run-plan`` would silently bypass the raw-args guard
        (which only saw feature) and crash deeper in the projection
        helper. After PR4.7 the guard routes through the same
        ``_resolve_profile_name`` resolver the rest of the orchestrator
        uses, so the env override is caught and the message names the
        env source so the operator fixes the right thing."""
        monkeypatch.setenv("ORCHO_PIPELINE", "planning")
        rc, _, err = self._run_main_capture([
            "--from-run-plan", "20260524_120000",
            "--profile", "feature",  # raw arg looks compatible
            "--task", "demo",
            "--project", "/tmp/nonexistent",
        ])
        assert rc == 2
        # The error must surface the env source, not the harmless
        # --profile flag value, so the operator knows where to look.
        assert "ORCHO_PIPELINE=planning" in err
        assert "contradictory" in err
        # Still names an actionable alternative.
        assert "feature" in err or "complex_feature" in err

    def test_env_override_to_review_caught_by_guard(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sibling case: ORCHO_PIPELINE=delivery_audit through the same path."""
        monkeypatch.setenv("ORCHO_PIPELINE", "delivery_audit")
        rc, _, err = self._run_main_capture([
            "--from-run-plan", "20260524_120000",
            "--profile", "feature",
            "--task", "demo",
            "--project", "/tmp/nonexistent",
        ])
        assert rc == 2
        assert "ORCHO_PIPELINE=delivery_audit" in err
        assert "contradictory" in err

    def test_env_override_to_advanced_passes_guard(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Negative control: ORCHO_PIPELINE pointing at a compatible
        profile (feature) must NOT trip the guard, even if --profile
        is explicitly something incompatible — the env wins per
        existing _resolve_profile_name semantics."""
        monkeypatch.setenv("ORCHO_PIPELINE", "feature")
        rc, _, err = self._run_main_capture([
            "--from-run-plan", "/tmp/does/not/exist",
            "--profile", "planning",  # would be caught WITHOUT env override
            "--task", "demo",
            "--project", "/tmp/nonexistent",
        ])
        # Still rc=2 because parent path is bogus, but the failure
        # must come from the parent resolver — not the combo guard.
        assert rc == 2
        assert "contradictory" not in err


# ── Task / project inheritance from parent meta ─────────────────────────────


class TestTaskProjectInheritance:
    """``--from-run-plan`` without explicit ``--task`` / ``--project``
    inherits both from the parent run's ``meta.json``. Explicit
    operator-supplied values always win (no silent override of
    intent).

    These tests drive the orchestrator's ``main()`` with synthetic
    argv + a synthetic parent run on disk (parsed_plan.json + meta.json)
    so the inheritance pathway is exercised in the same shape an
    operator would hit it from the CLI. The parent is built without
    spinning up a real pipeline run — that would conflate this test
    with the integration test for hydration. We only need the two
    files the inheritance reads.

    **Hermetic workspace isolation**: every test pins
    ``ORCHO_WORKSPACE`` / ``ORCHO_RUNSPACE`` to ``tmp_path`` via the
    ``isolated_workspace`` fixture below. Without it, ``main()``'s
    workspace-derivation block falls back to the ambient
    ``$ORCHO_WORKSPACE`` (or cwd walk-up) and tries to materialize
    a run dir in someone else's filesystem — reviewer hit
    ``PermissionError`` on
    ``/Users/.../workspace-orchestrator/...`` running on his machine.
    The isolation makes the tests reproducible across hosts.
    """

    @pytest.fixture(autouse=True)
    def isolated_workspace(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> Path:
        """Pin workspace env to tmp_path for every test in this class.

        ``--mock --dry-run`` short-circuits agent invocation, but
        ``main()`` still resolves a workspace BEFORE that. Without
        this fixture the resolution walks up cwd or reads the host's
        ``$ORCHO_WORKSPACE`` — both leak host state into the test.
        """
        runspace = tmp_path / "runspace"
        (runspace / "runs").mkdir(parents=True)
        neutral_cwd = tmp_path / "neutral-cwd"
        neutral_cwd.mkdir()
        monkeypatch.setenv("ORCHO_WORKSPACE", str(tmp_path))
        monkeypatch.setenv("ORCHO_RUNSPACE", str(runspace))
        # The project CLI intentionally lets cwd walk-up beat ambient env so a
        # real operator standing inside a workspace gets the physical context.
        # Tests must not run from the developer's checkout, otherwise walk-up
        # can discover the real /Users/.../workspace-orchestrator and write
        # synthetic run metadata there.
        monkeypatch.chdir(neutral_cwd)
        # Reset the AppConfig cache so the new env vars take effect
        # immediately for this test's orchestrator invocation.
        from core.infra import config as _config
        _config._reset_config()
        return tmp_path

    def _seed_parent(
        self,
        tmp_path: Path,
        *,
        run_id: str = "20260524_120000",
        task: str = "Add structured logging",
        project: str | None = None,
    ) -> Path:
        """Create ``<tmp>/runs/<run_id>/`` with the minimum artefacts
        the orchestrator's --from-run-plan path reads:
        ``parsed_plan.json`` (mandatory; resolver checks it) and
        ``meta.json`` (mandatory for inheritance; older runs may
        lack it, but inheritance tests assume it's there).

        Returned path is absolute — tests pass it directly to
        ``--from-run-plan`` as a path-shaped spec, bypassing the
        runs_dir lookup (no need to fake a worktree layout)."""
        import json as _json

        from agents.entities import SubTask
        from pipeline.plan_artifacts import write_parsed_plan_artifact
        from pipeline.plan_parser import ParsedPlan

        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        run_dir = runs_dir / run_id
        run_dir.mkdir()
        plan = ParsedPlan(
            short_summary="Seed plan",
            planning_context="Seed.",
            subtasks=(
                SubTask(id="t1", goal="Seed subtask"),
            ),
            source="json",
        )
        write_parsed_plan_artifact(run_dir, plan, attempt=1)
        meta: dict[str, Any] = {
            "task": task,
            "project": project or str(tmp_path / "fake_project"),
            "status": "awaiting_phase_handoff",
            "phases": {},
        }
        (run_dir / "meta.json").write_text(
            _json.dumps(meta), encoding="utf-8",
        )
        return run_dir

    def _run_main_capture(
        self, argv: list[str],
    ) -> tuple[int, str, str]:
        """Same shape as ``TestOrchestratorMutex._run_main_capture`` —
        duplicated locally so the two test classes stay independent."""
        from pipeline.project.cli import main as _cli_main
        saved_argv = sys.argv
        sys.argv = ["orchestrator", *argv]
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(
                err_buf,
            ):
                try:
                    _cli_main()
                except SystemExit as e:
                    rc = int(e.code or 0)
                else:
                    rc = 0
        finally:
            sys.argv = saved_argv
        assert "workspace overridden:" not in out_buf.getvalue()
        assert "workspace overridden:" not in err_buf.getvalue()
        return rc, out_buf.getvalue(), err_buf.getvalue()

    def test_task_inherits_from_parent_meta_when_not_supplied(
        self, tmp_path: Path,
    ) -> None:
        """Operator runs ``orcho run --from-run-plan <id> --project <p>``
        with NO ``--task``. Inheritance kicks in: task is taken from
        parent meta.json, run proceeds (we don't actually run agents,
        we trip on a downstream check, but the inheritance line must
        appear in stdout BEFORE that)."""
        parent_dir = self._seed_parent(
            tmp_path,
            task="Add structured logging from inherited task",
        )
        explicit_project = tmp_path / "explicit_project"
        explicit_project.mkdir()
        # Workspace points at tmp_path so find_runs_dir locates our
        # synthetic ``runs/`` subdir. No --task: must inherit.
        rc, out, err = self._run_main_capture([
            "--from-run-plan", str(parent_dir),
            "--profile", "feature",
            "--project", str(explicit_project),
            "--mock",
            "--dry-run",
        ])
        # Inheritance line is the load-bearing assertion. The run may
        # bail later (mocked env, no full pipeline), but the line
        # MUST appear before any failure.
        combined = out + err
        assert "task inherited from parent run" in combined
        assert parent_dir.name in combined
        # Sanity: not the project-inheritance line (--project was
        # supplied explicitly).
        assert "project inherited from parent" not in combined

    def test_project_inherits_from_parent_meta_when_not_supplied(
        self, tmp_path: Path,
    ) -> None:
        """Sibling case: --task supplied, --project omitted →
        project inherited from parent meta."""
        explicit_parent_project = tmp_path / "parent_project"
        explicit_parent_project.mkdir()
        parent_dir = self._seed_parent(
            tmp_path,
            task="Task explicit on child",
            project=str(explicit_parent_project),
        )
        rc, out, err = self._run_main_capture([
            "--from-run-plan", str(parent_dir),
            "--profile", "feature",
            "--task", "Explicit operator task wins over inheritance",
            "--mock",
            "--dry-run",
        ])
        combined = out + err
        assert "project inherited from parent" in combined
        assert str(explicit_parent_project) in combined
        # --task was explicit; no task-inheritance line.
        assert "task inherited from parent" not in combined

    def test_explicit_task_wins_over_parent_meta(
        self, tmp_path: Path,
    ) -> None:
        """Inheritance is a fallback, not an override. When operator
        supplies ``--task X`` and parent meta has ``task: Y``, the
        run uses X. Same precedence the documented invariant promises."""
        parent_dir = self._seed_parent(
            tmp_path,
            task="Parent task that must NOT be used",
        )
        explicit_project = tmp_path / "explicit_project_2"
        explicit_project.mkdir()
        rc, out, err = self._run_main_capture([
            "--from-run-plan", parent_dir.name,
            "--profile", "feature",
            "--task", "Explicit task that operator chose",
            "--project", str(explicit_project),
            "--workspace", str(tmp_path),
            "--mock",
            "--dry-run",
        ])
        combined = out + err
        # No inheritance lines at all — both were explicit.
        assert "task inherited from parent" not in combined
        assert "project inherited from parent" not in combined
        # And the parent's task string MUST NOT appear in the run's
        # transcript (would indicate the explicit override was lost).
        assert "Parent task that must NOT be used" not in combined


# ── CLI parser: --from-run-plan is registered on ``orcho run`` ─────────────


class TestCliParser:
    """``cli/orcho.py`` registers ``--from-run-plan`` on the ``run``
    subcommand so ``orcho run --from-run-plan X`` parses without
    error and the namespace carries the value."""

    def _parse_run_args(self, *argv: str) -> Any:
        from cli.orcho import build_parser
        parser = build_parser()
        return parser.parse_args(["run", *argv])

    def test_from_run_plan_argument_parses(self) -> None:
        args = self._parse_run_args(
            "--from-run-plan", "20260523_120000",
            "--task", "demo",
            "--project", "/tmp/p",
        )
        assert getattr(args, "from_run_plan", None) == "20260523_120000"

    def test_default_from_run_plan_is_none(self) -> None:
        args = self._parse_run_args("--task", "demo", "--project", "/tmp/p")
        assert getattr(args, "from_run_plan", None) is None

    def test_from_run_plan_accepts_path(self) -> None:
        args = self._parse_run_args(
            "--from-run-plan", "/abs/path/to/parent",
            "--task", "demo",
            "--project", "/tmp/p",
        )
        assert args.from_run_plan == "/abs/path/to/parent"


# ── implicit plan-only follow-up: contradictory child profile ───────────────


class TestImplicitPlanOnlyFollowupContradictoryProfile:
    """A plain follow-up (``--resume <plan-only-parent> --task ...``, NO
    explicit ``--from-run-plan``) whose child profile has no implement / review
    phases downstream of planning must be rejected on the CLI surface with rc=2
    and a clear operator message — never a traceback.

    This is the implicit counterpart of the explicit ``--from-run-plan``
    contradictory-profile guard: the promotion chokepoint raises
    ``FollowupPlanContinuationError`` deep inside ``run_pipeline``, and the CLI
    must catch it next to the other operator errors.
    """

    @pytest.fixture(autouse=True)
    def isolated_workspace(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> Path:
        """Pin workspace env to tmp_path so ``main()`` never touches host state
        (mirrors ``TestTaskProjectInheritance.isolated_workspace``)."""
        runspace = tmp_path / "runspace"
        (runspace / "runs").mkdir(parents=True)
        neutral_cwd = tmp_path / "neutral-cwd"
        neutral_cwd.mkdir()
        monkeypatch.setenv("ORCHO_WORKSPACE", str(tmp_path))
        monkeypatch.setenv("ORCHO_RUNSPACE", str(runspace))
        monkeypatch.chdir(neutral_cwd)
        from core.infra import config as _config
        _config._reset_config()
        return tmp_path

    def _seed_plan_only_parent(
        self, tmp_path: Path, project: Path, *, run_id: str,
    ) -> Path:
        """Seed a plan-only parent run under ``runspace/runs/<run_id>``: a
        persisted ``parsed_plan.json``, an ``worktree_isolation=off`` worktree
        block (the shared source checkout, no isolated worktree), and no
        undelivered diff — the exact shape a ``planning`` run leaves behind."""
        import json as _json

        from agents.entities import SubTask
        from pipeline.plan_artifacts import write_parsed_plan_artifact
        from pipeline.plan_parser import ParsedPlan

        run_dir = tmp_path / "runspace" / "runs" / run_id
        run_dir.mkdir(parents=True)
        plan = ParsedPlan(
            short_summary="Seed plan",
            planning_context="Seed.",
            subtasks=(SubTask(id="t1", goal="Seed subtask"),),
            source="json",
        )
        write_parsed_plan_artifact(run_dir, plan, attempt=1)
        (run_dir / "meta.json").write_text(
            _json.dumps({
                "task": "produce a plan",
                "project": str(project),
                "status": "done",
                "phases": {},
                "worktree": {"isolation": "off", "path": str(project)},
            }),
            encoding="utf-8",
        )
        return run_dir

    def _run_main_capture(self, argv: list[str]) -> tuple[int, str, str]:
        """Invoke ``main()`` with synthetic argv; capture stdout/stderr and the
        SystemExit code (same shape as ``TestOrchestratorMutex``)."""
        from pipeline.project.cli import main as _cli_main
        saved_argv = sys.argv
        sys.argv = ["orchestrator", *argv]
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(
                err_buf,
            ):
                try:
                    _cli_main()
                except SystemExit as e:
                    rc = int(e.code or 0)
                else:
                    rc = 0
        finally:
            sys.argv = saved_argv
        return rc, out_buf.getvalue(), err_buf.getvalue()

    @pytest.mark.parametrize(
        "profile_name", ["planning", "research", "code_review"],
    )
    def test_plain_followup_contradictory_profile_exits_rc2(
        self, tmp_path: Path, profile_name: str,
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        run_id = "20260524_120000"
        self._seed_plan_only_parent(tmp_path, project, run_id=run_id)

        rc, _out, err = self._run_main_capture([
            "--resume", run_id,
            "--task", "implement it",
            "--project", str(project),
            "--profile", profile_name,
            "--no-interactive",
            "--mock",
        ])

        # Operator error, not a crash: rc=2 with a clear message that names the
        # offending profile and the actionable remediation.
        assert rc == 2, f"expected rc=2, got {rc} (stderr={err!r})"
        assert profile_name in err
        assert "implement / review" in err
        # No legacy profile names leak into the diagnostic.
        for legacy in ("advanced", "lite", "enterprise"):
            assert legacy not in err
