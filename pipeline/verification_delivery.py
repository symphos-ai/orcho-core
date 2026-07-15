# SPDX-License-Identifier: Apache-2.0
"""verification_delivery.py — Stage 6 read-only delivery-gate assessment.

Pure computation of the delivery-gate verdict the commit/apply boundary needs:
the effective delivery policy, the required command-receipts that are missing /
failed / stale, and the generated runtime/verification *garbage* staged for
delivery — classified separately from the product diff. Nothing here executes a
command, writes a receipt, mutates a checkout, or blocks a transition; it only
reads and classifies (ADR 0083). The engine and orchestration layers decide what
to do with the returned :class:`DeliveryVerificationAssessment`.

The single fixed orchestration contract is
:func:`assess_delivery_verification` — the caller passes only the subject
checkout (``diff_cwd``); this module itself reads untracked / changed paths and
the checkout identity (head + changed-files fingerprint) from it. Receipt
staleness reuses the exact Stage 5 classification
(:func:`pipeline.verification_readiness.classify_required_receipts`) with
``checkout=diff_cwd``, so head/fingerprint are computed against the same subject
the Stage 3 receipt recorded its provenance against — no duplicated git logic
and no false stale. Policy ``require`` is reachable by an explicit
``delivery_policy`` in the contract or by explicitly scheduling a
``policy=require`` gate at ``before_delivery``; ``work_mode`` never escalates it.

This module imports no orchestration (``pipeline.project.*``) and resolves no
contract itself — the caller hands it the already-validated contract.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline.evidence.verification_receipt import (
    COMMAND_RECEIPTS_DIRNAME,
    ENV_RECEIPTS_DIRNAME,
    RECEIPTS_DIRNAME,
)
from pipeline.verification_contract import (
    PlaceholderContext,
    VerificationContract,
)
from pipeline.verification_policy import (
    GapEntry,
    consequence_by_command,
    effective_delivery_policy_by_command,
    partition_gaps,
)
from pipeline.verification_readiness import (
    apply_environment_provenance,
    classify_required_receipts,
    delivery_gate_plan,
)
from pipeline.verification_waiver import collect_gate_waivers

__all__ = [
    "DeliveryVerificationAssessment",
    "WaivedGate",
    "assess_delivery_verification",
    "classify_generated_paths",
    "resolve_delivery_policy",
]

#: Max characters retained for a waiver text / note preview on a
#: :class:`WaivedGate` — enough to identify the rationale without copying an
#: unbounded blob onto the durable evidence surface (T3/T4 consume this).
_WAIVER_PREVIEW_LIMIT = 200


def _waiver_preview(text: str | None) -> str | None:
    """Trim a waiver text / note to a bounded single-line preview, or ``None``."""
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped:
        return None
    if len(stripped) <= _WAIVER_PREVIEW_LIMIT:
        return stripped
    return stripped[: _WAIVER_PREVIEW_LIMIT - 1].rstrip() + "…"


# Path components that mark a generated runtime/verification artifact rather
# than product source. Matched component-by-component (never substring), so
# ``src/venv_utils.py`` is NOT garbage while ``.venv/lib/...`` is. The three
# receipts directories are imported (not literal strings) so a rename of the
# receipt layout stays single-sourced in
# :mod:`pipeline.evidence.verification_receipt`.
_GARBAGE_DIR_COMPONENTS: frozenset[str] = frozenset(
    {
        "venv",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".tox",
        "node_modules",
        RECEIPTS_DIRNAME,
        ENV_RECEIPTS_DIRNAME,
        COMMAND_RECEIPTS_DIRNAME,
    }
)


# The note appended to every warn/suggest receipt-blocker line: a gap under a
# non-require policy is surfaced, but delivery is NOT blocked by it.
SHIPPING_ALLOWED_NOTE = "shipping allowed by policy"


@dataclass(frozen=True)
class WaivedGate:
    """One required gate gap excused by a precise durable verification waiver.

    ``gate_command`` is the command whose ``failed`` / ``missing`` receipt was
    waived; ``status`` records which of the two it was. ``handoff_id`` /
    ``waiver_text`` / ``note`` carry the durable provenance (the
    ``gate:<command>:<round>`` handoff and a bounded preview of the rationale)
    so the delivery banner and persisted evidence (T3/T4) can show the waived
    gate distinctly — not as passed, not as a blocker.
    """

    gate_command: str
    status: str
    handoff_id: str = ""
    waiver_text: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class DeliveryVerificationAssessment:
    """Typed Stage 6 delivery-gate verdict (read-only).

    ``policy`` is the effective *boundary* delivery policy (manual|suggest|warn|
    require) — :func:`resolve_delivery_policy`, unchanged. The three
    ``required_*`` tuples are the required (non-manual) delivery commands whose
    receipt is missing / failed / stale; ``garbage_paths`` are generated
    artifacts detected among the subject checkout's untracked + changed paths,
    classified separately from product paths.

    Per-gate policy awareness (T2, ADR 0097): each gap carries its *effective*
    per-gate policy (from :func:`pipeline.verification_policy.effective_delivery_policy_by_command`).
    :attr:`blocking_gaps` are the gaps whose effective policy is ``require``;
    :attr:`warning_gaps` are ``warn`` / ``suggest`` gaps (delivery is allowed by
    policy); :attr:`manual_only_gaps` are gaps on manual/operator-only commands —
    visible, but NEVER counted as a missing-required receipt and NEVER blocking
    (ADR 0090). ``policy_by_command`` lists ``(command, policy)`` for every gap,
    render-only.

    :attr:`has_blockers` is true when any required (non-manual) gap or garbage
    path exists — the warn/suggest surfacing signal. :attr:`blocking` is true
    only when a ``require``-policy gap exists (or, under a ``require`` boundary,
    a garbage path) — the engine acts on ``blocking`` for a hard stop.
    """

    policy: str
    required_missing: tuple[str, ...] = ()
    required_failed: tuple[str, ...] = ()
    required_stale: tuple[str, ...] = ()
    garbage_paths: tuple[str, ...] = ()
    # Human-readable ``"command (reason)"`` strings parallel to
    # ``required_stale`` — the reason a stale receipt is stale (subject drift or
    # a depended-on dependency's HEAD move). Surfaced in :attr:`lines` only; the
    # audit/decision artifact keeps using ``required_stale`` (names) unchanged.
    stale_details: tuple[str, ...] = ()
    # Diagnostics (ADR 0089 / T3), additive and default empty so a
    # directly-constructed assessment renders byte-identical lines. The run dirs
    # searched for receipts (current run first, then follow-up parents) and the
    # actionable ``orcho verify`` hints; both are surfaced in :attr:`lines` only
    # when there is a missing required receipt, never in the decision artifact.
    searched_run_dirs: tuple[str, ...] = ()
    suggested_commands: tuple[str, ...] = ()
    # Per-gate policy partition (T2, ADR 0097), additive and render-only. Default
    # empty so a directly-constructed assessment keeps the legacy line rendering
    # and the legacy ``require``-boundary blocking fallback.
    blocking_gaps: tuple[GapEntry, ...] = ()
    warning_gaps: tuple[GapEntry, ...] = ()
    manual_only_gaps: tuple[GapEntry, ...] = ()
    policy_by_command: tuple[tuple[str, str], ...] = ()
    # Verification-gate waivers (T2), additive and default empty so a
    # directly-constructed assessment and the no-waiver path stay byte-identical:
    # the waived-handling branches never activate when these are empty. A
    # ``require`` gap whose command is covered by an exact durable
    # ``phase_handoff_waiver`` (``gate:<command>:<round>``) is removed from
    # ``blocking_gaps`` and recorded here instead — so it no longer blocks and is
    # never re-counted in ``required_failed`` / ``required_missing``.
    waived_failed: tuple[str, ...] = ()
    waived_missing: tuple[str, ...] = ()
    waived_gates: tuple[WaivedGate, ...] = ()

    @property
    def manual_only_commands(self) -> tuple[str, ...]:
        return tuple(e.command for e in self.manual_only_gaps)

    @property
    def has_blockers(self) -> bool:
        return bool(
            self.required_missing
            or self.required_failed
            or self.required_stale
            or self.garbage_paths
        )

    @property
    def blocking(self) -> bool:
        """True only when a ``require``-policy gap exists (ADR 0090).

        Prefers the per-gate partition: any ``require`` receipt gap blocks,
        regardless of the boundary ``policy``. ``garbage_paths`` blocks only when
        the boundary policy is ``require`` (its old gate). When the partition is
        absent — a directly-constructed assessment — falls back to the legacy
        ``policy == "require" AND has_blockers`` rule so existing callers and the
        ``require``-boundary garbage case keep their exact behavior.
        """
        if self.blocking_gaps:
            return True
        if self.policy == "require" and self.garbage_paths:
            return True
        if self.blocking_gaps or self.warning_gaps or self.manual_only_gaps:
            # Partition present and no require gap → not blocking.
            return False
        return self.policy == "require" and self.has_blockers

    @property
    def receipt_blocker_lines(self) -> tuple[str, ...]:
        """The missing / failed / stale receipt lines (no garbage, no hints).

        Single source for the receipt-blocker text shared by :attr:`lines` (the
        non-interactive banner) and the interactive delivery prompt, so the two
        surfaces never word the same blocker differently. Policy-aware: a
        ``require`` gap reads ``"missing required receipts: ..."`` (a blocker),
        while a ``warn`` / ``suggest`` gap reads ``"missing receipts (<policy>):
        ... — shipping allowed by policy"`` so the operator sees it is surfaced,
        not blocked, and the effective per-gate policy is named. Falls back to
        the legacy rendering for a directly-constructed assessment with no
        partition.
        """
        if self.blocking_gaps or self.warning_gaps:
            return self._partitioned_blocker_lines()
        return self._legacy_blocker_lines()

    @property
    def manual_only_line(self) -> str | None:
        """One advisory line naming manual/operator-only gaps, or ``None``.

        Manual-only gaps are visible but neither required nor blocking; the line
        states that plainly so they are never read as a missing-required receipt.
        """
        if not self.manual_only_gaps:
            return None
        names = ", ".join(f"{e.command} ({e.status})" for e in self.manual_only_gaps)
        return "manual-only receipts (operator-available, not auto-run, not required): " + names

    @property
    def diagnostic_lines(self) -> tuple[str, ...]:
        """Where we searched + the exact verify hints, when a required gap is open.

        Gated on ``suggested_commands`` (built whenever a required receipt is
        missing / failed / stale), so an actionable next step rides with EVERY
        gap kind — including a failed-only blocker — matching the readiness block
        and the DONE timeline, which both render hints on the same condition
        (ADR 0089 / 0096). An all-clean assessment carries no hints, so its
        banner stays byte-identical. Shared by :attr:`lines` and the interactive
        delivery prompt so neither surface can drop the searched dirs / hints.
        """
        if not self.suggested_commands:
            return ()
        out: list[str] = []
        if self.searched_run_dirs:
            out.append("searched: " + ", ".join(self.searched_run_dirs))
        out.extend(self.suggested_commands)
        return tuple(out)

    @property
    def lines(self) -> tuple[str, ...]:
        """Render-ready warning lines (one per non-empty blocker category)."""
        out: list[str] = list(self.receipt_blocker_lines)
        manual_line = self.manual_only_line
        if manual_line is not None:
            out.append(manual_line)
        if self.garbage_paths:
            out.append(
                "generated artifacts staged for delivery: " + ", ".join(self.garbage_paths),
            )
        out.extend(self.diagnostic_lines)
        return tuple(out)

    # ── line rendering internals ──────────────────────────────────────────────

    def _stale_detail_for(self, command: str) -> str:
        """The ``"command (reason)"`` stale detail for ``command`` (name fallback)."""
        for name, detail in zip(
            self.required_stale,
            self.stale_details,
            strict=False,
        ):
            if name == command:
                return detail
        return command

    def _partitioned_blocker_lines(self) -> tuple[str, ...]:
        out: list[str] = []
        # require gaps — hard blockers, worded exactly as the legacy banner.
        bm = [e.command for e in self.blocking_gaps if e.status == "missing"]
        bf = [e.command for e in self.blocking_gaps if e.status == "failed"]
        bs = [self._stale_detail_for(e.command) for e in self.blocking_gaps if e.status == "stale"]
        if bm:
            out.append("missing required receipts: " + ", ".join(bm))
        if bf:
            out.append("failed required receipts: " + ", ".join(bf))
        if bs:
            out.append("stale required receipts: " + ", ".join(bs))
        # warn / suggest gaps — surfaced, but shipping is allowed by policy.
        out.extend(self._warning_lines())
        return tuple(out)

    def _warning_lines(self) -> list[str]:
        out: list[str] = []
        for status, label in (
            ("missing", "missing receipts"),
            ("failed", "failed receipts"),
            ("stale", "stale receipts"),
        ):
            entries = [e for e in self.warning_gaps if e.status == status]
            if not entries:
                continue
            if status == "stale":
                names = ", ".join(
                    _warning_name(self._stale_detail_for(e.command), e.policy) for e in entries
                )
            else:
                names = ", ".join(_warning_name(e.command, e.policy) for e in entries)
            out.append(f"{label}: {names} — {SHIPPING_ALLOWED_NOTE}")
        return out

    def _legacy_blocker_lines(self) -> tuple[str, ...]:
        out: list[str] = []
        if self.required_missing:
            out.append(
                "missing required receipts: " + ", ".join(self.required_missing),
            )
        if self.required_failed:
            out.append(
                "failed required receipts: " + ", ".join(self.required_failed),
            )
        if self.required_stale:
            out.append(
                "stale required receipts: " + ", ".join(self.stale_details or self.required_stale),
            )
        return tuple(out)


def resolve_delivery_policy(contract: VerificationContract | None) -> str | None:
    """Resolve the effective Stage 6 delivery policy.

    ``None`` contract → ``None`` (no verification contract, no gate). An explicit
    ``contract.delivery_policy`` is honoured verbatim. Otherwise a contract that
    *explicitly schedules* a ``require``-policy gate at the delivery boundary
    (``before_delivery``) derives ``require``: declaring a required delivery gate
    IS the opt-in, and letting the boundary degrade to ``warn`` would deliver
    unverified changes the moment the scheduled hook is skipped (the silent-skip
    incident behind ADR 0090). A declared contract with neither → ``"warn"``.
    ``work_mode`` deliberately plays no role: it never escalates the policy on
    its own.
    """
    if contract is None:
        return None
    explicit = getattr(contract, "delivery_policy", None)
    if explicit is not None:
        return explicit
    for entry in getattr(contract, "schedule", ()) or ():
        if (
            getattr(entry, "hook", "") == "before_delivery"
            and getattr(entry, "policy", None) == "require"
        ):
            return "require"
    return "warn"


def classify_generated_paths(paths: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Return the subset of ``paths`` that are generated runtime/verification garbage.

    Deterministic, component-based: a path is garbage when any of its
    slash-separated components is in :data:`_GARBAGE_DIR_COMPONENTS` or ends with
    ``.egg-info``, or when its final component ends with ``.pyc`` / ``.pyo``.
    Order is the input order; duplicates are preserved (the caller dedupes).
    """
    out: list[str] = []
    for path in paths:
        if _is_generated(str(path)):
            out.append(str(path))
    return tuple(out)


