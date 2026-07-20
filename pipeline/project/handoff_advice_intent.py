# SPDX-License-Identifier: Apache-2.0
"""Lossless parsing of the handoff advisor's proposed intent."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

OPERATION_KINDS = frozenset({"repair", "preserve", "revert", "remove", "waive", "stop"})
EFFECT_KINDS = frozenset({"preserve", "advance", "violate", "unknown"})


@dataclass(frozen=True, slots=True)
class ProposedOperation:
    kind: str
    target: str
    raw: Any


@dataclass(frozen=True, slots=True)
class ContractEffect:
    invariant_id: str
    effect: str
    raw: Any


@dataclass(frozen=True, slots=True)
class AdviceIntent:
    proposed_operations: tuple[ProposedOperation, ...] = ()
    contract_effects: tuple[ContractEffect, ...] = ()
    diagnostics: tuple[str, ...] = ()


def parse_advice_intent(data: Mapping[str, Any]) -> AdviceIntent:
    """Parse every entry in order; diagnostics never discard a safety signal."""
    diagnostics: list[str] = []
    operations = _parse_operations(data.get("proposed_operations"), diagnostics)
    effects = _parse_effects(data.get("contract_effects"), diagnostics)
    return AdviceIntent(tuple(operations), tuple(effects), tuple(diagnostics))


def _parse_operations(value: Any, diagnostics: list[str]) -> list[ProposedOperation]:
    if not isinstance(value, list):
        diagnostics.append("proposed_operations_missing_or_malformed")
        return []
    if not value:
        diagnostics.append("proposed_operations_empty")
        return []
    result: list[ProposedOperation] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, Mapping):
            diagnostics.append(f"operation:{index}:malformed")
            result.append(ProposedOperation("", "", entry))
            continue
        kind = str(entry.get("kind") or "").strip().lower()
        target = str(entry.get("target") or entry.get("path") or "").strip()
        if kind not in OPERATION_KINDS:
            diagnostics.append(f"operation:{index}:unknown_kind:{kind}")
        if not target:
            diagnostics.append(f"operation:{index}:missing_target")
        result.append(ProposedOperation(kind, target, dict(entry)))
    return result


def _parse_effects(value: Any, diagnostics: list[str]) -> list[ContractEffect]:
    if not isinstance(value, list):
        diagnostics.append("contract_effects_missing_or_malformed")
        return []
    result: list[ContractEffect] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, Mapping):
            diagnostics.append(f"effect:{index}:malformed")
            result.append(ContractEffect("", "", entry))
            continue
        invariant_id = str(entry.get("invariant_id") or "").strip()
        effect = str(entry.get("effect") or "").strip().lower()
        if not invariant_id:
            diagnostics.append(f"effect:{index}:missing_invariant_id")
        if effect not in EFFECT_KINDS:
            diagnostics.append(f"effect:{index}:unknown_effect:{effect}")
        result.append(ContractEffect(invariant_id, effect, dict(entry)))
    return result


__all__ = ["AdviceIntent", "ContractEffect", "ProposedOperation", "parse_advice_intent"]
