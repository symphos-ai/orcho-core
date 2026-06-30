"""tests/acceptance/test_demo_1b_bootstrap.py - DEMO-1B bootstrap script."""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parents[2]
SCRIPT = CORE_DIR / "examples" / "scripts" / "bootstrap_demo_1b.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _fake_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(
        bin_dir / "fake-python",
        "#!/usr/bin/env bash\n"
        "exit 0\n",
    )
    _write_executable(
        bin_dir / "curl",
        "#!/usr/bin/env bash\n"
        "printf '200'\n",
    )
    _write_executable(
        bin_dir / "lsof",
        "#!/usr/bin/env bash\n"
        "exit 0\n",
    )
    _write_executable(
        bin_dir / "npm",
        "#!/usr/bin/env bash\n"
        "mkdir -p node_modules/.bin\n"
        "mkdir -p node_modules/vue/dist\n"
        "printf '#!/usr/bin/env bash\\nexit 0\\n' > node_modules/.bin/vue-tsc\n"
        "chmod +x node_modules/.bin/vue-tsc\n"
        "printf 'export default {}\\n' > node_modules/vue/dist/vue.esm-browser.prod.js\n"
        "printf '{\"lockfileVersion\": 3}\\n' > package-lock.json\n",
    )
    return bin_dir


