# ADR 0036 — Agent session-id persistence across subprocess restart (E1)

- **Status:** Accepted. Retroactive — documents the contract shipped on
  ``main`` as commit ``ed81779`` on 2026-05-24. Drafted 2026-05-24 to
  close the gap that ADR 0035 referenced this work as "the E1
  session-continuity baseline" without an ADR to anchor it.
- **Date:** 2026-05-24 (retroactive — original commit 2026-05-24)
- **Deciders:** project owner
- **Builds on:** [ADR 0026](0026-session-aware-prompt-parts.md) —
  per-key prompt-session state and the ``_session_aware_invoke``
  writer that this ADR extends.
- **Extended by:** [ADR 0035](0035-terminal-status-and-resume-observability.md)
  — the cross-subprocess metrics aggregation and halt-time bundle
  finalization build on the same "what survives subprocess restart"
  foundation.

## Context

A run paused at a phase-handoff (ADR 0031) and resumed via
``orcho_run_resume`` runs in a **fresh pipeline subprocess**. Until
this work shipped, the new subprocess instantiated fresh
``ClaudeAgent`` / ``CodexAgent`` instances with ``self.session_id =
None``. The next ``_session_aware_invoke`` would call ``agent.invoke(
... continue_session=True ...)``, but with ``session_id=None`` the
runtime adapter had no session to resume — the provider opened a
fresh conversation, and the prompt-session delta selector (M6)
silently sent omitted-by-cache parts that the new provider session
had never seen.

Empirical bug. The round-2 PLAN agent invented ``PhaseStep`` and
``render_phase_card`` class names that don't exist in the real
``phase_card.py``. The architect had read those classes in round 1
but couldn't recall the exact names without the prior conversation
in scope. The delta selector had legitimately decided the
``role:systems_architect@0`` and ``contract:plan_json:v1:english@1``
parts were already in the provider session's cache — they were, in
**round 1's** session, which the round-2 subprocess didn't resume
into.

Two adjacent design facts framed the fix:

