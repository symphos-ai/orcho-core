"""pipeline/cross_project/artifact_bundle.py — single-call cross-contract review.

Replaces the per-alias ``contract_check`` invocations (one Codex call
per project alias) with a single bundled call covering the whole cross
run. The reviewer receives:

- the cross plan;
- the per-alias project paths and any persisted per-alias signals
  (final_acceptance verdict, contract status, blocker / gap counts);
- each alias's actual unified diff (ADR 0053), capped, so the reviewer
  verifies producer/consumer contract consistency against the changed
  code without re-scanning the repos;
- the focused cross-project consistency checklist.

It returns the same review-shape JSON envelope the per-alias loop
returned. The runner mirrors the single verdict into per-alias entries
so downstream consumers (evidence, Web, MCP) keep their alias-keyed
shape; the entry carries ``source="artifact_bundle"`` so consumers can
distinguish the bundled verdict from the legacy per-alias one.

Metrics contract: exactly one ``codex.invoke`` call per cross-run for
``contract_check``. Token usage is captured once and recorded against
``contract_check`` in the cross-phase usage accumulator.

The bundle has its own prompt surface instead of the uncommitted-review
surface, so the system-tail does **not** carry ``review_target_strategy``.
The reviewer reviews the bundle markdown inline — there is no working-tree
pointer the runtime should resolve. That distinction is load-bearing: it
stops the runtime from re-reading per-project repos and defeats the cost
reduction this commit landed.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

# Soft cap on how much of the cross plan we splice into the bundle.
# The whole point of artifact-bundle is to keep the prompt compact;
# letting a long cross_plan.md balloon the prompt would defeat the
# cost-reduction goal. The truncated tail is preserved in the original
# session artifact (cross_plan.md on disk), so this only affects the
# reviewer's working context, not the audit trail.
_CROSS_PLAN_CHAR_CAP = 8000

# Per-alias diff cap (ADR 0053). The whole reason contract_check is a
# bundle is to avoid re-reading every repo; splicing each alias's diff
# in lets the reviewer actually verify producer/consumer field names in
# code without scanning the trees, but an unbounded diff would defeat
# that. The full diff stays on disk (per-alias diff.patch / run
# artifacts); this cap only bounds the reviewer's working context.
_ALIAS_DIFF_CHAR_CAP = 6000


def build_bundle_markdown(
    *,
    task: str,
    projects: Mapping[str, Path],
    session_phases: Mapping[str, Any],
    cross_plan_markdown: str = "",
    cross_plan_char_cap: int = _CROSS_PLAN_CHAR_CAP,
    alias_diffs: Mapping[str, str] | None = None,
    alias_diff_char_cap: int = _ALIAS_DIFF_CHAR_CAP,
) -> str:
    """Compose the bundle markdown body for the artifact-bundle gate.

    Returns plain markdown — no system-tail blocks. Callers pass this to
    :func:`build_bundle_review_prompt` so the review JSON contract is
    attached without dragging in ``review_target_strategy`` (which would
    tell the runtime to scan a working tree or commit, defeating the
    cost-reduction purpose of the bundle).

    ADR 0053: ``alias_diffs`` (alias → unified diff text) is spliced in
    as a ``## Per-alias diffs`` section so the reviewer can verify
    cross-project contract consistency against the **actual changed
    code**, not just the plan + child verdicts. Without it the gate only
    re-attests the bundle's textual claims. The caller captures the
    diffs (the function stays pure — no git I/O) and each is capped to
    ``alias_diff_char_cap``. Sentinel/empty diffs (``"(no diff)"`` /
    ``"(diff unavailable)"`` / empty) are skipped so a verification-only
    alias adds no noise.
    """
    lines: list[str] = []
    lines.append("# Cross-contract bundle")
    lines.append("")
    lines.append(f"**Task:** {task[:400]}")
    lines.append("")
    lines.append("## Aliases under review")
    for alias, project_path in projects.items():
        lines.append(f"- **[{alias}]** `{project_path}`")
        per_alias = _per_alias_summary(alias, session_phases)
        for sub in per_alias:
            lines.append(f"    - {sub}")
    lines.append("")

    if cross_plan_markdown:
        trimmed, was_trimmed = _trim_to_cap(
            cross_plan_markdown.strip(), cross_plan_char_cap,
        )
        lines.append("## Cross plan")
        lines.append("")
        lines.append("```")
        lines.append(trimmed)
        if was_trimmed:
            lines.append(
                "\n[…truncated for bundle review; full plan on disk "
                "as cross_plan.md]"
            )
        lines.append("```")
        lines.append("")

    _diff_lines = _render_alias_diffs(alias_diffs, projects, alias_diff_char_cap)
    if _diff_lines:
        lines.extend(_diff_lines)

    lines.append("## Verification checklist")
    lines.append(
        "Verify CROSS-PROJECT consistency across the aliases listed "
        "above:"
    )
    lines.append(
        "- API response field names match what client/Unity expects"
    )
    lines.append("- DB column names match what stats / SQL queries use")
    lines.append(
        "- Event / payload field names are consistent across producer "
        "and consumer"
    )
    lines.append(
        "- Hardcoded values (IDs, string constants) are the same "
        "everywhere"
    )
    lines.append(
        "- Version / schema mismatches between producer and consumer"
    )
    if _diff_lines:
        lines.append(
            "- Ground every finding in the per-alias diffs above: confirm "
            "the changed code on the producer and consumer sides actually "
            "agree on the shared field/payload names and types."
        )
    lines.append("")
    lines.append(
        "Issue a single JSON verdict for the system as a whole. "
        "Per-alias detail belongs in findings; the verdict applies to "
        "the coordinated change across all aliases."
    )
    return "\n".join(lines)


# Sentinel strings ``worktree_diff_against_base`` returns for clean /
# unavailable diffs — skipped so they add no bundle noise.
_EMPTY_DIFF_SENTINELS = frozenset({"(no diff)", "(diff unavailable)"})


def _render_alias_diffs(
    alias_diffs: Mapping[str, str] | None,
    projects: Mapping[str, Path],
    char_cap: int,
) -> list[str]:
    """Render the ``## Per-alias diffs`` section (ADR 0053).

    Iterates in ``projects`` order so the section is deterministic and
    aligned with the alias list above. Skips aliases with no diff
    (sentinel / empty) so a verification-only alias adds nothing.
    Returns ``[]`` when there is no diff to show — the caller then omits
    the section header entirely.
    """
    if not alias_diffs:
        return []
    out: list[str] = []
    for alias in projects:
        raw = (alias_diffs.get(alias) or "").strip()
        if not raw or raw in _EMPTY_DIFF_SENTINELS:
            continue
        trimmed, was_trimmed = _trim_to_cap(raw, char_cap)
        out.append(f"### [{alias}] diff")
        out.append("")
        out.append("```diff")
        out.append(trimmed)
        if was_trimmed:
            out.append(
                "\n[…truncated for bundle review; full diff in run "
                "artifacts]"
            )
        out.append("```")
        out.append("")
    if not out:
        return []
    return ["## Per-alias diffs", "", *out]


def build_bundle_review_prompt(
    bundle_markdown: str,
    *,
    project_dir: str = "",
) -> str:
    """Compose the single-call cross-contract review prompt.

    The bundle markdown is the review target. This prompt attaches only the
    typed review JSON contract, not the working-tree review-target strategy.
    """
    from core.infra.config import AppConfig
    from pipeline.prompts.composer import PromptSpec, render_composed_prompt
    from pipeline.prompts.contracts import review_json_contract

    cfg = AppConfig.load()
    # ADR 0028 / M10.5 Step 2 + 5: cross_contract_bundle task file is
    # static method prose; the bundle markdown rides as a typed
    # ``artifact`` part and the render routes through the cache-first
    # gateway so the assembler is the single ordering authority.
    from pipeline.prompts.builders import _render_prompt_output
    from pipeline.prompts.types import (
        PromptCacheScope,
        PromptLayer,
        PromptPart,
        PromptStability,
    )

    rendered = render_composed_prompt(
        PromptSpec(
            role="code_reviewer",
            task="cross_contract_bundle",
            format="detailed",
        ),
        project_dir=project_dir or None,
        variables={},
    )
    bundle_part = PromptPart(
        kind="artifact",
        name="cross_contract_bundle",
        source="artifact",
        body=f"BUNDLE:\n{bundle_markdown}",
        layer=PromptLayer.TURN,
        stability=PromptStability.TURN,
        cache_scope=PromptCacheScope.NONE,
        volatile_reason="cross-project contract bundle markdown under review",
        id="artifact:cross_contract_bundle",
    )
    return _render_prompt_output(
        rendered,
        system_tail=(review_json_contract(body_language=cfg.task_language),),
        extra_upper_parts=(bundle_part,),
    )


def _trim_to_cap(text: str, cap: int) -> tuple[str, bool]:
    """Trim ``text`` to at most ``cap`` characters, preserving the
    beginning. Returns ``(trimmed_text, was_trimmed)`` so callers can
    attach a marker about the truncation."""
    if cap <= 0 or len(text) <= cap:
        return text, False
    return text[:cap], True


def mirror_review_to_aliases(
    *,
    aliases: tuple[str, ...],
    parsed_dict_template: Mapping[str, Any],
    raw_response: str,
    rendered: str,
) -> dict[str, dict[str, Any]]:
    """Mirror the bundle's single review verdict into per-alias entries.

    The bundle produces one ``ParsedReview`` covering the whole system;
    each alias gets a copy of that verdict with ``source="artifact_bundle"``
    so the existing per-alias consumer shape continues to work and the
    audit trail records how the verdict was produced.
    """
    out: dict[str, dict[str, Any]] = {}
    for alias in aliases:
        out[alias] = {
            **{k: _copy_value(v) for k, v in parsed_dict_template.items()},
            "rendered": rendered,
            "raw_response": raw_response,
            "source": "artifact_bundle",
        }
    return out


def _per_alias_summary(
    alias: str, session_phases: Mapping[str, Any],
) -> list[str]:
    """Compact bullet list of per-alias signals already captured in
    the session.

    The cross-contract reviewer needs to see what each child's
    ``final_acceptance`` already found so it can fold those signals
    into the system-level verdict. Without this the bundle is missing
    half the picture: every per-project release blocker and
    verification gap that the child reviewer already flagged would be
    invisible at the cross level, and the artifact-bundle path would
    silently degrade reviewer quality versus the legacy per-alias
    repo-wide scan.

    Carries (when present):

    - ``final_acceptance.verdict``
    - ``ship_ready``
    - ``short_summary``
    - ``contract_status`` (the four task_contract / interfaces /
      persistence / tests aspects)
    - ``release_blockers`` count + ids (full bodies stay in the
      child's own evidence — the cross reviewer only needs the
      identifier to correlate)
    - ``verification_gaps`` count + risks (compact)

    Returns an empty list when no ``final_acceptance`` entry exists
    for the alias yet (e.g. plan-only profile, or a child that
    crashed before the gate).
    """
    projects_phase = session_phases.get("projects")
    if not isinstance(projects_phase, Mapping):
        return []
    child = projects_phase.get(alias)
    if not isinstance(child, Mapping):
        return []
    phases = child.get("phases")
    if not isinstance(phases, Mapping):
        return []
    fa = phases.get("final_acceptance")
    if not isinstance(fa, Mapping):
        return []
    out: list[str] = []
    verdict = fa.get("verdict")
    if isinstance(verdict, str):
        out.append(f"final_acceptance verdict: {verdict}")
    ship_ready = fa.get("ship_ready")
    if isinstance(ship_ready, bool):
        out.append(f"ship_ready: {ship_ready}")
    short = fa.get("short_summary")
    if isinstance(short, str) and short.strip():
        out.append(f"summary: {short[:160]}")
    cs = fa.get("contract_status")
    if isinstance(cs, Mapping):
        fields = ("task_contract", "interfaces", "persistence", "tests")
        parts = [
            f"{f}={cs[f]}" for f in fields
            if isinstance(cs.get(f), str)
        ]
        if parts:
            out.append("contract_status: " + ", ".join(parts))
    blockers = fa.get("release_blockers")
    if isinstance(blockers, list) and blockers:
        ids = [
            str(b.get("id"))
            for b in blockers
            if isinstance(b, Mapping) and b.get("id") is not None
        ]
        if ids:
            out.append(
                f"release_blockers ({len(blockers)}): " + ", ".join(ids[:8])
                + ("…" if len(ids) > 8 else "")
            )
        else:
            out.append(f"release_blockers: {len(blockers)} (no ids)")
    gaps = fa.get("verification_gaps")
    if isinstance(gaps, list) and gaps:
        risks = [
            str(g.get("risk"))
            for g in gaps
            if isinstance(g, Mapping) and g.get("risk")
        ]
        if risks:
            shortened = [r[:80] for r in risks[:4]]
            tail = "…" if len(risks) > 4 else ""
            out.append(
                f"verification_gaps ({len(gaps)}): "
                + " | ".join(shortened) + tail
            )
        else:
            out.append(f"verification_gaps: {len(gaps)}")
    return out


def _copy_value(value: Any) -> Any:
    """Shallow copy of list/dict structures so per-alias mirror entries
    are independent objects (downstream consumers occasionally mutate
    findings / risks / checks lists)."""
    if isinstance(value, list):
        return list(value)
    if isinstance(value, dict):
        return dict(value)
    return value


__all__ = [
    "build_bundle_markdown",
    "build_bundle_review_prompt",
    "mirror_review_to_aliases",
]
