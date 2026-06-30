"""core/io/transcript.py — CLI run-transcript renderer.

Turns a raw agent run into a structured, readable transcript without
hiding any information. Pipeline behavior is unchanged — this module is
pure presentation.

Design rules:

* **No information loss.** Every renderer returns the full content it was
  given. Truncation is the caller's choice via explicit knobs, never a
  default.
* **Format, don't shorten.** Long bodies are wrapped in light, scoped
  delimiters so phase boundaries stay visible without dominating the
  screen with heavy banners.
* **Cognitive header before detail.** Phase / agent / review blocks all
  start with a compact metadata line so an operator can scan a run
  without reading bodies.
* **Structured contracts render structurally.** Reviewer JSON contracts
  surface as ``Verdict``, ``Summary``, ``Findings``, ``Risks``,
  ``Checks`` — not as undifferentiated markdown.

All renderers return strings. ``print_*`` helpers exist alongside for
the common case where the caller would otherwise wrap each call in
``print()``. Tests pin the rendered shape.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from core.io.ansi import C, paint

if TYPE_CHECKING:
    from core.io.verification_header import VerificationHeaderView
    from pipeline.prompts.turn import PromptTraceView
    from pipeline.prompts.types import PromptPart


# Thin rule used for phase boundaries and transcript framing. The legacy
# heavy ``═`` × 62 banner is replaced with a single thin rule so phase
# boundaries stay scannable without dominating the screen.
RULE_WIDTH = 60
THIN_RULE = "─" * RULE_WIDTH
_SKILL_NAME_RE = re.compile(r"[`'\"](?P<name>[A-Za-z0-9][A-Za-z0-9_.-]{0,80})[`'\"]")
_SKILL_TRIGGER_RE = re.compile(
    r"\b(?:skill|using|use|used)\b|использ|скилл|скил|навык",
    re.IGNORECASE,
)


def _line(color: str, text: str = "") -> str:
    if not text:
        return ""
    return paint(text, color)


def detect_registered_skill_use(
    text: str,
    skill_names: Iterable[str] | None = None,
) -> str | None:
    """Return the registered skill named by an assistant prose chunk.

    Agents usually announce skill selection in free-form prose, for example
    ``Использую `frontend-qa`, потому что...``. Treat it as a first-class
    transcript action only when the quoted name is present in the supplied
    registry and the surrounding text contains a skill-use verb.
    """
    names = {name for name in (skill_names or ()) if name}
    if not text or not names:
        return None
    sample = text.strip()[:600]
    if not _SKILL_TRIGGER_RE.search(sample):
        return None
    for match in _SKILL_NAME_RE.finditer(sample):
        candidate = match.group("name")
        if candidate in names:
            return candidate
    return None


def format_skill_use_text(skill_name: str, text: str) -> str:
    """Render a detected skill selection as its own transcript action."""
    lines = [line.rstrip() for line in text.strip().splitlines()]
    if not lines:
        return ""

    rendered: list[str] = []
    for idx, line in enumerate(lines):
        if idx == 0:
            rendered.append(f"  🧠 Skill: {skill_name} — {line}")
        else:
            rendered.append(f"     {line}")
    return "\n".join(rendered) + "\n"


def format_thinking_text(
    text: str,
    *,
    skill_names: Iterable[str] | None = None,
) -> str:
    """Render visible assistant prose in the same compact style as tool calls."""
    lines = [line.rstrip() for line in text.strip().splitlines()]
    if not lines:
        return ""
    skill_name = detect_registered_skill_use(text, skill_names)
    if skill_name:
        return format_skill_use_text(skill_name, text)

    rendered: list[str] = []
    for idx, line in enumerate(lines):
        if idx == 0:
            rendered.append(f"  💬 Assistant: {line}")
        else:
            rendered.append(f"     {line}")
    return "\n".join(rendered) + "\n"


# ── Run header ─────────────────────────────────────────────────────────


def render_run_header(
    *,
    run_id: str | None,
    project: str,
    task: str,
    agents: Iterable[Mapping[str, str]],
    profile: str,
    session_mode: str,
    rounds: int,
    plan: bool,
    output_log: str | None = None,
    events_log: str | None = None,
    plugin_line: str | None = None,
    skills_line: str | None = None,
    verification: VerificationHeaderView | None = None,
    resumed: bool = False,
    completed_phases: Iterable[str] = (),
    parent_run_id: str | None = None,
    project_alias: str | None = None,
    followup_parent_run_id: str | None = None,
    followup_base_task: str | None = None,
    followup_parent_status: str | None = None,
    followup_child_status: str | None = None,
    followup_active_handoff_id: str | None = None,
) -> str:
    """Render the run header block.

    ``agents`` is a sequence of ``{"role", "model", "effort"}`` mappings,
    each optionally carrying a sanitized ``account`` diagnostic hint
    (``account=<label> / <email>``) rendered after the effort column.
    The block keeps every field the legacy header carried — model names,
    effort levels, profile, session mode, max rounds, plan toggle,
    plugin line, resume notice, output / event log paths — but lays
    them out as a scannable table instead of a single dense line.

    ``parent_run_id`` + ``project_alias`` mark sub-pipeline runs spawned
    by ``orcho cross``. When set, the title carries a "sub-pipeline
    [<alias>]" chip and ``Task`` is relabeled ``Subtask`` so a reviewer
    can tell at a glance that they're looking at one slice of a larger
    cross-project run rather than a standalone run.

    ``followup_parent_run_id`` + ``followup_base_task`` mark a
    ``--resume`` follow-up: a new run that uses an earlier run as
    context. The title carries a ``follow-up`` chip and a "Follow-up of"
    / "Base task" row pair so it's obvious at a glance which earlier
    run the operator is iterating on.
    """
    parts: list[str] = []
    is_subpipeline = bool(parent_run_id and project_alias)
    is_followup = bool(followup_parent_run_id) and not is_subpipeline

    title_bits: list[str] = ["Orcho Run"]
    if run_id:
        title_bits.append(run_id)
    title_bits.append(profile)
    if is_subpipeline:
        title_bits.append(f"sub-pipeline [{project_alias}]")
    if is_followup:
        title_bits.append(f"follow-up of {followup_parent_run_id}")
    if resumed:
        title_bits.append("resumed")
    parts.append(_line(C.MAGENTA + C.BOLD, "  ".join(title_bits)))

    if is_subpipeline:
        parts.append(_kv(
            "Parent",
            _dim(f"cross-run {parent_run_id} · alias [{project_alias}]"),
            C.CYAN,
        ))
    elif is_followup:
        parts.append(_kv(
            "Follow-up of",
            _dim(str(followup_parent_run_id)),
            C.CYAN,
        ))
        if followup_parent_status:
            parts.append(_kv(
                "Parent status",
                _dim(str(followup_parent_status)),
                C.CYAN,
            ))
        if followup_child_status:
            parts.append(_kv(
                "This run status",
                _dim(str(followup_child_status)),
                C.CYAN,
            ))
        if followup_active_handoff_id:
            parts.append(_kv(
                "Active handoff",
                _dim(str(followup_active_handoff_id)),
                C.CYAN,
            ))
        # Make the resume target explicit so it is never ambiguous
        # whether the parent or the child run is the one executing.
        parts.append(_kv(
            "Resuming",
            _dim(
                f"follow-up child {run_id}" if run_id
                else "follow-up child"
            ),
            C.CYAN,
        ))
        if followup_base_task:
            parts.append(_kv(
                "Base task",
                _dim(_crop(followup_base_task, 200)),
                C.CYAN,
            ))

    parts.append(_kv("Project", project, C.CYAN))
    task_label = "Subtask" if is_subpipeline else "Task"
    parts.append(_kv(task_label, _crop(task, 200), C.CYAN))
    if plugin_line:
        parts.append(_kv("Plugin", plugin_line, C.CYAN))
    if skills_line:
        parts.append(_kv("Skills", skills_line, C.CYAN))
    if verification is not None:
        from core.io.verification_header import render_verification_header
        parts.append(render_verification_header(verification))

    parts.append("")
    parts.append(_line(C.CYAN + C.BOLD, "Agents"))
    for entry in agents:
        role = str(entry.get("role", "?"))
        model = str(entry.get("model", "?"))
        effort = str(entry.get("effort") or "")
        # ``account`` is a pre-formatted, sanitized diagnostic hint
        # (``account=<label> / <email>``) attached upstream when a runtime
        # could safely report which account/org the run uses. Absent when
        # identity is unavailable — the contract is "show nothing", not a
        # placeholder.
        account = str(entry.get("account") or "")
        line = f"  {_pad(role, 10)}{_pad(model, 22)}{_dim(effort)}"
        if account:
            line = f"{line}  {_dim(account)}"
        parts.append(line)

    parts.append("")
    parts.append(_line(C.CYAN + C.BOLD, "State"))
    # ``plan=skip`` reads as a failure to a fresh operator. When the
    # profile legitimately has no PLAN phase (e.g. ``task`` / ``review``
    # variants, or sub-pipelines under ``orcho cross``), explain why so
    # they don't go hunting for an error.
    if plan:
        plan_label = "yes"
    elif is_subpipeline:
        plan_label = "skip  (cross-plan already supplied)"
    elif profile in ("task", "review"):
        plan_label = f"skip  (profile={profile} has no PLAN phase)"
    else:
        plan_label = "skip"
    parts.append(
        _kv(
            "session",
            f"{session_mode}  rounds={rounds}  plan={plan_label}",
            C.CYAN, indent=2,
        )
    )
    completed = list(completed_phases)
    if completed:
        parts.append(
            _kv(
                "checkpoint",
                f"{len(completed)} phases completed: {', '.join(completed)}",
                C.CYAN,
                indent=2,
            )
        )
    if output_log:
        parts.append(_kv("output", output_log, C.GREY, indent=2))
    if events_log:
        parts.append(_kv("events", events_log, C.GREY, indent=2))
    parts.append("")
    return "\n".join(parts)


# ── Cross-run header ──────────────────────────────────────────────────


def render_cross_run_header(
    *,
    run_id: str | None,
    task: str,
    projects: Mapping[str, str],
    agents: Iterable[Mapping[str, str]],
    project_agents: Iterable[Mapping[str, str]] = (),
    cross_mode: str,
    rounds: int,
    profile: str | None = None,
    plan_source: str | None = None,
    projection: str | None = None,
    output_log: str | None = None,
    events_log: str | None = None,
    resumed: bool = False,
    followup_parent_run_id: str | None = None,
    followup_base_task: str | None = None,
) -> str:
    """Render the cross-project run header.

    Mirrors :func:`render_run_header` so the cross transcript shares the
    same scannable layout, but lists the project map (alias → path) in
    place of a single ``Project`` line and splits agents into two
    explicit groups: cross-level agents (cross_hypothesis / cross_plan /
    contract_check / cross_final_acceptance) and projected per-project
    agents. Sub-pipelines emit their own per-project headers via
    :func:`render_run_header` when they start.

    ``profile`` / ``plan_source`` / ``projection`` surface the workflow
    knob and projection result up-front: a cross run always has
    ``plan_source="cross"`` (the cross-level plan is canonical), and
    ``projection`` is typically ``"global + per-project"``.
    """
    parts: list[str] = []
    is_followup = bool(followup_parent_run_id)
    title_bits: list[str] = ["Orcho Cross-Run"]
    if run_id:
        title_bits.append(run_id)
    title_bits.append(f"mode={cross_mode}")
    if is_followup:
        title_bits.append(f"follow-up of {followup_parent_run_id}")
    if resumed:
        title_bits.append("resumed")
    parts.append(_line(C.MAGENTA + C.BOLD, "  ".join(title_bits)))

    if is_followup:
        parts.append(_kv(
            "Follow-up of",
            _dim(str(followup_parent_run_id)),
            C.CYAN,
        ))
        if followup_base_task:
            parts.append(_kv(
                "Base task",
                _dim(_crop(followup_base_task, 200)),
                C.CYAN,
            ))

    parts.append(_kv("Task", _crop(task, 200), C.CYAN))
    parts.append(_kv("Projects", f"{len(projects)} aliases", C.CYAN))
    for alias, path in projects.items():
        parts.append(_kv(f"  [{alias}]", path, C.GREY, indent=2))
    if profile:
        parts.append(_kv("Profile", profile, C.CYAN))
    if plan_source:
        parts.append(_kv("Plan source", plan_source, C.CYAN))
    if projection:
        parts.append(_kv("Projection", projection, C.CYAN))

    parts.append("")
    parts.append(_line(C.CYAN + C.BOLD, "Cross Agents"))
    for entry in agents:
        role = str(entry.get("role", "?"))
        model = str(entry.get("model", "?"))
        effort = str(entry.get("effort") or "")
        parts.append(
            f"  {_pad(role, 18)}{_pad(model, 22)}{_dim(effort)}"
        )

    parts.append("")
    parts.append(_line(C.CYAN + C.BOLD, "Project Agents"))
    project_entries = list(project_agents)
    if project_entries:
        for entry in project_entries:
            role = str(entry.get("role", "?"))
            model = str(entry.get("model", "?"))
            effort = str(entry.get("effort") or "")
            parts.append(
                f"  {_pad(role, 18)}{_pad(model, 22)}{_dim(effort)}"
            )
    else:
        parts.append(f"  {_dim('(none - no per-project phases in this run)')}")

    parts.append("")
    parts.append(_line(C.CYAN + C.BOLD, "State"))
    parts.append(_kv("session", f"rounds_per_project={rounds}", C.CYAN, indent=2))
    if output_log:
        parts.append(_kv("output", output_log, C.GREY, indent=2))
    if events_log:
        parts.append(_kv("events", events_log, C.GREY, indent=2))
    parts.append("")
    return "\n".join(parts)


# ── Phase header ───────────────────────────────────────────────────────

# Phase-header continuity. The ``→ runtime=…`` invocation line is emitted
# by the runtime adapter, while the section banner is emitted separately by
# the orchestration layer. Because they live in different layers, a phase
# that fires several invocations under a single banner (e.g. the cross
# contract-check loop iterating projects) only gets a title above the FIRST
# invocation. To keep the invariant "every runtime invocation has its
# section title directly above it", ``render_agent_invocation`` synthesises
# a header from the active phase context whenever no header was rendered
# since the previous invocation. ``render_phase_header`` marks the state
# fresh so an explicit banner is never duplicated. Single-process, main-
# thread render path — plain module globals match the rest of this module.
_phase_header_fresh: bool = False
_last_phase_header_color: str = C.MAGENTA


def reset_phase_header_continuity() -> None:
    """Clear the phase-header continuity state (test hook)."""
    global _phase_header_fresh, _last_phase_header_color
    _phase_header_fresh = False
    _last_phase_header_color = C.MAGENTA


def mark_phase_header_fresh() -> None:
    """Record that a section title was just rendered by another renderer.

    The agent-invocation line synthesises a phase header when none was
    rendered since the previous invocation (so loop phases without their own
    per-iteration banner still get a title). A caller that emits its OWN
    per-step title through a different renderer — e.g. the ``subtask_dag``
    ``ORCHO subtask N/M START`` marker (``agents.stream``) — calls this so the
    next invocation does not stack a redundant ``[id] TITLE`` banner on top of
    that marker. Does not change ``_last_phase_header_color``.
    """
    global _phase_header_fresh
    _phase_header_fresh = True


def render_phase_header(
    phase_id: str,
    title: str,
    *,
    status: str | None = None,
    color: str = C.MAGENTA,
) -> str:
    """Render a thin phase boundary.

    Replaces the heavy ``═`` × 62 / triple-line banner with a single
    rule plus a ``[id] TITLE  status`` header line. Keeps the phase
    boundary visible without monopolising vertical space.
    """
    global _phase_header_fresh, _last_phase_header_color
    _phase_header_fresh = True
    _last_phase_header_color = color
    head_bits = [paint(f"[{phase_id}] {title}", color, C.BOLD)]
    if status:
        head_bits.append(_status_chip(status))
    head = "  ".join(head_bits)
    return f"\n{_line(color, THIN_RULE)}\n{head}\n{_line(color, THIN_RULE)}"


# ── Agent invocation ───────────────────────────────────────────────────


def render_agent_invocation(
    *,
    runtime: str,
    model: str,
    mode: str,
    effort: str | None = None,
    prompt: str | None = None,
    trace_view: PromptTraceView | None = None,
    session_id: str | None = None,
    continue_session: bool | None = None,
    session_supported: bool = True,
    cwd: str | None = None,
) -> str:
    """Render the one-line agent invocation echo printed before each
    agent CLI call.

    Format (single line, key=value):

        → runtime=claude · model=claude-opus-4-7 · effort=high
          · role=systems_architect · task=cross_plan · format=detailed
          · mode=read · session=fresh · cwd=/path

    ``mode`` is the per-call IAgentRuntime semantic, not a CLI flag:
    ``read`` for read-only invocations (``mutates_artifacts=False``),
    ``write`` for invocations that may mutate the project tree
    (``mutates_artifacts=True``).

    ``session_id`` is shown in full (no truncation): plugin authors
    debugging chained sessions need the exact id to grep stream-json
    logs against. ``session_supported=False`` emits ``session=n/a``.
    When a session-capable runtime starts a new provider conversation,
    ``continue_session`` lets callers render ``session=fresh`` instead of
    the older, ambiguous ``session=stateless``. The latter is kept only for
    callers that do not report continuation intent.

    All runtime backends route through this helper so the line shape
    is identical regardless of provider — easier to scan a mixed run.
    """
    bits = [
        f"runtime={runtime}",
        f"model={model}",
    ]
    if effort:
        bits.append(f"effort={effort}")
    prompt_bits = _prompt_legend_bits(prompt=prompt, trace_view=trace_view)
    bits.extend(prompt_bits)
    bits.append(f"mode={mode}")

    if not session_supported:
        session_repr = "n/a"
    elif session_id:
        session_repr = f"resume:{session_id}"
    elif continue_session is not None:
        session_repr = "fresh"
    else:
        session_repr = "stateless"
    bits.append(f"session={session_repr}")

    if cwd:
        bits.append(f"cwd={cwd}")

    line = f"  {paint('→', C.CYAN)} " + _dim(" · ".join(bits))

    # Guarantee a section title above the invocation. If no header was
    # rendered since the previous invocation, synthesise one from the
    # active phase context so per-iteration loops (cross contract-check,
    # delivery, …) get a title above every call — not just the first.
    global _phase_header_fresh
    header_prefix = ""
    if not _phase_header_fresh:
        from core.observability.events import current_phase_header
        ctx = current_phase_header()
        if ctx is not None:
            phase_id, phase_title = ctx
            from core.observability.logging import get_verbose
            if get_verbose():
                phase_title = f"[[Debug]] {phase_title}"
            header_prefix = render_phase_header(
                phase_id, phase_title, color=_last_phase_header_color,
            ) + "\n"
    # This invocation consumes the freshness: the next invocation without
    # an intervening banner re-synthesises its own header.
    _phase_header_fresh = False
    return header_prefix + line


def _prompt_legend_bits(
    *,
    prompt: str | None,
    trace_view: PromptTraceView | None,
) -> list[str]:
    """Return role/task/format legend bits for the current prompt.

    ``prompt_trace`` is a best-effort side channel. Match by exact prompt
    text so a direct runtime invocation cannot accidentally reuse a stale
    trace view from an earlier builder-routed phase.
    """
    if trace_view is None:
        return []
    if prompt is not None and trace_view.text != prompt:
        return []

    selected: dict[str, str] = {}
    for part in trace_view.parts:
        if part.kind in {"role", "task", "format"} and part.kind not in selected:
            selected[part.kind] = part.name

    return [
        f"{kind}={selected[kind]}"
        for kind in ("role", "task", "format")
        if kind in selected
    ]


def render_incoming_prompt(
    prompt: str,
    *,
    trace_view: PromptTraceView | None = None,
    model: str = "",
) -> str:
    """Render the incoming prompt block printed under
    :func:`render_agent_invocation` when ``--output debug`` is active.

    The block has four nested layers so a reader can see which prompt
    parts composed the prompt **and what each one contributes**
    without re-reading the agent transcript or jq-ing session.json:

    1. ``Composition`` manifest — one-line summary of every part with
       its size and cache scope
       (``role=systems_architect(core) 2.1k cache=global · …``) so
       the seam structure is greppable and dominance is visible.
    2. ``Totals`` line — sum of estimated tokens + per-class
       breakdown (M14.2 :class:`pipeline.observability.output_class
       .OutputClass`) so a reader can tell at a glance whether the
       prompt is dominated by re-fetchable file dumps, persisted
       artifacts, decision-bearing prose, or ephemeral noise.
    3. Framed body for each part in physical wire order. The heading
       carries the part's identity *plus* its tokens / cache scope /
       lifecycle class so a reader scrolling through bodies can
       still see the metrics without scrolling back to the manifest.
    4. Code-owned protected blocks are rendered where they appear
       in that physical order. The XML envelope itself
       (``<orcho:system-block …>``) is left intact in the body — it
       carries the same ``kind/name/version`` and round-trips into
       machine consumers.

    ``composition=None`` is the fallback path (caller invoked the
    runtime adapter without going through a builder). The renderer
    prints the raw prompt body in a single frame and flags the
    manifest as ``composition unavailable``.
    """
    parts: list[str] = []
    parts.append(_line(C.CYAN + C.BOLD, "  Incoming prompt"))
    parts.append(_line(C.GREY, "  " + THIN_RULE))

    if trace_view is None:
        parts.append("  " + _dim("Composition  (unavailable)"))
        parts.append(_line(C.GREY, "  " + THIN_RULE))
        parts.extend(_render_prompt_frame(None, prompt))
        parts.append(_line(C.GREY, "  " + THIN_RULE))
        return "\n".join(p for p in parts if p)

    segments = trace_view.segments

    # Per-segment metrics computed once and reused in manifest + frames
    # so the displayed numbers are guaranteed identical across the
    # two surfaces.
    metrics_by_seg_id = {
        seg.segment_id: _compute_part_metrics(seg.part, model=model)
        for seg in segments
    }

    manifest_rows: list[tuple[str, dict[str, Any]]] = []
    for seg in segments:
        label = _format_seam_label(seg.part)
        m = metrics_by_seg_id[seg.segment_id]
        manifest_rows.append((label, m))
    parts.extend(_format_composition_manifest(manifest_rows))

    # ── Totals ───────────────────────────────────────────────────────────
    parts.extend(_format_totals_lines(metrics_by_seg_id.values()))

    parts.append(_line(C.GREY, "  " + THIN_RULE))

    for seg in segments:
        # ``seg.text`` bakes the inter-segment separator glue ("\n\n") into
        # the LEADING bytes of every non-first segment (that's the wire-
        # layout invariant). Rendering it verbatim inside the frame would
        # print those blank lines as a spurious indent under the heading.
        # Lift the leading glue out so the frame body shows only this part's
        # own content; the glue is structurally the gap BETWEEN frames, which
        # the ``┌─`` heading already provides. The full wire string is still
        # reconstructable as join(seg.text) — we only relocate where the glue
        # is drawn, we don't drop it.
        body = seg.text.lstrip("\n") if seg.text else seg.text
        parts.extend(
            _render_prompt_frame(seg.part, body, metrics_by_seg_id[seg.segment_id]),
        )
    parts.append(_line(C.GREY, "  " + THIN_RULE))
    return "\n".join(p for p in parts if p)


def _format_seam_label(part: PromptPart) -> str:
    """One-line ``kind=name(source)`` label for the manifest."""
    if part.kind == "system_tail":
        version = f"@v{part.version}" if part.version is not None else ""
        label = _protected_block_label(part.name).lower().replace("-", "_")
        return f"{label}={part.name}{version}({part.source})"
    return f"{part.kind}={part.name}({part.source})"


def _format_composition_manifest(
    rows: list[tuple[str, dict[str, Any]]],
) -> list[str]:
    """Render the prompt-part manifest as scan-friendly rows.

    The old one-line ``Composition`` manifest became unreadable for
    realistic prompts because terminal wrapping split the middle of
    part names and cache annotations. Keep the exact seam label
    string for greppability, but place one prompt part per line with
    aligned token / cache / class columns.
    """
    if not rows:
        return ["  " + _kv("Composition", _dim("(empty)"), C.CYAN)]

    label_width = min(max(len(label) for label, _ in rows), 56)
    out = ["  " + _kv("Composition", _dim(f"{len(rows)} parts"), C.CYAN)]
    for label, metrics in rows:
        token_str = f"{_format_token_count(int(metrics.get('tokens') or 0))} tok"
        cache = metrics.get("cache_scope") or "?"
        cls = metrics.get("class") or "?"
        source = metrics.get("token_source") or "?"
        out.append(
            "    "
            + _dim(
                f"{label:<{label_width}}  "
                f"{token_str:>9}  cache={cache:<9} class={cls} source={source}",
            ),
        )
    return out


_OUTPUT_CONTRACT_NAMES = frozenset({
    "plan_json",
    "review_json",
    "release_json",
})

_POLICY_BLOCK_NAMES = frozenset({
    "change_handoff",
    "review_target",
    "authoring_language",
})


def _protected_block_label(name: str) -> str:
    """Human-facing label for code-owned ``system_tail`` prompt parts.

    ``system_tail`` is still the internal kind because the prompt
    builder owns that contract vocabulary, but after the cache-first
    physical reorder these parts no longer render at the tail. Debug
    output therefore labels their *role* in the prompt instead of the
    legacy placement bucket.
    """
    if name in _OUTPUT_CONTRACT_NAMES:
        return "OUTPUT-CONTRACT"
    if name in _POLICY_BLOCK_NAMES:
        return "POLICY"
    return "CONTRACT"


def _compute_part_metrics(part: PromptPart, *, model: str = "") -> dict[str, Any]:
    """Compute the per-part observable metrics surfaced in the
    debug prompt block.

    Returns a dict so the same numbers feed both the manifest
    one-liner and the frame heading without duplicate computation.
    Pure: no I/O, no global state.
    """
    # Late imports — keep the top-of-module dependency graph
    # honest. ``core.observability.metrics`` and
    # ``pipeline.observability.output_class`` are both light
    # (stdlib only), so the import cost is negligible.
    from core.observability.metrics import estimate_tokens_with_source
    from pipeline.observability.output_class import classify_prompt_part

    body = part.body or ""
    token_estimate = estimate_tokens_with_source(body, model=model)
    cls = classify_prompt_part(
        kind=part.kind, name=part.name, source=part.source,
    )
    cache_scope = (
        part.cache_scope.value
        if hasattr(part.cache_scope, "value")
        else str(part.cache_scope)
    )
    stability = (
        part.stability.value
        if hasattr(part.stability, "value")
        else str(part.stability)
    )
    return {
        "tokens": token_estimate.tokens,
        "token_source": token_estimate.source,
        "tokens_exact": token_estimate.exact,
        "cache_scope": cache_scope,
        "stability": stability,
        "class": cls.value if hasattr(cls, "value") else str(cls),
    }


def _format_token_count(n: int) -> str:
    """Mirror of the live-card token formatter — kept private here
    so transcript renderer has no cross-module dependency on
    ``core.observability.live_card``."""
    if n < 1_000:
        return str(n)
    for divisor, suffix in (
        (1_000_000_000, "G"),
        (1_000_000, "M"),
        (1_000, "k"),
    ):
        if n >= divisor:
            value = n / divisor
            return f"{value:.1f}{suffix}"
    return str(n)


def _format_totals_lines(metrics: Iterable[dict[str, Any]]) -> list[str]:
    """Render the ``Totals`` summary block under the manifest.

    Format::

        Totals  2.9k tok total
          re_fetchable      2.0k
          decision_bearing  0.6k
          ephemeral         0.3k

    Class breakdown only shows non-zero buckets and uses one row per
    class so long class names no longer turn the debug header into a
    wrapped paragraph. Empty input returns [].
    """
    entries = list(metrics)
    if not entries:
        return []
    total = sum(int(m.get("tokens") or 0) for m in entries)
    by_class: dict[str, int] = {}
    for m in entries:
        cls = str(m.get("class") or "ephemeral")
        by_class[cls] = by_class.get(cls, 0) + int(m.get("tokens") or 0)
    # Sorted descending so the dominant class lands first.
    ordered = sorted(by_class.items(), key=lambda kv: -kv[1])
    out = [
        "  " + _kv(
            "Totals",
            _dim(f"{_format_token_count(total)} tok total"),
            C.CYAN,
        ),
    ]
    for cls, tok in ordered:
        if tok <= 0:
            continue
        out.append(
            "    "
            + _dim(f"{cls:<18} {_format_token_count(tok):>8}"),
        )
    return out


def _render_prompt_frame(
    part: PromptPart | None,
    body: str,
    metrics: dict[str, Any] | None = None,
) -> list[str]:
    """Render one framed seam inside :func:`render_incoming_prompt`.

    The heading carries the kind + name + source so a reader can tell
    at a glance which prompt part this section came from. When
    *metrics* is provided (the normal path under
    ``render_incoming_prompt``), tokens / cache / class are
    appended so the reader does not need to scroll back to the
    manifest. ``None`` is the fallback path (composition
    unavailable) — heading stays minimal.

    Body lines are prefixed with a thin vertical bar so the seam
    stays visible even when the body itself spans many lines.
    """
    out: list[str] = []
    if part is None:
        heading = "BODY"
    else:
        kind, show_name = _prompt_frame_label(part)
        version = f" v{part.version}" if part.version is not None else ""
        name = f": {part.name}" if show_name else ""
        heading = f"{kind}{name}{version} ({part.source})"
        if metrics is not None:
            tok = _format_token_count(int(metrics.get("tokens") or 0))
            cache = metrics.get("cache_scope") or "?"
            cls = metrics.get("class") or "?"
            source = metrics.get("token_source") or "?"
            heading += f" · {tok} tok · cache={cache} · class={cls} · source={source}"
        # Trace/evidence pointer: if the part carries an on-disk
        # artifact location, surface it as a separate heading segment.
        # The path lives here (frame metadata) and never in the body
        # below — the body is wire-identical to what the model saw, so
        # leaking the path into it would make the transcript misleading
        # *and* re-introduce the false affordance we removed from the
        # builder. Operators correlating a part with its on-disk
        # artifact read this segment.
        if getattr(part, "artifact_path", None):
            heading += f" · path={part.artifact_path}"
    out.append(_line(C.CYAN, f"  ┌─ {heading} " + ("─" * max(0, RULE_WIDTH - len(heading) - 4))))
    for raw_line in (body or "").splitlines() or [""]:
        out.append(f"  {paint('│', C.GREY)} {raw_line}")
    return out


def _prompt_frame_label(part: PromptPart) -> tuple[str, bool]:
    """Return ``(label, show_name)`` for a prompt frame heading.

    ``PromptPart.name`` is a stable machine id. For typed plan views it
    describes a view id (``typed_plan`` / ``execution_plan``), not a human
    domain label, so showing it in the body heading adds noise. Keep the ids
    in manifests and prompt_render metadata; make the frame label
    operator-friendly.
    """
    if part.kind == "system_tail":
        return _protected_block_label(part.name), True
    if part.kind == "plan_contract":
        return "PLAN CONTRACT", False
    if part.kind == "plan_tasks":
        return "PLAN TASKS", False
    return part.kind.upper().replace("_", "-"), True


def render_agent_command(
    *,
    agent: str,
    model: str,
    effort: str | None = None,
    cwd: str | None = None,
    command: str,
) -> str:
    """Render the agent metadata + command block.

    Replaces the duplicated ``-> claude …`` / ``--- claude … ---``
    sequence with one block that carries the same data:

        Command
          claude --print --model … --effort high
          cwd  /path/to/project
          agent claude   model claude-opus-4-7   effort high

    No information is dropped — model/effort/cwd remain visible — but
    the command line is no longer printed twice.
    """
    parts: list[str] = []
    parts.append(_line(C.CYAN + C.BOLD, "  Command"))
    parts.append(f"    {command}")
    meta_bits = [f"agent {agent}", f"model {model}"]
    if effort:
        meta_bits.append(f"effort {effort}")
    parts.append("    " + _dim("   ".join(meta_bits)))
    if cwd:
        parts.append("    " + _dim(f"cwd {cwd}"))
    return "\n".join(parts)


def render_transcript_open(label: str = "Transcript") -> str:
    """Open a raw-output transcript block. The agent stream prints
    between this and :func:`render_transcript_close`."""
    return (
        f"\n{_line(C.CYAN + C.BOLD, '  ' + label)}\n"
        f"{_line(C.GREY, '  ' + THIN_RULE)}"
    )


def render_transcript_close() -> str:
    return _line(C.GREY, "  " + THIN_RULE)


def render_result(
    exit_code: int,
    duration_s: float,
    *,
    extra: str | None = None,
) -> str:
    """Render the agent result line.

    Replaces the legacy ``[EXIT code=0 duration=14.9s]`` block with a
    one-line ``Result`` row that keeps the same fields — exit code and
    duration — and an optional ``extra`` slot for things like a
    short_summary chip.
    """
    color = C.GREEN if exit_code == 0 else C.RED
    bits = [
        f"  {paint('Result', color, C.BOLD)}",
        f"exit {exit_code}",
        f"{duration_s:.1f}s",
    ]
    if extra:
        bits.append(_dim(extra))
    return "  ".join(bits)


# ── Implement summary ──────────────────────────────────────────────────


def render_implement_summary(
    *,
    title: str = "Implementation",
    files_touched: Iterable[str] = (),
    progress: Mapping[str, int] | None = None,
    session_id: str | None = None,
    followup_parent_session_id: str | None = None,
    summary: str | None = None,
) -> str:
    """Render a human-readable outcome block for the IMPLEMENT phase.

    The agent's body output goes to ``output.log`` / ``meta.phases.implement``;
    this block exists so a reviewer scanning the terminal can answer
    "did anything happen, and what?" without opening the run dir.

    All sections are optional — when nothing concrete is known (dry-run,
    no git, no parsed summary) the block degrades to just the title line.
    """
    files_list = list(files_touched)

    parts: list[str] = [_line(C.CYAN + C.BOLD, f"  {title}")]

    if progress and "total" in progress:
        completed = int(progress.get("completed", 0))
        total = int(progress.get("total", 0))
        ok = completed >= total and total > 0
        color = C.GREEN if ok else C.YELLOW
        kind = str(progress.get("kind") or "planned_files")
        label = {
            "subtasks": "subtasks done",
            "planned_files": "planned files exist",
        }.get(kind, "items complete")
        parts.append(
            _kv("progress",
                paint(f"{completed}/{total} {label}", color),
                C.CYAN, indent=4),
        )

    if files_list:
        head, tail = files_list[:5], files_list[5:]
        parts.append(_kv(
            "files",
            paint(f"{len(files_list)} changed", C.WHITE), C.CYAN, indent=4,
        ))
        for f in head:
            parts.append(f"      - {f}")
        if tail:
            parts.append(f"      {_dim(f'… and {len(tail)} more')}")

    if summary:
        cropped = _crop(summary.strip().splitlines()[0] if summary else "", 200)
        if cropped:
            parts.append(_summary_kv(cropped, indent=4))

    if session_id:
        if followup_parent_session_id:
            # Follow-up runs: surface BOTH the new captured sid and the
            # parent sid that was passed to --resume. Lets a reviewer
            # confirm at-a-glance that the resume actually fired.
            value = _dim(
                f"{session_id[:8]}…  "
                f"(resumed from parent: {followup_parent_session_id[:8]}…)"
            )
        else:
            value = _dim(f"{session_id[:8]}…  (resumable)")
        parts.append(_kv("session", value, C.CYAN, indent=4))

    if len(parts) == 1:
        # Nothing concrete to report — note that the phase ran but stayed
        # silent (no git diff, no parsed plan files, no agent summary).
        parts.append(_dim("    (no observable file changes; see output.log)"))

    return "\n".join(parts)


# ── Parse failure ──────────────────────────────────────────────────────


def render_parse_failure(
    *,
    title: str,
    error: str,
    raw_output: str,
) -> str:
    """Render a parse-failure block.

    When a phase's typed contract parser rejects the agent's output, the
    operator must see the raw text and the schema error — they are how
    we explain *why* the run halted. Hiding raw under the
    JSON-suppression toggle would make a halt feel silent. This block
    is a deliberate counterbalance: bright red header, full untruncated
    raw body, the parse error inline.
    """
    parts: list[str] = [
        _line(C.RED + C.BOLD, f"  Parse failure — {title}"),
        _line(C.RED, f"    {error}"),
        "",
        _line(C.CYAN + C.BOLD, "  Raw output"),
        _line(C.GREY, "  " + THIN_RULE),
    ]
    body = raw_output or "(empty)"
    for line in body.splitlines() or [""]:
        parts.append(f"  {line}")
    parts.append(_line(C.GREY, "  " + THIN_RULE))
    return "\n".join(parts)


# ── Reviewer contract ──────────────────────────────────────────────────


def render_review_block(
    review: Mapping[str, Any],
    *,
    title: str | None = "Review",
) -> str:
    """Render a typed reviewer JSON contract structurally.

    ``review`` is the dict shape produced by
    :class:`pipeline.review_parser.ParsedReview` (or any equivalent
    mapping with ``verdict`` / ``short_summary`` / ``findings`` /
    ``risks`` / ``checks`` keys). Bodies are preserved in full —
    truncation is never applied here.

    Release-gate (``final_acceptance``) payloads carry an extended
    shape on top of the review schema: optional ``ship_ready`` flag,
    ``release_blockers`` (richer finding shape with
    ``why_blocks_release``), ``verification_gaps``, and
    ``contract_status``. When those keys are present the renderer
    surfaces them so CLI output mirrors what
    :func:`pipeline.release_markdown.render_release_markdown` already
    writes into evidence — there is no separate release renderer for
    the terminal. When ``release_blockers`` is present it supersedes
    the review-shape ``findings`` mirror to avoid double-printing.
    """
    verdict = str(review.get("verdict") or "")
    summary = str(review.get("short_summary") or "")
    findings = list(review.get("findings") or ())
    risks = list(review.get("risks") or ())
    checks = list(review.get("checks") or ())
    parse_error = review.get("parse_error")
    # Release-gate extras (final_acceptance). All optional.
    ship_ready = review.get("ship_ready")
    release_blockers = list(review.get("release_blockers") or ())
    verification_gaps = list(review.get("verification_gaps") or ())
    contract_status = review.get("contract_status")

    parts: list[str] = []
    if title:
        parts.append(_line(C.CYAN + C.BOLD, f"  {title}"))
    parts.append(_kv("verdict", _color_verdict(verdict), C.CYAN, indent=4))
    if ship_ready is not None:
        parts.append(_kv(
            "ship_ready", _color_ship_ready(bool(ship_ready)),
            C.CYAN, indent=4,
        ))
    if summary:
        parts.append(_summary_kv(summary, indent=4))
    if parse_error:
        parts.append(_kv("parse_error", str(parse_error), C.RED, indent=4))

    # Release blockers carry why_blocks_release on top of the review
    # finding shape, so prefer them when the release payload supplied
    # both surfaces — otherwise the why_blocks_release line is lost.
    if release_blockers:
        parts.append("")
        parts.append(_line(C.CYAN + C.BOLD, "  Release blockers"))
        for blocker in release_blockers:
            parts.extend(_render_blocker_lines(blocker))
    elif findings:
        parts.append("")
        parts.append(_line(C.CYAN + C.BOLD, "  Findings"))
        for finding in findings:
            parts.extend(_render_finding_lines(finding))

    if verification_gaps:
        parts.append("")
        parts.append(_line(C.CYAN + C.BOLD, "  Verification gaps"))
        for gap in verification_gaps:
            parts.extend(_render_verification_gap_lines(gap))

    if risks:
        parts.append("")
        parts.append(_line(C.CYAN + C.BOLD, "  Risks"))
        for risk in risks:
            parts.append(f"    - {risk}")

    if checks:
        parts.append("")
        parts.append(_line(C.CYAN + C.BOLD, "  Checks"))
        for check in checks:
            parts.append(f"    - {check}")

    if contract_status:
        parts.append("")
        parts.append(_line(C.CYAN + C.BOLD, "  Contract status"))
        parts.extend(_render_contract_status_lines(contract_status))

    return "\n".join(parts)


def _render_finding_lines(finding: Mapping[str, Any]) -> list[str]:
    fid = str(finding.get("id") or "")
    severity = str(finding.get("severity") or "")
    title = str(finding.get("title") or "")
    body = str(finding.get("body") or "")
    required_fix = str(finding.get("required_fix") or "")
    file = finding.get("file")
    line = finding.get("line")

    sev_chip = _severity_chip(severity)
    head = f"    {paint(fid, C.BOLD)}  {sev_chip}  {title}"
    out = [head]
    if file:
        location = f"{file}:{line}" if line is not None else str(file)
        out.append(f"      {_dim('file ' + location)}")
    if body:
        out.append("")
        for body_line in body.splitlines() or [""]:
            out.append(f"      {body_line}")
    if required_fix:
        out.append("")
        out.append(f"      {paint('fix', C.BOLD)}  {required_fix}")
    return out


def _render_blocker_lines(blocker: Mapping[str, Any]) -> list[str]:
    """Render a release blocker. Same shape as a review finding plus
    the release-tier ``why_blocks_release`` line.
    """
    out = _render_finding_lines(blocker)
    why = str(blocker.get("why_blocks_release") or "")
    if why:
        out.append("")
        out.append(f"      {paint('why blocks release', C.BOLD)}  {why}")
    return out


def _render_verification_gap_lines(gap: Mapping[str, Any]) -> list[str]:
    risk = str(gap.get("risk") or "")
    missing = str(gap.get("missing_evidence") or "")
    required = str(gap.get("required_check") or "")
    out: list[str] = []
    if risk:
        out.append(f"    - {paint('risk', C.BOLD)}  {risk}")
    if missing:
        out.append(f"      {paint('missing', C.BOLD)}  {missing}")
    if required:
        out.append(f"      {paint('required', C.BOLD)}  {required}")
    return out


def _render_contract_status_lines(status: Mapping[str, Any]) -> list[str]:
    return [
        _kv("task_contract",
            _color_contract_value(str(status.get("task_contract") or "")),
            C.CYAN, indent=4),
        _kv("interfaces",
            _color_contract_value(str(status.get("interfaces") or "")),
            C.CYAN, indent=4),
        _kv("persistence",
            _color_contract_value(str(status.get("persistence") or "")),
            C.CYAN, indent=4),
        _kv("tests",
            _color_contract_value(str(status.get("tests") or "")),
            C.CYAN, indent=4),
    ]


# ── Plan contract ──────────────────────────────────────────────────────


def render_plan_block(plan: Mapping[str, Any], *, title: str = "Plan") -> str:
    """Render a parsed plan structurally.

    Reads keys produced by :class:`pipeline.plan_parser.ParsedPlan` /
    :func:`core.contracts.plan_schema`: ``short_summary``,
    ``planning_context``, ``goal``, ``acceptance_criteria``,
    ``owned_files``, ``commands_to_run``, ``risks``, ``review_focus``,
    ``mcp_context``, ``tasks``. Bodies are preserved in full — the
    block carries every contract field, never just counters.
    """
    summary = str(plan.get("short_summary") or plan.get("plan_summary") or "")
    planning_context = str(plan.get("planning_context") or "")
    goal = str(plan.get("goal") or "")
    acceptance = list(plan.get("acceptance_criteria") or ())
    owned_files = list(plan.get("owned_files") or ())
    commands = list(plan.get("commands_to_run") or ())
    risks = list(plan.get("risks") or ())
    review_focus = list(plan.get("review_focus") or ())
    mcp_context = list(plan.get("mcp_context") or ())
    tasks = list(plan.get("tasks") or ())

    parts: list[str] = [_line(C.CYAN + C.BOLD, f"  {title}")]
    if summary:
        parts.append(_summary_kv(summary, indent=4))
    if goal:
        parts.append(_kv("goal", goal, C.CYAN, indent=4))

    if planning_context:
        parts.append("")
        parts.append(_line(C.CYAN + C.BOLD, "  Planning Context"))
        parts.append(_line(C.GREY, "  " + THIN_RULE))
        for ctx_line in planning_context.splitlines() or [""]:
            parts.append(f"  {ctx_line}")
        parts.append(_line(C.GREY, "  " + THIN_RULE))

    # Counter contract row — small overview before the full lists below.
    contract_rows: list[tuple[str, str]] = []
    if acceptance:
        contract_rows.append(("acceptance", str(len(acceptance))))
    if owned_files:
        contract_rows.append(("owned files", str(len(owned_files))))
    if commands:
        contract_rows.append(("commands", str(len(commands))))
    if risks:
        contract_rows.append(("risks", str(len(risks))))
    if review_focus:
        contract_rows.append(("review focus", str(len(review_focus))))
    if mcp_context:
        contract_rows.append(("mcp_context", str(len(mcp_context))))
    if tasks:
        contract_rows.append(("tasks", str(len(tasks))))
    if contract_rows:
        parts.append("")
        parts.append(_line(C.CYAN + C.BOLD, "  Contract"))
        for label, value in contract_rows:
            parts.append(_kv(label, value, C.CYAN, indent=4))

    parts.extend(_render_bullet_section("Acceptance Criteria", acceptance))
    parts.extend(_render_bullet_section("Owned Files", owned_files))
    parts.extend(_render_bullet_section("Commands", commands))
    parts.extend(_render_bullet_section("Risks", risks))
    parts.extend(_render_bullet_section("Review Focus", review_focus))
    if mcp_context:
        parts.append("")
        parts.append(_line(C.CYAN + C.BOLD, "  MCP Context"))
        for entry in mcp_context:
            parts.append(f"    - {entry}")

    if tasks:
        parts.append("")
        parts.append(_line(C.CYAN + C.BOLD, "  Tasks"))
        for task in tasks:
            parts.extend(_render_task_lines(task))

    return "\n".join(parts)


def _render_bullet_section(title: str, items: list[Any]) -> list[str]:
    if not items:
        return []
    out = ["", _line(C.CYAN + C.BOLD, f"  {title}")]
    for entry in items:
        text = str(entry)
        first, *rest = text.splitlines() or [""]
        out.append(f"    - {first}")
        for line in rest:
            out.append(f"      {line}")
    return out


def _render_task_lines(task: Mapping[str, Any]) -> list[str]:
    tid = str(task.get("id") or "")
    goal = str(task.get("goal") or "")
    files = list(task.get("files") or ())
    deps = list(task.get("depends_on") or ())
    skill = task.get("skill")
    model = task.get("model")
    spec = str(task.get("spec") or "")
    done = list(task.get("done_criteria") or ())

    out = ["", f"    {paint(tid, C.BOLD)}  {goal}"]
    if files:
        out.append(f"      {_dim('files ' + ', '.join(files))}")
    if deps:
        out.append(f"      {_dim('depends_on ' + ', '.join(deps))}")
    extras: list[str] = []
    if skill:
        extras.append(f"skill {skill}")
    if model:
        extras.append(f"model {model}")
    if extras:
        out.append(f"      {_dim('   '.join(extras))}")
    if spec:
        out.append(f"      {paint('spec', C.CYAN)}")
        for line in spec.splitlines() or [""]:
            out.append(f"        {line}")
    if done:
        out.append(f"      {paint('done', C.CYAN)}")
        for crit in done:
            out.append(f"        - {crit}")
    return out


# ── Cross-plan contract ───────────────────────────────────────────────


def render_cross_plan_block(
    parsed: Mapping[str, Any],
    *,
    title: str = "Cross-project plan",
) -> str:
    """Render a parsed cross-project plan structurally.

    Reads the mapping shape produced by
    :meth:`pipeline.cross_project.plan_parser.ParsedCrossPlan.to_render_dict`:
    ``interface_contract`` (str), ``implementation_order`` (str),
    ``subtasks`` (sequence of ``[alias, body | None]``),
    ``aliases_missing`` (sequence of alias strings — informational; the
    per-alias ``None`` body in ``subtasks`` is the source of truth for
    rendering).

    Section bodies are preserved verbatim — tables, bullets, and prose
    inside ``interface_contract`` and ``implementation_order`` are not
    re-parsed. The renderer's job is framing, not transformation.
    """
    interface_contract = str(parsed.get("interface_contract") or "")
    implementation_order = str(parsed.get("implementation_order") or "")
    subtasks = list(parsed.get("subtasks") or ())
    aliases_missing = list(parsed.get("aliases_missing") or ())

    sections_present = sum(
        1 for body in (interface_contract, implementation_order) if body
    ) + (1 if subtasks else 0)
    resolved = sum(1 for _, body in subtasks if body is not None)
    missing = len(aliases_missing)

    parts: list[str] = [_line(C.CYAN + C.BOLD, f"  {title}")]
    overview_rows: list[tuple[str, str]] = [
        ("sections", str(sections_present)),
    ]
    if subtasks:
        if missing:
            overview_rows.append(
                ("subtasks", f"{resolved} resolved · {missing} missing"),
            )
        else:
            overview_rows.append(("subtasks", str(resolved)))
    for label, value in overview_rows:
        parts.append(_kv(label, value, C.CYAN, indent=4))

    parts.extend(_render_framed_section("Interface Contract", interface_contract))
    parts.extend(_render_subtasks_section(subtasks))
    parts.extend(
        _render_framed_section("Implementation Order", implementation_order)
    )

    return "\n".join(parts)


def _render_framed_section(title: str, body: str) -> list[str]:
    """Render a section title + THIN_RULE-framed body.

    Mirrors the "Planning Context" block in :func:`render_plan_block`,
    so cross-plan sections and single-plan context look identical at a
    glance. Empty bodies render as a dimmed ``(empty)`` placeholder
    instead of an empty frame.
    """
    out = ["", _line(C.CYAN + C.BOLD, f"  {title}")]
    body = body.rstrip()
    if not body:
        out.append(f"  {_dim('(empty)')}")
        return out
    out.append(_line(C.GREY, "  " + THIN_RULE))
    for line in body.splitlines():
        out.append(f"  {line}")
    out.append(_line(C.GREY, "  " + THIN_RULE))
    return out


def _render_subtasks_section(subtasks: list[Any]) -> list[str]:
    """Render the ``Per-Project Subtasks`` block.

    Each row is ``[alias]<pad>body`` with the alias column padded to
    align bodies vertically. Multi-line bodies indent continuation
    lines under the body column. A missing block renders as a dimmed
    ``(missing — fell through to full task)`` hint so operators can see
    at a glance which aliases the parser failed to locate.
    """
    if not subtasks:
        return []

    aliases = [str(alias) for alias, _ in subtasks]
    alias_col = max(len(a) for a in aliases) + 2  # brackets
    body_indent = 4 + alias_col + 2

    out = ["", _line(C.CYAN + C.BOLD, "  Per-Project Subtasks")]
    for alias, body in subtasks:
        chip = f"[{alias}]"
        chip_padded = _pad(chip, alias_col)
        if body is None:
            out.append(
                f"    {paint(chip_padded, C.CYAN)}"
                f"{_dim('(missing — fell through to full task)')}"
            )
            continue
        body_stripped = body.strip()
        if not body_stripped:
            out.append(
                f"    {paint(chip_padded, C.CYAN)}{_dim('(empty)')}"
            )
            continue
        body_lines = body_stripped.splitlines()
        out.append(
            f"    {paint(chip_padded, C.CYAN)}{body_lines[0]}"
        )
        for cont in body_lines[1:]:
            out.append(f"{' ' * body_indent}{cont}")
    return out


# ── Helpers ────────────────────────────────────────────────────────────


def _kv(key: str, value: str, color: str, *, indent: int = 0) -> str:
    return f"{' ' * indent}{paint(key, color)}  {value}"


def _summary_kv(value: str, *, indent: int = 0) -> str:
    return _kv("summary", paint(value, C.BOLD), C.CYAN, indent=indent)


def _dim(text: str) -> str:
    return paint(text, C.GREY) if text else ""


def _crop(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _pad(text: str, width: int) -> str:
    text = text or ""
    if len(text) >= width:
        return text + "  "
    return text + " " * (width - len(text))


def _color_verdict(verdict: str) -> str:
    if verdict == "APPROVED":
        return paint("APPROVED", C.GREEN, C.BOLD)
    if verdict == "REJECTED":
        return paint("REJECTED", C.RED, C.BOLD)
    return verdict


def _color_ship_ready(value: bool) -> str:
    """Bool ship_ready chip — bold green ``yes`` / bold red ``no``."""
    if value:
        return paint("yes", C.GREEN, C.BOLD)
    return paint("no", C.RED, C.BOLD)


# release_json contract_status enum values, partitioned by ship-impact.
# Kept here next to ``_color_verdict`` so the release surface uses one
# consistent palette and we never grow drifted lookups.
_CONTRACT_OK = frozenset({"satisfied", "compatible", "safe", "sufficient"})
_CONTRACT_BAD = frozenset({"incomplete", "broken", "missing"})
_CONTRACT_WARN = frozenset({"unclear", "risky", "weak"})


def _color_contract_value(value: str) -> str:
    """Colorize a ``contract_status`` enum value by ship impact.

    Green for shippable, bold red for blockers, yellow for soft
    warnings, dim for ``not_applicable``. Unknown values pass through
    uncolored so a future enum extension does not silently look
    "neutral green".
    """
    if value in _CONTRACT_OK:
        return paint(value, C.GREEN)
    if value in _CONTRACT_BAD:
        return paint(value, C.RED, C.BOLD)
    if value in _CONTRACT_WARN:
        return paint(value, C.YELLOW)
    if value == "not_applicable":
        return _dim(value)
    return value


def _status_chip(status: str) -> str:
    palette = {
        "running":  C.YELLOW,
        "ok":       C.GREEN,
        "approved": C.GREEN,
        "passed":   C.GREEN,
        "rejected": C.RED,
        "halted":   C.RED,
        "failed":   C.RED,
        "skipped":  C.GREY,
    }
    color = palette.get(status.lower(), C.CYAN)
    return paint(status, color)


def _severity_chip(severity: str) -> str:
    palette = {
        "P0": C.RED,
        "P1": C.RED,
        "P2": C.YELLOW,
        "P3": C.GREY,
    }
    color = palette.get(severity, C.CYAN)
    return paint(severity, color, C.BOLD)
