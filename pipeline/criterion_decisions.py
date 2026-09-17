# SPDX-License-Identifier: Apache-2.0
"""pipeline.criterion_decisions — durable typed human decisions (ADR 0188 §5).

One append-only artifact per run, ``criterion_decisions.json``, holding the
per-criterion decision chains an operator recorded. A ``human`` acceptance
criterion stays ``pending`` until a matching record exists here; general phase
continuation or a gate waiver never satisfies it.

Record shape — required ``decision_id``, ``run_id``, ``criterion_id``,
``decision`` (``accept|reject``), ``recorded_at``; optional ``note``,
``actor``, ``supersedes``. Optional keys are **absent** when unused: ``null``
is never written and is rejected on read. ``recorded_at`` is writer-assigned
canonical RFC 3339 UTC text (``YYYY-MM-DDTHH:MM:SS[.ffffff]Z``); the writer
normalizes an aware datetime to UTC and rejects a naive one *before* write.

Supersession is append-only with a single head: the first decision for a
criterion omits ``supersedes``; every later one must name the current head's
``decision_id``. A stale supersession (naming an already-superseded record) and
a branched supersession (a second decision naming the same previous head) are
both rejected before write, leaving the artifact byte-identical.

Those guarantees are only real if the read-validate-append-replace cycle is
atomic against other writers. It is not naturally: the log is rewritten whole
under an atomic rename, so two writers that both read the same head would each
compute a valid-looking append and the second rename would silently discard
the first record — losing an operator decision and forking the chain past the
validator. :func:`record_human_decision` therefore holds an exclusive
inter-process lock (``criterion_decisions.lock``) across the whole cycle, so
concurrent replacements serialize and exactly one wins; the loser then reads
the winner's record as the head and is rejected as a branched supersession.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pipeline.criterion_matrix import HumanDecisionFact

__all__ = [
    "DECISIONS_FILENAME",
    "DECISIONS_LOCK_FILENAME",
    "DECISION_SCHEMA_VERSION",
    "DECISION_VALUES",
    "RECORDED_AT_RE",
    "HumanDecision",
    "HumanDecisionError",
    "decidable_human_criterion",
    "is_canonical_recorded_at",
    "decision_chain",
    "decision_chain_head",
    "decisions_path",
    "human_decision_facts",
    "load_human_decisions",
    "record_human_decision",
    "run_identity",
    "to_recorded_at",
]

DECISIONS_FILENAME = "criterion_decisions.json"

#: Lock file guarding the read-validate-append-replace cycle. Deliberately a
#: SEPARATE path from the journal: the journal is replaced by an atomic
#: rename, so a lock held on its inode would stop guarding the file that
#: subsequent writers actually open.
DECISIONS_LOCK_FILENAME = "criterion_decisions.lock"
DECISION_SCHEMA_VERSION = "1"
DECISION_VALUES: tuple[str, ...] = ("accept", "reject")

#: Shape of the canonical RFC 3339 UTC text the writer assigns. Digit layout
#: only — :func:`is_canonical_recorded_at` also checks that the digits name a
#: real UTC instant, so a hand-edited ``2026-99-99T99:99:99Z`` cannot pass.
RECORDED_AT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{6})?Z$"
)

_REQUIRED_KEYS: frozenset[str] = frozenset(
    {"decision_id", "run_id", "criterion_id", "decision", "recorded_at"}
)
_OPTIONAL_KEYS: frozenset[str] = frozenset({"note", "actor", "supersedes"})


class HumanDecisionError(ValueError):
    """Raised for an invalid decision payload or an invalid supersession."""


@dataclass(frozen=True, slots=True)
class HumanDecision:
    """One durable decision record."""

    decision_id: str
    run_id: str
    criterion_id: str
    decision: Literal["accept", "reject"]
    recorded_at: str
    note: str | None = None
    actor: str | None = None
    supersedes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Durable JSON shape. Unused optional keys are absent, never null."""
        out: dict[str, Any] = {
            "decision_id": self.decision_id,
            "run_id": self.run_id,
            "criterion_id": self.criterion_id,
            "decision": self.decision,
            "recorded_at": self.recorded_at,
        }
        if self.note is not None:
            out["note"] = self.note
        if self.actor is not None:
            out["actor"] = self.actor
        if self.supersedes is not None:
            out["supersedes"] = self.supersedes
        return out


