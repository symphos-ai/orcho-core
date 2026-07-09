# SPDX-License-Identifier: Apache-2.0
"""The Agents header block must report the models the run will dispatch on.

Regression coverage for the banner/dispatch divergence: ``phase_config``
slots are the dispatch truth (per-phase ``phase_model_map`` entries land
there), while the header args carry only the coarse plan/implement/review
model triple. The banner used to render every reviewer-shaped row from
``review_model`` — so a run with ``validate_plan`` / ``final_acceptance``
pinned to different models printed the wrong models and the operator
learned the truth only from the phase headers or the usage summary.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agents.protocols import SessionMode
from agents.registry import PhaseAgentConfig
from core.io.ansi import strip_ansi
from pipeline.plugins import PluginConfig
from pipeline.project.run_setup import (
    _phase_agent_display,
    print_pipeline_header,
)
from pipeline.project.types import PresentationPolicy


def _agent(model: str, effort: str = "", runtime: str = "codex"):
    return SimpleNamespace(model=model, effort=effort, runtime=runtime)


def _divergent_phase_config() -> PhaseAgentConfig:
    """The live-run shape: reviewer-family phases pinned to THREE models."""
    return PhaseAgentConfig(
        plan_agent=_agent("claude-opus-4-8", "high", "claude"),
        validate_plan_agent=_agent("gpt-5.6-sol", "medium"),
        implement_agent=_agent("claude-opus-4-8", "high", "claude"),
        review_changes_agent=_agent("gpt-5.6-terra", "medium"),
        repair_changes_agent=_agent("claude-opus-4-8", "high", "claude"),
        repair_escalation_agent=_agent("claude-opus-4-8", "high", "claude"),
        final_acceptance_agent=_agent("gpt-5.5", "low"),
    )


class TestPhaseAgentDisplay:
    def test_maps_every_slot_to_model_and_effort(self) -> None:
        display = _phase_agent_display(_divergent_phase_config())
        assert display["validate_plan"] == ("gpt-5.6-sol", "medium")
        assert display["review_changes"] == ("gpt-5.6-terra", "medium")
        assert display["final_acceptance"] == ("gpt-5.5", "low")
        assert display["plan"] == ("claude-opus-4-8", "high")

    def test_none_config_and_missing_attrs_degrade(self) -> None:
        assert _phase_agent_display(None) == {}
        bare = _divergent_phase_config()
        # A runtime without model/effort attributes must not break the header.
        bare.plan_agent = object()
        display = _phase_agent_display(bare)
        assert "plan" not in display
        assert display["validate_plan"] == ("gpt-5.6-sol", "medium")


def _print_header(capsys, phase_config: PhaseAgentConfig | None) -> str:
    print_pipeline_header(
        presentation=PresentationPolicy.TERMINAL,
        project_path=Path("proj"),
        task="t",
        plan_model="coarse-plan",
        implement_model="coarse-implement",
        review_model="coarse-review",
        profile_name="advanced",
        session_mode=SessionMode.STATELESS,
        max_rounds=1,
        do_plan=True,
        plugin=PluginConfig(),
        output_dir=None,
        phase_config=phase_config,
    )
    return strip_ansi(capsys.readouterr().out)


def _agents_row(out: str, role: str) -> str:
    lines = [line for line in out.splitlines() if role in line]
    assert lines, f"no {role} row in header output:\n{out}"
    return lines[0]


class TestAgentsBlockUsesDispatchTruth:
    def test_rows_show_per_phase_models_not_coarse_triple(self, capsys) -> None:
        out = _print_header(capsys, _divergent_phase_config())
        assert "gpt-5.6-sol" in _agents_row(out, "VALIDATE_PLAN")
        assert "gpt-5.5" in _agents_row(out, "FINAL_ACCEPTANCE")
        assert "gpt-5.6-terra" in _agents_row(out, "REVIEW_CHANGES")
        # The coarse fallback triple must not leak into any row when the
        # dispatch config is available.
        assert "coarse-review" not in out
        assert "coarse-plan" not in out

    def test_effort_column_reads_the_slot(self, capsys) -> None:
        out = _print_header(capsys, _divergent_phase_config())
        assert "low" in _agents_row(out, "FINAL_ACCEPTANCE")
        assert "medium" in _agents_row(out, "VALIDATE_PLAN")

    def test_no_config_falls_back_to_coarse_models(self, capsys) -> None:
        out = _print_header(capsys, None)
        assert "coarse-plan" in _agents_row(out, "PLAN")
        assert "coarse-review" in _agents_row(out, "VALIDATE_PLAN")
        assert "coarse-review" in _agents_row(out, "FINAL_ACCEPTANCE")
