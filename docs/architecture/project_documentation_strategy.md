# Project Documentation Strategy

Orcho treats documentation strategy as part of successful software delivery,
not as cleanup after a run. This document describes the documentation model
Orcho should help maintain for a target project or workspace. Orcho's own
repository can dogfood the same model, but it is not the subject of the
strategy.

A project can pass tests and still fail as a development system if the next
contributor cannot answer:

- what changed;
- why it changed;
- which decisions are stable;
- which assumptions still need review;
- where to continue without rereading raw transcripts.

The documentation strategy is the operating model that keeps those answers
durable across runs, sessions, and agents.

## Goals

The documentation module managed with Orcho has five jobs:

1. Preserve decisions in a form humans can review.
2. Separate raw evidence from curated knowledge.
3. Keep stable contracts close to the schemas and code that define them.
4. Make long-running work resumable across sessions and agents.
5. Give indexing systems clean material to search later without making them
   the source of truth.

## Protocol Artifacts Are Not Documentation

Orcho's operational protocol artifacts are mandatory. They are required for the
orchestrator to run, resume, inspect, audit, and integrate with external
clients. They are not part of the optional documentation strategy.

Mandatory protocol artifacts include:

- run events;
- checkpoints;
- execution state;
- plan contract state;
- evidence bundles;
- review and gate results;
- metrics needed to explain the run;
- artifacts needed by CLI, MCP, Web, or other clients.

These artifacts may be rendered into human-readable summaries, and they may
feed documentation updates, but they are not "project documentation". A
documentation profile must not disable them, because disabling them would break
the protocol itself.

The documentation strategy starts after the protocol has enough durable state
to answer what happened in a run. It decides whether and how that state should
be promoted into feature specs, plans, project docs, or knowledge pages.

## Source Layers

For each target project, Orcho distinguishes one operational layer and four
documentation layers.

| Layer | Purpose | Examples | Owner |
|-------|---------|----------|-------|
| Protocol evidence | What happened in one run and what the protocol needs to resume or inspect it | run events, evidence bundle, review findings, command results, changed files | Orcho runtime |
| Feature spec | What the product or domain behavior should be | feature overview, scenarios, user-visible behavior, acceptance semantics, examples | Target repository |
| Delivery plan | What should happen next | milestone plan, scoped task plan, acceptance criteria, unresolved blockers | Human or planning agent |
| Project docs | What is true enough to build against | architecture, setup, API reference, schema docs, operational guides, ADRs | Target repository |
| Project knowledge | What should survive across sessions but may evolve quickly | facts, pitfalls, glossary, open questions, decision index | Target repository |

Protocol evidence is mandatory raw material. Feature specs define the intended
behavior. Delivery plans define the implementation path. Project docs and
project knowledge are the durable layers that keep architecture, contracts, and
working memory coherent after the run is over.

## Target Project Taxonomy

Orcho should not force every project into one directory layout. It should infer
or create a small documentation map for the target repository and keep that map
explicit. A common default looks like this:

| Need | Write it in |
|------|-------------|
| How to run, test, deploy, or operate the project | project README, `docs/setup/`, `docs/operations/` |
| Feature behavior, scenarios, and product/domain semantics | `docs/features/` |
| Architecture and lifecycle contracts | `docs/architecture/` |
| Stable API, schema, and wire-format contracts | `docs/reference/` |
| Developer or extension authoring | `docs/guides/` |
| Architectural decisions | `docs/adr/` |
| Active implementation plan | `docs/plans/YYYY-MM-DD-topic.md` |
| Exploratory research | `docs/research/YYYY-MM-DD-topic.md` |
| Fast-moving project memory | `docs/knowledge/` |

`docs/knowledge/` is intentionally separate from `docs/reference/`.
Reference docs describe stable contracts. Knowledge pages describe the
working memory of the project: known pitfalls, naming conventions, unresolved
questions, and cross-cutting decisions that are too small for an ADR.

