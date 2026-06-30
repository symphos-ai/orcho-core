# SPDX-License-Identifier: Apache-2.0
"""Retained-subject worktree continuity for checkpoint-resume.

A checkpoint-resume of a run that was paused after ``review_changes``
rejected its change must pick up the **same** physical worktree that holds
the rejected diff. Otherwise ``repair_changes`` would run against a clean
HEAD and silently lose the subject under review — the review-retry incident
shape, where the resumed run dir name does not even match the original
``wt_<id>`` worktree, so the resolver's ``wt_<run_id>`` reuse branch cannot
find it.

This module reads the **prior** persistent ``meta.worktree`` block (written
by the original run, before session-init overwrites ``meta.json``) and
classifies how a checkpoint-resume should pick up its worktree:

* **(a) no retained subject** — the prior block is absent or records
  ``isolation=off``. Returns ``None``; the resolver keeps its current
  behaviour unchanged.
* **(b) retained subject available** — the block records an isolated
  worktree whose path exists and is registered with the source repo's
  ``git worktree list``. The exact recorded path is reused for **any**
  checkpoint-resume (handed to the resolver as the retained subject), even
  when the run-dir name differs from the recorded ``wt_<id>``.
* **(c) retained subject unavailable** — the block records an isolated
  worktree but the path is missing or unregistered. When an active
  review-retry depends on that diff (an active ``review_changes`` handoff or
  a recorded ``retry_feedback`` decision for one), the resume stops with a
  recoverable operator error naming the missing path — never materialising a
  clean checkout. Without an active review-retry, generic resume keeps the
  resolver's current behaviour (returns ``None``).

The classification performs no write of its own; it only reads ``meta.json``,
the ``phase_handoff_decisions/`` artifacts, and the source repo's worktree
list. The chosen decision is persisted into the session worktree block by the
caller (``isolation_setup``) as an additive ``resume_continuity`` sub-block
for inspectability.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from pipeline.engine.worktree import registered_worktree_exists

_REVIEW_PHASE = "review_changes"
_DECISIONS_DIRNAME = "phase_handoff_decisions"
_META_FILENAME = "meta.json"


@dataclasses.dataclass(frozen=True)
class ResumeWorktreeDecision:
    """Resolved retained-subject decision for a checkpoint-resume.

    ``retained_subject`` is the prior ``meta.worktree`` dict to hand the
    worktree resolver (branch b), or ``None`` otherwise. ``blocked`` is True
    only for branch (c) under an active review-retry: the caller persists the
    decision and stops with a recoverable error before any checkout is
    materialised.
    """

    mode_label: str
    source: str
    path: str | None
    retained_subject: dict[str, Any] | None
    blocked: bool
    block_message: str | None
    # The full prior ``meta.worktree`` block, carried so the blocked branch
    # can restore the subject (path / isolation / base_ref) that session-init
    # dropped — keeping the run decidable and re-resumable after recovery.
    prior_worktree: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Additive ``resume_continuity`` view for the session worktree block."""
        return {
            "mode_label": self.mode_label,
            "path": self.path,
            "source": self.source,
        }


