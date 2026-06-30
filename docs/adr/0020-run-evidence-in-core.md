# ADR 0020: Baseline Run Evidence Lives in Core

- **Status:** accepted
- **Date:** 2026-05-07
- **Decider:** project owner
- **Extended by:** [ADR 0035](0035-terminal-status-and-resume-observability.md)
  (2026-05-24) — bundle finalize now also runs on phase-handoff halt
  (was: only on pipeline subprocess's natural finish, so halted runs
  ended without `evidence.json` / `metrics.json`). The closed
  `prompt_render` summary schema grew two writer-stamped fields
  (`phase_key`, `continue_session`); cross-subprocess metrics
  aggregation on resume is also covered there.

## Context

Orcho persists the state of a single run in several places:

- checkpoint store;
- `events.jsonl`;
- metrics;
- run artifacts;
- provider transcripts;
- git diff / working tree state.

For users and for integrations these must combine into a single answer to
the question: what happened in this run. If this layer were moved out of
`orcho-core` into a separate repository or an external shell, two problems
would appear:

1. The pipeline, events, and checkpoints live in core, while the
   interpretation of run state would live off to the side. That creates
   fragile private APIs and behavioral divergence between CLI, Web, MCP,
   and any external embedders.
2. Open-source users could not verify the result of a run on their own
   without additional tooling. That undermines trust in the engine layer.

## Decision

The baseline run-evidence implementation belongs to `orcho-core`.

Core must own:

- the typed plan contract;
- the event schema / event spine;
- run artifact collection;
- `evidence.json`;
- the baseline `evidence.md`;
- a CLI command at the level of `orcho evidence <run_id>`;
- a programmatic API / hooks for Web, MCP, and third-party embedders;
- a deterministic golden scenario that exercises the evidence path.

Target structure:

```text
pipeline/
  evidence/
    __init__.py
    schema.py
    collector.py
    bundle.py
    render_md.py

cli/
  orcho.py  # orcho evidence <run_id>
```

## Boundaries

Core evidence is responsible for retrospective single-run facts:

- task;
- accepted plan;
- phase timeline;
- review findings;
- commands/checks;
- artifacts;
- metrics;
- diff summary;
- skipped or unknown checks;
- final status.

Core evidence is not responsible for heavier packaging:

- signed compliance bundles;
- hash-chain / tamper-evident audit export;
- SOC2 / ISO / GDPR report templates;
- governance dashboards;
- cross-run organization analytics;
- projective cost/risk analytics;
- team policy management;
- cloud sync or multi-user auth;
- report narratives packaged for non-engineering audiences.

These layers can live in any external package built on top of the public
core APIs.

## Consequences

Positive:

- CLI, Web, MCP, and any external embedders read one model of run state.
- The public core stays self-contained and verifiable.
- External layers add UX, governance, compliance, and projections without
  gating access to the raw run data.
- Cross-MCP context can be logged into the same event/evidence pipeline.

Negative:

- Core gains more surface area that must be maintained stably.
- Basic run reports can no longer be gated outside core without violating
  this ADR.
- The evidence schema must be versioned carefully because external
  integrations will depend on it.

## Related documents

- The run-evidence audit roadmap (internal planning record)
- The cross-MCP orchestration plan (internal planning record)
