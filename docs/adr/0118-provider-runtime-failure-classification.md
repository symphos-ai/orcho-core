# ADR 0118 — Recoverable provider/runtime failure classification

Status: Accepted

## Context

A phase can die for very different reasons, and the durable failure record has
to tell them apart so a captain / MCP client picks a *safe next action* rather
than guessing:

* a code/test/review **verdict** (`final_acceptance` / `validate_plan`
  rejection) — the diff or plan was judged unacceptable;
* an **operator halt** — a human stopped the run;
* a **stalled command** (ADR 0103) — a child command the agent launched hung;
* a **provider-access** failure (ADR 0101) — the configured runtime cannot
  reach its provider surface at all, so blind retry will not help until the
  operator restores access or switches the phase to a different *configured*
  runtime/model;
* a **transient provider/runtime** condition — the provider returned a
  rate-limit, the transport dropped, the request timed out, or the local
  machine ran out of an OS resource. The run escalated past its retry budget
  and is terminal, but the condition is expected to clear, and the safe next
  action is to **resume or retry the same phase** — not to switch runtime and
  not to treat the diff/review as rejected.

Before this work that last shape had no stable discriminator. A
`RateLimitError` / `ApiConnectionError` / `ApiTimeoutError` /
`SystemResourceError` that escalated past retries landed in the generic
`AgentCallError` branch of `run._failure_metadata_for_exception` and persisted
only a bare `stderr_excerpt`. Downstream consumers (evidence slice, SDK
projection, terminal summaries) could not distinguish a recoverable provider
hiccup from a genuine code failure without scraping that excerpt — exactly the
log-scraping this codebase forbids.

The taxonomy already exists. `core/io/retry.py` raises typed subclasses of
`AgentCallError` for each transient shape. The missing piece is a typed
*classification* of those exceptions into a durable, provider-neutral
`failure_kind`, built by `isinstance` over the existing types — never by
re-parsing provider strings.

## Decision

### New `failure_kind = "provider_runtime"`

`pipeline/run_state/provider_runtime.py` (sibling to `stalled_command.py` and
`pipeline/project/provider_recovery.py`) owns the classification. It declares:

* `PROVIDER_RUNTIME_FAILURE_KIND = "provider_runtime"`;
* `RECOMMENDED_ACTION = "resume_or_retry_phase"`;
* `is_provider_runtime_failure(exc) -> bool` — the routing predicate;
* `build_provider_runtime_failure(exc, *, failed_phase, runtime, model)` — the
  durable record builder.

The record is:

```
{
  "failure_kind": "provider_runtime",
  "recoverable": True,
  "recommended_action": "resume_or_retry_phase",
  "failed_phase": <phase>,
  "runtime": <configured runtime>,
  "model": <configured model>,
  "provider_message": <sanitized excerpt>,   # omitted when empty
}
```

### Typed mapping — no string re-parsing

Membership in `provider_runtime` is decided **only** by `isinstance` over the
explicit set sourced from `core/io/retry.py`:

```
{ RateLimitError, ApiConnectionError, ApiTimeoutError, SystemResourceError }
```

Provider-branded signatures (`429`, `rate_limit_exceeded`, `timed out`, …) stay
where they already live — the classifier in `core/io/retry.py`. The new module
is provider-neutral: it sees only the typed exception, so there is no parallel
regex/string classifier and no provider name in core.

Deliberately **excluded**:

* `AgentAccessError` → stays `provider_access` (ADR 0101). It is a subclass of
  `AgentCallError`, so the access branch is checked *before* the
  `provider_runtime` branch; the two `failure_kind` values never overlap.
* `AgentAuthenticationError` and `ContextOverflowError` → an auth/prompt form,
  not a usage/session/transport condition. They keep the generic
  `stderr_excerpt` branch.
* a bare `AgentCallError` → generic fallback (`stderr_excerpt` only).

### Branch precedence in `_failure_metadata_for_exception`

The routing order is load-bearing:

1. `AgentCommandStalledError` → `stalled_command` (ADR 0103);
2. `AgentAccessError` → `provider_access` (ADR 0101);
3. `is_provider_runtime_failure(exc)` → `provider_runtime` (this ADR);
4. generic `AgentCallError` → `{"stderr_excerpt": …}`;
5. anything else → `{}` (byte-identical historical behaviour).

The failed phase's configured `runtime`/`model` resolution is shared by the
access and provider-runtime branches via a single defensive local helper, so it
is not duplicated.

### Sanitary boundary (ADR 0101)

`provider_message` is taken **only** from
`core.io.retry.sanitized_failure_excerpt(exc)`, the same JSONL-stripping channel
the access path uses. No raw `str(exc)`, raw stderr, payload, or prompt text
reaches an operator-visible or durable field. When the excerpt is empty the key
is omitted, matching the generic branch.

### Advisory only

`recoverable` and `recommended_action` are declarative metadata for a
captain / MCP client. This ADR adds **no** retry loop, no MCP tool, and does not
change run control flow: the terminal status stays `failed` and the existing
`run.end` semantics are unchanged. The new keys ride through the existing
`**failure_meta` spread into `session['failure']` and the `run.end` event.

## Consequences

* A transient provider/runtime escalation now carries a stable, typed
  classification that the evidence slice and SDK projection (later subtasks) can
  read without scraping logs.
* Code/test/review verdict failures, operator halts, `provider_access`,
  `stalled_command`, and setup failures are unaffected; their durable records
  are byte-identical.
* The classification is provider-neutral and append-only; future transient
  types added to `core/io/retry.py` join `provider_runtime` by being added to
  the typed set, never by string matching.
