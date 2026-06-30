# ADR 0027 - Execution surfaces and read-only fanout

**Status:** Proposed
**Date:** 2026-05-16
**Companion to:** ADR 0009 (composable prompt parts), ADR 0026 (session-aware prompt parts)

## Context

ADR 0009 made prompt authoring composable: role, task, format, and
code-owned contracts have separate ownership. ADR 0026 added typed prompt
parts, render envelopes, stable-prefix partitioning, and prompt-session
state. After that foundation, the next product problem is execution
topology.

Native coding agents expose ways to delegate work, but users still need to
know when to delegate, how to constrain delegated work, how to prevent writer
conflicts, and how to merge the results. Orcho should encode those choices in
profile/runtime policy instead of relying on the operator to remember an
agent-orchestration playbook.

There is also a prompt-quality problem. A single broad review prompt tends to
accumulate correctness, tests, security, maintainability, API-contract,
documentation, and project-specific rules. That makes prompts long and makes
each review less focused. Projects need a configurable way to select the
review or exploration lenses that matter for their domain.

## Decision

Introduce **execution surfaces**: named, bounded lenses attached to a
`PhaseStep` execution policy.

An execution surface is not a new phase. It is a sub-execution inside one
phase. Each surface has a small prompt selection and explicit execution
constraints. The phase handler runs the surfaces, parses their outputs, and
joins them into one canonical phase result.

Initial scope:

- read-only fanout only;
- first target phase: `review_changes`;
- every surface emits the existing `review_json` contract;
- Orcho merges surface findings into one canonical review result;
- writer fanout, DAG workers, and worktree integration stay out of v1.

Subagent mechanics are an implementation detail. The profile-level primitive
is the execution surface.

## Profile Shape

Do not hide topology under `overrides`. Execution topology is first-class
phase execution policy.

Extend `PhaseStep.execution` from a string-only field into a
backward-compatible union:

```json
"execution": "linear"
```

is equivalent to:

```json
"execution": {
  "mode": "linear"
}
```

Read-only review fanout can then be authored as:

```json
{
  "phase": "review_changes",
  "execution": {
    "mode": "fanout_review",
    "read_only": true,
    "join": "merge_findings_by_severity",
    "surfaces": [
      {
        "id": "correctness",
        "prompt": {
          "role": "code_reviewer",
          "task": "review_correctness",
          "format": "terse"
        }
      },
      {
        "id": "test_gaps",
        "prompt": {
          "role": "code_reviewer",
          "task": "review_test_gaps",
          "format": "terse"
        }
      },
      {
        "id": "security",
        "prompt": {
          "role": "code_reviewer",
          "task": "review_security",
          "format": "terse"
        }
      }
    ]
  }
}
```

The exact dataclass names are left to implementation, but the intended shape
is:

```python
@dataclass(frozen=True)
class ExecutionPolicy:
    mode: str = "linear"
    read_only: bool | None = None
    join: str | None = None
    surfaces: tuple[ExecutionSurface, ...] = ()
    session_split: str | None = None


@dataclass(frozen=True)
class ExecutionSurface:
    id: str
    prompt: PromptSpec
    model: str | None = None
    effort: str | None = None
```

`session_split` is included here because M11 needs profile-visible
prompt-session policy. It should live on execution policy, not in an
unrelated profile side channel.

## Runtime Semantics

For `fanout_review`:

1. Build the same review target context for every surface.
2. Render each surface prompt from its `PromptSpec`.
3. Attach the existing protected `review_json` contract.
4. Invoke each surface with read-only constraints.
5. Parse every response through the existing review parser.
6. Merge findings into one canonical review result.
7. Store per-surface evidence for observability.

A clean result means every surface approved with no findings.

Join semantics for v1:

- preserve every substantive finding;
- de-duplicate exact duplicates by stable finding body and file/line when
  available;
- sort by severity, then surface id, then finding id;
- if any surface returns a rejected verdict, the joined result is rejected;
- joined `risks` and `checks` are concatenated with surface attribution.

The phase log still exposes one canonical `review_changes` result so existing
loop predicates such as `review_changes.clean` remain meaningful.

## Relationship To DAG And Worktrees

Execution surfaces are read-only fanout for review and exploration. They do
not replace DAG implementation or worktree-isolated writers.

The split is:

| Concern | Owner |
| --- | --- |
| read-only review or exploration lenses | execution surfaces |
| independent implementation tasks | plan DAG |
| safe parallel writers | DAG executor plus worktree isolation |
| merge/conflict handling | integration gate |

Future writer fanout must go through DAG/worktree policy, not through
`fanout_review`.

## Prompt Engine Interaction

Each surface is rendered through the prompt engine introduced by ADR 0009 and
ADR 0026:

```text
surface PromptSpec
  -> role/task/format parts
  -> protected review_json contract
  -> shared dynamic review target
  -> PromptRenderEnvelope
```

This keeps surfaces small and cacheable. The review target and diff/artifact
context remain dynamic payload. The surface task is the narrow review lens.

## Non-Goals

- No writer fanout in v1.
- No DAG/worktree implementation changes in v1.
- No YAML migration.
- No replacement of `PhaseStep` or profile v2.
- No new review parser contract.
- No recursive delegation.
- No use of `overrides` as the topology surface.
- No broad prompt rewrite. M10 owns prompt prose.

## Consequences

Positive:

- Review prompts become smaller and more focused.
- Projects can choose review surfaces as a QA matrix.
- Different surfaces can later use different models or effort levels.
- Orcho controls read-only constraints and join semantics.
- Prompt-render traces can attribute cost, findings, and prompt parts per
  surface.

Costs:

- Profile schema grows.
- Phase execution must support fanout and join.
- Observability must represent per-surface prompt/render results.
- More profile authoring power requires stronger validation and clearer error
  messages.

## Migration Plan

1. M11: introduce `ExecutionPolicy` object while preserving
   `execution: "linear"` and add `session_split` there.
2. M12: make observability execution/surface-aware.
3. M13: implement read-only `fanout_review` for `review_changes`.
4. Later: add exploration fanout before planning.
5. Later: evaluate writer fanout only through DAG/worktree policy.

M11 and M12 briefs should be updated before implementation so they prepare the
profile and trace surfaces for this ADR without implementing fanout early.
