"""pipeline/control/operator_decisions.py — parse ``--decision`` flags.

Operators can override runner-owned gate decisions and similar control
points without gate-specific flags::

    --decision contract_check=run
    --decision contract_check=skip
    --decision-feedback "Tiny docs-only change."

This module provides the shared parser used by both ``orcho run`` and
``orcho cross`` so both subcommands recognise the surface, even though
the set of *applicable* targets depends on the subcommand:

- ``orcho cross`` accepts ``contract_check=run|skip``.
- ``orcho run`` accepts no targets today; supplying any target fails
  loudly rather than being silently parsed and dropped.

Future targets (``validate_plan=approve``, …) plug into the same surface
by extending ``DECISION_TARGETS_BY_SUBCOMMAND``; no new flag is needed.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


class OperatorDecisionError(ValueError):
    """Raised when ``--decision`` / ``--decision-feedback`` are malformed
    or carry a target/decision not applicable to the active subcommand.
    """


@dataclass(frozen=True)
class OperatorDecisionOverride:
    """One operator-supplied decision override for a control target.

    ``target`` and ``decision`` are validated against
    ``DECISION_TARGETS_BY_SUBCOMMAND`` at parse time. ``feedback`` is
    optional free-form text; when supplied, exactly one decision must
    accompany it (rule enforced by ``parse_operator_decisions``).
    """
    target: str
    decision: str
    feedback: str = ""


# Per-subcommand allowlist of accepted decision targets and their
# decision verbs. Empty target set means "subcommand recognises the
# flag surface but has no applicable targets today" — any supplied
# target fails clearly instead of being silently parsed and dropped.
#
# The ``commit`` target resolves the post-release commit-decision gate:
# the decision verbs map 1:1 onto
# :class:`pipeline.runtime.roles.CommitDecisionAction`. Auxiliary
# ``--commit-strategy`` / ``--commit-message`` / ``--commit-no-*``
# flags refine the ``approve`` verb but are wired separately by the
# CLI runner, not by this allowlist.
DECISION_TARGETS_BY_SUBCOMMAND: Mapping[str, Mapping[str, frozenset[str]]] = {
    "run": {
        "commit": frozenset({"fix", "approve", "apply", "skip", "halt"}),
    },
    "cross": {
        "contract_check": frozenset({"run", "skip"}),
        "commit": frozenset({"fix", "approve", "apply", "skip", "halt"}),
    },
}


def _split_one(raw: str) -> tuple[str, str]:
    if "=" not in raw:
        raise OperatorDecisionError(
            f"--decision: expected TARGET=DECISION, got {raw!r}"
        )
    target, _, decision = raw.partition("=")
    target = target.strip()
    decision = decision.strip()
    if not target or not decision:
        raise OperatorDecisionError(
            f"--decision: expected TARGET=DECISION, got {raw!r}"
        )
    return target, decision


def parse_operator_decisions(
    raw_decisions: Sequence[str] | None,
    feedback: str | None,
    *,
    subcommand: str,
) -> tuple[OperatorDecisionOverride, ...]:
    """Parse repeated ``--decision`` strings + optional feedback.

    Returns the parsed overrides in the order they were supplied. The
    feedback string (if provided) is attached to the single decision in
    the list; supplying feedback with anything other than exactly one
    decision is rejected for ambiguity (we don't want to silently glue
    the same string onto multiple targets).
    """
    if subcommand not in DECISION_TARGETS_BY_SUBCOMMAND:
        raise OperatorDecisionError(
            f"unknown subcommand {subcommand!r} for --decision parsing; "
            f"known: {sorted(DECISION_TARGETS_BY_SUBCOMMAND)}"
        )
    allowed_targets = DECISION_TARGETS_BY_SUBCOMMAND[subcommand]

    raw_list = list(raw_decisions or ())
    if not raw_list:
        if feedback:
            raise OperatorDecisionError(
                "--decision-feedback supplied without --decision"
            )
        return ()

    if feedback is not None and len(raw_list) != 1:
        raise OperatorDecisionError(
            "--decision-feedback may only accompany exactly one "
            "--decision; got "
            f"{len(raw_list)} decisions"
        )

    seen: set[str] = set()
    out: list[OperatorDecisionOverride] = []
    for raw in raw_list:
        target, decision = _split_one(raw)
        if target in seen:
            raise OperatorDecisionError(
                f"--decision: duplicate target {target!r}"
            )
        seen.add(target)

        if target not in allowed_targets:
            if not allowed_targets:
                raise OperatorDecisionError(
                    f"--decision: target {target!r} is not applicable to "
                    f"`orcho {subcommand}` (no decision targets supported)"
                )
            raise OperatorDecisionError(
                f"--decision: unknown target {target!r}; supported on "
                f"`orcho {subcommand}`: {sorted(allowed_targets)}"
            )
        allowed_decisions = allowed_targets[target]
        if decision not in allowed_decisions:
            raise OperatorDecisionError(
                f"--decision {target}: unsupported decision "
                f"{decision!r}; supported: {sorted(allowed_decisions)}"
            )
        attached_feedback = feedback if feedback is not None else ""
        out.append(OperatorDecisionOverride(
            target=target,
            decision=decision,
            feedback=attached_feedback,
        ))
    return tuple(out)
