# ADR 0021 — Public SDK boundary in `sdk/`

## Status

Accepted. Implemented.

## Context

Before this decision, programmatic access to orcho-core's run-state model lived
implicitly: split across `pipeline.project_orchestrator`, `pipeline.argv`,
`core.observability.metrics`, `core.observability.pricing`, `pipeline.evidence`,
and `core.io.prompt_loader`, with the fattest reusable logic (cost aggregation,
status assembly, history listing) buried inside `cli/orcho.py` as private
helpers and 280-line subcommand handlers.

ADR 0020 already pinned the run-evidence model to core and called for a
"programmatic API / hooks" surface. The standing project rule that **MCP must
not become a second orchestration implementation** turns that surface into a
hard requirement: without a typed boundary, every consumer (CLI, MCP server, Web
dashboard, third-party embedders, future out-of-process runners) re-parses the
same artifacts on its side. That fork is the failure mode this ADR prevents.

## Decision

A new `sdk/` package inside `orcho-core` is the public programmatic boundary.
Every embedder calls it as `from sdk import …`. The package is published as
part of `orcho-core` (no separate distribution today; physical separation can
follow if it ever becomes load-bearing).

### Surface

`sdk/__init__.py` declares `__all__` covering:

- **Errors:** `OrchoError`, `NoWorkspace`, `RunNotFound`, `PricingFetchError`,
  `PromptNotFound`, `EvidenceInvalid`. Every error subclass carries an
  `exit_code: int` so process-exit-code mapping lives once.
- **Serialisation:** `to_jsonable(value: Any) -> Any` — a recursive projection
  that walks dataclasses, lists/tuples/sets, dicts, `Path`, `datetime`/`date`,
  `Enum`, primitives, and everything else through `str(value)`. The result
  round-trips through `json.dumps`. This is the IPC contract.
- **Read/report API:** `find_runs_dir`, `find_run`, `load_meta`, `load_status`,
  `list_history`, `collect_evidence`, `render_evidence_md`,
  `write_evidence_bundle`, `get_run_metrics`, `list_metrics`, `list_prompts`,
  `resolve_prompt`, `show_pricing`, `refresh_pricing`, `aggregate_cost`.
- **Runner:** `run_pipeline`, `run_cross_pipeline`, `build_orch_argv`,
  `run_pipeline_from_args`, `run_cross_from_args`.
- **Types:** `RunRef`, `RunMeta`, `PhaseStatus`, `RunStatus`, `RunSummary`,
  `RunMetrics`, `PhaseBreakdown`, `AgentBreakdown`, `CostReport`,
  `EvidenceBundle`, `PromptResolution`, `PricingTable`, `RefreshResult`. All
  public dataclasses are `frozen=True, slots=True`.

### Contract

1. **Pure return values.** Every read/report SDK call returns a typed
   dataclass (or list of dataclasses). No `print`. No `sys.exit`.
2. **Typed errors.** SDK calls raise `OrchoError` subclasses; embedders
   choose how to render them. Returning sentinel `None` for "not found"
   is banned — `RunNotFound` is the contract.
3. **JSON-friendly through `to_jsonable`.** Every public return value
   round-trips through `json.dumps(to_jsonable(value))`. This is the
   stable projection an out-of-process consumer (e.g. a Rust daemon)
   would speak across IPC.
4. **Explicit context.** Every read/report call accepts the same kwarg
   triple: `workspace=`, `runs_dir=`, `cwd=`. Resolution order in
   `sdk/runs.find_runs_dir`:

   1. explicit `runs_dir`
   2. explicit `workspace` → `workspace/runspace/runs`
   3. `$ORCHO_RUNSPACE/runs`
   4. `$ORCHO_WORKSPACE/runspace/runs` (engine resolver)
   5. walk-up from `cwd` (only when `cwd` is not `None`)

   `cwd` defaults to `Path.cwd()` resolved at *call time*, not import
   time. Embedders that don't want walk-up pass `cwd=None` together
   with `workspace=` or `runs_dir=`.
5. **No environment mutation.** SDK never writes `os.environ`. The
   engine resolver consults env via `core.infra.config.AppConfig.load`,
   but explicit kwargs always win.
6. **Side-effecting calls flagged.** Two SDK functions write to disk:
   `refresh_pricing` (writes `~/.orcho/pricing.local.toml`) and
   `write_evidence_bundle` (writes `<out>/<run_id>/evidence.{json,md}`).
   Both are documented as side-effecting in `docs/reference/sdk_api.md`.
   Tests **must** monkeypatch the underlying paths so CI never writes
   the developer's real `~/.orcho/`.

