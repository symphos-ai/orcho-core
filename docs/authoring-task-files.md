# Writing task files

Task files turn a requested change into a bounded, reviewable unit of work.
Describe the intended behavior, the files likely to be involved, the evidence
that demonstrates the change, and what is deliberately outside the request.
Keep the task specific enough to implement and verify without inventing a
second, broader change.

## Verification is the engine's job

An implement task runs only targeted tests that exercise its concrete change.
The repository's full or broad suite is a project gate-policy: it is scheduled
and recorded by the engine after implementation, rather than being made an
implement subtask. This division keeps implementation evidence relevant and
lets the required gate retain one clear owner.

State the targeted test paths or commands that establish the local behavior.
If a repository required gate matters, name it as a requirement of the project
without adding it to the implementation commands.

## Expensive verification anti-patterns

Avoid task instructions that make verification costly, ambiguous, or hard to
attribute:

- Do not make a full test suite or broad suite an implementation subtask.
- Do not poll a background `pytest` process with `until`, `grep`, or repeated
  checks for an exit marker; run the bounded command directly when it is part
  of the task's targeted evidence.
- Do not duplicate the repository gate contract in each task file. The task
  should describe its local evidence, while the project owns its required gate.
- Do not accept unbounded claims such as “nothing anywhere broke.” Replace
  them with observable, scoped behavior or structure.

## A good acceptance example

For a change that adds validation to a public request field, an acceptance
section could say:

> - Invalid values return the documented validation error; valid values retain
>   the existing response shape.
> - The validator is called at the request boundary, and the public schema
>   remains unchanged except for the documented constraint.
> - Run `python -m pytest -q tests/unit/test_request_validation.py` and
>   `python -m pytest -q tests/unit/test_public_schema.py` as targeted
>   evidence.
> - The repository required gate runs after implementation under the project's
>   gate-policy.
> - Out of scope: new request fields, transport changes, and unrelated schema
>   cleanup.

This example gives reviewers structural and behavioral invariants, names the
small test surface that proves them, and leaves repository-wide verification to
the required gate.

## Declare anticipated files up front

List the files you expect to create or edit before implementation begins. This
makes scope visible and gives reviewers a chance to spot an accidental change
of surface area. Be especially explicit about public contracts, wire formats,
schemas, and generated references: an unanticipated change in one of these
areas can expand the task's scope and require additional compatibility and
verification work.

If investigation identifies a necessary file that was not anticipated, record
why it is needed and update the task's scope before treating it as part of the
change.

## Placement

Store project task files under `.orcho/.task-files/`. Keep them close to the
project they describe, use a descriptive filename, and make each file a
single bounded request with clear acceptance evidence.
