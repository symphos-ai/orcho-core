# SPDX-License-Identifier: Apache-2.0
"""pipeline.engine.delivery_ledger — durable delivery intent / fact record.

Why this exists (ADR 0191). A delivery commit is a side effect on the
operator's checkout, while the schema-validated audit artifact
(``commit_decisions/<id>.json``) is written *after* it. Anything that fails
inside that window — a schema refusal, a full disk, a SIGKILL — used to leave
a real commit with no durable trace in the run: status readers reported "no
delivery", the diagnosis recommended a plain resume, and a resume could try to
deliver the same diff again.

The ledger closes the window with two small writes around the commit:

* ``intent`` — written right before the first mutating git op of a commit
  (``git add``): the resolved action, the checkout the commit lands in, the
  base, the checkout's HEAD and branch *before* the commit, the message and
  the paths about to be staged;
* ``committed`` — written right after ``git commit`` succeeded, carrying the
  sha; ``recorded`` once the audit artifact was written too.

:func:`reconcile_delivery` is the read-only reader. It compares the ledger
with Git and says whether a delivery commit for this run already exists, so
``resolve_commit_delivery`` can adopt it (an idempotent resume, never a second
commit) and the diagnosis read-model can report the inconsistency with the
real sha instead of "not delivered". A commit created by an engine version
that kept no ledger is found by its deterministic fallback subject and
reported as ``legacy_commit`` — it is never adopted automatically, because
nothing durable says who decided it.

Only read-only git commands run here (``rev-parse``, ``symbolic-ref``,
``cat-file``, ``log``); the checkout is never mutated.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "LEDGER_DIRNAME",
    "LEDGER_SUFFIX",
    "RECON_COMMITTED_UNRECORDED",
    "RECON_COMMIT_MISSING",
    "RECON_INTENT_ONLY",
    "RECON_LEGACY_COMMIT",
    "RECON_NONE",
    "RECON_RECORDED",
    "RECON_UNREADABLE",
    "STAGE_COMMITTED",
    "STAGE_INTENT",
    "STAGE_RECORDED",
    "CommitFacts",
    "DeliveryLedgerError",
    "DeliveryLedgerRecord",
    "DeliveryReconciliation",
    "default_delivery_subject",
    "describe_commit",
    "ledger_path",
    "load_delivery_ledger",
    "reconcile_delivery",
    "record_delivery_audit",
    "record_delivery_commit",
    "record_delivery_intent",
    "safe_decision_id",
]

LEDGER_DIRNAME = "commit_decisions"
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")
LEDGER_SUFFIX = ".delivery.json"
LEDGER_SCHEMA_VERSION = "1"

STAGE_INTENT = "intent"
STAGE_COMMITTED = "committed"
STAGE_RECORDED = "recorded"
_STAGES: tuple[str, ...] = (STAGE_INTENT, STAGE_COMMITTED, STAGE_RECORDED)

#: Reconciliation states. ``found`` ones name an existing delivery commit.
RECON_NONE = "none"
RECON_INTENT_ONLY = "intent_only"
RECON_COMMITTED_UNRECORDED = "committed_unrecorded"
RECON_RECORDED = "recorded"
RECON_COMMIT_MISSING = "commit_missing"
RECON_LEGACY_COMMIT = "legacy_commit"
RECON_UNREADABLE = "unreadable"
_FOUND_STATES: frozenset[str] = frozenset({
    RECON_COMMITTED_UNRECORDED, RECON_RECORDED, RECON_LEGACY_COMMIT,
})

#: How many recent commits the legacy (no-ledger) probe inspects.
_LEGACY_PROBE_LIMIT = 200
_GIT_TIMEOUT_S = 30.0


class DeliveryLedgerError(ValueError):
    """A ledger record exists but cannot be trusted (malformed / unreadable)."""


def safe_decision_id(run_id: str) -> str:
    """The audit / ledger key for a run: its id sanitised to a file-safe token."""
    safe = _SAFE_ID_RE.sub("_", run_id).strip("._")
    return safe or "run"


def default_delivery_subject(run_id: str) -> str:
    """The deterministic commit subject used when no release summary exists.

    Shared with the commit executor so the legacy probe in
    :func:`reconcile_delivery` matches exactly what older engines wrote.
    """
    return f"chore: deliver orcho run {run_id}"


@dataclass(frozen=True, slots=True)
class DeliveryLedgerRecord:
    """One run's delivery intent and, once it exists, the commit fact."""

    run_id: str
    decision_id: str
    stage: str
    action: str
    commit_target: str
    baseline_ref: str
    message: str
    strategy: str | None
    staged_paths: tuple[str, ...]
    head_before: str | None
    branch_before: str | None
    intended_at: str
    delivery_branch: str | None = None
    commit_sha: str | None = None
    committed_at: str | None = None
    recorded_at: str | None = None
    # Who produced the record: empty for the engine's own resolve→apply path,
    # ``reconciled`` for a commit an operator recorded after the fact.
    provenance: str | None = None

    @property
    def subject(self) -> str:
        return self.message.splitlines()[0].strip() if self.message else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "run_id": self.run_id,
            "decision_id": self.decision_id,
            "stage": self.stage,
            "action": self.action,
            "commit_target": self.commit_target,
            "baseline_ref": self.baseline_ref,
            "message": self.message,
            "strategy": self.strategy,
            "staged_paths": list(self.staged_paths),
            "head_before": self.head_before,
            "branch_before": self.branch_before,
            "intended_at": self.intended_at,
            "delivery_branch": self.delivery_branch,
            "commit_sha": self.commit_sha,
            "committed_at": self.committed_at,
            "recorded_at": self.recorded_at,
            "provenance": self.provenance,
        }

    @classmethod
    def from_dict(cls, data: Any, *, where: str) -> DeliveryLedgerRecord:
        if not isinstance(data, dict):
            raise DeliveryLedgerError(f"{where}: ledger record must be a JSON object")
        if data.get("schema_version") != LEDGER_SCHEMA_VERSION:
            raise DeliveryLedgerError(
                f"{where}: unsupported schema_version {data.get('schema_version')!r}"
            )
        for key in ("run_id", "decision_id", "stage", "action", "commit_target",
                    "baseline_ref", "message", "intended_at"):
            value = data.get(key)
            if not isinstance(value, str) or (key != "message" and not value.strip()):
                raise DeliveryLedgerError(f"{where}: {key} must be a non-empty string")
        stage = str(data["stage"])
        if stage not in _STAGES:
            raise DeliveryLedgerError(f"{where}: stage must be one of {_STAGES}, got {stage!r}")
        paths = data.get("staged_paths")
        if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
            raise DeliveryLedgerError(f"{where}: staged_paths must be a list of strings")
        sha = data.get("commit_sha")
        if stage != STAGE_INTENT and (not isinstance(sha, str) or not sha.strip()):
            raise DeliveryLedgerError(
                f"{where}: commit_sha must be a non-empty string when stage={stage!r}"
            )
        return cls(
            run_id=str(data["run_id"]),
            decision_id=str(data["decision_id"]),
            stage=stage,
            action=str(data["action"]),
            commit_target=str(data["commit_target"]),
            baseline_ref=str(data["baseline_ref"]),
            message=str(data["message"]),
            strategy=_optional_str(data.get("strategy")),
            staged_paths=tuple(paths),
            head_before=_optional_str(data.get("head_before")),
            branch_before=_optional_str(data.get("branch_before")),
            intended_at=str(data["intended_at"]),
            delivery_branch=_optional_str(data.get("delivery_branch")),
            commit_sha=_optional_str(sha),
            committed_at=_optional_str(data.get("committed_at")),
            recorded_at=_optional_str(data.get("recorded_at")),
            provenance=_optional_str(data.get("provenance")),
        )


