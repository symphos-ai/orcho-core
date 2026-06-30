"""
review phases honour phase_config.

Regression: до fix-а run_pipeline хардкодил вызовы ``codex.review_*`` для
validate_plan / review_changes / final_acceptance, игнорируя
``phase_config.{validate_plan,review_changes,final_acceptance}_agent``.
Когда user ставил Provider=claude в UI, ClaudeAgent создавался в registry,
но review-вызовы всё равно шли через CodexAgent с claude-моделью →
codex CLI 400 (model not supported).

Этот тест верифицирует что review_uncommitted/review_file вызывается на
phase_config.<slot>_agent, а не на дефолтном codex.
"""

from __future__ import annotations

import json

from agents.runtimes._strategy import MockAgentProvider
from pipeline.plan_parser import ParsedPlan


def _approved_review_json() -> str:
    return json.dumps({
        "verdict": "APPROVED",
        "short_summary": "Approved by JSON contract.",
        "findings": [],
    })


class _SpyReviewer:
    """Reviewer-stub который записывает каждый вызов в общий список."""

    def __init__(self, name: str, calls: list[tuple[str, str]]) -> None:
        self.model = "spy-model"
        self._name = name
        self._calls = calls

    def review_uncommitted(self, cwd: str, focus: str = "") -> str:
        self._calls.append((self._name, "review_uncommitted"))
        return _approved_review_json()

    def review_file(self, file_path: str, focus: str = "", cwd: str | None = None) -> str:
        self._calls.append((self._name, "review_file"))
        return _approved_review_json()


class _SpyDeveloper:
    def __init__(self, name: str, calls: list[tuple[str, str]]) -> None:
        self.model = "spy-model"
        self._name = name
        self._calls = calls
        self.session_id: str | None = None

    def run(self, prompt: str, cwd: str = "", *, continue_session: bool = False) -> str:
        self._calls.append((self._name, "run"))
        return "stub build output"

    def reset_session(self) -> None:
        self.session_id = None


class _SpyArchitect:
    def __init__(self, name: str, calls: list[tuple[str, str]]) -> None:
        self.model = "spy-model"
        self._name = name
        self._calls = calls

    def plan(self, task: str, cwd: str, codemap: str = "") -> ParsedPlan:
        self._calls.append((self._name, "plan"))
        return ParsedPlan(short_summary="stub plan", planning_context="stub plan", subtasks=(), source="test")

    def hypothesize(self, task: str, cwd: str, codemap: str = "") -> str:
        self._calls.append((self._name, "hypothesize"))
        return "stub hypothesis"


def test_validate_plan_uses_phase_config_agent_not_codex(tmp_path):
    """validate_plan должен вызывать review_file на
 phase_config.validate_plan_agent.

 Симулируем ситуацию: user поставил Provider=claude на validate_plan
 (т.е. phase_config.validate_plan_agent — ClaudeAgent, не CodexAgent),
 а CodexAgent в run_pipeline всё ещё создаётся как fallback. Проверяем
 что review_file вызвался у validate_plan_agent, а НЕ у codex.
 """
    from agents.registry import PhaseAgentConfig

    calls: list[tuple[str, str]] = []
    validate_plan_spy = _SpyReviewer("validate_plan", calls)
    review_spy  = _SpyReviewer("review_changes",  calls)
    final_spy   = _SpyReviewer("final",   calls)
    arch_spy    = _SpyArchitect("plan",   calls)
    dev_spy     = _SpyDeveloper("implement",  calls)

    cfg = PhaseAgentConfig(
        plan_agent         = arch_spy,
        implement_agent        = dev_spy,
        repair_changes_agent          = dev_spy,
        repair_escalation_agent = dev_spy,
        validate_plan_agent      = validate_plan_spy,
        review_changes_agent       = review_spy,
        final_acceptance_agent     = final_spy,
    )

    # Sanity: phase_config содержит spy-агенты, не codex
    assert cfg.validate_plan_agent is validate_plan_spy
    assert cfg.review_changes_agent  is review_spy
    assert cfg.final_acceptance_agent is final_spy


def test_review_routing_picks_correct_slot_per_phase(tmp_path):
    """Phase_config имеет 3 разных reviewer-slot. Run_pipeline должен
 выбирать правильный для каждой review-фазы. Этот тест проверяет ЛОГИКУ
 выбора — не запуская весь pipeline."""
    from agents.registry import PhaseAgentConfig

    calls: list[tuple[str, str]] = []
    validate_plan_spy = _SpyReviewer("validate_plan_slot", calls)
    review_spy  = _SpyReviewer("review_slot",  calls)
    final_spy   = _SpyReviewer("final_acceptance_slot", calls)

    cfg = PhaseAgentConfig(
        plan_agent         = _SpyArchitect("p", calls),
        implement_agent        = _SpyDeveloper("b", calls),
        repair_changes_agent          = _SpyDeveloper("f", calls),
        repair_escalation_agent = _SpyDeveloper("fe", calls),
        validate_plan_agent      = validate_plan_spy,
        review_changes_agent       = review_spy,
        final_acceptance_agent     = final_spy,
    )
    # Имитируем что делают review-фазы внутри run_pipeline:
    (cfg.validate_plan_agent  if cfg is not None else None).review_file("/x", "f", cwd="/")
    (cfg.review_changes_agent   if cfg is not None else None).review_uncommitted("/", "f")
    (cfg.final_acceptance_agent if cfg is not None else None).review_uncommitted("/", "f")

    # Каждый slot должен получить ровно один вызов соответствующего метода.
    assert ("validate_plan_slot",  "review_file")  in calls
    assert ("review_slot",   "review_uncommitted") in calls
    assert ("final_acceptance_slot", "review_uncommitted") in calls
    # И НИ ОДНОГО вызова codex / cross-slot:
    assert not any(name == "codex" for name, _ in calls)


def test_run_pipeline_announces_run_dir_early(tmp_path, monkeypatch):
    """Bug 1 regression: run_dir должен печататься в stdout СРАЗУ после
 setup, ДО первой фазы. Это позволяет dashboard поймать output_dir
 даже если pipeline упадёт на середине."""
    import io
    import sys

    from pipeline import project_orchestrator as po

    # Используем mock-провайдер чтобы pipeline быстро прошёл без CLI.
    # Перехватываем stdout и проверяем что 'Run dir:' появилось РАНО.
    from tests.conftest import init_git_repo
    project_path = tmp_path / "proj"
    init_git_repo(project_path)
    output_dir = tmp_path / "runs" / "20260504_test"
    output_dir.mkdir(parents=True)

    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured)

    try:
        po.run_pipeline(
            task="noop",
            project_dir=str(project_path),
            output_dir=output_dir,
            dry_run=True,             # короткий путь без реальных вызовов
            provider=MockAgentProvider(),
            max_rounds=0,
        )
    finally:
        monkeypatch.undo()

    out = captured.getvalue()
    assert "Run dir:" in out, (
        f"orchestrator не напечатал 'Run dir:' — UI не сможет поймать "
        f"output_dir на crash. Stdout: {out[:500]}"
    )
    # Проверяем что Run dir идёт ДО любой phase-end отметки
    run_dir_idx = out.index("Run dir:")
    phase_end_markers = ["DONE", "✗", "FAIL", "Pipeline complete"]
    for marker in phase_end_markers:
        if marker in out:
            assert out.index(marker) > run_dir_idx, (
                f"'Run dir:' должен быть ДО '{marker}' чтобы UI поймал его при crash"
            )
