# Observability Surfaces

> Single page for what Orcho records about every prompt / call / run,
> where each record lives, and which CLI surface projects it. Start
> here when you need to read or tune what comes out of a run.

The reference is this page plus the linked source files; tests pin the
JSON shapes.

## What you get for free, by mode

| Mode | What prints | What's in `session.json` |
|---|---|---|
| `--output summary` (default) | end-of-run usage line + peak `Context: X / Y (Z%)` summary | full evidence stack (5 surfaces below) |
| `--output live` | summary + per-invocation live card (phase/cost + Orcho prompt / Provider input / Runtime overhead / Response / Context) | full evidence stack |
| `--output debug` | live + enriched prompt composition block with per-part metrics + Totals | full evidence stack |

The evidence in `session.json` is the **same** in all three modes —
the modes only change what gets *printed* to stdout. Anything visible
in debug is reconstructable from `session.json` after the fact.

For the append-only event timeline (`events.jsonl`) and the canonical
event vocabulary consumed by SDK/MCP/dashboard integrations, see the
[event registry](../reference/event_registry.md).
The active `SILENT` migration checklist of stdout surfaces that do or
do not have typed events is tracked in the stdout-to-event gap register
(internal planning record).

## Five evidence surfaces (in `session.json`)

Every agent invocation that flows through
`pipeline.phases.builtin._session_aware_invoke` stamps the same set
of sibling records into `state.phase_log[phase]`. Session adapters
then promote them into `session.phases.*` with the round-side
`_review` / `_repair` split where applicable.

| Surface | Answers | Path in `session.json` |
|---|---|---|
| `prompt_render` | what bytes went on the wire, what was selected vs omitted in the delta render, which session split fired | `phases.<phase>.prompt_render` |
| `context_growth` | per-call token estimates + lifecycle attribution (kind / trigger / phase / round / surface_id) + render-correlation keys | `phases.<phase>.context_growth` |
| `context_clearing` | what's eligible to clear under the `OutputClass` taxonomy (`clearable_tokens`, `clearable_part_ids`, `retained_part_ids`, `class_counts`) | `phases.<phase>.context_clearing` |
| `context_pressure` | how full the runtime's window is, with explicit source label from the context source hierarchy | `phases.<phase>.context_pressure` |
| `runtime_compaction` | runtime auto-compacted itself (observe-only — present only when the runtime emits the event) | `phases.<phase>.runtime_compaction` |

### Round-side split convention

For the review / repair loop, each side of the round writes its own
sibling records under suffixed keys:

```
session.phases.rounds[].prompt_render_review
session.phases.rounds[].prompt_render_repair
session.phases.rounds[].context_growth_review
session.phases.rounds[].context_growth_repair
session.phases.rounds[].context_clearing_review
session.phases.rounds[].context_clearing_repair
session.phases.rounds[].context_pressure_review
session.phases.rounds[].context_pressure_repair
session.phases.rounds[].runtime_compaction_review
session.phases.rounds[].runtime_compaction_repair
```

The suffix-by-side convention is stable across all five surfaces —
add a sixth and you'd follow the same pattern.

### Cross-call correlation

`prefix_hash` / `payload_hash` / `wire_chars` appear on **all five
surfaces** with byte-identical values for the same invocation. A
consumer joining `prompt_render` + `context_growth` +
`context_clearing` + `context_pressure` (+ optionally
`runtime_compaction`) on these three keys reconstructs a complete
per-call picture.

### Writer-stamped attribution on `prompt_render` (ADR 0035)

In addition to the render-shape fields above, `prompt_render` carries
three writer-stamped attribution slots so each entry is self-locating
without cross-referencing `runner.log`:

| Field | Source | What it answers |
|---|---|---|
| `phase_key` | the `phase` argument to `_session_aware_invoke` | which session key the invocation used — equals `trace_surface` for most surfaces; differs for CHAIN `repair_changes` (`phase_key="implement"` because the repair reuses the implement physical session) |
| `round` | `state.extras[_active_loop_round_key]` with phase-name fallback | the loop counter at invoke time (plan rounds 1/2/3, repair rounds 1/2) — `null` only for single-shot phases that don't sit inside a `LoopStep` |
| `continue_session` | the bool the caller forwarded to the runtime | `True` when the runtime was asked to resume the prior provider session (round-N delta render), `False` on round-1 fresh sessions and after a reset |

