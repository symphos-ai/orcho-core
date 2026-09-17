# SPDX-License-Identifier: Apache-2.0
"""pipeline.criterion_claims — durable typed criterion claims (ADR 0188).

An ``agent_assertion`` criterion is proved by nobody: the strongest evidence it
can ever carry is *advisory*. This module owns the durable append-only record
of those typed claims so the link survives artifact write, resume, and reload
instead of living only in the composing process's memory.

A claim never manufactures an official passing receipt. The reducer turns a
linked claim into ``advisory`` and nothing stronger.

Reviewer findings that carry ``criterion_id`` are the second source of the same
proof kind; :func:`claims_from_findings` projects them without a second
artifact.
"""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pipeline.criterion_decisions import to_recorded_at
from pipeline.criterion_matrix import CriterionClaim

__all__ = [
    "CLAIMS_FILENAME",
    "record_finding_links",
    "CLAIM_KINDS",
    "CLAIM_SCHEMA_VERSION",
    "CriterionClaimError",
    "CriterionClaimRecord",
    "claims_from_findings",
    "claims_path",
    "load_criterion_claims",
    "record_criterion_claim",
    "reducer_claims",
]

CLAIMS_FILENAME = "criterion_claims.json"
CLAIM_SCHEMA_VERSION = "1"
CLAIM_KINDS: tuple[str, ...] = ("claim", "finding")

_REQUIRED_KEYS: frozenset[str] = frozenset(
    {"claim_id", "run_id", "criterion_id", "kind", "actor", "statement", "recorded_at"}
)


class CriterionClaimError(ValueError):
    """Raised for an invalid claim payload."""


@dataclass(frozen=True, slots=True)
class CriterionClaimRecord:
    """One durable typed claim linking an actor's inspection to a criterion."""

    claim_id: str
    run_id: str
    criterion_id: str
    kind: Literal["claim", "finding"]
    actor: str
    statement: str
    recorded_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "run_id": self.run_id,
            "criterion_id": self.criterion_id,
            "kind": self.kind,
            "actor": self.actor,
            "statement": self.statement,
            "recorded_at": self.recorded_at,
        }

    def as_reducer_input(self) -> CriterionClaim:
        return CriterionClaim(
            criterion_id=self.criterion_id,
            id=self.claim_id,
            kind=self.kind,
            executor=self.actor,
        )


def claims_path(run_dir: Path | str) -> Path:
    return Path(run_dir) / CLAIMS_FILENAME


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CriterionClaimError(f"{field} must be a non-empty string")
    return value.strip()


def _record_from_wire(value: Any, where: str) -> CriterionClaimRecord:
    if not isinstance(value, Mapping):
        raise CriterionClaimError(f"{where} must be an object")
    unknown = sorted(set(value) - _REQUIRED_KEYS)
    if unknown:
        raise CriterionClaimError(f"{where} has unknown keys: {unknown}")
    missing = sorted(_REQUIRED_KEYS - set(value))
    if missing:
        raise CriterionClaimError(f"{where} missing required keys: {missing}")
    kind = value["kind"]
    if kind not in CLAIM_KINDS:
        raise CriterionClaimError(
            f"{where}.kind must be one of {list(CLAIM_KINDS)}, got {kind!r}"
        )
    return CriterionClaimRecord(
        claim_id=_text(value["claim_id"], f"{where}.claim_id"),
        run_id=_text(value["run_id"], f"{where}.run_id"),
        criterion_id=_text(value["criterion_id"], f"{where}.criterion_id"),
        kind=kind,
        actor=_text(value["actor"], f"{where}.actor"),
        statement=_text(value["statement"], f"{where}.statement"),
        recorded_at=_text(value["recorded_at"], f"{where}.recorded_at"),
    )


def load_criterion_claims(run_dir: Path | str) -> tuple[CriterionClaimRecord, ...]:
    """Load the append-only claim log in durable write order."""
    path = claims_path(run_dir)
    if not path.exists():
        return ()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise CriterionClaimError(f"{path} is not readable JSON: {e}") from e
    if not isinstance(raw, Mapping):
        raise CriterionClaimError(f"{path} must contain a JSON object")
    if raw.get("schema_version") != CLAIM_SCHEMA_VERSION:
        raise CriterionClaimError(
            f"{path} has unsupported schema_version {raw.get('schema_version')!r}"
        )
    entries = raw.get("claims")
    if not isinstance(entries, list):
        raise CriterionClaimError(f"{path}.claims must be a list")
    return tuple(
        _record_from_wire(entry, f"claims[{i}]") for i, entry in enumerate(entries)
    )


