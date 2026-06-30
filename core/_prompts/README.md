# `_prompts/` — Core Prompt Templates

Агностичные shipped prompt parts для динамических шагов пайплайна.
Работают для **любого проекта** без изменений.

Здесь лежат **только composable parts** — `roles/*`, `tasks/*`,
`formats/*`. Legacy flat names (`developer_build.md`,
`reviewer_code_review.md`, …) не существуют: builders их не знают,
а project-level flat overrides на эти имена не влияют на render.
Поддерживаются только overrides composable parts.

---

## ⚠️ Перед редактированием — Prompt Part Boundary

**Read `orcho-core/core/_prompts/AGENTS.md -> Prompt Boundary Discipline` first.**
**Run `pytest tests/unit/pipeline/prompts/test_prompt_boundary.py` ПЕРЕД commit'ом.**

### Ownership matrix

| Layer | Owns | Edit surface |
|---|---|---|
| `_prompts/roles/*.md` | Persona and posture only. Кто говорит. No runtime placeholders. | user / project / workspace |
| `_prompts/tasks/*.md` | Процедура и checks для phase. Что делать. Runtime task/body/artifact data arrives as typed dynamic `PromptPart`s, not `$variables`. | user / project / workspace |
| `_prompts/formats/*.md` | Presentation/detail/style — **role-agnostic, reusable**: `terse`, `detailed`, `bullets`, `handoff`. Как подать ответ. | user / project / workspace |
| `pipeline.prompts.contracts` system-tail blocks | Machine output shape, language posture, safety/orchestration policy. **Code-only**. | code |

### System-tail single sources of truth

| Concern | Block |
|---|---|
| Language posture (prose + code) | `authoring_language_strategy(task_language=...)` |
| Reviewer JSON body language | `review_json_contract(body_language=...)` |
| Plan JSON body language | `plan_json_contract(body_language=, input_language=)` |
| Destructive git, preserve user-owned, commit/push policy | `change_handoff_strategy(mode=...)` |
| What reviewer inspects (working-tree / commit / commit_set) | `review_target_strategy(mode=...)` |
| JSON-only output + REVIEW_SCHEMA_DOC | `review_json_contract` |
| JSON-only output + PLAN_SCHEMA_DOC | `plan_json_contract` |
| Cross-project subtask block grammar | `cross_subtask_block_contract` |

### Forbidden examples — **all checked by `test_prompt_boundary.py`**

| ❌ Don't put this in a user-editable part | ✅ Live where |
|---|---|
| `$task_language`, `$body_language`, `$artifact_language` | `authoring_language_strategy` / `review_json_contract` / `plan_json_contract` |
| "Language: Reply in {X}" / "Respond in {X}" prose | same |
| `VERDICT: APPROVED` / `VERDICT: REJECTED` protocol text | nowhere; reviewer gates use JSON only |
| JSON schema body, "exactly one JSON object", enum (P0/P1/P2/P3, APPROVED/REJECTED) | `review_json_contract` / `plan_json_contract` |
| "Do not run destructive git commands" / "Preserve user-owned working-tree" | `change_handoff_strategy` |
| Names of system-tail blocks (`authoring_language`, `change_handoff`, `cross_subtask_blocks`, etc.) | the blocks themselves |
| Persona-bound phrasing in `formats/*` ("reviewer findings", "code change discipline") | `roles/*` or `tasks/*` |
| `$task` / `$body` / `$extra_step` template variables in `formats/*` | `tasks/*` |

### Decision tree — "I want to add X. Where?"

- **X is about language** ("respond in Russian") → extend `authoring_language_strategy` (or the right `*_contract.body_language` if X is about a parser-owned JSON body).
- **X is about output shape** (JSON, enum values, schema) → extend the right `*_contract` block in `pipeline.prompts.contracts`.
- **X is about git / working-tree / commit behaviour** → extend `change_handoff_strategy`.
- **X is about review target** (what reviewer inspects) → extend `review_target_strategy`.
- **X is about how a finding/response should look** (style, brevity, bullets, handoff) → use or create a `formats/*` preset.
- **X is "you are the {role}"** → `roles/{role}.md`.
- **X is "for this phase, do {procedure}"** → `tasks/{procedure}.md`.

If two of the above apply at once, the more code-owned layer wins.

### When in doubt

```bash
pytest tests/unit/pipeline/prompts/test_prompt_boundary.py   # structural floor
pytest tests/unit/pipeline/prompts/test_prompts.py           # family-specific guards
```

This README is the operational reminder you hit when editing a prompt
file; the ownership matrix and forbidden-examples table above are the
authoritative boundary.

---

## Shipped parts

### roles
| File | Назначение |
|---|---|
| `roles/code_reviewer.md` | Code reviewer persona |
| `roles/implementation_engineer.md` | Implementation engineer persona |
| `roles/plan_reviewer.md` | Plan reviewer persona |
| `roles/release_manager.md` | Release manager persona |
| `roles/systems_architect.md` | Systems architect persona |
| `roles/product_owner.md` | Product owner persona |

