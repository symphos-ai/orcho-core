"""pipeline/skills/inject.py — Phase 7d-4: skill prompt injection helpers.

Two rendering surfaces:

1. **Roster** — compact ``name + description`` listing for the
   architect's plan / decompose prompt. The architect uses these to
   route subtasks via the ``skill`` field in the JSON fence. Bodies
   are *not* in the roster — that would explode the planning prompt
   on projects with many skills (50 skills × ~200 lines body ≈ 10k
   lines of context for one decision).

2. **Skill block** — full SKILL.md body wrapped in a structured XML
   container injected into the executing agent's prompt when a step
   or subtask binds a skill. Includes resource paths so the agent can
   ``cat scripts/foo.py`` on demand. Reproducibility metadata (source,
   checksum) lands in the wrapper attributes so post-mortem audits can
   tell which version of which skill ran.

A ``SkillBinding`` is recorded in ``state.extras['skill_bindings']``
each time a skill is bound to a step / subtask. The binding captures
``activation`` (explicit / architect_selected / user_requested),
``source``, and the package's ``checksum`` at binding time, allowing
post-mortem reproducibility ("this run used skill X version <hash>").

These helpers are pure (no I/O, no state mutation outside the explicit
``record_skill_binding`` API). PLAN consumes :func:`render_roster`
through ``pipeline.prompts.decompose_plan_prompt``; DAG subtask paths
consume :func:`render_skill_block` through
``pipeline.prompts.subtask.build_subtask_prompt`` (with persistence
handled by the dag runner via ``DagRunResult.skill_bindings``).
"""
from __future__ import annotations

from typing import Any

from pipeline.skills.types import SkillBinding, SkillPackage

__all__ = [
    "render_roster",
    "render_skill_block",
    "record_skill_binding",
]


# Soft cap on description length in the roster — keeps the prompt
# bounded even when plugin authors paste a long elevator pitch.
_ROSTER_DESCRIPTION_MAX = 240


def render_roster(packages: dict[str, SkillPackage]) -> str:
    """Compact roster for the architect's plan / decompose prompt.

    Format::

        Available skills (route matching subtasks by putting the exact
        name in the `skill` field):
        - `backend-endpoint`: Implement REST endpoints; routing + DTOs.
        - `db-migration`: Author and run database migrations.

    Returns ``""`` for an empty registry — the caller can fold that
    into a templated prompt without a stale "Available skills:" header.

    Names are sorted alphabetically for deterministic prompts (same
    discovery → same roster string → cacheable upstream).
    """
    if not packages:
        return ""
    lines = [
        "Available skills (route matching subtasks by putting the exact name "
        "in the `skill` field):",
    ]
    for name in sorted(packages):
        pkg = packages[name]
        desc = _truncate(pkg.description.strip(), _ROSTER_DESCRIPTION_MAX)
        lines.append(f"- `{name}`: {desc}")
    return "\n".join(lines)


def render_skill_block(
    pkg: SkillPackage,
    *,
    subtask_id: str | None = None,
) -> str:
    """Structured skill content block for the executing agent's prompt.

    Wraps the full SKILL.md body in an XML container that carries
    reproducibility metadata as attributes. Resources are listed
    (paths only — the agent reads them on demand via its file-read
    tool). The wrapper format is stable so prompt-cache tooling and
    audit logs can match against it.

    Args:
        pkg: bound skill package.
        subtask_id: optional DAG subtask identifier — included as a
            wrapper attribute so the post-mortem can correlate the
            binding to the subtask that ran it.

    Returns:
        Markdown-safe block. Caller decides where in the final prompt
        it lands (typically near the top, after AGENTS.md, before the
        task description).
    """
    attrs = [
        f'name="{_xml_attr(pkg.name)}"',
        f'source="{_xml_attr(pkg.source)}"',
        f'checksum="{_xml_attr(pkg.checksum)}"',
    ]
    if subtask_id:
        attrs.append(f'subtask_id="{_xml_attr(subtask_id)}"')

    parts = [f"<skill_content {' '.join(attrs)}>", pkg.body.strip()]
    if pkg.resource_manifest:
        parts.append("")
        parts.append("<skill_resources>")
        for entry in pkg.resource_manifest:
            parts.append(f"  <file>{_xml_text(entry.relative_path)}</file>")
        parts.append("</skill_resources>")
    parts.append("</skill_content>")
    return "\n".join(parts)


def record_skill_binding(
    state_extras: dict[str, Any],
    pkg: SkillPackage,
    *,
    activation: str,
    phase: str | None = None,
    subtask_id: str | None = None,
) -> SkillBinding:
    """Append a :class:`SkillBinding` to ``state_extras['skill_bindings']``.

    The binding captures the package's ``source`` and ``checksum`` at
    bind time so post-mortem reproducibility audits can detect drift
    ("this run used skill X@<hash>; the latest is <other hash> —
    output may not be reproducible against latest").

    Args:
        state_extras: typically ``PipelineState.extras``. The
            ``skill_bindings`` list is created on first call.
        pkg: bound skill.
        activation: ``"explicit"`` (PhaseStep.skill set), ``"architect_selected"``
            (DAG architect emitted ``subtask.skill``), or
            ``"user_requested"`` (CLI ``--skill`` flag).
        phase: phase name, when known.
        subtask_id: DAG subtask id, when known.

    Returns:
        The recorded binding (also appended to ``state_extras``).
    """
    binding = SkillBinding(
        skill_name=pkg.name,
        activation=activation,
        source=pkg.source,
        checksum=pkg.checksum,
        phase=phase,
        subtask_id=subtask_id,
    )
    bucket = state_extras.setdefault("skill_bindings", [])
    bucket.append(binding)
    return binding


# ── Internals ────────────────────────────────────────────────────────


def _truncate(text: str, limit: int) -> str:
    """Cut a description at the soft limit, ending in ``…`` if cut.

    Word-boundary aware so the ellipsis lands cleanly. If the text fits
    untouched, the original is returned. The limit excludes the ``…``
    so the visible string is at most ``limit`` characters.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    # Try to break on the last whitespace before the limit.
    cut = text.rfind(" ", 0, limit)
    if cut <= 0:
        cut = limit
    return text[:cut].rstrip() + "…"


def _xml_attr(value: str) -> str:
    """Escape ``"`` and ``&`` for use in an XML attribute value.

    SKILL.md frontmatter and identifiers should not contain XML-active
    characters in practice, but we defend the wrapper format against
    accidental quotes in skill names / sources.
    """
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _xml_text(value: str) -> str:
    """Escape ``<`` / ``>`` / ``&`` for use as XML text content."""
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