These fields close the gap where `evidence.json` `prompt_render[*]`
entries previously surfaced with `round=null` regardless of the
underlying iteration. The strict schema
(`pipeline.evidence.schema.REQUIRED_PROMPT_RENDER_KEYS`) requires
`phase_key` (str) and `continue_session` (bool); `round` remains
optional int. See ADR 0035 for the full contract and rollout
history.

### `prompt_render` durable shape — full field list

`pipeline.observability.prompt_render.DURABLE_FIELDS` is the
M12-stable shape every covered prompt-render trace normalises into.
Below is the complete enumeration (17 fields). Source
of truth: `pipeline/observability/prompt_render.py` `DURABLE_FIELDS`. The
durable trace lives on `PhaseRenderTrace.payload` after extraction;
the evidence summary in `evidence.json` flattens and projects it
further (see [SDK API reference](../reference/sdk_api.md) for the
summary field set).

| Field | Type | Origin | Notes |
|---|---|---|---|
| `render_mode` | `str` (`"full"` \| `"delta"`) | M6 selector | what wire shape went on the wire |
| `session_split` | `str` (split enum) | active step's prompt-session policy | `per_phase` / `per_role` / `common` / `stateless` |
| `physical_session_key` | `dict` \| `None` | renamed from source `session_key` | `{scope, run_id, runtime, model_key}`; `None` on STATELESS |
| `provider_session_id` | `str` \| `None` | `agent.session_id` at invoke time | what the provider gave us; persisted per-role to `checkpoints.db` (ADR 0036) |
| `part_ids` | `list[str]` | envelope full ordered set in `{id}@{version}` form | the source prompt; `part_ids ⊇ selected ∪ omitted ∪ delta_dropped`, in render order |
| `selected_part_keys` | `list[str]` | selector output | what made it onto the wire |
| `omitted_part_keys` | `list[str]` | selector output | what the selector skipped as cached |
| `delta_dropped_part_keys` | `list[str]` | selector output (ADR 0063) | parts dropped from the wire on a resumed turn because the runtime already holds them in history (e.g. the task on replan); empty on full renders |
| `prefix_hash` | `str` | envelope hash | byte-identical across all five sibling surfaces for the same invocation |
| `payload_hash` | `str` | envelope hash | same correlation key as `prefix_hash` |
| `wire_chars` | `int` | `len(wire_prompt)` | same correlation key |
| `execution_mode` | `str` | defaults to `"linear"` (ADR 0027 fanout reservation) | writer never stamps yet; pre-fanout sessions always linear |
| `surface_id` | `str` \| `None` | defaults to `None` (ADR 0027 reservation) | reserved for fanout work |
| `surface_count` | `int` | defaults to `1` (ADR 0027 reservation) | reserved for fanout work |
| `phase_key` | `str` \| `None` | writer-stamped, ADR 0035 | session-key phase argument; differs from `trace_surface` only on CHAIN `repair_changes` |
| `round` | `int` \| `None` | writer-stamped, ADR 0035 | loop counter at invoke time; `None` for single-shot phases outside a `LoopStep` |
| `continue_session` | `bool` \| `None` | writer-stamped, ADR 0035 | `True` when resuming the prior provider session |

The evidence summary (`evidence/prompt_render.py:summarize_trace_for_evidence`)
projects this durable shape into a flatter consumer-facing dict:

* `physical_session_key` → flattened into `session_scope` /
  `session_run_id` / `session_runtime` / `session_model`.
* `selected_part_keys` / `omitted_part_keys` / `delta_dropped_part_keys`
  → counts only (`selected_count`, `omitted_count`, `delta_dropped_count`).
* `part_ids` → **dropped** from the summary (lives on the durable
  trace only).
* `phase_key` falls back to `trace.phase` if writer didn't stamp.

For the four sibling surfaces (`context_growth`, `context_clearing`,
`context_pressure`, `runtime_compaction`) each declares its own
`DURABLE_FIELDS` tuple in the corresponding
`pipeline/observability/<surface>.py` module. They share the
cross-call correlation triple (`prefix_hash` / `payload_hash` /
`wire_chars`) and the `phase` / `round` / `surface_id` attribution
slots — consumers joining on those three keys reconstruct a complete
per-invocation picture across surfaces.

## The taxonomy layer

`pipeline.observability.output_class.OutputClass` is **pure
classification, no policy**. Four buckets:

