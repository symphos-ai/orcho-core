# Changelog

## Unreleased

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
