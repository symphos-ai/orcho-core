# SPDX-License-Identifier: Apache-2.0
"""verification_ledger.py — one gate ledger with full scheduled-gate identity.

A single read-only projection over a declared :class:`VerificationContract`
(and, optionally, a resolved :class:`ScheduledGatePlan` / the run's changed
files) that both the start banner and the DONE summary can consume. It is the
*single source* of two facts that were previously recomputed in two places:

* the **scheduled-gate identity** of every declared gate — the same
  ``(command, hook, phase)`` tuple the banner's ``_build_gate_rows`` dedups on,
  carried together with its operator-facing ``timing`` label and ``run_mode``
  (``auto`` / ``manual``), so no consumer has to re-read the raw
  ``contract.schedule`` to recover timing; and
* the **disposition** of each row: its *activation condition*
  (``always`` / ``on_path`` / ``operator`` / ``task_kind``, read straight from
  the declared ``contract.selection`` rules — this is *display of the declared
  rules*, never a re-implementation of path-matching), and, when a plan / the
  run's changed files are supplied, its *resolved* state
  (``active`` / ``dormant`` / ``manual``).

The resolve is by **identity**, not by command name: a row is ``active`` only
when its own ``(command, hook, phase)`` appears among the plan's entries. The
same command scheduled under a second identity that the plan did not select
stays ``dormant`` — ``plan.selected_commands`` is never the disposition source.

Purity mirrors :mod:`pipeline.verification_selection`: nothing here executes a
command, reads a receipt, or blocks a transition. The path-matching resolve is
delegated wholesale to :func:`build_scheduled_gate_plan` /
:class:`SelectionContext`; this module carries no ``fnmatch`` of its own.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from pipeline.verification_selection import (
    SelectionContext,
    build_scheduled_gate_plan,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from pipeline.verification_contract import (
        GateSet,
        ScheduleEntry,
        VerificationContract,
    )
    from pipeline.verification_selection import ScheduledGateEntry, ScheduledGatePlan


# ── timing / run-mode derivation (single source) ──────────────────────────
#
# These two are the canonical derivation of a hook's operator-facing timing
# label and its auto/manual run mode. They are byte-equivalent to the private
# ``core.io.verification_header._timing`` / ``_RUN_MODE`` the banner used to own;
# the banner consumes the ledger's timing/run_mode instead of recomputing them
# from the raw schedule.

# hook -> run mode. Phase-anchored and delivery hooks fire automatically; the
# operator/resume hooks are run by a human.
_RUN_MODE: dict[str, str] = {
    "before_phase": "auto",
    "after_phase": "auto",
    "before_delivery": "auto",
    "manual_only": "manual",
    "on_resume": "manual",
}

# Run mode for a hook not in the table (defensive; hooks are validated upstream).
_UNKNOWN_RUN_MODE = "auto"


def gate_timing(hook: str, phase: str) -> str:
    """Operator-facing timing label for a hook (+ phase where it carries one).

    Equivalent to the banner's private ``_timing``: ``after_phase`` on
    ``implement`` reads ``after_implement``, ``before_delivery`` reads
    ``delivery``, ``manual_only`` reads ``operator``, and any other hook is
    surfaced verbatim.
    """
    if hook in ("before_phase", "after_phase") and phase:
        return f"{hook.split('_')[0]}_{phase}"
    if hook == "before_delivery":
        return "delivery"
    if hook == "manual_only":
        return "operator"
    return hook


def gate_run_mode(hook: str) -> str:
    """``auto`` / ``manual`` run mode for a hook (equivalent to ``_RUN_MODE``)."""
    return _RUN_MODE.get(hook, _UNKNOWN_RUN_MODE)


# ── policy / kind derivation (single source) ───────────────────────────────
#
# The declared receipt-enforcement policy and declared cost of each gate — once
# owned by the private ``core.io.verification_header._gate_policy`` /
# ``_gate_kind`` the banner used to compute in its own pass. They now live on the
# ledger row so both the banner and the ``quality-gates`` command read one
# projection instead of recomputing policy/kind twice.

# The string for any property not knowable at run-header time (an effective
# policy that would only resolve after the work_mode transform, or a cost with no
# declared ``cheap`` flag). Surfaced honestly rather than hidden or invented.
_UNKNOWN = "unknown"

# Declared schedule policies ordered weakest -> strongest. Used only to pick the
# single most-consequential declared policy when several gate sets back one
# command; this is a presentation choice, not a gate computation (it mirrors
# :func:`pipeline.verification_selection._merge_defaults`' max-strictness merge).
_POLICY_STRENGTH: tuple[str, ...] = ("manual", "suggest", "warn", "require")


def _gate_policy(entry: ScheduleEntry, backing: Sequence[GateSet]) -> str:
    """Effective declared policy: entry, else strictest backing default, else unknown.

    ``backing`` is every gate set that contributes this command under the entry.
    When the entry omits a policy, the strictest declared ``default_policy`` across
    the backing sets wins — mirroring
    :func:`pipeline.verification_selection._merge_defaults` (max strictness by
    :data:`_POLICY_STRENGTH`), so a stricter gate set is never hidden behind a
    laxer one. The work_mode transform is intentionally NOT applied — when no
    policy is declared at any level the consequence is not known at header time,
    so we stay honest with ``unknown`` rather than inferring behaviour.
    """
    if entry.policy is not None:
        return entry.policy
    declared = [
        gate_set.default_policy
        for gate_set in backing
        if gate_set.default_policy is not None
    ]
    if declared:
        return max(declared, key=_POLICY_STRENGTH.index)
    return _UNKNOWN


def _gate_kind(
    contract: VerificationContract, command: str, backing: Sequence[GateSet],
) -> str:
    """Declared cost for a command: ``cheap`` when any declared source says so.

    Mirrors :func:`pipeline.verification_selection._merge_defaults`' OR-ed cheap:
    the row is ``cheap`` when the per-command ``cheap`` is true OR any backing gate
    set declares ``default_cheap`` true. Anything else (all sources false or
    undeclared) is ``unknown`` — we do not invent a cost taxonomy without a
    declared source.
    """
    spec = contract.commands.get(command, {})
    cheap = spec.get("cheap") is True or any(
        gate_set.default_cheap for gate_set in backing
    )
    return "cheap" if cheap else _UNKNOWN


def _execution_policy(
    contract: VerificationContract,
    entry: ScheduleEntry,
    command: str,
    backing: Sequence[GateSet],
) -> str:
    """Freeze the executable policy even when header presentation is unknown.

    ``policy`` intentionally remains a conservative declaration-facing field
    for existing header consumers.  The durable execution axis cannot defer to
    a later plugin resolve, though: snapshot/resume needs the work-mode-derived
    policy that the selection resolver would use for this exact identity.
    """
    declared = _gate_policy(entry, backing)
    if declared != _UNKNOWN:
        return declared
    from pipeline.verification_selection import derive_effective_policy

    return derive_effective_policy(
        None,
        contract.work_mode or "",
        required=False if entry.hook == "manual_only" else command in contract.required,
    )


# ── effective stage (pure derivation of the operator-facing ``when``) ──────


def effective_stage(
    policy: str, hook: str, phase: str, has_final_phase: bool | None,
) -> str:
    """Operator-facing *when* a gate actually runs, from policy + hook + profile.

    A pure function of already-derived facts — it reads no schedule, runs no
    ``fnmatch``, and executes no command. It answers "at what point in a run does
    this gate get exercised?", which the raw timing hook alone cannot: a
    ``require`` gate runs at its timing hook, but a ``warn`` / ``manual`` gate is
    only surfaced, not auto-run, so its real stage depends on whether the profile
    even has a final delivery phase.

    * ``require`` → the gate's timing hook (e.g. ``after_implement`` / ``delivery``
      via :func:`gate_timing`): a required gate is enforced right where it is
      scheduled.
    * a ``manual_only`` / ``on_resume`` hook, or a ``suggest`` policy → ``operator``:
      a human runs it, it is never part of the automatic flow.
    * ``warn`` / ``manual`` (and any other non-required auto gate) → it is not enforced
      inline, so it surfaces only near delivery: ``pre-final`` when the profile has
      a final phase (``has_final_phase`` truthy), ``not auto-run`` when it provably
      does not (``has_final_phase is False``), and ``profile-dependent`` when the
      profile is unknown (``has_final_phase is None``) — the baseline is *marked*
      as profile-dependent rather than guessed.
    """
    if policy == "require":
        return gate_timing(hook, phase)
    if hook in ("manual_only", "on_resume") or policy == "suggest":
        return "operator"
    # warn / manual / unknown on an auto hook: not enforced inline, so its real
    # stage hinges on whether the profile has a final delivery phase.
    if has_final_phase is None:
        return "profile-dependent"
    return "pre-final" if has_final_phase else "not auto-run"


# ── the ledger row ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GateLedgerRow:
    """One scheduled gate, keyed by its full identity, with its disposition.

    The identity is ``(gate, hook, phase)`` — the same triple the start banner's
    ``_build_gate_rows`` dedups on, so two schedule entries for one command under
    different ``(hook, phase)`` produce two distinct rows and never collapse.

    * ``timing`` / ``run_mode`` — the ready-to-render timing label and auto/manual
      run mode (from :func:`gate_timing` / :func:`gate_run_mode`); a consumer
      never re-reads the raw schedule to recover them.
    * ``gate_sets`` — the declaration-ordered names of the gate sets that bring
      this command under this entry (empty when the entry lists the command
      directly with no gate set).
    * ``condition`` — the *declared* activation condition read from
      ``contract.selection``: ``always`` (an ``always`` rule includes a backing
      set), ``operator`` (a ``manual_only`` hook or an ``operator`` rule),
      ``on_path`` (a ``paths`` rule — ``condition_paths`` carries the union of its
      globs), or ``task_kind``.
    * ``activation_binding`` — the normalized binding consumed by presentation;
      a directly scheduled command carries explicit ``always`` rather than a
      consumer-side fallback.
    * ``resolved`` — ``None`` at start (no plan / changed files supplied);
      otherwise ``manual`` for an operator/manual gate, ``active`` when this
      row's identity is among the plan's entries, else ``dormant``.
    * ``policy`` — the effective declared receipt-enforcement policy for this gate
      (manual|suggest|warn|require), or ``unknown`` when it would only resolve after
      the work_mode transform (from :func:`_gate_policy`). This is the ledger's
      single source of policy; the banner no longer recomputes it.
    * ``kind`` — declared cost: ``cheap`` when the command (or its gate set)
      declares ``cheap``/``default_cheap``, else ``unknown`` (from
      :func:`_gate_kind`). No cost taxonomy is invented without a declared source.
    * ``when`` — the derived operator-facing stage the gate actually runs at,
      from :func:`effective_stage` over ``policy`` / ``hook`` / ``phase`` and the
      builder's ``has_final_phase``. ``""`` for a directly-constructed row that
      did not pass through the builder.

    ``policy`` / ``kind`` / ``when`` all default so direct construction (e.g. in a
    test) stays valid without supplying them.
    """

    gate: str
    hook: str
    phase: str
    timing: str
    run_mode: str
    gate_sets: tuple[str, ...]
    condition: str
    condition_paths: tuple[str, ...] = ()
    selection_task_kinds: tuple[str, ...] = ()
    activation_binding: str = ""
    resolved: str | None = None
    policy: str = _UNKNOWN
    kind: str = _UNKNOWN
    when: str = ""
    # Durable scheduled-gate axes (ADR 0132).  The presentation fields above
    # remain for the current header consumers; these facts are deliberately not
    # derived from a terminal receipt.
    declared: bool = True
    selectable: bool = True
    selected: bool | None = None
    execution_policy: str = _UNKNOWN
    consequence: str = "none"
    disposition: str | None = None
    selection_reason: str | None = None
    executor: str | None = None
    trigger: str | None = None
    receipt_evidence: str | None = None

    @property
    def identity(self) -> tuple[str, str, str]:
        """The only key for a scheduled gate; commands alone never identify it."""
        return (self.gate, self.hook, self.phase)


Disposition = Literal[
    "not_selected", "manual_available", "suggested", "skipped_fresh",
    "executed_pass", "executed_fail", "residual_missing", "residual_stale",
    "residual_failed",
]

TERMINAL_DISPOSITIONS: frozenset[str] = frozenset({
    "not_selected", "manual_available", "suggested", "skipped_fresh",
    "executed_pass", "executed_fail", "residual_missing", "residual_stale",
    "residual_failed",
})


@dataclass(frozen=True)
class GateTrailEvent:
    """An append-only, identity-scoped durable observation.

    ``kind`` is intentionally small: an execution observation is the *only*
    source allowed to produce an executed disposition.  ``receipt`` records
    evidence separately and can only contribute a residual classification.
    """

    command: str
    hook: str
    phase: str
    kind: Literal["selection", "execution", "reuse", "receipt"]
    outcome: str = ""
    reason: str = ""
    receipt_evidence: str | None = None

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.command, self.hook, self.phase)


def reduce_disposition(row: GateLedgerRow, events: Sequence[GateTrailEvent]) -> Disposition:
    """Close one row using only its own trail, in ADR precedence order.

    Execution and reuse are evaluated before receipt evidence.  In particular a
    present receipt with no execution event can never become ``executed_pass``.
    """
    own = tuple(event for event in events if event.identity == row.identity)
    # ``False`` is the durable fact that a path/task-kind/operator rule declined
    # this identity.  ``None`` is merely an unvisited lifecycle epoch; declared
    # manual availability still closes intentionally as operator-visible.
    if row.selected is False:
        return "not_selected"
    if row.execution_policy == "manual":
        return "manual_available"
    if row.execution_policy == "suggest":
        return "suggested"
    executions = [event for event in own if event.kind == "execution"]
    if executions:
        return "executed_pass" if executions[-1].outcome == "pass" else "executed_fail"
    if any(event.kind == "reuse" and event.outcome == "fresh" for event in own):
        return "skipped_fresh"
    receipts = [event for event in own if event.kind == "receipt"]
    status = receipts[-1].outcome if receipts else "missing"
    if status == "stale":
        return "residual_stale"
    if status == "failed":
        return "residual_failed"
    return "residual_missing"


# ── selection-rule reading (declared conditions, not path-matching) ────────


@dataclass(frozen=True)
class _SelectionConditions:
    """Declared per-gate-set activation facts read straight from ``selection``."""

    always: frozenset[str]
    operator: frozenset[str]
    task_kind: dict[str, tuple[str, ...]]
    paths: dict[str, tuple[str, ...]]


def _read_selection_conditions(
    contract: VerificationContract,
) -> _SelectionConditions:
    """Map each gate set to the selection rules that name it (display only).

    Reads ``SelectionRule.kind`` directly: ``always`` / ``operator`` /
    ``task_kind`` membership and the union of ``paths`` globs per included set,
    in declaration order. This is a read of the declared rules for presentation;
    it does NOT re-implement the runtime path-matching (that lives in
    :mod:`pipeline.verification_selection` and is reused for the resolve).
    """
    always: set[str] = set()
    operator: set[str] = set()
    task_kind: dict[str, list[str]] = {}
    paths: dict[str, list[str]] = {}
    for rule in contract.selection:
        if rule.kind == "always":
            always.update(rule.include)
        elif rule.kind == "operator":
            operator.update(rule.include)
        elif rule.kind == "task_kind":
            for name in rule.include:
                bucket = task_kind.setdefault(name, [])
                if rule.task_kind and rule.task_kind not in bucket:
                    bucket.append(rule.task_kind)
        elif rule.kind == "paths":
            for name in rule.include:
                bucket = paths.setdefault(name, [])
                for glob in rule.paths:
                    if glob not in bucket:
                        bucket.append(glob)
    return _SelectionConditions(
        always=frozenset(always),
        operator=frozenset(operator),
        task_kind={name: tuple(kinds) for name, kinds in task_kind.items()},
        paths={name: tuple(globs) for name, globs in paths.items()},
    )


def _activation_binding(
    backing: Sequence[str], hook: str, conditions: _SelectionConditions,
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Read the normalized activation binding for a declared row.

    Priority (per the contract's declared selection algebra, read for display):

    1. any backing set in an ``always`` rule → ``always``;
    2. a ``manual_only`` hook OR any backing set in an ``operator`` rule →
       ``operator`` (an operator/manual gate is never disguised as auto);
    3. any backing set in a ``paths`` rule → ``on_path`` with the union of globs;
    4. any backing set in a ``task_kind`` rule → ``task_kind``;
    5. directly scheduled commands carry the explicit ``always`` binding.
    """
    if any(name in conditions.always for name in backing):
        return "always", (), ()
    if hook == "manual_only" or any(
        name in conditions.operator for name in backing
    ):
        return "operator", (), ()
    globs: list[str] = []
    for name in backing:
        for glob in conditions.paths.get(name, ()):
            if glob not in globs:
                globs.append(glob)
    if globs:
        return "on_path", tuple(globs), ()
    task_kinds: list[str] = []
    for name in backing:
        for task_kind in conditions.task_kind.get(name, ()):
            if task_kind not in task_kinds:
                task_kinds.append(task_kind)
    if task_kinds:
        return "task_kind", (), tuple(task_kinds)
    if not backing:
        return "always", (), ()
    return "unreachable", (), ()


