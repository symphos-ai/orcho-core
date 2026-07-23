# orcho-core Instructions

## Scope

This file applies to `orcho-core/`.

`orcho-core` is the Apache-2.0 public pipeline engine. It owns phases, agent
runtimes, registries, profiles, DAG execution, evidence, and CLI entry points.
It must remain independent from every other package in the workspace.

Also obey the workspace-level `../AGENTS.md`.

## Workspace Development Pipeline

When working on this repo inside the Orcho workspace, follow
`../DEVELOPMENT_PIPELINE.md`. That manual pipeline governs direct source
development only; it is separate from Orcho-managed worktree runs.

## Stable Install Is Read-Only

Stable Orcho is a pipx install (venv at `$HOME/.local/pipx/venvs/orcho`,
shims in `$HOME/.local/bin`). Do not edit files inside that venv directly.
Change the canonical workspace repos and promote with `orcho-promote`; touch
the stable install only when the user explicitly asks to debug or repair it.

## Build, Run, And Test

```bash
pip install -e ".[dev]"
pytest                              # final readiness, not the inner loop
pytest -k <name>                    # targeted test selection
pytest -q -m "not e2e and not packaging"  # broad non-scenario gate
ruff check .
orcho ...
orcho-run ...
orcho-cross ...
```

Use `--mock` for tests or smoke runs that need a real subprocess but not a real
model. It swaps the agent provider for `MockAgentProvider`, avoiding LLM cost
and keeping the pipeline fast.

Run focused tests for narrow changes. Run the broader suite when touching shared
contracts, schemas, CLI behavior, phase orchestration, or cross-repo interfaces.
For `orcho-core`, cost markers (`e2e`, `slow_process`, `git_worktree`,
`filesystem_heavy`) explain why tests are expensive; `serial` means
xdist-unsafe. Pick one relevant slice instead of running every marker. Routine
escalation is targeted path/marker -> `pytest -q -m "not e2e and not packaging"`
-> final `pytest -q`; do not run the broad non-e2e gate immediately before the
full suite unless you expect it to fail faster and guide a fix.

## Public Plugin API

The public plugin API is defined by entry-point groups in `pyproject.toml`:

- `orcho.agent_runtimes` registers agent backends. Built-ins include Claude,
  Codex, and Gemini.
- `orcho.phases` registers phase handlers. Built-ins include `plan`,
  `validate_plan`, `implement`, `review_changes`, `repair_changes`,
  `final_acceptance`, and `compliance_check`.
- `orcho.skills` registers `SkillPackage` implementations. There are no
  built-ins; this group is a pure third-party surface.

Re-registration overrides built-ins by name. Document this generically as an
extension mechanism for third-party packages.

Core owns extension protocols, not provider behavior. Keep entry-point
contracts, schemas, lifecycle decisions, and durable artifacts in `orcho-core`;
put provider-specific binary detection, authentication, API commands, retries,
and error translation in the registered runtime, phase, skill, or delivery
driver package. In short: `orcho-core` owns the protocol; plugins own provider
behavior.

Document extension points generically as third-party mechanisms.

## Project Rules

### Architecture Fitness Gate

Green tests are necessary, not sufficient. Before editing orchestration,
runtime, phase, app-boundary, CLI, or cross-project code, inspect whether the
target module/function is already overloaded.

Hard rules:

- Do not add a new responsibility to a function over roughly 150 lines or a
  module over roughly 700 lines unless the change is explicitly an extraction
  step.
- If the target is already overloaded, classify the change before editing:
  bug fix inside the existing responsibility, extraction of an existing
  responsibility, or new responsibility. New responsibilities must go to a
  focused module, not into the large body.
- Public facade modules may expose stable public entry points, but internal
  helper logic should live in its real module. Tests that need internals should
  import those internals from their real home.
- Prefer small typed context objects for resolved orchestration state over
  long local-variable trains.

### Project And Cross App Layering

`pipeline/project/app.py` is the typed project entry surface. It should own
`run_project_pipeline`, the public `run_pipeline` wrapper, request/result
adaptation, and only small routing glue. Do not add lifecycle work directly to
this file when a focused internal module can own it.

Expected project homes:

- `pipeline/project/profile_setup.py` — profile resolution, projection, and
  session-split override application.
- `pipeline/project/runtime_setup.py` — provider selection, phase models,
  phase config, agent registry, and follow-up/checkpoint session seeds.
- `pipeline/project/isolation_setup.py` — worktree selection, pre-run dirty
  intake, sandbox policy, and related ContextVar setup.
