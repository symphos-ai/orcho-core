# ADR 0029 — Context Lifecycle for Long-Running Agents

Date: 2026-05-17
Status: Accepted / implemented through M14.4.5
Supersedes: none
Related: ADR 0020 (run evidence), ADR 0026 (session-aware prompt parts),
ADR 0027 (execution surfaces), ADR 0028 (cache-first physical wire layout)

Implementation note (2026-05-18): `context_growth`, `output_class`,
`context_clearing`, `context_pressure`, runtime-compaction evidence, live-card
output, and debug per-part metrics have landed through M14.4.5. Memory and
evaluation coverage remain planned slices.

## Context

ADR 0026 made prompt rendering observable: Orcho can describe selected
prompt parts, stable prefixes, payload parts, hashes, and session split.
ADR 0028 makes the physical prompt order match the cache model so provider
prefix caches can reuse the broadest possible leading bytes.

That solves prompt layout. It does not solve **context lifecycle**.

Long-running coding agents accumulate several kinds of context:

- stable instructions and contracts;
- user task text and phase inputs;
- file reads, search results, command output, diffs, and generated artifacts;
- assistant reasoning / summaries / decisions;
- phase evidence and review findings;
- state that should survive a context reset or a later session.

These categories have different lifecycle rules. Some content should stay in
the active window. Some can be re-read from disk or artifacts and safely
cleared from active context. Some should be summarized before compaction.
Some should be persisted as structured memory so a later session can resume
without rediscovering the same facts.

Anthropic's "Context engineering: memory, compaction, and tool clearing"
cookbook frames these as three distinct primitives:

- tool-result clearing removes stale, re-fetchable tool payloads while
  retaining the fact that the tool call happened;
- compaction summarizes the current conversation when the window grows too
  large;
- memory stores selected notes outside the active context so future sessions
  can recover state.

The useful question is not whether Orcho should enable every primitive for
every run. The useful question is which context problem the workload is
actually hitting, and whether Orcho has enough evidence to tune the policy.

## Decision

Introduce a provider-neutral **context lifecycle layer** for long-running
agent runs. The layer is separate from prompt composition:

```text
prompt composition       -> what bytes are sent now
context lifecycle        -> what prior bytes stay active, are cleared,
                            are compacted, or are persisted
run evidence             -> what happened and why
```

Context lifecycle policy has three optional primitives.

### 1. Tool-result clearing

Orcho classifies tool and runtime outputs by whether they are re-fetchable.

| Class | Examples | Default lifecycle |
|---|---|---|
| Re-fetchable | file reads, search results, repeatable local command output, repeatable API reads | eligible for clearing after a bounded keep window |
| Persisted artifact | plan files, review JSON, release gate JSON, saved patch/diff artifacts | active payload can be cleared after artifact path + digest are recorded |
| Ephemeral | upload-only data, non-repeatable external state, transient command output not saved elsewhere | not cleared unless first summarized or persisted |
| Decision-bearing | accepted plans, review findings, final gate blockers, unresolved assumptions | never dropped silently; summarize or persist before clearing |

Clearing must never remove stable prompt prefix bytes. It targets historical
tool results / payload content, not the current prompt's cacheable instruction
tier.

### 2. Compaction

Compaction is a lossy operation and therefore needs a coding-agent-specific
contract. Orcho should preserve at least:

- current task and acceptance criteria;
- approved plan and non-goals;
- files read, files changed, and why they matter;
- code identifiers, schemas, commands, exact error messages, and test names
  that remain relevant;
- commands/tests already run and their outcomes;
- review findings, release blockers, and verification gaps;
- assumptions, unresolved questions, and known risks;
- phase, round, surface id, and session split metadata.

Compaction summaries are evidence-bearing artifacts. They need their own
hash/digest and should reference the source span they summarized.

### 3. Memory

Memory is structured note-taking outside the active context. It is not a
replacement for run evidence or artifacts. It is a lightweight recovery surface
for future turns/sessions.

Initial memory scope should be project/run oriented, not global preference
storage. Useful files include:

- progress log;
- accepted plan summary;
- active assumptions and non-goals;
- touched/read file index;
- verification status;
- unresolved blockers and follow-up checks.

Memory must stay bounded and organized. Stale entries should be replaced or
deleted instead of accumulating overlapping notes.

## Evidence Model

Every context lifecycle operation emits structured evidence:

| Field | Meaning |
|---|---|
| `kind` | `clear_tool_results`, `compact`, `memory_read`, `memory_write`, `memory_delete` |
| `phase` / `round` / `surface_id` | Where the event happened |
| `trigger` | token threshold, explicit policy, manual request, session resume |
| `input_tokens_before` / `input_tokens_after` | Context size estimate when available |
| `context_source` | Source used for context-pressure decisions: `runtime_reported`, `provider_usage`, `orcho_estimated`, `config_static`, or `unknown` |
| `context_window_tokens` | Effective runtime/model context window when known |
| `context_used_tokens` | Runtime-reported or estimated live context usage |
| `context_remaining_tokens` | Runtime-reported or derived remaining context budget |
| `context_fill_ratio` | `context_used_tokens / context_window_tokens` when both values are meaningful |
| `cleared_tokens` | Estimated removed tool-result tokens |
| `summary_tokens` | Tokens in compaction summary |
| `tool_use_count` | Number of tool results affected |
| `artifact_refs` | Paths/digests that make clearing recoverable |
| `cache_effect` | Whether the event invalidated provider cache or changed selected prefix/payload |

