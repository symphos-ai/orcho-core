"""
M1.6 CLI Polish tests.

Tests cover the `ma` CLI facade (cli/ma.py) without running real agents:
 * Parser: all subcommands parse correctly
 * cmd_status: reads meta.json + metrics.json from tmp run dirs
 * cmd_metrics: reads metrics.json, formats table
 * cmd_history: lists run dirs sorted newest-first
 * cmd_prompts: resolution chain display, --list flag
 * No tests for cmd_run / cmd_cross — these delegate to orchestrators
 which are covered in integration tests.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ── Runs-dir resolution lives in sdk.runs.find_runs_dir; tested in
#  tests/sdk/test_runs.py and tests/sdk/test_resolution_order.py
# ──────────────────────────────────────────────────────────────────────────


# ── Test helpers ──────────────────────────────────────────────────────────────

def _write_run(
    runs_dir: Path,
    run_id: str,
    *,
    task: str = "Test task",
    project: str = "/projects/test",
    status: str = "done",
    profile: str = "feature",
    phases: dict | None = None,
    tokens_in: int = 1000,
    tokens_out: int = 2000,
    duration_s: float = 10.0,
    rounds: int = 0,
    cost: float | None = None,
    cost_estimated: bool = False,
) -> Path:
    """Create a fake run directory with meta.json and metrics.json."""
    d = runs_dir / run_id
    d.mkdir(parents=True)

    (d / "meta.json").write_text(json.dumps({
        "task": task,
        "project": project,
        "status": status,
        "profile": profile,
        "timestamp": f"2026-05-02T{run_id[-6:-4]}:{run_id[-4:-2]}:00",
        "phases": phases or {"plan": {}, "implement": {}},
    }))

    metrics: dict = {
        "total_tokens_in": tokens_in,
        "total_tokens_out": tokens_out,
        "total_tokens": tokens_in + tokens_out,
        "total_duration_s": duration_s,
        "phases": {
            "plan":  {"model": "opus",   "tokens_in": tokens_in // 2,
                      "tokens_out": tokens_out // 2, "total_tokens": (tokens_in + tokens_out) // 2,
                      "duration_s": duration_s / 2},
            "implement": {"model": "sonnet", "tokens_in": tokens_in // 2,
                      "tokens_out": tokens_out // 2, "total_tokens": (tokens_in + tokens_out) // 2,
                      "duration_s": duration_s / 2},
        },
    }
    if rounds:
        metrics["total_rounds"] = rounds
    if cost is not None:
        metrics["total_cost_usd_equivalent"] = cost
        if cost_estimated:
            metrics["cost_estimated"] = True
        for phase in metrics["phases"].values():
            phase["cost_usd_equivalent"] = cost / 2.0
            if cost_estimated:
                phase["cost_estimated"] = True

    (d / "metrics.json").write_text(json.dumps(metrics))
    return d


def _break_cli_binary_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.infra import config

    def fail(name: str):
        raise AssertionError(f"unexpected {name} CLI binary lookup")

    monkeypatch.setattr(config, "get_claude_bin", lambda: fail("claude"))
    monkeypatch.setattr(config, "get_codex_bin", lambda: fail("codex"))


# ─────────────────────────────────────────────────────────────────────────────
# Parser tests
# ─────────────────────────────────────────────────────────────────────────────

class TestParser:
    @pytest.fixture(autouse=True)
    def _import_parser(self):
        from cli.orcho import build_parser
        self.build_parser = build_parser

    def test_no_command_parsed(self) -> None:
        parser = self.build_parser()
        args = parser.parse_args([])
        assert args.command is None

    def test_run_subcommand(self) -> None:
        parser = self.build_parser()
        args = parser.parse_args(["run", "--task", "Do X", "--project", "/p"])
        assert args.command == "run"
        assert args.task == "Do X"
        assert args.project == "/p"

    def test_run_task_file(self) -> None:
        parser = self.build_parser()
        args = parser.parse_args(["run", "--task-file", "task.md", "--project", "/p"])
        assert args.task_file == "task.md"

    def test_run_defaults(self) -> None:
        parser = self.build_parser()
        args = parser.parse_args(["run", "--task", "x", "--project", "/p"])
        assert args.max_rounds == 1
        assert args.mock is False
        assert args.dry_run is False
        # Default transcript mode comes from ``config.cli_output_mode()``
        # which reads ``cli.output_mode`` from config.defaults.json
        # (currently "live"); ORCHO_OUTPUT_MODE or local config overlays
        # can change this per-environment.
        assert args.output == "live"
        # ``--profile`` defaults to None: orchestrator resolves to
        # "feature" for fresh runs and to ``meta.profile`` on
        # ``--resume`` (inherit semantics). Explicit ``--profile`` flag
        # overrides both. See pipeline.control.resume_context.
        # resolve_resume_profile.
        assert args.profile is None
        assert args.session_mode == "auto"

    def test_run_runtime_flags_accept_extension_runtime(self) -> None:
        parser = self.build_parser()
        args = parser.parse_args([
            "run",
            "--task", "x",
            "--project", "/p",
            "--runtime-implement", "claude-glm",
        ])
        assert args.runtime_implement == "claude-glm"

    def test_runtimes_install_subcommand(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        parser = self.build_parser()
        destination = tmp_path / "bin" / "claude-glm"
        args = parser.parse_args([
            "runtimes",
            "install",
            "claude-glm",
            "--path",
            str(destination),
        ])

        assert args.command == "runtimes"
        assert args.runtimes_cmd == "install"
        assert args.func(args) == 0
        assert destination.exists()
        assert "Installed claude-glm wrapper" in capsys.readouterr().out

    def test_runtimes_install_refuses_existing_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        parser = self.build_parser()
        destination = tmp_path / "claude-glm"
        destination.write_text("custom\n")
        args = parser.parse_args([
            "runtimes",
            "install",
            "claude-glm",
            "--path",
            str(destination),
        ])

        assert args.func(args) == 2
        assert destination.read_text() == "custom\n"
        assert "pass --force" in capsys.readouterr().err

    def test_demos_bootstrap_subcommand(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        parser = self.build_parser()
        args = parser.parse_args([
            "demos",
            "bootstrap",
            "golden-api",
            "--root",
            str(tmp_path / "demo"),
        ])

        assert args.command == "demos"
        assert args.demos_cmd == "bootstrap"
        assert args.func(args) == 0
        out = capsys.readouterr().out
        assert "DEMO golden-api workspace ready." in out
        assert "orcho run" in out
        assert "--profile feature" in out
        assert (tmp_path / "demo" / "project").is_dir()

    def test_demos_install_alias(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        parser = self.build_parser()
        args = parser.parse_args([
            "demos",
            "install",
            "golden-api",
            "--root",
            str(tmp_path / "demo"),
        ])

        assert args.command == "demos"
        assert args.demos_cmd == "install"
        assert args.func(args) == 0
        assert "DEMO golden-api workspace ready." in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("group", "func_name"),
        [
            ("profiles", "cmd_profiles_list"),
            ("pricing", "cmd_pricing_show"),
            ("workflows", "cmd_workflows_list"),
        ],
    )
    def test_bare_listing_group_defaults_to_action(
        self, group: str, func_name: str,
    ) -> None:
        # A subcommand group whose obvious bare action is a listing/show must
        # not dead-end in argparse — bare `orcho <group>` resolves to that
        # action instead of `error: arguments are required` (exit 2).
        parser = self.build_parser()
        args = parser.parse_args([group])
        assert args.command == group
        assert args.func.__name__ == func_name

    @pytest.mark.parametrize("group", ["profile", "runtimes", "demos", "workspace"])
    def test_bare_arg_only_group_prints_help_clean(
        self, group: str, capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Groups whose subcommands all need arguments print their own help and
        # exit 0 on bare invocation, rather than argparse's exit-2 dead-end.
        parser = self.build_parser()
        args = parser.parse_args([group])
        assert args.command == group
        assert args.func(args) == 0
        assert f"orcho {group}" in capsys.readouterr().out

    def test_run_all_flags(self) -> None:
        parser = self.build_parser()
        args = parser.parse_args([
            "run", "--task", "X", "--project", "/p",
            "--max-rounds", "3", "--mock", "--dry-run", "--verbose",
            "--stream-output",
            "--profile", "plan", "--session-mode", "chain",
            "--session-split", "implement=common",
            "--session-split", "repair_changes=common",
            "--model-plan", "opus", "--model-implement", "sonnet",
        ])
        assert args.max_rounds == 3
        assert args.mock is True
        assert args.output == "live"
        assert args.profile == "plan"  #  was --mode plan
        assert args.session_mode == "chain"
        assert args.session_split == ["implement=common", "repair_changes=common"]
        assert args.model_plan == "opus"

    def test_run_output_flag_last_wins(self) -> None:
        parser = self.build_parser()
        args = parser.parse_args([
            "run", "--task", "X", "--project", "/p",
            "--stream-output", "--output", "debug", "--verbose",
            "--output", "summary",
        ])
        assert args.output == "summary"

    def test_run_output_aliases(self) -> None:
        parser = self.build_parser()
        live = parser.parse_args(["run", "--task", "X", "--project", "/p", "--stream-output"])
        debug = parser.parse_args(["run", "--task", "X", "--project", "/p", "-v"])
        assert live.output == "live"
        assert debug.output == "debug"

    def test_evidence_debug_flag(self) -> None:
        parser = self.build_parser()
        args = parser.parse_args(["evidence", "--format", "md", "--debug"])
        assert args.command == "evidence"
        assert args.format == "md"
        assert args.debug is True

    def test_evidence_default_format_is_cli(self) -> None:
        parser = self.build_parser()
        args = parser.parse_args(["evidence"])
        assert args.command == "evidence"
        assert args.format == "cli"
        assert args.view == "summary"

    def test_evidence_full_view_flag(self) -> None:
        parser = self.build_parser()
        args = parser.parse_args(["evidence", "--view", "full"])
        assert args.command == "evidence"
        assert args.format == "cli"
        assert args.view == "full"

    def test_profiles_list_does_not_resolve_cli_binaries(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        _break_cli_binary_lookup(monkeypatch)
        parser = self.build_parser()
        args = parser.parse_args(["profiles", "list"])

        assert args.func(args) == 0
        out = capsys.readouterr().out
        assert "Profiles" in out
        assert "feature" in out
        assert "Mode" in out
        assert "Worktree" in out
        assert "Production-grade dev cycle" not in out
        assert "orcho profiles list --verbose" in out

    def test_profiles_list_verbose_shows_descriptions(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        _break_cli_binary_lookup(monkeypatch)
        parser = self.build_parser()
        args = parser.parse_args(["profiles", "list", "--verbose"])

        assert args.func(args) == 0
        out = capsys.readouterr().out
        assert "feature" in out
        assert "Production-grade dev cycle" in out
        assert "orcho profiles list --verbose" not in out

    def test_profile_customize_writes_workspace_overlay(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _break_cli_binary_lookup(monkeypatch)
        workspace = tmp_path / "workspace"
        monkeypatch.setenv("ORCHO_WORKSPACE", str(workspace))
        parser = self.build_parser()
        args = parser.parse_args([
            "profile",
            "customize",
            "feature",
            "--mode",
            "pro",
            "--phase-effort",
            "implement=high",
        ])

        assert args.func(args) == 0
        out = capsys.readouterr().out
        assert "Updated profile customization for feature" in out
        data = json.loads(
            (workspace / ".orcho" / "config.local.json").read_text(
                encoding="utf-8",
            )
        )
        assert data["profiles_v2"]["feature"]["_profile"]["default_mode"] == "pro"
        assert data["profiles_v2"]["feature"]["implement"]["effort"] == "high"

    def test_workflows_list_does_not_resolve_cli_binaries(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        _break_cli_binary_lookup(monkeypatch)
        parser = self.build_parser()
        args = parser.parse_args(["workflows", "list"])

        assert args.func(args) == 0
        out = capsys.readouterr().out
        assert "Workflows" in out
        assert "Workflow" in out
        assert "task" in out
        assert "orcho workflows list --verbose" in out
        assert "orcho profiles list --verbose" not in out

    def test_top_level_help_documents_output_modes(self) -> None:
        parser = self.build_parser()
        help_text = parser.format_help()

        assert "Start here:" in help_text
        assert "Workflows:" in help_text
        assert "Output modes:" in help_text
        assert "status        What is happening / what should I do next?" in help_text
        assert "evidence      What happened / what proves it?" in help_text
        assert "metrics/cost  How much did it consume?" in help_text
        assert "diff          What changed?" in help_text
        assert "--output live" in help_text
        assert "--output debug" in help_text
        assert "orcho help --verbose" in help_text
        assert "ma help" not in help_text
        assert "REA-" not in help_text
        assert "Phase " not in help_text

    def test_command_groups_cover_every_subcommand(self) -> None:
        from cli._help import COMMAND_GROUPS

        parser = self.build_parser()
        sub_choices = set(parser._subparsers._group_actions[0].choices)
        grouped = {
            name for _, commands in COMMAND_GROUPS for name, _ in commands
        }
        # ``help`` is service-only and lives in the "More help" section, not
        # in a command group. ``web`` and ``tui`` are interface commands
        # intentionally hidden from the advertised listing until their packages
        # ship on PyPI (still registered + callable, just not advertised so a
        # new user is never pointed at an uninstallable ``pip install``).
        # Every OTHER subcommand must still be categorized.
        assert grouped == sub_choices - {"help", "web", "tui"}

    def test_onboarding_lists_every_command_grouped(self) -> None:
        from cli._help import COMMAND_GROUPS, QUICK_HELP
        from core.io.ansi import strip_ansi

        onboarding = strip_ansi(QUICK_HELP)
        for _, commands in COMMAND_GROUPS:
            for name, _desc in commands:
                assert name in onboarding, name

    def test_run_help_documents_output_aliases(self) -> None:
        parser = self.build_parser()
        run_parser = parser._subparsers._group_actions[0].choices["run"]
        help_text = run_parser.format_help()

        assert "Task input:" in help_text
        assert "Workspace and resume:" in help_text
        assert "Output modes:" in help_text
        assert "--stream-output    same as --output live" in help_text
        assert "--verbose, -v      same as --output debug" in help_text
        assert "last one on the CLI wins" in help_text

    def test_help_command_prints_onboarding_first(
        self,
        capsys: pytest.CaptureFixture,
    ) -> None:
        parser = self.build_parser()
        args = parser.parse_args(["help"])
        rc = args.func(args)
        out = capsys.readouterr().out

        assert rc == 0
        assert out.startswith(
            "Orcho — local-first control plane for AI software delivery"
        )
        assert "Start here:" in out
        assert "usage: orcho" not in out

    def test_cross_subcommand(self) -> None:
        parser = self.build_parser()
        args = parser.parse_args([
            "cross", "--task", "X", "--projects", "unity:/u", "api:/a",
            "--profile", "lite",
        ])
        assert args.command == "cross"
        assert args.projects == ["unity:/u", "api:/a"]
        assert args.profile == "lite"

    def test_tui_subcommand(self) -> None:
        parser = self.build_parser()
        args = parser.parse_args(["tui", "--run-dir", "/x", "--follow"])
        assert args.command == "tui"
        assert args.run_dir == "/x"
        assert args.follow is True
        assert args.replay is False
        from cli.orcho import cmd_tui
        assert args.func is cmd_tui

    def test_tui_follow_replay_mutually_exclusive(self) -> None:
        parser = self.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["tui", "--follow", "--replay"])

    def test_status_no_run_id(self) -> None:
        parser = self.build_parser()
        args = parser.parse_args(["status"])
        assert args.run_id is None

    def test_status_with_run_id(self) -> None:
        parser = self.build_parser()
        args = parser.parse_args(["status", "20260502_100000"])
        assert args.run_id == "20260502_100000"

    def test_metrics_defaults(self) -> None:
        parser = self.build_parser()
        args = parser.parse_args(["metrics"])
        assert args.last == 10
        assert args.run_id is None

    def test_metrics_with_options(self) -> None:
        parser = self.build_parser()
        args = parser.parse_args(["metrics", "--last", "20"])
        assert args.last == 20

    def test_metrics_last_rejects_placeholder(self, capsys: pytest.CaptureFixture[str]) -> None:
        parser = self.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["metrics", "-n", "LAST"])

        err = capsys.readouterr().err
        assert "expected a number" in err
        assert "`COUNT` is a placeholder" in err

    def test_history_defaults(self) -> None:
        parser = self.build_parser()
        args = parser.parse_args(["history"])
        assert args.last == 10

    def test_history_last_rejects_placeholder(self, capsys: pytest.CaptureFixture[str]) -> None:
        parser = self.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["history", "-n", "LAST"])

        err = capsys.readouterr().err
        assert "expected a number" in err
        assert "`COUNT` is a placeholder" in err

    def test_prompts_name(self) -> None:
        parser = self.build_parser()
        args = parser.parse_args(["prompts", "tasks/build"])
        assert args.name == "tasks/build"
        assert args.list is False

    def test_prompts_list_flag(self) -> None:
        parser = self.build_parser()
        args = parser.parse_args(["prompts", "--list"])
        assert args.list is True

    def test_run_task_and_task_file_mutually_exclusive(self) -> None:
        parser = self.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["run", "--task", "x", "--task-file", "f.md", "--project", "/p"])

    def test_version_flag_prints_versions_and_exits_zero(
        self,
        capsys: pytest.CaptureFixture,
    ) -> None:
        parser = self.build_parser()
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_args(["--version"])
        assert excinfo.value.code == 0
        out = capsys.readouterr().out
        assert "orcho-core" in out


# ─────────────────────────────────────────────────────────────────────────────
# --version string composition
# ─────────────────────────────────────────────────────────────────────────────

class TestVersionString:
    @staticmethod
    def _fake_version(known: dict[str, str]):
        from importlib import metadata

        def version(name: str) -> str:
            try:
                return known[name]
            except KeyError:
                raise metadata.PackageNotFoundError(name) from None

        return version

    def test_core_and_mcp_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cli import orcho

        monkeypatch.setattr(
            orcho.metadata, "version",
            self._fake_version({"orcho-core": "1.2.3", "orcho-mcp": "4.5.6"}),
        )
        assert orcho._version_string() == "orcho-core 1.2.3\norcho-mcp 4.5.6"

    def test_mcp_absent_is_omitted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cli import orcho

        monkeypatch.setattr(
            orcho.metadata, "version",
            self._fake_version({"orcho-core": "1.2.3"}),
        )
        assert orcho._version_string() == "orcho-core 1.2.3"

    def test_core_metadata_missing_degrades_gracefully(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cli import orcho

        monkeypatch.setattr(
            orcho.metadata, "version", self._fake_version({}),
        )
        out = orcho._version_string()
        assert out.startswith("orcho-core ")
        assert "package metadata not found" in out


# ─────────────────────────────────────────────────────────────────────────────
# cmd_run facade
# ─────────────────────────────────────────────────────────────────────────────

class TestCmdRunFacade:
    def test_missing_short_task_file_exits_before_profile_prompt(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        from cli import orcho

        project = tmp_path / "project"
        project.mkdir()
        args = orcho.build_parser().parse_args([
            "run",
            "--task-file", "missing.md",
            "--project", str(project),
        ])
        require_profile = MagicMock(return_value=None)
        run_pipeline = MagicMock(return_value=0)
        monkeypatch.setattr(orcho, "require_profile_or_exit", require_profile)
        monkeypatch.setattr(orcho, "run_pipeline_from_args", run_pipeline)

        rc = orcho.cmd_run(args)

        assert rc == 1
        captured = capsys.readouterr()
        assert "--task-file short name not found: missing.md" in captured.err
        assert ".orcho/.task-files" in captured.err
        assert "--task-file ./missing.md" in captured.err
        require_profile.assert_not_called()
        run_pipeline.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# cmd_status
# ─────────────────────────────────────────────────────────────────────────────

class TestCmdStatus:
    @pytest.fixture
    def runs_dir(self, tmp_path: Path, monkeypatch):
        rd = tmp_path / "runs"
        rd.mkdir()
        monkeypatch.setenv("ORCHO_RUNSPACE", str(tmp_path))
        return rd

    def test_status_last_run(self, runs_dir: Path, capsys) -> None:
        from cli.orcho import cmd_status
        _write_run(runs_dir, "20260502_100000", task="My task", status="done")
        args = _make_args(run_id=None)
        rc = cmd_status(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "20260502_100000" in out
        assert "My task" in out
        assert "done" in out

    def test_status_specific_run(self, runs_dir: Path, capsys) -> None:
        from cli.orcho import cmd_status
        _write_run(runs_dir, "20260501_080000", task="Old task")
        _write_run(runs_dir, "20260502_100000", task="New task")
        args = _make_args(run_id="20260501_080000")
        rc = cmd_status(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Old task" in out

    def test_status_shows_metrics(self, runs_dir: Path, capsys) -> None:
        from cli.orcho import cmd_status
        _write_run(runs_dir, "20260502_100000", tokens_in=5000, tokens_out=10000)
        args = _make_args(run_id=None)
        cmd_status(args)
        out = capsys.readouterr().out
        assert "15,000" in out  # total tokens

    def test_status_no_runs(self, runs_dir: Path, capsys) -> None:
        from cli.orcho import cmd_status
        args = _make_args(run_id=None)
        rc = cmd_status(args)
        assert rc == 1

    def test_status_unknown_run_id(self, runs_dir: Path, capsys) -> None:
        from cli.orcho import cmd_status
        _write_run(runs_dir, "20260502_100000")
        args = _make_args(run_id="20260101_000000")
        rc = cmd_status(args)
        assert rc == 1

    def test_status_shows_rounds_when_nonzero(self, runs_dir: Path, capsys) -> None:
        from cli.orcho import cmd_status
        _write_run(runs_dir, "20260502_100000", rounds=2)
        args = _make_args(run_id=None)
        cmd_status(args)
        out = capsys.readouterr().out
        assert "Rounds:  2" in out


# ─────────────────────────────────────────────────────────────────────────────
# cmd_metrics
# ─────────────────────────────────────────────────────────────────────────────

class TestCmdMetrics:
    @pytest.fixture
    def runs_dir(self, tmp_path: Path, monkeypatch):
        rd = tmp_path / "runs"
        rd.mkdir()
        monkeypatch.setenv("ORCHO_RUNSPACE", str(tmp_path))
        return rd

    def test_metrics_table_for_multiple_runs(self, runs_dir: Path, capsys) -> None:
        from cli.orcho import cmd_metrics
        _write_run(runs_dir, "20260501_000000", tokens_in=1000, tokens_out=2000, duration_s=10.0)
        _write_run(runs_dir, "20260502_000000", tokens_in=3000, tokens_out=6000, duration_s=20.0)
        args = _make_args(last=10)
        rc = cmd_metrics(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "20260501_000000" in out
        assert "20260502_000000" in out

    def test_metrics_history_aligns_long_ids_and_cost(
        self,
        runs_dir: Path,
        capsys,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from cli.orcho import cmd_metrics
        from core.infra import config

        monkeypatch.setenv("ORCHO_ACCOUNTING", "1")
        config._reset_config()
        try:
            _write_run(
                runs_dir,
                "20260707_162649_347471",
                project="/repo/demo_project",
                task="tool-handler smoke with enough text to clip",
                tokens_in=10_000,
                tokens_out=972,
                duration_s=0.8,
                rounds=1,
                cost=1.23,
                cost_estimated=True,
            )
            args = _make_args(last=10)
            rc = cmd_metrics(args)
        finally:
            config._reset_config()

        out = capsys.readouterr().out
        assert rc == 0
        assert "Metrics history · last 1 runs" in out
        assert "Cost ref" in out
        assert "estimated-api ~$1.23" in out
        assert "20260707_162649_347471   demo_project" in out

    def test_metrics_history_color_can_be_forced(self, tmp_path: Path) -> None:
        from cli._formatters import format_metrics_history
        from core.io.ansi import set_color_enabled, strip_ansi
        from sdk.types import RunMetrics

        run_dir = tmp_path / "runs" / "20260707_162649_347471"
        run_dir.mkdir(parents=True)
        (run_dir / "meta.json").write_text(
            json.dumps(
                {
                    "project": "/repo/orcho-core",
                    "task": "color metrics",
                }
            ),
            encoding="utf-8",
        )
        row = RunMetrics(
            run_id="20260707_162649_347471",
            run_dir=run_dir,
            total_tokens=10,
            total_duration_s=1.0,
            total_rounds=1,
            total_cost_usd_equivalent=1.23,
            raw={"total_cost_usd_equivalent": 1.23, "cost_estimated": True},
        )

        try:
            set_color_enabled(True)
            rendered = format_metrics_history([row])
        finally:
            set_color_enabled(None)

        assert "\x1b[" in rendered
        plain = strip_ansi(rendered)
        assert "Metrics history · last 1 runs" in plain
        assert "Cost ref" in plain
        assert "estimated-api ~$1.23" in plain

    def test_metrics_single_run_detail(self, runs_dir: Path, capsys) -> None:
        from cli.orcho import cmd_metrics
        _write_run(
            runs_dir,
            "20260502_100000",
            tokens_in=5000,
            tokens_out=10000,
            cost=2.0,
        )
        args = _make_args(run_id="20260502_100000")
        rc = cmd_metrics(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "5,000" in out
        assert "10,000" in out

    def test_metrics_empty_runs_dir(self, runs_dir: Path, capsys) -> None:
        from cli.orcho import cmd_metrics
        args = _make_args(last=10)
        rc = cmd_metrics(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "No runs" in out

    def test_metrics_last_n(self, runs_dir: Path, capsys) -> None:
        from cli.orcho import cmd_metrics
        for i in range(5):
            _write_run(runs_dir, f"2026050{i}_000000")
        args = _make_args(last=3)
        cmd_metrics(args)
        out = capsys.readouterr().out
        # Table should show 3 runs in summary line
        assert "3 runs" in out


# ─────────────────────────────────────────────────────────────────────────────
# cmd_history
# ─────────────────────────────────────────────────────────────────────────────

class TestCmdHistory:
    @pytest.fixture
    def runs_dir(self, tmp_path: Path, monkeypatch):
        rd = tmp_path / "runs"
        rd.mkdir()
        monkeypatch.setenv("ORCHO_RUNSPACE", str(tmp_path))
        return rd

    def test_history_lists_runs(self, runs_dir: Path, capsys) -> None:
        from cli.orcho import cmd_history
        _write_run(runs_dir, "20260501_000000", task="Old task")
        _write_run(runs_dir, "20260502_000000", task="New task")
        args = _make_args(last=10)
        rc = cmd_history(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Run history · last 2 shown" in out
        assert "20260501_000000" in out
        assert "20260502_000000" in out
        assert "orcho status <run-id>" in out

    def test_history_sorted_newest_first(self, runs_dir: Path, capsys) -> None:
        from cli.orcho import cmd_history
        _write_run(runs_dir, "20260501_000000")
        _write_run(runs_dir, "20260503_000000")
        _write_run(runs_dir, "20260502_000000")
        args = _make_args(last=10)
        cmd_history(args)
        out = capsys.readouterr().out
        lines = [line for line in out.splitlines() if "2026050" in line]
        assert lines[0].startswith("  20260503")  # newest first

    def test_history_last_n(self, runs_dir: Path, capsys) -> None:
        from cli.orcho import cmd_history
        for i in range(5):
            _write_run(runs_dir, f"2026050{i}_000000")
        args = _make_args(last=2)
        cmd_history(args)
        out = capsys.readouterr().out
        lines = [line for line in out.splitlines() if "2026050" in line]
        assert len(lines) == 2

    def test_history_no_meta_shows_placeholder(self, runs_dir: Path, capsys) -> None:
        from cli.orcho import cmd_history
        # Dir without meta.json
        (runs_dir / "20260502_bad").mkdir()
        args = _make_args(last=10)
        cmd_history(args)
        out = capsys.readouterr().out
        assert "no meta.json" in out

    def test_history_color_can_be_forced(self, runs_dir: Path, capsys) -> None:
        from cli.orcho import cmd_history
        from core.io.ansi import C, set_color_enabled

        _write_run(runs_dir, "20260502_000000", status="done")
        args = _make_args(last=10)

        try:
            set_color_enabled(True)
            cmd_history(args)
            colored = capsys.readouterr().out
        finally:
            set_color_enabled(None)

        assert f"{C.GREEN}done" in colored


# ─────────────────────────────────────────────────────────────────────────────
# cmd_prompts
# ─────────────────────────────────────────────────────────────────────────────

class TestCmdPrompts:
    def test_list_flag(self, capsys) -> None:
        from cli.orcho import cmd_prompts
        args = _make_args(name=None, list=True, project=None)
        rc = cmd_prompts(args)
        out = capsys.readouterr().out
        assert rc == 0
        # Listing should surface composable parts (post-ADR-0022 catalog).
        assert "tasks/implement" in out
        assert "Prompt catalog" in out
        assert "Formats" in out
        assert "Roles" in out
        assert "Tasks" in out

    def test_no_name_no_list_shows_summary(self, capsys) -> None:
        from cli.orcho import cmd_prompts
        args = _make_args(name=None, list=False, project=None)
        rc = cmd_prompts(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Prompt catalog" in out
        assert "Groups" in out
        assert "orcho prompts --list" in out
        assert "tasks/implement" not in out

    def test_resolution_chain_core_only(self, capsys) -> None:
        from cli.orcho import cmd_prompts
        args = _make_args(name="tasks/implement", list=False, project=None)
        rc = cmd_prompts(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "core" in out
        assert "→ Using: [core]" in out

    def test_resolution_chain_with_project(self, tmp_path: Path, capsys) -> None:
        from cli.orcho import cmd_prompts
        # Project override at composable-parts path wins over core.
        override_dir = tmp_path / ".orcho" / "multiagent" / "prompts" / "tasks"
        override_dir.mkdir(parents=True)
        (override_dir / "implement.md").write_text("$task")
        args = _make_args(name="tasks/implement", list=False, project=str(tmp_path))
        rc = cmd_prompts(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "project" in out
        assert "→ Using: [project]" in out

    def test_verbose_shows_content(self, capsys) -> None:
        # ADR 0028 / M10.5 Step 2: tasks/*.md files are pure static
        # method prose with no ``$<var>`` placeholders. Assert on a
        # stable phrase from the implement method.
        from cli.orcho import cmd_prompts
        args = _make_args(name="tasks/implement", list=False, project=None, verbose=True)
        rc = cmd_prompts(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "→ Using: [core]" in out
        assert "════════════════════════════════════════" in out
        assert "Implement the task end-to-end" in out

    def test_unknown_prompt_returns_1(self, capsys) -> None:
        from cli.orcho import cmd_prompts
        args = _make_args(name="nonexistent_prompt_xyz", list=False, project=None)
        rc = cmd_prompts(args)
        assert rc == 1

    def test_list_includes_composable_parts_and_legacy_set(self) -> None:
        """ADR 0009 baseline guard for `orcho prompts --list`.

 Pins the exact catalog membership so every later prompt-family
 migration commit produces a clean intentional diff against this
 set. When a part is added or a legacy flat name is retired,
 update both this baseline and the migrating family commit
 together.

 Reads through the canonical SDK enumerator; ``cli.orcho``
 re-exports the same symbol.
 """
        from sdk import list_prompts

        expected = frozenset({
            # Roles (Phase Q1a professional taxonomy).
            "roles/code_reviewer",
            "roles/implementation_engineer",
            "roles/plan_reviewer",
            "roles/release_manager",
            "roles/systems_architect",
            "roles/product_owner",
            # Tasks (ADR 0022 workflow-semantic phase taxonomy).
            "tasks/code_review",
            "tasks/implement",
            "tasks/repair_changes",
            "tasks/final_acceptance",
            # ADR 0085: correction profile entry-gate triage procedure.
            "tasks/correction_triage",
            # ADR 0124: interactive phase-handoff advisor procedure.
            "tasks/handoff_advice",
            "tasks/validate_plan",
            "tasks/plan",
            "tasks/replan",
            "tasks/decompose",
            "tasks/hypothesis",
            "tasks/readonly_plan",
            "tasks/validate_hypothesis",
            "tasks/review_uncommitted",
            "tasks/cross_plan",
            "tasks/cross_validate_plan",
            "tasks/cross_replan",
            "tasks/cross_contract_bundle",
            # ADR 0025 Phase 3: cross runner's system release gate.
            "tasks/cross_final_acceptance",
            # ADR 0032: prose framing for commit-decision gate llm_generate strategy.
            "tasks/commit_message",
            # Generic role-agnostic presentation presets.
            "formats/terse",
            "formats/compact",
            "formats/detailed",
            "formats/bullets",
            "formats/handoff",
        })
        actual = frozenset(list_prompts())
        assert actual == expected, (
            "Shipped prompt catalog drifted from ADR 0009 baseline.\n"
            f"  added:   {sorted(actual - expected)}\n"
            f"  removed: {sorted(expected - actual)}\n"
            "Update the EXPECTED set above in the same commit that "
            "migrates the affected prompt family."
        )


# ─────────────────────────────────────────────────────────────────────────────
# cmd_evidence
# ─────────────────────────────────────────────────────────────────────────────

class TestCmdEvidence:
    def test_cli_format_is_default_operator_summary(self, monkeypatch) -> None:
        import cli.orcho as orcho

        body = {
            "run_id": "R",
            "run_dir": "/tmp/runs/R",
            "schema_version": "1",
            "status": "done",
            "task": "Ship the thing",
            "profile": "feature",
            "plan": {
                "source": "json",
                "short_summary": "Small useful summary.",
                "planning_context": "P" * 1000,
                "subtask_count": 2,
                "has_contract": True,
                "acceptance_criteria": ["a"],
                "owned_files": ["cli.py"],
                "commands_to_run": ["pytest -q"],
            },
            "phases": [
                {"name": "PLAN", "title": "PLAN", "outcome": "ok", "attempt": 1},
                {
                    "name": "VALIDATE_PLAN",
                    "title": "validate",
                    "outcome": "skipped",
                    "attempt": 1,
                },
            ],
            "gates": [
                {
                    "name": "tests",
                    "kind": "computational",
                    "outcome": "skipped",
                    "duration_s": 0.0,
                }
            ],
            "commands": [],
            "artifacts": [],
            "metrics": {
                "total_tokens": 100,
                "total_tokens_in": 70,
                "total_tokens_out": 30,
                "total_duration_s": 1.5,
                "total_rounds": 1,
            },
            "errors": [],
            "findings": [],
        }
        fake_stdout = _FakeStdout(is_tty=False)
        monkeypatch.setattr(orcho.sys, "stdout", fake_stdout)
        monkeypatch.setattr(orcho, "collect_evidence", lambda *a, **k: _make_args(body=body))

        rc = orcho.cmd_evidence(_make_args(run_id=None, workspace=None, out=None))

        out = fake_stdout.getvalue()
        assert rc == 0
        assert "Evidence:" in out
        assert "Attention: yes" in out
        assert "1 gate skipped" in out
        assert "Recorded: none; 1 planned" in out
        assert "Planning context" not in out
        assert not out.lstrip().startswith("{")

    def test_cli_format_color_can_be_forced(self) -> None:
        from cli._evidence_cli import format_evidence_cli
        from core.io.ansi import get_color_enabled, set_color_enabled, strip_ansi

        bundle = _make_args(
            body={
                "run_id": "R",
                "run_dir": "/tmp/runs/R",
                "schema_version": "1",
                "status": "done",
                "task": "T",
                "profile": "feature",
                "plan": {"source": "json", "subtask_count": 0, "has_contract": False},
                "phases": [],
                "gates": [],
                "commands": [],
                "artifacts": [],
                "metrics": {
                    "total_tokens": 1,
                    "total_tokens_in": 1,
                    "total_tokens_out": 0,
                    "total_duration_s": 0.1,
                },
                "errors": [],
                "findings": [],
            }
        )

        before = get_color_enabled()
        set_color_enabled(True)
        try:
            rendered = format_evidence_cli(bundle)
        finally:
            set_color_enabled(before)

        assert "\x1b[" in rendered
        assert "Evidence:" in strip_ansi(rendered)

    def test_cli_artifact_paths_are_copyable_not_clipped(self) -> None:
        from cli._evidence_cli import format_evidence_cli
        from core.io.ansi import strip_ansi

        long_path = (
            "/Users/example/workspace-orchestrator/runspace/runs/"
            "20260708_135646_06db6e/plan_20260708_135646_06db6e_round_20.json"
        )
        bundle = _make_args(
            body={
                "run_id": "R",
                "run_dir": "/tmp/runs/R",
                "schema_version": "1",
                "status": "done",
                "task": "T",
                "profile": "feature",
                "plan": {"source": "json", "subtask_count": 0, "has_contract": False},
                "phases": [],
                "gates": [],
                "commands": [],
                "artifacts": [{"kind": "parsed_plan", "path": long_path}],
                "metrics": {
                    "total_tokens": 1,
                    "total_tokens_in": 1,
                    "total_tokens_out": 0,
                    "total_duration_s": 0.1,
                },
                "errors": [],
                "findings": [],
            }
        )

        rendered = strip_ansi(format_evidence_cli(bundle))

        assert long_path in rendered
        assert "plan_20260708_135646_06db6e_round_20..." not in rendered

    def test_cli_full_view_renders_plan_timeline_and_acceptance(self) -> None:
        from cli._evidence_cli import format_evidence_cli
        from core.io.ansi import strip_ansi

        bundle = _make_args(
            body={
                "run_id": "R",
                "run_dir": "/tmp/runs/R",
                "schema_version": "1",
                "status": "done",
                "task": "Ship the complete evidence picture",
                "profile": "feature",
                "plan": {
                    "source": "json",
                    "short_summary": "Build a richer evidence view.",
                    "planning_context": "Full planning context survives here.",
                    "subtask_count": 2,
                    "has_contract": True,
                    "goal": "Make evidence explain the run",
                    "acceptance_criteria": ["full plan visible", "review path visible"],
                    "owned_files": ["cli/_evidence_cli.py"],
                    "commands_to_run": ["pytest tests/unit/cli/test_cli_orcho.py -q"],
                    "risks": ["large output"],
                    "review_focus": ["operator UX"],
                    "subtasks": [
                        {
                            "id": "t1",
                            "goal": "Render the plan",
                            "owned_files": ["cli/_evidence_cli.py"],
                            "done_criteria": ["Plan section lists tasks"],
                        },
                        {
                            "id": "t2",
                            "goal": "Render the review path",
                            "depends_on": ["t1"],
                            "files": ["tests/unit/cli/test_cli_orcho.py"],
                            "done_criteria": ["Timeline lists acceptance"],
                        },
                    ],
                },
                "phases": [
                    {
                        "name": "plan",
                        "title": "PLAN",
                        "outcome": "ok",
                        "attempt": 1,
                        "started_at": "2026-07-08T10:00:00Z",
                        "ended_at": "2026-07-08T10:01:00Z",
                    },
                    {
                        "name": "review_changes",
                        "title": "Review",
                        "outcome": "ok",
                        "attempt": 2,
                        "started_at": "2026-07-08T10:02:00Z",
                        "ended_at": "2026-07-08T10:03:00Z",
                    },
                ],
                "gates": [],
                "commands": [],
                "artifacts": [],
                "implementation_receipts": [
                    {
                        "subtask_id": "t1",
                        "state": "done",
                        "runtime": "claude",
                        "model": "opus",
                        "criteria_report": [{"met": True}],
                    }
                ],
                "release_summary": [
                    {
                        "phase": "final_acceptance",
                        "attempt": 1,
                        "verdict": "APPROVED",
                        "summary": "Ready.",
                    }
                ],
                "metrics": {
                    "total_tokens": 1,
                    "total_tokens_in": 1,
                    "total_tokens_out": 0,
                    "total_duration_s": 0.1,
                },
                "errors": [],
                "findings": [],
            }
        )

        rendered = strip_ansi(format_evidence_cli(bundle, view="full"))

        assert "Plan contract:" in rendered
        assert "Full planning context survives here." in rendered
        assert "subtasks=2 · dag=yes · contract=yes" in rendered
        assert "Acceptance criteria:" in rendered
        assert "full plan visible" in rendered
        assert "Planned tasks:" in rendered
        assert "1. t1 Render the plan" in rendered
        assert "depends_on: t1" in rendered
        assert "Phase timeline:" in rendered
        assert "review_changes#2" in rendered
        assert "Implementation receipts:" in rendered
        assert "done        t1 claude / opus" in rendered
        assert "Acceptance:" in rendered
        assert "APPROVED   final_acceptance#1" in rendered

    def test_cli_findings_show_lifecycle_statuses(self) -> None:
        from cli._evidence_cli import format_evidence_cli
        from core.io.ansi import strip_ansi

        bundle = _make_args(
            body={
                "run_id": "R",
                "run_dir": "/tmp/runs/R",
                "schema_version": "1",
                "status": "done",
                "task": "T",
                "profile": "feature",
                "plan": {"source": "json", "subtask_count": 0, "has_contract": False},
                "phases": [],
                "gates": [],
                "commands": [],
                "artifacts": [],
                "metrics": {
                    "total_tokens": 1,
                    "total_tokens_in": 1,
                    "total_tokens_out": 0,
                    "total_duration_s": 0.1,
                },
                "errors": [],
                "findings": [
                    {
                        "id": "O1",
                        "severity": "P1",
                        "title": "Still broken",
                        "phase": "review_changes",
                        "attempt": 2,
                        "status": "open",
                    },
                    {
                        "id": "F1",
                        "severity": "P1",
                        "title": "Fixed earlier issue",
                        "phase": "review_changes",
                        "attempt": 1,
                        "status": "fixed",
                        "status_reason": "later review_changes attempt approved",
                    },
                    {
                        "id": "W1",
                        "severity": "P2",
                        "title": "Accepted risk",
                        "phase": "validate_plan",
                        "attempt": 1,
                        "status": "waived",
                    },
                    {
                        "id": "R1",
                        "severity": "P1",
                        "title": "Release blocker",
                        "phase": "final_acceptance",
                        "attempt": 1,
                        "status": "final_rejected",
                    },
                ],
            }
        )

        rendered = strip_ansi(format_evidence_cli(bundle))

        assert "active x2" in rendered
        assert "final-rejected x1 (P1x1)" in rendered
        assert "open x1 (P1x1)" in rendered
        assert "waived x1 (P2x1)" in rendered
        assert "fixed x1 (P1x1)" in rendered
        assert "REJECTED P1  final_acceptance#1" in rendered
        assert "OPEN     P1  review_changes#2" in rendered
        assert "WAIVED   P2  validate_plan#1" in rendered
        assert "FIXED    P1  review_changes#1" in rendered
        assert "later review_changes attempt approved" not in rendered

        debug_rendered = strip_ansi(format_evidence_cli(bundle, debug=True))
        assert "later review_changes attempt approved" in debug_rendered

    def test_json_projection_compacts_verbose_fields(self) -> None:
        from cli._formatters import project_evidence_json

        body = {
            "artifacts": [],
            "commands": [],
            "created_at": "2026-05-19T20:00:00",
            "run_dir": "/tmp/runs/R",
            "run_id": "R",
            "schema_version": "1",
            "status": "halted",
            "task": "T" * 900,
            "plan": {
                "source": "json",
                "planning_context": "P" * 1000,
                "acceptance_criteria": ["a", "b"],
            },
            "prompt_render": [{"wire_chars": 12}, {"wire_chars": 34}],
            "implementation_receipts": [{
                "subtask_id": "T1",
                "state": "done",
                "done_criteria": ["a", "b"],
                "criteria_report": [{"met": True}],
            }],
            "errors": [{
                "kind": "command_stalled",
                "terminal": False,
                "command_preview": "while pgrep pytest",
            }],
        }
        bundle = _make_args(body=body)

        projected = project_evidence_json(bundle)
        debug = project_evidence_json(bundle, debug=True)

        assert len(projected["task"]) < len(body["task"])
        assert projected["plan"] == {
            "source": "json",
            "acceptance_criteria_count": 2,
        }
        assert "prompt_render" not in projected
        assert projected["implementation_receipts"] == [{
            "subtask_id": "T1",
            "state": "done",
            "done_criteria_count": 2,
            "criteria_report_count": 1,
        }]
        assert projected["errors"] == []
        assert projected["omitted_diagnostics"]["command_stalled_live"]["count"] == 1
        assert projected["omitted_details"]["prompt_render"] == {
            "entries": 2,
            "wire_chars": 46,
        }
        assert projected["omitted_details"]["plan"]["full_chars"] > 1000
        assert list(projected)[:11] == [
            "schema_version",
            "run_id",
            "run_dir",
            "status",
            "created_at",
            "task",
            "plan",
            "errors",
            "omitted_diagnostics",
            "commands",
            "artifacts",
        ]
        assert debug is body

    def test_md_debug_flag_reaches_renderer(self, monkeypatch) -> None:
        import cli.orcho as orcho

        fake_stdout = _FakeStdout(is_tty=False)
        seen: dict[str, bool] = {}
        monkeypatch.setattr(orcho.sys, "stdout", fake_stdout)
        monkeypatch.setattr(orcho, "collect_evidence", lambda *a, **k: object())

        def render(_bundle, *, debug=False):
            seen["debug"] = debug
            return "# Run evidence — `R`\n"

        monkeypatch.setattr(orcho, "render_evidence_md", render)

        rc = orcho.cmd_evidence(_make_args(
            run_id=None, workspace=None, out=None, format="md", debug=True,
        ))

        assert rc == 0
        assert seen["debug"] is True

    def test_md_output_colorizes_when_stdout_is_tty(self, monkeypatch) -> None:
        import cli.orcho as orcho

        fake_stdout = _FakeStdout(is_tty=True)
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setattr(orcho.sys, "stdout", fake_stdout)
        monkeypatch.setattr(orcho, "collect_evidence", lambda *a, **k: object())
        monkeypatch.setattr(
            orcho,
            "render_evidence_md",
            lambda _bundle: (
                "# Run evidence — `R`\n\n"
                "## Findings\n\n"
                "### `P2` Missing coverage\n\n"
                "**Required fix:** Add a test.\n"
            ),
        )

        rc = orcho.cmd_evidence(_make_args(run_id=None, workspace=None, out=None, format="md"))

        assert rc == 0
        out = fake_stdout.getvalue()
        assert "\033[" in out
        assert "## Findings" in out
        assert "Required fix:" in out

    def test_md_output_stays_raw_when_stdout_is_not_tty(self, monkeypatch) -> None:
        import cli.orcho as orcho

        fake_stdout = _FakeStdout(is_tty=False)
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setattr(orcho.sys, "stdout", fake_stdout)
        monkeypatch.setattr(orcho, "collect_evidence", lambda *a, **k: object())
        monkeypatch.setattr(
            orcho,
            "render_evidence_md",
            lambda _bundle: "# Run evidence — `R`\n\n## Findings\n",
        )

        rc = orcho.cmd_evidence(_make_args(run_id=None, workspace=None, out=None, format="md"))

        assert rc == 0
        assert fake_stdout.getvalue() == "# Run evidence — `R`\n\n## Findings\n"

    def test_md_output_honours_no_color(self, monkeypatch) -> None:
        import cli.orcho as orcho

        fake_stdout = _FakeStdout(is_tty=True)
        monkeypatch.setenv("NO_COLOR", "1")
        monkeypatch.setattr(orcho.sys, "stdout", fake_stdout)
        monkeypatch.setattr(orcho, "collect_evidence", lambda *a, **k: object())
        monkeypatch.setattr(
            orcho,
            "render_evidence_md",
            lambda _bundle: "# Run evidence — `R`\n\n## Findings\n",
        )

        rc = orcho.cmd_evidence(_make_args(run_id=None, workspace=None, out=None, format="md"))

        assert rc == 0
        assert "\033[" not in fake_stdout.getvalue()

    def test_colorized_phase_table_highlights_outcomes(self) -> None:
        from cli._formatters import colorize_evidence_markdown
        from core.io.ansi import get_color_enabled, set_color_enabled

        md = (
            "## Phase timeline\n\n"
            "| # | Phase | Title | Outcome | Attempt |\n"
            "|---|-------|-------|---------|---------|\n"
            "| 1 | `VALIDATE_PLAN` | validate_plan | `rejected` | 1 |\n"
            "| 2 | `VALIDATE_PLAN` | validate_plan | `approved` | 2 |\n"
            "| 3 | `REVIEW_CHANGES` | REVIEW | `skipped: no uncommitted changes` | 1 |\n"
        )

        # Force color on so the test verifies the colored path even
        # under pytest's non-TTY captured stdout (auto-detect would
        # otherwise return plain text).
        before = get_color_enabled()
        set_color_enabled(True)
        try:
            out = colorize_evidence_markdown(md)
        finally:
            set_color_enabled(before)

        assert "\033[96m`VALIDATE_PLAN`\033[0m" in out
        assert "\033[91m`rejected`\033[0m" in out
        assert "\033[92m`approved`\033[0m" in out
        assert "\033[90m`skipped: no uncommitted changes`\033[0m" in out

    def test_md_output_colorizes_under_force_color_even_when_stdout_not_tty(
        self, monkeypatch,
    ) -> None:
        """P1 regression catcher: removing the pre-gate in cmd_evidence
        means ``set_color_enabled(True)`` reaches the colorizer even
        when ``sys.stdout`` looks non-TTY. The old gate vetoed the
        call before paint() ever ran, so a forced-color override
        could not produce colored output.
        """
        import cli.orcho as orcho
        from core.io.ansi import get_color_enabled, set_color_enabled

        fake_stdout = _FakeStdout(is_tty=False)
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setattr(orcho.sys, "stdout", fake_stdout)
        monkeypatch.setattr(orcho, "collect_evidence", lambda *a, **k: object())
        monkeypatch.setattr(
            orcho,
            "render_evidence_md",
            lambda _bundle: "# Run evidence — `R`\n\n## Findings\n",
        )

        before = get_color_enabled()
        set_color_enabled(True)
        try:
            rc = orcho.cmd_evidence(_make_args(
                run_id=None, workspace=None, out=None, format="md",
            ))
        finally:
            set_color_enabled(before)

        assert rc == 0
        assert "\x1b[" in fake_stdout.getvalue()

    def test_md_output_stays_plain_under_disabled_color_on_tty(
        self, monkeypatch,
    ) -> None:
        """Symmetric to the P1 catcher: ``set_color_enabled(False)``
        wins over a TTY stdout. The override is checked inside
        paint() after the cmd_evidence pre-gate was removed, so
        suppressing color end-to-end now works through the policy."""
        import cli.orcho as orcho
        from core.io.ansi import get_color_enabled, set_color_enabled

        fake_stdout = _FakeStdout(is_tty=True)
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setattr(orcho.sys, "stdout", fake_stdout)
        monkeypatch.setattr(orcho, "collect_evidence", lambda *a, **k: object())
        monkeypatch.setattr(
            orcho,
            "render_evidence_md",
            lambda _bundle: "# Run evidence — `R`\n\n## Findings\n",
        )

        before = get_color_enabled()
        set_color_enabled(False)
        try:
            rc = orcho.cmd_evidence(_make_args(
                run_id=None, workspace=None, out=None, format="md",
            ))
        finally:
            set_color_enabled(before)

        assert rc == 0
        assert "\x1b[" not in fake_stdout.getvalue()


_DIFF_PATCH = (
    "diff --git a/api/payload.py b/api/payload.py\n"
    "index abc1234..def5678 100644\n"
    "--- a/api/payload.py\n"
    "+++ b/api/payload.py\n"
    "@@ -1 +1 @@\n"
    "-old\n"
    "+new\n"
    "diff --git a/api/util.py b/api/util.py\n"
    "new file mode 100644\n"
    "index 0000000..abc1234\n"
    "--- /dev/null\n"
    "+++ b/api/util.py\n"
    "@@ -0,0 +1 @@\n"
    "+helper\n"
)


class TestCmdDiff:
    @pytest.fixture
    def runs_dir(self, tmp_path: Path, monkeypatch):
        rd = tmp_path / "runs"
        rd.mkdir()
        monkeypatch.setenv("ORCHO_RUNSPACE", str(tmp_path))
        return rd

    @staticmethod
    def _write_diff_run(
        runs_dir: Path, run_id: str, patch: str | None = _DIFF_PATCH,
    ) -> Path:
        d = runs_dir / run_id
        d.mkdir()
        if patch is not None:
            (d / "diff.patch").write_text(patch, encoding="utf-8")
        return d

    def test_diff_full_mode_prints_raw_patch_no_ansi(
        self, runs_dir: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_diff
        self._write_diff_run(runs_dir, "20260519_100000")
        args = _make_args(run_id="20260519_100000", diff_mode="full")
        rc = cmd_diff(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "-old" in out
        assert "+new" in out
        assert "diff --git" in out
        assert "\033[" not in out

    def test_diff_preview_mode_shows_per_file_headers(
        self, runs_dir: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_diff
        self._write_diff_run(runs_dir, "20260519_100001")
        args = _make_args(
            run_id="20260519_100001", diff_mode="preview", no_color=True,
        )
        rc = cmd_diff(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Update(api/payload.py)" in out
        assert "Update(api/util.py)" in out

    def test_diff_missing_mode_defaults_to_preview(
        self, runs_dir: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_diff
        self._write_diff_run(runs_dir, "20260519_100009")
        args = _make_args(run_id="20260519_100009", no_color=True)
        rc = cmd_diff(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Update(api/payload.py)" in out
        assert "diff --git" not in out

    def test_diff_stat_mode_shows_table(
        self, runs_dir: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_diff
        self._write_diff_run(runs_dir, "20260519_100002")
        args = _make_args(
            run_id="20260519_100002", diff_mode="stat", no_color=True,
        )
        rc = cmd_diff(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "api/payload.py" in out
        assert "+1 -1" in out
        assert "+1 -0" in out

    def test_diff_path_filter_one_file(
        self, runs_dir: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_diff
        self._write_diff_run(runs_dir, "20260519_100003")
        args = _make_args(
            run_id="20260519_100003",
            diff_mode="stat",
            path="api/util.py",
            no_color=True,
        )
        rc = cmd_diff(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "api/util.py" in out
        assert "api/payload.py" not in out

    def test_diff_path_no_match_exits_zero_with_message(
        self, runs_dir: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_diff
        self._write_diff_run(runs_dir, "20260519_100004")
        args = _make_args(
            run_id="20260519_100004",
            diff_mode="full",
            path="missing_dir/x.py",
        )
        rc = cmd_diff(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "No diff entries matched" in out
        assert "missing_dir/x.py" in out

    def test_diff_missing_artifact_exits_zero_with_message(
        self, runs_dir: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_diff
        self._write_diff_run(runs_dir, "20260519_100005", patch=None)
        args = _make_args(run_id="20260519_100005", diff_mode="full")
        rc = cmd_diff(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "No diff artifact recorded" in out

    def test_diff_missing_artifact_warns_with_durable_reason(
        self, runs_dir: Path, capsys,
    ) -> None:
        # F1: diff.patch absent but finalization recorded patch_missing in
        # durable meta. The operator must see the recorded reason + path on
        # stderr even though found is False.
        import json

        from cli.orcho import cmd_diff
        d = self._write_diff_run(runs_dir, "20260519_100008", patch=None)
        (d / "meta.json").write_text(
            json.dumps(
                {
                    "status": "done",
                    "diff_patch": {
                        "status": "patch_missing",
                        "reason": "patch_unavailable",
                        "patch_path": str(d / "diff.patch"),
                        "baseline_ref": "base-tree",
                        "detail": "capture returned None",
                    },
                },
            ),
            encoding="utf-8",
        )
        args = _make_args(run_id="20260519_100008", diff_mode="full")
        rc = cmd_diff(args)
        captured = capsys.readouterr()
        assert rc == 0
        assert "patch integrity" in captured.err
        assert "patch_missing" in captured.err
        assert "patch_unavailable" in captured.err
        assert str(d / "diff.patch") in captured.err

    def test_diff_truncation_appends_footer(
        self, runs_dir: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_diff
        self._write_diff_run(runs_dir, "20260519_100006")
        args = _make_args(
            run_id="20260519_100006", diff_mode="full", max_bytes=40,
        )
        rc = cmd_diff(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "... output truncated at 40 bytes ..." in out

    def test_diff_unknown_run_id_returns_nonzero(
        self, runs_dir: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_diff
        self._write_diff_run(runs_dir, "20260519_100007")
        args = _make_args(run_id="does_not_exist", diff_mode="full")
        rc = cmd_diff(args)
        assert rc != 0

    def test_diff_parser_rejects_zero_max_bytes(self) -> None:
        from cli.orcho import build_parser
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["diff", "any_run", "--max-bytes", "0"])

    def test_diff_parser_rejects_empty_path(self) -> None:
        from cli.orcho import build_parser
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["diff", "any_run", "--path", "   "])

    def test_diff_parser_mode_flags_mutually_exclusive(self) -> None:
        from cli.orcho import build_parser
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["diff", "any_run", "--preview", "--stat"])

    def test_diff_parser_default_mode_is_preview(self) -> None:
        from cli.orcho import build_parser
        args = build_parser().parse_args(["diff", "any_run"])
        assert args.diff_mode == "preview"


class TestCmdEvidenceDiff:
    """Test ``orcho evidence --diff[=mode]`` CLI/markdown + JSON wrappers."""

    @pytest.fixture
    def runs_dir(self, tmp_path: Path, monkeypatch):
        rd = tmp_path / "runs"
        rd.mkdir()
        monkeypatch.setenv("ORCHO_RUNSPACE", str(tmp_path))
        return rd

    @staticmethod
    def _write_evidence_run(
        runs_dir: Path, run_id: str, patch: str | None = _DIFF_PATCH,
    ) -> Path:
        d = _write_run(runs_dir, run_id)
        if patch is not None:
            (d / "diff.patch").write_text(patch, encoding="utf-8")
        return d

    def test_json_no_diff_flag_is_not_wrapped(
        self, runs_dir: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_evidence
        self._write_evidence_run(runs_dir, "20260519_200000")
        args = _make_args(
            run_id="20260519_200000", format="json", diff=None,
        )
        rc = cmd_evidence(args)
        out = capsys.readouterr().out
        assert rc == 0
        parsed = json.loads(out)
        assert "evidence" not in parsed
        assert "diff" not in parsed
        assert "phases" in parsed or "schema_version" in parsed or "run_id" in parsed

    def test_json_filters_live_command_stalls_without_debug(
        self, runs_dir: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_evidence

        run_dir = self._write_evidence_run(
            runs_dir, "20260519_200010", patch=None,
        )
        run_dir.joinpath("events.jsonl").write_text(
            "\n".join([
                json.dumps({
                    "seq": 1,
                    "ts": "2026-05-19T20:00:00",
                    "kind": "agent.command_stalled",
                    "phase": "IMPLEMENT",
                    "payload": {
                        "phase": "IMPLEMENT",
                        "reason": "unsafe_process_polling",
                        "elapsed_s": 91.0,
                        "terminal": False,
                        "command_preview": "while pgrep pytest",
                    },
                }),
                json.dumps({
                    "seq": 2,
                    "ts": "2026-05-19T20:01:00",
                    "kind": "run.end",
                    "phase": None,
                    "payload": {
                        "status": "halted",
                        "halt_reason": "final_acceptance_rejected",
                    },
                }),
            ])
            + "\n",
            encoding="utf-8",
        )

        rc = cmd_evidence(_make_args(
            run_id="20260519_200010", format="json", diff=None,
            debug=False,
        ))

        out = capsys.readouterr().out
        assert rc == 0
        parsed = json.loads(out)
        assert [e["kind"] for e in parsed["errors"]] == ["run_halted"]
        assert parsed["omitted_diagnostics"]["command_stalled_live"]["count"] == 1

    def test_json_debug_preserves_live_command_stalls(
        self, runs_dir: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_evidence

        run_dir = self._write_evidence_run(
            runs_dir, "20260519_200011", patch=None,
        )
        run_dir.joinpath("events.jsonl").write_text(
            json.dumps({
                "seq": 1,
                "ts": "2026-05-19T20:00:00",
                "kind": "agent.command_stalled",
                "phase": "IMPLEMENT",
                "payload": {
                    "phase": "IMPLEMENT",
                    "reason": "unsafe_process_polling",
                    "elapsed_s": 91.0,
                    "terminal": False,
                    "command_preview": "while pgrep pytest",
                },
            })
            + "\n",
            encoding="utf-8",
        )

        rc = cmd_evidence(_make_args(
            run_id="20260519_200011", format="json", diff=None,
            debug=True,
        ))

        out = capsys.readouterr().out
        assert rc == 0
        parsed = json.loads(out)
        assert [e["kind"] for e in parsed["errors"]] == ["command_stalled"]
        assert "omitted_diagnostics" not in parsed

    def test_json_with_diff_wraps_payload(
        self, runs_dir: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_evidence
        self._write_evidence_run(runs_dir, "20260519_200001")
        args = _make_args(
            run_id="20260519_200001", format="json", diff="stat",
        )
        rc = cmd_evidence(args)
        out = capsys.readouterr().out
        assert rc == 0
        parsed = json.loads(out)
        assert set(parsed.keys()) == {"evidence", "diff"}
        assert parsed["diff"]["mode"] == "stat"
        assert parsed["diff"]["found"] is True
        file_paths = {f["path"] for f in parsed["diff"]["files"]}
        assert "api/payload.py" in file_paths

    def test_json_with_diff_preserves_evidence_body(
        self, runs_dir: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_evidence
        self._write_evidence_run(runs_dir, "20260519_200002")
        baseline = _make_args(
            run_id="20260519_200002", format="json", diff=None,
        )
        cmd_evidence(baseline)
        baseline_body = json.loads(capsys.readouterr().out)

        wrapped = _make_args(
            run_id="20260519_200002", format="json", diff="preview",
        )
        cmd_evidence(wrapped)
        wrapped_body = json.loads(capsys.readouterr().out)
        assert wrapped_body["evidence"] == baseline_body

    def test_md_diff_appends_section(
        self, runs_dir: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_evidence
        self._write_evidence_run(runs_dir, "20260519_200003")
        args = _make_args(
            run_id="20260519_200003", format="md", diff="preview",
        )
        rc = cmd_evidence(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "## Diff" in out
        assert "api/payload.py" in out

    def test_cli_diff_is_default_and_appends_section(
        self, runs_dir: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_evidence
        self._write_evidence_run(runs_dir, "20260519_200012")
        args = _make_args(
            run_id="20260519_200012", format=None, diff="stat",
        )
        rc = cmd_evidence(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Evidence:" in out
        assert "## Diff" in out
        assert "+1 -1" in out
        assert not out.lstrip().startswith("{")

    def test_md_diff_stat_only_renders_table(
        self, runs_dir: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_evidence
        self._write_evidence_run(runs_dir, "20260519_200004")
        args = _make_args(
            run_id="20260519_200004", format="md", diff="stat",
        )
        cmd_evidence(args)
        out = capsys.readouterr().out
        assert "## Diff" in out
        assert "+1 -1" in out
        assert "+1 -0" in out

    def test_md_diff_full_renders_raw_patch(
        self, runs_dir: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_evidence
        self._write_evidence_run(runs_dir, "20260519_200005")
        args = _make_args(
            run_id="20260519_200005", format="md", diff="full",
        )
        cmd_evidence(args)
        out = capsys.readouterr().out
        assert "## Diff" in out
        assert "```diff" in out
        assert "diff --git" in out

    def test_md_diff_missing_artifact_shows_placeholder(
        self, runs_dir: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_evidence
        self._write_evidence_run(runs_dir, "20260519_200006", patch=None)
        args = _make_args(
            run_id="20260519_200006", format="md", diff="preview",
        )
        cmd_evidence(args)
        out = capsys.readouterr().out
        assert "## Diff" in out
        assert "_No diff artifact recorded._" in out

    def test_evidence_diff_parser_choices(self) -> None:
        from cli.orcho import build_parser
        parser = build_parser()
        for mode in ("preview", "stat", "full"):
            args = parser.parse_args(["evidence", "any_run", f"--diff={mode}"])
            assert args.diff == mode
        bare = parser.parse_args(["evidence", "any_run", "--diff"])
        assert bare.diff == "preview"
        default = parser.parse_args(["evidence", "any_run"])
        assert default.diff is None
        assert default.format == "cli"


# ─────────────────────────────────────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────────────────────────────────────

class _Ns:
    """Simple namespace mock for argparse.Namespace."""
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def __getattr__(self, name):
        return None


def _make_args(**kwargs) -> _Ns:
    return _Ns(**kwargs)


class _FakeStdout:
    def __init__(self, *, is_tty: bool) -> None:
        self._is_tty = is_tty
        self._chunks: list[str] = []

    def isatty(self) -> bool:
        return self._is_tty

    def write(self, text: str) -> int:
        self._chunks.append(text)
        return len(text)

    def getvalue(self) -> str:
        return "".join(self._chunks)


# ─────────────────────────────────────────────────────────────────────────────
# workspace init — parser + handler
# ─────────────────────────────────────────────────────────────────────────────

class TestWorkspaceInitParser:
    @pytest.fixture(autouse=True)
    def _import_parser(self):
        from cli.orcho import build_parser
        self.build_parser = build_parser

    def test_workspace_init_subcommand_exists(self) -> None:
        parser = self.build_parser()
        args = parser.parse_args(["workspace", "init", "/tmp/g"])
        assert args.command == "workspace"
        assert args.workspace_cmd == "init"
        assert args.project_group_root == "/tmp/g"

    def test_workspace_init_project_group_root_defaults_to_none(self) -> None:
        """Omitted positional resolves to cwd inside the handler."""
        parser = self.build_parser()
        args = parser.parse_args(["workspace", "init"])
        assert args.project_group_root is None

    def test_workspace_init_handler_falls_back_to_cwd(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        from cli.orcho import cmd_workspace_init
        monkeypatch.chdir(tmp_path)
        rc = cmd_workspace_init(_make_args(
            project_group_root=None,
            workspace_name=None,
            mcp_config=None,
            mcp_server_name=None,
            orcho_mcp_command="orcho-mcp",
            force=False,
            dry_run=True,
        ))
        assert rc == 0
        assert (tmp_path / "workspace-orchestrator" / "runspace" / "runs") \
            .is_dir() is False  # dry-run — nothing on disk


    def test_workspace_init_defaults(self) -> None:
        parser = self.build_parser()
        args = parser.parse_args(["workspace", "init", "/tmp/g"])
        assert args.workspace_name is None
        assert args.mcp_config is None
        assert args.mcp_server_name is None
        assert args.orcho_mcp_command == "orcho-mcp"
        assert args.force is False
        assert args.dry_run is False
        assert args.no_scaffold is False

    def test_workspace_init_all_flags(self) -> None:
        parser = self.build_parser()
        args = parser.parse_args([
            "workspace", "init", "/tmp/g",
            "--workspace-name", "fino",
            "--mcp-config", "/tmp/g/.mcp.json",
            "--mcp-server-name", "orcho-fino",
            "--orcho-mcp-command", "/abs/bin/orcho-mcp",
            "--force",
            "--dry-run",
            "--no-scaffold",
        ])
        assert args.workspace_name == "fino"
        assert args.mcp_config == "/tmp/g/.mcp.json"
        assert args.mcp_server_name == "orcho-fino"
        assert args.orcho_mcp_command == "/abs/bin/orcho-mcp"
        assert args.force is True
        assert args.dry_run is True
        assert args.no_scaffold is True

    def test_workspace_bare_prints_help_clean(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Bare `orcho workspace` no longer dead-ends in argparse: its
        # subcommands all need arguments, so it prints its own help and
        # exits 0 (see test_bare_arg_only_group_prints_help_clean).
        parser = self.build_parser()
        args = parser.parse_args(["workspace"])
        assert args.workspace_cmd is None
        assert args.func(args) == 0
        assert "orcho workspace" in capsys.readouterr().out

    def test_workspace_fine_tune_dry_run_parses(self) -> None:
        parser = self.build_parser()
        args = parser.parse_args([
            "workspace", "fine-tune", "/p", "--dry-run",
        ])
        assert args.command == "workspace"
        assert args.workspace_cmd == "fine-tune"
        assert args.project_dir == "/p"
        assert args.dry_run is True
        assert args.func.__name__ == "cmd_workspace_fine_tune"

    def test_workspace_fine_tune_defaults(self) -> None:
        parser = self.build_parser()
        args = parser.parse_args(["workspace", "fine-tune"])
        assert args.project_dir is None
        assert args.dry_run is False

    def test_workspace_fine_tune_handler_prints_candidates(
        self, tmp_path: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_workspace_fine_tune
        project = tmp_path / "proj"
        project.mkdir()
        (project / "pyproject.toml").write_text("[project]\nname='p'\n")
        args = _make_args(project_dir=str(project), dry_run=True)
        rc = cmd_workspace_fine_tune(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "fine-tune" in out
        assert "verification_envs" in out
        assert "No files were written." in out

    def test_workspace_fine_tune_handler_prints_child_project_suggestions(
        self, tmp_path: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_workspace_fine_tune
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        child = workspace / "orcho-core"
        child.mkdir()
        (child / "pyproject.toml").write_text("[project]\nname='core'\n")
        args = _make_args(project_dir=str(workspace), dry_run=True)

        rc = cmd_workspace_fine_tune(args)

        out = capsys.readouterr().out
        assert rc == 0
        assert "project roots detected below this directory" in out
        assert str(child) in out
        assert f"orcho workspace fine-tune {child}" in out


class TestCmdWorkspaceInit:
    def test_real_run_creates_layout_and_prints_summary(
        self, tmp_path: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_workspace_init
        root = tmp_path / "group"
        rc = cmd_workspace_init(_make_args(
            project_group_root=str(root),
            workspace_name=None,
            mcp_config=None,
            mcp_server_name=None,
            orcho_mcp_command="orcho-mcp",
            force=False,
            dry_run=False,
        ))
        assert rc == 0
        assert (root / "workspace-orchestrator" / "runspace" / "runs").is_dir()
        out = capsys.readouterr().out
        assert "Orcho workspace initialized" in out
        assert "Workspace:" in out
        assert "Runs:" in out
        assert "Local config:" in out
        assert "Extension points:" in out
        assert "Plugin template:" in out
        assert "Prompt overrides:" in out
        assert "Task files:" in out
        assert "MCP client setup" in out
        assert "choose one path" in out
        assert "one Orcho MCP server per workspace" in out
        assert "distinct name" in out
        assert "Terminal clients" in out
        assert "Codex CLI / Codex app" in out
        assert "codex mcp add" in out
        assert "Claude Code" in out
        assert "claude mcp add" in out
        assert "Gemini CLI" in out
        assert "gemini mcp add" in out
        assert "App config snippets" in out
        assert "do not run" in out
        assert out.count("Done when:") >= 5
        assert "codex mcp list" in out
        assert "claude mcp list" in out
        assert "gemini mcp list" in out
        assert "Claude app / JSON clients" in out
        assert "mcpServers shape" in out
        assert "Antigravity" in out
        assert "User/mcp.json servers shape" in out
        assert "After client restart" in out
        assert "orcho_workspace_info" in out

    def test_dry_run_says_nothing_written(
        self, tmp_path: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_workspace_init
        root = tmp_path / "group"
        rc = cmd_workspace_init(_make_args(
            project_group_root=str(root),
            dry_run=True,
        ))
        assert rc == 0
        out = capsys.readouterr().out
        assert "dry run" in out.lower()
        assert not root.exists(), "dry-run must not create anything"

    def test_mcp_config_path_reported_in_output(
        self, tmp_path: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_workspace_init
        root = tmp_path / "group"
        cfg = tmp_path / ".mcp.json"
        rc = cmd_workspace_init(_make_args(
            project_group_root=str(root),
            mcp_config=str(cfg),
        ))
        assert rc == 0
        out = capsys.readouterr().out
        assert str(cfg) in out
        assert "MCP config" in out
        assert cfg.is_file()

    def test_refusal_returns_nonzero_exit(
        self, tmp_path: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_workspace_init
        # Target that looks like a repo at the root → refused without --force.
        root = tmp_path / "g"
        root.mkdir()
        (root / "pyproject.toml").write_text("# repo", encoding="utf-8")
        rc = cmd_workspace_init(_make_args(
            project_group_root=str(root),
            force=False,
            dry_run=False,
        ))
        assert rc != 0
        err = capsys.readouterr().err
        assert "individual project repo" in err


class TestWorkspaceInitNoInteractive:
    def test_no_interactive_flag_parsed(self) -> None:
        from cli.orcho import build_parser
        parser = build_parser()
        args = parser.parse_args(["workspace", "init", "/tmp/g", "--no-interactive"])
        assert args.no_interactive is True

    def test_no_interactive_flag_defaults_to_false(self) -> None:
        from cli.orcho import build_parser
        parser = build_parser()
        args = parser.parse_args(["workspace", "init", "/tmp/g"])
        assert args.no_interactive is False

    def test_no_interactive_skip_shows_hint_when_undetected(
        self, tmp_path: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_workspace_init

        root = tmp_path / "group"
        root.mkdir()
        undetected = root / "undetected-folder"
        undetected.mkdir()  # no marker files — not auto-detected

        rc = cmd_workspace_init(_make_args(
            project_group_root=str(root),
            no_interactive=True,
            dry_run=False,
            force=False,
        ))
        assert rc == 0
        out = capsys.readouterr().out
        assert "not auto-detected" in out

    def test_no_interactive_does_not_prompt(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        from cli.orcho import cmd_workspace_init
        from pipeline.project import project_discovery_prompt as _prom

        root = tmp_path / "group"
        root.mkdir()
        (root / "no-marker").mkdir()

        called = []
        monkeypatch.setattr(_prom, "prompt_for_extra_projects", lambda *a, **kw: called.append(1) or [])

        cmd_workspace_init(_make_args(
            project_group_root=str(root),
            no_interactive=True,
        ))
        assert called == [], "prompt must not be called in no-interactive mode"

    def test_interactive_threads_extra_projects(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """When TTY + interactive, confirmed extra projects appear in config."""
        import io

        from cli.orcho import cmd_workspace_init
        from pipeline.project import project_discovery_prompt as _prom
        from sdk.workspace import ExtraProject

        root = tmp_path / "group"
        root.mkdir()
        (root / "no-marker").mkdir()

        extra = ExtraProject(name="no-marker", path=str(root / "no-marker"), git_dir="")
        monkeypatch.setattr(_prom, "prompt_for_extra_projects", lambda *a, **kw: [extra])

        # Fake stdin as a TTY.
        fake_stdin = io.StringIO("")
        fake_stdin.isatty = lambda: True  # type: ignore[method-assign]
        monkeypatch.setattr("sys.stdin", fake_stdin)

        rc = cmd_workspace_init(_make_args(
            project_group_root=str(root),
            no_interactive=False,
            dry_run=False,
            force=False,
        ))
        assert rc == 0
        import json
        ws_dir = root / "workspace-orchestrator"
        config = json.loads((ws_dir / ".orcho" / "config.local.json").read_text())
        assert "no-marker" in config.get("projects", {})

    def test_repo_root_refused_before_any_prompt_or_git_init(
        self, tmp_path: Path, monkeypatch, capsys,
    ) -> None:
        """P1: a single repo-root must be refused during preflight, BEFORE the
        interactive discovery/prompt runs — otherwise we could `git init` a
        child and only then reject the target."""
        import io

        from cli.orcho import cmd_workspace_init
        from pipeline.project import project_discovery_prompt as _prom

        # Target is itself a repo-root (marker at the root).
        root = tmp_path / "single_repo"
        root.mkdir()
        (root / ".git").mkdir()
        (root / "subproj").mkdir()  # a child the prompt could touch

        called = []
        monkeypatch.setattr(
            _prom, "prompt_for_extra_projects",
            lambda *a, **kw: called.append(1) or [],
        )
        fake_stdin = io.StringIO("")
        fake_stdin.isatty = lambda: True  # type: ignore[method-assign]
        monkeypatch.setattr("sys.stdin", fake_stdin)

        rc = cmd_workspace_init(_make_args(
            project_group_root=str(root),
            no_interactive=False,
            dry_run=False,
            force=False,
        ))

        assert rc != 0, "repo-root target must be refused"
        assert called == [], "prompt (and git init) must NOT run for a refused target"
        err = capsys.readouterr().err
        assert "individual project repo" in err

    # --- delivery setup hint (T3) ----------------------------------------

    @staticmethod
    def _group_with_child(tmp_path: Path) -> Path:
        """A group root holding one detectable child project."""
        root = tmp_path / "group"
        child = root / "proj"
        child.mkdir(parents=True)
        (child / "pyproject.toml").write_text("[project]\nname='proj'\n")
        return root

    def test_prints_delivery_setup_hint_when_helper_returns_hint(
        self, tmp_path: Path, monkeypatch, capsys,
    ) -> None:
        from cli.orcho import cmd_workspace_init

        root = self._group_with_child(tmp_path)
        monkeypatch.setattr(
            "pipeline.engine.delivery_publish.collect_delivery_setup_hints",
            lambda project_dir, **_: ["install the gh CLI to enable auto-push"],
        )

        rc = cmd_workspace_init(_make_args(
            project_group_root=str(root),
            no_interactive=True,
            dry_run=False,
            force=False,
        ))

        assert rc == 0
        out = capsys.readouterr().out
        assert "Delivery setup:" in out
        assert "install the gh CLI to enable auto-push" in out

    def test_no_hint_printed_when_helper_returns_empty(
        self, tmp_path: Path, monkeypatch, capsys,
    ) -> None:
        from cli.orcho import cmd_workspace_init

        root = self._group_with_child(tmp_path)
        monkeypatch.setattr(
            "pipeline.engine.delivery_publish.collect_delivery_setup_hints",
            lambda project_dir, **_: [],
        )

        rc = cmd_workspace_init(_make_args(
            project_group_root=str(root),
            no_interactive=True,
            dry_run=False,
            force=False,
        ))

        assert rc == 0
        assert "Delivery setup:" not in capsys.readouterr().out

    def test_dry_run_shows_hint_without_writing_files(
        self, tmp_path: Path, monkeypatch, capsys,
    ) -> None:
        from cli.orcho import cmd_workspace_init

        root = self._group_with_child(tmp_path)
        monkeypatch.setattr(
            "pipeline.engine.delivery_publish.collect_delivery_setup_hints",
            lambda project_dir, **_: ["install the gh CLI to enable auto-push"],
        )

        rc = cmd_workspace_init(_make_args(
            project_group_root=str(root),
            no_interactive=True,
            dry_run=True,
            force=False,
        ))

        assert rc == 0
        out = capsys.readouterr().out
        assert "install the gh CLI to enable auto-push" in out
        # --dry-run must not create the workspace layout on disk.
        assert not (root / "workspace-orchestrator").exists()

    def test_helper_exception_does_not_change_exit_code_or_print(
        self, tmp_path: Path, monkeypatch, capsys,
    ) -> None:
        from cli.orcho import cmd_workspace_init

        root = self._group_with_child(tmp_path)

        def _boom(project_dir, **_):
            raise RuntimeError("hint probe exploded")

        monkeypatch.setattr(
            "pipeline.engine.delivery_publish.collect_delivery_setup_hints",
            _boom,
        )

        rc = cmd_workspace_init(_make_args(
            project_group_root=str(root),
            no_interactive=True,
            dry_run=False,
            force=False,
        ))

        # Detection failure never disturbs the init outcome.
        assert rc == 0
        assert "Delivery setup:" not in capsys.readouterr().out


class TestFormatWorkspaceInitColorPolicy:
    """T3 color-policy guards for format_workspace_init.

    cmd_workspace_init has no caller-side gate (``print`` is
    unconditional), so the policy must reach every ANSI insertion
    through paint() inside the formatter. Two pin cases keep the
    contract honest:

      * Plain path: ``set_color_enabled(False)`` yields zero ANSI
        even when the rest of the env would have allowed color.
      * Colored path: ``set_color_enabled(True)`` yields the
        expected palette anchors (green header, cyan headings,
        grey JSON-block fences) even under pytest's non-TTY
        captured stdout — proving the formatter actually routes
        through paint().
    """

    @staticmethod
    def _result():
        from sdk.workspace import DetectedProject, WorkspaceInitResult
        return WorkspaceInitResult(
            group_root="/tmp/g",
            workspace_dir="/tmp/g/workspace-orchestrator",
            runs_dir="/tmp/g/workspace-orchestrator/runspace/runs",
            env_file="/tmp/g/workspace-orchestrator/orcho-env.sh",
            local_config_file="/tmp/g/workspace-orchestrator/config.local.json",
            detected_projects=(DetectedProject(name="api", path="/tmp/g/api"),),
            created_paths=(),
            skipped_paths=(),
            warnings=("env file diverged",),
            mcp_server_name="orcho-mcp",
            mcp_snippet={"mcpServers": {"orcho-mcp": {
                "command": "orcho-mcp",
                "args": [],
                "env": {"ORCHO_WORKSPACE": "/tmp/g/workspace-orchestrator"},
            }}},
            mcp_config_path=None,
            mcp_config_action="",
            dry_run=False,
        )

    def test_disabled_color_yields_zero_ansi(self) -> None:
        from cli._formatters import format_workspace_init
        from core.io.ansi import get_color_enabled, set_color_enabled

        before = get_color_enabled()
        set_color_enabled(False)
        try:
            out = format_workspace_init(self._result())
        finally:
            set_color_enabled(before)

        assert "\x1b[" not in out
        # Plain content still visible.
        assert "Orcho workspace initialized" in out
        assert "Detected projects:" in out
        assert "Warnings:" in out
        assert "env file diverged" in out

    def test_forced_color_emits_expected_palette(self) -> None:
        from cli._formatters import format_workspace_init
        from core.io.ansi import C, get_color_enabled, set_color_enabled

        before = get_color_enabled()
        set_color_enabled(True)
        try:
            out = format_workspace_init(self._result())
        finally:
            set_color_enabled(before)

        # Header is green+bold.
        assert C.GREEN in out and C.BOLD in out
        # Headings carry cyan.
        assert C.CYAN in out
        # Warnings block carries yellow.
        assert C.YELLOW in out
        # JSON / command block fences are grey.
        assert C.GREY in out


class TestFormatWorkspaceInitEmptyAutoDetect:
    """F1 regression: when detected_projects=() but extra_projects or
    undetected_count is non-zero, must NOT claim group root is empty."""

    @staticmethod
    def _result(extra_projects=(), undetected_count=0, interactive=False):
        from sdk.workspace import WorkspaceInitResult
        return WorkspaceInitResult(
            group_root="/tmp/g",
            workspace_dir="/tmp/g/workspace-orchestrator",
            runs_dir="/tmp/g/workspace-orchestrator/runspace/runs",
            env_file="/tmp/g/workspace-orchestrator/orcho-env.sh",
            local_config_file="/tmp/g/workspace-orchestrator/.orcho/config.local.json",
            detected_projects=(),
            created_paths=(),
            skipped_paths=(),
            warnings=(),
            mcp_server_name="orcho-g",
            mcp_snippet={"mcpServers": {"orcho-g": {
                "command": "orcho-mcp",
                "args": [],
                "env": {"ORCHO_WORKSPACE": "/tmp/g/workspace-orchestrator"},
            }}},
            mcp_config_path=None,
            mcp_config_action="printed",
            dry_run=False,
            extra_projects=extra_projects,
            undetected_count=undetected_count,
            interactive=interactive,
        )

    def test_truly_empty_group_says_empty(self) -> None:
        from cli._formatters import format_workspace_init
        out = format_workspace_init(self._result())
        assert "none — group root is empty" in out
        assert "none auto-detected" not in out

    def test_extra_projects_suppresses_empty_message(self) -> None:
        from cli._formatters import format_workspace_init
        from sdk.workspace import ExtraProject
        extra = (ExtraProject(name="mono", path="/tmp/g/mono", git_dir="src"),)
        out = format_workspace_init(self._result(extra_projects=extra, interactive=True))
        assert "none — group root is empty" not in out
        assert "none auto-detected" in out
        assert "Interactively registered projects:" in out
        assert "mono" in out

    def test_undetected_count_suppresses_empty_message(self) -> None:
        from cli._formatters import format_workspace_init
        out = format_workspace_init(self._result(undetected_count=2))
        assert "none — group root is empty" not in out
        assert "none auto-detected" in out
        assert "not auto-detected" in out


class TestFormatWorkspaceInitRuntimeDetection:
    """The init output leads with the CLI runtimes found on PATH."""

    @staticmethod
    def _result(runtimes):
        from sdk.workspace import DetectedProject, WorkspaceInitResult
        return WorkspaceInitResult(
            group_root="/tmp/g",
            workspace_dir="/tmp/g/workspace-orchestrator",
            runs_dir="/tmp/g/workspace-orchestrator/runspace/runs",
            env_file="/tmp/g/workspace-orchestrator/orcho-env.sh",
            local_config_file="/tmp/g/workspace-orchestrator/config.local.json",
            detected_projects=(DetectedProject(name="api", path="/tmp/g/api"),),
            created_paths=(),
            skipped_paths=(),
            warnings=(),
            mcp_server_name="orcho-mcp",
            mcp_snippet={"mcpServers": {"orcho-mcp": {
                "command": "orcho-mcp",
                "args": [],
                "env": {"ORCHO_WORKSPACE": "/tmp/g/workspace-orchestrator"},
            }}},
            mcp_config_path=None,
            mcp_config_action="",
            dry_run=False,
            detected_runtimes=runtimes,
        )

    def test_installed_runtime_listed_and_marked(self) -> None:
        from cli._formatters import format_workspace_init
        from core.io.ansi import get_color_enabled, set_color_enabled
        from sdk.workspace import DetectedRuntime

        before_runtimes = (
            DetectedRuntime("Codex CLI / Codex app", "codex", "/usr/bin/codex"),
            DetectedRuntime("Claude Code", "claude", None),
            DetectedRuntime("Gemini CLI", "gemini", None),
        )
        before = get_color_enabled()
        set_color_enabled(False)
        try:
            out = format_workspace_init(self._result(before_runtimes))
        finally:
            set_color_enabled(before)

        # Summary section lists only the installed runtime + its path.
        assert "Detected CLI runtimes:" in out
        assert "Codex CLI / Codex app (/usr/bin/codex)" in out
        # Installed client's setup block is marked.
        assert "Codex CLI / Codex app: ✓ installed" in out
        # Missing clients are flagged, not hidden.
        assert "Claude Code: (not found — `claude` not on PATH)" in out
        assert "Gemini CLI: (not found — `gemini` not on PATH)" in out

    def test_no_runtimes_installed_shows_none_hint(self) -> None:
        from cli._formatters import format_workspace_init
        from core.io.ansi import get_color_enabled, set_color_enabled
        from sdk.workspace import DetectedRuntime

        none_installed = tuple(
            DetectedRuntime(c, cmd, None) for c, cmd in (
                ("Codex CLI / Codex app", "codex"),
                ("Claude Code", "claude"),
                ("Gemini CLI", "gemini"),
            )
        )
        before = get_color_enabled()
        set_color_enabled(False)
        try:
            out = format_workspace_init(self._result(none_installed))
        finally:
            set_color_enabled(before)

        assert "Detected CLI runtimes:" in out
        assert "(none on PATH — see the setup blocks below)" in out


# ═════════════════════════════════════════════════════════════════════════════
#  pipeline.project_orchestrator.main() — single-project CLI entry point
# ═════════════════════════════════════════════════════════════════════════════
#
# Strategy: call `main()` directly with monkeypatched `sys.argv`. Heavy
# downstream calls (`run_pipeline`, `_assert_fresh_run_dir_available`) are
# mocked at the module level so the test exercises argparse + error
# routing + exit-code contracts only. `ORCHO_WORKSPACE` is set to a
# `tmp_path` so `config.get_runs_dir()` resolves cleanly without touching
# the user's real workspace.


class TestProjectOrchestratorMain:
    @pytest.fixture
    def main_env(self, tmp_path: Path, monkeypatch):
        """Hermetic CLI fixture: scratch workspace + project dirs + a
        mocked `run_pipeline` returning a healthy session."""

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.setenv("ORCHO_WORKSPACE", str(workspace))
        # Avoid the user's $ORCHO_RUN_ID leaking into our deterministic
        # output_dir computation (main() reads the env var directly).
        monkeypatch.delenv("ORCHO_RUN_ID", raising=False)
        # main() writes ORCHO_RUNSPACE directly when --workspace is
        # supplied; pre-register it with monkeypatch so the teardown
        # restores it (otherwise the SUT's write leaks to later tests).
        monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)
        # Anchor cwd to a neutral location with no ``workspace-orchestrator/``
        # ancestor so the CLI's cwd-walkup workspace auto-derive can't
        # override the env we set. Without this, running pytest from
        # inside a repo that happens to dogfood orcho (this one does)
        # makes the walkup find the dev workspace and ignore our scratch.
        monkeypatch.chdir(tmp_path)

        run_pipeline_mock = MagicMock(return_value={"status": "done"})
        # ADR 0042 Phase H: main() lives in pipeline.project.cli; its
        # from pipeline.project.app import run_pipeline binding is
        # what main() resolves. Patch THAT to intercept the call.
        from pipeline.project import cli as _cli
        monkeypatch.setattr(_cli, "run_pipeline", run_pipeline_mock)
        return {
            "workspace": workspace,
            "project": project,
            "run_pipeline": run_pipeline_mock,
        }

    def _set_argv(self, monkeypatch, *args: str) -> None:
        monkeypatch.setattr(sys, "argv", ["orcho-run", *args])

    def test_happy_path_calls_run_pipeline_with_expected_kwargs(
        self, main_env, monkeypatch
    ) -> None:
        self._set_argv(
            monkeypatch,
            "--task", "do thing",
            "--project", str(main_env["project"]),
            "--mock",
        )
        from pipeline.project_orchestrator import main
        main()

        assert main_env["run_pipeline"].called
        kwargs = main_env["run_pipeline"].call_args.kwargs
        assert kwargs["task"] == "do thing"
        assert kwargs["project_dir"] == str(main_env["project"])
        assert os.environ["ORCHO_RUN_ID"] == kwargs["output_dir"].name
        # --mock flips the provider to the mock variant; SessionMode
        # collapses to STATELESS in mock mode.
        from agents.protocols import SessionMode
        assert kwargs["session_mode"] is SessionMode.STATELESS

    def test_session_split_flag_sets_process_override(
        self, main_env, monkeypatch
    ) -> None:
        monkeypatch.delenv("ORCHO_SESSION_SPLIT_OVERRIDE", raising=False)
        self._set_argv(
            monkeypatch,
            "--task", "do thing",
            "--project", str(main_env["project"]),
            "--session-split", "implement=common",
            "--session-split", "repair_changes=common",
        )
        from pipeline.project_orchestrator import main
        try:
            main()

            assert (
                os.environ["ORCHO_SESSION_SPLIT_OVERRIDE"]
                == "implement=common,repair_changes=common"
            )
        finally:
            os.environ.pop("ORCHO_SESSION_SPLIT_OVERRIDE", None)
            from core.infra.config import AppConfig
            AppConfig.load.cache_clear()

    def test_auto_workspace_local_config_drives_phase_config(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from core.infra import config

        group = tmp_path / "group"
        workspace = group / "workspace-orchestrator"
        project = group / "api"
        workspace.mkdir(parents=True)
        project.mkdir(parents=True)
        local_cfg = workspace / ".orcho" / "config.local.json"
        local_cfg.parent.mkdir()
        local_cfg.write_text(
            json.dumps({
                "phases": {
                    "implement": {
                        "runtime": "claude",
                        "model": "claude-workspace-implement",
                        "effort": "high",
                    },
                    "validate_plan": {
                        "runtime": "codex",
                        "model": "gpt-workspace-validate",
                        "effort": "high",
                    },
                    "review_changes": {
                        "runtime": "codex",
                        "model": "gpt-workspace-review",
                        "effort": "high",
                    },
                    "final_acceptance": {
                        "runtime": "codex",
                        "model": "gpt-workspace-final",
                        "effort": "high",
                    },
                },
                "pipeline": {
                    "change_handoff": "commit",
                },
            }),
            encoding="utf-8",
        )
        monkeypatch.delenv("ORCHO_WORKSPACE", raising=False)
        monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)
        monkeypatch.delenv("ORCHO_DISABLE_LOCAL_CONFIG", raising=False)
        monkeypatch.delenv("ORCHO_RUN_ID", raising=False)
        config._reset_config()
        config.AppConfig.load()

        run_pipeline_mock = MagicMock(return_value={"status": "done"})
        # ADR 0042 Phase H: main() lives in pipeline.project.cli; its
        # from pipeline.project.app import run_pipeline binding is
        # what main() resolves. Patch THAT to intercept the call.
        from pipeline.project import cli as _cli
        monkeypatch.setattr(_cli, "run_pipeline", run_pipeline_mock)
        self._set_argv(
            monkeypatch,
            "--task", "do thing",
            "--project", str(project),
        )
        from pipeline.project_orchestrator import main
        main()

        kwargs = run_pipeline_mock.call_args.kwargs
        assert kwargs["model"] == "claude-workspace-implement"
        phase_config = kwargs["phase_config"]
        assert phase_config.implement_agent.model == "claude-workspace-implement"
        assert phase_config.validate_plan_agent.model == "gpt-workspace-validate"
        assert phase_config.review_changes_agent.model == "gpt-workspace-review"
        assert phase_config.final_acceptance_agent.model == "gpt-workspace-final"

    def test_task_file_wins_over_task_when_both_given(
        self, main_env, monkeypatch, tmp_path: Path
    ) -> None:
        task_file = tmp_path / "task.md"
        task_file.write_text("from file", encoding="utf-8")
        self._set_argv(
            monkeypatch,
            "--task", "from-cli",
            "--task-file", str(task_file),
            "--project", str(main_env["project"]),
            "--mock",
        )
        from pipeline.project_orchestrator import main
        main()

        kwargs = main_env["run_pipeline"].call_args.kwargs
        # task-file body wins; --task is ignored. .strip() runs on the
        # file body so trailing whitespace doesn't leak.
        assert kwargs["task"] == "from file"

    def test_missing_task_exits_1(
        self, main_env, monkeypatch, capsys: pytest.CaptureFixture
    ) -> None:
        self._set_argv(
            monkeypatch,
            "--project", str(main_env["project"]),
            "--mock",
        )
        from pipeline.project_orchestrator import main
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "provide --task or --task-file" in captured.err
        # Exit-1 fires before the run_pipeline call.
        assert not main_env["run_pipeline"].called

    def test_missing_short_task_file_exits_with_hint(
        self, main_env, monkeypatch, capsys: pytest.CaptureFixture,
    ) -> None:
        self._set_argv(
            monkeypatch,
            "--task-file", "missing.md",
            "--project", str(main_env["project"]),
            "--mock",
        )
        from pipeline.project_orchestrator import main
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "--task-file short name not found: missing.md" in captured.err
        assert ".orcho/.task-files" in captured.err
        assert "--task-file ./missing.md" in captured.err
        assert "Traceback" not in captured.err
        assert not main_env["run_pipeline"].called

    def test_awaiting_phase_handoff_exits_4(
        self, main_env, monkeypatch
    ) -> None:
        # Phase 3 cutover: a generic phase handoff pause must surface as
        # rc=4 so the dashboard / CI / MCP wrappers can pivot into the
        # manual-review screen instead of treating the run as a normal
        # exit.
        main_env["run_pipeline"].return_value = {
            "status": "awaiting_phase_handoff",
        }
        self._set_argv(
            monkeypatch,
            "--task", "X",
            "--project", str(main_env["project"]),
            "--mock",
        )
        from pipeline.project_orchestrator import main
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 4

    def test_run_id_collision_exits_2(
        self, main_env, monkeypatch, capsys: pytest.CaptureFixture
    ) -> None:
        # `RunIdCollisionError` is normally raised by
        # `_assert_fresh_run_dir_available` inside `run_pipeline`; for
        # exit-code coverage we raise it from the mock so the
        # `except RunIdCollisionError` clause in `main()` runs.
        from pipeline.project_orchestrator import RunIdCollisionError
        main_env["run_pipeline"].side_effect = RunIdCollisionError(
            "run_id 20260512_001 already exists"
        )
        self._set_argv(
            monkeypatch,
            "--task", "X",
            "--project", str(main_env["project"]),
            "--mock",
        )
        from pipeline.project_orchestrator import main
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert "run_id 20260512_001 already exists" in captured.err

    def test_loop_resume_blocked_exits_2_without_traceback(
        self, main_env, monkeypatch, capsys: pytest.CaptureFixture,
    ) -> None:
        from pipeline.runtime.resume import LoopResumeBlockedError

        main_env["run_pipeline"].side_effect = LoopResumeBlockedError(
            "loop cursor conflicts with active profile"
        )
        self._set_argv(
            monkeypatch,
            "--task", "X",
            "--project", str(main_env["project"]),
            "--mock",
        )
        from pipeline.project_orchestrator import main

        with pytest.raises(SystemExit) as exc:
            main()

        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert "Cannot resume from checkpoint" in captured.err
        assert "loop cursor conflicts" in captured.err
        assert "Traceback" not in captured.err

    def test_keyboard_interrupt_exits_130_with_message(
        self, main_env, monkeypatch, capsys: pytest.CaptureFixture
    ) -> None:
        main_env["run_pipeline"].side_effect = KeyboardInterrupt()
        self._set_argv(
            monkeypatch,
            "--task", "X",
            "--project", str(main_env["project"]),
            "--mock",
        )
        from pipeline.project_orchestrator import main
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 130
        captured = capsys.readouterr()
        # Plain string — differs from the cross-orchestrator's
        # "\n⚠ Interrupted" (with U+26A0 emoji).
        assert "\nInterrupted" in captured.out

    def test_help_exits_0_and_prints_epilog(
        self, monkeypatch, capsys: pytest.CaptureFixture
    ) -> None:
        self._set_argv(monkeypatch, "--help")
        from pipeline.project_orchestrator import main
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
        captured = capsys.readouterr()
        assert "Examples:" in captured.out

    def test_fresh_run_without_project_auto_picks_from_workspace_map(
        self, main_env, monkeypatch
    ) -> None:
        """When cwd is inside a project registered in the workspace
        map, bare ``orcho run`` auto-picks that project — no prompt,
        no cwd fallback."""
        import json

        workspace = main_env["workspace"]
        project = main_env["project"]
        (workspace / ".orcho").mkdir(parents=True, exist_ok=True)
        (workspace / ".orcho" / "config.local.json").write_text(
            json.dumps({"projects": {"app": str(project)}}),
            encoding="utf-8",
        )
        monkeypatch.chdir(project)
        self._set_argv(monkeypatch, "--task", "X", "--mock")
        from pipeline.project_orchestrator import main
        main()

        kwargs = main_env["run_pipeline"].call_args.kwargs
        assert kwargs["project_dir"] == str(project.resolve())

    def test_fresh_run_without_project_and_no_workspace_map_errors(
        self, tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Bare ``orcho run`` from a directory that is not inside any
        registered project — and with no workspace map at all — must
        fail loudly with a hint to (re)initialize the workspace,
        rather than silently defaulting to cwd and producing a
        degraded run."""
        group = tmp_path / "demo"
        api = group / "api"
        web = group / "web"
        api.mkdir(parents=True)
        web.mkdir()
        (api / "pyproject.toml").write_text("# api\n", encoding="utf-8")
        (web / "package.json").write_text("{}\n", encoding="utf-8")
        workspace = group / "workspace-orchestrator"
        workspace.mkdir()
        monkeypatch.chdir(group)
        monkeypatch.delenv("ORCHO_RUN_ID", raising=False)
        monkeypatch.setenv("ORCHO_WORKSPACE", str(workspace))
        monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)

        run_pipeline_mock = MagicMock(return_value={"status": "done"})
        from pipeline.project import cli as _cli
        monkeypatch.setattr(_cli, "run_pipeline", run_pipeline_mock)
        self._set_argv(monkeypatch, "--task", "X", "--mock")

        from pipeline.project_orchestrator import main
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "no registered projects" in err.lower()
        assert "orcho workspace init" in err
        assert not run_pipeline_mock.called

    def test_project_group_root_exits_with_concrete_project_hint(
        self, tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture
    ) -> None:
        group = tmp_path / "demo"
        api = group / "api"
        web = group / "web"
        api.mkdir(parents=True)
        web.mkdir()
        (api / "pyproject.toml").write_text("# api\n", encoding="utf-8")
        (web / "package.json").write_text("{}\n", encoding="utf-8")

        workspace = group / "workspace-orchestrator"
        workspace.mkdir()
        monkeypatch.delenv("ORCHO_RUN_ID", raising=False)
        monkeypatch.delenv("ORCHO_WORKSPACE", raising=False)
        monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)

        run_pipeline_mock = MagicMock(return_value={"status": "done"})
        from pipeline.project import cli as _cli
        monkeypatch.setattr(_cli, "run_pipeline", run_pipeline_mock)
        self._set_argv(
            monkeypatch,
            "--task", "edit user",
            "--project", str(group),
            "--mock",
        )

        from pipeline.project_orchestrator import main
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "project group root" in err
        assert f"api: {api}" in err
        assert f"web: {web}" in err
        assert f"orcho run --project {api}" in err
        assert "orcho cross --projects" in err
        assert not run_pipeline_mock.called

    def test_no_git_plain_project_dir_still_runs(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        project = tmp_path / "plain-project"
        project.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        monkeypatch.setenv("ORCHO_WORKSPACE", str(workspace))
        monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)
        monkeypatch.delenv("ORCHO_RUN_ID", raising=False)

        run_pipeline_mock = MagicMock(return_value={"status": "done"})
        from pipeline.project import cli as _cli
        monkeypatch.setattr(_cli, "run_pipeline", run_pipeline_mock)
        self._set_argv(
            monkeypatch,
            "--task", "edit user",
            "--project", str(project),
            "--mock",
        )

        from pipeline.project_orchestrator import main
        main()
        assert run_pipeline_mock.called

    def test_project_alias_resolves_from_workspace_config(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        group = tmp_path / "demo"
        project = group / "web"
        project.mkdir(parents=True)
        workspace = group / "workspace-orchestrator"
        local_config = workspace / ".orcho" / "config.local.json"
        local_config.parent.mkdir(parents=True)
        local_config.write_text(
            json.dumps({"projects": {"web": str(project)}}),
            encoding="utf-8",
        )
        monkeypatch.delenv("ORCHO_RUN_ID", raising=False)
        monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)

        run_pipeline_mock = MagicMock(return_value={"status": "done"})
        from pipeline.project import cli as _cli
        monkeypatch.setattr(_cli, "run_pipeline", run_pipeline_mock)
        self._set_argv(
            monkeypatch,
            "--task", "edit user",
            "--project", "web",
            "--workspace", str(workspace),
            "--mock",
        )

        from pipeline.project_orchestrator import main
        main()
        kwargs = run_pipeline_mock.call_args.kwargs
        assert kwargs["project_dir"] == str(project.resolve())

    def test_resume_honours_explicit_workspace_with_wrong_worktree_env(
        self, main_env, monkeypatch, tmp_path: Path
    ) -> None:
        # Regression: ``config.get_runs_dir()`` reads ``ORCHO_RUNSPACE``
        # before ``ORCHO_WORKSPACE``. Without overriding both in the
        # ``--workspace`` apply, an ambient ``ORCHO_RUNSPACE`` pointing
        # at a different worktree would win and resume meta would be
        # read from the wrong runs dir.
        import json
        wrong_workspace = tmp_path / "wrong-ws"
        wrong_workspace.mkdir()
        (wrong_workspace / "runspace" / "runs").mkdir(parents=True)
        monkeypatch.setenv(
            "ORCHO_RUNSPACE", str(wrong_workspace / "runspace"),
        )
        monkeypatch.delenv("ORCHO_WORKSPACE", raising=False)

        run_id = "20260518_140000"
        runs_dir = main_env["workspace"] / "runspace" / "runs"
        runs_dir.mkdir(parents=True)
        parent_dir = runs_dir / run_id
        parent_dir.mkdir()
        (parent_dir / "meta.json").write_text(json.dumps({
            "task": "T",
            "project": str(main_env["project"]),
            "status": "interrupted",
        }), encoding="utf-8")

        self._set_argv(
            monkeypatch,
            "--resume", run_id,
            "--workspace", str(main_env["workspace"]),
            "--no-interactive",
            "--mock",
        )
        from pipeline.project_orchestrator import main
        main()
        kwargs = main_env["run_pipeline"].call_args.kwargs
        assert kwargs["resume_from"] == run_id

    def test_resume_with_task_is_followup(
        self, main_env, monkeypatch, tmp_path: Path
    ) -> None:
        # ``--resume RUN_ID --task X`` is the canonical follow-up
        # invocation: new run dir, no checkpoint hydration, parent
        # linkage stored in followup_* kwargs and persisted to meta.json.
        import json
        run_id = "20260512_001"
        runs_dir = main_env["workspace"] / "runspace" / "runs"
        runs_dir.mkdir(parents=True)
        parent_dir = runs_dir / run_id
        parent_dir.mkdir()
        (parent_dir / "meta.json").write_text(json.dumps({
            "task": "original",
            "project": str(main_env["project"]),
            "status": "done",
            "phases": {
                "plan": [{"session_id": "plan-parent"}],
                "validate_plan": [{"session_id": "validate-parent"}],
                "implement": {"meta": {"session_id": "implement-parent"}},
                "rounds": [{
                    "review_session_id": "review-parent",
                    "repair_session_id": "repair-parent",
                }],
                "final_acceptance": {
                    "meta": {"session_id": "final-parent"},
                },
            },
        }), encoding="utf-8")
        self._set_argv(
            monkeypatch,
            "--task", "follow up",
            "--project", str(main_env["project"]),
            "--resume", run_id,
            "--no-interactive",
            "--mock",
        )
        from pipeline.project_orchestrator import main
        main()
        kwargs = main_env["run_pipeline"].call_args.kwargs
        assert kwargs["resume_from"] is None
        assert kwargs["resume_mode"] == "followup"
        assert kwargs["followup_parent_run_id"] == run_id
        assert kwargs["followup_base_task"] == "original"
        assert kwargs["followup_session_seeds"] == {
            "plan": "plan-parent",
            "validate_plan": "validate-parent",
            "implement": "implement-parent",
            "review_changes": "review-parent",
            "repair_changes": "repair-parent",
            "final_acceptance": "final-parent",
        }
        # Output dir is a fresh timestamp under the workspace, NOT the
        # parent dir — follow-ups never write into the parent.
        assert Path(kwargs["output_dir"]).name != run_id
        assert kwargs["task"] == "follow up"

    def test_resume_no_task_incomplete_parent_is_checkpoint(
        self, main_env, monkeypatch
    ) -> None:
        # Bare ``--resume RUN_ID`` against an incomplete parent and a
        # non-interactive invocation continues the existing run in
        # place: resume_from forwards, follow-up kwargs stay empty.
        import json
        run_id = "20260512_002"
        runs_dir = main_env["workspace"] / "runspace" / "runs"
        runs_dir.mkdir(parents=True)
        parent_dir = runs_dir / run_id
        parent_dir.mkdir()
        (parent_dir / "meta.json").write_text(json.dumps({
            "task": "original",
            "project": str(main_env["project"]),
            "status": "interrupted",
        }), encoding="utf-8")
        self._set_argv(
            monkeypatch,
            "--resume", run_id,
            "--no-interactive",
            "--mock",
        )
        from pipeline.project_orchestrator import main
        main()
        kwargs = main_env["run_pipeline"].call_args.kwargs
        assert kwargs["resume_from"] == run_id
        # ``resume_mode`` is a follow-up marker; CHECKPOINT leaves it
        # absent so meta.json doesn't carry a misleading field.
        assert kwargs["resume_mode"] is None
        assert kwargs["followup_parent_run_id"] is None
        assert Path(kwargs["output_dir"]).name == run_id

    def test_resume_latest_without_project_uses_walkup_runs_dir(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # Regression: bare ``--resume`` skips project-based workspace
        # inference, so a globally configured workspace could make the
        # follow-up meta load use a different runs dir than the SDK
        # ``latest`` resolver found via cwd walk-up.
        from core.infra import config

        group = tmp_path / "group"
        project = group / "api"
        workspace = group / "workspace-orchestrator"
        runs_dir = workspace / "runspace" / "runs"
        project.mkdir(parents=True)
        runs_dir.mkdir(parents=True)

        run_id = "20260521_193734"
        parent_dir = runs_dir / run_id
        parent_dir.mkdir()
        (parent_dir / "meta.json").write_text(json.dumps({
            "task": "original",
            "project": str(project),
            "status": "interrupted",
            "phases": {
                "plan": [{"session_id": "plan-parent"}],
                "validate_plan": [{"session_id": "validate-parent"}],
            },
        }), encoding="utf-8")

        wrong_workspace = tmp_path / "wrong-workspace"
        (wrong_workspace / "runspace" / "runs").mkdir(parents=True)
        monkeypatch.setenv("ORCHO_WORKSPACE", str(wrong_workspace))
        monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)
        monkeypatch.delenv("ORCHO_RUN_ID", raising=False)
        monkeypatch.chdir(project)
        config._reset_config()

        run_pipeline_mock = MagicMock(return_value={"status": "done"})
        # ADR 0042 Phase H: main() lives in pipeline.project.cli; its
        # from pipeline.project.app import run_pipeline binding is
        # what main() resolves. Patch THAT to intercept the call.
        from pipeline.project import cli as _cli
        monkeypatch.setattr(_cli, "run_pipeline", run_pipeline_mock)
        self._set_argv(
            monkeypatch,
            "--resume", "latest",
            "--no-interactive",
            "--mock",
        )

        from pipeline.project_orchestrator import main
        main()

        kwargs = run_pipeline_mock.call_args.kwargs
        assert kwargs["resume_from"] == run_id
        assert kwargs["project_dir"] == str(project)
        assert Path(kwargs["output_dir"]) == parent_dir

    def test_resume_latest_with_project_uses_project_workspace_runs_dir(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # Regression: with ``--project`` present, workspace inference can
        # point at a different workspace than cwd walk-up. ``latest`` and
        # meta hydration must use the same project-derived runs dir.
        from core.infra import config

        cwd_group = tmp_path / "cwd-group"
        cwd_project = cwd_group / "api"
        cwd_runs = (
            cwd_group / "workspace-orchestrator" / "runspace" / "runs"
        )
        cwd_project.mkdir(parents=True)
        cwd_runs.mkdir(parents=True)
        _write_run(
            cwd_runs,
            "20260526_120000",
            project=str(cwd_project),
            status="interrupted",
        )

        project_group = tmp_path / "project-group"
        project = project_group / "api"
        project_runs = (
            project_group / "workspace-orchestrator" / "runspace" / "runs"
        )
        project.mkdir(parents=True)
        project_runs.mkdir(parents=True)
        run_id = "20260526_090000"
        parent_dir = _write_run(
            project_runs,
            run_id,
            project=str(project),
            status="interrupted",
        )

        monkeypatch.delenv("ORCHO_WORKSPACE", raising=False)
        monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)
        monkeypatch.delenv("ORCHO_RUN_ID", raising=False)
        monkeypatch.chdir(cwd_project)
        config._reset_config()

        run_pipeline_mock = MagicMock(return_value={"status": "done"})
        from pipeline.project import cli as _cli
        monkeypatch.setattr(_cli, "run_pipeline", run_pipeline_mock)
        self._set_argv(
            monkeypatch,
            "--resume", "latest",
            "--project", str(project),
            "--no-interactive",
            "--mock",
        )

        from pipeline.project_orchestrator import main
        main()

        kwargs = run_pipeline_mock.call_args.kwargs
        assert kwargs["resume_from"] == run_id
        assert kwargs["project_dir"] == str(project)
        assert Path(kwargs["output_dir"]) == parent_dir

    def test_resume_no_task_completed_parent_exits_0_with_hint(
        self, main_env, monkeypatch, capsys: pytest.CaptureFixture
    ) -> None:
        # Bare ``--resume`` against a done parent without a task has
        # nothing to do — print a hint and exit 0 instead of silently
        # rerunning into a completed run dir.
        import json
        run_id = "20260512_003"
        runs_dir = main_env["workspace"] / "runspace" / "runs"
        runs_dir.mkdir(parents=True)
        parent_dir = runs_dir / run_id
        parent_dir.mkdir()
        (parent_dir / "meta.json").write_text(json.dumps({
            "task": "original",
            "project": str(main_env["project"]),
            "status": "done",
        }), encoding="utf-8")
        self._set_argv(
            monkeypatch,
            "--resume", run_id,
            "--no-interactive",
            "--mock",
        )
        from pipeline.project_orchestrator import main
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
        err = capsys.readouterr().err
        assert run_id in err
        assert "follow-up" in err
        # Pipeline call short-circuited.
        assert not main_env["run_pipeline"].called

    def test_resume_no_task_phase_handoff_halt_exits_0_with_hint(
        self, main_env, monkeypatch, capsys: pytest.CaptureFixture
    ) -> None:
        # ``phase_handoff_decide(action="halt")`` is terminal for the
        # checkpoint. A bare resume should not attempt dispatch; users
        # can still pass a new task to start a follow-up from this run.
        run_id = "20260512_004"
        runs_dir = main_env["workspace"] / "runspace" / "runs"
        runs_dir.mkdir(parents=True)
        parent_dir = runs_dir / run_id
        parent_dir.mkdir()
        (parent_dir / "meta.json").write_text(json.dumps({
            "task": "original",
            "project": str(main_env["project"]),
            "status": "halted",
            "halt_reason": "phase_handoff_halt",
        }), encoding="utf-8")
        self._set_argv(
            monkeypatch,
            "--resume", run_id,
            "--no-interactive",
            "--mock",
        )
        from pipeline.project_orchestrator import main
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
        err = capsys.readouterr().err
        assert run_id in err
        assert "checkpoint" in err
        assert "follow-up" in err
        assert not main_env["run_pipeline"].called

    def test_resume_no_task_final_acceptance_rejected_parent_exits_0_with_hint(
        self, main_env, monkeypatch, capsys: pytest.CaptureFixture
    ) -> None:
        # A rejected final acceptance (release verdict ``reject`` with no
        # applied delivery and no correction gate) is a terminal dead-end, not
        # a decision surface. A bare resume must not silently re-run into the
        # halted run dir — it prints a follow-up/inspect hint and exits 0.
        # Shape mirrors run 20260626_165338_90fb22.
        run_id = "20260512_005"
        runs_dir = main_env["workspace"] / "runspace" / "runs"
        runs_dir.mkdir(parents=True)
        parent_dir = runs_dir / run_id
        parent_dir.mkdir()
        (parent_dir / "meta.json").write_text(json.dumps({
            "task": "original",
            "project": str(main_env["project"]),
            "status": "halted",
            "halt_reason": "final_acceptance_rejected",
        }), encoding="utf-8")
        self._set_argv(
            monkeypatch,
            "--resume", run_id,
            "--no-interactive",
            "--mock",
        )
        from pipeline.project_orchestrator import main
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
        err = capsys.readouterr().err
        assert run_id in err
        assert "follow-up" in err
        # Checkpoint-resume did not start.
        assert not main_env["run_pipeline"].called

    # ── R1: active follow-up child profile resolution ──────────────────────
    #
    # When the operator bare-resumes a parent and interactively switches to
    # the active follow-up child, the *child's* durable ``meta.profile`` must
    # win unless the operator passed an explicit ``--profile``. The parent's
    # already-resolved (inherited) profile must not masquerade as an explicit
    # override for the newly selected child.

    def _seed_parent_and_active_child(
        self, main_env, monkeypatch, *, parent_id: str, child_id: str,
    ) -> None:
        """Write a parent (profile=feature) + an active follow-up child
        (profile=correction) and install the interactive switch: stdin is a
        TTY and the prompt picks the child as the resume target."""
        import io

        import pipeline.control as _control
        from pipeline.control import PromptedResumeIntent, ResumeMode

        runs_dir = main_env["workspace"] / "runspace" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / parent_id).mkdir()
        (runs_dir / parent_id / "meta.json").write_text(json.dumps({
            "task": "original",
            "project": str(main_env["project"]),
            "status": "interrupted",
            "profile": "feature",
        }), encoding="utf-8")
        (runs_dir / child_id).mkdir()
        (runs_dir / child_id / "meta.json").write_text(json.dumps({
            "task": "child follow-up",
            "project": str(main_env["project"]),
            "status": "interrupted",
            "profile": "correction",
            "resume_mode": "followup",
            "parent_run_id": parent_id,
        }), encoding="utf-8")

        # Force the interactive resume-intent path (TTY + not
        # --no-interactive) so the active-child switch branch runs.
        fake_stdin = io.StringIO("")
        fake_stdin.isatty = lambda: True  # type: ignore[method-assign]
        monkeypatch.setattr("sys.stdin", fake_stdin)
        # Operator picks the active follow-up child: CHECKPOINT into it.
        # main() does ``from pipeline.control import prompt_resume_intent``
        # at call time, so patch the source binding.
        monkeypatch.setattr(
            _control, "prompt_resume_intent",
            lambda **kw: PromptedResumeIntent(
                mode=ResumeMode.CHECKPOINT, resume_run_id=child_id,
            ),
        )

    def test_active_followup_child_profile_wins_without_explicit_profile(
        self, main_env, monkeypatch,
    ) -> None:
        # R1 regression (fails before T1, passes after): bare
        # ``--resume PARENT`` with NO ``--profile``; the operator picks the
        # active follow-up child whose meta.profile is ``correction``. The
        # child's durable profile must win over the parent's inherited
        # ``feature`` and over ambient ``ORCHO_PIPELINE``.
        parent_id = "20260512_100000"
        child_id = "20260512_110000"
        self._seed_parent_and_active_child(
            main_env, monkeypatch, parent_id=parent_id, child_id=child_id,
        )
        # Ambient work-kind env that must NOT win over the child meta.
        monkeypatch.setenv("ORCHO_PIPELINE", "task")
        self._set_argv(
            monkeypatch,
            "--resume", parent_id,
            "--mock",
        )
        from pipeline.project_orchestrator import main
        main()

        assert main_env["run_pipeline"].called
        kwargs = main_env["run_pipeline"].call_args.kwargs
        assert kwargs["resume_from"] == child_id
        # Before the T1 fix the second resolve_resume_profile saw the
        # mutated args.profile ('feature') as an explicit override and this
        # would be 'feature'. After the fix it is the child's own profile.
        assert kwargs["profile_name"] == "correction"

    def test_explicit_profile_still_overrides_active_followup_child(
        self, main_env, monkeypatch,
    ) -> None:
        # Paired positive case: a REAL explicit ``--profile`` stays an
        # operator override and beats the selected child's meta.profile
        # ('correction') as well as the parent's ('feature'), proving the
        # R1 fix does not weaken the genuine explicit-override path.
        parent_id = "20260512_100000"
        child_id = "20260512_110000"
        self._seed_parent_and_active_child(
            main_env, monkeypatch, parent_id=parent_id, child_id=child_id,
        )
        self._set_argv(
            monkeypatch,
            "--resume", parent_id,
            "--profile", "planning",
            "--mock",
        )
        from pipeline.project_orchestrator import main
        main()

        assert main_env["run_pipeline"].called
        kwargs = main_env["run_pipeline"].call_args.kwargs
        assert kwargs["resume_from"] == child_id
        # 'planning' came only from the explicit flag — distinct from the
        # parent ('feature') and child ('correction') meta profiles.
        assert kwargs["profile_name"] == "planning"


