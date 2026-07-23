"""Cross-project rendering helpers (peer module).

Extracted from :mod:`pipeline.cross_project.orchestrator` per ADR 0047
Phase B. This module owns the **rendering surface** non-CLI cross
peers reach for — phase banners, success/warn/preview chips, color
constants, the cross-plan preview renderer. Before this extraction
``orchestrator.py`` was load-bearing as a rendering peer:
``planning_loop.py``, ``handoff_payloads.py``, and the
``_DispatchPorts`` construction in the orchestrator body itself all
imported helpers from ``orchestrator``, leaving non-CLI cross modules
structurally dependent on the CLI entry-point module.

After Phase B:

* **`rendering.py` is a leaf peer.** It imports only from
  ``core.io.transcript``, ``core.observability.logging``, and
  ``pipeline.cross_project.plan_parser`` — no orchestrator import,
  no CLI dependency.
* **Non-CLI cross modules** (``planning_loop.py``, ``handoff_payloads.py``,
  the future ``app.py``, the future ``finalization.py``) MUST import
  render helpers from ``rendering.py``, not from ``orchestrator.py``.
* **`orchestrator.py` MAY re-import from ``rendering.py``** during the
  Phase B → Phase D transition because the run body still lives in
  orchestrator until Phase D moves it to ``app.py``. The cycle that
  Phase B prevents is the reverse direction (``rendering.py`` ever
  importing from ``orchestrator.py``); rendering must stay a leaf.

ADR 0047 D4 — **`banner()` render/log split**. Pre-Phase-B `banner()`
called ``print(...)`` AND ``log_phase(...)`` in one body; gating the
whole helper would suppress the structural ``phase.start`` /
``progress.log`` writes — the same trap ADR 0046 Phase C fell into
and fixed in the r5 commit. Phase B applies that fix prospectively:
``banner()`` now takes a ``terminal: bool = True`` keyword-only
parameter. When ``terminal=True`` the print fires + ``log_phase``
fires (byte-identical to legacy CLI behaviour). When ``terminal=False``
ONLY ``log_phase`` fires — silent callers (future cross SILENT in
Phase E) keep full observability parity with TERMINAL.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pipeline.cross_project.plan_parser as plan_parser
from core.io.ansi import C, paint
from core.io.transcript import render_cross_plan_block, render_review_block
from core.observability.logging import log_phase


def banner(
    phase: str,
    title: str,
    color: str = C.CYAN,
    *,
    phase_kind: str | None = None,
    attempt: int = 1,
    terminal: bool = True,
) -> None:
    """Cross-phase banner header — stdout + progress.log + event mirror.

    ADR 0047 D4 — render/log split. ``terminal=True`` (CLI / SDK
    default) preserves the legacy three-line banner header on stdout.
    ``terminal=False`` (silent callers, Phase E) skips the print
    surface but ``log_phase(...)`` ALWAYS fires so ``events.jsonl``
    + ``progress.log`` carry the matched ``phase.start`` event
    regardless of presentation. ADR 0046 stop #9 — file + event
    sinks are never gated by presentation policy.

    Args:
        phase:      Canonical phase name (e.g. ``"CROSS_PLAN"``,
                    ``"CONTRACT_CHECK"``, ``"DONE"``). Used as the
                    bracketed banner tag AND as the ``log_phase``
                    phase string.
        title:      Free-form title rendered after the bracketed tag.
        color:      ANSI color escape (default ``C.CYAN``).
        phase_kind: Canonical kind from
                    :mod:`core.observability.phases` (PLAN, IMPLEMENT,
                    ...). Threaded into the ``phase.start`` event so
                    the dashboard can group by attempt. Pass ``None``
                    for non-canonical milestones (banners like ``DONE``).
        attempt:    1-based attempt index. Singletons stay at 1; loops
                    (cross_plan ↔ cross_validate_plan) increment per
                    round.
        terminal:   Print the banner header to stdout when ``True``
                    (default). ``log_phase`` fires either way.
    """
    if terminal:
        line = "═" * 62
        print(f"\n{paint(line, color, C.BOLD)}")
        print(paint(f"  [{phase}] {title}", color, C.BOLD))
        print(f"{paint(line, color, C.BOLD)}\n")
    log_phase(phase, title, phase_kind=phase_kind, attempt=attempt)


def success(t: str) -> None:
    """Green success chip: ``  ✓ {t}``. Pure stdout — no event side-effect."""
    print(paint(f"  ✓ {t}", C.GREEN, C.BOLD))


def warn(t: str) -> None:
    """Yellow warning chip: ``  ⚠ {t}``. Pure stdout — no event side-effect."""
    print(paint(f"  ⚠ {t}", C.YELLOW, C.BOLD))


def recovery_hint(data: dict, *, terminal: bool = True) -> None:
    """Recovery block printed after a cross FAILED banner.

    Surfaces the cross ``run_dir``, per-alias retained worktree
    checkout paths, and per-alias ``diff.patch`` paths so the operator
    can inspect / edit the in-flight changes that did not make it to a
    commit. ``terminal=False`` makes the helper a no-op so SILENT
    callers (Phase E) stay quiet; ``run.end`` + ``session.json``
    already carry the same data structurally for headless consumers.

    Args:
        data: payload built by
            :func:`pipeline.cross_project.finalization._collect_recovery_hint_data`.
            Expected keys:

            * ``run_dir`` (str) — cross run dir.
            * ``worktrees`` (list[tuple[str, str]]) — per-alias
              ``(alias, checkout_path)`` pairs. Empty list when no
              alias dispatched yet.
            * ``diffs`` (list[tuple[str, str]]) — per-alias
              ``(alias, diff_patch_path)`` pairs for diff.patch files
              that exist on disk. Empty list when none captured.
        terminal: ``True`` (default) renders to stdout. ``False`` makes
            the helper a no-op for SILENT callers.
    """
    if not terminal:
        return
    print(f"\n{paint('  Recovery', C.YELLOW, C.BOLD)}")
    run_dir = data.get("run_dir")
    if run_dir:
        print(f"    {paint('run dir', C.CYAN)}    {run_dir}")
    worktrees = data.get("worktrees") or []
    if worktrees:
        print(f"    {paint('worktrees', C.CYAN)}")
        for alias, path in worktrees:
            print(f"      [{alias}]  {path}")
    diffs = data.get("diffs") or []
    if diffs:
        print(f"    {paint('diffs', C.CYAN)}")
        for alias, path in diffs:
            print(f"      [{alias}]  {path}")
    print(
        f"    {paint('next', C.CYAN)}       "
        "Inspect retained worktrees, rerun the task, or (once CFA "
        "pause lands)\n              "
        "use `phase_handoff_decide` to override."
    )


def delivery_block(per_alias: dict, *, terminal: bool = True) -> None:
    """Render the cross-delivery summary after the terminal banner.

    Lists each alias's delivery outcome — ``committed <sha>`` /
    ``applied_uncommitted`` / ``no_diff`` / ``skipped`` /
    ``<failure> <error>``. ``terminal=False`` is a no-op so SILENT
    callers stay quiet; ``session["phases"]["cross_delivery"]`` carries
    the same data structurally for headless consumers.

    Args:
        per_alias: ``session["phases"]["cross_delivery"]["per_alias"]``
            — ``{alias: {status, commit_sha?, error?, release_override?}}``.
        terminal: ``True`` (default) renders to stdout.
    """
    if not terminal or not per_alias:
        return
    print(f"\n{paint('  Delivery', C.CYAN, C.BOLD)}")
    for alias, rec in per_alias.items():
        status = rec.get("status", "?")
        sha = rec.get("commit_sha")
        error = rec.get("error")
        override = rec.get("release_override")
        if status == "committed" and sha:
            detail = f"committed {sha[:10]}"
            color = C.GREEN
        elif status in {"applied_uncommitted", "no_diff", "skipped",
                        "skipped_already_delivered", "disabled"}:
            detail = status
            color = C.GREEN
        else:
            detail = f"{status}{f' — {error}' if error else ''}"
            color = C.RED
        line = f"      [{alias}]  {paint(detail, color)}"
        if override:
            orig = override.get("original_verdict", "?")
            line += paint(f"  (override: {orig})", C.YELLOW)
        print(line)


def preview(
    label: str,
    text: str,
    color: str = C.WHITE,
    n: int | None = None,
) -> None:
    """Print ``label:`` followed by ``text``.

    Cross-plan bodies and Codex review renders are high-value — an
    operator running a real cross-project task needs to see the full
    plan and full contract-check findings, not the first 400/800 chars
    with a "…" trailer. Default ``n=None`` = no truncation. Callers
    that intentionally want a teaser can still pass ``n=N``.
    """
    print(f"\n{paint(f'{label}:', color)}")
    if n is None or len(text) <= n:
        print(text)
    else:
        print(text[:n] + "…")


def _render_cross_plan_preview(plan_output: str, aliases: list[str]) -> None:
    """Display a cross-plan response with structure when possible.

    Parses ``plan_output`` (the raw architect JSON) and prints the
    structured block on success. Falls through to a raw ``preview`` of
    the output when it is not a valid cross-plan JSON object — partial
    structured rendering would hide what the agent actually produced.
    """
    try:
        result = plan_parser.parse_cross_plan(plan_output, aliases)
    except plan_parser.CrossPlanParseError:
        preview("Cross-project plan", plan_output, C.MAGENTA)
        return
    print(render_cross_plan_block(result.parsed.to_render_dict()))


def render_cross_final_acceptance_block(entry: Mapping[str, Any]) -> str:
    """Render the cross final-acceptance verdict as a structured,
    ANSI-styled block instead of a raw markdown dump.

    Reuses the single-project release renderer
    (:func:`core.io.transcript.render_review_block`) so the cross
    ``CROSS_FINAL_ACCEPTANCE`` verdict surfaces the same aligned fields
    the mono ``final_acceptance`` path shows — ``verdict`` / ``ship_ready``
    / short summary / release blockers / verification gaps / contract
    status — rather than literal ``#`` / ``**`` / ``-`` markers.

    ``entry`` is the dual-shape phase-log dict produced by
    :func:`pipeline.cross_project.final_acceptance.result_to_phase_log_entry`.
    ``title=None`` keeps the ``preview`` label
    ("Cross-final-acceptance verdict:") as the sole header, mirroring the
    project path where the phase banner is the header (the renderer would
    otherwise add its own inner "Review" title line).
    """
    return render_review_block(entry, title=None)


def silent_renderers(terminal: bool):
    """ADR 0047 Phase E factory — return presentation-aware render
    callables.

    Returns ``(banner, success, warn, preview,
    _render_cross_plan_preview, print_fn, C)``. Under ``terminal=True``
    they are the canonical render helpers (byte-identical to
    pre-Phase-E behaviour). Under ``terminal=False``:

      * ``banner`` forwards ``terminal=False`` to the rendering
        implementation — ``log_phase`` STILL fires (ADR 0046 stop #9
        invariant: file + event sinks are never gated by presentation).
      * ``success`` / ``warn`` / ``preview`` /
        ``_render_cross_plan_preview`` / ``print_fn`` are pure-stdout
        no-ops.

    Both ``pipeline.cross_project.app._run_cross_pipeline_session``
    and every gated function in
    ``pipeline.cross_project.planning_loop`` destructure the seven
    callables at the top of their scope so the body code below
    reads exactly the same under both presentations — no explicit
    ``if terminal:`` gate needed at each call site. Python's
    local-name binding then resolves ``banner(...)`` / ``success(...)`` /
    ``print(...)`` / etc. to the right shadow at call time.

    Critically, this factory returns BOTH branches' callables
    unconditionally — calling it from BOTH the SILENT and TERMINAL
    code paths means Python's compiler marks the destructured names
    as locals throughout the function with no risk of
    ``UnboundLocalError`` from conditional ``def`` blocks.
    """
    if terminal:
        return (
            banner,
            success,
            warn,
            preview,
            _render_cross_plan_preview,
            print,
            C,
        )

    def _silent_banner(*args, **kwargs):
        kwargs["terminal"] = False
        return banner(*args, **kwargs)

    def _silent_noop(*_args, **_kwargs):
        return None

    return (
        _silent_banner,
        _silent_noop,   # success
        _silent_noop,   # warn
        _silent_noop,   # preview
        _silent_noop,   # _render_cross_plan_preview
        _silent_noop,   # print_fn
        C,
    )


__all__ = [
    "C",
    "_render_cross_plan_preview",
    "banner",
    "delivery_block",
    "paint",
    "preview",
    "recovery_hint",
    "silent_renderers",
    "success",
    "warn",
]
