# Event registry

> Reference for the canonical events written to `events.jsonl`.
> This is the consumer-facing companion to
> `core/observability/event_kinds.py`.

`events.jsonl` is the append-only run timeline. CLI output is only one
presentation of that timeline; headless consumers such as SDK readers,
MCP adapters, and dashboards should read events and persisted artifacts
instead of scraping terminal text.

Source of truth:

* Event names and required payload keys:
  `core/observability/event_kinds.py`
* Payload validation tests:
  `tests/unit/core/test_event_kinds.py`
* Runtime stream parsers:
  `agents/stream_parsers/claude_jsonl.py`
  and `agents/stream_parsers/codex_jsonl.py`

Extra payload fields are allowed. Required keys are a lower bound, not a
complete schema. Unknown event kinds are accepted so plugin authors can
emit advisory events without patching core first.

## All events at a glance

This table is the primary registry. "SILENT-ready" means the event is
written through the event store and does not depend on terminal stdout.
Some rows are naturally conditional: they appear only when that phase,
gate, runtime, or tool path actually runs.

| Event | Description | Required keys | SILENT-ready |
|---|---|---|---|
| `run.start` | Run lifecycle opened. | `task`, `run_kind` | Yes |
| `run.end` | Run lifecycle closed with final status. | `status` | Yes |
| `phase.start` | Phase boundary opened. | `title` | Yes |
| `phase.end` | Phase boundary closed. | `title`, `outcome` | Yes |
| `agent.start` | Runtime invocation opened. | `agent`, `model` | Yes |
| `agent.end` | Runtime invocation closed. | `agent` | Yes |
| `agent.text` | Human assistant prose. Typed JSON contracts are not emitted here. | none | Yes, prose only |
| `agent.skill_use` | Registered skill selection detected in assistant prose. | `skill_name`, `text` | Yes |
| `agent.contract_ready` | First chunk of a JSON contract answer was produced and handed to parsing. | `agent`, `format` | Yes |
| `agent.tool_use` | Built-in tool invocation such as shell, file read, or search. | none | Yes |
| `agent.mcp_tool_call` | Runtime-side MCP tool invocation. | `server`, `tool_name`, `status` | Yes |
| `agent.summary` | Runtime usage/cost summary when available. | none | Yes, if runtime reports it |
| `agent.guardrail` | Runtime safety guard fired. | `agent`, `guardrail`, `action` | Yes |
| `hypothesis.proposed` | Pre-plan hypothesis text proposed. | `attempt`, `max`, `text` | Yes, if hypothesis runs |
| `hypothesis.verdict` | Hypothesis reviewer verdict. | `attempt`, `approved` | Yes, if hypothesis runs |
| `hypothesis.exhausted` | All configured hypothesis attempts rejected. | `attempts`, `max` | Yes, if hypothesis runs |
| `validate_plan.verdict` | Single-project plan validation verdict. | `attempt`, `approved` | Yes, if validation runs |
| `phase.handoff_requested` | Phase-level pause requested for external decision. | `phase`, `handoff_type`, `trigger`, `round`, `handoff_id` | Yes |
| `cross_validate_plan.verdict` | Cross-plan validation verdict. | `attempt`, `approved` | Yes, if cross validation runs |
| `cross_final_acceptance.verdict` | Cross final acceptance verdict. | `approved`, `verdict`, `ship_ready`, `source`, `short_summary` | Yes, if gate runs |
| `cross.delivery.started` | Cross-level commit delivery loop opened. | `project_count` | Yes, if delivery runs |
| `cross.delivery.alias_committed` | One alias reached a success-like delivery status. | `alias`, `status` | Yes, if delivery runs |
| `cross.delivery.alias_failed` | One alias reached a failure-like delivery status, or the operator halted. | `alias`, `status` | Yes, if delivery runs |
| `cross.delivery.completed` | Cross-level delivery loop closed with the aggregate verdict. | `overall` | Yes, if delivery runs |
| `subtask.start` | `subtask_dag` subtask opened. | `subtask_id`, `index`, `total`, `goal`, `runtime`, `model`, `skill`, `depends_on` | Yes, if subtask_dag subtasks run |
| `subtask.end` | `subtask_dag` subtask closed. | `subtask_id`, `index`, `total`, `goal`, `ok`, `error`, `attestation_error`, `duration` | Yes, if subtask_dag subtasks run |
| `subtask.receipt` | Terminal per-subtask delivery receipt. | `subtask_id`, `state`, `done_criteria`, `criteria_report`, `attestation_summary`, `attestation_error`, `attestation_repaired`, `depends_on`, `duration` | Yes, if subtask_dag subtasks run |
| `plan.parsed` | PLAN output parsed into the typed plan shape. | `source`, `short_summary`, `planning_context`, `subtask_count`, `has_contract` | Yes, if PLAN runs |
| `gate.start` | Quality gate opened. | `name`, `gate_kind` | Yes, if gate runs |
| `gate.end` | Quality gate closed. | `name`, `outcome`, `duration_s` | Yes, if gate runs |
| `command.start` | Orchestrator-driven shell command opened. | `argv_summary`, `cwd` | Yes, if command runs |
| `command.end` | Orchestrator-driven shell command closed. | `exit_code`, `duration_s`, `outcome` | Yes, if command runs |
| `artifact.created` | Durable run artifact written. | `path`, `artifact_kind` | Yes, if artifact is written |

