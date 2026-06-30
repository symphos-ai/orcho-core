"""Presentation helpers for the interactive profile picker.

Owns the pure rendering side of the `orcho` work-kind menu: tagline/summary
trimming, full-description wrapping, the curated Common/Focused sectioning,
the per-row ``{default_mode} · {isolation} · {tagline}`` subtitle, the
on-demand ``?N`` detail view, and the menu renderer itself. Nothing here
reads from stdin (`input()`); the prompt loop lives in
`cli/_profile_prompt.py` and delegates the actual printing here.
"""
from __future__ import annotations

import textwrap
from collections.abc import Sequence
from typing import TYPE_CHECKING

from core.io.journey_prompt import (
    bold,
    default_chip,
    divider,
    grey,
    title,
)
from pipeline.project.constants import DEFAULT_PROFILE_NAME

if TYPE_CHECKING:
    from pipeline.runtime.work_kind_detection import AutoDetectResolution

# Display tagline budget: first sentence of a profile description, trimmed
# to a single readable line so the picker stays a one-line-per-profile menu
# instead of dumping the full 500-600 char paragraph.
_SUMMARY_MAX = 96

# Curated work-kind presentation (Stage C). ``Common`` surfaces the day-to-day
# delivery work kinds; ``Focused`` collects the scoped / specialist ones. Order
# within each group is the product-defined Expected-UX order, not alphabetical.
# ``feature`` leads ``Common`` and carries the ``[default]`` chip.
_COMMON_ORDER = ("feature", "small_task", "complex_feature")
_FOCUSED_ORDER = (
    "planning", "delivery_audit", "code_review", "research", "refactor",
    "migration",
)
_COMMON_LABEL = "Common"
_FOCUSED_LABEL = "Focused"
# Trailing fallback group for profiles with no semantic identity (third-party
# plugins / custom JSON). Rendered alphabetically after the curated groups.
_OTHER_LABEL = "Other"

# Synthetic auto-detect selector (Stage C). This is NOT a profile: it is a
# first-class menu entry, rendered above the ``Common`` group, that asks the
# detector to recommend a work kind + operating mode. Its spelling never
# collides with a ``SemanticProfile`` member (``auto-detect`` is rejected by
# ``SemanticProfile(...)``), so it cannot shadow a manual profile. The actual
# detector dispatch is wired downstream; the picker only emits this token as
# ``args.profile``.
AUTO_DETECT_CHOICE = "auto-detect"
_AUTO_DETECT_SUBTITLE = "recommend work kind & mode"
_AUTO_DETECT_DETAIL = (
    "Inspect the task and project, then recommend a work kind and operating "
    "mode. On a confirm-policy TTY you accept the recommendation or pick a "
    "profile yourself; non-interactive / trusted runs auto-select above a "
    "confidence threshold with a deterministic fallback."
)


def _profile_summary(description: str) -> str:
    """First sentence of ``description`` as a compact one-line tagline.

    Cuts at the first sentence/clause boundary (``. ``, ``—``, or `` — ``),
    strips a trailing period, and word-truncates with an ellipsis past
    :data:`_SUMMARY_MAX`. Empty / blank description → ``""``.
    """
    text = (description or "").strip()
    if not text:
        return ""
    head = text.splitlines()[0].strip()
    cuts = [
        idx for idx in (head.find(". "), head.find(" — "), head.find("—"))
        if idx != -1
    ]
    if cuts:
        head = head[: min(cuts)].strip()
    head = head.rstrip(".").strip()
    if len(head) > _SUMMARY_MAX:
        clipped = head[:_SUMMARY_MAX].rsplit(" ", 1)[0].rstrip()
        head = (clipped or head[:_SUMMARY_MAX].rstrip()) + "…"
    return head


def _full_description(description: str) -> str:
    """Word-wrap ``description`` to ~76 columns for on-demand detail view.

    Paragraph breaks (blank lines) are preserved; each paragraph is wrapped
    independently via :func:`textwrap.fill`. Empty description → ``""``.
    """
    text = (description or "").strip()
    if not text:
        return ""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    return "\n\n".join(textwrap.fill(p, width=76) for p in paragraphs)


