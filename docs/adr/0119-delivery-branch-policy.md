# ADR 0119 — Delivery branch policy

Status: Proposed

## Context

`commit_delivery` applies the accepted diff back to the target repository and
runs `git commit` on whatever branch the target's `HEAD` currently points at
(`pipeline/engine/commit_delivery.py::apply_commit_delivery`). It has no concept
of a *delivery target branch*. When the canonical checkout sits on its default
branch, an `approve` (auto-commit) delivery lands **directly on `main`**.

This surfaced concretely on 2026-07-02: a non-interactive run
(`20260702_184512_39b150`) delivered with `action=approve` and committed
straight onto the canonical `orcho-core` `main`, which was the checked-out
branch. The manual cycle used to mask this — the operator either committed to
main deliberately or hand-made a branch, merged, and promoted. Once delivery
became automatic, "current HEAD happens to be main" turned into a silent commit
on main.

The failure mode compounds with two adjacent gaps:

* **Bootstrap hazard.** When Orcho develops Orcho, a bad delivery on `main` gets
  promoted into the stable runner via `orcho-promote` — a broken runner. The
  promote chain must only ever consume a reviewed, merged `main`.
* **Structured-delivery thesis.** Orcho's job is to hand a human a reviewable
  unit — a **pull request**, not a commit already on `main`.

The delivery already threads the isolation shape it needs: `source_worktree` and
`project_path` are separate parameters, and a run is *in-place* (isolation off)
exactly when `source_worktree == project_dir` — the same discriminator
`pipeline/participants.py` uses to stamp `isolation = "off" | "per_run"`. So the
policy can be resolved from facts the engine already has, without new plumbing.

## Decision

### Invariant

**Delivery never auto-commits onto the repository's default branch.** This holds
in both isolation shapes and is the property the default enforces. `bypass`
(below) is the single, explicit opt-out.

The default branch is resolved from `refs/remotes/origin/HEAD` when available,
falling back to `main` then `master`; a configured `named` target overrides
detection.

### `branch_policy` modes

A new `commit.branch_policy` setting (sibling to `commit.default_strategy`),
one of:

* **`worktree_branch`** *(default)* — publish the run's own worktree branch as
  the delivery. Requires an isolated (`per_run`) run: the branch
  `orcho/run/<id>` already exists in the shared object store, based on
  `base_ref`. Delivery rebases it onto the freshly-fetched
  `origin/<default>`, renames/publishes it as `orcho/deliver/<run_id>-<slug>`,
  and emits a `delivery_pr_intent`. **The canonical checkout is never touched**
  — its `HEAD` and working tree stay exactly where the operator left them.
* **`protect_default`** — create `orcho/deliver/<run_id>-<slug>` off `base_ref`
  in the target checkout, switch to it, and commit there. Used for in-place runs
  whose `HEAD` is the default branch, and available as an explicit choice for
  operators who want the delivery branch checked out in the canonical repo.
* **`named`** — the operator supplies the target branch; delivery commits there
  (creating it off `base_ref` if absent).
* **`bypass`** — today's behavior: commit onto the target's current `HEAD`,
  **including the default branch**. The explicit escape hatch; never the
  default.

### Resolution table (`branch_policy` × isolation)

`worktree_branch` is *conditional on* isolation — it has no run branch to
publish when the run executed in place. The effective behavior is:

| `branch_policy`   | `per_run` (worktree)                    | `off` (in-place)                                              |
| ----------------- | --------------------------------------- | ------------------------------------------------------------ |
| `worktree_branch` | publish run branch (rebased) + PR-intent | **degrade → `protect_default`** (no run branch to publish)   |
| `protect_default` | (same as `worktree_branch` per_run)†     | HEAD=default → new `orcho/deliver/…`; HEAD=feature → commit onto that branch |
| `named`           | commit to named branch                  | commit to named branch                                        |
| `bypass`          | commit to run branch HEAD               | commit to current HEAD (incl. default)                        |

† For a `per_run` run there is no default-branch checkout to protect, so
`protect_default` and `worktree_branch` converge on publishing the run branch.

**In-place on a non-default branch commits onto that branch.** The operator ran
in place on their own feature branch deliberately; the policy guards the
*default* branch specifically, it does not force a fresh branch for every
in-place delivery.

### Rebase discipline

