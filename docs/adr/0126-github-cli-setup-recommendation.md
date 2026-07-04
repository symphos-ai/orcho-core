# ADR 0126 - GitHub CLI setup recommendation

Status: Proposed

## Context

ADR 0121 closed the delivery loop for a non-interactive `approve`
`worktree_branch` run: the built-in GitHub provider pushes
`orcho/deliver/<run_id>-<slug>` and opens a pull request over the signed commit
recorded by ADR 0119. When the `gh` CLI is missing or unauthenticated, that step
degrades gracefully — the run keeps its local delivery branch and its
`delivery_pr_intent`, records a warning, and stays `committed`. Publishing is
best-effort transport over an already-durable local deliverable.

Graceful degradation is correct, but two moments could do more than shrug and
carry on. Each is a point where Orcho already knows the operator is one small
setup step away from auto-push + a pull request, yet says nothing actionable:

* **At delivery time**, when `gh` is unavailable on a run whose remote is a
  GitHub remote, the generic "install the CLI and push manually" warning omits
  the one concrete next step that would enable the automated path.
* **At `orcho workspace init` time**, a freshly wired workspace whose projects
  are GitHub-hosted gives no hint that installing `gh` would unlock the
  publish step on future runs.

The tension is the same one ADR 0121 already fixed for publishing: `orcho-core`
and the CLI must stay provider-neutral (core owns the protocol, plugins own
provider behavior), while any GitHub-specific knowledge — how to recognize a
GitHub remote, how to phrase an install hint — lives only in the provider
package. A recommendation feature must not leak `gh`/`github` knowledge back
into core or the CLI, and must not turn a read-only nudge into an action that
installs or authenticates anything.

## Decision

Add an **optional** provider hook and a provider-neutral collector, then let the
GitHub provider fill it in and let two neutral surfaces surface it.

### `DeliveryPublisher.setup_hint` — an optional provider capability

`pipeline/engine/delivery_publish.py` extends the `DeliveryPublisher` protocol
with one optional method:

```python
def setup_hint(self, project_dir: Path) -> str | None: ...
```

A provider returns a human-readable setup recommendation **only when it could
help but is not ready** — for example a remote of its kind exists in
`project_dir`, yet its CLI is not installed or not authenticated — and `None`
otherwise. The method is text-only: forming a hint must never install,
authenticate, push, commit, mutate state, or raise.

The method is genuinely optional. Providers that omit `setup_hint` remain fully
legal `DeliveryPublisher` registrations; the collector duck-types the method
(`getattr` + `callable`) rather than requiring it, so no existing or third-party
provider is forced to implement it.

### `collect_delivery_setup_hints` — the provider-neutral collector

`collect_delivery_setup_hints(project_dir, *, commit_config=None) -> list[str]`
walks every provider registered under the ADR 0121 `orcho.delivery_providers`
group through the shared `pipeline.entry_points` discovery helper. For each
provider exposing a callable `setup_hint`, it invokes the hook inside
`try/except`, collects the non-empty results in discovery order, and
de-duplicates them. It is strictly best-effort: a provider without the method is
skipped silently, any provider exception is swallowed and that provider skipped,
and a discovery failure yields an empty list. It never raises and names no
provider. `commit_config` is accepted for symmetry with `publish_delivery` but
is not required — setup guidance is useful independent of the publish gate.

### GitHub-specific knowledge stays in the provider package

`pipeline/engine/delivery_providers/github.py` owns everything GitHub-specific:

* `_is_github_remote(url)` recognizes a `github.com` remote in both ssh
  scp-form (`git@github.com:owner/repo`) and https form
  (`https://github.com/owner/repo`), with `.git` optional and the host matched
  case-insensitively. Look-alike and self-hosted hosts are out of scope and read
  as non-GitHub.
* `_gh_install_hint()` phrases the install step per platform (Homebrew on macOS,
  otherwise a pointer to the official download page) — text only.
* `setup_hint(project_dir)` reads the project's `origin` remote and returns the
  install recommendation only when that remote is a GitHub remote **and** the
  CLI is missing or unauthenticated; otherwise `None`. Any error yields `None`.

Core and the CLI never call these helpers directly and hold no `gh`/`github`
literal — they see only the opaque hint string returned through the neutral
collector and the optional protocol method.

### Sharpened degrade warning at delivery time

The GitHub provider's degrade branch (when `gh` is unavailable at push time) now
consults the remote: on a GitHub remote it emits a warning that carries the
install hint (recommending the operator install the CLI to enable auto-push + a
pull request); on any other remote it keeps the prior generic message. Both
paths preserve the ADR 0121 contract exactly — `pushed=False`, a warning rather
than an exception, and no push attempted. The delivery decision and gate
semantics are unchanged.

### Post-init recommendation in `orcho workspace init`

After a successful `orcho workspace init`, the CLI gathers candidate project
directories from the init result (and the group root itself when it is a git
repo) and asks `collect_delivery_setup_hints` for advice, printing the first
non-empty hint once as a short side note. The CLI stays thin: it holds no
`gh`/`github` logic and only calls the neutral collector and prints. The whole
step is wrapped best-effort — any detection error prints nothing and never
changes the init exit code. Because the collector only reads, `--dry-run`
surfaces the same recommendation in its preview without any side effect.

## Out of scope

* **Installing or authenticating anything.** This ADR is recommend-only. Every
  surface produces descriptive text; nothing runs an install command, an auth
  command, or edits `PATH`. Enabling the automated path stays a deliberate,
  operator-run step.
* **Non-GitHub providers.** GitLab/Bitbucket/etc. remain future
  `orcho.delivery_providers` registrations; they can opt into recommendations
  later through the same optional `setup_hint` method with no core change.
* **A wire/SDK or MCP change.** Hints are in-process strings surfaced through the
  existing warning list and CLI output. No new persisted field and no paired
  `orcho-mcp` schema slice ship for this step, consistent with ADR 0121.
* **Changing publish or branch-policy semantics.** The ADR 0121 publish gate and
  the ADR 0119 branch policy are untouched; a missing CLI still degrades to
  `pushed=False` + warning while delivery stays successful.

## Consequences

* Operators on GitHub-hosted projects get one concrete, provider-neutral nudge
  toward enabling auto-push + pull-request creation — at delivery time when the
  CLI is missing, and once after `workspace init` — without any code path
  installing or authenticating on their behalf.
* The provider boundary holds. GitHub remote detection and install-hint wording
  live only in the provider package; `orcho-core` and the CLI reach them solely
  through the optional protocol method and the neutral collector, so a grep for
  `gh`/`github` outside `pipeline/engine/delivery_providers/` stays empty.
* The hook is additive and optional. Existing providers keep working unchanged,
  and a future provider can surface its own setup guidance by implementing
  `setup_hint` alone.
* No new wire slice ships. Recommendations ride existing strings, so there is no
  dual path and nothing new to keep in sync across `orcho-mcp`.
