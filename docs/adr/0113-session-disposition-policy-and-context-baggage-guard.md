# ADR 0113 — Session-disposition policy and context-baggage guard

- **Status:** Accepted
- **Date:** 2026-06-27
- **Deciders:** project owner
- **Relates to:** [ADR 0112](0112-multi-project-participant-set-and-scope-expansion-resetup.md)
  (shares the RunShape policy-projection pattern; different axis),
  [ADR 0036](0036-agent-session-persistence-across-subprocess-restart.md)
  (session-persistence substrate this policy governs),
  [ADR 0030](0030-runtime-context-autonomy.md) (the Orcho-owns-lifecycle /
  runtime-owns-context boundary this respects)
- **Coordinates with:** the session-aware subtask-DAG roadmap (this is the
  *selective* policy layer atop that roadmap's session-continuity work)
- **Supersedes:** nothing (append-only)

## Context

Orcho's run cost is dominated by input tokens, and the metric that matters is
**useful-work-per-token**, not tokens or dollars. The failure mode is a growing
context tail: each invocation re-transmits an ever-larger prefix (prior
transcripts, receipts, ritual) instead of carrying only the context the current
work needs.

Measured on run `20260627_091605_076e46` (~2h16m, ~110M tokens):

- **implement** 64.3M input, of which **63.1M cached read** — a giant prefix
  dragged forward across subtasks.
- **T4 companion** alone ~35.8M tokens.
- `continue_session=true` across T1→T4 subtasks.
- **review** input grew **1.16M (round 1) → 6.78M (round 6)** while tool calls
  **fell 31 → 6**. The agent did *less* new checking while *paying for more*
  context. That is the diagnostic signature: expensive because the tail grows,
  not because the work grows.

The cause is structural, not accidental:

1. **`continue_session` is decided ad-hoc, not as policy.** It is set per-adapter
   in `pipeline/phases/adapters.py` from "is there a resumed session id" and is
   hardcoded `True` in `pipeline/phases/review_contract_recovery.py`. There is no
   single place that decides, by work kind, whether reusing the prior provider
   session actually helps.
2. **Review and companion work inherit continuity they do not benefit from.**
   Review needs the *current* diff, unresolved findings, receipts, and contract —
   not the accumulated prior review/repair transcript. A companion-repo subtask
   needs a contract summary + changed files + required surfaces — not the full
   T1/T2/T3 transcript.

`RunShape` (`pipeline/runtime/run_shape.py`) already exists but is an **inert
Stage B value object** ("strictness posture, described not enforced"), and
`semantic_mode_defaults.py` currently projects only `SemanticProfile →
OperatingMode`. So the substrate for a projected policy exists but carries no
enforced session policy yet. This ADR adds the first one.

This is **not** a cost-accounting feature. A live cost dashboard, a budget
handoff at $X, or a review/repair loop cap are symptom management — a cap stops
the meter without telling you whether the spend was productive, and the
review/repair loop is the *product* (it caught the real P1s and the R1 blocker),
so it must not be capped. The right intervention is to stop carrying baggage, and
to assert it stays gone.

## Decision (proposed)

### 1. Session disposition is one projected policy, not an ad-hoc per-adapter flag

Introduce a single **session-disposition policy** that decides, per invocation,
whether to reuse the provider session (`continue_session=True`) or start fresh
with a compact handoff. It is keyed on the work kind (phase / subtask role) and
the run's operating mode, projected from the existing semantic-mode / RunShape
substrate — the same projection pattern ADR 0112 §5 uses for scope-expansion
sanction. The ad-hoc decisions in `adapters.py` / `review_contract_recovery.py`
are replaced by reads of this policy. (Whether the policy is a new enforced field
on `RunShape` or a sibling projection in `semantic_mode_defaults` is an
implementation choice for the plan phase; the decision here is that there is
**one** policy, not N hardcodes.)

### 2. Fresh-by-default; continue only on demonstrated same-zone benefit

`continue_session=True` only when the next invocation writes in the **same zone**
as the prior one and demonstrably benefits from the live transcript (e.g. a
follow-on edit subtask continuing the same file region). Everything else —
companion-repo work, review, audit, verification, boundary tasks — is **fresh +
compact handoff** by default.

### 3. Review is fresh by default

Review does not inherit the accumulated prior review/repair transcript. Its
input is assembled deliberately: the **current diff, unresolved findings,
receipts, and contract**. The `1.16M → 6.78M` growth with falling tool calls is
the textbook smell this removes.

### 4. Fresh is not free — a compact handoff is the load-bearing work

A fresh session must carry an Orcho-authored **compact handoff** (reuse the
existing digest machinery — `pipeline/control/implement_handoff_digest.py`,
`handoff_prompt.py`), not nothing. A bad handoff trades baggage for amnesia and
re-pays context anyway or loses continuity (the very thing the session-aware DAG
fixed). The engineering effort of this ADR lives in handoff quality, not in the
flag flip.

This respects ADR 0030: **Orcho owns the disposition** (a lifecycle decision)
and authors the compact handoff (contract input); the **runtime still owns its
context internals**. Orcho decides continue-vs-fresh; it does not reach into how
the runtime fills the context.

### 5. Baggage guard is a thin assertion, not a subsystem

Ship regression assertions, not a dashboard:

- a review invocation's input must not exceed ~N× the size of the diff it
  reviews;
- a fresh-policy invocation must not carry the transcript of prior subtasks.

A full "context baggage attribution" analytics product is **out of scope** and
deliberately deferred — the diagnosis above was reached without it, and it must
not be built before §2–§4 prove the baggage actually dropped. Measure that it
did not grow back; do not build the meter first.

## Scope / non-goals

- **Not** full `RunShape` enforcement — only the session-disposition policy
  becomes load-bearing here.
- **Not** a cost dashboard / budget handoff / loop cap. The review/repair loop is
  the product; if a backstop is ever wanted it is a *convergence* detector
  (escalate when a round stops reducing findings), keyed on inefficiency, not on
  a token/dollar threshold — and that is a separate decision, not this one.
- **Not** multi-project source resolution — that is [ADR 0112](0112-multi-project-participant-set-and-scope-expansion-resetup.md).
  0113 shares 0112's projection substrate but addresses a different axis: 0112 is
  *where code lives*; 0113 is *what context an invocation carries*.

## Sequencing

Land **after ADR 0112 P0** (correctness — the false-green fix — precedes
efficiency; and the one urgent efficiency item, provenance preflight, already
rides in 0112 P0). No reverse dependency: the projection substrate
(`run_shape.py` / `semantic_mode_defaults.py`) already exists, so 0113 is not
blocked by 0112. Coordinate the implementation phasing with the session-aware
subtask-DAG roadmap rather than creating a competing planner — 0113 is its
selective-policy counterpart.

## Acceptance criteria

- `continue_session` is decided in exactly one policy site; no phase/adapter
  hardcodes continuity.
- Review and companion invocations are fresh by default and carry a compact
  handoff; a same-zone follow-on may continue when the policy says so.
- Guard assertions from §5 are present and green; they fail if a fresh-policy
  invocation carries a prior-subtask transcript or review input balloons past the
  diff-relative bound.
- Correctness is unchanged: a fresh session with a good compact handoff produces
  the same verdicts as the prior continuity path on a representative flow (no new
  amnesia regressions).

## Implemented by

Status flipped Proposed → Accepted on delivery. The decision text above is
unchanged; this block records the as-built homes, the single rule, and the
reader sites.

- **Source of invocation roles.** A new public enum `SessionInvocationRole` in
  `pipeline/runtime/roles.py` is the single source of invocation-role identity
  (`implement`, `repair`, `plan`, `validate_plan`, `review`, `companion`,
  `format_repair`, `audit`, `verification`, `boundary`). It is a separate
  taxonomy from the legacy `AgentRole`, which is **not** reused for session
  continuity.
- **Policy home.** `pipeline/runtime/session_disposition.py` owns the pure,
  total projection `decide(*, role, same_write_zone, operating_mode) ->
  SessionDisposition` — the one and only continuity decision site (modelled on
  `semantic_mode_defaults.py`: no I/O, no `profiles.loader` import, with an
  exhaustive import-time guard over the role vocabulary).
- **Single rule.** `continue_session=True` **only** when the role is
  edit-shaped (`implement` / `repair`) **and** the follow-on is in the same
  write zone (`same_write_zone=True`). Every other role — `plan`/replan,
  `validate_plan`, `review`, `companion`, and the cross planner / reviewer, plus
  `format_repair` / `audit` / `verification` / `boundary` — is **FRESH** (round
  2+ planning/review work carries a compact handoff instead of resuming).
  `operating_mode` is a validated projection input that never relaxes
  continuation.
- **Role-explicit seam.** `pipeline/phases/builtin/session_keys.py` exposes the
  seam that always takes an **explicit** `SessionInvocationRole` and derives the
  policy inputs (`same_write_zone`, `operating_mode`) from run state:
  `decide_session_continuation(state, *, role, phase)` for state-keyed roles, the
  policy-backed `_should_continue_prompt_session(state, agent, *, role, phase)`
  for the agent-keyed (stored-session) implement/subtask roles, and the loop
  round helper `_should_resume(state, *, role, round_key)`. The role is **never
  inferred from the call stack or the caller's name** — callers pass it.
- **Reader sites** (read/reflect the policy with an explicit role; none decides
  continuity on its own):
  - `pipeline/phases/builtin/handlers/review_changes.py` — `review` → FRESH plus
    a round-2 compact handoff (repair receipt + current review subject +
    contracts), decoupled from continuation.
  - `pipeline/phases/builtin/handlers/repair_changes.py` — `repair` → policy on
    a same-write-zone input. The CHAIN posture is recorded only as that input
    (`state.extras['_repair_same_write_zone']`); **repair continuity is no
    longer derived from `SessionMode`/CHAIN**, and `state.extras['continue_session']`
    is no longer a second source.
  - `pipeline/phases/builtin/handlers/implement.py` — `implement` → CONTINUE on
    a same-write-zone follow-on, else FRESH.
  - `pipeline/phases/builtin/handlers/plan.py` — `plan`/replan → FRESH; replan
    carries the prior plan + reviewer critique handoff.
  - `pipeline/phases/builtin/handlers/validate_plan.py` — `validate_plan` →
    FRESH + plan/critique handoff.
  - `pipeline/phases/builtin/subtask_dag.py` — implement subtasks classify their
    invocation role per the **per-agent seeded write zone**
    (`_subtask_invocation_role`): a same-agent follow-on stays `implement`
    (CONTINUE allowed) only when same-write-zone is *demonstrated* — both the
    seeded zone and the subtask's declared write zone are non-empty and overlap
    (glob-aware, `_zones_overlap`, so `src/**` matches `src/foo.py`). A fresh
    specialist agent, a disjoint zone, or an **undeclared (empty) seed/current
    zone** are all fresh-by-default → reclassified `companion` → FRESH, so a
    reused agent never drags a prior subtask's transcript across zones. A
    companion invoke shares the implement physical session key but passes
    `commit_session=False` to `_session_aware_invoke`, so its fresh transcript
    never overwrites the shared implement session slot — a later same-write-zone
    implement follow-on resumes the original chain, not the companion's session.
  - `pipeline/dag_runner.py` — `_direct_invoke` routes the standalone path
    through `decide()` with an explicit `implement` role and a documented single
    source of inputs (no `bool(session_id)`).
  - `pipeline/cross_project/planning_loop.py` — `validate_cross_plan`
    (`review`) and cross-plan replan (`plan`) → FRESH + handoff; both hardcoded
    `continue_session=True` sites removed.
  - `pipeline/phases/review_contract_recovery.py` — the one-shot contract
    re-emit reads the policy as `format_repair` → FRESH (the prior output is
    embedded in the repair prompt).
  - `pipeline/phases/adapters.py` and
    `pipeline/phases/builtin/session_keys.py` — both `_runtime_session_meta`
    seams reflect the policy disposition the handler passed (the boolean it
    actually invoked with), not an independent `_last_resumed_session_id`
    probe. The session-aware handlers (implement, plan/replan, validate_plan,
    review, repair) thread their policy disposition into the seam so the
    persisted `phase_log` meta agrees with the policy even when the runtime
    resume primitive no-ops.
- **Baggage guard (§5).** Thin, test-only regression asserts live in
  `tests/unit/pipeline/runtime/test_session_adapters.py`
  (`TestReviewBaggageGuard`): the review input stays within a diff-relative
  bound, and a fresh-policy invocation does not carry a prior-round transcript.
  No measurement subsystem was added.

## Consequences

- Review input drops from the `6.78M`-class tail toward `~1M`; companion subtasks
  receive a contract summary + changed files + required surfaces instead of the
  full prior transcript.
- The cost concern is addressed by **removing baggage**, on the existing
  substrate — not by adding a measurement subsystem or a spending cap.
- Session disposition becomes the first enforced projected policy on the
  semantic-mode / RunShape substrate, establishing the pattern ADR 0112 §5
  (sanction) and future operating-mode policies reuse.
- The risk shifts to handoff quality; a weak compact handoff is now the thing to
  watch, and the §4 + acceptance "no amnesia regression" check is how it is held.

## Related

- [ADR 0112](0112-multi-project-participant-set-and-scope-expansion-resetup.md) —
  shared projection substrate, different axis.
- [ADR 0036](0036-agent-session-persistence-across-subprocess-restart.md) —
  the session-persistence substrate this policy governs.
- [ADR 0030](0030-runtime-context-autonomy.md) — Orcho owns lifecycle, runtime
  owns context; the boundary this disposition policy stays inside.
- `pipeline/phases/adapters.py`, `pipeline/phases/review_contract_recovery.py` —
  the current ad-hoc `continue_session` decision sites this replaces.
- `pipeline/control/implement_handoff_digest.py`, `pipeline/control/handoff_prompt.py`
  — the compact-handoff/digest machinery the fresh path reuses.

## Amendment 2026-06-29 — declarative per-phase continuity

The original decision centralised session-continuity into one decision site —
the right goal — but encoded the policy as a **hardcoded `frozenset` table** in
`pipeline/runtime/session_disposition.py` (`_FRESH_ONLY = {plan, validate_plan,
review, companion, …}` / `_EDIT_SHAPED = {implement, repair}`). That had two
problems:

1. **Regression for plan/validate.** Sweeping `plan` and `validate_plan` into
   `_FRESH_ONLY` made their loop round 2+ always start fresh, regressing the
   pre-0113 behaviour where a replan/re-validate resumed the prior loop session.
   The measured ROI for fresh covered **review only** (review input grew
   `1.16M → 6.78M` while tool calls fell `31 → 6` when it resumed); there was
   **no measured ROI for making plan/validate fresh** — they were classified by
   analogy, not data.
2. **Invisible-magic anti-pattern.** Whether a phase resumed or started fresh
   could not be seen or changed in the profile; it lived in a `frozenset` deep
   in the runtime, orthogonal to the `session_split` the operator *could*
   configure.

This amendment keeps 0113's goal (ONE continuity policy, not ad-hoc per-adapter
flags) but **moves the source of truth from the code table to a declarative
per-phase profile field**:

