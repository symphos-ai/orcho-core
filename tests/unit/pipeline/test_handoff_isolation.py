"""ADR 0042 Phase D guards.

Two structural invariants the handoff service must keep:

* :mod:`pipeline.project.handoff` MUST NOT import from
  ``pipeline.project.app`` or ``pipeline.project_orchestrator`` — at
  runtime **or** statically. ADR 0042 stop condition #10 has no
  exception; ``if TYPE_CHECKING:`` blocks are scanned the same as
  runtime imports. If a future edit adds either import the AST scan
  below trips, and the developer must restructure rather than create
  the inverted dependency.
* :class:`pipeline.project.handoff.PhaseHandoffLoopResult` is a
  discriminated union: exactly one of ``paused`` /
  ``continue_dispatch`` / ``halted`` must be true. The dataclass
  validates at construction; this test pins the rule so a future
  edit to ``__post_init__`` cannot relax the invariant unnoticed.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from pipeline.project.handoff import PhaseHandoffLoopResult

_BANNED = {
    "pipeline.project.app",
    "pipeline.project_orchestrator",
}


class TestHandoffImportIsolation:
    def test_handoff_module_does_not_import_app_or_orchestrator(
        self,
    ) -> None:
        """All imports — runtime and ``if TYPE_CHECKING:`` — are
        scanned. ADR 0042 stop condition #10 has no exception.
        """
        src = pathlib.Path(
            "pipeline/project/handoff.py",
        ).read_text(encoding="utf-8")
        tree = ast.parse(src)

        violations: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod in _BANNED or any(
                    mod.startswith(b + ".") for b in _BANNED
                ):
                    violations.append(
                        f"line {node.lineno}: from {mod} import …"
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name
                    if name in _BANNED or any(
                        name.startswith(b + ".") for b in _BANNED
                    ):
                        violations.append(
                            f"line {node.lineno}: import {name}"
                        )
        assert not violations, (
            "pipeline/project/handoff.py imports forbidden modules "
            "(ADR 0042 forbidden shape #10 — no TYPE_CHECKING "
            "exception):\n  "
            + "\n  ".join(violations)
        )


class TestPhaseHandoffLoopResultInvariant:
    def test_paused_only_accepted(self) -> None:
        r = PhaseHandoffLoopResult(
            profile=None, session={},
            paused=True, continue_dispatch=False, halted=False,
        )
        assert r.paused is True

    def test_continue_dispatch_only_accepted(self) -> None:
        r = PhaseHandoffLoopResult(
            profile=None, session={},
            paused=False, continue_dispatch=True, halted=False,
        )
        assert r.continue_dispatch is True

    def test_halted_only_accepted(self) -> None:
        r = PhaseHandoffLoopResult(
            profile=None, session={},
            paused=False, continue_dispatch=False, halted=True,
        )
        assert r.halted is True

    @pytest.mark.parametrize(
        "paused,continue_dispatch,halted",
        [
            (True, True, False),
            (True, False, True),
            (False, True, True),
            (True, True, True),
        ],
    )
    def test_multiple_true_rejected(
        self, paused: bool, continue_dispatch: bool, halted: bool,
    ) -> None:
        with pytest.raises(ValueError, match="exactly one of"):
            PhaseHandoffLoopResult(
                profile=None, session={},
                paused=paused,
                continue_dispatch=continue_dispatch,
                halted=halted,
            )

    def test_all_false_rejected(self) -> None:
        with pytest.raises(ValueError, match="exactly one of"):
            PhaseHandoffLoopResult(
                profile=None, session={},
                paused=False, continue_dispatch=False, halted=False,
            )
