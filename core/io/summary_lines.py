"""core/io/summary_lines.py — the summary-mode line presenter.

Single home for the grammar of ``--output summary``: one compact,
append-only line per run event (phase start/end, plan contract, gate
verdicts, subtask receipts, auto-fix, gates rollup, handoff cards,
resume, delivery). Every formatter is a **pure, side-effect-free**
function that returns a ``str`` — nothing here prints, opens files, or
touches process state, so the module imports clean and callers stay in
control of when and where a line is emitted.

Design rules mirror :mod:`core.io.pipeline_block`:

* **Colour only through :func:`core.io.ansi.paint`.** No raw CSI escape
  literals and no inline palette-constant f-string interpolation.
  ``color=None`` follows the shared auto-detect/override policy;
  explicit booleans force the outcome. Stderr-bound lines pass
  ``stream=sys.stderr`` so auto-detect consults the right descriptor
  (see :func:`core.io.ansi.paint`).
* **One glyph vocabulary.** Every line is prefixed by exactly one glyph
  from the fixed set ``▶ ✓ ✗ ⚠ ┌ ═ ↺ ·``. The glyph carries state at a
  glance even with colour stripped (tmux logs, CI capture, pipes).
* **Truncation is for free text only.** :func:`_truncate_tail` clips a
  free-text tail (a goal, a summary, a headline, a reason) to a visible
  column budget with a trailing ``…``. Structured tokens — ids, enum
  verdicts, phase names, gate names, handoff ids, paths — are never
  truncated, so they always read in full.

This module is intentionally not wired to any caller here; the phase and
run surfaces adopt it in later steps. Keeping the grammar in one place
stops summary f-strings from scattering across the orchestration body.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import TextIO

from core.io.ansi import C, paint, strip_ansi

# ── glyph vocabulary ─────────────────────────────────────────────────────
# The fixed prefix set. Each line begins with exactly one of these; the
# glyph is the colour-independent state signal.
GLYPH_START = "▶"      # a phase / subtask begins
GLYPH_OK = "✓"         # success: phase done, subtask met, verdict approved
GLYPH_FAIL = "✗"       # failure: phase failed, verdict rejected
GLYPH_WARN = "⚠"       # attention: incomplete subtask, auto-fix applied
GLYPH_CONTRACT = "┌"   # opens a contract / handoff card
GLYPH_RULE = "═"       # a gate-rollup bar
GLYPH_RETRY = "↺"      # a replan / retry / resume
SEP = "·"              # field separator

# Free-text tails clip at this many visible columns before the ``…``.
_DEFAULT_COLS = 100


def _truncate_tail(text: str, cols: int = _DEFAULT_COLS) -> str:
    """Clip a free-text tail to ``cols`` visible columns, adding ``…``.

    A free-text tail is plain content in the summary grammar: the presenter
    colours only glyphs and enum tokens, never the tail. Any ANSI escape in
    an incoming tail is therefore incidental (raw agent/contract text), so it
    is stripped up front via :func:`core.io.ansi.strip_ansi`. Stripping first
    means the clip measures true visible width AND can never leave a dangling
    SGR code that would colour-leak into the following separators or lines —
    a truncated coloured tail cannot lose its trailing reset because no
    escape survives at all. Internal runs of whitespace — including newlines —
    collapse to single spaces so a multi-line value can never break the
    single-line append grammar.

    The result's visible width is at most ``cols``: when clipping is needed
    the ``…`` occupies the final column (``text[:cols - 1] + '…'``). A tail
    already within budget is returned untouched. This helper is applied ONLY
    to free text — never to ids, enum verdicts, phase or gate names, handoff
    ids, or paths, which must always read in full.
    """
    collapsed = " ".join(strip_ansi(text).split())
    if len(collapsed) <= cols:
        return collapsed
    return collapsed[: cols - 1] + "…"


def _join(parts: Sequence[str], *, color: bool | None, stream: TextIO | None) -> str:
    """Join non-empty ``parts`` with a dim ``·`` separator."""
    sep = f" {paint(SEP, C.GREY, color=color, stream=stream)} "
    return sep.join(p for p in parts if p)


def _glyph(symbol: str, *codes: str, color: bool | None, stream: TextIO | None) -> str:
    """Paint a prefix glyph in the shared palette."""
    return paint(symbol, *codes, color=color, stream=stream)


# ── phase lifecycle ──────────────────────────────────────────────────────


def phase_start(
    phase: str,
    *,
    color: bool | None = None,
    stream: TextIO | None = None,
) -> str:
    """``▶ {phase}`` — a phase is entering."""
    glyph = _glyph(GLYPH_START, C.YELLOW, C.BOLD, color=color, stream=stream)
    return f"{glyph} {phase}"


def phase_end(
    phase: str,
    ok: bool,
    detail: str | None = None,
    *,
    color: bool | None = None,
    stream: TextIO | None = None,
) -> str:
    """``✓ {phase}`` / ``✗ {phase}`` with an optional free-text ``detail``."""
    symbol, code = (GLYPH_OK, C.GREEN) if ok else (GLYPH_FAIL, C.RED)
    glyph = _glyph(symbol, code, color=color, stream=stream)
    head = f"{glyph} {phase}"
    tail = _truncate_tail(detail) if detail else ""
    return _join([head, tail], color=color, stream=stream)


def plan_contract(
    tasks_ids: Sequence[str],
    acceptance_n: int,
    risks_n: int,
    *,
    color: bool | None = None,
    stream: TextIO | None = None,
) -> str:
    """``✓ plan · contract: 2 tasks (T1, T2) · acceptance 3 · risks 2``.

    The phase-END line that closes the ``▶ plan`` start, so ``plan`` reads
    as a completed ``▶``/``✓`` pair. ``tasks_ids`` render as a full
    comma-joined id list (ids are never truncated) inside the parenthesised
    tail; the leading count and the acceptance/risk counts are structured
    integers. When there are no task ids the ``(…)`` tail is omitted.
    """
    glyph = _glyph(GLYPH_OK, C.GREEN, color=color, stream=stream)
    ids = ", ".join(tasks_ids)
    contract = f"contract: {len(tasks_ids)} tasks"
    if ids:
        contract += f" ({ids})"
    return _join(
        [f"{glyph} plan", contract, f"acceptance {acceptance_n}", f"risks {risks_n}"],
        color=color,
        stream=stream,
    )


# ── verdicts (validate_plan / review_changes / final_acceptance) ─────────

# Enum verdicts that read as success; everything else that is not an
# explicit failure verdict falls back to the ⚠ attention glyph.
_APPROVE_VERDICTS = {"APPROVED", "PASS", "PASSED", "CLEAN", "ACCEPTED"}
_REJECT_VERDICTS = {"REJECTED", "FAIL", "FAILED", "BLOCKED"}


def verdict_line(
    phase: str,
    verdict_enum: str,
    headline: str | None = None,
    round: int | None = None,  # noqa: A002 — grammar term; not shadowing a used builtin
    *,
    color: bool | None = None,
    stream: TextIO | None = None,
) -> str:
    """``✓ validate_plan · APPROVED`` / ``✗ review_changes · REJECTED · F1 …``.

    The glyph and enum colour follow the verdict: approve verdicts read
    green ``✓``, reject verdicts red ``✗``, anything else the neutral
    ``⚠``. ``verdict_enum`` and ``phase`` are structured tokens and read
    in full; ``round`` (when given) renders as ``R{n}``; only ``headline``
    is treated as a free-text tail and clipped.
    """
    upper = verdict_enum.upper()
    if upper in _APPROVE_VERDICTS:
        symbol, code = GLYPH_OK, C.GREEN
    elif upper in _REJECT_VERDICTS:
        symbol, code = GLYPH_FAIL, C.RED
    else:
        symbol, code = GLYPH_WARN, C.YELLOW
    glyph = _glyph(symbol, code, color=color, stream=stream)
    enum_tok = paint(verdict_enum, code, color=color, stream=stream)
    parts = [f"{glyph} {phase}", enum_tok]
    if round is not None:
        parts.append(f"R{round}")
    if headline:
        parts.append(_truncate_tail(headline))
    return _join(parts, color=color, stream=stream)


# ── subtasks ─────────────────────────────────────────────────────────────


def subtask_start(
    id: str,  # noqa: A002 — subtask identifier; not shadowing a used builtin
    goal: str,
    *,
    color: bool | None = None,
    stream: TextIO | None = None,
) -> str:
    """``▶ {id} · {goal}`` — a subtask begins; ``goal`` is a free-text tail."""
    glyph = _glyph(GLYPH_START, C.YELLOW, C.BOLD, color=color, stream=stream)
    return _join([f"{glyph} {id}", _truncate_tail(goal)], color=color, stream=stream)


def subtask_done(
    id: str,  # noqa: A002 — subtask identifier; not shadowing a used builtin
    met: int,
    total: int,
    summary: str | None = None,
    *,
    color: bool | None = None,
    stream: TextIO | None = None,
) -> str:
    """``✓ {id} · done · {met}/{total} criteria · {summary}`` — subtask done.

    The ``done`` token and the ``{met}/{total} criteria`` count are the
    obligatory semantic tokens of a completed subtask; only ``summary`` is
    a free-text tail and clipped.
    """
    glyph = _glyph(GLYPH_OK, C.GREEN, color=color, stream=stream)
    parts = [f"{glyph} {id}", "done", f"{met}/{total} criteria"]
    if summary:
        parts.append(_truncate_tail(summary))
    return _join(parts, color=color, stream=stream)


def subtask_incomplete(
    id: str,  # noqa: A002 — subtask identifier; not shadowing a used builtin
    reason: str,
    *,
    color: bool | None = None,
    stream: TextIO | None = None,
) -> str:
    """``⚠ {id} · {reason}`` — a subtask did not meet its contract."""
    glyph = _glyph(GLYPH_WARN, C.YELLOW, color=color, stream=stream)
    return _join([f"{glyph} {id}", _truncate_tail(reason)], color=color, stream=stream)


def autofix_line(
    mode: str,
    repair_ids: Sequence[str],
    budget: int,
    on_exhausted: str,
    *,
    color: bool | None = None,
    stream: TextIO | None = None,
) -> str:
    """``┌ attestation auto-fix · <mode> · repair <ids> · budget <n> · on_exhausted <policy>``.

    The one-line card that opens an automatic subtask-attestation repair
    pass. ``┌`` marks it as a card (like a handoff). Every field is a
    structured token — ``mode`` (``auto_repair`` / ``retry_feedback``),
    the comma-joined ``repair_ids``, the integer ``budget``, and the
    ``on_exhausted`` policy — so none is truncated.
    """
    glyph = _glyph(GLYPH_CONTRACT, C.CYAN, color=color, stream=stream)
    ids = ",".join(repair_ids)
    return _join(
        [
            f"{glyph} attestation auto-fix",
            mode,
            f"repair {ids}",
            f"budget {budget}",
            f"on_exhausted {on_exhausted}",
        ],
        color=color,
        stream=stream,
    )


def implement_done(
    subtasks_done: int,
    subtasks_total: int,
    files_changed: int,
    *,
    color: bool | None = None,
    stream: TextIO | None = None,
) -> str:
    """``✓ implement · {done}/{total} subtasks · {n} files changed`` rollup."""
    glyph = _glyph(GLYPH_OK, C.GREEN, color=color, stream=stream)
    return _join(
        [
            f"{glyph} implement",
            f"{subtasks_done}/{subtasks_total} subtasks",
            f"{files_changed} files changed",
        ],
        color=color,
        stream=stream,
    )


# ── gates ────────────────────────────────────────────────────────────────


def gates_line(
    timing: str,
    results: str,
    receipts_path: str,
    ok: bool,
    *,
    color: bool | None = None,
    stream: TextIO | None = None,
) -> str:
    """``✓ gates {timing}: {results} · receipts → {path}`` — single-line gates.

    On success: ``✓ gates {timing}: g1 PASS · g2 PASS · receipts → {dir}``;
    on failure: ``✗ gates {timing}: g1 FAIL · receipt → {path}``. The
    ``✓``/``✗`` glyph and its colour follow ``ok``, carrying the pass/fail
    state even with colour stripped. ``timing`` labels the hook, ``results``
    holds the ``name PASS|FAIL`` gate tokens, and ``receipts_path`` is the
    receipts directory (success) or the specific failing receipt file
    (failure); the trailing ``receipts``/``receipt`` label follows ``ok``.
    All three are structured tokens (a hook label, gate names/verdicts, a
    path) and are never truncated.
    """
    symbol, code = (GLYPH_OK, C.GREEN) if ok else (GLYPH_FAIL, C.RED)
    glyph = _glyph(symbol, code, color=color, stream=stream)
    head = f"{glyph} gates {timing}: {results}" if results else f"{glyph} gates {timing}:"
    parts = [head]
    if receipts_path:
        label = "receipts" if ok else "receipt"
        parts.append(f"{label} → {receipts_path}")
    return _join(parts, color=color, stream=stream)


# ── handoff / retry / resume ─────────────────────────────────────────────


def handoff_line(
    handoff_id: str,
    trigger: str,
    verdict: str,
    *,
    color: bool | None = None,
    stream: TextIO | None = None,
) -> str:
    """``═ handoff {handoff_id} · {trigger} · verdict {verdict}`` — card head.

    All three fields are structured tokens (an id, a trigger name, a
    verdict enum) and read in full; only the ``verdict`` field carries a
    literal ``verdict `` label.
    """
    glyph = _glyph(GLYPH_RULE, C.CYAN, color=color, stream=stream)
    return _join(
        [f"{glyph} handoff {handoff_id}", trigger, f"verdict {verdict}"],
        color=color,
        stream=stream,
    )


def handoff_action_line(
    action: str,
    feedback: str | None = None,
    note: str | None = None,
    *,
    color: bool | None = None,
    stream: TextIO | None = None,
) -> str:
    """``action: {action} · feedback: "{feedback}" · {note}`` — decision line.

    The sub-line under a :func:`handoff_line` head. ``action`` is a
    structured enum token and ``note`` (e.g. ``run parks
    awaiting_phase_handoff``) is a structured status token — neither is
    truncated. Only ``feedback`` is a free-text tail: it is quoted and
    clipped. Callers indent this line under its head.
    """
    parts = [f"action: {action}"]
    if feedback:
        parts.append(f'feedback: "{_truncate_tail(feedback)}"')
    if note:
        parts.append(note)
    return _join(parts, color=color, stream=stream)


def resume_line(
    phases_completed: int,
    *,
    decision_action: str | None = None,
    decision_feedback: str | None = None,
    decision_result: str | None = None,
    color: bool | None = None,
    stream: TextIO | None = None,
) -> str:
    """``↺ resume from checkpoint · {n} phases completed · decision replay: …``.

    ``phases_completed`` is a structured count. The optional decision-replay
    field is composed here as ``{decision_action} "{decision_feedback}" →
    {decision_result}``: ``decision_action`` and ``decision_result`` are
    structured enum tokens (never truncated) and only ``decision_feedback``
    is a free-text tail, quoted and clipped. The whole field is omitted when
    no ``decision_action`` is given.
    """
    glyph = _glyph(GLYPH_RETRY, C.BLUE, color=color, stream=stream)
    parts = [
        f"{glyph} resume from checkpoint",
        f"{phases_completed} phases completed",
    ]
    if decision_action:
        replay = decision_action
        if decision_feedback:
            replay += f' "{_truncate_tail(decision_feedback)}"'
        if decision_result:
            replay += f" → {decision_result}"
        parts.append(f"decision replay: {replay}")
    return _join(parts, color=color, stream=stream)


def delivery_line(
    sha: str,
    branch: str | None = None,
    *,
    pr_url: str | None = None,
    color: bool | None = None,
    stream: TextIO | None = None,
) -> str:
    """``✓ delivery · committed {sha}`` (``· branch {branch}``) — the commit.

    ``sha`` and ``branch`` are structured refs and read in full; each
    carries its own label (``committed`` / ``branch``).

    A published-branch delivery (ADR 0119/0121) never touches the canonical
    checkout, so it carries no ``sha``. Pass an empty ``sha`` and the line
    takes the published shape — ``✓ delivery · PR {pr_url} · branch {branch}``
    when a PR was opened, or ``✓ delivery · branch {branch}`` when it degraded
    to a pushed-branch-only publish — instead of a misleading
    ``committed <empty>``.
    """
    glyph = _glyph(GLYPH_OK, C.GREEN, color=color, stream=stream)
    parts = [f"{glyph} delivery"]
    if sha:
        parts.append(f"committed {sha}")
    elif pr_url:
        parts.append(f"PR {pr_url}")
    if branch:
        parts.append(f"branch {branch}")
    if pr_url and sha:
        parts.append(f"PR {pr_url}")
    return _join(parts, color=color, stream=stream)


__all__ = [
    "GLYPH_CONTRACT",
    "GLYPH_FAIL",
    "GLYPH_OK",
    "GLYPH_RETRY",
    "GLYPH_RULE",
    "GLYPH_START",
    "GLYPH_WARN",
    "SEP",
    "autofix_line",
    "delivery_line",
    "gates_line",
    "handoff_action_line",
    "handoff_line",
    "implement_done",
    "phase_end",
    "phase_start",
    "plan_contract",
    "resume_line",
    "subtask_done",
    "subtask_incomplete",
    "subtask_start",
    "verdict_line",
]
