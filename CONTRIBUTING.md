# Contributing

Thanks for helping improve Orcho.

## Contribution License

By contributing to this repository, you agree that your contribution is
licensed under the Apache License, Version 2.0.

## Developer Certificate of Origin

Orcho uses the Developer Certificate of Origin (DCO) for contribution
provenance. Every commit in a pull request must include a `Signed-off-by`
line.

Use Git's sign-off flag when committing:

```bash
git commit --signoff -m "feat: describe the change"
```

or, in short form:

```bash
git commit -s -m "feat: describe the change"
```

The sign-off means you certify that you wrote the contribution, or otherwise
have the right to submit it under the repository's license.

---

## Dev Setup

```bash
git clone <repo>
cd orcho-core

python -m venv .venv
source .venv/bin/activate

pip install -e ".[dev]"
```

That's it. No external services needed — all tests use mock agents.

---

## Running Tests

```bash
# Full suite
python -m pytest tests/ -v

# By category
python -m pytest tests/unit/        # unit tests
python -m pytest tests/integration/ # integration tests
python -m pytest tests/acceptance/  # end-to-end with mock provider

# Specific test file
python -m pytest tests/unit/core/io/test_git_helpers_worktree.py -v

# Fast feedback (stop on first failure)
python -m pytest tests/ -x --tb=short
```

---

## Mock Providers

**Never write tests that call real APIs.** Use the built-in mock providers:

```python
from agents.runtimes import MockAgentProvider, FailingMockProvider

# Standard mock — returns canned responses
provider = MockAgentProvider()

# Simulates N failures before success — for retry tests
provider = FailingMockProvider(fail_times=2, error_type="rate_limit")

# Pass to run_pipeline
run_pipeline(..., provider=provider)
```

---

## Project Structure

```
orcho-core/
├── agents/                ← Agent layer
│   ├── entities.py        ← Dataclasses: ImplementationPlan, AtomicTask, etc.
│   ├── protocols.py       ← Protocol: IAgentRuntime
│   ├── registry.py        ← PhaseAgentConfig, AgentRegistry
│   └── runtimes/          ← Concrete runtimes and mock providers
│       ├── claude.py      ← ClaudeAgent (CLI wrapper)
│       ├── codex.py       ← CodexAgent (CLI wrapper)
│       ├── gemini.py      ← GeminiAgent (CLI wrapper)
│       └── _strategy.py   ← AgentProvider, MockAgentProvider, FailingMockProvider
├── cli/                   ← `orcho` CLI facade
├── core/
│   ├── _config/           ← Packaged defaults
│   ├── _prompts/          ← Core prompt templates
│   ├── contracts/         ← Plan/review/release schemas
│   ├── infra/             ← Config, platform, binary discovery
│   ├── io/                ← Retry, git helpers, prompt loader, transcript
│   ├── observability/     ← Events, metrics, logging, pricing
│   └── context/           ← Codemap / repo map builder
├── pipeline/
│   ├── project_orchestrator.py ← Single-project pipeline entry
│   ├── cross_project/     ← Cross-project planning, dispatch, gates
│   ├── runtime/           ← Profiles, steps, state, runner
│   ├── prompts/           ← Prompt composer and protected contracts
│   ├── control/           ← Handoff, resume, operator decisions
│   ├── engine/            ← Sessions, logging, worktrees, run diff
│   ├── evidence/          ← Evidence bundles
│   └── profiles/          ← Profile loading and validation
├── sdk/                   ← Typed headless API
├── tests/
│   ├── unit/              ← Per-module unit tests
│   ├── sdk/               ← Public SDK contract tests
│   ├── integration/       ← Pipeline flow tests
│   └── acceptance/        ← End-to-end mock flow tests
└── docs/                  ← Documentation
```

---

## Adding a New Feature

1. **Core module** → add it under the relevant package and mirror it in `tests/unit/<domain>/`
2. **Pipeline integration** → wire through `pipeline/runtime/`, `pipeline/phases/`, or `pipeline/project/` as appropriate
3. **CLI** → add subcommand or flag to `cli/orcho.py` or a helper module if user-facing
4. **Tests** → unit tests are mandatory; integration tests for pipeline changes
5. **Docs** → update relevant doc in `docs/`

### Test conventions

```python
# Use MockAgentProvider — never real API
# Use tmp_path fixture (pytest built-in) for temp files
# Use monkeypatch for config/env vars

class TestMyFeature:
    def test_happy_path(self, tmp_path: Path) -> None:
        ...

    def test_error_case(self) -> None:
        ...
```

---

## Commit Conventions

```
feat(scope): Short description in English

- Bullet list in Russian describing what changed
- Второй пункт
- Третий пункт
```

Scopes: `retry`, `metrics`, `cli`, `checkpoint`, `prompts`, `pipeline`, `agents`, `docs`, `tests`

---

## Error Recovery

The retry system in `core/io/retry.py` classifies errors automatically:

| Error class | Trigger | Default retries |
|---|---|---|
| `RateLimitError` | "429", "rate_limit_exceeded" | 4 |
| `ApiTimeoutError` | subprocess timeout, "timed out" | 2 |
| `ContextOverflowError` | "context_length_exceeded" | 1 |
| `AgentCallError` | any other failure | 2 |

```python
from core.io.retry import call_with_retry, RateLimitError

result = call_with_retry(
    fn=my_agent_call,
    error_type="rate_limit",
    phase="build",
    _sleep=lambda s: None,   # instant in tests
)
```

---

## Metrics

`MetricsCollector` in `core/observability/metrics.py` tracks tokens and duration per phase:

```python
from core.observability.metrics import MetricsCollector

m = MetricsCollector(default_model="claude-sonnet-4-6")
m.record_phase("build", prompt=prompt_text, output=result, duration_s=28.1)
m.add_round()          # increment fix round counter
m.save(output_dir)     # writes metrics.json
print(m.summary_line())
```

Token estimation: `len(text.encode("utf-8")) // 4` (±15% accuracy, no API needed).

---

## Prompt Resolution

```python
from core.io.prompt_loader import render_prompt, resolution_chain

# Render with project override support
text = render_prompt("tasks/build",
                     project_dir="/path/to/project",
                     task="...", tech_stack="...")

# Debug which file wins
chain = resolution_chain("tasks/build", project_dir="/path/to/project")
for level, path, exists in chain:
    print(f"[{level}] {path} {'✅' if exists else '○'}")
```
