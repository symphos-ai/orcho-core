# Changelog

## Unreleased

### Fixed
- Detached launches no longer inherit the launcher's stdin. A run spawned by
  an embedder whose own stdin is a live transport channel could block inside
  CPython's interpreter startup, while it built `sys.stdin` and probed that
  inherited handle — before executing a single line of Python. The run
  produced no events, no log bytes and no `meta.json`, so nothing downstream
  could name it. `stdin` is now pinned to `DEVNULL` at the single detached
  spawn site, matching what bounded service commands already did.

## 0.8.3 - 2026-08-23

Closes the startup-hang family reported from a native-Windows field host
(#250): a run could block forever before its first phase, nothing could stop
it, and every observation surface reported it as healthy.

### Fixed
- Service commands are bounded on every platform. A declared timeout now
  terminates the whole process tree and reaps under a separate budget, so a
  descendant holding inherited pipes (Git for Windows spawns the real
  `git.exe` as a grandchild) can no longer keep a run blocked indefinitely.
  Previously the post-timeout reap had no deadline at all.
- Run cancellation works on Windows. Detached launches record their process
  tree, and cancellation reaches it through a platform adapter instead of the
  POSIX-only `os.killpg` / `SIGKILL` pair, which simply did not exist there.
  A graceful cancel asks a `CREATE_NEW_PROCESS_GROUP` tree to stop and reports
  the mode it actually delivered rather than claiming a graceful stop.
- Liveness probing no longer terminates the process it inspects. On Windows
  `os.kill(pid, 0)` calls `TerminateProcess`; the probe now reads process
  state instead.
- A cancelled run stops claiming it is running. A SIGTERM / SIGBREAK handler
  persists the interrupted terminal state and emits `run.interrupted`; the
  previous atexit-only safety net never ran on signal termination, leaving
  `meta.json` at `status: running` forever.
- `has_uncommitted` and `git_diff_stat` raise instead of reporting a clean
  tree when git cannot be consulted, and `meta.json` is replaced atomically,
  so no reader can observe a truncated or misleading run state.

### Added
- A startup watchdog halts a run that has emitted nothing beyond `run.start`
  and grown no `output.log` inside a configurable budget, with a typed reason
  naming the service command in flight — "blocked in `git status` in <dir>"
  rather than an indefinite `running`.
- `run_diagnosis` gains a `stalled` condition, returned ahead of `active`,
  derived from durable artifacts only and recommending inspect-or-cancel.

### Known Notes
- Windows coverage for the timeout and cancel paths runs in the dedicated
  `windows-smoke` CI job.

## 0.8.2 - 2026-08-20

Closes the remaining finding from the native-Windows field report: the
per-phase effort knob on profiles now actually steers the run.

### Fixed
- A phase `effort` declared by the active profile — including `profiles_v2`
  overlays written by `orcho profile customize --phase-effort` — reaches
  agent dispatch. Previously the overlay was written, validated, and
  reported as a change, but slot construction read only the global
  `phases.<phase>.effort` config, so the run silently kept the global
  value (and the run header displayed it). The profile declaration now
  wins for its phase; the global config remains the default for phases
  the profile leaves silent, and the run header shows the effective
  value.

## 0.8.1 - 2026-08-20

This release removes the Windows command-line ceiling on composed phase
prompts. 0.8.0 made the failure explicit; 0.8.1 makes it not happen: the
prompt no longer travels through argv at all.

### Changed
- Built-in runtimes (`claude`, `claude-glm`, `codex`, `gemini`) now deliver
  the composed phase prompt to the agent process over stdin; argv carries
  only flags (ADR 0178). Field-measured plans of 40-61 kB — previously
  unlaunchable as `validate_plan` input on Windows because of the 32,767-char
  `CreateProcessW` limit — now run on every platform. Which runtime authors
  the plan no longer decides whether the next phase can start.
- The prompt is written from a dedicated writer thread with explicit EOF, so
  payloads larger than the OS pipe buffer cannot deadlock the child process.
- Prompts no longer appear in process-argument listings, so process
  inspection tools on shared hosts no longer see prompt contents.

### Known Notes
- Third-party runtime adapters keep argv delivery by default and remain
  covered by the 0.8.0 pre-spawn command-line guard; adapters can opt into
  stdin delivery via the new invocation surface documented in the agent
  contracts guide.

## 0.8.0 - 2026-08-20

This release makes Orcho usable on native Windows. A first-time onboarding
report on a non-UTF-8 Windows host found five blockers; four are fixed here,
and the fifth now fails fast with a diagnostic that names its own cause. The
`claude-glm` runtime also stops routing through a shell wrapper, which is a
breaking change to how that runtime is set up.

### Changed

- **BREAKING**: the `claude-glm` runtime launches the installed plain `claude`
  executable with an adapter-owned GLM environment. The packaged `claude-glm`
  wrapper scripts and the `orcho runtimes install` surface that installed them
  are gone. Model ids are configuration rather than baked into a script, and
  default to `glm-5.3` (opus/sonnet) and `glm-4.7` (haiku). See the migration
  note below.
- Child process stderr is drained continuously while the agent runs, instead
  of being read only after the child exits. A child that filled its stderr
  pipe previously blocked mid-phase and was misreported as a silent, stalled
  agent. Retention is bounded, and a truncated payload says how many bytes it
  dropped.
- Stalled-command evidence records bytes read per stream, so a child that is
  thinking and a child that is blocked on a full pipe no longer look identical
  to an operator.

### Fixed

- Git output is decoded as UTF-8 rather than with the process locale.
  Verification and delivery probes crashed on any repository containing
  non-ASCII pathnames when the console codepage was not UTF-8, and a probe
  whose capture died now reports a failure instead of raising.
- The sandbox no longer strips `ANTHROPIC_AUTH_TOKEN` or `CLAUDE_GLM_BIN`, the
  variables the shipped GLM runtime path requires. Stripping the token
  degraded the child into a fallback session that the remote endpoint rejected
  minutes later as an authentication error.
- An agent command line that exceeds the Windows process-creation limit fails
  immediately, naming the composed length, the applicable limit, and the
  argv-borne prompt as the cause, instead of dying with an unrelated
  "filename or extension is too long" error.
- The Microsoft Store `python` alias is never reported as a usable
  interpreter. It runs virtualized and cannot see Orcho's own workspace tree,
  so verification receipts recorded a passing environment that could not work.

### Migration

`claude-glm` operators: remove any installed `claude-glm` wrapper from `PATH`
and drop `orcho runtimes install claude-glm` from setup scripts. Keep
`ANTHROPIC_AUTH_TOKEN` in the environment as before; set `CLAUDE_GLM_BIN` only
to point at a specific `claude` executable. Override model ids through
configuration if the defaults above are not what your plan serves.

### Known limitations

- On Windows, composed phase prompts still ride argv, so a very large plan
  (roughly 32k characters, which a non-Latin plan reaches at about half that
  byte size) cannot be spawned. This is now a fast, self-explaining failure
  rather than an obscure one; out-of-band prompt delivery is a separate change.

## 0.7.0 - 2026-08-11

This release makes stalled verification commands a typed, recoverable
outcome, keeps delivery decidability honest, and adds retention-aware
workspace cleanup.

### Added

- Verification commands can declare a per-command `timeout` budget, and an
  invalid declaration fails with a typed contract error instead of being
  silently ignored.
- A verification command that exceeds its budget produces a typed `timeout`
  command outcome and failure kind, so repair and gate policy can react to it
  like any other classified failure.
- Workspace cleanup reports and reclaims retained run worktrees and old run
  roots. Retention protects unrecoverable work — dirty checkouts, unpushed
  branches, and resumable handoffs — while an explicit `--force` path reclaims
  abandoned old runs. The report and reclaim surfaces are also part of the
  typed public SDK.
- Run status exposes recorded spend and the last-event position, so external
  watchers can judge liveness without tailing raw events.
- Operator pauses carry durable requested-at timestamps.
- Existing projects can be connected to a managed workspace in place, without
  restructuring the checkout, and the quick-start documentation walks that
  path end to end.
- A deterministic mock review loop and documented mock harness reproduce
  false-ready review behavior without model cost.

### Changed

- A delivery gate is decidable only when a decision can actually be taken
  now: stopped runs no longer project decidable gates, and retained stopped
  gates keep their kind and a resume-first reason.
- Fast verification gates rerun eagerly after repair instead of waiting for
  the next slow pass.
- Public SDK timestamps are timezone-unambiguous.
- Plugin setup and project-owned quality gates are documented as core
  onboarding, with worked gate-strategy examples.

### Fixed

- Operator feedback given when retrying a paused handoff reaches the agent
  that redoes the work instead of being dropped.
- The workspace cleanup boundary guard is scoped to files this repository
  tracks.

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
