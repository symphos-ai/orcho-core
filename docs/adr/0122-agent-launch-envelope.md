# ADR 0122 — Agent Launch Envelope: isolation-first, no permission micro-management

- **Status:** Proposed
- **Date:** 2026-07-02
- **Deciders:** project owner
- **Related:**
  - [ADR 0023](0023-runtime-protocol-collapse.md) —
    `mutates_artifacts` read/write divide encoded at call construction
  - [ADR 0033](0033-worktree-foundation.md) — worktree
    isolation (GWT-1)
  - [ADR 0034](0034-sandbox-isolation-layered.md) —
    launch-layer L1 hygiene; boundary "orcho does not re-implement the
    runtime CLI's own sandbox"; container listed as a future backend
    behind an explicit trigger
  - [ADR 0104](0104-setup-preflight-terminal-state-projection.md)
    — setup preflight terminal-state projection (home for the new
    environment checks)

## Context

### Current state (verified 2026-07-02)

Every runtime driver hardcodes its most-privileged non-interactive launch
mode. There is no configuration surface — not in profiles, not in
`config.local.json`, not via environment:

| Runtime | Read calls (`mutates_artifacts=False`) | Write calls (`mutates_artifacts=True`) |
|---|---|---|
| Claude (`agents/runtimes/claude.py:548`) | no permission flags | `--permission-mode acceptEdits` + `--dangerously-skip-permissions` |
| Codex (`agents/runtimes/codex.py:414`) | `--dangerously-bypass-approvals-and-sandbox` | `--dangerously-bypass-approvals-and-sandbox` |
| Gemini (`agents/runtimes/gemini.py`) | `--approval-mode plan` | `--approval-mode yolo` (+ `--skip-trust` on every call) |

The rationale is documented in the drivers and is real: interactive
approval prompts have nowhere to go in a non-interactive stream, and
codex's read-only sandbox forbids the verification subprocesses
(`pytest`, `ruff`) that reviewer phases depend on.

Compensating layers exist and stay in force: worktree isolation
(ADR 0033), L1 env allowlist / rlimits / child cleanup / output masking
(ADR 0034), and the streaming destructive-git guardrail.

Incidental defect: on the Claude write path `--permission-mode
acceptEdits` is redundant — `--dangerously-skip-permissions` subsumes it.

### The actual problems (scoped honestly)

1. **Hard incompatibility.** Claude Code managed settings support
   `disableBypassPermissionsMode`; on such a machine every Orcho write
   phase fails at launch with an unclassified CLI error. The same
   setting also refuses root. The hardcode leaves no way out.
2. **No audit answer.** Run manifests do not record what launch
   envelope the agent was granted. "What was this agent allowed to do?"
   has no receipt to point at.
3. **No enforcement boundary orcho owns.** The worktree bounds git/cwd
   damage and L1 bounds env leakage, but network egress and filesystem
   outside the worktree are open, on the operator's own workstation.

### The fundamental constraint

Orcho orchestrates **external** runtime CLIs. Their permission systems
are vendor-owned, mutually asymmetric, and moving: one runtime offers a
delegated approval channel and hooks, another surfaces approvals only
through a different drive protocol, a third has only coarse modes.
An orchestrator cannot enforce fine-grained tool policy inside a
process it does not own — it can only *ask* the vendor's machinery to
do so, per vendor, per version, forever.

A survey of the ecosystem (2026-07-02) confirms the split: in-process
agent runtimes build policy engines (they own the tools); orchestrators
that drive external agents converge on **environment isolation** — an
ephemeral container/VM with restricted network and mounts, full agent
autonomy inside. The wall is runtime-agnostic by construction: one
mechanism covers every current and future runtime with zero per-vendor
mapping.

## Decision (proposed)

**Orcho does not participate in vendor permission systems. It owns the
launch envelope instead.** Three deliverables, in order of leverage:

### 1. Launch-envelope receipt (observability, no control claims)

Every agent invocation records in the run manifest exactly what
envelope the runtime was launched with: binary, privilege-relevant
flags/mode, worktree root, sandbox (L1) profile applied, isolation
backend (see below) if any. This is a statement of fact about the
launch, not a claim of enforcement — which is precisely why it is
honest and cheap. This is the audit answer.

### 2. Launch-mode escape hatch + preflight compatibility

The privileged flag set stops being unconditional:

