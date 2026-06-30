"""
pipeline/prompts/modes.py — professional prompt ablation mode.

Orcho's prompt stack has two layers:

1. **Professional prompt layer** — user-editable composable parts
   (``_prompts/{roles,tasks,formats}/*.md``) rendered through the
   composer. Carries persona, professional method, and presentation.
2. **System-tail layer** — code-owned parser contracts, handoff and
   review-target policy, language posture, cross-project grammar.
   Mandatory; not user-overridable.

This module exposes an **internal** ablation mode that toggles only the
professional layer. It is not a user-facing feature: there is no
profile JSON field, no CLI flag, no MCP option, no settings surface.
Builders accept a ``professional_prompt_mode`` kwarg so tests and eval
harnesses can compare:

    FULL    — current behavior: composed role/task/format + system-tail.
    MINIMAL — a short code-owned phase intent + required phase inputs
              + the same system-tail blocks.

System-tail is always attached. Disabling it would test protocol
breakage, not prompt quality.

Default is ``FULL``: every existing caller stays on the current path
without changes.
"""

from __future__ import annotations

from enum import StrEnum


class ProfessionalPromptMode(StrEnum):
    """Ablation mode for the professional prompt layer.

    Three modes split the professional layer into two orthogonal
    axes so the eval can attribute cost / verbosity / behavior to
    the right cause:

    - **Method layer** = ``roles/*`` + ``tasks/*``. Professional
      persona, posture, procedure.
    - **Format layer** = ``formats/*``. Output shape, verbosity,
      handoff style.
    - **System-tail** = code-owned parser contracts, language
      posture, handoff policy. Always attached; never disabled by
      ablation.

    Modes:

    - ``FULL`` — method + format + system-tail. Current production
      behavior.
    - ``MINIMAL_WITH_FORMAT`` — code-owned minimal intent + format
      + system-tail. Strips role/task professional method but
      keeps the format preset (so output verbosity stays comparable
      to ``FULL``). When the builder's ``PromptSpec`` has no
      ``format`` set, this degrades to ``MINIMAL``.
    - ``MINIMAL`` — code-owned minimal intent + system-tail. Strips
      both method and format layers.

    The three modes let us answer separate questions:

    - ``FULL`` vs ``MINIMAL_WITH_FORMAT``: does the role/task
      professional method add measurable value when output format
      is held constant?
    - ``MINIMAL_WITH_FORMAT`` vs ``MINIMAL``: how much of the
      output cost / structure comes from the format preset alone?
    - ``FULL`` vs ``MINIMAL``: cumulative cost of the entire
      professional layer.

    Default is ``FULL``: every existing caller stays on the current
    path without changes.
    """

    FULL = "full"
    MINIMAL_WITH_FORMAT = "minimal_with_format"
    MINIMAL = "minimal"


def coerce_professional_prompt_mode(
    value: str | ProfessionalPromptMode | None,
) -> ProfessionalPromptMode:
    """Normalize a builder kwarg to a :class:`ProfessionalPromptMode`.

    ``None`` and missing values default to ``FULL`` — that keeps every
    existing caller on the current behavior. String inputs are
    case-insensitive (``"full"`` / ``"minimal"``). Anything else
    raises ``ValueError``.
    """
    if value is None:
        return ProfessionalPromptMode.FULL
    if isinstance(value, ProfessionalPromptMode):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        for mode in ProfessionalPromptMode:
            if mode.value == normalized:
                return mode
        raise ValueError(
            f"unknown professional_prompt_mode: {value!r} "
            f"(expected one of {sorted(m.value for m in ProfessionalPromptMode)})"
        )
    raise ValueError(
        f"unsupported professional_prompt_mode type: {type(value).__name__}"
    )


__all__ = [
    "ProfessionalPromptMode",
    "coerce_professional_prompt_mode",
]
