# SPDX-License-Identifier: Apache-2.0
"""verification_policy.py — per-gate effective delivery policy + gap partition.

A small, pure layer between Stage 5 readiness / Stage 6 delivery and the
policy-aware UX surfaces (readiness block, delivery banner, DONE summary). It
answers two questions, deterministically and without side effects:

1. :func:`effective_delivery_policy_by_command` — for each required delivery
   command (the receipt view from
   :func:`pipeline.verification_readiness.resolve_delivery_selection`), what is
   its *effective* delivery policy:

   * ``manual_only`` when the command is in the caller-supplied manual/operator
     set (``sdk.verify.manual_or_operator_only_commands``);
   * otherwise the policy of the matching delivery-hook gate in the resolved
     :class:`~pipeline.verification_selection.ScheduledGatePlan`
     (``before_delivery`` / ``after_phase(implement)`` — strictest wins when a
     command is scheduled at more than one delivery position);
   * otherwise the boundary delivery policy the caller resolved via
     :func:`pipeline.verification_delivery.resolve_delivery_policy`.

2. :func:`partition_gaps` — split the per-command receipt gaps (missing /
   failed / stale) into ``blocking`` (effective policy ``require``), ``warning``
   (``warn`` / ``suggest``) and ``manual_only`` buckets, preserving input order.

Invariant (ADR 0090, "never falsely green"): a ``require``-policy gap MUST stay
blocking. Conversely a ``manual_only`` / ``operator_only`` gap is visible but
NEVER blocking and is NEVER counted as a missing-required receipt — reclassifying
it as manual must not hide a genuine auto-required gap, so the policy lookup
treats manual membership as authoritative only for commands the caller already
deemed manual/operator-only, and require gaps are partitioned strictly on the
effective ``require`` policy.

This module imports no orchestration (``pipeline.project.*``); it reads only the
pure ``pipeline.verification_*`` layer. ``sdk.verify`` and
``pipeline.verification_selection`` may be imported lazily, but neither is needed
here because the caller supplies the manual set, the resolved plan, and the
boundary policy.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pipeline.verification_contract import SCHEDULE_POLICIES, VerificationContract
from pipeline.verification_readiness import resolve_delivery_selection

__all__ = [
    "GapEntry",
    "GapPartition",
    "effective_delivery_policy_by_command",
    "consequence_by_command",
    "partition_gaps",
]


# Delivery-relevant (hook, phase) positions — a deliberate literal copy of
# ``pipeline.verification_readiness._DELIVERY_HOOKS`` so this pure module stays
# self-contained while resolving the same gates the readiness/delivery surfaces
# treat as official delivery proof.
_DELIVERY_HOOKS: tuple[tuple[str, str], ...] = (
    ("after_phase", "implement"),
    ("before_delivery", ""),
)

# Receipt statuses that constitute an unproven gap (everything but ``present``).
_GAP_STATUSES: frozenset[str] = frozenset({"missing", "failed", "stale", "unverifiable"})


@dataclass(frozen=True)
class GapEntry:
    """One unproven required delivery command, with its effective policy.

    ``status`` is the receipt classification (``missing`` / ``failed`` /
    ``stale``); ``policy`` is the effective delivery policy for the command
    (``require`` / ``warn`` / ``suggest`` / ``manual_only``).
    """

    command: str
    status: str
    policy: str


@dataclass(frozen=True)
class GapPartition:
    """Per-gate gaps split by effective delivery policy, input order preserved.

    ``blocking`` holds the ``require``-policy gaps (the only ones that may block
    delivery / drive a fix default action). ``warning`` holds ``warn`` /
    ``suggest`` gaps — delivery is allowed by policy. ``manual_only`` holds gaps
    on commands the caller marked manual/operator-only — visible, but neither
    blocking nor counted as missing required (ADR 0090). The parallel
    ``*_commands`` / ``*_policies`` properties expose name and policy tuples for
    callers that render parallel lists.
    """

    blocking: tuple[GapEntry, ...] = ()
    warning: tuple[GapEntry, ...] = ()
    operator: tuple[GapEntry, ...] = ()

    @property
    def has_blocking(self) -> bool:
        return bool(self.blocking)

    @property
    def blocking_commands(self) -> tuple[str, ...]:
        return tuple(e.command for e in self.blocking)

    @property
    def warning_commands(self) -> tuple[str, ...]:
        return tuple(e.command for e in self.warning)

    @property
    def operator_commands(self) -> tuple[str, ...]:
        return tuple(e.command for e in self.operator)

    @property
    def blocking_policies(self) -> tuple[str, ...]:
        return tuple(e.policy for e in self.blocking)

    @property
    def warning_policies(self) -> tuple[str, ...]:
        return tuple(e.policy for e in self.warning)

    def commands_with_status(self, status: str) -> tuple[str, ...]:
        """``blocking`` commands whose receipt classified as ``status``.

        Used by the delivery/readiness surfaces to reconstruct the
        missing/failed/stale required lists from the blocking bucket without
        re-classifying — only ``require``-policy gaps count as required gaps.
        """
        return tuple(e.command for e in self.blocking if e.status == status)


def effective_delivery_policy_by_command(
    contract: VerificationContract,
    plan: Any,
    manual_set: set[str] | frozenset[str] | Mapping[str, Any] | None,
    boundary_policy: str,
) -> dict[str, str]:
    """Effective delivery policy for each required delivery command, in order.

    For every command in the delivery selection's receipt view:

    * ``manual_only`` when the command is in ``manual_set`` (the raw
      manual/operator-only set from ``sdk.verify.manual_or_operator_only_commands``);
    * otherwise the policy of the matching ``ScheduledGateEntry`` at a delivery
      hook in ``plan`` — when a command is scheduled at more than one delivery
      position the strictest policy (by :data:`SCHEDULE_POLICIES` rank) wins, so
      a ``require`` gate is never softened by a sibling ``warn`` entry;
    * otherwise ``boundary_policy`` (the value the caller resolved via
      :func:`pipeline.verification_delivery.resolve_delivery_policy`) — this is
      the path a ``contract.required`` command takes when no delivery-hook gate
      schedules it.

    Pure: ``manual_set``, ``plan``, and ``boundary_policy`` are all supplied by
    the caller. Returns an ordered ``command -> policy`` dict.
    """
    manual = set(manual_set or ())
    plan_policy = _delivery_policy_by_command_from_plan(plan)
    result: dict[str, str] = {}
    for command in resolve_delivery_selection(contract, plan).receipt_commands:
        if command in manual:
            result[command] = "manual"
            continue
        scheduled = plan_policy.get(command)
        result[command] = scheduled if scheduled else boundary_policy
    return result


def consequence_by_command(
    status_by_command: Mapping[str, Any],
    declared_policy_by_command: Mapping[str, str],
) -> dict[str, str]:
    """Resolve post-result consequence without changing declared policy.

    A typed provenance/environment failure is a hygiene warning at readiness and
    delivery, even when its declared policy is ``require``.  This is deliberately
    separate from execution policy: callers retain the canonical policy map for
    rendering and audit, then use this result only for consequence routing.
    """
    consequences: dict[str, str] = {}
    for command, policy in declared_policy_by_command.items():
        if policy == "require":
            consequence = "required_action"
        elif policy in ("warn", "suggest"):
            consequence = "warning"
        else:
            consequence = "none"
        classification = status_by_command.get(command)
        if getattr(classification, "failure_kind", None) in {
            "provenance_failure",
            "env_failure",
        }:
            consequence = "warning"
        consequences[command] = consequence
    return consequences


def partition_gaps(
    status_by_command: Mapping[str, Any],
    policy_by_command: Mapping[str, str],
    consequence_by_command: Mapping[str, str] | None = None,
) -> GapPartition:
    """Split unproven-receipt gaps into blocking / warning / manual_only buckets.

    ``status_by_command`` maps command -> receipt status; values may be a plain
    status string or any object exposing a ``.status`` attribute (so a
    ``command -> ReceiptClassification`` mapping is accepted directly).
    ``policy_by_command`` is the output of
    :func:`effective_delivery_policy_by_command`.

    Only gap statuses (``missing`` / ``failed`` / ``stale``) are partitioned;
    ``present`` commands contribute nothing. Bucketing by effective policy:

    * ``require`` → ``blocking`` (the "never falsely green" invariant: a require
      gap is always a blocker);
    * ``warn`` / ``suggest`` → ``warning`` (delivery allowed by policy);
    * ``manual_only`` → ``manual_only`` (visible, never blocking, never missing
      required);
    * ``manual`` → ``manual_only`` (visible, never blocking).

    Input order is preserved within each bucket.
    """
    blocking: list[GapEntry] = []
    warning: list[GapEntry] = []
    operator: list[GapEntry] = []
    for command, raw_status in status_by_command.items():
        status = getattr(raw_status, "status", raw_status)
        if status not in _GAP_STATUSES:
            continue
        policy = policy_by_command.get(command, "")
        entry = GapEntry(command=command, status=status, policy=policy)
        consequence = (consequence_by_command or {}).get(command)
        if policy == "manual":
            operator.append(entry)
        elif consequence == "required_action" or (
            consequence is None and policy == "require"
        ):
            blocking.append(entry)
        elif consequence == "warning" or policy in ("warn", "suggest"):
            warning.append(entry)
    return GapPartition(
        blocking=tuple(blocking),
        warning=tuple(warning),
        operator=tuple(operator),
    )


# ── Internals ────────────────────────────────────────────────────────────────


def _delivery_policy_by_command_from_plan(plan: Any) -> dict[str, str]:
    """Strictest delivery-hook policy per command in ``plan`` (empty if none).

    Scans the plan's entries for the delivery positions in
    :data:`_DELIVERY_HOOKS` and keeps, per command, the policy with the highest
    :data:`SCHEDULE_POLICIES` rank. Tolerant of a ``None`` plan or entries that
    lack the expected attributes.
    """
    if plan is None:
        return {}
    out: dict[str, str] = {}
    for entry in getattr(plan, "entries", ()) or ():
        position = (getattr(entry, "hook", ""), getattr(entry, "phase", ""))
        if position not in _DELIVERY_HOOKS:
            continue
        command = getattr(entry, "command", "")
        policy = getattr(entry, "policy", "")
        if not command or not policy:
            continue
        current = out.get(command)
        if current is None or _policy_rank(policy) > _policy_rank(current):
            out[command] = policy
    return out


def _policy_rank(policy: str) -> int:
    """Rank a policy by strictness; unknown policies rank lowest (-1)."""
    try:
        return SCHEDULE_POLICIES.index(policy)
    except ValueError:
        return -1
