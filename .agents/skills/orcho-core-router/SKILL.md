---
name: orcho-core-router
description: "Use only for orcho-core triage/routing questions when the affected subdomain is unclear and no specific specialist, file path, or known subsystem is named. Route among prompt engine, runtime/session, phases/DAG, parsers/gates, evidence/observability, isolation/worktrees/sandbox, test infrastructure/goldens, cross-project, profiles/plugins, skills registry, SDK wire, and CLI UX. Do not use for implementation once a specialist scope is clear."
---

# Orcho Core Router

Route ambiguous `orcho-core` work to one primary specialist. Do not implement
here and do not deep-read ADRs here.

## Owns

- subdomain selection inside `orcho-core`
- pair-load decisions when a change crosses two core surfaces

## Does Not Own

- implementation
- deep ADR reading
- explicit file tasks that already name a specialist-owned subsystem

## First Reads

- `/Users/smartgamma/www/orcho/orcho-core/AGENTS.md`
- `orcho-core/docs/creator/02_package_structure.md`
- `orcho-core/docs/creator/05_core_subdomains.md`

## Decision Table

| Task area | Specialist |
| --- | --- |
| prompt files, prompt contracts, cache layout, `_prompts` | `orcho-prompt-engine` |
| runtime invocation, sessions, adapters, agent runtimes | `orcho-core-runtime-session` |
| phase lifecycle, phase handlers, DAG/subtasks, `StepOutcome` | `orcho-core-phases-engine` |
| plan/review/release parsers, gate semantics, halt/resume | `orcho-core-quality-gates` |
| evidence bundles, events, metrics, run diff, artifacts | `orcho-core-evidence-observability` |
| sandbox, isolation setup, dirty intake, worktrees | `orcho-core-isolation-worktrees-sandbox` |
| shared test infra, fixtures, golden snapshots, pytest config | `orcho-core-test-infra-goldens` |
| cross-project handoff, contract check, final bundle | `orcho-core-cross-project` |
| profiles, operating modes, plugin entry points | `orcho-core-profiles-plugins` |
| skill discovery, trust, injection, roster, traceability | `orcho-core-skills-registry` |
| SDK schema, public payload, MCP-visible core shape | `orcho-core-sdk-wire` |
| CLI commands, help, output formatting, terminal UX | `orcho-core-cli-ux` |

## Cross-Domain Pairs

- parser output exposed publicly -> `orcho-core-quality-gates` + `orcho-core-sdk-wire`
- evidence exposed publicly -> `orcho-core-evidence-observability` + `orcho-core-sdk-wire`
- cross final acceptance -> `orcho-core-cross-project` + `orcho-core-quality-gates`
- runtime prompt/session shape -> `orcho-core-runtime-session` + `orcho-prompt-engine`
- skill trace exposed through MCP -> `orcho-core-skills-registry` + `orcho-mcp` + maybe `orcho-core-sdk-wire`

## Output

Pick one primary specialist, add a neighbor only when needed, and name nearby
specialists that are not needed.

## Invariants

- Prefer a specialist over the router once a file path or subsystem is clear.
- Add at most one or two neighbors; do not load the whole registry.
- Do not use this skill for non-core repos.

## Verification

- Check the selected specialist's `First Reads` before implementation.
- If route crosses SDK/MCP, include `orcho-core-sdk-wire` and `orcho-mcp`.

## Neighbor Skills

- `orcho-workspace-router` when repo ownership is unclear
- `orcho-integrity-pipeline` before commit-ready claims
