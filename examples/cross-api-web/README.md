# cross-api-web — orcho REA-3.6 cross-project demo

Tiny two-repo fixture used to exercise orcho's **cross-project** pipeline
end-to-end. Companion to [`golden-api/`](../golden-api/) (single-project)
and aligned with the architecture doc at
[`docs/architecture/cross_project_pipeline.md`](../../docs/architecture/cross_project_pipeline.md).

## What's in here

```
cross-api-web/
├── api/                 # producer
│   ├── demo_server.py   # API-owned HTTP + SQLite demo server
│   ├── api/payload.py   # emits "email"             ← canonical API field
│   └── tests/…          # passes locally
└── web/                 # Vue + TypeScript consumer
    ├── src/contracts.ts # sends/reads "email_address" ← contract drift
    ├── src/main.ts      # frontend app
    └── tests/…          # passes locally with node --test
```

The bug is **only visible when the two repos are reviewed together**.
Each project's own local suite is green: the API repo uses pytest, and
the web repo uses Node's built-in test runner. That's the whole point of
the cross-project contract check — orcho's `CONTRACT_CHECK` reviewer
reads the diff across both projects, sees the field-name mismatch, and
emits a typed `REJECTED` review with a P1 finding and a `required_fix`.

`examples/scripts/bootstrap_demo_1b.sh` also seeds a realistic skill
roster so the CLI can show skill management across phases:

* workspace: `team-lead` for product-level planning, routing, and final
  evidence
* api project: `backend-python` for implementation and `backend-qa` for
  pytest coverage
* web project: `frontend-vuejs` for Vue.js/TypeScript implementation and
  `frontend-qa` for frontend contract tests

The live demo runtime keeps ownership aligned with the project split:
`api/demo_server.py` runs the REST API and SQLite storage from inside
the API repo, while the web repo uses Vite (`npm run dev`) and proxies
`/api` to that API server.

## One-command run (mock provider)

```bash
cd /path/to/orcho-core
ORCHO_WORKSPACE=/tmp/orcho-cross-demo \
  python -m pipeline.cross_project.orchestrator \
    --projects api:examples/cross-api-web/api web:examples/cross-api-web/web \
    --task "Align user payload contract between api and web" \
    --mock \
    --run-id CROSS_DEMO
```

Outputs land under `${ORCHO_WORKSPACE}/runspace/runs/CROSS_DEMO/`:

* `cross_plan.md` — single cross-project plan with per-alias subtasks
* `events.jsonl` — append-only event spine for the **whole** cross run,
  including each child run's `run.start` (with `parent_run_id` +
  `project_alias` linkage — see below)
* `meta.json` — cross session shape: `phases.projects.<alias>` per child,
  `phases.contract_check.<alias>` typed reviewer contract
* `progress.log` — human-readable timeline
* `<alias>/` — per-project artifact dirs (one per `--projects` alias),
  each containing the child's `meta.json`, `output.log`, plan files, etc.

## What to look at after the run

### 1. Typed cross-project reviewer contract (REA-3.5)

Each entry in `meta.json -> phases.contract_check[alias]` carries the
typed reviewer payload, not prose:

```json
{
  "verdict": "REJECTED",
  "short_summary": "P1: api expects/emits 'email' but web sends/reads 'email_address'.",
  "findings": [
    {
      "id": "F1",
      "severity": "P1",
      "title": "Field name drift between api producer and web consumer",
      "file": "web/src/contracts.ts",
      "line": 18,
      "body": "...",
      "required_fix": "Use the API's canonical user email field in the web contract."
    }
  ],
  "rendered": "# Contract Check — api\n...",
  "raw_response": "{\"verdict\":\"REJECTED\",...}"
}
```

`rendered` is the markdown orcho generates from the parsed JSON;
`raw_response` is the model's exact JSON for re-validation. Malformed
structured output is downgraded to `REJECTED` with a `parse_error`
field rather than silently degrading into prose parsing.

### 2. Child-run linkage (REA-3.6)

Open `events.jsonl` in the cross run dir and grep for `run.start`. The
first event is the cross run; each child carries the linkage pair:

```bash
$ jq -c 'select(.kind=="run.start") | .payload' events.jsonl
{"run_kind":"cross_project","projects":[{"alias":"api",...},{"alias":"web",...}],"cross_mode":"full",...}
{"run_kind":"single_project","project":".../api","profile":"task","parent_run_id":"CROSS_DEMO","project_alias":"api"}
{"run_kind":"single_project","project":".../web","profile":"task","parent_run_id":"CROSS_DEMO","project_alias":"web"}
```

Together those let an MCP / dashboard / evidence consumer rebuild the
parent → children → phases tree from the event stream alone, no
filesystem layout required.

## Why mock-only by default?

Same rationale as `golden-api/`. The mock provider keeps this
deterministic and runs in under a second, which is what we want for
acceptance tests and demo videos. Real Claude / Codex runs follow the
same pipeline shape — drop the `--mock` flag to invoke them — but
introduce latency, non-determinism, and API costs that don't belong in
CI.
