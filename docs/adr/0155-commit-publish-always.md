# ADR 0155 — Commit publish `always`

Status: Accepted

Related: [ADR 0119](0119-delivery-branch-policy.md) and
[ADR 0121](0121-git-provider-delivery-publishing.md)

## Context

ADR 0119 defines the delivery-branch policy and protects the repository default
branch. ADR 0121 adds the provider-neutral publish seam for an isolated
delivery branch. A `commit_on_branch` delivery, however, already has a signed
commit in the target checkout and an existing `DeliveryPrIntent`; operators
need an explicit way to ask for publication of that dedicated delivery branch
without changing the ordinary `auto` path or weakening the default-branch
invariant.

## Decision

`commit.publish` is normalized in one place to exactly `off`, `auto`, or
`always`; its default is `auto`. Values that are missing, blank, unknown, or
otherwise invalid normalize to `auto`.

* `off` short-circuits provider discovery and invocation.
* `auto` retains its existing behavior. In particular, it does not add a
  provider call to a `commit_on_branch` delivery.
* `always` enables an additional post-commit publish attempt only when the
  resolved delivery plan is `commit_on_branch` and its signed commit succeeded.

For that `always` case, core passes the already-created `DeliveryPrIntent` and
the actual `delivery_branch` to the existing `publish_delivery` seam. The
provider runs with `cwd=project_path`, the target checkout containing the
committed branch. The resulting PR URL, notices, and warnings are folded into
the one durable committed decision together with the checkout's real
`commit_sha`; the commit SHA is not replaced by a published-branch SHA.

Publication is best effort. A missing provider, provider exception, discovery
problem, or invalid provider result never rolls back the commit or changes the
delivery status to an error. The durable result remains `committed`, records
available warnings, and uses a truthful "branch ready" notice when no PR URL
confirms publication.

## Invariants

`always` means **publish an existing dedicated delivery branch after its
commit**, not “push the current branch.” `commit_in_place` is never handed to a
provider for any gate value. In particular, `always` never authorizes a push
of the repository's default branch, and it does not alter ADR 0119's
`branch_policy × isolation` resolution table.

The isolated `publish` plan retains ADR 0121 mechanics: `off` and `auto` do
not change, and `always` does not alter how that plan creates its local delivery
branch. All provider invocation remains behind `publish_delivery`; this ADR
does not add provider binaries, authentication, retries, or push logic to core.

## Consequences

Protected-default workflows can opt in to a best-effort PR publication after a
successful checkout commit while retaining a durable local branch and commit if
publication is unavailable. Operators can distinguish a confirmed PR URL from
a merely ready local branch rather than inferring that a push happened.

This reuses existing durable decision fields and projections. It creates no new
SDK or MCP wire field and makes no promise about any provider beyond the
existing `DeliveryPublisher` protocol.

## Out of scope

* Publishing `commit_in_place`, the current branch, or a default branch.
* Changing ADR 0119 or ADR 0121; both ADRs remain append-only.
* New provider capabilities, provider-specific configuration, auto-merge, or
  pull-request lifecycle management.
* A new SDK/MCP schema or a new public delivery payload field.
