# ADR 0045 — RunStatus artefact map

- **Status:** Accepted.
- **Date:** 2026-05-25
- **Deciders:** project owner
- **Builds on:**
  [ADR 0021](0021-public-sdk-boundary.md) — public SDK boundary
  (`from sdk import …`, typed dataclasses, never prints, JSON-
  serialisable via `to_jsonable`),
  [ADR 0041](0041-public-sdk-events-surface.md) — paired public
  SDK + MCP-mirror pattern for an embedder-facing surface.

## Context

`sdk.load_status` already gives embedders the full typed projection
of a run — meta, metrics, sub-projects, next_actions. What it does
not surface is **what artefacts the embedder may read for that run**.
Today an agent calling `orcho_run_status` sees the run's state but
not whether `parsed_plan.json` exists, whether a captured
`diff.patch` is available, or that the evidence bundle can be
composed on demand. The agent falls back to documentation or
filesystem scanning.

The gap is observable in the orcho-mcp wire surface (where the same
agents land via `orcho_run_status`): three resource URIs already
exist — `orcho://runs/{id}/parsed_plan.json`,
`orcho://runs/{id}/evidence`, `orcho://runs/{id}/diff.patch` — but
their existence is implicit. A fresh agent has no machine-readable
way to discover them.

## Decision

Add an `artefacts: tuple[ArtefactRef, ...]` field to
`sdk.RunStatus`, enumerating the readable artefacts for a resolved
run. Each `ArtefactRef` carries `kind`, `uri`, `mime`, and
`size_bytes`.

```python
@dataclass(frozen=True, slots=True)
class ArtefactRef:
    kind: str          # "parsed_plan" | "evidence" | "diff" today
    uri: str           # "orcho://runs/<id>/parsed_plan.json" etc.
    mime: str          # "application/json" | "text/x-patch"
    size_bytes: int | None  # None for composable resources
```

Enrichment rules in `sdk.status.load_status`:

- Runs AFTER `find_run` resolves the run, so `RunNotFound`
  semantics are unchanged — enrichment never runs against a
  missing run.
- `parsed_plan.json` present → entry with `size_bytes` from
  `os.stat`. Omitted when the plan phase has not produced it
  yet.
- `diff.patch` present → entry with `size_bytes` from `os.stat`.
  Omitted when no diff was captured.
- `evidence` — always emitted for a resolved run. The evidence
  bundle is **composable** (assembled at read time by
  `sdk.evidence.collect_evidence`); there is no single
  `evidence.json` on disk. `size_bytes` is `None` to signal this.
- A `_artefact_ref_if_file` helper wraps the `exists() → stat()`
  sequence in `try/except OSError`. A file can disappear between
  the existence check and `os.stat` (concurrent reap, race with
  delivery); enrichment must not fail `load_status` for that.
  Missing or unreadable entries are silently omitted.

## Rationale

### Why a separate field rather than computing on the consumer side

The embedder would otherwise need to know the file conventions
(`<run_dir>/parsed_plan.json` etc.) and the MCP resource URI scheme
(`orcho://runs/<id>/<artefact>`). Today only orcho-mcp does this
mapping. Two more embedders would re-implement the same mapping.
Centralising it on `RunStatus.artefacts` keeps the convention in
one place.

### Why `orcho://` URIs in SDK output

`RunStatus.artefacts[].uri` returns the MCP resource scheme, which
is not strictly an SDK-native scheme. Precedent: `sdk.actions`
(introduced earlier) already returns MCP tool names
(`orcho_run_start`, `orcho_run_resume`) as agent-facing hints,
not as SDK-native callables. The framing is consistent:

- **SDK** owns the run-state read API and carries **agent-facing
  hints** about what the consuming embedder can do with the run.
- **The embedder (today: orcho-mcp)** owns the concrete resource
  implementation behind those hints.

Without this framing, `orcho://` scheme inside `sdk` looks like an
unannounced leak. With it, the two roles are clean.

### Why `kind: str` and not `Literal`

The SDK stays forward-compatible. If a new artefact kind is added
in a later release (e.g. `commands` for a captured-commands log),
older embedders that don't know the new kind still parse the
status without breaking. The MCP-side wire model — which lives in
orcho-mcp — narrows `kind` to the closed `Literal["parsed_plan",
"evidence", "diff"]` set so wire clients can branch on an enum.
Asymmetry is deliberate.

### Why always emit evidence

`collect_evidence` works for any resolved run, even one mid-flight
with no findings yet — it assembles whatever exists. Returning the
entry unconditionally tells the agent "this is always readable",
which matches the resource's behaviour. The `size_bytes=None`
signals the composable nature so consumers don't expect a
single-file `Content-Length`.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Eager evidence bundle size (compute on every status call) | `collect_evidence` walks several artefact files; status is a hot polling path. Lazy is correct |
| Single combined `artefacts: dict[str, str]` (kind → URI) | Loses `mime` and `size_bytes`. A future fourth field would force a wire change |
| Return `ArtefactRef` only when caller passes `include_artefacts=True` | Adds a parameter every embedder has to learn. Always-on enrichment is cheap (two `os.stat` calls) |
| Skip evidence from the list because it is always implicit | Forces every embedder to know "evidence exists for every run" and synthesise the URI. The whole point of the map is to not require that knowledge |
| Embed the URI scheme inside the embedder (sdk returns only paths) | Splits the contract: each embedder rebuilds the URI from `kind` + `run_id`. Today only orcho-mcp does this; adding embedders would re-implement it |

## Consequences

- `sdk.RunStatus.artefacts` is now part of the public SDK surface.
  Follows ADR 0021 rules (typed dataclass, no prints, JSON-
  serialisable via `to_jsonable`).
- `docs/sdk_schema.json` updated to include `ArtefactRef` and the
  new `artefacts` field on `RunStatus`.
- `tests/sdk/test_history_status_metrics.py` covers all four
  artefact presence combinations (no optional / parsed_plan only /
  diff only / all three) plus the always-on evidence entry.
- The paired orcho-mcp change adds an `ArtefactRefRecord` wire
  model with `kind: Literal[...]` and passes the SDK artefacts
  through. See the matching orcho-mcp commit for the wire-side
  details.
- Future artefact kinds (e.g. `commands`, `run_log`) extend the
  set in two paired commits — core widens, MCP narrows. The
  forward-compat `str` `kind` on SDK side absorbs the change
  without breaking older embedders.