- New `ExecutionPolicy.session_continuity` (enum
  `fresh_only | loop_continue | same_zone_continue`), declared per phase in the
  `execution` block next to `session_split`. The two are **orthogonal**:
  `session_split` is how a session is shared *across phases* in one pass;
  `session_continuity` is whether a phase resumes *its own* prior session on a
  repeat invocation / loop round.
- `session_disposition.decide()` becomes a pure, total function over the closed
  `SessionContinuity` enum: it consumes the **resolved** policy plus the
  follow-on signals (`same_write_zone`, `loop_followon`) and performs no
  profile-JSON I/O. The resolver in `pipeline/phases/builtin/session_keys.py`
  maps the active step's declaration onto the enum above the call; auxiliary
  invocation roles (companion / contract re-emit / audit / verification /
  boundary) resolve to `fresh_only` from their invocation shape, one documented
  constant rather than a per-role table. The `_FRESH_ONLY` / `_EDIT_SHAPED`
  frozensets and the role partition are deleted.
- The import-time completeness guard's spirit is preserved in two places: the
  `decide()` match raises on any unhandled `SessionContinuity` member, and the
  resolver raises when a profile step EXISTS but omitted `session_continuity`
  (refusing to silently regress a phase to fresh). The only fresh fallback is
  the no-profile-context path (legacy `PipelineProfile` / standalone inline
  dispatch with no FSM-seeded step), where there is no declaration to read.

**Shipped defaults** keep today's behaviour for review/implement/repair and fix
the regression for plan/validate:

| phase | continuity | rationale |
| --- | --- | --- |
| `plan`, `validate_plan` | `loop_continue` | resume on round 2+ — restores pre-0113 behaviour (no measured fresh ROI here) |
| `implement`, `repair_changes` | `same_zone_continue` | resume only for a same-write-zone edit follow-on (unchanged) |
| `review_changes` | `fresh_only` | 0113's measured win is kept |

The continuity decision still lives in exactly one place (the resolver feeding
`decide()`); it is not re-scattered into adapters or
`review_contract_recovery.py`. The cross planner / reviewer
(`planning_loop._cross_continue_session`), the standalone DAG runner
(`dag_runner._direct_invoke`), and the format-repair re-emit
(`review_contract_recovery`) have no profile step, so they pass the policy
explicitly to `decide()` (`fresh_only` for cross plan/review and format-repair,
`same_zone_continue` for the standalone implement DAG).

**MCP visibility.** `session_continuity` is internal-only: like its sibling
`session_split`, it is not serialised by any SDK profile-listing surface
(`orcho_profiles_list`), so no `orcho-mcp` wire change or E2E smoke is required.
