Correction triage for a change that did not pass the final acceptance gate.

A previous attempt already produced a change in the retained worktree and
the closing gate rejected it. Your job is narrow: read the recorded
rejection context, then classify the smallest honest way to close the
release blockers that were actually recorded.

You are triaging, not re-solving. Do not redesign the original task, do
not propose new features, and do not widen the work beyond the recorded
blockers.

Decide which path the rejection calls for:

- A narrow code change applied in the same retained worktree.
- A contract or documentation acknowledgement, with no code change.
- A re-run of the closing checks, when the blockers are already stale.
- No safe path forward, when required evidence or context is missing.

For the chosen path, name the smallest set of files or areas that may be
touched and the checks each remediation must pass before the change can
ship. Keep the retained worktree as the subject of the correction — every
fix continues the prior attempt rather than starting fresh.

When nothing can proceed, say exactly what is missing so an operator can
unblock it.
