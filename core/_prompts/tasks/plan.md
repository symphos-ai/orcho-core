Produce an implementation plan for the task before any code lands:

## Verification ownership

The engine runs the project's declared full or broad suite as a required
post-implement gate. Do not create an implement subtask for that suite:
implement tasks name only targeted tests for their concrete change. Record the
full or broad suite as project gate-policy, not as an implement action. For a
checkable criterion, prefer a grep invariant or a behavioral assertion.

1. State what "done" means — observable behavior, files in scope,
   constraints. Derive acceptance criteria if the task omits them.
2. Inspect relevant code, callers/callees, and tests before planning.
3. Identify load-bearing files, interfaces, persisted shapes, and
   invariants the change must preserve.
4. Plan the full requested milestone, then choose the smallest coherent
   staged path through it. Keep any required refactor tightly scoped and
   justified. Name out-of-scope files and modules.
5. Make acceptance criteria checkable (behavior, test, command
   output, or contract state); avoid vague "works correctly".
6. Surface concrete assumptions, risks, and mitigations.
7. If already done, make this a verification plan with scoped evidence and
   targeted checkpoint commands; do not turn the full or broad suite into an
   implement task.
8. For milestone-sized work, group tasks into execution slices. Each slice
   should leave the repo in a coherent state, name only its targeted checkpoint
   commands, and say what must be true before moving to the next slice. Keep
   the full or broad suite as project gate-policy rather than a slice task.
9. Mark externally blocked or optional evidence as deferred instead of making
   it block the core delivery path. Name stop conditions when later slices
   should not run.
10. If the task is large enough for parallel work, make sub-agent boundaries,
   file ownership, dependencies, expected outputs, and integration points
   explicit.
