"""core/io/pipeline_block.py — pipeline progress block renderer.

Static ASCII visualization of the profile's phase chain rendered once
under the run header. Highlights what was completed, where the run
currently is, and what remains. Used by both the single-project
(:mod:`pipeline.project.app`) and cross-project
(:mod:`pipeline.cross_project.app`) entrypoints so a fresh run and
``--resume`` of a checkpointed run share one visual.

Design rules:

* **Static rendering only.** This module produces a single string. No
  curses, no in-place updates, no Rich Live. The caller prints it once
  next to the run header.
* **No transcript dependency.** Imports ANSI palette from
  :mod:`core.io.ansi` so the transcript surface stays a sibling, not a
  prerequisite.
* **Profile shape is the source of truth.** Phases come straight from
  ``Profile.steps``; ``LoopStep`` is rendered as a visual loop group
  (``(REVIEW ⟳ REPAIR)``) rather than flattened into a linear list, so
  reviewers can see the retry topology at a glance.
"""
from __future__ import annotations

import shutil
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from core.io.ansi import C, paint, strip_ansi

if TYPE_CHECKING:
    from pipeline.runtime.profile import LoopStep, Profile


# Symbols carry the state at a glance even without color (tmux logs,
# CI capture, redirected stdout).
_GLYPH_DONE = "✓"
_GLYPH_CURRENT = "▶"
_GLYPH_PENDING = "·"
_ARROW = "→"
_LOOP = "⟳"

# Unicode superscript digits 2-9. The loop chip uses superscripts when
# ``max_rounds`` lands in this range so the chip stays at two glyphs
# (``⟳²``). For one-round "loops" we omit the chip entirely (a single
# attempt is not really a retry budget), and for ten-plus rounds we
# fall back to ``⟳×N`` since ``¹⁰`` looks like 1+0 at terminal sizes.
_SUPERSCRIPT_DIGITS = {2: "²", 3: "³", 4: "⁴", 5: "⁵", 6: "⁶", 7: "⁷", 8: "⁸", 9: "⁹"}


def _loop_rounds_chip(max_rounds: int) -> str:
    """Render the retry-budget chip rendered next to ⟳.

    Empty string for ``max_rounds <= 1`` (no retry budget worth
    announcing), Unicode superscript for 2-9, ``×N`` for 10+.
    """
    if max_rounds <= 1:
        return ""
    if max_rounds in _SUPERSCRIPT_DIGITS:
        return _SUPERSCRIPT_DIGITS[max_rounds]
    return f"×{max_rounds}"


def render_pipeline_block(
    profile: Profile,
    *,
    completed: Sequence[str] = (),
    current: str | None = None,
    phase_runtimes: Mapping[str, str] | None = None,
    color: bool | None = None,
) -> str:
    """Render the pipeline chain block.

    ``profile`` supplies the ordered ``steps`` (``PhaseStep`` or
    ``LoopStep``). ``completed`` is the list of phase names already
    finished — typically ``PipelineState.completed`` on resume, or empty
    on a fresh run. ``current`` is the phase the run is about to enter;
    when ``None`` and ``completed`` is empty, the first phase in the
    profile is treated as current.

    ``phase_runtimes`` is an optional ``{phase_name: runtime_id}``
    mapping (e.g. ``{"plan": "claude", "validate_plan": "codex"}``).
    Each entry surfaces as a dim ``[Claude]`` / ``[Codex]`` chip next
    to its phase so an operator can see at a glance which agent
    backend owns each step. Phases missing from the map render bare.

    Returned string carries no trailing newline so callers can decide
    framing. ``color`` follows :func:`core.io.ansi.paint`: ``None`` uses
    the shared auto-detection/override policy, while explicit booleans
    force colored or plain output.
    """
    completed_set = set(completed)
    resolved_current = current or _first_pending(profile, completed_set)
    body = _render_chain(
        profile.steps,
        completed_set=completed_set,
        current=resolved_current,
        runtimes=phase_runtimes or {},
        color=color,
    )
    return f"{paint('Pipeline', C.CYAN, C.BOLD, color=color)}\n  {body}"


