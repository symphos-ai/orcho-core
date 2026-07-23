# ADR 0121 - Git-provider delivery publishing

Status: Proposed

## Context

ADR 0119 stops one step short of a shipped change. A non-interactive `approve`
run under the default `worktree_branch` policy now rebases the run's branch onto
a fresh `origin/<default>`, publishes it as `orcho/deliver/<run_id>-<slug>` in
local refs, and records a provider-neutral `delivery_pr_intent` (`branch`,
`base`, `title`, suggested `git` command). But it never pushes that branch and
never opens a pull request: core deliberately shells no `gh`/`glab` binary and
encodes no provider API. The operator is still left to run the suggested `git
push` and open the pull request by hand.

ADR 0119 already named the missing piece — "the actual `git push` and
pull-request creation belong to a **git-provider plugin**" — and deferred it.
This ADR is that plugin boundary plus the single core seam that drives it. It
must satisfy three constraints the surrounding design already fixed:

* **Provider-neutral core.** Per the workspace rule that core owns the protocol
  and plugins own provider behavior, core owns the contract, registry, and gate;
  a plugin owns binary detection, auth, push, and pull-request creation. Core
  must name no provider binary.
* **No new wire slice.** ADR 0119 already shipped the MCP-visible delivery
  projection (`delivery_branch` + typed `pr_intent`). Adding a pull-request URL
  must not force another paired `orcho-mcp` schema slice for this step.
* **DCO-safe.** ADR 0119 signs the delivery commit (`git commit -s`) inside the
  run worktree. Publishing must open a pull request *over that existing signed
  commit*; it must never create a new or unsigned commit.

## Decision

### `PublishResult` — the typed publish outcome

`pipeline/engine/delivery_publish.py::PublishResult` is a frozen in-process
value: `pushed` (did the branch reach the remote), `pr_url` (the opened pull
request, if any), and `warnings` (non-fatal diagnostics). It is intentionally
**not** a wire/SDK type. An opened `pr_url` travels as a string folded into the
delivery decision's existing `delivery_notices`, and any warning into
`delivery_warnings` — both already persisted and already in
`CommitDeliveryDecision.to_dict()` since ADR 0119. No new persisted field, no
new MCP slice.

### `DeliveryPublisher` protocol + `orcho.delivery_providers` group

`DeliveryPublisher` is the extension protocol a git-provider plugin implements:

```python
def publish(self, pr_intent, *, branch, cwd, remote) -> PublishResult: ...
```

Plugins register under the new `orcho.delivery_providers` entry-point group,
resolved through the shared `pipeline.entry_points` discovery helper so a
delivery provider follows the same author contract as every other Orcho
extension point (`orcho.agent_runtimes`, `orcho.phases`, `orcho.skills`). Any
embedder can register one; the built-in provider is registered the same way and
holds no privileged path.

### `publish_delivery` — the single publish seam

`publish_delivery(...)` is the one orchestration entry point, called from the
single delivery site `commit_delivery._deliver_published_branch` right after
`publish_delivery_branch` has produced the local delivery branch. It:

* reads the `commit.publish` config gate. `off` returns
  `PublishResult(pushed=False)` **without resolving or invoking any provider and
  without any shell call** — the published local branch is the deliverable, the
  ADR 0119 behavior;
* on `auto` (default) resolves a provider from `orcho.delivery_providers` by
  `commit.publish_provider` name or as the sole registration. An empty registry
  degrades silently; a missing named provider or an ambiguous choice degrades
  with a warning;
* invokes `provider.publish(...)` inside `try/except`. **Every** provider
  exception, and an invalid return, becomes a warning; it never re-raises.

Core here never names `gh`/`glab` and never shells a provider binary. The commit
site folds an opened `pr_url` into `delivery_notices` (else a "branch ready"
notice) and warnings into `delivery_warnings`; the delivery status stays
`committed` regardless of the publish outcome.

### Built-in GitHub provider — the single home for `gh` / `git push`

`pipeline/engine/delivery_providers/github.py::GitHubDeliveryProvider` is the
only module that shells a git provider. Its `publish` flow — all shell-outs
confined to this module — is: detect `gh` → `gh auth status` → `git push -u
<remote> <branch>` from the run worktree → `gh pr create` over the existing
signed commit, extracting `pr_url` from stdout. It **creates no commits**: the
signed delivery commit was produced upstream (ADR 0119) and this provider only
publishes it. Every failure mode (`gh` missing, auth failure, push failure,
`gh pr create` failure) maps to a `PublishResult` warning; `publish` never
raises. A successful push whose pull-request creation fails returns
`pushed=True, pr_url=None` with a warning, so a branch that reached the remote is
never lost. The subprocess runner is injectable so tests drive the provider
against a fake `gh` with no real network.

### `commit.publish` gate + degradation

A new `commit.publish` config key (default `auto`, with `off` as the explicit
opt-out) sits beside the ADR 0119 `commit.branch_policy`, plus
`commit.publish_provider` to disambiguate a multi-provider registry. A disabled
gate, a missing provider, or a provider failure all **degrade rather than
error**: the run keeps its correct local delivery branch and its
`delivery_pr_intent`, records a "branch ready" notice or a warning, and stays
`committed`. Publishing is best-effort transport layered over an already-durable
local deliverable — it can never fail the run.

## Out of scope

* **Non-GitHub providers.** GitLab/Bitbucket/etc. are future
  `orcho.delivery_providers` registrations against the same protocol; only the
  GitHub provider ships here.
* **A typed `pr_url` field in the MCP delivery slice.** `pr_url` rides inside
  the existing `delivery_notices` string list; promoting it to a typed
  projection field (and the paired `orcho-mcp` schema slice) is deferred until a
  captain client needs it structurally.
* **Auto-merge / PR lifecycle.** This step opens a pull request; it does not
  merge, label, assign, or otherwise manage it.
* **Changing the ADR 0119 policy table.** `branch_policy` resolution,
  default-branch detection, and the isolation × policy table are unchanged;
  this ADR only adds the publish step after a `worktree_branch` publish.

## Consequences

* A non-interactive `approve` `worktree_branch` run now closes the loop: it
  pushes `orcho/deliver/<run_id>-<slug>` to the remote and opens a pull request
  over the signed commit, with the pull-request URL surfaced through
  `delivery_notices`. The canonical checkout is still never touched.
* The provider boundary holds: `gh` / `git push` execution lives only in the
  registered provider package; a boundary guard test scans every
  `pipeline/engine/**` module (except the provider) to keep it that way.
* Publishing degrades safely. With the gate `off`, no provider installed, or a
  provider failure, the run still delivers a local branch + PR-intent and stays
  `committed` — the publish step can never break delivery.
* No new wire slice ships for the pull-request URL; per "No Backcompat
  Ceremony" there is no dual path — the single publish seam reads the gate and
  either publishes or degrades.