# ═════════════════════════════════════════════════════════════════════════════
#  run_tests — multi-suite aggregation (line 320-348 in project_orchestrator)
# ═════════════════════════════════════════════════════════════════════════════


class TestRunTestsMultiSuite:
    """The multi-suite branch of `run_tests` runs each `TestSuiteConfig`
    sequentially, aggregates outputs with `[name] status` headers, sums
    durations, and marks overall passed iff every runnable suite passed.
    Suites with `run_command=None` are listed in the output as skipped
    but don't execute.
    """

    def test_all_pass_aggregates_durations_and_marks_passed(
        self, monkeypatch
    ) -> None:
        from agents.entities import TestResult
        from pipeline import project_testing
        from pipeline.plugins import PluginConfig

        plugin = PluginConfig(quality_gates={
            "tests": {"suites": [
                {"name": "unit",  "run_command": "echo unit"},
                {"name": "behat", "run_command": "echo behat"},
            ]}
        })
        results = iter([
            TestResult(passed=True, output="unit ok",  duration=0.5),
            TestResult(passed=True, output="behat ok", duration=1.5),
        ])
        # ADR 0042 Phase J: legacy ``po._run_single_test`` shim retired.
        # Patch the canonical home; the function-default capture is
        # late-bound, so the monkeypatch takes effect at call time.
        monkeypatch.setattr(
            project_testing, "run_single_test",
            lambda *a, **k: next(results),
        )

        result = project_testing.run_tests("/cwd", plugin)
        assert result.passed is True
        assert result.duration == 2.0
        assert "[unit] ✓ passed" in result.output
        assert "[behat] ✓ passed" in result.output

    def test_any_failure_marks_overall_failed(
        self, monkeypatch
    ) -> None:
        from agents.entities import TestResult
        from pipeline import project_testing
        from pipeline.plugins import PluginConfig

        plugin = PluginConfig(quality_gates={
            "tests": {"suites": [
                {"name": "unit",  "run_command": "echo unit"},
                {"name": "behat", "run_command": "echo behat"},
            ]}
        })
        results = iter([
            TestResult(passed=True,  output="unit ok",     duration=0.5),
            TestResult(passed=False, output="behat broke", duration=1.5),
        ])
        # ADR 0042 Phase J: legacy ``po._run_single_test`` shim retired.
        # Patch the canonical home; the function-default capture is
        # late-bound, so the monkeypatch takes effect at call time.
        monkeypatch.setattr(
            project_testing, "run_single_test",
            lambda *a, **k: next(results),
        )

        result = project_testing.run_tests("/cwd", plugin)
        assert result.passed is False
        assert "[behat] ✗ FAILED" in result.output

    def test_no_runnable_suites_returns_skipped(self) -> None:
        from pipeline import project_testing
        from pipeline.plugins import PluginConfig

        plugin = PluginConfig(quality_gates={
            "tests": {"suites": [
                # Every suite documented but run_command=None → skipped.
                {"name": "stub-a", "run_command": None},
                {"name": "stub-b", "run_command": None},
            ]}
        })
        result = project_testing.run_tests("/cwd", plugin)
        assert result.skipped is True

    def test_skipped_suite_appears_in_aggregated_output(
        self, monkeypatch
    ) -> None:
        from agents.entities import TestResult
        from pipeline import project_testing
        from pipeline.plugins import PluginConfig

        plugin = PluginConfig(quality_gates={
            "tests": {"suites": [
                {"name": "real", "run_command": "echo real"},
                {"name": "stub", "run_command": None},
            ]}
        })
        monkeypatch.setattr(
            project_testing, "run_single_test",
            lambda *a, **k: TestResult(passed=True, output="real ok", duration=0.1,
        ),
        )

        result = project_testing.run_tests("/cwd", plugin)
        assert result.passed is True
        assert "[stub] skipped (no run_command)" in result.output
        assert "[real] ✓ passed" in result.output


