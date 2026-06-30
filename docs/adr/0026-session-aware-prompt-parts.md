# ADR 0026 — Session-aware prompt parts and stable prefixes

**Status:** Accepted / implemented (M1-M12)
**Date:** 2026-05-15
**Companion to:** ADR 0009 (composable prompt parts), ADR 0023 (runtime protocol collapse)

Implementation note (2026-05-18): M1-M12 landed the design through the
session-aware prompt-engine execution plan (internal planning record). The code
keeps legacy/debug compatibility fields (`kind`, `name`, `source`, `body`,
`version`) on `PromptPart` alongside the ADR fields (`id`, `layer`,
`stability`, `cache_scope`, `volatile_reason`).

## Context

ADR 0009 split Orcho prompts into role, task, format, and code-owned
contract layers. That solved ownership: project overrides can change
editable prose, while parser contracts and handoff policy stay protected.

The next bottleneck is session efficiency. Today a resumed runtime call can
inherit provider context, but the composer still tends to rebuild a full
prompt string. Multi-round phases therefore resend large role rubrics,
JSON contracts, handoff policy, and language policy even when the same
physical runtime session has already seen them.

Token trimming helps, but it is not the whole optimization. A prompt can be
shorter and still cache poorly if run-specific text appears before stable
instructions. The durable rule is:

```text
stable text must stay early, ordered, and byte-stable;
run/round text must stay behind it.
```

This ADR defines the prompt-session design that lets Orcho preserve stable
prefixes, track which prompt parts a physical session has already received,
and render only the per-turn delta when a session is resumed.

## Decision

Introduce prompt parts as typed rendering units rather than treating the
prompt as one opaque string.

```python
@dataclass(frozen=True)
class PromptPart:
    id: str
    layer: PromptLayer
    stability: PromptStability
    cache_scope: PromptCacheScope
    text: str
    volatile_reason: str | None = None
```

Initial layer vocabulary:

| Layer | Meaning | Typical stability |
| --- | --- | --- |
| `bootstrap` | Short Orcho-wide invariants shared by all phases | static |
| `role` | Architect, implementer, reviewer posture | static |
| `phase` | Phase-specific procedure and review surface | static or profile |
| `contract` | Parser/output schema and protected policy | static or profile |
| `context` | Project/workspace facts and loaded attachments | run |
| `turn` | Task text, artifact content, feedback, round number | turn |

Initial stability vocabulary:

| Stability | Meaning |
| --- | --- |
| `static` | Same across runs until the part version changes |
| `profile` | Depends on profile or handoff mode, but not on the task |
| `run` | Depends on project, task, attachments, or run config |
| `turn` | Depends on the current artifact, diff, feedback, or round |

Initial cache scope vocabulary:

| Cache scope | Meaning |
| --- | --- |
| `global` | Safe to keep as a stable prefix across projects and runs |
| `workspace` | Stable inside a project/workspace, not globally |
| `session` | Stable only inside one physical runtime session |
| `none` | Always resend |

The composer renders parts in two ordered regions:

```text
stable prefix:
  bootstrap
  role
  phase
  contract
  cacheable context

turn payload:
  task
  artifact/diff
  prior findings
  round feedback
  other volatile inputs
```

The boundary is structural, not a magic marker string. Renderers may emit a
human-readable divider for trace logs, but tests and cache behavior depend
on part metadata and ordering, not on a copied sentinel.

## Physical Session Keys

A logical run can use multiple physical runtime sessions. A physical session
is keyed by the runtime boundary:

```python
@dataclass(frozen=True)
class PhysicalSessionKey:
    run_id: str
    runtime: str
    model_key: str
    scope: str
```

Default scope values:

| Session mode | Scope key |
| --- | --- |
| `stateless` | no reusable session |
| `per_phase` | phase name |
| `per_role` | prompt role |
| `common` | run id |

`common` is still split by runtime and model. A run that plans with one
runtime and reviews with another gets two physical sessions. Orcho never
assumes that provider context can cross runtime boundaries.

Model boundaries are strict by default. A future runtime capability may
explicitly allow a relaxed model key, but the safe baseline is exact model
matching.

## Prompt Session State

The runtime `session_id` is not enough. Orcho also needs to know which
prompt parts were already sent into that physical session:

```python
@dataclass
class PromptSessionState:
    key: PhysicalSessionKey
    session_id: str | None
    sent_part_ids: set[str]
    active_role_id: str | None
    active_phase_id: str | None
    active_contract_ids: set[str]
```

Rendering policy:

```text
stateless:
  render every selected part

fresh physical session:
  render every selected part

resumed physical session:
  render every selected part whose cache_scope is none
  render every selected part whose stability is turn
  render every selected part id not in sent_part_ids
  render a changed role/phase/contract part even if the layer was active before
```

