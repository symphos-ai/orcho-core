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
  worktree but the path is missing or unregistered. When an active retry
  depends on that exact tree, the resume stops with a recoverable operator
  error naming the missing path — never materialising a clean checkout.
  Without such a retry, generic resume keeps the resolver's current
  behaviour (returns ``None``).

Two independent retries need the retained tree, for the same reason stated
differently: a **review** retry (an active ``review_changes`` handoff or a
recorded ``retry_feedback`` decision for one) needs the rejected diff, and an
**env** retry (``retry_verification``, see
:mod:`pipeline.project.verification_env_retry`) needs the exact subject the
failing verification receipts observed. A fresh checkout would silently lose
the diff in the first case and re-measure a different tree in the second, so
both block here rather than letting the resolver mint one.

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

from pipeline.engine.worktree import is_worktree_reclaimed, registered_worktree_exists
from pipeline.project.verification_env_retry import (
    is_env_retry_decision_candidate,
)

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
    # True only when it was the *env* retry that needed the missing tree. It
    # selects the serialized shape below; every other classification keeps the
    # persisted record it has always written.
    env_retry: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Additive ``resume_continuity`` view for the session worktree block.

        A blocked env retry is the one case that adds a key. The block is the
        whole outcome there — no checkout was materialised, and the operator
        has to restore a path before the run can move — so it must be readable
        without string-matching the operator-facing ``mode_label``. Every other
        classification, blocked review retry included, keeps the exact
        three-key record it has always persisted: this sub-block is durable
        run state, and widening its shape for paths that gained nothing would
        change what existing readers of those runs see.
        """
        view = {
            "mode_label": self.mode_label,
            "path": self.path,
            "source": self.source,
        }
        if self.blocked and self.env_retry:
            view["blocked"] = True
        return view


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
    """Read ``{action, handoff_id, artifact_stem}`` per artifact, tolerantly.

    A missing directory or an unreadable / malformed file is skipped — a
    single bad artifact never breaks the scan. Mirrors the tolerant readers
    in :mod:`pipeline.run_state.consistency`. ``artifact_stem`` is the
    filename the strict reader addresses by handoff id, so a consumer can
    recognise a decision whose *persisted* id is corrupted.
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
        out.append({
            "action": raw.get("action"),
            "handoff_id": raw.get("handoff_id"),
            "artifact_stem": entry.stem,
        })
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


def detect_env_retry_active(
    *,
    prior_meta: dict[str, Any],
    decisions: list[dict[str, Any]],
) -> bool:
    """True when the resumed run carries an active ``retry_verification``.

    Delegates the whole predicate to
    :func:`pipeline.project.verification_env_retry.is_env_retry_decision_candidate`,
    the owner of what an env retry *is* — this module only supplies the two
    facts it reads (the prior active payload and the tolerantly-parsed
    decisions). Unlike a review retry, one half alone is never enough: a
    verification pause with no recorded decision is an ordinary pause whose
    subject the resolver may legitimately re-derive.

    The *candidate* predicate, deliberately: a decision whose persisted ids are
    corrupted is still a claim that this subject was to be re-measured, and it
    is refused far later than this guard runs. Holding the retained tree for a
    record that turns out to be unusable costs a re-park; releasing it costs
    the subject itself.
    """
    active = prior_meta.get("phase_handoff")
    if not isinstance(active, dict):
        return False
    return is_env_retry_decision_candidate(active, decisions)


def _string_value(mapping: dict[str, Any], key: str) -> str | None:
    value = mapping.get(key)
    return value if isinstance(value, str) and value.strip() else None


def classify_resume_worktree(
    *,
    prior_worktree: dict[str, Any] | None,
    review_retry_active: bool,
    env_retry_active: bool = False,
    project_dir: Path,
) -> ResumeWorktreeDecision | None:
    """Classify how a checkpoint-resume should pick up its worktree.

    Returns ``None`` for the passthrough classes (a / generic-resume c),
    where the resolver keeps its current behaviour. Returns a reuse decision
    when the recorded isolated worktree is available (b), or a blocked
    decision when it is unavailable and either retry needs it (c). The two
    retry flags decide the same branch and differ only in the operator
    message: they name different evidence to restore.
    """
    # (a) no retained subject: no prior block, or isolation was off.
    if not isinstance(prior_worktree, dict):
        return None
    if prior_worktree.get("isolation") == "off":
        return None
    if is_worktree_reclaimed(prior_worktree):
        return ResumeWorktreeDecision(
            mode_label="blocked: retained retry subject reclaimed",
            source="meta.worktree",
            path=None,
            retained_subject=None,
            blocked=True,
            block_message=(
                "Cannot resume in place: the retained worktree was reclaimed. "
                "Its recorded path is historical; restore the archive explicitly "
                "or begin a new recovery run."
            ),
            prior_worktree=dict(prior_worktree),
            env_retry=env_retry_active,
        )

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
    if review_retry_active or env_retry_active:
        if review_retry_active:
            block_message = (
                "Cannot resume repair_changes: this run is in a review retry "
                "and needs the retained rejected diff subject, but its "
                f"recorded worktree {path!r} is missing or is not registered "
                "with the source repo's worktree list. Refusing to "
                "materialise a clean checkout and silently lose the rejected "
                "diff. Recover by restoring the retained worktree (e.g. `git "
                f"worktree add {path} <branch>`) or halt the run."
            )
        else:
            block_message = (
                "Cannot re-run the failed verification gates: this run is in "
                "a retry_verification retry and must re-measure the exact "
                "subject the failing receipts observed, but its recorded "
                f"verification worktree {path!r} is missing or is not "
                "registered with the source repo's worktree list. Refusing "
                "to materialise a clean checkout and re-measure a different "
                "tree. Recover by restoring the retained worktree (e.g. `git "
                f"worktree add {path} <branch>`) or halt the run."
            )
        return ResumeWorktreeDecision(
            mode_label="blocked: retained retry subject unavailable",
            source="meta.worktree",
            path=path,
            retained_subject=None,
            blocked=True,
            block_message=block_message,
            prior_worktree=dict(prior_worktree),
            env_retry=env_retry_active,
        )

    # Generic checkpoint-resume with no retry depending on the retained tree:
    # keep the resolver's current behaviour (reuse wt_<run_id> if present,
    # else a fresh checkout). No new error, no retained subject.
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
    return classify_resume_worktree(
        prior_worktree=prior_worktree,
        review_retry_active=detect_review_retry_active(
            prior_meta=prior_meta, decisions=decisions,
        ),
        env_retry_active=detect_env_retry_active(
            prior_meta=prior_meta, decisions=decisions,
        ),
        project_dir=Path(project_dir),
    )


__all__ = [
    "ResumeWorktreeDecision",
    "classify_resume_worktree",
    "detect_env_retry_active",
    "detect_review_retry_active",
    "resolve_resume_worktree",
]
