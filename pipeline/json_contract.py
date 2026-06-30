"""
Shared recovery for JSON-only agent contracts.

Strict contract output is still exactly one raw JSON object. Real agents can
occasionally prepend or append visible prose despite that instruction, so this
module provides one defensive recovery path: find exactly one schema-valid JSON
object embedded in the text, strip the surrounding non-JSON text, and surface a
parse warning to the caller.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class JsonContractPayload:
    data: dict[str, Any]
    original_data: dict[str, Any]
    parse_warnings: tuple[str, ...] = ()


def parse_json_contract_object(
    text: str,
    *,
    label: str,
    parse_error_cls: type[ValueError],
    is_candidate: Callable[[Any], bool],
    validate: Callable[[Any], dict[str, Any]],
) -> JsonContractPayload:
    """Parse a JSON-contract object with one guarded recovery path.

    The strict path accepts only a raw JSON object and propagates schema errors
    from ``validate``. If the raw parse fails or the text starts with prose,
    the recovery path scans every ``{`` offset and accepts exactly one
    candidate that passes both ``is_candidate`` and ``validate``.
    """
    raw = text or ""
    stripped = raw.strip()
    if not stripped:
        raise parse_error_cls(
            f"{label} output must be exactly one JSON object; "
            "empty output is not accepted"
        )

    strict_error: json.JSONDecodeError | None = None
    if stripped.startswith("{"):
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError as exc:
            strict_error = exc
        else:
            if not isinstance(decoded, dict):
                raise parse_error_cls(
                    f"{label} output must be exactly one JSON object; "
                    f"got {type(decoded).__name__}"
                )
            return _validated_payload(decoded, validate)

    recovered = _recover_embedded_object(
        stripped,
        label=label,
        parse_error_cls=parse_error_cls,
        is_candidate=is_candidate,
        validate=validate,
    )
    if recovered is not None:
        return recovered

    if strict_error is not None:
        raise parse_error_cls(f"raw JSON parse failed: {strict_error}") from strict_error
    raise parse_error_cls(
        f"{label} output must be exactly one JSON object; "
        "prose, markdown fences, and trailing commentary are not accepted"
    )


def _recover_embedded_object(
    text: str,
    *,
    label: str,
    parse_error_cls: type[ValueError],
    is_candidate: Callable[[Any], bool],
    validate: Callable[[Any], dict[str, Any]],
) -> JsonContractPayload | None:
    decoder = json.JSONDecoder()
    candidates: list[tuple[int, int, JsonContractPayload]] = []
    # Candidate-shaped objects that decoded + passed ``is_candidate`` but
    # FAILED ``validate``. When no valid candidate is found, a single such
    # object is almost certainly the intended one — surfacing its schema
    # error (keyed by span to dedupe nested re-scans) is far more useful to
    # the caller (e.g. a synthetic replan critique) than the generic
    # "no JSON object" message.
    invalid_shaped: dict[tuple[int, int], str] = {}

    for match in re.finditer(r"{", text):
        start = match.start()
        try:
            decoded, end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if not is_candidate(decoded):
            continue
        try:
            payload = _validated_payload(decoded, validate)
        except ValueError as exc:
            invalid_shaped[(start, start + end)] = str(exc)
            continue
        candidates.append((start, start + end, payload))

    if not candidates:
        if len(invalid_shaped) == 1:
            (only_error,) = invalid_shaped.values()
            raise parse_error_cls(only_error)
        return None
    if len(candidates) > 1:
        raise parse_error_cls(
            f"{label} output contains multiple embedded JSON contract "
            "objects; refusing to choose one"
        )

    start, end, payload = candidates[0]
    prefix = text[:start].strip()
    suffix = text[end:].strip()
    if not prefix and not suffix:
        return payload

    warning = (
        f"stripped non-JSON text around {label} JSON "
        f"(prefix={len(prefix)} chars, suffix={len(suffix)} chars)"
    )
    return JsonContractPayload(
        data=payload.data,
        original_data=payload.original_data,
        parse_warnings=(*payload.parse_warnings, warning),
    )


def _validated_payload(
    decoded: Any,
    validate: Callable[[Any], dict[str, Any]],
) -> JsonContractPayload:
    if not isinstance(decoded, dict):
        raise ValueError(f"expected JSON object, got {type(decoded).__name__}")

    original = copy.deepcopy(decoded)
    data = copy.deepcopy(decoded)
    validated = validate(data)
    if not isinstance(validated, dict):
        raise ValueError(f"validator returned {type(validated).__name__}")
    return JsonContractPayload(data=validated, original_data=original)

