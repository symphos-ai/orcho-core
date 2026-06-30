# ADR 0101 — MCP companion proof: `orcho_run_resume` accepts & forwards `runtime_override`

This is the executable-proof artifact for the MCP-transport leg of ADR 0101.
The SDK `next_actions` **replace** Action (`sdk/actions.py::compute_next_actions`)
publishes `tool = orcho_run_resume` with
`args = {run_id, runtime_override: {phase, runtime, model}}`. This document
proves — with captured machine output, not prose — that the **current strict
MCP tool schema and executor** in the `orcho-mcp` companion already accept that
arg and forward it to orcho-core's `persist_runtime_override` **before** the
resume subprocess spawns, so a non-MCP-side reviewer can verify the contract end
to end without leaving the orcho-core review subject.

The companion source lives in the sibling `orcho-mcp` repo (separate git
history — `orcho-core` must not depend on `orcho-mcp`). Its full diff is attached
to this review subject as [`0101-orcho-mcp-companion.patch`](0101-orcho-mcp-companion.patch)
(`git apply` from the `orcho-mcp` root). The reproduction commands below
regenerate every receipt against an installed `orcho-mcp`.

## 1. Strict schema accepts `runtime_override` (live tool, not a hand edit)

Dumped from the **live registered FastMCP tool** via
`await mcp.list_tools()` — i.e. the schema an MCP client is actually offered.
The committed `orcho-mcp/docs/mcp_schema.json` is gated by `make schema-check`
(`tools/dump_mcp_schema.py --check`), which dumps this same live schema and
fails on drift, so the snapshot cannot silently diverge from the running tool.

`orcho_run_resume.inputSchema` (relevant fragment):

```json
{
  "$defs": {
    "RuntimeOverrideArg": {
      "additionalProperties": false,
      "properties": {
        "phase":   { "type": "string", "title": "Phase" },
        "runtime": { "type": "string", "title": "Runtime" },
        "model":   { "type": "string", "title": "Model" }
      },
      "required": ["phase", "runtime", "model"],
      "title": "RuntimeOverrideArg",
      "type": "object"
    }
  },
  "properties": {
    "run_id":  { "type": "string", "title": "Run Id" },
    "profile": { "anyOf": [{ "type": "string" }, { "type": "null" }], "default": null },
    "runtime_override": {
      "anyOf": [{ "$ref": "#/$defs/RuntimeOverrideArg" }, { "type": "null" }],
      "default": null
    }
  },
  "required": ["run_id"]
}
```

Facts this pins:

- `runtime_override` **is** a declared property of the tool — a strict client
  serializes it through, it is not dropped as an unknown field.
- It accepts the exact shape the SDK replace Action emits:
  `RuntimeOverrideArg{phase, runtime, model}` (all three `required`).
- `additionalProperties: false` on `RuntimeOverrideArg` means a malformed pair
  is rejected by the schema, not coerced.
- `run_id` stays `required`, matching the ADR rule that every replace Action
  addresses a specific run.

## 2. Executor forwards to `persist_runtime_override` before spawn

`orcho-mcp/src/orcho_mcp/run_control/lifecycle.py::resume_run` (current
working-tree, line numbers from the companion patch):

```text
448        if blocked is not None:          # pre-flight guard: blocked/terminal → no spawn, no write
449            return blocked
...
459    if runtime_override is not None:     # operator chose a replacement
460        _persist_runtime_override(run_id, runtime_override)   # ← persist BEFORE spawn
...
464        handle = await supervisor.resume(run_id, profile=profile)   # ← subprocess spawns here
```

`_persist_runtime_override` (same file) resolves the run dir and delegates to
orcho-core — the single validation + persistence authority:

```python
def _persist_runtime_override(run_id, override):
    from orcho_mcp.services.run_lookup import find_run_dir
    run_dir = find_run_dir(run_id)
    with map_command_errors():                       # ValueError → typed InvalidPlanError
        from sdk.run_control.runtime_override import persist_runtime_override
        persist_runtime_override(
            run_dir, phase=override.phase,
            runtime=override.runtime, model=override.model,
        )
```

Ordering proven: pre-flight guard (L448) → **persist override (L459–460)** →
spawn (L464). A blocked/terminal resume returns before any write; a supplied
override is fixed into durable `meta.json` before the supervisor is ever asked
to spawn.

## 3. Signature match (no drift across the repo boundary)

The MCP call site and the orcho-core authority agree exactly:

| Side | Surface |
| --- | --- |
| orcho-mcp call | `persist_runtime_override(run_dir, phase=…, runtime=…, model=…)` |
| orcho-core def (`sdk/run_control/runtime_override.py`) | `persist_runtime_override(run_dir, *, phase, runtime, model, note=None, decided_at=None)` |

orcho-core validates `(runtime, model)` against the configured replacement
candidates and raises `RuntimeOverrideError` / `RuntimeOverrideConflict` (both
`ValueError`), which the MCP boundary's `map_command_errors()` maps to the typed
`InvalidPlanError`. A non-candidate pair therefore aborts the resume as a clean
bad-request — never a silent plain resume under the wrong runtime.

## 4. Captured receipts

Run from the `orcho-mcp` root against the installed package:

```text
$ python tools/dump_mcp_schema.py --check
schema-check: PASS (exit 0 — live tool schema == docs/mcp_schema.json)

$ python -m pytest -q tests/unit/run_control/test_resume_runtime_override.py
3 passed in 0.26s

$ python -m pytest -q tests/unit/run_control/ tests/acceptance/mock_pipeline/test_orcho_run_resume.py
99 passed, 6 deselected
```

The three mock-smokes in
`orcho-mcp/tests/unit/run_control/test_resume_runtime_override.py` assert the
wire behaviour directly:

1. `test_runtime_override_persisted_before_spawn` — the override lands in
   `meta.json` and the supervisor's `resume` is invoked, in that order.
2. `test_plain_resume_writes_no_override` — `runtime_override=None` writes no
   record (behaviour unchanged for the common case).
3. `test_non_candidate_override_rejected_without_spawn` — a non-candidate pair
   raises `InvalidPlanError` with **no** spawn and **no** write.

## 5. Reproduce every claim

```bash
# Schema acceptance (live tool, section 1):
cd <orcho-mcp> && python -c "
import asyncio, json
from orcho_mcp.tools import mcp
async def main():
    t = next(x for x in await mcp.list_tools() if x.name=='orcho_run_resume')
    s = t.inputSchema
    assert 'runtime_override' in s['properties']
    assert s['\$defs']['RuntimeOverrideArg']['required'] == ['phase','runtime','model']
    assert s['\$defs']['RuntimeOverrideArg']['additionalProperties'] is False
    print('OK: runtime_override accepted by live strict schema')
asyncio.run(main())"

# Snapshot-in-sync gate + mock-smokes (sections 1, 2, 4):
cd <orcho-mcp> && python tools/dump_mcp_schema.py --check
cd <orcho-mcp> && python -m pytest -q tests/unit/run_control/test_resume_runtime_override.py

# Full companion diff is attached at docs/adr/0101-orcho-mcp-companion.patch
```