| Class | Example | Default lifecycle |
|---|---|---|
| `RE_FETCHABLE` | file reads, search results, repeatable command output | eligible for clearing after a bounded keep window |
| `PERSISTED_ARTIFACT` | plan files, review JSON, saved patches | active payload clearable after artifact path + digest are recorded |
| `EPHEMERAL` | scratch tool reasoning, transient notes | not cleared unless summarised first |
| `DECISION_BEARING` | findings, blockers, accepted plan summary | never silently cleared |

The taxonomy is the load-bearing **foundation** under `context_clearing`
(clearing eligibility reads it) and the debug-block Totals line
(renders the per-class breakdown of the rendered prompt). A future
cross-session memory primitive will read it to decide what to persist.

Classifying without acting was a deliberate split: clearing
without a reliable taxonomy is dangerous (silently dropping a
decision-bearing finding is worse than not clearing at all).

## Context source hierarchy

`context_pressure.context_source` is one of these labels, in
descending authority:

```
runtime_reported  > provider_usage > orcho_estimated > config_static > unknown
```

`runtime_reported` activates when the runtime adapter exposes a
live `last_context_window_tokens` + `last_context_used_tokens`
pair. **Claude does** (reads
`result.modelUsage[<model>].contextWindow` from the CLI's
stream-json output). **Codex does not** — its `turn.completed.usage`
exposes no window field, so the resolver falls through to the
next available source.

The label is **load-bearing**. Today `context_pressure` is observe-only:
no shipped policy automatically clears, compacts, swaps models, or halts
from this field. If a future policy reads it, that policy must refuse to
act on `unknown` / `config_static` without explicit opt-in.

## Three CLI surfaces

| Surface | Activated by | What it shows |
|---|---|---|
| Live per-call card | `--output live` / `debug` | per-agent-invoke card: phase / round / duration / cost + Orcho prompt / Provider input / Runtime overhead / Response / Context lines (each rendered only when it has data); cache annotation distinguishes warm (`N% cached`), cold (`N% cached, M% priming`), and `prefix changed`; ⚠ `approaching limit` at 80% context fill |
| Debug composition block | `--output debug` | enriched `Incoming prompt` block: manifest with per-part tokens + cache scope; Totals line with per-class breakdown; frame headings carry `· N tok · cache=<scope> · class=<class>` |
| End-of-run Context summary | always | one line after the existing `Usage:` line: `Context: 193.6k / 1.0M (19%) [runtime_reported plan]` — peak fill ratio across all stamped surfaces |

All three are **print-only**: they do not modify `session.json`,
`evidence.json`, or `metrics.json`. They are pure projections of
the evidence stack. A future evidence consumer (MCP, dashboard,
lab probe) can compute the same summaries from the JSON without
contention.

## subtask_dag live markers

The `subtask_dag` implement executor prints per-subtask milestones to the live
agent log (visible under `--output live` / `debug`; `summary` keeps stdout echo
off). These come from `agents.stream.write_agent_log_section`, not from a child
process:

- `ORCHO subtask N/M START: <id>` — goal, runtime, model, skill, `current_only`,
  `execution_context`, `prompt_turn`, `upstream_deps`, `prompt_chars`.
- `ORCHO subtask N/M DONE: <id>` — session/render facts (`session_split`,
  `continue_session`, `render_mode`) and the attestation outcome
  (`attestation: met` / `incomplete (<reason>)`).
- **Attestation block** — right after the DONE marker, a criteria-bearing
  subtask renders an `ORCHO subtask N/M ATTESTATION (met|INCOMPLETE): <id>`
  block with one `✓`/`✗` line per criterion + evidence + summary (the developer's
  delivery proof, legible at a glance instead of buried in the transcript).

The **phase banner** (`[IMPLEMENT] … developer applies the change`) prints once
at the phase boundary; subtasks are steps within the one implement phase, so the
banner is not re-synthesised above every subtask invocation — each subtask's
`START` marker is its own section title.

## Layering / data flow

