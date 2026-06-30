# Authoring a Session Adapter

> Skeleton. <!-- TODO(orcho-phase-7): expand with end-to-end pip plugin
> example, registration through pyproject.toml, and discovery semantics
> once orcho.session_adapters entry_points group is wired. -->

A session adapter translates `state.phase_log[name]` (plus selected
`PipelineState` fields) into the canonical `session["phases"][name]`
shape. Customer plugins ship their own to override the session-dict
contract for specific phases.

## When to write one

- **Custom phase**: you registered a new phase via `orcho.phases`
  (e.g. `compliance_check`); now you want it visible in `session.json`
  with a structured shape, not just `state.phase_log["compliance_check"]
  = {"output": ...}`.

- **Override a built-in shape**: a plugin might enrich `plan` attempts
  with extra metadata (token-cost breakdown, legal-hold flags). A custom
  `PlanAdapter` registered under the same `"plan"` name overwrites the
  built-in.

- **Per-customer audit format**: regulatory environments may require a
  specific session.json schema. Custom adapters carry the company
  format; orcho-core stays generic.

## Contract (Phase 3)

```python
from typing import Protocol, runtime_checkable
from pipeline.runtime import PipelineState

@runtime_checkable
class SessionAdapter(Protocol):
    def write(
        self,
        phase_name: str,
        state: PipelineState,
        session: dict,
        *,
        round_n: int | None = None,
    ) -> None: ...
```

Implementations must:

1. Read everything they need from `state.phase_log[phase_name]` plus
   well-known `PipelineState` fields (`last_critique`, `parsed_plan`,
   `research_hypothesis`).
2. Mutate `session["phases"][phase_name]` in place. Either set a dict
   (for single-fire phases like `implement`, `final_acceptance`,
   `hypothesis`) or append to a list (for per-round phases like `plan`,
   `validate_plan`, `rounds`).
3. **Not** read foreign transcript formats or parse untrusted external
   JSON — that scope belongs to a separate `ExternalRuntimeAdapter` /
   `TranscriptImporter` concept, never to `SessionAdapter`.
4. Be deterministic given the same input — adapters fire from
   `_PipelineRun.run_*_loop` (Phases 3-4) or the lifecycle FSM (Phase
   5+) and may be invoked multiple times during checkpoint replay.

## Example skeleton

```python
from pipeline.session_adapters import SessionAdapter

class MyCustomPlanAdapter:
    def write(self, phase_name, state, session, *, round_n=None):
        log = state.phase_log.get("plan", {})
        entry = {
            "attempt":  log.get("attempt", round_n or 1),
            "output":   log.get("output", ""),
            "company_legal_hold": log.get("legal_hold", False),
        }
        session.setdefault("phases", {}).setdefault("plan", []).append(entry)
```

Register via `orcho.session_adapters` entry_points (Phase 7):

```toml
# pyproject.toml in your plugin package
[project.entry-points."orcho.session_adapters"]
plan = "my_plugin.adapters:MyCustomPlanAdapter"
```

## Phase 7 will add

- Entry_points discovery wiring with priority resolution
  (project skill > workspace > package).
- Override conflict diagnostics (`orcho skills list` will show
  shadowed registrations).
- Cross-tool transcript import via a separate concept
  (`ExternalRuntimeAdapter`, not `SessionAdapter`).
