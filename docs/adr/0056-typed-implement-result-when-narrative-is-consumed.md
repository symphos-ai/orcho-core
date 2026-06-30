# ADR 0056 — Typed agent output follows the consumer; a deferred implement result

- **Status:** Proposed (deferred — records the principle + trigger, not a change to ship now)
- **Date:** 2026-05-29
- **Deciders:** project owner
- **Relates to:** [ADR 0050](0050-structured-cross-handoff.md),
  [ADR 0054](0054-typed-cross-plan-json.md)

## Context

A recurring design question: "agents speak JSON — why does phase X
emit prose?" Answered ad hoc for reviewer gates (JSON), the architect
(JSON; cross is the outlier → ADR 0054), and `implement` / `repair_changes`
(prose). This ADR records the **invariant** behind those answers so the
question stops recurring, and names the concrete trigger that will flip
`implement` from prose to typed.

### The invariant (litmus)

> An agent's **output text** is a typed contract **iff something
> machine-consumes that text**. The artifact a downstream step routes on
> decides the shape — not the phase's importance.

Applying it to today's phases:

| Phase | Machine-consumed artifact | Output text shape | Typed? |
|---|---|---|---|
| Reviewer gates | the verdict (approve/reject loop, evidence) | `review_json` / `release_json` | yes |
| Architect / plan | the plan (subtask routing, plan_contract, handoff) | `plan_json` (mono) | yes (cross: ADR 0054) |
| Commit message | the message (delivery/commit) | `commit_message_json` | yes |
| `implement` / `repair_changes` | the **filesystem diff** (tool calls) | prose handoff | no — correctly |

`implement` is prose today because the thing downstream consumes is the
**diff**, not the agent's text. The review phase reads `git diff`; repair
reads the reviewer's JSON findings; nobody parses the implement narrative.
So forcing JSON onto `implement` now would be backwards — the work is the
edits, and the prose is a human/next-agent handoff with no parser. Note the
litmus already *predicts* the commit-message phase being JSON
(`commit_message_json_contract`): the moment the change *narrative* became
machine-consumed (delivery emits a commit message), it became typed.

## Decision (principle + deferred change)

1. **Adopt the litmus as the standing rule** for "does this phase emit
   JSON?" New phases and contracts are judged by what downstream routes
   on, not by seniority. This is the rule reviewers should cite instead
   of re-deriving it per phase.

2. **Deferred: a typed `implement_result`.** In real review, the reviewer
   reads more than the diff — they read the change **narrative** (the PR
   description: what changed, why, risks, how it was verified). Orcho will
   eventually generate that narrative (PR/MR creation, a richer evidence
   bundle, or an agent reviewer fed the description alongside the diff).
   At that point the implement handoff **becomes machine/human-consumed
   structured content**, and by the litmus it must become a typed contract
   — not scraped from prose (the exact trap ADR 0050 / ADR 0054 are
   unwinding for the handoff and the cross plan). Plan for it then, not
   now.

Proposed shape when the trigger fires (PR-description-shaped):

```json
{
  "title": "<imperative, <= 70 chars>",
  "summary": "<1-3 sentences: what changed and why>",
  "changes": ["<file or area>: <what changed>"],
  "risks": ["<residual risk / blast radius>"],
  "verification": ["<check run / evidence>"],
  "follow_ups": ["<deliberately deferred work>"]
}
```

The prose handoff stays as a derived render of this object (same pattern
as ADR 0050: typed object is the source of truth, markdown is the view) —
so the next-agent/human handoff text is preserved, just no longer the
authoritative channel.

## Trigger (when to implement, not before)

Implement `implement_result` when **any** of these lands a consumer that
reads the change narrative as data:

- PR/MR creation from a run (the description is generated, not hand-typed);
- an agent reviewer that is fed the change description alongside the diff;
- an evidence/dashboard surface that renders a structured change summary
  rather than the raw handoff blob.

Until then, `implement` / `repair_changes` stay prose — the diff is the
contract.

## Scope / non-goals

- **No change ships from this ADR.** It records the invariant and a
  deferred design so the recurring "why isn't implement JSON?" question
  has a written answer and the future work has a home.
- Does not alter `implement` / `repair_changes` prompts or the
  `format=handoff` surface today.
- Does not change reviewer, plan, or commit-message contracts.
- When implemented, it follows the ADR 0050 pattern (typed source of
  truth, markdown as derived view) and ships with an `orcho-mcp` mock
  smoke if it touches MCP-visible evidence shape.

## Consequences

- Reviewers of future contract changes have one rule to cite (the litmus),
  reducing per-phase bikeshedding about JSON vs prose.
- The implement-result work is pre-scoped: when a narrative consumer
  appears, the shape and the "typed source, derived prose" pattern are
  already decided.
- Risk if ignored: a narrative consumer gets bolted on by scraping the
  free-form handoff (regex over prose) — re-introducing exactly the
  fragility ADR 0050 (path leak) and ADR 0054 (marker/heading scrape)
  exist to remove. This ADR is the standing reminder not to do that.
