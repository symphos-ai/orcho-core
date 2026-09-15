"""pipeline/review_round_record.py — durable per-attempt review sub-record.

Every ``review_changes`` invocation produces a verdict, but only the
*critique text* of the last one survived into the session shape
(``RoundAdapter`` writes ``rounds[n]["critique"]``). A round that was
reviewed, repaired and re-verified therefore lost the original verdict,
its finding ids and their severities — the exact facts a later
``final_acceptance`` needs to say what was rejected and whether the
rejection still stands.

This module persists each attempt as a sub-record **inside the round
entry** rather than under ``session["phases"]["review_changes"]``. The
latter is not an option: ``_fsm_checkpoint`` saves any key present in
``session["phases"]`` as a completed checkpoint phase (with a loop
cursor), which would change ``resolve_loop_resume`` behaviour for the
review/repair loop. Keeping the record inside ``rounds`` is purely
additive to the durable shape and invisible to checkpoint/resume.

Each record also states where the attempt sat relative to the round's
repair (``repair_preceded``), read off the round entry at write time.
The pass alone cannot answer that: the operator-feedback retry round
runs ``repair_changes -> review_changes`` and stores its verdict as the
round's ``review`` pass, so an already-reviewed repair would otherwise
look like an unverified post-review claim.

Attempt identity is ``(round, pass)``:

* ``review``   — the round's first review pass.
* ``reverify`` — the ADR 0039 post-repair re-verify pass, which the loop
  runner announces by setting ``state.extras["_review_reverify_resume"]``
  before it re-dispatches ``review_changes``.

The pass is read from that explicit runner signal only — never inferred
from "a ``review`` key already exists", which would silently mislabel a
re-run of the same attempt. Writing the same ``(round, pass)`` twice
overwrites the same key, so a replayed attempt stays one attempt.
"""
from __future__ import annotations

from typing import Any

from pipeline.runtime import PipelineState

#: Sub-record key for the round's first review pass.
PASS_REVIEW = "review"
#: Sub-record key for the ADR 0039 post-repair re-verify pass.
PASS_REVERIFY = "reverify"

#: The runner-owned signal that the current ``review_changes`` dispatch is
#: the post-repair re-verify pass (``pipeline/runtime/runner.py``; set
#: before ``_dispatch_via_fsm`` and popped in its ``finally``, so the FSM
#: adapter stage always observes it inside the dispatch).
REVERIFY_FLAG = "_review_reverify_resume"


def write_review_round_record(
    session: dict,
    *,
    round_n: int | None,
    log: dict[str, Any] | None,
    pass_kind: str,
) -> dict[str, Any] | None:
    """Persist one review attempt into ``session['phases']['rounds']``.

    Returns the written sub-record, or ``None`` when the attempt carries
    no verdict to record.

    Skip rules (all mean "there was no review verdict on this attempt"):
    an empty/absent ``log``, a ``skipped`` marker (the no-uncommitted and
    delivery-incomplete short-circuits), or a log without ``verdict``.

    The round entry is located by ``round == round_n``; when the round has
    not been written yet (the reviewer runs before ``RoundAdapter``
    composes the round) a provisional ``{"round": round_n}`` entry is
    appended, which ``RoundAdapter`` later fills in place.
    """
    if round_n is None:
        raise ValueError(
            "write_review_round_record requires explicit round_n "
            "(loop step number)"
        )
    if pass_kind not in (PASS_REVIEW, PASS_REVERIFY):
        raise ValueError(
            f"unknown review pass {pass_kind!r}; "
            f"expected {PASS_REVIEW!r} or {PASS_REVERIFY!r}"
        )
    if not isinstance(log, dict) or not log:
        return None
    if "skipped" in log:
        return None
    if "verdict" not in log:
        return None

    rounds = session.setdefault("phases", {}).setdefault("rounds", [])
    entry: dict[str, Any] | None = None
    for candidate in rounds:
        if isinstance(candidate, dict) and candidate.get("round") == round_n:
            entry = candidate
    if entry is None:
        entry = {"round": round_n}
        rounds.append(entry)

    record: dict[str, Any] = {
        "pass":          pass_kind,
        "attempt":       round_n,
        "verdict":       log.get("verdict"),
        "approved":      bool(log.get("approved")),
        "clean":         bool(log.get("clean")),
        # Did a repair pass run before this attempt, in this round?
        # Observed, not inferred from the pass: ``reverify`` is post-repair
        # by definition, and the operator-feedback retry round repairs
        # first and reviews after while still storing the round's first
        # ``review`` pass — there the repair evidence is already on the
        # entry. The ordinary in-loop ``review`` pass sees a bare entry.
        "repair_preceded": (
            pass_kind == PASS_REVERIFY
            or bool(entry.get("repair_receipt") or entry.get("repair_output"))
        ),
        "short_summary": log.get("short_summary") or "",
        "findings":      list(log.get("findings") or []),
    }
    # An unparseable attempt is recorded as-is: a resolver must be able to
    # see that the attempt happened AND that its verdict is not usable.
    parse_error = log.get("parse_error")
    if parse_error:
        record["parse_error"] = str(parse_error)
    meta = log.get("meta")
    if isinstance(meta, dict):
        for key in ("session_id", "continue_session"):
            value = meta.get(key)
            if value is not None:
                record[key] = value

    # Same (round, pass) → same key, overwritten. Re-running an attempt
    # never grows the attempt list.
    entry[pass_kind] = record
    return record


class ReviewRoundAdapter:
    """Session adapter for ``review_changes``: one sub-record per attempt.

    Registered under ``review_changes`` so the lifecycle FSM auto-fires it
    after every review dispatch — the in-loop pass, the ADR 0039
    post-repair re-verify pass, and the halted parse-failure pass. It
    deliberately writes nothing under ``session["phases"]["review_changes"]``
    (see the module docstring on the checkpoint/loop-resume coupling).

    ``round_n`` without a value means the profile ran ``review_changes``
    outside a review/repair loop (``delivery_audit`` / ``code_review``):
    there is no round to attribute the attempt to, and inventing one would
    fabricate loop state, so the adapter is a no-op there.
    """

    def write(
        self,
        phase_name: str,
        state: PipelineState,
        session: dict,
        *,
        round_n: int | None = None,
    ) -> None:
        if round_n is None:
            return
        pending = state.phase_log.get("rounds_pending", {}) or {}
        if pending.get("_skip_adapter"):
            return
        log = state.phase_log.get(phase_name)
        pass_kind = (
            PASS_REVERIFY
            if state.extras.get(REVERIFY_FLAG) is True
            else PASS_REVIEW
        )
        write_review_round_record(
            session, round_n=round_n, log=log, pass_kind=pass_kind,
        )
