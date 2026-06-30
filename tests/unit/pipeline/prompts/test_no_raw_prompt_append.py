"""Guard: no raw post-builder string append in plan / cross-plan invoke paths.

ADR 0060 makes :class:`pipeline.prompts.turn.PromptTurn` the canonical render
surface. Post-builder edits (prefix, codemap, hypothesis suffix, cross
hypothesis context) must go through :class:`PromptTurnEditor`, never raw string
concatenation like ``prompt = f"{prompt}{suffix}"`` or ``prompt += extra``.

A raw append would serialize around the typed segment stream: the wire string
and the trace/envelope projections would diverge (the exact Bug-3 trace-honesty
failure this refactor eliminated), and a ``PromptTurn`` object concatenated into
an f-string would emit ``repr(PromptTurn(...))`` onto the wire.

This is a source-level grep guard over the load-bearing invoke modules. It is
intentionally narrow (named files, append-into-prompt patterns) so it locks the
contract without flagging unrelated string building.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.prompts


# Modules that assemble + dispatch a runtime prompt. These are exactly the
# paths where a post-builder raw append would reach the wire.
_GUARDED_FILES = (
    "pipeline/phases/builtin/session_invoke.py",
    # Phase-handler entry points build prompts/turns before invoke.
    "pipeline/phases/builtin/handlers/plan.py",
    "pipeline/phases/builtin/handlers/validate_plan.py",
    "pipeline/phases/builtin/handlers/implement.py",
    "pipeline/phases/builtin/handlers/review_changes.py",
    "pipeline/phases/builtin/handlers/repair_changes.py",
    "pipeline/phases/builtin/handlers/final_acceptance.py",
    "pipeline/phases/adapters.py",
    "pipeline/engine/hypothesis.py",
    "pipeline/cross_project/planning_loop.py",
    "pipeline/cross_project/session_invoke.py",
    "pipeline/cross_project/app.py",
    "pipeline/cross_project/session_run.py",
    "pipeline/cross_project/final_acceptance.py",
)

# ``prompt``-family identifiers that name a runtime prompt body. Appending to
# any of these post-builder is the forbidden pattern. Built by concatenation
# (not f-strings) to keep the regex escapes unambiguous.
_NAMES = r"(?:prompt|full_prompt|wire_prompt|review_prompt|turn)"

# Forbidden: ``<name> += ...``  and  ``<name> = f"{<name>}..."`` /
# ``<name> = <name> + ...`` (raw string growth of a prompt body).
_AUGMENTED_ASSIGN = re.compile(r"\b" + _NAMES + r"\s*\+=")
_FSTRING_REWRAP = re.compile(
    r"\b" + _NAMES + r"""\s*=\s*f?["'].*\{\s*""" + _NAMES + r"\s*[}.]",
)
_CONCAT_REWRAP = re.compile(
    r"\b" + _NAMES + r"\s*=\s*" + _NAMES + r"\s*\+",
)


def _repo_root() -> Path:
    # tests/unit/pipeline/prompts/ -> repo root is 4 parents up.
    return Path(__file__).resolve().parents[4]


@pytest.mark.parametrize("rel_path", _GUARDED_FILES)
def test_no_raw_prompt_append_in_invoke_paths(rel_path: str) -> None:
    path = _repo_root() / rel_path
    assert path.is_file(), f"guarded file missing: {rel_path}"

    offenders: list[str] = []
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if (
            _AUGMENTED_ASSIGN.search(line)
            or _FSTRING_REWRAP.search(line)
            or _CONCAT_REWRAP.search(line)
        ):
            offenders.append(f"{rel_path}:{lineno}: {stripped}")

    assert not offenders, (
        "Raw post-builder prompt append detected — route through "
        "PromptTurnEditor instead (ADR 0060):\n" + "\n".join(offenders)
    )
