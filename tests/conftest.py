"""
tests/conftest.py — Shared pytest fixtures for unit and integration tests.
"""

import os
import subprocess
import sys
import tempfile
import textwrap
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Local runtime preferences are allowed for real developer runs, but tests
# pin the committed default config and golden snapshots.
os.environ.setdefault("ORCHO_DISABLE_LOCAL_CONFIG", "1")

# Make multiagent-core importable from any test subdirectory
sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.plugins import PluginConfig

# ── Helpers ───────────────────────────────────────────────────────────────────
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Attach routing markers from the test path.

    These marks are for fast, domain-oriented debug loops; they do not
    replace the full-suite readiness gate.
    """
    root = config.rootpath
    for item in items:
        rel = Path(item.path).relative_to(root).as_posix()
        marks = _markers_for_test_path(rel)
        for mark in marks:
            item.add_marker(getattr(pytest.mark, mark))


def _markers_for_test_path(rel: str) -> set[str]:
    marks: set[str] = set()
    parts = rel.split("/")

    if len(parts) > 1:
        if parts[1] in {"unit", "integration", "acceptance"}:
            marks.add(parts[1])
        elif parts[1] == "sdk":
            marks.update({"integration", "sdk"})

    if "/pipeline/engine/" in rel or "worktree" in rel or "pre_run_dirty" in rel:
        marks.add("worktree")
        marks.add("git_worktree")
    if "commit_delivery" in rel or "run_diff" in rel:
        marks.add("git_worktree")
    if "/pipeline/cross_project/" in rel or "/integration/cross/" in rel or "cross_" in rel:
        marks.add("cross_project")
    if "/pipeline/run_state/" in rel:
        marks.add("run_state")
    if "/pipeline/prompts/" in rel or "prompt" in rel or "golden" in rel:
        marks.add("prompts")
    if (
        "/agents/" in rel
        or "/pipeline/runtime/" in rel
        or "/pipeline/phases/" in rel
        or "/pipeline/sandbox/" in rel
    ):
        marks.add("runtime")
    if "/sdk/" in rel:
        marks.add("sdk")
    if "/cli/" in rel or "bootstrap" in rel:
        marks.add("cli")
    if "/evidence/" in rel or "evidence" in rel:
        marks.add("evidence")
    if "/profiles/" in rel or "profile" in rel:
        marks.add("profiles")
    if "/quality_gates/" in rel or "gate" in rel or "final_acceptance" in rel:
        marks.add("quality_gates")
    if (
        "/pipeline/project/" in rel
        or "test_pipeline.py" in rel
        or "checkpoint" in rel
        or "from_run_plan" in rel
        or "round_router" in rel
        or "full_mock_flow" in rel
        or "golden_scenario" in rel
    ):
        marks.add("project_run")
    if "full_mock_flow" in rel:
        marks.update({"cross_project", "quality_gates"})
    if "golden_scenario" in rel:
        marks.update({"evidence", "prompts"})
    if "packaging" in rel or "wheel" in rel:
        marks.add("packaging")

    # Cost axis: explain *why* a test is expensive (orthogonal to layer/serial).
    if (
        "full_mock_flow" in rel
        or "worktree_e2e" in rel
        or "golden_scenario" in rel
        or "demo_1b_bootstrap" in rel
        or "demo_bootstrap" in rel
        or rel.startswith("tests/e2e/")
    ):
        marks.add("e2e")
        marks.add("filesystem_heavy")
    if "checkpoint" in rel or "from_run_plan" in rel:
        marks.add("filesystem_heavy")
    if rel.endswith("test_subprocess_lifecycle.py") or rel.endswith("test_stream.py"):
        marks.add("slow_process")

    if _is_serial_test_path(rel):
        marks.add("serial")

    return marks


def _is_serial_test_path(rel: str) -> bool:
    serial_fragments = (
        "/acceptance/",
        "/integration/",
        "/pipeline/engine/",
        "/pipeline/project/",
        "/pipeline/cross_project/",
        "/pipeline/sandbox/",
        "worktree",
        "checkpoint",
        "from_run_plan",
        "full_mock_flow",
        "golden_scenario",
        "packaging",
        "subprocess",
        "stream",
        "run_id",
        "event_store",
        "progress_log",
    )
    return any(fragment in rel for fragment in serial_fragments)


def init_git_repo(path: Path) -> None:
    """Initialize ``path`` as a git repo with one committed file.

    The pipeline engine requires ``project_dir`` to be a real git repo
    so worktree isolation can attach. Fixtures that hand the engine a
    bare ``tmp_path`` would otherwise hit ``WorktreeConfigError``.
    Tests that build a project dir for ``run_pipeline`` should call
    this helper from their fixture rather than ``Path.mkdir`` alone.
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=path, check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@orcho.invalid"],
        cwd=path, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Orcho Test"],
        cwd=path, check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=path, check=True,
    )
    (path / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=path, check=True,
    )


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_output_mode_globals() -> Generator[None, None, None]:
    """Pin the global ``apply_output_mode`` state to ``summary`` around
    every test — at setup *and* teardown.

    ``apply_output_mode`` mutates module-level globals (``set_stdout_echo``,
    ``set_verbose``, ``trace.enable_trace``, ``_output_mode``). Tests that
    exercise a real CLI ``main()`` flip those globals based on
    ``args.output`` — and with ``cli.output_mode`` now configurable
    (default ``"live"`` in ``config.defaults.json``), the live-echo
    global state would otherwise bleed into later tests in the same
    session (notably ``tests/unit/pipeline/project/test_silent_boundary``
    which pins zero stdout/stderr under the SILENT presentation policy).

    The teardown reset is the common case, but it is not sufficient on
    its own: if a polluting test's teardown never runs — a setup-time
    error in another fixture, a foreign-worktree import path that raises,
    or any aborted teardown — the dirty live/debug mode survives into the
    next test. ``run_project_pipeline(presentation=SILENT)`` does not pin
    the output mode itself (only the CLI does, from ``args.output``), so a
    leaked live echo makes the SILENT contract leak the agent transcript
    to stdout and ``[TRACE]`` lines to stderr. Resetting at setup as well
    makes every test start from the documented ``summary`` baseline
    regardless of how the previous test ended.

    The reset is cheap and unconditional; tests that explicitly need
    live/debug mode call ``apply_output_mode`` inside their own body
    (after this setup reset) and are unaffected.
    """
    from core.observability.logging import apply_output_mode
    apply_output_mode("summary")
    yield
    apply_output_mode("summary")

