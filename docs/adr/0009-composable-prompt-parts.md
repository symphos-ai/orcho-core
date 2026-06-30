# ADR 0009: Composable Prompt Parts

- **Status:** Accepted
- **Date:** 2026-05-07
- **Deciders:** project owner

## Context

Prompt templates were originally named and written as pipeline phases:
`phase0_architect_plan`, `phase1_developer_build`,
`phase2_reviewer_codex`, and so on. That was acceptable while the
pipeline was a fixed sequence, but the scheme dragged orchestration
abstractions into the prompt layer.

After the profile and runtime redesign, pipeline control belongs to the
typed architecture:

- `Profile`, `PhaseStep`, and `LoopStep` decide what runs, when, with
  which retries, gates, and inputs.
- Execution modes decide whether a step runs linearly or as a DAG.
- Session adapters decide how a step result lands in the durable
  session shape.
- Attachments, skills, and project context decide which external
  context the agent can see.

Prompt templates must not restate these control facts. Their job is to
shape the agent's role, the task framing, and the response contract.
The review/repair control contract is described separately in
[ADR 0010](0010-review-repair-contract.md).

## Decision

Separate prompting from orchestration:

```text
orchestration != prompting
step != prompt
role != phase
template != workflow
```

The target prompt system is a composer of small parts:

```text
_prompts/
  roles/
    architect.md
    developer.md
    reviewer.md

  tasks/
    plan.md
    decompose.md
    build.md
    code_review.md
    fix.md
    plan_qa.md

  formats/
    markdown_plan.md
    review_verdict.md
    dag_tasks_json.md
```

A profile step selects prompt parts, not a monolithic phase template:

```json
{
  "phase": "build",
  "runtime": "claude",
  "prompt": {
    "role": "implementation_engineer",
    "task": "build",
    "format": "handoff"
  }
}
```

The prompt composer assembles:

```text
role persona
+ task instruction
+ structured step inputs
+ output contract
+ project context
+ skills / attachments
```

On the current flat-template surface Orcho already applies the same
separation principle through `pipeline.prompts.contracts`:

```text
resolved user/project prompt
+ system-tail blocks
```

System-tail blocks are not a prompt override surface. They are appended
by code after the resolution chain and describe machine contracts or
execution strategy:

- `qa_verdict` — parser contract: the final `VERDICT:
  APPROVED/REJECTED` line is always in English;
- `change_handoff` — strategy: how authoring phases hand changes
  forward (`uncommitted` / `commit` / `commit_set`);
- `review_target` — strategy: which surface the reviewer must inspect.

This is a deliberate boundary: a project prompt may change the role,
style, and local instructions, but must not be able to accidentally
delete the machine verdict or alter handoff/review semantics.

## Boundaries

Prompt parts may define:

- the agent's role and thinking posture;
- the instruction for a specific task;
- quality expectations;
- the response format and parser contract;
- local phrasing of the context the agent will see.

Prompt parts must not define:

- phase numbers or a fixed pipeline order;
- retry, replan, review/fix loop, or resume mechanics;
- DAG scheduling or wave semantics;
- quality-gate launch policy;
- checkpoint/session persistence;
- the handoff strategy for code changes between authoring and review
  phases;
- the review target (`uncommitted`, commit, commit set);
- where previous steps store artifacts, beyond the named structured
  inputs the composer passes in.

## Consequences

Positive:

- Roles become reusable across different tasks and profiles.
- Response formats can change without copying the whole role.
- Project overrides become narrower: a project can replace only
  `roles/code_reviewer.md` or `formats/review_verdict.md`.
- The runtime/profile layer stays the single source of truth for
  workflow control.

Costs:

- The current flat `_prompts/*.md` files are a transitional surface.
  Once the composer API exists, they must move into `roles/`, `tasks/`,
  and `formats/`.
- Prompt rendering needs a typed config surface on `PhaseStep`, not a
  single string name.
- CLI/MCP prompt listing must learn to display composed prompts and
  individual parts without confusing the user.

## Migration sketch