def ledger_path(run_dir: Path | str, decision_id: str) -> Path:
    return Path(run_dir) / LEDGER_DIRNAME / f"{decision_id}{LEDGER_SUFFIX}"


def load_delivery_ledger(
    run_dir: Path | str, decision_id: str,
) -> DeliveryLedgerRecord | None:
    """The run's ledger record, ``None`` when none was ever written.

    Raises :class:`DeliveryLedgerError` for a record that exists but cannot be
    read or does not match the schema — an unreadable fact must never read as
    "no delivery happened".
    """
    path = ledger_path(run_dir, decision_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DeliveryLedgerError(f"{path}: unreadable ledger record: {exc}") from exc
    return DeliveryLedgerRecord.from_dict(data, where=str(path))


def record_delivery_intent(
    run_dir: Path | str,
    *,
    run_id: str,
    decision_id: str,
    action: str,
    commit_target: Path | str,
    baseline_ref: str,
    message: str,
    strategy: str | None,
    staged_paths: tuple[str, ...],
    delivery_branch: str | None = None,
    provenance: str | None = None,
) -> DeliveryLedgerRecord:
    """Write the ``intent`` stage right before the first mutating git op."""
    target = Path(commit_target)
    record = DeliveryLedgerRecord(
        run_id=run_id,
        decision_id=decision_id,
        stage=STAGE_INTENT,
        action=action,
        commit_target=str(target),
        baseline_ref=baseline_ref,
        message=message,
        strategy=strategy,
        staged_paths=tuple(staged_paths),
        head_before=_git(target, ["rev-parse", "--verify", "-q", "HEAD"]),
        branch_before=_git(target, ["symbolic-ref", "--short", "-q", "HEAD"]),
        intended_at=_now(),
        delivery_branch=delivery_branch,
        provenance=provenance,
    )
    _write(ledger_path(run_dir, decision_id), record.to_dict())
    return record


def record_delivery_commit(
    run_dir: Path | str, record: DeliveryLedgerRecord, commit_sha: str,
) -> DeliveryLedgerRecord:
    """Advance to ``committed`` right after ``git commit`` succeeded."""
    updated = replace(
        record, stage=STAGE_COMMITTED, commit_sha=commit_sha, committed_at=_now(),
    )
    _write(ledger_path(run_dir, record.decision_id), updated.to_dict())
    return updated


def record_delivery_audit(
    run_dir: Path | str, record: DeliveryLedgerRecord,
) -> DeliveryLedgerRecord:
    """Advance to ``recorded`` once the audit artifact has been written."""
    updated = replace(record, stage=STAGE_RECORDED, recorded_at=_now())
    _write(ledger_path(run_dir, record.decision_id), updated.to_dict())
    return updated


@dataclass(frozen=True, slots=True)
class DeliveryReconciliation:
    """What Git and the ledger jointly say about this run's delivery commit."""

    state: str
    commit_sha: str | None = None
    detail: str = ""
    record: DeliveryLedgerRecord | None = None

    @property
    def found(self) -> bool:
        return self.state in _FOUND_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "commit_sha": self.commit_sha,
            "detail": self.detail,
            "ledger_stage": self.record.stage if self.record else None,
        }


