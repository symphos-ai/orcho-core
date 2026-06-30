# ADR 0084 — Verification contract cross-repo receipt graph (Stage 7)

- Status: Accepted
- Date: 2026-06-10
- Relates to: ADR 0083 (delivery-gate awareness, Stage 6), ADR 0082
  (final-acceptance readiness awareness, Stage 5), ADR 0081 (scheduling and
  repair routing, Stage 4), ADR 0080 (native command-receipts, Stage 3),
  ADR 0078 (env-assertions, Stage 2), ADR 0077 (read-only projection, Stage 1)

## Context

Stages 1–6 made the verification contract declarable (Stage 1), executable on
demand (Stages 2–3), blocking through deterministic gate routing (Stage 4),
visible to the `final_acceptance` reviewer as read-only readiness (Stage 5), and
enforced at the delivery boundary (Stage 6). Throughout, a command-receipt's git
provenance has recorded exactly **one** subject — the run worktree
(`{checkout}`): its HEAD, baseline, and changed-files fingerprint. That single
subject is enough for an in-repo change but blind to the motivating incident.

The motivating incident is cross-repo. `orcho-mcp` depends on `orcho-core`; a
task in `orcho-mcp` proves itself against a *canonical* `orcho-core` checkout
named via `{dependency:orcho-core}` (see example (a) / (e) in
`docs/architecture/verification_contract.md`). When a command runs `pytest`
against that dependency checkout, the receipt proved the command exited 0 — but
it never recorded *which commit of `orcho-core`* the proof ran against. If the
dependency checkout's HEAD moves after the receipt is written (someone rebases
the canonical core, a sibling run advances it), the receipt is silently about a
world that no longer exists. The `api@abc123 + shared@def456` cross-repo summary
(example (e)) was explicitly documented as a *future* receipt goal — Orcho could
not yet name the dependency HEADs a command tested against.

Two things were missing:

1. **Provenance** — a receipt that records, per declared dependency, the commit
   it was tested against, so the proof names its cross-repo subjects.
2. **Bounded invalidation** — a way to mark exactly the affected receipts stale
   when a depended-on dependency's HEAD moves, without re-litigating in-repo
   staleness and without inventing new status vocabulary.

Stage 7 closes both, while holding every Stage 1–6 boundary: it executes no
dependency build, writes nothing into a dependency repo, and does not move the
evidence v1 bundle or the MCP wire.

## Decision

### 1. A `dependencies` block on the command-receipt (schema v2)

The command-receipt gains a top-level `dependencies` array — a **sibling** of
the existing `git` block, not a replacement. `git` stays the subject checkout's
own differential lens (`{checkout}` HEAD / baseline / fingerprint); `dependencies`
records the depended-on repos. There is **one record per declared
`dependency_repos` entry**, in deterministic order by name:

```json
{
  "name": "orcho-core",
  "path": "/abs/canonical/orcho-core",
  "head": "<git HEAD of the dependency checkout, or null>",
  "dirty": false,
  "changed_files_count": 0,
  "changed_files_fingerprint": "<sha256[:16] of sorted changed files, or null>",
  "depends_on": true
}
```

`COMMAND_RECEIPT_SCHEMA_VERSION` is bumped **1 → 2**. Per the project's
no-backcompat-ceremony rule for internal plumbing, the writer/readers move to v2
in place; there is no dual-path reader.

The dirty summary is deliberately **lossy**: `dirty` (bool), a count, and a
fingerprint — **never the dependency's changed file paths**. A dependency repo's
file names are not this repo's business to record; the summary is only enough to
note "the dependency tree was not pristine." When the dependency HEAD is
unavailable (`head` is `null` — not a git repo / git failed), all three dirty
fields are `null`: the tree could not be inspected safely, so nothing is
asserted about it.

### 2. The `depends_on` rule

`depends_on` answers "did *this command* actually use this dependency?" — it is
per-command, not a property of the declaration. It is `true` exactly when the
dependency's resolved path is a **path-prefix** (with an `os.sep` boundary, not
a bare substring) of at least one of:

- a token of the resolved `argv`,
- the effective cwd (`eff_cwd`),
- the resolved interpreter (`python`),
- a value of `env_overrides`.

The boundary requirement means `/repo/dep` matches `/repo/dep/bin/tool` and
`/repo/dep` itself, but **not** `/repo/department`. A declared dependency a
command never references gets a record with `depends_on: false` (its provenance
is still captured for completeness, but it never drives staleness).

### 3. HEAD-only, depends-on-only stale semantics

A receipt is **stale by dependency** when, for the *first* recorded dependency
entry that satisfies all of:

- `depends_on` is true,
- the recorded `head` is non-null,
- the dependency's *current* HEAD is non-null and **differs** from the recorded
  one,

the classifier reports `stale` with a reason that names the dependency and both
SHAs: `dependency orcho-core HEAD moved <old> -> <new>`.

The invalidation is deliberately **narrow**. None of the following make a
receipt stale:

- a HEAD move on a dependency with `depends_on: false`;
- a receipt with no `dependencies` block at all (old v1 receipts never become
  falsely stale);
- a current HEAD that cannot be read (no path / not a git repo) — staleness is
  *not asserted*, matching the in-repo degradation rule;
- a **dirty-only** change to the dependency (HEAD unchanged). Dirty is
  informational; it never participates in the stale decision.

Stage 5 readiness and the Stage 6 delivery gate both consult this through the
single shared classifier `classify_required_receipts`, which now returns a typed
`ReceiptClassification(status, reason)`. Subject-checkout staleness is checked
first (in-repo fingerprint / HEAD drift); dependency staleness only when the
subject still matches. The **status vocabulary is unchanged** —
`present / missing / failed / stale` — the reason is additive context surfaced in
the readiness render's *Stale receipts* / *Remaining before ready* sections and
in the delivery assessment's `lines`. The audit keys on the commit-decision
artifact (`verification_stale` et al.) keep using command **names** only; the
reasons live solely in the human-readable strings.

The `orcho verify run` CLI surface also gains an `against: <name>@<short-head>`
line (with a `+dirty` marker) per `depends_on` dependency, so an operator sees
which dependency commits each command was tested against. Stage 5 readiness
likewise surfaces a *Tested dependency commits* line for present receipts.

### 4. `changed_files_fingerprint` moves to a low-level module

To capture per-dependency provenance, the receipt **writer**
(`pipeline/verification_command.py`) must import the provenance helper
top-level. The fingerprint helper previously lived in `verification_command`,
and the Stage 5 reader (`verification_readiness`) imported it from there. Letting
the new provenance module import `verification_command` (for the fingerprint)
while `verification_command` imports the provenance module (for capture) would
create an import cycle.

The fix is a new **low-level** module
`pipeline/verification_dependencies.py` that imports only stdlib,
`core.io.git_helpers`, and the typed `PlaceholderContext`. `changed_files_fingerprint`
(and its `_FINGERPRINT_LEN` constant) **move here** and become the single home;
both the writer and the readiness reader import it from this module. The import
graph is strictly one-directional:

```text
verification_command  ─┐
                       ├─→  verification_dependencies  ─→  core.io.git_helpers
verification_readiness ─┘                                  (+ stdlib, PlaceholderContext)
```

`verification_dependencies` **never** imports `verification_command` or
`verification_readiness` — neither top-level nor lazily. A guard test imports
both `verification_command` and `verification_dependencies` together and asserts
that `verification_command.changed_files_fingerprint` *is* the object from
`verification_dependencies`, pinning the direction and forbidding a re-export
shim. The module exposes three pure, never-raising functions:
`capture_dependency_provenance`, `current_dependency_heads`, and
`dependency_stale_reason`.

## Consequences

- A cross-repo command-receipt now names the dependency commits it was tested
  against — the `api@abc123 + shared@def456` summary is realized, not aspirational.
- A dependency HEAD move invalidates exactly the receipts that depended on it,
  with a reason a reviewer can read, at both the Stage 5 readiness surface and
  the Stage 6 delivery gate — and nothing else is touched (the four negative
  conditions above stay `present`).
