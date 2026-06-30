"""Tests for the provider-neutral tool-invocation normalization layer."""
from __future__ import annotations

from pathlib import Path

import pytest

from agents.stream_parsers.tool_invocations import (
    categorize_builtin_tool,
    emit_tool_call,
    format_tool_call,
    normalize_builtin_tool,
    normalize_mcp_tool_call,
    parse_claude_mcp_tool_name,
)
from core.observability import events as evstore


@pytest.fixture
def store(tmp_path: Path):
    evstore.init_event_store(tmp_path)
    yield tmp_path
    evstore.init_event_store(None)


# ── categorize_builtin_tool ────────────────────────────────────────────────


class TestCategorizeBuiltinTool:
    def test_shell(self) -> None:
        assert categorize_builtin_tool("Bash") == "shell"

    def test_file_read(self) -> None:
        assert categorize_builtin_tool("Read") == "file_read"

    @pytest.mark.parametrize("name", ["Edit", "MultiEdit", "Write", "NotebookEdit"])
    def test_file_write(self, name: str) -> None:
        assert categorize_builtin_tool(name) == "file_write"

    @pytest.mark.parametrize("name", ["Grep", "Glob"])
    def test_search(self, name: str) -> None:
        assert categorize_builtin_tool(name) == "search"

    @pytest.mark.parametrize("name", ["WebFetch", "WebSearch"])
    def test_web(self, name: str) -> None:
        assert categorize_builtin_tool(name) == "web"

    def test_task(self) -> None:
        assert categorize_builtin_tool("Task") == "task"

    def test_todo(self) -> None:
        assert categorize_builtin_tool("TodoWrite") == "todo"

    def test_unknown(self) -> None:
        assert categorize_builtin_tool("FrobnicateXyz") == "unknown"


# ── parse_claude_mcp_tool_name ─────────────────────────────────────────────


class TestParseClaudeMcpToolName:
    def test_well_formed_name_splits(self) -> None:
        assert parse_claude_mcp_tool_name(
            "mcp__orcho_demo_mcp__orcho_plan_validate",
        ) == ("orcho_demo_mcp", "orcho_plan_validate")

    def test_keeps_underscores_in_server_segment(self) -> None:
        # split("__", 2) — server may contain single underscores; only the
        # double-underscore delimiter separates server from tool.
        assert parse_claude_mcp_tool_name(
            "mcp__server_with_underscores__tool",
        ) == ("server_with_underscores", "tool")

    def test_non_mcp_name_returns_none(self) -> None:
        assert parse_claude_mcp_tool_name("Bash") is None
        assert parse_claude_mcp_tool_name("Read") is None

    def test_malformed_mcp_name_returns_none(self) -> None:
        assert parse_claude_mcp_tool_name("mcp__only_server") is None
        assert parse_claude_mcp_tool_name("mcp____empty_server_segment") is None


# ── normalize_builtin_tool / normalize_mcp_tool_call ───────────────────────


class TestNormalizeBuiltinTool:
    def test_bash_command_summary(self) -> None:
        call = normalize_builtin_tool("Bash", {"command": "pytest -q"})
        assert call.event_kind == "agent.tool_use"
        assert call.tool_category == "shell"
        assert call.tool_name == "Bash"
        assert call.display_name == "Bash"
        assert call.summary == "pytest -q"
        assert call.input == {"command": "pytest -q"}
        # MCP-only fields stay empty on built-ins.
        assert call.server is None
        assert call.status is None
        assert call.arguments is None
        assert call.error is None

    def test_unknown_tool_falls_back_to_flat_summary(self) -> None:
        call = normalize_builtin_tool("FrobnicateXyz", {"a": 1, "b": 2})
        assert call.tool_category == "unknown"
        assert "a=1" in call.summary
        assert "b=2" in call.summary


