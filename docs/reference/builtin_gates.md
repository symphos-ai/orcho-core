# Built-in Quality Gates

> Skeleton — Phase 4 ships `tests` only. <!-- TODO(orcho-phase-7+):
> add lint, compile, format-check as third-party plugins land.
> Inferential gates (security_review, spec_compliance) ship as
> third-party plugins, not in core. -->

## `tests`

**Class:** `pipeline.quality_gates.TestsGate`
**Kind:** `computational`
**Reads from:** `state.plugin.quality_gates["tests"]`
**Output:** combined stdout/stderr from runnable suites

### Behaviour

- **Single suite** (`quality_gates["tests"].run_command` set,
  `quality_gates["tests"].suites`
  empty): runs the command in `cwd`, decides pass/fail by exit code +
  optional `fail_keyword` substring match in output.
- **Multi suite** (`quality_gates["tests"].suites` non-empty): runs each suite with
  a non-None `run_command` sequentially. Aggregate result is `passed`
  iff every runnable suite passed. Skipped suites (None command) are
  documented in output but don't fail.
- **No runnable suite** (`run_command=None` and empty `suites`):
  returns `QualityGateResult(passed=True, output="")` — equivalent
  to the legacy `TestResult(skipped=True)`.
- **Handler exception**: caught and surfaced as
  `QualityGateResult(passed=False, error="<exception text>")`. A
  broken test runner doesn't crash the pipeline.

### Config

Customer plugin config flows through
`PluginConfig.quality_gates["tests"]`. The built-in gate still coerces
that dict into the internal `TestingConfig` / `TestSuiteConfig`
dataclasses so the existing `run_tests` implementation can stay small.

### Wiring

`PhaseStep.quality_gates` fires through runtime / lifecycle dispatch.
When the provider's `run_tests` override returns None, `TestsGate` uses
the subprocess path. When a mock/custom provider returns a legacy
`TestResult` directly, `TestsGate` normalizes it into the same
`QualityGateResult` audit shape before the fail strategy applies. Result
state lands in `state.last_test_output` and
`state.extras["last_test_result"]` for downstream consumers (FIX prompt,
BuildAdapter session shape).

## Future built-in gates

The plan reserves slots for these (Phase 4 ships infrastructure;
implementation lands as gates are needed):

- **`lint`** — wraps `ruff` / `eslint` / `prettier --check` per
  language. Computational. Defaults to `on_fail=FEED_INTO_NEXT`
  (lint output flows into FIX prompt).
- **`compile`** — wraps language-specific compile checks
  (`mypy`, `tsc`, Unity Editor batch compile). Computational.
- **`format-check`** — wraps `black --check` / `prettier --check`.
  Computational. Defaults to `on_fail=INFORMATIONAL` (format drift
  shouldn't halt; most pipelines auto-fix in FIX).

Inferential gates (`security_review`, `spec_compliance`,
`api_compatibility`) are an extension surface for third-party plugins
shipped via `orcho.quality_gates` entry_points (Phase 7).

## See also

- [`docs/architecture/quality_gates.md`](../architecture/quality_gates.md) —
  fail strategies + computational vs inferential
- [`docs/guides/quality_gate_authoring.md`](../guides/quality_gate_authoring.md) —
  plugin authoring guide
