# Changelog

## Unreleased

### Fixed

- A plan whose acceptance criterion names a gate the project does not declare
  no longer ends the run. The reference is resolved at plan review, next to the
  existing verification-ownership check, and an unresolvable one is a
  synthesized `REJECTED` verdict carrying the declared identities, so the
  planner fixes it on the next planning round. It previously raised inside the
  plan phase and halted before implement, spending a full planning round and
  the run on a fixable naming mistake: a first dogfood run died on
  `C1 references gate 'python -m ruff check .'`, where the contract declares
  that gate as `lint`. The resolution stays fail-closed and still happens
  before implement.


### Added

- `orcho update` upgrades the CLI through the package manager that installed
  it. Orcho ships as an ordinary Python distribution, so the correct upgrade
  command depends on the installer that owns the environment; the command
  resolves that from on-disk evidence (pipx venv metadata, a `uv tool`
  receipt, virtualenv layout, PEP 610 `direct_url.json`) and delegates.
  A pip install is upgraded with its own interpreter, never with whatever
  `python` is first on `PATH`. Source checkouts, editable installs, a missing
  manager binary, and installs built from a local path rather than an index
  are reported with the command rather than upgraded, because upgrading would
  discard or fight the code actually running. `--dry-run` reports only.

- `meta.json` records `versions`: every installed distribution whose name
  starts with `orcho`, mapped to its version, as seen by the interpreter that
  wrote the run (`orcho-core` always present). Until now a run artifact
  carried no record of which engine produced it, so a behaviour observed in a
  run could not be matched to a release. Cross-project parent runs carry the
  same key; golden session snapshots mask its value.

### Fixed

- The cross-run header no longer reports the repair budget as if it were the
  planning budget either. Companion to the mono header fix above, on the
  surface it left behind: `orcho cross --max-rounds 4` printed
  `rounds_per_project=4` and the transcript then bannered
  `CROSS-PLAN -- Round 1/2`, because the cross plan loop's budget is the
  projection's own `LoopStep.max_rounds` and `--max-rounds` never reached it
  (ADR 0031). The header now names the repair cap for the loop it caps
  (`repair_rounds_per_project=4`) and carries the planning budget on the
  `Plan source` row (`cross  (2 rounds)`). `find_cross_plan_loop` becomes the
  single owner of "which projected LoopStep is the plan loop": the run flow
  and the header both read it there, which they must, since the header is
  assembled before the run flow resolves its own step handles. Behaviour is
  unchanged.

- The run header no longer reports the repair budget as if it were the
  planning budget. The State line rendered a bare `rounds=<max_rounds>`
  immediately next to `plan=yes`, so an operator who passed `--max-rounds 4`
  read the planning budget as 4 and was then surprised when the run paused at
  `validate_plan automatic round 2/2`. `--max-rounds` caps only the
  implement/review/repair loop; the plan/validate_plan budget is the active
  profile's plan `LoopStep.max_rounds` and has no per-run override (ADR 0031
  rejected global round overrides). The line now names both budgets and reads
  `plan=yes  (2 rounds)  repair_rounds=4`, with the plan budget read off the
  resolved profile through the existing `find_plan_loop` owner and omitted
  entirely when the profile is unresolved. Behaviour is unchanged — this was
  a labelling defect, not a scheduling one.

- `orcho run --resume <run_id>` no longer resets the round budget either.
  The entry directly below covers the SDK launcher that *builds* a resume
  argv; the CLI an operator types is the other half. Both `orcho run
  --max-rounds` and the orchestrator's own `--max-rounds` carried an argparse
  `default=1`, so neither could tell "the operator did not pass the flag" from
  "the operator asked for one round": `orcho run` re-materialised
  `--max-rounds 1` on every
  resume, and the orchestrator then fed that into the run config and wrote it
  back over the run's persisted `checkpoints.db` `run_meta.config_json`. Both
  defaults are now `None`, and the resume resolves explicit flag → the budget
  persisted for the resumed run → 1, announcing an inherited value so a
  changed budget is never silent. An explicit `--max-rounds` on the resume
  command line still wins, including `--max-rounds 1` against a larger
  persisted budget — re-passing the flag is how an operator deliberately
  narrows the remaining loop. A run with nothing persisted, or with a
  degenerate recorded value, resumes exactly as before.
  `pipeline.control.resume_budget` is the single owner of that rule; the SDK
  launcher now reads through it too, so the two resume frontends cannot drift
  on what counts as "nothing to inherit". Follow-up runs (a *new* run) and
  fresh runs inherit nothing, as before.