class TestNormalizeMcpToolCall:
    def test_basic_success_call(self) -> None:
        call = normalize_mcp_tool_call(
            server="orcho-demo-mcp",
            tool_name="orcho_plan_validate",
            arguments={"task": "x"},
            status="completed",
        )
        assert call.event_kind == "agent.mcp_tool_call"
        assert call.tool_category == "mcp"
        assert call.tool_name == "orcho_plan_validate"
        assert call.display_name == "orcho-demo-mcp.orcho_plan_validate"
        assert call.summary == "orcho-demo-mcp.orcho_plan_validate"
        assert call.server == "orcho-demo-mcp"
        assert call.status == "completed"
        assert call.arguments == {"task": "x"}
        assert call.error is None

    def test_failed_call_carries_error_payload(self) -> None:
        call = normalize_mcp_tool_call(
            server="orcho-demo-mcp",
            tool_name="orcho_plan_validate",
            status="failed",
            error={"message": "cancelled"},
        )
        assert call.status == "failed"
        assert call.error == {"message": "cancelled"}
        # arguments defaults to {} when omitted, not None.
        assert call.arguments == {}


# ── format_tool_call ───────────────────────────────────────────────────────


class TestFormatToolCall:
    def test_builtin_shell_with_summary(self) -> None:
        call = normalize_builtin_tool("Bash", {"command": "ls"})
        assert format_tool_call(call) == "  ⚡ Bash: ls\n"

    def test_builtin_without_summary(self) -> None:
        call = normalize_builtin_tool("Bash", {})
        assert format_tool_call(call) == "  ⚡ Bash\n"

    def test_builtin_search_uses_search_icon(self) -> None:
        call = normalize_builtin_tool("Grep", {"pattern": "TODO", "path": "/x"})
        assert format_tool_call(call).startswith("  🔍 Grep:")

    def test_builtin_file_read_uses_book_icon(self) -> None:
        call = normalize_builtin_tool("Read", {"file_path": "/a.py"})
        assert format_tool_call(call) == "  📖 Read: /a.py\n"

    def test_builtin_file_write_uses_pencil_icon(self) -> None:
        call = normalize_builtin_tool("Edit", {"file_path": "/a.py"})
        assert format_tool_call(call) == "  📝 Edit: /a.py\n"

    def test_builtin_unknown_uses_wrench_icon(self) -> None:
        call = normalize_builtin_tool("FrobnicateXyz", {"k": "v"})
        assert format_tool_call(call).startswith("  🛠 FrobnicateXyz:")

    def test_mcp_success(self) -> None:
        call = normalize_mcp_tool_call(
            server="orcho-demo-mcp",
            tool_name="orcho_plan_validate",
            status="completed",
        )
        assert (
            format_tool_call(call)
            == "  🔌 MCP: orcho-demo-mcp.orcho_plan_validate\n"
        )

    def test_mcp_failed_renders_error_message(self) -> None:
        call = normalize_mcp_tool_call(
            server="orcho-demo-mcp",
            tool_name="orcho_plan_validate",
            status="failed",
            error={"message": "user cancelled MCP tool call"},
        )
        assert format_tool_call(call) == (
            "  🔌 MCP: orcho-demo-mcp.orcho_plan_validate "
            "failed: user cancelled MCP tool call\n"
        )

    def test_mcp_failed_without_message_falls_back_to_plain_line(self) -> None:
        call = normalize_mcp_tool_call(
            server="s", tool_name="t", status="failed", error={"code": 1},
        )
        assert format_tool_call(call) == "  🔌 MCP: s.t\n"


# ── emit_tool_call ─────────────────────────────────────────────────────────


def _last_event(tmp_path: Path):
    evs = evstore.read_all(tmp_path)
    return evs[-1] if evs else None


