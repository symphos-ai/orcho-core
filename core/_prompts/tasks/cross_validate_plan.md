Review the cross-project implementation plan. The task description,
the supplied aliases, and the artifact bundle to review are supplied
separately by the runner.

This is a CROSS-PROJECT PLAN (no code yet) describing one
coordinated change spanning multiple codebases. Reject only on
substantive defects:

1. **Missing shared surface.** A plan that decomposes into
   independent per-repo tasks without naming the inter-project
   surface (wire contract, persisted shape, API path) fails the
   cross-project bar.
2. **Missing alias coverage.** Each supplied alias MUST receive a
   non-empty subtask. Empty or absent subtasks are rejection-grade.
3. **Producer/consumer drift.** For every field, type, or message
   in the Interface Contract, the producer-side and consumer-side
   projects must agree on name, type, and required/optional.
4. **Ignored persistence.** When the task implies storing or
   retrieving data, the plan must address persistence (schema,
   migration, model) — not only the wire shape.
5. **Wrong implementation order.** Producer-schema changes land
   before consumer parsers; migrations land before code reading the
   new columns. Flag plans that ship consumer changes ahead of the
   producer contract.
6. **Missing falsifiers.** The riskiest assumption usually isn't
   "producer/consumer might drift" — it's "these are the only
   places that need to know". Reject plans that don't name what
   evidence would prove the scope wrong.

Do not nitpick style. If rejecting, name the alias and section,
state the defect, and say what would make it acceptable.
