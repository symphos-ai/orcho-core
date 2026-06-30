# ADR 0023 — Collapse three role Protocols into a single `IAgentRuntime`

**Status:** Accepted
**Date:** 2026-05-13
**Supersedes:** the role-keyed reading of A5.2c
**Companion to:** ADR 0009 (composable prompt parts), ADR 0022 (workflow-semantic phase taxonomy)

## Context

Before this change the agent layer exposed three role-keyed Protocols:

```python
class IArchitectAgent(Protocol):
    def plan(self, task, cwd, codemap="") -> ParsedPlan: ...

class IDeveloperAgent(Protocol):
    def run(self, prompt, cwd, *, continue_session=False) -> str: ...
    def reset_session(self) -> None: ...

class IReviewerAgent(Protocol):
    def review_uncommitted(self, cwd, focus="") -> str: ...
    def review_file(self, file_path, focus="", cwd=None) -> str: ...

class IHypothesizingArchitect(IArchitectAgent, Protocol):
    def hypothesize(self, task, cwd, codemap="") -> str: ...
```

Two follow-on changes had already moved the load-bearing logic out of
these Protocols:

1. **ADR 0009** moved prompt composition out of the runtime. `_prompts/`
   role/task/format parts are assembled by `pipeline.prompts.builders` and
   the runtime receives a finished string.
2. **ADR 0022** renamed phases to workflow-semantic IDs; "plan / build /
   review / fix" is policy that lives in the orchestrator, not on the
   runtime type.

What remained was the artificial role split inside `agents/`. A single
concrete class (`ClaudeAgent`) implemented all three Protocols anyway — the
split only persisted because:

- Read-only methods (`plan` / `review_*` / `hypothesize`) deliberately ran
  without `--dangerously-skip-permissions` and without `--resume`, so the
  permission divide was encoded in the method name.
- Read-only methods (specifically Claude's `plan` / `review_*` /
  `hypothesize`) skipped `--output-format stream-json`, so they never
  captured a `session_id`. Read-only phases were therefore always stateless;
  bridges only worked across `run()` (BUILD → FIX).

`orcho-core/CLAUDE.md` explicitly deferred the Protocol unification "until
ADR 0009 implementation lands". With composer + `_prompts/` shipped, the
gate is lifted.

## Decision

Replace the three Protocols with a single :class:`agents.protocols.IAgentRuntime`:

```python
class IAgentRuntime(Protocol):
    model: str
    session_id: str | None    # bridge handle; None for stateless runtimes

    def invoke(
        self,
        prompt: str,
        cwd: str,
        *,
        mutates_artifacts: bool = False,
        continue_session: bool = False,
        attachments: tuple[Attachment, ...] = (),
    ) -> str: ...

    def reset_session(self) -> None: ...
```

Key design choices:

- **One Protocol, one method.** Each concrete runtime (Claude, Codex,
  Gemini) implements `invoke()`. Phase handlers no longer pick "which
  method"; they only pick `mutates_artifacts` and `continue_session`.
- **`mutates_artifacts: bool` is per-call, not per-instance.** It maps to
  the CLI's write permission (Claude: `--dangerously-skip-permissions`
  + `--permission-mode acceptEdits`; Codex: `codex exec` subcommand with
  `--dangerously-bypass-approvals-and-sandbox`). Default `False` is safe;
  only build / fix paths flip it on. The same runtime instance can serve
  both read and write calls — the bridge is preserved across them.
- **Returns raw `str`.** Plan parsing (`pipeline.plan_parser.parse_plan`),
  review verdict extraction (`pipeline.review_parser`), and any other
  downstream interpretation happen in the caller. The Protocol stays
  transport-only.
- **Session capture is universal where the runtime supports it.** Claude
  now uses `--output-format stream-json --verbose` on every call so a
  `session_id` is captured regardless of `mutates_artifacts`. This makes
  the runtime **bridge-capable**: any subsequent invocation can resume the
  conversation through `continue_session=True`.