class TestEmitToolCall:
    def test_builtin_emits_agent_tool_use(self, store) -> None:
        emit_tool_call(
            normalize_builtin_tool("Bash", {"command": "ls"}),
            agent_label="invoke",
        )
        e = _last_event(store)
        assert e.kind == "agent.tool_use"
        assert e.payload["tool_name"] == "Bash"
        assert e.payload["tool_category"] == "shell"
        assert e.payload["display_name"] == "Bash"
        assert e.payload["summary"] == "ls"
        assert e.payload["input"] == {"command": "ls"}
        assert e.payload["agent"] == "invoke"

    def test_mcp_emits_agent_mcp_tool_call(self, store) -> None:
        emit_tool_call(
            normalize_mcp_tool_call(
                server="orcho-demo-mcp",
                tool_name="orcho_plan_validate",
                arguments={"task": "x"},
                status="failed",
                error={"message": "cancelled"},
            ),
            agent_label="invoke",
        )
        e = _last_event(store)
        assert e.kind == "agent.mcp_tool_call"
        assert e.payload["server"] == "orcho-demo-mcp"
        assert e.payload["tool_name"] == "orcho_plan_validate"
        assert e.payload["tool_category"] == "mcp"
        assert (
            e.payload["display_name"] == "orcho-demo-mcp.orcho_plan_validate"
        )
        assert e.payload["arguments"] == {"task": "x"}
        assert e.payload["status"] == "failed"
        assert e.payload["error"] == {"message": "cancelled"}
        assert e.payload["agent"] == "invoke"

    def test_no_dual_emit_for_one_call(self, store) -> None:
        emit_tool_call(normalize_builtin_tool("Read", {"file_path": "/x"}))
        emit_tool_call(
            normalize_mcp_tool_call(server="s", tool_name="t", status="completed"),
        )
        kinds = [e.kind for e in evstore.read_all(store)]
        assert kinds == ["agent.tool_use", "agent.mcp_tool_call"]


# ── path normalization at emission ─────────────────────────────────────────


@pytest.fixture
def active_run(tmp_path: Path):
    """Activate a worktree checkout for the test's execution context.

    The orchestrator normally sets this once per run; emit_tool_call
    reads it via ContextVar to rewrite absolute run paths to relative
    form before serializing the event.
    """
    from pipeline.engine.worktree import (
        reset_active_worktree_checkout,
        set_active_worktree_checkout,
    )
    checkout = str(tmp_path / "runs" / "20260526_150010" / "checkout")
    run_dir = str(tmp_path / "runs" / "20260526_150010")
    token = set_active_worktree_checkout(checkout)
    try:
        yield checkout, run_dir
    finally:
        reset_active_worktree_checkout(token)


