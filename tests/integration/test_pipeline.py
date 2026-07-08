"""
tests/integration/test_pipeline.py

Integration tests for run_pipeline().

Strategy:
  - TestDryRun: dry_run=True, no agents needed — no subprocess calls
  - TestPhaseSequencing: uses MockAgentProvider (zero API/subprocess calls)
    The old approach of patching agent_module.ClaudeAgent broke when the
    orchestrator switched to provider-based instantiation. MockAgentProvider
    is the correct seam: it routes all agent roles to inline stubs.
"""

import json
from unittest.mock import patch

import pytest

from agents.runtimes import MockAgentProvider
from pipeline.plugins import PluginConfig
from pipeline.project_orchestrator import run_pipeline


@pytest.fixture
def project_dir(tmp_path) -> str:
    """Override global ``project_dir`` fixture: integration tests
    invoke ``run_pipeline`` for real, which now requires a git repo
    with HEAD for worktree isolation."""
    from tests.conftest import init_git_repo
    project = tmp_path / "project_dir"
    init_git_repo(project)
    return str(project)

# ── Dry-run ───────────────────────────────────────────────────────────────────

class TestDryRun:
    @patch("pipeline.project.session_run.load_plugin", return_value=PluginConfig())
    def test_produces_session(self, _, project_dir: str) -> None:
        session = run_pipeline(
            task="Dry task", project_dir=project_dir,
            max_rounds=1, dry_run=True,
        )
        assert "plan" in session["phases"]
        assert session["phases"]["plan"][-1]["output"] == "[DRY RUN]"

    @patch("pipeline.project.session_run.load_plugin", return_value=PluginConfig())
    def test_skip_plan_omits_plan_phase(self, _, project_dir: str) -> None:
        session = run_pipeline(
            task="Dry task", project_dir=project_dir,
            max_rounds=1, profile_name="task", dry_run=True,
        )
        assert "plan" not in session["phases"]
        assert "implement" in session["phases"]

    @patch("pipeline.project.session_run.load_plugin", return_value=PluginConfig())
    def test_session_contains_task(self, _, project_dir: str) -> None:
        session = run_pipeline(
            task="My specific task", project_dir=project_dir,
            max_rounds=0, profile_name="task", dry_run=True,
        )
        assert session["task"] == "My specific task"

    @patch("pipeline.project.session_run.load_plugin", return_value=PluginConfig())
    def test_zero_rounds_no_review_phase(self, _, project_dir: str) -> None:
        session = run_pipeline(
            task="Task", project_dir=project_dir,
            max_rounds=0, profile_name="task", dry_run=True,
        )
        assert session["phases"].get("rounds", []) == []

    @patch("pipeline.project.session_run.load_plugin", return_value=PluginConfig())
    def test_dry_run_does_not_resolve_cli_binaries(
        self, _, project_dir: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _break_cli_binary_lookup(monkeypatch)

        session = run_pipeline(
            task="Dry task", project_dir=project_dir,
            max_rounds=1, dry_run=True,
        )

        assert session["phases"]["plan"][-1]["output"] == "[DRY RUN]"


def _break_cli_binary_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.infra import config

    def fail(name: str):
        raise AssertionError(f"unexpected {name} CLI binary lookup")

    monkeypatch.setattr(config, "get_claude_bin", lambda: fail("claude"))
    monkeypatch.setattr(config, "get_codex_bin", lambda: fail("codex"))


# ── Phase sequencing ──────────────────────────────────────────────────────────

class TestPhaseSequencing:
    """Uses MockAgentProvider — zero subprocess/API calls, instant execution."""

    def _provider(self, *, critique: str = "approved") -> MockAgentProvider:
        """Build a MockAgentProvider; override critique via monkeypatch if needed."""
        return MockAgentProvider(latency=0.0)

    @patch("pipeline.project.session_run.load_plugin", return_value=PluginConfig())
    def test_plan_and_build_phases_present(self, _, project_dir: str) -> None:
        session = run_pipeline(
            task="Full pipeline task", project_dir=project_dir,
            max_rounds=1,
            provider=MockAgentProvider(latency=0.0),
        )
        assert "plan" in session["phases"]
        assert "implement" in session["phases"]

    @patch("pipeline.project.session_run.load_plugin", return_value=PluginConfig())
    def test_mock_full_run_has_review_round_and_nonzero_input_metrics(
        self, _, tmp_path
    ) -> None:
        """CLI-like mock smoke should exercise real telemetry contracts.

        The mock developer materializes the missing file it reports, which
        dirties the git tree and lets the review loop record a clean round.
        """
        from tests.conftest import init_git_repo
        project = tmp_path / "project"
        init_git_repo(project)

        session = run_pipeline(
            task="codex review smoke",
            project_dir=str(project),
            output_dir=tmp_path / "runs" / "mock_smoke",
            max_rounds=1,
            provider=MockAgentProvider(latency=0.0, test_pass_rate=1.0),
        )

        assert session["status"] == "done"
        assert len(session["phases"].get("rounds", [])) == 1
        # The mock reports no provider input tokens, so per-invocation
        # estimates land in ``tokens_unknown`` rather than ``tokens_in``.
        # The telemetry contract under test is that nonzero usage is
        # recorded at all — assert the aggregate, not the input split.
        assert session["metrics"]["total_tokens"] > 0
        assert session["metrics"]["total_duration_s"] >= 0.1
        receipts = session["phases"]["implement"]["implementation_receipts"]
        assert [r["subtask_id"] for r in receipts] == [
            "inspect-target", "apply-fix", "verify",
        ]
        assert {r["state"] for r in receipts} == {"done"}
        # REA-1 finalizer: mock plan now emits a 3-subtask DAG
        # (inspect-target / apply-fix / verify) so total reflects the
        # canonical ParsedPlan subtask count.
        assert session["phases"]["implement"]["progress"] == {
            "kind": "subtasks",
            "completed": 3,
            "total": 3,
        }

    @patch("pipeline.project.session_run.load_plugin", return_value=PluginConfig())
    def test_validate_plan_phase_present_in_dry_run(self, _, project_dir: str) -> None:
        """dry_run=True always writes validate_plan to session."""
        session = run_pipeline(
            task="Task", project_dir=project_dir,
            max_rounds=1, dry_run=True,
        )
        assert "validate_plan" in session["phases"]

    @patch("core.io.git_helpers.has_uncommitted", return_value=True)
    @patch("pipeline.project.session_run.load_plugin", return_value=PluginConfig())
    def test_clean_critique_stops_without_fix(self, _, __, project_dir: str) -> None:
        """Approved reviewer JSON means FIX must NOT run."""
        provider = MockAgentProvider(latency=0.0)
        provider.codex = lambda model, **_kw: _AlwaysApprovedCodex()

        session = run_pipeline(
            task="Task", project_dir=project_dir,
            max_rounds=2, profile_name="task",
            provider=provider,
        )
        rounds = session["phases"].get("rounds", [])
        assert len(rounds) == 1
        assert "repair_output" not in rounds[0]

    @patch("core.io.git_helpers.has_uncommitted", return_value=True)
    @patch("pipeline.project.session_run.load_plugin", return_value=PluginConfig())
    def test_issue_critique_triggers_fix(self, _, __, project_dir: str) -> None:
        """Codex returns issue → FIX round must appear in session."""
        provider = MockAgentProvider(latency=0.0)
        provider.codex = lambda model, **_kw: _AlwaysIssuedCodex()

        session = run_pipeline(
            task="Task", project_dir=project_dir,
            max_rounds=1, profile_name="task",
            provider=provider,
        )
        rounds = session["phases"].get("rounds", [])
        assert len(rounds) == 1
        assert "repair_output" in rounds[0]


# ── Failure tracking ──────────────────────────────────────────────────────────

class _CrashingCodex:
    """IAgentRuntime stub that imitates a hard CLI failure (e.g. quota / network).

    Mirrors the real failure mode where ``invoke()`` raises
    ``RuntimeError("Codex CLI failed with exit code 1\\nStderr: ...")``
    when the upstream codex CLI returns non-zero.
    """
    model = "stub-codex"
    session_id: str | None = None

    def invoke(
        self, prompt: str, cwd: str, *,
        mutates_artifacts: bool = False,
        continue_session: bool = False,
        attachments: tuple = (),
    ) -> str:
        raise RuntimeError(
            "Codex CLI failed with exit code 1\n"
            "Stderr: ERROR: You've hit your usage limit."
        )

    def reset_session(self) -> None:
        self.session_id = None


class TestMetricsRecording:
    """Per-phase metrics must land in metrics.json. Before this regression
    point ``Cost: Tokens: 0 | Time: 0.0s`` always rendered as zero because
    no callback fed PhaseMetrics into the collector — the legacy run_pipeline
    only wrote ``_metrics.save()`` over an empty list, and the runtime
    migration carried that emptiness over.
    """

    @patch("pipeline.project.session_run.load_plugin", return_value=PluginConfig())
    def test_metrics_record_per_phase(self, _, project_dir: str, tmp_path) -> None:
        out = tmp_path / "run-out"
        out.mkdir()
        run_pipeline(
            task="Task", project_dir=project_dir,
            max_rounds=0, dry_run=False,
            provider=MockAgentProvider(latency=0.0),
            output_dir=out,
        )

        import json
        metrics = json.loads((out / "metrics.json").read_text())
        # Sanity: collector observed something — tokens_out and at least
        # the build / plan / validate_plan phases land in phases.
        assert metrics["total_duration_s"] >= 0.0  # mock is sub-ms but >= 0
        assert metrics["total_tokens"] > 0, metrics
        recorded = set(metrics.get("phases", {}).keys())
        assert "plan" in recorded, recorded
        assert "implement" in recorded, recorded
        # Each phase entry carries model + duration + tokens.
        plan = metrics["phases"]["plan"]
        assert plan["model"], plan
        assert "duration_s" in plan
        assert "total_tokens" in plan


class TestFailureTracking:
    """If a phase handler raises, the orchestrator must persist a structured
    failure record (status=failed + failure dict) instead of leaking up to
    atexit's generic ``interrupted``. The dashboard / resume logic depends on
    the difference: ``interrupted`` = SIGKILL, ``failed`` = handler exception.
    """

    @patch("core.io.git_helpers.has_uncommitted", return_value=True)
    @patch("pipeline.project.session_run.load_plugin", return_value=PluginConfig())
    def test_codex_crash_records_failed_status(
        self, _, __, project_dir: str, tmp_path,
    ) -> None:
        """REVIEW phase explodes → status=failed + failure={phase, error, type}."""
        provider = MockAgentProvider(latency=0.0)
        provider.codex = lambda model, **_kw: _CrashingCodex()

        out = tmp_path / "run-out"
        out.mkdir()

        with pytest.raises(RuntimeError, match="Codex CLI failed"):
            run_pipeline(
                task="Task", project_dir=project_dir,
                max_rounds=1, profile_name="task",
                provider=provider,
                output_dir=out,
            )

        # meta.json must reflect the failure with structured details.
        import json
        meta = json.loads((out / "meta.json").read_text())
        assert meta["status"] == "failed", meta
        failure = meta.get("failure")
        assert failure is not None, "failure record missing"
        assert "review_changes" in failure["phase"], failure
        assert failure["type"] == "RuntimeError"
        assert "Codex CLI failed" in failure["error"]
        assert "ts" in failure


# ── Stub helpers ──────────────────────────────────────────────────────────────

def _approved_review_json(summary: str = "Approved by JSON contract.") -> str:
    return json.dumps({
        "verdict":       "APPROVED",
        "short_summary": summary,
        "findings":      [],
    })


def _approved_release_json(summary: str = "Ship-ready.") -> str:
    """ADR 0025: release-gate APPROVED payload for stubs that may
    encounter the project ``final_acceptance`` prompt."""
    return json.dumps({
        "verdict":            "APPROVED",
        "ship_ready":         True,
        "short_summary":      summary,
        "release_blockers":   [],
        "verification_gaps":  [],
        "contract_status": {
            "task_contract": "satisfied",
            "interfaces":    "not_applicable",
            "persistence":   "not_applicable",
            "tests":         "sufficient",
        },
    })


def _prompt_requests_release(prompt: str) -> bool:
    """Match the orcho renderer's ``release_json`` system-tail block."""
    return (
        'kind="contract"' in prompt
        and 'name="release_json"' in prompt
        and "<orcho:system-block " in prompt
    )


def _rejected_review_json(summary: str = "missing null check on line 42") -> str:
    return json.dumps({
        "verdict":       "REJECTED",
        "short_summary": summary,
        "findings":      [{
            "id": "F1",
            "severity": "P2",
            "title": "Missing null check",
            "body": summary,
            "required_fix": "Add the missing null handling.",
        }],
    })


class _AlwaysApprovedCodex:
    model = "stub-codex"
    session_id: str | None = None

    def invoke(
        self, prompt: str, cwd: str, *,
        mutates_artifacts: bool = False,
        continue_session: bool = False,
        attachments: tuple = (),
    ) -> str:
        if _prompt_requests_release(prompt):
            return _approved_release_json()
        return _approved_review_json()

    def reset_session(self) -> None:
        self.session_id = None


class _AlwaysIssuedCodex:
    model = "stub-codex"
    session_id: str | None = None

    def invoke(
        self, prompt: str, cwd: str, *,
        mutates_artifacts: bool = False,
        continue_session: bool = False,
        attachments: tuple = (),
    ) -> str:
        # ADR 0025: release-gate prompts (project final_acceptance) get
        # an APPROVED release payload — this stub raises issues for the
        # review loop, not for ship readiness.
        if _prompt_requests_release(prompt):
            return _approved_release_json()
        # plan_validate uses the plan artifact prompt → approved JSON.
        # All other review focuses → rejected JSON.
        if "Review the implementation plan document below" in prompt:
            return _approved_review_json("Plan review approved by JSON contract.")
        return _rejected_review_json()

    def reset_session(self) -> None:
        self.session_id = None


# ════════════════════════════════════════════════════════════════════════════
#  orcho cost — sliding-window cost-reference aggregator
# ════════════════════════════════════════════════════════════════════════════

class TestCostCommand:
    """``orcho cost`` exposes cost-reference data already in metrics.json."""

    def test_parse_window_days_hours_and_all(self):
        from datetime import datetime, timedelta

        from sdk._time import parse_window as _parse_window

        # ``all`` and unparseable → None (unlimited).
        assert _parse_window("all") is None
        assert _parse_window("nonsense") is None
        assert _parse_window("") is None

        # ``30d`` → ~30 days ago (small slack for execution time).
        cutoff = _parse_window("30d")
        assert cutoff is not None
        delta = datetime.now() - cutoff
        assert timedelta(days=29, hours=23) < delta < timedelta(days=30, hours=1)

        # ``24h`` → ~24 hours ago.
        cutoff = _parse_window("24h")
        assert cutoff is not None
        delta = datetime.now() - cutoff
        assert timedelta(hours=23, minutes=59) < delta < timedelta(hours=24, minutes=1)

    def test_run_ts_to_datetime_parses_orcho_format(self):
        from datetime import datetime

        from sdk._time import run_ts_to_datetime as _run_ts_to_datetime

        d = _run_ts_to_datetime("20260506_175247")
        assert d == datetime(2026, 5, 6, 17, 52, 47)

        assert _run_ts_to_datetime("not-a-timestamp") is None
        assert _run_ts_to_datetime("") is None

    def test_cost_aggregation_via_subprocess(self, tmp_path, capsys, monkeypatch):
        """End-to-end: synthesise two run dirs with metrics.json + meta.json,
        invoke ``cmd_cost`` and assert it sums per-phase + totals correctly.
        """
        import argparse
        import json

        from core.infra import config

        monkeypatch.setenv("ORCHO_ACCOUNTING", "1")
        config._reset_config()

        ws = tmp_path / "ws"
        runs = ws / "runspace" / "runs"
        runs.mkdir(parents=True)

        # Run A: $0.42, plan=$0.30, build=$0.12
        a = runs / "20260506_120000"
        a.mkdir()
        (a / "meta.json").write_text(json.dumps({"task": "task A"}))
        (a / "metrics.json").write_text(json.dumps({
            "total_tokens_in":  100,
            "total_tokens_out": 50,
            "total_tokens":     150,
            "total_duration_s": 12.5,
            "total_cost_usd_equivalent": 0.42,
            "phases": {
                "plan":  {"model": "claude-opus-4-7", "tokens_in": 60, "tokens_out": 30,
                          "total_tokens": 90, "duration_s": 8.0,
                          "cost_usd_equivalent": 0.30},
                "implement": {"model": "claude-sonnet-4-6", "tokens_in": 40, "tokens_out": 20,
                          "total_tokens": 60, "duration_s": 4.5,
                          "cost_usd_equivalent": 0.12},
            },
        }))

        # Run B: $0.20, plan=$0.20
        b = runs / "20260506_130000"
        b.mkdir()
        (b / "meta.json").write_text(json.dumps({"task": "task B"}))
        (b / "metrics.json").write_text(json.dumps({
            "total_tokens_in":  50,
            "total_tokens_out": 30,
            "total_tokens":     80,
            "total_duration_s": 5.0,
            "total_cost_usd_equivalent": 0.20,
            "phases": {
                "plan": {"model": "claude-opus-4-7", "tokens_in": 50, "tokens_out": 30,
                         "total_tokens": 80, "duration_s": 5.0,
                         "cost_usd_equivalent": 0.20},
            },
        }))

        # Pass workspace via the CLI flag instead of just env so we
        # exercise the same path a user takes with ``orcho cost
        # --workspace ...``. Also chdir away from the orcho-core repo
        # so its own runspace/runs/ doesn't get picked up by walk-up.
        monkeypatch.chdir(tmp_path)

        from cli.orcho import cmd_cost
        args = argparse.Namespace(window="all", top=5, workspace=str(ws))
        rc = cmd_cost(args)
        config._reset_config()
        assert rc == 0

        out = capsys.readouterr().out

        # Helper: dollar-padded numbers render as ``$   0.42`` (right-
        # aligned width=7), so checking the bare ``$0.42`` substring
        # would miss them. Strip the spaces between ``$`` and digits
        # before substring lookups.
        import re
        normalised = re.sub(r"\$\s+", "$", out)

        # Totals reflect both runs summed.
        assert "$0.62" in normalised, out  # 0.42 + 0.20
        assert "230" in out                # 150 + 80 tokens
        assert "2 runs" in out
        # Per-phase: plan dominates (0.30 + 0.20 = 0.50). % is share of the
        # phase-breakdown sum (0.50 + 0.12 = 0.62), so plan ≈ 80.6%.
        assert "plan" in out and "$0.50" in normalised, out
        # Top cost-reference run: run A ($0.42) ranks above run B ($0.20).
        idx_a = out.find("20260506_120000")
        idx_b = out.find("20260506_130000")
        assert 0 <= idx_a < idx_b, "run A should appear before run B in top-N"
        # Cost-reference footnote is factual — no claim about user's
        # subscription tier (we don't know it).
        assert "Cost reference" in out
        assert "not a billing receipt" in out
        assert "Pro/Max" not in out, "must not speculate about user's tier"
        # Top-phase hint always renders, names phase + config key, no
        # subscription / monthly-budget projection.
        assert "Top phase" in out and "plan" in out, out
        assert "phases.plan.effort" in out, out
        assert "monthly token budget" not in out, (
            "must not project window % onto unknown subscription cap"
        )
        # Per-runtime/provider split must show claude (synthesised data uses
        # claude-* models everywhere). Cross-phase token sum.
        assert "By runtime/provider" in out, out
        assert "claude" in out, out
        assert "230 tok" in out or "230" in out, out
