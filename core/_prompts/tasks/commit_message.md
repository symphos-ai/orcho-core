# Compose a commit message for the changes the run produced

You are summarising a finished run as a single commit message.

You will see:

- the release-gate summary that explains what the run accomplished,
- the diff between the project's pre-run state and now,
- the list of files this run touched.

Write a concise commit message that captures the *why* of the change,
not a restatement of the diff. The reader is a future maintainer reading
`git log`, not someone watching the run finish.

Guidelines for the content (the output shape itself is fixed by the
system contract appended to this prompt):

- Subject: imperative mood, one line, no trailing period.
- Body: motivation, scope, anything non-obvious from the diff. Skip the
  body when the subject is fully self-explanatory.
- Mark a change as breaking only when an external contract changes —
  callers will have to update their code or configuration.
- Pick the type that best fits the change's intent. When in doubt
  between `feat` and `refactor`, prefer `refactor` for internal
  reshuffles that do not change observable behaviour.
- Pick a scope only when it makes the subject clearer; an empty scope
  is fine.

Pre-existing dirty files that were NOT staged by the operator are not
your concern — describe only the changes inside this run.