- **`session_id: str | None` accepts `None` permanently.** Codex CLI has
  no stable resumable-session id; its `CodexAgent` leaves the field at
  `None` and silently ignores `continue_session`. The Protocol explicitly
  permits this — bridges live where the backend supports them.
- **Attachment ownership is split.** TEXT attachments are rendered into
  the prompt **outside** the runtime by
  `pipeline.attachment_inject.render_text_block`; the runtime never sees
  TEXT. Multimodal kinds (IMAGE / BINARY) ride as the `attachments`
  kwarg on `invoke`. Each runtime adapter is responsible for translating
  multimodal payloads to its CLI's flags. Runtimes **raise `ValueError`**
  when TEXT leaks into the kwarg — silent double injection would burn
  context.

### What this ADR explicitly does **not** do

- **Does not invert the orchestrator's chain policy.** Today
  `continue_session=True` is passed only on the BUILD → FIX edge; every
  other phase starts fresh. Phase 7 leaves this policy intact —
  bridge-as-default (resume whenever `session_id != None`) is a separate
  body of work because it changes pipeline behaviour.
- **Does not raise on cross-mutation resume.** The runtime is mechanical:
  if `continue_session=True` and `session_id is not None`, it resumes,
  regardless of whether the previous call mutated artifacts. Any
  fresh-eyes policy ("the reviewer must not inherit the build session")
  lives at the orchestrator, not the Protocol.
- **Does not unify DAG fan-out bridges.** Each parallel DAG subtask
  receives its own runtime instance via `resolve_subtask_agent`
  (`pipeline/agent_resolver.py:67`). Bridges therefore stay isolated
  per branch — concurrent `--resume` on a shared `session_id` would
  race.

## Alternatives considered

- **`RuntimeMode` enum (`READ_ONLY` / `READ_WRITE`).** A two-value enum
  is an over-named boolean — it adds no semantic clarity over
  `mutates_artifacts: bool` and grew a vocabulary axis ("what mode?")
  that does not match the CLI surface (which really is one flag).
  Rejected.
- **`allow_writes: bool`.** Semantically overloaded: returning a critique
  is also a "write" textually; the actual divide is whether the call
  changes files on disk. Renamed to `mutates_artifacts` to keep the
  intent unambiguous.
- **Raise in the runtime on cross-mutation resume.** Rejected: it
  forbids a natural bridge pattern where the same runtime alternates
  read / write calls (plan → build on the same Claude instance, with a
  third-party review in between on a different runtime). Cross-mode
  hygiene is policy, not a Protocol invariant.
- **`session_mode` field on the runtime.** Rejected as overengineering.
  Mutation is a per-call attribute; promoting it to runtime state added
  no observability the orchestrator could not derive from per-call
  evidence.
- **Permission encoded at construction time (separate read / write
  instances).** Would destroy the bridge model — two instances mean two
  sessions, so plan and build on "the same" runtime can no longer share
  context.
- **Runtime owns all attachment rendering (including TEXT).** Would
  duplicate `render_text_block` logic in every adapter and break the
  composer's contract that prompt text is finalised before the runtime
  sees it.

## Consequences

- **Plugin authors implement one Protocol.** A third-party runtime
  registered under `orcho.agent_runtimes` only has to provide
  `invoke()` + `reset_session()` + the two fields. The
  `IArchitectAgent` / `IDeveloperAgent` / `IReviewerAgent` import paths
  are gone.
- **Phase handlers are uniform.** `phases.run_plan` /
  `phases.run_build` / `phases.run_review` / `phases.run_fix` all call
  `agent.invoke(...)` with the right flag combination.
- **Capability-aware runtime selection becomes trivial.** A future
  adaptive runtime selector can compare runtimes on a single API
  surface; no side-car capability registry is needed.