### Positioning

The conceptual rule this ADR locks in:

> **orcho-core SDK is the protocol API. CLI renders it. MCP adapts it.
> Web visualises it. No consumer invents its own run-state model.**

This applies to every embedder — including third-party plugins and
out-of-process consumers — that needs to read or launch orcho runs.
The CLI is no longer the canonical place that knows how to walk
`runspace/runs/`; that knowledge lives in `sdk.runs.find_runs_dir` and
nowhere else.

## Consequences

- `cli/orcho.py` collapses from 1371 LoC to ~400 LoC in the matching
  CLI-decomposition follow-up: argparse + thin handlers that call SDK and
  route through a shared `_run_cli(call, formatter)` adapter.
- `orcho-mcp` migrates its read tools (`orcho_history`, `orcho_status`,
  `orcho_metrics`, `orcho_evidence`) to SDK calls as the first step of
  the MCP-migration milestone. That migration is deferred from this
  ADR's implementation PRs but is **not** deferred past that milestone.
- The Runtime Protocol redesign (`IArchitectAgent` etc.) remains
  deferred per ADR 0009 — independent of this boundary.
- Future architectural moves (out-of-process daemon, alternate runner
  implementations, separate `orcho-sdk` distribution) can build on the
  contract without re-litigating its shape.

## Alternatives considered

- **Re-export-only facade (`core/sdk.py`).** Cheaper to ship, but doesn't
  decompose the god-object CLI: `cmd_cost`'s aggregation stays buried in
  argparse handlers, no embedder gains a typed `CostReport`.
- **Separate `orcho-sdk` distribution.** Physical isolation makes the
  boundary harder, but adds a release-process burden ahead of any
  consumer that demands it. The package boundary inside `orcho-core` is
  reversible later.
- **Reuse `core.observability` and `pipeline.*` directly.** That's the
  status quo; it works for MCP today but leaks the unmarked surface
  problem the moment any third consumer needs the same logic.

## References

- `sdk/` package (this ADR's implementation)
- `docs/reference/sdk_api.md` — API reference for embedders
- ADR 0020 — Baseline run evidence in core (the open question this ADR
  answers in part)
- ADR 0009 — Composable prompt parts (deferred; orthogonal)

## Postscript — MCP migration implementation status (2026-05-10)

The "MCP migrates read tools" plan above landed in `orcho-mcp` across
four steps:

| Step | What landed |
|---|---|
| 1 (orcho-mcp `7503947`) | Read tools migrated to `from sdk import …`. Resources + supervisor argv path also moved over. Negative-import structural gate in `tests/mcp/test_no_direct_run_state.py` (25 cases) enforces no MCP-side reintroduction of run-state parsing. |
| 2a | Hard-cutover rename → `orcho_run_<verb>` namespace. No alias layer (no production users). |
| 2b/c/d | Resume / cancel / qa-decide hardening. |
| 3 | `orcho_run_evidence` typed inspection slices. |

**Tool name update.** The "MCP migrates `orcho_history` / `orcho_status`
/ `orcho_metrics` / `orcho_evidence`" line above predates the step-2a
rename. The current canonical names are `orcho_run_history`,
`orcho_run_status`, `orcho_run_metrics`, `orcho_run_evidence` — see
`orcho-mcp/docs/run_lifecycle.md` for the full surface.

**Companion SDK pre-steps in orcho-core** (no separate orcho-core tags
for this milestone — it is MCP-side; pre-step commits referenced by
hash so cross-repo readers can pin them):

| Commit | Scope |
|---|---|
| `c1dc0f8` | `sdk.RunStatus.sub_projects` includes meta-less aliases. |
| `e7e2fe0` | `sdk.qa_decide` + `sdk.load_qa_decision` + `QaDecision` dataclass + `InvalidQaState` error. |
| `8b1d3e0` | `sdk/evidence_slices.py` — `Finding`, `PlanSummary`, `EvidenceCommandRecord`, `EvidenceArtifactRecord`, `ErrorsAndHalt`, `SubRunLink` dataclasses + the matching slice helpers. |

The contract this ADR locks in stayed verbatim through the whole
migration: SDK is the protocol API; CLI renders it; MCP adapts it; no
consumer invents its own run-state model. The negative-import gate
proves it structurally rather than by convention.
