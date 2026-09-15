"""Latest applicable review evidence for the closing gate.

Answers one question from the durable ``review`` / ``reverify`` sub-records,
the operator waiver and the ``phase_handoff_decisions/`` artifacts: *which
review attempt still applies, what did it find, and what happened around it*.

Three rules keep it honest. An attempt that failed to parse has no verdict —
never latest, never supersedes, reported separately. A repair is a claim, not
a review, so repairing after a REJECTED verdict resolves nothing on its own.
And nothing is fabricated: no valid attempt yields ``None``, not an empty
block. How the gate must *weigh* this is code-owned prompt framing at the
builder seam, not here.

The live session and the ``meta.json`` resume path are deliberately
indistinguishable downstream: which one was used appears neither in
:meth:`FinalReviewContext.to_dict` nor in the render.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# Finding identity is imported, not re-derived: the waiver matching here and
# the evidence lifecycle projection must not be able to drift apart.
from pipeline.evidence.finding_lifecycle import _finding_fingerprint
from pipeline.phases.builtin.lifecycle import _ensure_lifecycle_ctx
from pipeline.repair_protocol import render_repair_receipt
from pipeline.review_round_record import PASS_REVERIFY, PASS_REVIEW

#: Within one round a review always precedes its post-repair re-verify.
_PASS_ORDER = {PASS_REVIEW: 0, PASS_REVERIFY: 1}
_PASS_LABEL = {PASS_REVIEW: "review", PASS_REVERIFY: "post-repair re-review"}
#: Only review-loop operator records belong in review evidence.
_REVIEW_PHASE = "review_changes"
_WAIVER_KEY = "phase_handoff_waiver"
_DECISIONS_DIRNAME = "phase_handoff_decisions"
#: Compact finding projection — enough to identify and act on a finding,
#: without the full reviewer body.
_FINDING_KEYS = ("id", "severity", "title", "file", "line", "required_fix")


# ── Types ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ReviewAttemptRef:
    """One ``review_changes`` invocation, identified by ``(round, pass)``."""

    round: int
    pass_kind: str
    verdict: str
    approved: bool
    short_summary: str
    findings: tuple[dict[str, Any], ...]
    parse_error: str | None = None
    #: Whether a repair pass had already run in this round when the attempt
    #: was recorded. Durable, written by the producer — the pass alone
    #: cannot say (the operator-feedback retry round repairs first and
    #: reviews after, yet records a ``review`` pass).
    repair_preceded: bool = False

    @property
    def valid(self) -> bool:
        """An attempt whose output never parsed carries no usable verdict."""
        return not self.parse_error

    @property
    def label(self) -> str:
        return _PASS_LABEL.get(self.pass_kind, self.pass_kind)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "round":         self.round,
            "pass":          self.pass_kind,
            "verdict":       self.verdict,
            "approved":      self.approved,
            "short_summary": self.short_summary,
            "findings":      [dict(f) for f in self.findings],
            "repair_preceded": self.repair_preceded,
        }
        if self.parse_error:
            out["parse_error"] = self.parse_error
        return out


@dataclass(frozen=True)
class OperatorRecord:
    """An operator rationale attached to the review loop.

    ``waived_finding_ids`` holds *identity keys*, not display ids: ``id:<id>``
    when the waived finding carried an id, else a ``severity|title|file|line``
    fingerprint — the identity ``evidence.finding_lifecycle`` matches on.
    """

    kind: str
    handoff_id: str
    phase: str
    action: str
    text: str
    note: str
    decided_at: str
    round: int | None = None
    waived_finding_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Tuples become lists, so the record survives a JSON round trip."""
        return {**asdict(self), "waived_finding_ids": list(self.waived_finding_ids)}


