---
name: orcho-core-profiles-plugins
description: "Use when editing Orcho profiles and plugin extension surfaces: profile schema/catalogue, semantic profiles, operating modes, profile projection, phase config, profile-driven gate scheduling/configuration, PluginConfig loading, entry-point groups for agent_runtimes/phases/skills, plugin authoring docs, and profile validation. Do not use for runtime execution internals or SDK wire unless paired."
---

# Orcho Core Profiles Plugins

Own profiles, operating modes, plugin loading, and extension-point contracts.

## First Reads

- `orcho-core/AGENTS.md`
- `orcho-core/docs/reference/profile_schema.md`
- `orcho-core/docs/guides/profile_authoring.md`
- `orcho-core/docs/expert/01_plugin.md`
- `orcho-core/pipeline/plugins.py`
- `orcho-core/pipeline/profiles/`
- `orcho-core/pyproject.toml` entry-point groups when extension points change

## Owns

- profile schema and catalogue
- semantic profiles and operating modes
- profile projection and phase config
- `PluginConfig` loading
- extension entry-point groups
- plugin/profile authoring docs

## Does Not Own

- runtime execution internals -> `orcho-core-runtime-session`
- phase lifecycle execution -> `orcho-core-phases-engine`
- public SDK/MCP payload shape -> `orcho-core-sdk-wire`
- prompt wording -> `orcho-prompt-engine`

## Invariants

- Core owns extension protocols; plugins own provider-specific behavior.
- Runtime constructors and profile listing must stay side-effect free.
- Extension docs in public packages must use generic third-party wording.
- Profile/schema changes need validation tests and MCP alignment if exposed.

## Verification

- From `orcho-core`: `python -m pytest -q tests/unit/pipeline/profiles tests/unit/pipeline/plugins`
- Run profile schema snapshot/validation tests when schema changes.
- Pair with SDK/MCP tests when profile shape is public.

## Neighbor Skills

- `orcho-core-sdk-wire` when profile/catalogue shape is public
- `orcho-mcp` when profile catalogue is exposed through MCP
- `orcho-core-runtime-session` when profile changes affect runtime construction
