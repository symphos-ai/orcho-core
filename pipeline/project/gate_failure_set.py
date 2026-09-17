"""gate_failure_set.py — the set of failed required gates for one hook firing.

A scheduled gate hook selects a *set* of required commands, so one firing can
end with more than one of them red. This module owns that set: what a single
gate failure is, which failure kinds no repair agent can resolve, and how a set
of failures is rendered into the two operator/agent-facing payloads —

* the **repair critique** (ADR 0081: the failed command output *is* the
  critique), which must carry every failing command's evidence, and
* the **gate handoff artifacts**, whose ``findings`` must list every failing
  command so the operator decision surface is never a strict subset of the
  blocking failures.

Rendering only. Deciding what to do with the set (repair, handoff, abort) and
performing it stays in :mod:`pipeline.project.gate_repair`; a single-failure set
renders byte-identically to the pre-set single-gate payloads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Failure kinds no repair agent can resolve from inside the run: the fix lives
# in the environment, the declared contract, or the operator's judgment. They
# route straight to the operator handoff instead of burning repair rounds.
AGENT_UNFIXABLE_KINDS = frozenset({
    "provenance_failure",
    "env_failure",
    # A repair agent cannot establish a missing or unavailable Git subject
    # identity.  Keep this fail-closed, but route it to the same operator
    # handoff path as other execution-environment failures rather than
    # spending repair-loop rounds on an external precondition.
    "unverifiable",
    # A command that ran out of wall-clock is not a failing test to repair: the
    # budget is declared in the plugin contract, and a genuine hang is a
    # diagnosis, not a code edit. Routed to the operator, but NOT as hygiene —
    # see :func:`finding_severity`.
    "timeout",
})

# Hygiene, in the severity sense, means "the proof machinery is broken, not the
# change". A timeout is agent-unfixable too, but it leaves the required gate
# with NO verdict at all — that is a P1 blocker, not a P3 note.
_HYGIENE_SEVERITY_KINDS = AGENT_UNFIXABLE_KINDS - {"timeout"}


@dataclass(frozen=True, slots=True)
class GateFailure:
    """One required gate command that failed at a hook, with its proof."""

    entry: Any
    receipt: dict
    classification: Any

    @property
    def command(self) -> str:
        return str(getattr(self.entry, "command", "") or "")

    @property
    def gate_set(self) -> str:
        return str(getattr(self.entry, "primary_gate_set", "") or "")

    @property
    def failure_kind(self) -> str:
        return str(getattr(self.classification, "failure_kind", "") or "") or "test_failure"

    @property
    def hygiene(self) -> bool:
        return getattr(self.classification, "failure_kind", None) in AGENT_UNFIXABLE_KINDS

    @property
    def evidence(self) -> str:
        from pipeline.verification_failure import format_receipt_failure

        return format_receipt_failure(self.classification, self.receipt)


def is_hygiene_failure(classification: Any) -> bool:
    """Whether this failure is agent-unfixable (operator-owned)."""
    return getattr(classification, "failure_kind", None) in AGENT_UNFIXABLE_KINDS


def finding_severity(failure_kind: str) -> str:
    return "P3" if failure_kind in _HYGIENE_SEVERITY_KINDS else "P1"


def required_fix(failure_kind: str, command: str) -> str:
    """The one action that resolves this failure, addressed to whoever can act."""
    if failure_kind == "timeout":
        return (
            f"The {command!r} gate produced no result: it did not finish within "
            "its wall-clock budget. Either raise "
            f"verification.commands[{command!r}].timeout in the project plugin "
            "if the command is honestly that long, or diagnose the hang — an "
            "empty output tail with the duration pinned to the budget means it "
            "never started producing work. Then rerun the gate, or choose an "
            "explicit waiver."
        )
    if failure_kind in _HYGIENE_SEVERITY_KINDS:
        return (
            "Fix the verification environment outside the agent or choose an "
            "explicit waiver."
        )
    return "Fix the failing verification command and rerun it."


def all_hygiene(failures: tuple[GateFailure, ...]) -> bool:
    """Whether EVERY failure in the set is agent-unfixable.

    The handoff's action set follows the whole set, not its first member: while
    one failure is still agent-fixable, a repair retry is a real option, so the
    waiver-only action set is offered exactly when nothing in the set can be
    repaired.
    """
    return bool(failures) and all(failure.hygiene for failure in failures)


# ── critique (ADR 0081: the failed command output IS the critique) ───────────


def critique(failures: tuple[GateFailure, ...]) -> tuple[str, str]:
    """Render ``(last_critique, last_test_output)`` for a whole failure set.

    Every failing command contributes its own block; a repair round that only
    ever saw the first block would fix one command and leave the rest red.
    """
    if not failures:
        return "", ""
    blocks = [_critique_block(failure) for failure in failures]
    outputs = [_test_output_block(failure) for failure in failures]
    if len(failures) == 1:
        return blocks[0], outputs[0]
    commands = ", ".join(failure.command for failure in failures)
    header = (
        f"{len(failures)} required verification gates failed: {commands}. "
        "Every command below must pass — fixing only one leaves the gate red."
    )
    return "\n\n".join([header, *blocks]), "\n\n".join(outputs)


def _critique_block(failure: GateFailure) -> str:
    evidence = failure.evidence
    parts = [
        "Required verification gate failed.",
        f"Gate set: {failure.gate_set}",
        f"Command: {failure.command}",
        evidence,
    ]
    if getattr(failure.classification, "failure_kind", None) == "test_failure":
        detail = failure.receipt.get("detail") or ""
        stderr = failure.receipt.get("stderr_tail") or ""
        stdout = failure.receipt.get("stdout_tail") or ""
        if detail:
            parts.append(f"Detail: {detail}")
        if stderr:
            parts.append(f"stderr:\n{stderr}")
        if stdout:
            parts.append(f"stdout:\n{stdout}")
    return "\n".join(parts)


def _test_output_block(failure: GateFailure) -> str:
    evidence = failure.evidence
    if getattr(failure.classification, "failure_kind", None) != "test_failure":
        return evidence
    stdout = failure.receipt.get("stdout_tail") or ""
    stderr = failure.receipt.get("stderr_tail") or ""
    return "\n".join(part for part in (evidence, stdout, stderr) if part)


# ── handoff artifacts ───────────────────────────────────────────────────────


def short_summary(failures: tuple[GateFailure, ...]) -> str:
    """One-line-per-command summary; identical to the evidence when single."""
    if not failures:
        return ""
    if len(failures) == 1:
        return failures[0].evidence
    return "\n".join(
        f"{failure.command}: {failure.evidence}" for failure in failures
    )


def findings(failures: tuple[GateFailure, ...]) -> list[dict[str, Any]]:
    """One finding per failing command — never a subset of the blocking set."""
    return [
        {
            "id": f"verification_gate_{failure.failure_kind}",
            "severity": finding_severity(failure.failure_kind),
            "title": f"Verification gate {failure.failure_kind}",
            "body": failure.evidence,
            "required_fix": required_fix(failure.failure_kind, failure.command),
            "failure_kind": failure.failure_kind,
            "command": failure.command,
        }
        for failure in failures
    ]


def handoff_artifacts(
    failures: tuple[GateFailure, ...], *, hook: str, gate_phase: str,
) -> dict[str, Any]:
    """Durable artifacts for one gate handoff over a whole failure set.

    The singular ``gate_command`` / ``gate_identity`` keep naming the primary
    (first) failure — waiver identity and handoff-route classification are
    single-identity contracts — while ``gate_commands`` / ``gate_identities``
    carry the complete blocking set for the retry path and the operator.
    """
    primary = failures[0]
    identities = [
        {"command": failure.command, "hook": hook, "phase": gate_phase}
        for failure in failures
    ]
    return {
        "gate_command": primary.command,
        "gate_set": primary.gate_set,
        "gate_identity": identities[0],
        "gate_commands": [failure.command for failure in failures],
        "gate_identities": identities,
        "findings": findings(failures),
        "short_summary": short_summary(failures),
    }


__all__ = [
    "AGENT_UNFIXABLE_KINDS",
    "GateFailure",
    "all_hygiene",
    "critique",
    "finding_severity",
    "findings",
    "handoff_artifacts",
    "is_hygiene_failure",
    "required_fix",
    "short_summary",
]