@dataclass(frozen=True)
class FinalReviewContext:
    """The resolved review evidence the closing gate reasons from."""

    run_id: str
    latest: ReviewAttemptRef
    superseded: tuple[ReviewAttemptRef, ...] = ()
    invalid: tuple[ReviewAttemptRef, ...] = ()
    unresolved_findings: tuple[dict[str, Any], ...] = ()
    repair_claim: dict[str, Any] | None = None
    repair_before_latest: dict[str, Any] | None = None
    operator: tuple[OperatorRecord, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the durable phase_log trace.

        Provenance is exactly ``run_id`` plus each attempt's ``(round, pass)``;
        how the facts were loaded is not part of the record.
        """
        out: dict[str, Any] = {
            "run_id":              self.run_id,
            "latest":              self.latest.to_dict(),
            "superseded":          [a.to_dict() for a in self.superseded],
            "invalid":             [a.to_dict() for a in self.invalid],
            "unresolved_findings": [dict(f) for f in self.unresolved_findings],
            "operator":            [o.to_dict() for o in self.operator],
        }
        if self.repair_claim is not None:
            out["repair_claim"] = dict(self.repair_claim)
        if self.repair_before_latest is not None:
            out["repair_before_latest"] = dict(self.repair_before_latest)
        return out


# ── Durable-fact readers ─────────────────────────────────────────────────────

def _load_json(path: Path) -> Any:
    """The module's only file read; missing / unreadable / corrupt → ``None``."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _mappings(value: Any) -> tuple[Mapping[str, Any], ...]:
    """The mapping items of a list-ish value; anything else yields nothing."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _session_for(state: Any) -> Mapping[str, Any] | None:
    """The live session if the lifecycle context carries one, else meta.json."""
    run_config = getattr(_ensure_lifecycle_ctx(state), "run_config", None)
    session = run_config.get("session") if isinstance(run_config, Mapping) else None
    if isinstance(session, Mapping):
        return session
    output_dir = getattr(state, "output_dir", None)
    if output_dir is None:
        return None
    meta = _load_json(Path(output_dir) / "meta.json")
    return meta if isinstance(meta, Mapping) else None


def _round_from_handoff_id(handoff_id: str) -> int | None:
    """``review_changes:repair_round:2`` / ``review_changes:2`` → ``2``."""
    digits = [chunk for chunk in handoff_id.split(":") if chunk.isdigit()]
    return int(digits[-1]) if digits else None


# ── Attempt ordering ─────────────────────────────────────────────────────────

def _attempt_from(round_n: int, pass_kind: str, record: Any) -> ReviewAttemptRef | None:
    """One attempt, or ``None`` when the sub-record carries no verdict."""
    if not isinstance(record, Mapping):
        return None
    verdict = record.get("verdict")
    if not isinstance(verdict, str) or not verdict.strip():
        return None
    parse_error = record.get("parse_error")
    # Records written before the producer stated the chronology fall back to
    # what the pass alone implies: a re-verify is post-repair by definition.
    preceded = record.get("repair_preceded")
    if not isinstance(preceded, bool):
        preceded = pass_kind == PASS_REVERIFY
    return ReviewAttemptRef(
        round=round_n, pass_kind=pass_kind, verdict=verdict.strip(),
        approved=bool(record.get("approved")),
        short_summary=str(record.get("short_summary") or ""),
        findings=tuple(
            {k: f[k] for k in _FINDING_KEYS if f.get(k) not in (None, "")}
            for f in _mappings(record.get("findings"))
        ),
        parse_error=str(parse_error) if parse_error else None,
        repair_preceded=preceded,
    )


def _ordered_attempts(rounds: Any) -> tuple[tuple[ReviewAttemptRef, ...], dict[int, dict]]:
    """Every recorded attempt in execution order, plus each round's receipt."""
    attempts: list[ReviewAttemptRef] = []
    receipts: dict[int, dict] = {}
    for entry in _mappings(rounds):
        round_n = entry.get("round")
        if not isinstance(round_n, int) or isinstance(round_n, bool):
            continue
        receipt = entry.get("repair_receipt")
        if isinstance(receipt, Mapping):
            receipts[round_n] = dict(receipt)
        for pass_kind in (PASS_REVIEW, PASS_REVERIFY):
            attempt = _attempt_from(round_n, pass_kind, entry.get(pass_kind))
            if attempt is not None:
                attempts.append(attempt)
    attempts.sort(key=lambda a: (a.round, _PASS_ORDER.get(a.pass_kind, 0)))
    return tuple(attempts), receipts


# ── Operator records ─────────────────────────────────────────────────────────

def _operator_record(kind: str, raw: Mapping[str, Any]) -> OperatorRecord:
    """Shape either durable source into the one operator-rationale type.

    The two differ only in spelling: a waiver states its rationale as
    ``waiver_text`` and its action is implicit, a decision carries ``action``
    and ``feedback``. Only a waiver names the findings it accepts.
    """
    handoff_id = str(raw.get("handoff_id") or "")
    waiver = kind == "waiver"
    return OperatorRecord(
        kind=kind, handoff_id=handoff_id, phase=_REVIEW_PHASE,
        action="continue_with_waiver" if waiver else str(raw.get("action") or ""),
        text=str(raw.get("waiver_text" if waiver else "feedback") or ""),
        note=str(raw.get("note") or ""),
        decided_at=str(raw.get("decided_at") or ""),
        round=_round_from_handoff_id(handoff_id),
        waived_finding_ids=tuple(
            _finding_fingerprint(f) for f in _mappings(raw.get("findings"))
        ) if waiver else (),
    )


def _review_decisions(state: Any) -> list[Mapping[str, Any]]:
    """Review-loop payloads from ``phase_handoff_decisions/``, by decision time.

    Lenient throughout: no run directory, a file that is not JSON, or a
    decision for another phase simply contributes nothing.
    """
    output_dir = getattr(state, "output_dir", None)
    directory = Path(output_dir) / _DECISIONS_DIRNAME if output_dir else None
    if directory is None or not directory.is_dir():
        return []
    payloads = (
        _load_json(entry) for entry in directory.iterdir() if entry.suffix == ".json"
    )
    reviews = [
        raw for raw in payloads
        if isinstance(raw, Mapping) and raw.get("phase") == _REVIEW_PHASE
    ]
    return sorted(reviews, key=lambda raw: str(raw.get("decided_at") or ""))


def _operator_records(state: Any, session: Mapping[str, Any]) -> tuple[OperatorRecord, ...]:
    """The review-loop waiver first, then its decisions in decision order."""
    waiver = getattr(state, "extras", {}).get(_WAIVER_KEY)
    if not isinstance(waiver, Mapping):
        waiver = session.get(_WAIVER_KEY)
    records: list[OperatorRecord] = []
    if isinstance(waiver, Mapping) and waiver.get("phase") == _REVIEW_PHASE:
        records.append(_operator_record("waiver", waiver))
    records += [_operator_record("decision", d) for d in _review_decisions(state)]
    return tuple(records)


# ── Resolver ─────────────────────────────────────────────────────────────────

def resolve_final_review_context(state: Any) -> FinalReviewContext | None:
    """Resolve the latest applicable review evidence, or ``None`` when there
    is nothing durable to say: a dry run (which touches no disk at all), no
    session, no rounds, or no parseable verdict."""
    if getattr(state, "dry_run", False):
        return None
    session = _session_for(state)
    if session is None:
        return None
    phases = session.get("phases")
    rounds = phases.get("rounds") if isinstance(phases, Mapping) else None
    if rounds is None:
        return None

    attempts, receipts = _ordered_attempts(rounds)
    valid = [a for a in attempts if a.valid]
    if not valid:
        # Every attempt failed to parse (or there were none): a verdict
        # cannot be invented from an unparseable one.
        return None
    latest = valid[-1]
    # Only an EARLIER VALID rejection is overruled — a later unparseable
    # attempt leaves the standing verdict exactly where it was.
    superseded = tuple(
        a for a in attempts[:attempts.index(latest)]
        if a.valid and not a.approved
    )
    # A repair does not review — but whether the round's receipt is an
    # unverified claim or already-reviewed context depends on when the
    # repair ran, which the attempt records itself. Inferring it from the
    # pass would misreport the operator-feedback retry round (repair first,
    # review after, stored as a ``review`` pass) as unverified.
    receipt = receipts.get(latest.round)
    reviewed_the_repair = latest.repair_preceded

    run_id = str(state.extras.get("run_id") or "").strip()
    if not run_id and state.output_dir is not None:
        run_id = Path(state.output_dir).name
    return FinalReviewContext(
        run_id=run_id, latest=latest, superseded=superseded,
        invalid=tuple(a for a in attempts if not a.valid),
        unresolved_findings=() if latest.approved else latest.findings,
        repair_claim=None if reviewed_the_repair else receipt,
        repair_before_latest=receipt if reviewed_the_repair else None,
        operator=_operator_records(state, session),
    )


# ── Renderer ─────────────────────────────────────────────────────────────────

def _section(title: str, body: Sequence[str]) -> list[str]:
    """A blank-separated block, or nothing when there is nothing to say."""
    return ["", title, *body] if body else []


def _finding_line(finding: Mapping[str, Any], waived_by: str | None) -> str:
    """``F1 [P1] title — file:line — required_fix (waived by operator: …)``."""
    head = " ".join(chunk for chunk in (
        str(finding.get("id") or "?"),
        f"[{finding['severity']}]" if finding.get("severity") else "",
        str(finding.get("title") or ""),
    ) if chunk)
    location = str(finding.get("file") or "")
    if location and finding.get("line") not in (None, ""):
        location = f"{location}:{finding['line']}"
    line = "- " + " — ".join(chunk for chunk in (
        head, location, str(finding.get("required_fix") or ""),
    ) if chunk)
    return line + (f" (waived by operator: {waived_by})" if waived_by else "")


def _attempt_line(attempt: ReviewAttemptRef) -> str:
    """``- round 2 review REJECTED (F1, F2)``."""
    ids = ", ".join(str(f.get("id") or "?") for f in attempt.findings)
    head = f"- round {attempt.round} {attempt.label} {attempt.verdict}"
    return head + (f" ({ids})" if ids else "")


def _operator_lines(records: Sequence[OperatorRecord]) -> list[str]:
    """One head line per record, with its rationale and note indented."""
    lines: list[str] = []
    for record in records:
        lines.append(" · ".join(chunk for chunk in (
            f"- {record.action}",
            f"round {record.round}" if record.round is not None else "",
            f"handoff {record.handoff_id}" if record.handoff_id else "",
            record.decided_at,
        ) if chunk))
        for label, value in (("rationale", record.text), ("note", record.note)):
            if value:
                lines.append(f"  {label}: {value}")
    return lines


def render_final_review_context(ctx: FinalReviewContext) -> str:
    """Render the resolved evidence as the prompt-facing block body."""
    latest = ctx.latest
    waived_by: dict[str, str] = {}
    for record in ctx.operator:
        for fingerprint in record.waived_finding_ids:
            waived_by.setdefault(fingerprint, record.handoff_id)

    lines = [
        f"Latest applicable review: round {latest.round} ({latest.label}), "
        f"verdict {latest.verdict}, run {ctx.run_id}",
    ]
    if latest.short_summary:
        lines.append(f"Summary: {latest.short_summary}")
    for title, body in (
        ("Findings from this review, not closed by a later review:", [
            _finding_line(f, waived_by.get(_finding_fingerprint(f)))
            for f in ctx.unresolved_findings
        ]),
        ("Superseded by the latest review:",
         [_attempt_line(a) for a in ctx.superseded]),
        ("Invalid attempts (not evidence):", [
            f"- invalid attempt: round {a.round} {a.pass_kind} — parse error"
            for a in ctx.invalid
        ]),
    ):
        lines += _section(title, body)
    if ctx.repair_claim is not None:
        lines += _section(
            "Repair claim after this review (not re-reviewed; verify "
            "against the current subject):",
            [render_repair_receipt(ctx.repair_claim)],
        )
    elif ctx.repair_before_latest is not None:
        lines += ["", (
            f"Repair before the latest review: round {latest.round} was "
            "repaired, and the review above ran after it."
        )]
    lines += _section("Operator decisions:", _operator_lines(ctx.operator))
    return "\n".join(lines).strip()