def record_criterion_claim(
    run_dir: Path | str,
    *,
    run_id: str,
    criterion_id: str,
    actor: str,
    statement: str,
    kind: str = "claim",
    recorded_at: datetime | None = None,
) -> CriterionClaimRecord:
    """Append one typed claim. ``claim_id`` and ``recorded_at`` are writer-assigned."""
    if kind not in CLAIM_KINDS:
        raise CriterionClaimError(
            f"kind must be one of {list(CLAIM_KINDS)}, got {kind!r}"
        )
    existing = load_criterion_claims(run_dir)
    record = CriterionClaimRecord(
        claim_id=_next_claim_id(criterion_id, kind, existing),
        run_id=_text(run_id, "run_id"),
        criterion_id=_text(criterion_id, "criterion_id"),
        kind=kind,  # type: ignore[arg-type]
        actor=_text(actor, "actor"),
        statement=_text(statement, "statement"),
        recorded_at=to_recorded_at(recorded_at),
    )
    _write_all(run_dir, (*existing, record))
    return record


def _next_claim_id(
    criterion_id: str, kind: str, existing: Sequence[CriterionClaimRecord],
) -> str:
    taken = {r.claim_id for r in existing}
    prefix = "claim" if kind == "claim" else "finding"
    n = sum(1 for r in existing if r.criterion_id == criterion_id) + 1
    while (candidate := f"{prefix}-{criterion_id}-{n}") in taken:
        n += 1
    return candidate


def _write_all(
    run_dir: Path | str, records: Sequence[CriterionClaimRecord],
) -> Path:
    path = claims_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "schema_version": CLAIM_SCHEMA_VERSION,
        "claims": [r.to_dict() for r in records],
    }
    text = json.dumps(body, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".claims-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def claims_from_findings(findings: Iterable[Mapping[str, Any]]) -> list[CriterionClaim]:
    """Project reviewer findings that declare ``criterion_id`` into claim facts.

    Findings without a criterion link are skipped: a finding is only criterion
    evidence when the reviewer typed the link, never when a reader guesses it
    from prose.
    """
    out: list[CriterionClaim] = []
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        criterion_id = str(finding.get("criterion_id") or "").strip()
        finding_id = str(finding.get("id") or "").strip()
        if not criterion_id or not finding_id:
            continue
        out.append(
            CriterionClaim(
                criterion_id=criterion_id, id=finding_id, kind="finding",
            )
        )
    return out


def reducer_claims(
    run_dir: Path | str,
    *,
    findings: Iterable[Mapping[str, Any]] = (),
) -> list[CriterionClaim]:
    """All durable claim facts for a run: recorded claims plus linked findings.

    Deterministic order — durable claim-log order first, then finding order —
    and deduplicated on ``(kind, id)`` so a finding mirrored into the claim log
    is not counted twice.
    """
    facts = [r.as_reducer_input() for r in load_criterion_claims(run_dir)]
    seen = {(f.kind, f.id) for f in facts}
    for fact in claims_from_findings(findings):
        if (fact.kind, fact.id) in seen:
            continue
        seen.add((fact.kind, fact.id))
        facts.append(fact)
    return facts


def record_finding_links(
    run_dir: Path | str | None,
    *,
    run_id: str,
    findings: Iterable[Mapping[str, Any]],
    actor: str,
) -> list[CriterionClaimRecord]:
    """Mirror criterion-linked findings into the durable claim log.

    A reviewer finding already reaches the criterion matrix through the
    evidence bundle's ``findings`` rollup. Recording the link here as well
    keeps it readable independently of the session/meta projection that
    rollup depends on — session slots are compactable, this log is not.

    Idempotent and inert by default: a finding with no ``criterion_id`` is
    skipped, and a link already in the log is not appended again, so a phase
    that produces no criterion-linked finding writes no artifact at all.
    """
    if run_dir is None:
        return []
    linked = [
        (str(f.get("criterion_id") or "").strip(), str(f.get("id") or "").strip())
        for f in findings
        if isinstance(f, Mapping)
    ]
    linked = [(cid, fid) for cid, fid in linked if cid and fid]
    if not linked:
        return []

    existing = load_criterion_claims(run_dir)
    known = {(r.kind, r.claim_id) for r in existing}
    records = list(existing)
    written: list[CriterionClaimRecord] = []
    for criterion_id, finding_id in linked:
        if ("finding", finding_id) in known:
            continue
        record = CriterionClaimRecord(
            claim_id=finding_id,
            run_id=_text(run_id, "run_id"),
            criterion_id=criterion_id,
            kind="finding",
            actor=_text(actor, "actor"),
            statement=f"Reviewer finding {finding_id} is linked to {criterion_id}.",
            recorded_at=to_recorded_at(),
        )
        known.add(("finding", finding_id))
        records.append(record)
        written.append(record)
    if written:
        _write_all(run_dir, records)
    return written
