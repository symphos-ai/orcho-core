"""Pure terminal-state writers for a run's flat state mapping (Stage 3b).

A single home for the field-level mutation that flips a run to one of its
terminal statuses (``done`` / ``halted`` / ``failed`` / ``interrupted``).
Each helper mutates a plain mutable mapping **in place** — the same flat
top-level shape whether the mapping is the in-memory ``session`` dict or a
``meta.json`` body loaded off disk. The two are byte-identical at this layer,
so one set of writers serves every lifecycle path.

These helpers do **no** file IO, emit **no** events, and touch **no**
checkpoint: persistence, ``run.end`` emission, and checkpoint status are the
caller's responsibility (the caller already owns ``save_session`` / ``emit`` /
``ckpt``). Duplicating any of that here would double-write or reorder the
``run.end`` boundary.

Stale-handoff policy (the load-bearing distinction):

- ``done`` and ``halted`` are *settled* terminals — any lingering active
  ``phase_handoff`` payload is stale and is cleared. The post-halt shape
  :func:`mark_run_halted` writes for ``halt_reason='phase_handoff_halt'``
  matches byte-for-byte what :mod:`pipeline.run_state.repair` heals a torn
  halt to (``status='halted'`` + ``halt_reason`` + ``halted_at`` + cleared
  ``phase_handoff``).
- ``failed`` and ``interrupted`` *preserve* an active ``phase_handoff``: an
  interrupted run that still carries an undecided handoff needs an operator
  decision, and :mod:`pipeline.run_state.repair` deliberately refuses to flip
  it automatically. Clearing it here would erase the very state the operator
  must act on.

Package discipline: this module depends on nothing — it must never import
runtime / resume / finalization paths, matching the rest of ``run_state``.
"""
from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any


def mark_run_done(state: dict[str, Any]) -> None:
    """Settle ``state`` to ``done`` and clear any stale active handoff.

    Sets ``status='done'`` and removes a lingering ``phase_handoff`` payload
    (a settled run carries no active handoff). ``halt_reason`` is left
    untouched — ``done`` is the success terminal and owns no halt reason.
    """
    state["status"] = "done"
    state.pop("phase_handoff", None)


def mark_run_halted(
    state: dict[str, Any],
    *,
    halt_reason: str,
    halted_at: str | None = None,
) -> None:
    """Settle ``state`` to ``halted`` and clear any stale active handoff.

    Sets ``status='halted'`` and ``halt_reason``; stamps ``halted_at`` only
    when supplied (some halt paths — e.g. quality-gate halts — carry no
    timestamp and must not gain a drifting one). Removes a lingering
    ``phase_handoff`` payload.

    For ``halt_reason='phase_handoff_halt'`` with a ``halted_at`` the
    resulting shape matches byte-for-byte the post-halt meta
    :mod:`pipeline.run_state.repair` heals a torn halt to.
    """
    state["status"] = "halted"
    state["halt_reason"] = halt_reason
    if halted_at is not None:
        state["halted_at"] = halted_at
    state.pop("phase_handoff", None)


def mark_run_awaiting_review(state: dict[str, Any]) -> None:
    """Pause ``state`` at ``awaiting_human_review`` — the plan/research tail.

    Sets ``status='awaiting_human_review'`` and **nothing else**. Unlike
    :func:`mark_run_done` / :func:`mark_run_halted`, this writer deliberately
    does **not** clear ``phase_handoff`` and writes **no** ``halt_reason``:
    ``awaiting_human_review`` is a *pause-for-review* terminal for a plan-only
    work kind (planning / research). The run produced an artifact for a human to
    sign off and the operator decision is still ahead, so the active
    ``phase_handoff`` must survive — clearing it would erase the very state the
    reviewer must act on.
    """
    state["status"] = "awaiting_human_review"


def mark_run_failed(state: dict[str, Any], *, halt_reason: str) -> None:
    """Mark ``state`` ``failed`` with ``halt_reason``, preserving the handoff.

    Sets ``status='failed'`` and ``halt_reason`` so every non-``done``
    terminal carries a non-null reason. An active ``phase_handoff`` is left
    in place — a failure does not resolve an outstanding handoff decision.
    """
    state["status"] = "failed"
    state["halt_reason"] = halt_reason


def mark_run_stalled(state: dict[str, Any], *, halt_reason: str) -> None:
    """Mark ``state`` ``failed`` for a stalled command, preserving the handoff.

    A terminal stalled command (idle-timeout escalation) is a failure terminal:
    it sets ``status='failed'`` and ``halt_reason`` exactly like
    :func:`mark_run_failed`, and likewise leaves any active ``phase_handoff``
    in place — a stall does not resolve an outstanding handoff decision, and
    ``failed`` keeps the run resumable so the recovery ``resume_from_checkpoint``
    action stays actionable. A distinct named writer (rather than calling
    ``mark_run_failed`` directly) marks the stall path at the call site and
    gives the stalled-command durable record a single, greppable home.
    """
    state["status"] = "failed"
    state["halt_reason"] = halt_reason


