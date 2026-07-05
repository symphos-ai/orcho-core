# Orcho — Documentation

The documentation is ordered from general to specific: start with the
concepts and the first run, go deeper only when you need to. Each level
assumes the previous one.

---

## Level 1 — User

> You want to use the system. You do not need to know how it works inside.

Start here, in order:

| File | What's inside |
|------|-----------|
| [user/00_getting_started.md](user/00_getting_started.md) | **Start here** — concepts, prerequisites, the full path from zero to the first result via MCP, Web, or CLI |
| [user/01_quickstart.md](user/01_quickstart.md) | Install and first run in 5 minutes (CLI path) |
| [user/02_commands.md](user/02_commands.md) | Every `orcho` command with examples |
| [user/03_workspaces.md](user/03_workspaces.md) | Connecting your project, workspaces, multiple repos |
| [user/04_results.md](user/04_results.md) | What a run produces, how to read artifacts, diff delivery |
| [user/early_adopter_install.md](user/early_adopter_install.md) | Full source-checkout install: core + MCP + Web |

---

## Level 2 — Expert operator

> You use the system regularly and want to get the most out of it:
> project plugins, custom prompts, model routing, fine-grained config.

| File | What's inside |
|------|-----------|
| [expert/01_plugin.md](expert/01_plugin.md) | Writing `plugin.py` — the full field reference |
| [expert/02_prompts.md](expert/02_prompts.md) | The 3-level prompt system, overrides |
| [expert/03_config.md](expert/03_config.md) | All env variables, models, timeouts, config layering |
| [expert/04_pipeline_phases.md](expert/04_pipeline_phases.md) | What every phase does and how to control them |
| [expert/05_windows.md](expert/05_windows.md) | Installing and configuring on Windows |

---

## Level 3 — Integrator (authoring guides)

> You are wiring Orcho into a team or an organisation: custom profiles,
> quality gates, execution modes, runtime adapters.

| File | What's inside |
|------|-----------|
| [guides/profile_authoring.md](guides/profile_authoring.md) | Declare your own pipeline profiles |
| [guides/quality_gate_authoring.md](guides/quality_gate_authoring.md) | Author quality gates and failure policies |
| [guides/execution_mode_authoring.md](guides/execution_mode_authoring.md) | Custom execution modes for phases |
| [guides/session_adapter_authoring.md](guides/session_adapter_authoring.md) | Session adapters for agent runtimes |
| [guides/multimodal_runtime_support.md](guides/multimodal_runtime_support.md) | Attachments and multimodal runtime support |

---

## Reference

> Exact schemas and registries. Look things up; do not read in order.

| File | What's inside |
|------|-----------|
| [reference/profile_schema.md](reference/profile_schema.md) | The full profile schema |
| [reference/run_artifacts.md](reference/run_artifacts.md) | Run directory files: `meta.json`, `events.jsonl`, `metrics.json`, `evidence.json` |
| [reference/event_registry.md](reference/event_registry.md) | `events.jsonl` registry: events, required payload keys, rules for `SILENT`/MCP consumers |
| [reference/operating_modes.md](reference/operating_modes.md) | Work modes `fast`/`pro`/`governed`: strictness matrix + when to use |
| [reference/resume_modes.md](reference/resume_modes.md) | Resume and session semantics |
| [reference/attachments.md](reference/attachments.md) | Attachment model |
| [reference/builtin_gates.md](reference/builtin_gates.md) | Built-in quality gates |
| [reference/sdk_api.md](reference/sdk_api.md) | SDK surface |
| [reference/types.md](reference/types.md) | Shared types |

---

## Level 4 — Engine developer

> You develop or extend the orchestrator itself. Full architecture,
> internal contracts, contribution workflow.

| File | What's inside |
|------|-----------|
| [creator/01_architecture.md](creator/01_architecture.md) | Full system architecture — all layers and links |
| [creator/02_package_structure.md](creator/02_package_structure.md) | Package layout, code organisation principles |
| [creator/03_agent_contracts.md](creator/03_agent_contracts.md) | Agent protocols, providers, mocks |
| [creator/04_pipeline_internals.md](creator/04_pipeline_internals.md) | Inside pipeline/engine/ — DRY core, RunContext, sessions |
| [creator/05_core_subdomains.md](creator/05_core_subdomains.md) | core/ subdomains — infra, observability, io, context |
| [creator/06_testing.md](creator/06_testing.md) | Test strategy, MockProvider, test layout |
| [creator/07_roadmap.md](creator/07_roadmap.md) | Roadmap — done, in progress, planned |
| [creator/08_contributing.md](creator/08_contributing.md) | How to contribute — git flow, code standards |
| [creator/09_dev_workflow.md](creator/09_dev_workflow.md) | Day-to-day engine development workflow |

Architecture deep-dives live in [architecture/](architecture/), decisions
in [adr/](adr/), demo walkthroughs in [demos/](demos/).
