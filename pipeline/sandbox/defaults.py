"""
pipeline/sandbox/defaults.py — built-in env allowlist and token patterns.

Two purposes:

* :data:`DEFAULT_ENV_ALLOWLIST` — minimum env variables that every
  agent runtime needs to function. The resolver merges this list
  with any profile-declared additions; operators add specific
  extras, they never need to re-enumerate the basics. Kept narrow
  on purpose — broadening hides the value of L1.

* :data:`BUILTIN_TOKEN_PATTERNS` — regex set for secrets we expect
  to see in agent output. Covers the three providers shipped under
  ``orcho.agent_runtimes`` today. Scope choice is recorded in ADR
  0034: minimum false-positives, not a generic secret scanner.

Both lists are append-only stability surfaces. Removing an entry
breaks operator config (allowlist) or unmasks already-deployed
secrets (token patterns); both need an ADR + announce.
"""
from __future__ import annotations

# Built-in env allowlist. Order is informational; the launcher
# uses set membership.
#
# Categories, for the operator reviewing this list:
#
# * shell/locale baseline — without these the agent CLI crashes
#   or produces broken text;
# * PATH — needed to resolve the agent binary itself;
# * HOME — agent CLIs read per-user config (``~/.claude``,
#   ``~/.codex``, ``~/.config/gemini``);
# * TERM / NO_COLOR / FORCE_COLOR — terminal capabilities so the
#   PTY-driven CLI renders sensibly;
# * provider API keys — the agent CLIs read these to authenticate;
# * runtime config overrides — :file:`core/infra/config.py` reads
#   them from env (CLAUDE_BIN, CODEX_BIN, GEMINI_BIN, CODEX_HOME);
# * ORCHO_* — our own namespace, agents can rely on it.
DEFAULT_ENV_ALLOWLIST: tuple[str, ...] = (
    # Shell + locale
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "LC_NUMERIC",
    "LC_TIME",
    "LC_COLLATE",
    "LC_MONETARY",
    "TZ",
    "TMPDIR",
    "TEMP",
    "TMP",
    # Terminal
    "TERM",
    "COLORTERM",
    "NO_COLOR",
    "FORCE_COLOR",
    # Windows essentials (Job Object backend strips everything else)
    "SYSTEMROOT",
    "WINDIR",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMDATA",
    "COMSPEC",
    "PATHEXT",
    # Agent provider API keys
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    # Agent runtime binary / config overrides — read by
    # :mod:`core.infra.config`. If the operator overrode the
    # binary location at the orcho level, the agent process
    # must see the same override.
    "CLAUDE_BIN",
    "CODEX_BIN",
    "GEMINI_BIN",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
)


# Prefix patterns that mean "allow this entire family". The launcher
# treats a variable as allowed when its name appears in
# DEFAULT_ENV_ALLOWLIST OR starts with one of these prefixes. This
# is *not* a glob system — only literal prefix match.
DEFAULT_ENV_ALLOWLIST_PREFIXES: tuple[str, ...] = (
    "ORCHO_",
)


# Built-in token-masking regex set. Each entry must:
#
# * match the entire secret in one group (the masker replaces the
#   whole match, not a capture inside it);
# * be anchored to a recognisable prefix unique to the issuer, so
#   accidental matches on UUIDs / hex strings are negligible.
#
# Patterns are str (not compiled) so the resolver can compile once
# at policy materialisation and surface bad regex with a precise
# operator-facing error.
BUILTIN_TOKEN_PATTERNS: tuple[str, ...] = (
    # Anthropic — sk-ant-{api01,sid01,…}-{base64 ~95 chars}
    r"sk-ant-[a-zA-Z0-9]{2,8}-[A-Za-z0-9_-]{20,}",
    # OpenAI / Codex — sk-{proj-…|svcacct-…|…}-{base64 ≥20 chars}
    # Anchored to ``sk-`` followed by ≥20 url-safe chars; longer
    # OpenAI key formats (``sk-proj-…``) are subsumed by the
    # greedy class.
    r"sk-[A-Za-z0-9_-]{20,}",
    # Google / Gemini — AIza{35 url-safe chars}, fixed length
    r"AIza[0-9A-Za-z_-]{35}",
)


__all__ = [
    "BUILTIN_TOKEN_PATTERNS",
    "DEFAULT_ENV_ALLOWLIST",
    "DEFAULT_ENV_ALLOWLIST_PREFIXES",
]
