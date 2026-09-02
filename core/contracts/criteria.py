# SPDX-License-Identifier: Apache-2.0
"""core.contracts.criteria — typed plan acceptance criteria (ADR 0188).

A plan-level acceptance criterion is a typed object with a stable ID and
exactly one verification class:

* ``executable``      — proved by official scheduled gates (``gate_refs``);
* ``agent_assertion`` — inspected by an agent; advisory only;
* ``human``           — decided by an operator (``human_instructions``).

This module owns the value objects, the durable JSON shape, schema
validation, and the **single** legacy ingress normalizer that turns an old
``list[str]`` artifact into typed criteria. Nothing else in the tree may
convert prose criteria into the typed model, and no new writer emits
``list[str]``.

The module is dependency-free on purpose: schema validation must stay
importable from the parser, the artifact loader, and the evidence layer
without dragging in the pipeline.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "CRITERION_ID_RE",
    "LEGACY_CRITERION_VERIFY",
    "PHASE_ANCHORED_HOOKS",
    "VERIFY_CLASSES",
    "AcceptanceCriterion",
    "CriterionSchemaError",
    "GateRef",
    "coerce_acceptance_criteria",
    "criteria_to_wire",
    "criterion_display",
    "normalize_legacy_criteria",
    "validate_acceptance_criteria",
    "validate_acceptance_refs",
]

#: The criterion ID grammar. Every typed criterion — composer-generated or
#: hand-authored — must match it; the legacy ingress normalizer generates ids
#: in the same grammar, so there is no second accepted shape.
CRITERION_ID_RE = re.compile(r"^C[1-9][0-9]*$")

#: The three (and only three) verification classes — ADR 0188 §2.
VERIFY_CLASSES: tuple[str, ...] = ("executable", "agent_assertion", "human")

#: Schedule hooks whose gate identity is *anchored to a phase*. Every other
#: hook (``before_delivery`` / ``on_resume`` / ``manual_only``) carries an
#: empty ``phase`` — see ``pipeline.verification_selection.ScheduledGateEntry``,
#: which owns the schedule vocabulary. Only the identity *shape* is mirrored
#: here so this module stays importable without the pipeline layer; the hook
#: name itself is resolved against the run's ledger, not against a copy of the
#: hook list.
PHASE_ANCHORED_HOOKS: tuple[str, ...] = ("before_phase", "after_phase")

#: Class assigned by the legacy ingress normalizer. A prose criterion carries
#: no gate identity and no operator instructions, so the only honest class is
#: the advisory one.
LEGACY_CRITERION_VERIFY = "agent_assertion"

_GATE_REF_KEYS: frozenset[str] = frozenset({"command", "hook", "phase"})
_CRITERION_KEYS: frozenset[str] = frozenset(
    {"id", "intent", "verify", "gate_refs", "human_instructions"}
)


class CriterionSchemaError(ValueError):
    """Raised when a criterion payload does not match the ADR 0188 shape."""


@dataclass(frozen=True, slots=True)
class GateRef:
    """The complete scheduled-gate identity a criterion refers to.

    A command name alone is *not* an identity: the same command scheduled
    under one hook for two phases is two distinct official gates
    (``pipeline.verification_ledger.GateLedgerRow.identity``).
    """

    command: str
    hook: str
    phase: str

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.command, self.hook, self.phase)

    def to_dict(self) -> dict[str, str]:
        return {"command": self.command, "hook": self.hook, "phase": self.phase}

    def label(self) -> str:
        """Compact operator-facing label, e.g. ``unit @ after_phase implement``."""
        where = f"{self.hook} {self.phase}".strip()
        return f"{self.command} @ {where}" if where else self.command

    def __str__(self) -> str:  # never leak a dataclass repr into output
        return self.label()


@dataclass(frozen=True, slots=True)
class AcceptanceCriterion:
    """One typed plan acceptance criterion."""

    id: str
    intent: str
    verify: str
    gate_refs: tuple[GateRef, ...] = ()
    human_instructions: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Durable JSON shape. Class-irrelevant keys are absent, never null."""
        out: dict[str, Any] = {
            "id": self.id,
            "intent": self.intent,
            "verify": self.verify,
        }
        if self.verify == "executable":
            out["gate_refs"] = [ref.to_dict() for ref in self.gate_refs]
        if self.verify == "human":
            out["human_instructions"] = self.human_instructions
        return out

    def display(self) -> str:
        """Deterministic one-line human projection (never a value-object repr)."""
        suffix = ""
        if self.verify == "executable" and self.gate_refs:
            suffix = " — " + ", ".join(ref.label() for ref in self.gate_refs)
        return f"{self.id} [{self.verify}] {self.intent}{suffix}"

    def __str__(self) -> str:
        return self.display()


