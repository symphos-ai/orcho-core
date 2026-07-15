"""Run status — one snapshot of a single run's meta + metrics."""
from __future__ import annotations

from pathlib import Path

from core.observability.metrics import scrub_accounting_fields
from pipeline.control.continuation import resolve_continuation_decision
from pipeline.run_state.setup_failure import merged_status
from sdk._runspace_context import accounting_enabled_for_context
from sdk.actions import compute_next_actions
from sdk.evidence_slices import active_stall_diagnostics, list_sub_runs
from sdk.runs import _CWD_DEFAULT, find_run, load_json_optional, load_meta
from sdk.types import ArtefactRef, GateStatus, PhaseStatus, RunMeta, RunStatus


def _artefact_ref_if_file(
    path: Path, *, kind: str, uri: str, mime: str,
) -> ArtefactRef | None:
    """Build an ``ArtefactRef`` for a physical file, or ``None`` if absent / unreadable.

    Wraps ``exists() → stat()`` in ``try/except OSError`` because the
    file can disappear between the existence check and ``os.stat``
    (concurrent reap, race with delivery). Artefact-map enrichment
    must not fail the whole ``load_status`` call — if we can't read
    the file's size cleanly, the artefact entry is silently omitted
    and the caller still gets a valid ``RunStatus``.
    """
    try:
        size = path.stat().st_size
    except (OSError, FileNotFoundError):
        return None
    return ArtefactRef(kind=kind, uri=uri, mime=mime, size_bytes=size)


def _collect_artefacts(run_id: str, run_dir: Path) -> tuple[ArtefactRef, ...]:
    """Enumerate readable artefacts for a resolved run.

    Called by ``load_status`` AFTER ``find_run`` has resolved the run —
    not-found semantics stay unchanged (``RunNotFound`` raises before
    this function runs).

    Three artefact kinds today:

    - ``parsed_plan`` — physical ``<run_dir>/parsed_plan.json``;
      ``size_bytes`` from ``os.stat``. Omitted when the file does
      not exist (e.g. plan phase hasn't run yet).
    - ``diff`` — physical ``<run_dir>/diff.patch``; ``size_bytes``
      from ``os.stat``. Omitted when the run did not capture a diff.
    - ``evidence`` — composable resource (assembled at read time by
      ``sdk.evidence.collect_evidence``, no single file on disk);
      always emitted for a resolved run with ``size_bytes=None``.

    The URI scheme (``orcho://...``) matches the orcho-mcp resource
    registrations — see ADR 0045 for the scheme-separation rationale.
    """
    refs: list[ArtefactRef] = []
    parsed_plan = _artefact_ref_if_file(
        run_dir / "parsed_plan.json",
        kind="parsed_plan",
        uri=f"orcho://runs/{run_id}/parsed_plan.json",
        mime="application/json",
    )
    if parsed_plan is not None:
        refs.append(parsed_plan)
    diff_patch = _artefact_ref_if_file(
        run_dir / "diff.patch",
        kind="diff",
        uri=f"orcho://runs/{run_id}/diff.patch",
        mime="text/x-patch",
    )
    if diff_patch is not None:
        refs.append(diff_patch)
    # Evidence is composable — always emit for a resolved run.
    refs.append(ArtefactRef(
        kind="evidence",
        uri=f"orcho://runs/{run_id}/evidence",
        mime="application/json",
        size_bytes=None,
    ))
    return tuple(refs)


def _collect_quality_gates(run_dir: Path) -> tuple[GateStatus, ...]:
    """Project finalized evidence gate rows into the lightweight status view.

    Gate events are finalized into ``evidence.json``. ``orcho status`` should
    not compose a fresh evidence bundle just to print a summary, but reading an
    existing finalized bundle is cheap and keeps status aligned with evidence.
    """
    evidence = load_json_optional(run_dir / "evidence.json")
    rows = evidence.get("gates") if isinstance(evidence, dict) else None
    if not isinstance(rows, list):
        return ()

    gates: list[GateStatus] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "?")
        duration_raw = row.get("duration_s")
        try:
            duration_s = (
                float(duration_raw)
                if duration_raw is not None
                else None
            )
        except (TypeError, ValueError):
            duration_s = None
        gates.append(GateStatus(
            name=name,
            outcome=(
                str(row["outcome"])
                if row.get("outcome") is not None
                else None
            ),
            kind=(
                str(row["kind"])
                if row.get("kind") is not None
                else None
            ),
            duration_s=duration_s,
            phase=(
                str(row["phase"])
                if row.get("phase") is not None
                else None
            ),
        ))
    return tuple(gates)


