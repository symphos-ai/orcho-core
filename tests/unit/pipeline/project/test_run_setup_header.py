"""Header rows owned by ``pipeline.project.run_setup``."""

from __future__ import annotations

from pathlib import Path

import pytest

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


class TestLedgerReadTolerance:
    """T7 — the header's decorative ledger read must not kill an env retry.

    On a ``retry_verification`` resume the scheduled-gate ledger is the very
    artifact the env-retry owner has to judge; it must reach that owner, which
    re-parks the pause with a named reason and runs no gate. A courtesy banner
    that raises first would replace that decidable pause with a dead run, so
    the read is downgraded — and only the read, only under the flag.
    """

    @staticmethod
    def _contract():
        from pipeline.verification_contract import VerificationContract

        return VerificationContract.from_plugin(PluginConfig(
            work_mode="governed",
            verification_envs={"ci": {"image": "python:3.12"}},
            verification={
                "default_env": "ci",
                "commands": {"lint": {"run": "ruff check .", "env": "ci"}},
                "schedule": [
                    {"after_phase": "implement", "policy": "require",
                     "commands": ["lint"], "on_fail": "handoff"},
                ],
            },
        ))

    @staticmethod
    def _write_valid_ledger(run_dir: Path, contract) -> None:
        from pipeline.project.verification_ledger_runtime import initialize_contract

        initialize_contract(run_dir, contract)

    @staticmethod
    def _write_corrupt_ledger(run_dir: Path) -> None:
        from pipeline.verification_ledger_store import FILENAME

        (run_dir / FILENAME).write_text("{not json at all", encoding="utf-8")

    def test_corrupt_ledger_is_tolerated_and_renders_as_unwritten(
        self, capsys, tmp_path: Path,
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        self._write_corrupt_ledger(run_dir)

        tolerated = _print_header(
            capsys, output_dir=run_dir, contract=self._contract(),
            ledger_read_tolerant=True,
        )

        # Identical to the header of a run whose ledger was never written:
        # a failed read carries no authoritative meaning of its own.
        unwritten_dir = tmp_path / "clean" / "run"
        unwritten_dir.mkdir(parents=True)
        unwritten = _print_header(
            capsys, output_dir=unwritten_dir, contract=self._contract(),
        )
        assert tolerated.replace(
            str(run_dir), "<RUN>",
        ) == unwritten.replace(str(unwritten_dir), "<RUN>")

    def test_corrupt_ledger_without_the_flag_still_raises(
        self, capsys, tmp_path: Path,
    ) -> None:
        from pipeline.verification_ledger_store import LedgerStoreError

        run_dir = tmp_path / "run"
        run_dir.mkdir()
        self._write_corrupt_ledger(run_dir)

        with pytest.raises(LedgerStoreError):
            _print_header(capsys, output_dir=run_dir, contract=self._contract())

    def test_valid_ledger_renders_identically_with_and_without_the_flag(
        self, capsys, tmp_path: Path,
    ) -> None:
        """The flag downgrades a failure only; it never changes a good read."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        self._write_valid_ledger(run_dir, self._contract())

        strict = _print_header(
            capsys, output_dir=run_dir, contract=self._contract(),
        )
        tolerant = _print_header(
            capsys, output_dir=run_dir, contract=self._contract(),
            ledger_read_tolerant=True,
        )
        assert strict == tolerant

    def test_silent_presentation_never_reads_the_ledger(
        self, capsys, tmp_path: Path,
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        self._write_corrupt_ledger(run_dir)

        assert _print_header(
            capsys, presentation=PresentationPolicy.SILENT,
            output_dir=run_dir, contract=self._contract(),
        ) == ""
