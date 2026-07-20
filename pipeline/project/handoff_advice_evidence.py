# SPDX-License-Identifier: Apache-2.0
"""Normalize Stage 0/1 handoff-advice artifacts into evidence-shaped records.

This is a pure **leaf** module (like ``handoff_advice_artifact``): it imports no
other ``pipeline.project`` module at runtime so it can be wired into both the
finalization flow and the evidence collector without an import cycle. It only
**reads** durable artifacts written by Stage 0/1 — it never decides, retries,
spawns a process, or mutates run state.

The single public entry point is :func:`collect_handoff_advice`. It folds three
durable sources into a stable, additive ``{'calls': [...], 'summary': {...}}``
document:

* ``<run_dir>/phase_handoff_advice/*.json`` — one advice object per advisor
  invocation (attempt-suffixed ``_N.json`` files are distinct divergent calls).
* ``<run_dir>/phase_handoff_decisions/*.json`` — operator/CI decisions. A
  decision is matched to the advice it was generated from STRICTLY by the
  ``advice_artifact=<relpath>`` token in its ``note`` (the same token
  :func:`pipeline.project.handoff_advice_artifact.build_provenance_note`
  writes), and the retry provenance is read from the ``feedback_source=<src>``
  token.
* ``meta['phases']`` — the phase records whose verdicts decide whether an applied
  retry actually helped (``resolved`` vs ``repeated``). Two durable shapes exist
  and BOTH are read:
    - finding-bearing phases that persist *structured* per-attempt verdict +
      findings (``validate_plan`` / ``final_acceptance`` / ``compliance_check`` /
      ``cross_final_acceptance``) — classified by fingerprint match;
    - the review/repair loop, which persists to ``meta['phases']['rounds']`` via
      ``RoundAdapter`` as text-only round entries (a per-round ``critique`` —
      blank ⟺ the review approved — and a ``round`` number; NO structured
      findings). Review-loop advice is therefore classified from durable
      *signals*: a later round that approved, downstream structured approval
      (``final_acceptance``) or a terminal success status (⟹ ``resolved``), and
      the advice-call sequence itself — a later advice call for the same phase
      means the rejection recurred through the retry (⟹ ``repeated``).

The classifier is deliberately conservative: an advice call is only ``resolved``
when there is positive approval evidence after the retry, never when the same
finding reappears in a rejected attempt or the same phase keeps producing advice;
ambiguity resolves to ``unknown``.

Stop condition (narrow): :func:`collect_handoff_advice` returns ``None`` ONLY
when the Stage 0/1 artifact surface is entirely absent — no advice directory
with artifacts AND no decision carrying advice provenance. The mere absence of a
matching decision for a present advice artifact is NOT a stop condition: that
advice is still emitted as an unapplied call (``applied_action=None``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

#: Durable artifact directories written by Stage 0/1.
_ADVICE_DIRNAME = "phase_handoff_advice"
_DECISIONS_DIRNAME = "phase_handoff_decisions"

#: Phases whose attempts persist *structured* per-attempt verdict + findings, so
#: outcome can be classified by finding-fingerprint match. Subset of
#: ``pipeline.evidence.collector._FINDING_BEARING_PHASES`` /
#: ``sdk.evidence_slices.FINDING_BEARING_PHASES`` — ``review_changes`` is
#: deliberately excluded here because the live review/repair loop persists
#: text-only ``rounds`` (see ``_REVIEW_LOOP_PHASES`` and the signal classifier).
_STRUCTURED_FINDING_PHASES: frozenset[str] = frozenset({
    "validate_plan",
    "final_acceptance",
    "compliance_check",
    "cross_final_acceptance",
})

#: Phases driven by the review/repair loop, persisted as text-only round entries
#: under ``meta['phases']['rounds']`` (no structured per-round findings). Advice
#: for these is classified from durable signals, not a fingerprint match.
_REVIEW_LOOP_PHASES: frozenset[str] = frozenset({
    "review_changes", "repair_changes",
})

#: Terminal run statuses that count as success (the pipeline only reaches these
#: once the review loop approved), used as a downstream ``resolved`` signal.
_SUCCESS_STATUSES: frozenset[str] = frozenset({
    "done", "success", "succeeded", "complete", "completed", "ok",
})

#: Advisor actions that are not a feedback retry — these never reach the
#: resolved/repeated classifier; the advice "stopped" the loop by design.
_NON_RETRY_ACTIONS: frozenset[str] = frozenset({
    "continue", "halt", "continue_with_waiver",
})

#: Tokens parsed out of a decision ``note`` (``feedback_source=…; advice_artifact=…``).
_ADVICE_ARTIFACT_RE = re.compile(r"advice_artifact=([^\s;]+)")
_FEEDBACK_SOURCE_RE = re.compile(r"feedback_source=([^\s;]+)")

#: Round number embedded in a review-loop ``handoff_id`` (e.g.
#: ``review_changes:repair_round:2``). Used to find the *next* round after the
#: one the advice fired on.
_HANDOFF_ROUND_RE = re.compile(r"(?:repair_round|round)[:_-](\d+)")


# ── tiny self-contained helpers (no pipeline.project imports) ───────────────


def _load_json(path: Path) -> dict[str, Any] | None:
    """Lenient JSON object reader: parsed dict or ``None`` on any error."""
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _phase_attempts(value: Any) -> list[dict[str, Any]]:
    """Normalize a ``meta['phases'][name]`` slot into a list of attempt dicts.

    Mirrors ``pipeline.evidence.collector._phase_attempts``: finding-bearing
    phases persist either as an attempt-list (``validate_plan`` / ``review_changes``
    / ``compliance_check``) or as a singleton dict (``final_acceptance`` /
    ``cross_final_acceptance`` since ADR 0025). Both must be visible or the
    outcome would falsely degrade to ``unknown``.
    """
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _fingerprint(findings: Any) -> frozenset[tuple[str, str, str]]:
    """Fingerprint findings by ``(id, severity, title)`` for repeat detection.

    Same shape as ``pipeline.project.handoff_advice_policy.findings_fingerprint``
    (reimplemented locally to keep this module a leaf). Two attempts with an
    identical triple set compare equal — that is a repeated finding.
    """
    out: set[tuple[str, str, str]] = set()
    if not isinstance(findings, (list, tuple)):
        return frozenset()
    for item in findings:
        if not isinstance(item, Mapping):
            continue
        out.add((
            str(item.get("id") or "").strip(),
            str(item.get("severity") or "").strip().upper(),
            str(item.get("title") or "").strip(),
        ))
    return frozenset(out)


def _fingerprint_str(fingerprint: frozenset[tuple[str, str, str]]) -> str:
    """Stable compact string form of a findings fingerprint.

    Mirrors ``pipeline.project.handoff._fingerprint_str``: sorted ``id|sev|title``
    triples joined by ``;``. This is the only finding detail copied into the
    evidence call — the full reviewer output is referenced via the advice
    artifact relpath, never duplicated here.
    """
    if not fingerprint:
        return ""
    return ";".join(sorted("|".join(part) for part in fingerprint))


def _severity_counts(findings: Any) -> dict[str, int]:
    """Count findings by (upper-cased) severity for a compact call digest."""
    counts: dict[str, int] = {}
    if not isinstance(findings, (list, tuple)):
        return counts
    for item in findings:
        if not isinstance(item, Mapping):
            continue
        sev = str(item.get("severity") or "").strip().upper() or "UNKNOWN"
        counts[sev] = counts.get(sev, 0) + 1
    return counts


def _attempt_approved(attempt: Mapping[str, Any]) -> bool:
    """True when an attempt is a clear approval (``approved`` or verdict APPROVED)."""
    if bool(attempt.get("approved")):
        return True
    verdict = str(attempt.get("verdict") or "").strip().upper()
    return verdict == "APPROVED"


def _attempt_verdict(attempt: Mapping[str, Any]) -> str:
    """Best-effort verdict string for an attempt (APPROVED/REJECTED/…)."""
    verdict = str(attempt.get("verdict") or "").strip().upper()
    if verdict:
        return verdict
    return "APPROVED" if bool(attempt.get("approved")) else "REJECTED"


def _trigger_from_verdict(verdict: str) -> str:
    """Map a verdict to the advisor trigger label (rejected/incomplete)."""
    if verdict == "INCOMPLETE":
        return "incomplete"
    if verdict == "REJECTED":
        return "rejected"
    return ""


# ── review/repair-loop durable signals (text-only ``rounds`` shape) ─────────


def _rounds(phases_meta: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Ordered ``meta['phases']['rounds']`` entries (RoundAdapter shape)."""
    rounds = phases_meta.get("rounds")
    if not isinstance(rounds, list):
        return []
    return [r for r in rounds if isinstance(r, dict)]


