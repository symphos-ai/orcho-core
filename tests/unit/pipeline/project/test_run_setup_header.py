"""Header rows owned by ``pipeline.project.run_setup``."""

from __future__ import annotations

from pathlib import Path

from agents.protocols import SessionMode
from core.io.ansi import strip_ansi
from pipeline.plugins import PluginConfig
from pipeline.project.run_setup import _skills_header_line, print_pipeline_header
from pipeline.project.types import PresentationPolicy
from pipeline.project.verification_disclosure import (
    HEADER_VALUE,
    VerificationContractPresence,
)
from pipeline.skills.types import SkillPackage


def _skill(name: str) -> SkillPackage:
    return SkillPackage(
        name=name,
        description=f"{name} skill",
        root_dir=Path(f"/skills/{name}"),
        skill_md_path=Path(f"/skills/{name}/SKILL.md"),
        body="body",
        frontmatter={"name": name, "description": f"{name} skill"},
    )


def test_skills_header_line_lists_discovered_skill_names_sorted() -> None:
    plugin = PluginConfig()
    plugin.skill_registry = {
        "quant-analytics-theory": _skill("quant-analytics-theory"),
        "quant-analytics-atas": _skill("quant-analytics-atas"),
    }

    assert (
        _skills_header_line(plugin)
        == "2: quant-analytics-atas, quant-analytics-theory"
    )


def test_skills_header_line_omits_empty_registry() -> None:
    assert _skills_header_line(PluginConfig()) is None


def _print_header(capsys, **overrides) -> str:
    kwargs = dict(
        presentation=PresentationPolicy.TERMINAL,
        project_path=Path("proj"),
        task="t",
        plan_model="m",
        implement_model="m",
        review_model="m",
        profile_name="feature",
        session_mode=SessionMode.STATELESS,
        max_rounds=1,
        do_plan=True,
        plugin=PluginConfig(),
        output_dir=None,
        contract=None,
    )
    kwargs.update(overrides)
    print_pipeline_header(**kwargs)
    return strip_ansi(capsys.readouterr().out)


class TestVerificationContractDisclosure:
    """The header states "no contract" instead of dropping the block."""

    def test_undeclared_contract_prints_the_fact(self, capsys) -> None:
        out = _print_header(
            capsys,
            contract_presence=VerificationContractPresence(declared=False),
        )
        assert HEADER_VALUE in out
        assert "Verification" in out

    def test_declared_contract_does_not_print_the_fact(self, capsys) -> None:
        out = _print_header(
            capsys,
            contract_presence=VerificationContractPresence(declared=True),
        )
        assert HEADER_VALUE not in out

    def test_unrecorded_fact_leaves_the_header_unchanged(self, capsys) -> None:
        """Runs that never recorded the fact keep today's silent header."""
        with_none = _print_header(capsys, contract_presence=None)
        without_kwarg = _print_header(capsys)
        assert HEADER_VALUE not in with_none
        assert with_none == without_kwarg

    def test_silent_presentation_prints_nothing(self, capsys) -> None:
        out = _print_header(
            capsys,
            presentation=PresentationPolicy.SILENT,
            contract_presence=VerificationContractPresence(declared=False),
        )
        assert out == ""
