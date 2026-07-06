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

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pipeline.verification_selection import (
    SelectionContext,
    build_scheduled_gate_plan,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from pipeline.verification_contract import GateSet, VerificationContract
    from pipeline.verification_selection import ScheduledGatePlan


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
    * ``resolved`` — ``None`` at start (no plan / changed files supplied);
      otherwise ``manual`` for an operator/manual gate, ``active`` when this
      row's identity is among the plan's entries, else ``dormant``.
    """

    gate: str
    hook: str
    phase: str
    timing: str
    run_mode: str
    gate_sets: tuple[str, ...]
    condition: str
    condition_paths: tuple[str, ...] = ()
    resolved: str | None = None


# ── selection-rule reading (declared conditions, not path-matching) ────────


@dataclass(frozen=True)
class _SelectionConditions:
    """Declared per-gate-set activation facts read straight from ``selection``."""

    always: frozenset[str]
    operator: frozenset[str]
    task_kind: frozenset[str]
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
    task_kind: set[str] = set()
    paths: dict[str, list[str]] = {}
    for rule in contract.selection:
        if rule.kind == "always":
            always.update(rule.include)
        elif rule.kind == "operator":
            operator.update(rule.include)
        elif rule.kind == "task_kind":
            task_kind.update(rule.include)
        elif rule.kind == "paths":
            for name in rule.include:
                bucket = paths.setdefault(name, [])
                for glob in rule.paths:
                    if glob not in bucket:
                        bucket.append(glob)
    return _SelectionConditions(
        always=frozenset(always),
        operator=frozenset(operator),
        task_kind=frozenset(task_kind),
        paths={name: tuple(globs) for name, globs in paths.items()},
    )


def _row_condition(
    backing: Sequence[str], hook: str, conditions: _SelectionConditions,
) -> tuple[str, tuple[str, ...]]:
    """Derive ``(condition, condition_paths)`` for a row from its backing sets.

    Priority (per the contract's declared selection algebra, read for display):

    1. any backing set in an ``always`` rule → ``always``;
    2. a ``manual_only`` hook OR any backing set in an ``operator`` rule →
       ``operator`` (an operator/manual gate is never disguised as auto);
    3. any backing set in a ``paths`` rule → ``on_path`` with the union of globs;
    4. any backing set in a ``task_kind`` rule → ``task_kind``;
    5. otherwise (a directly-scheduled command with no gate set, so nothing
       narrows it) → ``always``.
    """
    if any(name in conditions.always for name in backing):
        return "always", ()
    if hook == "manual_only" or any(
        name in conditions.operator for name in backing
    ):
        return "operator", ()
    globs: list[str] = []
    for name in backing:
        for glob in conditions.paths.get(name, ()):
            if glob not in globs:
                globs.append(glob)
    if globs:
        return "on_path", tuple(globs)
    if any(name in conditions.task_kind for name in backing):
        return "task_kind", ()
    return "always", ()


# ── the projection builder ─────────────────────────────────────────────────


def build_gate_ledger(
    contract: VerificationContract,
    *,
    plan: ScheduledGatePlan | None = None,
    changed_files: Iterable[str] | None = None,
) -> tuple[GateLedgerRow, ...]:
    """Project ``contract`` into the deduplicated gate ledger.

    Rows are built by walking ``contract.schedule`` exactly as the banner's
    ``_build_gate_rows`` does — expanding ``entry.commands`` then ``entry.gate_sets``
    (via ``contract.gate_sets``), forming the ``(command, hook, phase)`` identity
    and deduping on it — so the ledger's row set is identical to the banner's.

    Resolve (optional): when ``plan`` or ``changed_files`` is supplied, each row
    is resolved by IDENTITY. An operator/manual gate (``condition == "operator"``)
    is always ``manual``. Otherwise the row is ``active`` iff its
    ``(command, hook, phase)`` is one of the plan's entries, else ``dormant``.
    ``plan.selected_commands`` is never consulted for disposition. When
    ``changed_files`` is given without a ``plan``, the plan is built via the
    public :func:`build_scheduled_gate_plan` over a :class:`SelectionContext`
    seeded from the contract's declared intent — the path-matching is reused, not
    re-implemented. With neither supplied (run start) every ``resolved`` is
    ``None``.

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
        backing: dict[str, list[str]] = {}
        order: list[str] = []
        for cmd in entry.commands:
            if cmd not in backing:
                backing[cmd] = []
                order.append(cmd)
        for name in entry.gate_sets:
            gate_set: GateSet | None = contract.gate_sets.get(name)
            if gate_set is None:  # defensive; names validated upstream
                continue
            for cmd in gate_set.commands:
                if cmd not in backing:
                    backing[cmd] = []
                    order.append(cmd)
                backing[cmd].append(name)

        for command in order:
            identity = (command, entry.hook, entry.phase)
            if identity in seen:
                continue
            seen.add(identity)
            condition, condition_paths = _row_condition(
                backing[command], entry.hook, conditions,
            )
            rows.append(
                GateLedgerRow(
                    gate=command,
                    hook=entry.hook,
                    phase=entry.phase,
                    timing=gate_timing(entry.hook, entry.phase),
                    run_mode=gate_run_mode(entry.hook),
                    gate_sets=tuple(backing[command]),
                    condition=condition,
                    condition_paths=condition_paths,
                ),
            )

    resolve_plan = _resolve_plan(contract, plan, changed_files)
    if resolve_plan is None:
        return tuple(rows)

    active_identities = {
        (entry.command, entry.hook, entry.phase)
        for entry in resolve_plan.entries
    }
    return tuple(_resolve_row(row, active_identities) for row in rows)


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
    row: GateLedgerRow, active_identities: set[tuple[str, str, str]],
) -> GateLedgerRow:
    """Attach the identity-based disposition to a row (does not mutate)."""
    from dataclasses import replace

    if row.condition == "operator":
        resolved = "manual"
    elif (row.gate, row.hook, row.phase) in active_identities:
        resolved = "active"
    else:
        resolved = "dormant"
    return replace(row, resolved=resolved)


__all__ = [
    "GateLedgerRow",
    "build_gate_ledger",
    "gate_run_mode",
    "gate_timing",
]
