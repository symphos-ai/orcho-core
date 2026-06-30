# ADR 0076 — Durable verification-environment receipt

- Status: Accepted
- Date: 2026-06-07
- Relates to: ADR 0066 (repair-receipt re-review protocol),
  ADR 0071 (subtask-attestation repair receipts),
  ADR 0073 (implement-phase substance repair handoff),
  ADR 0075 (event-sourced run-state and terminal writes)

This ADR defines a new, additive durable artifact — the
**verification-environment receipt** — written by the `implement` and
`repair_changes` phases whenever they stand up a throwaway environment to
run real verification (install deps, create a venv, execute checks /
commands). It fixes the contract (JSON shape, on-disk location, who writes
it, who reads it) so the receipt writer and the reviewer-context consumer
build against one agreed surface.

## Context

When a developer-side phase verifies its own work it may need a real
environment: a virtualenv, an editable install, a test or lint invocation.
Today that work is ephemeral. Two problems follow:

1. **No durable proof the phase actually verified anything.** The reviewer
   sees the diff and the narrative but has no machine-readable record of
   *what was run, where, against which interpreter, and with what result*.
   A claim like "tests pass" in narrative is unverifiable after the fact and
   is exactly the kind of unsubstantiated assertion ADR 0071 and ADR 0073
   push back on.

2. **Verification side effects leak into the source checkout.** A phase that
   creates `.venv/` or installs generated files inside the agent project
   directory pollutes `git status --short -uall` and risks those paths being
   picked up as part of the change set. Verification must happen *outside*
   the source checkout, and its trace must land under the run directory the
   pipeline owns — never in the working tree.

There is no existing evidence artifact for this. The v1 evidence bundle
(`pipeline/evidence/`, ADR-adjacent schema in `pipeline/evidence/schema.py`)
folds the event stream into `evidence.json` and already carries
`implementation_receipts` (policy-owned per-subtask delivery receipts derived
from `subtask.receipt` events). Those describe *delivery claims*, not the
*verification environment*. The new receipt is a distinct concern and must
not overload or reshape the existing evidence schema.

## Decision

Introduce a per-verification durable JSON artifact, the
**verification-environment receipt**, written by both developer-side phases
that run environment checks.

### Receipt shape (the writer contract)

A receipt is a flat JSON object:

```json
{
  "phase": "repair_changes",
  "round": 1,
  "kind": "verification_environment",
  "cwd": "/abs/path/to/throwaway/verification/dir",
  "python": "3.12.4 (/abs/path/to/throwaway/.venv/bin/python)",
  "checks": [
    {"name": "ruff", "ok": true, "detail": "ruff check . — 0 issues"},
    {"name": "pytest", "ok": true, "detail": "tests/unit/... 42 passed"}
  ],
  "commands": [
    "python -m venv .venv",
    "ruff check .",
    "pytest -q tests/unit/..."
  ]
}
```

Field contract:

- `phase` — the writing phase name, exactly `"implement"` or
  `"repair_changes"`.
- `round` — the integer phase round the receipt belongs to (the same round
  counter the phase already tracks); `1`-based.
- `kind` — the literal `"verification_environment"`. Reserves the namespace
  so future durable receipt kinds can coexist under the same directory.
- `cwd` — absolute path of the working directory the checks ran in. This is
  the throwaway verification directory, which MUST be outside the source
  checkout (see location rules below).
- `python` — a human-readable interpreter identity (version + resolved
  interpreter path) of the environment the checks used.
- `checks` — ordered list of `{name, ok, detail}` objects: a stable check
  name, a boolean outcome, and a short human-readable detail line. This is
  the field the reviewer summary reads.
- `commands` — ordered list of the raw command strings that were executed,
  for audit / reproduction.

The shape is additive and self-contained: it does not embed or depend on the
evidence-bundle schema, and adding it breaks no existing reader. The phase
implementation owns the writer; this ADR owns the shape.

### On-disk location

Receipts are written under the **run output directory** the pipeline already
owns (`state.output_dir`), mirroring the existing
`run_dir / "phase_handoff_decisions" / *.json` convention:

