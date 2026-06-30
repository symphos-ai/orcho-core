Review the code changes against the task.

Anchor on the task contract and acceptance criteria. Read the diff
end-to-end before making findings. Apply only the failure-mode
categories that fit this change — do not run the list mechanically.

Categories to consider when relevant: logic and edge-case errors;
interface or persisted-shape breakage; missing error handling or
boundary checks; incomplete implementation, stubs, or hardcoded
assumptions; weak tests (missing, mock-heavy, happy-path only, or
not invariant-based); inconsistency with established local patterns;
performance risk on hot paths.

Report only findings that affect correctness, completeness,
maintainability, or the task contract. For each finding, name the
file/line/function and the exact failure scenario. Do not pad with
manufactured issues.
