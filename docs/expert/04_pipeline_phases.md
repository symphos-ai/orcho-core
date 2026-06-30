# Pipeline phases

## Full cycle (--profile feature)

```
hypothesis        — quick direction check before plan (optional)
plan              — architect writes the implementation plan in MD
validate_plan     — reviewer checks the plan before any code is written
implement         — developer implements the plan
review_changes    — reviewer checks the configured review target
repair_changes    — developer fixes the findings
final_acceptance  — reviewer does the final check before release
```

The `repair_changes` → `review_changes` → `repair_changes` cycle
repeats `--max-rounds` times (default: 1).

---

## Review/repair receipt protocol

After `repair_changes`, the next `review_changes` must not guess what was
fixed. Orcho passes two fresh blocks into the resumed review:

| Block | Contents |
|------|--------------|
| `repair_receipt:latest` | the repair phase's claim: what was fixed, what the operator accepted, what remains open |
| `current_review_subject:latest` | the fresh review subject for the current phase |

The reviewer keeps session continuity but must first verify the receipt
against the current subject. An old finding cannot be repeated without fresh
evidence from the current subject.

For `plan -> validate_plan -> replan -> validate_plan` the current subject
is built from `state.parsed_plan`, not from git. For
`review_changes -> repair_changes -> review_changes` the current subject is
built by the review target phase itself: usually a brief status/diff of the
active project state, but the protocol does not require a worktree or git.

---

## Run profiles (--profile)

| Profile | What runs | When to use |
|---------|----------------|-------------------|
| `feature` | plan/validate → implement → review/repair → final_acceptance | standard delivery run |
| `complex_feature` | `feature` + compliance_check | complex work with an extension-point audit gate |
| `small_task` | plan → validate_plan → implement | small direct change, cheapest loop |
| `planning` | only plan + validate_plan | you only need a plan, no code |
| `research` | plan-only research recipe | exploratory investigation |
| `delivery_audit` | review_changes → final_acceptance | audit the current delivery surface |
| `code_review` | review_changes → final_acceptance | focused current-tree review |
| `refactor` / `migration` | full-cycle recipes | larger structural work |
| `task` | implement + review_changes + repair_changes | internal/follow-up run from an existing plan |

```bash
# Only write a plan
orcho run --profile planning --task "Refactor auth system" --project .

# Only review the current changes
orcho run --profile delivery_audit --project .

# Implement from an existing plan
orcho run --profile task --task-file .orcho/docs/plan.md --project .
```

---

## Cross-project pipeline

```
hypothesis    — CROSS HYPOTHESIS: quick direction check across projects (optional)
cross_plan    — architect writes a plan covering all projects + interface contract
per-project   — each project runs its own sub-pipeline (implement → review_changes → repair_changes)
contract      — reviewer validates that the interface contract holds across projects
```

```bash
orcho cross \
  --task "Add rate limiting: API enforces limits, Unity client handles 429" \
  --projects api:~/api unity:~/unity
```

---

## Handoff between BUILD/FIX and REVIEW

Orcho does not treat a git commit as the natural end of BUILD. It is a
separate strategy for handing changes between phases.

Default strategy:

```text
change_handoff = uncommitted
review_target  = uncommitted
```

In practice this means:

- BUILD and FIX must leave the changes in the working tree.
- Agents must not run `git add`, `git commit`, `git branch`,
  `git tag`, `git push` or create PR/MR unless the task explicitly asks
  for it.
- REVIEW and final_acceptance check:
  - tracked changes via `git diff`;
  - relevant untracked files via `git status --short` + reading the files;
  - not the HEAD commit as the source of truth.

This policy does not live in `_prompts/*.md`. It is selected from
`Profile.change_handoff` or `AppConfig.pipeline.change_handoff` and
appended as a system-tail block:

- `change_handoff` — to authoring phases: PLAN / DECOMPOSE / REPLAN / BUILD / FIX / DAG subtasks.
- `review_target` — to review phases: REVIEW / final_acceptance.

