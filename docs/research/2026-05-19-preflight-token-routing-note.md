# Research Note: Preflight Token Counts as a Routing Signal

**Date:** 2026-05-19
**Status:** idea captured; no Orcho behavior change
**Related:** ADR 0028 (cache-first physical wire layout), ADR 0029
(context lifecycle), ADR 0030 (runtime context autonomy),
`2026-05-18-runtime-context-telemetry-probe.md`

## Thought

Preflight token counting can become more useful than a display metric if it
feeds model/runtime routing before the expensive generation call.

Today Orcho can estimate prompt size locally while composing the prompt. That
is enough for debug visibility, but it is not enough to make precise routing
decisions near model context limits. A future runtime layer could ask the
provider for the input token count after the final request shape is assembled
and before generation starts.

Example policy shape:

```text
small prompt     -> normal model/runtime
large prompt     -> long-context model/runtime
near hard limit  -> compact, split, or fail early with a useful diagnostic
```

The practical value is avoiding blind dispatch:

* A 35k-token request can stay on the cheaper/default path.
* A 180k-token request can route to a long-context path before the runtime
  rejects it.
* A request near the model limit can trigger compaction or artifact
  re-fetching before spending a generation attempt.

## Pre-dispatch Context Fit Guard

The stronger policy shape is a guard between prompt composition and runtime
dispatch:

```text
compose Orcho prompt parts
        -> estimate per PromptPart locally
        -> assemble exact runtime request
        -> preflight count total input tokens
        -> decide: send / compact / clear / rebuild / rotate session
```

This is materially different from trying to control the runtime's hidden
session memory. Orcho is not guessing whether a long-running runtime session
feels full. It is checking whether the next request that Orcho itself is about
to send can fit the selected model/runtime contract.

Possible actions when the request does not fit:

* Clear or omit `re_fetchable` prompt parts first, then re-run preflight.
* Replace large artifacts with artifact references and ask the runtime to
  re-fetch only what the phase needs.
* Summarize or compact an artifact bundle before review/final acceptance.
* Rebuild the full phase context into a fresh runtime session, if the selected
  runtime's session state is the source of pressure.
* Route to a long-context model/runtime when the request is large but valid.
* Fail early with a specific diagnostic when no configured policy can make the
  request fit.

This makes token counting an input to request shaping, not merely a metric
written after the fact.

## Constraints

Preflight counting should not become a correctness dependency in the ADR 0030
sense. Runtimes still own their internal context lifecycle, and Orcho still
owns phase contracts and recovery. Preflight routing is an optimization and
operator-experience improvement, not proof that the later generation will
preserve contract quality.

Provider count APIs usually return a total request count, not a breakdown by
Orcho `PromptPart`. That means per-part transcript rows can remain local
estimates, while the full request may carry a separate provider preflight
count.

The per-part local estimates still matter because they give the policy a
priority queue for what to remove or rewrite. Provider preflight answers
"does the full request fit?"; local `PromptPart` estimates answer "which
pieces are worth clearing first?"

## Possible Future Shape

```text
token_counting = heuristic
token_counting = preflight
token_counting = auto
```

`auto` is the most attractive default candidate: use the cheap local estimate
for ordinary prompts, and call provider preflight only when the local estimate
crosses a threshold such as 70-80% of the configured or runtime-reported
window.

Evidence fields worth preserving if implemented:

```text
input_tokens_estimate
input_tokens_preflight
token_count_source
token_count_exact
routing_decision
routing_reason
fit_guard_action
fit_guard_before_tokens
fit_guard_after_tokens
cleared_prompt_part_ids
```

## Open Questions

* Which runtime adapters expose a cheap, reliable preflight endpoint?
* Should preflight count the exact structured request or only the rendered
  prompt text?
* How many additional provider requests per run are acceptable?
* Should routing thresholds live in profiles, runtime config, or a future
  advisor layer?
* Can prefix-only and payload-only preflight counts provide enough signal to
  evaluate cache-first layout without excessive request overhead?
* Which prompt-part classes are safe to remove automatically, and which
  require a contract-aware rewrite or fresh-session rebuild?
