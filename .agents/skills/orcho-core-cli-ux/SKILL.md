---
name: orcho-core-cli-ux
description: "Use when editing Orcho CLI behavior: orcho-core/cli/*, CLI help text, output modes, interactive prompts, stdout/stderr formatting, transcript behavior, latest/resume commands, or public `orcho`, `orcho run`, `orcho cross`, `orcho status`, `orcho evidence`, `orcho diff`, `orcho web` UX. Do not use for pipeline semantics alone."
---

# Orcho Core CLI UX

Keep the CLI a thin, stable facade over SDK and pipeline boundaries.

## First Reads

- `orcho-core/AGENTS.md`
- `orcho-core/core/io/AGENTS.md` when stdout/stderr, ANSI, TTY, or prompts change
- `orcho-core/cli/`
- relevant `orcho-core/tests/unit/cli/test_*.py`

## Owns

- CLI command wiring
- help text and epilogs
- output modes
- stdout/stderr formatting
- transcript rendering
- interactive prompts
- latest/resume UX

## Does Not Own

- pipeline semantics -> relevant core specialist
- SDK payload shape -> `orcho-core-sdk-wire`
- MCP exposure -> `orcho-mcp`
- public wording hygiene -> `orcho-public-boundary`

## Invariants

- CLI handlers stay thin: parse args, call delegate, format, print, return code.
- stdout is intended output; errors go to stderr where appropriate.
- `--stream-output` and `--verbose` semantics stay stable.
- Preserve byte-level expectations where tests assert them.

## Verification

- `python -m pytest -q orcho-core/tests/unit/cli`
- command-specific tests for changed commands
- public boundary scan for help/docs wording

## Neighbor Skills

- `orcho-core-sdk-wire` when formatter inputs or public status/evidence shape change
- `orcho-public-boundary` for public help/docs/error wording
- `orcho-core-evidence-observability` when CLI renders evidence internals