def assess_delivery_verification(
    contract: VerificationContract | None,
    run_dir: Path | str,
    ctx: PlaceholderContext,
    extras: Mapping[str, Any] | None,
    diff_cwd: Path | str,
    baseline_ref: str = "HEAD",
) -> DeliveryVerificationAssessment | None:
    """The fixed Stage 6 orchestration contract: assess delivery readiness.

    Returns ``None`` when there is no contract. Otherwise
    reads, from ``diff_cwd`` itself:

    * untracked paths — ``git ls-files --others --exclude-standard``;
    * changed paths — ``git diff --name-only <baseline_ref>``.

    Required-receipt classification reuses
    :func:`pipeline.verification_readiness.classify_required_receipts` with
    ``checkout=str(diff_cwd)`` (same subject the Stage 3 receipt recorded). The
    garbage set is :func:`classify_generated_paths` over the deduped union of
    untracked + changed paths. The caller passes ONLY ``diff_cwd`` — never path
    lists. Never raises: git / IO failures degrade (paths → ``()``, staleness
    not asserted).
    """
    policy = resolve_delivery_policy(contract)
    if contract is None:
        return None

    # Resolve the delivery plan once so receipt classification and the per-gate
    # effective-policy lookup agree on the same scheduled gates.
    try:
        plan = delivery_gate_plan(contract, extras, str(diff_cwd))
    except Exception:  # noqa: BLE001 — assessment must never break delivery
        plan = None

    try:
        status_by_command = apply_environment_provenance(
            classify_required_receipts(
                contract,
                run_dir,
                ctx,
                checkout=str(diff_cwd),
                extras=extras,
                plan=plan,
            ),
            contract,
            run_dir,
        )
    except Exception:  # noqa: BLE001 — assessment must never break delivery
        status_by_command = {}

    # Per-gate effective delivery policy + blocking/warning/manual partition.
    # ``manual_set`` is the raw manual/operator-only command set (sdk.verify);
    # lazy-imported to keep this module's top level free of the SDK layer.
    try:
        from sdk.verify import manual_or_operator_only_commands

        manual_set = manual_or_operator_only_commands(contract)
    except Exception:  # noqa: BLE001 — assessment must never break delivery
        manual_set = set()
    policy_by_command = effective_delivery_policy_by_command(
        contract,
        plan,
        manual_set,
        boundary_policy=policy,
    )
    consequences = consequence_by_command(
        status_by_command,
        policy_by_command,
    )
    partition = partition_gaps(status_by_command, policy_by_command, consequences)

    # Verification-gate waivers (T2): a durable, operator-recorded
    # ``phase_handoff_waiver`` whose gate_command exactly matches a ``failed`` /
    # ``missing`` blocking gap excuses that gap. The matched gap is removed from
    # the blocking bucket (so ``blocking`` becomes False when no other require
    # gap remains) and recorded in the waived sets instead — never re-counted in
    # ``required_*``. Stale gaps are deliberately NOT waivable here (the waiver
    # accepts an accepted/known failure, not subject drift). Never raises.
    try:
        waivers = collect_gate_waivers(extras)
    except Exception:  # noqa: BLE001 — assessment must never break delivery
        waivers = {}
    kept_blocking: list[GapEntry] = []
    waived_gates: list[WaivedGate] = []
    for entry in partition.blocking:
        waiver = waivers.get(entry.command) if entry.status in ("failed", "missing") else None
        if waiver is None:
            kept_blocking.append(entry)
            continue
        waived_gates.append(
            WaivedGate(
                gate_command=entry.command,
                status=entry.status,
                handoff_id=waiver.handoff_id,
                waiver_text=_waiver_preview(waiver.waiver_text),
                note=_waiver_preview(waiver.note),
            )
        )
    blocking_gaps = tuple(kept_blocking)
    waived_failed = tuple(w.gate_command for w in waived_gates if w.status == "failed")
    waived_missing = tuple(w.gate_command for w in waived_gates if w.status == "missing")

    # Required (non-manual) gaps: the union of the *kept* require gaps +
    # warn/suggest buckets. Manual-only gaps are excluded from required_* (ADR
    # 0090); waived require gaps are excluded too, so a waived command never
    # also appears as a blocker line.
    required_gaps = (*blocking_gaps, *partition.warning)
    missing = tuple(e.command for e in required_gaps if e.status == "missing")
    failed = tuple(e.command for e in required_gaps if e.status == "failed")
    stale = tuple(e.command for e in required_gaps if e.status == "stale")
    stale_details = tuple(
        f"{c} ({status_by_command[c].reason})"
        if getattr(status_by_command.get(c), "reason", "")
        else c
        for c in stale
    )

    untracked = _git_output_lines(
        ["ls-files", "--others", "--exclude-standard"],
        diff_cwd,
    )
    changed = _git_output_lines(["diff", "--name-only", baseline_ref], diff_cwd)
    combined: list[str] = []
    seen: set[str] = set()
    for path in (*untracked, *changed):
        if path not in seen:
            seen.add(path)
            combined.append(path)
    garbage = classify_generated_paths(combined)

    # Diagnostics (ADR 0089 / T3): the run dirs searched for receipts (current
    # first, then follow-up parents from the SAME extras key readiness reads) and
    # the actionable verify hints — built from the shared readiness helper so the
    # delivery banner and the final-acceptance readiness block never diverge.
    from pipeline.verification_readiness import suggested_verify_commands
    from pipeline.verification_receipt_index import parent_sources_from_extras

    searched_run_dirs = (str(run_dir),) + tuple(
        s.run_dir for s in parent_sources_from_extras(extras)
    )
    suggested_commands: tuple[str, ...] = ()
    if missing or stale or failed:
        # Include ``failed`` so a failed-only blocker still carries the actionable
        # ``orcho verify`` hint — matching the readiness block and DONE timeline,
        # which build hints over the same (missing + stale + failed) set.
        suggested_commands = suggested_verify_commands(
            contract,
            (*missing, *stale, *failed),
            run_id=Path(run_dir).name,
            project=str(getattr(ctx, "project", "") or ""),
        )

    return DeliveryVerificationAssessment(
        policy=policy,
        required_missing=missing,
        required_failed=failed,
        required_stale=stale,
        garbage_paths=garbage,
        stale_details=stale_details,
        searched_run_dirs=searched_run_dirs,
        suggested_commands=suggested_commands,
        blocking_gaps=blocking_gaps,
        warning_gaps=partition.warning,
        manual_only_gaps=partition.manual_only,
        policy_by_command=tuple(policy_by_command.items()),
        waived_failed=waived_failed,
        waived_missing=waived_missing,
        waived_gates=tuple(waived_gates),
    )


# ── Internals ────────────────────────────────────────────────────────────────


def _warning_name(name: str, policy: str) -> str:
    """Render a non-blocking gap without implying automatic execution."""
    recommendation = "; operator recommendation" if policy == "suggest" else ""
    return f"{name} ({policy}{recommendation})"


def _is_generated(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    for part in parts:
        if not part:
            continue
        if part in _GARBAGE_DIR_COMPONENTS or part.endswith(".egg-info"):
            return True
    last = parts[-1] if parts else ""
    return last.endswith((".pyc", ".pyo"))


def _git_output_lines(args: list[str], cwd: Path | str) -> tuple[str, ...]:
    """Run ``git <args>`` in ``cwd`` and return non-empty stdout lines; degrade.

    Any git-side or OS-level failure (non-zero exit, missing binary, timeout)
    collapses to ``()`` — this module never raises on a git problem.
    """
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=30.0,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    if proc.returncode != 0:
        return ()
    return tuple(line.strip() for line in (proc.stdout or "").splitlines() if line.strip())
