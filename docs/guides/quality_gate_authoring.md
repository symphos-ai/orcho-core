# Authoring a Quality Gate

> Skeleton. <!-- TODO(orcho-phase-7): expand with end-to-end pip plugin
> example, registration through pyproject.toml entry_points, and
> discovery semantics once orcho.quality_gates entry_points group is
> wired. -->

A quality gate is a registered post-phase check that produces a typed
`QualityGateResult` and applies a declarative fail policy
(`HALT` / `FEED_INTO_NEXT` / `TRIGGER_REPLAN` / `INFORMATIONAL`). orcho
core ships one gate (`tests`); third-party plugins extend the registry
through `orcho.quality_gates` entry_points (Phase 7).

## When to write one

- **Add a verification step** that's not `tests`: lint, compile,
  format-check, license scan, security review, dependency vulnerability
  audit, contract / spec compliance.
- **Override an existing gate** with company-specific behaviour
  (e.g. internal test runner that wraps a SaaS dashboard).
- **Inferential gates**: an LLM judge that reads the diff and emits
  pass/fail (security review, code-style review, spec compliance).

## Contract (Phase 4)

```python
from typing import Protocol, runtime_checkable

from pipeline.quality_gates import QualityGateResult
from pipeline.runtime import GateKind, PipelineState, QualityGate

@runtime_checkable
class QualityGateHandler(Protocol):
    def execute(
        self,
        gate: QualityGate,
        state: PipelineState,
        cwd: str,
    ) -> QualityGateResult: ...
```

Implementations must:

1. **Never raise.** Wrap the underlying check in a try/except;
   exceptions become `QualityGateResult(passed=False, error="...")`
   so a broken gate doesn't crash the run. The dispatcher applies the
   gate's fail strategy to the failure result.
2. Return a frozen `QualityGateResult` with `name`, `passed`, `output`,
   `duration_s` populated. `kind` defaults to `COMPUTATIONAL`; set
   `INFERENTIAL` and populate `cost_usd` for LLM-driven gates.
3. Read config from `gate.config` (per-gate) and `state.plugin` (project
   defaults). Don't read environment variables — runtime makes those
   available through `state.plugin` already.
4. Be deterministic given the same input — gates fire from the
   `PhaseLifecycle` FSM after the phase executor and before
   session-adapter / checkpoint / metrics stages.

## Example skeleton

```python
import time
import subprocess
from pipeline.quality_gates import QualityGateResult
from pipeline.runtime import GateKind

class LintGate:
    """Runs `ruff check` and surfaces violations as gate output."""

    def execute(self, gate, state, cwd):
        config = gate.config or {}
        cmd = config.get("command", "ruff check .")
        timeout = config.get("timeout_sec", 60)
        t0 = time.monotonic()
        try:
            r = subprocess.run(
                cmd, shell=True, cwd=cwd, timeout=timeout,
                capture_output=True, text=True,
            )
        except Exception as e:
            return QualityGateResult(
                name=gate.name, passed=False,
                output=str(e), duration_s=time.monotonic() - t0,
                kind=GateKind.COMPUTATIONAL,
                error=type(e).__name__,
            )
        return QualityGateResult(
            name=gate.name,
            passed=(r.returncode == 0),
            output=r.stdout + r.stderr,
            duration_s=time.monotonic() - t0,
            kind=GateKind.COMPUTATIONAL,
        )
```

Register via `orcho.quality_gates` entry_points (Phase 7):

```toml
# pyproject.toml in your plugin package
[project.entry-points."orcho.quality_gates"]
lint = "my_plugin.gates:LintGate"
```

## Choosing a fail strategy

| Strategy | When to use |
|----------|-------------|
| `FEED_INTO_NEXT` | Default for actionable failures (test failures, lint warnings the FIX phase can address). Gate output flows into next phase's prompt. |
| `TRIGGER_REPLAN` | Gate failure means the *plan* is wrong, not the implementation. E.g. spec-compliance failure → architect needs to re-decompose. Output becomes critique for next loop round. |
| `HALT` | Gate failure means human intervention is required. E.g. compliance check, license scan. Run halts; meta.json captures the reason. |
| `INFORMATIONAL` | Gate output is for audit only, not blocking. E.g. metric drift detection, optional review by a third LLM. |

## Phase 7 will add

- `orcho.quality_gates` entry_points discovery wiring.
- Override conflict diagnostics (`orcho gates list` will show
  shadowed registrations).
- Inferential gate cost accounting integration (RunBudget, Phase 8.5).
- Per-step `PhaseStep.quality_gates` profile JSON authoring.

## See also

- [`docs/architecture/quality_gates.md`](../architecture/quality_gates.md)
- [`docs/reference/builtin_gates.md`](../reference/builtin_gates.md)
