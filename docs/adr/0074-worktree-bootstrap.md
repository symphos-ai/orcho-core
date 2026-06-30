# ADR 0074 — Worktree bootstrap steps

- Status: Accepted
- Date: 2026-06-06
- Relates to: ADR 0033 (per-run worktree isolation), ADR 0044 (pre-run dirty
  intake), ADR 0062 (workspace config git dir)

## Context

Per-run isolation creates a fresh git worktree from committed state. That is the
right source-safety contract, but it means gitignored project prerequisites are
absent from the run checkout. Examples include local native libraries under
`libs/`, generated SDK folders, or package-manager dependency trees. Later gates
can then fail even though the source checkout is runnable.

Copying ignored folders manually into every run worktree is not durable, and
teaching core about project-specific folders would break the provider-neutral
contract.

## Decision

`PluginConfig` gains a `worktree_bootstrap` field. It is a plugin-declared list
of preparation steps run after an isolated worktree exists and after any
pre-run dirty seed has been applied, but before sandbox setup and agent phases.

Core supports a small portable action contract:

- `{"copy": "libs"}` copies a path from the source checkout root into the same
  relative path in the run worktree.
- `{"run": ["composer", "install"]}` runs an argv command in the worktree
  without a shell.
- `{"python": "scripts/bootstrap.py"}` runs a Python script using the active
  Python interpreter.
- `{"shell": "..."}` is an explicit shell escape hatch for platform-specific
  projects.

Each step can declare `platforms` to opt into one or more platform labels
(`posix`, `windows`, `linux`, `darwin`, `macos`, or the raw `sys.platform`
value). Platform mismatches are recorded as skipped.

Bootstrap steps run only when worktree isolation is active. If isolation is off,
the session records `worktree_bootstrap.status="skipped"` rather than mutating
the user's source checkout.

## Consequences

- Project plugins can make run worktrees runnable without committing local
  dependency folders.
- Core owns the lifecycle point and portable primitives; plugins own project
  choices such as which folders to copy or which package-manager commands to
  run.
- Failures halt before agent phases, with `halt_reason=
  "worktree_bootstrap_failed"` persisted in the session.
- Subprocess stdout/stderr is not persisted in the session result, avoiding a
  new accidental secret-capture surface. The error stores the failing step and
  exit code.

## Example

```python
PLUGIN = {
    "name": "PHP API",
    "worktree_bootstrap": [
        {"copy": "libs"},
        {"run": ["composer", "install"], "timeout": 300},
    ],
}
```