```
              ┌──────────────────────────────┐
              │  agent (Claude / Codex / …)  │
              └──────────────┬───────────────┘
                             │ stamps last_* attributes after invoke
                             ▼
        ┌─────────────────────────────────────────┐
        │ adapter (claude.py / codex.py / …)      │
        │   writes last_context_*_tokens          │
        └──────────────────┬──────────────────────┘
                           │
                           ▼
   ┌──────────────────────────────────────────────────┐
   │ _session_aware_invoke (pipeline.phases.builtin)  │
   │   stamps 4-5 sibling surfaces into phase_log:    │
   │     prompt_render, context_growth,               │
   │     context_clearing, context_pressure,          │
   │     runtime_compaction (when present)            │
   └──────────────────┬───────────────────────────────┘
                      │
                      ▼ (handlers overwrite phase_log[phase]
                      │  → _carry_trace_metadata preserves
                      │  the five trace keys across overwrites)
                      │
                      ▼
   ┌──────────────────────────────────────────────────┐
   │ session adapters (pipeline.session_adapters)     │
   │   _copy_prompt_render / _copy_context_growth /   │
   │   _copy_context_clearing / _copy_context_pressure│
   │   / _copy_runtime_compaction promote each key.   │
   │   RoundAdapter writes _review / _repair split.   │
   └──────────────────┬───────────────────────────────┘
                      │
                      ▼
              ┌────────────────────────┐
              │ session.json           │
              │   phases.<phase>.*     │
              │   phases.rounds[].*    │
              └───────┬────────────────┘
                      │
        ┌─────────────┴───────────────┐
        ▼                             ▼
┌────────────────────┐    ┌─────────────────────────┐
│ CLI surfaces       │    │ evidence consumers      │
│  live card         │    │  orcho_run_evidence     │
│  debug block       │    │  MCP / web dashboard    │
│  Context summary   │    │  orcho-lab probes       │
└────────────────────┘    └─────────────────────────┘
```

## Wayfinder — "if you want X, read Y"

| Question | Read |
|---|---|
| How does Orcho sort prompt parts? | `pipeline/prompts/composer.py::assemble_cache_first_segments` plus the cache-first invariant described above. |
| Which contracts are code-owned vs user-editable? | `pipeline/prompts/contracts.py` + `tests/unit/pipeline/prompts/test_prompt_boundary.py`; the boundary itself is documented in [`core/_prompts/README.md`](../../core/_prompts/README.md). |
| Where is runtime context fullness recorded? | `pipeline/observability/context_pressure.py`; the JSON shape lands at `phases.<phase>.context_pressure`. |
| What class is my prompt part in? | `pipeline.observability.output_class.classify_prompt_part` |
| Why are these surfaces observe-only? | The taxonomy and pressure records are evidence first: Orcho classifies and measures before enabling any lossy clearing, compaction, model-swap, or halt policy. |
| What's in the live card I'm seeing? | This page's "Three CLI surfaces" and "Practical tuning guide" sections. |
| Why does the cache hit rate drop on this phase? | `--output debug`, read the Composition manifest's `cache=<scope>` per part; a non-GLOBAL part early in the prefix invalidates Anthropic's cache |
| What does `runtime_reported` vs `orcho_estimated` mean? | See "Context source hierarchy" above: `runtime_reported` comes from runtime-used/window telemetry; `orcho_estimated` is Orcho's local estimate when the runtime does not expose a window. |
| Did the runtime auto-compact this turn? | Check `session.phases.<phase>.runtime_compaction`; the key is absent when the runtime emitted no compaction event. |
| How is per-call duration measured? | `_session_aware_invoke` wraps `agent.invoke` with `time.monotonic()` — user-perceived turn-around, includes runtime-internal retries |
| Which events should an MCP/SILENT consumer follow? | [`docs/reference/event_registry.md`](../reference/event_registry.md); `agent.tool_use`, `agent.mcp_tool_call`, `agent.contract_ready`, and `agent.summary` are the live agent signals. |

## Practical tuning guide

When you see a live card like:

```text
✓ implement · 29.0s · $0.225
    Orcho prompt   1.5k tokens
    Provider input  167.6k tokens (90% cached, ~$0.18 saved)
    Runtime overhead  166.1k tokens (99% of input — agent system prompt + tools, not Orcho-built)
    Response  1.8k tokens
    Context   167.6k / 1.0M (17% full)
```

read it in passes:

1. **Orcho prompt** is what Orcho itself assembled (the parts shown in the
   `--output debug` Composition manifest). A tiny task can still produce a
   larger Orcho prompt when it includes stable contracts, role/task prose,
   project context, accepted plans, handoff artifacts, reviewed files, or
   repair feedback. **Provider input** is what the runtime actually received;
   **Runtime overhead** is the gap (`Provider input − Orcho prompt`) — the
   agent CLI's own system prompt + tool-definition schemas, which Orcho
   neither builds nor controls. The line is shown only when the gap is
   meaningfully positive, so a reader can tell at a glance that Orcho-owned
   parts are a fraction of the real wire prompt.
