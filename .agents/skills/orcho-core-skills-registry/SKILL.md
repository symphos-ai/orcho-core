---
name: orcho-core-skills-registry
description: "Use when editing Orcho skill support: orcho-core/pipeline/skills/*, agents/stream_parsers/skill_registry.py, skill discovery order, .agents/.claude/.forge compatibility, trust policy, roster rendering, skill injection, skill bindings, skill traceability/events, `orcho skills list/trace`, or MCP skill catalogue exposure. Pair with sdk-wire or orcho-mcp when exposed publicly."
---

# Orcho Core Skills Registry

Own Orcho's skill discovery, trust, injection, and traceability surfaces.

## First Reads

- `orcho-core/AGENTS.md`
- `orcho-core/pipeline/skills/types.py`
- `orcho-core/pipeline/skills/discover.py`
- `orcho-core/pipeline/skills/loader.py`
- `orcho-core/pipeline/skills/inject.py`
- `orcho-core/agents/stream_parsers/skill_registry.py`
- `docs/plans/core/2026-05-19-skill-traceability-cli-ux.md` when traceability changes

## Owns

- `SkillPackage`, `SkillBinding`, `SkillTrustPolicy`
- multi-source discovery and source priority
- `.agents/skills`, `.claude/skills`, `.forge/skills` compatibility
- trust gates and include-untrusted introspection
- skill roster rendering into planning
- full `SKILL.md` injection into subtask prompts
- skill-use stream parsing and events
- skill list/trace CLI behavior when paired with `orcho-core-cli-ux`

## Does Not Own

- generic prompt rendering/cache -> `orcho-prompt-engine`
- public SDK/MCP shape -> `orcho-core-sdk-wire`
- MCP handler implementation -> `orcho-mcp`
- general CLI formatting -> `orcho-core-cli-ux`

## Invariants

- Skills provide instructions, not runtime/model/provider selection.
- Project and compat skills are trust-gated by default.
- Discovery must preserve deterministic priority and shadowing diagnostics.
- Full skill bodies should not leak into durable evidence by default.
- Resource bodies are loaded only on demand.

## Verification

- `python -m pytest -q orcho-core/tests/unit/pipeline/skills`
- `python -m pytest -q orcho-core/tests/unit/agents/test_skill_registry.py`
- CLI skill tests when commands change
- MCP skill-list tests when exposed through MCP

## Neighbor Skills

- `orcho-core-cli-ux` for `orcho skills` command UX
- `orcho-core-sdk-wire` when skill trace/list shape becomes public
- `orcho-mcp` for MCP skill catalogue exposure
