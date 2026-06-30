"""pipeline.allowed_modifications — Render the project's allowed companion
modifications block for review prompts.

A project plugin may declare ``allowed_modifications`` (see
:class:`pipeline.plugins.PluginConfig`): a flat list of ``"glob — reason"``
entries naming satellite files — lockfiles, golden snapshots, regenerable
artifacts — whose modification is not a scope violation in any task of the
project. The review gates (review_changes, final_acceptance, validate_plan)
compose this block so the reviewing agent treats those files as in-scope.

The semantics text below is policy owned by core code, deliberately kept out
of ``core/_prompts`` (Prompt Boundary Discipline): the renderer, not a shipped
prompt part, decides what the allowance means. The renderer is purely
presentational — entries are emitted verbatim; the ``"glob — reason"`` shape is
a convention, not parsed here.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

# Code-owned semantics preamble. Keep this aligned with the review verdict
# contract: the allowance removes only the *scope* objection — content is
# still reviewed, and anything outside owned files + this list is still a
# scope violation.
_PREAMBLE = (
    "The files listed below are project-declared companion modifications. "
    "Changing them is NOT a scope violation in any task — they are expected "
    "satellite changes (lockfiles, regenerated snapshots, derived "
    "artifacts). Review the CONTENT of these changes by the usual quality "
    "criteria. Any diff outside the owned files plus this list is still a "
    "scope violation and must be rejected as before."
)


def render_allowed_modifications(entries: Sequence[str] | None) -> str:
    """Render the allowed companion modifications as a markdown block.

    Args:
        entries: the project's declared allowed-modification entries. ``None``
            or an empty sequence returns the empty string so callers can
            unconditionally compose this block into a prompt.

    Returns:
        A markdown ``## Allowed Companion Modifications`` block carrying the
        code-owned semantics preamble and one bullet per entry (verbatim)
        when ``entries`` is non-empty; the empty string otherwise.
    """
    if not entries:
        return ""

    lines: list[str] = ["## Allowed Companion Modifications", "", _PREAMBLE, ""]
    for entry in entries:
        lines.append(f"- {entry}")

    return "\n".join(lines).rstrip() + "\n"
