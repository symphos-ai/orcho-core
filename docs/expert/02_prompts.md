# Prompt system — 3 resolution levels

> All shipped prompts are composable parts (`roles/` + `tasks/` +
> `formats/`). Legacy root-level flat templates (`developer_build`,
> `reviewer_code_review`, …) do not exist and take no part in prompt
> resolution. See [core/_prompts/README.md](../../core/_prompts/README.md).

## How Orcho picks a prompt

For every composable part the name includes its subdirectory:

```
roles/code_reviewer
tasks/code_review
formats/detailed
```

Resolution chain (first match wins):

```
1. project/.orcho/multiagent/prompts/{layer}/{name}.md   ← per-project (highest priority)
2. workspace/.orcho/multiagent/prompts/{layer}/{name}.md ← workspace-level
3. core/_prompts/{layer}/{name}.md                      ← core (default, always present)
```

**Project prompt overrides are supported only for composable parts.**
Legacy root-level flat names (`developer_build.md`, `reviewer_code_review.md`,
`architect_plan.md`, …) no longer take part in prompt resolution. If a
project drops such a file in, it has no effect on the render.

## Two layers: user prompt and system-tail

Orcho deliberately separates the two kinds of text an agent receives:

1. **User/project prompt** — editable composable parts (`roles/*`,
   `tasks/*`, `formats/*`) and their project/workspace overrides. This is
   where the persona, the step procedure, and the presentation preset live.
2. **System-tail blocks** — system blocks that Orcho appends after the
   user prompt via `pipeline.prompts.contracts`. They carry an
   XML-like annotation `<orcho:system-block ...>` and must NOT live in
   `_prompts`.

A project override can replace the user prompt, but must not accidentally
erase the parser's machine contract, the language posture, or the execution
strategy.

Shipped system-tail blocks:

| Block | Kind | Where it is used | Purpose |
|------|------|------------------|------|
| `review_json` | `contract` | REVIEW / final_acceptance / validate_plan / HYPOTHESIS QA / file review | JSON output shape + REVIEW_SCHEMA_DOC |
| `plan_json` | `contract` | PLAN / REPLAN / DECOMPOSE | JSON output shape + PLAN_SCHEMA_DOC |
| `plan_verdict` | `contract` | legacy prose verdict | English-only `VERDICT: APPROVED/REJECTED` |
| `cross_subtask_blocks` | `contract` | CROSS PLAN | `=== SUBTASK [<alias>] ===` block grammar |
| `change_handoff` | `strategy` | authoring phases (PLAN/REPLAN/DECOMPOSE/BUILD/FIX/READONLY) | Git/working-tree handoff policy |
| `review_target` | `strategy` | REVIEW / final_acceptance / runtime review | What exactly the reviewer must look at |
| `authoring_language` | `strategy` | non-JSON authoring surfaces | Language posture (prose + code) |

Rule: if the text changes **workflow semantics** (commit or not, what counts
as the review target, which machine verdict is required, which language the
body fields use), it belongs in system-tail, not in `_prompts/*.md`. The
structural guardrail is `tests/unit/test_prompt_boundary.py`.

## Which shipped composable parts exist

```bash
orcho prompts --list    # full list
```

### roles
| File | Step / Purpose |
|------|------|
| `roles/code_reviewer.md` | Code reviewer persona |
| `roles/implementation_engineer.md` | Implementation engineer persona |
| `roles/release_manager.md` | Release manager persona |
| `roles/systems_architect.md` | Systems architect persona |
| `roles/product_owner.md` | Product owner persona |

### tasks
| File | Step |
|------|------|
| `tasks/plan.md` | PLAN |
| `tasks/replan.md` | REPLAN |
| `tasks/decompose.md` | decompose |
| `tasks/implement.md` | implement |
| `tasks/repair_changes.md` | repair_changes |
| `tasks/code_review.md` | review_changes |
| `tasks/final_acceptance.md` | final_acceptance |
| `tasks/validate_plan.md` | validate_plan |
| `tasks/hypothesis.md` | hypothesis |
| `tasks/validate_hypothesis.md` | validate_hypothesis |
| `tasks/readonly_plan.md` | readonly plan (runtime surface) |
| `tasks/review_uncommitted.md` | runtime review of configured change target |
| `tasks/cross_plan.md` | CROSS PLAN |
| `tasks/cross_contract_bundle.md` | contract_check artifact-bundle review |

