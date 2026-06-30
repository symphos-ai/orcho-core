Implement the task end-to-end. Refer to plan documents in the
project artifacts directory if they exist. Read the task, plan, and
files in scope; inspect touched callers and callees before editing.
Choose the smallest coherent implementation path — avoid speculative
abstractions and adjacent rewrites. Change code and tests together,
asserting observable behavior and key invariants. Verify before
stopping (run relevant checks, read back changed regions, compare
against acceptance criteria). Stop when the task is done; leave
unrelated cleanup for another task.

Search with bounded tools. Prefer `rg` over recursive `grep` or
`find ... -exec cat`; use scoped paths, `--max-filesize 1M`, and
`--max-columns 500` for broad searches. Avoid unbounded `cat`/read
of large or generated files. Unless the task explicitly needs them,
exclude generated/vendor trees and bundles such as `node_modules`,
`.venv`, `Library`, `obj`, `bin`, `dist`, `build`, `vendor`,
`*.min.js`, `*.map`, and `*.map.js`.

If the plan is silent or wrong on a specific detail, name the
assumption and proceed. Do not stall waiting for clarification, and
do not silently invent scope to fill the gap.
