# ADR 0140 — Verification proof follows content through an authorized delivery transition

- **Status:** Accepted
- **Date:** 2026-07-18
- **Supersedes:** ADR 0084 dependency freshness rule where it relies only on a
  dependency HEAD
- **Related:** ADR 0080, ADR 0089, ADR 0130, ADR 0132

## Context

Command receipts currently identify their subject with checkout HEAD, baseline
HEAD, and a digest of changed path names. A receipt becomes stale when the
current checkout HEAD or path digest differs from the recorded values.

That representation fails during normal delivery. A verified dirty worktree
can be committed without changing its resulting content: the commit advances
HEAD and makes the worktree clean, so both comparison values change even though
the command ran against the content being delivered. A successful delivery can
therefore be reported together with stale, blocking required receipts and an
instruction to rerun the same gates.

A path-name digest is also not a content identity. It cannot distinguish two
edits to the same path and omits file modes, symlink targets, and untracked-file
content.

## Decision

Verification proof is bound to an immutable content identity. It may cross a
Git representation change only through an engine-authored, validated delivery
transition. Receipts remain immutable observations; delivery records the edge
between subjects instead of rewriting a receipt.

### Content subject

Core defines one low-level `VerificationSubjectIdentity` value:

```text
VerificationSubjectIdentity
  version
  object_format
  tree_oid
  observed_head_oid
  baseline_oid
```

`tree_oid` is captured from the complete Git-visible checkout state using a
temporary index and `git write-tree`. It includes tracked modifications,
additions, deletions, renames, modes, symlinks, and non-ignored untracked files.
Capture must not mutate the real index, refs, or worktree. It is never-raising:
when identity cannot be established it returns a typed unavailable result.

The observed HEAD and baseline remain provenance, not equality substitutes.
Direct freshness requires equal content identity and unchanged observed HEAD.
An observed HEAD change carries proof only through the transition described
below. This keeps an unrelated empty commit or external ref movement distinct
from an engine-owned delivery.

A dirty submodule cannot be represented by its unchanged gitlink OID. If it is
an effective dependency of a verification command, it must receive its own
content identity; otherwise the subject is `unverifiable`.

### Receipt and dependency freshness

Command receipts record `VerificationSubjectIdentity` as their authoritative
subject. Changed-path summaries may remain diagnostic data, but they are not
used to establish freshness.

Effective dependencies (`depends_on=true`) use the same identity contract.
Their content drift invalidates proof; unused dependencies remain
non-blocking. This replaces ADR 0084's HEAD-only dependency comparison.

Historical receipts without the new identity stay readable as observations but
cannot be carried through a delivery transition.

### Authorized delivery transition

Delivery persists an immutable `VerificationSubjectTransition`:

```text
VerificationSubjectTransition
  version
  kind: exact_commit | exact_apply
  source_subject
  destination_subject
  delivery_provenance
  validated_at
  carried_receipts[]
  disposition
```

A receipt is carried only when all of these conditions hold:

1. its recorded source tree equals the captured pre-delivery tree;
2. the destination tree equals that source tree;
3. every effective dependency identity is still current;
4. the active delivery operation authored the transition; and
5. the transition artifact is complete, internally consistent, and durable.

For a publish path, local commit-tree validation happens before push or pull
request creation. For apply, the destination worktree snapshot must equal the
verified source snapshot. A cross-project commit-set records and validates a
transition per project before aggregate delivery completes.

### Pre-delivery consequences and post-delivery disposition

Before delivery, missing, failed, stale, or unverifiable required proof blocks
mutation according to the effective verification policy.

`unverifiable` is a distinct classification: a receipt exists but the engine
cannot prove its subject is current. Any public typed union that exposes receipt
classification must add it in the coordinated companion change.

After delivery, finalization projects the durable transition disposition rather
than recomputing a pre-delivery requirement against the mutated checkout:

- `direct`
- `carried_exact_commit`
- `carried_exact_apply`
- `stale_content_drift`
- `stale_dependency_drift`
- `transition_unverifiable`

An exact carried transition must not leave a residual required blocker or a
verification remediation hint. A mismatch before publication blocks
publication. Drift first observed after an irreversible external side effect is
reported as a delivery-integrity incident, not as a fictional pre-delivery
blocker.

## Consequences

- Exact Orcho delivery preserves successful verification proof.
- Same-path edits, modes, symlinks, deletions, and untracked content participate
  in subject equality.
- Delivery gains a validation point before publication and durable lineage after
  validation.
- Receipt capture remains safe at the API boundary while policy fails closed on
  unavailable proof.
- CLI, SDK, evidence, and any MCP projection consume the core-owned transition
  disposition and do not independently inspect Git state.
- Temporary-index capture may leave unreachable Git objects; normal Git garbage
  collection reclaims them.

## Alternatives considered

### Suppress the final blocker without changing identity

Rejected. It leaves false stale classifications in evidence and SDK surfaces.

### Ignore HEAD when changed paths match

Rejected. Changed paths are not content identity and are empty after commit.

### Treat matching tree OIDs as sufficient for every HEAD movement

Rejected. It cannot distinguish engine-owned delivery from an unrelated ref
transition and provides no durable audit edge.

### Rewrite receipts after delivery

Rejected. A receipt is an immutable observation; rewriting it destroys
provenance.

### Rerun every gate after commit

Rejected. It duplicates successful verification and still leaves apply and
cross-project continuity undefined.

## Delivery plan

1. **I4-R1 — subject identity:** extract temporary-index capture, add typed
   subject comparison, and migrate receipt and effective-dependency provenance.
2. **I4-R2 — delivery transition:** persist and validate exact commit/apply
   transitions, including per-project cross delivery records.
3. **I4-R3 — projection:** consume durable disposition in readiness, DONE,
   evidence, and SDK; add an MCP companion only when the public typed wire
   changes.

## Acceptance criteria

1. A receipt for a dirty tree remains valid when Orcho commits exactly that
   tree.
2. A content change to an already-listed path invalidates the receipt.
3. Extra staged content or a changed destination tree prevents publication.
4. An external HEAD move without an exact transition does not carry proof.
5. Effective dependency content drift invalidates proof.
6. A dirty effective submodule is captured or yields `unverifiable`.
7. Publish validates a local commit before push or pull request creation.
8. Final acceptance and delivery share one pre-delivery classifier.
9. Post-delivery surfaces project one durable disposition.