def _read_meta(run_dir: Path) -> dict[str, Any]:
    """Read ``meta.json`` tolerantly; return ``{}`` when absent/malformed."""
    meta_file = run_dir / _META_FILENAME
    if not meta_file.is_file():
        return {}
    try:
        data = json.loads(meta_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_decisions(run_dir: Path) -> list[dict[str, Any]]:
    """Read ``{action, handoff_id}`` per decision artifact, tolerantly.

    A missing directory or an unreadable / malformed file is skipped — a
    single bad artifact never breaks the scan. Mirrors the tolerant readers
    in :mod:`pipeline.run_state.consistency`.
    """
    decisions_dir = run_dir / _DECISIONS_DIRNAME
    if not decisions_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for entry in sorted(decisions_dir.iterdir()):
        if not (entry.is_file() and entry.suffix == ".json"):
            continue
        try:
            raw = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        out.append(
            {"action": raw.get("action"), "handoff_id": raw.get("handoff_id")}
        )
    return out


def detect_review_retry_active(
    *,
    prior_meta: dict[str, Any],
    decisions: list[dict[str, Any]],
) -> bool:
    """True when the resumed run carries an active ``review_changes`` retry.

    Two independent signals, either of which is sufficient:

    * an active ``meta.phase_handoff`` whose ``phase`` is ``review_changes``
      (the run paused awaiting a review decision), or
    * a recorded ``retry_feedback`` decision for a ``review_changes`` handoff
      (the operator already chose to retry; ``handoff_id`` is
      ``"review_changes:<key>:<round>"``, so it prefixes the phase name).
    """
    active = prior_meta.get("phase_handoff")
    if isinstance(active, dict) and active.get("phase") == _REVIEW_PHASE:
        return True
    for decision in decisions:
        if decision.get("action") != "retry_feedback":
            continue
        handoff_id = decision.get("handoff_id")
        if isinstance(handoff_id, str) and handoff_id.startswith(
            f"{_REVIEW_PHASE}:",
        ):
            return True
    return False


def _string_value(mapping: dict[str, Any], key: str) -> str | None:
    value = mapping.get(key)
    return value if isinstance(value, str) and value.strip() else None


def classify_resume_worktree(
    *,
    prior_worktree: dict[str, Any] | None,
    review_retry_active: bool,
    project_dir: Path,
) -> ResumeWorktreeDecision | None:
    """Classify how a checkpoint-resume should pick up its worktree.

    Returns ``None`` for the passthrough classes (a / generic-resume c),
    where the resolver keeps its current behaviour. Returns a reuse decision
    when the recorded isolated worktree is available (b), or a blocked
    decision when it is unavailable under an active review-retry (c).
    """
    # (a) no retained subject: no prior block, or isolation was off.
    if not isinstance(prior_worktree, dict):
        return None
    if prior_worktree.get("isolation") == "off":
        return None

    path = _string_value(prior_worktree, "path")
    available = path is not None and registered_worktree_exists(
        project_dir=project_dir, path=Path(path),
    )

    # (b) recorded isolated worktree is present + registered -> reuse it.
    if available:
        return ResumeWorktreeDecision(
            mode_label=f"retained retry subject {path}",
            source="meta.worktree",
            path=path,
            retained_subject=dict(prior_worktree),
            blocked=False,
            block_message=None,
        )

    # (c) recorded worktree is missing / unregistered.
    if review_retry_active:
        block_message = (
            "Cannot resume repair_changes: this run is in a review retry and "
            "needs the retained rejected diff subject, but its recorded "
            f"worktree {path!r} is missing or is not registered with the "
            "source repo's worktree list. Refusing to materialise a clean "
            "checkout and silently lose the rejected diff. Recover by "
            "restoring the retained worktree (e.g. `git worktree add "
            f"{path} <branch>`) or halt the run."
        )
        return ResumeWorktreeDecision(
            mode_label="blocked: retained retry subject unavailable",
            source="meta.worktree",
            path=path,
            retained_subject=None,
            blocked=True,
            block_message=block_message,
            prior_worktree=dict(prior_worktree),
        )

    # Generic checkpoint-resume without an active review-retry: keep the
    # resolver's current behaviour (reuse wt_<run_id> if present, else a
    # fresh checkout). No new error, no retained subject.
    return None


def resolve_resume_worktree(
    *,
    resume_from: str | None,
    output_dir: Path | None,
    project_dir: Path,
) -> ResumeWorktreeDecision | None:
    """Read the prior ``meta.worktree`` + retry signals and classify.

    Called from ``resolve_isolation_inputs`` strictly **before** session-init
    overwrites ``meta.json``. Returns ``None`` when this is not a checkpoint
    resume, when there is no prior worktree block, or for the passthrough
    classes — leaving the resolver's behaviour unchanged.
    """
    if resume_from is None or output_dir is None:
        return None
    run_dir = Path(output_dir)
    prior_meta = _read_meta(run_dir)
    prior_worktree = prior_meta.get("worktree")
    if not isinstance(prior_worktree, dict):
        return None
    decisions = _read_decisions(run_dir)
    review_retry_active = detect_review_retry_active(
        prior_meta=prior_meta, decisions=decisions,
    )
    return classify_resume_worktree(
        prior_worktree=prior_worktree,
        review_retry_active=review_retry_active,
        project_dir=Path(project_dir),
    )


__all__ = [
    "ResumeWorktreeDecision",
    "classify_resume_worktree",
    "detect_review_retry_active",
    "resolve_resume_worktree",
]
