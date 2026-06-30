"""Prompt resolution surface."""
from __future__ import annotations

from pathlib import Path

from core.io import prompt_loader as _loader
from sdk.errors import PromptNotFound
from sdk.types import PromptResolution, PromptResolutionStep


def list_prompts() -> list[str]:
    """List the names of every core prompt available out of the box."""
    return list(_loader.list_core_prompts())


def resolve_prompt(
    name: str,
    *,
    project_dir: Path | str | None = None,
) -> PromptResolution:
    """Resolve a prompt by name.

    Returns the typed resolution chain and the rendered body of the
    winner. Raises `PromptNotFound` only when no level resolves to an
    existing file — partial-chain resolutions still produce a result
    so embedders can present what was tried.
    """
    raw_chain = _loader.resolution_chain(name, project_dir)
    steps: list[PromptResolutionStep] = []
    winner: Path | None = None
    for level, path, exists in raw_chain:
        is_winner = bool(exists) and winner is None
        if is_winner:
            winner = path
        steps.append(
            PromptResolutionStep(
                location=level,
                path=path,
                exists=bool(exists),
                is_winner=is_winner,
            )
        )

    body: str | None = None
    if winner is not None:
        try:
            body = winner.read_text(encoding="utf-8")
        except OSError:
            body = None

    if winner is None:
        raise PromptNotFound(
            f"Prompt {name!r} not found in any of: "
            + ", ".join(s.location for s in steps)
        )

    return PromptResolution(
        name=name, chain=tuple(steps), winner=winner, body=body
    )


__all__ = ["list_prompts", "resolve_prompt"]
