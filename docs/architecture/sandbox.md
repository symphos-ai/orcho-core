# Sandbox Isolation

orcho-core ships **launch-layer process hygiene** on top of the
git worktree isolation introduced in ADR 0033. The schema accepts
exactly two modes — `off` (no isolation) and `env` (L1 active) —
and nothing else.

| Layer | What it does | Status | Cross-platform |
|---|---|---|---|
| **L1 — env hygiene** | env allowlist, resource limits, child-process cleanup, output token masking, capability detection | shipped (ADR 0034) | yes — same code path on Linux / macOS / Windows |

There is no L2 / L3 / L4 in the schema. Earlier drafts reserved
enum values for native-FS, network and container backends; they
were dropped on the way to merge once it became clear that the
runtime CLIs we drive (Claude Code, Codex) already gate the rows
those backends would have covered. If a concrete trigger surfaces
later — see "If a future backend ever lands" below — the schema
widens then, not pre-emptively.

The contract is that a mode **never silently degrades**: if a
profile asks for a value the resolver does not recognise, it
fails fast with a precise "is not one of" error rather than
falling back to a weaker mode the profile did not ask for.

## What this layer is *not*

This sandbox is **launch-layer hygiene + observability**, not a
defence-in-depth duplicate of the agent runtime's own sandbox.
Modern agent CLIs (Claude Code, Codex, etc.) ship their own
tool-policy, file-access approval, and command-execution
guardrails inside the agent process. orcho does not try to
re-implement those.

The boundary:

