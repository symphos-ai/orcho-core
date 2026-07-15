"""Post-release commit delivery executor (ADR 0032 / ADR 0043).

Phase B1 is deliberately single-project and release-summary only: it
transports a run-owned diff from the run checkout into the project
checkout, includes run-owned untracked files, then either commits it,
leaves it uncommitted, skips delivery, or halts the run at the delivery
gate.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from core.contracts.commit_decision_schema import validate_decision_dict
from core.io.ansi import C, paint
from core.io.git_helpers import apply_patch_to_checkout, worktree_diff_against_base
from core.io.journey_prompt import (
    bold,
    default_chip,
    divider,
    help_line,
    is_color_active,
    title,
)
from core.io.terminal_input import stdio_interactive
from pipeline.engine import delivery_branch as _delivery_branch
from pipeline.engine.delivery_branch import (
    DeliveryBranchOutcome,
    DeliveryPrIntent,
    checkout_delivery_branch,
    publish_delivery_branch,
    resolve_delivery_branch,
)
from pipeline.engine.delivery_publish import publish_delivery
from pipeline.engine.run_diff import resolve_git_root
from pipeline.run_state.release_verdict import is_release_blocked

if TYPE_CHECKING:
    from pipeline.verification_delivery import DeliveryVerificationAssessment

CommitDeliveryAction = Literal[
    "none", "fix", "approve", "apply", "skip", "halt",
]
CommitDeliveryStatus = Literal[
    "disabled",
    "not_applicable",
    "no_diff",
    "pending",
    "fix_requested",
    "committed",
    "applied_uncommitted",
    "skipped",
    "halted",
    "commit_failed",
    "apply_failed",
    "target_dirty",
    # Stage 6 delivery verification gate (ADR 0083): non-interactive
    # ``require``-policy block on missing/failed/stale receipts or generated
    # garbage. action='none', returned by resolve_commit_delivery BEFORE any
    # transport git op; never written through _persist's schema-validated
    # artifact.
    "verification_blocked",
]
CommitMessageGenerator = Callable[["CommitDeliveryDecision"], str | None]

# Single source of truth mapping a halted commit-delivery ``status`` to its
# recoverable ``halt_reason``. Both the live engine path
# (``pipeline/project/run.py``) and the SDK decision path
# (``sdk/run_control/delivery.py``) consume this map; callers that need a
# default for an unmapped (non-halting) status use ``commit_delivery_failed``,
# matching the historical executor-failure fallback. ``target_dirty`` (ADR
# 0032 / B1.2) stays distinct from operator-chosen ``halted`` so resume /
# dashboards can route straight to the cleanup decision.
COMMIT_DELIVERY_HALT_REASONS: dict[str, str] = {
    "halted": "commit_decision_halt",
    "fix_requested": "commit_decision_fix",
    "target_dirty": "commit_delivery_target_dirty",
    "commit_failed": "commit_delivery_failed",
    "apply_failed": "commit_delivery_failed",
    "verification_blocked": "commit_delivery_verification_blocked",
}

_ACTIONS = frozenset({"fix", "approve", "apply", "skip", "halt"})
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_PYTHON_BYTECODE_SUFFIXES = (".pyc", ".pyo")


@dataclass(frozen=True, slots=True)
class CommitDeliveryDecision:
    """Resolved post-release delivery decision and executor outcome."""

    action: CommitDeliveryAction
    status: CommitDeliveryStatus
    run_id: str
    decision_id: str
    project_path: Path
    source_path: Path
    baseline_ref: str
    dirty: bool = False
    release_summary: str = ""
    release_verdict: str = ""
    release_blockers: tuple[dict[str, Any], ...] = ()
    patch_text: str = ""
    changed_paths: tuple[str, ...] = ()
    untracked_paths: tuple[str, ...] = ()
    untracked_delivered: tuple[str, ...] = ()
    files_staged: tuple[str, ...] = ()
    include_untracked: bool = False
    commit_message_strategy: str | None = None
    final_message: str | None = None
    commit_sha: str | None = None
    error: str | None = None
    decided_at: str | None = None
    artifact_path: Path | None = None
    target_dirty_paths: tuple[str, ...] = ()
    target_dirty_retries: int = 0
    # Stage 6 verification gate (ADR 0083) — additive awareness. Empty unless a
    # delivery assessment was supplied; surfaced via to_dict only when non-empty
    # so the no-contract / gate=None path stays byte-identical.
    verification_policy: str = ""
    verification_missing: tuple[str, ...] = ()
    verification_failed: tuple[str, ...] = ()
    verification_stale: tuple[str, ...] = ()
    generated_garbage_paths: tuple[str, ...] = ()
    # Stage C delivery-scope enforcement (T4) — additive awareness. All empty
    # unless the run recorded a delivery scope (auto-detect cross recommendation
    # mapped to a scope), so the no-scope / explicit-mono path stays
    # byte-identical. ``scope_blocker`` is set only for a strict-mono violation;
    # ``scope_disclosure`` lists the per-alias sibling files (``[alias]/rel``).
    delivery_scope: str = ""
    scope_blocker: str = ""
    scope_disclosure: tuple[str, ...] = ()
    scope_projects: tuple[str, ...] = ()
    # T1 companion-repo disclosure — additive per-repo enrichment derived from
    # the durable plan scope. Each entry is a ``CompanionRepo`` carrying alias,
    # path, changed paths, and typed ``dirty|committed|planned_requirement``
    # state. Empty for a clean single-repo mono run, so the no-companion path
    # stays byte-identical. Serialised (never the patch text) via ``to_dict``.
    scope_companions: tuple[Any, ...] = ()
    # ADR 0119 delivery-branch policy — additive awareness. ``delivery_branch``
    # is the published/publishable branch (``orcho/deliver/…`` or a named /
    # feature branch); ``pr_intent`` the provider-neutral PR record. Both stay
    # empty for the ``bypass`` opt-out and the pre-ADR-0119 no-op path, and are
    # serialised via ``to_dict`` only when non-empty so that path stays
    # byte-identical. The single commit site resolves them through
    # :mod:`pipeline.engine.delivery_branch`.
    delivery_branch: str | None = None
    pr_intent: DeliveryPrIntent | None = None
    # ADR 0119/0121 — typed machine-readable twin of the human-readable
    # 'PR opened: {url}' line in ``delivery_notices``. Both are derived from the
    # same :attr:`PublishResult.pr_url` at the single publish site
    # (:func:`_deliver_published_branch`); ``None`` whenever no PR was opened
    # (no provider, gh absent, offline, apply-draft, bypass, push-without-PR).
    pr_url: str | None = None
    # ADR 0119 — non-fatal delivery-branch diagnostics (rebase conflict,
    # offline/no-remote degrade). Persisted onto the decision (and to_dict) so an
    # operator sees them; empty on the no-op / commit-in-place paths, so those
    # stay byte-identical.
    delivery_warnings: tuple[str, ...] = ()
    delivery_notices: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "action": self.action,
            "status": self.status,
            "run_id": self.run_id,
            "decision_id": self.decision_id,
            "project_path": str(self.project_path),
            "source_path": str(self.source_path),
            "baseline_ref": self.baseline_ref,
            "dirty": self.dirty,
        }
        if self.release_summary:
            out["release_summary"] = self.release_summary
        if self.release_verdict:
            out["release_verdict"] = self.release_verdict
        if self.release_blockers:
            out["release_blockers"] = [dict(blocker) for blocker in self.release_blockers]
        if self.changed_paths:
            out["changed_paths"] = list(self.changed_paths)
        if self.untracked_paths:
            out["untracked_paths"] = list(self.untracked_paths)
        if self.untracked_delivered:
            out["untracked_delivered"] = list(self.untracked_delivered)
        if self.files_staged:
            out["files_staged"] = list(self.files_staged)
        if self.target_dirty_paths:
            out["target_dirty_paths"] = list(self.target_dirty_paths)
        if self.target_dirty_retries:
            out["target_dirty_retries"] = self.target_dirty_retries
        out["include_untracked"] = self.include_untracked
        if self.final_message:
            out["final_message"] = self.final_message
        if self.commit_message_strategy:
            out["strategy"] = self.commit_message_strategy
        if self.commit_sha:
            out["commit_sha"] = self.commit_sha
        if self.error:
            out["error"] = self.error
        if self.decided_at:
            out["decided_at"] = self.decided_at
        if self.artifact_path:
            out["artifact_path"] = str(self.artifact_path)
        if self.verification_policy:
            out["verification_policy"] = self.verification_policy
        if self.verification_missing:
            out["verification_missing"] = list(self.verification_missing)
        if self.verification_failed:
            out["verification_failed"] = list(self.verification_failed)
        if self.verification_stale:
            out["verification_stale"] = list(self.verification_stale)
        if self.generated_garbage_paths:
            out["generated_garbage_paths"] = list(self.generated_garbage_paths)
        # Delivery-scope awareness (T4): present only when non-empty so the
        # no-scope path stays byte-identical.
        if self.delivery_scope:
            out["delivery_scope"] = self.delivery_scope
        if self.scope_blocker:
            out["scope_blocker"] = self.scope_blocker
        if self.scope_disclosure:
            out["scope_disclosure"] = list(self.scope_disclosure)
        if self.scope_projects:
            out["scope_projects"] = list(self.scope_projects)
        # T1 companion enrichment: durable per-repo disclosure, present only when
        # non-empty. Each ``CompanionRepo`` serialises to alias/path/state/changed
        # paths — never patch text (the patch_text invariant is preserved).
        if self.scope_companions:
            out["scope_companions"] = [c.to_dict() for c in self.scope_companions]
        # ADR 0119 delivery-branch awareness: present only when non-empty so the
        # bypass / no-op path stays byte-identical.
        if self.delivery_branch:
            out["delivery_branch"] = self.delivery_branch
        if self.pr_intent is not None:
            out["pr_intent"] = self.pr_intent.to_dict()
        # SDK-parity: the typed twin of the 'PR opened' notice is always keyed
        # (value when a PR was opened, ``None`` otherwise) so downstream
        # projections can read it without re-parsing ``delivery_notices``.
        out["pr_url"] = self.pr_url
        if self.delivery_warnings:
            out["delivery_warnings"] = list(self.delivery_warnings)
        if self.delivery_notices:
            out["delivery_notices"] = list(self.delivery_notices)
        return out


def resolve_commit_delivery(
    *,
    project_dir: Path,
    source_worktree: Path,
    run_dir: Path | None,
    run_id: str,
    session: Mapping[str, Any],
    commit_config: Mapping[str, Any] | None,
    no_interactive: bool,
    baseline_ref: str = "HEAD",
    commit_message_generator: CommitMessageGenerator | None = None,
    verification_gate: DeliveryVerificationAssessment | None = None,
    decision_action: CommitDeliveryAction | None = None,
    decision_mode: str = "auto",
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> CommitDeliveryDecision:
    """Resolve whether and how to deliver a finished run's diff.

    ``verification_gate`` is the Stage 6 delivery assessment built upstream
    (:func:`pipeline.verification_delivery.assess_delivery_verification`). The
    engine only CONSUMES it — it never resolves the contract or reads git for
    verification itself. ``None`` (no contract / policy ``off``) keeps the path
    byte-identical to prior behavior: no extra output, no verification keys on
    the decision. Otherwise the effective policy drives a hint (``suggest``), a
    warning (``warn``), or — for ``require`` with blockers — a hard
    ``verification_blocked`` in non-interactive mode (returned before any
    transport git op) / a fix-defaulted correction prompt interactively. The
    independent ``release_blocked`` gate keeps priority over this one.

    ``decision_action`` (ADR 0100) injects an operator-chosen action into a
    NON-interactive resolve: when set it replaces the config ``auto_in_ci``
    default as the resolved action, so an out-of-band delivery decision (the
    SDK ``decide_delivery`` executor re-resolving a parked gate) drives the
    same engine path the auto/interactive flows use. The hard guards stay in
    force: a non-APPROVED release still refuses ``approve`` / ``apply`` (those
    actions return ``not_applicable``), while ``fix`` / ``skip`` / ``halt``
    remain expressible. ``decision_action`` is ignored in interactive mode (the
    prompt owns the choice) and on the auto path (``None``).

    ``decision_mode`` (ADR 0100) is the provider-neutral parking switch. The
    default ``"auto"`` keeps the historical behavior byte-identical. ``"defer"``
    — only meaningful for a non-interactive run — parks the delivery decision:
    instead of auto-approving (or silently dropping a rejected release), it
    returns a ``pending`` decision carrying the full persistent context so the
    caller can hold the run at a recoverable delivery/correction gate and let an
    operator decide later through ``decide_delivery``.
    """
    # Anchor every downstream git op (apply / add / reset / rev-parse) and
    # untracked-file copy on the project's *git root*, not the registered
    # project dir. They diverge when ``git_dir`` is nested (e.g. a Unity
    # project under SVN with the C# repo at ``Assets/_Match-Three-Common``):
    # the run-owned patch is generated relative to the git root (the diff
    # worktree), so applying it from the SVN root fails with "No such file
    # or directory". ``decision.project_path`` is consumed only by git ops +
    # display here, so re-anchoring it once propagates to ``apply_commit_delivery``.
    project_dir = resolve_git_root(project_dir) or project_dir
    decision_id = _safe_decision_id(run_id)
    cfg = dict(commit_config or {})
    release = _release_entry(session)
    # ``release is None`` means the profile has no ``final_acceptance``
    # phase (e.g. ``lite``: plan → implement only). Caller already gates
    # entry on ``session["status"] == "done"``, so an absent verdict is
    # implicit approval — the profile simply chose not to release-gate
    # delivery — and must not be conflated with REJECTED.
    profile_gates_release = release is not None
    release_dict: Mapping[str, Any] = release or {}
    release_verdict = str(release_dict.get("verdict") or "")
    release_summary = str(release_dict.get("short_summary") or "").strip()
    release_blockers = _release_blockers(release_dict)
    # Defense-in-depth: a rejected release always authors a non-empty
    # ``short_summary`` per the release schema, but if a record ever reaches
    # here without one, fold the release blockers into ``release_summary`` so
    # the persisted decision still has a human-readable headline in addition to
    # the structured ``release_blockers`` list.
    if not release_summary:
        blocker_summary = _release_blocker_summary(release_dict)
        if blocker_summary:
            release_summary = blocker_summary
    release_fields = {
        "release_summary": release_summary,
        "release_verdict": release_verdict,
        "release_blockers": release_blockers,
    }

    if cfg.get("enabled") is False:
        return CommitDeliveryDecision(
            action="none",
            status="disabled",
            run_id=run_id,
            decision_id=decision_id,
            project_path=project_dir,
            source_path=source_worktree,
            baseline_ref=baseline_ref,
            **release_fields,
        )
    if run_dir is None:
        return CommitDeliveryDecision(
            action="none",
            status="not_applicable",
            run_id=run_id,
            decision_id=decision_id,
            project_path=project_dir,
            source_path=source_worktree,
            baseline_ref=baseline_ref,
            error="no run directory",
            **release_fields,
        )
    interactive = (not no_interactive) and stdio_interactive()
    # ADR 0100: ``defer`` parks the decision for a later operator call. It is
    # only meaningful for a non-interactive run; an interactive resolve always
    # owns the choice through the prompt below.
    defer = decision_mode == "defer" and not interactive
    # A non-APPROVED release verdict (REJECTED, or any non-approval) blocks
    # *automatic* delivery — but an operator at a TTY must still be offered the
    # delivery dialog so they can choose to deliver the diff anyway (it is their
    # checkout and their call). So: hard-block only when non-interactive (CI /
    # piped), where silently delivering a rejected change would be unsafe; in
    # interactive mode fall through to the prompt, which warns about the
    # rejection and defaults to ``skip`` so a bare Enter never delivers.
    #
    # Two exceptions to the non-interactive hard block (ADR 0100): ``defer``
    # parks the rejected run as a correction gate instead of dropping it, and an
    # injected ``decision_action`` of ``fix`` / ``skip`` / ``halt`` is an
    # operator choice that does not ship the diff, so it is honoured. Only the
    # auto default and an injected ``approve`` / ``apply`` are refused here.
    release_blocked = profile_gates_release and is_release_blocked(
        release_verdict, empty_blocks=True,
    )
    if (
        release_blocked
        and not interactive
        and not defer
        and decision_action in (None, "approve", "apply")
    ):
        return CommitDeliveryDecision(
            action="none",
            status="not_applicable",
            run_id=run_id,
            decision_id=decision_id,
            project_path=project_dir,
            source_path=source_worktree,
            baseline_ref=baseline_ref,
            error="release verdict is not APPROVED",
            **release_fields,
        )

    # Stage 6 verification delivery gate (ADR 0083). These fields are empty when
    # ``verification_gate is None`` (no contract / policy off), so everything
    # below is a no-op on that path and the decision carries no verification
    # keys. ``require`` + blockers in non-interactive mode refuses delivery here
    # — BEFORE the diff is even resolved (so a clean worktree cannot downgrade
    # the block to ``no_diff``) and before any transport git op (transport lives
    # in apply_commit_delivery, which is a no-op for a non-'pending' status).
    # ``release_blocked`` already returned for the non-interactive case above,
    # so it keeps priority.
    v_fields = _verification_decision_fields(verification_gate)
    if (
        verification_gate is not None
        and verification_gate.blocking  # policy == 'require' AND has_blockers
        and not interactive
        and not defer
        and decision_action in (None, "approve", "apply")
    ):
        return CommitDeliveryDecision(
            action="none",
            status="verification_blocked",
            run_id=run_id,
            decision_id=decision_id,
            project_path=project_dir,
            source_path=source_worktree,
            baseline_ref=baseline_ref,
            error=_verification_block_error(verification_gate),
            **v_fields,
            **release_fields,
        )

    patch_text = _run_owned_patch(source_worktree, baseline_ref)
    untracked_paths = _untracked_paths(source_worktree)
    # ``add_untracked=False`` means untracked never reaches the project
    # checkout. Combined with path-scoped staging in ``apply_commit_delivery``,
    # treating untracked as deliverable here would otherwise route a pure-
    # untracked run into ``commit_failed`` ("no run-owned paths to stage").
    deliverable_untracked = (
        untracked_paths if cfg.get("add_untracked", True) else ()
    )
    if (
        (not patch_text.strip() or patch_text == "(no diff)")
        and not deliverable_untracked
    ):
        return CommitDeliveryDecision(
            action="none",
            status="no_diff",
            run_id=run_id,
            decision_id=decision_id,
            project_path=project_dir,
            source_path=source_worktree,
            baseline_ref=baseline_ref,
            **release_fields,
        )
    if patch_text == "(diff unavailable)":
        return CommitDeliveryDecision(
            action="none",
            status="not_applicable",
            run_id=run_id,
            decision_id=decision_id,
            project_path=project_dir,
            source_path=source_worktree,
            baseline_ref=baseline_ref,
            error="run diff unavailable",
            **release_fields,
        )

    changed_paths = _dedupe_changed_against_untracked(
        _changed_paths(source_worktree, baseline_ref), untracked_paths,
    )

    # Stage C delivery-scope enforcement (T4) — thin hook. ``evaluate_delivery_scope``
    # returns ``None`` (no-op, behaviour unchanged) for any run that recorded no
    # delivery scope. The multi-repo collection + classification live entirely in
    # ``pipeline.engine.delivery_scope``; this body only consumes the result.
    from pipeline.engine.delivery_scope import evaluate_delivery_scope

    scope_assessment = evaluate_delivery_scope(
        session=session, primary_project_dir=project_dir, run_dir=run_dir,
    )
    scope_fields = _scope_decision_fields(scope_assessment)
    if scope_assessment is not None and scope_assessment.blocked:
        # Strict-mono violation: park a reversible, decidable gate carrying the
        # per-alias sibling disclosure. ``action='none'`` + ``status='pending'``
        # makes it decidable through ``delivery_decision_state`` /
        # ``decide_delivery``; ``apply_commit_delivery`` refuses to ship it
        # (the scope_blocker guard). The project checkout is never touched.
        return CommitDeliveryDecision(
            action="none",
            status="pending",
            run_id=run_id,
            decision_id=decision_id,
            project_path=project_dir,
            source_path=source_worktree,
            baseline_ref=baseline_ref,
            dirty=True,
            patch_text=patch_text,
            changed_paths=changed_paths,
            untracked_paths=untracked_paths,
            include_untracked=bool(cfg.get("add_untracked", True)),
            decided_at=datetime.now(UTC).isoformat(),
            **v_fields,
            **scope_fields,
            **release_fields,
        )

    if defer:
        # ADR 0100 — park the non-interactive decision with full persistent
        # context. ``action='none'`` + ``status='pending'`` marks an undecided
        # gate the caller holds the run at; ``apply_commit_delivery`` must NOT
        # be called on this decision (its action is unresolved). The diff
        # context (source_path / baseline_ref / changed_paths / untracked_paths)
        # is what ``decide_delivery`` later replays to reconstruct the patch.
        return CommitDeliveryDecision(
            action="none",
            status="pending",
            run_id=run_id,
            decision_id=decision_id,
            project_path=project_dir,
            source_path=source_worktree,
            baseline_ref=baseline_ref,
            dirty=True,
            patch_text=patch_text,
            changed_paths=changed_paths,
            untracked_paths=untracked_paths,
            include_untracked=bool(cfg.get("add_untracked", True)),
            decided_at=datetime.now(UTC).isoformat(),
            **v_fields,
            **scope_fields,
            **release_fields,
        )

    if (
        verification_gate is not None
        and not interactive
        and not verification_gate.blocking
        and (
            verification_gate.has_blockers
            or verification_gate.operator_gaps
            or verification_gate.waived_gates
        )
    ):
        # Non-interactive, non-blocking gaps: surface the warn/suggest receipts
        # (and any staged garbage), plus manual/operator-only commands as
        # not-auto-run advisory — but let delivery continue. Gated on the per-gate
        # partition (``not blocking`` with warn/suggest/garbage or manual-only),
        # not the boundary policy, so a warn/suggest gate under a ``require``
        # boundary is still surfaced as 'shipping allowed by policy' and a
        # manual-only-only gap is still visible. A require-blocking gap already
        # returned ``verification_blocked`` above, so it never reaches here.
        # ``waived_gates`` keeps a precisely-waived required gate visible too: it
        # no longer blocks (T2 removed it from blocking), but the terminal must
        # still show it was excused by a durable operator waiver.
        _log_verification_warning(verification_gate)

    default_action = (
        str(cfg.get("interactive_default", cfg.get("auto_in_ci", "approve")))
        if interactive
        else str(cfg.get("auto_in_ci", "approve"))
    )
    if default_action not in _ACTIONS:
        default_action = "approve"
    commit_message_strategy = _commit_message_strategy(cfg)
    # When acceptance rejected, route the operator to correction first: the safe
    # default is ``fix`` so a bare Enter never delivers or counts the run done.
    verification_correction = (
        verification_gate is not None and verification_gate.blocking
    )
    if release_blocked:
        default_action = "fix"
    elif verification_correction:
        # Interactive require-block (non-interactive already returned above):
        # mirror release_blocked — default to fix so a bare Enter never delivers.
        default_action = "fix"
    if interactive:
        # Read-only: classify where an ``approve`` would land so the menu tells
        # the truth. ``apply_commit_delivery`` re-resolves the policy for the real
        # transport, so this never changes delivery behavior.
        destination = _menu_destination(
            source_worktree=source_worktree,
            project_dir=project_dir,
            run_id=run_id,
            baseline_ref=baseline_ref,
            cfg=cfg,
            release_summary=release_summary,
        )
        action = _prompt_action(
            default_action=default_action,
            release_summary=release_summary,
            changed_paths=changed_paths,
            untracked_paths=untracked_paths,
            input_fn=input_fn,
            output_fn=output_fn,
            release_blocked=release_blocked,
            release_verdict=release_verdict,
            verification_correction=verification_correction,
            verification_warning_lines=_verification_prompt_warning_lines(
                verification_gate,
            ),
            verification_suggest_hint=_verification_suggest_hint(verification_gate),
            verification_garbage=_verification_prompt_garbage(verification_gate),
            verification_operator=_verification_prompt_operator(
                verification_gate,
            ),
            destination=destination,
        )
    elif decision_action is not None:
        # ADR 0100 — operator choice injected out of band (decide_delivery).
        # The release/verification hard guards above already refused an
        # injected approve/apply on a blocked run, so any approve/apply that
        # reaches here is permitted.
        action = decision_action
    else:
        action = default_action

    decision = CommitDeliveryDecision(
        action=action,  # type: ignore[arg-type]
        status="pending",
        run_id=run_id,
        decision_id=decision_id,
        project_path=project_dir,
        source_path=source_worktree,
        baseline_ref=baseline_ref,
        dirty=True,
        patch_text=patch_text,
        changed_paths=changed_paths,
        untracked_paths=untracked_paths,
        include_untracked=bool(cfg.get("add_untracked", True))
        if action in {"approve", "apply"}
        else False,
        commit_message_strategy=commit_message_strategy
        if action == "approve"
        else None,
        decided_at=datetime.now(UTC).isoformat(),
        **v_fields,
        **scope_fields,
        **release_fields,
    )
    if action == "approve":
        # On a publish-outward delivery (a PR will be opened) force
        # content_language authorship of the outward commit message even when
        # ``default_strategy`` is not ``llm_generate`` — the operator language
        # must never leak into a public commit / PR (ADR 0121). The bypass /
        # publish-off local-commit path keeps its configured strategy verbatim.
        force_llm = _will_open_pr(cfg) and commit_message_generator is not None
        final_message, actual_strategy = _resolve_final_commit_message(
            decision,
            configured_strategy=commit_message_strategy,
            generator=commit_message_generator,
            force_llm=force_llm,
        )
        decision = replace(
            decision,
            final_message=final_message,
            commit_message_strategy=actual_strategy,
        )
    return decision


def apply_commit_delivery(
    decision: CommitDeliveryDecision,
    *,
    run_dir: Path,
    commit_config: Mapping[str, Any] | None = None,
    no_interactive: bool = True,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> CommitDeliveryDecision:
    """Execute a resolved delivery decision and persist its audit artifact.

    The target-dirty guard fires here, right before any write to
    ``project_path`` — minimising the resolve→apply race window. The
    interactive prompt is gated by both ``no_interactive=False`` AND
    ``stdio_interactive()`` so MCP / background runs without a TTY
    cannot hang on ``input()``.
    """
    if decision.status != "pending":
        return decision

    if decision.scope_blocker:
        # Strict delivery-scope violation (T4): a reversible, decidable gate.
        # Never ship — leave it parked (status stays 'pending', action 'none')
        # so the operator resolves it through ``decide_delivery`` (skip / halt)
        # or re-runs with an expanded scope. The project checkout is untouched.
        return decision

    if decision.action == "fix":
        return _persist(decision, run_dir=run_dir, status="fix_requested")
    if decision.action == "skip":
        return _persist(decision, run_dir=run_dir, status="skipped")
    if decision.action == "halt":
        return _persist(decision, run_dir=run_dir, status="halted")

    interactive = (not no_interactive) and stdio_interactive()
    in_place_delivery = _same_checkout(decision.source_path, decision.project_path)

    # ADR 0119 — resolve the delivery branch policy for an auto/approve commit
    # (the interactive ``apply`` draft makes no branch decision). This is the
    # single point that decides where the commit lands; the policy table,
    # default-branch detection, rebase and PR-intent all live in
    # ``pipeline.engine.delivery_branch``. A ``worktree_branch`` publish never
    # writes the canonical checkout, so it diverges here — before the
    # target-dirty guard that protects the checkout it will not touch.
    delivery_outcome: DeliveryBranchOutcome | None = None
    if decision.action == "approve":
        delivery_outcome = _resolve_delivery_branch_outcome(decision, commit_config)
        if delivery_outcome.plan == "publish":
            return _deliver_published_branch(
                decision, delivery_outcome,
                run_dir=run_dir, commit_config=commit_config,
            )

    retries = 0
    while True:
        dirty = _target_dirty_paths(decision.project_path)
        if in_place_delivery:
            dirty = _parallel_dirty_paths_for_in_place_delivery(dirty, decision)
        if not dirty:
            break
        if not interactive:
            return _persist(
                decision,
                run_dir=run_dir,
                status="target_dirty",
                target_dirty_paths=dirty,
                target_dirty_retries=retries,
            )
        choice = _prompt_target_dirty(
            dirty_paths=dirty,
            retries=retries,
            input_fn=input_fn,
            output_fn=output_fn,
        )
        if choice == "retry":
            retries += 1
            continue
        if choice == "skip":
            return _persist(
                decision,
                run_dir=run_dir,
                action="skip",
                status="skipped",
                target_dirty_paths=dirty,
                target_dirty_retries=retries,
            )
        # choice == "halt"
        return _persist(
            decision,
            run_dir=run_dir,
            action="halt",
            status="halted",
            target_dirty_paths=dirty,
            target_dirty_retries=retries,
        )

    # Post-retry: if the operator advanced project_path's HEAD while
    # the gate was paused (e.g. ``git commit`` of their own changes to
    # clear the dirty block), the cached patch — generated against the
    # original baseline_ref — no longer applies. Re-anchor it against
    # the current HEAD before transport so an honest retry actually
    # delivers instead of silently failing in ``git apply --check``.
    if retries > 0:
        rebased = _rebase_patch_against_current_project_head(decision)
        if rebased is not None:
            decision = rebased
            if (
                (not decision.patch_text.strip()
                 or decision.patch_text == "(no diff)")
                and not decision.untracked_paths
            ):
                status_no_op: CommitDeliveryStatus = (
                    "commit_failed"
                    if decision.action == "approve"
                    else "apply_failed"
                )
                return _persist(
                    decision,
                    run_dir=run_dir,
                    status=status_no_op,
                    error=(
                        "operator commit already contains run-owned "
                        "changes; nothing remaining to deliver"
                    ),
                    target_dirty_retries=retries,
                )

    transported = _transport_patch(decision)
    if transported is not None:
        status: CommitDeliveryStatus = (
            "commit_failed" if decision.action == "approve" else "apply_failed"
        )
        return _persist(
            decision,
            run_dir=run_dir,
            status=status,
            error=transported,
            target_dirty_retries=retries,
        )
    untracked = _transport_untracked(decision)
    in_place_untracked = (
        decision.untracked_paths
        if in_place_delivery and decision.include_untracked
        else ()
    )
    if untracked.error:
        status = "commit_failed" if decision.action == "approve" else "apply_failed"
        return _persist(
            decision,
            run_dir=run_dir,
            status=status,
            error=untracked.error,
            untracked_delivered=untracked.delivered,
            target_dirty_retries=retries,
        )

    if decision.action == "apply":
        return _persist(
            decision,
            run_dir=run_dir,
            status="applied_uncommitted",
            untracked_delivered=untracked.delivered or in_place_untracked,
            target_dirty_retries=retries,
        )

    # ADR 0119 — a ``protect_default`` (in-place HEAD=default) or ``named``
    # delivery commits onto a dedicated branch, not the current HEAD: switch to
    # it (creating it off the resolved default branch / PR base if absent, not
    # the checkout's current tip) before staging so the commit never lands on
    # the default branch and the delivery branch is anchored to the base
    # delivery point. ``commit_in_place`` (in-place feature branch / bypass)
    # keeps the current HEAD.
    if delivery_outcome is not None and delivery_outcome.plan == "commit_on_branch":
        checkout_err = checkout_delivery_branch(
            decision.project_path,
            delivery_outcome.commit_branch or "",
            base_ref=delivery_outcome.base_ref,
        )
        if checkout_err is not None:
            return _persist(
                decision,
                run_dir=run_dir,
                status="commit_failed",
                error=(
                    f"could not switch to delivery branch "
                    f"{delivery_outcome.commit_branch!r}: {checkout_err}"
                ),
                target_dirty_retries=retries,
            )

    # Path-scoped staging — only run-owned paths reach the index.
    # Blanket ``git add -A`` would silently fold in any unrelated
    # parallel work that appeared between the guard's clean check and
    # this point (same-millisecond race). ``add_untracked`` config is
    # not consulted here — it already gated ``include_untracked``
    # upstream, so ``untracked.delivered`` is ``()`` when the operator
    # disabled untracked transport.
    staged_untracked = untracked.delivered or in_place_untracked
    stage_paths = list(decision.changed_paths) + list(staged_untracked)
    if not stage_paths:
        # Defensive — ``resolve_commit_delivery`` already surfaced
        # ``no_diff`` for empty-tracked + empty-deliverable-untracked.
        return _persist(
            decision,
            run_dir=run_dir,
            status="commit_failed",
            error="no run-owned paths to stage",
            target_dirty_retries=retries,
        )
    add = _run_git(decision.project_path, ["add", "--", *stage_paths])
    if not add.ok:
        return _persist(
            decision,
            run_dir=run_dir,
            status="commit_failed",
            error=add.error,
            target_dirty_retries=retries,
        )
    files_staged = _git_lines(decision.project_path, ["diff", "--cached", "--name-only"])
    commit = _run_git(
        decision.project_path,
        ["commit", "-s", "-m", decision.final_message or _message_from_release_summary("", decision.run_id)],
    )
    if not commit.ok:
        reset = _run_git(decision.project_path, ["reset"])
        error = commit.error
        if not reset.ok:
            error = f"{error}; failed to unstage checkout after commit failure: {reset.error}"
        return _persist(
            decision,
            run_dir=run_dir,
            status="commit_failed",
            error=error,
            files_staged=tuple(files_staged),
            untracked_delivered=staged_untracked,
            target_dirty_retries=retries,
        )
    sha = (_git_stdout(decision.project_path, ["rev-parse", "HEAD"]) or "").strip()
    return _persist(
        decision,
        run_dir=run_dir,
        status="committed",
        commit_sha=sha,
        files_staged=tuple(files_staged),
        untracked_delivered=staged_untracked,
        target_dirty_retries=retries,
        **_delivery_branch_persist_fields(delivery_outcome),
    )


def _resolve_delivery_branch_outcome(
    decision: CommitDeliveryDecision,
    commit_config: Mapping[str, Any] | None,
) -> DeliveryBranchOutcome:
    """Resolve the ADR 0119 delivery-branch outcome for this decision.

    Thin adapter over :func:`pipeline.engine.delivery_branch.resolve_delivery_branch`
    — it only reads ``branch_policy`` / ``branch_name`` off the commit config and
    forwards the decision's isolation facts. A commit config that carries no
    ``branch_policy`` (an un-migrated embedder or an ad-hoc caller) is forwarded
    as ``None`` and :func:`normalize_branch_policy` selects the ADR 0119 default
    ``worktree_branch`` — a missing key must never silently weaken the
    default-branch invariant into ``bypass``. ``bypass`` is reachable only by an
    explicit opt-out. No policy table lives here.
    """
    cfg = dict(commit_config or {})
    return resolve_delivery_branch(
        source_path=decision.source_path,
        project_path=decision.project_path,
        run_id=decision.run_id,
        base_ref=decision.baseline_ref,
        branch_policy=cfg.get("branch_policy"),
        named_branch=cfg.get("branch_name"),
        release_summary=decision.release_summary,
        commit_message=decision.final_message,
    )


def _delivery_branch_persist_fields(
    outcome: DeliveryBranchOutcome | None,
) -> dict[str, Any]:
    """Delivery-branch fields to thread into a committed ``_persist`` call.

    Empty dict for ``bypass`` / no-outcome, so the persisted decision stays
    byte-identical to the pre-ADR-0119 path.
    """
    if outcome is None or outcome.delivery_branch is None:
        return {}
    return {
        "delivery_branch": outcome.delivery_branch,
        "pr_intent": outcome.pr_intent,
        "delivery_warnings": outcome.warnings,
        "delivery_notices": outcome.notices,
    }


def _deliver_published_branch(
    decision: CommitDeliveryDecision,
    outcome: DeliveryBranchOutcome,
    *,
    run_dir: Path,
    commit_config: Mapping[str, Any] | None = None,
) -> CommitDeliveryDecision:
    """Execute a ``worktree_branch`` publish (ADR 0119 + ADR 0121).

    Commits the run's own work onto its branch inside the disposable run
    worktree, then publishes (rebase onto fresh default) via
    :func:`publish_delivery_branch`. The canonical checkout is never touched, so
    the decision surfaces ``commit_sha=None`` + ``delivery_branch``; the durable
    audit artifact records the real branch commit (``artifact_commit_sha``) to
    satisfy its schema. Non-fatal rebase/offline diagnostics ride along on
    ``delivery_warnings`` / ``delivery_notices``.

    ADR 0121: this is the single point that hands the published branch to a
    registered git-provider plugin via :func:`publish_delivery` (push +
    pull-request over the already-signed commit). An opened ``pr_url`` folds
    into ``delivery_notices`` as a string — no new wire field — and any publish
    warning folds into ``delivery_warnings``. A disabled gate, missing provider,
    or provider failure degrades to a "branch ready" notice / warning and the
    delivery status stays ``committed``.
    """
    branch_sha, error = _commit_run_branch(decision)
    if error is not None:
        return _persist(
            decision,
            run_dir=run_dir,
            status="commit_failed",
            error=error,
        )
    published = publish_delivery_branch(
        source_path=decision.source_path,
        project_path=decision.project_path,
        outcome=outcome,
    )
    result = publish_delivery(
        published.pr_intent,
        branch=published.delivery_branch,
        cwd=decision.source_path,
        commit_config=commit_config,
    )
    if result.pr_url:
        delivery_notices = published.notices + (f"PR opened: {result.pr_url}",)
    else:
        delivery_notices = published.notices + (
            f"delivery branch {published.delivery_branch} is ready; "
            "open a pull request or push it manually",
        )
    return _persist(
        decision,
        run_dir=run_dir,
        status="committed",
        commit_sha=None,
        artifact_commit_sha=branch_sha,
        delivery_branch=published.delivery_branch,
        pr_intent=published.pr_intent,
        # Same ``result.pr_url`` that shaped the 'PR opened' notice above;
        # ``None`` when no PR was opened. Never re-parsed from notices.
        pr_url=result.pr_url,
        delivery_warnings=published.warnings + result.warnings,
        delivery_notices=delivery_notices,
    )


def _commit_run_branch(
    decision: CommitDeliveryDecision,
) -> tuple[str | None, str | None]:
    """Stage + commit the run's own work onto its branch in the run worktree.

    Returns ``(commit_sha, None)`` on success, or ``(None, error)``. The run's
    changes are otherwise uncommitted working-tree diffs; this materialises them
    as the delivery commit so ``publish_delivery_branch`` has something to rebase
    and publish. Runs entirely inside ``decision.source_path`` (the run
    worktree) — never the canonical checkout.
    """
    source = decision.source_path
    stage_paths = list(decision.changed_paths)
    if decision.include_untracked:
        stage_paths += list(decision.untracked_paths)
    if stage_paths:
        add = _run_git(source, ["add", "--", *stage_paths])
        if not add.ok:
            return None, add.error
    staged = _run_git(source, ["diff", "--cached", "--name-only"])
    if staged.ok and staged.stdout.strip():
        message = decision.final_message or _message_from_release_summary(
            decision.release_summary, decision.run_id,
        )
        commit = _run_git(source, ["commit", "-s", "-m", message])
        if not commit.ok:
            _run_git(source, ["reset"])
            return None, commit.error
    sha = (_git_stdout(source, ["rev-parse", "HEAD"]) or "").strip()
    if not sha:
        return None, "could not resolve delivery branch HEAD after commit"
    return sha, None


def _run_owned_patch(source_worktree: Path, baseline_ref: str) -> str:
    return worktree_diff_against_base(source_worktree, base_ref=baseline_ref)


def _run_owned_patch_for_paths(
    source_worktree: Path,
    *,
    base_ref: str,
    paths: tuple[str, ...],
) -> str:
    safe_paths = tuple(path for path in paths if _safe_relative_path(path))
    if not safe_paths:
        return "(no diff)"
    result = _run_git(source_worktree, ["diff", base_ref, "--", *safe_paths])
    if not result.ok:
        return "(diff unavailable)"
    return result.stdout if result.stdout.strip() else "(no diff)"


def _rebase_patch_against_current_project_head(
    decision: CommitDeliveryDecision,
) -> CommitDeliveryDecision | None:
    """Recompute patch_text/changed_paths against project_dir's current HEAD.

    Used after the dirty-guard retry loop clears: the operator may have
    committed work in ``project_path`` between resolve-time and now, so
    ``decision.patch_text`` (captured against the original
    ``baseline_ref`` — typically the pre-run ``seed_tree_sha``) no
    longer applies cleanly. The worktree shares a git repo with the
    project, so the new commit is reachable from ``source_path`` and
    ``git diff <new_head>`` gives a patch whose context matches what
    ``project_path`` actually contains now.

    Returns a frozen decision with refreshed ``patch_text``,
    ``changed_paths``, and ``baseline_ref``, or ``None`` if HEAD could
    not be read or the new patch is unavailable — caller falls back to
    the original decision in that case.
    """
    new_head = _git_stdout(decision.project_path, ["rev-parse", "HEAD"])
    if not new_head or not new_head.strip():
        return None
    new_head = new_head.strip()
    new_patch = _run_owned_patch_for_paths(
        decision.source_path,
        base_ref=new_head,
        paths=decision.changed_paths,
    )
    if new_patch == "(diff unavailable)":
        return None
    new_changed_paths = _dedupe_changed_against_untracked(
        _changed_paths_for_delivery(
            decision.source_path, new_head, decision.changed_paths,
        ),
        decision.untracked_paths,
    )
    return replace(
        decision,
        patch_text=new_patch,
        changed_paths=new_changed_paths,
        baseline_ref=new_head,
    )


def _transport_patch(decision: CommitDeliveryDecision) -> str | None:
    if (
        _same_checkout(decision.source_path, decision.project_path)
        or not decision.patch_text.strip()
        or decision.patch_text == "(no diff)"
    ):
        return None
    check = apply_patch_to_checkout(
        decision.project_path, decision.patch_text, check_only=True,
    )
    if not check.ok:
        return check.error or "patch did not apply"
    applied = apply_patch_to_checkout(decision.project_path, decision.patch_text)
    if not applied.ok:
        return applied.error or "patch apply failed"
    return None


@dataclass(frozen=True, slots=True)
class _UntrackedTransport:
    delivered: tuple[str, ...] = ()
    error: str | None = None


def _transport_untracked(decision: CommitDeliveryDecision) -> _UntrackedTransport:
    if (
        not decision.include_untracked
        or not decision.untracked_paths
        or _same_checkout(decision.source_path, decision.project_path)
    ):
        return _UntrackedTransport()

    delivered: list[str] = []
    for rel in decision.untracked_paths:
        if not _safe_relative_path(rel):
            return _UntrackedTransport(
                delivered=tuple(delivered),
                error=f"unsafe untracked path {rel!r}",
            )
        src = decision.source_path / rel
        dst = decision.project_path / rel
        if not src.is_file():
            return _UntrackedTransport(
                delivered=tuple(delivered),
                error=f"untracked source missing: {rel}",
            )
        if dst.exists():
            return _UntrackedTransport(
                delivered=tuple(delivered),
                error=f"untracked destination already exists: {rel}",
            )
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        delivered.append(rel)
    return _UntrackedTransport(delivered=tuple(delivered))


def _persist(
    decision: CommitDeliveryDecision,
    *,
    run_dir: Path,
    status: CommitDeliveryStatus,
    error: str | None = None,
    commit_sha: str | None = None,
    files_staged: tuple[str, ...] = (),
    untracked_delivered: tuple[str, ...] = (),
    target_dirty_paths: tuple[str, ...] = (),
    target_dirty_retries: int = 0,
    action: CommitDeliveryAction | None = None,
    delivery_branch: str | None = None,
    pr_intent: DeliveryPrIntent | None = None,
    pr_url: str | None = None,
    delivery_warnings: tuple[str, ...] = (),
    delivery_notices: tuple[str, ...] = (),
    artifact_commit_sha: str | None = None,
) -> CommitDeliveryDecision:
    """Write the audit artifact and rebuild the frozen decision dataclass.

    ``action`` lets the dirty-prompt skip/halt path substitute the
    final action on the resulting decision (e.g. operator originally
    chose approve but resolved a target-dirty block via skip).

    ``artifact_commit_sha`` (ADR 0119) decouples the durable audit ``commit_sha``
    from the decision/wire ``commit_sha``. A ``worktree_branch`` publish makes a
    real commit on the run's delivery branch (recorded in the schema-locked audit
    artifact, which requires a non-empty sha for a ``committed`` record) but
    leaves the canonical checkout untouched, so the decision surfaces
    ``commit_sha=None`` and ``delivery_branch`` instead. When ``None`` the audit
    sha mirrors the decision ``commit_sha`` (byte-identical prior behavior).
    """
    final_action = action or decision.action
    artifact_path = _artifact_path(run_dir, decision.decision_id)
    artifact = _artifact_dict(
        decision,
        status=status,
        error=error,
        commit_sha=(
            artifact_commit_sha if artifact_commit_sha is not None else commit_sha
        ),
        files_staged=files_staged,
        untracked_delivered=untracked_delivered,
        target_dirty_paths=target_dirty_paths,
        target_dirty_retries=target_dirty_retries,
        action_override=action,
    )
    validate_decision_dict(artifact)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(artifact, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return CommitDeliveryDecision(
        action=final_action,
        status=status,
        run_id=decision.run_id,
        decision_id=decision.decision_id,
        project_path=decision.project_path,
        source_path=decision.source_path,
        baseline_ref=decision.baseline_ref,
        dirty=decision.dirty,
        release_summary=decision.release_summary,
        release_verdict=decision.release_verdict,
        release_blockers=decision.release_blockers,
        patch_text=decision.patch_text,
        changed_paths=decision.changed_paths,
        untracked_paths=decision.untracked_paths,
        untracked_delivered=untracked_delivered,
        files_staged=files_staged,
        include_untracked=decision.include_untracked,
        commit_message_strategy=(
            decision.commit_message_strategy
            if final_action == "approve" else None
        ),
        # Dirty-prompt skip/halt should not present a commit message —
        # the action was substituted away from approve.
        final_message=decision.final_message if final_action == "approve" else None,
        commit_sha=commit_sha,
        error=error,
        decided_at=decision.decided_at,
        artifact_path=artifact_path,
        target_dirty_paths=target_dirty_paths,
        target_dirty_retries=target_dirty_retries,
        # Stage 6 awareness travels with the delivered decision (e.g. warn
        # policy: delivery proceeds, the keys stay visible in to_dict).
        verification_policy=decision.verification_policy,
        verification_missing=decision.verification_missing,
        verification_failed=decision.verification_failed,
        verification_stale=decision.verification_stale,
        generated_garbage_paths=decision.generated_garbage_paths,
        # Delivery-scope awareness (T4) travels with the persisted decision so
        # the meta.commit_delivery block keeps its scope/disclosure context.
        delivery_scope=decision.delivery_scope,
        scope_blocker=decision.scope_blocker,
        scope_disclosure=decision.scope_disclosure,
        scope_projects=decision.scope_projects,
        scope_companions=decision.scope_companions,
        # ADR 0119 delivery-branch outcome travels with the delivered decision.
        delivery_branch=delivery_branch,
        pr_intent=pr_intent,
        # ADR 0121 — typed twin of the 'PR opened' notice, from PublishResult.
        pr_url=pr_url,
        delivery_warnings=delivery_warnings,
        delivery_notices=delivery_notices,
    )


def _artifact_dict(
    decision: CommitDeliveryDecision,
    *,
    status: CommitDeliveryStatus,
    error: str | None,
    commit_sha: str | None,
    files_staged: tuple[str, ...],
    untracked_delivered: tuple[str, ...],
    target_dirty_paths: tuple[str, ...] = (),
    target_dirty_retries: int = 0,
    action_override: CommitDeliveryAction | None = None,
) -> dict[str, Any]:
    """Build the JSON-serialisable audit artifact for one delivery decision.

    ``action_override`` covers the dirty-prompt skip/halt path: the
    operator's substituted action lands in the artifact instead of the
    original approve/apply, so the schema validator's cross-field
    coherence is satisfied (skip↔skipped, halt↔halted only).
    """
    effective_action = action_override or decision.action
    artifact: dict[str, Any] = {
        "run_id": decision.run_id,
        "decision_id": decision.decision_id,
        "action": effective_action,
        "include_untracked": decision.include_untracked,
        "include_pre_existing_dirty": False,
        "files_staged": list(files_staged),
        "untracked_delivered": list(untracked_delivered),
        "commit_status": status,
        "decided_at": decision.decided_at or datetime.now(UTC).isoformat(),
        "strategy": (
            decision.commit_message_strategy
            if effective_action == "approve" else None
        ),
        "final_message": (
            decision.final_message if effective_action == "approve" else None
        ),
        "commit_sha": commit_sha,
        "commit_error": error,
        "operator": None,
    }
    if target_dirty_paths:
        artifact["target_dirty_paths"] = list(target_dirty_paths)
    if target_dirty_retries:
        artifact["target_dirty_retries"] = target_dirty_retries
    # Stage 6 awareness (ADR 0083): additive keys, present only when non-empty,
    # so no-contract artifacts stay byte-identical to prior behavior.
    if decision.verification_policy:
        artifact["verification_policy"] = decision.verification_policy
    if decision.verification_missing:
        artifact["verification_missing"] = list(decision.verification_missing)
    if decision.verification_failed:
        artifact["verification_failed"] = list(decision.verification_failed)
    if decision.verification_stale:
        artifact["verification_stale"] = list(decision.verification_stale)
    if decision.generated_garbage_paths:
        artifact["generated_garbage_paths"] = list(
            decision.generated_garbage_paths,
        )
    return artifact


# ── Stage 6 verification delivery gate helpers (ADR 0083) ────────────────────
#
# All consume the assessment duck-typed and return empty / no-op for ``None``,
# so the no-contract path adds nothing. The assessment object itself is built
# upstream (pipeline.verification_delivery); the engine never resolves it here.


def _scope_decision_fields(assessment: Any) -> dict[str, Any]:
    """Additive delivery-scope fields to splat onto a CommitDeliveryDecision.

    Empty dict for ``None`` (no recorded scope), so the no-scope path adds
    nothing. ``scope_blocker`` is included only for a strict-mono violation.
    """
    if assessment is None:
        return {}
    fields: dict[str, Any] = {"delivery_scope": assessment.scope.value}
    if assessment.blocker:
        fields["scope_blocker"] = assessment.blocker
    if assessment.disclosure:
        fields["scope_disclosure"] = assessment.disclosure
    if assessment.affected_projects:
        fields["scope_projects"] = assessment.affected_projects
    if getattr(assessment, "companions", ()):  # T1 per-repo enrichment
        fields["scope_companions"] = tuple(assessment.companions)
    return fields


def _verification_decision_fields(
    gate: DeliveryVerificationAssessment | None,
) -> dict[str, Any]:
    """The additive verification fields to splat onto a CommitDeliveryDecision."""
    if gate is None:
        return {}
    return {
        "verification_policy": gate.policy,
        "verification_missing": gate.required_missing,
        "verification_failed": gate.required_failed,
        "verification_stale": gate.required_stale,
        "generated_garbage_paths": gate.garbage_paths,
    }


def _verification_block_error(gate: DeliveryVerificationAssessment) -> str:
    """One-line error for a non-interactive require-block, listing the blockers.

    Worded as a hard block on the required verification: delivery is blocked
    until the receipt exists or an operator waives it. ``gate.lines`` carries the
    require blockers plus the actionable fix hints (the ``orcho verify`` commands)
    so the banner always names a next step.
    """
    return (
        "Delivery gate — blocked by required verification: delivery blocked "
        "until receipt or waiver — " + "; ".join(gate.lines)
    )


def _verification_prompt_warning_lines(
    gate: DeliveryVerificationAssessment | None,
) -> tuple[str, ...]:
    """Receipt warning lines shown in the interactive prompt for warn/require.

    Uses the assessment's shared receipt-blocker lines plus its diagnostic lines
    (searched dirs + exact ``orcho verify`` hints when a receipt is missing), so
    the interactive prompt carries the same actionable guidance as the
    non-interactive banner — and never a bare "missing required receipts" with no
    next step (ADR 0089). Garbage is intentionally excluded here: it has its own
    distinct prompt section (``_verification_prompt_garbage``), so reusing
    ``gate.lines`` would duplicate it.
    """
    if gate is None or gate.policy not in ("warn", "require"):
        return ()
    return (
        gate.receipt_blocker_lines
        + _verification_waived_lines(gate)
        + gate.diagnostic_lines
    )


def _verification_waived_lines(
    gate: DeliveryVerificationAssessment | None,
) -> tuple[str, ...]:
    """Explicit waived-gate lines: a required gap excused by a durable waiver.

    One line per waived required gate, naming the gate command, the durable
    ``gate:<command>:<round>`` handoff that excused it, whether the underlying
    receipt was ``failed`` or ``missing``, and a bounded preview of the waiver
    rationale. Empty when no gate was waived, so the no-waiver path adds nothing.
    The waived gate is shown as excused — never as passed and never as a blocker.
    """
    if gate is None or not getattr(gate, "waived_gates", ()):
        return ()
    out: list[str] = []
    for w in gate.waived_gates:
        preview = w.waiver_text or w.note or ""
        note_suffix = f"; note: {preview}" if preview else ""
        out.append(
            f"waived required receipts: {w.gate_command} "
            f"(handoff {w.handoff_id}; {w.status}{note_suffix})"
        )
    return tuple(out)


def _verification_prompt_garbage(
    gate: DeliveryVerificationAssessment | None,
) -> tuple[str, ...]:
    """Generated-garbage paths shown as a distinct prompt section (warn/require)."""
    if gate is None or gate.policy not in ("warn", "require"):
        return ()
    return gate.garbage_paths


def _verification_prompt_operator(
    gate: DeliveryVerificationAssessment | None,
) -> tuple[str, ...]:
    """Manual/operator-only advisory line for the prompt, or empty.

    Policy-agnostic and independent of blockers: a required command parked behind
    ``manual_only`` / operator-only is never a blocker and never a missing-required
    receipt, but it must stay VISIBLE as not-auto-run — even when it is the only
    gap and delivery proceeds on the apply/approve default.
    """
    if gate is None:
        return ()
    line = gate.operator_line
    return (line,) if line else ()


def _verification_suggest_hint(
    gate: DeliveryVerificationAssessment | None,
) -> str:
    """Single hint line for the ``suggest`` policy; empty otherwise."""
    if gate is None or gate.policy != "suggest" or not gate.has_blockers:
        return ""
    return "Verification incomplete (suggest): " + "; ".join(gate.lines)


def _log_verification_warning(gate: DeliveryVerificationAssessment) -> None:
    """Emit a non-interactive non-blocking warning; delivery still proceeds.

    Used for warn/suggest gaps (and staged garbage) regardless of the boundary
    policy. The per-gate policy and the ``shipping allowed by policy`` note ride
    on ``gate.lines``, so the header stays policy-neutral.
    """
    from core.observability.logging import warn

    lines = list(gate.lines) + list(_verification_waived_lines(gate))
    warn(
        "Verification incomplete for delivery: "
        + "; ".join(lines),
    )


def _release_entry(session: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return the ``final_acceptance`` phase entry from a session, or None.

    ``None`` is load-bearing: it signals that the profile has no
    release-gating phase at all (e.g. ``lite``), which the resolver
    treats as implicit approval rather than as a missing verdict.
    Returning ``{}`` instead would collapse the two states.
    """
    phases = session.get("phases")
    if isinstance(phases, Mapping):
        entry = phases.get("final_acceptance")
        if isinstance(entry, Mapping):
            return entry
    return None


