# SPDX-License-Identifier: Apache-2.0
"""Canonical run-status vocabulary for lifecycle classification.

Single source of truth for the named status sets that drive resume / terminal
classification across the orchestrator. Before this module each consumer kept
its own ``frozenset`` literal; identical content drifting apart silently is the
exact regression these named sets exist to prevent.

The sets are deliberately kept **distinct by semantics even when their members
overlap**: a "failure terminal" status set is not the same concept as a
"resumable terminal" set, even though they currently share the same members.
Do not collapse sets by member-equality — they answer different questions and
may diverge.

Package discipline (load-bearing): this module lives in ``pipeline.run_state``
and is a leaf — it imports nothing from ``sdk``, runtime, resume, or
finalization paths. Pure data only; consumers import the named sets directly.
"""
from __future__ import annotations

#: Terminal-success statuses — the run finished cleanly with nothing to resume.
#: ``done`` is the canonical terminal-success status the orchestrator writes;
#: ``success`` / ``completed`` are accepted defensively so externally produced
#: metadata (third-party embedders, manual edits) is not silently rejected.
TERMINAL_SUCCESS_STATUSES = frozenset({"done", "success", "completed"})

#: Resumable terminal statuses — the run will not progress on its own, but an
#: operator can resume it via ``--resume``. ``halted`` covers explicit halt
#: decisions; ``failed`` covers crashed runs; ``interrupted`` covers
#: atexit-marked abnormal exits. ``awaiting_phase_handoff`` is intentionally
#: NOT here — it has its own handoff-decide action set.
RESUMABLE_TERMINAL_STATUSES = frozenset({"halted", "failed", "interrupted"})

#: Failure-terminal statuses — the run *failed* (as opposed to ``done`` success
#: or a deliberate ``cancelled``). A merged status in this set is what arms the
#: setup/preflight failure synthesis. Semantically distinct from
#: :data:`RESUMABLE_TERMINAL_STATUSES` (which answers "can an operator resume
#: it"), even though the two currently share the same members — keep them
#: separate so they can diverge.
FAILURE_TERMINAL_STATUSES = frozenset({"failed", "halted", "interrupted"})

#: Terminal statuses for a *cross* run — the cross-level fold treats these as
#: "the cross run is over". Includes ``cancelled`` (a deliberate cross stop)
#: alongside ``done`` / ``failed`` / ``halted``.
TERMINAL_CROSS_STATUSES = frozenset({"done", "failed", "halted", "cancelled"})

#: The run is paused awaiting an operator phase-handoff decision — the normal
#: decision-ready handoff state.
PAUSE_STATUS = "awaiting_phase_handoff"

#: Abnormal-exit terminal status. Also the "torn handoff" status: an
#: ``interrupted`` run that still carries an active handoff payload is a
#: decision-ready equivalent of :data:`PAUSE_STATUS` (the canonical
#: ``(status, active)`` decidability predicate lives in
#: ``sdk.phase_handoff._is_decidable_handoff_status``).
INTERRUPTED_STATUS = "interrupted"