| Concern | Belongs to runtime | Belongs to orcho L1 |
|---|---|---|
| Which `Bash` / tool calls the agent may issue | yes | no |
| Which files the agent may edit inside its cwd | yes (+ orcho's `command_guard` for destructive git) | no |
| Per-tool human-in-the-loop approvals | yes | no |
| What enters `os.environ` of the runtime CLI before it starts | no — runtime already inherited it | **yes** |
| What appears in `output.log` / stdout echo / transcript | no — runtime doesn't see our stream pipeline | **yes** |
| Grandchildren left alive after a parent-side timeout / abort | no — runtime has no parent watchdog | **yes** |
| CPU / RSS / open-files / file-size caps on the runtime process | no — the runtime does not self-limit | **yes** |
| Unified manifest "what protections actually applied" | no | **yes** |
| Same baseline applied to Claude, Codex, Gemini, third-party runtimes | no — each ships its own | **yes** |

L1 covers the launch-layer rows. Runtime sandbox covers the
in-process rows. Those sets do not overlap, so L1 is additive
rather than redundant.

### Relation to the launch envelope (ADR 0122)

One launch-layer concern deliberately lives *outside* the sandbox
schema: the privilege flags the runtime CLI is started with
(today the drivers hardcode e.g. `--permission-mode acceptEdits`
plus the bypass flag for write phases). [ADR 0122](../adr/0122-agent-launch-envelope.md)
owns that axis. It sets an isolation-first direction — a launch
receipt in the run manifest (binary, privilege-relevant flags,
worktree root, applied L1 profile), a per-runtime `native |
bypass` launch-mode knob with preflight detection of environments
where bypass cannot launch, and an official container envelope —
and explicitly rejects the permission-posture / per-phase tool
allowlist / approval-gateway alternative.

Status split, so this doc stays honest: the official container
image (the outside-in envelope around the whole orcho process)
ships from the distribution repo; the launch-mode knob, receipt,
and preflight are the accepted direction, not yet code. When they
land they extend the launch envelope, not the `sandbox.mode`
enum — L1 stays the in-process hygiene layer inside whatever
envelope the operator chose.

### If a future backend ever lands

The schema is narrow today on purpose; widening it is cheap if a
concrete trigger appears:

* **Native FS sandbox** (bwrap / sandbox-exec / AppContainer) —
  if orcho starts driving a runtime that does **not** ship its
  own FS policy. The runtimes we drive today already constrain
  file access at the runtime; layering an OS-native sandbox on
  top is duplicate work.
* **Network gating** — if a concrete exfiltration pattern shows
  up in real runs *and* the runtime CLI does not gate egress.
  Today the agents we ship route network through their own API
  calls; an HTTPS proxy with host allowlist is the right answer
  only after a real signal.
* **Container** — the trigger materialized, but outside-in: the
  operator needs an enforcement boundary that does not depend on
  vendor permission flags (ADR 0122), so the official container
  image wraps the *whole* orcho process rather than adding a
  `sandbox.mode` backend. An in-process trust boundary (e.g.
  third-party `orcho.skills` plugins loading arbitrary code)
  would still be the trigger for a container *backend* here.

Each of these would land as its own ADR + a new enum value, not
as the activation of a pre-reserved slot. **No monster where the
runtime already covers the case.**

## Threat model

L1 is designed against two threats:

1. **Accidental damage by own agent.** The LLM mis-fires and
   emits `rm -rf ~/`, a fork bomb, or echoes a secret it read
   from env into the log. Not adversarial — just a misaligned
   action with high blast radius.
2. **Prompt injection from repo content.** Issues, PR bodies,
   committed code, or third-party diffs contain instructions the
   agent follows. Adversarial in intent, executed by our trusted
   process.

L1 closes the launch-layer rows: secret leak via env read,
secret leak in output, fork bomb / OOM / disk fill, orphaned
child processes. Other rows — `rm -rf`, `git push --force`,
writes to `~/.bashrc`, HTTP exfiltration, DNS-based exfiltration
— are closed by the runtime sandbox of the agent CLI (Claude
Code, Codex, etc.) at the tool-call layer, not by orcho. The
worktree isolation from ADR 0033 covers the destructive-git
subset specifically so that holds even when the runtime
sandbox does not.

The complete defence stack today is **L1 + worktree + runtime
sandbox**. Building an orcho-side FS or network sandbox on top
of a runtime that already gates the same row would be defence-in-
depth duplication — explicitly out of scope under ADR 0034.

## L1 components

Five orthogonal mechanisms, all on by default when `mode=env`:

### Environment allowlist

`os.environ` is filtered through a built-in list + per-profile
additions. Built-in list lives in
`pipeline/sandbox/defaults.py:DEFAULT_ENV_ALLOWLIST`. The
denylist always wins over the allowlist, so an operator can
strip a value matched by a wildcard.

The run manifest records the count of stripped variables (never
the names — names would leak the existence of secrets that the
operator wanted hidden).

### Resource limits

Per-agent caps on:

* `cpu_seconds` — wall + user CPU budget;
* `memory_mb` — process address-space limit;
* `open_files` — file-descriptor table size;
* `file_size_mb` — single file size cap.

Implementation:

* **Linux / macOS** — `resource.setrlimit` called inside the
  `preexec_fn` after fork. `RLIMIT_CPU`, `RLIMIT_AS`,
  `RLIMIT_NOFILE`, `RLIMIT_FSIZE`. macOS silently ignores
  `RLIMIT_AS` on some versions; failures are non-fatal.
* **Windows** — Job Object with
  `PerProcessUserTimeLimit` and `ProcessMemoryLimit` set via
  `JobObjectExtendedLimitInformation`. `open_files` /
  `file_size_mb` are Unix-only and silently skipped.

Limit values use `0` to mean "no limit" — matches the existing
`timeouts.*_seconds=0` convention in `config.defaults.json`.

### Child-process cleanup

The agent and its grandchildren die when orcho dies.

* **Unix** — `setpgrp()` in `preexec_fn` gives the agent its own
  process group; a parent-side abort calls
  `os.killpg(pgid, SIGKILL)`. **Linux only** adds
  `PR_SET_PDEATHSIG=SIGTERM` via `prctl` for belt-and-braces:
  if orcho exits without a graceful path, the kernel itself
  notifies the agent.
* **Windows** — Job Object's `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`
  is automatic and stronger than the Unix equivalent (kernel-
  enforced on parent handle close, no signal race).

The stream records this lifecycle in a private, in-memory registry keyed to
the exact directly spawned `Popen`. Its safe handle can report `running`,
`exited(exit_code)`, or `unavailable`; terminal settlement is memoized and the
launcher stays retained through it. Retry is permitted only after `exited` (or
when no prior child exists), while `running` and `unavailable` fail closed.

This is a direct-child ownership boundary, not per-grandchild visibility. A
confirmed child process group is the widest cancellation boundary; Orcho does
not scan the host or infer ownership of nested provider tools by command name.
The handle and its observations are never persisted to run state or evidence.
Free-text `pgrep`/`pkill` remains a separate non-terminal guardrail diagnostic;
typed handle poll/wait does not enter that text-classification path. See
[ADR 0143](../adr/0143-provider-owned-child-lifecycle.md).

### Output token masking

A regex-based masker rewrites known secret shapes before they
reach `output.log` and the live stdout echo. Built-in patterns
cover the three providers shipped under `orcho.agent_runtimes`:

* `sk-ant-…` — Anthropic
* `sk-…` — OpenAI / Codex
* `AIza…` — Google / Gemini

Operators add patterns via `sandbox.masking.custom_patterns` in
config or profile. Bad regex fails at resolver time, not at
agent dispatch.

**Returned stdout stays raw.** Runtime parsers (Claude JSON
session-id extraction, Codex rollout-id detection) operate on
the value `_stream_run` returns; masking those would break
parsing. The masker is applied only to the **displayed and
persisted** stream, not the machine-consumed return value.

### Capability detection

At run init, `pipeline.sandbox.capabilities.detect_capabilities`
records the platform string and probes the `pywin32` import.
The snapshot lands in `meta.json.sandbox.capabilities` so
operators can see — for example — whether the Windows Job
Object backend was available. The probe stays narrow on purpose:
only inputs L1 actually consumes are recorded. Detecting bwrap /
sandbox-exec / podman would imply that orcho plans to use them.

## Configuration

### Global defaults

`core/_config/config.defaults.json` ships a `sandbox` block:

```jsonc
"sandbox": {
  "mode": "env",                  // off | env
  "env_allowlist": [],            // appends to DEFAULT_ENV_ALLOWLIST
  "env_denylist": [],             // wins over allowlist
  "limits": {
    "cpu_seconds": 0,             // 0 means no limit
    "memory_mb": 0,
    "open_files": 0,
    "file_size_mb": 0
  },
  "masking": {
    "builtin_patterns": true,     // sk-ant-, sk-, AIza-
    "custom_patterns": []
  }
}
```

There is no `network` / `proxy` knob: orcho does not gate
network egress. Runtimes do that at the API-call layer; building
an HTTPS proxy here would duplicate work the runtime already
covers. There is also no `mode: native` / `mode: container`
enum value — those would imply we plan to ship those backends.
If a concrete trigger surfaces later (a runtime registered under
`orcho.agent_runtimes` without its own FS policy, third-party
skills loading arbitrary code in-process), the enum widens
alongside the backend at that point.

### Per-profile override

A profile may override any field of the global sandbox block:

```jsonc
{
  "name": "long_running",
  "sandbox": {
    "limits": { "memory_mb": 16384, "cpu_seconds": 7200 },
    "env_allowlist": ["MY_PROJECT_TOKEN"]
  }
}
```

Override semantics:

* `mode`, `limits`, `masking` — replace field-by-field;
* `env_allowlist`, `env_denylist` — **additive** (profile
  entries extend the global list, they do not replace it).

Schema validation runs at profile-load time via
`pipeline.sandbox.resolver._parse_section` so a malformed
`sandbox` block fails fast — operator config bugs surface before
the run starts, not in the middle of an agent dispatch.

## Run manifest

`meta.json.sandbox` carries the resolved policy + capability
snapshot for every run:

```jsonc
"sandbox": {
  "mode": "env",
  "limits": { "cpu_seconds": 0, "memory_mb": 0, "open_files": 0, "file_size_mb": 0 },
  "env_allowlist_effective": ["PATH", "HOME", "..."],
  "env_stripped_count": 47,
  "masking": { "builtin_patterns": true, "custom_patterns": 0 },
  "capabilities": { "platform": "linux", "pywin32": false },
  "limit_hit": null
}
```

Operators inspecting a finished run see exactly what isolation
applied. `env_stripped_count` is the count, not the names — names
would leak the existence of secrets the allowlist intentionally
filtered. `limit_hit` is null when no rlimit / Job Object cap
was tripped, otherwise carries the kind and value of the cap
that fired.

## Opt-out

`sandbox.mode: off` in `config.local.json` or a profile turns
L1 off entirely — env is inherited verbatim, no rlimit, no
masking, no process-group setup. This is the escape valve for
agents or test fixtures that need the pre-L1 behaviour. The
opt-out is **per-profile**, not a global compile-time flag, so
the same orcho install can sandbox most runs and let an
exception through where needed.

## See also

- [ADR 0034 — Sandbox isolation, layered model](../adr/0034-sandbox-isolation-layered.md)
- [ADR 0033 — Git worktree foundation](../adr/0033-worktree-foundation.md)
- `pipeline/sandbox/` — module entry point.
- `tests/unit/pipeline/sandbox/` — coverage for each component.
