# ADR 0042: Project-Pipeline Application Boundary

Status: Accepted — all phases (A–J) shipped. See per-phase status table for commit hashes.

> Note: this ADR was originally drafted as 0041 in its planning document.
> ADR 0041 was claimed in the same period by an unrelated SDK-events
> surface decision; the number bumped to 0042 with no content change.

| Phase | Status  | Commit    | Notes |
|-------|---------|-----------|-------|
| A     | Shipped | `5168c3d` | Inventory + ADR (this file). |
| B     | Shipped | `48387e8` | `pipeline/project/types.py` — `ProjectRunRequest` / `ProjectRunResult` / `ProjectRunDeps` placeholder (retired in Phase J). Also `pipeline/project/constants.py` (single source of `DEFAULT_PROFILE_NAME`) and `pipeline/project/__init__.py` (empty). `run_pipeline` body untouched; dataclass surface ready for Phase I integration. Signature lock: `tests/unit/pipeline/test_project_run_request.py` pins `str(inspect.signature(run_pipeline))` to a regeneration-from-pytest reference. |
| C     | Shipped | `6ffe642` | `pipeline/project/bootstrap.py` — run-id resolution + `run.start` event, session init + atexit hook + halted-resume refusal, checkpoint init + phase-log hydration, fresh-dir guard + `RunIdCollisionError`, `PhaseHandoffHaltedError`, `infer_workspace_from_project`. `BootstrapResult` dataclass shipped for Phase I (cleanup gate in J if unused). `_apply_followup_session_seeds` stays out (would trip stop #11; deferred to Phase E). `_resolve_resume_latest` stays out (CLI-specific; deferred to Phase H). |
| D     | Shipped | `65dc811` | `pipeline/project/handoff.py` — pause / resume / retry + `process_pending_phase_handoffs` prompt-loop contract returning `PhaseHandoffLoopResult` (paused / continue_dispatch / halted discriminated union). Also `critique_is_empty`, loop strip/find helpers, critique rehydration, `load_handoff_decision_validated` (thin wrapper over `pipeline.control.load_handoff_decision`). The orchestrator's ~170-line inline while-loop replaced with a wrapper call + 4-line branch. AST guard at `tests/unit/pipeline/test_handoff_isolation.py` pins the layering invariant (no imports from app or orchestrator — runtime or `TYPE_CHECKING`; stop condition #10 has no exception). |
| E     | Shipped | `f21363b` | `pipeline/project/profile_dispatch.py` — `dispatch_via_v2_profile` (Phase 5c step 2 entry), `apply_runtime_max_rounds`, phase banners + outcome surfacing (`emit_phase_banner` / `emit_phase_log_end` / `resolve_round_n` / `followup_banner_suffix` / `print_handoff_outcome`), hypothesis prelude (`run_hypothesis_block`, `plan_hypothesis_step`, `hypothesis_attempts_for_step`, `hypothesis_format_for_step`), profile-shape helpers (`resolve_phase_models`, `resolve_mode_gates`, `profile_contains_phase`, `first_phase_step`), follow-up session seeding (`apply_followup_session_seeds`, `_FOLLOWUP_ROLE_TO_AGENT_ATTR` — punted from Phase C per pre-flight). `_PipelineRun` parameter typed `Any` (Phase F tightens). Cross-project hypothesis-helper imports + dispatch-related test patches (`pipeline.project.profile_dispatch.maybe_run_hypothesis` × 14; `config.phase_model` × 3; `_dispatch_via_v2_profile` × 4; `save_session` mid-loop patch) migrated in the same commit. |
| F     | Shipped | `1c7cb21` | `pipeline/project/run.py` — `_PipelineRun` execution-state dataclass + its three internal helpers (`_run_one_phase`, `_is_mock_provider`, and the DONE-summary trio `_render_done_summary` / `_done_phase_outcome` / `_profile_phase_names_in_order` that `finalize()` consumes). Methods unchanged; `finalize()` retains its full body (Phase G splits it). Class re-imported under the legacy private alias on the orchestrator so `run_pipeline`'s `_PipelineRun(...)` instantiation site stays byte-identical. Same-commit test migrations: `tests/unit/pipeline/quality_gates/test_handlers.py`, `tests/unit/pipeline/orchestrator/test_review_findings.py`, `tests/unit/pipeline/orchestrator/test_done_summary.py`, and `tests/unit/core/test_progress_log.py` (the `_emit_phase_log_end` smoke is now wired to `pipeline.project.profile_dispatch.emit_phase_log_end` directly). Delivery preservation: `_effective_diff_cwd` / `_commit_delivery_baseline` / `_run_commit_delivery` methods and the diff-before-delivery ordering inside `finalize()` carried over byte-identical (per ADR's "Concurrent work" section). |
| G     | Shipped | `1922ad2` | `pipeline/project/finalization.py` — `FinalizationContext`, `FinalizationResult` (field list derived from inspection of legacy `finalize()`: `session_path` / `metrics_path` / `diff_path` / `evidence_path` are the actual writer return values; `mirrored_artifacts: list[Path]` matches `mirror_to_projects` return type; wrapper hints `context_summary_text` / `has_api_equivalent_cost` / `is_subpipeline` / `mirror_error` / `worktree_teardown_message` are derived once during the silent pass), `finalize_project_run` (silent — writes session/metrics/diff/evidence, mutates session, emits `run.end` + `phase.start`/`phase.end`, sets checkpoint, mirrors artifacts, tears down worktree, but produces no stdout/stderr) + `finalize_with_terminal_output` (CLI wrapper — prints DONE banner + success chips + Session/Usage/Progress lines + mirror notice + worktree-teardown line, derived from `FinalizationResult`). DONE-summary trio (`_render_done_summary`, `_done_phase_outcome`, `_profile_phase_names_in_order`) lifted from `pipeline.project.run`. `_PipelineRun.finalize()` becomes a 7-line delegator. **Ordering invariants preserved**: `capture_run_diff` BEFORE `_run_commit_delivery`; `run.end` + checkpoint final status read post-delivery `session["status"]`. **DONE event de-dup**: the wrapper prints the colored header directly via `render_phase_header` (not `banner()`) because the silent service already emitted `phase.start("DONE")` via `log_phase` — calling `banner()` in the wrapper would double-emit and drift the event-order snapshot. New test `tests/unit/pipeline/test_finalization_silent.py` (4 cases) pins zero-terminal-output invariant across done / halted / awaiting_human_review paths. Same-commit migrations: `test_done_summary.py` repointed to `pipeline.project.finalization`. |
| I     | Shipped | `bfad101` | `pipeline/project/app.py` — `run_project_pipeline(request)` facade + `run_pipeline` back-compat wrapper (signature byte-for-byte preserved; locked by `test_project_run_request.py::TestSignatureLock`; the Phase I `deps` placeholder was retired in Phase J). All `run_pipeline`-supporting helpers (`_resolve_session_mode`, `_codemap_injectable`, `_validate_plan_file_paths`, `_make_state`, `_reviewer_provider_label`, `_read_chain_same_model_only`, `_print_pipeline_header`, `_synthesize_phase_config`, `_resolve_profile_name`, `_resolve_v2_profile`, `_profile_phase_names`, `_resolve_cross_handoff`, `_resolve_change_handoff`, `print_error`, `_VALID_PLAN_SOURCES`, `_HANDOFF_REQUIRED_PHASES`) moved with it because stop condition #3 forbids `pipeline.project.*` modules from reverse-importing `pipeline.project_orchestrator`. Orchestrator re-imports the symbols under their legacy aliases so existing tests + `pipeline.lifecycle._orch._XXX` dynamic access pattern keep resolving. **PEP 563 deliberately NOT enabled** in `app.py` — would silently stringify `inspect.signature(run_pipeline)` annotations and break the byte-for-byte contract (Phase B's pinned reference is the resolved form). Same-commit migrations: `tests/integration/test_checkpoint_pipeline.py` (the `pipeline.project.app.save_session` mid-finalize patch joins the existing `_session_mod` + `_pd` triple), `tests/acceptance/test_worktree_e2e.py::test_plugin_git_dir_creates_worktree_from_nested_root` (now also patches `pipeline.project.app.load_plugin` so the plugin-content-sensitive `git_dir="src"` reaches the worktree resolver), `docs/sdk_schema.json` (one-line drift — `run_pipeline.__module__` now `pipeline.project.app`; SDK consumers import names from `sdk.*`, not `pipeline.*`, so wire is unaffected). Transitional import-cycle smoke (from ADR r5 P2) green. |
| H     | Shipped | `467aded` | Two new modules: `pipeline/project/phase_config.py` (~114 LoC — `build_phase_config_from_overrides` lifted out of the orchestrator into its own module so `pipeline.cross_project` can depend on it without reaching into the CLI leaf) and `pipeline/project/cli.py` (~876 LoC — `main()` body, 33-arg argparse, `_resolve_resume_latest`, `_apply_resume_runs_context`, attachment loader, plus the `if __name__ == "__main__": main()` guard that makes `python -m pipeline.project.cli --help` work as a smoke command). `pipeline/project_orchestrator.py` trimmed from 1206 → 195 LoC: it's now a compatibility entrypoint module — the four stable shim names (`run_pipeline`, `main`, `SessionMode`, `RunIdCollisionError`) sit at the top with `__all__`, followed by the legacy underscore-prefixed re-export block that keeps `pipeline.lifecycle._orch.X` dynamic access + ~30 test imports resolving. Phase J's hygiene gate retires those legacy aliases by walking each consumer and switching them to direct imports. **Leaf-layer invariant verified**: `rg 'from pipeline\.project\.cli|import pipeline\.project\.cli'` returns empty across `pipeline/cross_project/`, `pipeline/runtime/`, `pipeline/control/`, `sdk/`, `cli/`, `core/`, and `tests/` (excluding the permitted `tests/unit/cli/**`). **CLI smokes green**: `orcho-run --help` and `.venv/bin/python -m pipeline.project.cli --help`. Same-commit migrations: `pipeline/cross_project/orchestrator.py` (`build_phase_config_from_overrides` repointed to `pipeline.project.phase_config`), `tests/unit/cli/test_cli_orcho.py` (three `monkeypatch.setattr(po, "run_pipeline", ...)` sites repointed to `pipeline.project.cli` — the `main()` body's binding is the patch target, not the orchestrator's re-export), `tests/unit/cli/test_resume_latest.py` (three `_resolve_resume_latest` imports repointed to `pipeline.project.cli`). |
| J     | Shipped | `06d75a6` | Final cleanup pass. Burned down the 29 legacy underscore-prefixed imports of `pipeline.project_orchestrator` by walking every consumer (`pipeline.lifecycle`, `pipeline.cross_project.orchestrator`, `pipeline.quality_gates`, `sdk/runner.py`, and ~16 test files) and rewiring to canonical homes (`pipeline.project.app`, `pipeline.project.handoff`, `pipeline.project.phase_config`, `pipeline.project.bootstrap`, `core.io.git_helpers`, `core.observability.logging`, `pipeline.project_testing`). The `pipeline.project_orchestrator` shim is now **36 LoC** — the 4 stable names (`run_pipeline`, `main`, `SessionMode`, `RunIdCollisionError`) in `__all__` + the `if __name__ == "__main__": main()` guard. No `__getattr__`, no test-runner wrappers, no logging re-exports. `cross_project/orchestrator.py` keeps a local 3-line `print_error`. `pipeline.quality_gates`' `TestsGate` falls through to `pipeline.project_testing.run_tests` directly. `pipeline.lifecycle` builds the default helpers from `core.io.git_helpers` + `pipeline.project.app` + `pipeline.project.handoff` + `pipeline.project_testing` without any `_orch.X` indirection. **`ProjectRunDeps` retired per ADR r4 P2** — Phase I never consumed the empty seam, so the placeholder dataclass + the `deps` parameter on `run_project_pipeline` are gone; a future ADR re-introduces a typed injection point when it has a concrete contract to ship. **Late-binding fix in `pipeline.project_testing`**: `run_single_test`'s `subprocess_run` / `timeout_expired` defaults + `run_tests`' `run_single_test_fn` default were captured at definition time, defeating `monkeypatch`-style overrides; refactored to `None` defaults with a one-line resolution inside the function body so tests that ``patch("pipeline.project_testing.subprocess.run", ...)`` or ``setattr(project_testing, "run_single_test", ...)`` see the rebinding. **Import-hygiene gate green**: AST walk of `pipeline/`, `tests/`, `sdk/`, `cli/`, `core/` finds zero `from pipeline.project_orchestrator import X` with X outside the allowed 4-name set, zero `import pipeline.project_orchestrator` bare or attribute form. **Attribute-surface check green**: no `_PipelineRun`, `_apply_phase_handoff_pause`, `build_phase_config_from_overrides`, `print_error`, `_dispatch_via_v2_profile`, `_infer_workspace_from_project`, `set_progress_log` left on the shim. **CLI leaf check green**: zero non-CLI / non-`tests/unit/cli/**` imports of `pipeline.project.cli`. **CLI smokes green**: `orcho-run --help` + `python -m pipeline.project.cli --help`. Full test suite (excluding pre-existing pty-exhaustion failures in `tests/unit/agents/test_stream.py` + `tests/unit/pipeline/sandbox/` — environmental, confirmed by reproduction on clean HEAD): 4129 passed. |

