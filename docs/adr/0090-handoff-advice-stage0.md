# ADR 0090 — Run handoff advice (Stage 0): interactive `advice` and `retry_with_advice`

- Status: Accepted
- Date: 2026-06-13
- Relates to: ADR 0031 (generic phase-handoff contract — the four canonical
  actions, `available_actions`, the strict decision-artifact reader, and the
  `decide ≠ resume` split this builds on), ADR 0069 (delivery dialog on
  rejected acceptance — the operator decision surface), ADR 0070
  (auto-correction follow-up loop — the unattended counterpart this stays
  distinct from), ADR 0086 (correction route Stage 1 — the adjacent
  reviewer-driven routing whose `[handoff_advice]` mock-marker precedent and
  Stage-0/next-stage framing this mirrors)

## Context

When a phase handoff pauses on a **rejected** reviewer verdict
(`validate_plan` / `review_changes`) or an **incomplete** implement delivery
(`subtask_dag`), the operator faces the recorded findings and the four
canonical actions (`continue` / `retry_feedback` / `halt` /
`continue_with_waiver`; ADR 0031). To craft a good `retry_feedback` verdict
they typically copy the findings and the reviewer output into a separate agent
session, ask it "what is the smallest honest fix here," and paste the answer
back as the operator verdict. That loop is manual, lossy (truncation,
copy-paste drift), and leaves no durable record of *which* advice produced the
applied feedback.

We want to close that loop **inside** the paused run: offer the operator a
read-only advisor that reads the same recorded review surface and recommends
the smallest honest way forward, and — when the operator accepts — feed the
generated feedback through the **existing** durable decision path. The hard
constraint is that this must not perturb the audit-grade decision contract:
the strict decision-artifact reader (`_read_existing_strict`) rejects unknown
fields by design, and any wire-format change to the decision artifact, the
profile shape, or the action vocabulary would require a matching `orcho-mcp`
update under the **MCP Validation** rule.

## Decision

Add two **UI pseudo-actions** to the interactive TTY handoff menu — `5) advice`
and `6) retry_with_advice` — layered **on top of** the four canonical actions.
They are not added to `available_actions`, are not accepted by
`phase_handoff_decide`, and never appear in a decision artifact's `action`
field. The canonical action vocabulary and its SDK validation are unchanged.

- **`advice`** is render-only: it invokes the read-only advisor, persists the
  recommendation as a durable advice artifact, and shows a follow-up sub-menu
  (`apply advice and retry` / `edit advice` / `back` / `halt`). `back` returns
  to the main menu having written **no** decision; the pause is already
  persisted, so the menu simply re-displays.
- **`retry_with_advice`** generates repair feedback from the findings and — only
  when the advisor recommends a retry — applies it as an ordinary
  `retry_feedback` decision.

In **both** branches, when the operator commits to a retry (or a halt), the
resulting decision is recorded through the unchanged
`sdk.phase_handoff.phase_handoff_decide` and resumed through the unchanged
`apply_phase_handoff_resume_with_banners` path. There is **no** parallel
decide/resume branch — the advisor only produces the `feedback` + `note` inputs
the existing path already consumes.

### Eligibility contract (trigger **and** verdict)

The advisory items are offered only when the orchestrator's policy predicate
(`handoff_advice.advice_actions_available(signal)`) holds — computed at the
orchestrator, never inside the pure prompt. All four conditions must be true:

1. **trigger** ∈ `{rejected, incomplete}`;
2. **verdict is rejected/incomplete-equivalent** — `approved is not True` **and**
   the normalised verdict label ∈ `{REJECTED, INCOMPLETE}` (an empty verdict
   with `approved=False` counts as the documented incomplete-equivalent). A
   handoff with a matching trigger but an **approved** verdict — e.g.
   `human_feedback_always` firing on an approved verdict — is **not** eligible;
3. `retry_feedback` ∈ `available_actions` (the durable retry path the advisory
   feedback flows through must be open);
4. there is something to advise on: a non-empty `last_output` **or** at least
   one recorded finding.

For a non-eligible handoff the menu, aliases, and input hint are byte-for-byte
identical to the pre-advice behaviour. Trigger **and** verdict are both checked
precisely so an approved-verdict pause never offers advice.

### Durable advice artifact

The advisor recommendation is persisted to
`<run_dir>/phase_handoff_advice/<safe_handoff_id>.json` (the same
collision-resistant `safe_handoff_id` slug the decision artifacts use; a
directory per run, a file per handoff). It is written by the advisor flow
**only** — never by a decision path — and **never** touches
`phase_handoff_decisions/`.

Schema:

```json
{
  "run_id":     "<run id>",
  "handoff_id": "<handoff id>",
  "phase":      "<paused phase>",
  "created_at": "<ISO-8601 UTC>",
  "advice": {
    "recommended_action": "continue | retry_feedback | halt | continue_with_waiver",
    "confidence":         "high | medium | low",
    "rationale":          "<one or two sentences>",
    "retry_feedback":     "<corrective feedback; required for a retry rec>",
    "risks":              ["<concrete risk>", "..."],
    "expected_files":     ["<file a retry would touch>", "..."],
    "operator_note":      "<optional short note>",
    "parse_warnings":     ["<normalisation note>", "..."]
  },
  "raw_output": "<verbatim advisor output>",
  "usage":      { "...": "per-invoke usage snapshot" }
}
```

