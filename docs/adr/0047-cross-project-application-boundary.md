# ADR 0047: Typed Cross-Project Application Boundary

Status: Accepted (Phases A–I shipped; commit hashes captured per row)

## Accepted summary

ADR 0047 split the cross-project orchestrator behind a typed
application boundary that mirrors ADR 0042 one-for-one and inherits
the silent-presentation contract from ADR 0046:

```
CrossRunRequest → run_cross_project_pipeline → CrossRunResult
                  ├─ presentation=TERMINAL → legacy cross transcript
                  └─ presentation=SILENT   → zero stdout/stderr;
                                              meta.json + events.jsonl
                                              + progress.log + mirror
                                              byte-identical to TERMINAL
                  ↓ per-alias dispatch
                  ProjectRunRequest(presentation=SILENT,
                                    no_interactive=True)
                  → run_project_pipeline
```

Pre-Phase-A `pipeline/cross_project/orchestrator.py` was 2321 LoC
(run body + render helpers + CLI + back-compat wrapper). At
Accepted it is 333 LoC of back-compat surface only: `parse_projects`,
`build_cross_context`, four prompt-builder wrappers, the 23-kwarg
`run_cross_pipeline` shim that routes through the typed boundary,
plus lazy re-exports of `main` / `print_error` for the legacy
test-patch surface. The body lives in `app.py`, render helpers in
`rendering.py`, the CLI leaf in `cli.py`, status decision + tail
finalization in `finalization.py`.

The Phase H end-to-end SILENT boundary tests are the
"is-it-actually-silent" check that proved the typed-boundary work
was load-bearing rather than decorative: on first execution they
exposed a real ADR 0046 stop #9 violation —
`pipeline/cross_project/app.py` called `setup_run_logging(output_dir,
session_ts, is_resume=...)` without passing `terminal=terminal`, so
under SILENT the two grey `📄 Live output` / `📡 Events` courtesy
chips leaked to stdout. `setup_run_logging` itself already had the
`terminal: bool` parameter (gated on the project side since ADR 0046
Phase C site 18) but the cross body did not thread it. Fix: thread
`terminal=terminal` into the cross call, in the same Phase H
commit (`e204454`). Without that boundary test the leak would have
shipped at Accepted. Phase E's unit-level threading tests caught
the obvious sites; the e2e test caught the inventory miss that
wasn't on the Phase E checklist. The lesson:
typed-boundary + presentation-policy is a contract checked at the
edge, not just at the wiring; the boundary tests are the only thing
that catches every "I forgot to forward the kwarg" leak.

Companion fixes that landed inside the per-phase commits:

  * Phase G r0 `2b58287` extracted the CLI leaf but reintroduced an
    eager `from pipeline.cross_project.cli import main, print_error`
    re-export at the bottom of `orchestrator.py`, so
    `import pipeline.cross_project` (which loads orchestrator
    transitively) eagerly pulled argparse / `sys.exit` into every
    consumer + emitted a `RuntimeWarning` when `python -m
    pipeline.cross_project.cli` also touched the module.
  * Phase G r1 `01880a1` replaced the eager re-export with a PEP 562
    module-level `__getattr__` so `cli` loads only on first attribute
    access. New subprocess-isolated guard
    `test_package_root_import_does_not_load_cli_module` pins the
    invariant: `import pipeline.cross_project` does NOT load `cli`;
    `from pipeline.cross_project import orchestrator` does NOT load
    `cli`; only `orchestrator.main` (the attribute access) triggers
    the lazy import. Identity invariant `orchestrator.main is
    cli.main` still holds; `monkeypatch.setattr(orchestrator, "main",
    fake)` still wins because patches write to `__dict__`, which
    beats `__getattr__` on subsequent reads.

The full phase-by-phase narrative + commit hashes live in the
status table directly below. Every row carries enough detail that
the next reader does not need to re-derive intent from the diff.

