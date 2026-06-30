"""gate_repair.py — Stage 4 hook+action router over the ScheduledGatePlan.

Consumes the resolved :class:`~pipeline.verification_selection.ScheduledGatePlan`
(T2) and routes failed *required* gates per hook and effective action — it never
recomputes selection. Only ``require``-policy gates participate in routing here
(``off`` / ``suggest`` / ``warn`` never block — they are surfaced read-only in
the per-phase prompt blocks, not executed as gates).

Action routing (read from the plan entry's effective ``action``):

* ``continue_warn`` — log a warning, do not block.
* ``abort`` — halt the run (``state.stop`` + ``mark_run_halted``).
* ``handoff`` — request a phase handoff (pause).
* ``repair_loop`` — governed by the deterministic
  :func:`~pipeline.verification_selection.repair_loop_target` matrix: it is a
  real repair flow ONLY for ``after_phase(implement)`` (and only when the profile
  has a ``repair_changes`` step); every other hook deterministically degrades to
  ``handoff`` with a logged note.

The ``after_phase(implement)`` repair flow is the token-minimising critical path
(ADR 0081): the failed command output *is* the critique, ``repair_changes`` is
dispatched WITHOUT a reviewer pass, and a re-execution of the same gate command
is the exit condition; budget exhaustion escalates to a handoff.

Hooks: ``before_phase`` (before entering a phase), ``after_phase`` (after a
phase; ``implement`` is the critical one), ``before_delivery`` (before the
FINAL_PHASES delivery boundary; blocks delivery only on a require gate whose
receipt is failed/missing/stale), ``on_resume`` (resume path). ``manual_only`` is
never auto-planned.

``dispatch_via_v2_profile`` calls the hook entry points as contract-gated hooks;
with no declared contract every entry point is a pure no-op so the no-contract
dispatch path stays byte-identical. Subprocess / FSM boundaries live behind the
``_run_gate_command`` / ``_dispatch_repair`` / ``_repair_step`` seams so unit
tests can drive routing without a real agent or worktree.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline.verification_selection import (
    SelectionContext,
    build_scheduled_gate_plan,
    repair_loop_target,
    selection_context_from_extras,
)

#: Append-only durable trail of per-scheduled-gate routing decisions (T1). One
#: entry per gate per hook firing — ``executed_pass`` / ``executed_fail`` written
#: inline at execution, ``skipped_fresh`` / ``skipped_manual`` written by the
#: per-hook reconciliation pass. Purely observational: the recorder never changes
#: which commands routing executes, nor the :class:`GateRepairOutcome`.
VERIFICATION_GATE_EVENTS_KEY = "verification_gate_events"


@dataclass(frozen=True)
class GateRepairOutcome:
    """Result of evaluating one scheduled-gate hook.

    ``active`` is ``False`` when the hook did not run (no contract, or no failing
    required gate for that hook). When ``active`` is ``True`` at most one of
    ``passed`` / ``paused`` / ``halted`` describes the terminal disposition
    (``passed`` also covers non-blocking ``continue_warn`` failures). ``rounds``
    counts repair rounds dispatched.
    """

    active: bool
    passed: bool = False
    paused: bool = False
    halted: bool = False
    rounds: int = 0


def run_gate_hook(
    run: Any, profile: Any, ctx: Any, *, hook: str, phase: str = "",
) -> GateRepairOutcome:
    """Evaluate the required gates scheduled for ``hook`` (+ ``phase``) and route.

    Returns a no-op (``active=False``) when no contract is declared, the hook is
    ``manual_only`` (never auto-planned), or no required gate is scheduled for
    this hook. Otherwise runs each required gate command and, on the first
    failure that yields a blocking disposition, returns it.
    """
    contract = _contract(run)
    if contract is None or hook == "manual_only":
        return GateRepairOutcome(active=False)

    plan = _plan(run, contract, epoch=_selection_epoch(hook, phase))
    gates = _gates_for_hook(plan, hook=hook, phase=phase)

    # ``executed`` accumulates the commands routing actually ran this hook so the
    # reconciliation pass can tell apart "ran" from "planned but not run".
    executed: set[str] = set()
    for entry in gates:
        receipt = _run_gate_command(run, contract, entry)
        executed.add(entry.command)
        _record_executed_gate_event(run, entry, receipt, hook=hook, phase=phase)
        if _passed(receipt):
            continue
        disposition = _route_failed_gate(
            run, profile, ctx, contract, entry, receipt, hook=hook, phase=phase,
        )
        if disposition is not None:
            _reconcile_skipped_gate_events(
                run, contract, plan, hook=hook, phase=phase, executed=executed,
            )
            return disposition

    _reconcile_skipped_gate_events(
        run, contract, plan, hook=hook, phase=phase, executed=executed,
    )
    if not gates:
        return GateRepairOutcome(active=False)
    return GateRepairOutcome(active=True, passed=True)


def run_post_implement_gate_repair(
    run: Any, profile: Any, ctx: Any,
) -> GateRepairOutcome:
    """The critical ``after_phase(implement)`` hook (ADR 0081 critical flow)."""
    return run_gate_hook(run, profile, ctx, hook="after_phase", phase="implement")


# ── per-phase dispatch callbacks (wired into run_profile via the run) ────────
#
# ``evaluate_pre_phase_gates`` is the ``on_phase_pre`` seam: it fires BEFORE a
# phase handler runs (the only point that can pre-empt a phase). It evaluates the
# ``before_phase`` gate for the phase, and — when the phase is a delivery
# boundary (FINAL_PHASES) — the ``before_delivery`` gate. ``evaluate_post_phase_gates``
# is the ``on_phase_end`` seam for ``after_phase`` gates (``implement`` is the
# critical repair flow). Both are no-ops without a declared contract, and both
# guard against re-entrancy: the ``after_phase(implement)`` repair flow dispatches
# ``repair_changes`` (which re-fires the phase callbacks), so a ``_in_gate_hook``
# flag stops nested gate evaluation.


def evaluate_pre_phase_gates(run: Any, phase: str) -> None:
    """``on_phase_pre``: before_phase(phase) + before_delivery on FINAL_PHASES."""
    if not _gate_active(run):
        return
    from pipeline.verification_contract import FINAL_PHASES

    run._in_gate_hook = True
    try:
        run_gate_hook(
            run, run._gate_profile, run._gate_ctx,
            hook="before_phase", phase=phase,
        )
        if getattr(run.state, "halt", False) or run.state.phase_handoff_request:
            return
        if phase in FINAL_PHASES:
            run_gate_hook(
                run, run._gate_profile, run._gate_ctx, hook="before_delivery",
            )
    finally:
        run._in_gate_hook = False


# ── isolated-source provenance preflight (ADR 0112 §3) ───────────────────────
#
# A before_phase(implement) fail-fast: when the run owns an isolated per-run
# worktree, abort BEFORE the implement/review cycle is spent if the verify source
# is not bound to that worktree (T1 resolver sees the source resolve to the
# canonical sibling / unresolved) or the env-provenance assertion fails against
# the wrong tree (T2). It reuses the existing provenance machinery
# (:func:`pipeline.engine.worktree_source.resolve_isolated_repo_source` and
# :func:`pipeline.evidence.verification_receipt.collect_environment_checks`) — no
# new phase/gate primitive — and is a strict no-op for single-checkout runs and
# every phase other than ``implement``.

#: Observational note kind recorded when the preflight aborts a run.
ISOLATED_SOURCE_PREFLIGHT_NOTE = "isolated_source_preflight_abort"
#: Halt reason stamped on the run when the preflight fails.
ISOLATED_SOURCE_PREFLIGHT_HALT = "isolated_source_provenance_preflight"


def evaluate_isolated_source_preflight(run: Any, phase: str) -> None:
    """``on_phase_pre``: provenance preflight before ``implement`` (ADR 0112 §3).

    Active ONLY when the run owns an isolated per-run worktree. Aborts the run
    (``state.stop`` + ``mark_run_halted``) before the ``implement`` handler runs
    when the isolated repo's verify source is unbindable to its worktree or its
    declared env-provenance assertion fails against the canonical sibling. A
    single-checkout run (no isolated worktree) is never given this interrupting
    gate, and the preflight is a no-op for every phase but ``implement``.
    """
    if phase != "implement":
        return
    if getattr(run, "_in_gate_hook", False):
        return
    isolated = _isolated_source_for_run(run)
    # Activate on ``is_declared`` (isolation is in force), NOT ``is_isolated`` (a
    # usable worktree path): a declared-but-unbound isolated repo must reach the
    # fail-closed resolver below and abort, not slip past as a single-checkout run
    # (ADR 0112 §3). Single-checkout runs have no declared isolation and return.
    if isolated is None or not getattr(isolated, "is_declared", False):
        return
    reason = _isolated_source_preflight_failure(run, isolated)
    if reason is None:
        return
    _abort_isolated_source_preflight(run, reason)


def _isolated_source_for_run(run: Any) -> Any:
    """Resolve the run's isolated source: the verify ctx's ``isolated_source``
    (set by ``placeholder_context_for``), else re-seeded from the persisted
    ``meta.worktree`` block. ``None`` for a single-checkout run.

    The durable/resume fallback rebuilds the run's one-participant
    :class:`pipeline.participants.ParticipantSet` from ``meta.worktree`` (the
    durable source) and reads the isolated source from it — the same ParticipantSet
    derivation :func:`...verification_contract.placeholder_context_for` uses, not a
    parallel ``isolated_source_from_meta`` path."""
    ctx = _placeholders(run)
    isolated = getattr(ctx, "isolated_source", None)
    if isolated is not None:
        return isolated
    from pipeline.participants import ParticipantSet

    session = getattr(run, "session", None)
    worktree = session.get("worktree") if isinstance(session, dict) else None
    participants = ParticipantSet.for_mono(checkout="", project="", worktree=worktree)
    return participants.isolated_source_for(next(iter(participants)))


def _isolated_source_preflight_failure(run: Any, isolated: Any) -> str | None:
    """Return an abort reason when the isolated source/provenance is wrong, else
    ``None``. Reuses the T1 resolver and the T2 env-provenance probe; never
    false-aborts on a probe error (degrades to ``None``)."""
    # (a) T1 resolver — the isolated repo's source must bind to its worktree, not
    # the canonical sibling. A degenerate (sibling-pointing / unresolved) worktree
    # raises IsolatedSourceError here.
    from pipeline.engine.worktree_source import (
        IsolatedSourceError,
        resolve_isolated_repo_source,
    )

    repo_name = Path(isolated.source_repo_path).name or "isolated-repo"
    try:
        resolve_isolated_repo_source(
            repo_name=repo_name,
            candidate=isolated.source_repo_path,
            isolated=isolated,
        )
    except IsolatedSourceError as exc:
        return f"isolated source could not be bound before implement: {exc}"

    # (b) T2 env-provenance — the declared assertions (or the core import
    # invariant) must prove the worktree tree, not the sibling. Run the probe
    # against the worktree checkout; a failing check fails the preflight.
    cwd = isolated.worktree_path or _preflight_cwd(run)
    if not cwd:
        return None
    try:
        from pipeline.evidence.verification_receipt import collect_environment_checks

        checks, _commands = collect_environment_checks(
            cwd, contract=_contract(run), ctx=_placeholders(run),
        )
    except Exception:  # noqa: BLE001 — preflight must never false-abort on probe error
        return None
    failed = next(
        (c for c in checks if isinstance(c, dict) and not c.get("passed")),
        None,
    )
    if failed is None:
        return None
    return (
        f"environment provenance failed before implement for isolated worktree "
        f"{cwd}: {failed.get('name')}: expected {failed.get('expected')!r} "
        f"actual {failed.get('actual')!r}"
    )


def _preflight_cwd(run: Any) -> str:
    """Worktree checkout the preflight probe runs in (``git_cwd`` fallback)."""
    state = getattr(run, "state", None)
    if state is None:
        return ""
    return str(state.extras.get("git_cwd") or getattr(state, "project_dir", "") or "")


def _abort_isolated_source_preflight(run: Any, reason: str) -> None:
    """Halt the run before ``implement`` on a failed isolated-source preflight."""
    from pipeline.run_state.terminal import mark_run_halted

    run.state.stop(f"{ISOLATED_SOURCE_PREFLIGHT_HALT}: {reason}")
    session = getattr(run, "session", None)
    if isinstance(session, dict):
        mark_run_halted(session, halt_reason=ISOLATED_SOURCE_PREFLIGHT_HALT)
    state = getattr(run, "state", None)
    if state is not None:
        notes = state.extras.setdefault("gate_repair_notes", [])
        notes.append({
            "kind": ISOLATED_SOURCE_PREFLIGHT_NOTE,
            "command": "",
            "detail": reason,
        })


def evaluate_post_phase_gates(run: Any, phase: str) -> None:
    """``on_phase_end``: the after_phase(phase) gate (implement is critical)."""
    if not _gate_active(run):
        return
    run._in_gate_hook = True
    try:
        run_gate_hook(
            run, run._gate_profile, run._gate_ctx,
            hook="after_phase", phase=phase,
        )
    finally:
        run._in_gate_hook = False


def _gate_active(run: Any) -> bool:
    """Whether the contract-gated per-phase hooks should run for this callback."""
    if getattr(run, "_in_gate_hook", False):
        return False
    if getattr(run, "_gate_profile", None) is None:
        return False
    return _contract(run) is not None


def arm_gate_context(run: Any, profile: Any, ctx: Any) -> None:
    """Stash the resolved profile + ctx so the per-phase gate hooks can fire.

    The ``on_phase_pre`` / ``on_phase_end`` gate seams build the executable gate
    plan from ``run._gate_profile`` / ``run._gate_ctx`` and short-circuit when
    ``_gate_profile`` is unset (see :func:`_gate_active`). EVERY dispatch that
    wires ``run._on_phase_end`` — the fresh dispatch in ``profile_dispatch`` AND
    the resume re-dispatch in ``handoff.process_pending_phase_handoffs`` — must
    arm this context first. A resume process starts with ``_gate_profile=None``
    (the ``_PipelineRun`` default); if the resume path forgets to arm it, the
    gate hooks go inert and the resumed run silently skips its post-implement
    verification gates, so their required receipts are never materialized and the
    delivery gate then blocks the (actually green) run on "missing required
    receipts". Single source so the two dispatch entries cannot drift.
    """
    run._gate_profile = profile
    run._gate_ctx = ctx


def disarm_gate_context(run: Any) -> None:
    """Clear the per-phase gate context once a dispatch completes."""
    run._gate_profile = None
    run._gate_ctx = None


# ── routing ─────────────────────────────────────────────────────────────────


def _route_failed_gate(
    run: Any, profile: Any, ctx: Any, contract: Any, entry: Any, receipt: dict,
    *, hook: str, phase: str,
) -> GateRepairOutcome | None:
    """Route one failed required gate; ``None`` means non-blocking (warned)."""
    if entry.policy != "require":
        # off / suggest / warn never block — surfaced read-only, not gated here.
        _warn_gate(run, entry, receipt, note="non-require gate failed")
        return None

    action = entry.action
    if action == "continue_warn":
        _warn_gate(run, entry, receipt, note="continue_warn")
        return None
    if action == "abort":
        _abort(run, entry, receipt)
        return GateRepairOutcome(active=True, halted=True)
    if action == "handoff":
        _synthesize_critique(run.state, entry, receipt)
        _request_handoff(run, entry, receipt, 1, 1, phase=_handoff_phase(hook, phase))
        return GateRepairOutcome(active=True, paused=True)
    if action == "repair_loop":
        if repair_loop_target(hook, phase) == "repair" and _repair_step(profile) is not None:
            return _repair_loop(
                run, profile, ctx, contract, entry, receipt, hook=hook, phase=phase,
            )
        # Deterministic degradation: repair_loop is unsupported off the
        # after_phase(implement) critical path (or no repair_changes step) — fall
        # back to handoff with a logged note.
        _record_repair_fallback(run, entry, hook, phase)
        _synthesize_critique(run.state, entry, receipt)
        _request_handoff(run, entry, receipt, 1, 1, phase=_handoff_phase(hook, phase))
        return GateRepairOutcome(active=True, paused=True)

    # Unknown action — treat as non-blocking warning.
    _warn_gate(run, entry, receipt, note=f"unknown action {action!r}")
    return None


def _repair_loop(
    run: Any, profile: Any, ctx: Any, contract: Any, entry: Any, receipt: dict,
    *, hook: str, phase: str,
) -> GateRepairOutcome:
    """Drive repair_changes -> gate re-check rounds up to the repair budget."""
    repair_step = _repair_step(profile)
    max_rounds = _repair_budget(run, profile)
    round_n = 0
    while True:
        round_n += 1
        _synthesize_critique(run.state, entry, receipt)
        if repair_step is None:
            _request_handoff(run, entry, receipt, round_n, max_rounds,
                             phase=_handoff_phase(hook, phase))
            return GateRepairOutcome(active=True, paused=True, rounds=round_n)

        _dispatch_repair(run, repair_step, ctx, round_n=round_n, max_rounds=max_rounds)
        if getattr(run.state, "halt", False):
            return GateRepairOutcome(active=True, halted=True, rounds=round_n)

        receipt = _run_gate_command(run, contract, entry)
        # Append-only: a repair-round recheck is a real execution of the official
        # gate command on this hook, so its executed_pass/executed_fail must land
        # in the durable trail too (the recheck pass is what flips the outcome).
        _record_executed_gate_event(run, entry, receipt, hook=hook, phase=phase)
        if _passed(receipt):
            return GateRepairOutcome(active=True, passed=True, rounds=round_n)
        if round_n >= max_rounds:
            _synthesize_critique(run.state, entry, receipt)
            _request_handoff(run, entry, receipt, round_n, max_rounds,
                             phase=_handoff_phase(hook, phase))
            return GateRepairOutcome(active=True, paused=True, rounds=round_n)


# ── gate selection (consume the plan; never recompute) ──────────────────────


def _gates_for_hook(plan: Any, *, hook: str, phase: str) -> list[Any]:
    """Required gate entries scheduled for ``hook`` (+ ``phase``), in plan order.

    Only ``require``-policy entries participate in routing — ``off`` / ``suggest``
    / ``warn`` are non-blocking and never executed as gates here. The phase is
    read directly off the resolved entry (Stage 4 keeps it in the gate identity),
    so phase-scoped hooks never collapse across phases.
    """
    gates: list[Any] = []
    for entry in plan.entries:
        if entry.hook != hook or entry.policy != "require":
            continue
        if hook in ("before_phase", "after_phase") and entry.phase != phase:
            continue
        gates.append(entry)
    return gates


# ── command execution + critique ───────────────────────────────────────────


def _run_gate_command(run: Any, contract: Any, entry: Any) -> dict:
    """Execute one gate command and return its receipt (monkeypatch seam).

    The receipt is also persisted under
    ``<run_dir>/verification_command_receipts/`` so the Stage 5 readiness
    block, the Stage 6 delivery gate, and the evidence bundle see the same
    proof the routing decision was based on. Re-runs (repair rounds) overwrite
    by command name — the latest execution is the authoritative receipt.
    """
    from pipeline.verification_command import run_command

    spec = contract.commands.get(entry.command, {})
    receipt = run_command(
        entry.command,
        spec,
        contract,
        _placeholders(run),
        required=True,
    )
    _persist_gate_receipt(run, receipt)
    return receipt


def _persist_gate_receipt(run: Any, receipt: dict) -> None:
    """Write the command receipt under the run dir; never raises."""
    output_dir = getattr(getattr(run, "state", None), "output_dir", None)
    from contextlib import suppress

    with suppress(Exception):
        from pipeline.evidence.verification_receipt import write_command_receipt

        write_command_receipt(output_dir=output_dir, result=receipt)


def _passed(receipt: dict) -> bool:
    """Authoritative pass rollup for a scheduled gate's receipt.

    Delegates to :func:`pipeline.evidence.verification_receipt.command_receipt_passed`
    so ``executed_pass`` / ``executed_fail`` are stamped on the same rollup
    readiness and delivery enforce: exit 0 AND every assertion passed AND an empty
    ``detail``. An exit-0 receipt with a failed assertion or a non-empty detail is
    ``executed_fail``, never a false-green ``executed_pass``.
    """
    from pipeline.evidence.verification_receipt import command_receipt_passed

    return command_receipt_passed(receipt)


# ── gate-event recorder (append-only, observational) ─────────────────────────
#
# A thin durable trail of per-scheduled-gate routing decisions, appended to
# ``state.extras[VERIFICATION_GATE_EVENTS_KEY]``. It records what routing DID —
# it never decides what routing does. ``executed_pass`` / ``executed_fail`` are
# stamped inline the moment a gate command runs (proof the hook executed it);
# ``skipped_fresh`` / ``skipped_manual`` are stamped by a per-hook reconciliation
# over the plan's entries for this hook+phase that routing did not execute. The
# reconciliation reads only durable facts (policy + receipt freshness via the
# read-only readiness classification) and is fully tolerant of stub runs.


def _append_gate_event(run: Any, event: dict) -> None:
    """Append one routing-decision event; no-op on a stub/odd run state."""
    extras = getattr(getattr(run, "state", None), "extras", None)
    if not isinstance(extras, dict):
        return
    trail = extras.setdefault(VERIFICATION_GATE_EVENTS_KEY, [])
    if isinstance(trail, list):
        trail.append(event)


def _gate_event(
    hook: str, phase: str, entry: Any, *, decision: str,
    exit_code: int | None = None, receipt_path: str | None = None,
) -> dict:
    """Build one append-only gate-event record for ``entry``."""
    return {
        "hook": hook,
        "phase": phase,
        "command": entry.command,
        "gate_set": entry.primary_gate_set,
        "decision": decision,
        "exit_code": exit_code,
        "receipt_path": receipt_path,
    }


def _record_executed_gate_event(
    run: Any, entry: Any, receipt: dict, *, hook: str, phase: str,
) -> None:
    """Stamp the ``executed_pass`` / ``executed_fail`` event for a run gate."""
    decision = "executed_pass" if _passed(receipt) else "executed_fail"
    output_dir = getattr(getattr(run, "state", None), "output_dir", None)
    receipt_path: str | None = None
    if output_dir is not None:
        from pipeline.evidence.verification_receipt import COMMAND_RECEIPTS_DIRNAME

        receipt_path = str(
            Path(output_dir) / COMMAND_RECEIPTS_DIRNAME / f"{entry.command}.json",
        )
    _append_gate_event(
        run,
        _gate_event(
            hook, phase, entry, decision=decision,
            exit_code=receipt.get("exit_code"), receipt_path=receipt_path,
        ),
    )


def _skip_decision_for(
    command: str, *, manual_set: set[str], fresh_set: set[str],
) -> str | None:
    """Durable skip decision for a planned-but-not-executed gate, or ``None``.

    ``skipped_manual`` when the command is withheld as ``manual_only`` or parked
    behind an unrequested operator gate-set; otherwise ``skipped_fresh`` when a
    fresh present receipt already exists. A ``missing``/``stale`` required command
    yields ``None`` — that stays a run-level residual, never a hook-skip event.
    """
    if command in manual_set:
        return "skipped_manual"
    if command in fresh_set:
        return "skipped_fresh"
    return None


def _manual_or_operator_set(contract: Any) -> set[str]:
    """Commands withheld as manual/operator-only (read-only; never raises)."""
    from contextlib import suppress

    with suppress(Exception):
        from sdk.verify import manual_or_operator_only_commands

        return manual_or_operator_only_commands(contract)
    return set()


def _fresh_required_commands(run: Any, contract: Any) -> set[str]:
    """Required commands whose on-disk receipt classifies ``present`` (read-only).

    Uses the same :func:`classify_required_receipts` readiness/delivery rely on,
    so freshness is decided by durable facts, never transcript heuristics. No-op
    (empty) when the run lacks an output dir or placeholder context, or whenever
    classification raises — the reconciler then records no ``skipped_fresh``.
    """
    state = getattr(run, "state", None)
    output_dir = getattr(state, "output_dir", None)
    extras = getattr(state, "extras", None)
    if output_dir is None or not isinstance(extras, dict):
        return set()
    ctx = extras.get("verification_placeholders")
    if ctx is None:
        return set()
    from contextlib import suppress

    with suppress(Exception):
        from pipeline.verification_readiness import classify_required_receipts

        classification = classify_required_receipts(
            contract, output_dir, ctx,
            checkout=getattr(ctx, "checkout", "") or "",
            extras=extras,
        )
        return {c for c, cl in classification.items() if cl.status == "present"}
    return set()


def _reconcile_skipped_gate_events(
    run: Any, contract: Any, plan: Any, *, hook: str, phase: str,
    executed: set[str],
) -> None:
    """Append skip events for gates planned at this hook but not executed.

    Purely observational: it never reruns or suppresses a command. For each plan
    entry scheduled at ``hook`` (+ ``phase`` for the phase-anchored hooks) whose
    command routing did not execute, it records the durable skip decision from
    :func:`_skip_decision_for`. Tolerant of stub runs (missing extras/state ->
    no-op) and of an empty pending set (no classification work).
    """
    extras = getattr(getattr(run, "state", None), "extras", None)
    if not isinstance(extras, dict):
        return
    pending = [
        entry
        for entry in plan.entries
        if entry.hook == hook
        and (hook not in ("before_phase", "after_phase") or entry.phase == phase)
        and entry.command not in executed
    ]
    if not pending:
        return

    manual_set = _manual_or_operator_set(contract)
    fresh_set = _fresh_required_commands(run, contract)
    seen: set[str] = set(executed)
    for entry in pending:
        if entry.command in seen:
            continue
        decision = _skip_decision_for(
            entry.command, manual_set=manual_set, fresh_set=fresh_set,
        )
        if decision is None:
            continue
        seen.add(entry.command)
        _append_gate_event(run, _gate_event(hook, phase, entry, decision=decision))


def _synthesize_critique(state: Any, entry: Any, receipt: dict) -> None:
    """Write the failed command output into ``state.last_critique`` / output."""
    stdout = receipt.get("stdout_tail") or ""
    stderr = receipt.get("stderr_tail") or ""
    detail = receipt.get("detail") or ""
    state.last_test_output = "\n".join(p for p in (stdout, stderr) if p)
    parts = [
        "Required verification gate failed.",
        f"Gate set: {entry.primary_gate_set}",
        f"Command: {entry.command}",
    ]
    if detail:
        parts.append(f"Detail: {detail}")
    if stderr:
        parts.append(f"stderr:\n{stderr}")
    if stdout:
        parts.append(f"stdout:\n{stdout}")
    state.last_critique = "\n".join(parts)


def _warn_gate(run: Any, entry: Any, receipt: dict, *, note: str) -> None:
    """Surface a non-blocking gate failure as a warning + a recorded note."""
    _record_note(run, "warn", entry=entry, detail=note)
    from contextlib import suppress

    with suppress(Exception):
        from core.observability.logging import warn

        warn(
            f"Verification gate {entry.command!r} <{entry.primary_gate_set}> "
            f"failed (non-blocking: {note}).",
        )


# ── repair dispatch + handoff/abort ─────────────────────────────────────────


def _repair_step(profile: Any) -> Any:
    """The first ``repair_changes`` PhaseStep in the profile, or ``None``."""
    from pipeline.project.profile_dispatch import first_phase_step

    return first_phase_step(profile, "repair_changes")


def _dispatch_repair(
    run: Any, repair_step: Any, ctx: Any, *, round_n: int, max_rounds: int,
) -> None:
    """Dispatch one ``repair_changes`` round through the lifecycle FSM."""
    from pipeline.runtime.runner import _dispatch_via_fsm

    run.state.extras["_active_loop_round_key"] = "repair_round"
    run.state.extras["repair_round"] = round_n
    run.state.extras["repair_round_max"] = max_rounds
    run.state = _dispatch_via_fsm(
        repair_step,
        run.state,
        ctx,
        on_phase_start=getattr(run, "_on_phase_start", None),
        on_phase_end=getattr(run, "_on_phase_end", None),
    )


def _handoff_phase(hook: str, phase: str) -> str:
    """Non-empty phase label for the handoff signal (FINAL phase for delivery)."""
    if phase:
        return phase
    if hook == "before_delivery":
        return "final_acceptance"
    return "implement"


def _request_handoff(
    run: Any, entry: Any, receipt: dict, round_n: int, max_rounds: int,
    *, phase: str,
) -> None:
    """Stash a phase-handoff signal so the caller persists the pause."""
    from pipeline.runtime.handoff import PhaseHandoffRequested
    from pipeline.runtime.roles import PhaseHandoffAction, PhaseHandoffType

    last_output = getattr(run.state, "last_critique", "") or ""
    signal = PhaseHandoffRequested(
        handoff_id=f"gate:{entry.command}:{round_n}",
        phase=phase,
        type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
        trigger="verification_gate_failed",
        verdict="REJECTED",
        approved=False,
        round_extras_key="repair_round",
        round=max(1, round_n),
        loop_max_rounds=max(1, max_rounds),
        available_actions=(
            PhaseHandoffAction.CONTINUE.value,
            PhaseHandoffAction.RETRY_FEEDBACK.value,
            PhaseHandoffAction.HALT.value,
            PhaseHandoffAction.CONTINUE_WITH_WAIVER.value,
        ),
        artifacts={
            "gate_command": entry.command,
            "gate_set": entry.primary_gate_set,
        },
        last_output=last_output,
    )
    run.state.phase_handoff_request = signal
    run.state.stop(f"phase handoff requested: {signal.handoff_id}")


def _abort(run: Any, entry: Any, receipt: dict) -> None:
    """Halt the run on an explicit ``abort`` gate action."""
    from pipeline.run_state.terminal import mark_run_halted

    reason = f"verification_gate_abort:{entry.command}"
    run.state.stop(reason)
    session = getattr(run, "session", None)
    if isinstance(session, dict):
        mark_run_halted(session, halt_reason=reason)


def _record_repair_fallback(run: Any, entry: Any, hook: str, phase: str) -> None:
    """Record the deterministic repair_loop -> handoff degradation."""
    _record_note(run, "repair_loop_fallback", entry=entry, detail=f"{hook}/{phase}")
    from contextlib import suppress

    with suppress(Exception):
        from core.observability.logging import warn

        warn(
            f"repair_loop unsupported for hook {hook!r} (phase {phase!r}); "
            f"gate {entry.command!r} degrades to handoff.",
        )


def _record_note(run: Any, kind: str, *, entry: Any, detail: str) -> None:
    state = getattr(run, "state", None)
    if state is None:
        return
    notes = state.extras.setdefault("gate_repair_notes", [])
    notes.append({"kind": kind, "command": entry.command, "detail": detail})


# ── context helpers ─────────────────────────────────────────────────────────


def _contract(run: Any) -> Any:
    state = getattr(run, "state", None)
    if state is None:
        return None
    return state.extras.get("verification_contract")


def _placeholders(run: Any) -> Any:
    state = getattr(run, "state", None)
    ph = state.extras.get("verification_placeholders") if state is not None else None
    if ph is not None:
        return ph
    from pipeline.verification_contract import PlaceholderContext

    return PlaceholderContext()


#: The executable routing plans cache — a dict keyed by **selection epoch**
#: (the ``hook:phase`` lifecycle position), distinct from the prompt-preview key
#: (``pipeline.phases.builtin.prompt_parts._GATE_PROMPT_PREVIEW_KEY``). Routing
#: NEVER reads the prompt preview. Phase-position keying is the lifecycle fix: a
#: plan built at any hook *before* ``implement`` (empty changed files) must never
#: be reused for ``after_phase(implement)``, whose path-based subsystem selection
#: depends on the *post-implement* changed files.
VERIFICATION_GATE_ROUTING_PLANS_KEY = "verification_gate_routing_plans"


def _selection_epoch(hook: str, phase: str) -> str:
    """Selection epoch (cache key) for a hook — keyed by lifecycle *position*.

    The key includes both the hook and the phase so the cache reflects *when* in
    the run the plan is built, not merely which hook fired. This matters because
    ``after_phase`` fires after **every** phase: ``after_phase(plan)`` /
    ``after_phase(validate_plan)`` run *before* ``implement`` (no implement
    changes yet), while ``after_phase(implement)`` runs *after* and must see the
    post-implement changed files for path-based subsystem selection. Distinct
    ``hook:phase`` keys guarantee an early ``after_phase(plan)`` plan is never
    reused for ``after_phase(implement)``. Phaseless hooks
    (``before_delivery`` / ``on_resume``) get their own bucket; an
    ``after_phase`` with no known phase gets its own (``after_phase:``) bucket and
    so never collides with ``after_phase:implement``.
    """
    return f"{hook}:{phase}"


def _plan(run: Any, contract: Any, *, epoch: str) -> Any:
    """Return the executable routing ``ScheduledGatePlan`` for ``epoch``.

    Built **once per epoch** (``hook:phase`` position) at the routing point and
    cached under ``state.extras[VERIFICATION_GATE_ROUTING_PLANS_KEY][epoch]``;
    later invocations at the same position reuse it (deterministic, no per-hook
    recompute), while a different position (e.g. ``after_phase:implement`` vs an
    earlier ``after_phase:plan``) builds its own plan from the changed files
    current at that point. Routing never reads the prompt preview.
    """
    state = run.state
    plans = state.extras.setdefault(VERIFICATION_GATE_ROUTING_PLANS_KEY, {})
    cached = plans.get(epoch)
    if cached is not None:
        return cached
    plan = build_scheduled_gate_plan(contract, _selection_context(run, contract))
    plans[epoch] = plan
    return plan


def _selection_context(run: Any, contract: Any) -> SelectionContext:
    """Build the selection context for executable routing.

    ``touched_paths`` are the run worktree's current changed files (best-effort,
    computed fresh per hook). ``task_kind`` / ``operator_sets`` are the
    operator/profile declarations threaded through ``state.extras`` (the stable
    run-level source — keys ``verification_task_kind`` /
    ``verification_operator_sets``); auto-inference of ``task_kind`` is a TODO.
    """
    touched: tuple[str, ...] = ()
    ph = _placeholders(run)
    checkout = getattr(ph, "checkout", "") or ""
    if checkout:
        try:
            from core.io.git_helpers import git_changed_files

            touched = tuple(git_changed_files(checkout))
        except Exception:  # noqa: BLE001 — selection touched-paths is best-effort
            touched = ()
    state = getattr(run, "state", None)
    extras = getattr(state, "extras", {}) or {}
    return selection_context_from_extras(extras, contract, touched_paths=touched)


def _repair_budget(run: Any, profile: Any) -> int:
    """Repair-round budget: runtime ``--max-rounds`` wins, else profile loop."""
    runtime = getattr(run, "max_rounds", 0) or 0
    if runtime > 0:
        return runtime
    from pipeline.project.handoff import find_repair_loop

    loop = find_repair_loop(profile)
    if loop is not None:
        return max(1, int(getattr(loop, "max_rounds", 1) or 1))
    return 1


__all__ = [
    "VERIFICATION_GATE_EVENTS_KEY",
    "GateRepairOutcome",
    "evaluate_post_phase_gates",
    "evaluate_pre_phase_gates",
    "run_gate_hook",
    "run_post_implement_gate_repair",
]