def reconcile_delivery(
    run_dir: Path | str,
    *,
    run_id: str,
    decision_id: str,
    project_path: Path | str | None,
) -> DeliveryReconciliation:
    """Compare the ledger with Git, read-only.

    * ``recorded`` — the audit artifact was written; the ledger is complete.
    * ``committed_unrecorded`` — a commit exists but the audit was never
      written (the run stopped between the two); the sha is authoritative.
    * ``intent_only`` — the engine intended a commit and none can be found.
    * ``commit_missing`` — the ledger names a sha Git does not have.
    * ``legacy_commit`` — no ledger, but the target checkout carries a commit
      with the run's deterministic fallback subject.
    * ``unreadable`` — a ledger record exists but does not parse.
    * ``none`` — nothing to reconcile.
    """
    try:
        record = load_delivery_ledger(run_dir, decision_id)
    except DeliveryLedgerError as exc:
        return DeliveryReconciliation(RECON_UNREADABLE, detail=str(exc))

    if record is None:
        if project_path is None:
            return DeliveryReconciliation(RECON_NONE)
        sha = _find_commit_by_subject(Path(project_path), default_delivery_subject(run_id))
        if sha is None:
            return DeliveryReconciliation(RECON_NONE)
        return DeliveryReconciliation(
            RECON_LEGACY_COMMIT,
            commit_sha=sha,
            detail=(
                f"the target checkout carries delivery commit {sha[:12]} for run "
                f"{run_id}, but the run recorded no delivery (created before the "
                "delivery ledger existed)"
            ),
        )

    target = Path(record.commit_target)
    if record.stage == STAGE_RECORDED:
        return DeliveryReconciliation(
            RECON_RECORDED, commit_sha=record.commit_sha,
            detail="delivery commit and audit artifact are both recorded",
            record=record,
        )
    if record.stage == STAGE_COMMITTED:
        if _commit_exists(target, record.commit_sha or ""):
            return DeliveryReconciliation(
                RECON_COMMITTED_UNRECORDED, commit_sha=record.commit_sha,
                detail=(
                    f"delivery commit {(record.commit_sha or '')[:12]} exists in "
                    f"{target} but its audit artifact was never written"
                ),
                record=record,
            )
        return DeliveryReconciliation(
            RECON_COMMIT_MISSING, commit_sha=record.commit_sha,
            detail=(
                f"the ledger records delivery commit {(record.commit_sha or '')[:12]} "
                f"but {target} does not carry it"
            ),
            record=record,
        )
    # intent only: look for the commit the intent describes.
    sha = _find_commit_after(
        target, head_before=record.head_before, subject=record.subject,
    )
    if sha is not None:
        return DeliveryReconciliation(
            RECON_COMMITTED_UNRECORDED, commit_sha=sha,
            detail=(
                f"delivery commit {sha[:12]} matches the recorded intent "
                f"(parent {(record.head_before or '?')[:12]}, subject "
                f"{record.subject!r}) but neither the commit fact nor the audit "
                "artifact was written"
            ),
            record=record,
        )
    return DeliveryReconciliation(
        RECON_INTENT_ONLY,
        detail="a delivery commit was intended but none matching the intent exists",
        record=record,
    )