1. Keep the current canonical flat names (`developer_build`,
   `architect_plan`, etc.) as a temporary implementation detail.
2. Add a prompt-composer module that can render `{role, task, format}`.
3. Extend the profile schema with an optional `prompt` object.
4. Migrate one low-risk step, most likely `reviewer_code_review`, to
   composed parts.
5. Migrate the remaining templates into `roles/`, `tasks/`, and
   `formats/`.
6. Remove flat template resolution once all shipped profiles use
   prompt composition.

## Ownership boundary clarification (2026-05-11)

Migrating the prompt families in practice showed that the original
`formats/*` definition mixed two different things: presentation style
and parser-required output shape. That created the wrong incentive to
put JSON schemas, enum values, and machine verdict fields into a
user-editable prompt part. This clarification supersedes the old
"parser contract" line in the Boundaries section above.

The corrected ownership matrix:

| Layer | Owns | Edit surface | Override |
| --- | --- | --- | --- |
| `roles/*.md` | Persona, posture, project anchor | user / project / workspace | yes |
| `tasks/*.md` | Procedure, checks, task variables | user / project / workspace | yes |
| `formats/*.md` | Presentation, detail, style: terse vs detailed, bullets vs prose, handoff style | user / project / workspace | yes |
| `pipeline.prompts.contracts` system-tail blocks (`review_json`, `qa_verdict`, `change_handoff`, `review_target`, `authoring_language`) | Machine output shape, parser contracts, execution policy | code | no |
| `pipeline.review_parser` / `core.contracts.review_schema` | Enforcement | code | no |

`formats/*.md` must never define parser-required JSON schemas, enum
values, machine verdict fields, or "return JSON" requirements. When a
phase output is parser-owned JSON, such as PLAN_QA, REVIEW, FINAL_QA, or
HYPOTHESIS_QA, the phase can legitimately omit `format` from its
`PromptSpec`. An empty placeholder `formats/*` file is forbidden: if
there is no user-editable presentation surface, the slot stays absent.

The system-tail block is the only authoritative source of machine output
shape. Its body can declare that it wins over earlier prompt text for
output shape, and parser-side rejection halts the phase before corrupted
output reaches downstream steps.

Enforcement lives in `tests/unit/pipeline/prompts/test_prompt_boundary.py`: shipped
`_prompts/formats/*.md` files are scanned for parser-contract tokens and
task-specific interpolation variables. New prompt families add a
`formats/*.md` part only when there is genuine user-editable presentation
content; otherwise the `format` field is omitted.

## Boundary invariant (2026-05-11)

Single-sentence rule for future migrations:

> Anything that must NOT be silently droppable by a project override of a
> user-editable prompt part lives in `pipeline.prompts.contracts` as a
> code-owned system-tail block.

That single rule generates the entire ownership matrix above. If you can
imagine a project author copying a `roles/`/`tasks/`/`formats/` file into
their override directory, deleting any line, and the result being a
broken Orcho run — that line is in the wrong layer.

Operational enforcement (in priority order):

1. **Structural** — `tests/unit/pipeline/prompts/test_prompt_boundary.py` fails CI on any
   leak of config-owned vocabulary into `_prompts/roles/`,
   `_prompts/tasks/`, or `_prompts/formats/`.
2. **Procedural** — `orcho-core/AGENTS.md → Prompt Boundary Discipline
   (ADR 0009)` is the first thing agents read; `_prompts/README.md`
   carries the ownership matrix and forbidden-examples table.
3. **Architectural** — this section + the ownership matrix above.

If 1 fires, fix the leak before commit. If 2 / 3 don't yet describe a
case you just encountered, update them in the same change.

## Legacy flat templates excised (2026-05-12)

Phase 8 removed the 13 legacy root-level flat templates
(`architect_decompose`, `architect_hypothesis`, `architect_plan`,
`architect_readonly_plan`, `architect_replan`, `cross_architect_plan`,
`developer_build`, `developer_fix`, `reviewer_code_review`,
`reviewer_file`, `reviewer_hypothesis_qa`, `reviewer_plan_qa`,
`reviewer_uncommitted`) along with the ``legacy_name`` fallback
parameter on ``render_composed_prompt``. The shipped ``_prompts/``
directory now contains only composable parts.

