---
name: orcho-core-runtime-session
description: "Use when editing Orcho runtime/session code: orcho-core/pipeline/runtime/*, pipeline/engine/session.py, pipeline/session_adapters.py, agents/runtimes/*, agents/protocols.py, agents/registry.py, runtime invocation, session continuity/reset, runtime context autonomy, or agent runtime adapters. Do not use for prompt rendering unless paired."
---

# Orcho Core Runtime Session

Own the boundary between Orcho phase contracts and runtime agent context.

## First Reads

- `orcho-core/AGENTS.md`
- `orcho-core/pipeline/engine/session.py`
- `orcho-core/pipeline/session_adapters.py`
- `orcho-core/pipeline/runtime/`
- changed runtime under `orcho-core/agents/`
- ADR 0023, 0029, or 0030 when relevant

## Owns

- runtime invocation
- session reset and continuity
- runtime context autonomy
- runtime adapter contracts
- provider-session fallback behavior

## Does Not Own

- prompt part rendering/cache layout -> `orcho-prompt-engine`
- phase lifecycle and DAG execution -> `orcho-core-phases-engine`
- public SDK result shape -> `orcho-core-sdk-wire`

## Invariants

- Runtime constructors must be side-effect free.
- Resolve external binaries lazily at first real invocation.
- Recovery is contract-failure-driven, not token-fill driven.
- Preserve settable runtime binary properties for tests/adapters.

## Verification

- runtime/session unit tests
- session adapter tests
- snapshot/session parity tests when reset semantics change

## Neighbor Skills

- `orcho-prompt-engine` when session-aware prompt parts/cache change
- `orcho-core-phases-engine` when phase lifecycle or retry sequencing changes
- `orcho-core-isolation-worktrees-sandbox` when cwd/worktree subjects change
