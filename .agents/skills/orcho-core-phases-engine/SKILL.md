---
name: orcho-core-phases-engine
description: "Use when editing phase lifecycle/execution mechanics: phase handlers/registry, orcho-core/pipeline/phases/*, project run phase bookkeeping, subtask DAG execution, StepOutcome, subtask receipts, done-criteria attestation, phase transitions, and phase-level handoff/routing. Do not use for parser verdicts, gate decisions, or runtime adapter internals unless paired."
---

# Orcho Core Phases Engine

Own phase lifecycle, DAG execution, phase handlers, and per-phase bookkeeping.

## First Reads

- `orcho-core/AGENTS.md`
- `orcho-core/docs/architecture/phase_lifecycle.md`
- `orcho-core/docs/architecture/execution_modes.md`
- `orcho-core/pipeline/phases/`
- `orcho-core/pipeline/project/run.py`
- changed phase handler or DAG module

## Owns

- phase handler registration and invocation
- phase lifecycle transitions
- `StepOutcome` handling
- DAG/subtask execution
- subtask receipts and done-criteria attestation
- phase handoff and repair routing glue
- phase-end bookkeeping

## Does Not Own

- parser/gate verdict meaning -> `orcho-core-quality-gates`
- runtime adapter invocation -> `orcho-core-runtime-session`
- prompt contract text -> `orcho-prompt-engine`
- public SDK/MCP payload shape -> `orcho-core-sdk-wire`

## Invariants

- Keep public app modules thin; lifecycle logic belongs in focused modules.
- Do not add new responsibilities to overloaded orchestration bodies.
- Preserve phase naming scheme.
- Phase changes that affect public state need SDK/MCP awareness.

## Verification

- From `orcho-core`: run targeted phase handler tests for the changed phase.
- From `orcho-core`: `python -m pytest -q tests/unit/pipeline` with a focused `-k` for subtask/DAG/phase lifecycle.
- Run a mock project acceptance smoke when lifecycle sequencing changes.
- Pair with SDK/MCP tests for public phase-state changes.

## Neighbor Skills

- `orcho-core-quality-gates` for gate/parser verdict semantics
- `orcho-core-runtime-session` for runtime adapter/session behavior
- `orcho-core-sdk-wire` for public phase state
