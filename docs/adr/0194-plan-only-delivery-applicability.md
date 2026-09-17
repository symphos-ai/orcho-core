# ADR 0194: Plan-only delivery applicability

- **Status:** accepted
- **Date:** 2026-09-17

## Context

Built-in `planning` and `research` produce a typed plan and an approval, then
pause at `validate_plan` for a human decision. After the operator persists
`continue`, checkpoint resume can finish the recipe without executing any
change-producing phases. Delivery verification previously treated missing
required downstream receipts as a delivery blocker even when this completed
recipe had no delivery subject.

A plan artifact is input for a later implementation run. Its existence does
not mean this run produced changes to ship. Conversely, absence of `implement`
is insufficient evidence: `delivery_audit` and `code_review` inspect an
existing uncommitted change and remain subject to delivery verification.

## Decision

Delivery owns applicability. `pipeline/engine/delivery_applicability.py`
classifies the complete resolved recipe and canonical run facts;
`resolve_commit_delivery` owns the Git subject and the final decision.
The project runner only supplies the complete profile and persists the result.

The narrow exception requires all of the following:

- The full typed recipe consists of `plan` followed by `validate_plan`, including
  phases inside loop steps; a profile name or the remaining resume steps is
  insufficient.
- The run is `done` with no active handoff, a nonempty planned task count, a
  latest approved `APPROVED` validation, and no other nonempty phase results.
- The existing baseline-aware patch reader proves an empty tracked patch, and
  the existing filtered untracked reader proves no deliverable untracked files
  under the configured `add_untracked` policy.

Unknown recipes, missing or inconsistent facts, an unavailable patch, and
failed untracked inspection cannot authorize this exception. A missing
`diff.patch` artifact alone proves nothing. No second Git diff algorithm or
receipt policy is introduced.

Adoption of an existing delivery commit and release guards retain priority.
Only the proven plan-only/no-subject case returns an explicit canonical
`not_applicable` before missing downstream receipts can block delivery.
Ordinary delivery retains verification-before-no-diff ordering: even a clean
checkout does not excuse its required receipts. Changed plan-only checkouts,
feature/refactor/migration runs and review-only subjects remain protected.

The explicit decision is persisted before terminal consumers are built. Its
internal persistence instruction is not a new wire field. Ordinary approved
final-acceptance no-diff runs continue to omit `commit_delivery`. Capture diff,
delivery outcome, terminal projection, and terminal writes keep their existing
ordering. Earlier handoff checkpoints and operator decisions remain historical
facts; they are not rewritten as successful completion.

## Consumers and continuation

Checkpoint state, `meta.json`, the latest `run.end`, evidence and SDK/CLI status
agree on `done` after approved continuation. The parsed plan and approval remain
available.

Оператор явно расширил scope на канонического владельца `sdk/actions.py` и
focused SDK/MCP compatibility tests для закрытия C2 / review F1. Успешный
terminal outcome `not_applicable` с `action=none`, без ошибки, delivery commit,
release verdict, активного handoff или halt reason и с подтверждённым
`parsed_plan.json` публикует единственное `from_run_plan` через существующий
builder. Физическое наличие артефакта обязательно: одного `plan_source`
недостаточно. Обычный feature done или `no_diff` не открывает новое действие;
существующая recovery-семантика halted/rejected остаётся прежней.

SDK не классифицирует рецепт повторно: отсутствие доставки уже доказал её
владелец, а остальные сохраняемые `not_applicable` (rejected release и adopted
commit) исключены по их каноническим полям. Действие использует существующий
`orcho_run_start` с `from_run_plan`, `profile=feature` и исходной задачей.
При отсутствии задачи builder запрашивает настоящий ввод оператора.
Схема SDK и компактная MCP-сериализация Action остаются без новых полей.

Missing receipts remain missing in evidence. This is applicability of delivery,
not a waiver, a synthetic passing receipt, or suppression of scheduled gates.
The engine continues to own scheduled gate execution and authoritative receipts;
manual/suggest entries remain operator-owned.

## Verification and limits

The acceptance regression executes both built-in profiles in real temporary Git
projects through a real handoff, SDK `continue`, checkpoint resume and terminal
consumers. It also exercises rejected handoffs and SDK `halt`, whose terminal
reason and refusal of subsequent resume remain unchanged. Delivery tests cover
real changed and untracked subjects, review-only recipes, unknown profile facts,
failed Git reads, and the existing clean-diff verification guard.

See [Verification Contract](../architecture/verification_contract.md#plan-only-delivery-applicability).
Focused SDK-тесты фиксируют компактный `Action.to_dict()` и публичный status
JSON; существующий schema snapshot проверяет отсутствие wire drift. Прямой
MCP status adapter также проверен на артефактах обоих acceptance mock-запусков
с импортом SDK из текущего checkout. Повтор инцидента на stable install
остаётся дополнительной проверкой; stable install не изменяется.
