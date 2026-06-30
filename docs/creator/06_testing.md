# Testing Strategy

## Test levels

```
tests/
├── unit/           Isolated tests. No subprocesses, no files.
├── sdk/            Public SDK contract and schema tests.
├── integration/    Pipeline tests with MockProvider. Create temp dirs.
└── acceptance/     End-to-end with the full mock flow through the CLI.
```

**Rule**: a test must work without the Claude CLI, the Codex CLI, and without network access.

---

## Running

```bash
# All tests
pytest tests/ -q

# Unit only
pytest tests/unit/ -v

# With coverage
pytest tests/ --cov=. --cov-report=term-missing

# A specific test
pytest tests/unit/core/test_platform_windows.py::test_wrap_windows_cmd -v
```

---

## MockAgentProvider

The foundation of all integration tests:

```python
# conftest.py — available to all tests
@pytest.fixture
def mock_run(tmp_path, monkeypatch):
    """Full mock pipeline run in tmp_path."""
    monkeypatch.setenv("ORCHO_RUNSPACE", str(tmp_path / "runspace"))
    provider = MockAgentProvider(
        plan_response="## Plan\n- Step 1",
        build_response="✓ Implemented",
        review_response="LGTM. No issues.",
    )
    # Pass the provider into run_pipeline(...) or a targeted helper.
    return provider
```

---

## Unit test patterns

### Test with monkeypatched env

```python
def test_config_model_from_env(monkeypatch):
    monkeypatch.setenv("MODEL_PLAN", "claude-test-model")
    from core.infra.config import _reset_config, AppConfig
    _reset_config()
    cfg = AppConfig.load()
    assert cfg.phase_model_map["plan"] == "claude-test-model"
    _reset_config()  # cleanup
```

### Binary test with tmp_path

```python
def test_find_binary_finds_in_candidates(tmp_path, monkeypatch):
    fake = tmp_path / "claude"
    fake.touch()
    from core.infra.config import _find_binary
    result = _find_binary("claude", [str(fake)])
    assert result == str(fake)
```

### Platform test with reload

```python
def test_platform_windows_candidates(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", r"C:\Users\test\AppData\Roaming")
    import importlib, core.infra.platform as m
    importlib.reload(m)   # recompute _IS_WINDOWS
    assert any(".cmd" in c for c in m.claude_candidates())
```

---

## What must be covered

| Module | What to test |
|--------|----------------|
| `core.infra.config._find_binary` | env var override, PATH, candidates, not found |
| `core.infra.config._wrap_windows_cmd` | .cmd on win32, .exe on win32, unix |
| `core.infra.platform.claude_candidates` | win32 vs unix |
| `core.io.prompt_loader.render_prompt` | resolution across all 3 levels |
| `core.io.retry.call_with_retry` | success, retry on rate_limit, ContextWindowError is not retried |
| `pipeline` | MockProvider flow, checkpoint skip on resume |

---

## Adding a new test

```bash
# 1. Pick the right level (unit/integration/acceptance)
# 2. Create the file tests/{level}/test_{module}.py
# 3. Run it and make sure it is green under CI conditions (no CLI)
pytest tests/unit/<domain>/test_my_module.py -v
# 4. Add pytest markers if needed (slow, windows, etc.)
```