Feature docs are distinct from delivery plans. A feature doc says what the
system should do, which scenarios matter, which invariants must hold, and how a
human should understand the behavior. A delivery plan says how to implement or
change that behavior in this milestone.

For existing projects, Orcho should adapt to the repository's current layout
instead of creating duplicate doc trees. The durable rule is not the path; it is
the separation between evidence, feature specs, plans, stable docs, and working
knowledge.

## Research And Eval Labs

Some documentation and validation work belongs in a separate research or eval
lab instead of the target project's runtime repo. A prompt calibration bench is
the canonical example: it may need synthetic cases, ablation modes, evaluator
scripts, provider comparison outputs, and roadmap notes that inform production
defaults without becoming production runtime code.

An external lab should have its own repository boundary:

- its own README and agent instructions;
- architecture notes and plans under its own `docs/`;
- fixtures and synthetic cases that are safe to share;
- tests and smoke commands that do not depend on real provider credentials;
- a one-way dependency on the runtime package it studies;
- no reverse import from the runtime package back into the lab.

In the Orcho workspace, `orcho-lab` is this kind of calibration bench for prompt
utility evaluation. It can inform prompt policy and profile defaults, but
`orcho-core` must remain independent from it. This is an example of the broader
documentation strategy: research/eval knowledge can live in its own project
when it has a different audience, cadence, and evidence model from the runtime
it studies.

## Wiki-Style Operating Loop

The preferred style is a lightweight wiki loop:

```text
protocol evidence -> extracted notes -> curated feature/docs/knowledge pages -> index
```

This mirrors the practical "LLM wiki" pattern: keep raw source material
available, but let the repository accumulate compact, linked, human-readable
pages that agents can maintain and humans can review.

The loop has four rules:

1. Do not make raw transcripts the durable interface.
2. Do not make search indexes the durable interface.
3. Promote repeated or consequential facts into curated markdown.
4. Link back to the evidence or ADR that justifies a non-obvious decision.

This style gives each project a durable memory without turning memory storage
into the documentation source of truth. Markdown remains the canonical layer;
indexes are accelerators.

## Agent-Friendly, Human-Readable Docs

Project documentation should be easy for agents to use without becoming
machine-state output. The same markdown page should serve both audiences:

- humans can scan it, trust it, and understand the narrative;
- agents can retrieve it, quote precise sections, extract constraints, and
  continue work without rereading raw transcripts.

Agent-friendly docs prefer:

- stable headings;
- short sections with one clear purpose;
- explicit status, owner, and freshness notes when useful;
- linked related docs and source evidence;
- Obsidian-friendly markdown structure and links;
- examples and acceptance semantics near the feature or contract they explain;
- concrete file paths, commands, schemas, and invariants;
- compact decision summaries before detailed rationale.

They avoid:

- prose that hides the actual decision;
- huge transcript-like pages;
- clever formatting that is hard to chunk or quote;
- machine-state blobs inside human docs;
- duplicating the same fact in several places without a clear canonical page.

The goal is not to make documentation robotic. The goal is structured,
human-written markdown with enough predictable shape that an agent can safely
use it as context.

## Run-To-Docs Flow

A completed run may produce documentation updates in this order:

1. **Protocol evidence:** record what happened and which checks ran.
2. **Summary:** render a short human-readable account of the run.
3. **Decision extraction:** identify stable decisions, assumptions, blockers,
   and follow-up work.
4. **Placement:** decide whether each item belongs in an ADR, architecture doc,
   feature spec, reference doc, guide, plan, research note, or knowledge page.
5. **Review:** keep doc changes in the repository diff so they can be reviewed
   with code.

The run should not blindly rewrite broad documentation. It should make narrow,
traceable updates in the target repository and prefer adding a small knowledge
note over reshaping a stable guide when the fact is still fresh.

## Configurable Documentation Capabilities

