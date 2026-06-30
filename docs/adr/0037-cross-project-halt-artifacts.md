# ADR 0037 — Cross-project halt artifacts

- **Status:** Accepted.
- **Date:** 2026-05-24
- **Deciders:** project owner
- **Builds on:**
  [ADR 0020](0020-run-evidence-in-core.md) — typed evidence bundle in core,
  [ADR 0035](0035-terminal-status-and-resume-observability.md) — single-run
  terminal-status + halt_reason invariants.

## Context

ADR 0035 fixed the per-project observability suite — every non-`done`
terminal status lands `meta.halt_reason` and an `evidence.json`
bundle. That work covered the single-project run only. The cross-
project orchestrator (`pipeline/cross_project/orchestrator.py`) has
its own terminal-status flow and was excluded from 0035 by scope.

Audit confirmed the asymmetry:

| Concern | Single-project (ADR 0035) | Cross-project |
|---|---|---|
| `meta.halt_reason` on non-done terminals | ✅ invariant enforced | ❌ only legacy `failure_reason` field |
| `evidence.json` on finalize | ✅ `write_bundle_or_placeholder` | ❌ zero callers in `pipeline/cross_project/*.py` |
| Per-child evidence | ✅ child finalize writes its own bundle | ✅ each sub-run already writes its own bundle (unchanged) |

Three cross-project terminal exit points were silent on the
invariant:

1. **Cross final acceptance failed** (`orchestrator.py` cfa-rejected
   branch) — `session["status"]="failed"`, `failure_reason` set,
   `halt_reason` absent.
2. **Cross contract-check `on_skip=block` blocking skip** (gate
   skipped by policy but `cross_final_acceptance` disabled) —
   `session["status"]="failed"`, `failure_reason` set, no
   `halt_reason`.
3. **Operator gate ABORT** (`GateDecision.ABORT` on
   `manual_confirm`) — `session["status"]="cancelled"`,
   `failure_reason` set, no `halt_reason`, returns from
   `run_cross_pipeline` early without reaching the metrics + mirror
   writers.

Post-mortem tooling that keys off the ADR 0035 invariant
(`meta.halt_reason` must answer the "why" on every non-`done`
terminal) had to special-case cross runs. Downstream readers that
expect `evidence.json` next to `meta.json` had to grow a fallback
that walks alias subdirs.

## Decision

Mirror the single-project invariants on the cross-project parent
run dir:

1. **Every non-`done` terminal cross-run writes `meta.halt_reason`.**
   Taxonomy aligns with single-run conventions and prefixes with
   `cross_` so callers can route on the parent-vs-child origin:

   | Cross terminal cause | `meta.halt_reason` |
   |---|---|
   | Cross final acceptance rejected | `cross_final_acceptance_failed` |
   | Cross final acceptance parse error | `cross_final_acceptance_parse_error` |
   | Cross final acceptance precondition violation | `cross_final_acceptance_precondition` |
   | `contract_check` blocking skip under `on_skip=block` | `cross_contract_check_blocking_skip` |
   | Operator-aborted manual-confirm gate | `cross_gate_aborted:<gate_name>` |

2. **Every cross terminal writes `evidence.json` + `evidence.md`**
   via `write_bundle_or_placeholder`. The writer first attempts the
   v1 composition; today the bundle collector handles the cross-
   parent dir gracefully and produces a minimal v1 bundle (most
   per-phase slots empty since the parent dir lacks the
   single-run-style phase log), so the file usually carries
   `schema_version="1"`. The placeholder fallback
   (`schema_version="0-placeholder"`) fires only when the
   collector raises (schema mismatch / IO / partial state). Per-
   child curated bundles still live under
   `<run_dir>/<alias>/evidence.json` and remain the authoritative
   source for per-project analysis. Consumers route on
   `schema_version` for shape; the invariant is that the file
   exists with a terminal `status`.

3. **Centralise via a `_finalize_cross_terminal` helper for the
   early-return terminals; the main flow keeps its existing
   finalize block with a defensive `halt_reason` fallback.** The
   helper covers the operator-`ABORT` early return — the only
   terminal that previously skipped its own `meta.json`/evidence
   writes — and centralises:
   - `session["status"]`,
   - `session["halt_reason"]` when status ≠ `done` and the caller
     has not already set it,
   - `meta.json` via `save_session`,
   - `write_bundle_or_placeholder` (suppresses non-fatal errors).

   The `failed`/`done` terminals at the end of
   `run_cross_pipeline` continue to set `status` and
   `halt_reason` inline (the local code already has the richer
   reason taxonomy at hand) and rely on the existing
   `if output_dir:` finalize block, which now grows:
   - a defensive `cross_<status>` fallback so a future terminal
     forgetting to set `halt_reason` still satisfies the
     invariant,
   - a `write_bundle_or_placeholder` call after the metrics rollup
     so the parent bundle exists symmetrically with the helper-
     driven early returns.

   The asymmetry exists because the final block writes
   `metrics.json` aggregated from per-alias sub-runs; that rollup
   has no equivalent on the early-return ABORT path (ABORT fires
   on `contract_check`, before any sub-pipeline runs, so the
   per-alias metrics dictionary is empty). Routing ABORT through
   a helper that wrote an empty metrics.json would be misleading
   noise rather than honest observability.

## Out of scope

- **Full cross-aware evidence bundle composer.** Aggregating per-
  child findings into a parent v1 bundle would require schema work
  beyond the invariant ADR. Tracked as a follow-up if cross runs
  grow rich post-mortem use cases. Today the parent always emits
  the placeholder; per-child bundles cover the per-project view.
- **Cross-project signal/SIGKILL semantics.** Supervisor-side halt
  reason for SIGKILL'd cross subprocess invocations rides on the
  generic single-run mechanism extended by the matching observability
  follow-up in `orcho-mcp` (supervisor halt_reason on abnormal
  exits); no cross-specific work needed.
- **`BlockedPolicy.HALT` activation.** `pipeline/cross_project/types.py`
  declares `BlockedPolicy.HALT` but no caller emits it today. When a
  caller wires it up, that work will extend the taxonomy table above
  with the matching reason.

## Consequences

### Wire-format additions

`meta.json` from cross runs now carries a `halt_reason` key on
non-`done` terminals — same field name and field type (`str | None`)
as the single-run schema, so SDK / MCP / dashboard consumers that
already key off it for single runs pick it up for cross runs without
new code. Cross-run `meta.json` keeps `failure_reason` as legacy free-
form context.

`evidence.json` is now guaranteed to exist in the parent cross-run
dir after finalize. Schema version is `"1"` when the bundle
collector handles the cross-parent dir gracefully (the common
path today) and `"0-placeholder"` when collection raises;
consumers route on `schema_version` the same way as single-run
paths.

### Behavioural changes

The operator-`ABORT` branch (`GateDecision.ABORT` on
`manual_confirm`) now writes `meta.json` with `halt_reason` +
`evidence.json` before returning — previously the early return
skipped `evidence.json`. `metrics.json` is intentionally not
written on this path: ABORT fires on `contract_check`, before any
sub-pipeline starts, so the per-alias metrics rollup is empty.
The terminal `failed`/`done` paths that reach the final block
keep writing `metrics.json` (per-alias rollup) and now also
write `evidence.json` symmetrically.

### Migration

None. The new field appears on new runs; old runs simply lack it
(consumers already treat `halt_reason=None` as "no reason recorded"
per ADR 0035).