This way a project-level prompt override can change style and local
instructions, but cannot accidentally break the review/repair contract.

Mode matrix:

| Mode | What the authoring phase does | What the review phase looks at |
|------|----------------------------|---------------------------|
| `uncommitted` | leaves the working tree dirty | working tree diff + relevant untracked files |
| `commit` | creates one task commit | the selected commit |
| `commit_set` | creates several task commits | set/range of commits |

`commit` and `commit_set` change the review target: REVIEW does not take the
`no-uncommitted` short-circuit, because a clean working tree is the normal
state after an authoring commit handoff.

---

## Per-phase model control

`orcho run` (single-project) uses canonical phase-name flags:

```bash
# Expensive models only where needed
orcho run --task "..." --project . \
  --model-plan           'claude-opus-4-8[1m]' \   # powerful for planning
  --model-implement      'claude-opus-4-8[1m]' \   # default coding model
  --model-review-changes gpt-5.5               # reviewer
```

`orcho cross` (cross-project) historically accepts the short names
`--model-build` / `--model-fix` / `--model-review`, which map to the
canonical `implement` / `repair_changes` / `review_changes`:

```bash
orcho cross --task "..." --projects api:~/api unity:~/unity \
  --model-plan   'claude-opus-4-8[1m]' \
  --model-build  'claude-opus-4-8[1m]' \   # → implement
  --model-fix    'claude-opus-4-8[1m]' \   # → repair_changes
  --model-review gpt-5.5               # → review_changes
```

For finer control (per-phase in any binary) use the env vars
`MODEL_<PHASE>` / `RUNTIME_<PHASE>` or keys in
`_config/config.local.json` (see [03_config.md](03_config.md)).

---

## Hypothesis phase (Phase 0.hypothesis)

Enabled explicitly on the profile's `plan` step:
```json
{"phase": "plan",
 "prompt": {"role": "systems_architect", "task": "plan", "format": "terse"},
 "hypothesis": {"attempts": 1, "format": "compact"}}
```

A missing `hypothesis` field or `attempts: 0` disables the pre-plan
hypothesis. `attempts` sets the number of attempts. `format` sets the format
for the hypothesis and hypothesis QA; if absent,
`plan.prompt.format` is used. This way a terse profile does not suddenly
expand into detailed, and a profile can explicitly pick compact.

**Approved hypothesis.** It does not replace the plan and does not authorize
execution; it is added to the first PLAN/CROSS_PLAN prompt as
`VALIDATED HYPOTHESIS (QA-approved planning context)`. The planner must do
one of three things:

- build the hypothesis direction into the plan;
- first verify or refute the hypothesis's riskiest assumption;
- explicitly explain why the plan diverges from the hypothesis.

**Rejected hypothesis.** If QA rejected all attempts,
Orcho does NOT throw away the reviewer's work: findings/risks/checks from
the structured review contract are collected and fed into the first
PLAN/CROSS_PLAN prompt as `REJECTED HYPOTHESIS FEEDBACK (not validated
direction)`. This is **negative** context — it names the dead ends the
reviewer pointed at and the missed assumptions the planner must verify.
It is **not** an approved direction, so the planner
must:

- avoid the rejected reasoning (do not repeat ideas QA rejected);
- address the reviewer's specific findings and risks;
- verify the assumptions the reviewer called falsifiable, or
  plan from scratch if none of the hypotheses came close.

The plan-phase metadata distinguishes the two paths:

| flag                              | meaning                                        |
|-----------------------------------|------------------------------------------------|
| `hypothesis_injected: True`       | **approved** context was added to the prompt   |
| `hypothesis_feedback_injected: True` | **rejected** feedback was added to the prompt |

Both flags cannot be `True` at the same time on a single plan attempt.

If the run uses `--plan-file` or resumes with an existing
`cross_plan.md`, no new plan is generated, so neither the hypothesis nor
its feedback can influence the content anymore.