def to_recorded_at(moment: datetime | None = None) -> str:
    """Canonical RFC 3339 UTC text for ``moment`` (default: now).

    A naive datetime is rejected: an unanchored wall clock is not a fact.
    An aware non-UTC datetime is normalized to UTC.
    """
    if moment is None:
        moment = datetime.now(UTC)
    if not isinstance(moment, datetime):
        raise HumanDecisionError("recorded_at source must be a datetime")
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise HumanDecisionError(
            "recorded_at source must be an aware datetime; naive input is rejected"
        )
    utc = moment.astimezone(UTC)
    text = utc.strftime("%Y-%m-%dT%H:%M:%S")
    if utc.microsecond:
        text += f".{utc.microsecond:06d}"
    return text + "Z"


def is_canonical_recorded_at(value: Any) -> bool:
    """True when ``value`` is canonical RFC 3339 UTC text naming a real instant.

    The shape check alone is not validation: it accepts month 99 and hour 99.
    Readers must never reparse or reformat the stored string, so this only
    *verifies* it — the value handed on is the original string, byte for byte.
    """
    if not isinstance(value, str) or not RECORDED_AT_RE.match(value):
        return False
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ" if "." in value else "%Y-%m-%dT%H:%M:%SZ"
    try:
        datetime.strptime(value, fmt).replace(tzinfo=UTC)
    except ValueError:
        return False
    return True


def decisions_path(run_dir: Path | str) -> Path:
    return Path(run_dir) / DECISIONS_FILENAME


def decisions_lock_path(run_dir: Path | str) -> Path:
    return Path(run_dir) / DECISIONS_LOCK_FILENAME


@contextmanager
def _decision_write_lock(run_dir: Path | str) -> Iterator[None]:
    """Serialize the append cycle across processes.

    Mirrors the fcntl.flock discipline core.observability.events
    already uses for cross-process appends: POSIX-only, and a no-op where
    fcntl is unavailable. On such a platform the validator still rejects
    every *observable* branch — the lock closes the narrow window where two
    writers read the same head concurrently, it is not the only defence.
    """
    path = decisions_lock_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX
        yield
        return
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run_identity(run_dir: Path | str) -> str:
    """The run id a decision written into ``run_dir`` must carry.

    ``meta.json``'s ``run_id`` is authoritative when readable (a cross-project
    child run lives under ``<parent>/<alias>/``, so its directory name is the
    alias, not the id); the directory name is the fallback. Used to reject a
    wrong-run write before it can land in the wrong audit log.
    """
    run_dir = Path(run_dir)
    meta_path = run_dir / "meta.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = None
        if isinstance(meta, Mapping):
            declared = meta.get("run_id")
            if isinstance(declared, str) and declared.strip():
                return declared.strip()
    return run_dir.name


def decidable_human_criterion(run_dir: Path | str, criterion_id: str) -> None:
    """Admit a decision only for a ``human`` criterion of this run's plan.

    A decision is auditable evidence about a *declared* criterion. Writing one
    for an id the accepted plan never declared, or for an ``executable`` /
    ``agent_assertion`` criterion, would fabricate proof for a criterion whose
    class forbids it (ADR 0188 §2), so both are rejected before write — as is a
    run with no accepted plan artifact to check against.
    """
    from pipeline.plan_artifacts import (
        LATEST_FILENAME,
        ParsedPlanArtifactError,
        load_parsed_plan_artifact,
    )

    run_dir = Path(run_dir)
    if not (run_dir / LATEST_FILENAME).is_file():
        raise HumanDecisionError(
            f"run {run_identity(run_dir)!r} has no accepted plan artifact; "
            "there is no criterion contract to decide against"
        )
    try:
        plan = load_parsed_plan_artifact(run_dir)
    except ParsedPlanArtifactError as e:
        raise HumanDecisionError(
            f"run {run_identity(run_dir)!r} has an unreadable plan artifact: {e}"
        ) from e

    known = {c.id: c for c in plan.acceptance_criteria}
    criterion = known.get(criterion_id)
    if criterion is None:
        raise HumanDecisionError(
            f"unknown criterion {criterion_id!r}; the accepted plan declares "
            f"{sorted(known)}"
        )
    if criterion.verify != "human":
        raise HumanDecisionError(
            f"criterion {criterion_id!r} is {criterion.verify!r}, not 'human'; "
            "only a human criterion is decided by an operator"
        )


