# SPDX-License-Identifier: Apache-2.0
"""Presentation of the official verification-gate timeline (ADR 0095).

This module holds the *entire* render model for making a run's official
verification gates visible — the compact live blocks printed right after the
Stage 9 auto-run / correction ``gate_rerun`` and at the scheduled gate seams,
and the compact ``Verification gates`` block in the DONE/HALTED summary. It is
deliberately free of side effects beyond read-only receipt reads in the
aggregator:

* The live renderers turn either a
  :class:`pipeline.project.verification_autorun.ReceiptAutoRunResult` (auto-run)
  or a hook's recorded routing-decision records (scheduled) into plain text
  lines. They never read the receipt directory to classify a scheduled gate and
  never print — the caller owns the print and any ANSI color.
* The aggregator (:func:`build_verification_timeline`) builds one
  :class:`VerificationTimeline` of per-hook :class:`VerificationGateEvent`s from
  the two durable trails — the auto-run trail
  (``extras['verification_autorun']``) and the scheduled-gate decision trail
  (``extras['verification_gate_events']``) — plus run-level residual and on-disk
  receipt names. A scheduled gate's status is read **only** from its recorded
  routing decision (``executed_pass`` → ran/pass, ``executed_fail`` → ran/fail,
  ``skipped_fresh`` → skipped fresh, ``skipped_manual`` → skipped manual); ran/pass
  is **never** inferred from an on-disk receipt, so a fresh receipt with no
  execution this hook is a ``skipped_fresh`` event, not ran/pass and not an absent
  event. The run-level projection (``missing``/``stale``/``failed`` residual plus
  the additive ``manual-only`` / ``inherited`` / searched-dirs / verify-hint
  operator context) comes from a separate read-only readiness pass and never
  overrides a hook decision; its verify hints share their source with the
  readiness block so the two surfaces can never contradict each other.

Colors and glyphs are intentionally *not* baked into the returned strings;
callers overlay color via :mod:`core.io.ansi`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pipeline.project.verification_autorun import ReceiptAutoRunResult

# ── live blocks (from a ReceiptAutoRunResult) ─────────────────────────────


def _ordered_unique(*groups: tuple[str, ...]) -> tuple[str, ...]:
    """First-seen-order union of several name tuples."""
    seen: dict[str, None] = {}
    for group in groups:
        for name in group:
            seen.setdefault(name, None)
    return tuple(seen)


def _live_command_status(name: str, result: ReceiptAutoRunResult) -> str:
    """Status label for one command under the fixed live priority."""
    if name in result.failed:
        return "FAIL"
    if name in result.residual_required:
        return "MISSING/STALE"
    if name in result.skipped_fresh:
        return "FRESH"
    if name in result.skipped_manual:
        return "SKIPPED MANUAL"
    return "PASS"


def render_gate_live_block(
    result: ReceiptAutoRunResult, *, hook_label: str,
) -> tuple[str, ...]:
    """Render a compact live block for one auto-run / gate-rerun pass.

    Returns ``()`` when ``not result.attempted`` (dry-run, no contract, or an
    empty resolved required set) — never a bare misleading header. Otherwise it
    builds, in order:

    * a header ``Verification gates — {hook_label}`` (the caller supplies the
      hook label, e.g. ``pre-final auto-run`` or ``correction gate rerun``);
    * an ``envs:`` line of ``name PASS|FAIL`` (FAIL iff the env is in
      ``failed_envs``), omitted when no env ran;
    * a ``commands:`` line over the union of ``ran_commands``, ``skipped_fresh``,
      ``skipped_manual`` and ``residual_required``, each tagged by the fixed
      priority, joined with `` · ``; omitted when the union is empty;
    * an ``errors: N`` line when the pass captured executor errors.

    The returned strings carry no color; the caller overlays it.
    """
    if not result.attempted:
        return ()

    lines: list[str] = [f"Verification gates — {hook_label}"]

    if result.ran_envs:
        failed_envs = set(result.failed_envs)
        env_parts = [
            f"{env} {'FAIL' if env in failed_envs else 'PASS'}"
            for env in result.ran_envs
        ]
        lines.append("envs: " + " · ".join(env_parts))

    commands = _ordered_unique(
        result.ran_commands,
        result.skipped_fresh,
        result.skipped_manual,
        result.residual_required,
    )
    if commands:
        cmd_parts = [
            f"{name} {_live_command_status(name, result)}" for name in commands
        ]
        lines.append("commands: " + " · ".join(cmd_parts))

    if result.errors:
        lines.append(f"errors: {len(result.errors)}")

    return tuple(lines)


# Status token per durable scheduled routing decision. The tokens match the
# autorun live vocabulary (``render_gate_live_block``) and the caller's
# status-tint table, so a scheduled block tints identically.
_SCHEDULED_DECISION_STATUS = {
    "executed_pass": "PASS",
    "executed_fail": "FAIL",
    "skipped_fresh": "FRESH",
    "skipped_manual": "SKIPPED MANUAL",
}


def render_scheduled_gate_live_block(
    events: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
    *,
    hook_label: str,
) -> tuple[str, ...]:
    """Render a compact live block for one scheduled-gate hook firing.

    ``events`` are the durable routing-decision records this hook recorded (the
    gate_repair recorder's append-only dicts), already filtered to the hook.
    Each is classified ONLY by its recorded ``decision`` — never by reading the
    on-disk receipt directory: ``executed_pass`` -> PASS, ``executed_fail`` ->
    FAIL, ``skipped_fresh`` -> FRESH, ``skipped_manual`` -> SKIPPED MANUAL. So a
    gate with a fresh on-disk receipt that this hook did NOT execute shows FRESH,
    never PASS (and never an absent block). Builds:

    * a header ``Verification gates — {hook_label}``;
    * a ``commands:`` line of ``name STATUS`` joined with `` · ``;
    * a ``receipts:`` line of the durable receipt paths carried by executed
      decisions (omitted when none).

    Returns ``()`` when there is no recognised decision to show. The strings
    carry no color; the caller overlays it.
    """
    rendered: list[str] = []
    receipts: list[str] = []
    for record in events:
        if not isinstance(record, Mapping):
            continue
        status = _SCHEDULED_DECISION_STATUS.get(str(record.get("decision", "")))
        command = str(record.get("command", ""))
        if status is None or not command:
            continue
        rendered.append(f"{command} {status}")
        receipt_path = record.get("receipt_path")
        if receipt_path:
            receipts.append(str(receipt_path))

    if not rendered:
        return ()

    lines = [f"Verification gates — {hook_label}"]
    lines.append("commands: " + " · ".join(rendered))
    if receipts:
        lines.append("receipts: " + " · ".join(_dedupe(receipts)))
    return tuple(lines)


# ── DONE/HALTED summary ───────────────────────────────────────────────────


def _autorun_trail_entries(
    extras: Mapping[str, Any] | None, session: Mapping[str, Any] | None,
) -> list[Mapping[str, Any]]:
    """Durable auto-run trail entries from extras (primary) + session mirror.

    The append-only ``extras['verification_autorun']`` list is the durable trail
    (one ``ReceiptAutoRunResult.to_evidence()`` dict per auto-run). The per-phase
    ``session['phase_log'][phase]['verification_autorun']`` mirror is read only as
    a fallback so a stub run that recorded only the session sink is still seen.
    """
    entries: list[Mapping[str, Any]] = []
    if isinstance(extras, Mapping):
        trail = extras.get("verification_autorun")
        if isinstance(trail, list):
            entries.extend(e for e in trail if isinstance(e, Mapping))
    if not entries and isinstance(session, Mapping):
        phase_log = session.get("phase_log")
        if isinstance(phase_log, Mapping):
            for phase_entry in phase_log.values():
                if isinstance(phase_entry, Mapping):
                    entry = phase_entry.get("verification_autorun")
                    if isinstance(entry, Mapping):
                        entries.append(entry)
    return entries


# ── per-event timeline model (T1) ─────────────────────────────────────────
#
# The DONE summary above collapses every official check into one set of buckets.
# The timeline model below keeps each hook firing as its own *event* so the
# render can show a compact per-hook line ("after_phase(implement): 3 ran/pass",
# "before_delivery: 1 ran/pass, 2 skipped fresh"). Two provenances feed it:
#
# * autorun events — one per durable ``extras['verification_autorun']`` entry,
#   labelled by its derived ``source`` (Stage 9 pre-final, correction pre-review,
#   or gate rerun). ``ran_pass`` = executed minus failed, ``ran_fail`` = failed,
#   where the trail's ``failed`` is the materializer's *authoritative* on-disk
#   classification (non-zero exit, a failed assertion, or a non-empty detail —
#   not the optimistic exit code), so an exit-0 assertion/detail failure never
#   renders as ran/pass here.
# * scheduled events — grouped from ``extras['verification_gate_events']`` (the
#   gate_repair recorder), one event per hook+phase. A scheduled gate's status is
#   read ONLY from its recorded routing decision: ``executed_pass`` -> ran/pass,
#   ``executed_fail`` -> ran/fail, ``skipped_fresh`` / ``skipped_manual`` as
#   recorded. ran/pass is NEVER inferred from an on-disk receipt — a fresh
#   receipt with no execution this hook is a ``skipped_fresh`` event, not ran/pass
#   and not an absent event.
#
# Run-level residual (missing/stale required) is a separate, run-scoped readiness
# reclassification — it never overrides a hook's own recorded decision.


_AUTORUN_HOOK_LABELS = {
    "stage9_autorun": "pre-final auto-run",
    "correction_pre_review": "correction pre-review auto-run",
    "gate_rerun": "correction gate rerun",
}

_SCHEDULED_DECISION_BUCKETS = {
    "executed_pass": "ran_pass",
    "executed_fail": "ran_fail",
    "skipped_fresh": "skipped_fresh",
    "skipped_manual": "skipped_manual",
}


def autorun_hook_label(source: str, phase: str = "") -> str:
    """Human hook label for an auto-run event, derived from ``(source, phase)``."""
    label = _AUTORUN_HOOK_LABELS.get(source)
    if label is not None:
        return label
    return f"auto-run({phase})" if phase else "verification auto-run"


def scheduled_hook_label(hook: str, phase: str) -> str:
    """Human hook label for a scheduled-gate event, from ``(hook, phase)``."""
    if hook in ("before_phase", "after_phase") and phase:
        return f"{hook}({phase})"
    return hook or "scheduled"


@dataclass(frozen=True)
class VerificationGateEvent:
    """One hook firing's official gate outcome, grouped by routing decision.

    ``source`` is the provenance (``stage9_autorun`` / ``correction_pre_review`` /
    ``gate_rerun`` for auto-run events, ``scheduled`` for gate-hook events). Each
    command appears in exactly one bucket for this event.
    """

    hook_label: str
    source: str
    ran_pass: tuple[str, ...] = ()
    ran_fail: tuple[str, ...] = ()
    skipped_fresh: tuple[str, ...] = ()
    skipped_manual: tuple[str, ...] = ()
    receipt_paths: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not (
            self.ran_pass
            or self.ran_fail
            or self.skipped_fresh
            or self.skipped_manual
        )


@dataclass(frozen=True)
class VerificationTimeline:
    """The full ordered official-gate timeline for a run.

    ``events`` is the merged, ordered sequence (scheduled hooks in lifecycle
    order, then auto-run blocks). ``residual_missing`` / ``residual_stale`` /
    ``residual_failed`` are the run-level readiness residual (never derived from a
    hook decision). ``receipts`` are the on-disk command-receipt names
    (provenance, not proof of a hook execution).

    The remaining fields are additive run-level operator context, all from the
    same readiness classification pass and none overriding a per-hook decision:
    ``manual_only`` are required commands intentionally not auto-run (manual /
    operator-only), ``inherited`` are present receipts carried from a parent run
    (``"<command> from run <id> (<path>)"``), ``searched_run_dirs`` are the run
    dirs consulted for receipts, and ``suggested_commands`` are the shared
    ``orcho verify`` hints (identical to the readiness block's).
    """

    events: tuple[VerificationGateEvent, ...] = ()
    residual_missing: tuple[str, ...] = ()
    residual_stale: tuple[str, ...] = ()
    residual_failed: tuple[str, ...] = ()
    receipts: tuple[str, ...] = ()
    manual_only: tuple[str, ...] = ()
    inherited: tuple[str, ...] = ()
    searched_run_dirs: tuple[str, ...] = ()
    suggested_commands: tuple[str, ...] = ()
    # Per-gate effective delivery policy (T5, ADR 0097) for the residual
    # commands: ``(command, policy)`` pairs (require|warn|suggest), render-only.
    # Empty for a directly-constructed timeline, so the policy-classification
    # lines are omitted and such a block renders byte-identically.
    policy_by_command: tuple[tuple[str, str], ...] = ()
    # Required (``require``) gates whose failed/missing receipt was excused by an
    # exact durable ``phase_handoff_waiver`` (``gate:<command>:<round>``). These
    # are removed from the residual buckets and from ``blocking_residual`` so they
    # never read as a blocker or as passed; they render on their own waived line.
    # ``waived_details`` carries ``(command, handoff_id)`` for that render. Both
    # default empty so a directly-constructed timeline is byte-identical.
    waived: tuple[str, ...] = ()
    waived_details: tuple[tuple[str, str], ...] = ()

    def is_empty(self) -> bool:
        return not (
            self.events
            or self.residual_missing
            or self.residual_stale
            or self.residual_failed
            or self.receipts
            or self.manual_only
            or self.inherited
            or self.waived
        )

    def _residual_commands(self) -> tuple[str, ...]:
        return _ordered_unique(
            self.residual_missing, self.residual_stale, self.residual_failed,
        )

    @property
    def blocking_residual(self) -> tuple[str, ...]:
        """Residual commands whose effective policy is ``require`` (blocking).

        Empty when the policy map is absent (a directly-constructed timeline) —
        the residual is then surfaced without a policy split. A command excused
        by an exact durable waiver (``self.waived``) is never blocking.
        """
        policy = dict(self.policy_by_command)
        waived = set(self.waived)
        return tuple(
            c for c in self._residual_commands()
            if policy.get(c) == "require" and c not in waived
        )

    @property
    def warning_residual(self) -> tuple[str, ...]:
        """Residual commands whose effective policy is ``warn`` / ``suggest``.

        These are surfaced but shipping is allowed by policy (ADR 0090); never
        blocking. Empty when the policy map is absent.
        """
        policy = dict(self.policy_by_command)
        return tuple(
            c for c in self._residual_commands()
            if policy.get(c) in ("warn", "suggest")
        )


def _scheduled_gate_events(
    extras: Mapping[str, Any] | None,
) -> list[Mapping[str, Any]]:
    """Durable scheduled-gate routing-decision records from extras."""
    if not isinstance(extras, Mapping):
        return []
    from pipeline.project.gate_repair import VERIFICATION_GATE_EVENTS_KEY

    trail = extras.get(VERIFICATION_GATE_EVENTS_KEY)
    if not isinstance(trail, list):
        return []
    return [e for e in trail if isinstance(e, Mapping)]


def _autorun_events(trail: list[Mapping[str, Any]]) -> list[VerificationGateEvent]:
    """One event per attempted auto-run trail entry, labelled by its source."""
    events: list[VerificationGateEvent] = []
    for entry in trail:
        if not entry.get("attempted", False):
            continue
        source = str(entry.get("source") or "stage9_autorun")
        phase = str(entry.get("phase", ""))
        failed = {str(c) for c in _as_names(entry.get("failed"))}
        ran = [str(c) for c in _as_names(entry.get("ran_commands"))]
        events.append(
            VerificationGateEvent(
                hook_label=autorun_hook_label(source, phase),
                source=source,
                ran_pass=tuple(c for c in ran if c not in failed),
                ran_fail=tuple(c for c in ran if c in failed),
                skipped_fresh=tuple(str(c) for c in _as_names(entry.get("skipped_fresh"))),
                skipped_manual=tuple(str(c) for c in _as_names(entry.get("skipped_manual"))),
                receipt_paths=tuple(str(c) for c in _as_names(entry.get("receipt_paths"))),
            ),
        )
    return events


def _scheduled_events(
    records: list[Mapping[str, Any]],
) -> list[VerificationGateEvent]:
    """Group scheduled-gate records into one event per hook+phase, in order."""
    order: list[str] = []
    buckets: dict[str, dict[str, list[str]]] = {}
    paths: dict[str, list[str]] = {}
    for record in records:
        label = scheduled_hook_label(
            str(record.get("hook", "")), str(record.get("phase", "")),
        )
        if label not in buckets:
            order.append(label)
            buckets[label] = {b: [] for b in _SCHEDULED_DECISION_BUCKETS.values()}
            paths[label] = []
        bucket = _SCHEDULED_DECISION_BUCKETS.get(str(record.get("decision", "")))
        command = str(record.get("command", ""))
        if bucket and command:
            buckets[label][bucket].append(command)
        receipt_path = record.get("receipt_path")
        if receipt_path:
            paths[label].append(str(receipt_path))

    events: list[VerificationGateEvent] = []
    for label in order:
        b = buckets[label]
        events.append(
            VerificationGateEvent(
                hook_label=label,
                source="scheduled",
                ran_pass=_dedupe(b["ran_pass"]),
                ran_fail=_dedupe(b["ran_fail"]),
                skipped_fresh=_dedupe(b["skipped_fresh"]),
                skipped_manual=_dedupe(b["skipped_manual"]),
                receipt_paths=_dedupe(paths[label]),
            ),
        )
    return events


def _as_names(value: Any) -> tuple[Any, ...]:
    return tuple(value) if isinstance(value, (list, tuple)) else ()


def _dedupe(names: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(names))


@dataclass(frozen=True)
class _RunLevelProjection:
    """Read-only run-level residual + operator-context fields (one classify pass).

    All fields are additive operator context derived from the SAME
    :func:`classify_required_receipts` pass plus the contract's manual/operator
    declarations and the parent-run sources. None of them overrides a per-hook
    recorded decision — they are run-scoped awareness only.
    """

    residual_missing: tuple[str, ...] = ()
    residual_stale: tuple[str, ...] = ()
    residual_failed: tuple[str, ...] = ()
    manual_only: tuple[str, ...] = ()
    inherited: tuple[str, ...] = ()
    searched_run_dirs: tuple[str, ...] = ()
    suggested_commands: tuple[str, ...] = ()
    policy_by_command: tuple[tuple[str, str], ...] = ()


def _run_level_projection(
    contract: Any, run_dir: Path, extras: Mapping[str, Any] | None,
) -> _RunLevelProjection:
    """Run-level residual + operator context from ONE classify pass (never raises).

    Read-only projection over the official receipts of ``run_dir``. It makes
    exactly one :func:`classify_required_receipts` call and derives from it:

    * ``residual_missing`` / ``residual_stale`` / ``residual_failed`` — the
      required commands still open in each non-present status (no on-disk receipt
      is ever read as ran/pass here; failed is the materializer's authoritative
      classification, same source as readiness).
    * ``manual_only`` — required commands the contract marks ``manual_only`` /
      operator-only and that are NOT present: intentionally not-auto work, taken
      from the RAW manual set BEFORE subtracting ``required`` so a command that is
      both required and manual stays manual rather than reading as missing auto
      work.
    * ``inherited`` — present receipts whose ``source_run_id`` differs from the
      current run (a parent-run receipt), as ``"<command> from run <id> (<path>)"``,
      mirroring ``readiness.receipt_provenance``.
    * ``searched_run_dirs`` — the current run dir then any follow-up parent dirs.
    * ``suggested_commands`` — the shared ``orcho verify`` hints, built with the
      SAME ``(*missing, *stale, *failed)`` argument set readiness uses, so the two
      surfaces print identical guidance. Empty when no required deficit is open.

    Lazy imports; any git / IO / import failure degrades the whole projection to
    empty rather than raising.
    """
    ctx = extras.get("verification_placeholders") if isinstance(extras, Mapping) else None
    if contract is None or ctx is None:
        return _RunLevelProjection()
    from contextlib import suppress

    projection = _RunLevelProjection()
    with suppress(Exception):
        from pipeline.verification_readiness import (
            apply_environment_provenance,
            classify_required_receipts,
            delivery_gate_plan,
            effective_policy_by_command,
            suggested_verify_commands,
        )

        checkout = getattr(ctx, "checkout", "") or ""
        try:
            plan = delivery_gate_plan(contract, extras, checkout)
        except Exception:  # noqa: BLE001 — plan resolution is best-effort
            plan = None

        # The single effective classification (ADR 0108): the shared overlay folds
        # the ADR 0106 environment-provenance downgrade onto the base receipt
        # classification, so this live render and the typed SDK projection consume
        # the SAME rule and can never diverge. A phase-scheduled gate whose
        # verification_environment receipt recorded a failed check arrives already
        # classified ``failed``, so it is bucketed exactly like any failed gate
        # below (manual gates are routed to ``manual_only`` first and stay
        # non-blocking).
        classification = apply_environment_provenance(
            classify_required_receipts(
                contract, run_dir, ctx,
                checkout=checkout, extras=extras, plan=plan,
            ),
            contract,
            run_dir,
        )

        # Per-gate effective delivery policy (require|warn|suggest|manual_only),
        # so the residual can be classified blocking vs warning and manual/
        # operator-only commands kept out of the required residual (ADR 0090).
        policy_by_command = effective_policy_by_command(contract, plan)

        def _is_manual(name: str) -> bool:
            return policy_by_command.get(name) == "manual_only"

        current_run_id = run_dir.name

        missing: list[str] = []
        stale: list[str] = []
        failed: list[str] = []
        manual_only: list[str] = []
        inherited: list[str] = []
        for name, cls in classification.items():
            status = cls.status
            if status == "present":
                if cls.source_run_id and cls.source_run_id != current_run_id:
                    inherited.append(
                        f"{name} from run {cls.source_run_id} ({cls.path})",
                    )
                continue
            # Manual/operator-only required commands are surfaced separately, NOT
            # as a required residual (they never block; ADR 0090).
            if _is_manual(name):
                manual_only.append(name)
                continue
            {"missing": missing, "stale": stale, "failed": failed}[status].append(
                name,
            )

        from pipeline.verification_receipt_index import parent_sources_from_extras

        searched = (str(run_dir),) + tuple(
            s.run_dir for s in parent_sources_from_extras(extras)
        )
        suggested: tuple[str, ...] = ()
        if missing or stale or failed:
            suggested = suggested_verify_commands(
                contract, (*missing, *stale, *failed),
                run_id=current_run_id,
                project=str(getattr(ctx, "project", "") or ""),
            )

        # Only the residual commands' policies are needed downstream (the render
        # split + the finalization approved-with-warning check).
        residual_set = {*missing, *stale, *failed}
        projection = _RunLevelProjection(
            residual_missing=tuple(missing),
            residual_stale=tuple(stale),
            residual_failed=tuple(failed),
            manual_only=tuple(manual_only),
            inherited=tuple(inherited),
            searched_run_dirs=searched,
            suggested_commands=suggested,
            policy_by_command=tuple(
                (c, p) for c, p in policy_by_command.items() if c in residual_set
            ),
        )
    return projection


def build_verification_timeline(
    *,
    run_dir: Path | str,
    extras: Mapping[str, Any] | None,
    session: Mapping[str, Any] | None = None,
) -> VerificationTimeline | None:
    """Build the merged per-event official-gate timeline for a run.

    Read-only. Sources, by kind:

    * scheduled-gate events — ``extras['verification_gate_events']`` ONLY (the
      gate_repair routing-decision trail). ran/pass is never inferred from an
      on-disk receipt; a fresh receipt with no execution this hook surfaces as a
      ``skipped_fresh`` event, not as ran/pass and not as an absent event.
    * auto-run events — the durable ``extras['verification_autorun']`` trail
      (with the session ``phase_log`` mirror as a stub fallback), one event each.
    * run-level projection — a read-only readiness pass (residual missing / stale
      / failed plus the additive manual-only, inherited, searched-dirs and verify
      hints), independent of any hook's recorded decision.

    Returns ``None`` (omit the whole block) when there is no contract AND no
    receipt AND no auto-run trail AND no scheduled gate-events — and, defensively,
    whenever the resulting timeline is entirely empty.
    """
    run_dir = Path(run_dir)

    trail = _autorun_trail_entries(extras, session)
    scheduled_records = _scheduled_gate_events(extras)
    contract = extras.get("verification_contract") if isinstance(extras, Mapping) else None

    from pipeline.evidence.verification_receipt import summarize_command_receipts

    command_receipts = summarize_command_receipts(run_dir)

    if (
        contract is None
        and not command_receipts
        and not trail
        and not scheduled_records
    ):
        return None

    events: list[VerificationGateEvent] = [
        e for e in _scheduled_events(scheduled_records) if not e.is_empty()
    ]
    events.extend(e for e in _autorun_events(trail) if not e.is_empty())

    projection = _run_level_projection(contract, run_dir, extras)
    receipts = _dedupe(
        [str(r.get("command", "")) for r in command_receipts if r.get("command")],
    )

    # Verification-gate waivers: a required (``require``) gate whose failed/missing
    # receipt was excused by an exact durable ``phase_handoff_waiver``. Pull the
    # waived commands out of the residual missing/failed buckets so they no longer
    # read as blocking (or as passed) and surface them on their own waived line.
    # Stale is never waivable (T2). Matched only when the residual command's
    # effective policy is ``require`` — consistent with the Stage-6 assessment,
    # which excuses only blocking gaps. Never raises (collector degrades to {}).
    missing, failed, waived, waived_details = _apply_waivers(
        projection, extras, session,
    )

    # The shared ``orcho verify`` fix hint is for OPEN required deficits. When a
    # waiver clears the last residual command, drop the hint (and the searched-dirs
    # line it gates) so a fully-waived run does not tell the operator to re-run a
    # gate they durably accepted. Untouched when a real deficit remains.
    suggested_commands = projection.suggested_commands
    if not (missing or projection.residual_stale or failed):
        suggested_commands = ()

    timeline = VerificationTimeline(
        events=tuple(events),
        residual_missing=missing,
        residual_stale=projection.residual_stale,
        residual_failed=failed,
        receipts=receipts,
        manual_only=projection.manual_only,
        inherited=projection.inherited,
        searched_run_dirs=projection.searched_run_dirs,
        suggested_commands=suggested_commands,
        policy_by_command=projection.policy_by_command,
        waived=waived,
        waived_details=waived_details,
    )
    if timeline.is_empty():
        return None
    return timeline


def _apply_waivers(
    projection: _RunLevelProjection,
    extras: Mapping[str, Any] | None,
    session: Mapping[str, Any] | None,
) -> tuple[
    tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[tuple[str, str], ...],
]:
    """Split the require-policy residual missing/failed by exact durable waiver.

    Returns ``(kept_missing, kept_failed, waived, waived_details)``. A residual
    command is moved to ``waived`` only when an exact ``gate:<command>:<round>``
    waiver covers it AND its effective policy is ``require`` (the same gate kind
    Stage-6 excuses). ``waived_details`` carries ``(command, handoff_id)`` in the
    waived order (missing first, then failed). Best-effort: any failure degrades
    to the original residual with no waived commands.
    """
    try:
        from pipeline.verification_waiver import collect_gate_waivers

        waivers = collect_gate_waivers(extras, session)
        if not waivers:
            return (
                projection.residual_missing, projection.residual_failed, (), (),
            )
        policy = dict(projection.policy_by_command)

        def _is_waived(command: str) -> bool:
            return command in waivers and policy.get(command) == "require"

        kept_missing: list[str] = []
        kept_failed: list[str] = []
        waived: list[str] = []
        waived_details: list[tuple[str, str]] = []
        for command in projection.residual_missing:
            if _is_waived(command):
                waived.append(command)
                waived_details.append((command, waivers[command].handoff_id))
            else:
                kept_missing.append(command)
        for command in projection.residual_failed:
            if _is_waived(command):
                waived.append(command)
                waived_details.append((command, waivers[command].handoff_id))
            else:
                kept_failed.append(command)
        return (
            tuple(kept_missing), tuple(kept_failed),
            tuple(waived), tuple(waived_details),
        )
    except Exception:  # noqa: BLE001 — presentation must never break finalize
        return (projection.residual_missing, projection.residual_failed, (), ())


# Per-event DONE buckets in render order, with their human labels. Each event
# line shows only the non-empty buckets, count-first (names live on the
# ``receipts:`` / ``residual:`` lines so the per-hook rows stay compact).
_DONE_EVENT_BUCKETS: tuple[tuple[str, str], ...] = (
    ("ran_pass", "ran/pass"),
    ("ran_fail", "ran/fail"),
    ("skipped_fresh", "skipped fresh"),
    ("skipped_manual", "skipped manual"),
)


def _event_line_segments(event: VerificationGateEvent) -> str:
    """Compact ``N label`` segments for one event's non-empty buckets."""
    parts: list[str] = []
    for attr, label in _DONE_EVENT_BUCKETS:
        names = getattr(event, attr)
        if names:
            parts.append(f"{len(names)} {label}")
    return ", ".join(parts)


def render_verification_gate_done_block(
    timeline: VerificationTimeline | None,
) -> tuple[str, ...]:
    """Render the compact DONE/HALTED ``Verification gates`` per-hook timeline.

    Builds, in order: a ``Verification gates:`` header, an ``events: N official
    gate events`` count, one compact line per event (its non-empty routing
    buckets — ``ran/pass`` / ``ran/fail`` / ``skipped fresh`` / ``skipped
    manual`` — count-first), a ``receipts:`` line naming the on-disk command
    receipts, and a ``residual:`` line (``missing=… stale=…``) when a required
    proof is still open. When the residual carries any ``stale`` command, a
    following ``note:`` legend explains that ``stale`` is a passed gate whose
    checkout fingerprint later moved (typically the delivery commit shifting
    HEAD), not a failed check — so a clean direct delivery does not read as a
    broken gate. Examples::

        Verification gates:
          events: 2 official gate events
          after_phase(implement): 3 ran/pass
          before_delivery: 1 ran/pass, 2 skipped fresh
          receipts: lint, smoke, test

        Verification gates:
          events: 1 official gate events
          after_phase(implement): 1 ran/pass
          residual: stale=smoke
          note: stale = passed before a later HEAD move (e.g. the delivery
            commit), not a failed check

    Carries no color (the caller overlays it). Returns ``()`` for an absent or
    empty timeline so the omission path renders nothing.
    """
    if timeline is None or timeline.is_empty():
        return ()

    lines: list[str] = ["Verification gates:"]
    lines.append(f"  events: {len(timeline.events)} official gate events")

    for event in timeline.events:
        segments = _event_line_segments(event)
        if segments:
            lines.append(f"  {event.hook_label}: {segments}")

    if timeline.receipts:
        lines.append(f"  receipts: {', '.join(timeline.receipts)}")

    if (
        timeline.residual_missing
        or timeline.residual_stale
        or timeline.residual_failed
    ):
        parts: list[str] = []
        if timeline.residual_missing:
            parts.append(f"missing={', '.join(timeline.residual_missing)}")
        if timeline.residual_stale:
            parts.append(f"stale={', '.join(timeline.residual_stale)}")
        if timeline.residual_failed:
            parts.append(f"failed={', '.join(timeline.residual_failed)}")
        lines.append(f"  residual: {' '.join(parts)}")
        # A ``stale`` proof is a gate that PASSED, then had its checkout
        # fingerprint move underneath it (typically the delivery commit
        # shifting HEAD after the gate ran) — not a failed check. Spell that
        # out so a clean direct delivery does not read as a broken gate. The
        # delivery-commit signal is not carried on the timeline, so the legend
        # stays general rather than asserting a specific cause.
        if timeline.residual_stale:
            lines.append(
                "  note: stale = passed before a later HEAD move "
                "(e.g. the delivery commit), not a failed check",
            )

        # Per-gate policy classification of the residual (T5, ADR 0097): which
        # open commands are ``require`` (blocking — must be proven before
        # delivery) vs ``warn`` / ``suggest`` (surfaced, but shipping is allowed
        # by policy). Rendered only when the effective policy is known (a built
        # timeline), so a directly-constructed residual block is byte-identical.
        policy = dict(timeline.policy_by_command)
        if policy:
            blocking = timeline.blocking_residual
            warning = timeline.warning_residual
            if blocking:
                lines.append(f"  blocking (require): {', '.join(blocking)}")
            if warning:
                annotated = ", ".join(
                    f"{name} ({policy.get(name, 'warn')})" for name in warning
                )
                lines.append(
                    f"  warning (warn/suggest): {annotated} "
                    "— shipping allowed by policy",
                )

    # Required gates excused by an exact durable operator waiver: shown on their
    # own line — never as passed and never as a blocker (they are already out of
    # the residual / blocking buckets). The handoff id makes the durable waiver
    # auditable from the terminal.
    if timeline.waived:
        handoffs = dict(timeline.waived_details)
        for command in timeline.waived:
            handoff_id = handoffs.get(command, "")
            handoff_part = f" (handoff {handoff_id})" if handoff_id else ""
            lines.append(
                f"  waived (operator): {command}{handoff_part} "
                "— required gate accepted via durable waiver",
            )

    # Required commands intentionally NOT auto-run (manual / operator-only):
    # surfaced so an operator does not mistake them for missing auto work.
    if timeline.manual_only:
        lines.append(f"  manual-only: {', '.join(timeline.manual_only)}")

    # Present receipts carried from a parent run (distinct from current-run proof).
    if timeline.inherited:
        lines.append(f"  inherited: {', '.join(timeline.inherited)}")

    # Actionable remediation, gated on an open required deficit. The dirs we
    # searched and the verify hints share their source with the readiness block
    # (suggested_verify_commands over missing+stale+failed), so the two surfaces
    # can never contradict each other.
    if timeline.suggested_commands:
        if timeline.searched_run_dirs:
            lines.append(
                f"  searched run dirs: {', '.join(timeline.searched_run_dirs)}",
            )
        lines.append(f"  fix: {'; '.join(timeline.suggested_commands)}")

    return tuple(lines)