## Context

`pipeline/project_orchestrator.py` is a 4560-LoC module that mixes CLI
argparse, run-bootstrap setup, profile resolution, runtime dispatch,
handoff pause/resume, finalization, observability, and terminal
rendering inside one module and one class (`_PipelineRun`, 561 LoC).
Four entry points already drive it as an application service today —
the `orcho-run` CLI (`pipeline.project_orchestrator:main`),
`sdk/runner.py`, `pipeline.cross_project.project_dispatch.run_pipeline`,
and direct test imports — but the surface is untyped (a very wide
positional kwarg list on `run_pipeline()`), tightly coupled to CLI
assumptions (interactive prompts, terminal rendering, atexit hooks),
and impossible to unit-test in pieces.

ADR 0040 (Phases A–F shipped) covered the shared control primitives
(`handoff_decisions`, `reviewed_loop`) and the cross-project monolith
split. The single-project orchestrator was not in that scope. This
ADR captures the parallel decomposition for the single-project side
and the rules that keep the two efforts coherent.

## Scope discipline — what this pass does and does NOT do

This pass creates a **typed orchestration boundary**
(`ProjectRunRequest` → `run_project_pipeline` → `ProjectRunResult`)
and decomposes the monolith into a `pipeline/project/` package with
clear responsibility boundaries. It is **not yet the final UI
boundary**. After this pass:

