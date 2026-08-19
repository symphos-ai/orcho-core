"""Env-allowlist filtering — built-in defaults, profile additions, denylist."""
from __future__ import annotations

from pipeline.sandbox.backends._env_filter import compute_child_env
from pipeline.sandbox.policy import SandboxPolicy


def _policy(allow: tuple[str, ...] = (), deny: tuple[str, ...] = ()) -> SandboxPolicy:
    return SandboxPolicy(env_allowlist=allow, env_denylist=deny)


class TestComputeChildEnv:
    def test_builtin_allows_path_home(self) -> None:
        env = {"PATH": "/bin", "HOME": "/h", "RANDOM_SECRET": "leak"}
        out, stripped = compute_child_env(env, _policy())
        assert "PATH" in out and "HOME" in out
        assert "RANDOM_SECRET" not in out
        assert stripped == 1

    def test_orcho_prefix_allowed_by_default(self) -> None:
        env = {"ORCHO_RUN_ID": "abc", "ORCHO_WORKSPACE": "/x", "FOO": "y"}
        out, stripped = compute_child_env(env, _policy())
        assert "ORCHO_RUN_ID" in out
        assert "ORCHO_WORKSPACE" in out
        assert "FOO" not in out
        assert stripped == 1

    def test_profile_allowlist_extends_builtin(self) -> None:
        env = {"PATH": "/bin", "MY_PROJECT_TOKEN": "tok"}
        out, _ = compute_child_env(env, _policy(allow=("MY_PROJECT_TOKEN",)))
        assert "MY_PROJECT_TOKEN" in out
        assert "PATH" in out  # builtin still works

    def test_denylist_overrides_builtin_allow(self) -> None:
        # PATH is in the built-in allowlist; denylist must still strip it.
        env = {"PATH": "/bin", "HOME": "/h"}
        out, stripped = compute_child_env(env, _policy(deny=("PATH",)))
        assert "PATH" not in out
        assert "HOME" in out
        assert stripped == 1

    def test_denylist_overrides_profile_allow(self) -> None:
        env = {"MY_TOK": "secret"}
        out, stripped = compute_child_env(
            env, _policy(allow=("MY_TOK",), deny=("MY_TOK",)),
        )
        assert "MY_TOK" not in out
        assert stripped == 1

    def test_provider_api_keys_pass_through(self) -> None:
        env = {
            "ANTHROPIC_API_KEY": "sk-ant-…",
            "OPENAI_API_KEY": "sk-…",
            "GEMINI_API_KEY": "AIza…",
            "GOOGLE_API_KEY": "AIza…",
            "AWS_SECRET_ACCESS_KEY": "AKIA…",
        }
        out, stripped = compute_child_env(env, _policy())
        for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
            assert k in out
        assert "AWS_SECRET_ACCESS_KEY" not in out
        assert stripped == 1

    def test_builtin_runtime_auth_and_bin_overrides_pass_through(self) -> None:
        # The shipped GLM runtime authenticates via ANTHROPIC_AUTH_TOKEN and
        # honors the CLAUDE_GLM_BIN binary override; a built-in runtime must
        # not depend on variables the built-in sandbox strips.
        env = {
            "ANTHROPIC_AUTH_TOKEN": "glm-key",
            "CLAUDE_GLM_BIN": r"C:\tools\claude.exe",
            "AWS_SECRET_ACCESS_KEY": "AKIA…",
        }
        out, stripped = compute_child_env(env, _policy())
        assert "ANTHROPIC_AUTH_TOKEN" in out
        assert "CLAUDE_GLM_BIN" in out
        assert "AWS_SECRET_ACCESS_KEY" not in out
        assert stripped == 1

    def test_empty_parent_env(self) -> None:
        out, stripped = compute_child_env({}, _policy())
        assert out == {}
        assert stripped == 0