def _round_num(entry: Mapping[str, Any]) -> int | None:
    """Round ordinal of a round entry, or ``None`` when absent/non-int."""
    value = entry.get("round")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _round_approved(entry: Mapping[str, Any]) -> bool:
    """True when a round entry represents an APPROVED review.

    The review handler sets ``state.last_critique = ""`` on approval and a
    non-empty fix critique on rejection (``review_changes`` handler); the
    ``RoundAdapter`` persists that as the round's ``critique``. So a round whose
    ``critique`` is present-but-blank marks an approved review. An explicit
    ``approved``/``verdict`` (forward-compat, if a future adapter adds it) wins.
    """
    if _attempt_approved(entry):
        return True
    if "critique" not in entry:
        return False
    critique = entry.get("critique")
    return isinstance(critique, str) and not critique.strip()


def _handoff_round(handoff_id: str) -> int | None:
    """Parse the round ordinal out of a review-loop ``handoff_id``."""
    m = _HANDOFF_ROUND_RE.search(handoff_id or "")
    return int(m.group(1)) if m else None


def _round_after(entry: Mapping[str, Any], advice_round: int | None) -> bool:
    """True when ``entry`` is a round strictly AFTER the advice's round.

    Conservative when the advice round is unknown (``None``): returns ``False``
    so the advice's own triggering round is never mistaken for a later one, and
    classification falls back to the downstream/override signals.
    """
    if advice_round is None:
        return False
    return (_round_num(entry) or 0) > advice_round