- Resuming a run no longer discards the operator's `max_rounds` budget. A run
  started with `max_rounds=4` reached its first subprocess correctly, but the
  resume argv carried no `--max-rounds`, so the orchestrator's argparse default
  of 1 applied: the repair loop silently shrank to a single round, and the
  shrunken value was then written back over the run's persisted
  `checkpoints.db` `run_meta.config_json`, destroying the record of what was
  originally requested. `resume_run` now reads the budget back from that store
  and re-emits the flag, alongside the `mock` / `output_mode` / profile values
  a resume already inherited. A run with nothing persisted still omits the flag
  and keeps the previous behaviour. `pipeline.checkpoint.read_run_config` is
  the read-only probe behind this: it never creates a checkpoint store, so a
  launcher cannot fabricate one for a run that never wrote one.

- The unsafe-process-polling guardrail no longer re-flags a command from the
  stream records that merely echo it. Claude stream-json `system`
  `task_started` / `task_notification` lines repeat an issued Bash command in
  `description` / `summary`; the shared guard treated every line's raw text as
  a command candidate, so one `pkill -f` produced extra `agent.guardrail`
  warns and non-terminal `agent.command_stalled` events. A JSON record now
  contributes only its structured tool-use commands (Claude `Bash`, Gemini
  `run_shell_command`, and Codex `command_execution` `item.command`, which the
  guard previously matched only through the raw JSON text); raw text is a
  candidate only for non-JSON lines. The non-terminal `command_preview` keeps
  the tail of an over-long command so a trailing poll stays visible, and
  `elapsed_s` is documented as time since the agent subprocess spawned, not
  the command's own runtime.

- The destructive-git guardrail now covers the Codex runtime. `codex exec
  --json` streams each shell command as a `command_execution` JSON record,
  which never starts with `git `, so the shared guard's human-readable text
  path never saw it and a Codex `git reset --hard HEAD` streamed straight
  through while the runtime docstring assumed coverage. The guard now
  inspects the `item.started` / `item.completed` records directly, mirroring
  the Claude and Gemini structured paths; `worktree_cwd_path` relaxes it the
  same way. Codex emits those records only after launching the command, so
  the verdict is a run halt (abort, `agent.guardrail` diagnostic,
  `ORCHO_GUARDRAIL_BLOCKED` sentinel), not a prevention.

- `run_diagnosis` / `recovery_lineage` no longer recommend resuming a source
  run that the launch preflight would refuse. A terminal recovery child whose
  source had a finalized `scheduled_gate_ledger.json` (written at every
  runner-side `run.end`) was diagnosed `recover_via_source_run` / "resume the
  source", and `orcho run resume` then rejected exactly that with "same-run
  resume is blocked: parent has a finalized scheduled-gate ledger". Source
  resumability is now the canonical `preflight_continuation` answer (a
  paused or live source is refused the same way); when the source cannot be
  resumed in place but preflight accepts a `from_run_plan` launch off its
  persisted plan, the diagnosis recommends `plan_artifact_continuation` with
  the source as `recommended_run_id` — the exit operators were already using
  by hand. Source-candidate facts moved to `sdk/run_control/recovery_source.py`.

- A provider-side HTTP 5xx (`API Error: 529 Overloaded`, `500 Internal
  server error`, 502/503/504, `server_error`) now classifies as the transient
  `ApiConnectionError` and gets the bounded connection retry budget. It used
  to fall into the never-retried "unrecognized error" bucket and halt the run
  with a bare `exit=1`; the failure line now carries the provider's own
  status text.
- The rate-limit classifier no longer matches a bare `429`, which also occurs
  inside UUIDs and hashes in provider stream output and mis-typed a 500
  failure as a rate limit. Only anchored forms (`api error: 429`, `http 429`,
  `status 429`) count.
- A plan (or any assistant reply) larger than the 96 KiB per-line model
  output cap no longer halts the run with `plan rejected before implement:
  raw JSON parse failed: Extra data: line 2 column 1`. The stdout line cap
  was byte-middle-cutting every oversized line that was JSON but not a
  tool-result envelope, which destroyed the stream-json `assistant` and
  `result` events carrying the reply; the text extractor then skipped the
  malformed lines and the phase received raw NDJSON. Non-tool JSON lines now
  pass through unchanged; tool-result lines and non-JSON blobs keep the cap.
  As defense in depth, the Claude runtime now raises a typed
  `AgentCallError` naming the real cause when a stream-json reply carries no
  assistant text at all, instead of silently returning the raw stream.
