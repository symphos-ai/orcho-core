# orcho-core — Architecture Evaluation

**Date:** 2026-05-28
**Mode:** Design evaluation (not an ADR)
**Reviewer:** engineering/architecture skill
**Scope reviewed:** `CLAUDE.md`, `AGENTS.md`, `README.md`, `docs/architecture/*`,
`docs/creator/02_package_structure.md`, all 48 ADRs (statuses + ADR 0048 in full),
`pyproject.toml` entry points, and the `pipeline/` · `core/` · `agents/` · `sdk/`
package layout (~165k LoC, 235 test modules).

---

## Context

orcho-core is the Apache-2.0 engine under the orcho ecosystem: a fire-and-forget,
multi-agent software-delivery orchestrator (PLAN → BUILD → REVIEW → FIX →
FINAL_ACCEPTANCE + variants), single- and cross-project. It deliberately positions
itself *against* both interactive copilots (Claude Code, Cursor) and general agent-graph
libraries (LangChain, AutoGen): the value is an opinionated, repeatable, audit-trailed
pipeline. The codebase is mature for a single-developer pre-1.0 project — coherent
docs, an append-only ADR log, a typed SDK boundary, and a disciplined test taxonomy.

The evaluation below assumes the project's own stated philosophy (single developer,
no internal install base, "no backcompat ceremony," docs-with-code) as the bar to
judge against — not an enterprise-team bar.

---

## What's strong

**Single-owner decomposition.** The four first-class concepts (Profile / PhaseStep /
LoopStep / ExecutionMode) plus opt-in cross-cutting concerns (QualityGate, HumanReview,
Attachment, Skill) give each decision exactly one home. The overview doc's "why these
concepts exist" section is the kind of intent capture most codebases never write down.
This is the architecture's biggest asset.

**Protocol-vs-provider boundary is real, not aspirational.** Three `entry_points`
groups (`orcho.agent_runtimes`, `orcho.phases`, `orcho.skills`) are the verified public
plugin surface — `pyproject.toml` matches the docs exactly, and core ships zero built-in
skills (pure third-party group). "Core owns the protocol; plugins own provider behavior"
is enforced structurally, and re-registration-overrides-by-name is a clean extension story.

**Durable-log-first observability.** ADR 0048's replay-first event hub is the standout
design decision. Making `events.jsonl` the single source of truth and treating every
push/notification as a *wake, not a delivery* is exactly the right invariant for a system
with multiple live consumers (web subscribe, MCP long-poll). It makes notification loss
structurally non-corrupting. The non-goals are honest and the open questions are
genuinely deferred, not hand-waved.

**Presentation policy as a testability seam.** `TERMINAL`/`SILENT` with byte-identical
persisted artifacts (ADR 0046/0047), locked by signature-pin tests, cleanly separates
"human transcript" from "machine state." SILENT runs being first-class hub publishers
(0048 D7) keeps the policy from leaking into the event layer.

**Supporting discipline that compounds:** frozen dataclasses + strict invariants
(0002), typed `StepOutcome` FSM over bool/halt flags (0004/0019), declarative loops,
cache-first prompt wire layout for prefix caching (0028), side-effect-free runtime
constructors (testable without CLIs), SQLite checkpoint + worktree resumability, and
cost-marked tests with a `MockAgentProvider` for zero-LLM pipeline runs.

---

## Risks and trade-offs

**1. The back-compat tension contradicts the project's own rule.** Both `CLAUDE.md`
and `AGENTS.md` state: single developer, no internal install base, "refactor in place,
cut legacy in the same change." Yet the architecture carries dual paths the rule would
forbid: the 28-kwarg `run_pipeline` / 23-kwarg `run_cross_pipeline` `from_kwargs` shims
"for back-compat," and the `run.session["phase_handoff"]` "compat mirror" alongside the
canonical `meta.phase_handoff`. The stated exception is "honest external API
(PyPI-published contracts)." So the real question to answer explicitly, per shim: **is
the CLI kwarg surface a published contract, or internal plumbing?** If only the typed
SDK is the external boundary (ADR 0021/0042 suggest it is), the kwarg wrappers are
internal and — by the project's own standard — should collapse into the typed
`ProjectRunRequest`/`CrossRunRequest`. Right now the codebase pays maintenance for
compatibility it tells itself it doesn't need.