def _downstream_approved(
    phases_meta: Mapping[str, Any], status: str,
) -> bool:
    """True when a durable signal shows the run progressed past an approval.

    Either a structured release gate approved (``final_acceptance`` /
    ``cross_final_acceptance`` — these only run after the review loop approves),
    or the run reached a terminal success status. Both are conservative
    ``resolved`` signals for a review-loop advice retry whose own phase keeps no
    structured per-round verdict.
    """
    for name in ("final_acceptance", "cross_final_acceptance"):
        for attempt in _phase_attempts(phases_meta.get(name)):
            if _attempt_approved(attempt) or attempt.get("ship_ready") is True:
                return True
    return status.strip().lower() in _SUCCESS_STATUSES


def _active_handoff(meta: Mapping[str, Any]) -> dict[str, Any]:
    """The active ``meta['phase_handoff']`` payload, or an empty dict.

    For a still-paused run this carries the rejected verdict/trigger/findings of
    the handoff that fired the advice (durable while paused; cleared on a
    terminal run). Used opportunistically to enrich a review-loop call's
    verdict/trigger/finding_fingerprint when the matching handoff is still
    active — never fabricated when absent.
    """
    active = meta.get("phase_handoff") if isinstance(meta, Mapping) else None
    return active if isinstance(active, dict) else {}


