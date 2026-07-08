# Examples

This directory contains source fixtures used by acceptance tests and demo
walkthroughs.

Do not run demos in place when the run may write project-local artifacts.
Use the bootstrap scripts in `examples/scripts/` to create disposable copies
under `/tmp/`.

For example:

```bash
examples/scripts/bootstrap_demo_1a.sh
```

That creates:

```
/tmp/orcho_demo_1a/project
/tmp/orcho_demo_1a/workspace-orchestrator
```
