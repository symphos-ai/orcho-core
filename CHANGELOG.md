# Changelog

## Unreleased

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
