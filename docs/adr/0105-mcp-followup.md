# ADR 0105 — MCP follow-up: delegate status merge to core + pass through `setup_failed`

This is the **expanded-delivery / follow-up** record for the `orcho-mcp`
transport leg of ADR 0104. It is intentionally a follow-up, **not** a
same-commit companion change: the orcho-core surface added here is
backward-compatible on the wire (the evidence `errors[]` list is additive — a
new `kind` is a new list entry, never a field rename or removal), so no
`orcho-mcp` edit is required for the core change to ship correctly. `orcho-mcp`
was **not** edited from this worktree (separate git history; orcho-core must not
depend on orcho-mcp).

Two items are tracked for the `orcho-mcp` companion. Both are drift-reduction /
pass-through, not behavior the core change depends on.

## Follow-up 1 — delegate `status_merge` to the core projection

`orcho-mcp/src/orcho_mcp/services/status_merge.py` currently carries its **own**
copy of the supervisor↔meta merge rule (`merged_status_from_meta`,
`merged_halt_reason_from_meta`, `supervisor_terminal_status`,
`supervisor_halt_reason`). ADR 0104 introduced the **same** rule on the core
side in `pipeline/run_state/setup_failure.py`:

| orcho-mcp (today) | orcho-core (ADR 0104) |
| --- | --- |
| `merged_status_from_meta(meta, run_dir)` | `merged_status(meta, run_dir)` |
| `merged_halt_reason_from_meta(meta, run_dir)` | `merged_halt_reason(meta, run_dir)` |
| `supervisor_terminal_status(run_dir)` | `supervisor_terminal_status(run_dir)` |
| `supervisor_halt_reason(run_dir)` | `supervisor_halt_reason(run_dir)` |

The two implementations are **proven byte-identical** today: T4's
`tests/unit/pipeline/run_state/test_setup_failure.py` imports
`orcho_mcp.services.status_merge.merged_status_from_meta` as a parity *oracle*
and asserts equality across the full merge matrix (terminal-meta-wins,
empty/`running`-meta supervisor fallback, `exit_code<0` → `interrupted` remap,
`exit_code>0` → `failed`, no-status → `None`). So the rule does not currently
drift — but two hand-maintained copies *can* drift the next time either side is
touched.

**Action for the companion:** have `orcho-mcp/services/status_merge.py` delegate
to the core functions (re-export / thin wrapper) instead of re-implementing the
branch logic, so there is exactly one implementation of the rule and the
parity-oracle test in core becomes a tautology rather than a drift guard.
orcho-mcp already imports orcho-core (`sdk` / `pipeline`), so the dependency
direction is preserved.

**Verification the companion should run (from the `orcho-mcp` root):**

```bash
python -m pytest -q tests/unit/services/test_status_merge.py    # or nearest
python -m pytest -q tests/unit/services/                        # status/projection slice
```

## Follow-up 2 — pass through the `setup_failed` error kind

ADR 0104 / T2 adds a synthesized evidence error record with
`kind = "setup_failed"` (fields `message` / `at` / `halt_reason` /
`runtime_log_hint`) to the evidence bundle's `errors[]` and to
`get_errors_halt(...).errors`. This is **additive**: clients that enumerate
`errors[]` already iterate heterogeneous `kind`s (`run_halted`, `run_failed`,
`plan_parse_error`, `phase_handoff_requested`, `phase_handoff_waiver`,
`verification_gate_waived`, `implement_delivery`, `command_stalled`), so a new
`kind` flows through any pass-through projection unchanged.

**Action for the companion:** confirm the MCP error/observe projections forward
the new `kind` verbatim (no allow-list that would silently drop it), and — if a
client renders a per-kind label — add a `setup_failed` label. No schema break:
`errors[]` is an open list of typed records, not an enum-constrained field.

**Verification the companion should run (from the `orcho-mcp` root):**

```bash
python -m pytest -q tests/unit/services/test_run_reads.py        # errors/halt projection
python -m pytest -q -k "errors or status or observe"
```

## Why this is a follow-up, not a blocker

- **No wire break.** `errors[]` is additive; merged status/halt_reason already
  match the MCP rule byte-for-byte (proven by the T4 oracle test), so every MCP
  surface keeps returning correct values *before* the companion lands.
- **No hidden divergence.** The merge rule is pinned identical by an executable
  oracle test in core; Follow-up 1 only removes the *second copy*, it does not
  change any resolved value.
- **Boundary respected.** orcho-mcp is not edited from this worktree; this note
  is the durable hand-off so the companion is "tracked, not silently ignored".

See `docs/adr/0104-setup-preflight-terminal-state-projection.md` for the core
decision and the merge-rule specification.