### tasks
| File | Шаг |
|---|---|
| `tasks/plan.md` | PLAN — architect создаёт implementation plan JSON |
| `tasks/replan.md` | replan — architect правит plan после rejection |
| `tasks/decompose.md` | decompose — architect эмитит DAG subtasks |
| `tasks/implement.md` | implement — developer реализует задачу |
| `tasks/repair_changes.md` | repair_changes — developer правит после critique |
| `tasks/code_review.md` | review_changes |
| `tasks/final_acceptance.md` | final_acceptance — release-readiness gate |
| `tasks/validate_plan.md` | validate_plan — plan_reviewer валидирует typed plan views (`plan_contract:typed_plan`, `plan_tasks:execution_plan`) |
| `tasks/hypothesis.md` | hypothesis — architect эмитит pre-plan гипотезу |
| `tasks/validate_hypothesis.md` | validate_hypothesis — reviewer валидирует гипотезу (artifact prepended as a code-owned `artifact:validate_hypothesis` part) |
| `tasks/readonly_plan.md` | readonly plan — protocol-level architect surface |
| `tasks/review_uncommitted.md` | runtime review configured change target |
| `tasks/cross_plan.md` | CROSS PLAN — architect планирует across multiple codebases |
| `tasks/cross_contract_bundle.md` | contract_check artifact-bundle review |

### formats
| File | Назначение |
|---|---|
| `formats/terse.md` | Concise, result-first |
| `formats/detailed.md` | Detailed: findings + rationale + risks + verification |
| `formats/bullets.md` | Short bullet lists |
| `formats/handoff.md` | Write for the next agent / maintainer |

---

## Runtime data and placeholders

Editable role/task/format parts should be stable markdown. Do not add
`$task`, `$body`, `$project_dir`, `$context`, language placeholders, schema
placeholders, or artifact bodies to these files.

Runtime facts are injected by code-owned typed parts:

| Runtime fact | Lives in |
|---|---|
| User task / current turn input | `turn_input` / phase-specific dynamic part |
| Plan, review, repair, or validation artifact body | `artifact` / `plan_contract` / `feedback` |
| Project, workspace, codemap, attachments | `context` / `codemap` / attachment-backed parts |
| Parser contract, language posture, git/review policy | `system_tail` blocks in `pipeline.prompts.contracts` |

The composer still uses `string.Template` internally for code-owned rendering
surfaces, but adding placeholders to shipped editable markdown breaks the
cache-first wire layout: any non-static byte in the leading prefix invalidates
provider prefix caching for every call. If a phase needs new dynamic data, add
a typed part in the builder/composer path instead of adding `$variable` prose
here.

---

## Project overrides

Поддерживаются ТОЛЬКО на уровне composable parts:

```
{project_dir}/.orcho/multiagent/prompts/roles/{role}.md
{project_dir}/.orcho/multiagent/prompts/tasks/{task}.md
{project_dir}/.orcho/multiagent/prompts/formats/{format}.md
```

Resolution chain (первый найденный выигрывает):

```
project/.orcho/multiagent/prompts/{layer}/{name}.md   ← project override
→ workspace/.orcho/multiagent/prompts/{layer}/{name}.md  ← workspace override
→ _prompts/{layer}/{name}.md                          ← core (этот каталог)
```

**Legacy root-level flat names (`developer_build.md`, `reviewer_code_review.md`,
etc.) больше не участвуют в resolution.** Project authors,
которые держали такие overrides, должны переписать их в виде composable
parts.

---

## Добавить новый шаблон

1. Создай нужный stable part в `roles/`, `tasks/` или `formats/`.
2. Не добавляй `$variables`; если нужны runtime facts, добавь typed dynamic
   part в builder/composer path.
3. Вызови composer из `pipeline.prompts.composer.render_composed_prompt` с
   подходящим `PromptSpec`.
4. Проверь, что новый part не несёт parser/schema/language/git/review-target
   контрактов; такие правила живут в `pipeline.prompts.contracts`.
5. **ПРОВЕРЬ** что `pytest tests/unit/pipeline/prompts/test_prompt_boundary.py`
   зелёный. Если форматированная часть, перепроверь decision tree выше.

## Добавить новый `PromptPart.kind`

1. Обнови `pipeline/prompts/types.py`, если нужен новый derived layer/default.
2. Добавь ordering в `pipeline/prompts/composer.py::_KIND_ORDER`, чтобы
   cache-first wire layout оставался предсказуемым.
3. Добавь lifecycle классификацию в
   `pipeline/observability/output_class.py::PROMPT_PART_CLASS_RULES`.
4. Обнови или добавь coverage в:
   - `tests/unit/pipeline/prompts/test_prompt_part_model.py`
   - `tests/unit/pipeline/prompts/test_wire_cache_layout.py`
   - `tests/unit/pipeline/observability/test_output_class.py`
5. Если новый kind меняет protocol-level prompt semantics, добавь новый ADR или
   superseding ADR note вместо правки истории.