# ── decision provenance parsing ─────────────────────────────────────────────


def _parse_note(note: Any) -> tuple[str | None, str | None]:
    """Extract ``(advice_artifact_relpath, feedback_source)`` from a decision note.

    Returns ``(None, None)`` when the note is absent or carries no advice
    provenance. The relpath is the strict matching key against advice artifacts.
    """
    if not isinstance(note, str) or not note:
        return None, None
    relpath_m = _ADVICE_ARTIFACT_RE.search(note)
    source_m = _FEEDBACK_SOURCE_RE.search(note)
    relpath = relpath_m.group(1) if relpath_m else None
    source = source_m.group(1) if source_m else None
    return relpath, source


def _load_decisions_by_advice(run_dir: Path) -> dict[str, dict[str, Any]]:
    """Map advice-artifact relpath → decision payload, for advice with provenance.

    Reads ``<run_dir>/phase_handoff_decisions/*.json`` leniently (skips
    unreadable / malformed files) and keys each decision by the
    ``advice_artifact=<relpath>`` token in its note. Decisions without advice
    provenance are ignored here (they did not originate from an advice call).
    """
    decisions_dir = run_dir / _DECISIONS_DIRNAME
    by_advice: dict[str, dict[str, Any]] = {}
    if not decisions_dir.is_dir():
        return by_advice
    for entry in sorted(decisions_dir.iterdir()):
        if not entry.is_file() or entry.suffix != ".json":
            continue
        decision = _load_json(entry)
        if decision is None:
            continue
        relpath, source = _parse_note(decision.get("note"))
        if relpath is None:
            continue
        by_advice[relpath] = {
            "action": decision.get("action"),
            "feedback_source": source,
            "phase": decision.get("phase"),
        }
    return by_advice


#: Decision actions that let a paused run proceed to completion. A decision
#: WITHOUT advice provenance carrying one of these is a manual/operator override
#: — the run advanced for a reason other than this advice's clean approval.
_OVERRIDE_ACTIONS: frozenset[str] = frozenset({
    "continue", "continue_with_waiver", "retry_feedback",
})


def _non_advice_override_phases(run_dir: Path, meta: Mapping[str, Any]) -> set[str]:
    """Phases whose review was advanced by a NON-advice override.

    A ``phase_handoff_waiver`` (operator accepted rejected findings) or a manual
    continue / continue_with_waiver / human ``retry_feedback`` decision that
    carries NO ``advice_artifact=`` provenance means the run proceeded past that
    phase for a reason other than the advice's own review approval. Such a phase
    must not let the weak terminal-success / final_acceptance signal read as the
    advice having resolved the finding (review focus: never falsely ``resolved``
    after a re-reject + waiver). Scoped by ``phase`` so an unrelated override
    elsewhere does not suppress a genuine resolution.
    """
    out: set[str] = set()
    waiver = meta.get("phase_handoff_waiver") if isinstance(meta, Mapping) else None
    if isinstance(waiver, Mapping):
        phase = waiver.get("phase")
        if isinstance(phase, str) and phase:
            out.add(phase)
    decisions_dir = run_dir / _DECISIONS_DIRNAME
    if not decisions_dir.is_dir():
        return out
    for entry in sorted(decisions_dir.iterdir()):
        if not entry.is_file() or entry.suffix != ".json":
            continue
        decision = _load_json(entry)
        if decision is None:
            continue
        relpath, _source = _parse_note(decision.get("note"))
        if relpath is not None:
            # Advice-driven decision — not a manual override.
            continue
        if decision.get("action") not in _OVERRIDE_ACTIONS:
            continue
        phase = decision.get("phase")
        if isinstance(phase, str) and phase:
            out.add(phase)
    return out


