# pipeline/engine/ — DRY core

## Why

Single-project and cross-project execution share infrastructure for:
- session initialization
- logging setup
- research loop
- checkpoint logic

`pipeline/engine/` owns these shared runtime services. The single-project entry
is `pipeline/project_orchestrator.py`; cross-project execution lives under
`pipeline/cross_project/`.

---

## RunContext (engine/context.py)

An immutable dataclass, created once in `init_session()` and passed through every phase.

```python
@dataclass(frozen=True)
class RunContext:
    run_id: str                # YYYYMMDD_HHMMSS
    output_dir: Path           # runspace/runs/{run_id}/
    plan_model: str
    implement_model: str
    repair_model: str
    review_model: str
    dry_run: bool              # if True — no real API calls
    max_rounds: int            # number of repair_changes → review_changes iterations
    profile_name: str          # advanced | lite | enterprise | plan | task | review | custom
    session_mode: SessionMode  # AUTO | FRESH | RESUME
    plugin: PluginConfig | None
```

**Principle:** RunContext is not config; it is the runtime state of a single run.
Config (`AppConfig`) exists independently and is read once at startup.

---

## session.py

```python
def init_session(runspace_dir: Path, cli_args: Namespace) -> RunContext:
    """Create output_dir, generate run_id, assemble RunContext."""
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = runspace_dir / "runs" / run_id
    output_dir.mkdir(parents=True)
    return RunContext(run_id=run_id, output_dir=output_dir, ...)

def save_session(ctx: RunContext, phases: dict[str, PhaseResult]) -> None:
    """Save meta.json and metrics.json after completion."""
    (ctx.output_dir / "meta.json").write_text(json.dumps({
        "run_id": ctx.run_id,
        "status": _overall_status(phases),
        "phases": {k: v.to_dict() for k, v in phases.items()},
    }, indent=2))
```

---

## run_logging.py

```python
def setup_run_logging(ctx: RunContext) -> tuple[Path, Path]:
    """Set up two log files for the run.

    Returns: (progress_log, output_log)

    progress.log — for `tail -f` during the run (brief progress)
    output.log   — full agent output (verbose)
    """
```

Uses the `tee` pattern: output goes to stdout and the file simultaneously.

---

## research.py

```python
def maybe_run_hypothesis(
    *,
    task: str,
    cwd: str,
    codemap: str,
    dry_run: bool,
    plan_agent: IAgentRuntime,
    qa_agent: IAgentRuntime,
    prompt_spec=None,
    hypothesis_format: str | None = None,
    override_enabled: bool | None = None,
    override_max_attempts: int | None = None,
) -> tuple[str | None, list[dict]]:
    """Run the research phase if enabled in config.

    Returns (hypothesis | None, attempts). The hypothesis is added to the
    PLAN phase context. Both agents (planner + QA) are ordinary
    `IAgentRuntime` instances; the role is defined by prompt composition,
    not by type.
    """
```

---

## checkpoint.py

SQLite-backed store. Enables `--resume` of interrupted runs.

```python
class CheckpointStore:
    def __init__(self, db_path: Path): ...
    def save(self, phase: str, status: str, output: str = "") -> None: ...
    def load_completed(self) -> set[str]: ...  # set of phases with status="ok"
    def is_completed(self, phase: str) -> bool: ...
```

**In the orchestrator:**
```python
store = CheckpointStore(ctx.output_dir / "checkpoints.db")
if not store.is_completed("build"):
    output = claude.run(build_prompt, cwd=project_dir)
    store.save("build", "ok", output)
```

---

## Adding a new phase

1. Register the phase in `PhaseRegistry`.
2. Describe it as a `PhaseStep` inside a v2 `Profile`.
3. Add checkpoint/session adapter wiring if the phase writes durable state.
4. Add a prompt in `_prompts/my_phase_name.md`.
5. Cover it with a test in `tests/integration/`.
