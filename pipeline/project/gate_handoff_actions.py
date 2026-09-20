"""gate_handoff_actions.py — which actions a gate handoff may offer.

One question, one home: given a blocking verification-gate failure set (live
:class:`~pipeline.project.gate_failure_set.GateFailure` objects) or the
artifacts persisted from one, *which operator actions is it honest to publish*
in ``available_actions``?

Two axes decide the menu:

* **hygiene** — every failure is agent-unfixable, so a repair retry would burn
  rounds on something no agent can reach. Read from each failure's explicit
  ``failure_kind``, never inferred from severity (a ``timeout`` is
  agent-unfixable yet carries P1).
* **env-retryable** — every failure is specifically an ``env_failure`` *and*
  the persisted record proves which gates to re-execute. ``retry_verification``
  re-runs exactly the persisted blocking set with no agent, so the record must
  be complete before it is offered: a valid, duplicate-free identity set whose
  primary is a member, a ``receipt_evidence`` pointer on **every** element, and
  a findings-to-identities command agreement. Anything less and the engine
  would be promising a re-execution it cannot address — so the action is simply
  not offered and the operator keeps waiver / halt.

The admission policy is deliberately fail-closed and evidence-first: it asks
the persisted record to prove itself rather than reconstructing what the run
"probably" blocked on. Nothing here executes a gate, reads the ledger, touches
the filesystem, or mutates state — it is pure policy over data that routing
(:mod:`pipeline.project.gate_repair`) then publishes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pipeline.control.handoff_routing import GateIdentity
from pipeline.project import gate_failure_set

#: The failure kind the engine can retry by re-executing the gate alone. The
#: operator repairs the environment outside the run; nothing in the checkout
#: changes, so no agent round is involved.
ENV_FAILURE_KIND = "env_failure"

#: Artifacts key holding where inside a declarative loop the pause was raised
#: (``loop_key`` / ``loop_phases`` / ``round`` / ``phase``). Written by routing
#: at pause time and read by the ``retry_verification`` resume, which must
#: continue the loop at the member *after* the raising phase instead of
#: re-entering the round from its first member. Absent for a top-level phase,
#: which needs no position. The identity parsers ignore it like any other extra
#: key.
LOOP_POSITION_KEY = "gate_loop_position"


# ── persisted-record parsers (strict, fail-closed) ──────────────────────────


def persisted_gate_identities(
    artifacts: Any,
) -> tuple[GateIdentity, ...] | None:
    """Parse ``gate_identities`` into a complete identity set, or ``None``.

    Returns the set with the primary (``gate_identity``) first, and ``None``
    for every defect: a missing / empty / non-list ``gate_identities``, an
    element that is not a mapping or lacks a non-empty ``command`` / ``hook``
    or a string ``phase``, a duplicated identity, or a primary that is missing,
    malformed, or not a member of the set.

    Extra keys on an element — ``receipt_evidence`` among them — are ignored
    here: this parser answers *which gates*, and nothing else.  Whether those
    gates carry evidence is :func:`persisted_gate_evidence`'s separate question,
    so a caller can tell "the set is malformed" from "the set is fine but
    unproven".
    """
    if not isinstance(artifacts, Mapping):
        return None
    raw = artifacts.get("gate_identities")
    if not isinstance(raw, list | tuple) or not raw:
        return None
    resolved: list[GateIdentity] = []
    for item in raw:
        identity = _identity_of(item)
        if identity is None or identity in resolved:
            return None
        resolved.append(identity)
    primary = _identity_of(artifacts.get("gate_identity"))
    if primary is None or primary not in resolved:
        return None
    # Primary first: it names the handoff and classifies its route.
    return (primary, *[item for item in resolved if item != primary])


def persisted_gate_evidence(artifacts: Any) -> dict[GateIdentity, str] | None:
    """Map each persisted identity to its receipt evidence path, or ``None``.

    ``None`` whenever the identity set itself is malformed, or whenever ANY
    element lacks a non-empty string ``receipt_evidence``. Partial evidence is
    not evidence: a retry that re-executed a gate it could not tie back to a
    failing receipt would be re-running on faith.
    """
    identities = persisted_gate_identities(artifacts)
    if identities is None:
        return None
    raw = artifacts.get("gate_identities")
    evidence: dict[GateIdentity, str] = {}
    for item in raw:
        identity = _identity_of(item)
        if identity is None:
            return None
        path = item.get("receipt_evidence")
        if not isinstance(path, str) or not path.strip():
            return None
        evidence[identity] = path
    if set(evidence) != set(identities):
        return None
    return evidence


def _identity_of(item: Any) -> GateIdentity | None:
    """A complete :class:`GateIdentity` from one persisted element, or ``None``."""
    if not isinstance(item, Mapping):
        return None
    command, hook, phase = item.get("command"), item.get("hook"), item.get("phase")
    if not (
        isinstance(command, str) and command
        and isinstance(hook, str) and hook
        and isinstance(phase, str)
    ):
        return None
    return GateIdentity(command, hook, phase)


# ── findings classification ─────────────────────────────────────────────────


def findings_are_hygiene(findings: Any) -> bool:
    """Whether EVERY persisted finding is agent-unfixable.

    Which actions to offer follows from what the failures ARE, so read each
    persisted ``failure_kind`` rather than inferring it from severity: a timeout
    is agent-unfixable (waiver / halt only) yet carries P1, so the severity
    proxy would silently offer it a repair retry. Read over the whole list, not
    its first member — a handoff can block on several commands, and one still
    agent-fixable failure keeps a repair retry a real option.
    """
    if not isinstance(findings, list | tuple) or not findings:
        return False
    verdicts: list[bool] = []
    for finding in findings:
        if not isinstance(finding, dict):
            return False
        kind = str(finding.get("failure_kind") or "")
        verdicts.append(
            kind in gate_failure_set.AGENT_UNFIXABLE_KINDS
            if kind
            else finding.get("severity") == "P3"
        )
    return all(verdicts)


def env_retry_eligible(findings: Any) -> bool:
    """Whether every persisted finding is explicitly an ``env_failure``.

    No severity proxy and no default: a finding written without an explicit
    ``failure_kind`` is not evidence of an environment failure, and a set that
    mixes ``env_failure`` with a timeout or a real test failure is not one the
    engine can close by re-running the command.
    """
    if not isinstance(findings, list | tuple) or not findings:
        return False
    return all(
        isinstance(finding, dict)
        and finding.get("failure_kind") == ENV_FAILURE_KIND
        for finding in findings
    )


def env_retry_admissible(artifacts: Any) -> bool:
    """Whether a persisted record may be offered ``retry_verification``.

    Every clause is a precondition for a re-execution the engine can actually
    address and prove:

    * every finding is an explicit ``env_failure`` (nothing an agent owns);
    * the identity set parses completely, without duplicates, primary included;
    * every identity carries a ``receipt_evidence`` pointer;
    * the findings and the identities name the same commands — a record whose
      blocking findings and re-executable identities disagree describes two
      different failure sets, and the engine must not guess which one the
      operator decided on.
    """
    if not isinstance(artifacts, Mapping):
        return False
    findings = artifacts.get("findings")
    if not env_retry_eligible(findings):
        return False
    identities = persisted_gate_identities(artifacts)
    if identities is None:
        return False
    if persisted_gate_evidence(artifacts) is None:
        return False
    finding_commands = {
        finding.get("command")
        for finding in findings
        if isinstance(finding, dict)
    }
    return finding_commands == {identity.command for identity in identities}


# ── the menu ────────────────────────────────────────────────────────────────


def verification_handoff_actions(
    profile: Any, *, hygiene: bool, env_retryable: bool = False,
) -> tuple[str, ...]:
    """Return only actions the current profile can execute for this failure.

    An env-only set whose record proves its blocking gates leads with
    ``retry_verification``: the operator's likeliest next move is to repair the
    environment and have the engine re-run exactly those gates. Every other
    menu is unchanged — a hygiene set without that proof still offers waiver /
    halt only, and a set with any agent-fixable member keeps its repair retry.
    """
    from pipeline.runtime.roles import PhaseHandoffAction

    if hygiene:
        if env_retryable:
            return (
                PhaseHandoffAction.RETRY_VERIFICATION.value,
                PhaseHandoffAction.CONTINUE_WITH_WAIVER.value,
                PhaseHandoffAction.HALT.value,
            )
        return (
            PhaseHandoffAction.CONTINUE_WITH_WAIVER.value,
            PhaseHandoffAction.HALT.value,
        )
    return (
        (
            PhaseHandoffAction.CONTINUE.value,
            PhaseHandoffAction.RETRY_FEEDBACK.value,
            PhaseHandoffAction.HALT.value,
            PhaseHandoffAction.CONTINUE_WITH_WAIVER.value,
        )
        if _repair_step(profile) is not None
        else (
            PhaseHandoffAction.CONTINUE.value,
            PhaseHandoffAction.HALT.value,
            PhaseHandoffAction.CONTINUE_WITH_WAIVER.value,
        )
    )


def _repair_step(profile: Any) -> Any:
    """The profile's ``repair_changes`` step, read through the routing seam.

    Deferred to :mod:`pipeline.project.gate_repair` rather than resolved here so
    the menu and the repair-route decision read the profile through exactly one
    seam — the one unit tests already drive routing through.
    """
    from pipeline.project.gate_repair import _repair_step as resolve

    return resolve(profile)


__all__ = [
    "ENV_FAILURE_KIND",
    "LOOP_POSITION_KEY",
    "env_retry_admissible",
    "env_retry_eligible",
    "findings_are_hygiene",
    "persisted_gate_evidence",
    "persisted_gate_identities",
    "verification_handoff_actions",
]