```
<run_output_dir>/verification_receipts/<phase>-round<N>-<kind>.json
```

Hard rules:

- The receipt file MUST land under `state.output_dir`, never in the agent
  source checkout / project directory.
- The throwaway verification environment (venv, installs, generated files)
  MUST be created outside the source checkout (e.g. a temp dir or a path
  under the run directory), so it never appears in `git status --short -uall`
  and is never mistaken for part of the change set.
- The directory is created lazily on first write, consistent with how the
  decisions directory is handled.

### Both phases write it

This is load-bearing and explicit: **both** `repair_changes` *and*
`implement` write a verification-environment receipt whenever they run
environment checks. The acceptance contract requires parity — a receipt
present for repair but absent for implement (or vice versa) is a defect, not
an acceptable partial. Phases that do not stand up a verification environment
in a given round simply write no receipt for that round; presence is
conditional on having actually verified, not unconditional.

### Reviewer gets a brief summary (the reader contract)

When receipts exist for the run, the reviewer prompt / context includes a
short summary derived from them (primarily the `checks` field: which checks
ran and whether they passed). The summary is brief context, not a verdict
input override: it does not change reviewer-verdict or waiver semantics. The
reviewer prompt builder owns wiring the summary in; the `checks` field shape
above is the surface it consumes.

## No schema break

- The existing v1 evidence bundle and its `schema.py` validation are
  untouched. The verification receipt is a separate file under
  `verification_receipts/`, not a new key inside `evidence.json`.
- `implementation_receipts` (delivery-claim receipts) and
  verification-environment receipts are distinct artifacts with distinct
  shapes and distinct owners; neither subsumes the other.
- Existing evidence readers continue to validate and parse unchanged.

## Consequences

- The reviewer gains durable, machine-readable proof of what verification a
  developer-side phase actually ran, against which interpreter, with which
  outcome — substantiating "I verified X" claims instead of trusting
  narrative.
- Verification side effects are structurally prevented from leaking into the
  source checkout / `git status`, because both the environment and the
  receipt are required to live outside the working tree.
- The writer in `implement` and `repair_changes` is implemented against the
  fixed JSON shape above; the reviewer-summary reader is implemented against
  the `checks` field. Both build to one contract.
- Future durable receipt kinds can share `verification_receipts/` by using a
  different `kind` without reshaping this artifact.

## Deferred

- **Strong schema validation of receipts.** This ADR fixes the shape but
  does not add a `validate_bundle`-style validator for receipts. If receipts
  later feed automated gates, a dedicated validator (mirroring
  `pipeline/evidence/schema.py`) is a follow-up.
- **Evidence-bundle inclusion.** Folding a projection of verification
  receipts into `evidence.json` (so external evidence consumers see them) is
  out of scope here; receipts stand alone under the run directory for now.

## Clarification — relation to the verification contract (Stage 0)

> Append-only addendum. The decision, status, date, and history above are
> unchanged; this section only situates the receipt within the broader
> verification-contract model documented later.

The verification-environment receipt defined in this ADR is the concrete,
**already-implemented** instance of the more general **authoritative receipt**
concept described in
[../architecture/verification_contract.md](../architecture/verification_contract.md).
In the contract's gate / environment / receipt triad, an authoritative receipt
is the durable proof that a declared command ran in a declared environment; the
receipt this ADR specifies is exactly that proof for the developer-side
verification environment.

This addresses the canonical-vs-checkout failure mode directly. The receipt
lives under the run output directory (`state.output_dir`), **never** the source
checkout, and it records *which environment and against which code* the command
actually executed — so a reviewer reads proof of the real subject under test
instead of re-running an ad-hoc host command that may resolve a different
interpreter or a separately installed copy rather than the canonical checkout.

The richer, named per-command receipt form the contract sketches — carrying
`env`, `assertions`, dependency repo HEADs, and similar fields — is a **future
goal** of the verification contract's evolution, **not** part of this ADR's
current shape. Today's receipt remains the environment-scoped artifact fixed by
the [Receipt shape](#receipt-shape-the-writer-contract) above (as shipped in
`pipeline/evidence/verification_receipt.py`).
