# ADR 0136 — Explicit waiver for incomplete implementation

## Status

Accepted.

This supersedes the bare-`continue` branch of the ADR 0073 implement handoff.

## Context

An exhausted `subtask_dag` implement handoff previously published both
`continue` and `continue_with_waiver`. Both actions accepted incomplete
implementation and persisted `delivery_status="waived"`, but bare `continue`
synthesized the waiver rationale without an explicit operator verdict. The
generic action label described continuation rather than waiver, so an operator
could unintentionally accept incomplete delivery.

## Decision

Incomplete implement handoffs publish exactly:

- `retry_feedback`;
- `continue_with_waiver`;
- `halt`.

`continue_with_waiver` continues to require a non-empty operator verdict. Bare
`continue` remains a valid generic handoff action for other phase contracts,
but the implement resume arm rejects it fail-closed if a stale or hand-edited
decision artifact attempts to use it.

## Consequences

Incomplete implementation cannot advance through an implicit waiver. CLI, SDK,
MCP, and other clients receive the narrowed action list from the existing
runtime-produced `available_actions` field, so no wire schema changes are
required. Existing generic continue behavior for plan, review, scope, and
cross-project handoffs is unchanged.