@pytest.fixture
def default_plugin() -> PluginConfig:
    """Minimal plugin with no customisations (generic mode)."""
    return PluginConfig()


@pytest.fixture
def full_plugin() -> PluginConfig:
    """Plugin with all fields populated — generic example project."""
    return PluginConfig(
        name="Sample Project",
        language="Python",
        architecture="Service layer + Repository pattern",
        ma_artifacts_dir="_docs",
        file_hints=["src/", "tests/"],
        plan_prompt_extra="Preserve all existing public interfaces.",
        build_prompt_extra="Do not modify auto-generated files.",
        review_focus_extra="Check for unhandled exceptions in public methods.",
    )


@pytest.fixture
def task() -> str:
    return "Add pagination support to the user listing endpoint"


@pytest.fixture
def project_dir() -> Generator[str, None, None]:
    """Real temporary directory — cleaned up after each test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def project_dir_with_plugin(project_dir: str) -> str:
    """Temp project directory with a valid plugin.py installed."""
    plugin_code = textwrap.dedent("""
        PLUGIN = {
            "name": "Test Project",
            "language": "Python",
            "architecture": "FastAPI + SQLAlchemy",
            "file_hints": ["src/", "tests/"],
        }
    """)
    plugin_path = Path(project_dir) / ".orcho" / "multiagent"
    plugin_path.mkdir(parents=True)
    (plugin_path / "plugin.py").write_text(plugin_code)
    return project_dir


@pytest.fixture
def mock_subprocess_ok(monkeypatch) -> MagicMock:
    """Patch subprocess.run to return success with 'done' output."""
    import agents
    mock = MagicMock(return_value=MagicMock(returncode=0, stdout="done", stderr=""))
    monkeypatch.setattr(agents.subprocess, "run", mock)
    return mock


def _make_popen_mock(stdout_lines: list[str], returncode: int = 0, stderr: str = "") -> MagicMock:
    """
    Build a Popen mock whose stdout is iterable line-by-line
    and communicate() returns ("", stderr).
    """
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = iter(stdout_lines)                  # iterable for `for raw_line in proc.stdout`
    proc.communicate.return_value = ("", stderr)      # (stdout_ignored, stderr)
    return proc


@pytest.fixture
def mock_popen(monkeypatch) -> MagicMock:
    """
    Patch subprocess.Popen in the agents package.
    Returns a factory mock; configure return_value per-test via
    mock_popen.return_value = _make_popen_mock([...]).
    Default: returncode=0, stdout=["done\\n"], stderr="".
    """
    import agents
    default_proc = _make_popen_mock(["done\n"], returncode=0, stderr="")
    popen_mock = MagicMock(return_value=default_proc)
    monkeypatch.setattr(agents.subprocess, "Popen", popen_mock)
    # expose helper so tests can build their own proc mocks
    popen_mock.make_proc = _make_popen_mock
    return popen_mock


@pytest.fixture
def mock_claude_bin(monkeypatch) -> None:
    from core.infra import config
    monkeypatch.setattr(config, "get_claude_bin", lambda: "/fake/claude")


@pytest.fixture
def mock_codex_bin(monkeypatch) -> None:
    from core.infra import config
    monkeypatch.setattr(config, "get_codex_bin", lambda: "/fake/codex")


@pytest.fixture
def mock_gemini_bin(monkeypatch) -> None:
    from core.infra import config
    monkeypatch.setattr(config, "get_gemini_bin", lambda: "/fake/gemini")