# ── the projection builder ─────────────────────────────────────────────────


def build_gate_ledger(
    contract: VerificationContract,
    *,
    plan: ScheduledGatePlan | None = None,
    changed_files: Iterable[str] | None = None,
    has_final_phase: bool | None = None,
) -> tuple[GateLedgerRow, ...]:
    """Project ``contract`` into the deduplicated gate ledger.

    Rows are built by walking ``contract.schedule`` exactly as the banner's
    ``_build_gate_rows`` does — expanding ``entry.commands`` then ``entry.gate_sets``
    (via ``contract.gate_sets``), forming the ``(command, hook, phase)`` identity
    and deduping on it — so the ledger's row set is identical to the banner's.

    Resolve (optional): when ``plan`` or ``changed_files`` is supplied, each row
    is resolved by IDENTITY. A manual hook or operator/manual gate is always
    ``manual``. Otherwise the row is ``active`` iff its
    ``(command, hook, phase)`` is one of the plan's entries, else ``dormant``.
    ``plan.selected_commands`` is never consulted for disposition. When
    ``changed_files`` is given without a ``plan``, the plan is built via the
    public :func:`build_scheduled_gate_plan` over a :class:`SelectionContext`
    seeded from the contract's declared intent — the path-matching is reused, not
    re-implemented. With neither supplied (run start) every ``resolved`` is
    ``None``.

    Policy / kind / when: each row also carries its effective declared receipt
    policy (:func:`_gate_policy`), declared cost (:func:`_gate_kind`), and the
    derived operator-facing stage ``when`` (:func:`effective_stage` over the
    policy/hook/phase and ``has_final_phase``). ``has_final_phase`` — whether the
    active profile has a final delivery phase (``True`` / ``False`` / ``None`` for
    unknown) — is display-only: it feeds only the ``when`` derivation and never
    affects resolve, plan, or the row set.

    Total: an empty ``selection`` / ``schedule`` / plan yields ``()`` or
    unresolved rows without raising.
    """
    conditions = _read_selection_conditions(contract)

    rows: list[GateLedgerRow] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in contract.schedule:
        # Per command in this entry, collect every backing gate set in
        # declaration order (entry.commands first, then gate-set commands), so a
        # command listed both directly and via a set keeps the set's identity.
        # ``backing`` carries the gate-set NAMES (condition/gate_sets column);
        # ``backing_sets`` carries the resolved :class:`GateSet` objects for the
        # policy/kind derivation in the same pass.
        backing: dict[str, list[str]] = {}
        backing_sets: dict[str, list[GateSet]] = {}
        order: list[str] = []
        for cmd in entry.commands:
            if cmd not in backing:
                backing[cmd] = []
                backing_sets[cmd] = []
                order.append(cmd)
        for name in entry.gate_sets:
            gate_set: GateSet | None = contract.gate_sets.get(name)
            if gate_set is None:  # defensive; names validated upstream
                continue
            for cmd in gate_set.commands:
                if cmd not in backing:
                    backing[cmd] = []
                    backing_sets[cmd] = []
                    order.append(cmd)
                backing[cmd].append(name)
                backing_sets[cmd].append(gate_set)

        for command in order:
            identity = (command, entry.hook, entry.phase)
            if identity in seen:
                continue
            seen.add(identity)
            activation_binding, condition_paths, selection_task_kinds = _activation_binding(
                backing[command], entry.hook, conditions,
            )
            policy = _gate_policy(entry, backing_sets[command])
            kind = _gate_kind(contract, command, backing_sets[command])
            rows.append(
                GateLedgerRow(
                    gate=command,
                    hook=entry.hook,
                    phase=entry.phase,
                    timing=gate_timing(entry.hook, entry.phase),
                    run_mode=gate_run_mode(entry.hook),
                    gate_sets=tuple(backing[command]),
                    condition=activation_binding,
                    condition_paths=condition_paths,
                    selection_task_kinds=selection_task_kinds,
                    activation_binding=activation_binding,
                    policy=policy,
                    kind=kind,
                    when=effective_stage(
                        policy, entry.hook, entry.phase, has_final_phase,
                    ),
                    selectable=activation_binding in {
                        "always", "on_path", "task_kind", "operator",
                    },
                    execution_policy=_execution_policy(
                        contract, entry, command, backing_sets[command],
                    ),
                    selection_reason=_selection_reason(activation_binding),
                ),
            )

    resolve_plan = _resolve_plan(contract, plan, changed_files)
    if resolve_plan is None:
        return tuple(rows)

    active_entries = {
        (entry.command, entry.hook, entry.phase): entry
        for entry in resolve_plan.entries
    }
    return tuple(_resolve_row(row, active_entries) for row in rows)


