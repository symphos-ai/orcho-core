# SPDX-License-Identifier: Apache-2.0
"""verification_readiness.py — Stage 5 read-only final-acceptance readiness.

Pure computation + rendering of the readiness summary the ``final_acceptance``
reviewer sees: env-assertion status, the scheduled delivery gates, the required
command-receipts classified present / missing / failed / stale, and a count of
observed exploratory (ad-hoc) commands. Nothing here executes a command, writes
a receipt, or blocks a transition — read-only awareness only (ADR 0082).

Authoritative selection input (ADR 0082 / F1 of the Stage 5 review): the gate
plan consulted here is the **delivery selection plan** — either the executable
routing plan already built for the ``before_delivery`` epoch
(:data:`ROUTING_PLANS_EXTRAS_KEY`), or a fresh plan built at readiness time from
the *current* checkout's changed files. The advisory prompt-preview plan
(``pipeline.phases.builtin.prompt_parts._GATE_PROMPT_PREVIEW_KEY``) is NEVER
read: it may have been memoized before ``implement`` mutated the tree, which
would hide a path-selected gate that became delivery-relevant afterwards.

Receipt reads go only through the tolerant loaders in
:mod:`pipeline.evidence.verification_receipt`; git / IO failures degrade, never
raise. Stale classification reuses the exact fingerprint helper the receipt
writer used (:func:`pipeline.verification_dependencies.changed_files_fingerprint`),
so a valid receipt is never falsely stale due to hash drift.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pipeline.evidence.verification_receipt import (
    EnvProvenanceFailure,
    load_command_receipts,
    load_env_assertion_receipts,
)
from pipeline.verification_contract import (
    PlaceholderContext,
    VerificationContract,
)
from pipeline.verification_execution import (
    ResolvedExecution,
    VerificationIdentity,
    resolve_selected_execution,
)
from pipeline.verification_failure import ReceiptClassification, classify_receipt
from pipeline.verification_receipt_index import receipt_file_path

__all__ = [
    "READINESS_POLICY_LINE",
    "ROUTING_PLANS_EXTRAS_KEY",
    "TRANSCRIPT_NOT_PROOF_NOTE",
    "ProvenanceGateFailure",
    "DeliverySelection",
    "ReadinessSummary",
    "ReceiptClassification",
    "apply_environment_provenance",
    "build_final_acceptance_readiness",
    "classify_required_receipts",
    "command_phase_schedule",
    "delivery_gate_plan",
    "effective_policy_by_command",
    "environment_provenance_gate_failures",
    "render_readiness_block",
    "resolve_delivery_selection",
    "required_receipt_gaps",
    "suggested_verify_commands",
    "transcript_not_proof_note",
]

# Verbatim reviewer policy (task contract). Rendered into every non-empty block.
READINESS_POLICY_LINE = (
    "Readiness blockers should be based on missing/failed/stale/invalid "
    "declared receipts, not only an ad-hoc host command mismatch."
)

# Channel-distinction note (T4): the single source of truth for telling an
# operator/reviewer that ad-hoc transcript commands are NOT accepted as proof —
# only an official receipt is. Shared by the readiness block and any
# correction/review remediation surface so the wording never drifts.
TRANSCRIPT_NOT_PROOF_NOTE = (
    "official receipt missing; transcript commands are not accepted as proof"
)


def transcript_not_proof_note() -> str:
    """The single channel-distinction note shared by readiness and review.

    Surfaced only as remediation context when a required receipt is
    missing/stale yet the run holds ad-hoc transcript commands: it states plainly
    that those commands are not proof, while the official verify commands remain
    the remediation path. Never parses the transcript as proof.
    """
    return TRANSCRIPT_NOT_PROOF_NOTE


# The executable routing-plans cache key — a deliberate literal copy of
# ``pipeline.project.gate_repair.VERIFICATION_GATE_ROUTING_PLANS_KEY`` so this
# pure module never imports the orchestration layer (locked by an equality
# test in tests/unit/pipeline/prompts/test_final_acceptance_readiness_block.py).
ROUTING_PLANS_EXTRAS_KEY = "verification_gate_routing_plans"

# The delivery selection epoch (``hook:phase`` — phaseless before_delivery).
_DELIVERY_EPOCH = "before_delivery:"

# Delivery-relevant (hook, phase) positions: the gates whose receipts the
# final-acceptance reviewer treats as official proof.
_DELIVERY_HOOKS: tuple[tuple[str, str], ...] = (
    ("after_phase", "implement"),
    ("before_delivery", ""),
)


@dataclass(frozen=True)
class DeliverySelection:
    """Delivery receipts and uncollapsed scheduled identities for one epoch.

    ``receipt_commands`` is deliberately command-level for receipt lookup, while
    ``identities`` preserves every ``(command, hook, phase)`` execution identity.
    Consumers can therefore dedupe receipt reads without losing executor or
    consequence decisions for a command scheduled at multiple delivery hooks.
    """

    receipt_commands: tuple[str, ...] = ()
    identities: tuple[ResolvedExecution, ...] = ()

    @property
    def executor_identities(self) -> tuple[ResolvedExecution, ...]:
        return tuple(item for item in self.identities if item.executor == "engine")

    @property
    def consequence_identities(self) -> tuple[ResolvedExecution, ...]:
        return tuple(item for item in self.identities if item.consequence != "none")


@dataclass(frozen=True)
class ReadinessSummary:
    """Typed result of :func:`build_final_acceptance_readiness`.

    ``env_statuses`` maps env name -> all_passed. ``gate_statuses`` are
    pre-rendered per-gate lines (command, hook, effective policy/action,
    receipt status). The four ``required_*`` tuples partition the required
    delivery command set. ``stale_reasons`` are ``"command: reason"`` strings
    parallel to ``required_stale`` (the reason a stale receipt is stale).
    ``tested_dependency_lines`` are ``"command: tested against dep@<sha>"``
    cross-repo summaries for present receipts whose dependencies were
    depended-on. ``exploratory_count`` counts observed ad-hoc ``command.end``
    events — informational only, never authoritative.

    Diagnostics (ADR 0089 / T3) are additive and default empty so a
    no-parent / no-blocker summary renders byte-identically: ``searched_run_dirs``
    are the run directories consulted for receipts (current run first, then any
    follow-up parents); ``receipt_provenance`` are
    ``"<command>: <status> from run <source_run_id> (<path>)"`` lines for present
    receipts INHERITED from a parent run (a current-run receipt adds no line);
    ``suggested_commands`` are the actionable ``orcho verify`` hints printed when
    some required receipt is missing/stale.
    """

    env_statuses: tuple[tuple[str, bool], ...] = ()
    gate_statuses: tuple[str, ...] = ()
    required_present: tuple[str, ...] = ()
    required_missing: tuple[str, ...] = ()
    required_failed: tuple[str, ...] = ()
    required_stale: tuple[str, ...] = ()
    stale_reasons: tuple[str, ...] = ()
    tested_dependency_lines: tuple[str, ...] = ()
    exploratory_count: int = 0
    exploratory_note: str = ""
    searched_run_dirs: tuple[str, ...] = ()
    receipt_provenance: tuple[str, ...] = ()
    suggested_commands: tuple[str, ...] = ()
    # Per-gate policy awareness (T3, ADR 0097), additive. ``policy_by_command``
    # is the effective delivery policy (require|warn|suggest|manual_only) for
    # every required delivery command, render-only. The ``required_*`` gaps above
    # exclude manual/operator-only commands; those land in ``manual_only_gaps``
    # instead — visible, but never a missing-required receipt and never in
    # "Remaining before ready" (ADR 0090). Default empty so a no-blocker summary
    # renders byte-identically.
    policy_by_command: tuple[tuple[str, str], ...] = ()
    # Post-result consequence is intentionally separate from policy: a hygiene
    # failure can be a warning while its canonical ``require`` policy remains
    # visible for audit. Internal prompt projection only; no durable receipt or
    # SDK shape consumes this field.
    consequence_by_command: tuple[tuple[str, str], ...] = ()
    manual_only_gaps: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (
            self.env_statuses
            or self.gate_statuses
            or self.required_present
            or self.required_missing
            or self.required_failed
            or self.required_stale
            or self.manual_only_gaps
            or self.exploratory_count
        )


def delivery_gate_plan(
    contract: VerificationContract,
    extras: Mapping[str, Any] | None,
    checkout: str,
) -> Any:
    """Resolve the authoritative delivery gate plan, or ``None``.

    Returns ``None`` when the contract declares no ``gate_sets`` / ``selection``
    (the raw-schedule fallback in :func:`resolve_delivery_selection` then
    applies). Otherwise, in order:

    1. The executable routing plan cached for the ``before_delivery`` epoch
       (built by ``pipeline.project.gate_repair`` from post-implement changed
       files when the delivery hook fired) — reusing it keeps readiness
       consistent with what actually routed.
    2. A fresh plan built here from the *current* ``checkout`` changed files.

    The advisory prompt-preview plan is deliberately not consulted — it may
    predate ``implement`` and miss path-selected gates (ADR 0082 / F1).
    """
    if not (getattr(contract, "gate_sets", None) or getattr(contract, "selection", ())):
        return None

    if extras is not None:
        plans = extras.get(ROUTING_PLANS_EXTRAS_KEY)
        if isinstance(plans, Mapping):
            cached = plans.get(_DELIVERY_EPOCH)
            if cached is not None:
                return cached

    from pipeline.verification_selection import (
        build_scheduled_gate_plan,
        selection_context_from_extras,
    )

    touched: tuple[str, ...] = ()
    if checkout:
        try:
            from core.io.git_helpers import git_changed_files

            touched = tuple(git_changed_files(checkout))
        except Exception:  # noqa: BLE001 — touched-paths is best-effort
            touched = ()
    return build_scheduled_gate_plan(
        contract,
        selection_context_from_extras(
            extras or {},
            contract,
            touched_paths=touched,
        ),
    )


def resolve_delivery_selection(
    contract: VerificationContract,
    plan: Any,
) -> DeliverySelection:
    """Resolve delivery receipt commands and each selected delivery identity.

    ``contract.required`` remains the leading command-level receipt view.  Plan
    entries at delivery positions are converted one-for-one into typed resolved
    identities; the same command at multiple hooks is intentionally retained.
    Without a plan, raw schedule commands still contribute receipt commands but
    cannot claim an effective execution identity before selection has occurred.
    """
    ordered: list[str] = []
    identities: list[ResolvedExecution] = []

    def _add(name: str) -> None:
        if name and name not in ordered:
            ordered.append(name)

    for name in contract.required:
        _add(name)

    if plan is not None:
        for entry in getattr(plan, "entries", ()) or ():
            position = (
                getattr(entry, "hook", ""),
                getattr(entry, "phase", ""),
            )
            if position in _DELIVERY_HOOKS:
                command = getattr(entry, "command", "")
                _add(command)
                identities.append(resolve_selected_execution(VerificationIdentity(
                    command=command,
                    hook=getattr(entry, "hook", ""),
                    phase=getattr(entry, "phase", ""),
                    policy=getattr(entry, "policy", ""),
                )))
        _add_implicit_required_identities(contract, identities)
        return DeliverySelection(tuple(ordered), tuple(identities))

    for entry in contract.schedule:
        if (entry.hook, entry.phase) not in _DELIVERY_HOOKS:
            continue
        for name in entry.commands or tuple(sorted(contract.commands)):
            _add(name)
            # No selected plan is available, but a raw delivery schedule still
            # has a complete declared identity. Apply the same work-mode policy
            # derivation the plan builder uses so execution consumers never need
            # their own policy fallback.
            from pipeline.verification_selection import derive_effective_policy

            policy = derive_effective_policy(
                entry.policy,
                contract.work_mode,
                required=name in contract.required,
            )
            identities.append(resolve_selected_execution(VerificationIdentity(
                command=name,
                hook=entry.hook,
                phase=entry.phase,
                policy=policy,
            )))
    _add_implicit_required_identities(contract, identities)
    return DeliverySelection(tuple(ordered), tuple(identities))


def _add_implicit_required_identities(
    contract: VerificationContract,
    identities: list[ResolvedExecution],
) -> None:
    """Fill the legacy required-receipt projection with typed identities.

    ``verification.required`` predates explicit scheduled selection and remains
    an implicit delivery gate when no delivery identity represents it. A command
    explicitly parked behind ``manual_only`` is instead represented as an
    operator identity. This also gives a required command scheduled only at a
    phase hook a delivery-position refresh: otherwise delivery can enforce a
    stale receipt without any engine identity that can regenerate it.
    """
    represented = {item.identity.command for item in identities}
    manual_commands = _manual_only_schedule_commands(contract)
    for command in contract.required:
        if command in represented:
            continue
        if command in manual_commands:
            identities.append(resolve_selected_execution(VerificationIdentity(
                command=command,
                hook="manual_only",
                phase="",
                policy="manual",
            )))
        else:
            identities.append(resolve_selected_execution(VerificationIdentity(
                command=command,
                hook="before_delivery",
                phase="",
                policy=_implicit_delivery_policy(contract),
            )))


def _implicit_delivery_policy(contract: VerificationContract) -> str:
    """Boundary policy for a required command lacking a delivery identity.

    The delivery module owns this projection. Import lazily because it imports
    readiness for assessment, while this helper is only reached after both
    modules finish initialization.
    """
    from pipeline.verification_delivery import resolve_delivery_policy

    return resolve_delivery_policy(contract) or "warn"


def _manual_only_schedule_commands(contract: VerificationContract) -> set[str]:
    """Return commands explicitly parked behind the operator-only hook."""
    commands: set[str] = set()
    for entry in contract.schedule:
        if entry.hook != "manual_only":
            continue
        commands.update(entry.commands)
        for gate_set in entry.gate_sets:
            declared = contract.gate_sets.get(gate_set)
            if declared is not None:
                commands.update(declared.commands)
    return commands


def effective_policy_by_command(
    contract: VerificationContract,
    plan: Any,
) -> dict[str, str]:
    """Effective delivery policy for each required delivery command (degrade-safe).

    Thin wrapper over
    :func:`pipeline.verification_policy.effective_delivery_policy_by_command`
    that resolves the two caller-supplied inputs here so every readiness surface
    derives identical policies: the raw manual/operator-only command set
    (``sdk.verify.manual_or_operator_only_commands``) and the boundary delivery
    policy (:func:`pipeline.verification_delivery.resolve_delivery_policy`, the
    single unchanged rule). All imports are lazy to keep this pure module free of
    import cycles (``verification_policy`` imports back into this module) and of
    the SDK layer at top level. Never raises: any failure degrades to an empty
    policy map, so readiness simply omits the per-gate annotation.
    """
    try:
        from pipeline.verification_delivery import resolve_delivery_policy
        from pipeline.verification_policy import (
            effective_delivery_policy_by_command,
        )

        try:
            from sdk.verify import manual_or_operator_only_commands

            manual_set = manual_or_operator_only_commands(contract)
        except Exception:  # noqa: BLE001 — manual-set lookup is best-effort
            manual_set = set()
        return effective_delivery_policy_by_command(
            contract,
            plan,
            manual_set,
            boundary_policy=resolve_delivery_policy(contract),
        )
    except Exception:  # noqa: BLE001 — readiness must never raise outward
        return {}


def _distinct_command_envs(
    contract: VerificationContract,
    commands: tuple[str, ...],
) -> tuple[str, ...]:
    """Distinct declared envs of ``commands`` (declared env, else default_env).

    Order-preserving and deduped; commands with no resolvable env contribute
    nothing. Used to name the env(s) the operator should re-assert.
    """
    out: list[str] = []
    for command in commands:
        spec = contract.commands.get(command) or {}
        env = str(spec.get("env") or getattr(contract, "default_env", "") or "")
        if env and env not in out:
            out.append(env)
    return tuple(out)


def suggested_verify_commands(
    contract: VerificationContract,
    commands: tuple[str, ...],
    *,
    run_id: str,
    project: str,
) -> tuple[str, ...]:
    """Actionable ``orcho verify`` hints for missing / stale required ``commands``.

    One ``orcho verify env --env <env> ...`` line per distinct env the given
    commands declared (so the operator can re-assert each environment), then an
    exact ``orcho verify run`` line. When every missing/stale command belongs to
    the contract's static ``verification.required`` set, the compact
    ``--required`` form is enough. When delivery selection added extra commands
    (for example path-selected subsystem gates), the hint must list the concrete
    command names positionally; otherwise ``--required`` can rerun too little
    and leave the operator in a correction loop. Both lines carry the CURRENT
    ``run_id`` and the subject ``project`` so the command is copy-paste runnable.
    Shared by the Stage 5 readiness block and the Stage 6 delivery banner so the
    two surfaces print identical guidance and can never contradict each other.
    """
    envs = _distinct_command_envs(contract, commands)
    lines = [f"orcho verify env --env {env} --run-id {run_id} --project {project}" for env in envs]
    if not envs:
        lines.append(f"orcho verify env --run-id {run_id} --project {project}")
    required_set = set(contract.required)
    if commands and not set(commands).issubset(required_set):
        names = " ".join(commands)
        lines.append(
            f"orcho verify run {names} --run-id {run_id} --project {project}",
        )
        return tuple(lines)
    lines.append(
        f"orcho verify run --required --run-id {run_id} --project {project}",
    )
    return tuple(lines)


@dataclass(frozen=True)
class ProvenanceGateFailure:
    """Operator-evidence for a delivery gate failed by environment provenance.

    A required gate scheduled at a phase whose ``verification_environment``
    receipt recorded a failing check (e.g. ``pipeline_import``) is downgraded to
    failed regardless of its own command-receipt state — a passing command
    receipt produced by an interpreter that imported the wrong tree is not real
    proof (ADR 0125). ``detail`` is the human-readable
    ``"<check>: expected <X> actual <Y>"`` string; ``receipt_path`` points at the
    failing phase receipt so an operator needs no raw logs.
    """

    command: str
    phase: str
    check: str
    expected: str | None
    actual: str | None
    receipt_path: str
    detail: str


def command_phase_schedule(contract: VerificationContract) -> dict[str, str]:
    """``command -> phase`` for commands bound to a phase-scheduled hook.

    Built from ``contract.schedule`` entries whose hook is ``before_phase`` or
    ``after_phase`` and that name a ``phase`` (an entry with no explicit commands
    covers every declared command, matching the projection convention in
    :func:`resolve_delivery_selection`). A command with no phase-bound schedule is
    absent — it gets no environment-provenance link, so gates without a phase
    schedule (``before_delivery`` / ``manual_only``) keep their existing
    semantics. Shared by the SDK projection and the live render so both derive the
    gate->phase association identically.
    """
    phases: dict[str, str] = {}
    for entry in contract.schedule:
        if entry.hook not in ("before_phase", "after_phase") or not entry.phase:
            continue
        for command in entry.commands or tuple(sorted(contract.commands)):
            if command:
                phases.setdefault(command, entry.phase)
    return phases


def environment_provenance_gate_failures(
    command_phases: Mapping[str, str],
    failures: Sequence[EnvProvenanceFailure],
) -> dict[str, ProvenanceGateFailure]:
    """Map each phase-scheduled gate to its environment-provenance failure, if any.

    The single downgrade rule shared by the typed SDK projection and the live
    DONE/HALTED render so the two never diverge: a gate whose scheduled phase has
    a failed ``verification_environment`` check is reported failed, carrying that
    check's operator-evidence. Pure: it neither reads receipts nor raises — the
    caller supplies ``failures`` from
    :func:`pipeline.evidence.verification_receipt.environment_provenance_failures`
    and ``command_phases`` from :func:`command_phase_schedule`. Gates absent from
    ``command_phases`` (no phase schedule) and phases with no failure are simply
    not returned, so only a genuine provenance break downgrades a gate.
    """
    by_phase: dict[str, EnvProvenanceFailure] = {}
    for failure in failures:
        by_phase.setdefault(failure.phase, failure)
    out: dict[str, ProvenanceGateFailure] = {}
    for command, phase in command_phases.items():
        if not phase:
            continue
        failure = by_phase.get(phase)
        if failure is None:
            continue
        out[command] = ProvenanceGateFailure(
            command=command,
            phase=phase,
            check=failure.check,
            expected=failure.expected,
            actual=failure.actual,
            receipt_path=failure.receipt_path,
            detail=(f"{failure.check}: expected {failure.expected} actual {failure.actual}"),
        )
    return out


def apply_environment_provenance(
    status_by_command: Mapping[str, ReceiptClassification],
    contract: VerificationContract,
    run_dir: Path | str,
) -> dict[str, ReceiptClassification]:
    """Overlay the ADR 0125 environment-provenance downgrade onto a classification.

    The single effective classification the four official surfaces share: given a
    ready ``command -> ReceiptClassification`` map from
    :func:`classify_required_receipts`, this returns a new map where every
    phase-scheduled gate whose scheduled phase recorded a failed
    ``verification_environment`` check is downgraded to ``failed`` — a passing
    command receipt produced by an interpreter that imported the wrong tree is not
    real proof. The downgraded classification carries the provenance failure's
    operator-evidence as its ``reason`` (the ``"<check>: expected <X> actual <Y>"``
    string) and repoints ``path`` at the failing phase receipt, while preserving
    the original ``source_run_id`` so inheritance provenance is not lost.

    Pure overlay over :func:`classify_required_receipts`: it never folds the
    downgrade into the seven-consumer base classification (the blast-radius risk
    in ADR 0108) and never reasons about manual/operator-only policy itself —
    policy routing stays downstream, so a manual gate that is downgraded here is
    still surfaced non-blocking by each surface's own manual handling. Never
    raises: any IO / JSON / lookup error degrades to the input map unchanged, so
    no projection or delivery decision can fail outward because of provenance.
    """
    try:
        from pipeline.evidence.verification_receipt import (
            environment_provenance_failures,
        )

        provenance = environment_provenance_gate_failures(
            command_phase_schedule(contract),
            environment_provenance_failures(run_dir),
        )
        if not provenance:
            return dict(status_by_command)
        overlaid = dict(status_by_command)
        for command, failure in provenance.items():
            current = overlaid.get(command)
            # Preserve an already-``failed`` receipt (notably a non-zero-exit
            # ``test_failure``): provenance must not relabel or soften a declared
            # blocker — that is the precedence the review required. But a
            # ``present`` receipt (fake proof from the wrong tree) OR a
            # ``missing`` one under a failed provenance gate both resolve to a
            # provenance-caused ``failed``; only a real prior failure is kept.
            if current is None or current.status == "failed":
                continue
            overlaid[command] = ReceiptClassification(
                status="failed",
                failure_kind="provenance_failure",
                reason=failure.detail,
                source_run_id=current.source_run_id,
                path=failure.receipt_path,
                exit_code=current.exit_code,
                assertions_total=current.assertions_total,
                assertions_passed=current.assertions_passed,
                assertions_failed=current.assertions_failed,
                failed_assertions=current.failed_assertions,
            )
        return overlaid
    except Exception:  # noqa: BLE001 — overlay must never raise outward
        return dict(status_by_command)


def classify_required_receipts(
    contract: VerificationContract,
    run_dir: Path | str,
    ctx: PlaceholderContext,
    *,
    checkout: str,
    extras: Mapping[str, Any] | None = None,
    plan: Any = None,
    parent_runs: Any = None,
) -> dict[str, ReceiptClassification]:
    """Classify each required delivery command's receipt, in required order.

    Returns an ordered ``command -> ReceiptClassification`` mapping over
    :func:`resolve_delivery_selection`; each classification carries one of the
    fixed four statuses (``present`` / ``missing`` / ``failed`` / ``stale``) plus
    an optional human ``reason`` (populated for stale) and additive
    ``source_run_id`` / ``path`` provenance. ``checkout`` is the EXPLICIT
    staleness subject: its ``(changed-files fingerprint, HEAD)`` is recomputed and
    compared against each receipt's persisted git provenance.

    Cross-repo staleness (ADR 0084): ``ctx``'s declared-dependency HEADs are
    read once per call (:func:`pipeline.verification_dependencies.current_dependency_heads`)
    and a receipt whose depended-on dependency's HEAD moved is stale with a
    reason naming the dependency and both SHAs.

    Parent-run inheritance (ADR 0089): ``parent_runs`` is an explicit override
    (an iterable of :class:`pipeline.verification_receipt_index.ReceiptSource` or
    ``(run_id, run_dir)`` pairs); when omitted the parent sources are read from
    ``extras`` under
    :data:`pipeline.verification_receipt_index.VERIFICATION_PARENT_RUNS_EXTRAS_KEY`.
    With no parent sources the classification of the current run's lone receipt is
    byte-identical to before. When parents exist, the per-command selection rule
    in :func:`_select_classification` decides whether a valid parent receipt is
    inherited — a fresh same-diff failure in the current run is never masked.

    This is the single classification shared by Stage 5 readiness (which passes
    ``ctx.checkout``) and the Stage 6 delivery gate
    (:mod:`pipeline.verification_delivery`, which passes its subject checkout):
    both pass the same subject so a receipt written against that checkout is
    never falsely stale. ``plan`` overrides the resolved delivery plan; when
    omitted it is resolved via :func:`delivery_gate_plan` from ``checkout``.
    Never raises: git / IO failures degrade (staleness is simply not asserted).
    """
    if plan is None:
        try:
            plan = delivery_gate_plan(contract, extras, checkout or "")
        except Exception:  # noqa: BLE001 — classification must never raise outward
            plan = None

    required = resolve_delivery_selection(contract, plan).receipt_commands
    receipts = {str(r.get("command", "")): r for r in load_command_receipts(run_dir)}
    current_fingerprint, current_head = _current_checkout_identity(checkout or "")

    # readiness -> dependencies is an allowed import direction (the low-level
    # module never imports back); lazy to keep the pure module's top level free
    # of the dependency capture layer, mirroring the fingerprint import below.
    from pipeline.verification_dependencies import current_dependency_heads

    try:
        dependency_heads = current_dependency_heads(ctx)
    except Exception:  # noqa: BLE001 — classification must never raise outward
        dependency_heads = {}

    # readiness -> receipt_index is an allowed direction (the index never imports
    # back); lazy to keep the no-parent path free of any extra work.
    from pipeline.verification_receipt_index import (
        ReceiptSource,
        coerce_receipt_sources,
        load_parent_candidates,
        parent_sources_from_extras,
    )

    sources = (
        coerce_receipt_sources(parent_runs)
        if parent_runs is not None
        else parent_sources_from_extras(extras)
    )
    parent_candidates = load_parent_candidates(sources, required) if sources else {}
    current_source = ReceiptSource(run_id=Path(run_dir).name, run_dir=str(run_dir))

    status_by_command: dict[str, ReceiptClassification] = {}
    for command in required:
        status_by_command[command] = _select_classification(
            command,
            contract=contract,
            current_receipt=receipts.get(command),
            current_source=current_source,
            parent_candidates=parent_candidates.get(command, ()),
            current_fingerprint=current_fingerprint,
            current_head=current_head,
            dependency_heads=dependency_heads,
        )
    return status_by_command


def required_receipt_gaps(
    contract: VerificationContract,
    run_dir: Path | str,
    ctx: PlaceholderContext,
    *,
    extras: Mapping[str, Any] | None = None,
    plan: Any = None,
    language: str | None = None,
) -> list[dict[str, Any]]:
    """Engine-computed release gaps for unproven required delivery commands.

    One ``{"risk", "missing_evidence", "required_check"}`` dict (the release
    schema's ``verification_gaps`` entry shape) per ``require``-policy delivery
    command whose receipt classifies missing / failed / stale — the deterministic
    backstop behind ADR 0090: a ``require``-policy gate that was skipped or
    failed must surface as a release gap even when the reviewer model omits it.

    Only ``require``-policy gaps are emitted (T3, ADR 0097): a ``warn`` /
    ``suggest`` gap is shipping-allowed by policy and is surfaced as a readiness
    warning, not a release blocker, and a ``manual_only`` / ``operator_only``
    command is never generated as a gap at all — so reclassifying a command as
    manual can never *hide* a real auto-required gap, while a genuine ``require``
    gap stays blocking. Human-readable values follow ``language`` when provided;
    protocol names, command names, paths, and command lines stay unchanged. Empty
    when every required receipt is present (or only non-require gaps remain).
    Reuses :func:`classify_required_receipts` (same subject, same inheritance
    rules as the readiness block the reviewer saw); never raises.
    """
    if plan is None:
        try:
            plan = delivery_gate_plan(contract, extras, ctx.checkout or "")
        except Exception:  # noqa: BLE001 — gaps must never raise outward
            plan = None
    status_by_command = apply_environment_provenance(
        classify_required_receipts(
            contract,
            run_dir,
            ctx,
            checkout=ctx.checkout or "",
            extras=extras,
            plan=plan,
        ),
        contract,
        run_dir,
    )
    policy_by_command = effective_policy_by_command(contract, plan)
    from pipeline.verification_policy import consequence_by_command

    consequences = consequence_by_command(
        status_by_command,
        policy_by_command,
    )
    gaps: list[dict[str, Any]] = []
    for command, classification in status_by_command.items():
        if classification.status == "present":
            continue
        # Only an effective ``require`` gap is a release blocker; warn/suggest are
        # shipping-allowed and manual_only is never a gap (ADR 0090).
        if consequences.get(command) != "required_action":
            continue
        run_decl = (contract.commands.get(command) or {}).get("run", "")
        if isinstance(run_decl, (list, tuple)):
            run_decl = " ".join(str(a) for a in run_decl)
        gaps.append(
            _required_receipt_gap_dict(
                command,
                classification=classification,
                required_check=str(run_decl) or command,
                language=language,
            )
        )
    return gaps


def _is_russian(language: str | None) -> bool:
    normalized = (language or "").strip().lower()
    return normalized.startswith(("ru", "rus", "russian", "рус"))


def _receipt_status_label(status: str, *, language: str | None) -> str:
    if not _is_russian(language):
        return status
    return {
        "missing": "отсутствует",
        "failed": "завершился ошибкой",
        "stale": "устарел",
    }.get(status, status)


def _required_receipt_gap_dict(
    command: str,
    *,
    classification: ReceiptClassification,
    required_check: str,
    language: str | None,
) -> dict[str, Any]:
    status = _receipt_status_label(classification.status, language=language)
    reason = f" ({classification.reason})" if classification.reason else ""
    if _is_russian(language):
        return {
            "risk": (
                f"Обязательный verification gate '{command}' не доказан: receipt {status}{reason}."
            ),
            "missing_evidence": (
                f"Нет проходящего command receipt для обязательного gate "
                f"'{command}' в verification_command_receipts/ для этого run."
            ),
            "required_check": required_check,
        }
    return {
        "risk": (
            f"Required verification gate '{command}' is unproven: "
            f"receipt {classification.status}{reason}."
        ),
        "missing_evidence": (
            f"No passing command receipt for required gate '{command}' "
            "under verification_command_receipts/ for this run."
        ),
        "required_check": required_check,
    }


def build_final_acceptance_readiness(
    contract: VerificationContract,
    run_dir: Path | str,
    ctx: PlaceholderContext,
    *,
    extras: Mapping[str, Any] | None = None,
    plan: Any = None,
) -> ReadinessSummary:
    """Compute the read-only readiness summary for ``final_acceptance``.

    ``plan`` overrides the resolved delivery plan (tests / explicit callers);
    when omitted it is resolved via :func:`delivery_gate_plan`. Never raises:
    git / IO failures degrade per-field.
    """
    if plan is None:
        try:
            plan = delivery_gate_plan(contract, extras, ctx.checkout or "")
        except Exception:  # noqa: BLE001 — readiness must never break the prompt
            plan = None

    status_by_command = apply_environment_provenance(
        classify_required_receipts(
            contract,
            run_dir,
            ctx,
            checkout=ctx.checkout or "",
            extras=extras,
            plan=plan,
        ),
        contract,
        run_dir,
    )

    # Per-gate effective policy (T3): manual/operator-only commands are split out
    # of the required gaps entirely so they never read as a missing-required
    # receipt (ADR 0090). Required gaps keep their natural required-command order
    # (shared with the suggested-command hints), not a per-policy grouping.
    policy_by_command = effective_policy_by_command(contract, plan)
    from pipeline.verification_policy import consequence_by_command

    consequences = consequence_by_command(status_by_command, policy_by_command)
    present: list[str] = []
    missing: list[str] = []
    failed: list[str] = []
    stale: list[str] = []
    stale_reasons: list[str] = []
    manual: list[str] = []
    for command, classification in status_by_command.items():
        status = classification.status
        if status == "present":
            present.append(command)
            continue
        if policy_by_command.get(command) in ("manual_only", "manual"):
            manual.append(command)
            continue
        {"missing": missing, "failed": failed, "stale": stale}[status].append(
            command,
        )
        if status == "stale":
            stale_reasons.append(
                f"{command}: {classification.reason or 'stale'}",
            )
    manual_only_gaps = tuple(manual)

    env_statuses = tuple(
        (str(r.get("env", "")), bool(r.get("all_passed", False)))
        for r in load_env_assertion_receipts(run_dir)
    )

    gate_statuses = _gate_status_lines(contract, plan, status_by_command)

    tested_dependency_lines = _tested_dependency_lines(run_dir, present)

    exploratory_count = _count_exploratory_commands(run_dir)
    exploratory_note = (
        "observed ad-hoc commands; exploratory only, not authoritative" if exploratory_count else ""
    )

    # Diagnostics (ADR 0089 / T3): the run dirs searched (current first, then
    # follow-up parents), provenance lines for parent-inherited present receipts,
    # and the actionable verify hints when something is missing/stale.
    from pipeline.verification_receipt_index import parent_sources_from_extras

    current_run_id = Path(run_dir).name
    searched_run_dirs = (str(run_dir),) + tuple(
        s.run_dir for s in parent_sources_from_extras(extras)
    )
    receipt_provenance = tuple(
        f"{command}: {cl.status} from run {cl.source_run_id} ({cl.path})"
        for command, cl in status_by_command.items()
        if cl.status == "present" and cl.source_run_id and cl.source_run_id != current_run_id
    )
    # Searched dirs + actionable verify hints whenever a required receipt is
    # open in ANY non-present way (missing / stale / failed). suggested_commands
    # receives the union so the DONE timeline can share the identical hint set
    # (see pipeline.project.verification_timeline). The required_* partitioning
    # and 'Remaining before ready' verdict above are untouched — only this
    # advisory surface widened to also cover failed.
    suggested_commands: tuple[str, ...] = ()
    if missing or stale or failed:
        suggested_commands = suggested_verify_commands(
            contract,
            (*missing, *stale, *failed),
            run_id=current_run_id,
            project=str(getattr(ctx, "project", "") or ""),
        )

    return ReadinessSummary(
        env_statuses=env_statuses,
        gate_statuses=gate_statuses,
        required_present=tuple(present),
        required_missing=tuple(missing),
        required_failed=tuple(failed),
        required_stale=tuple(stale),
        stale_reasons=tuple(stale_reasons),
        tested_dependency_lines=tested_dependency_lines,
        exploratory_count=exploratory_count,
        exploratory_note=exploratory_note,
        searched_run_dirs=searched_run_dirs,
        receipt_provenance=receipt_provenance,
        suggested_commands=suggested_commands,
        policy_by_command=tuple(policy_by_command.items()),
        consequence_by_command=tuple(consequences.items()),
        manual_only_gaps=manual_only_gaps,
    )


def render_readiness_block(summary: ReadinessSummary) -> str | None:
    """Render the readiness block, or ``None`` when there is nothing to show."""
    if summary.empty:
        return None

    lines = ["Verification readiness — final_acceptance:"]

    lines.append("  Environments:")
    if summary.env_statuses:
        lines.extend(f"    {env}: {'pass' if ok else 'FAIL'}" for env, ok in summary.env_statuses)
    else:
        lines.append("    (none recorded)")

    lines.append("  Scheduled gates:")
    if summary.gate_statuses:
        lines.extend(f"    {line}" for line in summary.gate_statuses)
    else:
        lines.append("    (none scheduled)")

    _section(lines, "Required receipts (present)", summary.required_present)
    if summary.tested_dependency_lines:
        lines.append("  Tested dependency commits:")
        lines.extend(f"    {item}" for item in summary.tested_dependency_lines)
    # Provenance for receipts inherited from a follow-up parent run (ADR 0089).
    # Empty — and thus absent — for any no-parent run, so the no-parent block is
    # byte-identical.
    if summary.receipt_provenance:
        lines.append("  Inherited receipt provenance:")
        lines.extend(f"    {item}" for item in summary.receipt_provenance)
    # Each missing/failed/stale gap is annotated with its effective per-gate
    # policy (T3): a ``require`` gap reads ``"<gap> (require)"``; a ``warn`` /
    # ``suggest`` gap reads ``"<gap> (<policy>) — shipping allowed by policy"`` so
    # the reviewer sees it is surfaced, not blocked. An empty section is
    # byte-identical ``"(none)"``.
    policy_map = dict(summary.policy_by_command)
    consequence_map = dict(summary.consequence_by_command)
    _gap_section(
        lines, "Missing receipts", summary.required_missing, policy_map, consequence_map,
    )
    _gap_section(
        lines, "Failed receipts", summary.required_failed, policy_map, consequence_map,
    )
    # Stale receipts carry their reason (subject drift or a dependency HEAD
    # move naming both SHAs) so the reviewer sees *why* a receipt is stale.
    stale_items = list(summary.stale_reasons or summary.required_stale)
    lines.append("  Stale receipts:")
    if summary.required_stale:
        lines.extend(
            f"    {_annotate_gap(item, policy_map.get(name, ''), consequence_map.get(name))}"
            for name, item in zip(
                summary.required_stale,
                stale_items,
                strict=False,
            )
        )
    else:
        lines.append("    (none)")

    # Manual/operator-only receipts (ADR 0090): visible, but not auto-run and
    # never a required/blocking gap. Absent — and thus byte-identical — when
    # there are no manual-only gaps.
    if summary.manual_only_gaps:
        lines.append("  Operator-available receipts:")
        lines.extend(
            f"    {name} ({policy_map.get(name, 'manual_only')}) — operator available, not auto-run"
            for name in summary.manual_only_gaps
        )

    lines.append("  Exploratory commands:")
    if summary.exploratory_count:
        lines.append(
            f"    {summary.exploratory_count} {summary.exploratory_note}",
        )
    else:
        lines.append("    (none observed)")

    # Only a required-action consequence blocks readiness. This retains the
    # declared ``require`` policy for hygiene outcomes without falsely treating
    # them as a release blocker (ADR 0130).
    require_missing = [
        c for c in summary.required_missing
        if consequence_map.get(c, "required_action" if policy_map.get(c) == "require" else "")
        == "required_action"
    ]
    require_failed = [
        c for c in summary.required_failed
        if consequence_map.get(c, "required_action" if policy_map.get(c) == "require" else "")
        == "required_action"
    ]
    require_stale = [
        item
        for name, item in zip(
            summary.required_stale,
            stale_items,
            strict=False,
        )
        if consequence_map.get(
            name, "required_action" if policy_map.get(name) == "require" else "",
        ) == "required_action"
    ]
    remaining = (
        [f"missing required: {c}" for c in require_missing]
        + [f"failed required: {c}" for c in require_failed]
        + [f"stale required: {item}" for item in require_stale]
    )
    lines.append("  Remaining before ready:")
    if remaining:
        lines.extend(f"    {item}" for item in remaining)
    elif (
        summary.required_missing
        or summary.required_failed
        or summary.required_stale
        or summary.manual_only_gaps
    ):
        # Non-require gaps remain (warn/suggest/manual): surfaced above, but
        # shipping is allowed by policy — nothing blocks readiness.
        lines.append(
            "    (none blocking — warn/suggest and manual-only receipts "
            "shipping allowed by policy)",
        )
    else:
        lines.append("    (none — declared proof complete)")

    # Channel distinction (T4): when a required receipt is missing/stale AND the
    # run only carries ad-hoc transcript commands, say outright that transcript
    # commands are not accepted as proof — the official verify commands rendered
    # below are remediation, never evidence. Gated on observed exploratory
    # commands and on an actual missing/stale blocker, so the fully-ready block
    # (and a no-exploratory block) stays byte-identical to before.
    if (summary.required_missing or summary.required_stale) and summary.exploratory_count:
        lines.append(f"  {transcript_not_proof_note()}")

    # Actionable diagnostics (ADR 0089 / T3): only when a required receipt is
    # missing/stale (``suggested_commands`` non-empty) do we name the run dirs we
    # searched and the exact verify commands. A fully-ready / no-blocker block is
    # therefore byte-identical to before.
    if summary.suggested_commands:
        if summary.searched_run_dirs:
            lines.append("  Searched run dirs:")
            lines.extend(f"    {d}" for d in summary.searched_run_dirs)
        lines.append("  Suggested verification:")
        lines.extend(f"    {c}" for c in summary.suggested_commands)

    lines.append(f"  Policy: {READINESS_POLICY_LINE}")
    return "\n".join(lines)


# ── Internals ────────────────────────────────────────────────────────────────


def _annotate_gap(text: str, policy: str, consequence: str | None = None) -> str:
    """Annotate one gap line with its effective per-gate policy (T3).

    ``require`` → ``"<text> (require)"`` (a blocker). ``warn`` / ``suggest`` →
    ``"<text> (<policy>) — shipping allowed by policy"`` (surfaced, not blocked).
    Any other non-empty policy is shown verbatim in parentheses; an empty policy
    leaves ``text`` unchanged.
    """
    if consequence == "warning" and policy == "require":
        return f"{text} ({policy}) — shipping allowed by warning consequence"
    if policy == "require":
        return f"{text} (require)"
    if policy in ("warn", "suggest"):
        recommendation = " — operator recommendation" if policy == "suggest" else ""
        return f"{text} ({policy}){recommendation} — shipping allowed by policy"
    if policy:
        return f"{text} ({policy})"
    return text


def _gap_section(
    lines: list[str],
    title: str,
    names: tuple[str, ...],
    policy_map: Mapping[str, str],
    consequence_map: Mapping[str, str],
) -> None:
    """Render a missing/failed gap section, annotating each name with its policy.

    An empty section renders the byte-identical ``"(none)"`` placeholder so a
    no-blocker readiness block is unchanged.
    """
    lines.append(f"  {title}:")
    if not names:
        lines.append("    (none)")
        return
    lines.extend(
        f"    {_annotate_gap(name, policy_map.get(name, ''), consequence_map.get(name))}"
        for name in names
    )


def _section(lines: list[str], title: str, names: tuple[str, ...]) -> None:
    lines.append(f"  {title}:")
    if names:
        lines.extend(f"    {name}" for name in names)
    else:
        lines.append("    (none)")


def _select_classification(
    command: str,
    *,
    contract: VerificationContract,
    current_receipt: Mapping[str, Any] | None,
    current_source: Any,
    parent_candidates: Any,
    current_fingerprint: str | None,
    current_head: str | None,
    dependency_heads: Mapping[str, str | None],
) -> ReceiptClassification:
    """Pick the authoritative classification for ``command`` across runs (ADR 0089).

    The selection order (review F1), against the current subject identity:

    1. The current run's receipt classifies ``present`` → it is chosen.
    2. The current run's receipt classifies ``failed`` AND its recorded
       fingerprint equals the current subject fingerprint, or either fingerprint
       is unrecorded → that ``failed`` is reported (current-run provenance) and
       no parent is consulted. A fresh failure on the same diff is never masked
       by an older parent pass — the "never falsely green" invariant.
    3. Otherwise (current receipt absent, stale, or failed against a *different*
       fingerprint) → the parent candidates whose recorded env matches the
       command's declared env (an env mismatch disqualifies a candidate outright)
       are scanned in search order and the first that classifies ``present``
       against the current subject is inherited, carrying that parent's
       provenance. Parent ``present`` is the *stricter* inheritance rule
       (:func:`_classify_parent_candidate`): the parent and the current subject
       fingerprint must both be known and equal, so a parent pass that does not
       demonstrably cover this diff is reported ``stale``, never inherited.
    4. If no parent qualified → the most informative status is reported: the
       current receipt's classification when one exists, else the first
       env-eligible parent candidate's classification (e.g. ``stale`` naming the
       fingerprint move), each with its own provenance.
    5. No usable receipt anywhere → ``missing``.

    The command's declared env (``contract.commands[command].env``, when
    non-empty) gates *eligibility*: a parent receipt from a different env is not a
    candidate for this command at all, so it can be inherited neither as present
    (3) nor surfaced as the fallback (4).

    With an empty ``parent_candidates`` this collapses to classifying the current
    receipt exactly as before (only the additive provenance fields are filled).
    """
    current_cls = (
        classify_receipt(
            current_receipt,
            current_fingerprint=current_fingerprint,
            current_head=current_head,
            dependency_heads=dependency_heads,
        )
        if current_receipt is not None
        else None
    )
    current_path = (
        receipt_file_path(current_source.run_dir, command) if current_receipt is not None else ""
    )

    # (1) current present wins outright.
    if current_cls is not None and current_cls.status == "present":
        return _with_provenance(
            current_cls,
            current_source.run_id,
            current_path,
        )

    # (2) fresh same-diff (or unrecorded-fingerprint) failure blocks inheritance.
    if (
        current_cls is not None
        and current_cls.status == "failed"
        and _failed_blocks_inheritance(current_receipt, current_fingerprint)
    ):
        return _with_provenance(
            current_cls,
            current_source.run_id,
            current_path,
        )

    # Env gates eligibility: a parent from a different env is never a candidate
    # for this command, so it can be inherited neither as present nor surfaced.
    declared_env = str(
        (contract.commands.get(command) or {}).get("env") or "",
    )
    eligible = [
        candidate
        for candidate in parent_candidates
        if not declared_env or str(candidate.receipt.get("env", "")) == declared_env
    ]

    # (3) degrade to the first eligible parent that is present against the subject.
    for candidate in eligible:
        candidate_cls = _classify_parent_candidate(
            candidate.receipt,
            current_fingerprint=current_fingerprint,
            current_head=current_head,
            dependency_heads=dependency_heads,
        )
        if candidate_cls.status == "present":
            return _with_provenance(
                candidate_cls,
                candidate.source_run_id,
                candidate.path,
            )

    # (4) most informative status with its provenance.
    if current_cls is not None:
        return _with_provenance(
            current_cls,
            current_source.run_id,
            current_path,
        )
    if eligible:
        first = eligible[0]
        first_cls = _classify_parent_candidate(
            first.receipt,
            current_fingerprint=current_fingerprint,
            current_head=current_head,
            dependency_heads=dependency_heads,
        )
        return _with_provenance(first_cls, first.source_run_id, first.path)

    # (5) nothing anywhere.
    return ReceiptClassification("missing", "missing")


def _with_provenance(
    classification: ReceiptClassification,
    source_run_id: str,
    path: str,
) -> ReceiptClassification:
    """Return ``classification`` with provenance fields filled (status/reason kept)."""
    return replace(
        classification,
        source_run_id=source_run_id or "",
        path=path or "",
    )


def _failed_blocks_inheritance(
    receipt: Mapping[str, Any] | None,
    current_fingerprint: str | None,
) -> bool:
    """True when a failed current receipt must block parent inheritance.

    Blocks when the receipt's recorded changed-files fingerprint matches the
    current subject fingerprint, OR either side's fingerprint is unrecorded
    (``None``) — an undated failure is treated as same-diff, never masked. Only a
    failure recorded against a *different* fingerprint lets a valid parent take
    over.
    """
    if receipt is None:
        return False
    git = receipt.get("git")
    git = git if isinstance(git, Mapping) else {}
    receipt_fingerprint = git.get("changed_files_fingerprint")
    if receipt_fingerprint is None or current_fingerprint is None:
        return True
    return receipt_fingerprint == current_fingerprint


def _classify_parent_candidate(
    receipt: Mapping[str, Any] | None,
    *,
    current_fingerprint: str | None,
    current_head: str | None,
    dependency_heads: Mapping[str, str | None],
) -> ReceiptClassification:
    """Classify a PARENT-run candidate under the stricter inheritance rule.

    A parent receipt may be inherited as ``present`` ONLY when it provably
    verified *this* diff: its recorded ``git.changed_files_fingerprint`` AND the
    current subject fingerprint must both be known and equal. The base
    :func:`_classify_receipt` (the unchanged current-run semantics) treats an
    *unrecorded* fingerprint — on either side — as "staleness not asserted" and
    returns ``present``; for inheritance that is not enough proof, so an
    otherwise-present parent whose fingerprint cannot be matched against the
    current checkout is reported ``stale`` with a fingerprint reason rather than
    silently inherited. This keeps the "never falsely green" invariant: a parent
    pass that does not demonstrably cover the current diff never satisfies
    delivery readiness. Non-``present`` base classifications (failed / stale /
    missing) pass through unchanged.
    """
    classification = classify_receipt(
        receipt,
        current_fingerprint=current_fingerprint,
        current_head=current_head,
        dependency_heads=dependency_heads,
    )
    if classification.status != "present" or receipt is None:
        return classification

    git = receipt.get("git")
    git = git if isinstance(git, Mapping) else {}
    receipt_fingerprint = git.get("changed_files_fingerprint")
    if current_fingerprint is None or receipt_fingerprint is None:
        return replace(
            classification,
            status="stale",
            failure_kind="stale",
            reason=(
                "parent receipt changed-files fingerprint unverifiable against current checkout"
            ),
        )
    if receipt_fingerprint != current_fingerprint:
        # Defensive: _classify_receipt already returns stale for a recorded
        # mismatch, so this is unreachable in practice; kept for symmetry.
        return replace(
            classification,
            status="stale",
            failure_kind="stale",
            reason="checkout changed-files fingerprint moved",
        )
    return classification


def _current_checkout_identity(checkout: str) -> tuple[str | None, str | None]:
    """Recompute the subject checkout's (fingerprint, head); degrade to None."""
    if not checkout:
        return None, None
    try:
        from core.io.git_helpers import git_head
        from pipeline.verification_dependencies import changed_files_fingerprint

        head = git_head(checkout)
        if head is None:
            return None, None
        return changed_files_fingerprint(checkout), head
    except Exception:  # noqa: BLE001 — readiness must never raise outward
        return None, None


