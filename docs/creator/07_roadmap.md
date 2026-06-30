# Roadmap

## Status: public baseline v0.1.0 (June 2026)

### Done

| Version | What |
|--------|-----|
| v0.1.0 | CLI `orcho run` / `cross`, MockProvider, 3-level prompts |
| v0.1.0 | Session chaining (Claude --resume), checkpoint --resume |
| v0.1.0 | AppConfig, plan/task languages (plan_language, task_language) |
| v0.1.0 | `pipeline/engine/` — DRY extraction from both orchestrators |
| v0.1.0 | `core/` subdomains: infra, observability, io, context |
| v0.1.0 | PACKAGE_ROOT — depth-independent path resolution |
| v0.1.0 | Windows Phase B: platform detection, PowerShell script, .cmd wrapping |
| v0.1.0 | Package naming cleanup and entry points fix |

---

## In progress

| Priority | What | Where |
|-----------|-----|-----|
| HIGH | Run Evidence and Audit: typed plan, event spine, evidence bundle, MCP control loop | `pipeline/evidence/`, `docs/adr/0020` |
| HIGH | MetricsCollector cross-project support | `pipeline/engine/` |
| HIGH | CheckpointStore: `--resume` for the cross pipeline | `pipeline/checkpoint.py` |
| MED | Interface contract validator (Phase C cross) | `pipeline/cross_project/orchestrator.py` |
| MED | `orcho status` / `orcho history` CLI commands | `cli/orcho.py` |

---

## Planned

### v0.3.0 — Contract Validation

- Codex validates the interface contract after all sub-pipelines complete
- `cross_contract.md` — generated in Phase 0 cross, validated in Phase Final
- Fail fast if projects violate the contract between them

### v0.3.x — Plugin Formalization

- `PluginConfig` → docs-as-code (types, validation, defaults)
- JSON Schema for plugin.py (for IDE hints)
- `orcho plugin validate` — check plugin.py

### v0.4.0 — Observability++

- OpenTelemetry traces (optional)
- `orcho metrics --dashboard` — HTML report across all runs
- Cost estimation before a run (`orcho estimate --task "..."`)

### v0.5.0 — Agent Runtime Routing++

- The Gemini CLI runtime is already supported through `orcho.agent_runtimes`.
- OpenAI runtime variants as an alternative to Claude for BUILD.
- Runtime routing by task type in PluginConfig (different runtimes for different phases).

### Long-term

- GitHub Actions integration (`orcho-action`)
- MCP server — Orcho as a tool for other agents
- Distributed cross-project (different machines, one workspace)

---

## Known limitations

| Limitation | Status |
|-------------|--------|
| Codex requires CWD = git root | Workaround in CodexAgent._find_git_root() |
| Claude stream-json sometimes fails to parse | Fallback regex in _extract_session_id() |
| Windows: only Python 3.12+ officially | No CI on Windows (unit tests with monkeypatch only) |
| codemap: regex fallback is worse than tree-sitter | Optional grammars solve it |