def mark_run_interrupted(
    state: dict[str, Any],
    *,
    interrupted_at: str,
    halt_reason: str = "interrupted",
) -> None:
    """Mark ``state`` ``interrupted``, preserving any active handoff.

    Sets ``status='interrupted'``, ``interrupted_at``, and ``halt_reason``
    (defaulting to ``'interrupted'`` — the honest minimal label for an
    abnormal exit that no signal handler could disambiguate). An active
    ``phase_handoff`` is deliberately left in place: an interrupted run that
    still carries an undecided handoff needs an operator decision, which
    :mod:`pipeline.run_state.repair` refuses to flip automatically.
    """
    state["status"] = "interrupted"
    state["interrupted_at"] = interrupted_at
    state["halt_reason"] = halt_reason


def settle_cross_terminal(
    state: dict[str, Any],
    *,
    status: str,
    halt_reason: str | None = None,
    halted_at: str | None = None,
    cross_ckpt: MutableMapping[str, Any] | None = None,
) -> None:
    """Settle a *cross* run to a terminal status, clearing any stale handoff.

    Sets ``status`` to one of the in-scope cross terminals
    (``done`` / ``failed`` / ``halted`` / ``cancelled``); sets ``halt_reason``
    when supplied and ``status != 'done'`` (``done`` is the success terminal
    and owns no halt reason); stamps ``halted_at`` only when supplied; and
    **always clears any lingering active** ``phase_handoff`` payload.

    Why this differs from the single-project Stage 3b writers
    (:func:`mark_run_failed` / :func:`mark_run_interrupted`, which *preserve*
    an active handoff): a single-project run can reach ``failed`` /
    ``interrupted`` with a genuinely *undecided* operator handoff still
    outstanding — clearing it would erase the very state the operator must act
    on, so repair deliberately refuses to flip it. A **cross** run never
    leaves a legitimately-active operator handoff at a terminal: every cross
    pause short-circuits the run to a final terminal before finalization, so
    any remaining ``phase_handoff`` payload at a cross terminal — for *every*
    in-scope terminal, ``failed`` included — is stale. Leaving it would make
    RunControl / resume surface an outdated pending action for a run that is
    already over.

    This is the **single settle-only clearing point** for the cross
    *gate-decision* residue :data:`CROSS_SETTLE_RESIDUE_KEYS` (today:
    ``pending_gate``). The session mirror is always evicted here; the
    persisted checkpoint copy is evicted too when the caller threads
    ``cross_ckpt`` (every cross finalize wrapper does). pending_gate is
    deliberately NOT a *handoff* marker and is never cleared at a handoff
    site — see :data:`CROSS_HANDOFF_MARKER_KEYS`.

    Pure in-place mutation: no file IO, no event emit, no checkpoint write —
    the caller owns persistence and the single ``run.end`` boundary. Does not
    introduce a new durable status; ``cancelled`` is already written by the
    existing contract_check ABORT path.
    """
    state["status"] = status
    if status != "done" and halt_reason is not None:
        state["halt_reason"] = halt_reason
    if halted_at is not None:
        state["halted_at"] = halted_at
    state.pop("phase_handoff", None)
    # Settle-only eviction of the gate-decision residue: the session mirror
    # always, the persisted checkpoint copy when threaded. eviction-only — no
    # IO; the caller persists ``cross_ckpt`` via ``write_cross_checkpoint``.
    evict_cross_settle_residue(state)
    if cross_ckpt is not None:
        evict_cross_settle_residue(cross_ckpt)


# ── canonical stale-key eviction (ADR 0115, slice 1) ───────────────────