- A per-runtime launch-mode knob (config/profile level) with exactly
  two values: `native` (launch without bypass flags — the runtime's own
  defaults and the operator's own settings/policies govern) and
  `bypass` (today's behavior, remains the default for now).
- Setup preflight (ADR 0104 family) detects environments where
  `bypass` cannot launch (`disableBypassPermissionsMode`, root) and
  produces a typed terminal state with a precise operator message —
  instead of a mid-run opaque CLI error. Where detection is possible
  it may auto-select `native` with a recorded note.
- The redundant `acceptEdits`-under-bypass defect is fixed in passing.

`native` mode makes no promises about phase success — a runtime that
prompts interactively under `native` will stall and hit the existing
stall/halt machinery, which is the honest outcome. No per-phase posture
taxonomy, no allowlist curation, no approval gateway.

### 3. Official isolation envelope (the enforcement layer)

The enforcement answer is the environment, not vendor flags: an
official Orcho execution envelope — container image / devcontainer
recipe with documented mount policy (workspace, runtime auth), and a
path to network egress control. This re-arms the ADR 0034 "future
backend" row with a materialized trigger: *the operator needs an
enforcement boundary that does not depend on any vendor's permission
system*. Scope, mount strategy for runtime credentials, toolchain
parity for verification phases, and egress policy are its own design
problem and land as a separate ADR + `sandbox.mode` enum value per
ADR 0034's rules; this ADR only fixes the direction: **isolation-first,
one wall for all runtimes**. The first deliverable on that path — an
official container image with credential bootstrap and a baked-in
default workspace — shipped from the distribution repo on 2026-07-03
(`ghcr.io/symphos-ai/orcho`).

Known honest costs to resolve in that ADR: runtime credentials must be
mounted inside the wall; default container networking does not gate
egress; verification phases need the project toolchain inside the
image; macOS file-IO overhead on mounted worktrees.

## Rejected alternative: permission-posture micro-management

An earlier draft of this ADR proposed a per-phase posture axis
(`restricted / guarded / bypass`) with per-runtime flag mappings,
curated tool allowlists per phase archetype, downgrade receipts, and a
delegated approval gateway (MCP prompt-tool / hooks / pending-decision
bridge). Rejected because:

- **Enforcement stays vendor-owned.** Every "guarded" guarantee would
  be a claim about another vendor's process honoring its own flags —
  not something orcho can attest.
- **Permanent asymmetric maintenance.** Three runtimes, three
  incompatible mechanisms, all moving (one vendor is already migrating
  prompts to a model-classifier mode). The mapping table is a chase,
  not a contract.
- **Complexity without a boundary.** Allowlist curation, downgrade
  semantics, and gateway plumbing add a large surface whose failure
  modes (mid-phase tool denial, stalls) degrade the product's core
  loop, while the actual assets (repo, credentials, network) remain
  unprotected on the host.

The isolation envelope delivers the boundary those mechanisms
approximate, uniformly, with zero per-vendor code. If a runtime's
native approval-delegation channel ever becomes load-bearing for a
concrete operator need, it belongs in that runtime's driver as an
optional, driver-owned integration — never as a core contract.

## Consequences

- Orcho becomes launchable under managed runtime policies (`native`
  mode + preflight) and answerable in security review (envelope
  receipt + isolation recipe), without claiming control it does not
  have.
- Core stays runtime-agnostic: no vendor permission vocabulary enters
  the engine contract; drivers keep owning their flags.
- The security roadmap consolidates onto one axis (envelope hardening:
  container → egress) instead of two competing ones.
- Runs under `native` on a prompting runtime stall and halt honestly;
  that is accepted behavior, documented for operators.

## Non-goals

- Re-implementing any runtime's sandbox or approval UX (ADR 0034
  boundary holds).
- Removing `bypass` — inside an isolation envelope it is the intended
  mode; bare-metal it remains the operator's explicit trust decision.
- Per-phase permission profiles, tool allowlists, or approval
  gateways (see Rejected alternative).

## Open questions

1. Default flip timing: should `--no-interactive` / CI operating modes
   eventually *require* either an isolation envelope or an explicit
   `bypass` acknowledgment in config?
2. Envelope receipt schema: flat flags list vs normalized fields —
   align with existing manifest/evidence conventions.
3. The isolation-envelope ADR: image strategy (single image vs
   devcontainer overlay), credential mount policy, egress allowlist
   mechanism, macOS performance guidance.
