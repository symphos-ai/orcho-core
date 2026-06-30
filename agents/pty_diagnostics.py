"""Diagnostics for startup-time pseudo-terminal allocation failures."""

from __future__ import annotations

import errno

PTY_EXHAUSTED_SENTINEL = "ORCHO_SYSTEM_PTY_EXHAUSTED"


def is_pty_exhaustion(exc: OSError) -> bool:
    """Return True when ``pty.openpty`` failed because no PTYs are available."""
    message = str(exc).lower()
    return (
        exc.errno in {errno.ENOSPC, errno.EAGAIN}
        or "out of pty devices" in message
        or "no available pty" in message
        or "device not configured" in message
    )


def render_pty_exhaustion_diagnostic(exc: OSError) -> str:
    """Human recovery text for a system PTY pool exhaustion blocker."""
    return "\n".join((
        f"{PTY_EXHAUSTED_SENTINEL}: PTY pool exhausted before agent startup.",
        "",
        "Orcho could not allocate a pseudo-terminal for the agent runtime.",
        "This is a system resource blocker, not a task, plan, or code-review failure.",
        "It is usually caused by external orphaned PTY holders, such as terminal",
        "sessions or computer-use/browser automation clients that were not cleaned up.",
        "",
        "Diagnostics:",
        "  python -c \"import pty; print(pty.openpty())\"",
        "  lsof 2>/dev/null | grep -E '/dev/(ttys|ptmx|pty)' | "
        "awk '{print $1, $2, $9}' | sort | uniq -c | sort -nr | head",
        "  ps aux | grep '[S]kyComputerUseClient'",
        "  ps aux | grep '[p]ty'",
        "",
        "Recovery:",
        "  close or restart the leaking client, or terminate orphaned PTY holders;",
        "  then rerun the same Orcho command.",
        "",
        f"Original error: {exc}",
    ))