def _default_mode_label(profile: object) -> str:
    """The profile's ``default_mode`` value as a string, or ``""`` if absent.

    Reads the semantic ``default_mode`` metadata (an ``OperatingMode`` enum
    on a loaded ``Profile``). Plugins / custom profiles that carry no default
    mode yield ``""`` so the subtitle degrades gracefully.
    """
    raw = getattr(profile, "default_mode", None)
    if raw is None:
        return ""
    return str(getattr(raw, "value", raw)).strip()


def _isolation_label(profile: object) -> str:
    """Human label for the profile's worktree isolation intent.

    ``worktree_isolation == "off"`` → ``"direct checkout"``; anything else
    (``per_run`` / ``per_phase`` / unset global default) → ``"isolated"``.
    """
    raw = getattr(profile, "worktree_isolation", None)
    if str(raw).strip() == "off":
        return "direct checkout"
    return "isolated"


def _row_subtitle(profile: object) -> str:
    """``{default_mode} · {isolation_label} · {tagline}`` for one menu row.

    ``default_mode`` is dropped when the profile carries no semantic default
    (plugins / custom). ``tagline`` is the first-sentence
    :func:`_profile_summary` of the description and is dropped when empty.
    """
    parts: list[str] = []
    mode = _default_mode_label(profile)
    if mode:
        parts.append(mode)
    parts.append(_isolation_label(profile))
    tagline = _profile_summary(
        str(getattr(profile, "description", "") or "")
    )
    if tagline:
        parts.append(tagline)
    return " · ".join(parts)


def _menu_sections(
    names: list[str], profiles: dict,
) -> list[tuple[str | None, list[str]]]:
    """Plan the menu layout as ``(header, [name, ...])`` sections.

    Curated work-kind presentation: a ``Common`` group (``feature``,
    ``small_task``, ``complex_feature``) then a ``Focused`` group (planning,
    delivery_audit, code_review, research, refactor, migration), each in the
    product-defined order and filtered to the names actually present (e.g.
    after the cross ``profile_filter``). Any remaining names with no semantic
    identity (third-party plugins / custom JSON) fall into a trailing
    alphabetical ``Other`` group.

    When no curated work kind is present at all (a plugin-only catalog), the
    menu degrades to a single flat ``sorted`` section with no header —
    deterministic and order-compatible with a bare catalog.
    """
    present = set(names)
    common = [n for n in _COMMON_ORDER if n in present]
    focused = [n for n in _FOCUSED_ORDER if n in present]
    curated = set(common) | set(focused)
    other = sorted(n for n in names if n not in curated)

    sections: list[tuple[str | None, list[str]]] = []
    if common:
        sections.append((_COMMON_LABEL, common))
    if focused:
        sections.append((_FOCUSED_LABEL, focused))
    if other:
        # No curated rows → plugin-only catalog: stay flat and headerless.
        header = _OTHER_LABEL if (common or focused) else None
        sections.append((header, other))
    return sections


def _print_profile_details(
    token: str, ordered_names: list[str], profiles: dict, *, color: bool,
) -> None:
    """Print the full wrapped description for the ``?N`` / ``? <name>`` query.

    A bare token (just ``?``) prints every profile's detail. An unrecognised
    token prints a brief hint. Never decrements the invalid-attempt budget.
    """
    targets: list[str]
    if not token:
        targets = list(ordered_names)
    elif token.isdigit() and 1 <= int(token) <= len(ordered_names):
        targets = [ordered_names[int(token) - 1]]
    elif token in ordered_names:
        targets = [token]
    else:
        print(grey(f"No such profile: {token!r}", color=color))
        return
    for name in targets:
        if name == AUTO_DETECT_CHOICE:
            # Synthetic selector: no catalog profile backs it.
            print(bold(name, color=color))
            print(_full_description(_AUTO_DETECT_DETAIL))
            continue
        full = _full_description(
            str(getattr(profiles[name], "description", "") or "")
        )
        print(bold(name, color=color))
        print(full if full else grey("(no description)", color=color))


