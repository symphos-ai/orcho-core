# ADR 0132 — Scheduled gate selection, execution, and durable disposition

Status: Accepted

## Context

A scheduled verification gate currently carries several facts that are treated
as if they were one fact:

- the command is declared in a project verification contract;
- a gate set can be selected for a run;
- the selection rule matched the current run context;
- the engine will execute the command;
- a failed command can block, warn, repair, or request a handoff;
- the run has durable evidence explaining what happened to the scheduled gate.

They are not equivalent.

The distinction became visible in run `20260714_185541`. Its project contract
declared and scheduled `phpstan`, `psalm`, `test-integration`, and
`test-functional` at `before_delivery` with `policy="warn"`. The start banner
rendered every command as `pre-final / auto / warn / always`, but the resolved
plan selected only the `smoke` gate set. The four commands did not execute and
received no final disposition.

Three implementation splits caused the mismatch:

1. `verification_selection` selects commands only through `selection` rules;
   referencing a gate set from `schedule` does not select it.
2. `verification_ledger` derives `run=auto` from the hook alone and falls back
   to `activation=always` when a scheduled gate set is not reachable from any
   selection rule.
3. The hook router executes only `require`, while the pre-final receipt
   materializer targets every selected delivery command without filtering its
   policy. A selected `before_delivery` command therefore auto-runs even when
   its effective policy is `off`.

The last behavior defeats the mode transform intended to make `fast` cheaper:
`suggest` becomes `off`, but Stage 9 still executes it. Conversely, a selected
phase-scoped `warn` gate has no executor because it never reaches Stage 9 and
the hook router filters it out.

The durable SDK and MCP projections expose a second mismatch. Manual execution
is represented as `status="SKIPPED"`, `policy="manual_only"`, and membership in
a separate `manual_only` aggregate. MCP then derives `trigger` and even a gate
class locally from those indirect signals. The SDK documents that scheduled
gate events are not persisted, so it cannot reconstruct the actual per-hook
trail from durable artifacts.

ADR 0081 correctly described its require-only hook router. ADR 0094 later added
the pre-final materializer. ADRs 0095–0097 added timeline and policy
presentation without unifying the two execution paths. ADR 0117 made cost
orthogonal to blocking policy, and ADR 0130 made post-result consequence depend
on typed failure class. This ADR joins those decisions into one scheduled-gate
model.

## Decision

### 1. Six independent axes

Every scheduled gate identity is modeled through six independent axes:

1. **Declared** — the command and its gate set exist in the normalized
   verification contract.
2. **Selectable** — the normalized contract contains an activation rule for the
   identity: `always`, `paths`, `task_kind`, or `operator`.
3. **Selected** — that rule matched the current run context.
4. **Execution policy** — the effective policy and hook determine whether the
   engine or an operator may execute the command.
5. **Consequence** — a result may warn, repair, request a handoff, abort, or have
   no transition effect.
6. **Disposition** — durable evidence records the observed result of the exact
   scheduled identity.

The load-bearing invariant is:

```text
declared != selectable != selected != executed != blocking
```

The scheduled identity is `(command, hook, phase)`. Two identities that use the
same command do not collapse merely because a command-level receipt can be
reused.

### 2. Canonical execution-policy vocabulary

The only execution-policy values are:

```text
manual | suggest | warn | require
```

`off` is removed. It is not accepted as an alias, is not normalized through a
compatibility path, and is not emitted by any public or internal surface.
Project contracts, built-in fixtures, tests, documentation, SDK records, and
MCP schemas migrate atomically. A contract that still declares `off` fails
validation.

The existing derived value `manual_only` is also removed from the policy axis.
Manual availability is represented by `policy="manual"`; a manual scheduling
hook remains a trigger fact, not a second policy vocabulary.

The policies mean:

| Policy | Engine auto-execution | Operator surface | Failure consequence |
|---|---|---|---|
| `manual` | never | command is available explicitly | none |
| `suggest` | never | command is recommended and available explicitly | none |
| `warn` | yes, when selected on an executable hook | result is shown | warning; never blocks |
| `require` | yes, when selected on an executable hook | result is shown | effective required action |

Cost metadata does not participate in this table.

### 3. Work-mode projection

Work mode transforms the declared tier as follows:

| Declared tier | `fast` | `pro` | `governed` |
|---|---|---|---|
| `manual` | `manual` | `manual` | `manual` |
| `suggest` | `manual` | `suggest` | `suggest` |
| `warn` | `warn` | `warn` | `require` |
| `require` | `require` | `require` | `require` |

`fast` therefore removes automatic or suggested work by projecting `suggest`
to `manual`; it does not disable the command.

An absent verification contract is represented as absence, not as a synthetic
verification policy.

### 4. Selection is explicit

`schedule` assigns timing and policy to an identity; it never activates a gate
set.

Every gate set referenced by an automatic schedule must be reachable through at
least one declared selection rule. An unreachable scheduled gate set is a
contract validation error, not an implicit `always` gate and not a run-time
`unbound` disposition.

A command scheduled directly without a gate set is normalized to an explicit
internal `always` activation binding. The ledger must not use a presentation
fallback to invent activation.

A selected command with no applicable schedule entry is normalized to an
explicit `(manual_only, policy="manual")` identity. Membership in
`verification.required` does not turn that operator-owned identity into an
automatic requirement; it only supplies the default tier where an executable
schedule omits policy.

An operator selection rule controls whether the gate set enters the run plan.
It does not by itself decide who executes a selected command; execution still
comes from policy plus hook.

### 5. One execution-eligibility resolver

Core owns one pure resolver over:

```text
selected + execution_policy + hook + phase -> executor + trigger + base consequence
```