class TestPathNormalization:
    def test_read_file_path_stripped_to_checkout_relative(
        self, store, active_run,
    ) -> None:
        checkout, _ = active_run
        emit_tool_call(
            normalize_builtin_tool(
                "Read", {"file_path": f"{checkout}/api/teams.py"},
            ),
        )
        e = _last_event(store)
        assert e.payload["input"] == {"file_path": "api/teams.py"}
        assert e.payload["summary"] == "api/teams.py"

    def test_grep_path_and_pattern_summary_normalized(
        self, store, active_run,
    ) -> None:
        checkout, _ = active_run
        emit_tool_call(
            normalize_builtin_tool(
                "Grep", {"pattern": "user", "path": checkout},
            ),
        )
        e = _last_event(store)
        assert e.payload["input"]["path"] == "."
        assert e.payload["summary"] == "'user' in ."

    def test_bash_command_with_embedded_paths_collapsed(
        self, store, active_run,
    ) -> None:
        checkout, _ = active_run
        emit_tool_call(
            normalize_builtin_tool(
                "Bash",
                {"command": f"ls {checkout}/api {checkout}/server"},
            ),
        )
        e = _last_event(store)
        assert e.payload["input"]["command"] == "ls api server"

    def test_run_dir_paths_get_at_run_prefix(
        self, store, active_run,
    ) -> None:
        _, run_dir = active_run
        emit_tool_call(
            normalize_builtin_tool(
                "Read", {"file_path": f"{run_dir}/evidence/foo.json"},
            ),
        )
        e = _last_event(store)
        assert e.payload["input"] == {"file_path": "@run/evidence/foo.json"}

    def test_mcp_arguments_normalized_too(self, store, active_run) -> None:
        checkout, _ = active_run
        emit_tool_call(
            normalize_mcp_tool_call(
                server="s",
                tool_name="t",
                arguments={"target_path": f"{checkout}/server/db.py"},
                status="completed",
            ),
        )
        e = _last_event(store)
        assert e.payload["arguments"] == {"target_path": "server/db.py"}

    def test_no_active_checkout_keeps_absolute_paths(self, store) -> None:
        emit_tool_call(
            normalize_builtin_tool(
                "Read", {"file_path": "/some/abs/path.py"},
            ),
        )
        e = _last_event(store)
        assert e.payload["input"] == {"file_path": "/some/abs/path.py"}

    def test_checkout_boundary_avoids_partial_match(
        self, store, active_run,
    ) -> None:
        # ``<checkout>_other`` sits under the run_dir, so it falls
        # through to the ``@run/`` rewrite — but the ``checkout`` rule
        # must NOT chew off its leading slash and produce ``_other/...``.
        checkout, _ = active_run
        sibling = checkout + "_other/foo.py"
        emit_tool_call(
            normalize_builtin_tool("Read", {"file_path": sibling}),
        )
        e = _last_event(store)
        assert e.payload["input"] == {
            "file_path": "@run/checkout_other/foo.py",
        }

    def test_paths_outside_run_dir_are_untouched(
        self, store, active_run,
    ) -> None:
        # Absolute paths that don't sit under the active run pass
        # through unchanged — only run-internal paths get rewritten.
        emit_tool_call(
            normalize_builtin_tool(
                "Read", {"file_path": "/etc/hosts"},
            ),
        )
        e = _last_event(store)
        assert e.payload["input"] == {"file_path": "/etc/hosts"}

    def test_stdout_format_uses_relative_paths(self, active_run) -> None:
        # Regression guard: format_tool_call renders the live stdout
        # transcript directly from NormalizedToolCall — bypassing
        # emit_tool_call. Normalization at construction time means the
        # live transcript shows ``api/teams.py`` instead of the full
        # ``<checkout>/api/teams.py``. The full path is appended in
        # grey parentheses so the operator can still orient.
        from core.io.ansi import get_color_enabled, set_color_enabled

        checkout, _ = active_run
        full = f"{checkout}/api/teams.py"
        call = normalize_builtin_tool("Read", {"file_path": full})
        # Force color on so the colored path runs under pytest's
        # non-TTY captured stdout — the shared paint() policy would
        # otherwise auto-detect to plain. The test pins the colored
        # rendering shape end-to-end, so the override is appropriate.
        before = get_color_enabled()
        set_color_enabled(True)
        try:
            rendered = format_tool_call(call)
        finally:
            set_color_enabled(before)
        assert rendered.startswith("  📖 Read: api/teams.py ")
        assert f"({full})" in rendered
        # Grey ANSI flanks the suffix.
        assert "\033[90m" in rendered
        assert "\033[0m" in rendered

    def test_stdout_format_collapses_bash_paths(self, active_run) -> None:
        # Bash carries no single primary path (the command line may embed
        # several), so abs_hint stays None and no grey suffix appears.
        checkout, _ = active_run
        call = normalize_builtin_tool(
            "Bash", {"command": f"ls {checkout}/api {checkout}/server"},
        )
        assert format_tool_call(call) == "  ⚡ Bash: ls api server\n"

    def test_abs_hint_captured_on_single_path_tools(
        self, active_run,
    ) -> None:
        checkout, _ = active_run
        full = f"{checkout}/server/db.py"
        call = normalize_builtin_tool("Edit", {"file_path": full})
        assert call.abs_hint == full

    def test_abs_hint_captured_on_grep_path(self, active_run) -> None:
        checkout, _ = active_run
        call = normalize_builtin_tool(
            "Grep", {"pattern": "user", "path": checkout},
        )
        assert call.abs_hint == checkout

    def test_abs_hint_none_when_no_rewrite_happened(self) -> None:
        # No active worktree, no rewrite — abs_hint stays None so the
        # transcript doesn't bolt on a redundant ``(path)`` suffix that
        # repeats the visible summary.
        call = normalize_builtin_tool(
            "Read", {"file_path": "/etc/hosts"},
        )
        assert call.abs_hint is None
        # And format doesn't append a suffix either.
        assert format_tool_call(call) == "  📖 Read: /etc/hosts\n"

    def test_abs_hint_not_emitted_to_event_payload(
        self, store, active_run,
    ) -> None:
        # Renderer-only field — events.jsonl stays clean of the original
        # absolute path. The on-disk wire keeps the short cognitive form.
        checkout, _ = active_run
        emit_tool_call(
            normalize_builtin_tool(
                "Read", {"file_path": f"{checkout}/api/teams.py"},
            ),
        )
        e = _last_event(store)
        assert "abs_hint" not in e.payload
