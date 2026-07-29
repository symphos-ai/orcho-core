# Deterministic mock harness

Orcho can exercise a complete false-ready delivery loop without calling a
model provider:

```text
implement reports done
  -> review_changes rejects
  -> repair_changes writes a correction
  -> review_changes approves
  -> final_acceptance closes the run
```

This is a deterministic harness for the delivery protocol. It is useful when
you need a repeatable run for CLI or SDK integration tests, release-candidate
smokes, observability debugging, documentation, or terminal recordings.

It does not evaluate model quality. The worker responses and blocker are
synthetic; the lifecycle, run state, artifacts, review/repair routing, evidence,
and final summary are real Orcho surfaces.

## CLI

Bootstrap the packaged golden fixture:

```bash
orcho demos bootstrap golden-api
```

Then run the command printed by the bootstrap, or invoke the equivalent
directly:

```bash
orcho run \
  --task "Fix validation bug in sample API" \
  --project /tmp/orcho_demo_1a/project \
  --workspace /tmp/orcho_demo_1a/workspace-orchestrator \
  --profile feature \
  --mock \
  --mock-review-reject 1 \
  --max-rounds 2 \
  --output live
```

`--mock-review-reject N` makes the first `N` `review_changes` calls return a
typed rejected verdict. The following repair attempt writes a deterministic
artifact, and review switches to approval after the rejection budget is
exhausted. Set `--max-rounds` high enough to permit the requested rejection
and a later approval.

The flag only has meaning with `--mock`. With `N=0`, the mock reviewer approves
its first review.

## SDK

The detached launch SDK exposes the same control:

```python
from sdk.run_control.launch import LaunchSpec, launch_run

result = launch_run(
    LaunchSpec(
        project_dir="/tmp/orcho_demo_1a/project",
        workspace="/tmp/orcho_demo_1a/workspace-orchestrator",
        task="Fix validation bug in sample API",
        profile="feature",
        mock=True,
        mock_review_reject=1,
        max_rounds=2,
        output_mode="live",
    )
)

exit_code = result.popen.wait(timeout=120)
assert exit_code == 0
print(result.run.run_id, result.run.run_dir)
```

Embedders can use `result.run.run_id` and `result.run.run_dir` to inspect the
same durable state that the CLI commands read.

## What to assert

A useful harness check should inspect product surfaces, not only the process
exit code:

1. streamed output contains a rejected `review_changes` verdict;
2. a `repair_changes` phase runs after that rejection;
3. the next review approves;
4. final acceptance reports `ship_ready`;
5. evidence retains the original finding and required fix;
6. the final summary reports both review and repair as successful;
7. the run directory contains the expected plan, events, diff, review, and
   evidence artifacts.

Use the public readers after the run:

```bash
orcho status --workspace /tmp/orcho_demo_1a/workspace-orchestrator
orcho evidence --format md \
  --workspace /tmp/orcho_demo_1a/workspace-orchestrator
orcho diff <run-id> --stat \
  --workspace /tmp/orcho_demo_1a/workspace-orchestrator
```

## Where it fits

| Use | What the harness gives you | What it does not replace |
| --- | --- | --- |
| CLI and SDK regression | Stable review/repair transitions and artifacts | Focused unit and integration tests |
| Release-candidate smoke | Fast installed-product check with no provider credentials | Provider-backed and project-specific release journeys |
| Observability debugging | Repeatable events, evidence, summaries, and exit state | Investigation of provider-specific failures |
| Documentation and recordings | Stable timing-independent story and redactable paths | A real-agent capability demonstration |

Treat it as the first rung of verification: cheap, deterministic, and broad
across Orcho surfaces. Follow it with the narrower or more expensive checks
needed for the release or integration under test.

## Related

- [Golden single-project CLI demo](../demos/demo-1a-single-project-cli.md)
- [Run artifacts](../reference/run_artifacts.md)
- [SDK API](../reference/sdk_api.md)