def render_pipeline_sections(
    sections: Sequence[tuple[str, Sequence]],
    *,
    phase_runtimes: Mapping[str, str] | None = None,
    completed: Sequence[str] = (),
    current: str | None = None,
    color: bool | None = None,
) -> str:
    """Render several labelled step-chains under one ``Pipeline`` header.

    ``sections`` is an ordered sequence of ``(label, steps)`` pairs where
    ``steps`` is a list of ``PhaseStep`` / ``LoopStep`` (same shape
    :func:`render_pipeline_block` accepts via ``profile.steps``). Each
    non-empty section renders as a dim label line followed by its
    indented chain::

        Pipeline
          Global
            ▶ plan → · validate_plan
          Per project (×2)
            · implement → ⟳² (· review_changes → · repair_changes) → …

    The cross-project entrypoint uses this to show the full projected
    shape — the cross-level chain, the per-project sub-pipeline, and the
    terminal cross gates — in one block. Per-project sub-pipelines run
    under ``PresentationPolicy.SILENT`` during a cross run and never
    print their own header, so without this the operator would only ever
    see the global chain and the rest of the pipeline would look lost.

    ``phase_runtimes`` is a single merged ``{phase: runtime}`` map across
    all sections — phase names do not collide between the cross, project,
    and gate sections, so one map drives every ``[Claude]`` / ``[Codex]``
    chip. The ▶ current marker lands on the first phase of the first
    non-empty section (unless ``current`` / ``completed`` override it);
    every other phase renders pending.
    """
    completed_set = set(completed)
    runtimes = phase_runtimes or {}
    resolved_current = current
    if resolved_current is None and not completed_set:
        for _label, steps in sections:
            name = _first_phase_in(steps)
            if name is not None:
                resolved_current = name
                break

    lines = [paint("Pipeline", C.CYAN, C.BOLD, color=color)]
    for label, steps in sections:
        if not steps:
            continue
        chain = _render_chain(
            steps,
            completed_set=completed_set,
            current=resolved_current,
            runtimes=runtimes,
            color=color,
        )
        lines.append(f"  {paint(label, C.GREY, C.BOLD, color=color)}")
        lines.extend(f"    {ln}" for ln in chain.splitlines())
    return "\n".join(lines)


def _render_chain(
    steps: Sequence,
    *,
    completed_set: set[str],
    current: str | None,
    runtimes: Mapping[str, str],
    color: bool | None,
) -> str:
    """Render one ordered step list into a ``→``-joined chain string.

    Wraps to multiple lines when the chain exceeds the terminal width.
    Shared by :func:`render_pipeline_block` (single chain) and
    :func:`render_pipeline_sections` (one chain per labelled section).
    """
    # Local import — avoids a runtime/io cycle at module import time.
    from pipeline.runtime.profile import LoopStep
    from pipeline.runtime.steps import PhaseStep

    tokens: list[str] = []
    for entry in steps:
        if isinstance(entry, PhaseStep):
            tokens.append(_phase_token(
                entry.phase,
                completed_set,
                current,
                runtime=runtimes.get(entry.phase),
                color=color,
            ))
        elif isinstance(entry, LoopStep):
            tokens.append(_loop_token(
                entry,
                completed_set,
                current,
                runtimes=runtimes,
                color=color,
            ))
        # Unknown step types are silently skipped — Profile.__post_init__
        # already enforces the union, so this branch is unreachable in
        # practice; keeping it defensive keeps the renderer total.

    sep = f" {paint(_ARROW, C.GREY, color=color)} "
    one_line = sep.join(tokens)
    width = _terminal_width()
    return (
        one_line
        if _visible_len(one_line) <= width
        else _wrap_chain(tokens, sep, width, color=color)
    )


def _first_phase_in(steps: Sequence) -> str | None:
    """First phase name in ``steps`` (descending into a leading loop)."""
    from pipeline.runtime.profile import LoopStep
    from pipeline.runtime.steps import PhaseStep

    for entry in steps:
        if isinstance(entry, PhaseStep):
            return entry.phase
        if isinstance(entry, LoopStep) and entry.steps:
            return entry.steps[0].phase
    return None