Project prompt overrides are supported for composable prompt parts:
``roles/*``, ``tasks/*``, ``formats/*``. Legacy root-level flat prompt
names are removed and no longer participate in prompt resolution; a
project that ships e.g. ``.agent/multiagent/prompts/developer_build.md``
has zero effect on the rendered prompt and must be migrated to the
composable-part layout.

Negative tests
(``tests/unit/pipeline/prompts/test_prompt_composer.py::test_legacy_root_flat_override_no_longer_renders``,
``tests/unit/pipeline/prompts/test_prompts.py::TestLegacyFlatOverrideRemoved``) pin
this behavior; structural boundary tests
(``tests/unit/pipeline/prompts/test_prompt_boundary.py``) keep the existing leak guards
green.

## Final architecture state (2026-05-12)

The ADR 0009 family migration plus the Phase 7.9 contract-template
cleanup converge on this canonical surface. Treat the points below
as the architecture invariants for any future prompt work.

### Canonical prompt surface

- **User-editable composable parts** live in
  ``_prompts/{roles,tasks,formats}/*.md`` and resolve through the
  project → workspace → core chain. Project overrides are supported
  only at this layer.
- **Legacy root-level flat templates do not exist.** The
  ``render_composed_prompt`` API has no ``legacy_name`` fallback;
  any flat-named file shipped by a project has zero effect on
  prompt resolution.
- **Code-owned system-tail contracts** live in
  ``pipeline/prompts/contract_templates.py`` as frozen
  :class:`SystemPromptTemplate` constants with typed
  :class:`TemplateSlot` declarations. ``pipeline/prompts/contracts.py``
  is the public API + branching layer. There is **no markdown file
  under** ``_prompts/system`` **and there will not be one** — project
  overrides must not reach the machine-contract / safety-policy
  layer.

### Body-content discipline (Phase 7.9b / 7.9c / 7.9e)

Every rendered system-tail body contains operative content only.
The criterion:

> A line that tells the agent what action to take or what form the
> response takes is prompt. A line that explains why the pipeline
> is shaped this way is documentation.

In practice:

- No self-descriptive meta lines (``"This block is an Orcho machine
  contract, not project guidance."``, ``"If any earlier prompt text
  conflicts ..."``). The ``<orcho:system-block kind="..." name="...">``
  envelope already declares this structurally.
- No orchestrator brand or phase names in rendered bodies (no
  ``Orcho``, no ``REVIEW/FIX/FINAL_QA``, no ``authoring phase``,
  no ``pipeline``, no ``downstream``).
- The wrapping ``<orcho:system-block>`` XML namespace marker is
  intentionally kept — it is structural protocol surface (parser
  marker), not natural-language self-description.

Structural CI guards in ``tests/unit/pipeline/prompts/test_prompt_boundary.py``:

- ``test_no_orchestrator_topology_in_contract_template_bodies`` —
  strict regex over every ``SYSTEM_PROMPT_TEMPLATES`` body.
- ``test_no_orchestrator_brand_in_user_editable_parts`` — looser
  regex over ``_prompts/{roles,tasks,formats}/*.md`` (flags
  ``Orcho`` / ``pipeline`` / ``orchestrat*`` / ``downstream`` /
  ``upstream`` / ``authoring phase``; allows uppercase phase verbs
  in task-instruction headers like ``TASK TO DECOMPOSE``).

### Typed slot contracts (Phase 7.9d)

Template variables are typed contract slots, not bare names:

- :class:`TemplateSlot` declares ``kind`` (``schema`` / ``language``
  / ``directive`` / ``grammar`` / ``plain``), ``value_type``,
  ``required``, ``allow_empty``, ``multiline``.
- :meth:`SystemPromptTemplate.render` validates supplied values
  against their slots — unknown vars, missing required vars, wrong
  types, empty value in ``allow_empty=False`` slots, and **newlines
  in ``multiline=False`` slots** all raise before substitution.
  The newline guard is the explicit prompt-injection rail.
- :meth:`SystemPromptTemplate.__post_init__` enforces at module-
  import time that slot names and body placeholders match 1:1.

### Open smell

``language_directive`` is still passed to ``render()`` as a
pre-assembled prompt fragment (``"\nWrite the human-readable JSON
fields ... in Russian."`` or ``""``) rather than as structured data
(``body_language="Russian"``). The slot is marked ``kind="directive"``
to flag it for future replacement with a renderer-composed fragment.
Typed slots are the prerequisite for that move; no action item
attached to this ADR.

## Phase Q1a — Runtime role vs prompt role split (2026-05-12)

The runtime ``AgentRole`` enum (``developer`` / ``architect`` /
``reviewer``) and the prompt-role file names under ``_prompts/roles/``
were nominally aligned through fallback resolution. That alignment
conflated two concerns:

- **Runtime role** — execution-dispatch slot. Picks the agent runtime,
  binds the plugin role mapping, lives on ``PhaseStep.role`` and on
  ``Profile``-top-level ``role`` JSON. Stable wire-format vocabulary.
- **Prompt role** — professional persona file the agent renders as.
  User-editable presentation surface; taxonomy free to grow as new
  personas emerge.

Phase Q1a separates the two taxonomies without touching ``AgentRole``
or any wire format:

- ``_prompts/roles/developer.md`` → ``implementation_engineer.md``
- ``_prompts/roles/architect.md`` → ``systems_architect.md``
- ``_prompts/roles/reviewer.md`` → ``code_reviewer.md``
- New: ``_prompts/roles/product_owner.md`` (opt-in via explicit
  ``prompt.role``; no runtime fallback binding).

``PromptSpec.part_names`` now translates ``fallback_role`` through a
frozen ``RUNTIME_TO_PROMPT_ROLE`` mapping in
``pipeline/prompts/spec.py``:

```python
RUNTIME_TO_PROMPT_ROLE = {
    "developer": "implementation_engineer",
    "architect": "systems_architect",
    "reviewer": "code_reviewer",
}
```

Explicit ``PromptSpec.role`` still passes through verbatim — a profile
can route a custom persona (``technical_editor``, ``security_auditor``)
through any runtime role.

Profile JSON is unchanged: all 24 shipped ``PromptSpec`` declarations
use ``fallback_role`` (no explicit ``prompt.role``), so the mapping
covers them transparently. The runtime ``AgentRole`` enum and
``Profile``-top-level ``role`` field are untouched — this is a
prompt-only rename.

Phase/task-name taxonomy (``plan_qa`` → ``validate_plan``,
``hypothesis_qa`` → ``validate_hypothesis``) is **out of scope** for
Q1a. Those are phase names, not prompt-role names: they thread through
the phase registry, session shape ``state.phase_log["plan_qa"]``, MCP
wire format, evidence files, and golden snapshots. A phase-name
rename needs its own ADR + migration plan.

## Phase A5.2a cleanup — No profile runtime-role fallback (2026-05-12)

A5.2a supersedes the Q1a fallback bridge. Prompt composition now has
one role-like input: ``PromptSpec.role``, the prompt persona file name
under ``_prompts/roles/``.

The removed bridge includes:

- ``RUNTIME_TO_PROMPT_ROLE`` and ``resolve_prompt_role``;
- ``fallback_role`` parameters on prompt composition APIs;
- ``role`` keys in shipped v2 profile steps;
- ``PhaseStep.role`` in the profile dataclass / JSON schema.

Profiles that declare a ``prompt`` block must set ``prompt.role``
explicitly. Omitting the whole ``prompt`` block means "use the
phase builder's code-owned default prompt spec"; it does not create a
runtime-role fallback path.

The older ``AgentRole`` vocabulary still exists only for legacy
behaviour-intent type surfaces such as cross-project planning and typed
agent-protocol wrappers. It is not a runtime-selection layer, a second
prompt taxonomy, or part of profile prompt rendering.
