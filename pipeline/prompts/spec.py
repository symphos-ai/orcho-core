"""
pipeline/prompts/spec.py — composable prompt part declaration.

``PromptSpec`` names the user-editable composable parts (``roles/`` +
``tasks/`` + ``formats/``) that compose into a rendered prompt. It is
pure data: no I/O, no rendering, no loader dependencies. The composer
(``pipeline.prompts.composer.render_composed_prompt``) consumes a
``PromptSpec`` and produces a prompt string.

This module deliberately has no runtime imports so any layer that
needs to declare which parts a phase uses (``PhaseStep.prompt`` in
``pipeline.runtime``, profile loader validation, builders) can import
it without pulling in execution machinery.

Prompt persona boundary
-----------------------

``PromptSpec.role`` is the persona file name under ``_prompts/roles/``:
``implementation_engineer`` / ``systems_architect`` /
``code_reviewer`` / ``product_owner``, plus any project override.
It is the only role-like value prompt rendering understands.

A5.2a draws the boundary hard: prompt composition never consults the
older ``AgentRole`` dispatch slots. Callers either supply an explicit
prompt persona on the spec, or rely on a builder's prompt-taxonomy
default.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptSpec:
    """Composable prompt part names.

    ``role``, ``task``, and ``format`` map to files under
    ``_prompts/roles/``, ``_prompts/tasks/``, and ``_prompts/formats/``.

    ``role`` is a prompt-role name (persona file under
    ``_prompts/roles/``). It is optional only as a transitional
    convenience for ``dataclasses.replace`` patterns that re-shape
    an existing spec; any spec reaching ``part_names()`` MUST carry
    a role. Builders supply prompt-taxonomy defaults; profile
    authors declare ``prompt.role`` explicitly.
    """

    task: str
    role: str | None = None
    format: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "task", self._require_part("task", self.task))
        if self.role is not None:
            object.__setattr__(self, "role", self._require_part("role", self.role))
        if self.format is not None:
            object.__setattr__(self, "format", self._require_part("format", self.format))

    @staticmethod
    def _require_part(name: str, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"PromptSpec.{name} is empty")
        return value.strip()

    def part_names(self) -> tuple[str, ...]:
        """Return the ordered tuple of ``<kind>/<name>`` part paths.

        Raises if ``role`` is not set — every rendered prompt must
        anchor on an explicit prompt-role persona file. There is no
        runtime-role fallback path; that boundary is enforced here.
        """
        if not self.role:
            raise ValueError(
                "PromptSpec.role is required to render parts; "
                "set role to a prompt-taxonomy persona (e.g. "
                "'implementation_engineer', 'systems_architect', "
                "'code_reviewer', 'product_owner', or a project override)"
            )
        names = [f"roles/{self.role}", f"tasks/{self.task}"]
        if self.format:
            names.append(f"formats/{self.format}")
        return tuple(names)


__all__ = ["PromptSpec"]
