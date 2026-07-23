"""
agents/stream_log.py — structured diagnostic sections for the agent log.

Runtime subprocess output streams through :mod:`agents.stream`, but
orchestrator-owned milestones ("subtask X started", verification-gate summaries)
do not come from a child process. These helpers write those milestones into the
same tail-able ``output.log`` — and, optionally, the stdout echo — using the
identical section framing, so post-mortem tooling sees one consistent stream.

The live-output sink itself (the ``output.log`` path and the stdout-echo flag)
lives on :mod:`agents.stream`; this module reaches it lazily so there is no
import cycle between the two.
"""
from __future__ import annotations

import sys


def write_agent_log_section(
    label: str,
    content: str = "",
    *,
    duration_s: float | None = None,
    label_codes: tuple[str, ...] = (),
    content_key_codes: tuple[str, ...] = (),
    separator_codes: tuple[str, ...] = (),
    exit_codes: tuple[str, ...] = (),
) -> None:
    """Append a structured diagnostic section to live agent output.

    Runtime subprocess output already streams through :mod:`agents.stream`, but
    orchestrator-owned milestones (for example "subtask X started") do not
    come from a child process. This helper writes those milestones to the same
    tail-able log and optional stdout echo without pretending they are model
    output.
    """
    from agents import stream as _stream

    echo_payload = _render_agent_log_section(
        label,
        content,
        duration_s=duration_s,
        label_codes=label_codes,
        content_key_codes=content_key_codes,
        separator_codes=separator_codes,
        exit_codes=exit_codes,
        color=None,
    )
    file_payload = _render_agent_log_section(
        label,
        content,
        duration_s=duration_s,
        label_codes=(),
        content_key_codes=(),
        separator_codes=(),
        exit_codes=(),
        color=False,
    )
    _stream._echo_stdout(echo_payload)
    if _stream._agent_log is None:
        return
    try:
        _stream._agent_log.parent.mkdir(parents=True, exist_ok=True)
        with _stream._agent_log.open("a", encoding="utf-8", buffering=1) as fh:
            fh.write(file_payload)
            fh.flush()
    except Exception:
        pass


def append_agent_log_section(label: str, content: str = "") -> None:
    """Append a plain structured section to ``output.log`` without stdout echo.

    Some orchestrator-owned milestones already render directly to the terminal
    via local presentation helpers. They still need durable parity in the
    tail-able operator log, but routing them through ``write_agent_log_section``
    would double-print when stdout echo is enabled. This helper writes the same
    section shape to the configured agent log only.
    """
    from agents import stream as _stream

    if _stream._agent_log is None:
        return
    payload = _render_agent_log_section(
        label,
        content,
        duration_s=None,
        label_codes=(),
        content_key_codes=(),
        separator_codes=(),
        exit_codes=(),
        color=False,
    )
    try:
        _stream._agent_log.parent.mkdir(parents=True, exist_ok=True)
        with _stream._agent_log.open("a", encoding="utf-8", buffering=1) as fh:
            fh.write(payload)
            fh.flush()
    except Exception:
        pass


def _render_agent_log_section(
    label: str,
    content: str,
    *,
    duration_s: float | None,
    label_codes: tuple[str, ...],
    content_key_codes: tuple[str, ...],
    separator_codes: tuple[str, ...],
    exit_codes: tuple[str, ...],
    color: bool | None,
) -> str:
    from core.io.ansi import paint

    sep = paint("-" * 60, *separator_codes, color=color, stream=sys.stdout)
    label_text = paint(label, *label_codes, color=color, stream=sys.stdout)
    header = f"\n{sep}\n{label_text}\n{sep}\n"
    body = ""
    if content:
        body = _paint_section_content_keys(
            content if content.endswith("\n") else f"{content}\n",
            codes=content_key_codes,
            color=color,
        )
    exit_line = ""
    if duration_s is not None:
        exit_text = f"[EXIT code=0 duration={duration_s:.2f}s]"
        exit_line = f"{paint(exit_text, *exit_codes, color=color, stream=sys.stdout)}\n"
    return f"{header}{body}{exit_line}"


def _paint_section_content_keys(
    content: str,
    *,
    codes: tuple[str, ...],
    color: bool | None,
) -> str:
    if not codes:
        return content
    from core.io.ansi import paint

    rendered: list[str] = []
    for line in content.splitlines(keepends=True):
        body = line[:-1] if line.endswith("\n") else line
        newline = "\n" if line.endswith("\n") else ""
        key, sep, rest = body.partition(":")
        if sep and key and all(ch.isalnum() or ch == "_" for ch in key):
            rendered.append(
                f"{paint(key, *codes, color=color, stream=sys.stdout)}:"
                f"{rest}{newline}"
            )
        else:
            rendered.append(line)
    return "".join(rendered)


__all__ = [
    "append_agent_log_section",
    "write_agent_log_section",
]