**Divergent versions never overwrite.** A repeat write of the same handoff with
**identical** advice content returns the same path (idempotent, no rewrite). A
**different** advice for the same handoff — a second advisory pass that produced
a different recommendation, or an operator who edited the feedback before
applying — is written to a new attempt-suffixed file (`<safe_id>_2.json`,
`<safe_id>_3.json`, …) whose path is returned. Earlier advisor artifacts and
any human-authored artifacts are left intact; the suffix probe only ever steps
over an occupant whose `advice` block differs.

### Provenance via a deterministic decision `note`

The link from a durable `retry_feedback` decision back to the advice it was
generated from is carried in the decision's `note` field, using a fixed,
parseable shape:

```
feedback_source=agent_advice; advice_artifact=<actual relative path>
```

The path in the note is **always** the path the artifact write returned —
never a recomputed deterministic name. This matters for the divergent and
operator-edit cases: when the applied feedback was edited, the flow first
persists a divergent advice artifact whose `retry_feedback` **is** the applied
text, then builds the note from **that** suffixed path, so the durable decision
always references the exact advice object its feedback came from.

**Why a note, not a new decision field.** The decision artifact is audit-grade:
`_read_existing_strict` validates every persisted field and rejects unknown
ones, and the artifact format is part of the wire contract that `orcho-mcp`
mirrors. Adding a `feedback_source` / `advice_artifact` field would change that
wire shape and pull in an `orcho-mcp` update + E2E smoke under the **MCP
Validation** rule. The existing free-text `note` field already rides through
`phase_handoff_decide` unchanged, so encoding provenance there closes the loop
without touching the strict reader and without an MCP change in Stage 0. The
exact-payload idempotency of `phase_handoff_decide` is therefore preserved:
re-applying the same generated decision (same `feedback` + same `note` with the
same artifact path) replays cleanly; a divergent payload still conflicts.

### Safety rules

The advisor is read-only (`mutates_artifacts=False`) and its recommendation is
gated before it can become a decision:

- **No automatic waiver, ever.** A `continue_with_waiver` recommendation is
  render-only — it is displayed but never auto-applied, at any severity. A
  waiver remains a deliberate human action through the canonical
  `continue_with_waiver` path.
- **Low-confidence requires confirmation.** A `low`-confidence retry
  recommendation is applied only after an explicit operator confirmation; a
  declined confirmation returns to the menu with no decision.
- **Non-retry recommendations are render-only.** Only a non-low-confidence
  `retry_feedback` recommendation is auto-appliable. A `continue` / `halt` /
  `continue_with_waiver` recommendation is shown and explained, and the
  `retry_with_advice` path reports that no automatic retry was performed.
- **Defensive `retry_feedback` availability check.** Even though the menu is
  only offered when `retry_feedback ∈ available_actions`, the apply path
  re-checks it before recording the decision.
- **Advisor errors never break the loop.** An invocation exception or an
  unparseable advisor response warns and returns to the menu without writing a
  decision.

## Schema / MCP impact

Additive and CLI/session-only:

- The `phase_handoff_advice/` directory and its artifact are **new** durable
  state, written only by the interactive advisor flow; no existing artifact
  shape changed.
- The decision artifact format is **unchanged** — provenance rides the existing
  `note` field. The four canonical actions, `available_actions`, and the SDK
  decision validation are untouched.
- Because no wire-format, profile shape, mode flag, or gate primitive changed,
  the **MCP Validation** rule does **not** fire: no `orcho-mcp` synchronization
  or E2E mock smoke is required for this stage. This was the deciding factor in
  choosing note-provenance over a decision-field extension.

## Consequences

- An operator resolving a rejected/incomplete pause can get an in-run, read-only
  recommendation and apply (or edit-then-apply) it as an ordinary
  `retry_feedback` — no copy-paste into an external agent, and a durable record
  of which advice produced the applied feedback.
- Every applied advisory retry is auditable end-to-end: the decision's `note`
  points at the exact advice artifact, including the divergent/edited cases.
- Non-eligible handoffs are completely unaffected — the predicate gates the
  menu, and the canonical decision contract is byte-for-byte unchanged.
- The advisor usage is metered under a separate `handoff_advice` slot, distinct
  from phase usage.

## Non-goals (future stages)

- **Unattended / CI auto-retry.** This stage is interactive-only: the advisor
  never decides on the operator's behalf. An automatic advisor-driven retry in
  a non-interactive run is a future stage, not introduced here.
- **Autonomous waiver.** No advice path applies a waiver automatically; that
  stays a deliberate operator action through `continue_with_waiver`. An
  advisor-recommended auto-waiver is explicitly out of scope.
- **MCP / Web surface for advisory actions.** Surfacing the advice artifact or
  the advisory pseudo-actions on the MCP evidence/run surfaces or any non-TTY
  transport is deferred; this stage is the interactive TTY menu plus
  `session.json`/run-dir state only.