- **Cleaner attachment story.** TEXT and multimodal have different
  owners with no overlap. Callers that accidentally pass TEXT through
  `attachments=` get an immediate `ValueError`.
- **Public docs surface.** `docs/creator/03_agent_contracts.md` is
  rewritten in the same change so plugin authors see the new contract
  on day one.

## Migration

In-place rewrite, no deprecation period (single-developer project, no
external install base — see `orcho-core/CLAUDE.md` § "No backcompat
ceremony"). All concrete runtimes and call sites were migrated in the
same commit; the old Protocol module is gone.

Test rewrite is the remaining body of work tracked as a separate follow-up:
existing tests directly exercise `.plan()` / `.run()` / `.review_*()` and
must move to `.invoke()` with the appropriate flag set.


## Phase 7.10: Round-Resume Policy Active

Looping phase handlers now resume their own runtime bridge after the first
round/attempt. PLAN/replan, validate_plan, review_changes, and hypothesis
attempts start fresh on round 1 and pass `continue_session=True` on round 2+.

The bridge remains runtime-local. Cross-runtime context still flows only through
prompt text.

Clean-slate behavior is preserved through `reset_session()`: clearing the
runtime's `session_id` makes the next `continue_session=True` call start fresh,
after which the runtime may capture a new bridge id for later rounds.

Repair escalation intentionally starts fresh when it swaps to a different
runtime instance/model. Final acceptance remains fresh because it is a closing
gate rather than a retry loop.


## CROSS_VALIDATE_PLAN — symmetric counterpart to validate_plan

The single-project pipeline runs `plan → validate_plan` with up to
`max_plan_rounds` retries and an optional `block_on_plan_reject` halt
(ADR 0022). The cross-project pipeline lacked the symmetric gate: it
produced `cross_plan.md` in a single architect call and went straight to
sub-pipelines, so a cross-plan that mis-scoped persistence or skipped
a project was only caught after sub-pipelines had already burned tokens.

`CROSS_VALIDATE_PLAN` closes that gap. Cross orchestration now follows
the same loop shape:

```
for round in 1..max_plan_rounds:
    if round == 1:
        cross_plan_prompt → claude_plan.invoke(prompt, common_cwd)
    else:
        cross_replan_prompt(critique) → claude_plan.invoke(
            prompt, common_cwd, continue_session=True
        )
    cross_plan.md written
    codex.invoke(review_prompt, common_cwd, continue_session=(round>1))
    parse_review → (approved, critique, review_dict)
    if approved: break
else:
    if block_on_plan_reject: halt
    else: warn + proceed with last plan
```

Reused machinery:
- `pipeline.review_parser.parse_review` + `review_json_contract`
  system-tail — same parser/contract as `validate_plan`, so reviewer
  output flows through one path.
- A local `_validate_cross_plan` helper symmetric to the hypothesis QA
  helper — isolated to the cross orchestrator because cross runtimes
  aren't `PipelineState`-coupled.
- `max_plan_rounds` / `block_on_plan_reject` config keys — same
  knobs, same semantics, no new surface.
- Round-resume policy (Phase 7.10) — round > 1 resumes both architect
  and reviewer bridges via `continue_session=True`.

Effort defaults: `CROSS_VALIDATE_PLAN` reads
`phase_effort_map["validate_plan"]`, mirroring how `CROSS_PLAN` reads
`phase_effort_map["plan"]`. The cross run-header Agents legend now
lists the new phase alongside the others.

New prompt assets:
- `_prompts/tasks/cross_validate_plan.md` — reviewer focus, mirrors
  `tasks/validate_plan.md` with cross-project-specific bars (subtask
  coverage, persistence-gap detection, producer/consumer alignment).
- `_prompts/tasks/cross_replan.md` — architect revise prompt, mirrors
  `tasks/replan.md` while preserving the `=== SUBTASK [<alias>] ===`
  machine grammar via `cross_subtask_block_contract`.
