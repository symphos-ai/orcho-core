# Agent Contracts

## Protocol (agents/protocols.py)

The agent contract is a single Protocol, `IAgentRuntime` (`invoke()` +
`reset_session()`). One runtime class implements it for all roles; the
orchestrator distinguishes roles through prompt composition and per-call
flags, not through the agent type. The former role-keyed protocols
(`IArchitectAgent` / `IDeveloperAgent` / `IReviewerAgent` /
`IHypothesizingArchitect`) have been removed — nothing equivalent remains
in the code.

```python
from agents.protocols import IAgentRuntime

class IAgentRuntime(Protocol):
    model: str
    session_id: str | None  # bridge handle; None for stateless runtimes
    _followup_resume_pending: bool
    _last_resumed_session_id: str | None
    _last_followup_parent_session_id: str | None

    def invoke(
        self,
        prompt: str,
        cwd: str,
        *,
        mutates_artifacts: bool = False,
        continue_session: bool = False,
        attachments: tuple[Attachment, ...] = (),
    ) -> str: ...

    def reset_session(self) -> None: ...
```

> `invoke` accepts `prompt: str` — this is the only point where the prompt
> exists as a string. Inside the prompt engine the canonical object is
> `PromptTurn` (ordered segment stream, ADR 0060); builders return
> `PromptTurn`, and the caller serializes it to a string via `turn.text`
> exactly at this boundary. The runtime Protocol itself does not change.

**Why a single Protocol:**
- The composer (`pipeline/prompts/composer.py`) builds the prompt and
  assembles it into the canonical `PromptTurn` (ADR 0060); the runtime is
  left with transporting `turn.text`.
- The same `ClaudeAgent` served all three former roles — separate
  Protocols duplicated the interface with no architectural gain.
- Plugin authors need to implement exactly one signature to publish a new
  runtime through the `orcho.agent_runtimes` entry-points group.

### Runtime construction

Orchestrators do not call `provider.claude(...)` / `provider.codex(...)`
directly. The single construction surface is:

```python
AgentProvider.resolve(runtime, model, *, effort=None)
```

`RealAgentProvider` internally delegates to the registry:

```python
AgentRegistry.resolve(model, runtime="claude", *, effort=None)
```

The argument order is intentionally different: the provider takes
`runtime` first, the registry takes `model` first. This comes from API
history and is pinned by tests; when adding a call site, check the order
explicitly.

Runtime constructors must be side-effect free. Creating
`PhaseAgentConfig`, loading config, listing profiles, and `dry_run=True`
must not require an installed CLI. The path to the external binary is
resolved lazily on the first real `invoke()` via `lazy_cli_binary`; the
`agent.bin` property remains settable for tests and runtime adapters.

### `invoke()` parameters

| Parameter | Semantics |
|---|---|
| `prompt` | Fully assembled text (the composer has already run). |
| `cwd` | Project working directory. |
| `mutates_artifacts` | `True` — the call may modify project files on disk (build / fix). `False` (default) — read-only. At the Claude level this maps to `--dangerously-skip-permissions` + `--permission-mode acceptEdits`. |
| `continue_session` | `True` + `session_id is not None` ⇒ `--resume <session_id>`. Otherwise a fresh start with capture of a new `session_id`. |
| `attachments` | **Multimodal only** (IMAGE / BINARY). TEXT has already been rendered into `prompt` by the composer — passing TEXT raises `ValueError`. |

### Session bridge

`session_id` lives on the runtime instance and survives any phase
boundaries — it is the "ongoing conversation" of that runtime.
Cross-runtime data flow happens **only through prompt content**: the
output of a phase on one bridge is embedded by the composer into the
prompt of the next phase on the other. Bridges never merge.

The current orchestrator policy chains the session
(`continue_session=True`) only on the BUILD → FIX edge; everything else
starts fresh. The runtime is nevertheless **bridge-capable** — a future
policy inversion (continue_session=True by default whenever
`session_id != None`) will require no Protocol changes.

### `reset_session()`

Explicitly burns the bridge: clears `session_id`. The next `invoke()`
starts fresh. The main use case is human-in-the-loop control: when the
loop pauses, the operator can choose "preserve bridge" (continue) or
"burn bridge" (`reset_session()` + a new `invoke()`).

---

## Concrete runtimes

### ClaudeAgent (`agents/runtimes/claude.py`)

```python
class ClaudeAgent:
    bin: str              # path to the claude CLI
    model: str            # current model
    effort: str | None    # low | medium | high | xhigh | max
    session_id: str | None
```

Specifics:
- All calls use `--output-format stream-json --verbose`
  (universal session capture; resume works in any mode).
- `mutates_artifacts=True` ⇒ adds
  `--permission-mode acceptEdits` + `--dangerously-skip-permissions`.
- `--verbose` is mandatory for non-interactive stream-json (Claude Code).
- Multimodal attachments enter the CLI args via `split_by_kind()` from
  `pipeline/attachment_inject.py`.

### CodexAgent (`agents/runtimes/codex.py`)

```python
class CodexAgent:
    bin: str
    model: str
    effort: str | None
    session_id: str | None
```

Specifics:
- All calls go through `codex exec --json` so the runtime can read
  stream events, usage, and the session bridge.
- `mutates_artifacts=False` ⇒ read-only sandbox.
- `mutates_artifacts=True` ⇒ Orcho's write-capable flags for
  non-interactive execution.
- `continue_session=True` + captured `session_id` ⇒ resume the current
  Codex bridge.
