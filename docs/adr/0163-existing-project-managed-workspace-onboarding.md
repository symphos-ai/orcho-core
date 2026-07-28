# ADR 0163: Existing-project managed workspace onboarding

## Status

Accepted

## Context

The original `workspace init` contract treated one path as three identities:

- the directory to scan for projects;
- the parent of `workspace-orchestrator/`;
- the logical workspace and MCP name source.

That shape works for an intentionally prepared multi-repository group, but it
made ordinary adoption hostile. Pointing init at an existing repository was
rejected. Pointing it at a broad parent such as `~/www` scanned unrelated
siblings and still missed projects. The documented workaround was to move or
copy the repository under a prepared parent.

The runtime has no such ancestry requirement. Project checkouts are already
absolute identities, workspace configuration already records absolute project
paths, and run/evidence state can live anywhere selected by
`ORCHO_WORKSPACE`.

## Decision

An existing repository is the primary onboarding input.

When `workspace init` receives a repository root, it:

1. leaves the repository in place;
2. registers exactly that canonical project path;
3. creates the control workspace outside the checkout under the platform data
   root;
4. uses `<repo-slug>-<sha256(canonical-path)[:10]>` as the collision-resistant
   managed identity;
5. accepts an explicit control-workspace override;
6. skips sibling discovery.

The managed path retains a terminal `workspace-orchestrator/` component so
existing workspace/runspace conventions remain valid.

CLI readers and launchers use one project-to-workspace resolver:

1. an explicit `--workspace` remains authoritative;
2. an existing sibling/group workspace is preferred for an intentional shared
   topology;
3. otherwise an existing deterministic managed workspace is selected;
4. an ambient workspace remains the fallback when no project-bound workspace
   exists.

MCP configuration stays explicit. Init prints or writes an
`ORCHO_WORKSPACE` binding; the server does not guess a project from its process
cwd.

A non-repository init target keeps the existing group bootstrap and child
discovery behavior. This is the advanced cross-project topology, not the
single-project prerequisite.

## Consequences

- The primary journey is `cd existing-repo && orcho workspace init`.
- Repositories under `~/www`, `~/projects`, or unrelated parents do not need to
  move.
- `orcho run --project .`, `orcho status`, and other run readers can resolve the
  managed workspace without sourcing an env script.
- Same-basename repositories do not share run state.
- Moving a repository changes its managed identity. The old workspace remains
  intact; the operator may select it explicitly or initialise the moved project
  again.
- Shared cross-project workspaces remain available and are documented after
  the mono-project journey as a best practice.

## Rejected alternatives

### Put `workspace-orchestrator/` inside the repository

This mixes runtime state with source, complicates ignore policy, and violates
the existing separation between the canonical checkout and the control plane.

### Require a prepared parent folder

This preserves the demo topology by making repository reorganisation an
adoption prerequisite.

### Add both a global registry and project-local pointer

Two writable bindings create precedence and drift problems. The deterministic
managed identity plus explicit workspace override is sufficient for this
slice.

### Scan the repository parent automatically

A broad parent often contains unrelated projects and is not evidence of one
intended Orcho workspace.
