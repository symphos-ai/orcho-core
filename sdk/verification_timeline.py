# SPDX-License-Identifier: Apache-2.0
"""sdk.verification_timeline — read-only typed projection of a run's official
verification gates from durable artifacts only.

This is the SDK-level source of truth for "what do the verification gates of
this run look like". Unlike the live render model in
:mod:`pipeline.project.verification_timeline` (which reads the in-memory
``state.extras``), this projection reconstructs everything it needs from the
*durable* run directory:

* the run/project/contract are re-resolved exactly as :mod:`sdk.verify` does
  (:func:`sdk.verify._resolve_run_project_contract` + ``_checkout_dir_for_meta``
  + ``pipeline.verification_contract.placeholder_context_for``);
* per-gate status is the read-only classification of on-disk command receipts
  (:func:`pipeline.verification_readiness.classify_required_receipts`) plus the
  effective delivery policy
  (:func:`pipeline.verification_readiness.effective_policy_by_command`);
* the auto-run mirror is read from the durable ``meta['phase_log']`` (the
  ``verification_autorun`` per-phase mirror persisted alongside the run).

Boundary discipline (ADR 0021): returns a JSON-able typed dataclass
(serialise via :func:`sdk.to_jsonable`), executes nothing, writes nothing, and
never parses terminal banners. Only :class:`~sdk.errors.RunNotFound` propagates;
every other git / IO / import failure degrades to an empty / ``None`` field.

Status enum (F1/P2): each gate's ``status`` is EXACTLY one of
``PASS`` / ``FAIL`` / ``MISSING`` / ``STALE`` / ``SKIPPED`` / ``FRESH`` — there
is deliberately no ``MANUAL`` value. A manual / operator-only gate is surfaced as
``status=SKIPPED`` with ``policy='manual_only'`` and membership in the aggregate
``manual_only`` set, never as missing and never carrying a ``rerun_hint``.

Core gap (criterion 6): the per-firing scheduled-hook trail
(``verification_gate_events``) is NOT persisted durably, so this projection
cannot reconstruct a per-scheduled-hook firing timeline. ``scheduled_trail``
is therefore always reported absent; see :data:`SCHEDULED_TRAIL_GAP`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from sdk.runs import _CWD_DEFAULT, find_run, load_meta
from sdk.verify import _checkout_dir_for_meta, _resolve_run_project_contract

if TYPE_CHECKING:
    from pipeline.verification_contract import VerificationContract

# The six — and only six — gate status values. ``MANUAL`` is intentionally
# absent: a manual/operator-only gate is ``SKIPPED`` with ``policy='manual_only'``.
GateStatus = Literal["PASS", "FAIL", "MISSING", "STALE", "SKIPPED", "FRESH"]

# Exact, ordered tuple of the legal status values — mirrored by the MCP Pydantic
# Literal and docs/mcp_schema.json so the enum stays consistent end-to-end.
GATE_STATUSES: tuple[GateStatus, ...] = (
    "PASS",
    "FAIL",
    "MISSING",
    "STALE",
    "SKIPPED",
    "FRESH",
)

# The exact core follow-up gap (criterion 6): the per-firing scheduled-gate
# decision trail is live-only (``extras['verification_gate_events']``), never
# mirrored to a durable artifact, so a per-scheduled-hook firing timeline cannot
# be reconstructed here. Core must mirror that trail before MCP can show it.
# Terminal banners are NOT a source of truth and are never parsed.
SCHEDULED_TRAIL_GAP = (
    "per-firing scheduled-gate events (verification_gate_events) are not "
    "persisted durably; core must mirror the gate_repair routing-decision trail "
    "to a durable run artifact before a per-scheduled-hook firing timeline can be "
    "projected. Terminal banners are not parsed as a source of truth."
)


@dataclass(frozen=True, slots=True)
class GateProjection:
    """One required delivery command's durable gate state.

    ``status`` is EXACTLY one of :data:`GATE_STATUSES` (no ``MANUAL``). A
    manual/operator-only gate is ``SKIPPED`` with ``policy='manual_only'``.
    ``inherited`` is true when the deciding receipt came from a parent run
    (``source_run_id`` set and different from the current run). ``searched_run_dirs``
    and ``rerun_hint`` are populated ONLY for a non-present required gate that is
    not manual (status ``MISSING`` / ``STALE`` / ``FAIL``); present and manual
    gates carry neither.

    ``detail`` is a human-readable operator note, empty for an ordinary gate. It
    is populated when an environment-provenance break downgrades the gate to
    ``FAIL`` (ADR 0125): it names the failing ``verification_environment`` check
    and its expected/actual, e.g. ``"pipeline_import: expected <X> actual <Y>"``,
    while ``receipt_path`` then points at that phase receipt.
    """

    command: str
    env: str
    hook: str
    policy: str
    required: bool
    status: GateStatus
    receipt_path: str = ""
    source_run_id: str = ""
    inherited: bool = False
    stale_reason: str = ""
    searched_run_dirs: tuple[str, ...] = ()
    rerun_hint: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True, slots=True)
class AutorunEvent:
    """One durable auto-run firing mirrored from ``meta['phase_log']``.

    ``ran_pass`` is the executed commands minus the trail's authoritative
    ``failed`` set; ``ran_fail`` is that failed set. ``skipped_fresh`` were already
    present (fresh receipt, not executed this firing), ``skipped_manual`` were
    intentionally not auto-run. ``receipt_paths`` are the durable env/command
    receipt paths this firing wrote.
    """

    phase: str
    source: str
    hook_label: str
    ran_pass: tuple[str, ...] = ()
    ran_fail: tuple[str, ...] = ()
    skipped_fresh: tuple[str, ...] = ()
    skipped_manual: tuple[str, ...] = ()
    receipt_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class VerificationTimelineProjection:
    """JSON-able read-only projection of a run's official verification gates.

    ``gates`` is the per-gate detail (the heart of the projection). The
    aggregate ``residual_*`` / ``manual_only`` / ``inherited`` / ``searched_run_dirs``
    / ``suggested_commands`` / ``policy_by_command`` fields mirror the readiness
    summary for the summary view and backward compatibility. ``autorun_events`` is
    the durable auto-run mirror. ``scheduled_trail_available`` is always ``False``
    (see :data:`SCHEDULED_TRAIL_GAP`).
    """

    run_id: str
    project: str = ""
    has_contract: bool = False
    gates: tuple[GateProjection, ...] = ()
    env_statuses: tuple[tuple[str, bool], ...] = ()
    residual_missing: tuple[str, ...] = ()
    residual_stale: tuple[str, ...] = ()
    residual_failed: tuple[str, ...] = ()
    manual_only: tuple[str, ...] = ()
    inherited: tuple[str, ...] = ()
    searched_run_dirs: tuple[str, ...] = ()
    suggested_commands: tuple[str, ...] = ()
    policy_by_command: tuple[tuple[str, str], ...] = ()
    autorun_events: tuple[AutorunEvent, ...] = ()
    scheduled_trail_available: bool = False
    scheduled_trail_gap: str = SCHEDULED_TRAIL_GAP


# Delivery-relevant (hook, phase) positions whose gate a final-acceptance
# reviewer treats as official — a deliberate mirror of
# ``pipeline.verification_readiness._DELIVERY_HOOKS`` so the per-gate hook label
# matches ``_gate_status_lines`` exactly (kept in sync by the unit tests).
_DELIVERY_HOOKS: tuple[tuple[str, str], ...] = (
    ("after_phase", "implement"),
    ("before_delivery", ""),
)


def _hook_labels(contract: VerificationContract, plan: Any) -> dict[str, str]:
    """``command -> hook label`` for the delivery-relevant scheduled gates.

    Built from the resolved ``plan`` when one exists (entry order), else from the
    raw ``contract.schedule`` — the same source and labelling
    (``hook(phase)``) as :func:`pipeline.verification_readiness._gate_status_lines`.
    Commands with no delivery-hook schedule (e.g. a static ``required`` command)
    are simply absent and default to ``""`` at the call site.
    """
    labels: dict[str, str] = {}
    if plan is not None:
        for entry in getattr(plan, "entries", ()) or ():
            hook = getattr(entry, "hook", "")
            phase = getattr(entry, "phase", "")
            if (hook, phase) not in _DELIVERY_HOOKS:
                continue
            command = getattr(entry, "command", "")
            label = hook + (f"({phase})" if phase else "")
            if command:
                labels.setdefault(command, label)
        return labels
    for entry in contract.schedule:
        if (entry.hook, entry.phase) not in _DELIVERY_HOOKS:
            continue
        label = entry.hook + (f"({entry.phase})" if entry.phase else "")
        for command in entry.commands or tuple(sorted(contract.commands)):
            labels.setdefault(command, label)
    return labels


def _command_env(contract: VerificationContract, command: str) -> str:
    spec = contract.commands.get(command) or {}
    return str(spec.get("env") or getattr(contract, "default_env", "") or "")


# Per-phase auto-run mirror sources, in durable read order: the top-level
# ``meta['phase_log']`` (current persistence) then a legacy ``meta['session']
# ['phase_log']`` nesting, so an older meta shape is still seen.
def _phase_log(meta: Mapping[str, Any]) -> Mapping[str, Any]:
    phase_log = meta.get("phase_log")
    if isinstance(phase_log, Mapping):
        return phase_log
    session = meta.get("session")
    if isinstance(session, Mapping):
        nested = session.get("phase_log")
        if isinstance(nested, Mapping):
            return nested
    return {}


def _as_names(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    return ()


def _autorun_events(meta: Mapping[str, Any]) -> tuple[AutorunEvent, ...]:
    """Durable auto-run mirror from ``meta['phase_log'][*]['verification_autorun']``.

    ``ran_pass`` excludes the trail's authoritative ``failed`` set; ``ran_fail`` is
    that set. Tolerant of any odd shape (degrades to no events). Never reads the
    receipt directory and never infers ran/pass from an on-disk receipt.
    """
    from pipeline.project.verification_timeline import autorun_hook_label

    events: list[AutorunEvent] = []
    for phase, entry in _phase_log(meta).items():
        if not isinstance(entry, Mapping):
            continue
        autorun = entry.get("verification_autorun")
        if not isinstance(autorun, Mapping) or not autorun.get("attempted", False):
            continue
        source = str(autorun.get("source") or "stage9_autorun")
        phase_name = str(autorun.get("phase") or phase)
        failed = set(_as_names(autorun.get("failed")))
        ran = _as_names(autorun.get("ran_commands"))
        events.append(
            AutorunEvent(
                phase=phase_name,
                source=source,
                hook_label=autorun_hook_label(source, phase_name),
                ran_pass=tuple(c for c in ran if c not in failed),
                ran_fail=tuple(c for c in ran if c in failed),
                skipped_fresh=_as_names(autorun.get("skipped_fresh")),
                skipped_manual=_as_names(autorun.get("skipped_manual")),
                receipt_paths=_as_names(autorun.get("receipt_paths")),
            ),
        )
    return tuple(events)


def _durable_parent_sources(meta: Mapping[str, Any]) -> tuple[Any, ...]:
    """Parent receipt sources reconstructed from durable follow-up linkage.

    A follow-up run records ``parent_run_id`` / ``parent_run_dir`` in its meta
    (persisted so the parent->follow-up timeline is reconstructable from meta
    alone). Project them into the ``(run_id, run_dir)`` pair shape
    :func:`pipeline.verification_readiness.classify_required_receipts` accepts so
    a present parent receipt can be inherited without any live ``extras``. Empty
    when the run is not a follow-up.
    """
    parent_run_id = meta.get("parent_run_id")
    parent_run_dir = meta.get("parent_run_dir")
    if isinstance(parent_run_id, str) and isinstance(parent_run_dir, str) and (
        parent_run_id and parent_run_dir
    ):
        return ((parent_run_id, parent_run_dir),)
    return ()


def get_verification_timeline(
    *,
    run_id: str | None = None,
    project: str | None = None,
    workspace: str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> VerificationTimelineProjection:
    """Read-only durable projection of a run's official verification gates.

    Resolves the run (``run_id`` or newest), re-resolves its project/contract
    exactly as :mod:`sdk.verify` does, and classifies the durable command
    receipts into a typed per-gate projection plus the aggregate readiness view
    and the auto-run mirror. Executes nothing, writes nothing.

    ``cwd`` is forwarded to run discovery (:func:`find_run`); it defaults to the
    walk-up sentinel. An embedder that must not bind to an arbitrary process cwd
    — the MCP server — passes ``cwd=None`` to disable walk-up, matching every
    other SDK read accessor.

    Only :class:`~sdk.errors.RunNotFound` propagates; any other resolution / git /
    IO / import failure degrades to a ``has_contract=False`` projection (criterion:
    empty contract / IO error must not raise).
    """
    ref = find_run(run_id, workspace=workspace, cwd=cwd)
    run_dir = ref.run_dir
    run_id_value = ref.run_id
    meta = load_meta(run_dir)
    # The auto-run mirror is durable and contract-independent — read it up front so
    # even a degraded (no-contract) projection still reflects what fired.
    autorun_events = _autorun_events(meta)

    try:
        _, _, project_dir, contract, ws, meta_resolved = _resolve_run_project_contract(
            project=project, run_id=run_id_value, workspace=workspace, cwd=cwd,
        )
    except Exception:  # noqa: BLE001 — only RunNotFound (above) propagates
        return VerificationTimelineProjection(
            run_id=run_id_value,
            project=project or "",
            has_contract=False,
            autorun_events=autorun_events,
        )

    if contract is None or not contract.commands:
        return VerificationTimelineProjection(
            run_id=run_id_value,
            project=project_dir,
            has_contract=False,
            autorun_events=autorun_events,
        )

    return _project_with_contract(
        run_id=run_id_value,
        run_dir=run_dir,
        meta=meta_resolved,
        project_dir=project_dir,
        contract=contract,
        ws=ws,
        autorun_events=autorun_events,
    )


def _project_with_contract(
    *,
    run_id: str,
    run_dir: Path,
    meta: Mapping[str, Any],
    project_dir: str,
    contract: VerificationContract,
    ws: str,
    autorun_events: tuple[AutorunEvent, ...],
) -> VerificationTimelineProjection:
    """Build the full projection for a run with a declared contract (never raises)."""
    from pipeline.evidence.verification_receipt import (
        load_env_assertion_receipts,
    )
    from pipeline.verification_contract import placeholder_context_for
    from pipeline.verification_readiness import (
        apply_environment_provenance,
        classify_required_receipts,
        delivery_gate_plan,
        effective_policy_by_command,
        suggested_verify_commands,
    )

    checkout_dir = _checkout_dir_for_meta(dict(meta), project_dir)
    ctx = placeholder_context_for(
        contract,
        checkout=checkout_dir,
        project=project_dir,
        workspace=ws,
        run_dir=str(run_dir),
    )

    try:
        plan = delivery_gate_plan(contract, None, checkout_dir)
    except Exception:  # noqa: BLE001 — plan resolution is best-effort
        plan = None

    parent_runs = _durable_parent_sources(meta)
    # The single effective classification (ADR 0108): the shared overlay folds the
    # ADR 0125 environment-provenance downgrade onto the base receipt
    # classification, so this projection no longer recomputes the rule itself — a
    # phase-scheduled gate whose verification_environment receipt recorded a failed
    # check arrives here already classified ``failed`` with the operator-evidence
    # carried as its ``reason`` and its ``path`` repointed at the phase receipt.
    classification = apply_environment_provenance(
        classify_required_receipts(
            contract, run_dir, ctx,
            checkout=checkout_dir, extras=None, plan=plan,
            parent_runs=parent_runs or None,
        ),
        contract,
        run_dir,
    )
    policy_by_command = effective_policy_by_command(contract, plan)
    hook_labels = _hook_labels(contract, plan)
    required_set = set(contract.required)

    # The durable searched set: the current run dir, then any follow-up parents.
    searched_run_dirs = (str(run_dir),) + tuple(
        str(run_dir_value) for _run_id, run_dir_value in parent_runs
    )

    # Auto-run skipped_fresh union drives the FRESH status of present gates.
    skipped_fresh: set[str] = set()
    for event in autorun_events:
        skipped_fresh.update(event.skipped_fresh)

    gates: list[GateProjection] = []
    missing: list[str] = []
    stale: list[str] = []
    failed: list[str] = []
    manual: list[str] = []
    inherited: list[str] = []
    for command, cls in classification.items():
        policy = policy_by_command.get(command, "")
        is_manual = policy == "manual_only"
        env = _command_env(contract, command)
        hook = hook_labels.get(command, "")
        receipt_path = cls.path or ""
        source_run_id = cls.source_run_id or ""
        is_inherited = bool(source_run_id and source_run_id != run_id)

        # The overlaid classification already carries the ADR 0125 downgrade: a
        # provenance break arrives as ``failed`` with the operator-evidence on its
        # ``reason`` and ``path`` repointed at the phase receipt. A manual gate is
        # never blocking — ``_gate_status`` maps it to SKIPPED regardless of the
        # overlaid status, so the downgrade is surfaced as ``detail`` but never
        # escalated for a manual command.
        status = _gate_status(cls.status, is_manual=is_manual,
                              fresh=command in skipped_fresh)
        detail = cls.reason if cls.status == "failed" and not is_manual else ""

        # Per-gate remediation only for a non-present required gate that is not
        # manual: same logic (suggested_verify_commands scoped to ONE command) as
        # the aggregate, and the same searched dirs, so per-gate and summary never
        # diverge. Present / manual gates carry neither.
        gate_searched: tuple[str, ...] = ()
        rerun_hint: tuple[str, ...] = ()
        if status in ("MISSING", "STALE", "FAIL") and not is_manual:
            gate_searched = searched_run_dirs
            rerun_hint = suggested_verify_commands(
                contract, (command,), run_id=run_id, project=project_dir,
            )

        gates.append(GateProjection(
            command=command,
            env=env,
            hook=hook,
            policy=policy,
            required=command in required_set,
            status=status,
            receipt_path=receipt_path,
            source_run_id=source_run_id,
            inherited=is_inherited,
            stale_reason=cls.reason if cls.status == "stale" else "",
            searched_run_dirs=gate_searched,
            rerun_hint=rerun_hint,
            detail=detail,
        ))

        # Aggregate buckets (mirror build_final_acceptance_readiness): the
        # overlaid ``failed`` from a provenance break joins residual_failed exactly
        # like any other failed gate (manual gates are routed to ``manual`` first,
        # so a downgraded manual gate stays non-blocking).
        if cls.status == "present":
            if is_inherited:
                inherited.append(f"{command} from run {source_run_id} ({receipt_path})")
            continue
        if is_manual:
            manual.append(command)
            continue
        {"missing": missing, "stale": stale, "failed": failed}[cls.status].append(command)

    suggested_commands: tuple[str, ...] = ()
    if missing or stale or failed:
        suggested_commands = suggested_verify_commands(
            contract, (*missing, *stale, *failed),
            run_id=run_id, project=project_dir,
        )

    env_statuses = tuple(
        (str(r.get("env", "")), bool(r.get("all_passed", False)))
        for r in load_env_assertion_receipts(run_dir)
    )

    residual_set = {*missing, *stale, *failed}
    return VerificationTimelineProjection(
        run_id=run_id,
        project=project_dir,
        has_contract=True,
        gates=tuple(gates),
        env_statuses=env_statuses,
        residual_missing=tuple(missing),
        residual_stale=tuple(stale),
        residual_failed=tuple(failed),
        manual_only=tuple(manual),
        inherited=tuple(inherited),
        searched_run_dirs=searched_run_dirs,
        suggested_commands=suggested_commands,
        policy_by_command=tuple(
            (c, p) for c, p in policy_by_command.items() if c in residual_set
        ),
        autorun_events=autorun_events,
    )


def _gate_status(
    raw_status: str, *, is_manual: bool, fresh: bool,
) -> GateStatus:
    """Map the four-status receipt classification + policy into the six-value enum.

    manual_only → SKIPPED (always, regardless of receipt state — a manual gate is
    intentionally not auto-run, never missing). present → FRESH when the command
    was an auto-run ``skipped_fresh`` (already-present, not executed this firing),
    else PASS. missing → MISSING, failed → FAIL, stale → STALE. There is no
    ``MANUAL`` value.
    """
    if is_manual:
        return "SKIPPED"
    if raw_status == "present":
        return "FRESH" if fresh else "PASS"
    return {
        "missing": "MISSING",
        "failed": "FAIL",
        "stale": "STALE",
    }.get(raw_status, "MISSING")


__all__ = [
    "GATE_STATUSES",
    "SCHEDULED_TRAIL_GAP",
    "AutorunEvent",
    "GateProjection",
    "GateStatus",
    "VerificationTimelineProjection",
    "get_verification_timeline",
]