def _run(
    demo_root: Path,
    fake_bin: Path,
    *,
    cwd: Path | None = None,
    phase: str | None = None,
    extra_args: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    args = [str(SCRIPT)]
    if phase is not None:
        args.append(phase)
    if extra_args:
        args.extend(extra_args)
    env = {
        **os.environ,
        "ORCHO_DEMO_ROOT": str(demo_root),
        "ORCHO_DEMO_PORT": "8799",
        "ORCHO_DEMO_PYTHON": str(fake_bin / "fake-python"),
        "ORCHO_DEMO_NPM": str(fake_bin / "npm"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


def _git_status(repo: Path) -> str:
    return subprocess.check_output(
        ["git", "status", "--short", "--untracked-files=all"],
        cwd=repo,
        text=True,
    )


class TestBootstrapDemo1B:
    def test_rebootstrap_cleans_artifacts_and_commits_initial_state(
        self,
        tmp_path: Path,
    ) -> None:
        demo_root = tmp_path / "demo"
        fake_bin = _fake_bin(tmp_path)
        _run(demo_root, fake_bin)

        api_project = demo_root / "api"
        web_project = demo_root / "web"
        workspace = demo_root / "workspace-orchestrator"

        (api_project / "src" / "api").mkdir(parents=True)
        (api_project / "src" / "api" / "implementation.txt").write_text(
            "mock artifact\n",
        )
        (web_project / ".orcho").mkdir(exist_ok=True)
        (web_project / ".orcho" / "last_build.md").write_text("stale\n")
        (workspace / "runs").mkdir()
        (workspace / "runs" / "stale.json").write_text("{}\n")
        (demo_root / "demo.db").write_text("old db\n")
        (demo_root / "server.log").write_text("old log\n")
        (demo_root / "web.log").write_text("old web log\n")
        (demo_root / "root-artifact.txt").write_text("old root artifact\n")
        (demo_root / ".server.pid").write_text("999999\n")
        (demo_root / ".web.pid").write_text("999998\n")

        result = _run(demo_root, fake_bin, cwd=api_project)

        assert not (api_project / "src").exists()
        assert not (web_project / ".orcho" / "last_build.md").exists()
        assert (api_project / ".orcho" / "multiagent" / "plugin.py").is_file()
        assert (web_project / ".orcho" / "multiagent" / "plugin.py").is_file()
        assert (workspace / ".agents" / "skills" / "team-lead" / "SKILL.md").is_file()
        assert (api_project / ".agents" / "skills" / "backend-python" / "SKILL.md").is_file()
        assert (api_project / ".agents" / "skills" / "backend-qa" / "SKILL.md").is_file()
        assert (web_project / ".agents" / "skills" / "frontend-vuejs" / "SKILL.md").is_file()
        assert (web_project / ".agents" / "skills" / "frontend-qa" / "SKILL.md").is_file()
        assert not (workspace / ".agents" / "skills" / "contract-surface").exists()
        assert not (api_project / ".agents" / "skills" / "api-contract-specialist").exists()
        assert not (web_project / ".agents" / "skills" / "web-contract-consumer").exists()
        for root in (workspace, api_project, web_project):
            mirror = root / ".claude" / "skills"
            assert mirror.is_symlink()
            assert os.readlink(mirror) == "../.agents/skills"
            assert (
                mirror / next((root / ".agents" / "skills").iterdir()).name
            ).exists()
        assert "workspace: team-lead" in result.stdout
        assert "api:       backend-python, backend-qa" in result.stdout
        assert "web:       frontend-vuejs, frontend-qa" in result.stdout
        assert "Client mirrors:" in result.stdout
        assert ".claude/skills -> ../.agents/skills" in result.stdout
        assert "DECOMPOSE/PLAN: team-lead maps product flow" in result.stdout
        assert "REVIEW/QA:      backend-qa + frontend-qa" in result.stdout
        assert "API watcher (re)started" in result.stdout
        assert "Web watcher (Vite + HMR" in result.stdout
        assert "Open http://localhost:5173/ in a browser." in result.stdout
        assert "Local config options for another run:" in result.stdout
        assert "--language French --accounting" in result.stdout
        assert "--accounting writes accounting.enabled=true" in result.stdout
        local_config = workspace / ".orcho" / "config.local.json"
        assert local_config.is_file()
        local_data = json.loads(local_config.read_text(encoding="utf-8"))
        assert local_data["phases"]["implement"]["model"]
        assert not (demo_root / "demo.db").exists()
        assert not (demo_root / "root-artifact.txt").exists()
        assert (demo_root / "server.log").is_file()
        assert (demo_root / "web.log").is_file()
        assert (demo_root / ".server.pid").is_file()
        assert (demo_root / ".web.pid").is_file()
        assert (demo_root / "Makefile").is_file()
        makefile = (demo_root / "Makefile").read_text(encoding="utf-8")
        assert "make dev" in makefile
        assert "api-reset" in makefile
        test_web_block = makefile.split("test-web:", 1)[1].split("\n\n", 1)[0]
        assert "node --test tests/contracts.test.mjs" in test_web_block
        assert "npm run build" in test_web_block

        assert _git_status(api_project) == ""
        assert _git_status(web_project) == ""
        assert (api_project / "demo_server.py").is_file()
        assert (api_project / "api" / "payload.py").is_file()
        assert not (demo_root / "demo_server.py").exists()
        assert (web_project / "index.html").is_file()
        assert (web_project / "src" / "contracts.ts").is_file()
        assert (web_project / "src" / "main.ts").is_file()
        assert (web_project / "package.json").is_file()
        assert (web_project / "package-lock.json").is_file()
        assert (web_project / "node_modules" / ".bin" / "vue-tsc").is_file()
        index_html = (web_project / "index.html").read_text(encoding="utf-8")
        assert "/vendor/vue.esm-browser.prod.js" in index_html
        assert (web_project / "node_modules" / "vue" / "dist" / "vue.esm-browser.prod.js").is_file()

    def test_copy_phase_skips_workspace_and_mcp(
        self, tmp_path: Path,
    ) -> None:
        """``copy`` phase: bare fixture copy + git init only."""
        demo_root = tmp_path / "demo"
        fake_bin = _fake_bin(tmp_path)
        result = _run(demo_root, fake_bin, phase="copy")

        api_project = demo_root / "api"
        web_project = demo_root / "web"
        workspace = demo_root / "workspace-orchestrator"
        mcp_config = demo_root / ".mcp.json"

        assert (api_project / "api" / "payload.py").is_file()
        assert (web_project / "src" / "contracts.ts").is_file()
        assert not workspace.exists(), (
            "copy phase must not create the workspace dir"
        )
        assert not mcp_config.exists(), (
            "copy phase must not write .mcp.json"
        )
        assert not (api_project / ".orcho").exists()
        assert not (web_project / ".orcho").exists()
        assert _git_status(api_project) == ""
        assert _git_status(web_project) == ""
        assert "phase: copy" in result.stdout
        assert "Workspace + MCP NOT configured" in result.stdout
        assert "orcho workspace init" in result.stdout

    def test_init_phase_creates_workspace_but_not_mcp(
        self, tmp_path: Path,
    ) -> None:
        """``init`` phase: workspace + skills, no .mcp.json."""
        demo_root = tmp_path / "demo"
        fake_bin = _fake_bin(tmp_path)
        result = _run(demo_root, fake_bin, phase="init")

        api_project = demo_root / "api"
        web_project = demo_root / "web"
        workspace = demo_root / "workspace-orchestrator"
        mcp_config = demo_root / ".mcp.json"

        assert workspace.is_dir()
        assert (workspace / ".agents" / "skills" / "team-lead" / "SKILL.md").is_file()
        assert (api_project / ".agents" / "skills" / "backend-python" / "SKILL.md").is_file()
        assert (web_project / ".agents" / "skills" / "frontend-vuejs" / "SKILL.md").is_file()
        assert not mcp_config.exists(), (
            "init phase must not write .mcp.json"
        )
        assert "phase: init" in result.stdout
        assert "MCP NOT wired" in result.stdout
        assert '"orcho-demo-1b"' in result.stdout  # printed manual snippet

    def test_init_phase_can_seed_language_and_accounting_config(
        self,
        tmp_path: Path,
    ) -> None:
        demo_root = tmp_path / "demo"
        fake_bin = _fake_bin(tmp_path)
        _run(
            demo_root,
            fake_bin,
            phase="init",
            extra_args=["--language", "Russian", "--accounting"],
        )

        local_config = (
            demo_root
            / "workspace-orchestrator"
            / ".orcho"
            / "config.local.json"
        )
        data = json.loads(local_config.read_text(encoding="utf-8"))
        assert data["language"]["plan_language"] == "Russian"
        assert data["language"]["task_language"] == "Russian"
        assert data["accounting"]["enabled"] is True

    def test_mcp_command_override_via_env(
        self, tmp_path: Path,
    ) -> None:
        """``ORCHO_DEMO_MCP_COMMAND`` wins over auto-detection."""
        demo_root = tmp_path / "demo"
        fake_bin = _fake_bin(tmp_path)
        override = "/opt/custom/orcho-mcp"
        proc = subprocess.run(
            [str(SCRIPT), "mcp"],
            env={
                **os.environ,
                "ORCHO_DEMO_ROOT": str(demo_root),
                "ORCHO_DEMO_PORT": "8799",
                "ORCHO_DEMO_PYTHON": str(fake_bin / "fake-python"),
                "ORCHO_DEMO_NPM": str(fake_bin / "npm"),
                "ORCHO_DEMO_MCP_COMMAND": override,
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            },
            capture_output=True,
            text=True,
            check=True,
        )
        # The override flows into both the generated .mcp.json and the
        # printed manual snippet.
        mcp_config = demo_root / ".mcp.json"
        assert mcp_config.is_file()
        config = json.loads(mcp_config.read_text(encoding="utf-8"))
        servers = config.get("mcpServers") or config.get("servers") or {}
        entry = servers.get("orcho-demo-1b")
        assert entry, f"orcho-demo-1b not in {config}"
        assert entry["command"] == override
        # Sanity: the dev-venv path must not appear anywhere in stdout,
        # since the override (and the lack of a stable install in this
        # test environment) means resolution never falls through to it.
        dev_venv = str(CORE_DIR / ".venv" / "bin" / "orcho-mcp")
        assert dev_venv not in proc.stdout

    def test_unknown_phase_exits_nonzero(
        self, tmp_path: Path,
    ) -> None:
        demo_root = tmp_path / "demo"
        fake_bin = _fake_bin(tmp_path)
        proc = subprocess.run(
            [str(SCRIPT), "garbage"],
            env={
                **os.environ,
                "ORCHO_DEMO_ROOT": str(demo_root),
                "ORCHO_DEMO_PORT": "8799",
                "ORCHO_DEMO_PYTHON": str(fake_bin / "fake-python"),
                "ORCHO_DEMO_NPM": str(fake_bin / "npm"),
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 2
        assert "unknown phase" in proc.stderr
