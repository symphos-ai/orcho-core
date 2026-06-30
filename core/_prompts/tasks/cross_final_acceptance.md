Cross-project final acceptance gate.

Earlier rounds already approved each repo (per-project release gate)
and verified the diffs line up on shared interfaces (contract check).
Your decision is the system-level question:

**Can the coordinated multi-repo change ship as one system?**

What that means in practice:

- You are not re-reviewing individual repos and not re-checking
  pairwise contracts. Trust the per-project release verdict and the
  contract-check pass unless system-level coordination breaks them.
- Check that every required alias produced a release-ready verdict
  and that no alias's "ship-ready" depends on a sibling whose
  verdict left a propagating gap.
- Check that the coordinated change tells a coherent story for an
  external observer (runtime, data plane, callers) — not locally
  ship-ready slices that compose into a broken whole.
- Check that no system-level invariant from the cross-plan was
  weakened by the per-project implementations.

Anchor on the cross plan, per-project release verdicts, and contract
check results in the body above.

Release blockers (P0/P1/P2) capture system-level ship blockers, not
single-repo issues. Verification gaps capture missing evidence
across the boundary (e.g. no end-to-end smoke test for the new
producer/consumer path).

Report system-level contract status: `task_contract` (cross-plan
goal satisfied), `interfaces` (system-level interfaces hold),
`persistence` (state safe across the boundary), `tests` (cross-system
risk surface covered).

A clean release is the expected outcome when every per-project gate
and contract check approved. Do not invent system-level blockers and
do not re-litigate decisions earlier gates accepted. (If
preconditions are not met, the cross runner already synthesised a
REJECTED verdict before you were invoked.)
