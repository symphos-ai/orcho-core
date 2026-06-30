# Contributor Guide

## Quick start for development

```bash
# 1. Fork or clone the dev repo
git clone git@github.com:symphos-ai/orcho-core.git ~/www/orcho
cd ~/www/orcho

# 2. Create a venv
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 3. Make sure the tests are green
pytest tests/ -q

# 4. Create a feature branch
git checkout -b feature/my-feature
```

---

## Git Flow

```
main           ← stable (GitHub, always green)
  └─ feature/* ← development (~/www/orcho)
  └─ fix/*     ← hotfixes
  └─ docs/*    ← documentation only
```

**Rules:**
- `main` never breaks — tests are mandatory before a PR
- Commits follow Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`)
- One PR = one logical task

---

## Code standards

### Python

- Python 3.12+, type hints everywhere
- Formatter: `ruff` (configured in `pyproject.toml`)
- No external dependencies in `core/` (stdlib only)
- `agents/` and `pipeline/` may depend on `core/`
- Private functions get a `_` prefix

```bash
# Check style
ruff check .
ruff format --check .
```

### Tests

- Every new public module → at least one test
- Never use real Claude/Codex in tests
- `tmp_path` for the filesystem, `monkeypatch` for env vars
- A test must work without internet and without the Claude CLI

### Documentation

- Structure: `docs/user/` → `docs/expert/` → `docs/creator/`
- New public API → update `docs/expert/` or `docs/creator/`
- UX change → update `docs/user/`

---

## Workflow for adding a new pipeline step

1. `pipeline/runtime/steps.py` / `pipeline/runtime/profile.py` → describe the step shape if a new execution surface is needed.
2. `pipeline/phases/` or the `orcho.phases` plugin entry point → register the handler.
3. `core/_prompts/tasks/` and `pipeline/prompts/` → add prompt wiring if the step invokes an agent.
4. `pipeline/profiles/loader.py` / bundled profile JSON → add the phase to the relevant profile.
5. `tests/unit/pipeline/<domain>/` → cover the handler/profile/runtime contract.
6. `docs/expert/04_pipeline_phases.md` or `docs/guides/` → describe the public authoring surface.

---

## Workflow for adding a new provider

1. `agents/runtimes/my_provider.py` → implement the runtime.
2. `agents/runtimes/_strategy.py` → add mock/factory wiring if needed.
3. `core/infra/platform.py` → `my_candidates() -> list[str]`
4. `core/infra/config.py` → `get_my_bin() -> str`
5. `tests/unit/agents/test_my_provider.py` → unit tests
6. `docs/creator/03_agent_contracts.md` → document it

---

## Updating stable after merge

The easiest path is the [`orcho-promote`](./09_dev_workflow.md) shortcut, which
pushes DEV, pulls into STABLE, and reinstalls dependencies in one call.
The full dual-venv workflow is described in
[`09_dev_workflow.md`](./09_dev_workflow.md).

If you do it by hand:

```bash
# After the PR is merged into main:
cd ~/.local/share/orcho-core
git pull origin main

# Reinstall if pyproject.toml changed
pip install -e ".[web]"
```