| Phase | Status  | Commit    | Notes |
|-------|---------|-----------|-------|
| A     | Shipped | `d5b1240` | ADR doc (this file). Pins the literal `str(inspect.signature(run_cross_pipeline))` — **23 params** verified against `pipeline.cross_project.orchestrator`. Sections: Context, Decisions D1–D9, Architecture target, Inventory, Phase plan, Stop conditions, Verification matrix. Status table B–I = Planned. |
| B     | Shipped | `8743890` | Extracted render helpers (`C`, `banner` with new `terminal=True` keyword-only split, `success`, `warn`, `preview`, `_render_cross_plan_preview`) from `pipeline/cross_project/orchestrator.py` to new peer module `pipeline/cross_project/rendering.py`. `orchestrator.py` keeps a thin re-import block (`from pipeline.cross_project.rendering import C, _render_cross_plan_preview, banner, preview, success, warn`) so its own run body (still resident until Phase D) keeps working byte-identical, and dropped the now-unused `render_cross_plan_block` import from `core.io.transcript`. Re-targeted render imports in `planning_loop.py`: 4 pure-render blocks at lines 284, 305, 331, 373 retargeted wholesale to `rendering`; 2 mixed blocks at lines 504 and 865 split (render helpers from `rendering`, usage + prompt helpers stay at `orchestrator` per D3's deferred-to-Phase-D rule); 1 mixed block at line 688 turned out to be pure-render after re-reading and retargeted wholesale; line 931 (pure usage) untouched. `handoff_payloads.py:197` re-checked — its `warn` import comes from `core.observability.logging`, not from `pipeline.cross_project.orchestrator`, so that site was a phantom in the pre-Phase-B inventory and required no migration (Phase E will still gate the call site itself under SILENT). Migrated 3 patch targets in `tests/unit/pipeline/cross_project/test_cross_orchestrator.py` (the `_render_cross_plan_preview` tests at lines 1758/1793/1826) — they now patch `render_cross_plan_block` + `preview` on `pipeline.cross_project.rendering` instead of `orchestrator`, because the resolution namespace moved. **Phase B invariant verified empty** via `grep -rn 'from pipeline.cross_project.orchestrator import' pipeline/cross_project/ | grep -E '\b(C\|banner\|success\|warn\|preview\|_render_cross_plan_preview)\b'` — no non-CLI cross peer still imports render helpers from `orchestrator`. New `tests/unit/pipeline/cross_project/test_cross_rendering.py` (9 tests) locks: `banner(terminal=True)` prints + calls `log_phase`; `banner(terminal=False)` suppresses print but `log_phase` STILL fires (ADR 0046 stop #9 / Phase C r5 invariant applied prospectively); `banner` threads `phase_kind`/`attempt` into `log_phase`; `success`/`warn`/`preview` are pure-stdout (no `log_phase` call); `preview` default is no truncation; `preview(n=N)` adds `…` trailer; canonical re-export surface importable; orchestrator-to-rendering identity holds during the transition. Verification: full suite (4184 unit + integration + acceptance, 2 skipped) green; `ruff check .` clean. |
| C     | Shipped | `c5d02a9` | Promoted `PresentationPolicy(StrEnum) {TERMINAL, SILENT}` from `pipeline.project.types` to neutral `pipeline/presentation.py`; `pipeline.project.types` re-exports it as a single-line import (`from pipeline.presentation import PresentationPolicy`). Identity invariant verified by `TestPresentationPolicyPromotionIdentity` — `pipeline.project.types.PresentationPolicy is pipeline.presentation.PresentationPolicy` AND `pipeline.project.PresentationPolicy` (the package-level re-export) is the same object too. ADR 0046 sites (project app, run, bootstrap, handoff, profile_dispatch, cross project_dispatch + tests) all keep resolving the SAME enum via their existing imports — full ADR 0046 `TestPresentationPolicy` suite (22 tests) re-runs green. Added `pipeline/cross_project/app_types.py` (new file — `types.py` was too dense with M13 Phase 1 domain types; Phase C plan's escape hatch) with `CrossRunRequest` + `CrossRunResult`. CrossRunRequest is frozen + slotted, mirrors `run_cross_pipeline`'s 23 params 1:1, plus the request-only `presentation` field defaulting to `PresentationPolicy.TERMINAL`. `__post_init__` mirrors project semantics: string → enum coercion via `object.__setattr__` on the frozen dataclass; invalid string raises `ValueError`; `SILENT` + `no_interactive=False` raises `ValueError` (hard invariant). `from_kwargs(**kwargs)` integration helper validates kwarg names against declared fields (`TypeError` on unknown). `CrossRunResult` carries `session` dict + `output_dir` + `run_id` — the actual locals Phase D's body will return (NOT request passthroughs). New `tests/unit/pipeline/cross_project/test_cross_run_request.py` (20 tests across 5 groups): identity promotion (3), signature lock + 23-param sanity (2), field parity + count + allowlist lock (3), TestCrossPresentationPolicy 8-case mirror, CrossRunResult shape (3). **Signature lock pinned** to the regenerated-from-pytest reference: `(task: str, projects: dict[str, pathlib.Path], max_rounds: int = 1, model: str = 'claude-sonnet-4-6', …)` — note `claude-sonnet-4-6` is the fallback under the test-env `ORCHO_DISABLE_LOCAL_CONFIG=1` and is what the pin captures (the Phase A draft's `claude-opus-4-7` reflected a different env's local-config override; corrected in this commit). `run_cross_pipeline` untouched in Phase C — only types + tests + module-level promotion. Verification: 20 new tests green; project signature locks unchanged (`tests/unit/pipeline/test_project_run_request.py` 22 tests green); cross + integration + acceptance suite 471 tests green; full suite 4204 passed; ruff clean. |
| D     | Shipped | `8801efd` | Added `pipeline/cross_project/app.py` with `run_cross_project_pipeline(request: CrossRunRequest) -> CrossRunResult` (typed boundary) + private `_run_cross_pipeline_session(request) -> (session, output_dir, run_id)` (the 1300-LoC body migrated bodily from `orchestrator.py`). Moved with the body: `_PHASE_AGENT_ATTRS` + 9 helpers (`_flatten_profile_entries`, `_agent_model_for_phase`, `_agent_entries_for_project_steps`, `_gate_will_run`, `_read_plan_file`, `_capture_invoke_usage`, `_print_usage_snapshot`, `_print_cross_planning_usage`, `_print_cross_checks_usage`). `orchestrator.py::run_cross_pipeline` shrank from 1325 LoC to a 25-LoC thin wrapper that builds `CrossRunRequest(...)` and calls `run_cross_project_pipeline(request).session` — 23-kwarg signature preserved byte-identical so Phase C's lock stays green. **Signature lock for `run_cross_project_pipeline`** pinned to `"(request: pipeline.cross_project.app_types.CrossRunRequest) -> pipeline.cross_project.app_types.CrossRunResult"` (used `from __future__ import annotations` deliberately NOT enabled — mirrors project `app.py` discipline so the lock captures resolved class names, not string forms). `CrossRunResult` carries actual `output_dir` + `run_id` from the body's bootstrap, not request passthroughs. Pre-flight cycle check ran cleanly: the body uses `_accumulate_phase_usage` (already in `usage.py`'s canonical home), so no helper move to `usage.py` was forced. Back-compat re-exports added to `orchestrator.py` for test-patch surface: `_accumulate_phase_usage` / `_capture_invoke_usage` / `_print_cross_checks_usage` / `_print_cross_planning_usage` / `_print_usage_snapshot` (from `app`), `_assert_fresh_run_dir_available` (from `bootstrap`), `_plan_hypothesis_step` (from `profile_dispatch`), `render_cross_plan_block` (from `core.io.transcript`). `planning_loop.py`'s 3 lazy import blocks (lines 505, 875, 948 pre-fix) updated to point at canonical homes (`app` for `_capture_invoke_usage` / `_print_usage_snapshot`; `usage` for `accumulate_phase_usage`); `cross_replan_prompt` / `cross_plan_prompt` stay at `orchestrator` (public prompt surface). Test migration (4 files): `test_runner_gate_policy.py` / `test_codex_total_only_metrics.py` patch sites now target `pipeline.cross_project.usage.accumulate_phase_usage` + `pipeline.cross_project.app._accumulate_phase_usage` (both surfaces because planning_loop's fresh lazy imports resolve through usage while app's top-level alias binds at module load); `test_cross_orchestrator.py` `run_hypothesis_loop` / `_plan_hypothesis_step` patches retarget to `pipeline.cross_project.app`; `test_capture_invoke_usage.py` `_UNPRICED_MODELS_WARNED` patches retarget to `app`; `test_cross_silent_dispatch.py` `_plan_hypothesis_step` retargets to `app`. New `tests/unit/pipeline/cross_project/test_cross_app_isolation.py` (6 AST guards): `app.py` does not import `orchestrator`; **`app_types.py` does not import `orchestrator`** (reviewer pre-flight check); `rendering.py` does not import `orchestrator` (Phase B invariant pinned); `constants.py` is a stdlib-only leaf; no `sys.exit` reachable from `app.py`; no `pipeline.project.cli` reach from any cross module (ADR 0042 stop #9 inherited). Verification: 422 cross + integration + acceptance tests green; 4218 full suite passed; ruff clean. |
| E     | Shipped | `0755b0d` | Threaded `request.presentation` through cross runtime via three explicit seams (NOT `state.extras` — cross planning doesn't carry a `PipelineState`). **Implementation pivot from the original plan**: replaced the manual `_DispatchPorts` wrap + per-site `if terminal:` gating strategy with a **`silent_renderers` factory** in `pipeline/cross_project/rendering.py`. The factory returns `(banner, success, warn, preview, _render_cross_plan_preview, print_fn, C)` per the `terminal` flag; under TERMINAL byte-identical to the canonical helpers, under SILENT the stdout helpers are no-ops and `banner` forwards `terminal=False` to the rendering implementation (so `log_phase` STILL fires — ADR 0046 stop #9 invariant inherited). Each gated function destructures the seven callables at its top: Python's local-name binding then resolves every subsequent `banner(...)` / `success(...)` / `warn(...)` / `preview(...)` / `print(...)` call to the right shadow without needing per-call `if` gates or risking `UnboundLocalError` from conditional `def` blocks (the naïve `if not terminal: def banner(...)` shape was caught by ruff F823 before commit). Wiring: (1) `pipeline/cross_project/app.py::_run_cross_pipeline_session` destructures `silent_renderers(terminal)` after `presentation = request.presentation` unpack — every body site in the 13-row inventory now obeys the policy automatically. (2) `CrossPlanningContext` gains `terminal: bool = True` field; each of planning_loop's 6 gated functions (`_bypass`, `_approve_pre_existing`, `_approve_resume_cached`, `_resume_handoff_decision`, `_retry_feedback_round`, `_run_initial_loop`, `_invoke_plan_round`, `_invoke_validate_round`) destructures `silent_renderers(ctx.terminal)` — covers the 25+ planning_loop sites. (3) `ProjectDispatchContext` gains `terminal: bool = True` field, passed from `app.py`; the body inside `project_dispatch.py` threads `ctx.terminal` into `apply_cross_phase_handoff_pause(..., terminal=ctx.terminal)` so the parent-side pause-banner `warn(...)` (`handoff_payloads.py:197`) suppresses under SILENT. The 4 module-level helpers in `app.py` (`_read_plan_file`, `_capture_invoke_usage`, `_print_usage_snapshot`, `_print_cross_planning_usage`, `_print_cross_checks_usage`) gained explicit `terminal: bool = True` keyword params + internal `if terminal:` gates so the gating reaches their own `warn` / `success` / `print` calls. `gate_decisions.py:108` raw stderr print stays ungated — it's inside the interactive-prompt branch (`if interactive_allowed and stdin_is_tty and stdout_is_tty:`) which is structurally unreachable under SILENT (`no_interactive=True` hard invariant short-circuits the branch). `_DispatchPorts` wrap happens automatically: the local `banner` / `success` / `warn` shadows in `app.py`'s body propagate into the dataclass when the body constructs `_DispatchPorts(banner=banner, success=success, warn=warn)`. Per-alias child invariant unchanged — `ProjectRunRequest(presentation=SILENT, no_interactive=True)` shape at the child build site stays exact. New `tests/unit/pipeline/cross_project/test_cross_silent_threading.py` (11 focused unit tests across 3 seams): factory contract — TERMINAL returns canonical helpers, SILENT suppresses stdout but banner STILL calls log_phase (the right contract), TERMINAL polarity guard; `CrossPlanningContext.terminal` field present + default True; `ProjectDispatchContext.terminal` field; `apply_cross_phase_handoff_pause(terminal=False)` suppresses warn while preserving structural side-effects (session status, checkpoint markers, `phase.handoff_requested` event); TERMINAL warn polarity sanity; presentation→terminal flag chain for SILENT (enum + string-coerced) and TERMINAL default. Verification: 11 focused tests green; 293 cross + integration tests green; 4229 full suite passed (+11 vs Phase D); ruff clean. |
| F     | Shipped | `4d77f8d` | Structural cross finalization split mirroring ADR 0042 Phase G. Extracted the inline tail (status decision + `run.end` emit + per-project metrics rollup + `session.json`/`metrics.json`/evidence-bundle persistence + artifact mirror) from the bottom of `_run_cross_pipeline_session` into a new `pipeline/cross_project/finalization.py`. Two callables: `finalize_cross_run(ctx) -> CrossFinalizationResult` (silent service — owns the three-branch decision tree, mutates `session`, emits `run.end` exactly once, persists when `output_dir=True`, mirrors artifacts, catches mirror exceptions into `result.mirror_error`; **zero stdout/stderr**) and `finalize_cross_with_terminal_output(ctx)` (terminal wrapper — calls the silent service once, then renders DONE / FAILED banner + chips off the structured `CrossFinalizationResult`; does NOT re-decide status, NOT re-emit `run.end`, NOT re-write any persisted file). `CrossFinalizationContext` is a frozen + slotted value object carrying every local the inline tail used to read directly (`run_dir`, `output_dir`, `session`, `projects`, `max_rounds`, `cfa_result`, `contract_results`, `contract_check_failed`, `contract_check_failure_reason`, `cross_phase_usage`). `CrossFinalizationResult` carries `(status, halt_reason, failure_reason, skipped_by_policy, session_path, metrics_path, mirrored_artifacts, mirror_error, per_project_metrics)` — every persisted-path field is the actual writer return. `_run_cross_pipeline_session` body shrank by 120+ LoC; the leftover inline tail is now an 18-LoC branch on `terminal` that builds the context and calls one of the two finalization callables. Dropped the now-unused top-level `import json` from `app.py`. `terminal.py`'s existing `finalize_cross_terminal()` (early-return path) untouched — independent of the tail split. New `tests/unit/pipeline/cross_project/test_cross_finalization_silent.py` (13 focused tests across 6 seams): silent service produces zero stdout/stderr; `run.end` emitted exactly once via silent service; terminal wrapper does NOT re-emit `run.end` (load-bearing invariant); 3-branch status decision tree (CFA-skipped + blocking-skip → failed, CFA-skipped without blocking → done + `skipped_by_policy=True`, CFA-rejected → failed with agent halt_reason; CFA parse_error → failed with parse_error halt_reason); persistence — `session.json` + `metrics.json` written when `output_dir=True`, skipped when `False`; terminal wrapper renders DONE banner + `Projects: ...` + `Session: ...` + `Metrics: ...` + `Mirrored ...` chips, FAILED banner carries `failure_reason`, policy-skip variant renders the `(cross_final_acceptance skipped by policy)` text; mirror error surfaces in result + terminal `! mirror skipped` line. Verification: 13 focused tests green; 4337 full suite passed (+13 vs Phase E); ruff clean. |
| G     | Shipped | `2b58287` + r1 `01880a1` | Extracted the cross CLI leaf into a new `pipeline/cross_project/cli.py`: `main()` (the 540-LoC argparse / resume-mode / workspace-walk-up / projects-parse / phase_config build / `run_cross_pipeline` call / status → exit-code body), `print_error` (CLI-only red-on-stderr printer), `_resolve_cross_resume_latest` (process-exiting resume-latest chooser), `KeyboardInterrupt` handler. `orchestrator.py` shrank from 929 LoC to 333 LoC; it now imports `Path` + `PhaseAgentConfig` + `AgentProvider` + `config` + prompt + plugin + alias helpers only (CLI imports removed), keeps `parse_projects` + `build_cross_context` + 4 prompt builders + the 23-kwarg `run_cross_pipeline` back-compat wrapper, and re-exports `main` + `print_error` from `cli` for the legacy test-patch surface (~30 patches under `tests/unit/cli/test_cross_orchestrator_main.py` + `sdk.runner.run_cross_from_args` during the Phase G → I transition). `pyproject.toml`: `orcho-cross = "pipeline.cross_project.cli:main"`. SDK bridge `sdk.runner.run_cross_from_args` now does `from pipeline.cross_project import cli as xcli; xcli.main()`. Test patch-target migration: `test_cross_orchestrator_main.py` (~30 `from pipeline.cross_project.orchestrator import main` → `from pipeline.cross_project.cli import main` — the canonical import path; legacy orchestrator name resolves through the re-export but the typed home is `cli`); `test_decision_flags.py` (1 patch: `pipeline.cross_project.orchestrator.main` → `pipeline.cross_project.cli.main` — this one MUST migrate because `sdk.runner` calls through `cli.main` now, so patching the orchestrator alias would silently miss); `test_runner_gate_policy.py` + `tests/unit/core/test_event_kinds.py` + `tests/acceptance/test_full_mock_flow.py` (3 `load_plugin` patches retargeted from `pipeline.cross_project.orchestrator.load_plugin` to `pipeline.cross_project.app.load_plugin` since the body's `load_plugin` call resolves through `app.py`'s own import after Phase D, not through orchestrator). New `tests/unit/pipeline/cross_project/test_cross_cli_isolation.py` (24 AST guards): 20 non-CLI peers checked one-by-one for `pipeline.cross_project.cli` imports (orchestrator deliberately excluded — it's the back-compat shim); `app.py` body doesn't reach `sys.exit` (re-pinned post-G); no cross module imports `pipeline.project.cli` (ADR 0042 stop #9 inherited); `rendering.py` does not import `cli`; back-compat identity invariant — `orchestrator.main is cli.main` AND `orchestrator.print_error is cli.print_error` so legacy patches land on the same object. CLI smoke: `python -m pipeline.cross_project.cli --help` exits 0; `from cli.orcho import cmd_cross` imports cleanly. **r1 (`01880a1`)** — closed the eager-CLI-load tail the reviewer flagged after r0: the back-compat `main` / `print_error` re-export from `orchestrator` was a top-level `from pipeline.cross_project.cli import …`, so a bare `import pipeline.cross_project` (which loads `orchestrator` via the package `__init__`) eagerly pulled the CLI leaf — argparse, `sys.exit`, and every CLI helper — into `sys.modules`, AND `python -m pipeline.cross_project.cli --help` emitted `RuntimeWarning: 'pipeline.cross_project.cli' found in sys.modules after import of package 'pipeline.cross_project', but prior to execution of 'pipeline.cross_project.cli'`. The double-load was cosmetic but the CLI was no longer a leaf by load order — every package-root consumer incurred its import cost. r1 replaces the eager `from … import` with a PEP 562 module-level `__getattr__` that imports `cli` on first attribute access; the identity invariant (`orchestrator.main is cli.main`) still holds (cached `cli.main` returned on every call) and `monkeypatch.setattr(orchestrator, "main", fake)` still wins (patches write to `__dict__`, which beats `__getattr__` on subsequent reads — Python attribute-lookup order). New subprocess-isolated `test_package_root_import_does_not_load_cli_module` pins three steps: (1) `import pipeline.cross_project` does NOT load cli; (2) `from pipeline.cross_project import orchestrator` does NOT load cli either; (3) `orchestrator.main` attribute access DOES trigger the lazy load. Drift back to eager re-export fails step 1 immediately. Verification: 25 isolation guards green; full suite 4367 passed (+4 vs r0); ruff clean. |
| H     | Shipped | `e204454` | End-to-end cross SILENT boundary tests + light library-consumer migration. **New `tests/integration/cross/test_cross_silent_boundary.py` (7 tests)**: (1) SILENT done path — 2-project run via `run_cross_project_pipeline(CrossRunRequest(presentation=SILENT, no_interactive=True))`, asserts `capsys.out == ""` AND `capsys.err == ""`, typed `CrossRunResult` carries real `output_dir`+`run_id`+`session["status"] == "done"`, persisted `meta.json` on disk mirrors in-memory status, both children get `presentation=SILENT, no_interactive=True`; (2) `run.start` + exactly-one `run.end` event emitted under SILENT (Phase F invariant — the silent service is the only emitter), `events.jsonl` lands on disk even with zero stdout; (3) SILENT child-failure path — one child returns `status="failed"`, cross body continues through contract_check + finalization (ADR 0025), failure surfaces structurally in persisted `phases.projects` while `capsys` stays empty; (4) TERMINAL default regression — `CrossRunRequest()` with no explicit policy keeps the legacy CLI transcript shape, asserts `▶ SUB-PIPELINE [api]` + `▶ SUB-PIPELINE [web]` + `[CONTRACT_CHECK]` + `[DONE]` + `Projects: 2 \| Rounds each:` chips present (marker-presence, NOT byte-exact — no snapshot file maintained for cross transcript); (5) cross silent-path modules (`app`, `planning_loop`, `project_dispatch`, `handoff_payloads`) all carry presentation threading (either consult `PresentationPolicy.TERMINAL` literal or accept `terminal: bool` field/param); (6) `app.py` does NOT import `orchestrator` post-G; (7) no cross module imports `pipeline.project.cli` (ADR 0042 stop #9 inherited). **Phase H r1-on-discovery — closed a stop-#9 violation the integration tests exposed**: `pipeline/cross_project/app.py:441` called `setup_run_logging(output_dir, session_ts, is_resume=...)` without passing `terminal=terminal`, so the two grey ``📄 Live output`` / ``📡 Events`` chips leaked to stdout under SILENT even though `setup_run_logging` itself already had the `terminal: bool` parameter (ADR 0046 Phase C site 18, gated on the project side but not threaded from the cross body). Fix: thread `terminal=terminal` into the cross call. Without this fix, every cross SILENT run printed 2 grey courtesy chips — the integration tests caught it on first execution. **Light consumer migration** added to `tests/integration/cross/test_cross_silent_dispatch.py` (2 new tests): `test_cross_dispatch_uses_silent_presentation_via_typed_boundary` re-runs the Phase D child-SILENT contract pin but drives through `run_cross_project_pipeline(CrossRunRequest(...))` (proves the typed boundary is a real drop-in for the legacy 23-kwarg `run_cross_pipeline(...)` wrapper, not a partial shim); `test_typed_boundary_accepts_request_builder_helper` exercises `CrossRunRequest.from_kwargs(**kwargs)` — the integration helper for MCP / SDK / orcho-web cross bridge callers that have kwargs in hand. Legacy signature-lock tests stay on `run_cross_pipeline(...)` per ADR plan (legacy wrapper is a permanent surface). Acceptance + full_mock_flow tests deliberately NOT migrated — they hit the legacy 23-kwarg wrapper through real-world driver code; their migration is out of scope for Phase H. Verification: 14 cross integration tests green (was 5 pre-Phase-H); 4376 full suite passed (+9 vs Phase G); ruff clean. |
| I     | Shipped | `3126d57` | Flipped ADR 0047 to `Accepted` with the closing summary above (per-phase commit hashes already filled in their rows; Phase G captures r0 `2b58287` + r1 `01880a1`; Phase H captures `e204454`). Updated `docs/architecture/overview.md`: (1) top-level diagram gains a cross-boundary box stacked over the project-boundary box with the per-alias dispatch arrow pointing at `ProjectRunRequest(presentation=SILENT, no_interactive=True)` — visual statement that cross is a typed-boundary peer of project, not a wrapper that calls into project CLI; (2) `## Why these concepts exist` § now carries a cross-boundary bullet parallel to the project-boundary + presentation-policy bullets ADR 0046 Phase G added, with explicit cross-links to ADR 0042 + ADR 0046 + ADR 0047 so the boundary story is one coherent landing spot; (3) presentation-policy bullet rewritten to call out that `PresentationPolicy` lives in the neutral `pipeline.presentation` module and is shared by both `ProjectRunRequest` and `CrossRunRequest`. The Accepted summary calls out the Phase H E2E inventory expansion — `setup_run_logging(... terminal=terminal)` was caught on first execution of the end-to-end SILENT boundary test, not by Phase E unit-level threading — to anchor for the next reader why the boundary tests are the load-bearing check (not just an additional layer of unit coverage). |

## Context

ADR 0042 decomposed the single-project orchestrator behind a typed boundary:
```
ProjectRunRequest → run_project_pipeline → ProjectRunResult
```
ADR 0046 made that boundary presentation-aware:
```
PresentationPolicy.TERMINAL  → byte-identical legacy transcript
PresentationPolicy.SILENT    → zero stdout/stderr; events.jsonl + progress.log + session.json byte-identical
```
ADR 0046 Phase D switched cross-project's per-alias dispatch to consume the project boundary correctly: every child now runs under `ProjectRunRequest(presentation=SILENT, no_interactive=True)`.

**Current gap.** Cross-project itself still has an untyped app surface. `pipeline/cross_project/orchestrator.py::run_cross_pipeline(...)` is a 23-kwarg function (lines 463–1789) with a 1327-LoC body that mixes typed inputs, run bootstrap, profile projection, terminal rendering, planning loop, per-project dispatch, contract gates, finalization, and CLI-style helpers. Its CLI `main()` is co-located in the same file (lines 1790–2321) with argparse + `sys.exit` + `KeyboardInterrupt` + `print_error`. Library callers don't exist yet — the only non-test consumer is `main()` itself — but the second `run_cross_pipeline(...)` would be in trouble.

Worse, `orchestrator.py` is **load-bearing as a rendering peer**, not just a CLI entry. `pipeline/cross_project/planning_loop.py` imports `banner`/`success`/`warn`/`C`/`_render_cross_plan_preview`/`_capture_invoke_usage`/`_print_usage_snapshot` from `orchestrator` at 8 sites (lines 284, 305, 331, 373, 501, 688, 858, 924). `pipeline/cross_project/handoff_payloads.py:197` imports `warn`. The cross run body itself constructs `_DispatchPorts(banner=banner, success=success, warn=warn)` from orchestrator-local helpers. Any typed-boundary extraction that leaves `banner`/`success`/`warn` in `orchestrator.py` keeps the cycle.

Plus a render-vs-log subtlety: `orchestrator.py::banner` (line 123) calls `print(...)` AND `log_phase(...)` (line 129). Gating `banner(...)` as a whole would suppress the structural `phase.start` / progress.log writes — the same trap ADR 0046 Phase C fell into and fixed in the r5 commit. The cross helpers need the same render-from-log split.

## Signature pin (Phase A.2 — pre-flight reference)

The Phase C signature-lock test pins exactly this string. Regenerated via:
```bash
.venv/bin/python -c "import inspect; from pipeline.cross_project.orchestrator import run_cross_pipeline; print(str(inspect.signature(run_cross_pipeline)))"
```

```
(task: str, projects: dict[str, pathlib.Path], max_rounds: int = 1, model: str = 'claude-sonnet-4-6', output_dir: pathlib.Path | None = None, dry_run: bool = False, mock: bool = False, provider: 'AgentProvider | None' = None, phase_config: agents.registry.PhaseAgentConfig | None = None, cross_mode: str = 'full', plan_file: str | None = None, resume_from: str | None = None, hypothesis_enabled: bool | None = None, profile_name: str = 'advanced', operator_decisions: 'tuple | None' = None, no_interactive: bool = False, resumed_meta: 'dict | None' = None, resume_mode: str | None = None, followup_parent_run_id: str | None = None, followup_parent_run_dir: str | None = None, followup_parent_status: str | None = None, followup_base_task: str | None = None, followup_session_seeds_per_alias: 'dict[str, dict[str, str]] | None' = None) -> dict
```

**Param count: 23.** Phase C's `CrossRunRequest` field count must equal 23 + 1 (`presentation`) = **24**, with `_REQUEST_ONLY_FIELDS = {"presentation"}` parity allowlist.

## Architecture target

```
CLI leaf (orcho-cross + top-level orcho cross bridge)   — terminal presentation; extracted in Phase G
  ↓
typed cross app boundary                                — run_cross_project_pipeline(request)
                                                          ├─ presentation=TERMINAL → legacy CLI parity
                                                          └─ presentation=SILENT   → zero stdout/stderr,
                                                                                     structured side-effects only
  ↓
cross planning / per-alias dispatch / contract / finalization
  └─ per-alias child  → run_project_pipeline(
                          ProjectRunRequest(..., presentation=SILENT, no_interactive=True)
                        )

shared peer modules:
  pipeline/cross_project/rendering.py   — banner (with terminal= split) / success / warn / preview / C /
                                          _render_cross_plan_preview                            (Phase B)
  pipeline/cross_project/app_types.py   — CrossRunRequest / CrossRunResult                       (Phase C)
  pipeline/cross_project/constants.py   — CROSS_DEFAULT_PROFILE (neutral home, breaks cycle)     (Phase C)
  pipeline/cross_project/app.py         — run_cross_project_pipeline body                        (Phase D)
  pipeline/cross_project/finalization.py — finalize_cross_run / finalize_cross_with_terminal_output (Phase F)
  pipeline/cross_project/cli.py         — main / argparse / print_error / sys.exit               (Phase G)
  pipeline/presentation.py              — PresentationPolicy (promoted from project.types)       (Phase C)
```

Mirrors the ADR 0042 + ADR 0046 layout one-for-one: project boundary → cross boundary at the same architectural level, with the cross child arrow consuming the project SILENT contract.

## Decisions

### D1. `PresentationPolicy` promoted to a neutral shared module
Move the enum out of `pipeline.project.types` into a new `pipeline/presentation.py`. Re-export from `pipeline.project.types` for back-compat (7 existing importers continue working byte-identical). The new `CrossRunRequest` in `pipeline/cross_project/app_types.py` imports from the neutral home. Justification: project and cross are real callers now — the "shared primitive only after two real callers" rule is satisfied. Low-churn: 7 internal modules total, no external packages.

### D2. Typed boundary owns the body. Body lives in `app.py`.
ADR 0042 Phase I review caught the "wrapper-calls-wrapper" smell. Same direction here, with a stronger commitment: **`run_cross_project_pipeline`'s body lives in `pipeline/cross_project/app.py` itself**, not imported from `orchestrator.py`. The orchestrator becomes a thin back-compat wrapper module that:
```python
# pipeline/cross_project/orchestrator.py — minimal back-compat surface after Phase D
from pipeline.cross_project.app import run_cross_project_pipeline
from pipeline.cross_project.app_types import CrossRunRequest

def run_cross_pipeline(...23 kwargs...) -> dict:
    return run_cross_project_pipeline(CrossRunRequest(...)).session

# (plus `main()` until Phase G extracts it to cli.py)
```
The body never re-imports from `orchestrator`. Direction enforced by an AST guard test (Phase D).

### D3. Render helpers extracted to a peer module BEFORE typed boundary lands (Phase B)
**Render helpers only.** `C` (color constants), `banner` (with the new `terminal=` split per D4), `success`, `warn`, `preview`, `_render_cross_plan_preview` move from `orchestrator.py` to a new `pipeline/cross_project/rendering.py`. **Usage / metrics helpers** (`_capture_invoke_usage`, `_print_usage_snapshot`, `_print_cross_planning_usage`, `_print_cross_checks_usage`, `_accumulate_phase_usage`) are conceptually closer to `usage.py` and stay in `orchestrator.py` for Phase B; Phase D's pre-flight re-checks whether the body migration creates an import cycle and, if so, moves the offenders to `pipeline/cross_project/usage.py` in the same commit. Phase B import-site migrations touch every consumer of a render helper: **7 of `planning_loop.py`'s 8 import blocks** (4 pure-render — lines 284, 305, 331, 373 — retarget wholesale to `rendering.py`; and **3 mixed blocks** — lines 501, 688, 858 — each split so render helpers (`C`, `banner`, `success`, `warn`, `_render_cross_plan_preview`) come from `rendering.py` while usage helpers (`_accumulate_phase_usage`, `_capture_invoke_usage`, `_print_usage_snapshot`) and prompt helpers (`cross_plan_prompt`, `cross_replan_prompt`) stay at `orchestrator.py` until Phase D's cycle check). Line 924 is pure usage and stays untouched. Plus `handoff_payloads.py:197` (single `warn` import) and tests that import render helpers. **This phase precedes typed-boundary work**; otherwise the orchestrator stays load-bearing as a rendering peer, and Phase D would have to import rendering back through `orchestrator.py` — recreating the cycle.

### D4. `banner()` gets a `terminal=True` keyword-only render/log split (ADR 0046 Phase C r5 pattern, applied prospectively)
Current `banner()` body:
```python
def banner(phase, title, color=C.CYAN, *, phase_kind=None, attempt=1):
    print("\n═...═")
    print(f"  [{phase}] {title}")
    print("═...═\n")
    log_phase(phase, title, phase_kind=phase_kind, attempt=attempt)
```
Phase B reshape (now in `rendering.py`):
```python
def banner(phase, title, color=C.CYAN, *, phase_kind=None, attempt=1, terminal=True):
    if terminal:
        print("\n═...═")
        print(f"  [{phase}] {title}")
        print("═...═\n")
    log_phase(phase, title, phase_kind=phase_kind, attempt=attempt)  # always
```
`success` / `warn` / `preview` stay pure-stdout (no `log_phase` call inside them today — verified at `orchestrator.py:131–146`). They can be gated whole at call sites without the split.

### D5. Presentation threads via dataclass fields + DispatchPorts wrap (NOT `state.extras`)
`state.extras["_silent"]` is a project-handler-level seam (ADR 0046 sites 19–21). Cross planning uses `CrossPlanningContext` (planning_loop.py:183–221) which does NOT carry a `PipelineState`. Threading presentation through cross uses three seams:

1. **`CrossPlanningContext` gains a `terminal: bool` field** (defaulting `True`); planning_loop.py reads it at every gated site.
2. **`_DispatchPorts` construction wraps under SILENT** (at the `_DispatchPorts(banner=banner, success=success, warn=warn)` construction site, which migrates into `app.py` via Phase D):
   ```python
   if presentation is PresentationPolicy.SILENT:
       _dispatch_ports = _DispatchPorts(
           banner=lambda *a, **kw: banner(*a, terminal=False, **kw),  # log_phase still fires
           success=lambda _t: None,                                    # pure stdout — drop
           warn=lambda _t: None,                                       # pure stdout — drop
       )
   else:
       _dispatch_ports = _DispatchPorts(banner=banner, success=success, warn=warn)
   ```
3. **Direct `banner(...)` / `success(...)` / `warn(...)` / `print(...)` call sites in the typed-boundary body** consult the local `terminal = presentation is TERMINAL` (or use `banner(..., terminal=terminal)`).

### D6. Cross finalization gets a Phase-G-style structural split (Phase F)
Mirror ADR 0042 Phase G project finalization. The inline tail at `orchestrator.py:1700–1776` (DONE banner, session/usage/metrics chips, mirror notice, cross_metrics rollup, run.end emit) extracts into `pipeline/cross_project/finalization.py`:
```python
finalize_cross_run(ctx) -> CrossFinalizationResult                       # silent service
finalize_cross_with_terminal_output(ctx) -> CrossFinalizationResult       # terminal wrapper
```
**Invariant (load-bearing):** the silent service owns the **status decision** (sets `session["status"]`) + `run.end` emit + persistence. The terminal wrapper calls the silent service FIRST, then renders chips/banner based on the returned `CrossFinalizationResult`. No double status decision, no double `run.end` emit, no double persistence. Mirrors ADR 0042 Phase G project finalization invariant.

`terminal.py`'s existing `finalize_cross_terminal()` (early-return path) is independent and stays — it's already on disk-I/O only, no stdout. The new split owns the tail.

### D7. Hard invariant: `SILENT` implies `no_interactive=True` (mirrors project)
`CrossRunRequest.__post_init__` rejects `SILENT` + `no_interactive=False` with `ValueError`. String coercion (`presentation="silent"` → enum) via `object.__setattr__` on the frozen dataclass. Same shape as `ProjectRunRequest.__post_init__`.

### D8. Field parity contract for the legacy wrapper
`_REQUEST_ONLY_FIELDS = {"presentation"}` allowlist for cross, same as project. Parity test asserts `fields(CrossRunRequest) - {"presentation"} == params(run_cross_pipeline)`. Drift in either direction trips the test. Field count: 23 (function params) + 1 (`presentation`) = 24.

### D9. CLI extraction is full ADR-0042 parity (Phase G)
`main()` (orchestrator.py:1790–2321) + argparse + `print_error` + `_resolve_resume_latest` + `KeyboardInterrupt` + `sys.exit` mapping + workspace inference move to `pipeline/cross_project/cli.py`. `pyproject.toml`: `orcho-cross = "pipeline.cross_project.cli:main"`. The top-level `orcho cross` bridge (`cli/orcho.py::cmd_cross` → `sdk.run_cross_from_args`) updates to the new path. Test patch targets migrate (~50 in `test_cross_orchestrator_main.py` + 1 in `test_decision_flags.py` + `load_plugin` patches in `test_runner_gate_policy.py` and `test_full_mock_flow.py`). Smoke: BOTH `orcho-cross --help` AND `orcho cross --help` exit 0.

## Cross-tree stdout/stderr inventory

Verified via `rg -n 'banner\(|success\(|warn\(|print\(|print_error\(|preview\(' pipeline/cross_project/` (full-tree, not just orchestrator + project_dispatch).

### `orchestrator.py` — 13 cross-orchestrator-owned terminal renders (Phase E gates)
| # | Site | File:line |
|---|---|---|
| 1 | `print(render_cross_run_header(...))` | `orchestrator.py:656` |
| 2 | Per-alias `[{alias}] plugin: {plugin.name}` | `orchestrator.py:679` |
| 3 | `banner("CROSS_HYPOTHESIS", …)` | `orchestrator.py:783` |
| 4 | `success(f"Run dir: {run_dir}")` | `orchestrator.py:1075` |
| 5 | Distribution print + per-alias subtask line | `orchestrator.py:1081–1089` |
| 6 | `banner("PLAN COMPLETE", …)` | `orchestrator.py:1098` |
| 7 | `success("Subtasks extracted for N projects")` | `orchestrator.py:1099` |
| 8 | `banner("CONTRACT_CHECK", …)` | `orchestrator.py:1294` |
| 9 | Per-phase banners (IMPLEMENT / REPAIR / REVIEW / FINAL_ACCEPTANCE / DONE) | `orchestrator.py:1554–1702` |
| 10 | `success(f"Projects: N \| Rounds each: M")` | `orchestrator.py:1707` |
| 11 | `print(cross_summary_table(...))` + usage `success(...)` | `orchestrator.py:1725–1727` |
| 12 | `success(f"Session: {sf}")` / `success(f"Metrics: {mf}")` | `orchestrator.py:1740–1757` |
| 13 | `success(f"Mirrored N artifacts...")` | `orchestrator.py:1782` |

### `planning_loop.py` — 25+ sites (Phase E gates)
| Site | File:line |
|---|---|
| `success(...)` initial | `planning_loop.py:286` |
| `banner("CROSS_PLAN", …)` + `print(f"  [cwd] …")` + `print(f"  [run dir] …")` | `planning_loop.py:309–311` |
| `banner(…)` + `print(…)` pair (replan round) | `planning_loop.py:334–336` |
| `banner(...)` phase transition | `planning_loop.py:405` |
| `success("Cross run halted by operator")` | `planning_loop.py:446` |
| `_render_cross_plan_preview(plan_output, list(ctx.aliases))` | `planning_loop.py:543` |
| `banner(...)` / `print(...)` / `success(...)` / `warn(...)` cluster | `planning_loop.py:514, 519–520, 550, 604, 623, 697, 702–703, 709, 728, 731, 747, 759, 761, 836, 877, 889` |

### `handoff_payloads.py` (Phase E gate — single site)
| Site | File:line |
|---|---|
| `warn(f"Cross phase handoff requested for {payload['phase']!r}…")` | `handoff_payloads.py:197` |

### `gate_decisions.py` (Phase E gate — loose end)
| Site | File:line |
|---|---|
| `print(f"  unrecognised answer {answer!r}; …", file=sys.stderr)` — NOT gated today | `gate_decisions.py:108` |

### `usage.py` (helper definition, not a leak)
| Site | File:line | Notes |
|---|---|---|
| `def _default_warn(message): print(message)` | `usage.py:11–12` | Defensive fallback used when no warn is wired. Phase E inventory pre-flight triages: if reachable under SILENT, gate then; if fallback-only, document as helper definition (not a leak). |

### CLI-only paths (move to `cli.py` in Phase G)
| Site | File:line |
|---|---|
| `print(f"  ↳ --resume auto-resolved...")` | `orchestrator.py:1987` |
| Workspace inference hints | `orchestrator.py:2108–2118` |
| `KeyboardInterrupt` summary | `orchestrator.py:2311, 2317` |
| `print_error(...)` call sites in CLI / `_resolve_resume_latest` / `main()` body | `orchestrator.py:449, 459` (inside `_resolve_resume_latest`), `1958, 1997, 2000, 2079, 2085, 2137, 2140, 2150, 2163` (inside `main()`). All move with `cli.py` in Phase G; `_resolve_resume_latest` migrates as a CLI helper. |

### Per-alias separator (cross-owned)
| Site | File:line | Notes |
|---|---|---|
| `▶ SUB-PIPELINE [alias]` via `ctx.ports.banner(...)` | `project_dispatch.py:211` | Routes through `_DispatchPorts`. Gated via Phase E's port-wrap strategy (D5). No structural change inside `project_dispatch.py` itself — the change lives at the construction site in `app.py`. |

### Helper definitions in `orchestrator.py` (Phase B moves render helpers; Phase G moves CLI helpers)
`orchestrator.py:98–162` defines `print_error` (CLI-only, → `cli.py` in Phase G), `class C` (color constants — Phase B), `banner` (line 123, with `log_phase` inside — Phase B + `terminal=` split), `success` (line 131 — Phase B), `warn` (line 132 — Phase B), `preview` (line 133 — Phase B), `_render_cross_plan_preview` (line 149 — Phase B).

### File / event writers (NEVER gated — ADR 0046 stop #9 inherited)
`log_phase(...)` everywhere; `events.emit(...)` everywhere; `progress.log` / `meta.json` / `events.jsonl` writers; `set_progress_log` / `init_event_store` / `set_agent_log` from `pipeline/engine/run_logging.py` (already gated correctly in ADR 0046 Phase F).

## Stop conditions

Leave partial work uncommitted, write up the blocker, ask for direction if:

1. **`run_cross_pipeline` signature drifts** beyond the actual 23 declared params (verified in Phase A.2 above).
2. **`run_cross_project_pipeline` becomes a wrapper around `run_cross_pipeline`** (Phase I smell from ADR 0042 — typed boundary owns the body, body lives in `app.py`).
3. **`app.py` imports `orchestrator`** (any direction other than `orchestrator` → `app`).
4. **SILENT suppresses `log_phase`** / `events.emit` / `progress.log` / checkpoint writes / session.json writes anywhere (ADR 0046 stop #9 inherited). Phase E test pattern asserts `log_phase` IS called under SILENT — gating the helper as a whole is the forbidden ADR 0046 Phase C r5 antipattern.
5. **Cross imports `pipeline.project.cli`** (ADR 0042 stop #9 inherited).
6. **Cross app/types/rendering import any CLI module** after Phase G lands (Phase G AST guard catches this).
7. **Cross starts depending on `pipeline.project_orchestrator`** beyond the four stable shim names (ADR 0042 Phase J discipline).
8. **`sys.exit` becomes reachable from `run_cross_project_pipeline`** (Phase D AST guard catches this).
9. **New request-only field appears** without an `_REQUEST_ONLY_FIELDS` allowlist update + parity test entry in the same commit.
10. **Per-alias child dispatch stops using `ProjectRunRequest(presentation=SILENT, no_interactive=True)`** (ADR 0046 Phase D win condition).
11. **Persisted session/meta/checkpoint shape changes** without an explicit migration decision.
12. **Cross final acceptance semantics change** (ADR 0025 + ADR 0037 inherited).
13. **`PresentationPolicy` enum gets duplicated** (`CrossPresentationPolicy`) — the promotion in C.1 is the explicit "one enum, two callers" decision.
14. **Inventory drift:** new cross stdout/stderr site appears that doesn't map to an inventory row + gated branch. Same handling as ADR 0046 stop #11.
15. **`banner()` `log_phase` call gets gated** — the `terminal=` split must keep `log_phase` unconditional. This is the load-bearing observability invariant the cross r5-prospective fix exists for.
16. **Rendering helpers stay in `orchestrator.py`** after Phase B — non-CLI cross peer modules must import render helpers from `rendering.py`, not from `orchestrator.py`. AST grep catches this.
17. **Double status decision or double `run.end` emit** in the Phase F finalization split — the terminal wrapper consumes the silent service's result; it does not re-decide.

## Verification matrix

| Phase | Command | Pass criterion |
|---|---|---|
| A | `ruff check . && git diff --check` | clean; ADR file present with literal 23-param signature pinned |
| B | `pytest tests/unit/pipeline/cross_project/test_cross_rendering.py -vv` | `banner(terminal=False)` test green; `success`/`warn`/`preview` no-side-effect tests green |
| B | current cross suite + new tests | green |
| C | `pytest tests/unit/pipeline/cross_project/test_cross_run_request.py -vv` | parity (23+1) + signature lock + 8-case `TestCrossPresentationPolicy` green |
| C | `python -c "from pipeline.presentation import PresentationPolicy; from pipeline.project.types import PresentationPolicy as P2; assert PresentationPolicy is P2"` | identity re-export holds |
| D | `python -c "from pipeline.cross_project.app import run_cross_project_pipeline; from pipeline.cross_project.orchestrator import run_cross_pipeline"` | import smoke |
| D | `pytest -k 'cross_project_pipeline_signature' -q` | new signature lock green |
| D | `pytest tests/unit/pipeline/cross_project/test_cross_app_isolation.py -vv` | AST guard: `app` doesn't import `orchestrator`; no `sys.exit` reachable |
| D | current cross suite + new tests | green |
| E | `pytest tests/unit/pipeline/cross_project/test_cross_silent_threading.py -vv` | focused tests green; `log_phase` IS called under SILENT |
| E | inventory `rg` scan | every match maps to inventory row + gated branch |
| F | `pytest tests/unit/pipeline/cross_project/test_cross_finalization_silent.py -vv` | silent finalize emits zero stdout/stderr; persisted state byte-identical |
| G | `orcho-cross --help && orcho cross --help` | both exit 0; argparse usage prints |
| G | `pytest tests/unit/pipeline/cross_project/test_cross_cli_isolation.py tests/unit/cli/test_cross_orchestrator_main.py tests/unit/cli/test_decision_flags.py -q` | AST guard + migrated patch targets green |
| H | `pytest tests/integration/cross/test_cross_silent_boundary.py -vv` | 5 end-to-end tests green |
| H | full `pytest --ignore=tests/unit/pipeline/sandbox -q` | 3991+ baseline (from ADR 0046 Phase F) + new tests green |
| I | `grep 'Status: Accepted' docs/adr/0047-cross-project-application-boundary.md` | match |
| I | per-phase rows in ADR 0047 status table | all `Shipped` with commit hashes |

## Consequences

**Wins.**
- Cross-project gains the same typed library entry the project pipeline has had since ADR 0042. Future direct-library UI, MCP migrations, and orcho-web's cross-run subprocess hardening have a clean call shape: `run_cross_project_pipeline(CrossRunRequest(...))`.
- Cross SILENT closes the second half of the ADR 0046 follow-up. Both project and cross now produce **zero stdout/stderr** when called as a library, with all file + event side-effects preserved.
- `orchestrator.py` stops being a rendering peer. Non-CLI cross modules depend on `rendering.py` (a leaf) instead of `orchestrator.py` (an entry point). The cycle dissolves.
- `PresentationPolicy` lives in a neutral home (`pipeline.presentation`) with both project and cross as real callers — the "shared primitive only after two real callers" rule is satisfied.
- The structural cross finalization split (Phase F) mirrors ADR 0042 Phase G; both repos now finalize through a silent service + terminal wrapper pair with the same no-double-decision invariant.
- The CLI extraction (Phase G) brings cross to ADR 0042 parity: `pipeline/cross_project/cli.py` owns argparse + `sys.exit` + `KeyboardInterrupt`, the typed boundary is CLI-free.

**Trade-offs.**
- Phase B's render-extract diff is wide (new `rendering.py` + 7 of 8 `planning_loop` import blocks — 4 retarget wholesale, 3 mixed blocks split into render-from-`rendering.py` + usage/prompt-stays-at-`orchestrator.py` — plus `handoff_payloads.py:197` + tests). Mitigated by keeping Phase B's scope minimal: render helpers only, usage helpers stay in orchestrator until Phase D's cycle check forces a move.
- Phase G's CLI test migration touches ~50 patch targets in `test_cross_orchestrator_main.py`. Mitigated by a shared helper that translates `pipeline.cross_project.orchestrator.X` → `pipeline.cross_project.cli.X` mechanically.
- `CrossRunRequest.__post_init__` runs on every construction. O(1) coerce + reject; negligible.

**Follow-up unlocked by this ADR.**
- **orcho-web cross-run subprocess hardening.** The cross half of ADR 0046's follow-up. With ADR 0047 shipped, orcho-web can call `run_cross_project_pipeline(CrossRunRequest(..., presentation=SILENT, no_interactive=True))` library-side or tail-follow `events.jsonl` instead of parsing stdout. Own ADR scoped to orcho-web.
- **Cross prompt builder hygiene** (`cross_plan_prompt`, `cross_replan_prompt`, `cross_plan_review_focus`). These live in `orchestrator.py` today and are imported by `planning_loop.py` + tests. Moving them to `pipeline/cross_project/prompts.py` (which already exists) is a separate hygiene ADR, deliberately out of scope here.
- **Generic shared orchestrator.** Project and cross share enums + presentation discipline, not bodies. Don't unify until a third real caller appears.

## Reused existing code

This ADR composes against, not duplicates:
- `pipeline.project.types.ProjectRunRequest` + `PresentationPolicy` — Phase C promotion preserves both via re-export.
- `pipeline.project.app.run_project_pipeline` — cross dispatch's child seam from ADR 0046 Phase D stays untouched.
- `pipeline.cross_project.types` — left intact (M13 Phase 1 domain types). Phase C added `pipeline/cross_project/app_types.py` for the app-boundary DTOs instead of co-locating with the domain types (different concerns).
- `pipeline.cross_project.constants` (new in Phase C) — owns `CROSS_DEFAULT_PROFILE`. Orchestrator + app_types both import from here; orchestrator keeps a back-compat re-export at its original name.
- `pipeline.cross_project.terminal.finalize_cross_terminal` — early-return finalization, independent of Phase F's tail split.
- `pipeline.cross_project.project_dispatch` — already uses `run_project_pipeline(ProjectRunRequest(SILENT))` from ADR 0046 Phase D; Phase E adds the cross-level presentation thread via `_DispatchPorts` wrap.
- `pipeline.cross_project.checkpoint` / `handoff` / `handoff_payloads` / `planning_loop` / `profile_projection` / `plan_parser` / `prompts` / `usage` / `artifact_bundle` — unchanged structurally (imports rewire to `rendering.py` in Phase B).
- ADR 0042 Phase J's stable-shim discipline (4 names from `pipeline.project_orchestrator`) — inherited.
- ADR 0046 r5 lesson — the `banner(terminal=True)` split applied prospectively to cross.

## Out of scope (deferred)

- **Cross prompt builders move** to `pipeline/cross_project/prompts.py` — separate hygiene ADR.
- **Generic shared orchestrator** unifying project + cross bodies — three-caller threshold not yet met.
- **MCP wire schema changes** — if Phase H reveals payload drift, file a follow-up ADR scoped to MCP.
- **orcho-web migration** — still on ADR 0046's follow-up; this ADR unlocks the cross half.
- **`pipeline.presentation` further promotion** (to `core.presentation` or its own package) — not justified yet.
