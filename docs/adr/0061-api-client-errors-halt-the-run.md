# ADR 0061 — API-client errors halt the run with a reason

- Status: Accepted
- Date: 2026-05-31
- Supersedes: none
- Superseded by: none

## Context

A pipeline run drives external agent CLIs (Claude, Codex, Gemini) through the
`IAgentRuntime.invoke()` contract, which returns the assistant's raw text. The
runtime adapters only treated **authentication** failures as fatal. Every other
failure shape was swallowed:

- `ClaudeAgent.invoke` / `GeminiAgent.invoke` returned
  `_extract_assistant_text(stdout) or stdout` even when the CLI exited
  non-zero, so a transport error became the phase's "output".
- The Claude/Codex CLIs can also exit **zero** after their internal reconnect
  loop gives up, printing the transport error as the final assistant message
  (e.g. `API Error: Unable to connect to API (ConnectionRefused)`) or as a
  stream `{"type":"error", ...}` event.

Because the handler returned normally, the lifecycle FSM saw no failure: the
phase was marked `✓`, its "summary" was the error text, and the run walked on
into the next phase against empty/garbage context. Observed in the wild: an
`implement` phase reported `✓ implement` with summary
`API Error: Unable to connect to API (ConnectionRefused)`, then `review_changes`
ran anyway on a stream that was still disconnecting.

The error taxonomy and retry engine in `core/io/retry.py` already existed
(`AgentCallError` and subtypes, `classify_error` / `classify_from_exit`,
`call_with_retry`) but were not wired into the invocation path.

## Decision

An API-client error is recognised at the runtime boundary and converted into a
typed `AgentCallError`, so the run halts with a clear cause instead of
continuing. Transient transport errors are retried first.

1. **Taxonomy (core owns the protocol).** Add `ApiConnectionError(AgentCallError)`
   plus `_CONNECTION_PATTERNS` to `core/io/retry.py`; `classify_error` maps
   connection-refused / DNS / stream-disconnect / 5xx signatures to it.
   `RetryConfig` gains `connection_max_retries` (default 2). `call_with_retry`'s
   loop now bounds attempts by the *caught error's* own budget rather than the
   generic budget, so a policy can retry connection errors while surfacing
   generic failures at once.

2. **Translation + retries (plugins own provider behavior).**
   `agents/runtimes/_failures.py` is the single place that turns a raw CLI
   result into a typed error:
   - non-zero exit → `classify_from_exit` over `(stderr, stdout)` (auth handled
     separately with its existing formatted guidance);
   - exit-zero where the model's *own reply* is a transport-error message →
     `ApiConnectionError`. The exit-0 check scans **only the extracted reply
     text** (the string `invoke()` is about to return), never stdout plumbing
     or stderr logs. A success-exit CLI routinely prints unrelated operational
     noise to stderr — e.g. Codex `failed to record rollout: stream
     disconnected` on a clean exit — and scanning that would discard a valid
     model answer and loop forever on retry. The exit code is authoritative:
     the model answered unless the answer itself is the error. Two error
     shapes count: a structured `{"type":"error","message":...}` stream event
     (machine-emitted, so a sentinel anywhere in its message halts), or a
     plain-text reply whose first content-bearing line (after skipping markdown
     fences/quotes/bullets and non-error JSON plumbing) *begins* with a
     sentinel. A reply that merely *mentions* the phrase in prose — e.g. a
     legitimate debugging answer about "API Error: Unable to connect…" — is not
     a false positive.
   `run_invoke_with_retry` runs each invocation under `RUNTIME_RETRY_CONFIG`
   (generic `max_retries=0`; only connection/rate-limit/timeout retry). All
   three runtimes route their `invoke()` tail through these helpers, passing
   the extracted reply as `reply_text`; guardrail blocks remain an intentional
   non-failure that returns the sentinel string.

3. **Controlled halt (core owns lifecycle).** A raised `AgentCallError`
   propagates through the lifecycle FSM (`StepStatus.FAILED`) and
   `_dispatch_via_fsm` re-raises it; `_record_phase_failure` records
   `status="failed"`, `halt_reason="phase_failure:ApiConnectionError"`, and the
   structured `failure` block with the cause, and renders the `FAILED in <phase>`
   line. The run does not advance to the next phase.

4. **Clean CLI exit (boundary catches the terminal type).** The re-raised
   `AgentCallError` is an *expected* terminal outcome, not a crash, so the SDK
   boundary catches it. `sdk/runner.py` (`run_pipeline_from_args` and
   `run_cross_from_args`) catches the base `AgentCallError` — previously only
   the `AgentAuthenticationError` subclass was caught, so a bare
   `AgentCallError` (API unreachable, generic non-zero exit) escaped to the top
   of `cli/orcho.py:main()` and printed a Python traceback. The boundary now
   prints the cause to stderr and returns exit code 1.

## Consequences

- A non-zero exit or recognised transport failure now **raises** from
  `invoke()`. Runtime adapter behavior changed for Claude and Gemini (Codex
  already raised on non-zero exit, now typed). Tests that documented the old
  swallow were updated.
- No wire-format change: `halt_reason` keeps its `phase_failure:<ExcType>`
  shape; `ApiConnectionError` is a new *value*, and the cause already travels in
  the existing `failure` block. `orcho-mcp` consumers that key off
  `meta.halt_reason` keep working; the e2e mock smoke is unchanged.
- Generic CLI failures (`max_retries=0`) halt immediately; only transient
  transport shapes add bounded backoff (~3s worst case) before halting.
- Exit-zero detection is deliberately narrow (a short sentinel list): a
  sentinel anywhere in a structured `{"type":"error"}` event's message, or at
  the start of a plain-text reply's first content line. This avoids
  misclassifying legitimate assistant prose that merely mentions networking
  terms.
- Each retry attempt is a distinct subprocess, so `agent.start` / `agent.end`
  now fire **per attempt** with a fresh `agent_call_id` (the emit pair moved
  inside the retry thunk). A retried invocation emits N paired start→end events
  instead of one start with N ends, keeping event-pairing consumers correct.
  `_last_call_id` reflects the most recent attempt.
