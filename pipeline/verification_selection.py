"""verification_selection.py — deterministic Stage 4 gate-selection algebra.

Pure, read-only. Given a validated :class:`VerificationContract` and a
:class:`SelectionContext`, this module builds a deterministic
:class:`ScheduledGatePlan`: which declared commands are gates, under which hook,
with which *effective* policy and action.

Nothing here executes a command, writes a receipt, blocks a transition, or
triggers repair — it only resolves the policy algebra. The two transforms are:

* **Effective policy** — an explicit ``schedule.policy`` (anything that is not
  ``None``, including ``"suggest"``) is authoritative and is *not* transformed
  by ``work_mode``. Otherwise the merged gate-set ``default_policy`` (which may
  be ``None``) is fed through the ``work_mode`` derivation table (docs
  ``verification_contract.md`` §work_mode).
* **Effective action** — an explicit ``schedule.action`` is authoritative
  (including an operator ``"abort"``, which is never blurred). Otherwise a
  merged gate-set ``default_action`` wins; otherwise a strictly deterministic
  ``work_mode``-derived action is used. In particular ``governed`` +
  ``before_delivery`` with no explicit action derives exactly ``handoff`` (never
  ``abort``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import TYPE_CHECKING, Any

from pipeline.verification_contract import (
    GATE_ACTIONS,
    SCHEDULE_HOOKS,
    SCHEDULE_POLICIES,
)

if TYPE_CHECKING:
    from pipeline.verification_contract import GateSet, VerificationContract


@dataclass(frozen=True)
class SelectionContext:
    """Caller-supplied inputs that drive gate-set selection.

    ``task_kind`` is an operator/profile declaration (auto-inference is a TODO).
    ``touched_paths`` come from the run's owned/changed files. ``operator_sets``
    are gate-set names the operator named explicitly. ``work_mode`` overrides the
    contract's ``work_mode`` when set; otherwise the contract value is used.
    """

    task_kind: str | None = None
    touched_paths: tuple[str, ...] = ()
    operator_sets: tuple[str, ...] = ()
    work_mode: str = ""


# Run-level ``state.extras`` keys carrying the operator/profile selection inputs.
# These are the single, stable source threaded into both the executable routing
# plan (gate_repair) and the prompt-projection preview (prompt_parts) so the two
# agree. Auto-inference of ``task_kind`` from the task is a TODO (ADR 0081).
TASK_KIND_EXTRAS_KEY = "verification_task_kind"
OPERATOR_SETS_EXTRAS_KEY = "verification_operator_sets"


def selection_context_from_extras(
    extras: Mapping[str, Any],
    contract: VerificationContract,
    *,
    touched_paths: tuple[str, ...] = (),
) -> SelectionContext:
    """Build a :class:`SelectionContext` from run-level ``state.extras`` inputs.

    Resolution order for ``task_kind`` / ``operator_sets``: a per-run operator/CLI
    request on ``state.extras`` (the override keys) wins; otherwise the
    contract-declared intent (``contract.task_kind`` / ``contract.operator_sets``
    — the production source) is used; otherwise the inert default (``None`` /
    empty tuple). ``work_mode`` comes from the contract. ``touched_paths`` (the
    run's changed files) are supplied by the caller since computing them is an
    I/O concern this pure module avoids. Malformed extras degrade to the
    contract/default value.
    """
    raw_kind = extras.get(TASK_KIND_EXTRAS_KEY)
    if isinstance(raw_kind, str) and raw_kind:
        task_kind: str | None = raw_kind
    else:
        task_kind = (getattr(contract, "task_kind", "") or "") or None
    raw_ops = extras.get(OPERATOR_SETS_EXTRAS_KEY)
    if isinstance(raw_ops, (list, tuple)):
        operator_sets = tuple(str(s) for s in raw_ops)
    else:
        operator_sets = tuple(getattr(contract, "operator_sets", ()) or ())
    return SelectionContext(
        task_kind=task_kind,
        touched_paths=tuple(touched_paths),
        operator_sets=operator_sets,
        work_mode=getattr(contract, "work_mode", "") or "",
    )


@dataclass(frozen=True)
class ScheduledGateEntry:
    """One resolved gate: a command under a hook, with effective policy/action.

    ``phase`` is the phase-anchored target for ``before_phase`` / ``after_phase``
    hooks (empty for ``before_delivery`` / ``on_resume`` / ``manual_only``). It is
    part of the gate identity: the same command scheduled under one hook for two
    different phases produces two distinct entries (never collapses).
    """

    command: str
    hook: str
    phase: str
    policy: str
    action: str
    contributing_gate_sets: tuple[str, ...]
    primary_gate_set: str


@dataclass(frozen=True)
class ScheduledGatePlan:
    """The full, deterministic gate plan for a run context."""

    entries: tuple[ScheduledGateEntry, ...] = ()
    selected_gate_sets: tuple[str, ...] = ()
    selected_commands: tuple[str, ...] = ()


def _policy_rank(policy: str) -> int:
    return SCHEDULE_POLICIES.index(policy)


def _action_rank(action: str) -> int:
    return GATE_ACTIONS.index(action)


def _select_gate_sets(
    contract: VerificationContract, ctx: SelectionContext,
) -> tuple[str, ...]:
    """Pick gate sets in the fixed order baseline->task_kind->subsystem->operator.

    Within each step rules are processed in declaration order; the result is
    deduped preserving first occurrence so the order is stable and deterministic.
    """
    ordered: list[str] = []

    def _add(names: tuple[str, ...]) -> None:
        for name in names:
            if name in contract.gate_sets and name not in ordered:
                ordered.append(name)

    # baseline — always rules.
    for rule in contract.selection:
        if rule.kind == "always":
            _add(rule.include)
    # task_kind — rules whose declared kind matches the context.
    if ctx.task_kind is not None:
        for rule in contract.selection:
            if rule.kind == "task_kind" and rule.task_kind == ctx.task_kind:
                _add(rule.include)
    # subsystem — paths rules whose globs match any touched path.
    for rule in contract.selection:
        if rule.kind == "paths" and _paths_match(rule.paths, ctx.touched_paths):
            _add(rule.include)
    # operator — opt-in only. An ``operator`` selection rule *declares* which
    # gate sets are operator-gated, but a set is selected ONLY when the run
    # explicitly requested it via ``ctx.operator_sets``. Sets named directly in
    # ``operator_sets`` (and declared) are selected too. With no operator request
    # no operator gate is selected — expensive opt-in gates never fire by
    # default, keeping first-run usage non-hostile.
    requested = set(ctx.operator_sets)
    for rule in contract.selection:
        if rule.kind == "operator":
            _add(tuple(name for name in rule.include if name in requested))
    _add(ctx.operator_sets)

    return tuple(ordered)


def _paths_match(globs: tuple[str, ...], touched: tuple[str, ...]) -> bool:
    return any(fnmatch(path, glob) for glob in globs for path in touched)


def _merge_defaults(
    sets: list[GateSet],
) -> tuple[str | None, str | None]:
    """Merge gate-set defaults: max-strictness policy/action.

    Only sets that *declare* a default contribute to that default; absence stays
    ``None`` so the work_mode transform can decide later. Cost (``default_cheap``)
    is *not* merged here — it is declared metadata read independently by the
    verification header for display, and never feeds the blocking policy
    (ADR 0117).
    """
    policy: str | None = None
    action: str | None = None
    for gs in sets:
        if gs.default_policy is not None and (
            policy is None or _policy_rank(gs.default_policy) > _policy_rank(policy)
        ):
            policy = gs.default_policy
        if gs.default_action is not None and (
            action is None or _action_rank(gs.default_action) > _action_rank(action)
        ):
            action = gs.default_action
    return policy, action


def derive_effective_policy(
    base_policy: str | None,
    work_mode: str,
    *,
    required: bool,
) -> str:
    """Derive the effective blocking policy from a declared tier + work_mode.

    The blocking tier is the declared ``base_policy`` when set, otherwise
    ``require`` for a required command and ``suggest`` for an advisory one. Cost
    is **never** an input: an expensive ``require`` gate still blocks (ADR 0117).
    The fixed mode × tier table:

    * ``fast`` — relaxes only the advisory ``suggest`` tier to ``off`` for speed;
      ``warn`` and ``require`` are honored as declared.
    * ``pro`` — honors the declared tier exactly.
    * ``governed`` — escalates ``warn`` to ``require``; ``require`` and ``suggest``
      are honored as declared.
    * unset (``""`` / anything else) — honors the declared tier.
    """
    tier = base_policy if base_policy is not None else (
        "require" if required else "suggest"
    )
    if work_mode == "fast":
        return "off" if tier == "suggest" else tier
    if work_mode == "pro":
        return tier
    if work_mode == "governed":
        return "require" if tier == "warn" else tier
    return tier


def derive_effective_action(hook: str, phase: str, work_mode: str) -> str:
    """Strictly deterministic work_mode-derived action (the F1 fallback).

    * Failed pre-review require on implement (``after_phase`` / ``implement``):
      ``pro`` / ``governed`` -> ``repair_loop``; ``fast`` / unset ->
      ``continue_warn``.
    * Delivery (``before_delivery``): ``pro`` / ``governed`` -> ``handoff``
      (never ``abort``); ``fast`` / unset -> ``continue_warn``.
    * Everything else -> ``continue_warn``.
    """
    if hook == "after_phase" and phase == "implement":
        return "repair_loop" if work_mode in ("pro", "governed") else "continue_warn"
    if hook == "before_delivery":
        return "handoff" if work_mode in ("pro", "governed") else "continue_warn"
    return "continue_warn"


def repair_loop_target(hook: str, phase: str) -> str:
    """Deterministic ``repair_loop``-by-hook matrix.

    A ``repair_loop`` action is a *real* repair flow only for an ``after_phase``
    gate on a phase that has a ``repair_changes`` pair — in practice
    ``implement``. Every other hook/phase combination (``before_phase``,
    ``before_delivery``, ``on_resume``, ``after_phase`` of any non-implement
    phase) deterministically degrades to ``handoff``. This is independent of the
    work_mode-derived action: an *explicit* ``repair_loop`` (schedule.action or a
    gate-set default) routed onto a hook that cannot repair still degrades here.

    Returns ``"repair"`` or ``"handoff"``.
    """
    if hook == "after_phase" and phase == "implement":
        return "repair"
    return "handoff"


@dataclass
class _PairResolution:
    """Mutable accumulator for the (command, hook) tie-breaker."""

    policy: str | None = None
    action: str | None = None
    gate_set_restriction: set[str] = field(default_factory=set)

    def absorb(self, entry_policy: str | None, entry_action: str | None,
               entry_gate_sets: tuple[str, ...]) -> None:
        if entry_policy is not None and (
            self.policy is None
            or _policy_rank(entry_policy) > _policy_rank(self.policy)
        ):
            self.policy = entry_policy
        if entry_action is not None and (
            self.action is None
            or _action_rank(entry_action) > _action_rank(self.action)
        ):
            self.action = entry_action
        self.gate_set_restriction.update(entry_gate_sets)


def build_scheduled_gate_plan(
    contract: VerificationContract, ctx: SelectionContext,
) -> ScheduledGatePlan:
    """Build the deterministic :class:`ScheduledGatePlan` for a run context."""
    work_mode = ctx.work_mode or contract.work_mode

    selected_sets = _select_gate_sets(contract, ctx)

    # selected_commands: union of selected sets' commands, deduped in the order
    # they are first encountered (set order, then command order within a set).
    selected_commands: list[str] = []
    contributing: dict[str, list[str]] = {}
    for set_name in selected_sets:
        gate_set = contract.gate_sets[set_name]
        for command in gate_set.commands:
            if command not in contributing:
                contributing[command] = []
                selected_commands.append(command)
            if set_name not in contributing[command]:
                contributing[command].append(set_name)

    entries: list[ScheduledGateEntry] = []
    for command in selected_commands:
        command_sets = tuple(contributing[command])
        primary = command_sets[0]
        required = command in contract.required

        # Resolve applicable schedule entries, grouped by (hook, phase), with a
        # per-pair tie-breaker over (policy, action) and a union of gate_set
        # restrictions. Phase is part of the key so a command scheduled under one
        # hook for two phases yields two entries (the tie-breaker applies only
        # within a single phase-scoped pair).
        by_key: dict[tuple[str, str], _PairResolution] = {}
        for sched in contract.schedule:
            if not _schedule_applies(sched, command, command_sets):
                continue
            key = (sched.hook, sched.phase)
            res = by_key.setdefault(key, _PairResolution())
            res.absorb(sched.policy, sched.action, sched.gate_sets)

        if not by_key:
            by_key[("manual_only", "")] = _PairResolution()

        for hook, phase in sorted(by_key, key=lambda k: (_hook_rank(k[0]), k[1])):
            res = by_key[(hook, phase)]
            merge_source = _merge_source(
                contract, command_sets, res.gate_set_restriction,
            )
            base_policy, base_action = _merge_defaults(merge_source)

            if res.policy is not None:
                effective_policy = res.policy
            else:
                effective_policy = derive_effective_policy(
                    base_policy, work_mode, required=required,
                )

            if res.action is not None:
                effective_action = res.action
            elif base_action is not None:
                effective_action = base_action
            else:
                effective_action = derive_effective_action(hook, phase, work_mode)

            entries.append(
                ScheduledGateEntry(
                    command=command,
                    hook=hook,
                    phase=phase,
                    policy=effective_policy,
                    action=effective_action,
                    contributing_gate_sets=command_sets,
                    primary_gate_set=primary,
                ),
            )

    return ScheduledGatePlan(
        entries=tuple(entries),
        selected_gate_sets=selected_sets,
        selected_commands=tuple(selected_commands),
    )


def _schedule_applies(sched, command: str, command_sets: tuple[str, ...]) -> bool:
    """Whether a schedule entry applies to a selected command.

    Applies when the entry names the command, names a contributing gate set, or
    is a blanket entry (no commands and no gate_sets) that covers every selected
    command for its hook.
    """
    if command in sched.commands:
        return True
    if any(gs in command_sets for gs in sched.gate_sets):
        return True
    return not sched.commands and not sched.gate_sets


def _merge_source(
    contract: VerificationContract,
    command_sets: tuple[str, ...],
    restriction: set[str],
) -> list[GateSet]:
    """Gate sets feeding the defaults merge, narrowed by schedule.gate_sets."""
    names = command_sets
    if restriction:
        names = tuple(s for s in command_sets if s in restriction)
    return [contract.gate_sets[name] for name in names]


def _hook_rank(hook: str) -> int:
    return SCHEDULE_HOOKS.index(hook) if hook in SCHEDULE_HOOKS else len(SCHEDULE_HOOKS)
