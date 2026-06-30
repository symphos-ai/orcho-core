# ADR 0007: Quality Gate Fail Strategies as Data, Not Code

- **Status:** Accepted
- **Date:** 2026-05-06
- **Phase:** 4
- **Deciders:** project owner

## Context

A pipeline's verification needs aren't uniform: a missing test failure
needs to feed into the next FIX prompt; a compliance check failure
needs to halt the run for human review; an LLM judge's drift detection
might be informational only.

The legacy orchestrator hard-coded one strategy per call site:

- `_on_phase_end` for build/fix: ran tests, set
  `state.last_test_output` (FEED_INTO_NEXT semantics, no name).
- `_phase_plan_qa`: read `block_on_qa_reject` flag, branched between
  HALT and continue.
- `_phase_final_qa`: same flag, same branch.

Adding a new gate would require code changes in the orchestrator (where
to fire it), in the handler (when to set state), and in the loop
(how to interpret the failure). Three coupled changes per gate.

## Decision

Express fail policy as **data on the gate instance**, not as branching
code at the call site:

```python
@dataclass(frozen=True)
class QualityGate:
    name: str
    on_fail: FailStrategy   # HALT | FEED_INTO_NEXT | TRIGGER_REPLAN | INFORMATIONAL
    kind: GateKind = GateKind.COMPUTATIONAL
    feed_target: str | None = None
    config: dict[str, Any] | None = None
```

`apply_fail_strategy(gate, result, state)` reads `gate.on_fail` (one
StrEnum value) and mutates state accordingly. New strategies are added
to the enum; their handling lives in one match-statement, not scattered
across N call sites.

```python
match gate.on_fail:
    case FailStrategy.HALT:
        state.stop(...)
    case FailStrategy.FEED_INTO_NEXT:
        state.extras[gate.feed_target] = result.output
    case FailStrategy.TRIGGER_REPLAN:
        state.last_critique = result.output
    case FailStrategy.INFORMATIONAL:
        pass
```

## Drivers

- **Discriminated union via StrEnum.** Each strategy carries clear
  semantics: HALT stops the run, FEED_INTO_NEXT routes failure as
  input, TRIGGER_REPLAN promotes failure to critique. A user-authored
  custom gate can pick any of these by changing one field.

- **Composability for Phase 5+.** When `PhaseStep.quality_gates`
  lands (per Phase 1 type), profile authors write JSON like:

  ```json
  {"phase": "build",
   "quality_gates": [
     {"name": "tests",      "on_fail": "feed_into_next",
                            "feed_target": "last_test_output"},
     {"name": "lint",       "on_fail": "informational"},
     {"name": "compliance", "on_fail": "halt"}
   ]}
  ```

  The orchestrator dispatches all three after the build handler runs;
  each gate's strategy is applied independently. No code changes
  needed to recombine.

- **Third-party extension.** Plugin authors ship inferential gates
  (`security_review`, `spec_compliance`) and register them through the
  `orcho.quality_gates` entry_points group; profile authors pick
  `on_fail` declaratively per profile. Strategy-as-code would force
  every plugin to ship orchestrator patches per gate.

- **Audit trail clarity.** `result + applied strategy` is the audit
  record: "tests failed → on_fail=FEED_INTO_NEXT → state.extras[last_test_output]
  populated → next FIX prompt got the test failures". One line per
  gate, mechanical to read.

## Consequences

### Positive

- One match statement covers all current strategies. New strategies
  (e.g. `RETRY_WITH_BUDGET_CAP`) extend the enum + match arm, not
  the call sites.
- Plugin authors write strategy = data, never edit core dispatch code.
- Snapshot tests pin per-strategy mutations (one test per arm).

### Negative / Costs

- The orchestrator's `_on_phase_end` post-build/fix path now goes
  through the gate registry + strategy dispatcher even for the
  legacy single-purpose test invocation. ~5 LoC overhead per phase
  end. Negligible.
- `FEED_INTO_NEXT` requires `feed_target` non-None — invariant
  enforced in `QualityGate.__post_init__`. Plugin authors must
  remember to set it. Documented in the authoring guide; loud
  ValueError at construction catches the typo.

### Neutral

- The Phase 4 wiring keeps `TestingConfig` as the input shape for
  TestsGate (it's a config struct, orthogonal to the strategy
  enum). Phase 5's bigger sweep migrates `TestingConfig` → flat
  `PluginConfig.quality_gates: dict`.

## Validation

`tests/unit/pipeline/quality_gates/test_dispatch.py` covers all
four arms with state-mutation assertions per strategy:

- HALT → `state.halt = True`, reason populated
- FEED_INTO_NEXT → `state.extras[feed_target] = output`
- TRIGGER_REPLAN → `state.last_critique = output`
- INFORMATIONAL → no state mutation

Plus a passing-result smoke that verifies all four are no-ops when
`result.passed == True`.

## Alternatives Considered

### A. Strategy as callbacks (`on_fail: Callable`)

Rejected — turns gate config into Python code, defeats the JSON-
authorable profile shape. Plugin authors couldn't ship gates through
declarative entry_points config; they'd need to ship orchestration
code too.

### B. Strategy as plugin-defined classes (`on_fail: type[FailHandler]`)

Rejected — too much ceremony for what is fundamentally one of four
verbs. Discoverability suffers ("which FailHandler classes are
available?"). Enum + match scales to ~10 strategies cleanly; if we
need more we'll re-evaluate.

### C. No declarative strategy; let each gate handler mutate state directly

Rejected — couples gate handler to specific state shape. A `tests`
handler would need to know "set last_test_output for FIX phase to
read"; an inferential `security_review` handler would need to know
"halt with a specific reason". Strategy-as-data lets the handler
return a pure result; the dispatcher applies the policy.

## References

- ADR 0001: pipeline architecture redesign
- `docs/architecture/quality_gates.md`
- `pipeline/quality_gates.py`
- `tests/unit/pipeline/quality_gates/`