def _decisions_carry_advice_provenance(run_dir: Path) -> bool:
    """True when any decision note references an advice artifact / feedback source.

    Used only to widen the Stage 0/1 surface check: a run that retried then
    pruned its advice directory still counts as having a supported surface.
    """
    decisions_dir = run_dir / _DECISIONS_DIRNAME
    if not decisions_dir.is_dir():
        return False
    for entry in sorted(decisions_dir.iterdir()):
        if not entry.is_file() or entry.suffix != ".json":
            continue
        decision = _load_json(entry)
        if decision is None:
            continue
        note = decision.get("note")
        if isinstance(note, str) and (
            "advice_artifact=" in note or "feedback_source=" in note
        ):
            return True
    return False


# ── advice artifact loading ─────────────────────────────────────────────────


def _load_advice_artifacts(run_dir: Path) -> list[tuple[str, dict[str, Any]]]:
    """Load every advice artifact as ``(relpath, payload)``, deterministically.

    Ordered by ``(created_at, filename)`` so attempt-suffixed divergent calls
    (``<id>.json`` then ``<id>_2.json`` …) keep a stable, causal order. Tolerant:
    skips non-JSON / malformed files.
    """
    advice_dir = run_dir / _ADVICE_DIRNAME
    if not advice_dir.is_dir():
        return []
    loaded: list[tuple[str, dict[str, Any]]] = []
    for entry in sorted(advice_dir.iterdir()):
        if not entry.is_file() or entry.suffix != ".json":
            continue
        payload = _load_json(entry)
        if payload is None:
            continue
        relpath = f"{_ADVICE_DIRNAME}/{entry.name}"
        loaded.append((relpath, payload))
    loaded.sort(key=lambda item: (str(item[1].get("created_at") or ""), item[0]))
    return loaded


# ── outcome classification ──────────────────────────────────────────────────


def _rejected_attempts(attempts: list[dict[str, Any]]) -> list[int]:
    """Indices (into ``attempts``) of attempts that are not a clear approval."""
    return [i for i, a in enumerate(attempts) if not _attempt_approved(a)]


def _is_applied_retry(recommended_action: str, applied_action: str | None) -> bool:
    """True when an applied ``retry_feedback`` decision backs the advice."""
    return (
        applied_action == "retry_feedback"
        and recommended_action not in _NON_RETRY_ACTIONS
    )


def _classify_structured(
    *,
    pre_fingerprint: frozenset[tuple[str, str, str]],
    next_attempt: dict[str, Any] | None,
) -> tuple[str, bool | None, bool]:
    """Classify an applied retry on a phase with STRUCTURED per-attempt findings.

    * ``repeated`` when the pre-retry finding fingerprint reappears in the next
      attempt (NEVER resolved with the same finding rejected).
    * ``resolved`` when the next attempt approved / the finding cleared.
    * ``unknown`` when the run ended before the next verdict.
    """
    if next_attempt is None:
        return "unknown", None, False
    next_fp = _fingerprint(next_attempt.get("findings"))
    if pre_fingerprint and (pre_fingerprint & next_fp):
        return "repeated", False, True
    if _attempt_approved(next_attempt) or not next_fp:
        return "resolved", True, False
    # Next attempt is rejected with a different finding set — the phase did not
    # clear, but we cannot prove the same finding repeated. Conservative: do not
    # claim resolved; report unknown.
    return "unknown", None, False


