# Prompt Pipeline Instructions

## Scope

This file applies to `orcho-core/pipeline/prompts/`.

Also obey the repository-level `../../AGENTS.md`. Shipped prompt markdown under
`core/_prompts/` has its own local instructions.

## Prompt Boundary Discipline

`pipeline.prompts.contracts` and `pipeline.prompts.contract_templates` own all
machine-readable output shape, language posture, safety/orchestration policy,
and parser contracts. User-editable prompt parts under `core/_prompts/roles`,
`core/_prompts/tasks`, and `core/_prompts/formats` must not define those
contracts.

Reviewer gates (`validate_plan`, `review_changes`, `final_acceptance`, and
hypothesis QA) are JSON-only. The only accepted reviewer gate surface is
`review_json_contract`: exactly one JSON object validated by
`pipeline.review_parser.parse_review`. Do not add or restore prose verdict
protocols, `plan_verdict`, markdown-fenced JSON parsing, LGTM/no-issues
heuristics, or empty-output approval shortcuts. Clean reviews are represented
as `{"verdict": "APPROVED", "findings": []}` and still pass through the same
JSON parser. Mocks, tests, dry-run paths, and fixture agents must emit the
same JSON contract instead of special prose strings.

When editing prompt contracts or prompt composition:

- Keep parser-owned schema, enum, and JSON-only instructions in code-owned
  contract blocks.
- Keep language posture in `authoring_language_strategy` or the relevant
  JSON contract body-language argument.
- Keep git, working-tree, and commit policy in `change_handoff_strategy`.
- Keep review-target policy in `review_target_strategy`.
- Do not move runtime task/body/artifact data into user-editable static parts;
  use typed dynamic `PromptPart`s.

After editing this package, run:

```bash
pytest tests/unit/pipeline/prompts/test_prompt_boundary.py
pytest tests/unit/pipeline/prompts/test_prompts.py
```

Run relevant parser/gate tests too when changing reviewer, plan, commit
message, or cross-project contract behavior.
