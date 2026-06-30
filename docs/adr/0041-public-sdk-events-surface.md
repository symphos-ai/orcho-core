# ADR 0041 — Public SDK events surface

- **Status:** Accepted.
- **Date:** 2026-05-25
- **Deciders:** project owner
- **Builds on:**
  [ADR 0021](0021-public-sdk-boundary.md) — public SDK boundary
  (`from sdk import …`, typed dataclasses, never prints, JSON-
  serialisable via `to_jsonable`).

## Context

The SDK boundary set up in ADR 0021 covers runs, status, history,
metrics, evidence, prompts, profiles, run diff, phase handoff, and
workspace bootstrap. Every embedder read path goes through `from sdk
import …` for those surfaces.

Run-event replay was the exception. There is no public SDK function
for "give me every event from `run_id`'s `events.jsonl`" — the only
working call is `core.observability.events.read_all(run_dir)`, an
engine internal that returns the internal `Event` dataclass.

Concrete consequence in `orcho-mcp`:

- `orcho_mcp/services/run_reads.py`, `orcho_mcp/observe/summary.py`,
  and `orcho_mcp/observe/watch.py` all imported
  `core.observability.events.read_all` directly.
- The orcho-mcp architectural guard `test_no_direct_run_state.py`
  had to allow-list `core.observability.events` as a documented
  exception, weakening the "MCP read paths must speak SDK" invariant.
- Any other embedder needing the event stream would face the same
  choice: take the engine internal or write its own JSONL parser.

The gap was known and called out in
`orcho-mcp/docs/architecture/mcp_boundaries.md` and in the open
review brief — closure was deferred until a clean public shape was
agreed.

## Decision

Add a public SDK function `list_events` plus a public dataclass
`RunEvent`, both exported from `sdk.__init__`:

```python
# sdk/types.py
@dataclass(frozen=True, slots=True)
class RunEvent:
    seq: int
    ts: str
    kind: str
    phase: str | None
    payload: dict[str, Any] = field(default_factory=dict)


# sdk/events.py
def list_events(
    run_id: str,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> tuple[RunEvent, ...]:
    ...
```

Semantics:

- Resolves `run_id` through `find_run` so workspace / runs-dir / cwd
  rules match every other SDK read.
- Returns a tuple (immutable, ordered) of `RunEvent` records in seq
  order. A run with no `events.jsonl` returns an empty tuple, not an
  error — symmetric with how the internal `read_all` behaves.
- Raises `RunNotFound` / `NoWorkspace` through `find_run`. No new
  error types are introduced.
- The internal `core.observability.events` module continues to exist
  unchanged. `Event` (internal dataclass) and `RunEvent` (public
  dataclass) have the same shape today; the boundary exists so the
  internal can evolve without breaking embedders.

The write side (`core.observability.events.append_event`, used by
the orchestrator and by the MCP supervisor's orphan / reap-event
markers) is **not** exposed through SDK. Writers participate in a
run rather than read it; that contract belongs in a separate ADR if
and when an embedder needs it.

## Rationale

- **Closes the only outstanding allow-list exception in the
  MCP read-state architectural guard.** After this ADR lands and
  orcho-mcp routes through `sdk.list_events`, the negative-import
  guard can hard-forbid `core.observability.events.read_all`
  imports in MCP read paths — which it now does.
- **Embedder isolation.** Returning the internal `Event` would
  leak `core.observability.events` into every embedder's type
  surface. Publishing a thin `RunEvent` keeps the internal free to
  evolve (rename a field, add a `_meta` block, switch backing
  store) without breaking external consumers.
- **Wire pairing held.** MCP per-phase validation rule says wire-
  format changes ship with the matching update + E2E mock smoke in
  `orcho-mcp` in the same change. They do — see the paired
  orcho-mcp commit.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Continue allow-listing `core.observability.events.read_all` in MCP guards | Permanent allow-list exception in an architectural guard is exactly the smell the guard exists to surface. Closing the gap once is cheaper than carrying the exception forever |
| Expose `core.observability.events.read_all` directly (re-export the internal) | Couples every embedder's type surface to the engine's internal `Event` shape; defeats the SDK boundary purpose |
| Add a tail / streaming surface (`sdk.tail_events`) in the same ADR | Streaming requires backpressure / cancellation semantics that the current MCP consumers don't need; `list_events` covers every MCP read path today (`watch`, `summary`, `run_reads` all replay-then-filter). Streaming is a future ADR if a real consumer emerges |
| Expose `append_event` alongside `list_events` | Write-side participation in a run is a different contract — different error surface, different correctness window, different consumer. Bundling them muddies both. Keep separate |

## Consequences

- `sdk.list_events` and `sdk.RunEvent` are now part of the public
  SDK surface. They follow ADR 0021 rules (typed dataclass, no
  prints, no `sys.exit`, JSON-serialisable via `to_jsonable`).
- `docs/sdk_schema.json` updated to include `RunEvent`.
- `tests/sdk/test_events.py` covers happy path, missing run,
  missing `events.jsonl`, ordering, payload pass-through.
- In `orcho-mcp`, three modules migrated off
  `core.observability.events.read_all` onto
  `sdk.list_events` via a new
  `orcho_mcp/services/run_events.py` wrapper. The
  `test_no_direct_run_state.py` guard now forbids `read_all`
  imports in `orcho_mcp/` outright — see paired orcho-mcp commit.
- Supervisor's `append_event` usage (orphan / reap markers) is
  unaffected; the new guard explicitly only bans the read side.