def _classify_signal(
    *,
    advice_round: int | None,
    rounds: list[dict[str, Any]],
) -> tuple[str, bool | None, bool]:
    """Classify an applied retry on the review/repair loop (text-only rounds).

    The outcome of an applied ``retry_feedback`` is decided by the **immediately
    following** verdict of the same phase — the retry's own review round — never
    by a later round. Uses durable signals only and NEVER claims ``resolved``
    while that next review re-rejected:

    * ``repeated`` when a LATER advice call exists for the same phase — the
      rejection recurred through the retry, so this retry did not end the loop.
      This is the conservative review-loop substitute for a finding fingerprint,
      which the round shape does not persist.
    * Otherwise classify by the NEAREST round after the advice round (the retry's
      review): a blank critique / approved round → ``resolved``; a non-empty
      critique (re-rejected) → ``repeated``, even if a *later* round eventually
      approved or the run reached terminal ``done`` (the advice's own retry did
      not clear it).
    * ``resolved`` via the WEAK downstream signal (a release gate approved / a
      terminal success status) ONLY when there is no post-advice round verdict at
      all AND no non-advice override (a ``phase_handoff_waiver`` / manual
      continue/feedback decision for this phase) — that override means the run
      may have finished DESPITE an unresolved review, so it must not read as the
      advice resolving it.
    * ``unknown`` otherwise (e.g. the run ended paused before any verdict).
    """
    post_advice = [r for r in rounds if _round_after(r, advice_round)]
    if post_advice:
        # The retry's own review is the NEAREST following round, not any later
        # one — a re-reject here is ``repeated`` even if round N+2 later passed.
        nearest = min(post_advice, key=lambda r: (_round_num(r) or 0))
        if _round_approved(nearest):
            return "resolved", True, False
        return "repeated", False, True
    return "unknown", None, False


# ── call construction ───────────────────────────────────────────────────────