- `run_project_pipeline` is the **typed orchestration boundary with
  legacy terminal behaviour preserved** — it still emits banners and
  the CLI-equivalent terminal output via `finalize_with_terminal_output`
  during the existing CLI path. It is a compat/parity boundary for
  this pass, not a fully headless surface.
- A **silent** lower-level service exists
  (`finalize_project_run(ctx) -> FinalizationResult`) — "silent" meaning
  it writes files, mutates session, emits events, closes the
  checkpoint, **but produces no terminal output**. UI clients compose
  against this layer themselves until a later phase elevates a truly
  silent app-level boundary.
- The interactive in-process handoff prompt has an explicit contract
  (`process_pending_phase_handoffs(...) -> PhaseHandoffLoopResult`)
  inside `pipeline.project.handoff` — not in CLI.
- A truly silent `run_project_pipeline` mode (or presentation-policy
  injection) is **deferred to a later ADR/phase** once the lifecycle
  event surface stabilises. This ADR does not promise headless
  top-level orchestration; that's the next step on the same roadmap.

## Decision — module layout

```
pipeline/
├── control/                          # SHARED primitives (ADR 0040). Unchanged.
│   ├── handoff_decisions.py
│   ├── reviewed_loop.py
│   ├── resume_context.py
│   ├── operator_decisions.py
│   ├── handoff_prompt.py
│   ├── resume_prompt.py
│   └── from_run_plan.py
│
├── project/                          # NEW. Project-local application boundary.
│   ├── __init__.py                   # Empty in Phase B. Optional re-exports after Phase I only if cycle-free.
│   ├── constants.py                  # DEFAULT_PROFILE_NAME etc. if no clean source. (Phase B.)
│   ├── types.py                      # ProjectRunRequest, ProjectRunResult. (Phase B; ProjectRunDeps retired in Phase J.)
│   ├── bootstrap.py                  # Run-id + session + checkpoint + workspace-infer setup. (Phase C.)
│   ├── handoff.py                    # Pause/resume + loop strip + critique rehydration + in-process prompt loop. (Phase D.)
│   ├── profile_dispatch.py           # Runtime dispatch helpers + hypothesis block. (Phase E.)
│   ├── run.py                        # _PipelineRun execution-state class. (Phase F.)
│   ├── finalization.py               # finalize_project_run (silent) + finalize_with_terminal_output. (Phase G.)
│   ├── app.py                        # run_project_pipeline(request) + run_pipeline wrapper. (Phase I — BEFORE H.)
│   ├── phase_config.py               # build_phase_config_from_overrides. NOT in cli.py; cross imports from here. (Phase H.)
│   └── cli.py                        # main() + argparse + workspace banner + resume-mode chooser. LEAF. (Phase H.)
│
├── project_testing.py                # Existing flat module. _resolve_tests_config / run_tests stay here.
├── cross_project/                    # Existing. ADR 0040 Phases A–F complete.
└── project_orchestrator.py           # THIN compat shim after Phase J. <50 LoC.
                                      # Stable re-exports only: run_pipeline, main, SessionMode, RunIdCollisionError.
```