**2. Internal scheduling history is leaking into durable architecture docs.** Milestone
tags (M12, M14.1, M14.3, M14.4.3, M14.4.5) and granular phase IDs (5e-5 substep 6,
7d-2) appear in prose that's meant to outlive them — `phase_lifecycle.md`, the entry-point
comments, the overview. The phase-naming scheme itself is a deliberate, defensible
choice; the problem is mixing *when something shipped* into docs describing *what it
does*. These references rot the moment the milestone numbering is retired, and they
raise onboarding cost. Recommend a hard split: behavioral docs describe behavior;
provenance (milestone, ADR, commit) lives in status tables and ADR cross-links only.

**3. The ADR log has a 0011–0018 gap, which dents the append-only/audit claim.** ADRs
jump 0010 → 0019. "Append-only, supersede don't edit" is a stated invariant and a
selling point of the audit trail. Eight missing numbers with no tombstone undercuts it —
a reader can't tell if they were abandoned, renumbered, or never existed. Add an ADR
index (or stub files) recording reserved/abandoned numbers so the sequence is
self-explaining.

**4. Transposed positional `resolve(...)` signatures are a latent bug magnet.**
`provider.resolve(runtime, model, effort)` vs `registry.resolve(model, runtime, effort)`
— documented with "do not transpose these at call-sites." A doc warning is the weakest
possible enforcement for two same-arity calls with swapped meaning. Make the
transposition structurally impossible: keyword-only parameters (`*, runtime, model,
effort`) on at least one of the two. This is a small change that removes an entire class
of silent misroute.

**5. In-process-only event hub vs a known remote consumer.** ADR 0048 scopes the hub to
single-process fan-out and explicitly defers the cross-process bridge. That's a
reasonable slice boundary, but orcho-web's "future remote orcho host" is a *named*
consumer in the same doc. The sequencing risk: shaping `subscribe()` and the
backpressure policy (open question #1) against only in-process consumers, then
discovering the remote bridge wants a different cursor/ack contract. Mitigation is cheap
— sanity-check the subscribe signature against a sketched SSE/WS bridge *before* the
implementation slice locks the API, even though the bridge ships later.

**6. "Byte-identical artifacts" may be over-constrained — confirm the exclusion set.**
The TERMINAL/SILENT guarantee is powerful but expensive: every artifact write must be
provably identical across two code paths forever. Confirm what the byte-identical bar
explicitly excludes (timestamps, durations, pids, run ids, absolute paths). If those
aren't carved out, the guarantee is either secretly violated or the tests are
normalizing in ways worth documenting as part of the contract.

---

## Trade-offs worth stating plainly (not problems)

- **Opinionated workflow over general reuse.** Narrowing to software-delivery is the
  right call for the stated goal and is documented clearly; just accept that it caps
  reuse as a generic agent framework.
- **Subprocess-per-phase isolation** (session persistence across restart, ADR 0036)
  buys clean isolation and resumability at the cost of process-spawn overhead and
  session-reconstruction complexity. Fine, but it's the main performance/complexity
  tax and should stay a conscious choice as phase count grows.

---

## Recommendations (priority order)

1. **Resolve the back-compat contradiction explicitly.** Write a one-page "what is the
   external API surface" note (or an ADR) classifying each retained shim as
   published-contract vs internal. Cut the internal ones per the project's own rule.
2. **Add keyword-only args to the two `resolve(...)` methods** to make the documented
   transposition footgun structurally impossible. (Smallest effort, removes a real bug
   class.)
3. **Add an ADR index** recording 0011–0018 disposition; restore the append-only
   guarantee's credibility.
4. **De-couple behavioral docs from milestone provenance.** Move M-tags and substep IDs
   out of `phase_lifecycle.md` / overview prose into status tables and ADR links.
5. **Pre-validate the 0048 subscribe API against a sketched remote bridge** before the
   implementation slice locks it, given orcho-web is a named future consumer.
6. **Document the byte-identical exclusion set** for SILENT/TERMINAL artifacts as part
   of the boundary contract.

---

## Bottom line

This is a well-architected system with unusually strong intent documentation and a
genuinely good observability backbone (the replay-first / wake-is-not-delivery design is
the highlight). The most actionable finding is internal: the project's "no backcompat
ceremony" rule and its retained CLI kwarg shims are in direct tension, and resolving that
honestly would both shrink the surface and restore the codebase's consistency with its
own stated philosophy. Everything else is incremental hardening, not redesign.
