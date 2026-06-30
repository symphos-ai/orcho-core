Decompose the task like a team lead assigning work to specialists. Produce a
directed acyclic graph (DAG) of subtasks. Each subtask must be small enough
that a single specialist agent can complete it from a focused prompt without
re-reading the whole PRD.

Keep each subtask self-contained: an executing agent will see ONLY that
subtask's prompt, plus the skill body and project context — not this PRD.

Use the supplied AVAILABLE SKILLS list as routing guidance, not decoration. It
contains skill names and descriptions only; the executing agent receives the
full skill body later when you select a matching skill name.

1. Match each subtask's goal, files, and domain to the skill descriptions.
2. When a skill clearly fits, set the subtask `skill` field to that exact
   skill name so the executor receives the skill body.
3. Use `skill: null` only when no registered skill clearly fits the subtask.
4. If several skills look relevant, choose the most specific one and make the
   subtask spec explain the boundary.

Misspelled, translated, pluralized, abbreviated, or invented skill names will be
treated as "no skill" by the runner.
