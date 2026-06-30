"""tests/fixtures/_normalize.py — Snapshot normalization helpers.

The pipeline produces session.json / events.jsonl with run-specific
fields (timestamps, run IDs, absolute paths, monotonic durations).
For golden snapshots we strip these to get byte-stable comparison
between runs.

Phase 5b acceptance bar: same hermetic mock run → same normalized
session + events. Snapshot regression test asserts equality vs
``tests/fixtures/golden/<scenario>.json``.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# Volatile fields stripped to a constant placeholder.
_VOLATILE_KEYS = frozenset({
    "ts", "started_at", "ended_at", "session_id",
    "mtime", "mtime_ns",
    # Git SHAs — the worktree resolver now genuinely creates an
    # isolated checkout for snapshot runs (previously the fixture
    # was a non-git dir and the engine silently degraded to off,
    # leaving these blank). The SHAs are deterministic for a single
    # run but vary across pytest invocations because the test
    # fixture initialises a fresh git repo each time. Key-based mask
    # (rather than SHA-shape regex) keeps the existing snapshot
    # contract uniform across all golden consumers.
    "base_ref", "source_start_head", "commit_sha", "baseline_ref",
    # M12 trace foundation: prompt_render content-derived fields.
    # ``wire_chars`` depends on the rendered prompt byte length;
    # ``prefix_hash`` / ``payload_hash`` are SHA-256 over part
    # bodies that bake in project-specific substitutions
    # (``$project_dir``, tmpdir paths). All three are deterministic
    # for a given run but drift across pytest invocations because
    # the temp directory path differs each time. Shape snapshots
    # only need the structural presence of the keys; dedicated
    # tests assert on the actual values.
    "wire_chars", "prefix_hash", "payload_hash",
})

# Volatile-by-substring patterns — applied to any key whose name contains
# the substring. Catches ``duration_s``, ``total_duration_s``,
# ``avg_duration``, ``ts_*``, etc. without enumerating each.
_VOLATILE_SUBSTRINGS = ("duration", "_at", "ts_", "elapsed")

# Keys that happen to contain a volatile substring but are stable
# schema fields, not timestamps.
_STABLE_KEYS = frozenset({
    "phase_attempts",
})

# Host-specific manifest sub-sections that must be replaced wholesale
# rather than walked field-by-field. These reflect properties of the
# developer's machine (installed binaries, current ``os.environ``)
# that the snapshot must not bind. The *presence* of each key is the
# schema invariant; the concrete value is not.
_HOST_SPECIFIC_PATHS: frozenset[tuple[str, ...]] = frozenset({
    # docker/podman/bwrap presence — varies between CI runners and
    # local dev boxes.
    ("sandbox", "capabilities"),
    # Effective env allowlist post-filter — depends on what the dev
    # has exported in their shell.
    ("sandbox", "env_allowlist_effective"),
    # Number of stripped vars — same reason.
    ("sandbox", "env_stripped_count"),
})

# Token counts are useful metrics but poor session-shape snapshot material:
# prompt wording changes should not make shape fixtures fail merely because
# ``tokens_in`` decreased or increased. Dedicated budget tests own regression
# thresholds; shape snapshots only assert that the metric keys still exist.
_TOKEN_COUNT_KEYS = frozenset({
    "clearable_tokens",
    "context_fill_ratio",
    "cleared_tokens",
    "context_remaining_tokens",
    "context_used_tokens",
    "context_window_tokens",
    "input_tokens_estimate",
    "output_tokens_estimate",
    "summary_tokens",
    "tokens_in",
    "tokens_out",
    "tokens_total",
    "tokens_unknown",
    "total_tokens",
    "total_tokens_in",
    "total_tokens_out",
    "total_tokens_unknown",
})


def _is_volatile_key(key: str) -> bool:
    if key in _STABLE_KEYS:
        return False
    if key in _VOLATILE_KEYS:
        return True
    return any(sub in key for sub in _VOLATILE_SUBSTRINGS)

# Run ID and timestamp patterns we replace with constants.
_RUN_ID_RE = re.compile(r"\d{8}_\d{6}(?:_[a-f0-9]+)?")
_ISO_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?")


def normalize_session(session: dict, *, project_root: str | None = None) -> dict:
    """Strip volatile fields from a session dict in-place-friendly clone.

    - Run IDs / timestamps → ``"<RUN_ID>"`` / ``"<TIMESTAMP>"``
    - ``ts`` / ``started_at`` / ``ended_at`` / ``duration_*`` → ``"<DURATION>"``
    - Absolute paths under ``project_root`` → ``"<PROJECT>/..."``
    - Other absolute paths under tmpdir → ``"<TMP>/..."``

    Returns a fresh dict; does not mutate input.
    """
    return _normalize(session, project_root=project_root)


def normalize_events(lines: list[str], *, project_root: str | None = None) -> list[dict]:
    """Parse events.jsonl lines, normalize each, return list of dicts.
    Events with volatile fields stripped per the same rules as
    sessions; ordering preserved exactly (golden file pins order)."""
    import json as _json
    out: list[dict] = []
    for raw in lines:
        s = raw.strip()
        if not s:
            continue
        out.append(_normalize(_json.loads(s), project_root=project_root))
    return out


def _normalize(value: Any, *, project_root: str | None, path: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        result: dict = {}
        for k, v in value.items():
            child_path = path + (k,) if isinstance(k, str) else path
            if child_path in _HOST_SPECIFIC_PATHS:
                # Whole subtree replaced with a marker — schema check
                # only verifies presence of the key, not host-specific
                # booleans / values inside.
                result[k] = "<HOST_SPECIFIC>"
            elif isinstance(k, str) and _is_volatile_key(k):
                result[k] = "<VOLATILE>"
            elif isinstance(k, str) and k in _TOKEN_COUNT_KEYS:
                result[k] = "<TOKEN_COUNT>"
            else:
                result[k] = _normalize(v, project_root=project_root, path=child_path)
        return result
    if isinstance(value, list):
        return [_normalize(v, project_root=project_root, path=path) for v in value]
    if isinstance(value, str):
        return _normalize_str(value, project_root=project_root)
    return value


def _normalize_str(s: str, *, project_root: str | None) -> str:
    # Hermetic snapshot tests use ``snapshot_run`` when no earlier run-state
    # global has supplied a generated run id; in the full suite the same shape
    # can carry a timestamp-style run id. Both are run identity, not shape.
    s = s.replace("snapshot_run", "<RUN_ID>")
    # Run ID timestamps.
    s = _RUN_ID_RE.sub("<RUN_ID>", s)
    # ISO timestamps.
    s = _ISO_TS_RE.sub("<TIMESTAMP>", s)
    # Project-root path → <PROJECT>.
    if project_root:
        proj = str(Path(project_root).resolve())
        if proj in s:
            s = s.replace(proj, "<PROJECT>")
    # Pytest tmp paths — scrub the whole absolute path to ``<TMP>``.
    # The directory layout *before* the ``pytest-of-<user>`` segment
    # varies by platform and by ``TMPDIR``:
    #   * Linux default:      ``/tmp/pytest-of-<user>/...``
    #   * macOS default:      ``/private/var/folders/.../pytest-of-<user>/...``
    #   * relocated TMPDIR:   ``/private/tmp/claude-501/pytest-of-<user>/...``
    #     (e.g. a sandbox that exports ``TMPDIR=/tmp/claude-501``; ``/tmp``
    #     resolves through the macOS symlink to ``/private/tmp`` and an
    #     extra segment sits between the tmp root and ``pytest-of-``).
    # Rather than enumerate every tmp-root shape, anchor on the stable
    # ``pytest-of-<user>`` marker and consume the leading path segments
    # up to it. ``[^\s"']*?`` is non-greedy so the match starts at the
    # path's leading slash, not somewhere mid-string.
    s = re.sub(
        r"/[^\s\"']*?/pytest-of-[^/]+/[^\s\"']+",
        "<TMP>",
        s,
    )
    return s
