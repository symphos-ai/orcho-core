# Changelog

## Unreleased

## 0.7.0 - 2026-08-11

This release adds a safe, report-first workspace cleanup surface, makes
verification timeouts declarable and typed, and tightens run-control honesty:
a gate is only advertised as decidable when it can actually be decided, and
operator retry feedback reaches the agent that redoes the work.

### Added

- Workspace cleanup: a report-first `workspace cleanup` command previews what
  would be reclaimed — separating reclaimable checkouts from work that is
  protected as unrecoverable (uncommitted changes, unpushed commits, runs that
  may still resume) and from inert references — then archives or deletes only
  on explicit reclaim. Run roots have their own retention model, and
  `--force` (valid only with an explicit `--older-than` cutoff) can reclaim
  abandoned old runs past value protections while structural invariants (live
  runs, shared checkouts, unreadable state) are never overridden.
- `sdk.cleanup`: a typed public report and reclaim surface over workspace
  cleanup; the bundled CLI consumes the same projection so both render one
  shape.
- Verification commands accept a declarable per-command `timeout` (seconds);
  an invalid verification contract now fails as a typed configuration error
  naming the plugin file and the legal vocabulary instead of a traceback.
- Command receipts record a typed execution outcome (`completed`, `timeout`,
  `error`, `empty`), and timeout is a first-class failure kind: a required
  gate that ran out of budget stays blocking, burns no repair rounds, and
  pauses for the operator with the actual lever named.
- Phase handoffs carry a durable UTC timestamp, and `RunStatus` exposes run
  spend and the last-event position so status clients avoid separate metrics
  or event-history reads.
- Onboarding can connect an existing project in place, and a deterministic
  mock review loop supports reproducible false-ready harness scenarios.

### Changed

- A deferred delivery or correction gate on a stopped run is published as
  durable context, not a decision surface: `delivery_decision_state` and
  `decide_delivery` share one lifecycle predicate, so a client is never told
  "decidable" by one surface and refused by the other.
- Public SDK timestamps are normalized at the projection boundary and
  published offset-aware; malformed stamps degrade to `None` instead of
  guessing.
- Documentation: existing-project quick start, shared product workspace
  activation, plugin setup as core onboarding, and quality-gate strategy
  guidance.

### Fixed

- Operator retry feedback at a phase handoff reaches the agent that redoes
  the work instead of being dropped on the floor.
- Fast gates rerun after repair, so a repaired run cannot ship on stale fast
  proof.

## 0.6.0 - 2026-07-28

This release strengthens cross-project recovery, makes verification cost a
typed scheduling signal, and normalizes generated delivery messages.

### Added

- Verification commands expose `fast`, `moderate`, `slow`, and `unknown` cost
  metadata across plugin contracts, prompts, workspace scaffolds, and public
  documentation.
- Cross-project evidence includes canonical plan and execution-state details
  needed by durable readers.

### Changed

- The former boolean `cheap` verification vocabulary is replaced by the typed
  `cost` contract.
- Generated commit subjects follow one normalized authorship and release
  summary contract.
- Scheduled-verification onboarding explains cost-aware targeted feedback
  without transferring engine-owned gates into implementation tasks.

### Fixed

- Cross-project resumes re-arm final acceptance after resumable child handoffs
  and redispatch interrupted same-run children exactly once.
- Parent cross-runs settle truthfully when a provider fails, and required gate
  declarations are persisted before a run becomes publicly observable.
- Verification subprocesses receive the correct run isolation identity.
- Hidden unavailable interface commands no longer recommend packages that are
  not published.

### Security

- Release-path GitHub Actions use immutable pins, CodeQL runs for protected
  release branches, and dependency updates preserve the reviewed workflow
  lifecycle.

## 0.5.1 - 2026-07-26

Stabilization release for the 0.5 line, driven by independent black-box
release qualification of the installed CLI and MCP surfaces. No breaking
contract changes.

### Added

- Workspaces support a shared, committable `.orcho/config.json` layer
  alongside the personal `config.local.json` overlay, so teams can share
  policy such as delivery publishing defaults.
- Generated workspace scaffolds include guidance on verification ownership.

### Fixed