- A worktree bootstrap that outlived `startup_stall_seconds` (a long
  `npm ci`, for example) completed successfully and was then halted as
  `startup_stalled` at the next checkpoint, because bootstrap steps emit no
  event and write no `output.log`, the watchdog's only progress signals. The
  bootstrap path now reports a heartbeat to the startup watchdog per completed
  step and after success; the heartbeat restarts the idle budget and refreshes
  `startup_command.json` (`armed_at` is the start of the current idle window)
  while keeping the watchdog armed for a hang before the first phase
  (ADR 0180 addendum).

## 0.9.0 - 2026-08-29

Onboarding stops producing something inert, and a run that has ceased to exist
stops being reported as working. Both came out of walking the paths as a user
rather than as an author: the first from a first-run of `workspace init`, the
second from a field report of four runs that died mid-`implement` and were
still described as live work 23 hours later.

### Added

- `orcho workspace mcp` prints the workspace's resolved MCP server identity
  with no flags, from what `init` persisted, so an agent client can be pointed
  at the right server without reconstructing the command by hand
  (ADR 0185).
- Fine-tune proposes verification commands that can actually run. Per-language
  marker probes are an ordered registry any third-party module can extend
  through `register_marker_probe`, and the Node probe reads the marker
  `package.json` instead of proposing a fixed pair — it offers only scripts
  that exist, falls back to `npx tsc --noEmit` when TypeScript is a dependency
  without a typecheck script, and says when it found nothing.

### Changed

- `orcho workspace init` is a decision surface rather than a scaffolder
  (ADR 0184). It no longer writes a `plugin.py` under the workspace root,
  where nothing reads it: the reader is `<project>/.orcho/multiagent/plugin.py`,
  and those two paths coincide only when the workspace root *is* the project
  root, which managed workspaces no longer produce. Every such run reported
  `no plugin — generic mode`, recorded gates as `skipped`, and delivered
  anyway. Init now asks about the project plugin, discloses a project with no
  repo markers before asking, and reports a skeleton as
  `created (empty — fill commands)` instead of an undifferentiated success.
  Its output is also considerably shorter.
- The shipped `config.defaults.json` no longer carries this project's own
  topology signals. They were hardcoded, so a user's task that merely
  mentioned "wire format" drew a cross-repo recommendation over two
  repositories they had never heard of. The table ships empty, with the
  mechanism documented and an ignored `_example` showing the shape; empty
  signals mean no cross recommendation.
- Skill shadowing reports one counted line instead of one per skill. Shadowing
  is the expected outcome when a project overlays a shared catalogue, and on a
  host with its own 26-skill set the diagnostic pushed every command's real
  output off the screen. `--output debug` keeps the per-skill detail.

### Fixed

- A run whose process is gone is no longer reported as working. The event
  writer emits naive local timestamps; the liveness reader accepted only
  offset-aware values and aged everything else to unknown, so the predicate
  behind both `run_diagnosis`'s `stalled` verdict and `repair-state`'s
  `running_without_live_process` repair could never become true on a real run.
  Both branches existed and neither could fire. `orcho status` now reports such
  a run and names the command that finalises it.
- A verification gate that ran clean but cannot be proven against the current
  checkout no longer reads as a failing test suite. An exit-0 receipt whose
  subject cannot be compared classifies `unverifiable`, and routing refuses it
  — correctly — but the operator saw `✓ passed` followed by a REJECTED handoff,
  with `outcome: "failed"` the only explanation on record. The result line now
  follows the classification, and `gate.end` carries `receipt_status` /
  `failure_kind` beside the unchanged rollup.
- `pre_run_dirty` intake seeds untracked directories. `git status --porcelain`
  collapses a wholly-untracked directory to one trailing-slash entry, which the
  seed loop treated as a file: any project with an untracked folder could not
  use the intake default, and the loop aborted mid-copy leaving a partially
  seeded worktree. The resulting halt also printed nothing at all.
