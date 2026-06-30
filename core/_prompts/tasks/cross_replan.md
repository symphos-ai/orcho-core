The QA reviewer found issues with your previous cross-project plan.
The task description, the projects involved, and the reviewer
critique are supplied separately by the runner.

Revise to address every finding:

1. **Smallest change per finding.** Touch the smallest piece of the
   plan that resolves each finding (one subtask or the shared
   interface contract). Parts the reviewer did not critique survive
   unchanged.
2. **Address persistence gaps explicitly.** If a finding flagged a
   missing schema/migration/storage change, add it as a concrete
   subtask in the right project or justify why no change is needed.
3. **Show finding → edit mapping.** Briefly note, in the relevant
   subtask, which finding each revision addresses.
4. **Handle the un-addressable.** If a finding is out of scope,
   contradictory, or wrong, say so explicitly in the relevant
   subtask. Do not silently leave it unaddressed and do not invent
   scope.

Do not write implementation code — your output is the revised plan.