# ─────────────────────────────────────────────────────────────────────────────
# cmd_repair_state (orcho repair-state)
# ─────────────────────────────────────────────────────────────────────────────

_HALT_DECIDED_AT = "2026-06-07T12:00:00+00:00"


def _repair_write_events(run_dir: Path, lines: list[dict]) -> None:
    run_dir.joinpath("events.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )


def _repair_write_meta(run_dir: Path, meta: dict) -> None:
    run_dir.joinpath("meta.json").write_text(json.dumps(meta), encoding="utf-8")


def _repair_read_meta(run_dir: Path) -> dict:
    return json.loads(run_dir.joinpath("meta.json").read_text(encoding="utf-8"))


def _repair_write_decision(run_dir: Path, name: str, decision: dict) -> None:
    dd = run_dir / "phase_handoff_decisions"
    dd.mkdir(exist_ok=True)
    dd.joinpath(f"{name}.json").write_text(json.dumps(decision), encoding="utf-8")


def _repair_handoff_event(handoff_id: str, phase: str = "validate_plan") -> dict:
    return {
        "seq": 1,
        "ts": "t",
        "kind": "phase.handoff_requested",
        "phase": phase,
        "payload": {"handoff_id": handoff_id, "phase": phase},
    }


def _make_torn_halt_run(run_dir: Path) -> None:
    """interrupted + active handoff + halt decision -> repairs to halted."""
    _repair_write_events(run_dir, [_repair_handoff_event("h1")])
    _repair_write_meta(
        run_dir, {"status": "interrupted", "phase_handoff": {"id": "h1"}}
    )
    _repair_write_decision(
        run_dir,
        "h1",
        {"action": "halt", "handoff_id": "h1", "decided_at": _HALT_DECIDED_AT},
    )


def _make_refusal_run(run_dir: Path) -> None:
    """interrupted + active handoff + NO halt decision -> refusal."""
    _repair_write_events(run_dir, [_repair_handoff_event("h1")])
    _repair_write_meta(
        run_dir, {"status": "interrupted", "phase_handoff": {"id": "h1"}}
    )


class TestCmdRepairState:
    @pytest.fixture
    def workspace(self, tmp_path: Path) -> Path:
        (tmp_path / "runspace" / "runs").mkdir(parents=True)
        return tmp_path

    def _run_dir(self, workspace: Path, run_id: str) -> Path:
        d = workspace / "runspace" / "runs" / run_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _repairs_dir(self, run_dir: Path) -> Path:
        return run_dir / "run_state_repairs"

    def _parse(self, *argv: str):
        from cli.orcho import build_parser

        return build_parser().parse_args(["repair-state", *argv])

    # ── parser wiring ───────────────────────────────────────────────────────
    def test_repair_state_parser_wires(self) -> None:
        from cli.orcho import cmd_repair_state

        args = self._parse("RID")
        assert args.func is cmd_repair_state
        assert args.run_id == "RID"
        assert args.apply is False
        assert args.json is False
        assert args.workspace is None

    def test_repair_state_parser_flags_parse(self) -> None:
        args = self._parse("RID", "--apply", "--json", "--workspace", "/ws")
        assert args.apply is True
        assert args.json is True
        assert args.workspace == "/ws"

    # ── dry-run: status shown, no mutation ───────────────────────────────────
    def test_repair_state_dry_run_shows_status_and_writes_nothing(
        self, workspace: Path, capsys
    ) -> None:
        from cli.orcho import cmd_repair_state

        run_dir = self._run_dir(workspace, "20260101_000000")
        _make_torn_halt_run(run_dir)
        args = self._parse("20260101_000000", "--workspace", str(workspace))
        rc = cmd_repair_state(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Status" in out
        assert "interrupted" in out
        assert "Proposed changes" in out
        assert not self._repairs_dir(run_dir).exists()

    # ── apply: backup + audit written, meta repaired ─────────────────────────
    def test_repair_state_apply_writes_backup_and_audit(
        self, workspace: Path, capsys
    ) -> None:
        from cli.orcho import cmd_repair_state

        run_dir = self._run_dir(workspace, "20260101_000000")
        _make_torn_halt_run(run_dir)
        args = self._parse(
            "20260101_000000", "--apply", "--workspace", str(workspace)
        )
        rc = cmd_repair_state(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Applied: yes" in out
        assert "Backup:" in out
        assert "Audit:" in out
        meta = _repair_read_meta(run_dir)
        assert meta["status"] == "halted"
        assert "phase_handoff" not in meta
        repairs = self._repairs_dir(run_dir)
        assert repairs.is_dir()
        files = sorted(repairs.iterdir())
        backups = [p for p in files if p.name.endswith(".bak.json")]
        audits = [p for p in files if not p.name.endswith(".bak.json")]
        assert len(backups) == 1
        assert len(audits) == 1

    def test_repair_state_second_apply_is_idempotent_noop(
        self, workspace: Path, capsys
    ) -> None:
        from cli.orcho import cmd_repair_state

        run_dir = self._run_dir(workspace, "20260101_000000")
        _make_torn_halt_run(run_dir)
        args = self._parse(
            "20260101_000000", "--apply", "--workspace", str(workspace)
        )
        assert cmd_repair_state(args) == 0
        capsys.readouterr()
        files_after_first = sorted(self._repairs_dir(run_dir).iterdir())

        rc = cmd_repair_state(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Applied: no" in out
        # No new backup/audit artifacts beyond the first apply.
        assert sorted(self._repairs_dir(run_dir).iterdir()) == files_after_first

    # ── refusal: needs_operator_decision, no mutation even with --apply ──────
    def test_repair_state_refusal_prints_hint_and_writes_nothing(
        self, workspace: Path, capsys
    ) -> None:
        from cli.orcho import cmd_repair_state

        run_dir = self._run_dir(workspace, "20260101_000000")
        _make_refusal_run(run_dir)
        args = self._parse(
            "20260101_000000", "--apply", "--workspace", str(workspace)
        )
        rc = cmd_repair_state(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "operator decision" in out.lower()
        assert "Hint:" in out
        assert _repair_read_meta(run_dir)["phase_handoff"] == {"id": "h1"}
        assert not self._repairs_dir(run_dir).exists()

    # ── --json: single object with all required keys ─────────────────────────
    def test_repair_state_json_emits_single_object_with_keys(
        self, workspace: Path, capsys
    ) -> None:
        from cli.orcho import cmd_repair_state

        run_dir = self._run_dir(workspace, "20260101_000000")
        _make_torn_halt_run(run_dir)
        args = self._parse(
            "20260101_000000", "--json", "--workspace", str(workspace)
        )
        rc = cmd_repair_state(args)
        out = capsys.readouterr().out
        assert rc == 0
        obj = json.loads(out)
        required = {
            "run_id",
            "run_dir",
            "action",
            "apply_requested",
            "applied",
            "issue_codes",
            "changes",
            "needs_operator_decision",
            "repair_hint",
            "backup_path",
            "audit_path",
            "repaired_at",
        }
        assert required <= set(obj.keys())
        assert obj["run_id"] == "20260101_000000"
        assert obj["apply_requested"] is False
        assert obj["changes"]
        first = obj["changes"][0]
        assert {"field", "before", "after", "issue_code"} <= set(first.keys())

    # ── resolve errors: clean stderr, nonzero, no traceback ──────────────────
    def test_repair_state_unknown_run_id_errors_clean(self, workspace: Path, capsys) -> None:
        from cli.orcho import cmd_repair_state

        self._run_dir(workspace, "20260101_000000")
        args = self._parse("NOPE", "--workspace", str(workspace))
        rc = cmd_repair_state(args)
        captured = capsys.readouterr()
        assert rc != 0
        assert captured.err.strip()
        assert "Traceback" not in captured.err
        assert captured.out == ""

    def test_repair_state_missing_workspace_errors_clean(
        self, tmp_path: Path, capsys
    ) -> None:
        from cli.orcho import cmd_repair_state

        missing = tmp_path / "no_such_ws"
        args = self._parse("RID", "--workspace", str(missing))
        rc = cmd_repair_state(args)
        captured = capsys.readouterr()
        assert rc != 0
        assert captured.err.strip()
        assert "Traceback" not in captured.err
        assert captured.out == ""

    # ── repair failures (F2): patch the module-level repair_run_state symbol ──
    def test_repair_state_value_error_branch_exit_2_clean(
        self, workspace: Path, capsys, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import cli.orcho as orcho

        run_dir = self._run_dir(workspace, "20260101_000000")
        _make_torn_halt_run(run_dir)

        def _boom(*a, **k):
            raise ValueError("unsupported action")

        monkeypatch.setattr(orcho, "repair_run_state", _boom)
        args = self._parse(
            "20260101_000000", "--json", "--workspace", str(workspace)
        )
        rc = orcho.cmd_repair_state(args)
        captured = capsys.readouterr()
        assert rc == 2
        assert captured.err.strip()
        assert "Traceback" not in captured.err
        assert captured.out == ""

    def test_repair_state_runtime_error_branch_exit_1_clean(
        self, workspace: Path, capsys, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import cli.orcho as orcho

        run_dir = self._run_dir(workspace, "20260101_000000")
        _make_torn_halt_run(run_dir)

        def _boom(*a, **k):
            raise RuntimeError("failed to atomically replace meta.json")

        monkeypatch.setattr(orcho, "repair_run_state", _boom)
        args = self._parse(
            "20260101_000000", "--workspace", str(workspace)
        )
        rc = orcho.cmd_repair_state(args)
        captured = capsys.readouterr()
        assert rc == 1
        assert captured.err.strip()
        assert "Traceback" not in captured.err
        assert captured.out == ""


# ─────────────────────────────────────────────────────────────────────────────
# verify env — parser + handler
# ─────────────────────────────────────────────────────────────────────────────

_VERIFY_PLUGIN_TEMPLATE = '''\
PLUGIN = {{
    "verification_envs": {{
        "ci": {{
            "assertions": [
                {{"import": "{pkg}", "path_equals": "{init}"}},
            ],
        }},
    }},
    "verification": {{"default_env": "ci"}},
}}
'''


def _write_verify_project(root: Path, *, pkg: str = "proj_pkg") -> Path:
    """A project with a local package and a plugin asserting its import path."""
    project = root / "project"
    package = project / pkg
    package.mkdir(parents=True)
    init = package / "__init__.py"
    init.write_text("VALUE = 1\n", encoding="utf-8")

    plugin_dir = project / ".orcho" / "multiagent"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text(
        _VERIFY_PLUGIN_TEMPLATE.format(pkg=pkg, init=str(init)),
        encoding="utf-8",
    )
    return project


def _write_meta_run(runs_dir: Path, run_id: str, *, project: str | None) -> Path:
    d = runs_dir / run_id
    d.mkdir(parents=True)
    meta: dict = {"task": "t", "status": "done"}
    if project is not None:
        meta["project"] = project
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return d


class TestVerifyEnvParser:
    @pytest.fixture(autouse=True)
    def _import_parser(self):
        from cli.orcho import build_parser
        self.build_parser = build_parser

    def test_verify_env_subcommand_parses(self) -> None:
        parser = self.build_parser()
        args = parser.parse_args([
            "verify", "env",
            "--project", "/p",
            "--env", "ci",
            "--run-id", "20260101_000000",
            "--workspace", "/ws",
        ])
        assert args.command == "verify"
        assert args.verify_cmd == "env"
        assert args.project == "/p"
        assert args.env == "ci"
        assert args.run_id == "20260101_000000"
        assert args.workspace == "/ws"

    def test_verify_env_defaults(self) -> None:
        parser = self.build_parser()
        args = parser.parse_args(["verify", "env"])
        assert args.project is None
        assert args.env is None
        assert args.run_id is None
        assert args.workspace is None

    def test_verify_without_subcommand_shows_overview(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        parser = self.build_parser()
        args = parser.parse_args(["verify"])

        assert args.func(args) == 0
        out = capsys.readouterr().out
        assert "Verify · declared receipts" in out
        assert "orcho verify env" in out
        assert "orcho verify run --required" in out

    def test_verify_env_does_not_resolve_cli_binaries(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _break_cli_binary_lookup(monkeypatch)
        parser = self.build_parser()
        args = parser.parse_args(["verify", "env", "--env", "ci"])
        assert args.func.__name__ == "cmd_verify_env"

    def test_verify_filters_skill_shadow_chatter(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        from cli import orcho as cli_orcho
        from sdk.verify import VerifyListResult

        def fake_verify_list(**kwargs):
            print(
                "  ! skills: workspace skill 'x' (workspace) shadowed by "
                "project skill at /tmp/x"
            )
            print("  kept diagnostic")
            return VerifyListResult(run_id="r1", commands=[])

        monkeypatch.setattr(cli_orcho, "verify_list", fake_verify_list)

        rc = cli_orcho.cmd_verify_list(
            _make_args(project=None, run_id=None, workspace=None)
        )

        out = capsys.readouterr().out
        assert rc == 0
        assert "shadowed by" not in out
        assert "kept diagnostic" in out
        assert "verify list" in out


class TestCmdVerifyEnv:
    @pytest.fixture
    def runs_dir(self, tmp_path: Path, monkeypatch):
        rd = tmp_path / "runs"
        rd.mkdir()
        monkeypatch.setenv("ORCHO_RUNSPACE", str(tmp_path))
        return rd

    def test_smoke_writes_env_receipt_and_passes(
        self, tmp_path: Path, runs_dir: Path, monkeypatch, capsys,
    ) -> None:
        from cli.orcho import cmd_verify_env
        from pipeline.evidence.verification_receipt import (
            ENV_RECEIPTS_DIRNAME,
            VERIFICATION_ENV_KIND,
        )

        project = _write_verify_project(tmp_path)
        run_dir = _write_meta_run(runs_dir, "20260101_000000", project=str(project))
        # Process cwd deliberately unrelated to the checkout.
        monkeypatch.chdir(tmp_path)

        rc = cmd_verify_env(_make_args(
            project=str(project), env="ci",
            run_id="20260101_000000", workspace=None,
        ))

        out = capsys.readouterr().out
        assert rc == 0
        assert "PASS" in out

        receipt = run_dir / ENV_RECEIPTS_DIRNAME / "verify_env_ci.json"
        assert receipt.is_file()
        data = json.loads(receipt.read_text(encoding="utf-8"))
        assert data["kind"] == VERIFICATION_ENV_KIND
        assert data["env"] == "ci"
        assert data["subject"]["checkout"] == str(project)
        assert data["all_passed"] is True
        # No receipt leaked into the checkout.
        assert not (project / ENV_RECEIPTS_DIRNAME).exists()

    def test_default_env_used_when_env_omitted(
        self, tmp_path: Path, runs_dir: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_verify_env
        from pipeline.evidence.verification_receipt import ENV_RECEIPTS_DIRNAME

        project = _write_verify_project(tmp_path)
        run_dir = _write_meta_run(runs_dir, "20260101_000000", project=str(project))

        rc = cmd_verify_env(_make_args(
            project=str(project), env=None,
            run_id="20260101_000000", workspace=None,
        ))
        assert rc == 0
        assert (run_dir / ENV_RECEIPTS_DIRNAME / "verify_env_ci.json").is_file()

    def test_project_run_mismatch_errors_without_receipt(
        self, tmp_path: Path, runs_dir: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_verify_env
        from pipeline.evidence.verification_receipt import ENV_RECEIPTS_DIRNAME

        project = _write_verify_project(tmp_path)
        # Newest run records a DIFFERENT project; no --run-id is passed.
        other = tmp_path / "somewhere_else"
        other.mkdir()
        run_dir = _write_meta_run(runs_dir, "20260101_000000", project=str(other))

        rc = cmd_verify_env(_make_args(
            project=str(project), env="ci",
            run_id=None, workspace=None,
        ))

        captured = capsys.readouterr()
        assert rc != 0
        assert "does not match run" in captured.err
        # NOTHING written into the resolved run dir.
        assert not (run_dir / ENV_RECEIPTS_DIRNAME).exists()

    def test_missing_meta_project_errors_without_receipt(
        self, tmp_path: Path, runs_dir: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_verify_env
        from pipeline.evidence.verification_receipt import ENV_RECEIPTS_DIRNAME

        project = _write_verify_project(tmp_path)
        run_dir = _write_meta_run(runs_dir, "20260101_000000", project=None)

        rc = cmd_verify_env(_make_args(
            project=str(project), env="ci",
            run_id="20260101_000000", workspace=None,
        ))
        assert rc != 0
        assert not (run_dir / ENV_RECEIPTS_DIRNAME).exists()

    def test_no_contract_errors_without_receipt(
        self, tmp_path: Path, runs_dir: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_verify_env
        from pipeline.evidence.verification_receipt import ENV_RECEIPTS_DIRNAME

        # A project with no plugin → no contract.
        project = tmp_path / "bare"
        project.mkdir()
        run_dir = _write_meta_run(runs_dir, "20260101_000000", project=str(project))

        rc = cmd_verify_env(_make_args(
            project=str(project), env=None,
            run_id="20260101_000000", workspace=None,
        ))
        assert rc != 0
        assert not (run_dir / ENV_RECEIPTS_DIRNAME).exists()

    def test_unknown_env_errors_without_receipt(
        self, tmp_path: Path, runs_dir: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_verify_env
        from pipeline.evidence.verification_receipt import ENV_RECEIPTS_DIRNAME

        project = _write_verify_project(tmp_path)
        run_dir = _write_meta_run(runs_dir, "20260101_000000", project=str(project))

        rc = cmd_verify_env(_make_args(
            project=str(project), env="nope",
            run_id="20260101_000000", workspace=None,
        ))
        assert rc != 0
        assert not (run_dir / ENV_RECEIPTS_DIRNAME).exists()


# ── verify list / run (Stage 3) ────────────────────────────────────────────

_VERIFY_CMD_PLUGIN = '''\
PLUGIN = {
    "verification_envs": {"ci": {}},
    "verification": {
        "default_env": "ci",
        "required": ["req"],
        "commands": {
            "show_cwd": {"run": "python -c \\"import os;print(os.getcwd())\\""},
            "boom": {"run": "python -c \\"import sys;sys.exit(4)\\""},
            "req": {"run": "python -c \\"pass\\"", "parity": "differential"},
        },
    },
}
'''


def _write_verify_cmd_project(root: Path) -> Path:
    project = root / "cmd_project"
    plugin_dir = project / ".orcho" / "multiagent"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text(_VERIFY_CMD_PLUGIN, encoding="utf-8")
    return project


def _init_worktree_repo(repo: Path) -> str:
    import subprocess

    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@orcho.invalid"], cwd=repo, check=True,
    )
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True,
    )
    (repo / "README.md").write_text("# x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    r = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo,
        capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


def _write_meta_run_worktree(
    runs_dir: Path, run_id: str, *, project: Path, worktree: dict | None,
) -> Path:
    d = runs_dir / run_id
    d.mkdir(parents=True)
    meta: dict = {"task": "t", "status": "done", "project": str(project)}
    if worktree is not None:
        meta["worktree"] = worktree
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return d


class TestVerifyListRunParser:
    @pytest.fixture(autouse=True)
    def _import_parser(self):
        from cli.orcho import build_parser
        self.build_parser = build_parser

    def test_verify_list_parses(self) -> None:
        parser = self.build_parser()
        args = parser.parse_args([
            "verify", "list", "--project", "/p",
            "--run-id", "20260101_000000", "--workspace", "/ws",
        ])
        assert args.verify_cmd == "list"
        assert args.func.__name__ == "cmd_verify_list"
        assert args.project == "/p"

    def test_verify_run_parses_names_and_required(self) -> None:
        parser = self.build_parser()
        args = parser.parse_args(["verify", "run", "lint", "test", "--required"])
        assert args.verify_cmd == "run"
        assert args.func.__name__ == "cmd_verify_run"
        assert args.names == ["lint", "test"]
        assert args.required is True

    def test_verify_run_parses_include_manual(self) -> None:
        parser = self.build_parser()
        args = parser.parse_args(["verify", "run", "--include-manual"])
        assert args.verify_cmd == "run"
        assert args.func.__name__ == "cmd_verify_run"
        assert args.include_manual is True

    def test_verify_run_defaults(self) -> None:
        parser = self.build_parser()
        args = parser.parse_args(["verify", "run"])
        assert args.names == []
        assert args.required is False
        assert args.include_manual is False

    def test_verify_run_rejects_env_flag(self) -> None:
        parser = self.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["verify", "run", "--env", "ci"])


class TestCmdVerifyListRun:
    @pytest.fixture
    def runs_dir(self, tmp_path: Path, monkeypatch):
        rd = tmp_path / "runs"
        rd.mkdir()
        monkeypatch.setenv("ORCHO_RUNSPACE", str(tmp_path))
        return rd

    def test_list_prints_commands_without_executing(
        self, tmp_path: Path, runs_dir: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_verify_list
        from pipeline.evidence.verification_receipt import COMMAND_RECEIPTS_DIRNAME

        project = _write_verify_cmd_project(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        run_dir = _write_meta_run_worktree(
            runs_dir, "20260101_000000", project=project,
            worktree={"path": str(worktree), "base_ref": "abc"},
        )

        rc = cmd_verify_list(_make_args(
            project=str(project), run_id="20260101_000000", workspace=None,
        ))
        out = capsys.readouterr().out
        assert rc == 0
        assert "show_cwd" in out
        assert "req" in out
        assert "$ python -c" in out
        assert "['python'" not in out
        assert "Preview only" in out
        assert "orcho verify run --required" in out
        # Nothing executed.
        assert not (run_dir / COMMAND_RECEIPTS_DIRNAME).exists()

    def test_run_executes_in_worktree_and_writes_receipt(
        self, tmp_path: Path, runs_dir: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_verify_run
        from pipeline.evidence.verification_receipt import COMMAND_RECEIPTS_DIRNAME

        project = _write_verify_cmd_project(tmp_path)
        worktree = tmp_path / "wt"
        head = _init_worktree_repo(worktree)
        run_dir = _write_meta_run_worktree(
            runs_dir, "20260101_000000", project=project,
            worktree={"path": str(worktree), "base_ref": "base-xyz"},
        )

        rc = cmd_verify_run(_make_args(
            project=str(project), run_id="20260101_000000", workspace=None,
            names=["show_cwd"], required=False,
        ))
        out = capsys.readouterr().out
        assert rc == 0
        assert "PASS" in out
        # Receipt landed and the command executed inside the worktree.
        receipt = run_dir / COMMAND_RECEIPTS_DIRNAME / "show_cwd.json"
        assert receipt.is_file()
        data = json.loads(receipt.read_text(encoding="utf-8"))
        assert data["cwd"] == str(worktree)
        assert data["git"]["checkout_head"] == head

    def test_run_failing_command_exits_1(
        self, tmp_path: Path, runs_dir: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_verify_run

        project = _write_verify_cmd_project(tmp_path)
        worktree = tmp_path / "wt"
        _init_worktree_repo(worktree)
        _write_meta_run_worktree(
            runs_dir, "20260101_000000", project=project,
            worktree={"path": str(worktree), "base_ref": "base-xyz"},
        )

        rc = cmd_verify_run(_make_args(
            project=str(project), run_id="20260101_000000", workspace=None,
            names=["boom"], required=False,
        ))
        out = capsys.readouterr().out
        assert rc == 1
        assert "FAIL" in out

    def test_run_required_differential_shows_heads(
        self, tmp_path: Path, runs_dir: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_verify_run

        project = _write_verify_cmd_project(tmp_path)
        worktree = tmp_path / "wt"
        head = _init_worktree_repo(worktree)
        _write_meta_run_worktree(
            runs_dir, "20260101_000000", project=project,
            worktree={"path": str(worktree), "base_ref": "base-xyz"},
        )

        rc = cmd_verify_run(_make_args(
            project=str(project), run_id="20260101_000000", workspace=None,
            names=[], required=True,
        ))
        out = capsys.readouterr().out
        assert rc == 0
        assert "parity=differential" in out
        assert head in out
        assert "base-xyz" in out

    def test_run_unknown_command_exits_2_without_write(
        self, tmp_path: Path, runs_dir: Path, capsys,
    ) -> None:
        from cli.orcho import cmd_verify_run
        from pipeline.evidence.verification_receipt import COMMAND_RECEIPTS_DIRNAME

        project = _write_verify_cmd_project(tmp_path)
        run_dir = _write_meta_run_worktree(
            runs_dir, "20260101_000000", project=project, worktree=None,
        )

        rc = cmd_verify_run(_make_args(
            project=str(project), run_id="20260101_000000", workspace=None,
            names=["ghost"], required=False,
        ))
        assert rc == 2
        assert not (run_dir / COMMAND_RECEIPTS_DIRNAME).exists()


class TestVerifyRunNameAlias:
    """``--name`` is a thin alias for a single positional command: it maps to
    the same ``verify_run(commands=[name])`` dispatch, and the four invalid
    combinations are rejected before any execution."""

    def test_parser_accepts_name_alias(self) -> None:
        from cli.orcho import build_parser

        args = build_parser().parse_args(["verify", "run", "--name", "lint"])
        assert args.verify_cmd == "run"
        assert args.func.__name__ == "cmd_verify_run"
        assert args.name == "lint"
        assert args.names == []

    @staticmethod
    def _patch_verify_run(monkeypatch):
        """Patch the sdk verify_run imported into cli.orcho and capture kwargs.

        ``format_verify_run`` is stubbed too so the fake result need not be a
        real ``VerifyRunResult``.
        """
        import cli.orcho as orcho_cli

        calls: list[dict] = []

        class _Result:
            all_passed = True

        def _fake_verify_run(**kwargs):
            calls.append(kwargs)
            return _Result()

        monkeypatch.setattr(orcho_cli, "verify_run", _fake_verify_run)
        monkeypatch.setattr(orcho_cli, "format_verify_run", lambda result: "")
        return calls

    def test_name_alias_dispatch_matches_positional(self, monkeypatch) -> None:
        from cli.orcho import cmd_verify_run

        calls = self._patch_verify_run(monkeypatch)

        rc_name = cmd_verify_run(_make_args(
            project="/p", run_id="r", workspace=None,
            name="lint", names=[], required=False,
        ))
        rc_pos = cmd_verify_run(_make_args(
            project="/p", run_id="r", workspace=None,
            names=["lint"], required=False,
        ))

        assert rc_name == 0
        assert rc_pos == 0
        assert len(calls) == 2
        assert calls[0]["commands"] == ["lint"]
        assert calls[0]["required_only"] is False
        # Both forms produce an identical dispatch.
        assert calls[0]["commands"] == calls[1]["commands"]
        assert calls[0]["required_only"] == calls[1]["required_only"]

    def test_required_unchanged_calls_required_only(self, monkeypatch) -> None:
        from cli.orcho import cmd_verify_run

        calls = self._patch_verify_run(monkeypatch)

        rc = cmd_verify_run(_make_args(
            project="/p", run_id="r", workspace=None,
            names=[], required=True,
        ))

        assert rc == 0
        assert len(calls) == 1
        assert calls[0]["required_only"] is True
        assert calls[0]["commands"] is None

    def test_positional_with_name_rejected(self, monkeypatch, capsys) -> None:
        from cli.orcho import cmd_verify_run

        calls = self._patch_verify_run(monkeypatch)

        rc = cmd_verify_run(_make_args(
            project="/p", run_id="r", workspace=None,
            name="lint", names=["test"], required=False,
        ))

        assert rc == 2
        assert calls == []  # nothing executed
        err = capsys.readouterr().err
        assert "--name" in err and "positional" in err

    def test_required_with_positional_rejected(self, monkeypatch, capsys) -> None:
        from cli.orcho import cmd_verify_run

        calls = self._patch_verify_run(monkeypatch)

        rc = cmd_verify_run(_make_args(
            project="/p", run_id="r", workspace=None,
            names=["test"], required=True,
        ))

        assert rc == 2
        assert calls == []
        err = capsys.readouterr().err
        assert "--required" in err and "positional" in err

    def test_required_with_name_rejected(self, monkeypatch, capsys) -> None:
        from cli.orcho import cmd_verify_run

        calls = self._patch_verify_run(monkeypatch)

        rc = cmd_verify_run(_make_args(
            project="/p", run_id="r", workspace=None,
            name="lint", names=[], required=True,
        ))

        assert rc == 2
        assert calls == []
        err = capsys.readouterr().err
        assert "--required" in err and "--name" in err

    def test_empty_name_rejected(self, monkeypatch, capsys) -> None:
        from cli.orcho import cmd_verify_run

        calls = self._patch_verify_run(monkeypatch)

        rc = cmd_verify_run(_make_args(
            project="/p", run_id="r", workspace=None,
            name="   ", names=[], required=False,
        ))

        assert rc == 2
        assert calls == []
        err = capsys.readouterr().err
        assert "--name must not be empty" in err


class TestFormatVerifyRunAgainstLine:
    """``format_verify_run`` renders an ``against:`` line for commands tested
    against declared dependency repos (ADR 0084), and stays byte-identical for
    commands with no dependencies. The formatter emits no ANSI, so the
    process-level color override is irrelevant here."""

    @staticmethod
    def _outcome(**overrides):
        from sdk.verify import CommandOutcome

        base = dict(
            command="lint", env="ci", exit_code=0, passed=True,
            parity="absolute", receipt_path=Path("/r/lint.json"),
            duration_s=0.1, stdout_tail="", stderr_tail="",
            checkout_head=None, baseline_head=None, dependencies=(),
        )
        base.update(overrides)
        return CommandOutcome(**base)

    def _format(self, outcome):
        from cli._formatters import format_verify_run
        from sdk.verify import VerifyRunResult

        return format_verify_run(
            VerifyRunResult(run_id="r", outcomes=[outcome], all_passed=True),
        )

    def test_against_line_lists_dependency_commits(self) -> None:
        out = self._format(self._outcome(
            dependencies=("orcho-core@abc1234", "shared@def4567"),
        ))
        assert "        against: orcho-core@abc1234 + shared@def4567" in out

    def test_dirty_marker_rendered(self) -> None:
        out = self._format(self._outcome(dependencies=("shared@def4567+dirty",)))
        assert "        against: shared@def4567+dirty" in out

    def test_no_against_line_without_dependencies(self) -> None:
        out = self._format(self._outcome(dependencies=()))
        assert "against:" not in out

    def test_against_line_coexists_with_differential_heads(self) -> None:
        out = self._format(self._outcome(
            parity="differential",
            checkout_head="head111", baseline_head="base222",
            dependencies=("shared@def4567",),
        ))
        assert "checkout_head: head111" in out
        assert "baseline_head: base222" in out
        assert "        against: shared@def4567" in out


@pytest.fixture
def _restore_color_override():
    """Snapshot and restore the process-level color override."""
    from core.io.ansi import get_color_enabled, set_color_enabled

    previous = get_color_enabled()
    try:
        yield
    finally:
        set_color_enabled(previous)


class TestOnboardingColor:
    """Coloring routes through ``paint(color=None)`` (override + auto-detect)."""

    def test_strip_ansi_render_equals_plain_quick_help(
        self, _restore_color_override,
    ) -> None:
        from cli._help import QUICK_HELP, render_quick_help
        from core.io.ansi import set_color_enabled, strip_ansi

        # Force color on so render emits ANSI; stripping must recover the
        # canonical plain onboarding text byte-for-byte.
        set_color_enabled(True)
        assert strip_ansi(render_quick_help()) == QUICK_HELP

    def test_override_true_emits_ansi_under_capsys(
        self,
        capsys: pytest.CaptureFixture[str],
        _restore_color_override,
    ) -> None:
        from cli._help import print_quick_help, render_quick_help
        from core.io.ansi import C, set_color_enabled

        set_color_enabled(True)
        rendered = render_quick_help()
        assert C.RESET in rendered

        print_quick_help()
        out = capsys.readouterr().out
        assert C.RESET in out

    def test_override_false_is_plain(
        self,
        capsys: pytest.CaptureFixture[str],
        _restore_color_override,
    ) -> None:
        from cli._help import print_quick_help, render_quick_help
        from core.io.ansi import set_color_enabled, strip_ansi

        set_color_enabled(False)
        rendered = render_quick_help()
        assert strip_ansi(rendered) == rendered

        print_quick_help()
        out = capsys.readouterr().out
        assert strip_ansi(out) == out

    def test_no_override_non_tty_is_plain(
        self,
        capsys: pytest.CaptureFixture[str],
        _restore_color_override,
    ) -> None:
        from cli._help import render_quick_help
        from core.io.ansi import set_color_enabled, strip_ansi

        # No override + capsys (non-TTY) stdout -> auto-detect yields plain.
        set_color_enabled(None)
        rendered = render_quick_help()
        assert strip_ansi(rendered) == rendered

    def test_no_color_env_is_plain(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _restore_color_override,
    ) -> None:
        from cli._help import render_quick_help
        from core.io.ansi import set_color_enabled, strip_ansi

        set_color_enabled(None)
        monkeypatch.setenv("NO_COLOR", "1")
        rendered = render_quick_help()
        assert strip_ansi(rendered) == rendered


class TestVerboseHelpGroups:
    """`orcho help --verbose` groups subcommands by COMMAND_GROUPS."""

    def test_verbose_help_groups_and_covers_all_subcommands(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        from cli._help import COMMAND_GROUPS
        from cli.orcho import build_parser
        from core.io.ansi import strip_ansi

        parser = build_parser()
        sub_choices = set(parser._subparsers._group_actions[0].choices)

        args = parser.parse_args(["help", "--verbose"])
        rc = args.func(args)
        out = strip_ansi(capsys.readouterr().out)

        assert rc == 0

        # Every category header is present, and at least one command's help
        # body (its [NAME] banner) from each group is printed.
        for title, commands in COMMAND_GROUPS:
            assert f"{title.upper()}" in out, title
            assert any(f"[{name.upper()}]" in out for name, _ in commands), title

        # The union of printed [NAME] banners covers every subcommand,
        # including service-only ``help`` via the 'Other' group.
        for name in sub_choices:
            assert f"[{name.upper()}]" in out, name
        assert "OTHER" in out
        assert "[HELP]" in out


class TestTuiDispatch:
    """``orcho tui`` delegates to the optional ``orcho-tui`` package, mirroring
    ``orcho web`` → ``orcho-web``: a lazy, guarded import so ``orcho-core`` keeps
    no hard dependency on its sibling."""

    def test_not_installed_prints_install_hint(self, monkeypatch, capsys) -> None:
        import argparse
        import builtins

        from cli.orcho import cmd_tui

        real_import = builtins.__import__

        def _no_orcho_tui(name, *a, **k):
            if name.startswith("orcho_tui"):
                raise ImportError("no orcho_tui")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _no_orcho_tui)
        args = argparse.Namespace(run_id=None, run_dir="/x", follow=False, replay=False)
        assert cmd_tui(args) == 1
        assert "orcho-tui is not installed" in capsys.readouterr().err

    def test_dispatch_translates_argv(self, monkeypatch) -> None:
        import argparse
        import sys
        import types

        from cli.orcho import cmd_tui

        seen: dict[str, list[str]] = {}
        fake = types.ModuleType("orcho_tui.cli")
        fake.main = lambda argv: (seen.__setitem__("argv", argv), 0)[1]
        pkg = types.ModuleType("orcho_tui")
        monkeypatch.setitem(sys.modules, "orcho_tui", pkg)
        monkeypatch.setitem(sys.modules, "orcho_tui.cli", fake)

        args = argparse.Namespace(run_id="r1", run_dir=None, follow=True, replay=False)
        assert cmd_tui(args) == 0
        assert seen["argv"] == ["--run-id", "r1", "--follow"]
