"""
verification_contract.py — Read-only Stage 1 projection of the verification
contract.

Core owns the *protocol* here: the typed contract shape, its validation, the
placeholder syntax, and the code-owned default prompt-policy that decides which
facts each phase is allowed to see. Core does NOT own provider behavior — nothing
in this module executes ``commands``, writes a receipt, blocks a phase
transition, or triggers repair. The contract is loaded from the plugin (see the
four read-only fields on :class:`pipeline.plugins.PluginConfig`) and projected,
read-only, into the run header and into limited per-phase prompt blocks.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pipeline.verification_execution import (
    VerificationIdentity,
    resolve_selected_execution,
)

if TYPE_CHECKING:
    from pipeline.plugins import PluginConfig


class VerificationContractError(ValueError):
    """Raised when a *declared* verification contract is structurally invalid.

    Never raised for an undeclared contract — :meth:`VerificationContract.from_plugin`
    returns ``None`` in that case so the no-contract path stays byte-identical.
    """


# Known schedule policies — how strongly a hook's commands are surfaced.
SCHEDULE_POLICIES: tuple[str, ...] = ("manual", "suggest", "warn", "require")

# Known gate actions — what happens when a gate's policy is enforced. These are
# the *declared* values; the effective action for a hook is derived later
# (Stage 4) from explicit schedule/gate-set values and the work_mode transform.
GATE_ACTIONS: tuple[str, ...] = (
    "continue_warn",
    "repair_loop",
    "handoff",
    "abort",
)

# The four selection-rule type-keys. A selection rule declares exactly one of
# these as its type. ``always`` / ``operator`` carry the included gate_set names
# directly as their value; ``task_kind`` / ``paths`` carry a matcher value plus
# a separate ``include`` list of gate_set names.
SELECTION_RULE_KEYS: tuple[str, ...] = (
    "always",
    "task_kind",
    "paths",
    "operator",
)

# Known schedule hooks — when a set of commands becomes relevant. In the
# declared contract each hook is used as an entry *key* (hook-as-key form):
# ``{"after_phase": "implement", ...}`` / ``{"before_delivery": True, ...}``.
SCHEDULE_HOOKS: tuple[str, ...] = (
    "before_phase",
    "after_phase",
    "before_delivery",
    "on_resume",
    "manual_only",
)

# Accepted ``work_mode`` values; ``""`` means unset.
WORK_MODES: tuple[str, ...] = ("", "fast", "pro", "governed")

# Accepted per-command ``parity`` values. ``absolute`` means the command's
# assertions stand on their own; ``differential`` means the receipt is read
# against a baseline (e.g. the run worktree's base ref). Default is ``absolute``.
COMMAND_PARITIES: tuple[str, ...] = ("absolute", "differential")

# Phases treated as delivery boundaries for the ``before_delivery`` hook.
# Stage 1 code-owned default.
# TODO(orcho-verification-stage2): make the delivery-phase set (and the whole
# prompt-policy) part of the workspace->project->run override chain.
FINAL_PHASES: tuple[str, ...] = ("final_acceptance", "compliance_check")


@dataclass(frozen=True)
class ScheduleEntry:
    """One normalised schedule rule: a hook, its policy, and target commands.

    ``policy`` and ``action`` are ``None`` when the entry omits them — absence is
    deliberately distinct from any explicit value (including ``"suggest"`` and
    ``"manual"``). A ``None`` policy/action means "not set; derive it later" (from
    gate-set defaults and the work_mode transform). ``gate_sets``
    optionally restricts which declared gate sets this entry's defaults merge
    from.
    """

    hook: str
    policy: str | None = None
    action: str | None = None
    phase: str = ""
    commands: tuple[str, ...] = ()
    gate_sets: tuple[str, ...] = ()


@dataclass(frozen=True)
class GateSet:
    """A declared named set of commands plus optional gate-policy defaults.

    ``default_policy`` / ``default_action`` / ``default_cheap`` are ``None`` when
    omitted — absence is the input for the later work_mode transform, not a
    silent default. ``commands`` is required and validated against the declared
    command table.
    """

    name: str
    commands: tuple[str, ...]
    default_policy: str | None = None
    default_action: str | None = None
    default_cheap: bool | None = None


@dataclass(frozen=True)
class SelectionRule:
    """One ordered gate-set selection rule.

    Exactly one of :data:`SELECTION_RULE_KEYS` is the rule's type (``kind``).
    ``include`` is the validated tuple of declared gate-set names this rule
    activates. ``task_kind`` (for ``kind == "task_kind"``) and ``paths`` (for
    ``kind == "paths"``) carry the matcher; ``always`` / ``operator`` rules carry
    their gate sets directly in ``include``.
    """

    kind: str
    include: tuple[str, ...] = ()
    task_kind: str = ""
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerificationContract:
    """Normalised, validated read-only view of a declared verification contract."""

    dependency_repos: dict[str, dict[str, Any]]
    verification_envs: dict[str, dict[str, Any]]
    commands: dict[str, dict[str, Any]]
    schedule: tuple[ScheduleEntry, ...]
    default_env: str
    # Names of declared commands that form the required-gate set (validated
    # against ``commands``). Empty tuple means nothing is required.
    required: tuple[str, ...]
    work_mode: str
    # Declared named gate sets, keyed by name. Each carries its command list and
    # optional policy/action/cheap defaults. Empty when none are declared.
    gate_sets: dict[str, GateSet] = field(default_factory=dict)
    # Ordered gate-set selection rules. Empty when none are declared.
    selection: tuple[SelectionRule, ...] = ()
    # Declared Stage-4 selection intent (the production source for the selection
    # context). ``task_kind`` matches ``task_kind`` selection rules; ``""`` means
    # unset. ``operator_sets`` are the gate sets this project/profile opts into
    # for the run (operator gate sets activate only when named here). A per-run
    # operator/CLI request may override these via ``state.extras`` (keys
    # ``verification_task_kind`` / ``verification_operator_sets``); see
    # :func:`pipeline.verification_selection.selection_context_from_extras`.
    task_kind: str = ""
    operator_sets: tuple[str, ...] = ()
    # Stage 6 delivery-gate policy (manual|suggest|warn|require) declared via
    # ``verification.delivery_policy``. ``None`` means "not declared" — the
    # effective policy is then derived by
    # :func:`pipeline.verification_delivery.resolve_delivery_policy` (contract
    # present but unset → ``warn``; ``require`` only via this explicit value,
    # never escalated from ``work_mode``). See ADR 0083.
    delivery_policy: str | None = None

    @property
    def declared(self) -> bool:
        """Always ``True`` — a constructed contract is, by definition, declared.

        The undeclared case is represented by ``from_plugin`` returning ``None``,
        not by a contract instance with empty fields.
        """
        return True

    @classmethod
    def from_plugin(cls, plugin: PluginConfig) -> VerificationContract | None:
        """Build a validated contract from a plugin, or ``None`` if undeclared.

        Returns ``None`` when *none* of the contract fields are declared, so the
        no-contract path is byte-identical to prior behavior. When at least one
        field is declared, the whole contract is normalised and validated;
        invalid declarations raise :class:`VerificationContractError`.
        """
        # Read raw values WITHOUT truthiness coercion. Declared-ness is "differs
        # from the empty default", not "is truthy" — otherwise an explicitly
        # declared but wrong falsey shape (``[]`` / ``0`` / ``""`` where a
        # dict/str is required) would be silently erased to a default and skip
        # validation. Such shapes must reach the type checks below and raise.
        deps = getattr(plugin, "dependency_repos", {})
        envs = getattr(plugin, "verification_envs", {})
        verification = getattr(plugin, "verification", {})
        work_mode = getattr(plugin, "work_mode", "")

        if deps == {} and envs == {} and verification == {} and work_mode == "":
            return None

        norm_deps = _normalize_mapping_of_dicts(deps, "dependency_repos")
        norm_envs = _normalize_mapping_of_dicts(envs, "verification_envs")

        if not isinstance(verification, dict):
            raise VerificationContractError(
                f"verification must be a dict, got {type(verification).__name__}",
            )

        if work_mode not in WORK_MODES:
            raise VerificationContractError(
                f"work_mode {work_mode!r} is not one of {WORK_MODES!r}",
            )

        commands = _normalize_commands(verification.get("commands", {}), norm_envs)

        default_env = verification.get("default_env", "")
        if not isinstance(default_env, str):
            raise VerificationContractError(
                f"verification.default_env must be a str, got "
                f"{type(default_env).__name__}",
            )
        if default_env and default_env not in norm_envs:
            raise VerificationContractError(
                f"verification.default_env {default_env!r} is not a declared "
                f"verification_env (known: {sorted(norm_envs)!r})",
            )

        gate_sets = _normalize_gate_sets(verification.get("gate_sets", {}), commands)
        selection = _normalize_selection(verification.get("selection", []), gate_sets)
        schedule = _normalize_schedule(
            verification.get("schedule", []), commands, gate_sets,
        )
        required = _normalize_required(verification.get("required", []), commands)
        task_kind, operator_sets = _normalize_selection_intent(
            verification, gate_sets,
        )
        delivery_policy = _normalize_delivery_policy(verification)
        _validate_schedule_semantics(schedule, gate_sets, selection)

        return cls(
            dependency_repos=norm_deps,
            verification_envs=norm_envs,
            commands=commands,
            schedule=schedule,
            default_env=default_env,
            required=required,
            work_mode=work_mode,
            gate_sets=gate_sets,
            selection=selection,
            task_kind=task_kind,
            operator_sets=operator_sets,
            delivery_policy=delivery_policy,
        )


def _normalize_mapping_of_dicts(
    value: Any, label: str,
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise VerificationContractError(
            f"{label} must be a dict, got {type(value).__name__}",
        )
    out: dict[str, dict[str, Any]] = {}
    for key, body in value.items():
        if not isinstance(body, dict):
            raise VerificationContractError(
                f"{label}[{key!r}] must be a dict, got {type(body).__name__}",
            )
        out[str(key)] = dict(body)
    return out


def _normalize_commands(
    value: Any, envs: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise VerificationContractError(
            f"verification.commands must be a dict, got {type(value).__name__}",
        )
    out: dict[str, dict[str, Any]] = {}
    for name, spec in value.items():
        if isinstance(spec, str):
            cmd = {"run": spec}
        elif isinstance(spec, dict):
            cmd = dict(spec)
        else:
            raise VerificationContractError(
                f"verification.commands[{name!r}] must be a str or dict, got "
                f"{type(spec).__name__}",
            )
        env_ref = cmd.get("env")
        if env_ref is not None and env_ref not in envs:
            raise VerificationContractError(
                f"verification.commands[{name!r}].env {env_ref!r} is not a "
                f"declared verification_env (known: {sorted(envs)!r})",
            )
        if "parity" in cmd:
            parity = cmd["parity"]
            if not isinstance(parity, str) or parity not in COMMAND_PARITIES:
                raise VerificationContractError(
                    f"verification.commands[{name!r}].parity must be "
                    f"absolute|differential, got {parity!r}",
                )
        if "cheap" in cmd and not isinstance(cmd["cheap"], bool):
            raise VerificationContractError(
                f"verification.commands[{name!r}].cheap must be a bool, got "
                f"{type(cmd['cheap']).__name__}",
            )
        out[str(name)] = cmd
    return out


def _normalize_required(
    value: Any, commands: dict[str, dict[str, Any]],
) -> tuple[str, ...]:
    """Normalise ``verification.required`` to a tuple of declared command names.

    Accepts a list/tuple of strings; each name must be a declared command (else
    :class:`VerificationContractError` listing the known commands). Any non-list
    shape raises. Absence defaults to an empty tuple (nothing required).
    """
    if not isinstance(value, (list, tuple)):
        raise VerificationContractError(
            f"verification.required must be a list of command names, got "
            f"{type(value).__name__}",
        )
    names: list[str] = []
    for ref in value:
        if not isinstance(ref, str):
            raise VerificationContractError(
                f"verification.required entries must be command names (str), got "
                f"{type(ref).__name__}",
            )
        if ref not in commands:
            raise VerificationContractError(
                f"verification.required references unknown command {ref!r} "
                f"(known: {sorted(commands)!r})",
            )
        names.append(ref)
    return tuple(names)


def _normalize_gate_sets(
    value: Any, commands: dict[str, dict[str, Any]],
) -> dict[str, GateSet]:
    """Normalise ``verification.gate_sets`` to a name->:class:`GateSet` mapping.

    Each gate set must declare a ``commands`` list of declared command names.
    ``default_policy`` / ``default_action`` / ``default_cheap`` are optional;
    absence stays ``None`` (the input for the later work_mode transform), and an
    explicitly-present-but-invalid value raises. Absence of the whole mapping is
    an empty dict.
    """
    if not isinstance(value, dict):
        raise VerificationContractError(
            f"verification.gate_sets must be a dict, got {type(value).__name__}",
        )
    out: dict[str, GateSet] = {}
    for name, spec in value.items():
        if not isinstance(spec, dict):
            raise VerificationContractError(
                f"verification.gate_sets[{name!r}] must be a dict, got "
                f"{type(spec).__name__}",
            )
        cmd_refs = spec.get("commands")
        if not isinstance(cmd_refs, (list, tuple)):
            raise VerificationContractError(
                f"verification.gate_sets[{name!r}].commands must be a list, got "
                f"{type(cmd_refs).__name__}",
            )
        for ref in cmd_refs:
            if ref not in commands:
                raise VerificationContractError(
                    f"verification.gate_sets[{name!r}] references unknown command "
                    f"{ref!r} (known: {sorted(commands)!r})",
                )
        default_policy = spec.get("default_policy")
        if default_policy is not None and default_policy not in SCHEDULE_POLICIES:
            raise VerificationContractError(
                f"verification.gate_sets[{name!r}].default_policy {default_policy!r} "
                f"is not one of {SCHEDULE_POLICIES!r}",
            )
        default_action = spec.get("default_action")
        if default_action is not None and default_action not in GATE_ACTIONS:
            raise VerificationContractError(
                f"verification.gate_sets[{name!r}].default_action {default_action!r} "
                f"is not one of {GATE_ACTIONS!r}",
            )
        default_cheap = spec.get("default_cheap")
        if default_cheap is not None and not isinstance(default_cheap, bool):
            raise VerificationContractError(
                f"verification.gate_sets[{name!r}].default_cheap must be a bool, "
                f"got {type(default_cheap).__name__}",
            )
        out[str(name)] = GateSet(
            name=str(name),
            commands=tuple(str(r) for r in cmd_refs),
            default_policy=default_policy,
            default_action=default_action,
            default_cheap=default_cheap,
        )
    return out


def _selection_include(
    refs: Any, gate_sets: dict[str, GateSet], label: str,
) -> tuple[str, ...]:
    """Validate a list of gate-set names against the declared gate sets."""
    if not isinstance(refs, (list, tuple)):
        raise VerificationContractError(
            f"{label} must be a list of gate_set names, got "
            f"{type(refs).__name__}",
        )
    names: list[str] = []
    for ref in refs:
        if not isinstance(ref, str):
            raise VerificationContractError(
                f"{label} entries must be gate_set names (str), got "
                f"{type(ref).__name__}",
            )
        if ref not in gate_sets:
            raise VerificationContractError(
                f"{label} references unknown gate_set {ref!r} "
                f"(known: {sorted(gate_sets)!r})",
            )
        names.append(ref)
    return tuple(names)


def _normalize_selection(
    value: Any, gate_sets: dict[str, GateSet],
) -> tuple[SelectionRule, ...]:
    """Normalise ``verification.selection`` to an ordered tuple of rules.

    Each rule declares exactly one of :data:`SELECTION_RULE_KEYS`. ``always`` and
    ``operator`` carry their included gate-set names directly as the type-key's
    value; ``task_kind`` (a non-empty string) and ``paths`` (a glob list) carry a
    matcher plus a separate ``include`` list. All gate-set references are
    validated against the declared gate sets. Absence is an empty tuple.
    """
    if not isinstance(value, (list, tuple)):
        raise VerificationContractError(
            f"verification.selection must be a list, got {type(value).__name__}",
        )
    rules: list[SelectionRule] = []
    for raw in value:
        if not isinstance(raw, dict):
            raise VerificationContractError(
                f"verification.selection entries must be dicts, got "
                f"{type(raw).__name__}",
            )
        present = [k for k in SELECTION_RULE_KEYS if k in raw]
        if len(present) != 1:
            raise VerificationContractError(
                f"selection rule must declare exactly one type key "
                f"(one of {SELECTION_RULE_KEYS!r}), got {sorted(present)!r}",
            )
        kind = present[0]
        if kind in ("always", "operator"):
            include = _selection_include(
                raw[kind], gate_sets, f"selection.{kind}",
            )
            rules.append(SelectionRule(kind=kind, include=include))
        elif kind == "task_kind":
            task_kind = raw["task_kind"]
            if not isinstance(task_kind, str) or not task_kind:
                raise VerificationContractError(
                    f"selection.task_kind must be a non-empty str, got "
                    f"{task_kind!r}",
                )
            include = _selection_include(
                raw.get("include", []), gate_sets, "selection.task_kind.include",
            )
            rules.append(
                SelectionRule(kind=kind, task_kind=task_kind, include=include),
            )
        else:  # paths
            globs = raw["paths"]
            if not isinstance(globs, (list, tuple)):
                raise VerificationContractError(
                    f"selection.paths must be a list of globs, got "
                    f"{type(globs).__name__}",
                )
            for g in globs:
                if not isinstance(g, str):
                    raise VerificationContractError(
                        f"selection.paths entries must be glob strings, got "
                        f"{type(g).__name__}",
                    )
            include = _selection_include(
                raw.get("include", []), gate_sets, "selection.paths.include",
            )
            rules.append(
                SelectionRule(
                    kind=kind,
                    paths=tuple(str(g) for g in globs),
                    include=include,
                ),
            )
    return tuple(rules)


def _normalize_selection_intent(
    verification: dict[str, Any], gate_sets: dict[str, GateSet],
) -> tuple[str, tuple[str, ...]]:
    """Normalise the declared Stage-4 selection intent.

    ``verification.task_kind`` is an optional non-empty string (absence → ``""``).
    ``verification.operator_sets`` is an optional list of declared gate-set names
    the project/profile opts into (absence → ``()``); each must be a declared
    gate set. These are the production source for the selection context; a per-run
    operator/CLI request may still override them via ``state.extras``.
    """
    raw_kind = verification.get("task_kind", "")
    if not isinstance(raw_kind, str):
        raise VerificationContractError(
            f"verification.task_kind must be a str, got {type(raw_kind).__name__}",
        )
    raw_ops = verification.get("operator_sets", [])
    if not isinstance(raw_ops, (list, tuple)):
        raise VerificationContractError(
            f"verification.operator_sets must be a list of gate_set names, got "
            f"{type(raw_ops).__name__}",
        )
    operator_sets: list[str] = []
    for ref in raw_ops:
        if not isinstance(ref, str):
            raise VerificationContractError(
                f"verification.operator_sets entries must be gate_set names "
                f"(str), got {type(ref).__name__}",
            )
        if ref not in gate_sets:
            raise VerificationContractError(
                f"verification.operator_sets references unknown gate_set {ref!r} "
                f"(known: {sorted(gate_sets)!r})",
            )
        operator_sets.append(ref)
    return raw_kind, tuple(operator_sets)


def _normalize_delivery_policy(verification: dict[str, Any]) -> str | None:
    """Normalise the optional ``verification.delivery_policy`` (Stage 6 / ADR 0083).

    Absence → ``None`` (derive later: contract present but unset means ``warn``).
    A present value must be one of :data:`SCHEDULE_POLICIES` (the existing
    manual|suggest|warn|require vocabulary); anything else
    raises :class:`VerificationContractError`.
    """
    if "delivery_policy" not in verification:
        return None
    policy = verification["delivery_policy"]
    if not isinstance(policy, str) or policy not in SCHEDULE_POLICIES:
        raise VerificationContractError(
            f"verification.delivery_policy {policy!r} is not one of "
            f"{SCHEDULE_POLICIES!r}",
        )
    return policy


def _normalize_schedule(
    value: Any,
    commands: dict[str, dict[str, Any]],
    gate_sets: dict[str, GateSet],
) -> tuple[ScheduleEntry, ...]:
    if not isinstance(value, (list, tuple)):
        raise VerificationContractError(
            f"verification.schedule must be a list, got {type(value).__name__}",
        )
    entries: list[ScheduleEntry] = []
    for raw in value:
        if not isinstance(raw, dict):
            raise VerificationContractError(
                f"verification.schedule entries must be dicts, got "
                f"{type(raw).__name__}",
            )
        # Documented hook-as-key form (see
        # docs/architecture/verification_contract.md): exactly one hook name is
        # an entry key. For ``before_phase`` / ``after_phase`` its value is the
        # target phase name; for ``before_delivery`` / ``on_resume`` /
        # ``manual_only`` its value is a flag (typically ``True``).
        present_hooks = [h for h in SCHEDULE_HOOKS if h in raw]
        if len(present_hooks) != 1:
            raise VerificationContractError(
                f"schedule entry must declare exactly one hook key "
                f"(one of {SCHEDULE_HOOKS!r}), got {sorted(present_hooks)!r}",
            )
        hook = present_hooks[0]
        hook_value = raw[hook]
        if hook in ("before_phase", "after_phase"):
            if not isinstance(hook_value, str) or not hook_value:
                raise VerificationContractError(
                    f"schedule hook {hook!r} must name a phase (non-empty str), "
                    f"got {hook_value!r}",
                )
            phase = hook_value
        else:
            phase = ""
        # Policy and action are OPTIONAL. Absence of the key normalises to
        # ``None`` (derive later), which is deliberately distinct from any
        # explicit value — including ``"suggest"`` and ``"manual"``. Only an
        # explicitly-present-but-invalid value raises.
        policy = raw.get("policy")
        if policy is not None and policy not in SCHEDULE_POLICIES:
            raise VerificationContractError(
                f"schedule policy {policy!r} is not one of {SCHEDULE_POLICIES!r}",
            )
        action = raw.get("action")
        if action is not None and action not in GATE_ACTIONS:
            raise VerificationContractError(
                f"schedule action {action!r} is not one of {GATE_ACTIONS!r}",
            )
        cmd_refs = raw.get("commands", [])
        if not isinstance(cmd_refs, (list, tuple)):
            raise VerificationContractError(
                f"schedule entry commands must be a list, got "
                f"{type(cmd_refs).__name__}",
            )
        for ref in cmd_refs:
            if ref not in commands:
                raise VerificationContractError(
                    f"schedule entry references unknown command {ref!r} "
                    f"(known: {sorted(commands)!r})",
                )
        gate_set_refs = raw.get("gate_sets", [])
        if not isinstance(gate_set_refs, (list, tuple)):
            raise VerificationContractError(
                f"schedule entry gate_sets must be a list, got "
                f"{type(gate_set_refs).__name__}",
            )
        for ref in gate_set_refs:
            if ref not in gate_sets:
                raise VerificationContractError(
                    f"schedule entry references unknown gate_set {ref!r} "
                    f"(known: {sorted(gate_sets)!r})",
                )
        entries.append(
            ScheduleEntry(
                hook=hook,
                policy=policy,
                action=action,
                phase=phase,
                commands=tuple(str(r) for r in cmd_refs),
                gate_sets=tuple(str(r) for r in gate_set_refs),
            ),
        )
    return tuple(entries)


def _validate_schedule_semantics(
    schedule: tuple[ScheduleEntry, ...],
    gate_sets: dict[str, GateSet],
    selection: tuple[SelectionRule, ...],
) -> None:
    """Reject unreachable automatic targets and invalid policy/action pairs."""
    selected_sets = {name for rule in selection for name in rule.include}
    for entry in schedule:
        identity = entry.hook + (f"+{entry.phase}" if entry.phase else "")
        if entry.hook != "manual_only":
            for name in entry.gate_sets:
                if name not in selected_sets:
                    raise VerificationContractError(
                        f"automatic schedule {identity} references unreachable "
                        f"gate_set {name!r}: no selection rule includes it",
                    )
        effective, default_sources = _schedule_validation_sources(
            entry, gate_sets, selected_sets,
        )
        if entry.hook == "manual_only" and effective not in ("manual", "suggest"):
            raise VerificationContractError(
                f"manual_only schedule {identity} only accepts manual or suggest "
                f"policy, got {effective!r}",
            )
        has_action = entry.action is not None or any(
            gate_sets[name].default_action is not None for name in default_sources
        )
        if has_action and effective != "require":
            raise VerificationContractError(
                f"schedule action requires policy='require' at {identity}, "
                f"got {effective!r}",
            )


def _schedule_validation_sources(
    entry: ScheduleEntry,
    gate_sets: dict[str, GateSet],
    selected_sets: set[str],
) -> tuple[str, set[str]]:
    """Resolve the declared tier and default sources used by validation.

    This is the contract-time counterpart to selection's max-strictness merge:
    an explicit tier wins, otherwise all defaults that can contribute through
    the entry are merged. A blanket entry can cover every selectable set, so
    their defaults are included as well. Validation intentionally does not
    apply a work-mode projection: actions are legal only on a declared
    ``require`` tier, irrespective of the run's mode.
    """
    names = set(entry.gate_sets)
    if not names:
        if entry.commands:
            names = {
                name
                for name in selected_sets
                if any(command in gate_sets[name].commands for command in entry.commands)
            }
        else:
            names = selected_sets
    if entry.policy is not None:
        return entry.policy, names
    declared = [
        gate_sets[name].default_policy
        for name in names
        if gate_sets[name].default_policy is not None
    ]
    policy = max(declared, key=SCHEDULE_POLICIES.index) if declared else (
        "manual" if entry.hook == "manual_only" else "suggest"
    )
    return policy, names


@dataclass(frozen=True)
class PlaceholderContext:
    """Resolved paths available for syntactic placeholder substitution.

    ``run_dir`` is ``None`` when unavailable (e.g. inside a prompt builder that
    has no run directory yet); the ``{run_dir}`` token then stays literal.

    ``isolated_source`` carries the run's isolated per-run worktree metadata
    (:class:`pipeline.engine.worktree_source.IsolatedSource`) when the repo runs
    in isolation, else ``None``. It is the fail-closed seam consumed by
    :func:`pipeline.verification_env.resolve_env_runtime` so the verify-env cwd
    of an isolated repo binds to the worktree, never the canonical sibling. The
    ``{dependency:X}`` map is already redirected at build time by
    :func:`placeholder_context_for`, so this field is only needed for the cwd axis.
    """

    checkout: str = ""
    project: str = ""
    workspace: str = ""
    run_dir: str | None = None
    dependencies: dict[str, str] = field(default_factory=dict)
    isolated_source: Any = None


def placeholder_context_for(
    contract: VerificationContract,
    *,
    checkout: str,
    project: str,
    workspace: str,
    run_dir: str | None,
    worktree: Mapping[str, Any] | None = None,
    participant_set: Any | None = None,
) -> PlaceholderContext:
    """Build a :class:`PlaceholderContext` from already-resolved run paths.

    ``checkout`` / ``project`` / ``workspace`` are pre-resolved strings; each
    declared ``dependency_repos`` entry's ``path`` is resolved relative to
    ``project`` (absolute paths are kept as-is). Purely syntactic — no
    filesystem access beyond path normalisation. This is the DRY seam shared by
    the read-only Stage 1 projection and the Stage 2 env-assertion engine.

    ``worktree`` is the run's ``session['worktree']``-shaped isolation block (or
    ``None`` for a single-checkout run). When the repo runs in an isolated per-run
    worktree (ADR 0033), any declared dependency whose canonical path identifies
    that repo is redirected to the worktree checkout instead of the canonical
    sibling — the fail-closed source resolution of ADR 0112 §3 / ADR 0108 branch
    (b). A dependency that is unbindable to its worktree (unresolved or sibling-
    pointing) raises :class:`pipeline.engine.worktree_source.IsolatedSourceError`
    rather than silently resolving to the clean sibling tree. Genuine external
    dependencies and single-checkout runs are unaffected (sibling fallback stays
    legal). The resolved :class:`IsolatedSource` is also carried on the context so
    the verify-env cwd axis can fail closed the same way.

    The isolated source is derived once, through the run's *participant set*
    (ADR 0112 §1). When the caller threads the run-scoped ``participant_set``
    (the in-memory set seeded onto ``state.extras`` by ``state_setup``), it is the
    single source of truth: the participant matching THIS call's repo identity
    (``project``, falling back to ``checkout``) is read from it — never the set's
    first element — so a multi-participant cross set redirects each repo through
    its OWN participant and state / control verification never diverge from the
    run-scoped set. Otherwise a
    one-participant mono :class:`pipeline.participants.ParticipantSet` is built
    IN-MEMORY from the ``worktree`` meta block when threaded, else from the
    ``checkout`` / ``project`` gap (a per-run worktree always lands on a checkout
    distinct from its source) — the back-compat path for external callers that
    pass only paths. Either way the set encapsulates the whole derivation; there is
    no parallel meta/path-gap branch here. It is NOT persisted: the durable source
    stays ``meta.worktree`` and the set is re-seeded from it on resume.
    Single-checkout runs (``checkout`` == ``project``) derive nothing and stay
    byte-identical.
    """
    from pathlib import Path

    from pipeline.engine.worktree_source import resolve_isolated_repo_source
    from pipeline.participants import ParticipantSet

    # Select the participant for THIS call by repo/project identity, never the
    # set's first element — a multi-participant set (cross) must redirect the
    # current repo through its OWN participant, else a non-first repo stays on
    # the canonical sibling (false-green, ADR 0112 §3).
    participant = None
    if participant_set is not None and len(participant_set):
        participants = participant_set
        participant = participants.get(project) or participants.get(checkout)
    if participant is None:
        # No threaded set, or the threaded set does not carry this repo: build
        # the one-participant mono set from paths/meta (back-compat derivation).
        participants = ParticipantSet.for_mono(
            checkout=checkout, project=project, worktree=worktree,
        )
        participant = participants.get(project) or next(iter(participants))
    isolated = participants.isolated_source_for(participant)

    dependencies: dict[str, str] = {}
    for name, body in contract.dependency_repos.items():
        path = body.get("path")
        if not path:
            continue
        candidate = Path(path)
        resolved = str(
            candidate if candidate.is_absolute() else Path(project) / candidate,
        )
        # Fail-closed redirect: a dependency that names an isolated repo binds to
        # its worktree, never the canonical sibling. No-op for external deps and
        # single-checkout runs. Each dependency is redirected through the
        # participant that OWNS its repo when the threaded set carries it — not
        # only the call's selected participant — so a dependency naming ANOTHER
        # participant (e.g. an out-of-set sibling promoted mid-run, ADR 0112 §4)
        # binds to THAT repo's worktree rather than staying on the canonical
        # sibling. Falls back to the selected participant's source otherwise.
        dep_isolated = isolated
        if participant_set is not None and len(participant_set):
            dep_part = participant_set.get(resolved)
            if dep_part is not None:
                dep_isolated = participant_set.isolated_source_for(dep_part)
        dependencies[name] = resolve_isolated_repo_source(
            repo_name=name, candidate=resolved, isolated=dep_isolated,
        )
    return PlaceholderContext(
        checkout=checkout,
        project=project,
        workspace=workspace,
        run_dir=run_dir,
        dependencies=dependencies,
        isolated_source=isolated,
    )


_TOKEN_RE = re.compile(r"\{([A-Za-z0-9_]+(?::[A-Za-z0-9_\-./]+)?)\}")


def resolve_placeholders(text: str, ctx: PlaceholderContext) -> str:
    """Syntactically substitute known placeholders; leave unknown ones literal.

    Recognises ``{checkout}``, ``{project}``, ``{workspace}``, ``{run_dir}`` and
    ``{dependency:name}``. Any token that is unknown or whose value is
    unavailable (empty path, ``run_dir is None``, or an undeclared dependency)
    is left verbatim. Never raises.
    """
    if not text:
        return text

    def _repl(match: re.Match[str]) -> str:
        token = match.group(1)
        if token == "checkout":
            return ctx.checkout or match.group(0)
        if token == "project":
            return ctx.project or match.group(0)
        if token == "workspace":
            return ctx.workspace or match.group(0)
        if token == "run_dir":
            return ctx.run_dir if ctx.run_dir else match.group(0)
        if token.startswith("dependency:"):
            name = token.split(":", 1)[1]
            value = ctx.dependencies.get(name)
            return value if value else match.group(0)
        return match.group(0)

    return _TOKEN_RE.sub(_repl, text)


def render_header_summary(contract: VerificationContract | None) -> str | None:
    """Compact one-line header summary of a declared contract, or ``None``.

    Surfaces only *names* and policies (work_mode, env names, command names,
    schedule policies) — no path resolution.
    """
    if contract is None:
        return None
    bits: list[str] = []
    if contract.work_mode:
        bits.append(f"work_mode={contract.work_mode}")
    if contract.verification_envs:
        bits.append("envs=" + ",".join(sorted(contract.verification_envs)))
    if contract.commands:
        bits.append("commands=" + ",".join(sorted(contract.commands)))
    if contract.schedule:
        # A ``None`` policy is "not set; derive later" — surface it as
        # ``derived`` rather than crashing the sort on a None/str mix.
        policies = sorted({e.policy or "derived" for e in contract.schedule})
        bits.append("schedule=" + ",".join(policies))
    if not bits:
        return None
    return "Verification contract: " + "; ".join(bits)


def _entry_applies(entry: ScheduleEntry, phase: str) -> bool:
    """Code-owned default prompt-policy: which schedule entries a phase sees.

    Stage 1 surfaces only phase-anchored hooks (``before_phase``/``after_phase``
    matching the phase) and ``before_delivery`` on the final phases. ``on_resume``
    and ``manual_only`` are valid hooks but are not projected per-phase here.
    """
    if entry.hook in ("before_phase", "after_phase"):
        return entry.phase == phase
    if entry.hook == "before_delivery":
        return phase in FINAL_PHASES
    return False


def render_phase_block(
    contract: VerificationContract | None,
    phase: str,
    ctx: PlaceholderContext,
) -> str | None:
    """Render the *limited* per-phase contract block, or ``None`` if empty.

    Only schedule entries relevant to ``phase`` (per the code-owned default
    prompt-policy) are shown with their commands placeholder-resolved. A ``None``
    policy is "not set; derive later" and is rendered as ``derived``.
    The whole config is never dumped into the prompt.
    """
    if contract is None:
        return None
    relevant = [
        e for e in contract.schedule
        if _entry_applies(e, phase)
    ]
    if not relevant:
        return None

    lines = [f"Verification contract — {phase}:"]
    for entry in relevant:
        policy_label = entry.policy or "derived"
        cmd_names = entry.commands or tuple(sorted(contract.commands))
        for name in cmd_names:
            cmd = contract.commands.get(name)
            if not cmd:
                continue
            run = resolve_placeholders(str(cmd.get("run", "")), ctx)
            lines.append(f"  [{entry.hook}/{policy_label}] {name}: {run}")

    if len(lines) == 1:
        return None
    return "\n".join(lines)


# ── Stage 4 plan-based projection ─────────────────────────────────────────
#
# When a contract declares gate_sets / selection, the per-phase prompt block is
# projected from the *resolved* ``ScheduledGatePlan`` (effective policy/action
# after the work_mode transform, gate source via ``primary_gate_set``) rather
# than the raw schedule. The plan object is consumed duck-typed (``entries`` of
# objects exposing ``command`` / ``hook`` / ``policy`` / ``action`` /
# ``primary_gate_set``) so this module never imports the selection engine
# (which imports this module). Blocks stay limited and per-phase — only the
# entries relevant to the phase are listed, never the whole config.
#
# Default prompt-policy is Orcho-owned; the workspace -> project -> run override
# chain is a TODO (see the FINAL_PHASES note above).


def _gate_line(
    contract: VerificationContract, entry: Any, entry_phase: str,
    ctx: PlaceholderContext,
) -> str:
    cmd = contract.commands.get(entry.command, {})
    run = resolve_placeholders(str(cmd.get("run", "")), ctx)
    hook_label = entry.hook + (f"({entry_phase})" if entry_phase else "")
    resolved = resolve_selected_execution(VerificationIdentity(
        command=entry.command,
        hook=entry.hook,
        phase=entry_phase,
        policy=entry.policy,
    ))
    if resolved.consequence == "required_action":
        posture = f"require; action={entry.action}"
    elif resolved.executor == "operator":
        posture = "operator available" if entry.policy == "manual" else "operator recommendation"
    else:
        posture = "engine warning; shipping allowed"
    return f"  [{hook_label} {posture}] {entry.command} <{entry.primary_gate_set}>: {run}"


def render_phase_gate_block(
    contract: VerificationContract | None,
    plan: Any,
    phase: str,
    ctx: PlaceholderContext,
) -> str | None:
    """Project the resolved :class:`ScheduledGatePlan` into a limited block.

    Returns ``None`` when there is no contract, no plan, an empty plan, or the
    phase has nothing relevant to surface. Four phase shapes are projected:

    * ``plan`` — env summary + every scheduled gate (forward-looking).
    * ``implement`` — debugging freedom + the gates checked after implement.
    * ``review_changes`` — declared receipts take priority over ad-hoc commands.
    * ``final_acceptance`` / delivery — delivery gates + the blocking policy.

    Effective policy/action come from the plan (post work_mode transform); the
    gate source is shown via ``primary_gate_set``. The whole config is never
    dumped — only the entries relevant to ``phase`` are listed.
    """
    if contract is None or plan is None:
        return None
    entries = getattr(plan, "entries", ()) or ()
    if not entries:
        return None
    # Phase is part of the resolved gate identity (Stage 4); read it directly.
    annotated = [(e, getattr(e, "phase", "")) for e in entries]

    if phase == "plan":
        relevant = [(e, p) for (e, p) in annotated if e.hook != "manual_only"]
        if not relevant:
            return None
        lines = [f"Verification contract — {phase}:"]
        if contract.verification_envs:
            lines.append(
                "  envs: " + ", ".join(sorted(contract.verification_envs)),
            )
        lines.append("  Scheduled gates:")
        lines.extend(_gate_line(contract, e, p, ctx) for e, p in relevant)
        return "\n".join(lines)

    if phase == "implement":
        relevant = [
            (e, p) for (e, p) in annotated
            if (e.hook == "after_phase" and p == "implement")
            or e.hook == "before_delivery"
        ]
        if not relevant:
            return None
        lines = [
            f"Verification contract — {phase}:",
            "  Debug freely; the gates below are what will be checked next.",
            "  Scheduled gates after implement:",
        ]
        lines.extend(_gate_line(contract, e, p, ctx) for e, p in relevant)
        return "\n".join(lines)

    if phase == "review_changes":
        relevant = [
            (e, p) for (e, p) in annotated
            if resolve_selected_execution(VerificationIdentity(
                command=e.command,
                hook=e.hook,
                phase=p,
                policy=e.policy,
            )).consequence != "none"
        ]
        if not relevant:
            return None
        lines = [
            f"Verification contract — {phase}:",
            "  Declared receipts are authoritative; ad-hoc commands are "
            "exploratory and must not invalidate a declared receipt.",
            "  Authoritative receipts:",
        ]
        lines.extend(_gate_line(contract, e, p, ctx) for e, p in relevant)
        return "\n".join(lines)

    if phase in FINAL_PHASES:
        relevant = [(e, p) for (e, p) in annotated if e.hook == "before_delivery"]
        if not relevant:
            return None
        lines = [
            f"Verification contract — {phase}:",
            "  Require gates block on missing, failed, or stale receipts; "
            "warnings are visible and shipping-allowed.",
        ]
        lines.extend(_gate_line(contract, e, p, ctx) for e, p in relevant)
        return "\n".join(lines)

    return None
