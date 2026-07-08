#!/usr/bin/env python3
"""
orcho-watch.py — Live pipeline monitor backed by the JSONL event-store.

Usage:
    python3 orcho-watch.py [run_dir]

Without an arg: finds the latest ``runs/<ts>/`` under the resolved runspace
(``$ORCHO_RUNSPACE``/``$ORCHO_WORKSPACE`` aware via core.infra.platform).

Reads ``run_dir/events.jsonl`` via ``core.observability.events.tail()``. The
old logic that parsed ``output.log`` (Claude stream-json) and ``progress.log``
in parallel has been collapsed into a single source: every relevant event is
already in the JSONL store, written by the orchestrator, log_phase(), and
the per-provider agent classes.

Backward compat: if a run directory has no ``events.jsonl`` (older runs from
before the event-store), the watcher prints a hint and exits.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parent
_ENGINE_ROOT = _TOOLS_DIR.parent
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

from core.observability import events as evstore  # noqa: E402  # after sys.path bootstrap

# ── Find run directory ────────────────────────────────────────────────────────

def _resolve_runspace() -> Path | None:
    """Resolve the runspace for read-only monitoring.

    Source priority:
      1. ``$ORCHO_RUNSPACE`` env (explicit override; always wins).
      2. Cwd walk-up: the first parent with ``runspace/runs/``. This beats the
         global ``$ORCHO_WORKSPACE`` because the user's physical cwd is a
         stronger context signal than a shell env var that may point elsewhere.
      3. ``$ORCHO_WORKSPACE`` / engine resolver as a fallback when walk-up does
         not find a runspace.

    This tool only reads run artifacts, so cwd walk-up is safe here, unlike in
    the pipeline runtime where it is intentionally disabled.
    """
    # 1. Explicit $ORCHO_RUNSPACE override.
    if env_runspace := os.environ.get("ORCHO_RUNSPACE"):
        p = Path(env_runspace)
        if (p / "runs").is_dir():
            return p

    # 2. Cwd walk-up is contextually stronger than env.
    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        runspace = candidate / "runspace"
        if (runspace / "runs").is_dir():
            return runspace

    # 3. Engine resolver ($ORCHO_WORKSPACE -> workspace_dir().runspace).
    try:
        from core.infra.platform import runspace_dir
        return runspace_dir()
    except Exception:
        return None


def find_latest_run() -> Path:
    runspace = _resolve_runspace()
    if runspace is None:
        print(
            "Runspace is not resolved. Run this tool from any folder inside "
            "workspace-orchestrator/, set $ORCHO_WORKSPACE, or pass a run "
            "directory argument."
        )
        sys.exit(1)

    if not (runspace / "runs").is_dir():
        print(f"{runspace} has no runs/ subdirectory. Run the pipeline first.")
        sys.exit(1)

    # Prefer runs that have events.jsonl; fall back to the newest run dir.
    runs_with_events = sorted(
        runspace.glob("runs/*/events.jsonl"),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    if runs_with_events:
        return runs_with_events[0].parent

    runs = sorted(
        [d for d in (runspace / "runs").iterdir() if d.is_dir()],
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    if not runs:
        print(f"{runspace}/runs has no runs.")
        sys.exit(1)
    return runs[0]


def resolve_run_dir() -> Path:
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if not arg:
        return find_latest_run()
    p = Path(arg)
    if p.is_dir():
        return p
    if p.is_file():
        return p.parent
    print(f"Run dir does not exist: {p}")
    sys.exit(1)


# ── Path shortener (cosmetic) ────────────────────────────────────────────────
_HOME = str(Path.home())
_ENGINE_PREFIX = str(_ENGINE_ROOT) + os.sep
_WS_ROOT = os.environ.get("ORCHO_WORKSPACE", "").strip()
_WS_PREFIX = (_WS_ROOT + os.sep) if _WS_ROOT else None


def shorten(path: str) -> str:
    if not path:
        return path
    if path.startswith(_ENGINE_PREFIX):
        return "./" + path[len(_ENGINE_PREFIX):]
    if _WS_PREFIX and path.startswith(_WS_PREFIX):
        return "ws/" + path[len(_WS_PREFIX):]
    if path.startswith(_HOME + os.sep):
        return "~/" + path[len(_HOME) + 1:]
    return path


# ── Pretty-print one event ────────────────────────────────────────────────────

PHASE_ICONS = {
    "HYPOTHESIS": "🔬",
    # "RESEARCH" reserved for the upcoming deep /unity-research mode.
    "PLAN": "📐", "VALIDATE_PLAN": "🔎",
    "IMPLEMENT": "🏗️", "REVIEW_CHANGES": "🕵️",
    "REPAIR_CHANGES": "🔧", "FINAL_ACCEPTANCE": "✅",
    "CROSS_HYPOTHESIS": "🔬", "CROSS_PLAN": "📐",
    "CONTRACT_CHECK": "🕵️", "DONE": "🏁",
}


def phase_icon(phase: str) -> str:
    if not phase:
        return "▶️"
    for k, v in PHASE_ICONS.items():
        if phase.startswith(k):
            return v
    return "▶️"


TOOL_ICONS = {
    "Read":       "📖",
    "Bash":       "⚡",
    "Grep":       "🔍",
    "Glob":       "📂",
    "Write":      "✏️",
    "Edit":       "✏️",
    "TodoWrite":  "📋",
    "Task":       "🤖",
    "WebFetch":   "🌐",
    "WebSearch":  "🌐",
}


def fmt_time(iso: str) -> str:
    # 2026-05-04T13:10:01.123 → 13:10:01
    return iso[11:19] if len(iso) >= 19 else iso


def render_event(e: evstore.Event) -> str | None:
    """Return a printable line for one event, or None to skip."""
    t = fmt_time(e.ts)
    k = e.kind
    p = e.payload

    if k == "run.start":
        task = (p.get("task") or "").splitlines()[0][:80]
        return (f"\n{'═'*70}\n"
                f"  🔭  ORCHO WATCH  —  task: {task}\n"
                f"{'═'*70}")

    if k == "run.end":
        status = p.get("status", "")
        return f"\n  🏁  [{t}]  RUN END  →  {status}\n"

    if k == "phase.start":
        title = p.get("title", "")
        icon = phase_icon(e.phase or "")
        return (f"\n{'─'*70}\n"
                f"  {icon}  [{t}]  {e.phase}  —  {title}\n"
                f"{'─'*70}")

    if k == "phase.end":
        outcome = p.get("outcome", "")
        label = e.phase or p.get("title", "")
        return f"  ✅ {label} done [{t}]  →  {str(outcome)[:80]}\n"

    if k == "agent.start":
        agent = p.get("agent", "?")
        model = p.get("model", "")
        label = p.get("label", "")
        return f"  ▶  {agent}  {model}  ({label})"

    if k == "agent.end":
        rc = p.get("return_code")
        d = p.get("duration", 0) or 0
        ok = "ok" if rc == 0 else f"FAIL rc={rc}"
        return f"  ◀  {p.get('agent','?')} done  {ok}  {d:.1f}s"

    if k == "agent.tool_use":
        name = p.get("tool_name", "")
        icon = TOOL_ICONS.get(name, "🔧")
        summary = p.get("summary", "")
        if name in ("Read", "Edit", "Write"):
            summary = shorten(summary)
        elif name == "Grep":
            try:
                pat, _, path = summary.partition(" in ")
                summary = f"{pat} in {shorten(path)}"
            except Exception:
                pass
        return f"  {icon} {name:9s} {summary}"

    if k == "agent.text":
        text = p.get("text", "")
        first = next((ln for ln in text.splitlines() if ln.strip()), "").strip()
        return f"  💬 {first[:110]}"

    if k == "agent.error":
        return f"  ❌ {p.get('error_class','error')}: {str(p.get('message',''))[:120]}"

    if k == "agent.retry":
        return f"  🔄 retry {p.get('attempt','?')} — {str(p.get('reason',''))[:80]}"

    if k == "agent.summary":
        cost = p.get("cost_usd")
        ti = p.get("input_tokens")
        to = p.get("output_tokens")
        bits = []
        if cost is not None:
            bits.append(f"${cost:.2f}")
        if ti is not None and to is not None:
            bits.append(f"in={ti} out={to}")
        return f"  📊 {' · '.join(bits)}" if bits else None

    return None  # unknown kind → skip


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    run_dir = resolve_run_dir()
    events_path = run_dir / "events.jsonl"

    if not events_path.exists():
        print(
            f"{events_path} does not exist; this run predates the event-store "
            "migration.\n"
            "Legacy logs (output.log/progress.log) are no longer parsed by "
            "orcho-watch."
        )
        return 1

    print(f"\n{'═'*70}")
    print(f"  🔭  ORCHO WATCH  —  {run_dir.name}")
    print(f"{'═'*70}\n")

    # Live tail. Stop ~3s after run.end so any trailing events flush.
    end_seen_at: list[float] = []  # mutable closure cell
    try:
        for ev in evstore.tail(
            run_dir, since_seq=0, poll=0.3,
            stop_predicate=lambda: bool(end_seen_at)
                                   and (time.monotonic() - end_seen_at[0]) > 3.0,
        ):
            line = render_event(ev)
            if line:
                print(line)
            if ev.kind == "run.end" and not end_seen_at:
                end_seen_at.append(time.monotonic())
    except KeyboardInterrupt:
        print("\n  (stopped)")
        return 130

    print(f"\n{'═'*70}")
    print("  🎉  PIPELINE COMPLETE")
    print(f"{'═'*70}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
