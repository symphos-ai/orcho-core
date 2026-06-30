"""Parse Codex CLI rollout JSONL for live context telemetry.

Pure read-only utility. Never raises into the runtime path:
malformed / missing rollouts return None and the caller falls
back to ORCHO_ESTIMATED via resolve_context_pressure().
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path


@dataclass(frozen=True)
class CodexTelemetrySnapshot:
    context_window_tokens: int | None
    context_used_tokens: int | None
    context_remaining_tokens: int | None
    rate_limits: dict | None
    raw_source_path: str | None  # Runtime-internal debug only


def _codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env) if env else Path.home() / ".codex"


# Process-local lookup cache: id (any of session_id / conversation_id /
# thread_id flavors) -> resolved rollout path. Bounded by number of
# distinct Codex sessions seen during the orchestrator process lifetime.
_ROLLOUT_PATH_CACHE: dict[str, Path] = {}


def _date_dir(root: Path, d: date) -> Path:
    return root / f"{d.year:04d}" / f"{d.month:02d}" / f"{d.day:02d}"


def _recent_date_dirs(root: Path) -> list[Path]:
    """Today + yesterday in local time — matches Codex's on-disk layout."""
    today = date.today()
    yesterday = today - timedelta(days=1)
    return [_date_dir(root, today), _date_dir(root, yesterday)]


def find_codex_rollout(
    session_id: str | None, thread_id: str | None = None,
) -> Path | None:
    """Locate the rollout-*.jsonl associated with the given id.

    Today's CLI emits the same UUID under every key-flavor, so
    `session_id` and `thread_id` should match. The function still tries
    both with dedupe — robustness is nearly free and protects against a
    hypothetical future where the two diverge.

    Lookup is layered: each layer tries every candidate before moving
    to the next, more expensive layer. This guarantees a wrong session_id
    candidate doesn't drag us through a full ~/.codex/sessions rglob
    before we try the correct thread_id.

      Layer 1 — process-local cache (O(1) per candidate).
      Layer 2 — recent date dirs filename glob `rollout-*-<id>.jsonl`
                (today + yesterday, local time; hot path).
      Layer 3 — recent date dirs header scan against `session_meta.payload.id`
                (paranoia insurance for hypothetical schema drift; cheap
                because it only opens the first JSONL line per file).
      Layer 4 — full `rglob` under <codex_home>/sessions (cold path; only
                reached when nothing recent matches).
    """
    candidates = _candidate_ids(session_id, thread_id)
    if not candidates:
        return None
    root = _codex_home() / "sessions"
    if not root.exists():
        return None
    recent = [d for d in _recent_date_dirs(root) if d.exists()]
    # Layer 1 — cache.
    for sid in candidates:
        cached = _ROLLOUT_PATH_CACHE.get(sid)
        if cached is not None and cached.exists():
            return cached
        if cached is not None:
            _ROLLOUT_PATH_CACHE.pop(sid, None)
    # Layer 2 — recent-dir filename glob, all candidates.
    # If `codex exec resume` ever produces a second rollout with the same
    # session-id suffix in a different timestamp, we want the freshest one
    # so Live context tracks the active session, not a stale duplicate.
    for sid in candidates:
        matches: list[Path] = []
        for d in recent:
            matches.extend(d.glob(f"rollout-*-{sid}.jsonl"))
        winner = _newest(matches)
        if winner is not None:
            _ROLLOUT_PATH_CACHE[sid] = winner
            return winner
    # Layer 3 — recent-dir header scan, all candidates.
    for sid in candidates:
        matches = []
        for d in recent:
            matches.extend(
                p for p in d.glob("rollout-*.jsonl")
                if _header_id_matches(p, sid)
            )
        winner = _newest(matches)
        if winner is not None:
            _ROLLOUT_PATH_CACHE[sid] = winner
            return winner
    # Layer 4 — full rglob (cold path), all candidates.
    for sid in candidates:
        matches = list(root.rglob(f"rollout-*-{sid}.jsonl"))
        winner = _newest(matches)
        if winner is not None:
            _ROLLOUT_PATH_CACHE[sid] = winner
            return winner
    return None


def _newest(paths: list[Path]) -> Path | None:
    """Return the most recently modified path, or ``None`` if empty.

    Used by every lookup layer so a resumed session with multiple
    same-id rollouts on disk always reports against the freshest one.
    Falls back to lexicographic order if any ``stat`` call raises
    (e.g. file vanished between glob and stat) — the lexicographic
    suffix already encodes the creation timestamp, so it's a safe
    second-best.
    """
    if not paths:
        return None
    if len(paths) == 1:
        return paths[0]
    try:
        return max(paths, key=lambda p: p.stat().st_mtime)
    except OSError:
        return max(paths, key=lambda p: p.name)


def _candidate_ids(*ids: str | None) -> list[str]:
    """Order-preserving dedupe of non-empty id values."""
    seen: set[str] = set()
    out: list[str] = []
    for sid in ids:
        if isinstance(sid, str) and sid and sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def _header_id_matches(path: Path, sid: str) -> bool:
    """Cheap check: parse only the first JSONL line for session_meta.payload.id."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            first = fh.readline().strip()
        if not first.startswith("{"):
            return False
        obj = json.loads(first)
        if obj.get("type") != "session_meta":
            return False
        payload = obj.get("payload") or {}
        return payload.get("id") == sid
    except (OSError, ValueError, TypeError):
        return False


def parse_codex_rollout(path: Path) -> CodexTelemetrySnapshot:
    window: int | None = None
    used: int | None = None
    rate_limits: dict | None = None
    try:
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw or not raw.startswith("{"):
                    continue
                try:
                    obj = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if obj.get("type") != "event_msg":
                    continue
                payload = obj.get("payload") or {}
                ptype = payload.get("type")
                if ptype == "task_started":
                    w = payload.get("model_context_window")
                    if isinstance(w, int) and w > 0:
                        window = w
                    continue
                if ptype != "token_count":
                    continue
                info = payload.get("info")
                if isinstance(info, dict):
                    w = info.get("model_context_window")
                    if isinstance(w, int) and w > 0:
                        window = w
                    last = info.get("last_token_usage")
                    if isinstance(last, dict):
                        u = last.get("input_tokens")
                        if isinstance(u, int) and u >= 0:
                            used = u
                rl = payload.get("rate_limits")
                if isinstance(rl, dict):
                    rate_limits = rl
    except OSError:
        return CodexTelemetrySnapshot(None, None, None, None, None)
    remaining: int | None = None
    if isinstance(window, int) and isinstance(used, int) and window >= used:
        remaining = window - used
    return CodexTelemetrySnapshot(
        context_window_tokens=window,
        context_used_tokens=used,
        context_remaining_tokens=remaining,
        rate_limits=rate_limits,
        raw_source_path=str(path),
    )


def load_codex_telemetry(
    session_id: str | None,
    thread_id: str | None = None,
) -> CodexTelemetrySnapshot | None:
    path = find_codex_rollout(session_id, thread_id)
    if path is None:
        return None
    return parse_codex_rollout(path)