### Dependency direction

```
cli.py  ─┐
         │ depends on
         ▼
app.py ─→ types.py
  │   ─→ bootstrap.py ─→ control/resume_context.py
  │   ─→ handoff.py   ─→ control/handoff_decisions.py, control/handoff_prompt.py
  │   ─→ profile_dispatch.py ─→ runtime/
  │   ─→ run.py
  │   ─→ finalization.py
  │   ─→ phase_config.py

cross_project/ ─→ types.py, bootstrap.py, profile_dispatch.py, phase_config.py
              (NEVER ─→ cli.py)

sdk/runner.py ─→ project_orchestrator.py (shim) ─→ app.py
```

**Layering rule (load-bearing).** `pipeline.project.cli` is a **leaf**.
Cross-project, runtime, sdk, and non-CLI tests must never import from
it. CLI-targeted tests (`tests/unit/cli/**`) ARE allowed to import
`pipeline.project.cli` — they are the consumer the leaf exists for.
Anything that cross / runtime / sdk / non-CLI tests need as a core
utility lives in a non-CLI module (`bootstrap.py`, `phase_config.py`,
`handoff.py`, …). The CLI calls into those modules, not the other way
around.

## Decision — per-layer classification

Each section of `project_orchestrator.py` falls into exactly one
category:

