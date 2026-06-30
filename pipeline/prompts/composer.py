"""Prompt composer for composable role/task/format parts."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path

from core.io import prompt_loader
from core.observability import prompt_trace
from pipeline.prompts.spec import PromptSpec
from pipeline.prompts.types import (
    PromptCacheScope,
    PromptPart,
    PromptStability,
)

__all__ = [
    "PromptSpec",
    "assemble_cache_first_segments",
    "render_prompt_parts",
    "render_composed_prompt",
]

# ``spec.part_names()`` returns paths like ``roles/architect`` —
# strip the trailing ``s`` to get the singular slot kind used by
# :class:`PromptPart`.
_KIND_FROM_DIR = {"roles": "role", "tasks": "task", "formats": "format"}

# Substitution variables that mark a part's body as **per-turn**
# content rather than stable persona/procedure prose. A template
# that references any of these — even via ``${...}`` form — is
# conservatively classified as TURN payload so its rendered body
# never leaks into the cacheable stable prefix. Finer template /
# body splitting belongs to M10; M2 keeps the rule coarse and
# unambiguous so prefix hashes never see substituted task text.
_TURN_VARIABLES: frozenset[str] = frozenset({
    "task",
    "artifact",
    "artifact_block",
    "artifact_body",
    "feedback",
    "diff",
    "round",
    "file_path",
    "file",
    "critique",
    "test_failures",
    "body",
    "plan_contract",
    "plan_tasks",
    "focus",
    "bundle_markdown",
})

# Substitution variables that mark a part's body as **per-run /
# per-project** content. The substituted body is stable inside a
# single run but varies across runs or projects (project context
# block, project_dir anchor, codemap, plugin additive knobs, cross
# bundle metadata). Classifying RUN/WORKSPACE keeps the body out
# of the globally cacheable prefix while still letting M6's delta
# selector handle session reuse correctly under the M11.5
# prefix-contiguity rule.
_RUN_VARIABLES: frozenset[str] = frozenset({
    "context",
    "project_dir",
    "extra_step",
    "extra_checks",
    "codemap_section",
    "skill_roster_block",
    "ma_artifacts_dir",
    "aliases",
    "paths_list",
})


def _make_var_pattern(names: frozenset[str]) -> re.Pattern[str]:
    """Build a regex that matches ``$name`` or ``${name}`` for any
    *names* entry.

    ``string.Template`` accepts both ``$name`` and ``${name}``
    forms; matching either form keeps the classifier honest when a
    template author switches notation. The bare ``$name`` branch
    uses a trailing ``\\b`` so ``$tasks`` (different identifier)
    does not match. The ``${name}`` branch closes on ``}`` and
    needs no trailing boundary — the close brace already
    terminates the identifier.
    """
    if not names:
        return re.compile(r"(?!x)x")  # never matches
    escaped = "|".join(re.escape(v) for v in sorted(names))
    return re.compile(
        rf"\$(?:(?:{escaped})\b|\{{(?:{escaped})\}})",
    )


_TURN_VAR_PATTERN = _make_var_pattern(_TURN_VARIABLES)
_RUN_VAR_PATTERN = _make_var_pattern(_RUN_VARIABLES)


def _classify_editable_part(
    template_body: str,
    *,
    resolution_source: str | None = None,
) -> tuple[PromptStability, PromptCacheScope, str | None]:
    """Decide stability/cache_scope/volatile_reason for an editable part.

    - TURN/NONE when *template_body* references any known turn
      variable (task, diff, feedback, focus, etc.). The body is
      always re-sent and never enters the cacheable prefix.
    - RUN/WORKSPACE when *template_body* references a run/project
      variable (``$context``, ``$project_dir``, plugin additive
      knobs, codemap, skill roster). The substituted body is
      stable within a run but project-specific, so it must not
      enter the globally cacheable prefix; the M6 delta selector
      keeps it on the wire under the M11.5 prefix-contiguity rule.
    - STATIC + tier-of-resolution otherwise (ADR 0028 / M10.5).
      The cache tier comes from where the file was resolved:

      ===============  ==================
      Resolution       Cache scope
      ===============  ==================
      ``"core"``       :data:`GLOBAL`
      ``"workspace"``  :data:`WORKSPACE`
      ``"project"``    :data:`PROJECT`
      ``None``         :data:`GLOBAL` (back-compat default; callers
                        that do not pass ``resolution_source`` keep the
                        M1 GLOBAL behaviour)
      ===============  ==================

      Workspace and project overrides therefore land in narrower
      cache tiers without changing the wire bytes — the assembler
      groups them into the matching tier slot. Body/hash
      invalidation drives eviction when the override file content
      changes.
    """
    if _TURN_VAR_PATTERN.search(template_body):
        return (
            PromptStability.TURN,
            PromptCacheScope.NONE,
            "template references per-turn substitution variables",
        )
    if _RUN_VAR_PATTERN.search(template_body):
        return (
            PromptStability.RUN,
            PromptCacheScope.WORKSPACE,
            "template references project / run substitution variables",
        )
    # ADR 0028 / M10.5: override-aware classifier. STATIC + tier of
    # resolution. Default-shipped files stay GLOBAL; overrides demote
    # to WORKSPACE / PROJECT so the cache-first assembler can group
    # them in the correct breadth tier.
    if resolution_source == "workspace":
        return (PromptStability.STATIC, PromptCacheScope.WORKSPACE, None)
    if resolution_source == "project":
        return (PromptStability.STATIC, PromptCacheScope.PROJECT, None)
    return (PromptStability.STATIC, PromptCacheScope.GLOBAL, None)


def render_prompt_parts(
    spec: PromptSpec,
    *,
    project_dir: str | Path | None = None,
    variables: Mapping[str, object] | None = None,
) -> str:
    """Render a composable prompt from role/task/format parts.

    ``spec.role`` must be set — there is no runtime-role fallback at
    the composer level. Builders own the prompt-taxonomy defaults;
    profile authors declare ``prompt.role`` explicitly.

    Side effect: stashes the rendered parts (with their resolution
    source and ADR-0026 stability metadata) into
    :mod:`core.observability.prompt_trace` so the builder gateway
    can finalize a debug-only composition view and an M2 prefix /
    payload render envelope.
    """
    values = dict(variables or {})
    parts: list[PromptPart] = []
    for name in spec.part_names():
        body = prompt_loader.render_prompt(name, project_dir=project_dir, **values)
        if not body.strip():
            continue
        directory, _, leaf = name.partition("/")
        kind = _KIND_FROM_DIR.get(directory, directory)
        source = prompt_loader.resolution_source(name, project_dir=project_dir) or "core"
        try:
            raw = prompt_loader.raw_template(name, project_dir=project_dir)
        except FileNotFoundError:
            # Reachable only when a caller monkey-patches
            # ``prompt_loader.render_prompt`` to alias a missing
            # template name. Production paths cannot reach here:
            # if the template were missing, ``render_prompt`` above
            # would have raised first. Fall back to the default
            # metadata so the spied composition still works.
            raw = ""
        stability, cache_scope, reason = _classify_editable_part(
            raw, resolution_source=source,
        )
        parts.append(
            PromptPart(
                kind=kind,
                name=leaf,
                source=source,
                body=body,
                stability=stability,
                cache_scope=cache_scope,
                volatile_reason=reason,
            ),
        )
    prompt_trace.set_last_upper(tuple(parts))
    return "\n\n".join(p.body for p in parts).strip()


def render_composed_prompt(
    spec: PromptSpec,
    *,
    project_dir: str | Path | None = None,
    variables: Mapping[str, object] | None = None,
) -> str:
    """Render ``spec`` from composable ``roles/`` + ``tasks/`` +
    ``formats/`` parts.

    Project/workspace overrides are supported at the composable-part
    level (``roles/<name>``, ``tasks/<name>``, ``formats/<name>``).
    There is no root-level flat-template fallback.
    """
    return render_prompt_parts(
        spec,
        project_dir=project_dir,
        variables=dict(variables or {}),
    )


# ---------------------------------------------------------------------------
# ADR 0028 / M10.5: cache-first assembler.
# ---------------------------------------------------------------------------

# Cache breadth tier order. Broader scopes ship earlier in the wire
# so provider prefix caches hit the largest possible leading run.
# ``SESSION`` sits between PROJECT and NONE because a session-scoped
# part is reused only within one physical session — narrower than a
# project but still cacheable on a resumed session.
_TIER_ORDER: tuple[PromptCacheScope, ...] = (
    PromptCacheScope.GLOBAL,
    PromptCacheScope.WORKSPACE,
    PromptCacheScope.PROJECT,
    PromptCacheScope.SESSION,
    PromptCacheScope.NONE,
)
_TIER_INDEX: dict[PromptCacheScope, int] = {
    scope: idx for idx, scope in enumerate(_TIER_ORDER)
}

# Within a tier, prefer broader / more invariant kinds first.
# Protected contracts (``system_tail``) carry the universal
# strategies and parser schemas every other part depends on, so
# they lead each tier. Editable composable parts follow in
# role → format → task order so a surface that varies only ``task``
# (ADR 0027 fanout, M13) keeps the longest shared prefix possible.
# ``context`` (project / workspace) closes the cacheable tier slot;
# dynamic kinds (``artifact``, ``turn_input``, ``feedback``,
# ``minimal_intent``) sit at the end of their tier — usually
# TIER NONE.
_KIND_ORDER: dict[str, int] = {
    "system_tail": 0,
    "plan_contract": 1,
    "plan_tasks": 2,
    "handoff_contract": 3,
    "role": 4,
    "format": 5,
    "task": 6,
    "context": 7,
    "artifact": 8,
    "minimal_intent": 9,
    "turn_input": 10,
    "feedback": 11,
    "reviewer_critique": 11,
    "human_feedback": 12,
}
_KIND_DEFAULT_ORDER = 99


def _assembly_key(
    indexed: tuple[int, PromptPart],
) -> tuple[int, int, int, int]:
    """Sort key for :func:`assemble_cache_first_segments`.

    Returns ``(eligibility, tier_index, kind_index, original_index)``.

    The leading ``eligibility`` slot keeps every
    :func:`envelope.is_prefix_eligible` part ahead of every non-
    eligible part in the assembler output. M2's contiguous-prefix
    rule otherwise demotes a prefix-eligible part that renders after
    a non-eligible part into the payload partition — pinning
    eligibility first guarantees the actual leading wire bytes are
    the cacheable ones.

    Within ``eligibility``, sort by cache-breadth tier (broader
    first) and then by kind sub-order, with original index as the
    stable-sort tiebreaker. Parts with unknown kinds fall to the end
    of their tier without crashing.
    """
    from pipeline.prompts.envelope import is_prefix_eligible

    idx, part = indexed
    eligibility = 0 if is_prefix_eligible(part) else 1
    tier = _TIER_INDEX.get(part.cache_scope, len(_TIER_ORDER))
    kind = _KIND_ORDER.get(part.kind, _KIND_DEFAULT_ORDER)
    return (eligibility, tier, kind, idx)


def assemble_cache_first_segments(
    parts: Iterable[PromptPart],
) -> PromptTurn:
    """Assemble *parts* into a :class:`PromptTurn` ordered by cache breadth.

    ADR 0028 / M10.5 / ADR 0060: the single ordering authority for wire
    prompts.  Sorts *parts* into the cache-tier hierarchy
    (``GLOBAL → WORKSPACE → PROJECT → SESSION → NONE``) and within each tier
    into a fixed kind sub-order (protected contracts → role → format → task →
    context → dynamic).

    Each non-empty-body part becomes a :class:`PromptSegment` with separator
    glue baked in:

    - First non-empty part → ``segment.text = part.body`` (no prefix)
    - Subsequent non-empty parts → ``segment.text = "\\n\\n" + part.body``
    - Empty-body parts → ``segment.text = ""`` (zero wire bytes)

    ``turn.text`` is therefore byte-identical to the old
    ``"\\n\\n".join(p.body for p in ordered if p.body).strip()`` output
    when every ``part.body`` is already stripped (which production paths
    guarantee — bodies come from :func:`render_prompt` or code-owned
    ``block.render()`` calls that do not produce leading/trailing whitespace).

    The sort is stable: parts that share ``(cache_scope, kind)`` keep their
    original relative order.  Empty bodies are kept (an empty part still has
    envelope identity).
    """
    from pipeline.prompts.turn import PromptSegment, PromptTurn

    parts_list = list(parts)
    if not parts_list:
        return PromptTurn(segments=())
    ordered = tuple(
        p for _idx, p in sorted(enumerate(parts_list), key=_assembly_key)
    )
    segments: list[PromptSegment] = []
    non_empty_count = 0
    for pos, part in enumerate(ordered):
        seg_id = f"{part.id}:{pos}"
        if not part.body:
            segments.append(PromptSegment(text="", part=part, segment_id=seg_id))
        else:
            prefix = "" if non_empty_count == 0 else "\n\n"
            segments.append(PromptSegment(
                text=prefix + part.body,
                part=part,
                segment_id=seg_id,
            ))
            non_empty_count += 1
    return PromptTurn(segments=tuple(segments))


# ---------------------------------------------------------------------------
# TYPE_CHECKING-only import resolved at runtime inside the function body
# above to avoid a circular import (turn.py imports from types.py which
# this module also imports).
# ---------------------------------------------------------------------------
if False:
    from pipeline.prompts.turn import PromptTurn  # noqa: F401  (type stub)