- Resuming an unattended verification-gate halt no longer crashes: the gate
  context is re-armed safely, and stale gate retries park again with a typed
  refusal instead of offering an unexecutable repair step.
- Verification gate progress and retry attempts are presented clearly, and
  workspace gate policy is chosen from the project's verification evidence.
- Cross-project resumes preserve the recorded provider intent: the durable
  provider mode is inherited by default, and resumes fail closed with a
  typed error when the persisted mode is missing instead of silently
  switching providers.
- Engine-owned verification gates no longer leak into implement-phase
  acceptance criteria during planning.
- Delivery validates publish results before reporting them and clearly
  surfaces degraded publish readiness in the run summary.
- Isolation identifiers are scoped to the project run lifecycle.

## 0.5.0 - 2026-07-23

### Added

- Scheduled verification is represented by a durable ledger with explicit
  selection, execution, consequence, and disposition evidence.
- Verification receipts bind results to immutable subject identities and keep
  automatic rerun attempts as distinct durable evidence.
- Provider-owned long commands have a managed lifecycle with run-scoped
  receipts and duplicate-execution protection.
- Cross-project runs persist an execution graph and reduce canonical parent
  state from their child pipelines and cross gates.
- The public SDK exposes canonical continuation preflight, run diagnosis,
  managed-command evidence, and cross-execution state.

### Changed

- Verification ownership is explicit: implementation prompts favor targeted,
  cost-aware checks while the engine owns scheduled broad gates and repair
  routing.
- Pre-final gate selection and delivery readiness use the same authoritative
  scheduled identities.
- Scope expansion in `pro` mode is advisory; governed runs retain the
  decision-gated behavior.
- Skill discovery defaults to local project and workspace scopes instead of
  injecting unrelated global skill context.
- Cross-project resume reuses durable child and gate state rather than
  manufacturing completion from stale parent snapshots.

### Fixed

- Diagnose and resume now agree on checkpoint readiness: an interruption inside
  an unfinished phase uses a persisted plan continuation instead of advertising
  a same-run resume that preflight will reject.
- Verification repair preserves retry context, refreshes stale failed receipts,
  and reports automatic reruns without overwriting the first attempt.
- Provider retries wait for the owned child process to settle and cannot launch
  duplicate heavy commands with the same identity.
- Correction follow-ups retain their worktree and use ordinary follow-up
  semantics rather than plan-only continuation.
- Exact declared write scope survives planning, cross handoffs, and final
  acceptance without false expansion findings.
- Delivery output distinguishes committed, published, retained, and rejected
  outcomes and exposes the published commit identity.
- Resumed cross delivery does not rerun a completed cross-final-acceptance gate
  or print a false phase banner.
- The advertised `output.log` exists even when a resume completes entirely from
  cached durable state.
- Test isolation and verification-subject fixtures keep the full suite stable
  without repeated Git snapshot work in unit hot paths.

### Documentation

- Added architecture decisions for scheduled-gate lifecycle, verification
  ownership, verification subject continuity, canonical continuation, and
  cross-project parent-state reduction.
- Documented task-authoring rules that keep broad quality gates engine-owned.

## 0.4.0 - 2026-07-08

### Added

- `claude-glm` agent runtime, including an installable wrapper with a Windows
  `.cmd` variant so `orcho runtimes install claude-glm` provisions a working
  wrapper on Windows.
- `orcho profile customize` command for tailoring one execution profile,
  backed by a public SDK customization surface.
- `orcho demos bootstrap` command that creates a disposable packaged demo
  workspace for a first guided run.
- Quality-gates verification matrix inspector: the declared gate matrix is
  exposed as a read-only CLI command, and the run header uses the same
  formatter so banner output and operator inspection stay aligned.
- Default CLI evidence view plus a full evidence dossier view covering the
  plan contract, phase timeline, implementation receipts, and acceptance
  verdicts.
- Evidence findings carry lifecycle statuses.
- Public SDK surface for reading profile catalogue metadata.

### Changed

- The run diff command defaults to a preview render.
- Cost reports read as usage accounting: breakdowns are attributed to the
  recorded runtime (wrapper runtimes and resume overrides preserved), child
  pipeline usage is separated from phase summaries, workspace project
  breakdowns are reported, and cost-reference wording is clarified.
