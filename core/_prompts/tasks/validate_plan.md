Review the implementation plan against the task.

This is a PLAN document (no code yet). Review whether executing it
would satisfy the task.

Reject only concrete defects:

- missing or unobservable acceptance criteria;
- wrong, missing, or over-broad file ownership;
- implementation order that violates real dependencies;
- missing risks, assumptions, or falsifiers for risky scope;
- speculative refactors or scope beyond the task;
- acceptance criteria that do not trace to the task: a criterion the task
  never asked for that widens scope (work in another repository, a human
  verdict the task did not request, an invariant over files the task did not
  name) is a defect of the plan — name it as one, do not treat it as
  diligence;
- steps that do not deliver the required behavior;
- milestone-sized work that is presented as a flat task list without coherent
  execution slices, checkpoint commands, deferred/externally blocked evidence,
  or stop conditions;
- non-trivial independent workstreams that should use sub-agents but
  lack delegation boundaries, file ownership, dependencies, or an
  integration point.

Do NOT nitpick style. Focus on correctness and completeness of the plan.
If rejecting, name the section or item, state the defect, and say what
would make it acceptable. Reference plan sections, task IDs, acceptance
criteria, owned files, or stated constraints; do not ask for code line
references or diff evidence because no implementation exists yet.