| Layer | Current location | Classification | Destination |
|---|---|---|---|
| Run bootstrap (run-id, session, checkpoint, workspace) | lines 1248–1772 | project-local extraction | `pipeline.project.bootstrap` (or split `workspace.py` if CLI-coupled) |
| Phase-handoff pause / resume / retry + in-process prompt loop | lines 2908–3595 + dispatch interleave | project-local extraction | `pipeline.project.handoff` |
| Runtime dispatch (`_dispatch_via_v2_profile` + helpers + hypothesis block) | lines 2513–2906 + 3597–3819 | project-local extraction | `pipeline.project.profile_dispatch` |
| `_PipelineRun` execution-state class | lines 648–1208 | project-local extraction | `pipeline.project.run` |
| **Commit-delivery wiring on `_PipelineRun`** (added by delivery work, see "Concurrent work" below) — `_effective_diff_cwd` / `_commit_delivery_baseline` / `_run_commit_delivery` | lines 1009–1064 inside `_PipelineRun` | project-local extraction, moves with class in Phase F | `pipeline.project.run` |
| Finalization (status / metrics / diff / evidence / mirror / checkpoint close) | inside `_PipelineRun.finalize()` (168 LoC, lines 1039–1207) | project-local extraction — split into silent service + terminal wrapper | `pipeline.project.finalization` |
| CLI argparse + `main()` body + resume-mode chooser + attachment loader | lines 3821–4560 | project-local extraction — **leaf** | `pipeline.project.cli` |
| `build_phase_config_from_overrides` (per-phase agent/runtime config builder) | lines 419–509 | project-local extraction — separate module, NOT inside CLI | `pipeline.project.phase_config` |
| `run_pipeline` body | lines 1819–2306 | project-local extraction — typed boundary | `pipeline.project.app` |
| Stable surface (`run_pipeline`, `main`, `SessionMode`, `RunIdCollisionError`) | top-level + various | must stay in shim | `pipeline.project_orchestrator` (4 re-exports only) |
| `_resolve_tests_config` / `run_tests` | tests bridge | stays in existing module | `pipeline.project_testing` |
| Shared control primitive (handoff decisions, reviewed loop, resume context, …) | not single-project-owned | shared per ADR 0040 | `pipeline.control.*` (no new entries expected) |
| BaseOrchestrator / universal runner / IOrchestratorPhase / broad ports object / 40-field ProjectContext | n/a | **forbidden abstraction** | (does not exist) |

## Decision — forbidden shapes

Carried forward from ADR 0040 plus new entries for this pass:

1. **No `BaseOrchestrator` class.** Single uses the runtime FSM; cross
   doesn't. A common ancestor forces one to subclass the other and
   infects both directions.
2. **No "universal runner".** `_run_loop_step` already is the universal
   loop runner for single-project. Coupling it to cross-level handlers
   was rejected in ADR 0040 and remains rejected here.
3. **No generic `IOrchestratorPhase` Strategy.**
4. **No broad-ports object** (`DisplayPorts`, `TerminalPorts`,
   `EventPorts`, …) parameter threaded through a module.
5. **No 40-field `ProjectContext`.** `ProjectRunRequest` is the
   deliberate DTO exception — see Phase B notes. No other dataclass
   may approach that size.
