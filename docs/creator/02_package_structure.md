# Package Structure

## File tree

```
orcho-core/
│
├── cli/
│   └── orcho.py                        CLI facade (orcho run / cross / status…)
│
├── pipeline/
│   ├── project_orchestrator.py         Single-project pipeline
│   ├── cross_project/                  Cross-project planning, dispatch, gates
│   ├── runtime/                        Profiles, steps, state, runner
│   ├── prompts/                        Composable prompt parts and contracts
│   ├── control/                        Handoff, resume, operator decisions
│   ├── engine/
│   │   ├── __init__.py
│   │   ├── session.py                  init_session(), save_session()
│   │   ├── run_logging.py              setup_run_logging()
│   │   ├── worktree.py                 per-run worktree management
│   │   ├── run_diff.py                 diff capture/rendering
│   │   └── hypothesis.py               run_hypothesis_loop(), maybe_run_hypothesis()
│   ├── evidence/                       Evidence bundle collection/rendering
│   ├── profiles/                       Profile loading/validation
│   ├── sandbox/                        Subprocess isolation
│   ├── skills/                         Skill discovery/injection
│   ├── plugins.py                      PluginConfig + load_plugin()
│   └── checkpoint.py                   SQLite checkpoint store
│
├── core/
│   ├── __init__.py                     Core package marker
│   ├── _prompts/                       Core prompt templates (*.md)
│   ├── _config/
│   │   ├── config.defaults.json        Defaults (in git)
│   │   └── config.local.json           Local overrides (gitignored)
│   ├── contracts/                      Plan/review/release schemas
│   ├── infra/
│   │   ├── __init__.py
│   │   ├── config.py                   AppConfig, models, _find_binary, _wrap_windows_cmd
│   │   ├── paths.py                    CONFIG_DIR, PROMPTS_DIR, SOURCE_ROOT
│   │   └── platform.py                 engine_home, runspace_dir, binary candidates
│   ├── observability/
│   │   ├── __init__.py
│   │   ├── logging.py                  banner, warn, success, C (colors)
│   │   ├── metrics.py                  MetricsCollector, load_historical_runs
│   │   └── trace.py                    vtrace, vdump, vtimed
│   ├── io/
│   │   ├── __init__.py
│   │   ├── retry.py                    call_with_retry, RetryPolicy
│   │   ├── git_helpers.py              has_uncommitted, git_diff_stat
│   │   └── prompt_loader.py            render_prompt (3-level resolution)
│   └── context/
│       └── __init__.py                 build_repo_map, inject_context
│
├── agents/
│   ├── __init__.py                     _stream_run (subprocess streaming)
│   ├── protocols.py                    IAgentRuntime
│   ├── entities.py                     ImplementationPlan, ReviewResult
│   ├── stream_parsers/                 Runtime JSONL parsers
│   └── runtimes/
│       ├── claude.py                   ClaudeAgent
│       ├── codex.py                    CodexAgent
│       ├── gemini.py                   GeminiAgent
│       └── _strategy.py                MockAgentProvider, FailingMockProvider
│
├── sdk/                                Typed headless API for tools/embedders
│
├── tests/
│   ├── conftest.py                     Shared fixtures
│   ├── unit/                           Isolated module tests
│   ├── sdk/                            Public SDK contract tests
│   ├── integration/                    Pipeline tests with MockProvider
│   └── acceptance/                     End-to-end with the full mock flow
│
├── shell/
│   ├── orcho-env-base.sh               Bash/Zsh env setup
│   └── orcho-env-base.ps1              PowerShell env setup
│
├── docs/                               Documentation (this file)
├── pyproject.toml                      Package metadata + entry points
└── CONTRIBUTING.md                     Contributor guide
```

## Naming conventions

| What | Convention | Example |
|----|-----------|--------|
| Modules | `snake_case` | `prompt_loader.py` |
| Classes | `PascalCase` | `ClaudeAgent` |
| Private | `_underscore` | `_find_binary`, `_IS_WINDOWS` |
| Constants | `UPPER_SNAKE` | `CONFIG_DIR`, `PROMPTS_DIR`, `CODEX_MODEL` |
| Entry points | `orcho-*` | `orcho-run`, `orcho-cross` |

## How core/ is organized

Each subdomain (`infra/`, `observability/`, `io/`, `context/`) has its own `__init__.py` that re-exports the public API.
Import through stable package boundaries when the subdomain already provides
such an API. For leaf helpers with a clear owning scope, a direct import from
the specific module is acceptable.

```python
# Correct
from core.infra import config
from core.io.prompt_loader import render_prompt
from core.observability.logging import banner

# Fragile under refactoring: private helpers must not become external API
from core.infra.config import CLAUDE_TIMEOUT
```
