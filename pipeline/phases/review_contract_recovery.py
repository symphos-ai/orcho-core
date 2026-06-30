"""One-shot recovery for reviewer JSON contract misses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.contracts.review_schema import REVIEW_SCHEMA_DOC
from pipeline.phases.builtin.session_keys import auxiliary_session_continuity
from pipeline.runtime.roles import SessionInvocationRole
from pipeline.runtime.run_shape import OperatingMode
from pipeline.runtime.session_disposition import decide


@dataclass(frozen=True)
class ReviewContractRetryResult:
    raw_output: str
    repair_meta: dict[str, Any]


def review_contract_retry_prompt(
    *,
    phase: str,
    parse_error: str,
    raw_output: str,
) -> str:
    """Build a one-shot repair prompt for a reviewer JSON contract miss.

    Uses ``REVIEW_SCHEMA_DOC`` directly: the prompt is self-contained — it
    embeds the previous ``raw_output`` verbatim and asks only for a
    format-preserving re-emit — so it does not depend on a warm reviewer
    session and is safe to send in a fresh one (see the ``format_repair``
    disposition in :func:`retry_review_contract_once`).
    """
    return (
        "Your previous response for Orcho phase "
        f"`{phase}` violated the required JSON output contract.\n\n"
        "Do not re-review the code and do not add prose. Convert your "
        "previous response into the exact contract below. Preserve the same "
        "review decision, findings, risks, and checks from the previous "
        "response. Return exactly one JSON object and nothing else.\n\n"
        f"Parse error:\n{parse_error}\n\n"
        f"{REVIEW_SCHEMA_DOC}\n\n"
        "Previous response:\n"
        "```text\n"
        f"{raw_output}\n"
        "```"
    )


def retry_review_contract_once(
    agent: Any,
    *,
    phase: str,
    cwd: str,
    raw_output: str,
    parse_error: Exception,
    attachments: tuple = (),
) -> ReviewContractRetryResult:
    """Ask the reviewer to re-emit its verdict as JSON.

    The continuation disposition comes from the session-disposition policy.
    The ``format_repair`` continuity is resolved through the single auxiliary
    classifier (:func:`pipeline.phases.builtin.session_keys.auxiliary_session_continuity`)
    rather than re-hardcoding ``SessionContinuity.FRESH_ONLY`` here: that keeps
    the auxiliary-role classification in exactly one place, so a change to how
    ``format_repair`` is classified (or an extra check added to the resolver)
    is honoured on this path too. The result is fresh — a contract re-emit is
    non-edit-shaped, so it always starts a fresh session — and that is
    content-safe because the repair prompt embeds the previous ``raw_output``
    verbatim (see :func:`review_contract_retry_prompt`), so the one-shot retry
    contract holds without resuming the prior session.

    The caller parses ``raw_output`` so both success and failure paths can
    persist the retry's exact response.
    """
    prompt = review_contract_retry_prompt(
        phase=phase,
        parse_error=str(parse_error),
        raw_output=raw_output,
    )
    disposition = decide(
        policy=auxiliary_session_continuity(SessionInvocationRole.FORMAT_REPAIR),
        same_write_zone=False,
        loop_followon=False,
        operating_mode=OperatingMode.FAST,
    )
    repaired_raw = agent.invoke(
        prompt,
        cwd,
        continue_session=disposition.continue_session,
        attachments=attachments,
        mutates_artifacts=False,
    )
    return ReviewContractRetryResult(
        raw_output=repaired_raw,
        repair_meta={
            "triggered": True,
            "original_parse_error": str(parse_error),
            "original_raw_output": raw_output,
        },
    )