def load_status(
    run_id: str | None = None,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> RunStatus:
    """Load status for a run id, or the newest run when `run_id` is None.

    Raises `NoWorkspace` / `RunNotFound` (via `find_run`). The returned
    `RunStatus` carries the typed projection plus `raw_meta` /
    `raw_metrics` for embedders that need full fidelity.
    """
    ref = find_run(run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd)
    raw_meta = load_meta(ref.run_dir)
    raw_metrics = load_json_optional(ref.run_dir / "metrics.json")
    if not accounting_enabled_for_context(
        workspace=workspace,
        runs_dir=ref.run_dir.parent,
    ):
        raw_metrics = scrub_accounting_fields(raw_metrics)

    # ADR 0104: reconcile meta.status with the optional launcher state via the
    # shared merge rule (terminal meta wins; launcher consulted only for an
    # empty/'running' meta.status; signal-reaped 'failed' -> 'interrupted').
    # Applied to the PROJECTED RunMeta.status and to compute_next_actions only —
    # raw_meta / raw_metrics stay byte-for-byte raw for full fidelity. This is
    # the SAME rule get_errors_halt uses, so load_status and evidence agree on
    # status/halt_reason for the same run dir.
    merged_status_value = merged_status(raw_meta, ref.run_dir) if raw_meta else None

    meta: RunMeta | None = None
    if raw_meta:
        phases = raw_meta.get("phases") or {}
        meta = RunMeta(
            project=raw_meta.get("project"),
            task=str(raw_meta.get("task", "")),
            status=(
                merged_status_value
                if merged_status_value is not None
                else raw_meta.get("status")
            ),
            profile=raw_meta.get("profile"),
            timestamp=raw_meta.get("timestamp"),
            phases=tuple(phases.keys()) if isinstance(phases, dict) else (),
            projects=tuple((raw_meta.get("projects") or {}).keys()),
            extra={
                k: v
                for k, v in raw_meta.items()
                if k
                not in {
                    "project",
                    "task",
                    "status",
                    "profile",
                    "timestamp",
                    "phases",
                    "projects",
                }
            },
        )

    sub_projects = [
        PhaseStatus(name=link.name, status=link.status)
        for link in list_sub_runs(ref.run_id, runs_dir=ref.run_dir.parent, cwd=None)
    ]

    # ADR 0045: enumerate readable artefacts so agents discover what
    # they can fetch without scanning run_dir themselves. Enrichment
    # is best-effort — IO errors leave the entry out, never fail
    # the call.
    artefacts = _collect_artefacts(ref.run_id, ref.run_dir)
    quality_gates = _collect_quality_gates(ref.run_dir)

    # MCP UX A1 / Principle 1: derive suggested follow-ups from the
    # raw meta so the LLM consuming this status payload sees workflow
    # next steps without having to remember them. State-derived,
    # recomputed every load.
    # Live event-backed non-terminal stall diagnostics (read from the
    # event-store) feed the second recovery source so an unsafe-process-polling
    # warning surfaces in next_actions while the phase is still running, before
    # any terminal failure.
    live_stalls = active_stall_diagnostics(ref.run_dir)
    continuation_decision = resolve_continuation_decision(
        run_id=ref.run_id,
        meta=raw_meta or None,
        parent_run_dir=ref.run_dir,
    )
    next_actions = compute_next_actions(
        raw_meta or None,
        run_id=ref.run_id,
        live_stall_diagnostics=live_stalls,
        has_parsed_plan_artifact=any(
            artifact.kind == "parsed_plan" for artifact in artefacts
        ),
        status=merged_status_value,
        continuation_decision=continuation_decision,
    )

    return RunStatus(
        run_ref=ref,
        meta=meta,
        total_tokens=int(raw_metrics.get("total_tokens", 0) or 0),
        total_tokens_in=int(raw_metrics.get("total_tokens_in", 0) or 0),
        total_tokens_out=int(raw_metrics.get("total_tokens_out", 0) or 0),
        total_duration_s=float(raw_metrics.get("total_duration_s", 0.0) or 0.0),
        total_rounds=int(raw_metrics.get("total_rounds", 0) or 0),
        total_retries=int(raw_metrics.get("total_retries", 0) or 0),
        sub_projects=tuple(sub_projects),
        quality_gates=quality_gates,
        worktree=raw_meta.get("worktree") if raw_meta else None,
        raw_meta=raw_meta,
        raw_metrics=raw_metrics,
        next_actions=next_actions,
        continuation_decision=continuation_decision,
        artefacts=artefacts,
    )
