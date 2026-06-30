---
name: orcho-core-sdk-wire
description: "Use when editing orcho-core/sdk/*, docs/sdk_schema.json, public dataclasses, status/history/evidence/metrics/cost/run-diff payloads, plan/review/release wire schemas, or any core shape exposed through MCP. Pair with orcho-mcp for MCP-visible changes. Do not use for private parser internals or prompt prose."
---

# Orcho Core SDK Wire

Protect the public Python SDK and MCP-visible core payload shape.

## First Reads

- `orcho-core/AGENTS.md`
- `orcho-core/sdk/types.py`
- changed `orcho-core/sdk/*` file
- `orcho-core/docs/sdk_schema.json` when schema changes
- `orcho-core/docs/adr/0021-public-sdk-boundary.md`

## Owns

- `orcho-core/sdk/*`
- SDK-visible run/status/history/evidence/metrics/cost/run-diff payloads
- `docs/sdk_schema.json`
- public plan/review/release wire shape
- core-side shape that `orcho-mcp` adapts

## Does Not Own

- parser implementation details -> `orcho-core-quality-gates`
- evidence internals -> `orcho-core-evidence-observability`
- prompt prose -> `orcho-prompt-engine`
- MCP handler implementation -> `orcho-mcp`

## Invariants

- Public SDK returns typed dataclasses, not ad-hoc dicts.
- No `print`, `sys.exit`, or environment mutation in SDK APIs.
- Schema changes require snapshot updates and MCP alignment.
- Wire-format changes need same-change `orcho-mcp` validation.

## Verification

- `python -m pytest -q orcho-core/tests/sdk/test_schema_snapshot.py`
- relevant `orcho-core/tests/sdk/test_*`
- matching `orcho-mcp` schema/registration/E2E smoke for exposed changes

## Neighbor Skills

- `orcho-mcp` for MCP-visible changes
- `orcho-core-evidence-observability` for evidence/status slices
- `orcho-core-quality-gates` for parser/gate payloads
- `orcho-public-boundary` for public docs/docstrings