- Run status summaries are clearer: quality gate summary, metrics-based usage
  with phase attempts, workspace accounting honored, and colored cost output.
- Metrics CLI output is easier to read.
- Inspection command UX is polished and command roles are documented.

### Fixed

- Scope expansion never miscategorises a test module as a genuine-safety
  change, and test modules are recognised across ecosystems (Go, JS/TS,
  JVM/.NET, Ruby, Rust, and more), not only Python.
- Non-fatal delivery warnings are coloured yellow so they read as warnings
  instead of neutral text.
- Long evidence artifact paths are no longer clipped, so they stay copyable.

### Documentation

- Windows and Linux pipx install steps work as written: pipx is bootstrapped
  through the interpreter where needed, with an explicit shell-reopen step.

## 0.3.0 - 2026-07-06

### Added

- Native Windows support: a cross-platform stream transport lets `orcho run`
  drive agents on Windows, and stdio is forced to UTF-8 so runs no longer crash
  on Windows consoles.
- The delivery outcome is framed as a prominent terminal banner, making the
  change journey obvious at the end of a run.
- A progress banner is shown while verification gates run.
- Resume summarizes the completed phases it skips, so a resumed run reads
  clearly instead of appearing to start mid-pipeline.
- Public detached-launch SDK surface for embedders that start runs out of band.
- `orcho tui` subcommand that delegates to the optional `orcho-tui` package.
- Operating-modes reference matrix (fast / pro / governed).

### Changed

- The feature profile now defaults to the `pro` operating mode.
- The `orcho run` profile picker defaults to auto-detect.
- The interactive delivery gate now defaults to `approve` (Enter commits),
  matching the non-interactive default.
- The delivery decision and its SDK projection carry a typed `pr_url`.
- Operator-facing output and published artifact language are kept separate, so
  terminal messaging never leaks into committed artifacts.
- Getting-started leads the first run with a free `--mock` dry run.

### Fixed

- The no-PR delivery banner is coloured yellow, not green, and default-branch
  protection notices are clarified.
- Run output accounts for ledger gate activation.
- Unrecognized agent failures now produce an actionable message.
- `web` and `tui` are hidden from CLI help until their packages ship.
- Cross-project delivery authors its commit message in the configured content
  language and no longer inherits the mono release-gate policy.
- The full plan-task rollup is counted across resumes.
- The pre-run-dirty checkpoint commit is signed off for DCO.

### Documentation

- Position Orcho as a production harness across the README and reference docs.
- Separate install instructions by OS and document the DCO sign-off rule for
  direct commits.

## 0.2.0 - 2026-07-05

### Added

- `--version` flag on the `orcho` CLI.
- Delivery can publish to a hosted Git provider: it pushes the delivery branch
  and opens a pull request (ADR 0121).
- Branch-policy delivery: runs never auto-commit to the default branch and
  route changes through a dedicated delivery branch (ADR 0119).
- Isolation-first agent launch envelope with a native/bypass execution knob and
  a preflight receipt (ADR 0122).
- Delivery recommends installing `gh` when the target project is GitHub-hosted.

### Changed

- Terminal output is compressed into a verifiable summary arc.
- Delivery commits are signed off to satisfy DCO contribution checks.

### Fixed

- Classify signal-based agent terminations so provider-failure recovery reacts
  correctly.
- Strip run-scoped environment channels from verification-gate subprocesses.
- Avoid unattended handoff deadlocks in project runs.
- Windows: correct virtualenv path, metrics encoding, and mock project name.
- Render the cross-project final-acceptance verdict structurally rather than as
  raw markdown.

## 0.1.0 - 2026-07-01

Initial release baseline for `orcho-core`.

### Added

- Local-first pipeline engine for planning, implementation, review, repair, and final acceptance workflows.
- CLI entry points for project runs and cross-project orchestration.
- Extension point groups for agent runtimes, phase handlers, and third-party skill packages.
- Run evidence, observability, prompt rendering, profile loading, and SDK surfaces for downstream tools.

### Known Notes

- This release establishes the first public package baseline and API line.
- The package is in alpha; public contracts should still be treated as early and evolving within the `0.1.x` line.