Everything above the mandatory protocol layer must be configurable.
Documentation strategy is a module, not a hard requirement for running Orcho.

A project may choose a set of documentation capabilities. Capabilities are
independent so a project can enable, for example, ADRs and plans without feature
spec updates.

| Capability | Behavior |
|------------|----------|
| `summaries` | Render human-readable run summaries without editing project docs. |
| `plans` | Update delivery plans and completion markers. |
| `features` | Update feature specs when user-visible or domain behavior changes. |
| `adrs` | Create or supersede ADRs for architectural decisions. |
| `reference` | Update stable API, schema, and wire-format references. |
| `guides` | Update developer, extension, setup, or operations guides. |
| `knowledge` | Update fast-moving knowledge pages such as pitfalls, glossary, open questions, and decision indexes. |
| `research` | Add or update exploratory research notes. |

Profiles are optional presets over the capability set, not the primitive model.

| Profile | Behavior |
|---------|----------|
| `off` | Do not update target-project documentation. Keep mandatory protocol artifacts only. |
| `evidence-summary` | Enable `summaries`. |
| `planning` | Enable `summaries` and `plans`. |
| `product` | Enable `summaries`, `plans`, and `features`. |
| `architecture` | Enable `summaries`, `plans`, `adrs`, `reference`, and `guides`. |
| `full` | Enable all documentation capabilities. |

Example configuration:

```yaml
documentation:
  enabled: true

  # Primitive model: independent capabilities, not a linear profile.
  capabilities:
    - summaries
    - plans
    - adrs
    - reference
    - knowledge

  # Optional preset alias. If both profile and capabilities are set,
  # capabilities are the final explicit source of truth.
  profile: architecture

  paths:
    features: docs/features
    plans: docs/plans
    adrs: docs/adr
    reference: docs/reference
    guides: docs/guides
    knowledge: docs/knowledge
    research: docs/research
    summaries: .orcho/summaries

  behavior:
    update_existing_first: true
    create_missing_pages: true
    require_human_review_for:
      - adrs
      - reference
    never_rewrite_broad_docs: true
    link_to_protocol_evidence: true

  style:
    agent_friendly: true
    human_readable: true
    obsidian_friendly: true
    stable_headings: true
    decision_summary_first: true
    avoid_machine_state_blobs: true

  indexing:
    enabled: false
    adapters: []
```

A project that wants ADRs and plans but not feature specs can express that
directly:

```yaml
documentation:
  enabled: true
  capabilities:
    - summaries
    - plans
    - adrs

  paths:
    plans: docs/plans
    adrs: docs/adr
    summaries: .orcho/summaries

  behavior:
    require_human_review_for:
      - adrs
```

Full ablation disables only documentation maintenance:

```yaml
documentation:
  enabled: false
```

Even when `documentation.enabled` is `false`, Orcho still writes mandatory
protocol artifacts: events, checkpoints, execution state, evidence, gate
results, and metrics. The flag only prevents promotion into target-project
documentation.

This supports ablation: documentation maintenance can be disabled as a module
without weakening the core protocol. When the module is off, Orcho still keeps
the artifacts required to run, resume, inspect, and explain work. What it does
not do is promote run knowledge into the target repository's documentation.

Capabilities should be explicit so users can compare outcomes with and without
specific documentation behaviors. The default for serious project delivery
should favor documentation, but the architecture must allow clean removal of the
documentation layer for experiments, narrow automation, or projects that manage
docs through a different system.

## Execution Model

Documentation strategy should not be implemented as a single global system
prompt injection. Prompting can remind agents about documentation policy, but
the source of truth is typed configuration plus an explicit documentation
workflow.

The execution model has four layers:

1. **Configuration:** `documentation.enabled`, capabilities, paths, style, and
   review requirements are resolved before the run starts.
2. **Prompt composition:** relevant phases receive structured documentation
   context and policy blocks through the prompt composer.