def _render_menu(
    names: list[str], profiles: dict, *, color: bool,
    include_auto_detect: bool = False,
) -> list[str]:
    """Print the framed work-kind menu and return ``ordered_names``.

    Renders one work kind per line — number, name, an optional ``[default]``
    chip on the default work kind (:data:`DEFAULT_PROFILE_NAME`, ``feature``),
    and a ``{default_mode} · {isolation} · {tagline}`` subtitle — grouped into
    the curated Common/Focused sections planned by :func:`_menu_sections`. The
    returned list maps each printed number (1-based) to its profile name so the
    prompt loop and :func:`_print_profile_details` resolve numeric input
    identically. Never reads from stdin.

    When ``include_auto_detect`` is set, a synthetic
    :data:`AUTO_DETECT_CHOICE` row is rendered first (as ``1)``, above the
    ``Common`` group) without hiding any manual profile. It carries no
    ``[default]`` chip; selecting it yields the ``auto-detect`` token.
    """
    ordered_names: list[str] = []
    print(title("Select work kind", color=color))
    print(divider(color=color))
    number = 0
    if include_auto_detect:
        number += 1
        ordered_names.append(AUTO_DETECT_CHOICE)
        print(
            f"  {bold(str(number), color=color)}) "
            f"{bold(AUTO_DETECT_CHOICE, color=color)}"
        )
        print(f"     {grey(_AUTO_DETECT_SUBTITLE, color=color)}")
    for header, members in _menu_sections(names, profiles):
        if header is not None:
            print(grey(f"  {header}", color=color))
        for name in members:
            number += 1
            ordered_names.append(name)
            chip = (
                f" {default_chip(color=color)}"
                if name == DEFAULT_PROFILE_NAME else ""
            )
            print(f"  {bold(str(number), color=color)}) {bold(name, color=color)}{chip}")
            subtitle = _row_subtitle(profiles[name])
            if subtitle:
                print(f"     {grey(subtitle, color=color)}")
    print(divider(color=color))
    return ordered_names


# ── Auto-detect topology result + choices (Stage C / T3) ─────────────────────

# Provider-neutral labels for the three explicit topology choices. The order is
# the Expected-UX order; the operator picks 1/2/3 (see
# ``cli._profile_prompt.resolve_topology_choice``). Choice 1 never starts a
# cross run inside the current mono process — it surfaces a directive.
_TOPOLOGY_CHOICE_LINES = (
    "1) Start cross run with these projects [recommended]",
    "2) Continue mono run and allow expanded delivery",
    "3) Continue strict mono",
)


def render_autodetect_result(
    resolution: AutoDetectResolution, *, color: bool,
) -> None:
    """Print the framed ``Auto-detect result`` block and the three choices.

    Pure presentation — never reads stdin. The caller shows this only for a
    high-confidence ``cross_recommended`` resolution. Surfaces the resolved
    work kind (``profile``), the recommended ``topology`` (``cross
    recommended``), ``confidence``, the projected ``projects``, and a short
    ``reason``. Wording stays provider-neutral.
    """
    print(title("Auto-detect result", color=color))
    print(divider(color=color))
    print(f"  profile     {bold(resolution.actual_profile.value, color=color)}")
    print(f"  topology    {bold('cross recommended', color=color)}")
    if resolution.confidence is not None:
        print(f"  confidence  {resolution.confidence:.2f}")
    print(f"  projects    {', '.join(resolution.delivery_projects)}")
    if resolution.topology_reason:
        print(f"  reason      {grey(resolution.topology_reason, color=color)}")
    print(divider(color=color))
    print(bold("Choices", color=color))
    for line in _TOPOLOGY_CHOICE_LINES:
        print(f"  {line}")


def format_cross_directive(projects: Sequence[str]) -> str:
    """Ready-to-edit ``orcho cross`` command for the projected ``projects``.

    The aliases are projected by the topology heuristic; the operator supplies
    each repo path (``<alias>:<path>``) and confirms explicitly. Mirrors the
    existing ``orcho cross --projects … --task '…'`` hint shape and stays
    provider-neutral.
    """
    args = " ".join(f"{alias}:<path>" for alias in projects)
    return f"orcho cross --projects {args} --task '...'"
