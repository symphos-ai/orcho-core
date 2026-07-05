# Changelog

## Unreleased

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
