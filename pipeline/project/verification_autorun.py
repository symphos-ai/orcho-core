# SPDX-License-Identifier: Apache-2.0
"""Materialise missing/stale required verification receipts for a run.

Shared executor behind Stage 9 auto-run: before ``final_acceptance`` and the
delivery gate read readiness, a run's *missing* and *stale* required delivery
receipts are regenerated once so the reviewer sees ``present`` evidence instead
of a gap it cannot itself fix. The materializer is deliberately narrow:

* It never reruns a ``failed`` receipt of the current subject — a fresh
  same-diff failure must stay failed, never silently re-greened.
* It never auto-runs commands a contract marks ``manual_only`` or parks behind
  an unrequested operator gate-set; those remain an explicit operator
  escape-hatch (``skipped_manual``).
* It executes strictly through :mod:`sdk.verify` (one env pass + one command
  pass — no retry loop) and degrades every executor failure into ``errors``
  rather than raising: the authoritative release verdict still belongs to
  ``final_acceptance`` / the delivery gate, which re-read the receipts from disk.

Classification is delegated to :func:`pipeline.verification_readiness.classify_required_receipts`
with an explicit :class:`~pipeline.verification_contract.PlaceholderContext`, so
staleness (including ADR 0084 cross-repo dependency HEAD drift and ADR 0089
parent-run inheritance) is decided exactly as readiness/delivery decide it.

The materializer also consumes the *same* authoritative delivery-plan that
readiness (Stage 5) and the delivery gate (Stage 6) read: the full ``state.extras``
is threaded into classification so ``delivery_gate_plan`` reuses the cached
``before_delivery`` routing plan
(:data:`pipeline.verification_readiness.ROUTING_PLANS_EXTRAS_KEY`) rather than
rebuilding a fresh plan from the live checkout. That is what makes path-selected
delivery gates (for example ``cli-sdk-unit``) land in the materialized
delivery selection receipt view — so auto-run targets exactly what readiness
and delivery enforce, never a narrower ``contract.required``-only subset.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pipeline.verification_failure import failed_receipt_refresh_eligible
from pipeline.verification_readiness import (
    classify_required_receipts,
    delivery_gate_plan,
    resolve_delivery_selection,
)
from pipeline.verification_receipt_index import VERIFICATION_PARENT_RUNS_EXTRAS_KEY

if TYPE_CHECKING:
    from pipeline.verification_contract import (
        PlaceholderContext,
        VerificationContract,
    )


@dataclass(frozen=True)
class ReceiptAutoRunResult:
    """Durable evidence for one required-receipt auto-run attempt.

    ``attempted`` is ``False`` only for the strict no-op paths (dry-run, no
    contract, or an empty resolved delivery-required set once the delivery plan
    is resolved); otherwise classification ran. The tuples
    partition the required delivery commands the run touched: ``ran_commands``
    were executed this pass, ``failed`` is the subset whose *authoritative*
    on-disk receipt did not pass — a non-zero/None exit, a failed declared
    assertion, or a non-empty execution ``detail`` (re-read after the pass, so an
    exit-0 command with a failed assertion or detail surfaces here, never as
    green), ``skipped_fresh`` were already ``present``, ``skipped_manual`` were
    missing/stale but withheld as manual/operator-only. ``ran_envs`` are the
    verification envs executed once each, ``receipt_paths`` the env + command
    receipt files written, and ``errors`` the captured (never raised)
    :mod:`sdk.verify` failures.

    ``failed_envs`` and ``residual_required`` carry the *authoritative on-disk*
    state observed after the pass, not just the exit codes this call saw:
    ``failed_envs`` are envs whose receipt did not pass (``all_passed`` false),
    and ``residual_required`` are required commands still classified
    ``missing``/``stale`` once every receipt is re-read from disk (an exit-0
    command whose receipt was never written, or one left stale by dependency
    drift, surfaces here even though it is not in ``failed``). They exist so a
    caller can decide "never falsely green" from the materialised disk state
    rather than from this call's optimistic view; they are deliberately kept out
    of :meth:`to_evidence` (the Stage 9 evidence contract is fixed, ADR 0094).
    """

    attempted: bool
    reason: str
    ran_envs: tuple[str, ...] = ()
    ran_commands: tuple[str, ...] = ()
    skipped_manual: tuple[str, ...] = ()
    skipped_fresh: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    receipt_paths: tuple[str, ...] = ()
    failed_envs: tuple[str, ...] = ()
    residual_required: tuple[str, ...] = ()

    def to_evidence(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "reason": self.reason,
            "ran_envs": list(self.ran_envs),
            "ran_commands": list(self.ran_commands),
            "skipped_manual": list(self.skipped_manual),
            "skipped_fresh": list(self.skipped_fresh),
            "failed": list(self.failed),
            "errors": list(self.errors),
            "receipt_paths": list(self.receipt_paths),
        }


def _workspace_for_run_dir(output_dir: Path) -> str | None:
    """Return ``<workspace>`` for ``<workspace>/runspace/runs/<run_id>``."""
    try:
        if output_dir.parent.name == "runs" and output_dir.parent.parent.name == "runspace":
            return str(output_dir.parent.parent.parent)
    except IndexError:
        return None
    return None


def _env_for_command(contract: VerificationContract, command: str) -> str:
    """Declared env for ``command`` (else the contract default env)."""
    spec = contract.commands.get(command)
    declared = spec.get("env") if isinstance(spec, dict) else None
    return str(declared or contract.default_env or "")


def materialize_required_receipts(
    *,
    run_id: str,
    run_dir: Path | str,
    project_dir: str,
    checkout: str,
    contract: VerificationContract | None,
    ctx: PlaceholderContext | None,
    workspace: str | None = None,
    parent_sources: Any = (),
    extras: Mapping[str, Any] | None = None,
    dry_run: bool = False,
    reason: str,
) -> ReceiptAutoRunResult:
    """Regenerate a run's missing/stale required receipts in a single pass.

    ``ctx`` is the explicit classification context; it carries the
    checkout/project/workspace/run_dir and resolved dependency paths
    :func:`classify_required_receipts` needs for staleness (including Stage 8
    parent/dependency continuity). When ``ctx is None`` it is built
    deterministically from the resolved run paths via
    :func:`pipeline.verification_contract.placeholder_context_for`.

    ``extras`` is the run's full ``state.extras`` and is threaded *verbatim* into
    both :func:`classify_required_receipts` calls (the start targeting pass and
    the post-recheck residual pass). It already carries
    :data:`pipeline.verification_readiness.ROUTING_PLANS_EXTRAS_KEY` (the cached
    ``before_delivery`` routing plan) and
    :data:`pipeline.verification_receipt_index.VERIFICATION_PARENT_RUNS_EXTRAS_KEY`
    (ADR 0089 parent sources). Threading the full extras makes
    ``delivery_gate_plan`` reuse the *same* authoritative plan readiness/delivery
    use instead of rebuilding a fresh plan from the live checkout — so
    path-selected delivery gates (for example ``cli-sdk-unit``) are materialized.

    ``parent_sources`` (ADR 0089) is an explicit fallback: when given and the
    parent-runs key is absent from ``extras``, it is merged into a private copy
    (``dict(extras or {})``, never mutating the caller's mapping) so parent-receipt
    inheritance still matches readiness.

    No-op (``attempted=False``) when ``dry_run`` is set, no contract is declared,
    or the resolved delivery-required set is empty (no required delivery command
    survives plan resolution). Otherwise classification selects ``missing`` +
    ``stale`` required commands; ``failed`` receipts are left untouched,
    ``present`` ones land in ``skipped_fresh``, and manual/operator-only ones in
    ``skipped_manual``. Each needed env runs once via :func:`sdk.verify.verify_env`
    and the surviving target commands run once via :func:`sdk.verify.verify_run`.
    Any :mod:`sdk.verify` exception is captured in ``errors`` and never raised.
    """
    if dry_run:
        return ReceiptAutoRunResult(
            attempted=False,
            reason="dry_run; verification receipt auto-run is a no-op",
        )
    if contract is None:
        return ReceiptAutoRunResult(
            attempted=False,
            reason="no verification contract; no required receipts to materialize",
        )

    run_dir = Path(run_dir)
    resolved_workspace = workspace or _workspace_for_run_dir(run_dir)

    if ctx is None:
        from pipeline.verification_contract import placeholder_context_for

        ctx = placeholder_context_for(
            contract,
            checkout=checkout,
            project=project_dir,
            workspace=resolved_workspace or "",
            run_dir=str(run_dir),
        )

    # The authoritative delivery plan + parent sources flow through the *full*
    # extras: ROUTING_PLANS_EXTRAS_KEY lets ``delivery_gate_plan`` reuse the
    # cached before_delivery plan (the same one readiness/delivery read), so
    # path-selected gates land in the required set. ``parent_sources`` is only a
    # fallback when extras lacks the parent-runs key; merge into a copy so the
    # caller's mapping is never mutated.
    classify_extras: dict[str, Any] = dict(extras or {})
    if parent_sources and VERIFICATION_PARENT_RUNS_EXTRAS_KEY not in classify_extras:
        classify_extras[VERIFICATION_PARENT_RUNS_EXTRAS_KEY] = tuple(parent_sources)

    # Start targeting pass: resolve required delivery commands from the cached
    # plan. classify_required_receipts is read-only and never raises; running it
    # before importing sdk.verify keeps the executor (and its placeholder/loader
    # chain) off the import path on the empty-required no-op below.
    classification = classify_required_receipts(
        contract, run_dir, ctx, checkout=checkout, extras=classify_extras,
    )
    if not classification:
        return ReceiptAutoRunResult(
            attempted=False,
            reason=(
                "no required delivery commands after plan resolution; "
                "no required receipts to materialize"
            ),
        )

    # Failed-receipt refresh is based only on the official receipt physically
    # owned by this run. A parent candidate selected by readiness inheritance,
    # or an unavailable current identity, is never evidence for execution.
    from pipeline.evidence.verification_receipt import load_command_receipts
    from pipeline.verification_subject import (
        VerificationSubjectAvailable,
        capture_verification_subject,
    )

    current_receipts = {
        str(receipt.get("command", "")): receipt
        for receipt in load_command_receipts(run_dir)
    }
    try:
        captured_subject = capture_verification_subject(Path(checkout)) if checkout else None
        current_subject = (
            captured_subject.identity
            if isinstance(captured_subject, VerificationSubjectAvailable)
            else None
        )
    except Exception:  # noqa: BLE001 — identity errors remain fail-closed
        current_subject = None

    # Materialization covers selected engine identities at every delivery
    # position.  Receipt reads remain command-level, but execution ownership
    # comes from the typed resolver so manual/suggest never reach sdk.verify.
    # This includes after_phase(implement): a repair can make its earlier
    # receipt stale before final acceptance, and the pre-final pass is the only
    # automatic path that can refresh that delivery-enforced proof.
    try:
        plan = delivery_gate_plan(contract, classify_extras, checkout)
        selection = resolve_delivery_selection(contract, plan)
    except Exception:  # noqa: BLE001 — no resolved identity means no execution
        selection = None
    engine_commands: set[str] = set()
    operator_commands: set[str] = set()
    for resolved in getattr(selection, "identities", ()):
        # Operator-owned identities remain visible in the receipt view whatever
        # their hook; they are withheld as ``skipped_manual``.  Any selected
        # engine identity in the delivery receipt view is materialized.
        if resolved.executor == "operator":
            operator_commands.add(resolved.identity.command)
        elif resolved.executor == "engine":
            engine_commands.add(resolved.identity.command)
    # A command can have an operator-owned phase identity and an automatic
    # before-delivery identity. Receipt materialization is command-level, so the
    # delivery executor wins; only commands with no delivery executor are
    # withheld as manual.
    operator_commands.difference_update(engine_commands)

    # Lazy: keep sdk.verify (and its placeholder/loader chain) off this module's
    # import path until a real engine-owned materialization runs.
    verify_env = verify_run = None

    targets: list[str] = []
    skipped_manual: list[str] = []
    skipped_fresh: list[str] = []
    for command, status in classification.items():
        if command not in engine_commands and command not in operator_commands:
            continue
        if (
            failed_receipt_refresh_eligible(
                current_receipts.get(command), current_subject=current_subject,
            )
        ):
            if command in operator_commands:
                skipped_manual.append(command)
            else:
                targets.append(command)
            continue
        if status.status == "present":
            skipped_fresh.append(command)
            continue
        # Same-subject and unverifiable failed receipts remain failed and are
        # intentionally left out of every execution bucket.
        if status.status in ("missing", "stale", "unverifiable"):
            if command in operator_commands:
                skipped_manual.append(command)
            else:
                targets.append(command)

    if targets:
        from sdk.verify import verify_env as _verify_env, verify_run as _verify_run

        verify_env, verify_run = _verify_env, _verify_run

    ran_envs: list[str] = []
    ran_commands: list[str] = []
    exit_failed: list[str] = []
    failed_envs: list[str] = []
    errors: list[str] = []
    receipt_paths: list[str] = []

    seen_envs: set[str] = set()
    needed_envs: list[str] = []
    for command in targets:
        env_name = _env_for_command(contract, command)
        if env_name and env_name not in seen_envs:
            seen_envs.add(env_name)
            needed_envs.append(env_name)

    for env_name in needed_envs:
        try:
            assert verify_env is not None
            env_result = verify_env(
                project=project_dir,
                env=env_name,
                run_id=run_id,
                workspace=resolved_workspace,
                runs_dir=run_dir.parent,
                subject_checkout=checkout or None,
            )
        except Exception as exc:  # noqa: BLE001 — degrade to evidence, never raise
            errors.append(f"verify_env[{env_name}] {type(exc).__name__}: {exc}")
            continue
        ran_envs.append(env_name)
        # Authoritative: a failed env receipt (all_passed false) must surface
        # even though verify_env returned without raising. Absent flag => not
        # proven green.
        if not getattr(env_result, "all_passed", False):
            failed_envs.append(env_name)
        if env_result.receipt_path is not None:
            receipt_paths.append(str(env_result.receipt_path))

    if targets:
        try:
            assert verify_run is not None
            run_result = verify_run(
                project=project_dir,
                run_id=run_id,
                workspace=resolved_workspace,
                runs_dir=run_dir.parent,
                commands=list(targets),
                # Both receipt kinds receive the same resolved physical subject.
                subject_checkout=checkout or None,
            )
        except Exception as exc:  # noqa: BLE001 — degrade to evidence, never raise
            errors.append(f"verify_run {type(exc).__name__}: {exc}")
        else:
            for outcome in run_result.outcomes:
                ran_commands.append(outcome.command)
                if outcome.receipt_path is not None:
                    receipt_paths.append(str(outcome.receipt_path))
                if outcome.exit_code != 0:
                    exit_failed.append(outcome.command)

    # Re-read the receipts from disk and re-classify: this is the authoritative
    # "did the materialization actually leave required receipts present?" check.
    # A command that exited 0 but whose receipt was never written, or one left
    # stale by dependency drift, stays missing/stale here even though it is not
    # in ``failed`` — callers gate "never falsely green" on this, never on the
    # optimistic exit codes above. ``failed`` receipts classify as ``failed``
    # (not missing/stale), so they do not double-count into the residual set.
    post = classify_required_receipts(
        contract, run_dir, ctx, checkout=checkout, extras=classify_extras,
    )
    residual_required = [
        command
        for command, status in post.items()
        if (
            status.status in ("missing", "stale", "unverifiable")
            # An operator-owned delivery identity is intentionally withheld and
            # already reported through ``skipped_manual``.  It is not an
            # unmaterialized required receipt merely because it remains absent.
            and command not in operator_commands
        )
    ]
    # ``failed`` is authoritative, not the optimistic exit codes: a ran command
    # whose on-disk receipt classifies ``failed`` (non-zero exit, a failed
    # assertion, or a non-empty detail) is failed even when ``verify_run``
    # reported exit 0. Re-read via the post classification so the durable trail —
    # and every surface that renders it (the live block, the DONE timeline) or
    # routes on it (correction gate rerun) — can never read an assertion/detail
    # failure as green. ``missing``/``stale`` stay in ``residual_required``, so a
    # command counts in exactly one of the two sets.
    post_failed = {
        command for command, status in post.items() if status.status == "failed"
    }
    failed = [
        command
        for command in ran_commands
        if command in exit_failed or command in post_failed
    ]

    return ReceiptAutoRunResult(
        attempted=True,
        reason=reason,
        ran_envs=tuple(ran_envs),
        ran_commands=tuple(ran_commands),
        skipped_manual=tuple(skipped_manual),
        skipped_fresh=tuple(skipped_fresh),
        failed=tuple(failed),
        errors=tuple(errors),
        receipt_paths=tuple(receipt_paths),
        failed_envs=tuple(failed_envs),
        residual_required=tuple(residual_required),
    )


def _autorun_source(phase: str, reason: str) -> str:
    """Derive the additive durable ``source`` tag for one auto-run entry.

    Three official auto-run provenances feed the *same* trail sink and are told
    apart by ``(phase, reason)`` (T1): ``gate_rerun`` — the correction
    ``gate_rerun`` shortcut, recorded at the ``correction_triage`` phase;
    ``correction_pre_review`` — the pre-review materialization on a correction
    run's ``review_changes`` phase; ``stage9_autorun`` — the default
    pre-final / pre-delivery auto-run. Keyed off the durable phase first, with
    the reason as a secondary signal so a relabelled phase still resolves. This
    is purely additive: it never feeds :meth:`ReceiptAutoRunResult.to_evidence`
    (the ADR 0094 evidence keys are fixed).
    """
    lowered = reason.lower()
    if phase == "correction_triage" or "gate rerun" in lowered:
        return "gate_rerun"
    if phase == "review_changes" or "pre-review" in lowered:
        return "correction_pre_review"
    return "stage9_autorun"


def _record_autorun_evidence(run: Any, phase: str, result: ReceiptAutoRunResult) -> None:
    """Persist one auto-run result under the fixed Stage 9 evidence contract (F3).

    Two sinks: an append-only ``state.extras['verification_autorun']`` list (the
    durable audit trail across every phase that triggered an auto-run) and a
    per-phase mirror at ``session['phase_log'][phase]['verification_autorun']``
    (nested dicts created on demand). Tolerant of stub runs: a missing/odd state
    or session is simply skipped.

    The recorded entry is :meth:`ReceiptAutoRunResult.to_evidence` (the fixed
    ADR 0094 shape) enriched additively with the run ``phase`` and a derived
    ``source`` (T1), so the timeline aggregator can label each auto-run block by
    its provenance without re-deriving it. ``to_evidence`` itself is unchanged.
    """
    evidence = result.to_evidence()
    evidence["phase"] = phase
    evidence["source"] = _autorun_source(phase, result.reason)

    extras = getattr(getattr(run, "state", None), "extras", None)
    if isinstance(extras, dict):
        trail = extras.setdefault("verification_autorun", [])
        if isinstance(trail, list):
            trail.append(evidence)

    session = getattr(run, "session", None)
    if isinstance(session, dict):
        phase_log = session.setdefault("phase_log", {})
        if isinstance(phase_log, dict):
            phase_entry = phase_log.setdefault(phase, {})
            if isinstance(phase_entry, dict):
                phase_entry["verification_autorun"] = evidence


def select_before_delivery_epoch(run: Any) -> Any | None:
    """Durably select the pre-final delivery epoch before receipt execution.

    The mono-project final-phase seam is its sole caller.  Correction
    pre-review materialization deliberately does not freeze delivery scope.
    """
    state = getattr(run, "state", None)
    if getattr(state, "dry_run", False):
        return None
    extras = getattr(state, "extras", {}) or {}
    contract = extras.get("verification_contract")
    if contract is None:
        return None
    from pipeline.project.verification_ledger_runtime import select_epoch
    from pipeline.project.verification_selection_context import selection_context_for_run

    return select_epoch(
        run, contract, epoch="before_delivery:",
        context=selection_context_for_run(run, contract),
    )


def _record_autorun_executions(
    run: Any,
    run_dir: Path,
    delivery_plan: Any | None,
    result: ReceiptAutoRunResult,
) -> None:
    """Attach each Stage 9 execution to its immutable receipt attempt.

    Flat command receipts remain the authoritative latest view for readiness.
    This adapter only snapshots each freshly materialized flat receipt into the
    shared scheduled-execution evidence store before adding the corresponding
    identity-scoped ledger event.
    """
    from pipeline.evidence.verification_receipt import (
        load_command_receipts,
        write_scheduled_command_receipt,
    )
    from pipeline.project.verification_ledger_runtime import record_execution
    from pipeline.verification_ledger_store import load_ledger

    receipts_by_command = {
        str(receipt.get("command", "")): receipt
        for receipt in load_command_receipts(run_dir)
    }
    entries_by_command: dict[str, list[Any]] = {}
    for entry in getattr(delivery_plan, "entries", ()):
        entries_by_command.setdefault(entry.command, []).append(entry)

    try:
        prior_executions = {
            event.identity
            for event in load_ledger(run_dir).trail
            if event.kind == "execution"
        }
    except Exception:  # noqa: BLE001 — output-dir-less adapters have no ledger
        prior_executions = set()

    for command in result.ran_commands:
        receipt = receipts_by_command.get(command)
        if receipt is None:
            continue
        for entry in entries_by_command.get(command, ()):
            evidence = write_scheduled_command_receipt(
                output_dir=run_dir,
                result=receipt,
                hook=str(getattr(entry, "hook", "")),
                phase=str(getattr(entry, "phase", "")),
            )
            receipt_path = (
                evidence.relative_to(run_dir).as_posix()
                if evidence is not None else None
            )
            identity = (entry.command, entry.hook, entry.phase)
            record_execution(
                run,
                entry,
                passed=command not in result.failed,
                receipt_evidence=receipt_path,
                rerun=identity in prior_executions,
            )
            prior_executions.add(identity)


def auto_run_required_receipts(
    run: Any, phase: str, *, reason: str, delivery_plan: Any | None = None,
) -> ReceiptAutoRunResult:
    """Materialise the run's required receipts before a final phase, and record
    durable evidence.

    Thin run-level adapter over :func:`materialize_required_receipts`. Guard
    no-ops (``state.dry_run`` set, no ``output_dir``, no projected verification
    contract) return ``attempted=False`` and record nothing — dry-run stays fully
    side-effect free. Otherwise it resolves the run context (run id / dir,
    project, projected contract + placeholder ctx, subject checkout, parent
    receipt sources) from ``run`` and ``state.extras``, runs the materializer,
    and stamps the result into both evidence sinks via
    :func:`_record_autorun_evidence`. Never raises: the materializer degrades
    executor failures into evidence and ``final_acceptance`` / delivery remain
    the authoritative gates.
    """
    state = getattr(run, "state", None)
    if getattr(state, "dry_run", False):
        return ReceiptAutoRunResult(
            attempted=False,
            reason="dry_run; verification receipt auto-run is a no-op",
        )

    output_dir = getattr(run, "output_dir", None)
    if output_dir is None:
        return ReceiptAutoRunResult(
            attempted=False,
            reason="no output_dir; cannot materialize verification receipts",
        )

    extras = getattr(state, "extras", {}) or {}
    contract = extras.get("verification_contract")
    if contract is None:
        return ReceiptAutoRunResult(
            attempted=False,
            reason="no verification contract; no required receipts to materialize",
        )

    ctx = extras.get("verification_placeholders")
    parent_sources = extras.get(VERIFICATION_PARENT_RUNS_EXTRAS_KEY, ())

    run_dir = Path(output_dir)
    project_dir = str(getattr(run, "project_path", ""))
    checkout = getattr(ctx, "checkout", "") if ctx is not None else ""
    if not checkout:
        diff_cwd = getattr(run, "_effective_diff_cwd", None)
        checkout = str(diff_cwd()) if callable(diff_cwd) else project_dir

    result = materialize_required_receipts(
        run_id=run_dir.name,
        run_dir=run_dir,
        project_dir=project_dir,
        checkout=checkout,
        contract=contract,
        ctx=ctx,
        workspace=_workspace_for_run_dir(run_dir),
        parent_sources=parent_sources,
        # Full state.extras so the materializer reuses the cached before_delivery
        # routing plan (path-selected delivery gates) that readiness and the
        # delivery gate read. parent_sources already lives inside ``extras`` under
        # VERIFICATION_PARENT_RUNS_EXTRAS_KEY, so passing both is not
        # double-counted (the materializer skips its fallback merge when the key
        # is already present).
        extras=extras,
        dry_run=False,
        reason=reason,
    )
    # The final-phase caller selected ``delivery_plan`` durably before invoking
    # sdk.verify. Correction pre-review passes no plan and cannot create a
    # premature delivery epoch.
    from pipeline.project.verification_ledger_runtime import record_reuse
    by_command: dict[str, tuple[Any, ...]] = {}
    for entry in getattr(delivery_plan, "entries", ()):
        by_command.setdefault(entry.command, ())
        by_command[entry.command] += (entry,)
    _record_autorun_executions(run, run_dir, delivery_plan, result)
    for command in result.skipped_fresh:
        for entry in by_command.get(command, ()):
            record_reuse(run, entry, fresh=True)
    _record_autorun_evidence(run, phase, result)
    return result
