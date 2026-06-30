# Prompt Part Instructions

## Scope

This file applies to `orcho-core/core/_prompts/`.

Also obey the repository-level `../../AGENTS.md`. Prompt contract code under
`pipeline/prompts/` has its own local instructions.

## Prompt Boundary Discipline

The architecture has four layers. Every piece of content lives in exactly one
of them. Never duplicate content across layers.

| Layer | Owns | Edit surface |
|---|---|---|
| `_prompts/roles/*.md` | Persona and posture only; no runtime placeholders. | user / project / workspace |
| `_prompts/tasks/*.md` | Phase procedure and checks only; runtime task/body/artifact data arrives as typed dynamic `PromptPart`s. | user / project / workspace |
| `_prompts/formats/*.md` | Presentation/detail/style only; role-agnostic reusable presets such as terse, detailed, bullets, handoff. | user / project / workspace |
| `pipeline.prompts.contracts` system-tail blocks | Machine output shape, language posture, safety/orchestration policy. | code-only |

The system-tail blocks are the single source of truth for:

- Language posture: `authoring_language_strategy`,
  `review_json_contract(body_language=...)`,
  `plan_json_contract(body_language=, input_language=)`.
- Safety policy: `change_handoff_strategy`, `review_target_strategy`.
- Parser contracts: `review_json_contract`, `plan_json_contract`.

Hard rules, checked by `tests/unit/pipeline/prompts/test_prompt_boundary.py`:

1. Never put `$task_language`, `$body_language`, `$artifact_language`, or
   `$input_language` in a user-editable part.
2. Never phrase "Reply in {X}" or "Respond in {X}" in a user-editable part.
3. Never put JSON schema text, "exactly one JSON object", enum values, or
   `VERDICT: APPROVED` prose in a user-editable part.
4. Never put destructive-git or preserve-user-owned policy prose in a
   user-editable part. Extend `change_handoff_strategy` if the rule is
   missing.
5. Never name system-tail blocks such as `authoring_language`,
   `change_handoff`, `review_target`, `review_json`, or `plan_json` inside a
   user-editable part.
6. `formats/*` is role-agnostic style only: never persona-specific,
   task-specific, or variable-bearing.

When deciding where a change belongs:

- Language behavior belongs in `authoring_language_strategy` or the relevant
  JSON contract body-language argument.
- Output shape, schema, enum, and parser rules belong in the right
  `*_contract` block.
- Git, working-tree, and commit behavior belongs in
  `change_handoff_strategy`.
- Review-target behavior belongs in `review_target_strategy`.
- Presentation style belongs in `formats/*`.
- Persona belongs in `roles/*`.
- Phase procedure belongs in `tasks/*`.

After editing prompt parts, run:

```bash
pytest tests/unit/pipeline/prompts/test_prompt_boundary.py
pytest tests/unit/pipeline/prompts/test_prompts.py
```
