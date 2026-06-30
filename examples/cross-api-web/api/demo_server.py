#!/usr/bin/env python3
"""demo_server.py — AcmeCorp admin tool entry point.

Thin CLI wrapper around the :mod:`server` package. All architecture
(routing, storage, wire dispatch, error mapping, HTTP framing) lives
inside ``server/``; this file parses flags, sanity-checks the project
trees, initialises the db, and starts the serve loop. In development
the API process auto-restarts when Python files under ``api/`` or
``server/`` change, so registry/routing/schema edits land without a
manual restart. For the frontend, run Vite from the web project; it
proxies ``/api`` to this server and hot-reloads browser modules.

Layout::

    GET  /                          — Vue frontend shell
    GET  /<slug>[/new|/edit/<id>]   — Vue frontend shell
    GET  /src/*  /assets/*  /vendor/* — frontend modules / assets
    GET  /api/meta                  — section metadata + counts
    GET  /api/<slug>                — list rows
    POST /api/<slug>                — create row
    PUT  /api/<slug>/<id>           — update row when section allows it

Usage::

    python demo_server.py
    cd ../web && npm run dev       # frontend hot reload on :5173
    python demo_server.py --api /path/to/demo/api \\
                          --web /path/to/demo/web \\
                          --db  /path/to/demo/demo.db \\
                          --port 8000
    python demo_server.py --reset   # drop tables and reseed
    python demo_server.py --no-reload
"""
from __future__ import annotations

import argparse
import os
import socketserver
import subprocess
import sys
import time
from pathlib import Path

from server import build_handler, db

_REQUIRED_FILES: tuple[tuple[str, str], ...] = (
    ("api", "api/payload.py"),
    ("api", "api/teams.py"),
    ("api", "api/projects.py"),
    ("web", "index.html"),
    ("web", "src/main.ts"),
    ("web", "src/sections.ts"),
)


def main() -> int:
    defaults = _default_roots()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--api",
        default=defaults["api"],
        help="API project root (default: directory containing demo_server.py)",
    )
    ap.add_argument(
        "--web",
        default=defaults["web"],
        help="Web project root (default: sibling ../web)",
    )
    ap.add_argument(
        "--db",
        default=defaults["db"],
        help="SQLite path (default: sibling ../demo.db)",
    )
    ap.add_argument(
        "--reset", action="store_true",
        help="Drop tables and reseed fixtures before serving",
    )
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument(
        "--reload",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Restart the API server when Python files under api/ or "
            "server/ change. Enabled by default for the demo dev loop."
        ),
    )
    args = ap.parse_args()

    roots = {
        "api": str(Path(args.api).resolve()),
        "web": str(Path(args.web).resolve()),
        "db":  str(Path(args.db).resolve()),
    }
    if not _preflight(roots):
        return 2

    if args.reload and os.environ.get("ORCHO_DEMO_SERVER_CHILD") != "1":
        return _run_reloader(roots)

    db.init(roots["db"], reset=args.reset)
    _print_banner(args.port, roots, reset=args.reset)

    handler = build_handler(roots["api"], roots["web"], roots["db"])

    class _ReusableServer(socketserver.TCPServer):
        allow_reuse_address = True

    try:
        with _ReusableServer(("127.0.0.1", args.port), handler) as srv:
            srv.serve_forever()
    except KeyboardInterrupt:
        print("\nBye.")
    return 0


def _default_roots() -> dict[str, str]:
    api_root = Path(__file__).resolve().parent
    demo_root = api_root.parent
    return {
        "api": str(api_root),
        "web": str(demo_root / "web"),
        "db": str(demo_root / "demo.db"),
    }


def _run_reloader(roots: dict[str, str]) -> int:
    """Run a child server and restart it when API Python files change."""

    env = dict(os.environ)
    env["ORCHO_DEMO_SERVER_CHILD"] = "1"
    watched = _snapshot(roots["api"])
    proc: subprocess.Popen | None = None
    try:
        while True:
            if proc is None or proc.poll() is not None:
                if proc is not None:
                    return int(proc.returncode or 0)
                proc = subprocess.Popen([sys.executable, *sys.argv], env=env)

            time.sleep(0.35)
            current = _snapshot(roots["api"])
            if current == watched:
                continue

            watched = current
            print("\n[demo-server] API file change detected; restarting...")
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            proc = None
    except KeyboardInterrupt:
        if proc is not None and proc.poll() is None:
            proc.terminate()
        print("\nBye.")
        return 0


def _snapshot(api_root: str) -> tuple[tuple[str, int, int], ...]:
    root = Path(api_root)
    rows: list[tuple[str, int, int]] = []
    for path in _watch_files(root):
        try:
            st = path.stat()
        except FileNotFoundError:
            continue
        rows.append((str(path.relative_to(root)), st.st_mtime_ns, st.st_size))
    return tuple(sorted(rows))


def _watch_files(api_root: Path) -> list[Path]:
    watch_roots = [api_root / "api", api_root / "server"]
    files: list[Path] = []
    for root in watch_roots:
        if not root.is_dir():
            continue
        files.extend(
            p for p in root.rglob("*.py")
            if "__pycache__" not in p.parts
        )
    demo_server = api_root / "demo_server.py"
    if demo_server.is_file():
        files.append(demo_server)
    return files


def _preflight(roots: dict[str, str]) -> bool:
    for kind, child in _REQUIRED_FILES:
        full = Path(roots[kind]) / child
        if not full.is_file():
            print(
                f"ERROR: expected {child} under {roots[kind]}",
                file=sys.stderr,
            )
            return False
    return True


def _print_banner(port: int, roots: dict[str, str], *, reset: bool) -> None:
    print(f"AcmeCorp admin → http://localhost:{port}/")
    print(f"  api:  {roots['api']}")
    print(f"  web:  {roots['web']}")
    print(f"  db:   {roots['db']}{'  (reset)' if reset else ''}")
    print("  UI:   cd web && npm run dev  (Vite HMR, proxies /api here)")
    print("  REST: GET/POST/PUT /api/{users,teams,projects}")
    print("  API:  Python file changes auto-restart this server.")
    print("  Ctrl+C to stop.")


if __name__ == "__main__":
    raise SystemExit(main())
