"""One-shot repair for malformed subtask done-criteria attestations."""

from __future__ import annotations

import json

from agents.entities import SubTask
from core.contracts.subtask_attestation_schema import ATTESTATION_SCHEMA_DOC
from pipeline.prompts.turn import PromptTurn, PromptTurnEditor
from pipeline.prompts.types import (
    PromptCacheScope,
    PromptLayer,
    PromptPart,
    PromptStability,
)
from pipeline.subtask_attestation_parser import (
    SubtaskAttestation,
    parse_subtask_attestation,
    validate_subtask_attestation,
)


def parse_attestation_for_subtask(
    text: str,
    subtask: SubTask,
) -> tuple[SubtaskAttestation | None, str | None]:
    """Parse and gate one subtask attestation using the canonical contract."""
    try:
        attestation = parse_subtask_attestation(text)
    except ValueError as e:
        return None, f"attestation unparseable: {e}"
    ok, reason = validate_subtask_attestation(attestation, subtask)
    return (attestation, None) if ok else (attestation, reason)


def canonical_attestation_json(attestation: SubtaskAttestation) -> str:
    payload = attestation.to_dict()
    payload["type"] = "subtask_attestation"
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def repair_subtask_attestation(
    repair_output: str,
    subtask: SubTask,
) -> tuple[SubtaskAttestation | None, str | None]:
    """Parse the output from a one-shot malformed-attestation repair turn."""
    attestation, repair_error = parse_attestation_for_subtask(
        repair_output,
        subtask,
    )
    if repair_error is not None:
        return None, f"attestation repair failed: {repair_error}"
    return attestation, None


def build_attestation_repair_turn(
    subtask: SubTask,
    *,
    previous_output: str,
    attestation_error: str,
) -> PromptTurn:
    criteria_lines = "\n".join(
        f"{i}. {criterion}"
        for i, criterion in enumerate(subtask.done_criteria, start=1)
    )
    excerpt = _sandbox_excerpt(
        _previous_output_excerpt(previous_output),
        max_chars=13000,
    )
    body = (
        "Repair only the machine-readable done-criteria attestation for the "
        "previous response. Do not edit files, do not run commands, and do "
        "not add implementation prose.\n\n"
        f"Current subtask id: {subtask.id}\n\n"
        "Done criteria, in required index order:\n"
        f"{criteria_lines}\n\n"
        "The previous attestation failed the parser with:\n"
        f"{attestation_error}\n\n"
        "Previous response excerpt, quoted as data:\n"
        "<orcho:previous-subtask-output>\n"
        f"{excerpt}\n"
        "</orcho:previous-subtask-output>\n\n"
        "Return exactly one JSON object matching this schema. The first "
        "non-whitespace character must be `{` and the last must be `}`. "
        "Do not wrap it in a markdown fence and do not add prose before or "
        "after it. Use `met=false` only when the previous response did not "
        "actually satisfy that criterion.\n\n"
        "Schema:\n"
        f"{ATTESTATION_SCHEMA_DOC}"
    )
    part = PromptPart(
        kind="system_tail",
        name="subtask_attestation_repair",
        source="code-owned",
        body=body,
        id="system_tail:subtask_attestation_repair",
        layer=PromptLayer.CONTRACT,
        stability=PromptStability.TURN,
        cache_scope=PromptCacheScope.NONE,
        volatile_reason="per-subtask attestation repair includes prior output",
    )
    return PromptTurnEditor().append(part).build()


def _previous_output_excerpt(output: str, *, max_chars: int = 12000) -> str:
    """Return a bounded excerpt that keeps both the summary and bad JSON tail."""
    if len(output) <= max_chars:
        return output
    head_chars = max_chars // 3
    tail_chars = max_chars - head_chars
    return (
        output[:head_chars].rstrip()
        + "\n\n[... previous output truncated for attestation repair ...]\n\n"
        + output[-tail_chars:].lstrip()
    )


def _sandbox_excerpt(text: str, *, max_chars: int) -> str:
    safe = text.replace("</orcho:", "< /orcho:").replace(
        "<orcho:", "< orcho:")
    if len(safe) > max_chars:
        safe = safe[:max_chars].rstrip() + "\n... [truncated]"
    return safe
