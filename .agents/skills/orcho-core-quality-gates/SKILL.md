---
name: orcho-core-quality-gates
description: "Use when editing orcho-core parser and gate semantics: plan_parser, review_parser, release_parser, plan_contract, quality_gates, pipeline/control, review/repair/final acceptance, halt/resume/operator decisions, release verdicts. Do not use for prompt wording or public wire shape unless paired."
---

# Orcho Core Quality Gates

Own parser internals and the meaning of review, repair, final acceptance, and
halt/resume decisions.

## First Reads

- `orcho-core/AGENTS.md`
- `orcho-core/pipeline/plan_parser.py`
- `orcho-core/pipeline/review_parser.py`
- `orcho-core/pipeline/release_parser.py`
- `orcho-core/pipeline/quality_gates.py`
- `orcho-core/pipeline/control/*` when control flow changes
- relevant ADR for the gate being changed

## Owns

- parser semantics and parser errors
- gate verdict meaning
- review/repair/final acceptance behavior
- halt/resume/operator decision flow
- release approval/rejection semantics

## Does Not Own

- public payload shape -> `orcho-core-sdk-wire`
- prompt contract text -> `orcho-prompt-engine`
- cross-project orchestration -> `orcho-core-cross-project`

## Invariants

- Parser output shape changes require tests.
- Review, repair, and final acceptance are distinct contracts.
- A rejected release verdict is not the same as a parser/schema hard halt.
- No low-priority finding should mask a release-tier blocker.

## Verification

- targeted parser/gate tests under `orcho-core/tests/unit/pipeline`
- halt/resume tests when operator flow changes
- SDK/MCP tests when output shape becomes public

## Neighbor Skills

- `orcho-core-sdk-wire` for public parser/gate output shape
- `orcho-prompt-engine` for gate prompt contract text
- `orcho-core-cross-project` for cross final acceptance orchestration