## Consumer rules

* Use `run.start` and `run.end` for run lifecycle.
* Use `phase.start` and `phase.end` for phase boundaries.
* Use `agent.tool_use` and `agent.mcp_tool_call` for live tool activity.
  These events are emitted from the JSONL parser path and do not depend
  on stdout rendering, so they are preserved under `SILENT`.
* Use `agent.summary` for runtime usage summaries when the runtime emits
  them.
* Use `agent.contract_ready` to know that a JSON contract response was
  produced and handed to the parser. The raw JSON contract is not emitted
  as `agent.text`; the parsed result lands in `meta.json`/`session.phases`
  and phase-specific verdict events.
* Use `agent.text` only for human prose. Do not expect typed JSON
  contracts there.
* Use `phase.handoff_requested` for pause/resume UX; do not infer handoff
  state from terminal banners.

## Silent mode

`PresentationPolicy.SILENT` suppresses terminal presentation. It must not
suppress event-store writes. In silent runs:

| Signal | Event |
|---|---|
| Run lifecycle | `run.start`, `run.end` |
| Phase lifecycle | `phase.start`, `phase.end` |
| Built-in tool invocation | `agent.tool_use` |
| Runtime MCP tool invocation | `agent.mcp_tool_call` |
| JSON contract answer ready | `agent.contract_ready` |
| Usage summary | `agent.summary` |
| Handoff pause | `phase.handoff_requested` |

Terminal-only conveniences such as banners, live cards, and rendered
review blocks are not part of the event contract. Consumers needing
phase output should read `meta.json`, `evidence.json`, and the phase
artifact files listed in [run artifacts](run_artifacts.md).

The migration checklist of stdout-rendered signals that are
artifact-backed, event-backed, or still missing a typed event is
tracked in the stdout-to-event gap register (internal planning record).
The longer-term delivery model, including web reactive subscriptions
and MCP wake acceleration, is tracked in the reactive event delivery
plan (internal planning record).

## Registry

### Run lifecycle

| Event | Required keys | Notes |
|---|---|---|
| `run.start` | `task`, `run_kind` | `run_kind` is `single_project` or `cross_project`; see discriminated fields below. |
| `run.end` | `status` | Final run status as persisted by finalization. |

`run.start` has additional validation by `run_kind`:

| `run_kind` | Additional required keys |
|---|---|
| `single_project` | `project`, `profile` |
| `cross_project` | `cross_mode`, `profile`, `plan_source`, `projects` |

For `single_project`, `parent_run_id` and `project_alias` are optional
but must appear together. For `cross_project`, each `projects[]` entry
must contain `alias` and `path`.

### Phase lifecycle

| Event | Required keys | Notes |
|---|---|---|
| `phase.start` | `title` | Emitted when a phase boundary starts. |
| `phase.end` | `title`, `outcome` | Emitted when a phase boundary closes. |

### Agent runtime

| Event | Required keys | Notes |
|---|---|---|
| `agent.start` | `agent`, `model` | Invocation boundary. |
| `agent.end` | `agent` | Invocation boundary. |
| `agent.text` | none | Human prose only; typed JSON contracts are omitted. |
| `agent.skill_use` | `skill_name`, `text` | Registered skill selection detected in assistant prose. |
| `agent.contract_ready` | `agent`, `format` | First chunk of a JSON contract response was produced; `format` is currently `json`. |
| `agent.tool_use` | none | Built-in tool call such as shell, file read, or search. Payload is normalized by `tool_invocations.py`. |
| `agent.mcp_tool_call` | `server`, `tool_name`, `status` | Runtime-side MCP tool call. |
| `agent.summary` | none | Runtime usage/cost summary when available. |
| `agent.guardrail` | `agent`, `guardrail`, `action` | Runtime safety guard fired. |

Common `agent.tool_use` payload fields include `tool_name`,
`tool_category`, `display_name`, `summary`, `input`, and optional
`agent`. They are not all required because different runtimes report
different detail levels.

Common `agent.summary` payload fields include `usage`, `input_tokens`,
`output_tokens`, cached-token fields, session ids, and cost fields when
the runtime reports them.

### Hypothesis

| Event | Required keys | Notes |
|---|---|---|
| `hypothesis.proposed` | `attempt`, `max`, `text` | Proposed pre-plan direction. |
| `hypothesis.verdict` | `attempt`, `approved` | Reviewer verdict for the hypothesis. |
| `hypothesis.exhausted` | `attempts`, `max` | All configured hypothesis attempts rejected. |

### Validation and handoff

