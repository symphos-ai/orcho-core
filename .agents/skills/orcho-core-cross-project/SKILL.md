---
name: orcho-core-cross-project
description: "Use when editing cross-project orchestration lifecycle and wiring: orcho-core/pipeline/cross_project/*, handoff, contract check, artifact bundle, profile projection, child dispatch, cross-project gate handoff, release-gate orchestration, and final-acceptance bundle flow. Pair with quality-gates for parser/verdict/gate semantics; do not use for single-project gate/parser changes."
---

# Orcho Core Cross Project

Own multi-project orchestration and cross-project acceptance.

## First Reads

- `orcho-core/AGENTS.md`
- `orcho-core/pipeline/cross_project/`
- `orcho-core/docs/architecture/cross_project_pipeline.md`
- `orcho-core/docs/adr/0024-cross-profile-projection.md`
- `orcho-core/docs/adr/0025-release-gate-and-cross-final-acceptance.md`
- `orcho-core/docs/adr/0050-structured-cross-handoff.md`

## Owns

- cross-project planning and dispatch
- handoff artifacts
- artifact bundles
- cross profile projection
- contract-check decisions
- cross final acceptance

## Does Not Own

- single-project gate semantics -> `orcho-core-quality-gates`
- child runtime invocation internals -> `orcho-core-runtime-session`
- SDK/MCP public shape -> `orcho-core-sdk-wire`

## Invariants

- Keep dependency direction clean; cross-project code lives in `orcho-core`.
- Do not inline focused cross-project modules back into app facades.
- Cross changes often need MCP/Web awareness, but not always code changes.

## Verification

- From `orcho-core`: `python -m pytest -q tests/unit/pipeline/cross_project`
- From `orcho-core`: include targeted profile projection and artifact bundle tests when touched.
- Pair with SDK/MCP tests when public payload changes.

## Neighbor Skills

- `orcho-core-quality-gates` for parser/verdict/gate semantics
- `orcho-core-sdk-wire` for public payload shape
- `orcho-mcp` for MCP-visible cross-project state