- `pipeline/project/state_setup.py` — `PipelineState` construction, extras,
  attachments, and from-run-plan hydration.
- `pipeline/project/run.py` — the per-run execution object and phase-end
  bookkeeping.

`pipeline/cross_project/app.py` is the typed cross-project entry surface. It
should own `run_cross_project_pipeline`, request/result adaptation, and small
routing glue. Cross profile projection, run setup, agent setup, contract
checks, child dispatch, and finalization belong in focused modules.

Expected cross-project homes:

- `pipeline/cross_project/profile_setup.py` — cross profile projection,
  child-profile construction, and cross gate policy lookup.
- `pipeline/cross_project/run_setup.py` — run id, logging, run.start event,
  session/checkpoint setup, and presentation renderers.
- `pipeline/cross_project/agent_setup.py` — cross-level runtime/model
  selection and display metadata.
- `pipeline/cross_project/contract_check.py` — contract-check decisions,
  cached resume handling, artifact-bundle review, per-alias review, parse
  failure shaping, and usage accumulation.
- Existing focused modules such as `planning_loop.py`, `project_dispatch.py`,
  `cfa_gate.py`, and `final_acceptance.py` should stay the owners of their
  current domains; do not re-inline their behavior into `app.py`.

### Runtime Construction

Runtime agent constructors must be side-effect free. Constructing a runtime
object, building `PhaseAgentConfig`, listing profiles, rendering help, or
running `dry_run=True` must not require local CLI binaries. Resolve external
agent binaries lazily at the first real invocation, and preserve `agent.bin`
as a settable `str` property for tests and third-party runtime adapters.

### No Backcompat Ceremony

This is a single-developer project with no production install base for internal
plumbing. Do not add feature flags, parallel legacy paths, dual-path migrations,
or compatibility wrappers for internal changes. Refactor in place and cut legacy
code in the same change.

External API surfaces, such as PyPI-published contracts, still need real
backcompat. When unsure, distinguish user-facing API from internal plumbing.

### Documentation Discipline

Large-scope work ships documentation deliverables in the same change as code.
Order docs from general overview to API/schema reference, authoring guide, and
edge cases.

Per-phase docs belong in `docs/`, not workspace artifacts. A phase is complete
only when code/tests are green, the ADR is written when needed, and schema/API
docs are sufficient for the next phase to start.

Full authoring guides may remain TODO while the surface stabilizes. Mark them
with `<!-- TODO(orcho-phase-X): expand -->`.

### Phase Naming

Pipeline phases use granular versioned names such as `5`, `5c`, `5e`, `5e5`,
`7c`, `7d`, and `7e`. Preserve the existing naming scheme.

### DCO Sign-Off On Direct Commits

This public repo enforces DCO (a `Signed-off-by:` trailer matching the
committer identity) on every commit in a PR's `base..HEAD`. Orcho's own
delivery engine (`resolve_commit_delivery`, mono and cross) already signs its
commits automatically — no action needed there.

When an agent or operator commits directly to this repo outside an Orcho run
(hand-authored fixes, PR branches, hotfixes), **always use `git commit -s`**
(or an equivalent trailer). Forgetting this is a recurring, entirely
avoidable failure mode: the PR goes red on the `signoff` check and the commit
has to be amended and force-pushed after the fact. Add the sign-off up front
instead of reacting to the red check.

### ADR Discipline

Protocol-level changes need an ADR in `docs/adr/` using the next free number.
Reference the ADR from the commit and from reshaped documentation. ADRs are
append-only; supersede with a new ADR rather than editing history.

### MCP Validation

Wire-format changes in runtime schemas, profile shape, mode flags, or gate
primitives must ship with matching `orcho-mcp` updates and an E2E mock smoke in
the same commit. Do not defer MCP alignment to the end of a milestone.

### Local Instruction Navigator

- `core/io/AGENTS.md` — ANSI color, stdout/stderr rendering, terminal
  prompts, and TTY-related helpers.
- `core/_prompts/AGENTS.md` — shipped prompt parts under roles, tasks,
  and formats.
- `pipeline/prompts/AGENTS.md` — prompt composer, contracts, templates,
  typed parts, and prompt runtime helpers.

## Pointers

- Architecture overview: `docs/architecture/overview.md`
- Phase lifecycle: `docs/architecture/phase_lifecycle.md`
- Agent contracts: `docs/creator/03_agent_contracts.md`
- Package structure: `docs/creator/02_package_structure.md`
- Prompt parts: `core/_prompts/README.md`
- Dev workflow: `docs/creator/09_dev_workflow.md`