`worktree_branch` fetches and rebases the delivery branch onto the current
`origin/<default>` before publishing, so the PR range contains only the run's
commit (this also avoids the "stale-base sweeps unsigned ancestor commits into
the DCO range" failure seen the same day). A rebase **conflict is not fatal**:
publish the branch un-rebased and attach a warning naming the conflict for the
operator to resolve. Offline / no configured remote degrades to leaving the
delivery branch in local refs with a "push when a remote is available" notice.

### Core owns the protocol; a git-provider plugin owns push + PR

Core produces the provider-neutral artifacts only:

* the published/publishable delivery branch, and
* a durable **`delivery_pr_intent`** record: `branch`, `base`, `title`
  (from the release summary), `body` pointer, and a suggested command.

The actual `git push` and pull-request creation belong to a **git-provider
plugin** (GitHub/GitLab/…), keyed off the existing runtime/provider extension
philosophy — core names no `gh`/`glab` binary and encodes no provider API. A run
with no provider plugin still gets a correct local delivery branch and the
`delivery_pr_intent`; opening the PR is then a manual or plugin step.

### MCP / SDK alignment (in scope — not deferred)

Delivery is an MCP-visible gate primitive: the `delivery` evidence slice,
`orcho_delivery_gate`, and the SDK `DeliveryGateProjection` /
`DeliverySummaryRecord` already surface the delivery outcome to captain clients.
This change alters that outcome shape — a delivery can now resolve to a
**published branch + `delivery_pr_intent`** rather than a `commit_sha` on the
current branch — so per the orcho-core MCP Validation rule the MCP surface ships
**with** this work, not at the end of the milestone.

Scope of the paired alignment:

* **orcho-core SDK projection.** The delivery projection gains the additive
  facts: `delivery_branch` (the published/publishable branch) and a typed
  `pr_intent` sub-record (`branch`, `base`, `title`, `suggested_command`).
  `commit_sha` stays populated for `protect_default` / `named` / `bypass`
  commits and is absent for a `worktree_branch` publish that only pushed a
  branch. `docs/sdk_schema.json` is regenerated in the same change.
* **orcho-mcp companion (separate repo, coordinated — no cross-repo commit).**
  The `delivery` evidence slice and the delivery-gate projection surface
  `delivery_branch` + `pr_intent`; `docs/mcp_schema.json` is regenerated; and an
  **E2E mock-pipeline smoke** exercises at least one `branch_policy` path
  (`orcho_run_start(mode=…, mock=true)` → `orcho_run_evidence(slice="delivery")`
  asserts the branch/PR-intent shape). Because a run targets one repo, this is a
  paired orcho-mcp change delivered alongside — the milestone is not "done"
  until it lands.

The additive fields follow the ADR 0110 owned-files discipline: the SDK
projection dataclass/schema files are declared up front so the delivery run does
not trip the scope-expansion gate on a new public `sdk/*` surface.

### Interaction with the action default (ADR 0032 / delivery pipeline)

`branch_policy` only takes effect when delivery actually commits (`approve`). The
interactive `apply` default drafts the diff **uncommitted** into the target
working tree for the human to place themselves — no branch decision is made, and
the invariant is satisfied trivially (nothing is committed). The branch policy
therefore governs `approve` (the CI/non-interactive default and the explicit
interactive approve).

## Consequences

* A non-interactive `approve` run can no longer silently land on `main`: it
  yields a rebased `orcho/deliver/…` branch plus a `delivery_pr_intent`, and the
  canonical checkout is left clean on its current branch.
* The promote chain (`orcho-promote`) only ever sees a reviewed, merged `main`,
  closing the dogfood bootstrap hazard.
* `worktree_branch` never mutates the canonical checkout, removing the collision
  between an Orcho delivery and concurrent human work in the same checkout.
* In-place feature-branch workflows are unchanged; only the default branch is
  protected. `bypass` preserves the exact prior behavior for anyone who wants
  it, opt-in.
* `commit.branch_policy` is a delivery config key, not itself a wire/runtime
  schema change. But `delivery_branch` + `pr_intent` extend the MCP-visible
  delivery projection, so the SDK shape, `orcho-mcp` delivery slice/gate, the
  regenerated `sdk_schema.json` / `mcp_schema.json`, and an orcho-mcp E2E mock
  smoke ship **with** this work (see "MCP / SDK alignment"), not deferred.
* Per "No Backcompat Ceremony", there is no legacy dual-path: the single commit
  site resolves the policy; `bypass` reproduces the old behavior when explicitly
  selected rather than as a parallel code path.
