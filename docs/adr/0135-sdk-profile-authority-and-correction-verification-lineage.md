# ADR 0135 — SDK profile authority and correction verification lineage

## Status

Accepted.

## Context

Detached SDK launches already provide an explicit profile argument. Exporting
that selection through `ORCHO_PIPELINE` as well created two competing profile
surfaces and leaked the orchestration selector into project verification
subprocesses. Separately, a correction child can be observable before its own
session persists the reused worktree block, even though its durable parent
lineage already identifies the retained verification subject.

## Decision

Detached SDK launches select profiles only through their explicit argv. They
remove an inherited `ORCHO_PIPELINE` value from the child environment while
preserving the environment variable as a direct-CLI experiment surface.

Verification of a correction follow-up with no local worktree block resolves
the nearest retained identity through its durable parent lineage. The lineage
must remain within the same project, must be acyclic, and must resolve to
readable recorded worktree metadata. Invalid or incomplete lineage fails before
any command, assertion, or receipt write. The canonical project remains the
contract source and is never substituted as the physical subject.

## Consequences

Embedder-selected profiles cannot be displaced by ambient process state, and
project commands no longer receive an orchestration-only profile override.
Early verification of correction children proves the retained change rather
than a clean canonical checkout. Direct CLI profile experiments remain
unchanged.
