"""Authentication diagnostics shared by CLI runtime adapters."""

from __future__ import annotations

import json

from core.io.retry import AgentAuthenticationError

_AUTH_FAILURE_PATTERNS = (
    "failed to authenticate",
    "invalid authentication credentials",
    "authentication failed",
    "not authenticated",
    "not logged in",
    "login required",
    "invalid api key",
    "api key is invalid",
    "missing api key",
    "unauthorized",
)

_AUTH_HINTS = {
    "claude": {
        "login": "claude auth logout && claude auth login",
        "status": "claude auth status",
        "smoke": "claude --print --model <model> 'Reply OK only'",
        "automation": "export ANTHROPIC_API_KEY=...",
    },
    "claude-glm": {
        "login": "refresh the credentials used by your claude-glm wrapper",
        "status": "claude-glm auth status",
        "smoke": "claude-glm --print --model <model> 'Reply OK only'",
        "automation": "set the API key consumed by your claude-glm wrapper",
    },
    "codex": {
        "login": "codex login",
        "status": "codex login status",
        "smoke": "codex exec --json --dangerously-bypass-approvals-and-sandbox 'Reply OK only'",
        "automation": "printenv OPENAI_API_KEY | codex login --with-api-key",
    },
    "gemini": {
        "login": "gemini /login",
        "status": "gemini /auth",
        "smoke": "gemini -p 'Reply OK only' -m <model> -o stream-json --skip-trust",
        "automation": "export GEMINI_API_KEY=... (or GOOGLE_API_KEY)",
    },
}


def looks_like_auth_failure(*parts: str | None) -> bool:
    """Return True when CLI output is an authentication failure."""
    text = "\n".join(p for p in parts if p).lower()
    if not text:
        return False
    return any(pattern in text for pattern in _AUTH_FAILURE_PATTERNS)


def raise_authentication_error(
    *,
    runtime: str,
    model: str,
    cli: str,
    exit_code: int,
    stdout: str = "",
    stderr: str = "",
) -> None:
    """Raise a user-facing auth error with the exact runtime and next step."""
    hint = _AUTH_HINTS.get(runtime, {})
    login = hint.get("login", f"{runtime} login")
    status = hint.get("status", f"{runtime} login status")
    smoke = hint.get("smoke", f"{runtime} --help")
    automation = hint.get("automation")
    original = _compact_original_error(stdout=stdout, stderr=stderr)
    smoke = smoke.replace("<model>", model)

    first_line = (
        f"Runtime credentials were rejected for runtime={runtime!r} "
        f"model={model!r}; refresh the CLI login and retry."
    )
    lines = [
        first_line,
        "",
        f"Runtime: {runtime}",
        f"Model: {model}",
        f"CLI: {cli}",
        f"Exit code: {exit_code}",
        "",
        "Refresh CLI credentials:",
        f"  {login}",
        "",
        "Check current auth status:",
        f"  {status}",
        "",
        "Smoke-test a real model call:",
        f"  {smoke}",
    ]
    if automation:
        lines += [
            "",
            "For non-interactive setup:",
            f"  {automation}",
        ]
    if original and _show_debug_details():
        lines += [
            "",
            "Original CLI error (--output debug):",
            _indent(original),
        ]
    raise AgentAuthenticationError(
        "\n".join(lines),
        exit_code=exit_code,
        stderr=stderr,
    )


def _compact_original_error(*, stdout: str, stderr: str) -> str:
    raw = (stderr or stdout or "").strip()
    if not raw:
        return ""
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    lines = _extract_readable_lines(lines)
    auth_lines = [
        line for line in lines if looks_like_auth_failure(line)
    ]
    if auth_lines:
        return "\n".join(auth_lines[:3])[:1000]

    non_protocol_lines = [
        line for line in lines if not line.lstrip().startswith("{")
    ]
    if non_protocol_lines:
        lines = non_protocol_lines

    compact = "\n".join(lines[:8])
    if len(lines) > 8:
        compact += "\n..."
    return compact[:1000]


def _extract_readable_lines(lines: list[str]) -> list[str]:
    readable: list[str] = []
    for line in lines:
        if not line.lstrip().startswith("{"):
            readable.append(line)
            continue
        readable.extend(_extract_json_protocol_text(line))
    return readable or lines


def _show_debug_details() -> bool:
    from core.observability.logging import get_verbose
    return get_verbose()


def _extract_json_protocol_text(line: str) -> list[str]:
    try:
        obj = json.loads(line)
    except (TypeError, ValueError):
        return []
    if not isinstance(obj, dict):
        return []

    out: list[str] = []
    _collect_content_text(obj.get("message"), out)
    _collect_content_text(obj, out)
    if isinstance(obj.get("error"), str):
        out.append(obj["error"])
    status = obj.get("api_error_status")
    if status:
        out.append(f"API error status: {status}")
    return [line for line in out if line.strip()]


def _collect_content_text(node: object, out: list[str]) -> None:
    if not isinstance(node, dict):
        return
    content = node.get("content")
    if not isinstance(content, list):
        return
    for item in content:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            out.append(item["text"])


def _indent(text: str) -> str:
    return "\n".join(f"  {line}" for line in text.splitlines())