# ── validation ───────────────────────────────────────────────────────────────


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HumanDecisionError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any, field: str) -> str | None:
    """Validate an optional field. ``None`` means *absent*, never ``null``."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HumanDecisionError(f"{field} must be a string when present")
    trimmed = value.strip()
    if not trimmed:
        raise HumanDecisionError(
            f"{field} must be non-empty when present; omit it instead"
        )
    return trimmed


def _decision_from_wire(value: Any, where: str) -> HumanDecision:
    if not isinstance(value, Mapping):
        raise HumanDecisionError(f"{where} must be an object")
    unknown = sorted(set(value) - _REQUIRED_KEYS - _OPTIONAL_KEYS)
    if unknown:
        raise HumanDecisionError(f"{where} has unknown keys: {unknown}")
    missing = sorted(_REQUIRED_KEYS - set(value))
    if missing:
        raise HumanDecisionError(f"{where} missing required keys: {missing}")
    for key in sorted(_OPTIONAL_KEYS):
        if key in value and value[key] is None:
            raise HumanDecisionError(
                f"{where}.{key} is null; an unused optional field must be absent"
            )
    decision = value["decision"]
    if decision not in DECISION_VALUES:
        raise HumanDecisionError(
            f"{where}.decision must be one of {list(DECISION_VALUES)}, "
            f"got {decision!r}"
        )
    recorded_at = value["recorded_at"]
    if not is_canonical_recorded_at(recorded_at):
        raise HumanDecisionError(
            f"{where}.recorded_at must be RFC 3339 UTC text naming a real "
            f"instant (YYYY-MM-DDTHH:MM:SS[.ffffff]Z), got {recorded_at!r}"
        )
    return HumanDecision(
        decision_id=_required_text(value["decision_id"], f"{where}.decision_id"),
        run_id=_required_text(value["run_id"], f"{where}.run_id"),
        criterion_id=_required_text(value["criterion_id"], f"{where}.criterion_id"),
        decision=decision,
        recorded_at=recorded_at,
        note=_optional_text(value.get("note"), f"{where}.note"),
        actor=_optional_text(value.get("actor"), f"{where}.actor"),
        supersedes=_optional_text(value.get("supersedes"), f"{where}.supersedes"),
    )


# ── reading ──────────────────────────────────────────────────────────────────


def load_human_decisions(run_dir: Path | str) -> tuple[HumanDecision, ...]:
    """Load the append-only decision log in durable write order.

    A missing artifact is an empty log. A malformed artifact raises — a
    decision record is auditable evidence and must never degrade silently.
    """
    path = decisions_path(run_dir)
    if not path.exists():
        return ()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise HumanDecisionError(f"{path} is not readable JSON: {e}") from e
    if not isinstance(raw, Mapping):
        raise HumanDecisionError(f"{path} must contain a JSON object")
    version = raw.get("schema_version")
    if version != DECISION_SCHEMA_VERSION:
        raise HumanDecisionError(
            f"{path} has unsupported schema_version {version!r}"
        )
    entries = raw.get("decisions")
    if not isinstance(entries, list):
        raise HumanDecisionError(f"{path}.decisions must be a list")
    records = tuple(
        _decision_from_wire(entry, f"decisions[{i}]")
        for i, entry in enumerate(entries)
    )
    _validate_journal(records, run_identity(run_dir))
    return records


def _validate_journal(
    records: Sequence[HumanDecision], expected_run_id: str,
) -> None:
    """Replay the append-only log and reject any malformed chain.

    Per-record schema validation is not enough: the chain invariant is a
    property of the *sequence*. Reading it back is where a hand-edited or
    partially-written artifact must be caught, so a resumed run can never feed
    a branched or dangling chain into the reducer (ADR 0188 §5).

    Validated in durable write order:

    * ``decision_id`` is unique across the whole run, not just per criterion;
    * every record carries this run's id;
    * the first record of a criterion omits ``supersedes``;
    * every later record names its criterion's *current* head exactly — which
      rules out a dangling id, a stale id, a branch, and a cross-criterion
      reference in one check.
    """
    seen_ids: set[str] = set()
    head_by_criterion: dict[str, str] = {}
    for i, record in enumerate(records):
        where = f"decisions[{i}]"
        if record.decision_id in seen_ids:
            raise HumanDecisionError(
                f"{where} repeats decision_id {record.decision_id!r}; "
                "decision ids are unique within a run"
            )
        seen_ids.add(record.decision_id)
        if record.run_id != expected_run_id:
            raise HumanDecisionError(
                f"{where} belongs to run {record.run_id!r}, not "
                f"{expected_run_id!r}"
            )
        head = head_by_criterion.get(record.criterion_id)
        if head is None:
            if record.supersedes is not None:
                raise HumanDecisionError(
                    f"{where} is the first decision for criterion "
                    f"{record.criterion_id!r} but names supersedes "
                    f"{record.supersedes!r}"
                )
        elif record.supersedes != head:
            raise HumanDecisionError(
                f"{where} must supersede the current head {head!r} of "
                f"criterion {record.criterion_id!r}, got {record.supersedes!r}"
            )
        head_by_criterion[record.criterion_id] = record.decision_id


def decision_chain(
    records: Sequence[HumanDecision], criterion_id: str,
) -> tuple[HumanDecision, ...]:
    """Every record for ``criterion_id`` in durable write order."""
    return tuple(r for r in records if r.criterion_id == criterion_id)


def decision_chain_head(
    records: Sequence[HumanDecision], criterion_id: str,
) -> HumanDecision | None:
    """The single validated head of a criterion's chain, or ``None``.

    Deterministic across reload/resume. ``records`` must come from
    :func:`load_human_decisions`, which has already replayed the chain: the
    head is then unambiguously the last record for the criterion in durable
    write order. The unreferenced-record scan below is a defence in depth for a
    directly-constructed sequence, and reports a branch rather than silently
    picking one side.
    """
    chain = decision_chain(records, criterion_id)
    if not chain:
        return None
    superseded = {r.supersedes for r in chain if r.supersedes}
    heads = [r for r in chain if r.decision_id not in superseded]
    if len(heads) != 1:
        raise HumanDecisionError(
            f"criterion {criterion_id!r} has {len(heads)} decision-chain heads; "
            "the artifact is branched"
        )
    if heads[0] is not chain[-1]:
        raise HumanDecisionError(
            f"criterion {criterion_id!r} has a chain head that is not its last "
            "durable record; the artifact is out of order"
        )
    return heads[0]


def human_decision_facts(
    run_dir: Path | str,
) -> dict[str, HumanDecisionFact]:
    """Reducer input: one validated head per criterion that has decisions."""
    records = load_human_decisions(run_dir)
    facts: dict[str, HumanDecisionFact] = {}
    for criterion_id in dict.fromkeys(r.criterion_id for r in records):
        head = decision_chain_head(records, criterion_id)
        if head is None:
            continue
        facts[criterion_id] = HumanDecisionFact(
            criterion_id=criterion_id,
            decision_id=head.decision_id,
            decision=head.decision,
        )
    return facts


# ── writing ──────────────────────────────────────────────────────────────────


def record_human_decision(
    run_dir: Path | str,
    *,
    run_id: str,
    criterion_id: str,
    decision: str,
    note: str | None = None,
    actor: str | None = None,
    supersedes: str | None = None,
    recorded_at: datetime | None = None,
) -> HumanDecision:
    """Append one validated decision. Every rejection happens before write.

    ``decision_id`` and ``recorded_at`` are writer-assigned; a caller cannot
    supply either. On any validation failure the artifact is left untouched
    byte-for-byte.

    Every admission check runs here, at the durable boundary, so the SDK, the
    CLI, and any direct writer share one gate: the run identity must match the
    target run dir (no cross-run write), and the criterion must be a ``human``
    criterion the accepted plan actually declares.
    """
    run_id = _required_text(run_id, "run_id")
    criterion_id = _required_text(criterion_id, "criterion_id")
    expected_run_id = run_identity(run_dir)
    if run_id != expected_run_id:
        raise HumanDecisionError(
            f"run_id {run_id!r} does not identify run {expected_run_id!r} at "
            f"{Path(run_dir)}; a decision is never written to another run's log"
        )
    if decision not in DECISION_VALUES:
        raise HumanDecisionError(
            f"decision must be one of {list(DECISION_VALUES)}, got {decision!r}"
        )
    decidable_human_criterion(run_dir, criterion_id)
    note = _optional_text(note, "note")
    actor = _optional_text(actor, "actor")
    supersedes = _optional_text(supersedes, "supersedes")
    stamp = to_recorded_at(recorded_at)

    # Everything from here reads the journal and then replaces it, so it runs
    # under the exclusive write lock. Splitting the read from the write would
    # let a second writer's rename discard the first writer's record.
    with _decision_write_lock(run_dir):
        return _append_locked(
            run_dir,
            run_id=run_id,
            criterion_id=criterion_id,
            decision=decision,
            note=note,
            actor=actor,
            supersedes=supersedes,
            stamp=stamp,
        )


def _append_locked(
    run_dir: Path | str,
    *,
    run_id: str,
    criterion_id: str,
    decision: str,
    note: str | None,
    actor: str | None,
    supersedes: str | None,
    stamp: str,
) -> HumanDecision:
    """The supersession check and the append, as one serialized step.

    Called only with the write lock held. Reading the head and writing the
    successor must not be separable: that gap is exactly where two concurrent
    replacements would both validate against the same head.
    """
    existing = load_human_decisions(run_dir)
    chain = decision_chain(existing, criterion_id)
    head = decision_chain_head(existing, criterion_id)

    if head is None:
        if supersedes is not None:
            raise HumanDecisionError(
                f"criterion {criterion_id!r} has no prior decision; "
                "the first decision must omit supersedes"
            )
    elif supersedes is None:
        raise HumanDecisionError(
            f"criterion {criterion_id!r} already has decision "
            f"{head.decision_id!r}; a replacement must name it in supersedes"
        )
    elif supersedes != head.decision_id:
        known = {r.decision_id for r in chain}
        if supersedes == head.supersedes:
            # The named record is the head's own predecessor: a second
            # decision against the same previous head would fork the chain.
            raise HumanDecisionError(
                f"branched supersession: {supersedes!r} was already superseded "
                f"by {head.decision_id!r} for criterion {criterion_id!r}"
            )
        if supersedes in known:
            raise HumanDecisionError(
                f"stale supersession: {supersedes!r} is not the current head "
                f"of criterion {criterion_id!r} ({head.decision_id!r})"
            )
        raise HumanDecisionError(
            f"supersedes {supersedes!r} is not a decision of criterion "
            f"{criterion_id!r}"
        )

    record = HumanDecision(
        decision_id=_next_decision_id(criterion_id, existing),
        run_id=run_id,
        criterion_id=criterion_id,
        decision=decision,  # type: ignore[arg-type]
        recorded_at=stamp,
        note=note,
        actor=actor,
        supersedes=supersedes,
    )
    _write_all(run_dir, (*existing, record))
    return record


def _next_decision_id(
    criterion_id: str, existing: Sequence[HumanDecision],
) -> str:
    """Stable, unique-within-run decision id: ``hd-<criterion>-<n>``."""
    taken = {r.decision_id for r in existing}
    n = sum(1 for r in existing if r.criterion_id == criterion_id) + 1
    while (candidate := f"hd-{criterion_id}-{n}") in taken:
        n += 1
    return candidate


def _write_all(run_dir: Path | str, records: Sequence[HumanDecision]) -> Path:
    path = decisions_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "schema_version": DECISION_SCHEMA_VERSION,
        "decisions": [r.to_dict() for r in records],
    }
    text = json.dumps(body, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".decisions-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path
