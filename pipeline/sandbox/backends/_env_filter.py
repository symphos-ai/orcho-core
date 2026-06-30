"""
pipeline/sandbox/backends/_env_filter.py — shared env-allowlist helper.

Both Unix and Windows backends compute the child environment the
same way: take parent env, keep only entries in the resolved
allowlist (built-in + profile additions), drop anything in the
denylist last. Extracted here so the logic — and its tests —
live in one place.

The launcher returns the **count** of stripped variables, not the
names. Names would leak the existence of secrets that the operator
intentionally wanted filtered (the whole point of the allowlist).
The count is enough to spot misconfigurations like "I forgot to
allow MY_API_TOKEN and now my agent fails".
"""
from __future__ import annotations

from pipeline.sandbox.defaults import (
    DEFAULT_ENV_ALLOWLIST,
    DEFAULT_ENV_ALLOWLIST_PREFIXES,
)
from pipeline.sandbox.policy import SandboxPolicy


def _is_allowed(
    name: str,
    *,
    allowlist: frozenset[str],
    prefixes: tuple[str, ...],
) -> bool:
    if name in allowlist:
        return True
    return any(name.startswith(p) for p in prefixes)


def compute_child_env(
    parent_env: dict[str, str],
    policy: SandboxPolicy,
) -> tuple[dict[str, str], int]:
    """Return (filtered_env, stripped_count).

    Filtering order:

    1. Start with the built-in default allowlist + prefix rules.
    2. Union with profile-declared additions.
    3. Drop anything matching the profile-declared denylist
       (literal match only — no globbing). Denylist wins.
    4. Iterate parent env, keep entries that pass.

    The result is a fresh dict — never a view over the parent
    env. The launcher passes it to ``Popen(env=…)``.
    """
    allowlist = frozenset(DEFAULT_ENV_ALLOWLIST) | frozenset(policy.env_allowlist)
    denylist = frozenset(policy.env_denylist)
    prefixes = DEFAULT_ENV_ALLOWLIST_PREFIXES

    child: dict[str, str] = {}
    stripped = 0
    for name, value in parent_env.items():
        if name in denylist:
            stripped += 1
            continue
        if _is_allowed(name, allowlist=allowlist, prefixes=prefixes):
            child[name] = value
        else:
            stripped += 1
    return child, stripped


__all__ = ["compute_child_env"]