def _first_pending(profile: Profile, completed: set[str]) -> str | None:
    """First phase name in profile order that is not yet in ``completed``."""
    from pipeline.runtime.profile import LoopStep
    from pipeline.runtime.steps import PhaseStep

    for entry in profile.steps:
        if isinstance(entry, PhaseStep):
            if entry.phase not in completed:
                return entry.phase
        elif isinstance(entry, LoopStep):
            for inner in entry.steps:
                if inner.phase not in completed:
                    return inner.phase
    return None


def _runtime_chip(runtime: str | None, *, color: bool | None) -> str:
    """Render the ``[Claude]`` / ``[Codex]`` chip rendered next to a phase.

    Empty when no runtime is known. The label uses ``str.capitalize()``
    so the chip reads naturally regardless of how the runtime id is
    cased internally (``"claude"`` → ``"Claude"``, ``"CODEX"`` →
    ``"Codex"``). Color is dim cyan so the chip reads as metadata next
    to the state-coloured phase token.
    """
    if not runtime:
        return ""
    label = runtime.capitalize()
    return " " + paint(f"[{label}]", C.CYAN, C.DIM, color=color)


def _phase_token(
    name: str,
    completed: set[str],
    current: str | None,
    *,
    runtime: str | None = None,
    color: bool | None,
) -> str:
    chip = _runtime_chip(runtime, color=color)
    if name in completed:
        return paint(f"{_GLYPH_DONE} {name}", C.GREEN, color=color) + chip
    if name == current:
        return paint(f"{_GLYPH_CURRENT} {name}", C.YELLOW, C.BOLD, color=color) + chip
    # Pending: only the glyph is dimmed (· = "not yet reached"); the name
    # stays in the default foreground so the pipeline topology reads clearly
    # at a glance. Colour is a progress signal, not a saliency signal.
    return f"{paint(_GLYPH_PENDING, C.GREY, color=color)} {name}{chip}"


def _loop_token(
    loop: LoopStep,
    completed: set[str],
    current: str | None,
    *,
    runtimes: Mapping[str, str],
    color: bool | None,
) -> str:
    """Render a ``LoopStep`` as ``⟳ⁿ (step → step)``.

    The loop glyph is hoisted out as a prefix marker so the parens
    visibly group one round's worth of steps. ``max_rounds`` becomes
    a small chip (``⟳²`` / ``⟳×12``) so an operator can tell at a
    glance how many retries the profile budgeted for this block.
    """
    inner_sep = f" {paint(_ARROW, C.GREY, color=color)} "
    inner = inner_sep.join(
        _phase_token(
            s.phase, completed, current,
            runtime=runtimes.get(s.phase),
            color=color,
        )
        for s in loop.steps
    )
    chip = _loop_rounds_chip(loop.max_rounds)
    prefix = f"{paint(f'{_LOOP}{chip}', C.GREY, color=color)} "
    return (
        f"{prefix}{paint('(', C.GREY, color=color)}"
        f"{inner}"
        f"{paint(')', C.GREY, color=color)}"
    )


def _terminal_width() -> int:
    try:
        return max(shutil.get_terminal_size((80, 24)).columns, 40)
    except OSError:
        return 80


def _visible_len(text: str) -> int:
    return len(strip_ansi(text))


def _wrap_chain(
    tokens: list[str],
    sep: str,
    width: int,
    *,
    color: bool | None,
) -> str:
    """Greedy line-wrap of the chain across ``width`` columns.

    Continuation lines indent two spaces and start with the arrow so
    the chain visually keeps flowing across the break.
    """
    indent = "  "
    cont_indent = "    "
    lines: list[str] = []
    current_line = ""
    for i, tok in enumerate(tokens):
        candidate = tok if not current_line else current_line + sep + tok
        if _visible_len(candidate) + len(indent) <= width:
            current_line = candidate
            continue
        if current_line:
            lines.append(current_line)
            current_line = tok
        else:
            # A single token longer than the width — emit anyway.
            lines.append(tok)
            current_line = ""
        # Mark continuation by prefixing the new line with a dim arrow.
        if i < len(tokens) and current_line:
            current_line = f"{paint(_ARROW, C.GREY, color=color)} {current_line}"
    if current_line:
        lines.append(current_line)
    if not lines:
        return ""
    head, *tail = lines
    return ("\n" + cont_indent).join([head, *tail])


__all__ = ["render_pipeline_block", "render_pipeline_sections"]
