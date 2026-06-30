---
name: orcho-core-verification-matrix
description: "Use only when selecting targeted tests, smoke checks, pytest marks, or verification strategy for orcho-core changes, including questions like \"which tests should I run?\" Covers SDK schema, prompts, runtime/session, evidence, cross-project, CLI, profiles/plugins, skills registry, golden fixtures, and acceptance mock flows. Do not use for implementation, explanation, or final integrity gates."
---

# Orcho Core Verification Matrix

Map changed `orcho-core` surfaces to focused test commands before the full
integrity gate.

## Owns

- targeted test and smoke selection for `orcho-core`
- pytest mark/path recommendations
- downstream smoke recommendations before final gate

## Does Not Own

- implementation
- domain semantics
- final readiness gate -> `orcho-integrity-pipeline`

## First Reads

- `/Users/smartgamma/www/orcho/DEVELOPMENT_PIPELINE.md`
- `orcho-core/AGENTS.md`
- changed source files
- nearest test directories

## Test Map

| Changed surface | Start with |
| --- | --- |
| SDK schema/wire | `orcho-core/tests/sdk/test_schema_snapshot.py`, relevant `tests/sdk/test_*` |
| evidence slices/run diff | `tests/sdk/test_evidence_slices.py`, evidence tests, run diff tests |
| prompt boundary/cache | prompt tests under `tests/unit/pipeline/prompts` |
| runtime/session | runtime/session tests under `tests/unit/pipeline` |
| quality gates/parsers | parser and gate tests under `tests/unit/pipeline` |
| cross-project | `tests/unit/pipeline/cross_project` |
| CLI UX | `tests/unit/cli` |
| profiles/plugins | profile/plugin tests under `tests/unit/pipeline` |
| skills registry | `tests/unit/pipeline/skills`, skill parser tests |
| full mock behavior | relevant acceptance/integration mock flow |

## Invariants

- Targeted tests do not replace final `ruff`, pytest gate, and `git diff --check`.
- Public schema changes include snapshots and downstream smoke.
- Prompt golden diffs must be reviewed, not blindly accepted.

## Verification

- This skill recommends commands; it does not replace running them.
- If public schema or MCP-visible shape changes, include snapshot and MCP smoke commands.

## Output

Name primary domain skill, targeted commands, whether full pytest is still
required, downstream smoke, and blockers if any command cannot run.

## Command Policy

- Run commands from `orcho-core` unless the command explicitly names another repo.
- Prefer `python -m pytest -q <path>` for targeted tests.
- Final readiness still belongs to `orcho-integrity-pipeline`.

## Neighbor Skills

- relevant core domain specialist for behavior
- `orcho-integrity-pipeline` for final readiness
- `orcho-mcp` for MCP-visible shape or registration changes