2. **Cache percentage** is `cache_read_tokens / tokens_in` when the
   runtime reports cache-read tokens. The annotation distinguishes three
   states using the three-way split (read / creation / fresh): a warm hit
   (`N% cached, ~$X saved`); a cold/priming call that wrote most of the
   prompt into cache this turn (`N% cached, M% priming` — NOT a problem);
   and a genuine `prefix changed` (low coverage = a large fresh remainder,
   the cacheable prefix was invalidated). A low read % alone is **not**
   `prefix changed` — a first/cold call shows low read while priming. To
   improve warm-cache reuse, keep broad-scope parts stable and early: avoid
   moving dynamic artifact/feedback/task bodies into role/task/format
   markdown, and inspect `cache=<scope>` in debug output when the live card
   actually says `prefix changed`.
3. **Context fullness** is the best available reading from the source
   hierarchy above. Trust `runtime_reported` first because the runtime exposed
   live used/window tokens. Treat `orcho_estimated` as a useful Orcho-side
   estimate, not a provider truth. `config_static` and `unknown` are not
   strong enough for automatic lossy action.

Common questions:

| Question | What to inspect | What to do |
|---|---|---|
| Why did this phase send so many tokens? | `--output debug` Totals line and per-frame headings. Look for large `artifact`, `plan_contract`, `feedback`, or `context` parts. | If the large part is expected, rely on cache/context evidence. If it is accidental, move static prose back to role/task/format or move dynamic payload into an explicit typed part. |
| Why did cache hit rate drop? | Live card annotation: `prefix changed` is a real drop (low coverage / large fresh remainder); `N% priming` is a cold first call writing the cache, NOT a drop — ignore it. Then debug manifest `cache=<scope>` order; `prompt_render.prefix_hash` across calls. | On a real `prefix changed`, find the first changed or non-cacheable leading part. Stable contracts/roles/formats should remain broad-scope and byte-stable. A `priming` call needs no action — the next resumed call reads it back. |
| Which context source should I trust? | `context_pressure.context_source`. | `runtime_reported` > `provider_usage` > `orcho_estimated` > `config_static` > `unknown`. |
| How do I choose different models per phase? | `core/infra/config.py` phase map or env vars such as `MODEL_IMPLEMENT`, `MODEL_REVIEW_CHANGES`, `RUNTIME_IMPLEMENT`, `RUNTIME_REVIEW_CHANGES`. | Set phase-specific runtime/model values when implementation and review need different cost, speed, or reasoning profiles. |
| What if context is approaching the limit? | Live card warning at 80% context fill; `session.json` `context_pressure` entries. | Today this is observe-only. Preserve the run evidence, inspect which parts are growing, and choose a runtime/model/profile change manually. Future policy should act only on strong source labels. |

The rule of thumb: use live mode for quick anomaly detection, debug mode
for attribution, and `session.json` when you need to compare calls after
the run.

## See also

- [`pipeline/prompts/composer.py`](../../pipeline/prompts/composer.py)
  — cache-first wire assembly
- [`pipeline/prompts/contracts.py`](../../pipeline/prompts/contracts.py)
  — code-owned system-tail blocks
- [`pipeline/observability/output_class.py`](../../pipeline/observability/output_class.py)
  — `OutputClass` taxonomy
- [`pipeline/observability/context_pressure.py`](../../pipeline/observability/context_pressure.py)
  — context source hierarchy + resolver
- [`core/observability/live_card.py`](../../core/observability/live_card.py)
  — CLI live-card rendering
- [Event registry](../reference/event_registry.md)
  — canonical `events.jsonl` kinds and required payload keys
- [`tests/unit/pipeline/prompts/test_prompt_boundary.py`](../../tests/unit/pipeline/prompts/test_prompt_boundary.py)
  — boundary invariants pinned in tests

## Reserved for future slices

- Cross-session memory primitive — will read the `OutputClass`
  taxonomy to decide what to persist across sessions.
- Lab coverage — will exercise the full evidence stack against
  real long-running agents.
- Execution-surface fanout — `surface_id` / `surface_count` slots
  stay `None` / `1` across all surfaces until fanout activates them;
  the slots are already part of every durable shape so promotion is
  additive.
- Auto-policy on `context_pressure` — no consumer reads the
  surface to drive action today; a future profile knob can trigger
  compaction / model swap / halt with resume hint when
  `context_fill_ratio` crosses a threshold from a
  `runtime_reported` or `provider_usage` source.
