"""
pipeline/verification_waiver.py — durable verification-gate waiver reader.

A ``phase_handoff_waiver`` (ADR 0073) is the durable record that an operator
(or, under the implement auto-waiver fallback, the pipeline itself) accepted a
rejected/incomplete phase verdict. The verification-gate repair loop reuses the
same record: when a required verification gate fails, the operator may resolve
the pause with ``continue_with_waiver``, which persists a waiver whose
``handoff_id`` is ``gate:<command>:<round>`` (see
:func:`pipeline.project.gate_repair`).

This module is the provider-neutral *reader* over that durable record. It turns
the stored waiver(s) into verification-gate waivers keyed by the exact gate
command, so the Stage-6 delivery assessment can let a precisely-waived required
receipt through without unblocking any neighbouring gate.

Design constraints:

* Pure. No orchestration imports, no subprocess, no IO beyond reading the
  mappings handed in.
* Identity comes ONLY from the durable structure — an explicit ``gate_command``
  field, or the ``gate:<command>:<round>`` ``handoff_id`` shape. Terminal prose
  is never parsed.
* Review / plan / implement-incompleteness waivers (whose ``handoff_id`` is not
  ``gate:...`` and which carry no explicit ``gate_command``) are NOT returned —
  they do not cover verification receipts.
* Never raises on malformed input; degrades to an empty mapping.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

#: ``state.extras`` / ``session`` key holding the durable waiver payload(s).
#: Mirrors :data:`pipeline.project.handoff_waiver.WAIVER_KEY`.
WAIVER_KEY = "phase_handoff_waiver"

#: Prefix marking a verification-gate handoff id (``gate:<command>:<round>``).
_GATE_PREFIX = "gate:"


@dataclass(frozen=True)
class GateWaiver:
    """A durable waiver resolved to one exact verification-gate command.

    ``gate_command`` is the literal gate command the waiver covers — either the
    explicit ``gate_command`` field on the record or the ``<command>`` segment
    of a ``gate:<command>:<round>`` ``handoff_id``. The remaining fields carry
    the durable provenance the delivery banner / persisted evidence surface.
    """

    gate_command: str
    handoff_id: str
    waiver_text: str
    note: str | None = None
    phase: str | None = None
    decided_by: str | None = None


def _gate_command_of(record: Mapping) -> str | None:
    """Resolve the exact gate command a waiver record covers, or ``None``.

    Identity is taken ONLY from the durable structure:

    * an explicit non-empty ``gate_command`` field wins; otherwise
    * a ``handoff_id`` of the form ``gate:<command>:<round>`` — the ``gate:``
      prefix is stripped and the round is split off with ``rpartition(':')`` so
      a command that itself contains ``':'`` is preserved intact.

    Any other shape (review/plan/implement-incompleteness waivers, missing or
    malformed ids) yields ``None`` and the record is skipped by the caller.
    """
    explicit = record.get("gate_command")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()

    handoff_id = record.get("handoff_id")
    if not isinstance(handoff_id, str) or not handoff_id.startswith(_GATE_PREFIX):
        return None
    remainder = handoff_id[len(_GATE_PREFIX):]
    command, sep, _round = remainder.rpartition(":")
    if not sep or not command:
        # Not the ``gate:<command>:<round>`` shape (no round segment).
        return None
    return command


def _as_str_or_none(value: object) -> str | None:
    """Coerce an optional string field, leaving non-strings as ``None``."""
    return value if isinstance(value, str) else None


def _waiver_from_record(record: Mapping) -> GateWaiver | None:
    """Build a :class:`GateWaiver` from one record, or ``None`` if irrelevant."""
    command = _gate_command_of(record)
    if command is None:
        return None
    waiver_text = record.get("waiver_text")
    return GateWaiver(
        gate_command=command,
        handoff_id=str(record.get("handoff_id") or ""),
        waiver_text=waiver_text if isinstance(waiver_text, str) else "",
        note=_as_str_or_none(record.get("note")),
        phase=_as_str_or_none(record.get("phase")),
        decided_by=_as_str_or_none(record.get("decided_by")),
    )


def _records_under_key(source: Mapping | None) -> list[Mapping]:
    """Normalise the value under ``WAIVER_KEY`` to a list of mapping records.

    Accepts a single ``dict`` (today's shape) or a ``list`` of dicts (kept open
    for the future), ignoring every other / malformed form.
    """
    if not isinstance(source, Mapping):
        return []
    raw = source.get(WAIVER_KEY)
    if isinstance(raw, Mapping):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return [item for item in raw if isinstance(item, Mapping)]
    return []


def collect_gate_waivers(
    extras: Mapping | None,
    session: Mapping | None = None,
) -> dict[str, GateWaiver]:
    """Collect verification-gate waivers keyed by exact gate command.

    Reads the durable ``phase_handoff_waiver`` record(s) from ``extras`` (the
    in-process source of truth, taking priority) and ``session`` (the
    durably-persisted fallback used by a fresh-process resume). Records are
    merged by gate command with ``extras`` winning on conflict.

    Only records that resolve to a verification-gate command via
    :func:`_gate_command_of` are returned; everything else is dropped. Never
    raises — malformed input degrades to ``{}``.
    """
    result: dict[str, GateWaiver] = {}
    try:
        # Session first so extras can override the same command.
        for source in (session, extras):
            for record in _records_under_key(source):
                waiver = _waiver_from_record(record)
                if waiver is not None:
                    result[waiver.gate_command] = waiver
    except Exception:
        return {}
    return result


__all__ = ["GateWaiver", "WAIVER_KEY", "collect_gate_waivers"]