The hook router, pre-final materializer, readiness, prompts, ledger, and CLI
consume this result. They do not maintain independent policy filters.

Hook behavior is:

| Hook | `manual` / `suggest` | `warn` | `require` |
|---|---|---|---|
| `before_phase` | no automatic execution | execute at hook; continue on failure | execute at hook; apply required action |
| `after_phase` | no automatic execution | execute at hook; continue on failure | execute at hook; apply required action |
| `before_delivery` | no automatic execution | materialize before final; continue on failure | materialize before final; enforce at delivery boundary |
| `manual_only` | explicit operator execution only | invalid combination | invalid combination |
| `on_resume` | explicit resume/operator execution | execute through the resume hook; continue on failure | execute through the resume hook; apply required action |

The `manual_only` hook accepts only `manual` or `suggest`. A selected
`before_delivery` identity is executed at most once for an unchanged subject:
the delivery hook reuses a fresh receipt produced by the pre-final
materializer.

`action` is meaningful only for `require`. A `warn` gate has the fixed
consequence `continue_warn`; `manual` and `suggest` have no automatic action.
Invalid policy/action and policy/hook combinations fail contract validation
instead of being ignored.

Typed result classification may change consequence without rewriting execution
policy. For example, ADR 0130 can make a required environment-provenance failure
waivable or warning-level while the gate remains visibly declared as
`execution_policy="require"`.

### 6. Durable disposition closes every identity

Core persists a versioned scheduled-gate ledger in the run directory. It is the
durable source for SDK, terminal completion, resume, evidence, and MCP
projection. It records the normalized identity and axes rather than
re-resolving a historical run from the current project plugin.

Every declared scheduled identity reaches exactly one terminal disposition:

```text
not_selected
manual_available
suggested
skipped_fresh
executed_pass
executed_fail
residual_missing
residual_stale
residual_failed
```

`not_selected` carries the selection reason (`paths`, `task_kind`, or
`operator`). `manual_available` and `suggested` are intentional non-execution,
not failures. `skipped_fresh` means a concrete executor inspected a fresh
receipt and did not rerun the command. Residual dispositions retain receipt
classification and policy consequence.

The ledger must not infer `executed_pass` from the mere existence of a receipt.
Execution requires a durable execution event. Selection decisions and scheduled
execution events are persisted with the full identity.

Historical plugin changes do not rewrite the ledger of a completed run.

### 7. Operator-facing vocabulary

The start, live, resume, and completion surfaces use these columns or their
typed equivalents:

| Fact | Examples |
|---|---|
| trigger | `after implement`, `pre-final`, `operator`, `on resume` |
| selection | `always`, `on path`, `task kind`, `operator` |
| execution | `auto`, `manual`, `suggest` |
| consequence | `none`, `warning`, `repair`, `handoff`, `abort` |
| disposition | one durable value from the list above |

`auto` is rendered only when the execution-eligibility resolver names an engine
executor. A hook being part of the automatic lifecycle is insufficient.
`pre-final` names a real trigger, not a synonym for advisory presentation.

### 8. SDK and MCP wire

The SDK exposes the six axes and durable disposition directly. It no longer
encodes manual availability as `SKIPPED + manual_only`, and it no longer reports
that the scheduled trail is unavailable after the durable ledger lands.

The existing `manual_only` policy and aggregate fields, ambiguous six-value
gate status encoding, and locally derived trigger/classification are replaced
by the canonical core projection. There are no duplicate compatibility fields.

MCP consumes the SDK projection without re-deriving selection, execution,
consequence, trigger, gate class, or disposition. Its Pydantic models, tool
descriptions, schema snapshot, unit tests, stdio registration tests, and mock
pipeline smoke migrate in the same delivery bundle as the core SDK change.

This is an intentional pre-release wire break. It is not additive because
retaining both shapes would preserve the ambiguity this ADR removes.

## Consequences

- A project plugin cannot silently schedule an unreachable gate set.
- `fast` no longer auto-runs a command projected to manual execution.
- Selected phase-scoped `warn` gates gain a real non-blocking executor.
- A banner cannot promise automatic execution solely from the hook.
- Every gate shown at run start is closed by one durable disposition.
- Historical inspection is stable when the project plugin changes later.
- SDK and MCP clients receive the same core-owned semantics.
- Project configurations using `off`, non-required actions, or unreachable
  scheduled gate sets must be updated before they can run.

## Rejected alternatives

### Make `schedule` select every referenced gate set

Rejected. It would bypass path, task-kind, and operator selection and could run
expensive commands unexpectedly.

### Fix only the start banner

Rejected. It would leave the two executor paths inconsistent, keep automatic
execution of manual policy, and leave durable/MCP projections incomplete.

### Keep `off` as an alias for `manual`

Rejected. Aliases and dual policy paths retain ambiguous vocabulary and expand
the state space every consumer must handle.

### Add a second `execution=auto|manual` plugin field

Rejected for this stage. Policy, hook, and selection are sufficient for the
accepted matrix. A future external execution owner would require a separate
decision rather than overloading cost or action.

### Let MCP continue deriving trigger and gate class

Rejected. The execution and selection facts belong to core and must have the
same durable provenance on every surface.

## Implementation order

1. Contract vocabulary, validation, normalized selection bindings, and the
   shared execution-eligibility resolver.
2. Hook router and pre-final materializer adoption of the shared resolver.
3. Durable ledger, SDK projection, terminal/resume/evidence surfaces, and public
   core documentation.
4. Matching MCP models, projection, schema snapshot, and protocol smoke.

Steps 3 and 4 are one coordinated wire bundle even though each repository keeps
its own commit history.