@dataclass(frozen=True, slots=True)
class CommitFacts:
    """Read-only facts about one commit, for an operator to verify and record."""

    sha: str
    parents: tuple[str, ...]
    message: str
    files: tuple[str, ...]
    author: str
    committed_at: str


def describe_commit(cwd: Path | str, sha: str) -> CommitFacts | None:
    """Full sha, parents, message, touched files and author of ``sha``, or ``None``."""
    target = Path(cwd)
    full = _git(target, ["rev-parse", "--verify", "-q", f"{sha}^{{commit}}"])
    if full is None:
        return None
    header = _git(target, ["log", "-1", "--format=%P%x1f%an <%ae>%x1f%cI", full]) or ""
    parts = header.split("\x1f")
    parents = tuple(p for p in (parts[0].split() if parts else ()) if p)
    author = parts[1] if len(parts) > 1 else ""
    committed_at = parts[2] if len(parts) > 2 else ""
    message = _git(target, ["log", "-1", "--format=%B", full]) or ""
    files_out = _git(target, ["show", "--format=", "--name-only", full]) or ""
    files = tuple(line.strip() for line in files_out.splitlines() if line.strip())
    return CommitFacts(
        sha=full, parents=parents, message=message, files=files,
        author=author, committed_at=committed_at,
    )


# ── read-only git ─────────────────────────────────────────────────────────────


def _git(cwd: Path, args: list[str]) -> str | None:
    if not cwd.is_dir():
        return None
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_S,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    return out or None


def _commit_exists(cwd: Path, sha: str) -> bool:
    if not sha:
        return False
    return _git(cwd, ["cat-file", "-e", f"{sha}^{{commit}}"]) is not None or (
        _git(cwd, ["rev-parse", "--verify", "-q", f"{sha}^{{commit}}"]) is not None
    )


def _log_rows(cwd: Path, args: list[str]) -> list[tuple[str, str, str]]:
    out = _git(cwd, ["log", "--format=%H%x1f%P%x1f%s", *args])
    rows: list[tuple[str, str, str]] = []
    for line in (out or "").splitlines():
        parts = line.split("\x1f")
        if len(parts) == 3:
            rows.append((parts[0].strip(), parts[1].strip(), parts[2].strip()))
    return rows


def _find_commit_after(
    cwd: Path, *, head_before: str | None, subject: str,
) -> str | None:
    """The commit whose parent is ``head_before`` and whose subject matches."""
    if not subject:
        return None
    if not head_before:
        return _find_commit_by_subject(cwd, subject)
    for sha, parents, line_subject in _log_rows(cwd, [f"{head_before}..HEAD"]):
        if head_before in parents.split() and line_subject == subject:
            return sha
    return None


def _find_commit_by_subject(
    cwd: Path, subject: str, *, limit: int = _LEGACY_PROBE_LIMIT,
) -> str | None:
    """Newest commit on HEAD's first-parent line with exactly this subject."""
    if not subject:
        return None
    for sha, _parents, line_subject in _log_rows(cwd, ["-n", str(limit), "--first-parent"]):
        if line_subject == subject:
            return sha
    return None


# ── small helpers ─────────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _optional_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)
