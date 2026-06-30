"""Cross-project terminal-status finalization (ADR 0037).

Early-return terminals in :mod:`pipeline.cross_project.orchestrator`
(today: the operator-``ABORT`` branch on ``contract_check``'s
``manual_confirm`` gate) call :func:`finalize_cross_terminal` to satisfy
the ADR 0037 invariant: every terminal run leaves a synchronous
``meta.json`` with a terminal ``status`` (+ ``halt_reason`` when not
``done``) and an ``evidence.json``/``evidence.md`` pair on disk.

The non-early-return terminals at the end of ``run_cross_pipeline``
still finalize inline because they additionally write a per-alias
``metrics.json`` rollup that early-return terminals have no data for —
routing those through a single helper would dilute the rollup honesty.
"""

from __future__ import annotations

from pathlib import Path

from pipeline.engine import save_session as save_cross_session
from pipeline.run_state.terminal import settle_cross_terminal


def finalize_cross_terminal(
    *,
    run_dir: Path | None,
    session: dict,
    status: str,
    halt_reason: str | None = None,
    cross_ckpt: dict | None = None,
) -> None:
    """Persist a terminal status for a cross run.

    Steps:

    1. Field-mutate ``session`` via
       :func:`pipeline.run_state.terminal.settle_cross_terminal` — sets
       ``status``, stamps ``halt_reason`` when ``status != "done"`` and the
       caller did not pre-set one, and **clears any stale active**
       ``session["phase_handoff"]`` for every in-scope cross terminal
       (``halted`` / ``cancelled`` / ``failed`` / ``done``). A cross pause
       (set by ``apply_cross_phase_handoff_pause``) short-circuits the run to
       a final terminal, so any payload still present here is stale —
       unlike single-project ``failed`` / ``interrupted`` which preserve it.
       Caller-supplied reasons match the ADR 0035 taxonomy; the
       ``cross_<status>`` fallback exists so future terminals that forget to
       pass one still honour the invariant. When ``cross_ckpt`` is threaded,
       the settle also evicts the settle-only ``pending_gate`` residue from
       both the session mirror and the checkpoint copy — this is the single
       settle-only clearing point for that gate-decision key (ADR 0115
       slice 4); the persist in step 2b writes the cleaned checkpoint.
    2. Writes ``meta.json`` via :func:`save_cross_session` so the new
       field hits disk synchronously.
    2b. Persists the (pending_gate-cleared) ``cross_ckpt`` via
       :func:`write_cross_checkpoint` when one was threaded.
    3. Writes ``evidence.json`` + ``evidence.md`` via
       :func:`pipeline.evidence.write_bundle_or_placeholder`. Best-effort:
       a writer failure does not invalidate the ``meta.halt_reason``
       guarantee already satisfied above. Single-run finalize uses the
       same suppression posture.

    ``run_dir is None`` short-circuits the disk writes — in-memory tests
    still observe the session + checkpoint mutation.
    """
    effective_reason: str | None = None
    if status != "done" and not session.get("halt_reason"):
        effective_reason = (
            halt_reason if halt_reason is not None else f"cross_{status}"
        )
    settle_cross_terminal(
        session,
        status=status,
        halt_reason=effective_reason,
        cross_ckpt=cross_ckpt,
    )
    if run_dir is None:
        return
    save_cross_session(run_dir, session)
    if cross_ckpt is not None:
        from pipeline.cross_project.checkpoint import write_cross_checkpoint
        write_cross_checkpoint(run_dir, cross_ckpt)
    try:
        from pipeline.evidence import write_bundle_or_placeholder
        write_bundle_or_placeholder(
            run_dir,
            run_id=session.get("run_id") or run_dir.name,
            status=status,
        )
    except Exception:  # noqa: BLE001
        # Evidence write is best-effort; meta.halt_reason already
        # persisted above. See module docstring for rationale.
        pass