- The argparse `==SUPPRESS==` placeholder no longer leaks into help output.
- On Windows, a launched run asks to break out of the launcher's job object.
  `CREATE_NEW_PROCESS_GROUP` only reroutes console control events, so a client
  that supervises the launcher inside a kill-on-close job took every detached
  run down with it — terminated by the kernel with no traceback, no log bytes,
  and no terminal event. A job that forbids breakaway falls back to the
  previous behaviour rather than refusing to start the run.

### Known Notes

- The Windows job-object change addresses the most probable mechanism for a
  reported signature — runs dying mid-phase with zero diagnostics of any kind —
  but that report could not be reproduced in CI, and neither can the fix be
  exercised there. CI confirms only that the launch path still works on
  Windows. Field confirmation is still outstanding.

## 0.8.6 - 2026-08-26

Three defects that share one shape: a value is written, accepted, and then
silently not used. Reported from a native-Windows field host.

### Fixed
- A phase `effort` declared by the active profile now reaches the run. The rule
  that a profile declaration beats the global `phases.<phase>.effort` map had
  two owners, and the one the CLI reaches read only the global map: every CLI
  run therefore discarded the profile's value, including one that
  `orcho profile customize --phase-effort` had just written and reported as a
  success. Both construction paths now delegate to a single resolver.
- The Claude-compatible GLM adapter owns its CLI configuration directory. It
  previously supplied the token, endpoint and model mapping but let the child
  inherit the operator's ordinary configuration directory — and the CLI
  resolves credentials from there in preference to the environment, so on any
  host with a logged-in Claude subscription every GLM call failed
  authentication on a valid token. Setting `CLAUDE_CONFIG_DIR` by hand could
  not fix it: the variable is process-global, so isolating this runtime also
  stripped the credentials of every other Claude-family runtime in the same
  pipeline, which made mixed-vendor runs impossible. The adapter now points
  the child at a per-user directory (`~/.orcho/claude-glm-config`, overridable
  via `claude_glm.config_dir` or `CLAUDE_GLM_CONFIG_DIR`).
- `orcho profile customize --session-split` no longer reports a write the run
  will silently discard. `pipeline.session_split_override` applies after the
  profile by design — it is the operator escape hatch — so a valid, persisted
  overlay value can still not be what the run uses. The command now names the
  collision, and the value stays written so it takes effect once the override
  is removed.

### Documentation
- The configuration reference and the profile authoring guide now state which
  side wins between a `profiles_v2` overlay and a global block: `phases.*`
  effort, `pipeline.change_handoff`, `pipeline.implementation_execution` and
  `worktree.isolation` are defaults that a profile declaration beats, while
  `pipeline.session_split_override` deliberately beats the profile. The rule
  previously existed only in a comment inside the shipped defaults file.
- The per-phase routing flags document which phases they set. Two of the four
  govern a group — `--runtime-repair-changes` also sets `repair_escalation`,
  and `--runtime-review-changes` also sets `validate_plan` and
  `final_acceptance` — which nothing user-facing had said.



## 0.8.5 - 2026-08-25

Closes the observability half of the field-reported Windows startup-hang
family (#259). The hangs themselves were fixed in 0.8.3 and 0.8.4; these are
the surfaces that reported a run as healthy while it could not advance.

### Fixed
- A run that cannot advance is no longer reported as active. `run_diagnosis`
  now classifies two further shapes as stalled: a recorded process that is
  gone with stale durable progress, and a launch that never reached its own
  startup arming. The previous stall verdict read `startup_command.json`,
  which the orchestrator writes from inside the run — so a run that died
  before that point left nothing to judge, and the worst startup failure was
  invisible to the detector built for it. The verdicts now rest on facts the
  launcher records at spawn time.
- `repair-state` can finalise an orphaned run. A non-terminal run with no live
  process and no terminal event is now a repairable shape that proposes
  `interrupted`, idempotently. Previously it reported no issues and no
  proposed changes, leaving such runs permanently `running`.
- A non-positive or non-finite startup grace can no longer disable stall
  ageing. `NaN` survived the previous validation and made every ageing
  comparison false, silently switching the detector off.

### Notes
- `pid_is_alive` establishes only that a PID currently exists. Because an OS
  may reuse a PID, an alive result is deliberately not treated as proof of
  ownership, and a dead result is only acted on together with stale durable
  progress and no terminal event.

## 0.8.4 - 2026-08-23

A single-line follow-up to the 0.8.3 startup-hang family (#250). The same
field host re-tested 0.8.3 and found a second, independent defect underneath
the one we fixed: every run launched through an embedder never started, while
the identical task through the CLI worked.

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