#: The single canonical set of *transient settle residue* keys on a run's flat
#: state mapping. Each key is written by an earlier (non-terminal, or a prior
#: rejected attempt's) lifecycle step and is stale the moment a finalization /
#: settle reconcile drives the run to a *settled* terminal (``done`` /
#: ``halted``). Evicting the set keeps meta.json, ``run.end``, SDK status, and
#: the ADR 0114 read-model projections reconciled to the authoritative terminal
#: outcome instead of letting different surfaces read different residue. This
#: replaces three divergent hand-rolled eviction lists (ADR 0115 §"Three
#: divergent stale-key eviction lists").
#:
#: Per-key justification — point of write → why it is transient at a settled
#: terminal and not read by a legitimate later phase:
#:
#: * ``phase_handoff`` — the active operator-handoff payload (written by
#:   :mod:`pipeline.run_state.handoff`). A *settled* terminal carries no active
#:   handoff; :func:`mark_run_done` / :func:`mark_run_halted` already clear it,
#:   so listing it here only makes the canonical set self-contained (no
#:   behaviour change).
#: * ``halt`` — the nested halt compat block written by
#:   ``finalization._resolve_terminal_status``; phase-specific halt detail that
#:   is meaningless once the halt is superseded by a ``done`` settle.
#: * ``halt_reason`` / ``halted_at`` — top-level halt residue written by
#:   :func:`mark_run_halted`; a now-``done`` settle owns no halt reason. This
#:   also covers *value-level* halt reasons such as ``correction_not_converging``
#:   and ``final_acceptance_no_diff`` — they are values of ``halt_reason``, not
#:   separate top-level keys.
#: * ``rejected_outcome`` / ``delivery_override`` — the ADR 0106 rejected-release
#:   terminal marker and operator-override marker (written by
#:   ``finalization._apply_rejected_release_terminal_outcome``); a superseding
#:   APPROVED / delivered settle invalidates them.
#: * ``no_op_outcome`` — the ``final_acceptance_no_diff`` halted display marker
#:   (``finalization._apply_no_diff_final_acceptance_outcome``); read only for
#:   the halted no-op banner, never by a resume predicate or later phase (grep:
#:   only the finalization writer + tests read it).
#: * ``correction_fixed_point`` — the ADR 0098 non-convergence display block;
#:   read only by the halted non-convergence banner
#:   (``finalization._non_convergence_lines``) and a ``state.extras`` scope-
#:   expansion read, never by a resume predicate; stale once the run settles
#:   ``done``.
#:
#: Deliberately **NOT** in the set (each retained with cause):
#:
#: * ``phase_handoff_waiver`` — a *durable* operator waiver record (ADR 0073)
#:   read by evidence collection AFTER settle
#:   (``pipeline/evidence/collector.py``); evicting it would drop legitimate
#:   audit state, so it is not residue.
#: * ``commit_delivery`` / ``multi_project_delivery`` — delivery records whose
#:   eviction is *conditional* decision-logic owned by each call-site (a
#:   phantom-gate / companion-mirror guard), never unconditional residue.
#: * ``phase_handoff_override`` / ``human_feedback`` / ``last_critique`` — not
#:   keys of this flat mapping at all: they live on ``state.extras`` / the
#:   runtime ``State`` object, outside the settle mapping this helper owns.
TRANSIENT_SETTLE_KEYS: tuple[str, ...] = (
    "phase_handoff",
    "halt",
    "halt_reason",
    "halted_at",
    "rejected_outcome",
    "delivery_override",
    "no_op_outcome",
    "correction_fixed_point",
)


def evict_transient_settle_keys(state: MutableMapping[str, Any]) -> None:
    """Pop every :data:`TRANSIENT_SETTLE_KEYS` entry from ``state`` in place.

    The single canonical stale-key eviction for a *settled* terminal
    (``done`` / ``halted``) reconcile. Idempotent (``pop`` with default) and
    **eviction-only**: it clears residue and never writes ``status`` /
    ``halt_reason`` / a delivery record, and never touches the *conditional*
    delivery keys (``commit_delivery`` / ``multi_project_delivery``) — those
    stay on each call-site's own guard, which decides *when* to clear them.

    Works on any ``MutableMapping`` so the same canon serves the in-memory
    ``session`` dict, a ``meta.json`` body loaded off disk, and a parent run's
    meta superseded by a follow-up.
    """
    for key in TRANSIENT_SETTLE_KEYS:
        state.pop(key, None)


# ── canonical cross-project stale-key eviction (ADR 0115, slice 4) ──────
#
# Two DISJOINT canonical sets model the two distinct ways cross residue goes
# stale. Each is eviction-only and idempotent. The disjointness is
# load-bearing: ``pending_gate`` becomes stale at *settle* (the run is over),
# the handoff markers become stale at *handoff consumption* (the operator
# decision is applied) — and ``pending_gate`` must NEVER be cleared at a
# handoff site (it is not a handoff marker). Keeping the sets separate is what
# guarantees the single settle-only clearing point for ``pending_gate``.


