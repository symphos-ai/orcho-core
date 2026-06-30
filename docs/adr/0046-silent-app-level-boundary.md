# ADR 0046: Silent App-Level Boundary on `run_project_pipeline`

Status: Accepted (all phases B–G shipped; commit hashes pinned in the status table below)

| Phase | Status  | Commit    | Notes |
|-------|---------|-----------|-------|
| A     | Shipped | `eb85034` | ADR doc (this file). |
| B     | Shipped | `205a698` | `PresentationPolicy(StrEnum) {TERMINAL, SILENT}` + `ProjectRunRequest.presentation` field (request-only) + `__post_init__` validator (coerce string → enum via `object.__setattr__`; reject `SILENT` + `no_interactive=False` with `ValueError`). `pipeline/project/__init__.py` re-exports `PresentationPolicy` at package root. `tests/unit/pipeline/test_project_run_request.py`: parity contract migrated to `_REQUEST_ONLY_FIELDS = {"presentation"}` with two extended parity tests + a third sanity test that pins the allowlist contents against both sides; new `TestPresentationPolicy` (8 cases: default TERMINAL, SILENT+no_interactive=False raises, SILENT+no_interactive=True succeeds, string `"silent"` coerces, string `"terminal"` coerces, invalid string raises, enum importable from `pipeline.project`, `from_kwargs` accepts `presentation`); new `TestRunProjectPipelineSignatureLock` pins `str(inspect.signature(run_project_pipeline))` to `"(request: pipeline.project.types.ProjectRunRequest) -> pipeline.project.types.ProjectRunResult"`. Existing ADR 0042 Phase J signature lock for `run_pipeline` stays byte-identical (wrapper does NOT grow `presentation`). Smoke: `from pipeline.project import PresentationPolicy; PresentationPolicy.SILENT.value == "silent"`. |
| C     | Shipped | `d2d2f5e` + r5 fix `4391552` | Wired policy through `_PipelineRun` (new `_presentation: PresentationPolicy = TERMINAL` field; sites 7+8 use the new `terminal=` split — see r5 fix below; site 14 `_record_phase_failure` red FAILED block + first-line warn; site 17 `_fsm_checkpoint` warn; site 16 branched `finalize()` between `finalize_project_run` / `finalize_with_terminal_output`). `pipeline/project/app.py`: gated `_print_pipeline_header` call (site 1), `--from-run-plan` projection print (site 2), `Codemap` success (site 3), `Attachments` success (site 4); branched sites 5+6 to re-raise `WorktreeConfigError` / `SandboxConfigError` under SILENT (TERMINAL preserves `print_error + sys.exit(2)` byte-identical); destructured `presentation = request.presentation` and threaded `presentation=presentation` into `_init_checkpoint_with_resume(...)` + `_presentation=presentation` into `_PipelineRun(...)`. `pipeline/project/profile_dispatch.py`: conditional `on_handoff_outcome=print_handoff_outcome if … is TERMINAL else None` (site 11); split `banner("HYPOTHESIS", …)` in `run_hypothesis_block` into `render_phase_header` (gated) + `log_phase("HYPOTHESIS", "START", …)` (unconditional) so SILENT keeps the matched `phase.start` event paired with the closing `log_phase(..., "END", …)` call (site 12); gated mid-loop `warn(...)` in `_on_round_end` (site 13). `pipeline/project/handoff.py`: gated `apply_phase_handoff_pause` `warn(...)` (site 10) — sibling import of `PresentationPolicy` from `pipeline.project.types` (allowed under ADR 0042 stop #10 — banned list is app / project_orchestrator only). `pipeline/project/bootstrap.py`: `init_checkpoint_with_resume` accepts new keyword-only `presentation: PresentationPolicy = TERMINAL` and gates the "Resuming from checkpoint" `success(...)` (site 15). Pre-flight `rg` scan confirmed all 17 inventory sites + helper-vs-callsite triage (e.g. `print_error` body at app.py:117 + `emit_phase_banner` body — helper definitions, gated at call sites). **r5 fix (review caught a P1 observability regression).** Initial Phase C gated `_on_phase_start` / `_on_phase_end` by short-circuiting the whole `emit_phase_banner` / `emit_phase_log_end` helper under SILENT; that dropped not just the courtesy `banner(...)` print but also the underlying `log_phase(...)` call, which is the file + event sink (writes `progress.log` + emits `phase.start` / `phase.end` to `events.jsonl`). ADR 0046 stop #9 forbids gating `log_phase`. Fix: refactored both helpers to accept `terminal: bool = True` and split render-from-log inline (compute label always, `print(render_phase_header(...))` only when `terminal=True`, `log_phase(...)` unconditional). `_PipelineRun._on_phase_start` / `_on_phase_end` / `_emit_phase_log_end` now call the helpers with `terminal=self._presentation is PresentationPolicy.TERMINAL` instead of skipping them entirely. Same split applied to `run_hypothesis_block` so SILENT runs no longer produce an unpaired `phase.end` event without a matching `phase.start`. New `tests/unit/pipeline/project/test_silent_policy_threading.py`: 5 focused tests (finalize branch on SILENT vs TERMINAL, banner SILENT suppresses stdout BUT calls `log_phase` with the canonical PLAN header + `phase_key="plan"`, banner TERMINAL prints header AND calls `log_phase` (polarity guard), phase-log-end SILENT suppresses stdout BUT calls `log_phase("END", …)`, `_record_phase_failure` silent path produces no stdout/stderr while mutating session + emitting `run.end`). Existing ADR 0042 Phase D AST guard (`tests/unit/pipeline/test_handoff_isolation.py`) still green. Full Phase C verification matrix green (2810 tests under `tests/unit/pipeline/` excluding sandbox). `ruff check .` clean. |
| D     | Shipped | `fb17300` | `pipeline/cross_project/project_dispatch.py` switched the per-alias library call from `run_pipeline(...)` (legacy 28-kwarg wrapper) to `run_project_pipeline(ProjectRunRequest(..., presentation=PresentationPolicy.SILENT, no_interactive=True))`. Imports: dropped `from pipeline.project_orchestrator import SessionMode, run_pipeline`; added `from pipeline.project.app import run_project_pipeline` + `from pipeline.project.types import PresentationPolicy, ProjectRunRequest`; `SessionMode` re-imported from `pipeline.project_orchestrator` (one of the four stable shim names per ADR 0042 Phase J). Module docstring updated to reference the typed boundary + SILENT presentation. The 19 kwargs threaded into `ProjectRunRequest` are byte-identical to the prior `run_pipeline(...)` shape; only `presentation` + `no_interactive` are new. Updated 5 existing cross tests in `tests/unit/pipeline/cross_project/test_cross_orchestrator.py` that monkeypatched `_dispatch.run_pipeline` to monkeypatch `_dispatch.run_project_pipeline` via a shared `_make_fake_run_project_pipeline(handler)` shim that translates the new `(request) → ProjectRunResult` shape back into the legacy `**kwargs → session-dict` shape so each test's existing assertions on captured kwargs keep working unchanged. New `tests/integration/cross/test_cross_silent_dispatch.py` (5 tests): contract pin (`request.presentation is SILENT` + `request.no_interactive is True` for every alias dispatched, with cross-level `▶ SUB-PIPELINE [alias]` banner still firing); transcript hygiene (per-child leak patterns like `Codemap:` / `Attachments:` / `Handoff (` / `Resuming from checkpoint` / `FAILED in` / `[PLAN]` / `[VALIDATE_PLAN]` / `[IMPLEMENT]` etc. absent from cross transcript — cross's own DONE / Session / Usage lines deliberately allowed because cross transcript is the next ADR's scope, not this one's); AST guard (cross does not regain dependency on `pipeline.project.cli` — ADR 0042 stop #9); positive smoke (cross imports the typed boundary names + no longer references the legacy `run_pipeline` wrapper); field-set sanity (`ProjectRunRequest` carries `presentation` + `no_interactive` + `project_alias`). Verification: 3750 unit + 5 integration tests pass; `ruff check .` clean. |
| E     | Shipped | `a5440fe` | orcho-web migration breadcrumb recorded in § Consequences below ("Follow-up unlocked by this ADR" → "orcho-web subprocess hardening"). No code in orcho-core — orcho-web edits live in a future ADR scoped to that repo. Also bundles two P3 cleanups from Phase D review: (1) `test_cross_silent_dispatch.py` docstring now lists the actual narrowed leak set (`Codemap:`, `Attachments:`, `Handoff (`, `Resuming from checkpoint`, `FAILED in`, `[PLAN]` / `[VALIDATE_PLAN]` / `[IMPLEMENT]` etc.) and explicitly notes that cross-shared strings (`DONE` / `Pipeline complete` / `Session:` / `Usage:` / `Progress log:`) are deliberately excluded per ADR § Out of scope; (2) `pipeline/cross_project/project_dispatch.py` comment near the handoff-artifact write no longer references `run_pipeline` — updated to point at the typed child request built below (`run_project_pipeline` per Phase D). |
| F     | Shipped | `b5816b9` | `tests/unit/pipeline/project/test_silent_boundary.py` — 5 end-to-end boundary tests through `run_project_pipeline(SILENT)` with `MockAgentProvider` driving real child runs: (1) **done path** — `profile_name="task"`, asserts `capsys.out/err == ""`, persisted `session.json` with `status="done"`, non-empty `events.jsonl` carrying `phase.start` + `phase.end` + `run.end`; (2) **handoff-pause path** — `validate_plan_reject_rounds=99` + `profile_name="advanced"`, asserts silent stdout/stderr (load-bearing: site 10 `warn(...)` + r5 `log_phase` split), `status="awaiting_phase_handoff"` + `phase_handoff` payload, `phase.handoff_requested` event present; (3) **failure path** — monkeypatch `pipeline.runtime.runner.run_profile` to raise `RuntimeError`, wrap in `pytest.raises`, read from persisted state: `capsys.out/err == ""` (site 14 red FAILED block + first-line warn gated), `session.json` carries `status="failed"` + `failure.type="RuntimeError"` + `halt_reason="phase_failure:RuntimeError"`, `events.jsonl` carries `run.end` event with `payload.status="failed"` + `payload.error_type="RuntimeError"`; (4) **grep guard** — literal `is PresentationPolicy.TERMINAL` substring present in every silent-path module (`pipeline/project/app.py`, `run.py`, `profile_dispatch.py`, `handoff.py`, `bootstrap.py`); (5) **terminal-default regression** — same setup with default `presentation=TERMINAL`, asserts `IMPLEMENT` / `DONE` / `Session:` / `Usage:` / `Run dir:` markers present in stdout (back-compat byte-identical). **Phase F follow-up — inventory expansion (caught by the new boundary tests).** The done-path + failure-path tests caught four leak sites missed by the Phase C six-module scan: (a) **site 18** — `pipeline/engine/run_logging.py::setup_run_logging` prints two grey courtesy chips (`📄 Live output → tail -f …` + `📡 Events → …`) that bypassed Phase C because the helper lives in `pipeline/engine/` not `pipeline/project/*`. Fix: added keyword-only `terminal: bool = True` param; `bootstrap.resolve_run_id_and_setup_logging` accepts a new `presentation` param and threads `terminal=presentation is TERMINAL` into the call. `set_progress_log` / `init_event_store` / `set_agent_log` always fire regardless (ADR 0046 stop #9 — file + event sinks never gated). (b)/(c)/(d) **sites 19/20/21** — `pipeline/phases/builtin.py::_print_plan_preview` / `_print_implement_summary` / `_print_review_preview` are handler-level transparency blocks (parsed-plan render, files-touched + diff preview, verdict + findings render) — historically gated only on `state.dry_run`. Fix: extended each gate to `state.dry_run or state.extras.get("_silent")`; `pipeline/project/app.py` sets `state.extras["_silent"] = presentation is PresentationPolicy.SILENT` next to the existing run-level extras. The four new sites are also documented in the inventory section below. The acceptance test `test_crashed_child_blocks_via_precondition` (which monkeypatched `pipeline.cross_project.project_dispatch.run_pipeline` directly) was updated to patch `run_project_pipeline` and read `request.project_alias` off the typed request. Verification: 3991 unit + integration + acceptance tests pass; ruff clean; signature lock pin for `run_project_pipeline` unchanged from Phase B (`(request: pipeline.project.types.ProjectRunRequest) -> pipeline.project.types.ProjectRunResult`). |
| G     | Shipped | `1daf657` | Status flipped from `Proposed` to `Accepted`. All B–F rows hold commit hashes (`205a698`, `d2d2f5e` + r5 `4391552`, `fb17300`, `a5440fe`, `b5816b9`). `docs/architecture/overview.md` updated: the top-level diagram now shows the **CLI leaf → `run_pipeline` (28-kwarg shim) → `ProjectRunRequest.from_kwargs`** path alongside the **library caller → `run_project_pipeline(ProjectRunRequest(...))`** path with TERMINAL / SILENT branches; new "Project application boundary" + "Presentation policy" bullets in § Why these concepts exist cross-link to ADR 0042 + ADR 0046 so the next architectural reader has a single landing spot for both contracts. Final hygiene gates: `ruff check .` clean; public-boundary term scan clean on every Phase F–G file (the canonical token list lives in the hygiene test); `git diff --check` clean. |

## Context

ADR 0042 split `pipeline/project_orchestrator.py` into a typed application boundary (`pipeline.project.app.run_project_pipeline(request: ProjectRunRequest) → ProjectRunResult`) plus a decomposed `pipeline/project/` package. Phase G further split finalization into a **silent service** (`finalize_project_run`) and a **terminal wrapper** (`finalize_with_terminal_output`). Phase J retired the empty `ProjectRunDeps` placeholder per r4 P2 with the explicit note that a later ADR would re-introduce a typed injection seam when it had a concrete contract.

**Current gap.** `run_project_pipeline` is typed at the boundary but is **not silent**. A library caller that wants to drive the pipeline structurally still gets banners + success chips + handoff lines + the DONE block to stdout. The CLI is the only consumer that genuinely benefits from that output; every other consumer either discards it (MCP via subprocess pipe), parses it fragilely (orcho-web tail-follows stdout), or duplicates it into a parent transcript (cross-project).

**Load-bearing consumer.** `pipeline.cross_project.project_dispatch._dispatch_one_alias` calls `run_pipeline(...)` *as a library function* — every per-project banner is interleaved into the cross-run transcript verbatim alongside cross's own `▶ SUB-PIPELINE [alias]` separator. The duplication is structural, not a polish bug.

**Win condition.** A `ProjectRunRequest` with `presentation=PresentationPolicy.SILENT` drives `run_project_pipeline` to produce **zero stdout/stderr** while keeping every persisted artifact + emitted event + checkpoint transition + worktree teardown identical to the terminal path. The CLI default stays `TERMINAL` so existing `run_pipeline(...)` callers (SDK, integration tests, the `orcho-run` entrypoint) get byte-identical transcripts.

## Architecture target

```
CLI leaf                       — terminal presentation, today
  ↓
typed project app boundary     — run_project_pipeline(request)
                                  ├─ presentation=TERMINAL → legacy CLI parity (existing behaviour)
                                  └─ presentation=SILENT   → zero stdout/stderr, structured side-effects only
  ↓
bootstrap / handoff / dispatch / run / finalization
```

Cross-project + a future direct-library UI both target the SILENT branch. CLI stays on TERMINAL.

## Decision

### Policy surface — `PresentationPolicy` on `ProjectRunRequest` (option A)

```python
class PresentationPolicy(StrEnum):
    TERMINAL = "terminal"  # default — banner/success/warn/print fire as today
    SILENT   = "silent"    # no stdout/stderr; files + events + checkpoint still happen


@dataclass(frozen=True, slots=True)
class ProjectRunRequest:
    ...
    presentation: PresentationPolicy = PresentationPolicy.TERMINAL
```

**Why on the request, not on a new `ProjectRunDeps` or as a top-level kwarg.**
- Phase J retired the empty `ProjectRunDeps` exactly because Phase I never consumed it. A new `deps` parameter with a single enum field would re-litigate that ruling for too little payoff. When/if a real injection surface lands (logger ports, transcript renderer, event sink), a future ADR re-introduces `deps` with non-trivial content and migrates `presentation` over.
- Every other run-shape knob (`no_interactive`, `dry_run`, `worktree_config_override`, `profile_obj`) lives on the typed request. Presentation belongs there.
- The wide-kwarg `run_pipeline(...)` back-compat wrapper does NOT grow a `presentation` kwarg — that's request-only. The Phase J signature lock stays exact.

### Hard invariant — `SILENT` implies `no_interactive=True` + enum coercion

`__post_init__` on the frozen `ProjectRunRequest` does two jobs:

1. **Coerce** string → enum (`from_kwargs(presentation="silent")` callers go through here). Without coercion, `self.presentation is PresentationPolicy.SILENT` quietly fails.
2. **Reject** `SILENT` + `no_interactive=False` with `ValueError`. No silent widening; embedders must be explicit.

```python
def __post_init__(self) -> None:
    if not isinstance(self.presentation, PresentationPolicy):
        try:
            normalised = PresentationPolicy(self.presentation)
        except ValueError as exc:
            raise ValueError(
                f"invalid presentation policy: {self.presentation!r}"
            ) from exc
        object.__setattr__(self, "presentation", normalised)
    if self.presentation is PresentationPolicy.SILENT and not self.no_interactive:
        raise ValueError(
            "PresentationPolicy.SILENT requires no_interactive=True"
        )
```

The existing non-interactive handoff branch in `pipeline.project.handoff.process_pending_phase_handoffs` does the right thing under `no_interactive=True`: emits `phase.handoff_requested`, writes `session["status"] = "awaiting_phase_handoff"`, sets `PipelineStatus.AWAITING_PHASE_HANDOFF` on checkpoint, returns `PhaseHandoffLoopResult(paused=True)`. But `apply_phase_handoff_pause` calls `warn(...)` **before** any branch (site 10 in the inventory below), so the silent path DOES need code changes in `handoff.py`.

### Stdout/stderr inventory — silent path blocks all 17 sites

| #  | Site                                                                                    | File                                       | Phase |
|----|-----------------------------------------------------------------------------------------|--------------------------------------------|-------|
| 1  | `_print_pipeline_header` header + `success("Run dir: …")`                               | `pipeline/project/app.py`                  | C     |
| 2  | `print(f"  ↳ --from-run-plan projected profile …")`                                     | `pipeline/project/app.py`                  | C     |
| 3  | `success("Codemap: N lines injected …")`                                                | `pipeline/project/app.py`                  | C     |
| 4  | `success("Attachments: N threaded …")`                                                  | `pipeline/project/app.py`                  | C     |
| 5  | `print_error("Worktree config error …")` + `sys.exit(2)` → re-raise typed error          | `pipeline/project/app.py`                  | C     |
| 6  | `print_error("Sandbox config error …")` + `sys.exit(2)` → re-raise typed error           | `pipeline/project/app.py`                  | C     |
| 7  | `emit_phase_banner(name, st)` (FSM `_on_phase_start`)                                    | `pipeline/project/run.py`                  | C     |
| 8  | `emit_phase_log_end` from `_on_phase_end`                                                | `pipeline/project/run.py`                  | C     |
| 9  | `print(f"{C.GREY}  ↳ skipped: …")` inside `emit_phase_log_end`                          | `pipeline/project/profile_dispatch.py`     | C (indirect via #8) |
| 10 | **`apply_phase_handoff_pause` `warn(...)` — fires before the no_interactive branch**     | `pipeline/project/handoff.py`              | C     |
| 11 | `print_handoff_outcome(outcome)` (passed as `on_handoff_outcome=`)                       | `pipeline/project/profile_dispatch.py`     | C     |
| 12 | `banner("HYPOTHESIS", …)` inside `run_hypothesis_block`                                  | `pipeline/project/profile_dispatch.py`     | C     |
| 13 | `warn(…)` on the mid-loop `save_session` failure path                                    | `pipeline/project/profile_dispatch.py`     | C     |
| 14 | `_record_phase_failure`'s red `FAILED` block + `warn(...)`                               | `pipeline/project/run.py`                  | C     |
| 15 | **`init_checkpoint_with_resume` `success("Resuming from …")`**                          | `pipeline/project/bootstrap.py`            | C     |
| 16 | `finalize_with_terminal_output` (DONE banner + chips + cost note + mirror + worktree)    | `pipeline/project/finalization.py`         | already gated by `finalize()` branch (C) |
| 17 | **`_fsm_checkpoint` `warn(...)` on checkpoint save failure**                            | `pipeline/project/run.py`                  | C     |
| 18 | **`setup_run_logging` `📄 Live output` + `📡 Events` courtesy chips** (added in F follow-up — bypassed Phase C scan because helper lives outside `pipeline/project/*`) | `pipeline/engine/run_logging.py`           | F     |
| 19 | **`_print_plan_preview` parsed-plan structured block** (handler-level transparency block — extended existing `state.dry_run` gate to also short-circuit on `state.extras["_silent"]`) | `pipeline/phases/builtin.py`               | F     |
| 20 | **`_print_implement_summary` files-touched + diff preview block** (same shape as 19) | `pipeline/phases/builtin.py`               | F     |
| 21 | **`_print_review_preview` verdict + findings block** (validate_plan / review_changes / final_acceptance — same shape as 19) | `pipeline/phases/builtin.py`               | F     |

**Note on `log_phase`.** Writes to `progress.log` (file) and emits `phase.start`/`phase.end` events. NOT a stdout site. The silent path keeps `log_phase` calls unchanged — clients reading `events.jsonl` + `progress.log` see the same record either way. Stop condition #9 forbids gating `log_phase`.

### Dispatch-banner gating — keep `_dispatch_active` separate from `_presentation`

`_dispatch_active` is a per-run *"are we inside dispatch right now"* flag whose narrow purpose is to keep direct `_PipelineRun(...)` test instantiations from firing banners. Different concern. Add a separate `_presentation: PresentationPolicy = PresentationPolicy.TERMINAL` field on `_PipelineRun`. Every gated site reads both flags:

```python
if self._dispatch_active and self._presentation is PresentationPolicy.TERMINAL:
    emit_phase_banner(name, st)
```

### Error-path semantics

Sites 5, 6, 14 are *terminal renderings of an error that's already signalled structurally*. The exception/halt is the structural signal; the print is the courtesy for a human reader.

`WorktreeConfigError` and `SandboxConfigError` already exist as typed `ValueError`-derived errors. Under SILENT: re-raise the original typed error without `sys.exit(2)`. The library caller catches and renders. Under TERMINAL: keep `print_error + sys.exit(2)` exactly as today.

Site 14 (`_record_phase_failure`): under SILENT, skip the red `FAILED` block + the `warn(first_line)` print. The structured `failure` block on `session`, the `run.end` event with `status="failed"`, and the original exception propagating out of dispatch are the signal.

### Finalization branching

`_PipelineRun.finalize()` becomes:

```python
def finalize(self) -> dict:
    from pipeline.project.finalization import (
        FinalizationContext,
        finalize_project_run,
        finalize_with_terminal_output,
    )
    ctx = FinalizationContext(run=self)
    if self._presentation is PresentationPolicy.SILENT:
        finalize_project_run(ctx)
    else:
        finalize_with_terminal_output(ctx)
    return self.session
```

`finalize_project_run` is already silent — Phase G of ADR 0042 locked that with `tests/unit/pipeline/test_finalization_silent.py`. The delegator just picks the right side.

## Phase plan

Each phase is one focused commit (same discipline as ADR 0042). Pre-commit gates per phase: worktree-state guard, `ruff check .`, targeted tests, `git diff --check`, banned-term scan on touched files. **Each phase commit MUST update its own row in this ADR's status table in the same commit.**

### Phase A — ADR doc

This file. Status `Proposed` at land; per-phase rows initially `Planned`. Phase G flips to `Accepted`.

### Phase B — `PresentationPolicy` + `ProjectRunRequest.presentation` + parity migration

* `pipeline/project/types.py` — `PresentationPolicy(StrEnum)` + new field + `__post_init__` (coerce + reject). Update module + `from_kwargs` docstrings re: `_REQUEST_ONLY_FIELDS`.
* `pipeline/project/__init__.py` — re-export `PresentationPolicy`.
* `tests/unit/pipeline/test_project_run_request.py` — migrate `TestRequestFieldParity` to use `_REQUEST_ONLY_FIELDS = {"presentation"}`. New `TestPresentationPolicy` (default TERMINAL, SILENT+no_interactive=False raises, SILENT+no_interactive=True succeeds, string "silent" coerces, invalid string raises, enum importable from `pipeline.project`). New signature lock for `run_project_pipeline`.

### Phase C — Wire policy through silent-path modules

**Pre-flight (mandatory):** scan only the silent-path modules — `pipeline/project/cli.py` is the leaf and excluded:

```
rg -n 'banner\(|success\(|warn\(|print\(|print_error\(' \
   pipeline/project/app.py pipeline/project/run.py \
   pipeline/project/profile_dispatch.py pipeline/project/handoff.py \
   pipeline/project/bootstrap.py pipeline/project/finalization.py \
   pipeline/project_testing.py \
   | grep -v 'log_phase\|emit\b'
```

Triage rule: inventory rows are **reachable call sites**, not helper definitions. `print_error` the helper definition contains a `print(...)` body — that's not a leak point; sites 5 + 6 (where `print_error` is *called*) are.

Files touched: `run.py`, `app.py`, `profile_dispatch.py`, `handoff.py` (top-level sibling import of `PresentationPolicy` from `pipeline.project.types`), `bootstrap.py` (new `presentation` keyword-only param on `init_checkpoint_with_resume`).

Tests: `tests/unit/pipeline/project/test_silent_policy_threading.py` — three focused unit tests (finalize branch, banner suppression, failure path).

### Phase D — Cross-project switches to SILENT (the win)

`pipeline/cross_project/project_dispatch.py:278` (the per-alias library call site): replace `run_pipeline(...)` with `run_project_pipeline(ProjectRunRequest(..., presentation=PresentationPolicy.SILENT, no_interactive=True)).session`. Imports rewire to `pipeline.project.app` + `pipeline.project.types`; `SessionMode` stays from the shim (one of the four stable names).

Tests: `tests/integration/cross/test_cross_silent_dispatch.py` — 2-project mock cross, capsys asserts the cross-level `▶ SUB-PIPELINE [alias]` banner fires exactly N times but the transcript contains no per-project DONE / `Pipeline complete` / `Session:` / `Usage:` / `Progress log:` / `Handoff (` / phase START/END headers.

### Phase E — orcho-web migration breadcrumb

Documentation only in orcho-core. See § Consequences for the orcho-web follow-up note. The subprocess + stdout-parsing pattern at `orcho-web/services/runner.py` is the next concrete migration unlocked by this ADR — own ADR scoped to orcho-web.

### Phase F — Signature lock + AST/grep guard + zero-stdout boundary tests

`tests/unit/pipeline/project/test_silent_boundary.py` — five end-to-end boundary tests:

1. **Done path** — `MockAgentProvider`, `profile_name="task"`, `presentation=SILENT, no_interactive=True`. Assert capsys empty + persisted session.json + non-empty events.jsonl.
2. **Handoff-pause path** — `MockAgentProvider(validate_plan_reject_rounds=99)` + `profile_name="advanced"` (validate_plan is in this profile's loop; `task` skips planning). Assert capsys empty + `status="awaiting_phase_handoff"` + `phase.handoff_requested` event present. Load-bearing — site 10 leaks without the Phase C `handoff.py` gate.
3. **Failure path** — monkeypatch `pipeline.project.profile_dispatch.run_profile` → `RuntimeError("test failure")`. Wrap in `pytest.raises` (no `ProjectRunResult` returned). Read assertions from disk: capsys empty, `session.json` has `status="failed"` + `failure.type="RuntimeError"`, `events.jsonl` has `run.end` with `status="failed"`.
4. **Grep guard** — each silent-path module contains at least one `presentation is PresentationPolicy.TERMINAL` or `_presentation is PresentationPolicy.TERMINAL` literal.
5. **Terminal-default regression** — default `presentation=TERMINAL`, capsys contains legacy transcript shape (`"PLAN"`, `"IMPLEMENT"`, `"DONE"`, `"Session:"`, `"Usage:"`).

Plus the `run_project_pipeline` signature lock pinned to `"(request: pipeline.project.types.ProjectRunRequest) -> pipeline.project.types.ProjectRunResult"` with a Phase B-style regeneration recipe.

### Phase G — Status → Accepted

Edit this file: `Status: Proposed` → `Status: Accepted`. Fill all per-phase commit hashes. Audit `docs/architecture/overview.md` and add ADR 0046 reference if it catalogs the boundary surface.

## Stop conditions

1. **Default policy drift** — any change to `presentation` default away from `TERMINAL` trips back-compat.
2. **`run_pipeline` signature drift** — the 28-kwarg wrapper does NOT grow a `presentation` kwarg.
3. **`run_project_pipeline` signature drift** — stays `(request) → ProjectRunResult`. No `deps`, no `presentation` kwarg.
4. **Reverse import** — any new `from pipeline.project.cli import …` outside `tests/unit/cli/**`. ADR 0042 stop #9 holds.
5. **`_orch.X` regression** — any new orchestrator-module attribute access beyond the four stable names. ADR 0042 Phase J cleanup not regressed.
6. **`finalize_project_run` regression** — any new stdout/stderr inside the silent service. Phase G of ADR 0042's guard stays green.
7. **`SILENT` + `no_interactive=False` accepted** — `__post_init__` must reject. If a code path legitimately needs SILENT with interactive prompts, the policy is wrong — stop and redesign.
8. **`ProjectRunDeps` reintroduction** — if a phase needs more than a single enum to plumb policy, that's a signal for a *separate* ADR.
9. **`log_phase` gating** — `log_phase` is a file/event writer, not a stdout site. If a phase needs to gate it, the file writer is leaking to stdout — fix that, not the gate.
10. **Field-parity contract drift** — `_REQUEST_ONLY_FIELDS = {"presentation"}`. New request-only fields join the allowlist with justification, not silent expansion.
11. **Inventory drift** — Phase C pre-flight `rg` scan covers the six silent-path modules + `project_testing.py`, **excludes `pipeline/project/cli.py`** (terminal-by-definition leaf). Every match maps to an inventory row + gated branch; new sites add a row + gate in the same commit.
12. **Enum coercion regression** — `ProjectRunRequest(presentation="silent", …)` must yield `request.presentation is PresentationPolicy.SILENT`. Phase B's tests cover this.

## Verification matrix

| Phase | Command                                                                                                  | Pass criterion |
|-------|----------------------------------------------------------------------------------------------------------|----------------|
| A     | `ruff check . && git diff --check`                                                                       | clean; ADR file present |
| B     | `pytest tests/unit/pipeline/test_project_run_request.py -vv`                                             | existing + `TestPresentationPolicy` green |
| B     | `python -c "from pipeline.project import PresentationPolicy; PresentationPolicy.SILENT"`                  | resolves |
| C     | `pytest tests/unit/pipeline/project/test_silent_policy_threading.py -vv`                                 | 5 focused tests green (incl. SILENT-stdout-empty + log_phase still called START/END pair guard) |
| C     | `pytest tests/unit/pipeline/project/ tests/unit/pipeline/test_finalization_silent.py tests/unit/pipeline/test_handoff_isolation.py -q` | green |
| C     | inventory scan (six silent-path modules + `project_testing.py`, excludes `cli.py`)                       | every match maps to an inventory row + gated branch |
| D     | `pytest tests/integration/cross/test_cross_silent_dispatch.py -vv`                                       | cross transcript has `▶ SUB-PIPELINE` but no per-project DONE / Session / Usage / Handoff lines |
| D     | `pytest tests/unit/pipeline/cross_project/ tests/integration/cross/ -q`                                  | full cross suite green |
| F     | `pytest tests/unit/pipeline/project/test_silent_boundary.py -vv`                                         | 5 new tests pass |
| F     | `pytest -k 'signature_matches_pinned_reference' -q`                                                      | `run_pipeline` AND `run_project_pipeline` signature locks pass |
| F     | full `pytest` minus known pty-exhausted sandbox tests                                                    | green |
| G     | `grep 'Status: Accepted' docs/adr/0046-silent-app-level-boundary.md`                                     | match |
| G     | per-phase rows                                                                                            | all `Shipped` with commit hashes |

## Consequences

**Wins.**
- Cross-project transcript stops carrying per-project banners. The cross-level `▶ SUB-PIPELINE [alias]` separator becomes the canonical per-alias delimiter rather than competing with child DONE blocks.
- Library callers (future direct-library UI, integration tests, MCP if it ever stops subprocess-spawning) can call `run_project_pipeline(request)` and get a structured run with no terminal pollution.
- `ProjectRunDeps` retirement (ADR 0042 Phase J) is preserved — policy lives on the request until a real deps contract justifies a second parameter.

**Trade-offs.**
- Phase C touches six files in the silent-path inventory. Each gate is one-line + a comment, but the surface is wide. Phase C's pre-flight rg scan + the literal-grep guard in Phase F catch drift; the cost is one extra test that grovels source.
- `ProjectRunRequest.__post_init__` now has runtime logic on every construction. The coerce + reject is O(1) and runs once per request; negligible.
- The frozen-dataclass `object.__setattr__` coercion is mildly unusual but standard for frozen dataclasses that need normalisation. Phase B's tests pin the behaviour.

**Follow-up unlocked by this ADR.**
- **orcho-web subprocess hardening (the next concrete migration this ADR unlocks).** `orcho-web/services/runner.py` currently spawns `python -u -m cli.orcho run …` and parses stdout line-by-line for phase banners + run_dir — a fragile contract that re-breaks every time the CLI transcript shape shifts. With Phase D shipped (`fb17300`), the typed silent boundary is available to any library caller; orcho-web has two clean options:
   1. **Library-side, recommended.** Drop the subprocess; `pip install orcho-core` into the orcho-web venv and call `run_project_pipeline(ProjectRunRequest(..., presentation=SILENT, no_interactive=True))` directly. Read state from the persisted `events.jsonl` + `meta.json` instead of stdout. Eliminates a process boundary, kills the parser, gains structured exceptions for free.
   2. **Subprocess-retained.** Keep the spawn for isolation (separate venv, separate failure mode) but stop tail-following stdout. Tail-follow `events.jsonl` instead — same per-phase events, plus `phase_kind` / `attempt` / `round` context the stdout banners never carried.
   Own ADR scoped to orcho-web. Out of scope here: that ADR makes the call between options 1 and 2 based on orcho-web's concrete constraints (venv layout, Streamlit reload semantics, error-surfacing UX). The breadcrumb here exists so the future orcho-web ADR can reference `0046#follow-up` rather than re-explain the boundary.
- **Full cross-project app boundary.** Phase D here does the minimum cross migration — switching the per-alias library call to use SILENT. The full cross story (typed `CrossRunRequest` / `CrossRunResult`, cross-level presentation policy, cross's own argv/print surface) is a separate ADR.
- **`app.py` axis-split.** The 1202-LoC `_run_project_pipeline_session` body is the next refactor target after cross. Split along genuine seams: `--from-run-plan` projection, resume-meta hydration, pre-run-dirty + worktree setup, dispatch + finalize coordination.

## Reused existing code

- `pipeline.project.finalization.finalize_project_run` — already silent (ADR 0042 Phase G locked).
- `pipeline.project.finalization.finalize_with_terminal_output` — already the terminal wrapper.
- `pipeline.project.handoff.process_pending_phase_handoffs` — already routes to non-interactive branch under `no_interactive=True`.
- `pipeline.control.handoff_prompt.should_prompt_for_phase_handoff` — TTY + `no_interactive` AND-gate; unchanged.
- `pipeline.project.run._PipelineRun._dispatch_active` — per-run dispatch-state flag; unchanged, kept distinct from `_presentation`.
- `core.observability.logging.log_phase` — file + event writer; unchanged, not gated by policy.

## Out of scope (deferred)

- Cross-project boundary (own ADR after 0046).
- `pipeline/project/app.py` axis-split (own ADR after cross).
- orcho-web subprocess + stdout-parsing migration (own ADR scoped to orcho-web).
- `EVENT_ONLY` / advanced policy tiers (defer until a real consumer asks for it).
- `ProjectRunDeps` reintroduction (defer until a real deps contract — multiple fields, not just one enum — justifies it).