### formats
| File | Purpose |
|------|------|
| `formats/terse.md` | Concise, result-first |
| `formats/compact.md` | Compact, decision-oriented |
| `formats/detailed.md` | Detailed: findings + rationale + risks + verification |
| `formats/bullets.md` | Short bullet lists |
| `formats/handoff.md` | Write for the next agent / maintainer |

### Typed prompt config on `PhaseStep`

Profile authors can declare which composable parts a phase step uses:

```json
{
  "phase": "review",
  "execution": "linear",
  "prompt": {
    "role": "code_reviewer",
    "task": "code_review",
    "format": "detailed"
  }
}
```

`PhaseStep.prompt` selects the `roles/` + `tasks/` + optional `formats/`
parts that a phase can render. It does not define workflow order,
retry, quality gates, change handoff, or review target. Those remain on
`Profile.steps`, `LoopStep`, `QualityGate`, `Profile.change_handoff`,
and `pipeline.prompts.contracts`.

`prompt.role` is required when the profile declares a `prompt` block.
It names a prompt persona file under `_prompts/roles/`; it does not pick
the agent runtime. Omitting the whole `prompt` block lets the phase
builder use its code-owned default.

## Change handoff strategy

Default from `core/_config/config.defaults.json`:

```text
change_handoff = uncommitted
review_target  = uncommitted
```

This means:

- BUILD/FIX leave changes in the working tree.
- Authoring agents do not run `git add`, `git commit`, branch/tag/push
  or create PR/MR unless the user explicitly asks.
- REVIEW/final_acceptance look at the working-tree diff plus untracked files
  relevant to the task.
- PLAN must not include commit/PR/push in the Definition of Done unless it
  is part of the user's task.

Available surface:

| Mode | Authoring handoff | Review target |
|------|-------------------|---------------|
| `uncommitted` | leave the working tree dirty | `git status`, `git diff`, relevant untracked files |
| `commit` | create one task commit | diff of the selected commit |
| `commit_set` | create several task commits | range/set of commits |

The mode is taken from `Profile.change_handoff`, or — if the profile does
not set it — from `AppConfig.pipeline.change_handoff` (`ORCHO_CHANGE_HANDOFF`
can override it locally). This is not edited in `_prompts/*.md`.

## Override a prompt for your project

Project overrides are supported only at the composable-part level:

```bash
mkdir -p your-project/.orcho/multiagent/prompts/tasks/
mkdir -p your-project/.orcho/multiagent/prompts/roles/

# Copy a core part as the base
cp ~/.local/share/orcho-core/_prompts/tasks/build.md \
   your-project/.orcho/multiagent/prompts/tasks/build.md

cp ~/.local/share/orcho-core/_prompts/roles/code_reviewer.md \
   your-project/.orcho/multiagent/prompts/roles/code_reviewer.md

# Edit for your stack
```

## Template syntax

Composable parts are `string.Template` with `$var` variables:

```markdown
TASK: $task

Project context:
$context

## Implementation
1. Read the plan
2. Make the changes
$extra_step
```

Variables available in a template:

| Variable | Source |
|-----------|--------|
| `$task` | `--task` CLI argument |
| `$project_name` | `plugin.name` |
| `$tech_stack` | `plugin.tech_stack` |
| `$architecture` | `plugin.architecture` |
| `$plan_prompt_extra` | `plugin.plan_prompt_extra` |
| `$build_prompt_extra` | `plugin.build_prompt_extra` |
| `$review_focus_extra` | `plugin.review_focus_extra` |

Unknown `$variables` pass through unchanged (safe_substitute). Do **not**
interpolate `$task_language` / `$body_language` in user-editable parts —
that is config-owned policy living in system-tail.

## Check what is being used

```bash
# Show the final prompt the agent will receive
orcho prompts tasks/build --project /path/to/project
```

## Workspace-level prompts

Override for **all projects** in the workspace:

```
workspace/
└── .orcho/multiagent/prompts/
    └── tasks/code_review.md   ← your own review policy for the whole workspace
```