After a successful runtime invocation, the caller records the captured
runtime `session_id` and all sent part ids in `PromptSessionState`.

## Bootstrap

Bootstrap is a small protected prefix, not a new monolith. It contains only
rules that are identical for every Orcho prompt:

```text
Follow the active task and newest phase instructions.
Preserve user-owned working-tree changes.
Destructive git operations require an explicit rollback request.
Machine output contracts are binding.
When a JSON contract is active, emit only that JSON object.
Keep identifiers, language keywords, and protocol enum values literal.
```

Bootstrap is code-owned and cannot be removed by project prompt overrides.
It should stay short enough that adding it once per physical session is
obviously worth the context it consumes.

## Contract Switching

Session reuse must never make output shape ambiguous.

Rules:

1. If the active output contract changes, send the new contract part.
2. If the prompt role changes, send the new role part.
3. If the phase surface changes, send the new phase part.
4. If only the artifact, diff, feedback, or round changes, send only the
   turn payload.
5. If a part id version changes, treat it as unseen and send it again.

Example reviewer flow in `per_role` mode:

```text
validate_hypothesis:
  bootstrap + reviewer role + validate_hypothesis phase
  + review_json contract + hypothesis artifact

validate_plan:
  validate_plan phase + plan artifact
  review_json is omitted if the same contract id is already active

review_changes:
  review_changes phase + review target
  review_json is omitted if the same contract id is already active
```

## Stable Prefix Invariants

The composer must preserve these invariants:

1. Static parts precede run and turn parts.
2. Static part text is byte-stable for the same part id.
3. Task text, artifact content, file paths, feedback, and round numbers do
   not appear in globally cacheable parts.
4. A change to a part's meaning bumps its id version.
5. Render traces include selected part ids and prefix hashes so regressions
   are observable without pinning exact token counts.

Recommended trace fields:

```json
{
  "part_ids": ["bootstrap:orcho:v1", "role:reviewer:v1"],
  "stable_prefix_hash": "sha256:...",
  "turn_payload_hash": "sha256:...",
  "render_mode": "full|delta",
  "session_key": "..."
}
```

The token-budget tests should track threshold regressions, while prompt
shape snapshots should track structure: selected part ids, part ordering,
and hash stability.

## Volatility Discipline

Prompt parts are stable by default. A part that changes per run or per turn
must opt into volatility and explain why.

```python
PromptPart(
    id="turn:plan_artifact:v1",
    layer=PromptLayer.TURN,
    stability=PromptStability.TURN,
    cache_scope=PromptCacheScope.NONE,
    volatile_reason="Contains the current plan artifact under review.",
    text=plan_markdown,
)
```

Rules:

1. `volatile_reason` is required when `stability` is `run` or `turn`, or
   when `cache_scope` is `none`.
2. `volatile_reason` must be absent for `static` + `global` parts.
3. Volatile parts are forbidden in the globally cacheable prefix.
4. Static parts must not interpolate task text, artifact content, round
   numbers, run ids, temporary paths, timestamps, or previous findings.
5. If a part needs those values, keep the part below the stable prefix and
   document the volatility reason.

This makes dynamic prompt content an explicit exception rather than the
default. A review of prompt changes should be able to answer: which parts
became volatile, and why was that necessary?

Examples:

| Part | Stability | Cache scope | Reason |
| --- | --- | --- | --- |
| `contract:review_json:v2:compact` | static | global | none |
| `policy:change_handoff:uncommitted:v1` | profile | global | none |
| `context:project_digest:v1` | run | workspace | Project-specific code/context summary |
| `turn:review_diff:v1` | turn | none | Contains the current diff under review |
| `turn:replan_feedback:v1` | turn | none | Contains the previous rejected critique |

Static/profile parts may be memoized for a physical session and reused until
one of these invalidation events occurs:

- a new physical session key is selected;
- the runtime session is reset;
- the conversation is compacted or cleared;
- the part id version changes;
- the part's profile-dependent selector changes, such as handoff mode or
  contract verbosity.

## Dynamic Protected Parts

Protected does not mean cacheable. A prompt part can be code-owned,
required, and non-overridable while still belonging below the stable prefix.

The design has two independent axes:

| Axis | Values | Question answered |
| --- | --- | --- |
| Ownership | `protected`, `editable`, `runtime_context` | Who may change or remove it? |
| Placement | `stable_prefix`, `turn_payload` | Where does it belong for cache and session reuse? |

Only static/cacheable protected parts belong in the stable prefix. Protected
parts that depend on runtime tools, environment, project state, run config,
or the current turn must live in the dynamic payload and carry volatility
metadata.

Examples:

