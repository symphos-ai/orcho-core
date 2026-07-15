"""Post-release delivery decision executor and read-state (ADR 0100).

This is the sanctioned out-of-band entry point a headless client (MCP, UI,
supervisor) calls to resolve a parked commit-delivery gate. It NEVER
re-implements delivery policy: it replays the persisted gate context through
the engine executors (:func:`pipeline.engine.commit_delivery.resolve_commit_delivery`
+ :func:`~pipeline.engine.commit_delivery.apply_commit_delivery`), so the same
release / verification / dirty guards the live run enforced are re-checked
freshly here.

Two surfaces:

- :func:`decide_delivery` — apply an operator action (``approve`` / ``apply`` /
  ``fix`` / ``skip`` / ``halt``) to a parked gate and finalize the run.
- :func:`delivery_decision_state` — the read-only projection feeding a client's
  gate UI: which actions are currently safe to offer.

Discipline: this module reads/writes durable run artifacts only; it never
prints, renders, or imports a terminal layer. The in-memory ``patch_text`` is
NEVER persisted, so it is never read back here — the delivery context is
reconstructed from the persisted keys (``source_path`` / ``project_path`` /
``baseline_ref`` / ``changed_paths`` / ``untracked_paths``) by re-resolving the
diff against the held worktree.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pipeline.run_state.release_verdict import is_release_blocked
from pipeline.run_state.terminal_outcome import settle_delivery_terminal
from sdk.run_control.types import (
    DeliveryDecisionActionValue,
    DeliveryDecisionResult,
    DeliveryDecisionState,
    DeliveryPrIntent,
)
from sdk.runs import _CWD_DEFAULT, find_run, load_meta

if TYPE_CHECKING:
    from pipeline.engine.commit_delivery import CommitDeliveryDecision

__all__ = ["decide_delivery", "delivery_decision_state"]

_DECIDABLE_STATUSES: frozenset[str] = frozenset({"pending", "fix_requested"})
_DELIVERY_ACTIONS: frozenset[str] = frozenset(
    {"approve", "apply", "fix", "skip", "halt"},
)
# Actions that ship (or would ship) the diff into the project checkout. These
# are the only actions a hard guard (rejected release / required verification)
# refuses; fix / skip / halt never write the diff, so they stay available.
_SHIPPING_ACTIONS: frozenset[str] = frozenset({"approve", "apply"})
# The delivery-done classification (which applied statuses settle ``done``)
# lives ONLY in the reducer (``settle_delivery_terminal`` /
# ``_DELIVERY_DONE_STATUSES``). The SDK keeps no parallel done-set: ``_finalize``
# derives ``accepted`` / ``blocker`` / result ``halt_reason`` from the reducer's
# returned terminal outcome, so the wire shape can never disagree with the settle.
# Map a halted commit-delivery status to the typed ``blocker`` (refusal cause)
# surfaced when the action could not actually execute.
_STATUS_BLOCKERS: dict[str, str] = {
    "target_dirty": "target_dirty",
    "verification_blocked": "verification_blocked",
}
# Contract-valid result status for a refusal: ``not_applicable`` is the
# CommitDeliveryStatus value for a run with no decidable gate.
_NOT_APPLICABLE: str = "not_applicable"
# Stage C delivery-scope enforcement (T4): typed reason a strict-mono scope
# violation surfaces. Mirrors ``pipeline.engine.delivery_scope`` so the blocker
# string is identical on both sides of the wire.
_DELIVERY_SCOPE_BLOCKER: str = "delivery_scope_violation"
# Captured run-patch is corrupt: git can evaluate it but ``git apply --check``
# rejects it (triad ``patch_invalid``). Shipping a corrupt patch would either
# fail or silently mis-apply, so approve/apply are refused via the existing
# ``blocker`` field; fix/skip/halt stay open. Reused on both surfaces so the
# string is identical on the wire.
_PATCH_INVALID_BLOCKER: str = "patch_invalid"


def _commit_delivery_statuses() -> frozenset[str]:
    """The valid ``CommitDeliveryStatus`` values, derived from the engine Literal.

    Derived (not hardcoded) so the SDK refusal status can never drift out of the
    engine's status enum. Lazy-imported to keep this module's top level free of
    the engine layer, mirroring the rest of the delivery executor.
    """
    from typing import get_args

    from pipeline.engine.commit_delivery import CommitDeliveryStatus

    return frozenset(get_args(CommitDeliveryStatus))


def _is_rejected_release_gate(ctx: Any) -> bool:
    """True when ``ctx`` is the auto/non-interactive rejected-release decision.

    The producer (``run.py`` / T2) persists a rejected release that auto-
    delivery refused as a ``not_applicable`` decision carrying a non-empty,
    non-``APPROVED`` ``release_verdict``. That is NOT a true "nothing to
    decide" state — the operator can still request a correction (``fix``) or
    skip / halt — so it must read as a decidable correction gate here rather
    than collapsing to ``no pending delivery gate``. The ordinary defer-parked
    rejected gate (``status='pending'``) is already decidable via
    ``_DECIDABLE_STATUSES`` and is unaffected.
    """
    if not isinstance(ctx, dict):
        return False
    if ctx.get("status") != _NOT_APPLICABLE:
        return False
    return is_release_blocked(ctx.get("release_verdict"), empty_blocks=False)


def _refusal_status(ctx: Any) -> str:
    """A contract-valid ``status`` for the ``no_pending_delivery_gate`` refusal.

    Returns the persisted status when it is a known ``CommitDeliveryStatus`` (an
    already-decided run keeps its informative terminal status, e.g.
    ``committed`` / ``skipped``); otherwise ``not_applicable`` — never an
    out-of-contract value like ``none`` that an MCP mirror switching on the
    delivery-status enum would not recognise.
    """
    if isinstance(ctx, dict):
        status = ctx.get("status")
        if isinstance(status, str) and status in _commit_delivery_statuses():
            return status
    return _NOT_APPLICABLE


def decide_delivery(
    run_id: str,
    action: DeliveryDecisionActionValue,
    note: str | None = None,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> DeliveryDecisionResult:
    """Resolve a parked post-release delivery / correction gate.

    Loads ``meta.commit_delivery`` (the persisted gate context). When no
    decidable gate exists, returns ``accepted=False, blocker=
    'no_pending_delivery_gate'`` — never an exception. Otherwise it replays the
    engine executors with the operator's ``action`` injected, finalizes the run
    (``done`` for approve/apply/skip, a recoverable ``halted`` for fix/halt and
    for hard refusals), and returns the typed outcome.

    Raises:
        ValueError: ``run_id`` empty or ``action`` not one of approve / apply /
            fix / skip / halt — a programming error, distinct from a typed
            business refusal.
    """
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("decide_delivery: run_id must be a non-empty string")
    if action not in _DELIVERY_ACTIONS:
        raise ValueError(
            f"decide_delivery: action must be one of "
            f"{sorted(_DELIVERY_ACTIONS)!r}, got {action!r}"
        )
    if note is not None and not isinstance(note, str):
        raise ValueError("decide_delivery: note must be a string or None")

    ref = find_run(run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd)
    meta = load_meta(ref.run_dir)
    ctx = meta.get("commit_delivery") if isinstance(meta, dict) else None
    resolved_run_id = _resolved_run_id(ctx, ref.run_dir, run_id)

    if (
        not isinstance(ctx, dict)
        or (
            ctx.get("status") not in _DECIDABLE_STATUSES
            and not _is_rejected_release_gate(ctx)
        )
    ):
        return DeliveryDecisionResult(
            run_id=resolved_run_id,
            action=action,
            accepted=False,
            status=_refusal_status(ctx),
            terminal_outcome=_meta_terminal_outcome(meta),
            blocker="no_pending_delivery_gate",
        )

    release_verdict = str(ctx.get("release_verdict") or "")
    release_blocked = is_release_blocked(release_verdict, empty_blocks=False)
    verification_blocked = _verification_blocked(meta, ctx, ref.run_dir)
    scope_blocked = bool(ctx.get("scope_blocker"))
    scope_disclosure = _scope_disclosure(ctx)

    # Hard guards re-checked freshly against durable state. Only the shipping
    # actions are refused; fix / skip / halt stay expressible.
    if action in _SHIPPING_ACTIONS and scope_blocked:
        # Strict-mono delivery-scope violation: shipping into the project
        # checkout is refused. The gate stays reversible — the operator can
        # skip / halt, or re-run the work with an expanded delivery scope.
        return DeliveryDecisionResult(
            run_id=resolved_run_id,
            action=action,
            accepted=False,
            status="not_applicable",
            terminal_outcome="halted",
            blocker=_DELIVERY_SCOPE_BLOCKER,
            scope_disclosure=scope_disclosure,
        )
    if action in _SHIPPING_ACTIONS and verification_blocked:
        return DeliveryDecisionResult(
            run_id=resolved_run_id,
            action=action,
            accepted=False,
            status="verification_blocked",
            terminal_outcome="halted",
            blocker="verification_blocked",
        )
    if action in _SHIPPING_ACTIONS and release_blocked:
        return DeliveryDecisionResult(
            run_id=resolved_run_id,
            action=action,
            accepted=False,
            status="not_applicable",
            terminal_outcome="halted",
            blocker="release_blocked",
        )
    if action == "skip" and release_blocked:
        # ``skip`` does not apply delivery, yet it settles to ``skipped`` ∈ the
        # reducer's delivery-done statuses — which would call ``mark_run_done``
        # and clear the ``final_acceptance_rejected`` halt, turning a rejected
        # release into a
        # silent clean ``done`` with no ``delivery_override`` marker. That is
        # exactly the silent-success hole ADR 0106 closes, so a rejected release
        # refuses ``skip`` too: the operator keeps ``fix`` (correct + re-review)
        # and ``halt`` (give up, stays recoverable), and the only path to
        # ``done`` remains an actually-applied operator override (TTY dialog,
        # ADR 0069), which always records the durable override marker.
        return DeliveryDecisionResult(
            run_id=resolved_run_id,
            action=action,
            accepted=False,
            status="not_applicable",
            terminal_outcome="halted",
            blocker="release_blocked",
        )
    patch_invalid, patch_path = _patch_invalid_blocked(meta, ctx, ref.run_dir)
    if action in _SHIPPING_ACTIONS and patch_invalid:
        return DeliveryDecisionResult(
            run_id=resolved_run_id,
            action=action,
            accepted=False,
            status="not_applicable",
            terminal_outcome="halted",
            blocker=_PATCH_INVALID_BLOCKER,
        )

    decision = _reresolve(ctx, resolved_run_id, ref.run_dir, action)
    if decision.status != "pending":
        # The worktree no longer yields a deliverable diff (someone delivered
        # it already, or the held checkout was cleaned up). Surface a typed
        # refusal rather than silently doing nothing.
        blocker = (
            "already_delivered" if decision.status in ("no_diff", "disabled")
            else "stale_run"
        )
        return DeliveryDecisionResult(
            run_id=resolved_run_id,
            action=action,
            accepted=False,
            status=decision.status,
            terminal_outcome=_meta_terminal_outcome(meta),
            blocker=blocker,
        )

    from pipeline.engine.commit_delivery import apply_commit_delivery

    applied = apply_commit_delivery(
        decision,
        run_dir=ref.run_dir,
        commit_config=_replay_commit_config(ctx),
        no_interactive=True,
    )

    return _finalize(
        meta,
        ref.run_dir,
        resolved_run_id,
        action,
        applied,
        scope_disclosure=scope_disclosure,
    )


def delivery_decision_state(
    run_id: str,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
    meta: dict[str, Any] | None = None,
) -> DeliveryDecisionState:
    """Project a parked delivery gate as the actions a client may offer.

    Strictly read-only. ``decidable`` is ``True`` only for a run parked on a
    pending delivery / correction gate. ``available_actions`` lists exactly the
    actions core considers safe right now (a rejected release or a required
    verification gap removes ``approve`` / ``apply``); ``blocked_actions`` lists
    those a hard guard currently refuses.

    ``meta`` is the provider seam shared with :func:`run_diagnosis` /
    :func:`recovery_lineage`: when supplied it is used verbatim in place of the
    on-disk ``meta.json`` read, so an embedder's supervisor-merged gate context
    (a ``commit_delivery`` block resolved out-of-band) classifies the gate
    instead of a stale on-disk snapshot. The durable run_dir is still resolved
    via :func:`find_run` for the fresh artifact re-checks (verification receipt,
    patch apply) that read held files rather than the meta snapshot.
    """
    ref = find_run(run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd)
    if not isinstance(meta, dict):
        meta = load_meta(ref.run_dir)
    ctx = meta.get("commit_delivery") if isinstance(meta, dict) else None
    resolved_run_id = _resolved_run_id(ctx, ref.run_dir, run_id)

    if (
        not isinstance(ctx, dict)
        or (
            ctx.get("status") not in _DECIDABLE_STATUSES
            and not _is_rejected_release_gate(ctx)
        )
    ):
        return DeliveryDecisionState(
            run_id=resolved_run_id,
            decidable=False,
            kind="none",
            reason="no pending delivery gate",
        )

    status = str(ctx.get("status"))
    release_verdict = str(ctx.get("release_verdict") or "")
    release_blocked = is_release_blocked(release_verdict, empty_blocks=False)
    scope_disclosure = _scope_disclosure(ctx)

    # A correction whose ``fix`` decision was already taken
    # (``status='fix_requested'``) or that dead-ended on an auto-refused rejected
    # release (:func:`_is_rejected_release_gate`) has NO meaningful in-gate next
    # step: repeating ``fix`` is inert and a bare resume cannot advance it. The
    # actionable path is an ordinary follow-up against the retained worktree, so
    # advertise only ``halt`` (give up) and route the client to that follow-up
    # via ``reason`` (a field MCP already maps verbatim). The freshly defer-parked
    # rejected gate (``status='pending'`` + ``release_blocked``) is unaffected —
    # there ``fix`` is still the actionable operator decision below.
    if status == "fix_requested" or _is_rejected_release_gate(ctx):
        all_actions = ("fix", "approve", "apply", "skip", "halt")
        return DeliveryDecisionState(
            run_id=resolved_run_id,
            decidable=True,
            kind="correction",
            available_actions=("halt",),
            blocked_actions=tuple(a for a in all_actions if a != "halt"),
            default_action=None,
            reason=_followup_correction_reason(resolved_run_id, ref.run_dir),
            scope_disclosure=scope_disclosure,
        )

    verification_blocked = _verification_blocked(meta, ctx, ref.run_dir)
    scope_blocked = bool(ctx.get("scope_blocker"))
    patch_invalid, patch_invalid_path = _patch_invalid_blocked(meta, ctx, ref.run_dir)
    # ``fix_requested`` is handled above (follow-up required); the only correction
    # that reaches here is the freshly defer-parked rejected gate.
    is_correction = release_blocked

    if is_correction:
        kind = "correction"
        all_actions = ("fix", "approve", "apply", "skip", "halt")
    else:
        kind = "delivery"
        all_actions = ("approve", "apply", "skip", "halt")

    blocked: list[str] = []
    if release_blocked or verification_blocked or scope_blocked or patch_invalid:
        blocked = [a for a in all_actions if a in _SHIPPING_ACTIONS]
    if release_blocked and "skip" in all_actions:
        # A rejected release also refuses ``skip``: it settles to ``skipped`` ∈
        # done-statuses without applying delivery, which would present the
        # rejected run as a clean ``done`` (no override marker). Only ``fix`` /
        # ``halt`` remain — neither yields a clean success. (ADR 0106.)
        blocked.append("skip")
    available = tuple(a for a in all_actions if a not in blocked)

    if is_correction:
        default_action: str | None = "fix"
    elif "approve" in available:
        default_action = "approve"
    else:
        default_action = "skip" if "skip" in available else None

    # Precedence mirrors the decide_delivery guards: release, then verification,
    # then delivery scope. The scope disclosure rides on the state regardless so
    # a client can show exactly which sibling files triggered the block.
    reason = None
    if release_blocked:
        # ``skip`` is blocked on a rejected release (it would settle a clean
        # ``done`` without applied delivery), so the reason must advertise only
        # the actually-available paths: ``fix`` (correct + re-review) or
        # ``halt`` — never ``skip``. (ADR 0106.)
        reason = f"release verdict {release_verdict!r} — fix or halt only"
        release_summary = str(ctx.get("release_summary") or "").strip()
        if release_summary:
            reason = f"{reason}; {release_summary}"
        blocker_reason = _release_blocker_reason(ctx)
        if blocker_reason:
            reason = f"{reason}; {blocker_reason}"
    elif verification_blocked:
        reason = "required verification incomplete — receipt or waiver needed"
    elif scope_blocked:
        reason = (
            "delivery_scope_violation — sibling-repo changes outside strict "
            "mono scope; expand the delivery scope or skip / halt"
        )
    elif patch_invalid:
        reason = _patch_invalid_reason(patch_invalid_path)

    return DeliveryDecisionState(
        run_id=resolved_run_id,
        decidable=True,
        kind=kind,
        available_actions=available,
        blocked_actions=tuple(blocked),
        default_action=default_action,
        reason=reason,
        scope_disclosure=scope_disclosure,
    )


# ── internals ────────────────────────────────────────────────────────────────


def _scope_disclosure(ctx: Any) -> tuple[str, ...]:
    """Per-alias companion files (``[alias]/rel``) from a parked gate context.

    Projects the enriched companion disclosure (ADR 0107 / T3) into the
    backward-compatible ``[alias]/rel`` string list the SDK has always carried.
    Two durable sources off the gate context (``meta.commit_delivery``) are
    merged:

    1. the legacy ``scope_disclosure`` list — the strict-mono *violation*
       siblings (the files that tripped the block), kept FIRST and in their
       original order so an existing consumer sees a byte-identical prefix;
    2. the T1 ``scope_companions`` blocks — every declared companion repo's
       ``changed_paths`` (``dirty`` and observably ``committed`` alike), appended
       (sorted, de-duplicated) so the projection now discloses the full companion
       file set, not only the blocking siblings.

    The per-repo *typed state* (``dirty`` / ``committed`` / ``planned_requirement``)
    and full repo path are NOT folded into these strings — they live in the
    core-durable ``multi_project_delivery`` evidence block. Keeping the wire shape
    a plain ``tuple[str, ...]`` of ``[alias]/rel`` means existing MCP consumers
    (which read ``scope_disclosure`` as ``list[str]``) are unaffected. Empty tuple
    when absent or malformed. Never raises.
    """
    if not isinstance(ctx, dict):
        return ()
    seen: set[str] = set()
    out: list[str] = []

    def _add(path: Any) -> None:
        if isinstance(path, str) and path and path not in seen:
            seen.add(path)
            out.append(path)

    raw = ctx.get("scope_disclosure")
    if isinstance(raw, (list, tuple)):
        for path in raw:
            _add(path)

    companions = ctx.get("scope_companions")
    if isinstance(companions, (list, tuple)):
        companion_paths: list[str] = []
        for companion in companions:
            if not isinstance(companion, dict):
                continue
            changed = companion.get("changed_paths")
            if isinstance(changed, (list, tuple)):
                companion_paths.extend(
                    p for p in changed if isinstance(p, str) and p
                )
        for path in sorted(companion_paths):
            _add(path)

    return tuple(out)


def _release_blockers(ctx: Any) -> tuple[dict[str, Any], ...]:
    """Structured release blockers preserved on the parked delivery context."""
    if not isinstance(ctx, dict):
        return ()
    raw = ctx.get("release_blockers")
    if not isinstance(raw, (list, tuple)):
        return ()
    out: list[dict[str, Any]] = []
    for blocker in raw:
        if not isinstance(blocker, dict):
            continue
        normalized = {str(key): value for key, value in blocker.items()}
        if normalized:
            out.append(normalized)
    return tuple(out)


def _release_blocker_reason(ctx: Any) -> str:
    """Compact human-readable blocker detail for delivery decision state."""
    parts: list[str] = []
    for blocker in _release_blockers(ctx):
        blocker_id = str(blocker.get("id") or "").strip()
        title = str(
            blocker.get("title")
            or blocker.get("why_blocks_release")
            or blocker.get("body")
            or ""
        ).strip()
        required_fix = str(blocker.get("required_fix") or "").strip()

        label = f"{blocker_id}: {title}" if blocker_id and title else blocker_id or title
        if not label:
            label = "release blocker"
        if required_fix:
            label = f"{label} (required fix: {required_fix})"
        parts.append(label)
    if not parts:
        return ""
    return "release blockers: " + "; ".join(parts)


def _followup_correction_reason(run_id: str, run_dir: Path) -> str:
    """Next-step pointer for a fix-marked / rejected-dead-end correction gate.

    The actionable next step is NOT a same-run resume or a repeated ``fix`` — it
    is an ordinary correction follow-up against the retained worktree. MCP maps
    this ``reason`` verbatim; the typed launch request supplies the required
    operator comment and does not replay ``diff.patch``.
    """
    reason = (
        f"correction requested — next step is an ordinary follow-up "
        f"(orcho_run_resume run_id={run_id} with operator comment); a bare resume or a repeated "
        f"fix is inert"
    )
    return reason


def _resolved_run_id(ctx: Any, run_dir: Path, fallback: str) -> str:
    if isinstance(ctx, dict):
        rid = ctx.get("run_id")
        if isinstance(rid, str) and rid:
            return rid
    return run_dir.name or fallback


def _patch_invalid_blocked(
    meta: dict[str, Any], ctx: dict[str, Any], run_dir: Path,
) -> tuple[bool, str | None]:
    """Whether the captured run patch is corrupt (triad ``patch_invalid``).

    Returns ``(blocked, patch_path)``. The source of truth is the durable
    ``meta.diff_patch`` block persisted at finalization (T2); when that block is
    absent — e.g. a gate parked before the durable block existed — it falls
    back to a fresh :func:`check_diff_patch_apply` against the parked baseline,
    mirroring how :func:`_verification_blocked` re-derives from durable state.

    Only ``patch_invalid`` blocks shipping. ``patch_missing`` (a legitimate
    no-diff run or an absent artifact) and ``patch_valid`` / ``patch_unknown``
    never block — a degraded check is not conflated with a real apply failure.
    Never raises.
    """
    block = meta.get("diff_patch") if isinstance(meta, dict) else None
    if isinstance(block, dict) and isinstance(block.get("status"), str):
        patch_path = block.get("patch_path")
        return (
            block["status"] == _PATCH_INVALID_BLOCKER,
            patch_path if isinstance(patch_path, str) else None,
        )

    # No durable block: re-derive freshly against the parked baseline.
    patch_file = run_dir / "diff.patch"
    baseline_ref = ctx.get("baseline_ref") if isinstance(ctx, dict) else None
    if not patch_file.is_file() or not baseline_ref:
        return False, None
    source = ctx.get("source_path") or ctx.get("project_path")
    if not isinstance(source, str) or not source:
        return False, None
    try:
        from pipeline.engine.diff_apply_check import (
            check_diff_patch_apply,
            diff_patch_triad,
        )

        result = check_diff_patch_apply(
            source,
            patch_path=patch_file,
            baseline_ref=str(baseline_ref),
        )
        return diff_patch_triad(result) == _PATCH_INVALID_BLOCKER, str(patch_file)
    except Exception:  # noqa: BLE001 — a read-only check must never break a decision
        return False, None


def _patch_invalid_reason(patch_path: str | None) -> str:
    """Actionable reason for a ``patch_invalid`` shipping block.

    Names the corrupt artifact and the recovery path so an operator (or a
    client UI) knows the gate is reversible: the patch failed
    ``git apply --check`` against the delivery baseline.
    """
    where = patch_path or "the captured diff.patch"
    return (
        f"patch_invalid — captured patch at {where} fails git apply --check "
        "against the delivery baseline; recover from worktree or rerun"
    )


def _verification_blocked(
    meta: dict[str, Any], ctx: dict[str, Any], run_dir: Path,
) -> bool:
    """Whether a required verification gap still blocks shipping, re-checked fresh.

    The persisted ``verification_*`` fields are a snapshot from park time; a
    receipt materialized or a durable waiver recorded *after* parking must
    unblock delivery, so this re-derives the assessment from durable state
    rather than trusting the stale snapshot. When the contract cannot be
    rebuilt (no recorded project, plugin load failure) it conservatively falls
    back to the persisted fields — an unverifiable gate keeps its recorded
    block rather than silently shipping.
    """
    assessment, reconstructed = _reassess_delivery_verification(meta, ctx, run_dir)
    if reconstructed:
        return assessment is not None and assessment.blocking
    return _persisted_verification_blocked(ctx)


def _persisted_verification_blocked(ctx: dict[str, Any]) -> bool:
    """True when the persisted gate recorded a ``require``-policy block.

    Fallback used only when the contract cannot be reconstructed: the full
    assessment object is not persisted, so the hard block is re-asserted from
    the recorded blockers rather than re-derived from the contract.
    """
    if ctx.get("verification_policy") != "require":
        return False
    return any(
        ctx.get(key)
        for key in ("verification_missing", "verification_failed", "verification_stale")
    )


def _reassess_delivery_verification(
    meta: dict[str, Any], ctx: dict[str, Any], run_dir: Path,
) -> tuple[Any, bool]:
    """Re-derive the delivery verification assessment from durable state.

    Returns ``(assessment, reconstructed)``. ``reconstructed=True`` means the
    contract was rebuilt and ``assessment`` is the fresh
    :class:`~pipeline.verification_delivery.DeliveryVerificationAssessment`
    (or ``None`` for a policy ``off`` / no-contract gate). ``reconstructed=False``
    means the contract could not be rebuilt and the caller must fall back to the
    persisted fields. Never raises — any failure degrades to ``(None, False)``.

    The contract / placeholders / durable waivers are reconstructed the same way
    the read-only verification surfaces do (project plugin + persisted session),
    so the require-block re-check honours receipts materialized and waivers
    recorded after the gate was parked.
    """
    try:
        project = meta.get("project")
        source_path = ctx.get("source_path")
        if not isinstance(project, str) or not project:
            return None, False
        if not isinstance(source_path, str) or not source_path:
            return None, False

        from core.infra.platform import workspace_dir as resolve_workspace
        from pipeline.plugins import load_plugin
        from pipeline.verification_contract import (
            VerificationContract,
            placeholder_context_for,
        )
        from pipeline.verification_delivery import assess_delivery_verification
        from pipeline.verification_waiver import WAIVER_KEY

        contract = VerificationContract.from_plugin(load_plugin(project))
        pctx = placeholder_context_for(
            contract,
            checkout=source_path,
            project=project,
            workspace=str(resolve_workspace() or project),
            run_dir=str(run_dir),
        )
        # Durable waivers live on the persisted session (meta); thread them into
        # ``extras`` so ``collect_gate_waivers`` (extras-only) sees an operator
        # waiver recorded after parking.
        extras: dict[str, Any] = {}
        waivers = meta.get(WAIVER_KEY)
        if waivers is not None:
            extras[WAIVER_KEY] = waivers

        assessment = assess_delivery_verification(
            contract,
            run_dir,
            pctx,
            extras,
            diff_cwd=source_path,
            baseline_ref=str(ctx.get("baseline_ref") or "HEAD"),
        )
        return assessment, True
    except Exception:  # noqa: BLE001 — re-assessment must never break a decision
        return None, False


def _reresolve(
    ctx: dict[str, Any],
    run_id: str,
    run_dir: Path,
    action: DeliveryDecisionActionValue,
) -> CommitDeliveryDecision:
    """Replay the engine resolve against the held worktree with ``action``.

    The patch is recomputed fresh from ``source_path`` + ``baseline_ref`` — the
    persisted (non-serialised) ``patch_text`` is never read. ``decision_mode``
    is forced to ``auto`` so the resolve produces an applicable pending decision
    (not another parked one); ``verification_gate=None`` because the require
    block, when present, was already enforced freshly by the caller above.
    """
    from pipeline.engine.commit_delivery import resolve_commit_delivery

    release_verdict = str(ctx.get("release_verdict") or "")
    release_blockers = _release_blockers(ctx)
    session: dict[str, Any] = {"status": "done"}
    if release_verdict:
        release_entry: dict[str, Any] = {
            "verdict": release_verdict,
            "short_summary": str(ctx.get("release_summary") or ""),
        }
        if release_blockers:
            release_entry["release_blockers"] = list(release_blockers)
        session["phases"] = {
            "final_acceptance": release_entry,
        }

    return resolve_commit_delivery(
        project_dir=Path(str(ctx.get("project_path"))),
        source_worktree=Path(str(ctx.get("source_path"))),
        run_dir=run_dir,
        run_id=run_id,
        session=session,
        commit_config=_replay_commit_config(ctx),
        no_interactive=True,
        baseline_ref=str(ctx.get("baseline_ref") or "HEAD"),
        decision_action=action,
        verification_gate=None,
    )


def _replay_commit_config(ctx: dict[str, Any]) -> dict[str, Any]:
    """Commit config for replaying a parked gate.

    Delivery-relevant fields are pinned from the PERSISTED gate context, not the
    current config: a run parked with ``add_untracked=false`` must not start
    delivering untracked files just because the live ``AppConfig`` default later
    flipped to ``true``. ``decision_mode`` is forced to ``auto`` so the replay
    produces an applicable pending decision rather than re-parking it.
    """
    from core.infra import config

    return {
        **config.AppConfig.load().commit,
        "decision_mode": "auto",
        "add_untracked": bool(ctx.get("include_untracked")),
    }


def _finalize(
    meta: dict[str, Any],
    run_dir: Path,
    run_id: str,
    action: DeliveryDecisionActionValue,
    applied: CommitDeliveryDecision,
    *,
    scope_disclosure: tuple[str, ...] = (),
) -> DeliveryDecisionResult:
    """Persist the applied decision, settle the run, build the typed result.

    ``scope_disclosure`` is the enriched companion disclosure projected from the
    durable parked gate context (``[alias]/rel`` strings, ADR 0107 / T3). It is
    carried verbatim onto the result so an accepted decision that left a declared
    companion repo behind still surfaces the companion file set; empty for a
    single-repo run, keeping non-companion results byte-identical.
    """
    from pipeline.engine.commit_delivery import COMMIT_DELIVERY_HALT_REASONS

    status = applied.status
    # Capture the durable companion disclosure BEFORE overwriting
    # ``commit_delivery`` — the parked gate context still carries it here, and
    # ``applied.to_dict()`` would not re-derive it on the SDK replay (the replay
    # session has no ``auto_detect`` scope block).
    companion_blocks = _companion_disclosure_blocks(meta, applied)
    meta["commit_delivery"] = applied.to_dict()
    # F1 (review retry): keep the durable ``multi_project_delivery`` block in sync
    # with the applied result. The defer flow parked it with
    # ``primary_status='pending'``; after an operator approve the primary really
    # commits, so the durable companion disclosure must flip to the terminal
    # status (committed / applied_uncommitted / skipped) rather than read stale
    # ``pending`` — otherwise finalization / evidence would present the delivery
    # as complete while a still-dirty companion sits behind a stale caveat.
    _sync_multi_project_delivery(meta, status, companion_blocks)

    artifact_paths: list[str] = []
    if applied.artifact_path is not None:
        artifact_paths.append(str(applied.artifact_path))
    patch = run_dir / "diff.patch"
    if patch.is_file():
        artifact_paths.append(str(patch))

    # The SDK resolves only the delivery facts the reducer CONSUMES — the
    # ``halt_reason`` (from ``COMMIT_DELIVERY_HALT_REASONS``) and the
    # ``halted_at`` timestamp (the reducer never computes one) — and hands them
    # in ready-made. The reducer is the SINGLE owner of the delivery-done
    # classification and the done↔halted flip; the SDK keeps NO parallel
    # done-set. ``accepted`` / ``blocker`` / the result ``halt_reason`` are
    # derived AFTER the settle from the reducer's returned terminal outcome, so
    # the wire shape can never disagree with the reducer's done/halted decision
    # even if the reducer's done-set later changes (ADR 0115 slice 3b). The done
    # branch leaves the just-overwritten ``commit_delivery`` intact (the
    # canonical eviction never touches it).
    halt_reason = COMMIT_DELIVERY_HALT_REASONS.get(status, "commit_delivery_failed")
    halted_at = datetime.now(UTC).isoformat(timespec="seconds")

    terminal_outcome = settle_delivery_terminal(
        meta,
        applied_status=status,
        halt_reason=halt_reason,
        halted_at=halted_at,
    )

    if terminal_outcome == "done":
        accepted = True
        result_halt_reason: str | None = None
        blocker: str | None = None
    else:
        # ``fix`` / ``halt`` are accepted operator choices that legitimately
        # leave the run halted; an executor failure (commit_failed / dirty) is
        # a refusal.
        accepted = status in ("fix_requested", "halted")
        result_halt_reason = halt_reason
        blocker = _STATUS_BLOCKERS.get(status)

    _write_meta(run_dir, meta)

    return DeliveryDecisionResult(
        run_id=run_id,
        action=action,
        accepted=accepted,
        status=status,
        terminal_outcome=terminal_outcome,
        halt_reason=result_halt_reason,
        artifact_paths=tuple(artifact_paths),
        commit_sha=applied.commit_sha,
        # ADR 0119 — additive delivery-branch projection. ``commit_sha`` above
        # already carries the fill rule (populated for a commit onto the target
        # checkout, ``None`` for a pure worktree_branch publish); ``delivery_branch``
        # + ``pr_intent`` surface the published-branch outcome alongside it.
        delivery_branch=applied.delivery_branch,
        pr_intent=_pr_intent_projection(applied.pr_intent),
        # ADR 0121 — provider-neutral opened-PR URL from the applied decision;
        # ``None`` for any delivery without an opened PR.
        pr_url=applied.pr_url,
        blocker=blocker,
        # The SDK never starts a correction follow-up synchronously
        # (``drive_correction_followups`` is TTY-only): a ``fix`` here only
        # marks the run correction-ready, so the follow-up id is always None.
        followup_run_id=None,
        # ADR 0107 / T3: enriched companion disclosure from the durable parked
        # gate, so an accepted decision still names a companion repo left behind.
        scope_disclosure=scope_disclosure,
    )


def _pr_intent_projection(intent: Any) -> DeliveryPrIntent | None:
    """Project the core delivery PR-intent onto the SDK boundary type (ADR 0119).

    ``intent`` is the core
    :class:`pipeline.engine.delivery_branch.DeliveryPrIntent` carried on the
    applied decision (``None`` on any commit-onto-checkout / ``bypass`` path).
    Mapped field-for-field onto the public SDK :class:`DeliveryPrIntent` so the
    wire surface owns its own typed shape rather than leaking the engine type.
    """
    if intent is None:
        return None
    return DeliveryPrIntent(
        branch=intent.branch,
        base=intent.base,
        title=intent.title,
        suggested_command=intent.suggested_command,
    )


def _companion_disclosure_blocks(
    meta: dict[str, Any], applied: CommitDeliveryDecision,
) -> list[dict[str, Any]]:
    """The durable per-repo companion disclosure to mirror into the meta block.

    Resolves the companion ``{alias, path, state, changed_paths}`` dicts from the
    most authoritative durable source available, in priority order:

    1. ``applied.scope_companions`` — present only when the replay re-derived the
       delivery scope (it normally does not on the SDK path, whose replay session
       carries no ``auto_detect`` block), so this is the rare fresh case;
    2. the existing ``meta['multi_project_delivery'].companions`` — the block the
       defer flow parked alongside the gate (the common SDK case);
    3. ``meta['commit_delivery'].scope_companions`` — the parked gate context,
       still un-overwritten when this runs.

    Returns ``[]`` for a single-repo run (no companion in any source), which keeps
    the ``multi_project_delivery`` block absent and the result byte-identical.
    Never raises.
    """
    companions = getattr(applied, "scope_companions", ()) or ()
    if companions:
        return [c.to_dict() for c in companions]

    existing = meta.get("multi_project_delivery")
    if isinstance(existing, dict):
        blocks = existing.get("companions")
        if isinstance(blocks, list) and blocks:
            return [b for b in blocks if isinstance(b, dict)]

    ctx = meta.get("commit_delivery")
    if isinstance(ctx, dict):
        blocks = ctx.get("scope_companions")
        if isinstance(blocks, list) and blocks:
            return [b for b in blocks if isinstance(b, dict)]

    return []


def _sync_multi_project_delivery(
    meta: dict[str, Any], primary_status: str, companions: list[dict[str, Any]],
) -> None:
    """Mirror the applied primary status into the durable companion block.

    Mirrors :func:`pipeline.project.run._record_multi_project_delivery`: a no-op
    (block left absent, single-repo result byte-identical) when no companion was
    declared; otherwise rewrites ``meta['multi_project_delivery']`` with the now
    terminal ``primary_status`` and the preserved per-repo disclosure so the
    durable evidence surface matches the committed/applied reality instead of the
    parked ``pending`` snapshot.
    """
    if not companions:
        return
    meta["multi_project_delivery"] = {
        "primary_status": primary_status,
        "companions": companions,
    }


def _meta_terminal_outcome(meta: dict[str, Any]) -> str:
    return "done" if meta.get("status") == "done" else "halted"


def _write_meta(run_dir: Path, meta: dict[str, Any]) -> None:
    (run_dir / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