def _gate_status_lines(
    contract: VerificationContract,
    plan: Any,
    status_by_command: Mapping[str, ReceiptClassification],
) -> tuple[str, ...]:
    """Per-gate status lines for the delivery-relevant scheduled gates."""

    def _status(command: str) -> str:
        classification = status_by_command.get(command)
        return classification.status if classification is not None else "unscheduled"

    lines: list[str] = []
    if plan is not None:
        for entry in getattr(plan, "entries", ()) or ():
            hook = getattr(entry, "hook", "")
            phase = getattr(entry, "phase", "")
            if (hook, phase) not in _DELIVERY_HOOKS:
                continue
            command = getattr(entry, "command", "")
            hook_label = hook + (f"({phase})" if phase else "")
            lines.append(
                f"{command} [{hook_label} {getattr(entry, 'policy', '?')}"
                f"->{getattr(entry, 'action', '?')}]: {_status(command)}",
            )
        return tuple(lines)

    for entry in contract.schedule:
        if (entry.hook, entry.phase) not in _DELIVERY_HOOKS:
            continue
        hook_label = entry.hook + (f"({entry.phase})" if entry.phase else "")
        policy_label = entry.policy or "derived"
        for command in entry.commands or tuple(sorted(contract.commands)):
            lines.append(
                f"{command} [{hook_label}/{policy_label}]: {_status(command)}",
            )
    return tuple(lines)