def criterion_display(value: Any) -> str:
    """Render any criterion form for user-visible output.

    Accepts a typed criterion, its durable dict form, or a bare legacy
    string, so every renderer has one call site and none of them can emit
    ``repr(value_object)``.
    """
    if isinstance(value, AcceptanceCriterion):
        return value.display()
    if isinstance(value, Mapping):
        cid = str(value.get("id") or "").strip()
        intent = str(value.get("intent") or "").strip()
        verify = str(value.get("verify") or "").strip()
        if cid and intent and verify:
            refs = value.get("gate_refs")
            suffix = ""
            if isinstance(refs, Sequence) and not isinstance(refs, (str, bytes)):
                labels = [
                    GateRef(
                        str(r.get("command") or ""),
                        str(r.get("hook") or ""),
                        str(r.get("phase") or ""),
                    ).label()
                    for r in refs
                    if isinstance(r, Mapping)
                ]
                if labels:
                    suffix = " — " + ", ".join(labels)
            return f"{cid} [{verify}] {intent}{suffix}"
    return str(value)


# ── validation ───────────────────────────────────────────────────────────────


def _require_non_empty_str(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CriterionSchemaError(f"{where} must be a non-empty string")
    return value.strip()


def _gate_ref_from_dict(value: Any, where: str) -> GateRef:
    if not isinstance(value, Mapping):
        raise CriterionSchemaError(
            f"{where} must be an object with command/hook/phase, got "
            f"{type(value).__name__}"
        )
    unknown = sorted(set(value) - _GATE_REF_KEYS)
    if unknown:
        raise CriterionSchemaError(f"{where} has unknown keys: {unknown}")
    missing = sorted(_GATE_REF_KEYS - set(value))
    if missing:
        raise CriterionSchemaError(
            f"{where} is not a complete gate identity; missing: {missing} "
            "(a command name alone is not a gate identity)"
        )
    hook = _require_non_empty_str(value["hook"], f"{where}.hook")
    phase = value["phase"]
    if not isinstance(phase, str):
        raise CriterionSchemaError(f"{where}.phase must be a string")
    phase = phase.strip()
    # The triple is always complete; ``phase`` is empty exactly when the hook
    # is not phase-anchored, which is how the scheduled-gate ledger keys those
    # identities. Requiring a phase there would make every ``before_delivery``
    # gate unaddressable; allowing one would invent an identity no ledger row
    # can match.
    if hook in PHASE_ANCHORED_HOOKS and not phase:
        raise CriterionSchemaError(
            f"{where}.phase must be a non-empty string for the phase-anchored "
            f"hook {hook!r}"
        )
    if hook not in PHASE_ANCHORED_HOOKS and phase:
        raise CriterionSchemaError(
            f"{where}.phase must be empty for the non-phase-anchored hook "
            f"{hook!r}, got {phase!r}"
        )
    return GateRef(
        command=_require_non_empty_str(value["command"], f"{where}.command"),
        hook=hook,
        phase=phase,
    )


def _criterion_from_dict(value: Mapping[str, Any], where: str) -> AcceptanceCriterion:
    unknown = sorted(set(value) - _CRITERION_KEYS)
    if unknown:
        raise CriterionSchemaError(f"{where} has unknown keys: {unknown}")

    cid = _require_non_empty_str(value.get("id"), f"{where}.id")
    if not CRITERION_ID_RE.fullmatch(cid):
        raise CriterionSchemaError(
            f"{where}.id must match {CRITERION_ID_RE.pattern} (e.g. 'C1'), "
            f"got {cid!r}"
        )
    intent = _require_non_empty_str(value.get("intent"), f"{where}.intent")
    verify = value.get("verify")
    if verify not in VERIFY_CLASSES:
        raise CriterionSchemaError(
            f"{where}.verify must be one of {list(VERIFY_CLASSES)}, got {verify!r}"
        )

    raw_refs = value.get("gate_refs")
    instructions = value.get("human_instructions")

    if verify == "executable":
        if instructions is not None:
            raise CriterionSchemaError(
                f"{where}.human_instructions is not allowed on an executable criterion"
            )
        if not isinstance(raw_refs, Sequence) or isinstance(raw_refs, (str, bytes)):
            raise CriterionSchemaError(f"{where}.gate_refs must be a list")
        if not raw_refs:
            raise CriterionSchemaError(
                f"{where}.gate_refs must name at least one official gate identity"
            )
        refs = tuple(
            _gate_ref_from_dict(r, f"{where}.gate_refs[{i}]")
            for i, r in enumerate(raw_refs)
        )
        seen: set[tuple[str, str, str]] = set()
        for ref in refs:
            if ref.identity in seen:
                raise CriterionSchemaError(
                    f"{where}.gate_refs repeats identity {ref.label()}"
                )
            seen.add(ref.identity)
        return AcceptanceCriterion(id=cid, intent=intent, verify=verify, gate_refs=refs)

    if raw_refs is not None:
        raise CriterionSchemaError(
            f"{where}.gate_refs is only allowed on an executable criterion"
        )

    if verify == "human":
        text = _require_non_empty_str(instructions, f"{where}.human_instructions")
        return AcceptanceCriterion(
            id=cid, intent=intent, verify=verify, human_instructions=text
        )

    if instructions is not None:
        raise CriterionSchemaError(
            f"{where}.human_instructions is only allowed on a human criterion"
        )
    return AcceptanceCriterion(id=cid, intent=intent, verify=verify)


def validate_acceptance_criteria(
    value: Any, *, where: str = "acceptance_criteria",
) -> tuple[AcceptanceCriterion, ...]:
    """Validate a *typed* criteria payload. Legacy strings are rejected here.

    Accepts already-typed :class:`AcceptanceCriterion` instances unchanged so
    an in-memory plan can be re-validated without a serialization round trip.
    """
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise CriterionSchemaError(f"{where} must be a list")

    criteria: list[AcceptanceCriterion] = []
    for i, entry in enumerate(value):
        loc = f"{where}[{i}]"
        if isinstance(entry, AcceptanceCriterion):
            criteria.append(_criterion_from_dict(entry.to_dict(), loc))
            continue
        if not isinstance(entry, Mapping):
            raise CriterionSchemaError(
                f"{loc} must be a typed criterion object, got "
                f"{type(entry).__name__} (new writers never emit list[str])"
            )
        criteria.append(_criterion_from_dict(entry, loc))

    seen: set[str] = set()
    for criterion in criteria:
        if criterion.id in seen:
            raise CriterionSchemaError(
                f"{where} repeats criterion id {criterion.id!r}"
            )
        seen.add(criterion.id)
    return tuple(criteria)


def validate_acceptance_refs(
    value: Any, *, where: str,
) -> tuple[str, ...]:
    """Validate one task's ``acceptance_refs`` list of criterion IDs."""
    if value is None:
        return ()
    if (not isinstance(value, Sequence) or isinstance(value, (str, bytes))
            or not all(isinstance(x, str) for x in value)):
        raise CriterionSchemaError(f"{where} must be a list of criterion ids")
    refs: list[str] = []
    for i, raw in enumerate(value):
        ref = _require_non_empty_str(raw, f"{where}[{i}]")
        if ref in refs:
            raise CriterionSchemaError(f"{where} repeats criterion id {ref!r}")
        refs.append(ref)
    return tuple(refs)


# ── legacy ingress (the ONE normalizer) ──────────────────────────────────────


def normalize_legacy_criteria(value: Sequence[str]) -> tuple[AcceptanceCriterion, ...]:
    """Turn an old ``list[str]`` artifact into typed criteria — ADR 0188 §6.

    This is the **only** place in the tree allowed to invent criterion IDs
    from array position, and it may do so only for an immutable legacy
    artifact. Legacy prose carries neither gate identity nor operator
    instructions, so every entry becomes ``agent_assertion``.
    """
    criteria: list[AcceptanceCriterion] = []
    for i, raw in enumerate(value, 1):
        intent = str(raw).strip()
        if not intent:
            continue
        criteria.append(
            AcceptanceCriterion(
                id=f"C{i}", intent=intent, verify=LEGACY_CRITERION_VERIFY,
            )
        )
    return tuple(criteria)


def coerce_acceptance_criteria(
    value: Any, *, where: str = "acceptance_criteria",
) -> tuple[AcceptanceCriterion, ...]:
    """Ingress: accept the typed shape, or route a legacy list through the normalizer.

    A mixed list (some strings, some objects) is a writer bug and is rejected.
    """
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise CriterionSchemaError(f"{where} must be a list")
    entries = list(value)
    if entries and all(isinstance(x, str) for x in entries):
        return normalize_legacy_criteria(entries)
    if any(isinstance(x, str) for x in entries):
        raise CriterionSchemaError(
            f"{where} mixes legacy strings with typed criterion objects"
        )
    return validate_acceptance_criteria(entries, where=where)


def criteria_to_wire(
    criteria: Sequence[AcceptanceCriterion],
) -> list[dict[str, Any]]:
    """Durable/wire projection of typed criteria, in declaration order."""
    return [c.to_dict() for c in criteria]
