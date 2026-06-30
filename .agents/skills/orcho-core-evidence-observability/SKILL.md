---
name: orcho-core-evidence-observability
description: "Use for Orcho evidence/observability storage, emission, mirrors, bundles, events, metrics, run diff, and artifact metadata: orcho-core/pipeline/evidence/*, pipeline/observability/*, core/observability/*, pipeline/artifacts/types.py, run_diff.py, artifact_mirror.py, run_logging.py, including persisted prompt-render evidence. Do not use for prompt wording/templates/render contracts unless paired."
---

# Orcho Core Evidence Observability

Keep run evidence, events, metrics, run diff, and artifact mirrors explainable
and stable.

## First Reads

- `orcho-core/AGENTS.md`
- `orcho-core/pipeline/evidence/`
- `orcho-core/pipeline/observability/`
- `orcho-core/core/observability/`
- `orcho-core/pipeline/artifacts/types.py`
- `orcho-core/pipeline/engine/run_diff.py`
- `orcho-core/pipeline/engine/artifact_mirror.py`
- `orcho-core/docs/adr/0020-run-evidence-in-core.md`

## Owns

- evidence bundles and collectors
- event and metric surfaces
- prompt render evidence
- run diff and artifact mirrors
- artifact metadata

## Does Not Own

- SDK-visible shape -> `orcho-core-sdk-wire`
- cross-project artifact bundle -> `orcho-core-cross-project`
- prompt composition itself -> `orcho-prompt-engine`

## Invariants

- Evidence should explain task, plan, phase timeline, findings, commands,
  artifacts, metrics, diff summary, skipped checks, and final status.
- Event kinds and metric names are stable surfaces.
- Do not drop important phase trace silently.

## Verification

- From `orcho-core`: `python -m pytest -q tests/unit/pipeline/evidence`
- From `orcho-core`: `python -m pytest -q tests/sdk/test_evidence_slices.py tests/sdk/test_run_diff.py` when SDK-visible
- From `orcho-core`: run targeted run-diff/artifact tests when those files change.
- Pair with `orcho-mcp` smoke when exposed through MCP.

## Neighbor Skills

- `orcho-core-sdk-wire` for public evidence/status shape
- `orcho-prompt-engine` when prompt render contract changes
- `orcho-mcp` when evidence is exposed through MCP