def _resolve_plan(
    contract: VerificationContract,
    plan: ScheduledGatePlan | None,
    changed_files: Iterable[str] | None,
) -> ScheduledGatePlan | None:
    """Resolve to a plan for disposition, or ``None`` when none was requested.

    A caller-supplied ``plan`` wins. Otherwise, when ``changed_files`` is given
    (even empty), build the plan via the public selection engine over a context
    seeded from the contract's declared intent — reusing its path-matching. With
    neither, return ``None`` so every row stays unresolved (run start).
    """
    if plan is not None:
        return plan
    if changed_files is None:
        return None
    ctx = SelectionContext(
        task_kind=(contract.task_kind or None),
        touched_paths=tuple(changed_files),
        operator_sets=tuple(contract.operator_sets),
        work_mode=contract.work_mode or "",
    )
    return build_scheduled_gate_plan(contract, ctx)


def _resolve_row(
    row: GateLedgerRow,
    active_entries: dict[tuple[str, str, str], ScheduledGateEntry],
) -> GateLedgerRow:
    """Attach the identity-based disposition to a row (does not mutate)."""
    planned_entry = active_entries.get((row.gate, row.hook, row.phase))
    planned_binding = planned_entry.activation_binding if planned_entry else None
    is_manual = row.hook in ("manual_only", "on_resume") or (
        row.activation_binding == "operator"
    )
    # Selection uses ``selected`` as its internal generic binding for gate-set
    # entries; the ledger retains their declared condition (always/on_path/etc.).
    # Only an automatic direct command's explicit ``always`` binding replaces it.
    # A plan must not turn an operator/manual row into an automatic one.
    binding = (
        row.activation_binding
        if is_manual
        else planned_binding if planned_binding == "always" else row.activation_binding
    )
    if is_manual:
        resolved = "manual"
    elif planned_entry is not None:
        resolved = "active"
    else:
        resolved = "dormant"
    selected = planned_entry is not None
    # The selected plan owns effective policy.  For an unselected row the
    # declaration remains visible, but there is intentionally no executor.
    policy = planned_entry.policy if planned_entry is not None else row.policy
    executor: str | None = None
    trigger: str | None = None
    consequence = "none"
    if selected and policy in {"manual", "suggest", "warn", "require"}:
        from pipeline.verification_execution import resolve_execution_eligibility

        eligibility = resolve_execution_eligibility(
            True, policy, row.hook, row.phase,
        )
        executor = eligibility.executor
        trigger = eligibility.trigger
        consequence = eligibility.consequence
    return replace(
        row,
        activation_binding=binding,
        condition=binding,
        resolved=resolved,
        selected=selected,
        execution_policy=policy,
        executor=executor,
        trigger=trigger,
        consequence=consequence,
        selection_reason=None if selected else _selection_reason(row.activation_binding),
    )


def _selection_reason(binding: str) -> str | None:
    """Translate presentation binding to the durable unselected vocabulary."""
    return {"on_path": "paths", "task_kind": "task_kind", "operator": "operator"}.get(binding)


__all__ = [
    "GateLedgerRow",
    "GateTrailEvent",
    "TERMINAL_DISPOSITIONS",
    "build_gate_ledger",
    "effective_stage",
    "gate_run_mode",
    "gate_timing",
    "reduce_disposition",
]