3. **Protocol evidence:** the runtime records mandatory events, state, gates,
   evidence, and metrics regardless of the documentation module.
4. **Doc sync:** after a phase or run, a documentation step decides which
   markdown files should be updated according to enabled capabilities.

This keeps control in the orchestrator. Agents can propose documentation
updates, but the documentation module decides whether those updates are allowed,
where they belong, and whether human review is required.

## Prompt Integration

Prompt integration should use composable prompt parts, not a hidden global
system prompt. Relevant prompt parts may include:

| Prompt part | Purpose |
|-------------|---------|
| `documentation_policy` | Enabled capabilities, paths, style rules, and review requirements. |
| `documentation_context` | Small selected excerpts from feature specs, plans, docs, ADRs, or knowledge pages. |
| `documentation_update_contract` | How an agent reports proposed doc changes or doc gaps. |

These parts should be injected only into phases that need them:

- planning phases read feature specs, plans, ADRs, and knowledge pages;
- build/fix phases may collect doc-update candidates but should not rewrite
  broad docs opportunistically;
- review phases check whether changed behavior requires documentation updates;
- doc-sync phases produce narrow markdown patches.

Machine-critical protocol contracts still belong to non-overridable system-tail
blocks. Documentation policy is lower risk than parser contracts, but it should
still be rendered from typed config so project prompts cannot accidentally erase
documentation requirements.

## Default Documentation Prompt Parts

Orcho should ship default prompt parts for documentation maintenance. Projects
may override the wording, but the default behavior should be good enough for a
new repository.

Default prompt parts:

| Part | Used by | Responsibility |
|------|---------|----------------|
| `documentation_policy` | plan, build, review, doc-sync | Explain enabled capabilities, configured paths, style rules, and human-review requirements. |
| `documentation_context` | plan, build, review, doc-sync | Provide compact excerpts from relevant feature specs, plans, ADRs, reference docs, and knowledge pages. |
| `documentation_impact_analysis` | review, doc-sync | Ask whether the run changed behavior, contracts, decisions, plans, or reusable project knowledge. |
| `documentation_patch_contract` | doc-sync | Require narrow markdown edits, stable headings, related links, and no machine-state blobs. |
| `documentation_skip_reason` | doc-sync | Require an explicit reason when an enabled capability receives no update. |

The default prompt content should make the documentation role explicit:

```text
You are maintaining the target project's documentation strategy.
Use protocol evidence as source material, but do not treat protocol evidence
as project documentation. Update only the documentation layers enabled by the
configured capabilities. Prefer narrow markdown patches. Keep docs
agent-friendly, human-readable, and Obsidian-friendly. If an important fact
does not belong in the enabled documentation capabilities, report it as a
skipped documentation candidate with a reason.
```

These prompt parts are defaults, not the authority. The authority remains the
typed documentation config and the doc-sync workflow.

## Override Boundary

End users may customize documentation wording through markdown prompt overrides,
project instructions, and local style guides. Those overrides are useful for
tone, examples, naming conventions, and repository-specific documentation
layout.

They must not be able to break documentation-module integrity.

Non-overridable documentation contracts include:

- mandatory protocol artifacts remain enabled;
- `documentation.enabled` and capability resolution come from typed config;
- disabled capabilities cannot be updated by prompt wording alone;
- configured paths and review requirements are enforced by the doc-sync module;
- machine-state blobs are not written into human documentation;
- doc-sync emits protocol events for applied, skipped, blocked, and failed
  documentation actions;
- parser/output contracts needed by Orcho remain in system-tail blocks, not in
  user-editable markdown.

User-editable markdown can ask for a preferred voice or structure, but it cannot
override the module's safety rails. If a project override conflicts with typed
documentation config, the typed config wins and the conflict should be recorded
as a skipped or blocked documentation action.

## Doc Sync Workflow

A documentation sync step should follow a deterministic flow:

```text
protocol evidence
  -> doc-impact analysis
  -> capability/path placement
  -> narrow markdown patch
  -> optional human review
  -> event recording
```

Doc-impact analysis answers:

- Did user-visible or domain behavior change?
- Did a stable API, schema, or wire format change?
- Was an architectural decision made?
- Did the run complete or supersede a delivery plan item?
- Did the run reveal a repeated pitfall, glossary term, or open question?

Capability/path placement maps each candidate to the configured documentation
capabilities. If `features` is disabled, feature spec updates are skipped even
when behavior changed. If `adrs` is enabled but `features` is disabled, ADR
updates may still proceed.

Doc sync must emit its own protocol events so later evidence can explain which
documentation updates were proposed, applied, skipped, or blocked.

## Obsidian-Friendly Knowledge Structure

When Orcho creates or updates markdown documentation, the output should remain
plain portable markdown while also working well as an Obsidian vault.

Obsidian-friendly docs prefer:

- relative links between markdown pages;
- stable filenames that work as durable note identities;
- index or map-of-content pages for major areas such as features, ADRs, plans,
  and knowledge;
- backlinks or "related docs" sections when a decision spans multiple pages;
- short note-sized knowledge pages instead of one large catch-all memory file;
- image and artifact links that remain valid from the repository checkout;
- optional tags or frontmatter only when the target project already uses them.

Orcho should not require Obsidian-specific syntax as the only navigation
mechanism. Standard markdown links are the baseline. Obsidian conventions are a
compatibility target that makes the documentation pleasant to browse, not a
hard dependency.

## Completion Bar

For project delivery, large-scope work is complete only when the relevant
documentation layer has been updated:

- new or changed product behavior: feature spec updated;
- schema or wire-format change: reference docs updated;
- protocol-level decision: ADR written or superseded;
- new extension point: authoring guide updated;
- new lifecycle behavior: architecture doc updated;
- milestone implementation: plan marked complete, superseded, or linked to
  follow-up work;
- repeated lesson or pitfall: knowledge page updated.

Full guides may lag while a surface stabilizes, but the next phase must have
enough reference material to proceed safely.

This completion bar is about the target project. Orcho may use the same rule
for its own codebase, but that is dogfooding, not the core definition.

## Indexing Adapters

Indexing adapters are a later layer over the documentation strategy. They may
embed, chunk, rank, or search:

- protocol evidence;
- feature specs;
- project docs;
- project knowledge;
- delivery plans;
- ADRs.

Adapters must not become the only place where an important decision exists.
Their job is recall, not authorship. If an adapter finds a fact that matters
for future development, the fact should be promoted into the appropriate
markdown layer.

## Naming And Freshness

Prefer dated filenames for plans and research:

```text
docs/plans/2026-05-10-project-documentation-strategy.md
docs/research/2026-05-10-memory-systems.md
```

Prefer stable concept names for features, architecture, guides, and references:

```text
docs/features/trailing-stop.md
docs/architecture/project_documentation_strategy.md
docs/guides/profile_authoring.md
docs/reference/profile_schema.md
```

Use append-only ADRs for architectural history. Supersede an ADR with a new
one rather than editing accepted history.

If the target project already has naming conventions, Orcho should follow
them. The default structure exists to make new projects coherent, not to
overwrite established documentation practice.

## Anti-Patterns

- Treating `events.jsonl` or protocol evidence as project documentation.
- Treating a chat transcript as the source of truth.
- Creating a new plan when an existing plan only needs a status update.
- Hiding stable decisions in run summaries.
- Rebuilding docs from memory when a canonical page already exists.
- Making an index adapter responsible for facts that should be in markdown.
- Confusing Orcho's own documentation layout with the documentation strategy
  of the project Orcho is developing.
- Letting documentation capabilities disable artifacts required by the Orcho
  protocol.

## See Also

- [Architecture overview](overview.md)
- [Phase lifecycle](phase_lifecycle.md)