6. **No compat wrappers for internal reshuffles.** Internal symbols
   move + consumers update in the same commit (Phase 5 pattern;
   CLAUDE.md "no backcompat ceremony"). Only the 4 stable-surface
   names live on the shim.
7. **No frontend-specific branching inside core.** No
   `if running_under_cli:` / `if running_under_mcp:` switches.
8. **(New) No non-CLI imports from `pipeline.project.cli`.** CLI is a
   leaf. CLI-targeted tests under `tests/unit/cli/**` ARE allowed;
   everything else is forbidden.
9. **(New) No imports from `pipeline.project.handoff` into
   `pipeline.project.app` or `pipeline.project_orchestrator`.** The
   handoff service is composed by the app service, not the other way
   around. Verified by an AST check (Phase D).
10. **(New) No agents / profile / runtime deps in `bootstrap.py`.**
    Bootstrap stays narrow (run-setup). If it grows runtime-shape deps,
    extract `session_setup.py` instead (Phase C stop condition).

## Decision — back-compat shim

The shim `pipeline/project_orchestrator.py` re-exports **exactly four
symbols** after Phase J:

| Symbol | Source after Phase J | Reason |
|---|---|---|
| `run_pipeline` | `pipeline.project.app` | Used by `sdk/runner.py`, integration / acceptance tests, `pipeline.cross_project.project_dispatch`. Phase I keeps the exact current positional signature byte-for-byte. |
| `main` | `pipeline.project.cli` | `orcho-run` entrypoint per `pyproject.toml`. |
| `SessionMode` | `agents.protocols` | Re-export; used by `pipeline.cross_project.project_dispatch`. |
| `RunIdCollisionError` | `pipeline.project.bootstrap` | Public exception; imported by tests + cross-project orchestrator. |

Everything else is internal. Each phase's migration list moves the
relevant symbol and updates every consumer (cross-project, tests) in
the **same commit**. The exact inventory at each phase is regenerated
via `rg 'project_orchestrator'` before the extraction begins — the
plan document's tables are a starting set, not a closed inventory.

## Scope explicitly NOT in this ADR (deferred)

| Item | Deferred to |
|---|---|
| Truly silent `run_project_pipeline` mode (no terminal output from the app facade) | Future ADR once lifecycle event surface stabilises |
| Interactive handoff prompt → CLI adapter | Future phase; defeats the "no in-process prompt in CLI" rule today |
| Single-project plan/validate loop → `reviewed_loop` | ADR 0040 Phase deferred indefinitely; only revisit if a second concrete caller appears |
| Merge of single- and cross-project terminal-finalize | ADR 0040 records why; shape divergent, not just name |
| `sdk/runner.py` migration from `run_pipeline` to `run_project_pipeline(request)` | Optional Phase J cleanup; not load-bearing |
| Renaming `pipeline/project_testing.py` → `pipeline/project/testing.py` | Optional Phase J cleanup |
| Persisted-contract changes (`meta.json` / `evidence.json` / `metrics.json` / `diff.patch` / `phase_handoff_decisions/*.json`) | Out of scope; byte-shape preserved across all phases |
| CLI behavior changes (argparse / prompts / banners / error text) | Out of scope; preserved verbatim |

## Stop conditions

If any of these occur mid-phase, leave the partial work uncommitted,
write up the blocker, and ask for direction before proceeding:

1. **Broad ports object pressure.** A new module needs a
   `DisplayPorts` / `TerminalPorts` / `EventPorts` parameter to compose
   with another module.
2. **Dataclass size creep.** A dataclass other than
   `ProjectRunRequest` grows past ~15 fields. `ProjectRunRequest` is
   the deliberate DTO exception (mirrors the public `run_pipeline`
   signature 1:1).
3. **Reverse import.** Extracting code requires importing back from
   `project_orchestrator.py` into a `pipeline.project.*` module.
4. **Behavior change pressure.** Refactor must be behavior-preserving
   across each phase. Defer "while we're here" tweaks.
5. **Cross-project depends on project-local in a cycle.**
6. **CLI-only orchestration.** CLI becomes the only way to reach
   orchestration semantics.
7. **Interactive handoff prompt → CLI.** Trying to extract the
   in-process handoff prompt loop into `cli.py`.
8. **`run_pipeline` signature drift.** Phase I touches the explicit
   signature in any way other than verbatim preservation.
9. **Non-CLI code imports from `pipeline.project.cli`.** CLI tests
   under `tests/unit/cli/**` ARE allowed; everything else is forbidden.
10. **`pipeline.project.handoff` imports from `pipeline.project.app`
    or `pipeline.project_orchestrator`.**
