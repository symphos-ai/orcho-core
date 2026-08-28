"""Renderers for the ``orcho workspace mcp`` setup journey.

The complete client setup block has one production owner so ``workspace init``
can point to a reproducible, read-only command instead of duplicating JSON and
shell instructions.
"""

from __future__ import annotations

import json
import shlex

from core.io.ansi import C, paint
from sdk.workspace import WorkspaceInitResult
from sdk.workspace_mcp import WorkspaceMcpSetup


def format_workspace_mcp_setup(setup: WorkspaceMcpSetup) -> str:
    """Render the full MCP client setup block for one active workspace."""
    server_entry = setup.mcp_snippet["mcpServers"][setup.mcp_server_name]
    mcp_command = str(server_entry["command"])
    workspace_dir = str(server_entry["env"]["ORCHO_WORKSPACE"])
    quoted_server = shlex.quote(setup.mcp_server_name)
    quoted_workspace = shlex.quote(workspace_dir)
    quoted_command = shlex.quote(mcp_command)
    installed_runtimes = [r for r in setup.detected_runtimes if r.installed]
    by_client = {runtime.client: runtime for runtime in setup.detected_runtimes}

    out: list[str] = [""]
    out.append(_heading("MCP client setup — choose one path:"))
    out.append(
        f"    {paint('Note:', C.YELLOW)} "
        f"{paint('for multiple workspaces, register one Orcho MCP server per workspace with a distinct name (for example orcho-demo-mcp, orcho-atas-mcp).', C.GREY)}"
    )
    out.append("")
    out.append(_subheading("Terminal clients — run one command in your shell:"))
    if installed_runtimes:
        out.append(
            f"    {paint('Tip:', C.GREEN)} "
            f"{paint('clients marked ✓ are installed on this machine — start with those.', C.GREY)}"
        )
    out.append("")
    out.append(
        _client_subheading(
            "Codex CLI / Codex app:",
            by_client.get("Codex CLI / Codex app"),
        )
    )
    out.extend(
        _command_block(
            [
                f"codex mcp add {quoted_server} \\",
                f"  --env ORCHO_WORKSPACE={quoted_workspace} \\",
                f"  -- {quoted_command}",
            ]
        )
    )
    out.append(
        _done_when(
            f"`codex mcp list` shows `{setup.mcp_server_name}` as enabled; "
            "restart the Codex session before using tools."
        )
    )
    out.append("")
    out.append(_client_subheading("Claude Code:", by_client.get("Claude Code")))
    out.extend(
        _command_block(
            [
                f"claude mcp add {quoted_server} \\",
                f"  --env ORCHO_WORKSPACE={quoted_workspace} \\",
                f"  -- {quoted_command}",
            ]
        )
    )
    out.append(
        _done_when(
            f"`claude mcp list` shows `{setup.mcp_server_name}`; "
            "restart the Claude Code session before using tools."
        )
    )
    out.append("")
    out.append(_client_subheading("Gemini CLI:", by_client.get("Gemini CLI")))
    out.extend(
        _command_block(
            [
                f"gemini mcp add --env ORCHO_WORKSPACE={quoted_workspace} \\",
                f"  {quoted_server} {quoted_command}",
            ]
        )
    )
    out.append(
        _done_when(
            f"`gemini mcp list` shows `{setup.mcp_server_name}`; "
            "restart the Gemini session before using tools."
        )
    )
    out.append("")
    out.append(_subheading("App config snippets — copy into the app config, do not run:"))
    out.append("")
    out.append(_subheading("Claude app / JSON clients — mcpServers shape:"))
    out.extend(
        _json_block(
            json.dumps(
                setup.mcp_snippet,
                indent=2,
                ensure_ascii=False,
            ).splitlines()
        )
    )
    out.append(
        _done_when("the app config contains this server entry and the app has been restarted.")
    )
    out.append("")

    antigravity = {
        "servers": {
            setup.mcp_server_name: {
                "type": "stdio",
                "command": mcp_command,
                "args": list(server_entry.get("args", [])),
                "env": {"ORCHO_WORKSPACE": workspace_dir},
            },
        },
        "inputs": [],
    }
    out.append(_subheading("Antigravity app — User/mcp.json servers shape:"))
    out.extend(
        _json_block(
            json.dumps(
                antigravity,
                indent=2,
                ensure_ascii=False,
            ).splitlines()
        )
    )
    out.append(
        _done_when("`User/mcp.json` contains this server entry and Antigravity has been restarted.")
    )
    out.append("")
    out.append(_subheading("After client restart — verify:"))
    out.append(f"    {paint('orcho_workspace_info', C.GREEN)}")
    out.append(f"    {paint(f'Expected workspace: {workspace_dir}', C.GREY)}")
    out.append("")
    return "\n".join(out)


def format_workspace_mcp_init_summary(result: WorkspaceInitResult) -> str:
    """Render init's concise, reproducible pointer to the full setup."""
    server_entry = result.mcp_snippet["mcpServers"][result.mcp_server_name]
    workspace_dir = str(server_entry["env"]["ORCHO_WORKSPACE"])
    command = str(server_entry["command"])
    detected = (
        ", ".join(
            f"{runtime.client}{' ✓' if runtime.installed else ' (not found)'}"
            for runtime in result.detected_runtimes
        )
        or "none on PATH"
    )
    replay = " ".join(
        (
            "orcho workspace mcp",
            f"--workspace {shlex.quote(workspace_dir)}",
            f"--mcp-server-name {shlex.quote(result.mcp_server_name)}",
            f"--orcho-mcp-command {shlex.quote(command)}",
        )
    )
    out = [
        _heading(f"MCP client setup: {paint(f'Detected clients: {detected}', C.GREY)}"),
    ]
    if result.mcp_config_path is not None:
        verb = {
            "wrote": "Wrote",
            "merged": "Merged into",
            "no-op": "Already up to date in",
            "replaced": "Replaced server entry in",
        }.get(result.mcp_config_action, "Updated")
        out.append(
            f"    {paint('--mcp-config:', C.CYAN)} {paint(verb, C.GREEN)} "
            f"{paint(result.mcp_config_path, C.GREEN)} "
            f"{paint(f'(server: {result.mcp_server_name})', C.GREY)}"
        )
    out.append(f"    {paint('Full setup:', C.CYAN)} {paint(replay, C.GREEN)}")
    return "\n".join(out)


def _heading(text: str) -> str:
    return f"  {paint(text, C.CYAN, C.BOLD)}"


def _subheading(text: str) -> str:
    return f"  {paint(text, C.CYAN)}"


def _client_subheading(text: str, runtime) -> str:
    base = _subheading(text)
    if runtime is None:
        return base
    if runtime.installed:
        return f"{base} {paint('✓ installed', C.GREEN, C.BOLD)}"
    return f"{base} {paint(f'(not found — `{runtime.command}` not on PATH)', C.GREY)}"


def _command_block(lines: list[str]) -> list[str]:
    out = [f"    {paint('```bash', C.GREY)}"]
    out.extend(f"    {paint(line, C.GREEN)}" for line in lines)
    out.append(f"    {paint('```', C.GREY)}")
    return out


def _json_block(lines: list[str]) -> list[str]:
    out = [f"    {paint('```json', C.GREY)}"]
    out.extend(f"    {paint(line, C.GREY)}" for line in lines)
    out.append(f"    {paint('```', C.GREY)}")
    return out


def _done_when(text: str) -> str:
    return f"    {paint('Done when:', C.GREEN)} {paint(text, C.GREY)}"


__all__ = ["format_workspace_mcp_init_summary", "format_workspace_mcp_setup"]
