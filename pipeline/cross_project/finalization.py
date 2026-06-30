"""Silent + terminal-wrapper finalization for the cross-project pipeline.

ADR 0047 Phase F. Mirrors :mod:`pipeline.project.finalization`'s
silent-service / terminal-wrapper split (ADR 0042 Phase G), applied
to the cross-level tail that used to live inline at the bottom of
:func:`pipeline.cross_project.app._run_cross_pipeline_session`:

  * status decision (failed / skipped-by-policy / done) based on the
    optional cross_final_acceptance result and the set of blocking
    contract_check skips;
  * ``session`` mutation (``status`` / ``halt_reason`` /
    ``failure_reason``);
  * unconditional ``run.end`` event emit;
  * per-project metrics rollup;
  * session.json + metrics.json + evidence-bundle persistence;
  * artifact mirror.

**Service split.**

* :func:`finalize_cross_run` is the silent structured service. It
  owns the status decision, mutates ``session``, emits ``run.end``,
  writes ``session.json`` / ``metrics.json`` / the evidence bundle,
  and runs the artifact mirror. Produces **zero stdout / stderr**.
  SILENT cross callers (Phase E) consume this directly.

* :func:`finalize_cross_with_terminal_output` is the CLI / TERMINAL
  wrapper: it calls the silent service FIRST, then renders the
  DONE / FAILED banner + the per-phase chips (``Projects: ...``,
  cross-run rollup, ``Session: ...``, ``Metrics: ...``,
  ``Mirrored N artifacts ...``) using the structured
  :class:`CrossFinalizationResult` as the only source of truth. The
  wrapper does NOT re-decide status, NOT re-emit ``run.end``, NOT
  re-write any persisted file — that is the load-bearing invariant
  Phase F's tests pin (mirrors ADR 0042 Phase G project finalization).

**Direction rule (ADR 0047 D2).** This module sits in
``pipeline.cross_project`` and must NOT import from
:mod:`pipeline.cross_project.orchestrator`. It imports rendering
helpers from :mod:`pipeline.cross_project.rendering` (the leaf peer
from Phase B) and the engine writers it needs by stable name.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from core.io.ansi import C, paint
from core.observability import events as _events
from pipeline.cross_project.rendering import success
from pipeline.engine import save_session as save_cross_session
from pipeline.run_state.terminal import settle_cross_terminal


@dataclass(frozen=True, slots=True)
class CrossFinalizationContext:
    """Inputs for :func:`finalize_cross_run`.

    Captures every local from the cross app body that the inline
    finalization tail used to read directly. Frozen + slotted because
    the silent service treats this as a value object — all mutation
    targets (``session``) are dicts whose identity is what gets
    threaded, not the context itself.

    Args:
        run_dir: per-run directory under ``worktree/runs/``. Holds
            ``session.json``, ``metrics.json``, ``progress.log``, the
            evidence bundle.
        output_dir: ``True`` when the run was invoked with an output
            directory (i.e. real disk writes happen). When ``False``
            the silent service skips persistence but still emits
            ``run.end`` and mirrors artifacts.
        session: cross-level session dict. **Mutated** in place with
            ``status`` / ``halt_reason`` / ``failure_reason`` /
            ``metrics``. Callers see the updates after the silent
            service returns.
        projects: alias → project-path mapping (used for the rollup
            line and the artifact mirror).
        max_rounds: configured maximum review/repair rounds per
            project. Surfaces in the ``Projects: N | Rounds each: M``
            chip rendered by the terminal wrapper.
        cfa_result: the ``CrossFinalAcceptanceResult`` returned by
            :func:`pipeline.cross_project.cross_final_acceptance.run_cross_final_acceptance`,
            or ``None`` when CFA was skipped by config (advanced
            profile with the gate disabled). Drives the status
            decision.
        contract_results: per-alias contract_check entries from
            ``session["phases"]["contract_check"]``. The silent
            service inspects them to detect blocking skips when CFA
            is disabled by policy.
        contract_check_failed: ``True`` when at least one
            contract_check fired and failed. Threaded forward from
            the cross body so the silent service can chain reasons.
        contract_check_failure_reason: human-readable summary of the
            contract_check failure (e.g. ``"web: skipped (rejected)"``).
            ``None`` when ``contract_check_failed`` is ``False``.
        cross_phase_usage: per-phase usage rollup the cross body
            accumulated (e.g. ``{"plan": {...}, "cross_final_acceptance":
            {...}}``). Surfaces in the cross-run rollup table.
    """

    run_dir: Path
    output_dir: bool
    session: dict[str, Any]
    projects: dict[str, Path]
    max_rounds: int
    cfa_result: Any | None
    contract_results: dict[str, Any]
    contract_check_failed: bool
    contract_check_failure_reason: str | None
    cross_phase_usage: dict[str, Any]
    # Phase B — the ``CrossDeliveryResult`` from ``run_cross_delivery``,
    # or ``None`` when delivery did not run (policy-skipped CFA). When
    # present it drives the terminal status off the delivery aggregate:
    # ``ok``/``disabled`` → done; ``partial`` → cross_delivery_partial;
    # ``failed`` → cross_delivery_failed; ``halted`` → halted.
    delivery_result: Any | None = None
    # ADR 0115 slice 4 — the cross checkpoint dict. Threaded so the
    # normal done/failed settle clears the settle-only ``pending_gate``
    # residue from the checkpoint at the same single substrate the
    # early-return terminals use. ``default_factory=dict`` keeps in-memory
    # tests (which carry no gate residue) constructing the context without
    # a checkpoint; the production caller always threads ``ctx.cross_ckpt``.
    cross_ckpt: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CrossFinalizationResult:
    """Structured outcome of :func:`finalize_cross_run`.

    The terminal wrapper consumes this and produces banners + chips;
    no field is recomputed inside the wrapper. Every persisted-path
    field carries the actual writer return (``None`` when the writer
    was skipped because ``output_dir`` was ``False`` or because the
    underlying data was empty).

    Args:
        status: terminal session status — ``"done"`` or ``"failed"``.
            Mirrors ``session["status"]`` after the silent service
            runs.
        halt_reason: machine-readable halt reason when ``status`` is
            ``"failed"``, else ``None``. Mirrors
            ``session["halt_reason"]``.
        failure_reason: human-readable failure summary when
            ``status`` is ``"failed"``, else ``None``. Mirrors
            ``session["failure_reason"]``.
        skipped_by_policy: ``True`` when CFA was disabled by config
            AND no blocking contract_check skips were detected — the
            wrapper renders the ``"(cross_final_acceptance skipped
            by policy)"`` variant of the DONE banner. ``False``
            otherwise.
        session_path: where ``session.json`` was written, or ``None``
            when persistence was skipped (``output_dir`` was ``False``).
        metrics_path: where ``metrics.json`` was written, or ``None``
            when ``output_dir`` was ``False`` OR the rollup carried
            no data.
        mirrored_artifacts: list of absolute paths the artifact
            mirror copied (may be empty). When the mirror raised, the
            list is empty and ``mirror_error`` carries the exception
            text.
        mirror_error: error message string when the mirror raised
            during the silent service, else ``None``. The wrapper
            surfaces it via a ``! mirror skipped`` line.
        per_project_metrics: per-alias metrics rollup (``{alias:
            {phase_usage: ..., total_cost: ...}}``). The wrapper uses
            it to decide whether to print the cross-run rollup
            table.
    """

    status: Literal["done", "failed", "halted"]
    halt_reason: str | None
    failure_reason: str | None
    skipped_by_policy: bool

    session_path: Path | None
    metrics_path: Path | None
    mirrored_artifacts: list[Path] = field(default_factory=list)
    mirror_error: str | None = None
    per_project_metrics: dict[str, dict] = field(default_factory=dict)


# ── recovery hint data ────────────────────────────────────────────────


def _collect_recovery_hint_data(
    ctx: CrossFinalizationContext,
) -> dict[str, Any]:
    """Build the Recovery block payload rendered after the FAILED
    banner by :func:`finalize_cross_with_terminal_output`.

    Source of truth for per-alias worktree path is the child session
    field ``session["phases"]["projects"][alias]["worktree"]["path"]``.
    Child sessions live under ``session["phases"]["projects"]`` (per
    :func:`pipeline.cross_project.project_dispatch._dispatch_one_alias`,
    which assigns ``ctx.session["phases"]["projects"][alias] =
    project_session``). Top-level ``session["projects"]`` is a plain
    ``{alias: project_path_string}`` map and does NOT carry the
    child-side worktree info.

    When the child session field is absent (e.g. the child crashed
    before its session was persisted) AND the conventional path
    ``<cross_run_dir>/worktrees/wt_<alias>/checkout`` exists on disk,
    the convention path is surfaced as a fallback. Otherwise the alias
    is omitted from the worktree list (operator sees only what is
    actually recoverable).

    Per-alias ``diff.patch`` paths point at
    ``<cross_run_dir>/<alias>/diff.patch`` — that is the existing child
    artifact dir established by ``project_dispatch:235``
    (``alias_artifacts = ctx.run_dir / alias``) and the canonical
    ``run_dir/diff.patch`` write site in
    :func:`pipeline.engine.run_diff.capture_run_diff`. Files are only
    surfaced when they exist on disk.

    Cross-project status note: each per-alias child runs through
    ``run_project_pipeline`` with ``output_dir=<cross_run_dir>/<alias>``
    (see ``project_dispatch.py`` ~:386), so this per-alias diff.patch is
    ALREADY apply-checked by the child ``finalize_project_run`` against the
    child's own baseline — the recovery-hint here points at that same
    verified file. No separate per-alias apply-check is required at the
    cross level. The only follow-up candidate is an aggregated cross-level
    artifact-bundle / CFA combined patch, IF one is ever assembled
    separately from these child diff.patch files (follow-up: cross-bundle).

    Returns the dict consumed by
    :func:`pipeline.cross_project.rendering.recovery_hint`. The
    ``worktrees`` / ``diffs`` lists may be empty when the cross run
    failed before dispatch — the renderer omits empty sections so the
    block degrades gracefully.
    """
    per_alias_worktrees: list[tuple[str, str]] = []
    per_alias_diffs: list[tuple[str, str]] = []

    children = ctx.session.get("phases", {}).get("projects", {})
    children_map = children if isinstance(children, dict) else {}

    for alias in ctx.projects:
        child = children_map.get(alias)
        worktree_path: str | None = None
        if isinstance(child, dict):
            worktree = child.get("worktree")
            if isinstance(worktree, dict):
                wp = worktree.get("path")
                if isinstance(wp, str) and wp:
                    worktree_path = wp
        if worktree_path is None:
            # Convention fallback: <cross_run_dir>/worktrees/wt_<alias>/checkout.
            # Production worktree id for a cross child is ``wt_<alias>`` because
            # the child's output_dir name = alias (see project_dispatch:235 +
            # engine.worktree._worktree_id_for_run). Only surface this fallback
            # when the directory actually exists on disk — it never lies about
            # something that isn't there.
            convention = (
                ctx.run_dir / "worktrees" / f"wt_{alias}" / "checkout"
            )
            if convention.exists():
                worktree_path = str(convention)
        if worktree_path is not None:
            per_alias_worktrees.append((alias, worktree_path))

        diff_path = ctx.run_dir / alias / "diff.patch"
        if diff_path.exists():
            per_alias_diffs.append((alias, str(diff_path)))

    return {
        "run_dir": str(ctx.run_dir),
        "worktrees": per_alias_worktrees,
        "diffs": per_alias_diffs,
    }


# ── status decision ──────────────────────────────────────────────────


def _decide_status(
    ctx: CrossFinalizationContext,
) -> tuple[Literal["done", "failed", "halted"], str | None, str | None, bool]:
    """Return ``(status, halt_reason, failure_reason, skipped_by_policy)``.

    Mirrors the three-branch decision tree that used to live inline
    at the bottom of ``_run_cross_pipeline_session``:

      1. CFA was disabled by config (``ctx.cfa_result is None``):
         a. any blocking contract_check skips → ``failed`` with
            ``cross_contract_check_blocking_skip``;
         b. otherwise → ``done`` with the policy-skip variant.
      2. CFA ran and rejected / failed to parse → ``failed`` with the
         per-source halt_reason and a chained
         ``contract_check_failure_reason`` when present.
      3. CFA ran and approved → ``done``.

    Phase B overlay: when ``ctx.delivery_result`` is present (delivery
    ran on the CFA-approved / override path) and the base decision is
    ``done``, the delivery aggregate overrides the terminal status —
    ``ok``/``disabled`` stay ``done``; ``partial`` →
    ``cross_delivery_partial``; ``failed`` → ``cross_delivery_failed``;
    ``halted`` → ``halted``. Delivery never upgrades a base ``failed``.
    """
    base = _decide_base_status(ctx)
    dr = ctx.delivery_result
    if dr is None or base[0] != "done":
        return base
    _, _, _, skipped_by_policy = base
    overall = getattr(dr, "overall", "ok")
    if overall in {"ok", "disabled"}:
        return "done", None, None, skipped_by_policy
    if overall == "partial":
        return (
            "failed",
            "cross_delivery_partial",
            "cross delivery partial: one or more aliases not delivered",
            False,
        )
    if overall == "failed":
        return (
            "failed",
            "cross_delivery_failed",
            "cross delivery failed: no alias delivered",
            False,
        )
    if overall == "halted":
        return (
            "halted",
            "phase_handoff_halt",
            "cross delivery halted by operator",
            False,
        )
    return base


def _decide_base_status(
    ctx: CrossFinalizationContext,
) -> tuple[Literal["done", "failed"], str | None, str | None, bool]:
    """The pre-delivery CFA / contract_check status decision."""
    if ctx.cfa_result is None:
        _blocking_skips: list[str] = []
        for _alias, _entry in ctx.contract_results.items():
            if (
                isinstance(_entry, dict)
                and _entry.get("skipped") is True
                and _entry.get("source") == "operator"
                and _entry.get("on_skip", "block") == "block"
            ):
                _blocking_skips.append(_alias)
        if _blocking_skips:
            failure_reason = (
                "contract_check skipped under on_skip=block while "
                "cross_final_acceptance disabled (aliases: "
                f"{', '.join(_blocking_skips)})"
            )
            return (
                "failed",
                "cross_contract_check_blocking_skip",
                failure_reason,
                False,
            )
        return "done", None, None, True

    cfa = ctx.cfa_result
    _cfa_failed = (
        not cfa.parsed.approved or cfa.source == "parse_error"
    )
    if not _cfa_failed:
        return "done", None, None, False

    _reason_parts = ["cross_final_acceptance"]
    if cfa.source == "parse_error":
        _reason_parts.append("parse error")
        halt_reason = "cross_final_acceptance_parse_error"
    elif cfa.source == "precondition":
        _reason_parts.append("precondition violations")
        halt_reason = "cross_final_acceptance_precondition"
    else:
        _reason_parts.append("agent REJECTED")
        halt_reason = "cross_final_acceptance_failed"
    if ctx.contract_check_failed and ctx.contract_check_failure_reason:
        _reason_parts.append(ctx.contract_check_failure_reason)
    return "failed", halt_reason, ": ".join(_reason_parts), False


def _collect_per_project_metrics(session: dict[str, Any]) -> dict[str, dict]:
    """Walk ``session["phases"]["projects"]`` and pluck the per-alias
    ``metrics`` dicts in a stable order."""
    out: dict[str, dict] = {}
    projects_entry = session.get("phases", {}).get("projects", {})
    if not isinstance(projects_entry, dict):
        return out
    for alias, sub in projects_entry.items():
        if not isinstance(sub, dict):
            continue
        metrics = sub.get("metrics")
        if isinstance(metrics, dict):
            out[alias] = metrics
    return out


# ── silent service ───────────────────────────────────────────────────


def finalize_cross_run(
    ctx: CrossFinalizationContext,
) -> CrossFinalizationResult:
    """Silent structured tail-finalization for a cross-project run.

    Owns the status decision, ``session`` mutation, ``run.end`` event
    emit, per-project metrics rollup, ``session.json`` +
    ``metrics.json`` + evidence-bundle persistence, and the artifact
    mirror. Produces **zero stdout / stderr**.

    Re-entrant only via the terminal wrapper: nothing inside is
    idempotent, so calling this function twice on the same context
    would double-emit ``run.end`` and double-write the metrics blob.
    SILENT callers invoke this directly; TERMINAL callers go through
    :func:`finalize_cross_with_terminal_output` which wraps a single
    call and renders the chips.

    Args:
        ctx: structured inputs — see :class:`CrossFinalizationContext`.

    Returns:
        A :class:`CrossFinalizationResult` summarising the decided
        status, persisted paths, and per-project metrics. The
        terminal wrapper reads these fields to render the
        DONE / FAILED banner + chips with no recomputation.
    """
    status, halt_reason, failure_reason, skipped_by_policy = (
        _decide_status(ctx)
    )
    session = ctx.session
    # Field-mutate status / halt_reason through the cross-safe terminal
    # helper so a stale active phase_handoff is cleared for EVERY in-scope
    # cross terminal — done, halted, and FAILED. The failed path must NOT
    # use the single-project mark_run_failed, which deliberately preserves
    # an undecided handoff: a cross pause always short-circuits to a final
    # terminal, so any payload left here is stale and would otherwise make
    # RunControl / resume surface an outdated pending action. The status
    # decision tree is unchanged; only the field write routes through here.
    # Threading ``cross_ckpt`` makes this normal done/failed settle the same
    # single settle-only clearing point for the ``pending_gate`` gate-decision
    # residue that the early-return terminals use (ADR 0115 slice 4): the
    # session mirror is evicted here, the checkpoint copy below.
    settle_cross_terminal(
        session,
        status=status,
        halt_reason=halt_reason,
        cross_ckpt=ctx.cross_ckpt,
    )
    if failure_reason is not None:
        session["failure_reason"] = failure_reason

    _events.emit(
        "run.end",
        status=session.get("status", "done"),
        projects=len(ctx.projects),
        rounds=ctx.max_rounds,
    )

    per_project_metrics = _collect_per_project_metrics(session)

    session_path: Path | None = None
    metrics_path: Path | None = None
    if ctx.output_dir:
        if session["status"] != "done" and not session.get("halt_reason"):
            session["halt_reason"] = f"cross_{session['status']}"
        session_path = save_cross_session(ctx.run_dir, session)
        # Persist the settle-only ``pending_gate`` eviction the settle applied
        # to the checkpoint copy (ADR 0115 slice 4). Best-effort, same posture
        # as every other cross checkpoint write.
        from pipeline.cross_project.checkpoint import write_cross_checkpoint
        write_cross_checkpoint(ctx.run_dir, ctx.cross_ckpt)
        if per_project_metrics or ctx.cross_phase_usage:
            from core.observability.metrics import cross_metrics_dict

            cross_metrics = cross_metrics_dict(
                per_project_metrics, ctx.cross_phase_usage,
            )
            session["metrics"] = cross_metrics
            metrics_path = ctx.run_dir / "metrics.json"
            metrics_path.write_text(
                json.dumps(cross_metrics, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        try:
            from pipeline.evidence import write_bundle_or_placeholder

            write_bundle_or_placeholder(
                ctx.run_dir,
                run_id=session.get("run_id") or ctx.run_dir.name,
                status=session["status"],
            )
        except Exception:  # noqa: BLE001
            # Evidence-bundle writer is best-effort; a failure must
            # not nullify the rest of the finalization tail. ADR 0047
            # Phase F preserves the legacy silence-on-fail behaviour.
            pass

    mirrored_artifacts: list[Path] = []
    mirror_error: str | None = None
    try:
        from core.infra import config
        from pipeline.engine.artifact_mirror import mirror_to_projects

        _app_cfg = config.AppConfig.load()
        mirrored_artifacts = mirror_to_projects(
            ctx.run_dir, ctx.projects, _app_cfg.artifacts,
        )
    except Exception as exc:  # noqa: BLE001
        mirror_error = str(exc)

    return CrossFinalizationResult(
        status=status,
        halt_reason=halt_reason,
        failure_reason=failure_reason,
        skipped_by_policy=skipped_by_policy,
        session_path=session_path,
        metrics_path=metrics_path,
        mirrored_artifacts=mirrored_artifacts,
        mirror_error=mirror_error,
        per_project_metrics=per_project_metrics,
    )


# ── terminal wrapper ─────────────────────────────────────────────────


def finalize_cross_with_terminal_output(
    ctx: CrossFinalizationContext,
) -> CrossFinalizationResult:
    """Terminal-rendering wrapper around :func:`finalize_cross_run`.

    Calls the silent service exactly once and then renders the
    legacy CLI transcript tail: the DONE / FAILED banner, the
    ``Projects: ...`` chip, the cross-run rollup table when there
    is data to render, the ``Session: ...`` / ``Metrics: ...``
    chips when those files were persisted, and the
    ``Mirrored N artifacts ...`` chip when the mirror produced
    output.

    Load-bearing invariant: this wrapper does NOT re-decide status,
    NOT re-emit ``run.end``, NOT re-write any persisted file. Every
    rendered line is derived from the
    :class:`CrossFinalizationResult` the silent service returned.
    Phase F tests pin this invariant (`run.end` emitted exactly
    once, no double session.json write, no status re-decision).
    """
    # Import banner here so the silent path doesn't pull rendering
    # imports onto the silent code path. The wrapper IS the terminal
    # branch — its imports stay local.
    from pipeline.cross_project.rendering import (
        banner,
        delivery_block,
        recovery_hint,
    )

    result = finalize_cross_run(ctx)

    if result.status == "failed":
        banner(
            "FAILED",
            f"Cross-project pipeline failed: {result.failure_reason}",
            C.RED,
        )
    elif result.status == "halted":
        banner(
            "HALTED",
            f"Cross-project pipeline halted: {result.failure_reason}",
            C.YELLOW,
        )
    elif result.skipped_by_policy:
        banner(
            "DONE",
            "Cross-project pipeline complete (cross_final_acceptance "
            "skipped by policy) ✅",
            C.GREEN,
        )
    else:
        banner("DONE", "Cross-project pipeline complete ✅", C.GREEN)

    # Phase B — per-alias delivery summary (committed sha / applied /
    # no_diff / failed). Sourced from the phase-scoped evidence the
    # delivery loop wrote; a no-op when delivery did not run.
    _delivery_evidence = ctx.session.get("phases", {}).get("cross_delivery")
    if isinstance(_delivery_evidence, dict):
        delivery_block(_delivery_evidence.get("per_alias", {}))

    # Recovery hint: surface the cross run_dir + retained per-alias
    # worktree checkouts + per-alias diff.patch paths so the operator
    # can inspect / edit the in-flight changes a non-``done`` run left
    # on disk — useful exactly when CFA rejected, delivery was partial
    # / failed, or the operator halted.
    if result.status in {"failed", "halted"}:
        recovery_hint(_collect_recovery_hint_data(ctx))

    success(
        f"Projects: {len(ctx.projects)} | Rounds each: {ctx.max_rounds}",
    )

    if result.per_project_metrics or ctx.cross_phase_usage:
        from core.observability.metrics import (
            cross_summary_line,
            cross_summary_table,
        )

        print(f"\n{paint('Cross-run rollup:', C.BOLD)}")
        print(
            cross_summary_table(
                result.per_project_metrics, ctx.cross_phase_usage,
            ),
        )
        success(
            "Cross usage: "
            + cross_summary_line(
                result.per_project_metrics, ctx.cross_phase_usage,
            ),
        )

    if result.session_path is not None:
        success(f"Session: {result.session_path}")
    if result.metrics_path is not None:
        success(f"Metrics: {result.metrics_path}")

    if result.mirror_error is not None:
        print(f"  ! mirror skipped: {result.mirror_error}")
    elif result.mirrored_artifacts:
        success(
            f"Mirrored {len(result.mirrored_artifacts)} artifacts across "
            f"{len(ctx.projects)} projects",
        )

    return result


__all__ = [
    "CrossFinalizationContext",
    "CrossFinalizationResult",
    "finalize_cross_run",
    "finalize_cross_with_terminal_output",
]
