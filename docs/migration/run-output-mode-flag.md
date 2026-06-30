# Migration: `--verbose` / `--stream-output` → `--output {summary,live,debug}`

> **TL;DR:** the Orcho run flags for transcript output have collapsed into
> one knob, `--output {summary,live,debug}`. Legacy `--verbose` / `-v` and
> `--stream-output` remain as aliases with no deprecation warning.
> **Behavior change:** `--verbose` (= `--output debug`) now includes the
> live agent transcript. Before the normalization `--verbose` showed only
> trace and untruncated previews, without agent stdout. After it, `debug`
> is a monotonic superset of `live`, and the live agent stream comes
> together with trace.

## CLI: `orcho run` and `orcho cross`

| Pre-normalization | After | Behavior |
|---|---|---|
| (no flags) | `--output summary` (default) | phase banners + structured blocks + outcome |
| `--stream-output` | `--output live` | summary + live agent stream on stdout |
| `--verbose` / `-v` | `--output debug` | live + stderr trace + untruncated previews |
| `--verbose --stream-output` | `--output debug` | same as plain `--verbose` now (the stack is monotonic) |
| `--verbose` **without** `--stream-output` | `--output debug` | **change:** now includes the agent stream |

`argparse` last-on-CLI wins: `--verbose --output summary` gives
`summary`, and `--stream-output --verbose` gives `debug`. Behavior is
symmetric between `orcho run` and `orcho cross` — the old
`cross --verbose`, which only enabled trace and not untruncated previews
(a bug), now goes through the single `apply_output_mode(args.output)`.

## Behavior change for `--verbose`

Before the normalization:

- `--verbose` = `enable_trace()` + `set_verbose(True)` (untruncated phase
  previews). The live agent stream **was not enabled** — that was the
  separate `--stream-output` axis.

After:

- `--verbose` = alias for `--output debug` = live agent stream + trace +
  untruncated previews.

If your scenario specifically relied on "trace without the agent
stream" (for example CI logs where you do not want agent JSONL in the
transcript), you must either:

- run without `--verbose` and accept that previews are truncated
  (700-char truncation) and trace is off — `--output summary`;
- OR accept that `debug` now includes the agent stream — this is
  intentional, the stack is monotonic.

There is no in-between option (trace only, no agent echo) in the new
contract.

## SDK / MCP / Web

| Surface | Before | After |
|---|---|---|
| `pipeline.argv.build_orch_argv()` | kwargs `verbose: bool`, `stream_output: bool` | new kwarg `output_mode: str = "summary"`. The old booleans remain as shims (`verbose=True` → debug; `stream_output=True` → live) — rewriting SDK callers is not required. |
| MCP `orcho_run_start` tool | the flag was not passed through | new parameter `output_mode: Literal["summary","live","debug"] = "summary"`. Default `summary`. |
| MCP `RunsSupervisor.spawn` | no output flags in argv | accepts `output_mode: str = "summary"`, passes it to `build_orch_argv`. Resume defaults to `summary` (it does not inherit the original mode). |
| Web `runner.stream_pipeline` / `build_extra_args` | no output flags | accepts `output_mode: str = "summary"`. The Expert UI dashboard adds a `summary/live/debug` selectbox. |
| `cli/orcho.py` argparse | `--verbose action=store_true`, `--stream-output action=store_true` | `--output choices=(summary,live,debug)`; `--verbose`/`-v` and `--stream-output` remain as `store_const dest='output'`. |

The schema snapshots (`docs/sdk_schema.json`,
`orcho-mcp/docs/mcp_schema.json`) have been regenerated.

## Internal contract

The single switch point is
`core.observability.logging.apply_output_mode(mode)`:

```python
def apply_output_mode(mode: str | None) -> OutputMode:
    output_mode = normalize_output_mode(mode)  # "summary" | "live" | "debug"
    trace.enable_trace(output_mode == "debug")
    set_stdout_echo(output_mode in {"live", "debug"})
    set_verbose(output_mode == "debug")
    return output_mode
```

The orchestrators call one line instead of three separate flips.
`pipeline/project_orchestrator.py` and
`pipeline/cross_project/orchestrator.py` go through the same helper —
the cross bug (where `--verbose` flipped only trace, without
`set_verbose(True)`) disappeared automatically as a side effect.

## Compatibility

- **Scripts using `--verbose` / `--stream-output`** keep working
  unchanged. No deprecation warnings. But `--verbose` is now noisier
  than before the normalization (see the behavior change above).
- **SDK callers** using `build_orch_argv(verbose=True)` or
  `(stream_output=True)` keep working through the legacy shim. The
  canonical form is `output_mode="debug"` / `"live"`.
- **MCP clients** without the `output_mode` parameter get `summary` —
  exactly the de-facto behavior before (the old supervisor did not pass
  verbose/stream at all).
- **Web dashboard** defaults to `summary` in Quick mode. The Expert
  selectbox gives an explicit choice.

## Files updated by this migration

- `cli/orcho.py` — argparse `_add_common_run_args`.
- `core/observability/logging.py` — `apply_output_mode`, `normalize_output_mode`, the `OutputMode` literal.
- `pipeline/argv.py` — the `output_mode` kwarg + the legacy boolean shim resolver.
- `pipeline/project_orchestrator.py` + `pipeline/cross_project/orchestrator.py` — one `apply_output_mode(args.output)` line instead of scattered flips.
- `sdk/runner.py` — `build_orch_argv_from_args` and `run_cross_from_args` pass the new kwarg through.
- `docs/sdk_schema.json` — regenerated snapshot.
- `orcho-mcp/orcho_mcp/supervisor.py` + `orcho-mcp/orcho_mcp/tools.py` — `output_mode` through `spawn` and `orcho_run_start`.
- `orcho-web/orcho_web/services/runner.py` + `orcho_web/views/run.py` — `output_mode` in `stream_pipeline` / `build_extra_args` + the Expert UI selectbox.
- `docs/user/02_commands.md` + `docs/creator/05_core_subdomains.md` —
  user and creator documentation rewritten for `--output`.