- Provenance capture and staleness are single-sourced through one low-level
  module with a one-directional import graph; the fingerprint helper has one home.

## Boundaries, stated explicitly

- **No commits or writes into dependency repos.** Capture is read-only git
  (`rev-parse` / `status`) against each declared dependency path; receipts are
  written only under `run_dir`, never into a dependency checkout or the source
  tree.
- **Never-raise discipline.** Every git/IO failure during capture or
  classification degrades (a field becomes `null`, staleness is simply not
  asserted) — exactly as in the Stage 2–6 modules. No new raise path is added.
- **Bounded cost.** Capture and classification are `O(declared dependencies)`
  git calls — one HEAD read per dependency — with no recursion and no workspace
  scan.
- **No dependency file paths recorded.** The dirty summary is bool / count /
  fingerprint only; a dependency's changed file names never enter this repo's
  receipts.
- **Dirty is informational.** It is captured and surfaced (CLI `+dirty`) but
  never drives staleness — only a HEAD move does.
- **Status vocabulary and audit keys unchanged.** `present/missing/failed/stale`
  and the `commit_decision` artifact keys (`verification_missing` / `_failed` /
  `_stale`) are untouched; reasons live only in human-readable strings.

## MCP wire falsifier

**Claim: Stage 7 does NOT change the MCP-facing wire shape. No `orcho-mcp`
update or mock smoke is required.** (Same discipline as the ADR 0080 T9
falsifier.)

Evidence:

1. **The `dependencies` block is out-of-wire by physical location.** It is
   written under `<run_dir>/verification_command_receipts/<command>.json` — the
   directory the evidence collector deliberately does **not** read (it reads only
   `verification_receipts/`). The `verification_command` kind has no slot in the
   evidence v1 schema (`REQUIRED_TOP_LEVEL_KEYS` / `REQUIRED_COMMAND_KEYS`).
2. **The evidence digest never carries `dependencies`.** `summarize_command_receipts`
   — the compact projection that feeds the evidence v1 bundle via the collector —
   was left untouched; its key set is exactly the v1 set (`command`, `env`,
   `exit_code`, `parity`, `passed`, `has_baseline`). This is pinned by the
   falsifier test `test_summary_never_carries_dependencies_key` in
   `tests/unit/pipeline/evidence/test_verification_receipt.py`: if the
   `dependencies` block ever leaks into the digest, that test fails and the
   falsifier flips — an `orcho-mcp` update plus an E2E mock smoke then become
   mandatory.
3. **No new mode flag, profile-shape field, runtime schema, or gate primitive.**
   The `schema_version` bump is on the run-local command-receipt artifact only;
   the `CommandOutcome.dependencies` CLI/SDK field is additive presentation, not
   serialized onto any MCP request/response. `_build_verification_readiness`
   (the evidence readiness digest) returns its prior shape.

**Stop condition.** If a later requirement puts the dependency graph or
dependency-stale verdict on an MCP request/response, that is a separate
`orcho-mcp` workstream with its own contract — not a silent core change.

## References

- `pipeline/verification_dependencies.py` — low-level cross-repo provenance:
  `changed_files_fingerprint` (new home), `capture_dependency_provenance`,
  `current_dependency_heads`, `dependency_stale_reason`
- `pipeline/verification_command.py` — `run_command` captures the `dependencies`
  block (top-level import from `verification_dependencies`, no cycle)
- `pipeline/evidence/verification_receipt.py` — `COMMAND_RECEIPT_SCHEMA_VERSION`
  = 2, `_normalize_dependencies`; `summarize_command_receipts` unchanged
- `pipeline/verification_readiness.py` — `ReceiptClassification`, dependency
  staleness in `classify_required_receipts`, *Stale receipts* reasons and
  *Tested dependency commits* render
- `pipeline/verification_delivery.py` — `DeliveryVerificationAssessment.stale_details`,
  reason in `lines`
- `sdk/verify.py` / `cli/_formatters.py` — `CommandOutcome.dependencies` and the
  `against:` line in `verify run`
- `docs/architecture/verification_contract.md` — Stage 7 section, the v2
  command-receipt shape, the *Implemented today vs proposed* row
