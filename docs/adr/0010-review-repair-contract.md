# ADR 0010: Review/Repair Contract

- **Status:** Accepted
- **Date:** 2026-05-07
- **Deciders:** project owner

## Context

The manual workflow that motivated Orcho's design is simple and
strong:

```text
Claude builds.
Codex reviews the real diff.
Claude/Codex repairs against concrete critique.
Codex reviews the real diff again.
```

This loop works because it preserves four things:

- a builder with creative momentum;
- a reviewer with a fresh perspective;
- a repair step that receives concrete critique;
- a final check of the repository in the state it is actually in.

But freezing this exact sequence as a permanent architectural law would
return us to the static pipeline Orcho is moving away from. Dynamic
profiles must be allowed to skip, reorder, branch, parallelize, or
replace steps when experiments produce better results.

The architecture must preserve the strength of the manual loop without
freezing the manual loop itself.

## Decision

Define the **Review/Repair contract** as four concrete scenes that
profiles compose, not as one mandatory sequence.

### Scene 1: Change

Some actor changes the repository or produces a durable artifact.

Examples:

- Claude writes code.
- A DAG build executes multiple subtasks.
- A planner writes a contract or implementation plan.
- A tool applies a mechanical migration.

The scene must leave inspectable state: a git diff, changed files, an
artifact path, test output, contract output, or a structured step
result.

### Scene 2: Inspection

Some independent signal verifies the real state produced by the Change
scene.

Examples:

- Codex reviews the actual git diff.
- A quality gate runs tests against the working tree.
- A contract validator checks cross-project interfaces.
- A human reviews the generated plan before code is written.

Inspection must not rely only on the previous actor's summary. It must
receive concrete state: a diff, files, artifacts, test output, or
contract data.

### Scene 3: Repair

If Inspection found a problem, the modifying actor receives concrete
critique and attempts to fix the state.

Examples:

- Claude fixes Codex review findings.
- Codex applies a small surgical fix from its own review.
- A test-fix step receives failing test output.
- A planner revises the plan from plan-QA critique.

Repair must receive exactly the critique that triggered it. When
structured findings exist, it must not be given a generic
"try again"-level prompt.

### Scene 4: Exit Confidence

Before a run reports success, Orcho records why it is safe enough to
stop.

Examples:

- a clean Codex review of the final diff;
- passing required quality gates;
- an approved human review;
- accepted contract validation;
- an explicit low-confidence / fast-mode exit.

Fast profiles may weaken this scene, but the weakening must be visible
in the profile and in the session output.

## Baseline profile

The manual loop remains the baseline profile and the reference for
comparison:

```text
Change:      Claude builds
Inspection:  Codex reviews the real diff
Repair:      Claude or Codex repairs against concrete critique
Confidence:  Codex reviews the real diff again
```

This baseline profile is not the only valid workflow. It is the control
sample: new profiles may be better, but they must be comparable to it.

## Dynamic profiles

A dynamic profile may change the sequence while preserving the
contract.

### Tests-first branch

```text
Change:      Claude builds
Inspection:  tests run
Repair:      test-fix receives failing output
Inspection:  Codex reviews the risky final diff
Confidence:  tests pass + review clean
```

### Risk-based review

```text
Change:      Claude changes docs only
Inspection:  docs lint / artifact check
Confidence:  low-risk exit; independent code review skipped by policy
```

### Cross-project contract

```text
Change:      architect writes a cross-project contract
Inspection:  contract validator checks affected projects
Change:      per-project builders implement subtasks
Inspection:  Codex reviews each real diff
Repair:      project-specific fixes receive review critique
Confidence:  final contract validation + final diff review
```

## Required session evidence

Session/events output must make the contract visible:

```json
{
  "change": {
    "actor": "claude",
    "state_kind": "git_diff"
  },
  "inspection": {
    "actor": "codex",
    "input_kind": "git_diff",
    "verdict": "rejected"
  },
  "repair": {
    "actor": "claude",
    "input_kind": "review_critique"
  },
  "exit_confidence": {
    "signals": ["codex_review_clean", "tests_passed"]
  }
}
```

The exact shape may evolve, but a completed run must explain:

- what changed;
- what verified the real state;
- which critique drove the repair, if a repair happened;
- why Orcho decided to stop.

## Guardrail

Any new profile, execution mode, prompt composer, skill router, or
quality gate must answer the question:

```text
Did this make the manual Claude-build / Codex-review / repair loop
stronger, more observable, or more selectively replaceable?
```

If the answer is "no", the change is most likely architectural
decoration.

## Consequences

Positive:

- The manual loop is protected without hard-coding a static workflow.
- Experiments can beat the baseline by changing order, actors, gates,
  or branching.
- Profile authors may deliberately weaken confidence for speed, but the
  tradeoff becomes visible.
- Tests can verify contract scenes instead of one exact sequence.

Costs:

- Runtime/session telemetry needs enough metadata to prove which scenes
  occurred.
- Profiles need a way to declare skipped or weakened inspections.
- Tests must cover the baseline loop and at least one dynamic branch.
