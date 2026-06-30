---
name: orcho-prompt-engine
description: "Use for Orcho prompt authoring/rendering internals: orcho-core/pipeline/prompts/*, orcho-core/core/_prompts/*, prompt templates/contracts, cache-aware rendering, session/delta prompt metadata, prompt snapshots, and prompt boundary tests. Pair with evidence/observability only when rendered prompt data is stored as evidence/events/artifacts."
---

# Orcho Prompt Engine

Work safely on prompt composition, prompt contracts, cache layout, and prompt
evidence. Keep machine contracts in code-owned prompt blocks, not loose prose.

## First Reads

- `orcho-core/AGENTS.md`
- `orcho-core/pipeline/prompts/AGENTS.md`
- `orcho-core/core/_prompts/AGENTS.md`
- `orcho-core/docs/adr/0009-composable-prompt-parts.md`
- `orcho-core/docs/adr/0028-cache-first-physical-wire-layout.md`
- changed prompt builder/template files

## Owns

- `pipeline/prompts/*`
- `core/_prompts/*`
- prompt part ordering and cache metadata
- prompt contract templates and protected system-tail policy
- prompt render evidence and prompt snapshots

## Does Not Own

- runtime adapter invocation -> `orcho-core-runtime-session`
- SDK/MCP public payload shape -> `orcho-core-sdk-wire`
- parser/gate verdict semantics -> `orcho-core-quality-gates`

## Invariants

- Think in ordered prompt parts, not one rendered string.
- Preserve cache-stable prefix ordering unless deliberately changing it with tests.
- Put dynamic facts in typed prompt parts with explicit stability/cache scope.
- Keep protected contracts in code-owned templates.
- Do not silently widen editable markdown into protocol policy.

## Verification

- From `orcho-core`: `python -m pytest -q tests/unit/pipeline/prompts`
- From `orcho-core`: `python -m pytest -q tests/unit/pipeline/runtime/test_snapshot_session_parity.py` when cache/session behavior changes
- Review prompt snapshot/golden diffs before accepting them.

## Neighbor Skills

- `orcho-core-runtime-session` when prompt/session continuity changes
- `orcho-core-evidence-observability` when rendered prompt data is persisted as evidence
- `orcho-lab` when evaluating prompt variants
