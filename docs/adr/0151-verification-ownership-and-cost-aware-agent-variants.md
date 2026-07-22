# ADR 0151 — Verification ownership and cost-aware agent variants

- **Status:** Accepted
- **Date:** 2026-07-22
- **Relates to:** [ADR 0117](0117-verification-blocking-tier-independent-of-cost.md), [ADR 0132](0132-scheduled-gate-selection-execution-and-disposition.md), and [ADR 0150](0150-verification-retry-observability-parity.md)

## Context

Scheduled verification has one authoritative executor and receipt trail: the
engine executes the selected scheduled identity `(command, hook, phase)` and
records its disposition.  Agents can nevertheless perform useful supplemental
checks.  Without a separate model, an agent's command, cost preference, or
evidence can be mistaken for an official execution, and cheap variants can
silently change blocking behaviour.  That violates the independent axes in ADR
0132 and the cost/policy separation in ADR 0117.

This decision defines a no-legacy cost vocabulary and a bounded, typed surface
for named agent variants while preserving engine ownership of scheduled gates.

## Decision

### Independent axes

`cost` is metadata only.  Its sole vocabulary is `fast | moderate | slow |
unknown`; `default_cost` is the corresponding gate-set default.  `cost` and
`default_cost` unconditionally replace `cheap` and `default_cheap` everywhere.
There are no aliases, deprecated parsing paths, boolean compatibility fields,
or legacy output tail.  Missing declared cost resolves deterministically to
`unknown`; command cost overrides `default_cost`.

Cost is independent of declared/effective policy (`manual | suggest | warn |
require`), selection, executor, action, consequence, and disposition.  In
particular, it cannot downgrade, escalate, or otherwise decide `block`,
`repair`, or `handoff`; a selected `require` gate blocks according to its
effective policy regardless of cost.

The official identity remains the normalized `(command, hook, phase)` selected
by the verification contract.  The engine alone executes that identity and
creates or refreshes its scheduled-gate receipt and disposition ledger entry.
Agent evidence cannot satisfy, reuse, replace, refresh, or overwrite an
official receipt, including a receipt scheduled by a gate retry.  The
`scheduled_gate_ledger.json` remains a disposition ledger, not a container for
agent-check results.  Supplemental checks use a separate typed agent-check
evidence surface linked to, but distinct from, the official identity.

### Named agent variants

A public typed named agent variant contains: its stable name; its exact linked
official identity; its own normalized command; resolved `cost`; a typed,
explicit `relation` and `scope` to that identity; and a declared `cadence`.
The relation/scope describes what supplemental coverage it intends, rather
than claiming equivalence with the official command.  Cadence says when the
agent may propose or run the supplemental check; it is not an execution policy
and carries no blocking action.

The identity link is mandatory even where a variant's scope is narrower or
broader.  A variant is unavailable when its declared prerequisite cannot be
resolved or invoked.  That state records unavailable supplemental evidence and
does not start an automatic retry loop, change the official gate's status, or
create a repair/handoff disposition.

### Exact overlap validation

After typed plan parsing, core performs one pure overlap check: it compares a
variant's declared command to the contract-native normalized command of its
linked official identity for exact equality.  Exact equality is deterministic;
it neither executes commands nor considers evidence.  The check deliberately
does not infer meaning from prose, shell semantics, Make wrappers, aliases,
environment expansion, or semantic equivalence.  An exact match is rejected
as overlap because it would duplicate the official command; non-matches remain
supplemental and cannot be promoted to official execution.

## Phase projections

| Phase | Facts available and permitted reliance |
|---|---|
| PLAN | Official identities and available declared variants, including their cost, relation/scope, and cadence; no receipt may be presumed. |
| VALIDATE_PLAN | The parsed typed plan, official ownership link, cost resolution, and exact post-parse overlap result; it validates ownership but does not execute checks. |
| IMPLEMENT | May run declared variants at their cadence and ad-hoc non-overlapping supplemental checks, recording only typed agent-check evidence; official scheduled execution remains engine-owned. |
| REVIEW / FINAL | Treat only the official ledger and its receipts as authoritative for scheduled-gate readiness/disposition; agent evidence may inform review but cannot satisfy a scheduled gate. |

## Migration and decomposition

The implementation graph is acyclic:

```text
V1 → V2 → V3 → core public-contract promotion barrier → M1 (conditional)
```

1. **V1 — contract, schema, and resolution.** Introduce `cost/default_cost`,
   deterministic effective-cost resolution, and the typed variant declaration;
   directly migrate first-party core and MCP-plugin declarations, public
   schemas, documentation, and tests.  Stop if any `cheap/default_cheap` alias,
   parser compatibility branch, or emitted legacy field remains.
2. **V2 — phase projection and typed evidence.** Project available variants
   into PLAN and VALIDATE_PLAN and provide the separate agent-check evidence
   surface for IMPLEMENT.  Stop if agent evidence can reach the scheduled
   disposition ledger or alter an official receipt.
3. **V3 — validation seam.** Add the pure post-parse exact-normalized-command
   overlap validation.  Stop if validation introduces prose or shell heuristics
   or executes a command.
4. **Core public-contract promotion barrier.** Promote and validate the
   released core public contract before any downstream consumer is checked
   against it.  A downstream MCP migration must not be validated or released
   against stale core.
5. **M1 — conditional MCP-visible evidence projection.** Only if V2's typed
   agent-check evidence is MCP-visible, migrate its MCP projection after the
   barrier; MCP projects core semantics and does not own them.  Stop if M1
   requires a second policy, cost, or disposition model.

This is a direct migration, not a dual-path rollout.  First-party core/MCP
plugin declarations move in V1; public schemas, docs, and tests change with
their owning contract rather than accepting legacy booleans.

## Rollout and dogfood falsifiers

Dogfood permits changed-file Ruff and targeted tests.  It must not duplicate
the exact broad engine-owned gate; scheduled broad verification retains its
official receipt.  A selected `require` blocks independently of `cost`, and an
unavailable variant produces evidence without retrying.  The rollout is
falsified if any of those facts fail, if an agent receipt satisfies a scheduled
gate, or if a legacy boolean is accepted.

## Consequences

- Cost-aware planning becomes explicit without giving cost hidden control over
  policy, action, or disposition.
- Supplemental agent checks are useful and auditable, but cannot forge the
  authoritative verification trail.
- The direct migration intentionally breaks old `cheap/default_cheap` inputs
  rather than preserving ambiguous boolean compatibility.
- Exact matching is predictable but does not detect semantically equivalent
  wrappers; authors must declare an honest non-overlapping variant instead.
- Separate evidence increases surface area, but avoids overloading the durable
  scheduled-gate ledger and preserves ADR 0150 retry observability.

## Rejected alternatives

1. **Keep boolean compatibility aliases.** Rejected because aliases and legacy
   parsing perpetuate two public vocabularies and unclear cost resolution.
2. **Use shell, wrapper, alias, or prose heuristics for overlap.** Rejected
   because they are non-deterministic, unsafe to validate, and cannot define a
   stable contract.
3. **Let agent execution reuse or write official receipts.** Rejected because
   it breaks engine ownership, scheduled identity, retry lineage, and durable
   disposition semantics.
4. **Store agent checks in the scheduled-gate ledger.** Rejected because that
   ledger expresses official execution and disposition, not supplemental
   evidence.
5. **Let cost choose blocking policy or required action.** Rejected by ADR
   0117: authors choose policy explicitly, while cost remains descriptive.