- `_safe_cwd()` resolves the nearest git root (Codex requires a
  git-trusted cwd).

### GeminiAgent (`agents/runtimes/gemini.py`)

```python
class GeminiAgent:
    bin: str
    model: str
    effort: str | None
    session_id: str | None
```

Specifics:
- Wraps `@google/gemini-cli` via
  `gemini -p <prompt> -m <model> -o stream-json --skip-trust
  --approval-mode <plan|yolo>`.
- `mutates_artifacts=False` ⇒ `--approval-mode plan`. This is the CLI's
  read-only mode: reviewer phases may read files but must not write.
- `mutates_artifacts=True` ⇒ `--approval-mode yolo`. Write phases run
  non-interactive; destructive git stays behind the runtime guardrail.
- `continue_session=True` + captured `session_id` ⇒ `-r <session_id>`.
  `continue_session=True` without a captured id is a no-op.
- `effort` is accepted for API parity and operator display, but Gemini
  CLI 0.40 has no public reasoning-effort flag, so the value does not
  change the invocation.
- `last_cost_usd = None`: the CLI does not report USD. Monetary
  estimation, if enabled, lives in the rate-card/accounting layer.
- Token math: `stats.cached` is a subset of `stats.input_tokens`, not an
  additional bucket. Therefore `last_tokens_in = input_tokens`,
  `last_tokens_in_fresh = max(0, input_tokens - cached)`,
  `last_tokens_in_cache_read = cached`.
- The shell-command guardrail depends on the Gemini CLI 0.40 tool name
  `run_shell_command` and reads the command from `parameters.command`.
  If a future CLI version drifts, this parser must be updated.

---

## Mock provider (`agents/runtimes/_strategy.py`)

`MockAgentProvider.resolve(runtime, model, effort=...)` is the same
construction surface as the real provider's. For `claude` and `gemini`
it returns a `_MockClaude` with a stamped `runtime`; for `codex` it
returns the singleton `_MockCodex` (important for the validate-plan
reject counter).

`_MockClaude.invoke()` dispatches behavior on `mutates_artifacts` plus
prompt-recognition heuristics:

| Condition | Behavior |
|---|---|
| `mutates_artifacts=True` | materializes files from the "### Modified files" block (as `_MockClaude.run` used to do). |
| read-only + prompt looks like a hypothesis | returns a hypothesis stub. |
| read-only + prompt looks like a plan / replan | returns plan markdown (the caller calls `parse_plan` itself if it needs a `ParsedPlan`). |
| everything else | generic read-only echo. |

`_MockCodex.invoke()`:

| Condition | Behavior |
|---|---|
| `mutates_artifacts=True` | `NotImplementedError` (Codex exec under `--mock` is not covered; pinned to Claude). |
| prompt contains a `plan_*.md` path | review_file branch with a reject/approve counter (`validate_plan_reject_rounds`). |
| everything else | review_uncommitted-style response. |

---

## Output contract: subtask done-criteria attestation (P7, ADR 0068)

Beyond the `IAgentRuntime` runtime protocol, `subtask_dag` defines an
**output contract** for the developer agent. For a subtask that has
`done_criteria`, the prompt gets a code-owned system block,
`subtask_attestation`, and after the usual human-readable output the agent
must append **exactly one** machine-readable JSON object — a
self-attestation for each criterion:

```json
{
  "type": "subtask_attestation",
  "subtask_id": "<id of the current subtask, verbatim>",
  "criteria": [
    {"index": 1, "criterion": "<criterion text>", "met": true,
     "evidence": "<one sentence: what was done / where>"}
  ],
  "summary": "<=280 characters, one-line summary"
}
```

Rules: the object comes **last**, exactly one, with no markdown fence; one
entry per criterion with `index` 1..N in `done_criteria` order (no gaps or
duplicates); `met=true` only if the criterion is actually satisfied.

Orcho validates **only shape and completeness** (schema + every criterion
`met=true`), but **not the truth** of the evidence — truthfulness is
checked by the quality gates (`tests` / `review_changes` /
`final_acceptance`). Binding is by **index**, not by text (the agent may
rephrase a criterion). A missing / broken / mismatched / not-all-met
attestation marks the subtask `incomplete` and blocks delivery. A subtask
without `done_criteria` gets no contract.

The mock provider closes the attestation from the "Current Executable
Subtask" block of the prompt
(`agents/runtimes/_strategy.py:_mock_subtask_attestation`), just like the
real agent. See `pipeline/prompts/contracts.py:subtask_attestation_contract`
and `core/contracts/subtask_attestation_schema.py`.

---

## Adding a new runtime

1. Create a class implementing `IAgentRuntime` (`invoke` +
   `reset_session` + the `model` / `session_id` fields).
2. Register it in the `orcho.agent_runtimes` entry-points group
   (see `pyproject.toml` for the built-in runtimes).
3. Make sure the constructor does not touch the CLI, the network, or
   config files outside the normal config load, and does not validate
   auth. All of that must happen on the first `invoke()`.
4. If the runtime must participate in smoke / snapshot tests under
   `--mock`, add a mock mapping in `_strategy.py`.
5. Per-phase override: set `runtime`/`model` in
   `_config/config.local.json` (`phases.<phase>.runtime` / `model`) or
   via the env vars `RUNTIME_<PHASE>` / `MODEL_<PHASE>`. Per-step
   override for DAG subtasks goes through `PhaseStep.overrides["runtime"]`.