def _tested_dependency_lines(
    run_dir: Path | str,
    present_commands: list[str],
) -> tuple[str, ...]:
    """Compact ``"command: tested against dep@<sha>"`` lines for present receipts.

    Cross-repo summary on the readiness side: for each present required command,
    surface the depended-on dependencies (``depends_on`` true, recorded HEAD) the
    receipt was tested against. Read-only and tolerant — an old receipt without a
    ``dependencies`` block simply contributes nothing. Never raises.
    """
    if not present_commands:
        return ()
    receipts = {str(r.get("command", "")): r for r in load_command_receipts(run_dir)}
    lines: list[str] = []
    for command in present_commands:
        receipt = receipts.get(command)
        deps = receipt.get("dependencies") if isinstance(receipt, Mapping) else None
        if not isinstance(deps, list):
            continue
        bits: list[str] = []
        for entry in deps:
            if not isinstance(entry, Mapping) or not entry.get("depends_on"):
                continue
            name = entry.get("name")
            head = entry.get("head")
            if not name or not head:
                continue
            bits.append(f"{name}@{str(head)[:12]}")
        if bits:
            lines.append(f"{command}: tested against " + ", ".join(bits))
    return tuple(lines)


def _count_exploratory_commands(run_dir: Path | str) -> int:
    """Count observed ad-hoc ``command.end`` events; tolerant of any IO shape."""
    path = Path(run_dir) / "events.jsonl"
    if not path.is_file():
        return 0
    count = 0
    try:
        body = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("kind") == "command.end":
            count += 1
    return count