#: Cross **settle-only** residue — keys that are stale the moment a *cross*
#: run reaches any terminal via :func:`settle_cross_terminal`, regardless of
#: which path got it there. Cleared at exactly one place: the settle substrate
#: both cross finalize wrappers funnel through. Never cleared at a handoff
#: site.
#:
#: Per-key justification — point of write → why stale at a settled terminal:
#:
#: * ``pending_gate`` — the operator gate-decision payload written by the
#:   contract_check PAUSE branch onto BOTH the session and the cross
#:   checkpoint (``contract_check.py``), and restored on resume
#:   (``run_setup.py``). A cross run that reaches *any* terminal has, by
#:   construction, resolved or abandoned that gate (the ABORT path cancels;
#:   RUN/SKIP proceed to a real terminal), so a lingering ``pending_gate`` is
#:   stale resume-residue that would make RunControl / the cross resume
#:   read-model surface an outdated pending action for a run that is already
#:   over. There is no other clearing point anywhere — settle is the single
#:   one.
CROSS_SETTLE_RESIDUE_KEYS: tuple[str, ...] = (
    "pending_gate",
)


#: Cross **handoff-only** markers — the kind-discriminated handoff-pause
#: checkpoint fields written when a cross run pauses for an operator decision.
#: Cleared at every handoff-*consumption* site (after the decision is applied
#: / dispatched), NOT at settle. Deliberately EXCLUDES ``pending_gate`` (a
#: gate-decision payload, settle-only — see :data:`CROSS_SETTLE_RESIDUE_KEYS`)
#: and ``cfa_paused_state`` (CFA-specific resume state, popped locally by the
#: CFA gate with its own justification, never a shared handoff marker).
#:
#: Per-key justification — why stale once the handoff decision is consumed:
#:
#: * ``phase_handoff_pending`` — the active-pause boolean. Set ``False`` (not
#:   popped) once the decision is applied so a subsequent resume cannot
#:   re-enter the pause branch on a stale ``True``.
#: * ``phase_handoff_id`` — the decision-artifact id the pause published; once
#:   the artifact is loaded and dispatched it points at a consumed decision.
#: * ``phase_handoff_kind`` — the ``plan`` / ``project`` / ``cfa`` resume
#:   dispatch discriminator; stale after consumption — leaving it would let a
#:   later resume mis-route on the prior kind (the partial-clear bug this
#:   slice removes left exactly this key behind).
#: * ``phase_handoff_project_alias`` / ``phase_handoff_child_id`` — the
#:   ``kind == "project"`` child-handoff coordinates; meaningless once that
#:   child decision is dispatched. Popped unconditionally (idempotent) so the
#:   ``plan`` / ``cfa`` kinds, which never set them, are unaffected.
CROSS_HANDOFF_MARKER_KEYS: tuple[str, ...] = (
    "phase_handoff_id",
    "phase_handoff_kind",
    "phase_handoff_project_alias",
    "phase_handoff_child_id",
)


def evict_cross_settle_residue(state: MutableMapping[str, Any]) -> None:
    """Pop every :data:`CROSS_SETTLE_RESIDUE_KEYS` entry from ``state``.

    The single settle-only eviction for the cross gate-decision residue.
    Idempotent (``pop`` with default) and **eviction-only**: clears residue,
    never writes ``status`` / ``halt_reason`` / a delivery record, and never
    touches the conditional delivery keys (``commit_delivery`` /
    ``multi_project_delivery``) or the positive ``phase0_done`` progress
    marker. Serves both the in-memory session mirror and the cross-checkpoint
    copy of ``pending_gate`` (the caller persists the checkpoint).
    """
    for key in CROSS_SETTLE_RESIDUE_KEYS:
        state.pop(key, None)


def evict_cross_handoff_markers(cross_ckpt: MutableMapping[str, Any]) -> None:
    """Clear the cross handoff-pause markers in place after consumption.

    Sets ``phase_handoff_pending=False`` and pops every
    :data:`CROSS_HANDOFF_MARKER_KEYS` entry. The single canonical eviction for
    a consumed cross handoff — it replaces the divergent inline pop-lists that
    used to live at each handoff site (the planning-loop halt/continue/
    retry-approved partial clears, the two ``handoff.py`` blocks, and the CFA
    gate's shared-marker clear). Idempotent and **eviction-only**: it never
    writes ``status`` / ``halt_reason``, never touches ``pending_gate``
    (settle-only) or ``cfa_paused_state`` (CFA-local), and leaves the positive
    ``phase0_done`` progress marker for the call-site to set separately.
    """
    cross_ckpt["phase_handoff_pending"] = False
    for key in CROSS_HANDOFF_MARKER_KEYS:
        cross_ckpt.pop(key, None)


__all__ = [
    "CROSS_HANDOFF_MARKER_KEYS",
    "CROSS_SETTLE_RESIDUE_KEYS",
    "TRANSIENT_SETTLE_KEYS",
    "evict_cross_handoff_markers",
    "evict_cross_settle_residue",
    "evict_transient_settle_keys",
    "mark_run_awaiting_review",
    "mark_run_done",
    "mark_run_failed",
    "mark_run_halted",
    "mark_run_interrupted",
    "mark_run_stalled",
    "settle_cross_terminal",
]
