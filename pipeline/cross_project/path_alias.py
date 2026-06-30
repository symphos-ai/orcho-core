"""Cross-project path aliasing for plan output.

The CROSS_PLAN agent receives absolute project roots in its prompt so
it can call ``Read`` / ``Bash`` tools against real files. Left alone,
those absolute paths bleed into the produced plan markdown — readers
then see ``/Users/<op>/www/<workspace>/api/server/handlers.py`` instead
of the workspace-relative ``[api]/server/handlers.py`` form the rest
of the cross UX uses (alias-prefixed paths show up in cross handoff
artifacts, per-alias bundles, and validate-plan focus prompts).

This module owns the post-processing pass that rewrites plan text from
the absolute form back to ``[alias]/relative/path``. It is a leaf
peer — no imports from ``planning_loop``, ``rendering``, or the cross
orchestrator — so unit tests and the planning loop can both reach for
``aliasize_plan_paths`` without pulling in the rendering stack.

The rewrite is deliberately conservative:

* Roots are normalised to a string and stripped of trailing separators
  (both ``/`` and ``\\``) before substitution, so ``/foo/api/`` and
  ``/foo/api`` both match, and Windows ``C:\\ws\\api\\`` collapses to
  ``C:\\ws\\api`` first.
* Each root is matched in both its native form (whatever
  ``str(Path(p))`` produced — backslash-form on Windows) AND its
  forward-slash-only form, because agents conventionally emit forward
  slashes in markdown regardless of host OS.
* Path-continuation matches accept both ``/`` and ``\\`` as the
  separator after the root, so a Windows agent that emits
  ``C:\\ws\\api\\server.py`` is rewritten the same way as one that
  emits ``C:/ws/api/server.py``. Output always uses ``/`` so the
  ``[alias]/relative/path`` form is portable.
* Roots are visited in descending length order so a nested project
  ``/ws/api/sub`` is replaced before the shorter ``/ws/api`` prefix
  would swallow its tail.
* The bare-root form (no trailing path) only fires at a path/word
  boundary (end of string, whitespace, closing quote / bracket /
  parenthesis, backtick, etc.). Substrings embedded inside unrelated
  identifiers are left alone.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

__all__ = ["aliasize_plan_paths"]


# Characters that terminate a bare-root match. Excludes ``/`` and
# ``\\`` deliberately — those are path separators, and a root
# followed by either should be handled by the path-continuation
# branch (so ``<root>/<rest>`` and ``<root>\\<rest>`` both rewrite
# to ``[alias]/<rest>`` rather than ``[alias]/<rest>`` getting eaten
# by a bare-root match).
_BOUNDARY_AFTER = r"(?=$|[\s,;:.!?\"'`)\]>}])"


def _normalise_root(s: str) -> str:
    """Strip trailing slash and backslash. Both ``/foo/`` and
    ``C:\\foo\\`` become the canonical separator-less form so the
    length sort and variant generation work uniformly."""
    return s.rstrip("/").rstrip("\\")


def _root_variants(abs_root: str) -> list[str]:
    """Return the native form plus the forward-slash-only form.

    On POSIX ``abs_root`` never contains a backslash so the two
    variants collapse to one. On Windows where
    ``str(Path("C:\\\\ws\\\\api"))`` produces ``"C:\\\\ws\\\\api"``,
    the forward-slash variant ``"C:/ws/api"`` matches agent-emitted
    markdown that conventionally uses forward slashes.
    """
    variants = [abs_root]
    forward = abs_root.replace("\\", "/")
    if forward != abs_root:
        variants.append(forward)
    return variants


def aliasize_plan_paths(
    text: str,
    projects: Mapping[str, Path],
) -> str:
    """Rewrite absolute project roots in ``text`` to ``[alias]/`` form.

    Iterates ``projects`` by path length descending so a deeper root
    matches before a shorter sibling that would otherwise eat the
    suffix. The bare-root form (no trailing path) is rewritten to
    ``[alias]`` only at a path/word boundary; the path-continuation
    form (``<root>{/,\\\\}<rest>``) is rewritten to ``[alias]/<rest>``
    unconditionally.

    Cross-platform: on Windows where ``str(Path(p))`` carries
    backslashes, this function matches both the backslash form
    (what the prompt passed to the agent) AND the forward-slash form
    (what the agent likely emits in markdown). Output is always
    ``[alias]/relative/path`` with forward slashes.

    Idempotent — re-running on already-aliased text is a no-op because
    the bracket form does not match the absolute prefix grammar.
    """
    if not text or not projects:
        return text
    pairs = sorted(
        (
            (_normalise_root(str(Path(p))), alias)
            for alias, p in projects.items()
            if _normalise_root(str(Path(p)))
        ),
        key=lambda kv: len(kv[0]),
        reverse=True,
    )
    for abs_root, alias in pairs:
        for variant in _root_variants(abs_root):
            # Path-continuation form: <root>{/,\\}<rest> →
            # [alias]/<rest>. Accept both separator styles after the
            # root so Windows agents that emit ``C:\ws\api\file.py``
            # rewrite the same as ones that emit ``C:/ws/api/file.py``.
            for sep in ("/", "\\"):
                text = text.replace(f"{variant}{sep}", f"[{alias}]/")
            # Bare-root form: <root> at a word/path boundary →
            # [alias]. Anchored on a non-word, non-separator boundary
            # to avoid mangling substrings that share the prefix
            # (e.g. ``/foo/api`` should not flip inside
            # ``/foo/apiserver``).
            text = re.sub(
                re.escape(variant) + _BOUNDARY_AFTER,
                f"[{alias}]",
                text,
            )
    return text