Provider-specific token buckets may remain in runtime metadata, but evidence
should expose provider-neutral totals wherever possible.

### Runtime context source hierarchy

Context lifecycle policy must not treat static model-window config as
authoritative when a runtime can report live context pressure. Claude, Codex,
and other agent runtimes may expose context-fullness indicators that include
session history, tool-result payloads, hidden runtime overhead, attachments,
cached-prefix accounting, and provider-side compaction state that Orcho cannot
derive from rendered prompt bytes alone.

Policy decisions use this source hierarchy:

```text
runtime_reported  -> runtime exposes live used/remaining/window context
provider_usage    -> provider usage buckets + known model window
orcho_estimated   -> Orcho prompt/session estimate
config_static     -> configured model window / trigger token fallback
unknown           -> no automatic lossy action
```

`trigger_tokens` values are fallback thresholds and policy hints. They are not
the source of truth when runtime-reported context pressure is available.
Automatic lossy actions should prefer ratio-based triggers such as
`context_fill_ratio >= 0.75`; static token thresholds apply only when the
runtime cannot report a meaningful fill ratio.

## Policy Surface

Profiles may eventually expose a `context_lifecycle` policy block. The exact
schema is deferred, but the shape should be explicit rather than hidden in
prompt text:

```json
{
  "context_lifecycle": {
    "clearing": {
      "enabled": true,
      "trigger_tokens": 100000,
      "keep_recent_tool_uses": 3
    },
    "compaction": {
      "enabled": true,
      "trigger_fill_ratio": 0.75,
      "fallback_trigger_tokens": 150000,
      "contract": "coding_agent_v1"
    },
    "memory": {
      "enabled": false,
      "scope": "project_run"
    }
  }
}
```

The default policy should be conservative:

- collect context-growth evidence first;
- prefer runtime-reported context pressure over static config thresholds;
- treat configured token thresholds as fallback only;
- do not clear non-recoverable content;
- do not enable cross-session memory globally by default;
- prefer explicit profile opt-in before lossy compaction affects phase
  behavior.

## Interaction With Prompt Engine

Context lifecycle must respect ADR 0028:

- stable prefix parts remain stable and early;
- dynamic tool and artifact bodies remain payload;
- clearing and compaction operate on historical payload/context, not on
  editable role/task/format definitions;
- selected/omitted prompt parts, prefix hash, payload hash, and context
  lifecycle events are correlated in evidence.

Prompt contracts may define compaction and memory-writing instructions, but
the decision to clear/compact/persist belongs to runtime/profile policy.

## Interaction With ADR 0027

Execution surfaces make context lifecycle more important. Fanout review can
produce multiple tool/result streams and multiple prompt-render records inside
one phase.

The lifecycle layer must preserve surface attribution:

- clearing events know which surface produced the cleared content;
- compaction summaries retain surface ids for findings and checks;
- memory writes avoid merging unrelated surface conclusions without labels.

## Non-Goals

- No provider-specific API commitment in this ADR.
- No broad prompt prose rewrite.
- No replacement of run evidence, artifacts, or phase logs.
- No automatic global user preference memory.
- No clearing of ephemeral data unless the data is first summarized or
  persisted.
- No hidden behavior change for short sessions that naturally stay under the
  context limit.

## Consequences

### Positive

- Orcho can distinguish prompt layout problems from context-growth problems.
- Long-running runs get bounded working sets without relying only on larger
  model context windows.
- Evidence can show whether clearing, compaction, or memory helped quality,
  latency, and cost.
- Future evaluation cases can test context policies against realistic
  coding-agent workloads.

### Negative / cost

- More policy knobs and more evidence fields to validate.
- Compaction introduces controlled lossiness and must be tested with real
  coding-agent cases.
- Memory requires storage hygiene and stale-note handling.
- Clearing can cause redundant re-reads if thresholds or keep windows are too
  aggressive.

### Risks

| ID | Risk | Mitigation |
|---|---|---|
| R1 | Compaction drops a subtle technical decision | Coding-agent compaction contract plus evaluation probes that ask for preserved details |
| R2 | Clearing removes data that cannot be recovered | Re-fetchability classification; persist artifact path + digest before clearing |
| R3 | Memory becomes stale or overbroad | Project/run scope first; bounded files; replace stale notes |
| R4 | Context lifecycle events obscure cache evidence | Record cache effect and correlate events with prompt hashes |
| R5 | Fanout surfaces lose attribution after compaction | Preserve `surface_id` in summaries and memory writes |

## Test Suite Authority

Initial implementation should add fake-provider tests for:

- re-fetchable vs non-re-fetchable output classification;
- clearing evidence records with token deltas and artifact refs;
- compaction summary contract preserving files, commands, findings, blockers,
  and assumptions;
- memory write/read/delete evidence;
- fanout surface attribution through clearing/compaction;
- prompt-render evidence correlation before and after lifecycle events.

An external evaluation bench should exercise long-running coding-agent cases
before lossy context lifecycle behavior is enabled by default.

## Plan Reference

Execution slices and step boundaries are tracked in the context-lifecycle
planning record (internal, not shipped with this repo).
