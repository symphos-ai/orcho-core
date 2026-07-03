# ADR 0120 - Unattended no-interactive phase handoffs

Status: Proposed

## Context

Project phase handoffs are useful in interactive and supervisor-controlled
runs: the run parks at `awaiting_phase_handoff`, writes the active handoff into
`meta.json`, and waits for `phase_handoff_decide`.

That same pending-decision contract is wrong for an explicitly unattended CLI
run. When a user invokes `orcho run --no-interactive`, there may be no external
controller watching the run and no human prompt available. Parking on a
human-only decision can leave the run unable to make progress.

The existing `no_interactive` flag is not enough to distinguish those cases.
Headless supervisors also set `no_interactive=True` so terminal prompts are not
shown, but they still rely on the persisted pending handoff and decision API.

## Decision

Add a project-level `unattended` signal on `ProjectRunRequest` and `_PipelineRun`.
The CLI sets `unattended=True` for `orcho run --no-interactive`. Programmatic
and supervisor callers keep the default `False`, even when they set
`no_interactive=True`.

`unattended` is request-only. The legacy `run_pipeline(...)` kwarg surface stays
unchanged; the CLI dispatches through the typed `ProjectRunRequest` boundary.

When a phase handoff reaches the non-interactive branch:

* `unattended=False` preserves the existing park-and-decide behavior.
* `unattended=True` resolves advisory handoffs by recording a normal
  `continue` decision through the existing SDK decision-artifact path.
* `unattended=True` does not synthesize approval for authoritative or safety
  handoffs. Scope-expansion handoffs, implement handoffs, and handoffs without
  `continue` in `available_actions` become terminal halts with
  `halt_reason="phase_handoff_unattended_halt"`.

The unattended halt also records a compact `phase_handoff_unattended` block in
`meta.json` with the handoff id, phase, trigger, policy reason, and note. The
phase-handoff decision artifact schema is unchanged.

## Consequences

Interactive behavior is unchanged: prompts, menus, retry feedback, and operator
decisions remain on the same path.

Supervisor behavior is unchanged by default: `no_interactive=True` alone still
parks at `awaiting_phase_handoff`.

CLI unattended runs no longer dead-end on advisory phase handoffs. They either
continue through the existing audit trail or halt with a stable machine-readable
reason when continuing would imply authority the policy cannot safely assume.
