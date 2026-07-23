# Runtime Identity Diagnostics

## What it is

A run can show **which provider account / organization** a runtime is executing
under, surfaced as a short hint in the early run header next to each phase's
runtime / model / effort:

```text
Agents
  PLAN      claude-opus-4-8  high  account=Smart-gamma / sales@…
```

The value behind that hint is a small provider-neutral object,
`agents.runtimes.identity.RuntimeIdentity`:

| Field           | Meaning                                             |
| --------------- | --------------------------------------------------- |
| `runtime`       | backend id (e.g. `claude`)                          |
| `source`        | where it came from — `runtime_status` for a real probe, `mock` for fakes, or a miss reason: `unavailable`, `no_status_surface`, `no_account_surface`, `unsupported` (runtime has no probe method), `probe_error` |
| `available`     | whether a usable identity was resolved              |
| `provider`      | optional vendor label                               |
| `account_label` | optional org / account display name                 |
| `email`         | optional account email                              |

## Why it exists

Diagnostic signal, nothing more. The motivating failure: a run was invoking a
provider CLI under a *different* account than the operator was watching usage
for. It looked like a mystery rate-limit, but it was an identity / quota-bucket
mismatch. Showing the account early — before expensive phases start — turns that
class of confusion into a glance at the header.

## It is **not** authoritative

Identity is **never** an authorization or policy decision:

- It is **not** a delivery gate and never blocks a run.
- A missing or unavailable identity is **not** an error — the run proceeds and
  the header simply shows nothing for that phase.
- Nothing in the pipeline branches on its value.

## Safety contract

1. **Sanitized.** Only fields a provider already shows in a *user-facing* status
   command may be populated (`account_label` / `email`). Access tokens, refresh
   tokens, cookies, auth-file paths, and raw auth JSON must never reach the
   object. Producers copy an explicit allowlist of fields out of any status
   output and discard the rest. The identity is rendered to the terminal header
   only; it is not written to `meta.json`, events, or metrics.
2. **Best effort.** Probing is wrapped so any failure (missing binary, timeout,
   non-zero exit, unparsable output, no account field) resolves to an
   `unavailable` identity. A probe never raises into run setup.
3. **Lazy.** Runtime construction, profile listing, and dry-run rendering stay
   side-effect free. A real probe runs only on the TERMINAL run-setup path
   (`pipeline/project/session_run.py`), never on dry-run or non-TERMINAL
   surfaces.

## Provider boundary

Core owns the **shape** (`RuntimeIdentity`) and the **rendering** (the header
hint). Provider-specific extraction lives in the runtime adapters:

- **Claude** (`agents/runtimes/claude.py`) — probes `claude auth status`, the
  non-interactive status surface the CLI already shows users, with a short
  timeout, and reads only the `email` / `orgName` fields.
- **Codex** (`agents/runtimes/codex.py`) — returns an unavailable identity
  with `source="no_account_surface"`. The only non-interactive status
  surface, `codex login status`, reports the *auth method* and no
  user-facing account / organization / email, so there is nothing safe to
  surface. If a future CLI exposes a stable account field, parse it in the
  adapter the same way Claude does.

A third-party runtime that implements no `probe_identity` simply yields
`unavailable` via `agents.runtimes.identity.probe_runtime_identity`; the
capability is structural, not a required Protocol method.

## Dedup

Identities are probed once per distinct agent **instance**, never collapsed by
runtime name. Two phases pinned to the same runtime can still run under
different accounts; collapsing by name would hide exactly the mismatch this
diagnostic exists to catch, so each instance surfaces its own hint.

## Not the failure-classification plane

Identity answers "whose account is this runtime using?" — a header-only
glance. The adjacent question, "why did this runtime *fail*?", lives in a
separate machinery with the opposite persistence contract (it **does** write
structured metadata to `meta.json`, `run.end` events, and the evidence
bundle):

- **Provider/runtime failure classification** (ADR 0118) — transient
  provider-side failures (rate limit, timeout, connection, killed process)
  classify as `failure_kind="provider_runtime"` with bounded retries;
  see `core/io/retry.py` and `pipeline/run_state/provider_runtime.py`.
- **Signal-exit classification** — `classify_signal_exit` in
  `core/io/retry.py` splits kill-shaped exits (SIGKILL/SIGSEGV/SIGABRT →
  recoverable `provider_runtime`) from cancel-shaped ones (SIGINT/SIGTERM →
  immediate halt), and durable records carry `halt_reason` values like
  `signal:<NAME>` / `abnormal_exit:<rc>`.
- **Provider-access recovery** (ADR 0101) — access-shaped failures
  (`AgentAccessError`) project `failure_kind="provider_access"` with typed
  `recovery_actions` (retry / halt / replace runtime+model) via
  `pipeline/project/provider_recovery.py` and the durable runtime override
  in `sdk/run_control/runtime_override.py`.

If you are debugging *who* the runtime was, start here; if you are debugging
*why it died*, start there.