| Part | Ownership | Placement | Why |
| --- | --- | --- | --- |
| `bootstrap:orcho:v1` | protected | stable prefix | Same for every Orcho prompt |
| `contract:review_json:v2` | protected | stable prefix | Parser schema, versioned and static |
| `policy:change_handoff:uncommitted:v1` | protected | stable prefix | Static for that selected mode |
| `runtime:tool_instructions:v1` | protected | turn payload | Depends on available tools/connectors |
| `runtime:environment:v1` | protected | turn payload | Depends on cwd, platform, and run context |
| `context:project_hints:v1` | runtime context | turn payload | Project-specific, not globally reusable |
| `turn:attachment_summary:v1` | runtime context | turn payload | Current invocation input |

Rules:

1. Parser schemas and global policy contracts may be stable prefix parts.
2. Tool, environment, project, attachment, and run-specific instructions
   default to dynamic payload placement.
3. Dynamic protected parts require `volatile_reason`.
4. Registry-managed dynamic sections should be explicit parts, not text
   spliced into bootstrap or parser contracts.
5. Feature flags and profile knobs should select part ids; they should not
   silently mutate static part text.
6. If a protected part changes across runs, trace output must show the part
   id, stability, cache scope, and volatility reason.

## Override Semantics

This design preserves Orcho's existing project/workspace prompt tuning model.
Session-aware parts must not turn protected contracts into a replacement for
local prompt overrides.

Editable prompt parts keep the same resolution chain:

```text
project prompt override
workspace prompt override
core prompt
```

The chain is first match wins. For each editable slot, the composer selects
one winner:

```text
role slot:
  roles/<role>

task slot:
  tasks/<task>

format slot:
  formats/<format>
```

If a project supplies `.agent/multiagent/prompts/tasks/plan.md`, that file
replaces the core `tasks/plan.md`; both must never render together. The same
rule applies to workspace overrides and to role/format parts.

Protected prompt parts are selected independently of editable overrides:

```text
bootstrap
parser/output contracts
handoff and review-target policy
language / enum posture
```

Protected means code-owned and non-overridable. It does not mean "append at
the end." In the session-aware renderer, protected contracts usually live in
the stable prefix so they are safe, early, and cache-friendly.

Additive project knobs remain additive and must be named that way. Examples
include project-specific plan/build/review extra instructions. They are not
overrides and should render as context or turn payload parts with explicit
metadata, not as a second copy of the core editable slot.

Required invariants:

1. A project or workspace override replaces the matching core editable part.
2. Core and override text for the same editable slot never render together.
3. Protected parts cannot be removed by editable prompt overrides.
4. Protected placement is chosen by cache/contract needs, not by the old
   system-tail position.
5. Prompt traces show the selected editable source (`project`, `workspace`,
   or `core`) for each slot so tuning is debuggable.

## Migration Plan

1. Add `PromptPart`, `PromptEnvelope.parts`, and a renderer that still
   produces the current full prompt in stateless mode.
2. Move existing system-tail blocks into typed protected parts:
   bootstrap, contracts, policies, and language posture.
3. Add prefix/turn partitioning and tests that prove different tasks share
   the same stable prefix for the same role/phase/contract selection.
4. Add `PromptSessionState` and pure selection logic for full vs delta
   rendering.
5. Replace phase-specific file prompt templates such as
   `tasks/validate_plan_file.md` and `tasks/validate_hypothesis_file.md`
   with the normal validation phase part plus dynamic artifact parts
   (`artifact:file_path`, `artifact:file_content`). File content is turn
   payload, not a second copy of the validation procedure.
6. Rewrite shipped prompt prose against the new part model. This is not a
   mechanical word trim: move generic behavior into bootstrap/role/policy,
   keep phase parts phase-specific, keep dynamic context out of static
   parts, and prefer short imperative instructions over explanatory
   rationale unless the rationale prevents a known failure mode.
7. Wire `per_phase` mode for multi-round `validate_plan` first.
8. Extend to hypothesis/plan/replan and review/repair loops.
9. Add `per_role` and `common` profile knobs only after per-phase behavior
   is tested and observable.

## Consequences

Benefits:

- Resumed sessions can stop resending stable contracts and role rubrics.
- Provider-side prefix reuse becomes more likely because volatile text is
  kept out of the prompt prefix.
- Prompt traces become easier to reason about: reviewers can see which
  protected contracts were present without reading the full prompt.
- Future compact/strict contract variants become ordinary part ids rather
  than ad-hoc string branches.

Costs:

- Prompt rendering becomes stateful when session reuse is enabled.
- Part ids need version discipline.
- Tests must cover both prompt text and prompt shape.
- Runtime/session observability must include the physical session key, not
  only the raw runtime `session_id`.

## Non-goals

- This ADR does not require immediate delta rendering for every phase.
- This ADR does not assume all runtimes support resumable sessions.
- This ADR does not let project prompts remove protected contracts.
- This ADR does not define provider-specific cache APIs; it defines the
  Orcho-side prompt structure that makes stable-prefix reuse possible.