| Event | Required keys | Notes |
|---|---|---|
| `validate_plan.verdict` | `attempt`, `approved` | Single-project plan validation verdict. |
| `phase.handoff_requested` | `phase`, `handoff_type`, `trigger`, `round`, `handoff_id` | Generic phase-level handoff pause. |

### Cross-project gates

| Event | Required keys | Notes |
|---|---|---|
| `cross_validate_plan.verdict` | `attempt`, `approved` | Cross-plan validation verdict. |
| `cross_final_acceptance.verdict` | `approved`, `verdict`, `ship_ready`, `source`, `short_summary` | Terminal cross release gate verdict. |

### Cross-level commit delivery

Emitted by the cross delivery loop (`pipeline/cross_project/cross_delivery.py`)
after the cross release gate approves or the operator overrides a REJECTED
verdict. All four fire strictly before `run.end`.

| Event | Required keys | Notes |
|---|---|---|
| `cross.delivery.started` | `project_count` | Per-alias delivery loop opened. |
| `cross.delivery.alias_committed` | `alias`, `status` | Alias reached `committed` / `applied_uncommitted` / `no_diff` / `skipped` / `skipped_already_delivered`. `commit_sha` present for `committed`. |
| `cross.delivery.alias_failed` | `alias`, `status` | Alias reached `target_dirty` / `commit_failed` / `apply_failed` / `not_applicable`, or `halted` (operator stop). `error` carries the reason. |
| `cross.delivery.completed` | `overall` | Aggregate verdict (`ok` / `partial` / `failed` / `halted` / `disabled`) the finalizer maps to a terminal status. |

### Subtask DAG

Emitted by the `subtask_dag` implement executor (one set per planned subtask).
`index` (1-based) and `total` (whole-DAG count) are live progress coordinates
so a watcher can render "N of M (<goal>)" without the wave plan.

| Event | Required keys | Notes |
|---|---|---|
| `subtask.start` | `subtask_id`, `index`, `total`, `goal`, `runtime`, `model`, `skill`, `depends_on` | Subtask invocation opened. |
| `subtask.end` | `subtask_id`, `index`, `total`, `goal`, `ok`, `error`, `attestation_error`, `duration` | Subtask invocation closed. `ok` is the honest "fully succeeded" signal: it is `false` not only on a hard exec `error` but also when the done-criteria attestation gate did not close (`attestation_error` set, `error` None → the subtask is `incomplete`). Consumers distinguish `done` (ok true) / `incomplete` (ok false, error None, attestation_error set) / `failed` (ok false, error set). `None`-valued keys are stripped on emit. |
| `subtask.receipt` | `subtask_id`, `state`, `done_criteria`, `criteria_report`, `attestation_summary`, `attestation_error`, `attestation_repaired`, `depends_on`, `duration` | Terminal delivery record. `state` ∈ `done` / `incomplete` / `failed` / `skipped` (ADR 0067 + 0068 + 0071). `criteria_report` is the developer's per-criterion self-attestation (`[{index, criterion, met, evidence}, …]`); `attestation_summary` is its one-line summary; `attestation_error` carries the gate reason on an `incomplete` receipt. `attestation_repaired=true` means the original machine-readable attestation was malformed but a single no-artifact-mutation repair turn produced a valid one. Attestation fields are present only for criteria-bearing subtasks. |

### Evidence and command events

| Event | Required keys | Notes |
|---|---|---|
| `plan.parsed` | `source`, `short_summary`, `planning_context`, `subtask_count`, `has_contract` | PLAN output parsed into the typed plan shape. |
| `gate.start` | `name`, `gate_kind` | Quality gate started. |
| `gate.end` | `name`, `outcome`, `duration_s` | Quality gate ended. |
| `command.start` | `argv_summary`, `cwd` | Orchestrator-driven shell command started. |
| `command.end` | `exit_code`, `duration_s`, `outcome` | Orchestrator-driven shell command ended. |
| `artifact.created` | `path`, `artifact_kind` | Durable run artifact written. |

## Reading a live MCP/SILENT run

For a live progress UI, a minimal consumer loop can render these events:

1. `phase.start`: open or update the current phase row.
2. `agent.tool_use` / `agent.mcp_tool_call`: append tool activity under
   the current phase.
3. `agent.contract_ready`: show that the model's typed answer is ready
   and is being parsed/rendered.
4. Phase-specific verdict events (`validate_plan.verdict`,
   `cross_validate_plan.verdict`, `cross_final_acceptance.verdict`):
   update decision chips.
5. `agent.summary`: update token/cost usage.
6. `phase.end`: close the phase row.
7. `run.end`: close the run.

Do not wait for `agent.text` to decide whether a typed phase responded.
For JSON-contract phases, `agent.contract_ready` plus the parsed phase
entry in `meta.json` is the stable signal.

## Compatibility policy

Event kind strings are wire shape. Do not rename an existing event kind
without a schema migration plan. Adding optional payload fields is
compatible. Adding a new required key to an existing event is a breaking
change for consumers that validate event payloads.
