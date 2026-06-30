"""Generic phase-handoff decisions — the sanctioned in-pipeline pause point.

A ``PhaseStep`` that declares a non-bypass ``handoff`` policy can pause the
run when its runtime trigger fires (e.g. ``validate_plan`` rejected on the
final automatic loop round under ``human_feedback_on_reject``). The
orchestrator writes ``meta.phase_handoff`` (the canonical active payload)
and exits rc=4 with ``meta.status="awaiting_phase_handoff"``.

``phase_handoff_decide(run_id, handoff_id, action, ...)`` is how a human
(or supervisor agent) resolves that pause. Four actions:

- ``continue`` — proceed past the paused phase as a manual override. The
  machine verdict is preserved (no rewrite to approved). The decision is
  recorded; ``orcho_run_resume`` later reads it and injects a
  ``phase_handoff_override`` marker so the loop runner exits without
  mutating ``validate_plan.approved``.
- ``retry_feedback`` — inject feedback and run exactly one extra
  human-directed ``plan → validate_plan`` round. ``LoopStep.max_rounds``
  is not mutated; the runner tracks ``human_directed_rounds`` separately.
  Requires ``feedback``.
- ``halt`` — finalise the run as halted. ``meta.status`` flips to
  ``halted``; ``meta.phase_handoff`` is no longer treated as active.
- ``continue_with_waiver`` — proceed past the paused phase like
  ``continue`` (machine verdict stays REJECTED, no extra reviewer round),
  but require a non-empty operator verdict and durably record a waiver.
  The waiver is authoritatively injected into all downstream review gates
  so the waived findings are not reopened as blocking. Requires
  ``feedback`` (the operator verdict).

The function NEVER spawns a process. It is a pure state transition: it
reads ``meta``, validates ``handoff_id`` against the active payload,
validates ``action`` against the runtime-produced ``available_actions``,
writes a decision artifact, and (for ``halt``) flips status. Actual
continuation (continue / retry_feedback / continue_with_waiver) lives in
``orcho_run_resume``.

Decision artifacts live under
``<run_dir>/phase_handoff_decisions/{safe_handoff_id}.json`` — directory
per run, file per handoff id — so several sequential handoffs in a single
run coexist. ``safe_handoff_id`` is deterministic and collision-resistant:
a sanitised slug of ``handoff_id`` plus a short hash of the original id,
so two distinct ids never alias.

Idempotency is **exact-payload**: a repeat call with the same
``handoff_id`` + ``action`` + ``feedback`` + ``note`` returns the
persisted decision unchanged (no rewrite, no ``decided_at`` refresh). A
repeat with the same ``handoff_id`` but a different action / feedback /
note raises — the decision artifact is the audit record of the *exact*
human instruction used on resume, so transports retrying with
inconsistent payloads is a conflict, not a silent overwrite.

Halt is idempotent-against-artifact: after the first ``halt`` flips
``meta.status`` to ``halted`` (and clears the active handoff), a repeat
exact-payload ``halt`` for the same ``handoff_id`` still succeeds against
the persisted artifact alone — no active ``meta.phase_handoff`` is
required for idempotent replay.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pipeline.run_state.status_vocab import INTERRUPTED_STATUS, PAUSE_STATUS
from pipeline.run_state.terminal import mark_run_halted
from pipeline.run_state.types import HandoffAction
from pipeline.runtime.roles import PhaseHandoffAction
from sdk.actions import Action, compute_next_actions
from sdk.errors import InvalidPhaseHandoffState
from sdk.runs import _CWD_DEFAULT, find_run, load_meta

PhaseHandoffActionValue = Literal[
    "continue", "retry_feedback", "halt", "continue_with_waiver",
]

_DECISIONS_DIRNAME = "phase_handoff_decisions"
_HALTED_STATUS = "halted"
_VALID_ACTIONS: frozenset[str] = frozenset({a.value for a in PhaseHandoffAction})
# The active (non-terminal) resume actions whose *application* lives in
# ``orcho_run_resume`` — continue / continue_with_waiver / retry_feedback.
# Sourced from the shared run_state transition enum so the project resume
# path and this SDK decision path classify the active/terminal split from one
# contract. ``halt`` is deliberately absent: it is the terminal action, applied
# here only as a status flip through ``mark_run_halted`` (terminal.py), never
# resumed.
_ACTIVE_RESUME_ACTIONS: frozenset[str] = frozenset(a.value for a in HandoffAction)
_SAFE_ID_SLUG_LIMIT = 80
_SAFE_ID_HASH_LEN = 10


@dataclass(frozen=True, slots=True)
class PhaseHandoffDecision:
    """The persisted handoff decision. Returned by ``phase_handoff_decide``.

    ``next_actions`` (MCP UX A1, Principle 1): suggested follow-up
    tool calls. The decision API is a pure state transition — it
    does NOT spawn a process — so for ``continue`` / ``retry_feedback``
    the natural follow-up is ``orcho_run_resume`` to actually
    advance the run. For ``halt`` the run is terminal and the
    follow-ups come from the post-halt meta state (resume to a fresh
    attempt, spawn a child via ``--from-run-plan`` if a plan was
    persisted, etc.).
    """

    run_id: str
    handoff_id: str
    phase: str
    action: PhaseHandoffActionValue
    feedback: str | None
    note: str | None
    decided_at: str  # ISO 8601, UTC
    next_actions: tuple = ()  # tuple[Action, ...] — avoids forward-ref cycle


def safe_handoff_id(handoff_id: str) -> str:
    """Encode ``handoff_id`` as a filesystem-safe, collision-resistant slug.

    The encoder concatenates a sanitised slug (alnum + ``_``) with a short
    SHA-256 hash of the *original* id. Two distinct ids never alias: the
    hash is computed over the raw input, so even ids whose slugs collide
    after sanitisation get distinct artifact names.
    """
    if not isinstance(handoff_id, str) or not handoff_id:
        raise ValueError("safe_handoff_id: handoff_id must be a non-empty string")
    sanitised = re.sub(r"[^A-Za-z0-9_]+", "_", handoff_id).strip("_")
    if not sanitised:
        sanitised = "handoff"
    if len(sanitised) > _SAFE_ID_SLUG_LIMIT:
        sanitised = sanitised[:_SAFE_ID_SLUG_LIMIT].rstrip("_") or "handoff"
    digest = hashlib.sha256(handoff_id.encode("utf-8")).hexdigest()[:_SAFE_ID_HASH_LEN]
    return f"{sanitised}_{digest}"


def phase_handoff_decide(
    run_id: str,
    handoff_id: str,
    action: PhaseHandoffActionValue,
    *,
    feedback: str | None = None,
    note: str | None = None,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> PhaseHandoffDecision:
    """Resolve a paused phase handoff.

    Validates ``handoff_id`` against the active ``meta.phase_handoff``
    payload (or, for idempotent ``halt`` replay, against an existing
    decision artifact). Validates ``action`` against the runtime-produced
    ``available_actions`` published in the active payload.

    Writes a decision artifact under
    ``<run_dir>/phase_handoff_decisions/{safe_handoff_id}.json``. For
    ``halt``, synchronously flips ``meta.status`` to ``halted`` and clears
    the active ``meta.phase_handoff`` payload. ``continue`` /
    ``retry_feedback`` do not spawn a process — actual continuation lives
    in ``orcho_run_resume``.

    Raises:
        ValueError: ``action`` not in the canonical set, or
            ``retry_feedback`` without ``feedback``.
        InvalidPhaseHandoffState: ``handoff_id`` does not match the
            active handoff (or any existing decision artifact); or the
            run is not in ``awaiting_phase_handoff`` (and no replay
            artifact exists); or a prior decision for the same
            ``handoff_id`` recorded a different payload.
    """
    # Validate every caller-supplied field *before* touching the
    # filesystem. ``find_run("")`` falls through to "newest run", so an
    # empty ``run_id`` from a buggy MCP/UI call would otherwise mutate
    # whatever run happens to sort newest — a decision API has no such
    # licence. ``note`` mirrors ``feedback`` in the persisted schema and
    # must be a string or None, not silently coerced.
    if not isinstance(run_id, str) or not run_id:
        raise ValueError(
            "phase_handoff_decide: run_id must be a non-empty string"
        )
    if not isinstance(handoff_id, str) or not handoff_id:
        raise ValueError(
            "phase_handoff_decide: handoff_id must be a non-empty string"
        )
    _validate_action(action)
    _validate_feedback(action, feedback)
    if note is not None and not isinstance(note, str):
        raise ValueError(
            f"phase_handoff_decide: note must be a string or None, "
            f"got {type(note).__name__}"
        )

    ref = find_run(run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd)
    meta = load_meta(ref.run_dir)
    meta_status = meta.get("status")
    active = meta.get("phase_handoff") if isinstance(meta, dict) else None

    decisions_dir = ref.run_dir / _DECISIONS_DIRNAME
    artifact_path = decisions_dir / f"{safe_handoff_id(handoff_id)}.json"
    existing = _read_existing_strict(artifact_path, run_id, handoff_id)

    if existing is not None:
        # Exact-payload idempotency: same run_id + handoff_id + action +
        # feedback + note → return the persisted record unchanged. Any
        # field divergence is a conflict, not a silent overwrite. The
        # id comparison defends against (a) corrupted audit artifacts
        # whose persisted ids don't match the path, and (b) a theoretical
        # ``safe_handoff_id`` hash collision aliasing two distinct ids.
        if (
            existing.action == action
            and existing.feedback == feedback
            and existing.note == note
            and existing.handoff_id == handoff_id
            and existing.run_id == run_id
        ):
            # ``next_actions`` is state-derived, not persisted in the
            # artifact. Recompute on every return so an idempotent
            # replay surfaces fresh suggestions reflecting current
            # meta state (e.g. if the operator already called resume
            # between the two decide calls and meta has advanced).
            existing_next_actions = _next_actions_after_decide(
                action=action, run_id=run_id, meta_after_decide=meta,
            )
            return dataclasses.replace(
                existing, next_actions=existing_next_actions,
            )
        raise InvalidPhaseHandoffState(
            f"phase_handoff_decide: run {run_id} handoff {handoff_id!r} "
            f"already decided ({existing.action!r}, feedback="
            f"{existing.feedback!r}, note={existing.note!r}); refusing to "
            f"overwrite with ({action!r}, feedback={feedback!r}, "
            f"note={note!r}). Decisions are exact-payload idempotent."
        )

    # No prior artifact → the run must be actively paused on this handoff
    # with a well-formed payload. A process can be interrupted/crash after
    # the handoff payload lands but before the operator decision is written;
    # that leaves ``status='interrupted'`` plus an active payload. Treat it
    # as the same pending handoff so an operator can still halt or decide
    # the run instead of hand-editing meta.json.
    if not _is_decidable_handoff_status(meta_status, active):
        raise InvalidPhaseHandoffState(
            f"phase_handoff_decide: run {run_id} is not awaiting a phase "
            f"handoff (meta.status={meta_status!r}). The pause must be in "
            "effect before a decision can be recorded."
        )
    if not isinstance(active, dict):
        raise InvalidPhaseHandoffState(
            f"phase_handoff_decide: run {run_id} is in {PAUSE_STATUS!r} "
            "but meta.phase_handoff is missing or not an object — the "
            "active handoff payload is required to decide. Manual repair "
            "of meta.json is required."
        )
    active_id = active.get("id")
    if not isinstance(active_id, str) or not active_id:
        raise InvalidPhaseHandoffState(
            f"phase_handoff_decide: run {run_id} active phase_handoff "
            "payload is missing a non-empty 'id'. Manual repair of "
            "meta.json is required."
        )
    if active_id != handoff_id:
        raise InvalidPhaseHandoffState(
            f"phase_handoff_decide: run {run_id} active handoff id is "
            f"{active_id!r}; refusing to decide on {handoff_id!r}. The "
            "client must read the current handoff payload before deciding."
        )
    available_actions = _extract_available_actions(active, handoff_id)
    if action not in available_actions:
        raise InvalidPhaseHandoffState(
            f"phase_handoff_decide: action {action!r} is not in the active "
            f"handoff's available_actions {sorted(available_actions)!r} for "
            f"handoff {handoff_id!r}. Action availability is decided by the "
            "runtime from the verdict, not by the client."
        )
    active_phase = active.get("phase")
    if not isinstance(active_phase, str) or not active_phase:
        raise InvalidPhaseHandoffState(
            f"phase_handoff_decide: run {run_id} active phase_handoff "
            "payload is missing a non-empty 'phase'. Manual repair of "
            "meta.json is required."
        )

    decided_at = datetime.now(UTC).isoformat(timespec="seconds")
    payload_without_actions = PhaseHandoffDecision(
        run_id=run_id,
        handoff_id=handoff_id,
        phase=active_phase,
        action=action,
        feedback=feedback,
        note=note,
        decided_at=decided_at,
    )
    _write_artifact(artifact_path, payload_without_actions)

    if action == "halt":
        # Terminal transition: pause → halted. The SDK directly mutates
        # meta.json because the alternative is to spawn a no-op
        # subprocess just to flip status. ``meta.phase_handoff`` is no
        # longer treated as active after halt; it's cleared here so
        # pending-handoff queues key on status + active payload, not on
        # stale state.
        mark_run_halted(
            meta, halt_reason="phase_handoff_halt", halted_at=decided_at,
        )
        try:
            (ref.run_dir / "meta.json").write_text(
                json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError as e:
            raise InvalidPhaseHandoffState(
                f"phase_handoff_decide: wrote decision artifact but could "
                f"not flip meta.status to 'halted': {e}. Run is in "
                "inconsistent state — fix meta.json by hand."
            ) from e
        # Finalize the evidence bundle so a halted run lands on disk in
        # the same shape as a ``done`` run. Without this, the pipeline
        # subprocess that exited at the pause never got to write
        # ``evidence.json`` / ``evidence.md``, and the halt-side meta
        # flip alone leaves the run terminal-but-bundle-less — every
        # post-mortem of a halted run would have to fall back to raw
        # ``events.jsonl`` + ``meta.json``. ``write_bundle_or_placeholder``
        # falls back to the REA-0 placeholder when collection fails
        # (partial events, schema mismatch on a partial run), so this
        # call is best-effort but always produces a file.
        # Filesystem error on bundle write must not poison the halt
        # transition — meta flip already succeeded, the audit artifact
        # is on disk, and the operator can re-run
        # ``write_bundle_or_placeholder`` manually if needed.
        import contextlib as _ctx

        from pipeline.evidence import write_bundle_or_placeholder
        with _ctx.suppress(OSError):
            write_bundle_or_placeholder(
                ref.run_dir, run_id=run_id, status=_HALTED_STATUS,
            )

    # MCP UX A1 / Principle 1: derive next-step suggestions for the
    # caller. For ``continue`` / ``retry_feedback`` the decision API
    # does NOT spawn anything — caller must follow up with
    # ``orcho_run_resume`` to advance. We surface that single
    # required follow-up explicitly. For ``halt`` the run is terminal
    # and the post-halt meta drives suggestions via the generic
    # compute_next_actions helper (resume + possibly from_run_plan).
    next_actions = _next_actions_after_decide(
        action=action, run_id=run_id, meta_after_decide=meta,
    )
    return dataclasses.replace(payload_without_actions, next_actions=next_actions)


def _is_decidable_handoff_status(status: Any, active: Any) -> bool:
    """Return True when ``meta`` represents a decision-ready handoff.

    ``awaiting_phase_handoff`` is the normal state. ``interrupted`` with
    an active payload is a torn equivalent produced when resume/prompt
    handling is interrupted after the handoff is persisted; accepting it
    here lets the sanctioned decision API repair the run.
    """
    if status == PAUSE_STATUS:
        return True
    return status == INTERRUPTED_STATUS and isinstance(active, dict)


def load_active_phase_handoff(
    run_id: str,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> dict[str, Any] | None:
    """Return the canonical active ``meta.phase_handoff`` payload, or None.

    Reads from ``meta.json`` only. Callers (UI / MCP status / resume)
    must opt-in to also read decision artifacts via
    :func:`load_phase_handoff_decisions`.
    """
    ref = find_run(run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd)
    meta = load_meta(ref.run_dir)
    active = meta.get("phase_handoff") if isinstance(meta, dict) else None
    if not isinstance(active, dict):
        return None
    return active


def load_phase_handoff_decisions(
    run_id: str,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> list[PhaseHandoffDecision]:
    """Return every persisted ``PhaseHandoffDecision`` for ``run_id``.

    Sorted by ``decided_at`` (string sort is sufficient because the
    timestamps are ISO 8601 in UTC). Returns an empty list when the
    decisions directory is absent or empty.
    """
    ref = find_run(run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd)
    decisions_dir = ref.run_dir / _DECISIONS_DIRNAME
    if not decisions_dir.is_dir():
        return []
    decisions: list[PhaseHandoffDecision] = []
    for entry in sorted(decisions_dir.iterdir()):
        if entry.is_file() and entry.suffix == ".json":
            parsed = _read_existing_lenient(entry)
            if parsed is not None:
                decisions.append(parsed)
    decisions.sort(key=lambda d: (d.decided_at, d.handoff_id))
    return decisions


def load_phase_handoff_decision(
    run_id: str,
    handoff_id: str,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> PhaseHandoffDecision | None:
    """Load a single decision by ``handoff_id`` with strict audit-shape validation.

    Distinguishes:

    * **Absent.** Returns ``None`` so resume consumers can short-circuit.
    * **Present and well-formed.** Returns the parsed
      :class:`PhaseHandoffDecision`. The strict reader validates every
      persisted field (run_id / handoff_id / phase / action / feedback /
      note / decided_at) and refuses to materialise a decision whose
      persisted ids do not match the artifact path.
    * **Present but corrupt.** Raises :class:`InvalidPhaseHandoffState`
      — the decision artifact is the audit record of the exact human
      instruction used on resume; silently downgrading a corrupt
      artifact to "absent" would let resume trust a tampered payload.

    Distinct from :func:`load_phase_handoff_decisions` (lenient bulk
    reader for UI/list rendering, skips bad files) — this is the
    gatekeeper for new transitions and resume flows.
    """
    ref = find_run(run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd)
    artifact = (
        ref.run_dir / _DECISIONS_DIRNAME
        / f"{safe_handoff_id(handoff_id)}.json"
    )
    return _read_existing_strict(artifact, run_id, handoff_id)


def _validate_action(action: str) -> None:
    if action not in _VALID_ACTIONS:
        raise ValueError(
            f"phase_handoff_decide: action must be one of "
            f"{sorted(_VALID_ACTIONS)!r}, got {action!r}"
        )


_FEEDBACK_REQUIRED_ACTIONS: frozenset[str] = frozenset({
    PhaseHandoffAction.RETRY_FEEDBACK.value,
    PhaseHandoffAction.CONTINUE_WITH_WAIVER.value,
})


def _validate_feedback(action: str, feedback: str | None) -> None:
    if action in _FEEDBACK_REQUIRED_ACTIONS and (
        feedback is None
        or not isinstance(feedback, str)
        or not feedback.strip()
    ):
        raise ValueError(
            f"phase_handoff_decide: {action} requires a non-empty "
            "feedback string — retry_feedback injects it into the next "
            "plan round; continue_with_waiver records it as the durable "
            "operator verdict that waives the rejected findings."
        )
    if feedback is not None and not isinstance(feedback, str):
        raise ValueError(
            f"phase_handoff_decide: feedback must be a string or None, "
            f"got {type(feedback).__name__}"
        )


def _extract_available_actions(
    active: dict[str, Any], handoff_id: str,
) -> frozenset[str]:
    """Extract a well-formed, non-empty ``available_actions`` set.

    ``available_actions`` is the only sanctioned source of action
    availability — the runtime publishes it based on verdict + handoff
    policy. A missing key, a wrong type, or an empty list is a runtime
    contract violation, not a license for the SDK to fall back to "all
    canonical actions". Each case raises ``InvalidPhaseHandoffState``.
    """
    raw_actions = active.get("available_actions")
    if not isinstance(raw_actions, (list, tuple)):
        raise InvalidPhaseHandoffState(
            f"phase_handoff_decide: handoff {handoff_id!r} active payload "
            "is missing 'available_actions' or it is not a list. The "
            "runtime must publish action availability when pausing for a "
            "decision."
        )
    if not raw_actions:
        raise InvalidPhaseHandoffState(
            f"phase_handoff_decide: handoff {handoff_id!r} active payload "
            "has an empty 'available_actions'. With no permitted action, "
            "no decision can be recorded; manual repair of meta.json is "
            "required."
        )
    actions: set[str] = set()
    for entry in raw_actions:
        if not isinstance(entry, str) or not entry:
            raise InvalidPhaseHandoffState(
                f"phase_handoff_decide: handoff {handoff_id!r} active "
                f"payload 'available_actions' contains a non-string or "
                f"empty entry ({entry!r})."
            )
        actions.add(entry)
    return frozenset(actions)


def _read_existing_strict(
    artifact_path: Path, run_id: str, handoff_id: str,
) -> PhaseHandoffDecision | None:
    """Strict reader for ``phase_handoff_decide``.

    Distinguishes three cases:

    * **Absent.** No file → return ``None``; caller proceeds to write a
      fresh decision.
    * **Present and parseable.** Returns the persisted
      ``PhaseHandoffDecision`` for the idempotency check.
    * **Present but unreadable / invalid JSON / wrong shape / persisted
      ids mismatch the path.** Raises ``InvalidPhaseHandoffState``. The
      decision artifact is the audit record of the exact human
      instruction used on resume; silently overwriting a corrupt artifact
      would lose that record. The caller must repair the file manually.
    """
    if not artifact_path.exists():
        return None
    if not artifact_path.is_file():
        raise InvalidPhaseHandoffState(
            f"phase_handoff_decide: decision artifact path "
            f"{str(artifact_path)!r} exists but is not a regular file. "
            "Manual repair required."
        )
    try:
        raw_text = artifact_path.read_text(encoding="utf-8")
    except OSError as e:
        raise InvalidPhaseHandoffState(
            f"phase_handoff_decide: cannot read decision artifact "
            f"{str(artifact_path)!r}: {e}. Manual repair required."
        ) from e
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise InvalidPhaseHandoffState(
            f"phase_handoff_decide: decision artifact "
            f"{str(artifact_path)!r} is not valid JSON ({e}). The "
            "artifact is the audit record of a prior decision; refusing "
            "to overwrite. Manual repair required."
        ) from e
    if not isinstance(raw, dict):
        raise InvalidPhaseHandoffState(
            f"phase_handoff_decide: decision artifact "
            f"{str(artifact_path)!r} is not a JSON object."
        )
    action = raw.get("action")
    if action not in _VALID_ACTIONS:
        raise InvalidPhaseHandoffState(
            f"phase_handoff_decide: decision artifact "
            f"{str(artifact_path)!r} has invalid action {action!r}; "
            f"expected one of {sorted(_VALID_ACTIONS)!r}. Manual repair "
            "required."
        )
    raw_run_id = raw.get("run_id")
    if not isinstance(raw_run_id, str) or not raw_run_id:
        raise InvalidPhaseHandoffState(
            f"phase_handoff_decide: decision artifact "
            f"{str(artifact_path)!r} has missing or non-string 'run_id'. "
            "Manual repair required."
        )
    raw_handoff_id = raw.get("handoff_id")
    if not isinstance(raw_handoff_id, str) or not raw_handoff_id:
        raise InvalidPhaseHandoffState(
            f"phase_handoff_decide: decision artifact "
            f"{str(artifact_path)!r} has missing or non-string "
            "'handoff_id'. Manual repair required."
        )
    raw_phase = raw.get("phase")
    if not isinstance(raw_phase, str) or not raw_phase:
        raise InvalidPhaseHandoffState(
            f"phase_handoff_decide: decision artifact "
            f"{str(artifact_path)!r} has missing or non-string 'phase'. "
            "Manual repair required."
        )
    raw_decided_at = raw.get("decided_at")
    if not isinstance(raw_decided_at, str) or not raw_decided_at:
        raise InvalidPhaseHandoffState(
            f"phase_handoff_decide: decision artifact "
            f"{str(artifact_path)!r} has missing or non-string "
            "'decided_at'. Manual repair required."
        )
    raw_feedback = raw.get("feedback")
    if raw_feedback is not None and not isinstance(raw_feedback, str):
        raise InvalidPhaseHandoffState(
            f"phase_handoff_decide: decision artifact "
            f"{str(artifact_path)!r} has non-string, non-null "
            f"'feedback' ({type(raw_feedback).__name__}). Manual repair "
            "required."
        )
    raw_note = raw.get("note")
    if raw_note is not None and not isinstance(raw_note, str):
        raise InvalidPhaseHandoffState(
            f"phase_handoff_decide: decision artifact "
            f"{str(artifact_path)!r} has non-string, non-null 'note' "
            f"({type(raw_note).__name__}). Manual repair required."
        )
    decision = PhaseHandoffDecision(
        run_id=raw_run_id,
        handoff_id=raw_handoff_id,
        phase=raw_phase,
        action=action,
        feedback=raw_feedback,
        note=raw_note,
        decided_at=raw_decided_at,
    )
    # The artifact lives in ``<run_dir>/phase_handoff_decisions/{safe(handoff_id)}.json``;
    # its persisted ids must agree with the path. A mismatch signals
    # corruption (manual edit, copy-paste, hash collision) and must not
    # be papered over by silent overwrite.
    if decision.run_id != run_id or decision.handoff_id != handoff_id:
        raise InvalidPhaseHandoffState(
            f"phase_handoff_decide: decision artifact "
            f"{str(artifact_path)!r} persists run_id={decision.run_id!r}, "
            f"handoff_id={decision.handoff_id!r}, but the path resolves "
            f"to run_id={run_id!r}, handoff_id={handoff_id!r}. The audit "
            "record is corrupted; manual repair required."
        )
    return decision


def _read_existing_lenient(
    artifact_path: Path,
) -> PhaseHandoffDecision | None:
    """Lenient reader for the list loader (UI / audit display).

    Returns ``None`` on any read failure or shape error so a single
    corrupted file does not break a directory scan. The strict reader
    above is the gatekeeper for new decisions; this one is purely for
    display.
    """
    if not artifact_path.is_file():
        return None
    try:
        raw = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    action = raw.get("action")
    if action not in _VALID_ACTIONS:
        return None
    return PhaseHandoffDecision(
        run_id=str(raw.get("run_id", "")),
        handoff_id=str(raw.get("handoff_id", "")),
        phase=str(raw.get("phase", "")),
        action=action,
        feedback=raw.get("feedback"),
        note=raw.get("note"),
        decided_at=str(raw.get("decided_at", "")),
    )


def _write_artifact(artifact_path: Path, payload: PhaseHandoffDecision) -> None:
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(
            {
                "run_id":     payload.run_id,
                "handoff_id": payload.handoff_id,
                "phase":      payload.phase,
                "action":     payload.action,
                "feedback":   payload.feedback,
                "note":       payload.note,
                "decided_at": payload.decided_at,
            },
            indent=2,
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )


def record_decision_artifact(
    run_dir: Path,
    handoff_id: str,
    decision: PhaseHandoffDecision,
    *,
    skip_status_guard: bool = True,
) -> PhaseHandoffDecision:
    """Idempotently persist a phase-handoff decision artifact.

    Factored out of the public ``phase_handoff_decide`` write so an internal
    caller can record a decision WITHOUT the public path's active-pause
    preconditions. The exact-payload idempotency and conflict semantics are
    identical to the public path (a repeat with the same
    ``action``/``feedback``/``note``/ids returns the persisted record; a
    divergent payload for the same ``handoff_id`` raises) — ``decided_by`` is
    NOT part of the artifact schema and therefore never participates in this
    comparison; it is applier-set provenance carried on the waiver payload, not
    the decision artifact.

    ``skip_status_guard`` exists for the synthetic auto-waiver path (ADR 0073):
    that waiver is recorded mid-implement while ``meta.status`` is still
    ``running`` — there is no active pause to validate against, and the public
    ``phase_handoff_decide`` (which requires ``awaiting_phase_handoff``) must
    NOT be invoked on a running run. When ``False`` the run must currently be
    paused on a phase handoff, mirroring the public guard ordering
    (idempotency/conflict is checked first, then the status guard, then write).

    This function does not touch the public ``phase_handoff_decide`` body; that
    path keeps its own inline guard + idempotency unchanged.
    """
    artifact_path = (
        run_dir / _DECISIONS_DIRNAME / f"{safe_handoff_id(handoff_id)}.json"
    )
    existing = _read_existing_strict(artifact_path, decision.run_id, handoff_id)
    if existing is not None:
        if (
            existing.action == decision.action
            and existing.feedback == decision.feedback
            and existing.note == decision.note
            and existing.handoff_id == decision.handoff_id
            and existing.run_id == decision.run_id
        ):
            return existing
        raise InvalidPhaseHandoffState(
            f"record_decision_artifact: run {decision.run_id} handoff "
            f"{handoff_id!r} already decided ({existing.action!r}, feedback="
            f"{existing.feedback!r}, note={existing.note!r}); refusing to "
            f"overwrite with ({decision.action!r}, feedback="
            f"{decision.feedback!r}, note={decision.note!r}). Decisions are "
            "exact-payload idempotent."
        )
    if not skip_status_guard:
        meta = load_meta(run_dir)
        if meta.get("status") != PAUSE_STATUS:
            raise InvalidPhaseHandoffState(
                f"record_decision_artifact: run {decision.run_id} is not "
                f"awaiting a phase handoff (meta.status="
                f"{meta.get('status')!r}); the pause must be in effect before "
                "a guarded decision can be recorded."
            )
    _write_artifact(artifact_path, decision)
    return decision


def write_synthetic_waiver_decision(
    run_dir: Path,
    *,
    run_id: str,
    handoff_id: str,
    phase: str,
    feedback: str,
    note: str | None = None,
    decided_at: str | None = None,
) -> PhaseHandoffDecision:
    """Record a synthetic ``continue_with_waiver`` decision for an auto-waiver.

    Used by the ADR 0073 implement substance-repair fallback when
    ``on_exhausted='auto_waiver'`` fires mid-run: it persists the same decision
    artifact a human ``continue_with_waiver`` would, but goes through
    :func:`record_decision_artifact` with ``skip_status_guard=True`` because the
    run is still ``running`` (no active pause). ``feedback`` is the waiver text
    and must be non-empty — a waiver must record why the incomplete delivery is
    accepted.
    """
    if not isinstance(feedback, str) or not feedback.strip():
        raise ValueError(
            "write_synthetic_waiver_decision: feedback (waiver text) must be a "
            "non-empty string."
        )
    decided_at = decided_at or datetime.now(UTC).isoformat(timespec="seconds")
    decision = PhaseHandoffDecision(
        run_id=run_id,
        handoff_id=handoff_id,
        phase=phase,
        action="continue_with_waiver",
        feedback=feedback,
        note=note,
        decided_at=decided_at,
    )
    return record_decision_artifact(
        run_dir, handoff_id, decision, skip_status_guard=True,
    )


def _next_actions_after_decide(
    *,
    action: PhaseHandoffActionValue,
    run_id: str,
    meta_after_decide: Mapping[str, Any],
) -> tuple[Action, ...]:
    """Compute ``next_actions`` for a fresh PhaseHandoffDecision.

    The decision API does not spawn anything — it is a pure state
    transition. Callers always need to follow up:

    * ``continue`` / ``retry_feedback`` / ``continue_with_waiver``: the
      run is still paused (meta.status stays ``awaiting_phase_handoff``)
      but the decision artifact has been written. Calling
      ``orcho_run_resume`` is the mandatory next step; the resume reads
      the decision artifact and advances the run.
    * ``halt``: meta.status has been flipped to ``halted`` and
      meta.phase_handoff cleared. The post-halt meta drives the
      suggestion set via the generic ``compute_next_actions``
      helper — typically ``orcho_run_resume`` (to restart from
      checkpoint) and, when a plan was persisted,
      ``orcho_run_start`` with ``from_run_plan=<id>``.
    """
    if action in _ACTIVE_RESUME_ACTIONS:
        return (
            Action(
                intent=(
                    "Resume the run so the decision takes effect "
                    "(the decide API only wrote the artifact; the "
                    "pipeline subprocess starts via resume)."
                ),
                tool="orcho_run_resume",
                args={"run_id": run_id},
                optional=False,
            ),
        )
    # ``halt`` path: meta has been mutated to halted; let the
    # generic computer surface resume + any from-run-plan
    # suggestion the post-halt state supports.
    return compute_next_actions(meta_after_decide, run_id=run_id)
