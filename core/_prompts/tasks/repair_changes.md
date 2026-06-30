You previously implemented the task. A reviewer found issues to
address.

Re-read the original task contract first — it remains the bar.
Address blocking issues before stylistic or speculative ones.
Edit narrowly; leave unrelated file content unchanged. In the
handoff, map each change to the finding it addresses.

Search with bounded tools. Prefer `rg` over recursive `grep` or
`find ... -exec cat`; use scoped paths, `--max-filesize 1M`, and
`--max-columns 500` for broad searches. Avoid unbounded `cat`/read
of large or generated files. Unless the task explicitly needs them,
exclude generated/vendor trees and bundles such as `node_modules`,
`.venv`, `Library`, `obj`, `bin`, `dist`, `build`, `vendor`,
`*.min.js`, `*.map`, and `*.map.js`.

If a finding is wrong, out of scope, or conflicts with another,
say so explicitly. Do not silently skip and do not guess which
conflicting finding to honor — surface the conflict so a human
can decide.
