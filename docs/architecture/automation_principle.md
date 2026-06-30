# Automation Principle — Verifiable Autonomy, Advisory Authority

This is the animating principle behind Orcho's verification, acceptance, and
handoff design (ADR 0064, ADR 0065, and the Semantic Profiles master plan).

## The principle

**Orcho is autonomous wherever its output is independently verifiable, and
advisory only at the seams where it would otherwise certify itself — and those
seams are few, but iron.**

- It runs the gates, fixes what they flag, and **proves** the result rather than
  **asserting** it.
- It stays advisory exactly where a decision would let it grade its own homework:
  waiving a failing check, granting "done", approving a plan.
- Those seams are deliberately concentrated. Asking a human at every step turns
  oversight into rubber-stamping — which is worse than autonomy, because it
  manufactures a false sense of control.

The failure mode this avoids is **self-certification**. A system that grades its
own homework must be advisory; a system whose work an independent oracle checks
can be fully autonomous.

## The dividing line

The line is not "automated vs manual". It is **"independently verifiable" vs
"self-authority-granting"**:

| Orcho is autonomous | Orcho is advisory |
| --- | --- |
| run commands, edits, retries | waive a red gate |
| red→green repair loop | "is the plan good enough?" |
| detect "is this red environmental?" | "is this `agent_assertion` met?" |
| propose a waiver command | grant the final ship at `human`/waiver seams |

## How it shows up in the engine

- **Gates execute, the gate is the oracle.** Running `pytest`/`cargo`/`phpstan`
  and reading the exit code is ground truth, not a judgment — so it is fully
  autonomous (ADR 0065).
- **Verdict is computed, not claimed.** `ship_ready` is a function of executed
  `executable` criteria + policy-classified findings, not the reviewer's
  self-report (ADR 0065 §6a).
- **Planned subtasks become delivery accounting units.** In `subtask_dag`
  execution, each required subtask needs a terminal receipt (`done`, `blocked`,
  `failed`, `skipped`, or `waived`). The gate checks receipts and their
  task-level proof, rather than trusting a broad "implemented the plan" summary.
- **Self-certification is made visible.** The `executable / agent_assertion /
  human` taxonomy turns "trust without proof" into an auditable metric: a
  `ship_ready=true` resting mostly on `agent_assertion` is a yellow flag.
- **Waiver is the canonical human seam.** A failing check can only be set aside
  by a person or committed config — never by the agent, never auto-substituted on
  retries. Orcho *detects* an environmental red and *proposes* the waiver; the
  human *decides*; the bypass is always recorded. Rare, high-stakes, on the
  record.

## Restated for design reviews

> Autonomous everywhere there is an independent check; advisory exactly on the
> seams where the system would otherwise certify itself — and those seams must be
> few, but iron.