def _release_blocker_summary(release_dict: Mapping[str, Any]) -> str:
    """Render a compact one-line summary from ``release_blockers`` titles.

    Used only as a fallback when ``short_summary`` is empty, so a rejected
    release decision never persists with no human-readable reason. Returns ""
    when there are no usable blocker titles.
    """
    titles: list[str] = []
    for blocker in _release_blockers(release_dict):
        title = str(
            blocker.get("title") or blocker.get("why_blocks_release") or ""
        ).strip()
        if title:
            titles.append(title)
    if not titles:
        return ""
    return "Release blockers: " + "; ".join(titles)


def _release_blockers(release_dict: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """Return JSON-object release blockers, preserving the validated payload."""
    blockers = release_dict.get("release_blockers")
    if not isinstance(blockers, list):
        return ()
    out: list[dict[str, Any]] = []
    for blocker in blockers:
        if not isinstance(blocker, Mapping):
            continue
        normalized = {str(key): value for key, value in blocker.items()}
        if normalized:
            out.append(normalized)
    return tuple(out)


def _changed_paths(source_worktree: Path, baseline_ref: str) -> tuple[str, ...]:
    lines = _git_lines(source_worktree, ["diff", "--name-only", baseline_ref])
    return tuple(line for line in lines if line.strip())


def _changed_paths_for_delivery(
    source_worktree: Path,
    baseline_ref: str,
    paths: tuple[str, ...],
) -> tuple[str, ...]:
    safe_paths = tuple(path for path in paths if _safe_relative_path(path))
    if not safe_paths:
        return ()
    lines = _git_lines(
        source_worktree,
        ["diff", "--name-only", baseline_ref, "--", *safe_paths],
    )
    return tuple(line for line in lines if line.strip())


def _dedupe_changed_against_untracked(
    changed_paths: tuple[str, ...],
    untracked_paths: tuple[str, ...],
) -> tuple[str, ...]:
    """Classify each path once: drop changed paths already deemed untracked.

    A run-seeded untracked file gets folded into the seed baseline as a
    tracked add (``write-tree`` after ``add -A``) while the worktree HEAD
    stays at the pre-seed base. That single path then surfaces in BOTH
    ``git diff --name-only <seed_tree>`` (the index lacks it, so the
    baseline tree's tracked blob reads as a change) AND
    ``ls-files --others`` (it is still untracked on disk). Untracked
    wins, so the delivery gate renders one ``??`` line instead of a
    phantom ``M``/``??`` pair, and transport copies the file once rather
    than also trying to patch it.
    """
    if not untracked_paths:
        return changed_paths
    untracked = set(untracked_paths)
    return tuple(path for path in changed_paths if path not in untracked)


def _untracked_paths(source_worktree: Path) -> tuple[str, ...]:
    lines = _git_lines(
        source_worktree,
        ["ls-files", "--others", "--exclude-standard"],
    )
    return tuple(
        line for line in lines
        if line.strip() and not _is_python_bytecode_artifact(line)
    )


def _target_dirty_paths(project_dir: Path) -> tuple[str, ...]:
    """Return ``git status --porcelain=v1`` lines verbatim from project_dir.

    Lines keep their porcelain prefixes (" M ", "?? ", "A  ", "D  ",
    etc.) so operators reading the audit artifact can tell modified
    files from untracked at a glance — the same convention the
    delivery prompt uses for the run's own changed/untracked split.

    Empty tuple ⇒ clean checkout; any non-empty tuple ⇒ delivery
    must pause for an operator decision.
    """
    lines = _git_lines(project_dir, ["status", "--porcelain=v1", "-uall"])
    return tuple(
        line for line in lines
        if line.strip() and not _is_untracked_python_bytecode_status(line)
    )


def _same_checkout(left: Path, right: Path) -> bool:
    return left.resolve() == right.resolve()


def _parallel_dirty_paths_for_in_place_delivery(
    dirty_paths: tuple[str, ...],
    decision: CommitDeliveryDecision,
) -> tuple[str, ...]:
    """Drop run-owned dirty paths when source and target are the same checkout.

    Worktree delivery protects the canonical checkout from unrelated parallel
    edits: any target dirtiness is suspicious before transport. In in-place
    profiles there is no transport step — the run already mutated
    ``project_path`` — so the dirty guard must not classify the run's own
    changed paths as parallel work.
    """
    owned_paths = set(decision.changed_paths)
    if decision.include_untracked:
        owned_paths.update(decision.untracked_paths)
    if not owned_paths:
        return dirty_paths
    return tuple(
        line for line in dirty_paths
        if (_status_path(line) or "") not in owned_paths
    )


def _status_path(line: str) -> str | None:
    if not line.strip() or len(line) < 4:
        return None
    path = line[3:].strip()
    if " -> " in path:
        path = path.rsplit(" -> ", 1)[-1]
    return path or None


def _is_untracked_python_bytecode_status(line: str) -> bool:
    return line.startswith("?? ") and _is_python_bytecode_artifact(line[3:].strip())


def _is_python_bytecode_artifact(path: str) -> bool:
    p = Path(path)
    return "__pycache__" in p.parts or p.suffix in _PYTHON_BYTECODE_SUFFIXES


def _safe_relative_path(path: str) -> bool:
    p = Path(path)
    return not p.is_absolute() and ".." not in p.parts


def _commit_message_strategy(cfg: Mapping[str, Any]) -> str:
    raw = str(cfg.get("default_strategy") or "release_summary").strip()
    if raw in {"release_summary", "llm_generate", "operator_typed"}:
        return raw
    return "release_summary"


def _will_open_pr(cfg: Mapping[str, Any]) -> bool:
    """True when this delivery is on a publish-outward path (opens a PR).

    A run publishes outward unless publication is explicitly turned off
    (``publish=off``) or the branch policy is the ``bypass`` opt-out (commit onto
    the current HEAD, no branch/PR intent). On that outward path the outward
    commit message must be authored in ``content_language`` regardless of the
    configured ``default_strategy`` — so the operator language never leaks into
    a public commit / PR (ADR 0121).
    """
    publish = str(cfg.get("publish", "auto")).strip().lower()
    if publish == "off":
        return False
    # Consult ``normalize_branch_policy`` through the ``delivery_branch`` module
    # (not a direct import binding) so this stays consistent with the *actual*
    # branch decision made by ``resolve_delivery_branch`` — including any embedder
    # / test seam that overrides the policy. A ``bypass`` delivery commits onto
    # the current HEAD with no branch or PR intent, so it is not publish-outward.
    policy = _delivery_branch.normalize_branch_policy(cfg.get("branch_policy"))
    return policy != "bypass"


def _resolve_final_commit_message(
    decision: CommitDeliveryDecision,
    *,
    configured_strategy: str,
    generator: CommitMessageGenerator | None,
    force_llm: bool = False,
) -> tuple[str, str]:
    fallback = _message_from_release_summary(
        decision.release_summary, decision.run_id,
    )
    if (configured_strategy == "llm_generate" or force_llm) and generator is not None:
        generated = generator(decision)
        if isinstance(generated, str) and generated.strip():
            return generated.strip(), "llm_generate"
    return fallback, "release_summary"


def render_commit_message_prompt(
    decision: CommitDeliveryDecision,
    *,
    body_language: str | None = None,
) -> str:
    """Build the read-only prompt for ``commit.default_strategy=llm_generate``."""
    from core.io.prompt_loader import render_prompt
    from pipeline.prompts.contracts import (
        commit_message_json_contract,
        compose_prompt,
    )

    guidance = render_prompt(
        "tasks/commit_message",
        project_dir=decision.source_path,
    )
    changed = "\n".join(f"- {p}" for p in decision.changed_paths) or "(none)"
    untracked = (
        "\n".join(f"- {p}" for p in decision.untracked_paths) or "(none)"
    )
    body = (
        f"{guidance}\n\n"
        "Release summary:\n"
        f"{decision.release_summary or '(none)'}\n\n"
        "Run-owned changed files:\n"
        f"{changed}\n\n"
        "Run-owned untracked files:\n"
        f"{untracked}\n\n"
        "Run diff:\n"
        "```diff\n"
        f"{decision.patch_text.rstrip()}\n"
        "```"
    )
    return compose_prompt(
        body,
        system_tail=(commit_message_json_contract(body_language=body_language),),
    )


def _message_from_release_summary(summary: str, run_id: str) -> str:
    subject = " ".join(summary.split())
    if not subject:
        subject = f"chore: deliver orcho run {run_id}"
    return subject


# Destination tokens describe where an ``approve`` will actually land, so the
# delivery menu wording matches the ADR 0119 branch policy that
# ``apply_commit_delivery`` independently re-resolves. ``resolve_commit_delivery``
# derives the token from a read-only ``resolve_delivery_branch`` outcome;
# ``fallback`` is used when that read fails, so the wording never lies about a
# publish (checkout untouched) as a plain checkout commit.
_DESTINATION_CHECKOUT_COMMIT = "checkout_commit"
_DESTINATION_PUBLISHED_BRANCH = "published_branch"
_DESTINATION_BRANCH_IN_CHECKOUT = "branch_in_checkout"
_DESTINATION_FALLBACK = "fallback"


def _menu_destination(
    *,
    source_worktree: Path,
    project_dir: Path,
    run_id: str,
    baseline_ref: str,
    cfg: Mapping[str, Any],
    release_summary: str,
) -> str:
    """Read-only classification of where an ``approve`` will land (menu wording).

    Independently re-resolves the ADR 0119 delivery-branch policy for DISPLAY
    only — ``apply_commit_delivery`` resolves it again for the real transport, so
    this changes no behavior. ``resolve_delivery_branch`` only reads git
    (``detect_default_branch``); it writes nothing. Any failure falls back to a
    generic honest wording so the menu never claims a checkout commit for a
    publish.
    """
    try:
        outcome = resolve_delivery_branch(
            source_path=source_worktree,
            project_path=project_dir,
            run_id=run_id,
            base_ref=baseline_ref,
            branch_policy=cfg.get("branch_policy"),
            named_branch=cfg.get("branch_name"),
            release_summary=release_summary,
        )
    except Exception:
        return _DESTINATION_FALLBACK
    if outcome.plan == "publish":
        return _DESTINATION_PUBLISHED_BRANCH
    if outcome.plan == "commit_on_branch":
        return _DESTINATION_BRANCH_IN_CHECKOUT
    return _DESTINATION_CHECKOUT_COMMIT


def _approve_helptext(destination: str, *, correction: bool) -> str:
    """``approve`` menu description, tailored to the resolved destination."""
    if destination == _DESTINATION_CHECKOUT_COMMIT:
        # Preserve the shipped bypass / commit-in-place wording byte-for-byte.
        if correction:
            return (
                "Override final acceptance, apply the diff to the project "
                "checkout, AND create a commit."
            )
        return "Apply the diff to the project checkout AND create a commit."
    if destination == _DESTINATION_PUBLISHED_BRANCH:
        body = (
            "commit onto a delivery branch, push it, and open a pull request "
            "— your project checkout is NOT modified."
        )
    elif destination == _DESTINATION_BRANCH_IN_CHECKOUT:
        body = (
            "commit onto a local delivery branch in your project checkout. "
            "This does not push the branch or open a pull request."
        )
    else:  # fallback — honest for both publish and in-place under the policy.
        body = (
            "commit — under the branch policy this pushes an orcho/deliver "
            "branch and opens a PR on protected targets, or commits to your "
            "checkout otherwise."
        )
    if correction:
        return "Override final acceptance, " + body
    return body[0].upper() + body[1:]


def _apply_helptext(destination: str, *, correction: bool) -> str:
    """``apply`` menu description; clarified only for the publish destination."""
    if destination == _DESTINATION_PUBLISHED_BRANCH:
        if correction:
            return (
                "Override final acceptance, apply the diff to your project "
                "checkout, uncommitted, for a later operator-owned commit."
            )
        return (
            "Apply the diff to your project checkout, uncommitted, for a later "
            "operator-owned commit."
        )
    if correction:
        return (
            "Override final acceptance, apply the diff to the project checkout, "
            "and leave it uncommitted."
        )
    return (
        "Apply the diff to the project checkout, leave it uncommitted "
        "for a later operator-owned commit."
    )


def _delivery_menu_options(
    *, correction: bool, destination: str,
) -> tuple[tuple[str, str, str, str], ...]:
    """Build the delivery / correction menu rows.

    ids, glyphs, actions, and aliases are identical across destinations; only the
    ``approve`` and ``apply`` descriptions are tailored so the menu tells the
    truth about where the diff lands.
    """
    approve = _approve_helptext(destination, correction=correction)
    apply_text = _apply_helptext(destination, correction=correction)
    if correction:
        return (
            ("1", "🔧", "fix",
             "Continue with a correction follow-up in the same retained worktree."),
            ("2", "✅", "approve", approve),
            ("3", "📥", "apply", apply_text),
            ("4", "⏭", "skip",
             "Retain artifacts and do not deliver. The run is not marked correction-ready."),
            ("5", "🛑", "halt",
             "Don't deliver and mark the run HALTED. Use when the run should be "
             "flagged as wrong, not corrected."),
        )
    return (
        ("1", "✅", "approve", approve),
        ("2", "📥", "apply", apply_text),
        ("3", "⏭",  "skip",
         "Don't deliver — finish the run as DONE (success). The diff stays in "
         "the retained run artifacts; you can deliver it manually later."),
        ("4", "🛑", "halt",
         "Don't deliver and mark the run HALTED (not a success). Use when "
         "something is wrong and the run should be flagged, not counted as done."),
    )

_DELIVERY_ALIASES: dict[str, str] = {
    "1": "approve", "a": "approve", "approve": "approve",
    "2": "apply",   "apply": "apply",
    "3": "skip",    "s": "skip", "skip": "skip",
    "4": "halt",    "h": "halt", "halt": "halt",
}

_CORRECTION_ALIASES: dict[str, str] = {
    "1": "fix",     "f": "fix", "fix": "fix",
    "2": "approve", "a": "approve", "approve": "approve",
    "3": "apply",   "apply": "apply",
    "4": "skip",    "s": "skip", "skip": "skip",
    "retain": "skip",
    "5": "halt",    "h": "halt", "halt": "halt",
}


_OUTCOME_SILENT: frozenset[str] = frozenset(
    {"disabled", "not_applicable", "no_diff", "pending"},
)


_DELIVERY_BANNER_WIDTH = 68


def _delivery_banner(
    headline: str,
    rows: tuple[tuple[str, str], ...],
    *,
    tone: str,
    output_fn: Callable[[str], None],
    color: bool,
    extra_lines: tuple[str, ...] = (),
) -> None:
    """Render a framed, tone-coloured delivery banner.

    A ruled box (``═`` × 68) around a bold headline that names the delivery
    disposition — PULL REQUEST OPENED / BRANCH PUSHED / COMMITTED TO YOUR
    CHECKOUT / SKIPPED / HALTED / FAILED — so the operator cannot miss what
    became of the run's work. ``rows`` are ``(label, value)`` pairs with the
    labels aligned; ``extra_lines`` are already-formatted trailing lines (e.g.
    a ``  + path`` block) shown between the rows and the closing rule.
    """
    rule = paint(_DELIVERY_BANNER_WIDTH * "═", tone, color=color)
    output_fn("")
    output_fn(rule)
    output_fn(paint(f"  📦  DELIVERY — {headline}", tone, C.BOLD, color=color))
    output_fn(rule)
    label_w = max((len(label) for label, _ in rows), default=0)
    for label, value in rows:
        output_fn(f"   {bold(label.ljust(label_w), color=color)}   {value}")
    for line in extra_lines:
        output_fn(line)
    output_fn(rule)


def _render_published_branch(
    decision: CommitDeliveryDecision,
    *,
    output_fn: Callable[[str], None],
    color: bool,
) -> None:
    """Render the ADR 0119/0121 published-branch ``committed`` outcome.

    The canonical checkout is never touched here (``commit_sha is None``): the
    run's work rides a pushed ``orcho/deliver/…`` branch. The banner headline
    states the disposition outright — a PR was opened, or the branch is pushed
    and still needs a PR. Degrade reasons ride on ``delivery_warnings`` and are
    surfaced under the banner by :func:`_render_delivery_diagnostics`.
    """
    checkout_note = help_line(
        "not modified — your working tree is untouched", color=color,
    )
    if decision.pr_url:
        _delivery_banner(
            "PULL REQUEST OPENED",
            (
                ("PR", decision.pr_url),
                ("Branch", decision.delivery_branch or ""),
                ("Checkout", checkout_note),
            ),
            tone=C.GREEN,
            output_fn=output_fn,
            color=color,
        )
    else:
        # ``pr_url=None`` does not prove whether the provider pushed the branch:
        # the current durable decision intentionally stores no ``pushed`` bit.
        # Stay honest about the known state and give an ordered next action.
        _delivery_banner(
            "DELIVERY BRANCH READY  ·  no PR",
            (
                ("Branch", decision.delivery_branch or ""),
                (
                    "Next",
                    "push the branch if needed, then open a pull request",
                ),
                ("Checkout", checkout_note),
            ),
            tone=C.YELLOW,
            output_fn=output_fn,
            color=color,
        )


def _render_local_delivery_branch(
    decision: CommitDeliveryDecision,
    *,
    output_fn: Callable[[str], None],
    color: bool,
) -> None:
    """Render an in-place protected commit on a local delivery branch."""
    sha7 = (decision.commit_sha or "")[:7]
    first_line = (
        decision.final_message.splitlines()[0]
        if decision.final_message else ""
    )
    _delivery_banner(
        "COMMITTED TO LOCAL DELIVERY BRANCH",
        (
            ("Commit", f"{sha7}  {first_line}".rstrip()),
            ("Branch", decision.delivery_branch or ""),
            (
                "Checkout",
                help_line(
                    "switched to the delivery branch; working tree is clean",
                    color=color,
                ),
            ),
            (
                "Next",
                "push the branch, then open a pull request if desired",
            ),
        ),
        tone=C.GREEN,
        output_fn=output_fn,
        color=color,
    )


def _render_checkout_commit(
    decision: CommitDeliveryDecision,
    *,
    output_fn: Callable[[str], None],
    color: bool,
) -> None:
    """Render the local checkout-commit ``committed`` outcome.

    A real commit landed on the canonical checkout (``commit_sha`` set, no
    ``delivery_branch`` — the ``apply``-style / bypass local-commit path). The
    ``  + <path>`` block follows the banner rows.
    """
    sha7 = (decision.commit_sha or "")[:7]
    first_line = ""
    if decision.final_message:
        first_line = decision.final_message.splitlines()[0]
    staged = list(decision.files_staged)
    staged_set = set(decision.files_staged)
    extras = [
        u for u in decision.untracked_delivered if u not in staged_set
    ]
    _delivery_banner(
        "COMMITTED TO YOUR CHECKOUT",
        (
            ("Commit", f"{sha7}  {first_line}".rstrip()),
            ("Where", help_line(
                "project checkout — working tree changed", color=color,
            )),
        ),
        tone=C.GREEN,
        output_fn=output_fn,
        color=color,
        extra_lines=tuple(f"   + {path}" for path in staged + extras),
    )


def _render_delivery_diagnostics(
    decision: CommitDeliveryDecision,
    *,
    output_fn: Callable[[str], None],
    color: bool,
) -> None:
    """Surface non-fatal delivery diagnostics on a terminal outcome.

    ``delivery_warnings`` (degrade reasons: provider disabled / missing /
    offline / failed, rebase conflicts) are always shown, one compact line
    each, so a degraded publish is visible instead of swallowed.
    ``delivery_notices`` are shown too, except those already represented by the
    published-branch block: the ``PR opened:`` notice (rendered as ``→ PR
    opened``) and the ``… is ready; open a pull request …`` notice (rendered as
    the manual-publish fallback) are dropped to avoid double-printing.
    """
    for warning in decision.delivery_warnings:
        output_fn(f"  {paint(f'⚠ {warning}', C.YELLOW, C.BOLD, color=color)}")
    for notice in decision.delivery_notices:
        if notice.startswith("PR opened:"):
            continue
        if "is ready; open a pull request" in notice:
            continue
        output_fn("  " + help_line(notice, color=color))


def render_delivery_outcome(
    decision: CommitDeliveryDecision,
    *,
    output_fn: Callable[[str], None] = print,
    run_dir: Path | None = None,
) -> None:
    """Print a one-shot human-readable outcome for a terminal delivery status.

    Pre-terminal statuses (``disabled``, ``not_applicable``, ``no_diff``,
    ``pending``) are silent: the caller is still mid-flow and the next
    step owns the user-visible signal. All other statuses are terminal
    and print one block describing what happened, followed by any
    non-fatal delivery diagnostics (:func:`_render_delivery_diagnostics`).

    The ``committed`` status has three shapes (ADR 0119/0121): a publish-path
    branch (``delivery_branch`` set, ``commit_sha is None``), an in-place
    protected local branch (both fields set), and a plain checkout commit
    (``commit_sha`` set, no ``delivery_branch``). Each renders a distinct block.
    """
    status = decision.status
    if status in _OUTCOME_SILENT:
        return
    color = is_color_active()

    if status == "committed":
        if decision.delivery_branch and decision.commit_sha:
            _render_local_delivery_branch(
                decision, output_fn=output_fn, color=color,
            )
        elif decision.delivery_branch:
            _render_published_branch(
                decision, output_fn=output_fn, color=color,
            )
        else:
            _render_checkout_commit(
                decision, output_fn=output_fn, color=color,
            )
    elif status == "applied_uncommitted":
        changed = list(decision.changed_paths)
        changed_set = set(decision.changed_paths)
        extras = [
            u for u in decision.untracked_delivered if u not in changed_set
        ]
        file_lines = tuple(f"   + {path}" for path in changed + extras)
        review = "   " + help_line(
            f"Review with: git -C {decision.project_path} status", color=color,
        )
        _delivery_banner(
            "APPLIED TO CHECKOUT  ·  no commit",
            (("Where", help_line(
                "project checkout — commit it manually", color=color,
            )),),
            tone=C.YELLOW,
            output_fn=output_fn,
            color=color,
            extra_lines=file_lines + (review,),
        )
    elif status == "skipped":
        if run_dir is not None:
            location: Path | str = run_dir
        elif decision.artifact_path is not None:
            location = decision.artifact_path.parent
        else:
            location = "(unknown)"
        _delivery_banner(
            "SKIPPED  ·  diff retained",
            (("Diff", str(location)),),
            tone=C.YELLOW,
            output_fn=output_fn,
            color=color,
        )
    elif status == "halted":
        _delivery_banner(
            "HALTED  ·  nothing delivered",
            (("Reason", "commit_decision_halt"),),
            tone=C.RED,
            output_fn=output_fn,
            color=color,
        )
    elif status == "fix_requested":
        _delivery_banner(
            "CORRECTION FOLLOW-UP REQUESTED",
            (("Worktree", str(decision.source_path)),),
            tone=C.YELLOW,
            output_fn=output_fn,
            color=color,
        )
    elif status == "commit_failed":
        _delivery_banner(
            "COMMIT FAILED",
            (("Error", decision.error or ""),),
            tone=C.RED,
            output_fn=output_fn,
            color=color,
        )
    elif status == "apply_failed":
        _delivery_banner(
            "APPLY FAILED",
            (("Error", decision.error or ""),),
            tone=C.RED,
            output_fn=output_fn,
            color=color,
        )
    elif status == "target_dirty":
        sample = list(decision.target_dirty_paths[:3])
        joined = ", ".join(sample)
        suffix = "..." if len(decision.target_dirty_paths) > 3 else ""
        _delivery_banner(
            "ABORTED  ·  checkout was dirty",
            (("Dirty", f"{joined}{suffix}"),),
            tone=C.RED,
            output_fn=output_fn,
            color=color,
        )
    elif status == "verification_blocked":
        _delivery_banner(
            "BLOCKED  ·  verification incomplete",
            (("Error", decision.error or ""),),
            tone=C.RED,
            output_fn=output_fn,
            color=color,
        )

    _render_delivery_diagnostics(decision, output_fn=output_fn, color=color)


def _prompt_action(
    *,
    default_action: str,
    release_summary: str,
    changed_paths: tuple[str, ...],
    untracked_paths: tuple[str, ...],
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
    release_blocked: bool = False,
    release_verdict: str = "",
    verification_correction: bool = False,
    verification_warning_lines: tuple[str, ...] = (),
    verification_suggest_hint: str = "",
    verification_garbage: tuple[str, ...] = (),
    verification_operator: tuple[str, ...] = (),
    destination: str = _DESTINATION_CHECKOUT_COMMIT,
) -> str:
    color = is_color_active()
    # Either independent gate routes the operator to the correction menu (which
    # offers ``fix``); release_blocked keeps title priority when both apply.
    correction = release_blocked or verification_correction
    options = _delivery_menu_options(correction=correction, destination=destination)
    aliases = (
        _CORRECTION_ALIASES if correction else _DELIVERY_ALIASES
    )
    if release_blocked:
        title_text = "Correction gate — acceptance rejected"
    elif verification_correction:
        title_text = "Delivery gate — blocked by required verification"
    else:
        title_text = "Delivery gate — diff is ready"
    output_fn("")
    output_fn(divider(color=color))
    output_fn(f"  {title(title_text, color=color)}")
    output_fn(divider(color=color))
    if release_blocked:
        verdict_label = release_verdict or "not approved"
        output_fn(
            f"  {bold('⚠ Final acceptance did NOT approve this change', color=color)} "
            f"(verdict: {verdict_label})."
        )
        output_fn(
            f"  {help_line('Default is fix: keep the worktree and run a correction follow-up.', color=color)}"
        )
        output_fn("")
    elif verification_correction:
        # A ``require``-policy verification gap blocks delivery (ADR 0090):
        # nothing ships until the receipt exists or the operator waives it.
        output_fn(
            f"  {bold('⛔ Required verification incomplete — delivery blocked until receipt or waiver.', color=color)}"
        )
        output_fn(
            f"  {help_line('Default is fix: keep the worktree, materialize the receipt (or waive), then deliver.', color=color)}"
        )
        output_fn("")
    # Verification gate is independent of release: render its warnings / hint
    # whenever supplied (warn or require policies). The single suggest hint is
    # mutually exclusive with the warning block by construction in the caller.
    # The header is policy-aware: a ``require`` gap is a hard block, while a
    # ``warn`` gap is surfaced but shipping is allowed by policy.
    if verification_warning_lines:
        warning_header = (
            "⛔ Delivery blocked by required verification:"
            if verification_correction
            else "⚠ Verification incomplete (warn) — shipping allowed by policy:"
        )
        output_fn(f"  {bold(warning_header, color=color)}")
        for line in verification_warning_lines:
            output_fn(f"  {help_line(line, color=color)}")
        output_fn("")
    elif verification_suggest_hint:
        output_fn(
            f"  {help_line(verification_suggest_hint, color=color)}"
        )
        output_fn("")
    # Manual/operator-only commands are advisory and policy-agnostic: never a
    # blocker, never a missing-required receipt, but always visible as
    # not-auto-run — even when they are the only gap and delivery proceeds on the
    # apply/approve default.
    if verification_operator:
        for line in verification_operator:
            output_fn(f"  {help_line(line, color=color)}")
        output_fn("")
    if release_summary:
        output_fn(
            f"  {bold('Release summary:', color=color)} {release_summary}",
        )
        output_fn("")
    if changed_paths or untracked_paths:
        output_fn(f"  {bold('Run diff:', color=color)}")
        # The "  M  <path>" / "  ?? <path>" lines stay plain text — the
        # delivery decision artifact consumers and tests read them
        # verbatim from the output stream.
        for path in changed_paths:
            output_fn(f"  M  {path}")
        for path in untracked_paths:
            output_fn(f"  ?? {path}")
        output_fn("")
    # Generated environment garbage is rendered as a DISTINCT section from the
    # product diff above — it is detected, not part of the M/?? delivery set.
    if verification_garbage:
        output_fn(
            f"  {bold('Generated environment garbage (not product diff):', color=color)}"
        )
        for path in verification_garbage:
            output_fn(f"  ⚠ {path}")
        output_fn("")
    output_fn(f"  {bold('What do you want to do?', color=color)}")
    output_fn("")
    for num, glyph, name, helptext in options:
        label = f"{num}) {glyph} {bold(name, color=color)}"
        if name == default_action:
            label = f"{label}  {default_chip(color=color)}"
        output_fn(f"    {label}")
        output_fn(f"       {help_line(helptext, color=color)}")
    output_fn("")
    aliases = {**aliases, "": default_action}
    while True:
        choices = "1/2/3/4/5 or name" if correction else "1/2/3/4 or name"
        prompt_text = (
            f"  Choose [{bold(default_action, color=color)}] "
            f"({choices}): "
        )
        raw = input_fn(prompt_text).strip().lower()
        action = aliases.get(raw)
        if action:
            # Choosing to deliver despite a require-policy verification block is
            # an explicit operator override/waiver — mark it so the confirming
            # output never reads as a clean, fully-proven delivery.
            if verification_correction and action in ("approve", "apply"):
                output_fn(
                    f"  {bold('⚠ Overriding the required verification block — operator waiver; delivering anyway.', color=color)}"
                )
            return action
        output_fn(
            f"    {help_line(_invalid_choice_hint(correction), color=color)}",
        )


def _invalid_choice_hint(correction: bool) -> str:
    if correction:
        return "Please choose fix, approve, apply, skip, or halt."
    return "Please choose approve, apply, skip, or halt."


_TARGET_DIRTY_OPTIONS: tuple[tuple[str, str, str, str], ...] = (
    ("1", "🔁", "retry",
     "Re-check the project checkout (clean it up first, then continue)."),
    ("2", "⏭",  "skip",
     "Don't deliver — finish the run as DONE (success). The diff stays in "
     "the retained run artifacts; deliver it manually later."),
    ("3", "🛑", "halt",
     "Don't deliver and mark the run HALTED (not a success) — flag it as "
     "blocked at this gate rather than counting it as done."),
)

_TARGET_DIRTY_ALIASES: dict[str, Literal["retry", "skip", "halt"]] = {
    "1": "retry", "r": "retry", "retry": "retry",
    "2": "skip",  "s": "skip", "skip": "skip",
    "3": "halt",  "h": "halt", "halt": "halt",
    "": "retry",
}


def _prompt_target_dirty(
    *,
    dirty_paths: tuple[str, ...],
    retries: int,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> Literal["retry", "skip", "halt"]:
    """Pause delivery on a dirty project checkout.

    Three operator choices: ``retry`` re-checks the checkout (the
    operator just cleaned up); ``skip`` abandons delivery, keeping the
    diff only in retained run artifacts; ``halt`` terminates the run
    as blocked. Original delivery action (approve/apply) is preserved
    on ``retry``; ``skip``/``halt`` substitute it.
    """
    color = is_color_active()
    output_fn("")
    output_fn(divider(color=color))
    output_fn(
        f"  {title('Delivery paused — project checkout is dirty', color=color)}",
    )
    output_fn(divider(color=color))
    output_fn(
        f"  {help_line('Delivery is paused to avoid mixing this run with parallel work.', color=color)}",
    )
    output_fn("")
    output_fn(f"  {bold('Uncommitted changes in target checkout:', color=color)}")
    for line in dirty_paths:
        output_fn(f"  {line}")
    if retries:
        output_fn("")
        output_fn(
            f"  {help_line(f'(retries so far: {retries})', color=color)}",
        )
    output_fn("")
    output_fn(f"  {bold('What do you want to do?', color=color)}")
    output_fn("")
    for num, glyph, name, helptext in _TARGET_DIRTY_OPTIONS:
        label = f"{num}) {glyph} {bold(name, color=color)}"
        if name == "retry":
            label = f"{label}  {default_chip(color=color)}"
        output_fn(f"    {label}")
        output_fn(f"       {help_line(helptext, color=color)}")
    output_fn("")
    while True:
        prompt_text = (
            f"  Choose [{bold('retry', color=color)}] "
            f"(1/2/3 or name): "
        )
        raw = input_fn(prompt_text).strip().lower()
        action = _TARGET_DIRTY_ALIASES.get(raw)
        if action is not None:
            return action
        output_fn(
            f"    {help_line('Please choose retry, skip, or halt.', color=color)}",
        )


def _artifact_path(run_dir: Path, decision_id: str) -> Path:
    return run_dir / "commit_decisions" / f"{decision_id}.json"


def _safe_decision_id(run_id: str) -> str:
    safe = _SAFE_ID_RE.sub("_", run_id).strip("._")
    return safe or "run"


@dataclass(frozen=True, slots=True)
class _GitResult:
    ok: bool
    stdout: str = ""
    error: str | None = None


def _git_stdout(cwd: Path, args: list[str]) -> str | None:
    result = _run_git(cwd, args)
    return result.stdout if result.ok else None


def _git_lines(cwd: Path, args: list[str]) -> tuple[str, ...]:
    out = _git_stdout(cwd, args)
    if out is None:
        return ()
    return tuple(line for line in out.splitlines() if line.strip())


def _run_git(cwd: Path, args: list[str]) -> _GitResult:
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=30.0,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except FileNotFoundError as exc:
        return _GitResult(ok=False, error=f"git binary not found: {exc}")
    except OSError as exc:
        return _GitResult(ok=False, error=f"git invocation failed: {exc}")
    except subprocess.TimeoutExpired:
        return _GitResult(ok=False, error="git command timed out after 30s")
    if r.returncode != 0:
        return _GitResult(
            ok=False, error=r.stderr.strip() or r.stdout.strip() or f"rc={r.returncode}",
        )
    return _GitResult(ok=True, stdout=r.stdout)


__all__ = [
    "CommitDeliveryDecision",
    "apply_commit_delivery",
    "render_commit_message_prompt",
    "render_delivery_outcome",
    "resolve_commit_delivery",
]