* ``_session_aware_invoke`` was already the single writer that
  captured ``agent.session_id`` after every successful invoke
  (ADR 0026's commit-on-success rule). The session id was therefore
  reliably present in the parent subprocess's memory; it just never
  reached the next subprocess.
* The cross-run follow-up path (parent-run → child-run via
  ``--from-run-plan``) already had a seeding helper
  ``_apply_followup_session_seeds`` that read per-role
  ``session_id`` values from the parent's persisted ``meta.phases``
  and stamped ``agent.session_id = sid`` +
  ``_followup_resume_pending = True`` on the matching slot. Intra-run
  resume needed the same wiring — what was missing was a durable
  storage for the in-flight session ids that survived subprocess exit.

## Decision

Persist ``agent.session_id`` per ``PhaseAgentConfig`` slot in the
run's SQLite checkpoint store after every successful
``_session_aware_invoke``, and merge those rows into the seed set
that ``_apply_followup_session_seeds`` applies at pipeline startup.
A single unified seed channel feeds both cross-run follow-up and
intra-run resume; on key collision the checkpoint wins because it
reflects what *this* run last did, which is strictly fresher than
any parent-meta snapshot.

### Storage shape

New table in ``pipeline/checkpoint.py``:

```sql
CREATE TABLE IF NOT EXISTS agent_sessions (
    run_id     TEXT NOT NULL,
    role_attr  TEXT NOT NULL,
    session_id TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (run_id, role_attr)
);
```

Two new ``CheckpointStore`` methods:

* ``set_agent_session(role_attr: str, session_id: str | None)`` —
  persists the id for the given slot. ``session_id=None``
  intentionally DELETEs the row (see "Burn-and-vacate" below).
* ``get_agent_sessions(run_id: str | None = None) -> dict[str, str]``
  — returns ``{role_attr: session_id}`` for the given run (default:
  own ``run_id``). Empty dict for fresh runs.

``role_attr`` is the ``PhaseAgentConfig`` slot name
(``plan_agent``, ``validate_plan_agent``, ``implement_agent``,
``review_changes_agent``, ``repair_changes_agent``,
``final_acceptance_agent``, ``hypothesis_qa_agent``,
``compliance_check_agent``) — the canonical wire-shape for "which
agent" inside the orchestrator.

### Writer — post-invoke sync

``_session_aware_invoke`` in ``pipeline/phases/builtin.py`` resolves
the slot the agent is bound to via ``_agent_to_role_attr(state,
agent)`` — identity comparison against ``state.phase_config``, NOT
the phase label. This is **CHAIN-safe**: a ``repair_changes`` phase
that dispatches the implement agent (same model on the same physical
session) saves under ``implement_agent``, not under
``repair_changes_agent``.

After the invoke returns successfully, the writer unconditionally
calls ``ckpt.set_agent_session(role_attr, agent.session_id)`` —
non-None writes upsert the row, None deletes it. The call is wrapped
in ``contextlib.suppress(Exception)`` so a checkpoint I/O failure
cannot poison an otherwise-successful invocation; the session
continues working in-memory and the next resume falls back to a
fresh provider session.

### Reader — startup seed merge

At run startup in ``pipeline/project_orchestrator.py``:

1. Open ``CheckpointStore`` (existing) for the run dir.
2. Call ``ckpt.get_agent_sessions()`` → ``{role_attr: sid}``.
3. Translate ``role_attr`` keys back to ``role`` keys via the
   inverse of ``_FOLLOWUP_ROLE_TO_AGENT_ATTR``. (Storage keys by slot
   — what's known at invoke time; the seeder reads by role —
   ``"plan"``/``"validate_plan"``/etc.) The wire-shape adapter at
   this boundary is load-bearing.
4. Merge with the existing cross-run ``followup_session_seeds`` dict
   from parent meta. **Checkpoint takes precedence on key collision**
   (write-through semantics: own-run latest beats parent-snapshot).
5. Pass the merged dict to ``_apply_followup_session_seeds`` —
   which keeps its existing ``agent.session_id = sid`` +
   ``_followup_resume_pending = True`` wiring.

The ``_ckpt`` instance is stashed in ``state.extras["_ckpt"]`` so
the invoke-time writer has a handle without re-opening the
connection.

### Burn-and-vacate semantics

``set_agent_session(role_attr, None)`` DELETEs the row. This matters
when a runtime burns its session mid-run (Claude session reset, any
path that sets ``agent.session_id = None``). Without the DELETE, a
stale row would survive into the next subprocess's startup, the
seeder would arm a session id that the provider no longer
recognises, and the next ``continue_session=True`` would fail
silently against the dead session. Always-sync-with-post-invoke-truth
keeps the checkpoint consistent with reality.

## Consequences

### What now survives subprocess restart on resume

This ADR closes the per-role session-id leg. Combined with the prior
state already preserved, a CHECKPOINT-mode resume picks up:

| State | Lives in | Rehydrated by |
|---|---|---|
| Per-role provider session ids | ``checkpoints.db`` ``agent_sessions`` | ``_ckpt.get_agent_sessions`` → ``_apply_followup_session_seeds`` |
| Completed phase log entries | ``checkpoints.db`` ``checkpoints`` | ``_ckpt.load(run_id)`` → ``session["phases"]`` setdefault |
| Active phase-handoff payload | ``meta.phase_handoff`` (in meta.json) | ``_init_session_with_atexit`` carry-forward |
| Profile name | ``meta.profile`` | ``_resolve_resume_profile`` (defaults to ``"advanced"``) |
| Task / project path | ``meta.task`` / ``meta.project`` | ``_resolve_task`` / ``_resolve_project`` |
| Decision artifact | ``<run_dir>/phase_handoff_decisions/<safe_id>.json`` | strict reader in ``_apply_phase_handoff_resume`` |
| Metrics accumulator | ``metrics.json`` (pause snapshot, ADR 0035) | ``_metrics.load_from_disk`` (ADR 0035) |

### Single unified seed channel

Cross-run follow-up and intra-run resume now feed
``_apply_followup_session_seeds`` through the same merged dict. No
parallel legacy path. No feature flag. The wire-shape adapter
(``role_attr`` ↔ ``role``) is the only translation; it has a
regression test pinning both halves.

### Backwards compatibility

Pre-ADR runs (no ``agent_sessions`` table on disk) work unchanged:
``CREATE TABLE IF NOT EXISTS`` is a no-op when the table exists,
``get_agent_sessions`` on a missing table returns ``{}``, and the
seeder gracefully handles an empty seed dict. No migration needed.

### Test coverage

* L1 unit ``TestAgentSessions`` (7 cases) in
  ``tests/unit/pipeline/lifecycle/test_checkpoint.py``: empty / set /
  replace / clear / multi-role / cross-run isolation /
  survive-close-and-reopen.
* L4 integration ``TestAgentSessionsPersistE1`` (5 cases) in
  ``tests/integration/test_checkpoint_pipeline.py``: pipeline writes
  after invoke; latest-invoke-wins; fresh ``CheckpointStore`` on the
  same on-disk file reads them (subprocess #2 simulation); regression
  for the ``role_attr → role`` translation; regression for
  DELETE-on-None.
* End-to-end via MCP validated 2026-05-24 on a mock run:
  `prompt_render` ``provider_session_id``
  stayed ``mock-claude-1`` / ``mock-codex-1`` across rounds 1→3 in
  two subprocesses; ``mock-claude`` / ``mock-codex`` counters did
  **not** increment despite the subprocess restart, confirming that
  the rehydrate path correctly armed ``continue_session`` against
  the prior session ids.

### Out of scope

* **Cross-run seed persistence beyond ``meta.phases``.** A
  ``state.cross_run_seeds`` collection that survives the parent
  subprocess and feeds child runs uniformly was sketched in the M16
  plan but is not in this ADR. The current cross-run path still
  reads parent's ``meta.phases``; this ADR only fixes the intra-run
  resume gap.
* **Provider session lifetime guarantees.** Orcho persists the id;
  whether the provider still honours it on resume is a provider
  contract (Claude session TTL, Codex behaviour). A burned session
  surfaces as ``agent.session_id = None`` and the DELETE branch
  vacates the row — no further taxonomy at this layer.

## Commit trail

* ``ed81779`` — feat(pipeline): persist agent.session_id in
  checkpoint across subprocess restart

Subsequent observability work atop this baseline:

* ADR 0035 (commits ``4a96f72`` / ``46596c3`` / ``e2ac261`` /
  ``559c8a9``) — terminal-status + resume observability completeness.