11. **`bootstrap.py` accepts agents/profile/runtime deps.** Extract
    `session_setup.py` instead.

## Acceptance criteria for the ADR itself (Phase A)

- Docs-only commit. No runtime changes.
- `ruff check .` clean.
- `git diff --check` clean.
- Public text hygiene check clean. The canonical blocked-token list lives in
  the hygiene test; this ADR does not enumerate the tokens. The scan
  is enforced in CI by
  `tests/unit/core/test_open_core_hygiene.py::test_no_banned_tokens_in_public_packages`.
- Terminology: refer to non-CLI consumers as "frontends", "embedders",
  or "UI clients" — not by any specific product name.

## Concurrent work — delivery wiring (preserve through Phases F + G)

Parallel to this refactor's Phases A–C, a separate delivery effort
landed Phase A of commit-delivery work in commits `2eb0564` (`feat:
deliver run diffs after release`) and `1cef754` (`docs: set apply as
interactive delivery default`). That work introduced load-bearing
code inside `_PipelineRun` and inside `finalize()` that Phases F + G
of this ADR must preserve verbatim. The pieces:

### `_PipelineRun` methods (Phase F target)

* `_effective_diff_cwd(self) -> Path` (orchestrator line ~1009) —
  returns the worktree path when running isolated, otherwise the
  project path. Used by both diff capture and delivery so they agree
  on which checkout to read.
* `_commit_delivery_baseline(self) -> str` (line ~1014) — reads
  `session["pre_run_dirty"]["seed_tree_sha"]` when present,
  otherwise the worktree's `base_ref`, otherwise `"HEAD"`. This
  prevents run N+1 from re-delivering the diff that was seeded from
  run N's pre-run dirty intake.