def _usage_fields(usage: Any) -> dict[str, Any]:
    """Extract available usage fields from an advice artifact's ``usage`` map.

    Only present, non-None numeric fields are emitted. ``cost_usd_equivalent`` is
    included ONLY when the provider supplied accounting — never an invented
    estimate; its absence is meaningful (cost unknown).
    """
    out: dict[str, Any] = {}
    if not isinstance(usage, Mapping):
        return out
    for src_key, dst_key in (
        ("tokens_in", "tokens_in"),
        ("tokens_out", "tokens_out"),
        ("tokens_in_cache_read", "tokens_cached"),
        ("duration_s", "duration_s"),
    ):
        value = usage.get(src_key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[dst_key] = value
    cost = usage.get("cost_usd_equivalent")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        out["cost_usd_equivalent"] = cost
    model = usage.get("model")
    if isinstance(model, str) and model:
        out["model"] = model
    return out


def _build_call(
    relpath: str,
    payload: dict[str, Any],
    *,
    decision: dict[str, Any] | None,
    phases_meta: Mapping[str, Any],
    phase_attempts_by_name: dict[str, list[dict[str, Any]]],
    advice_index_in_phase: int,
    active_handoff: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one evidence call record from an advice artifact (+ optional decision)."""
    advice = payload.get("advice")
    advice = advice if isinstance(advice, Mapping) else {}
    phase = str(payload.get("phase") or "")
    handoff_id = str(payload.get("handoff_id") or "")
    recommended_action = str(advice.get("recommended_action") or "")
    confidence = str(advice.get("confidence") or "")

    applied_action = decision.get("action") if decision else None
    applied_action = applied_action if isinstance(applied_action, str) else None
    feedback_source = decision.get("feedback_source") if decision else None

    is_applied_retry = _is_applied_retry(recommended_action, applied_action)

    # Defaults — refined per classification path below.
    pre_findings: Any = None
    pre_fp: frozenset[tuple[str, str, str]] = frozenset()
    verdict = ""

    if not is_applied_retry:
        # Non-retry / unapplied advice never reaches the resolved/repeated
        # classifier; the advice "stopped" the loop by design.
        outcome, resolved, repeated = "stopped", None, False
        # Still surface the rejected verdict/findings when the matching handoff
        # is durably available (paused run), without fabricating otherwise.
        if str(active_handoff.get("id") or "") == handoff_id:
            verdict = str(active_handoff.get("verdict") or "").strip().upper()
            artifacts = active_handoff.get("artifacts")
            if isinstance(artifacts, Mapping):
                pre_findings = artifacts.get("findings")
                pre_fp = _fingerprint(pre_findings)
    elif phase_attempts_by_name.get(phase):
        # Structured finding-bearing phase whose attempts persist per-attempt
        # verdict + findings (validate_plan / final_acceptance / compliance_check
        # / cross_final_acceptance — or a forward-compat structured review_changes
        # shape, should a future adapter persist one): classify by fingerprint
        # match. Real review/repair-loop runs have NO such structured attempts
        # (they persist text-only ``rounds``) and fall to the signal path below.
        attempts = phase_attempts_by_name.get(phase, [])
        rejected_idx = _rejected_attempts(attempts)
        next_attempt: dict[str, Any] | None = None
        if advice_index_in_phase < len(rejected_idx):
            pre_pos = rejected_idx[advice_index_in_phase]
            pre_attempt = attempts[pre_pos]
            pre_findings = pre_attempt.get("findings")
            pre_fp = _fingerprint(pre_findings)
            verdict = _attempt_verdict(pre_attempt)
            if pre_pos + 1 < len(attempts):
                next_attempt = attempts[pre_pos + 1]
        outcome, resolved, repeated = _classify_structured(
            pre_fingerprint=pre_fp, next_attempt=next_attempt,
        )
    else:
        # Review/repair loop (or any phase without structured attempts):
        # classify from durable signals. Pull verdict/findings from the active
        # handoff payload when it is the one this advice fired on.
        if str(active_handoff.get("id") or "") == handoff_id:
            verdict = str(active_handoff.get("verdict") or "").strip().upper()
            artifacts = active_handoff.get("artifacts")
            if isinstance(artifacts, Mapping):
                pre_findings = artifacts.get("findings")
                pre_fp = _fingerprint(pre_findings)
        if not verdict:
            # Advice only ever fires on a rejected/incomplete handoff, so the
            # trigger verdict is REJECTED even when the active payload is gone
            # (terminal run) — this is a contract fact, not a guess.
            verdict = "REJECTED"
        outcome, resolved, repeated = _classify_signal(
            advice_round=_handoff_round(handoff_id),
            rounds=_rounds(phases_meta),
        )

    call: dict[str, Any] = {
        "handoff_id": handoff_id,
        "phase": phase,
        "advice_artifact": relpath,
        "trigger": _trigger_from_verdict(verdict),
        "verdict": verdict,
        "feedback_source": feedback_source,
        "recommended_action": recommended_action,
        "applied_action": applied_action,
        "confidence": confidence,
        "finding_fingerprint": _fingerprint_str(pre_fp),
        "resolved": resolved,
        "repeated": repeated,
        "outcome": outcome,
    }
    severity_counts = _severity_counts(pre_findings)
    if severity_counts:
        call["severity_counts"] = severity_counts
    call.update(_usage_fields(payload.get("usage")))
    return call


# ── summary aggregation ─────────────────────────────────────────────────────


def _build_summary(calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-call records into the stable summary block.

    Tokens (in/out), cache-read tokens, and duration are summed across the calls
    that report each — and ONLY emitted when at least one call carried that
    field, so a run without that signal keeps the historical shape rather than
    showing a fabricated zero. ``cost_usd_equivalent`` is aggregated ONLY when
    every token-bearing call carries accounting (a partial cost would mislead).
    The upper layer writes ``metrics.json['handoff_advice']`` straight from this
    ``usage`` block, so cached tokens / duration must survive here to reach it.
    """
    summary: dict[str, Any] = {
        "calls": len(calls),
        "applied_retries": sum(
            1 for c in calls if c.get("applied_action") == "retry_feedback"
        ),
        "resolved_retries": sum(1 for c in calls if c.get("outcome") == "resolved"),
        "repeated": sum(1 for c in calls if c.get("outcome") == "repeated"),
        "stopped": sum(1 for c in calls if c.get("outcome") == "stopped"),
        "unknown": sum(1 for c in calls if c.get("outcome") == "unknown"),
    }

    tokens_in = 0
    tokens_out = 0
    tokens_cached = 0
    duration_s = 0.0
    cost_total = 0.0
    saw_tokens = False
    saw_cached = False
    saw_duration = False
    all_have_cost = True
    for call in calls:
        if "tokens_in" in call or "tokens_out" in call:
            saw_tokens = True
            tokens_in += int(call.get("tokens_in") or 0)
            tokens_out += int(call.get("tokens_out") or 0)
            if "cost_usd_equivalent" in call:
                cost_total += float(call["cost_usd_equivalent"])
            else:
                all_have_cost = False
        if "tokens_cached" in call:
            saw_cached = True
            tokens_cached += int(call.get("tokens_cached") or 0)
        if "duration_s" in call:
            saw_duration = True
            duration_s += float(call.get("duration_s") or 0.0)

    if saw_tokens or saw_cached or saw_duration:
        usage: dict[str, Any] = {}
        if saw_tokens:
            usage["tokens_in"] = tokens_in
            usage["tokens_out"] = tokens_out
        if saw_cached:
            usage["tokens_cached"] = tokens_cached
        if saw_duration:
            usage["duration_s"] = round(duration_s, 3)
        if saw_tokens and all_have_cost:
            usage["cost_usd_equivalent"] = cost_total
        if usage:
            summary["usage"] = usage
    return summary


# ── public entry point ──────────────────────────────────────────────────────


def collect_handoff_advice(
    run_dir: Path | str, meta: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Normalize Stage 0/1 advice artifacts into ``{'calls', 'summary'}`` or None.

    ``meta`` is the ``meta.json`` mapping (the finalization flow passes
    ``run.session``; the collector passes the loaded ``meta.json`` — both carry
    the ``'phases'`` key). Outcome classification reads ``meta['phases']``.

    Returns ``None`` ONLY when the Stage 0/1 artifact surface is entirely absent
    (no advice artifacts AND no decision carrying advice provenance) — i.e.
    Stage 0/1 is not on HEAD. When advice artifacts exist, a call is emitted for
    EACH of them, even without a matching decision (then ``applied_action=None``
    and the outcome is ``stopped``).
    """
    run_dir = Path(run_dir)

    advice_artifacts = _load_advice_artifacts(run_dir)
    if not advice_artifacts and not _decisions_carry_advice_provenance(run_dir):
        return None

    phases_meta = meta.get("phases") if isinstance(meta, Mapping) else None
    phases_meta = phases_meta if isinstance(phases_meta, Mapping) else {}
    # Structured per-attempt findings are loaded for the finding-bearing phases
    # AND for the review-loop phases — the latter is empty in real runs (which
    # persist text-only ``rounds``) but honours a forward-compat structured
    # ``review_changes`` shape when one is present, routing it to fingerprint
    # classification instead of the signal path.
    phase_attempts_by_name: dict[str, list[dict[str, Any]]] = {
        name: _phase_attempts(phases_meta.get(name))
        for name in (_STRUCTURED_FINDING_PHASES | _REVIEW_LOOP_PHASES)
    }
    active_handoff = _active_handoff(meta)

    decisions_by_advice = _load_decisions_by_advice(run_dir)

    calls: list[dict[str, Any]] = []
    advice_seen_per_phase: dict[str, int] = {}
    for relpath, payload in advice_artifacts:
        phase = str(payload.get("phase") or "")
        idx = advice_seen_per_phase.get(phase, 0)
        advice_seen_per_phase[phase] = idx + 1
        calls.append(
            _build_call(
                relpath,
                payload,
                decision=decisions_by_advice.get(relpath),
                phases_meta=phases_meta,
                phase_attempts_by_name=phase_attempts_by_name,
                advice_index_in_phase=idx,
                active_handoff=active_handoff,
            )
        )

    return {"calls": calls, "summary": _build_summary(calls)}


__all__ = ["collect_handoff_advice"]