* `_run_commit_delivery(self, diff_cwd: Path) -> None` (line ~1026)
  — single-project Phase A delivery executor. Calls
  `pipeline.engine.commit_delivery.resolve_commit_delivery` and
  conditionally `apply_commit_delivery`. Guards on
  `parent_run_id` / `project_alias` to skip sub-pipelines (cross
  per-alias delivery is the separate delivery effort's Phase B).
  Mutates `session["status"]` to `"halted"` with `halt_reason ∈
  {"commit_decision_halt", "commit_delivery_failed"}` on the halt
  branches.

Phase F moves all three to `pipeline/project/run.py` along with the
rest of `_PipelineRun`. No special handling required — they're
methods on the class.

### `_PipelineRun.finalize()` ordering (Phase G target)

The current `finalize()` body at lines ~1125–1128 captures the diff
before running delivery:

```python
_effective_diff_cwd = self._effective_diff_cwd()
capture_run_diff(_effective_diff_cwd, self.output_dir)   # diff.patch BEFORE
self._run_commit_delivery(_effective_diff_cwd)           # delivery AFTER
```

**This ordering is load-bearing.** `diff.patch` must remain the
recovery artifact even after `apply` mutates the project checkout —
swapping the calls would leave the run dir without a diff once
delivery succeeds. Phase G's silent `finalize_project_run` MUST
preserve this exact order.

The current `finalize()` also reads `session["status"]` (not
`state.halt`) when writing the `run.end` event and the checkpoint
status, because delivery can turn `done` into `halted` after
`_run_commit_delivery` runs. Phase G's status-determination logic
must consult `session["status"]` for the same reason.

### Phase G `FinalizationResult` field-list pre-flight

The plan said `FinalizationResult`'s artifact fields are derived
during Phase G pre-flight from what `finalize()` actually produces.
That pre-flight must include the delivery side-effects:

* `session["commit_delivery"]` (a dict from `decision.to_dict()`) is
  set by `_run_commit_delivery` when delivery ran. Decide during
  Phase G whether the field surfaces on `FinalizationResult` or
  stays session-only (UI clients can read `result.session
  ["commit_delivery"]` either way).
* `halt_reason ∈ {"commit_decision_halt",
  "commit_delivery_failed"}` joins the existing halt-reason set.
  No taxonomy change — just an additional reason value.

### Cross-project

The separate delivery effort's Phase A is single-project only.
Cross-project per-alias delivery is its Phase B and not landed.
Nothing in ADR 0042's Phases D–J should wire delivery into
`pipeline.cross_project.*`.

### Related files (out of scope for ADR 0042 but referenced)

* `pipeline/engine/commit_delivery.py` — new Phase A executor
  (approve/apply/skip/halt).
* `pipeline/engine/pre_run_dirty.py` — pre-run include now records
  `seed_tree_sha`.
* `core/_config/config.defaults.json` and `core/infra/config.py` —
  `commit.interactive_default = apply`, `commit.auto_in_ci =
  approve`.
* `docs/user/04_results.md` — user-facing semantics for
  apply/approve/skip/halt.

Treat these as external dependencies — read them, don't touch them
from this ADR's commits.

## Size review — extracted modules

Phase J's cleanup gate flags any extracted module over 1000 LoC
without a follow-up split plan. The current size of each module is
tracked below as it ships. A module above the threshold must be
called out here with a justification or a split target — the gate
trips silently otherwise.

| Module | LoC (current) | Threshold | Note |
|---|---:|---:|---|
| `pipeline/project/bootstrap.py` | 498 | 1000 | Below threshold. |
| `pipeline/project/handoff.py`   | 1050 | 1000 | **Above threshold by ~5%.** Single cohesive concern: phase-handoff pause / resume / retry / drain. The four moved functions (`apply_phase_handoff_pause`, `apply_phase_handoff_resume`, `apply_review_repair_handoff_retry`, `process_pending_phase_handoffs`) share their loop-strip / critique-rehydration / decision-loading helpers; the helpers do not split cleanly along an axis that produces two modules with distinct callers. **No split planned for Phase J.** If a future change adds materially to this surface (new handoff verb, new resume flavour), revisit then. |
| `pipeline/project/profile_dispatch.py` | 787 | 1000 | Below threshold. Three cohesive groups (profile-shape inspection, banner/log-end rendering, dispatch + hypothesis prelude) live together because the call sites bridge them on the same code path. |
| `pipeline/project/run.py`              | 581 | 1000 | Below threshold. Down from 788 LoC after Phase G's `finalize()` split (the body migrated to `pipeline.project.finalization`); the dataclass body + `_record_phase_failure` + `_safe_phase` + per-phase methods + delivery helpers + the new 7-line `finalize()` delegator remain. |
| `pipeline/project/finalization.py`     | 467 | 1000 | Below threshold. Silent service + terminal wrapper + the DONE-summary trio (`_render_done_summary` / `_done_phase_outcome` / `_profile_phase_names_in_order`). |
| `pipeline/project/app.py`              | 1202 | 1000 | **Above threshold by ~20%.** `_run_project_pipeline_session` body itself is ~545 LoC of run-setup ceremony (resume meta hydration, profile projection, ContextVar registration, sandbox + worktree wiring, follow-up seed merging, dispatch + finalize). The 13 supporting helpers (`_resolve_profile_name` / `_resolve_v2_profile` / `_synthesize_phase_config` / `_print_pipeline_header` / etc.) sit alongside because they have no other consumers and moving them to a side module would create the reverse-import that stop #3 forbids. Phase I's inversion (`a3acd42`) added the `run_pipeline` thin wrapper + the request-typed boundary on top of the legacy body. **No split planned for Phase J.** A future refactor may extract the `--from-run-plan` projection block + the resume-meta hydration block into focused helpers, but those are inside the body and split the function rather than the module. |
| `pipeline/project/cli.py`              | 876  | 1000 | Below threshold. `main()` is the bulk (33-arg argparse + arg-to-kwarg processing + resume mode hydration + dispatch into `run_pipeline`); plus the two resume helpers (`_resolve_resume_latest`, `_apply_resume_runs_context`) that fire from `main()`. Phase J may consolidate the argparse builder into a focused helper, but the size is acceptable for a CLI leaf. |
| `pipeline/project/phase_config.py`     | 114  | 1000 | Below threshold. Single function (`build_phase_config_from_overrides`) + docstring. Lives in its own module specifically so `pipeline.cross_project` can depend on it without reaching into `pipeline.project.cli` (the leaf). |
| `pipeline/project_orchestrator.py`     | 36   | 50   | **Under target.** Phase J retired the legacy underscore-prefixed re-export block + the test-runner wrappers + `__getattr__`. Module now holds the four stable shim names (`run_pipeline`, `main`, `SessionMode`, `RunIdCollisionError`) re-exported from canonical homes, `__all__` listing them, and the `if __name__ == "__main__": main()` guard. |

The size column is refreshed as each phase lands. Phase J's gate
reads from this table and either confirms each row stays
sub-threshold or that an above-threshold row has an explicit
justification (as above) or a split target.

## Status table maintenance

Each subsequent phase commit updates exactly the row of the status
table at the top of this file (Status: `Shipped`, Commit: `<short
hash>`). Phase J marks the ADR `Accepted` and appends the final LoC
table for the new modules + the shim.
