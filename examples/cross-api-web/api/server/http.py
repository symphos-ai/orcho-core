"""HTTP framing glue.

The handler subclass below owns nothing but wire-framing: parse URL,
match a route, open a connection, invoke the pure handler, translate
exceptions, write the response. All real logic lives in the modules
this one imports.
"""
from __future__ import annotations

import http.server
import json
import sys
import traceback
import urllib.parse

from . import db, errors, handlers, routing, static
from .sections import BY_SLUG


def _safe_header_value(value: str) -> str:
    if "\r" in value or "\n" in value:
        raise ValueError("HTTP header values must not contain CR or LF")
    if value == "application/json; charset=utf-8":
        return "application/json; charset=utf-8"
    if value == "text/html; charset=utf-8":
        return "text/html; charset=utf-8"
    if value == "text/css":
        return "text/css"
    if value == "text/javascript":
        return "text/javascript"
    if value == "application/javascript":
        return "application/javascript"
    if value == "application/json":
        return "application/json"
    if value == "image/svg+xml":
        return "image/svg+xml"
    if value == "image/png":
        return "image/png"
    if value == "image/jpeg":
        return "image/jpeg"
    if value == "image/gif":
        return "image/gif"
    if value == "image/x-icon":
        return "image/x-icon"
    return "application/octet-stream"


def build_handler(api_root: str, web_root: str, db_path: str):
    """Construct a :class:`BaseHTTPRequestHandler` subclass bound to deps."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = urllib.parse.urlparse(self.path).path
            if path.startswith("/api"):
                self._dispatch_api()
                return
            self._serve_frontend(path)

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch_api()

        def do_PUT(self) -> None:  # noqa: N802
            self._dispatch_api()

        # ── api dispatch ────────────────────────────────────────────

        def _dispatch_api(self) -> None:
            path = urllib.parse.urlparse(self.path).path
            route = routing.match(self.command, path)
            if route is None:
                self._send_error(errors.not_found(
                    f"no route for {self.command} {path!r}",
                ))
                return
            try:
                status, body = self._invoke(route)
            except errors.HttpError as exc:
                self._send_error(exc)
                return
            except Exception as exc:  # noqa: BLE001
                # Log full traceback server-side; return a clean shape
                # to the client.
                print(
                    f"[demo-server] unhandled error in"
                    f" {self.command} {path}: {exc}",
                    file=sys.stderr,
                )
                traceback.print_exc(file=sys.stderr)
                self._send_error(errors.from_exception(exc))
                return
            self._send_json(status, body)

        def _invoke(self, route: routing.Route) -> tuple[int, dict]:
            conn = db.connect(db_path)
            try:
                if route.name == "api.meta":
                    return handlers.meta(conn)

                section = handlers.resolve_section(route.params["slug"])

                if route.name == "api.list":
                    return handlers.list_section(conn, section)

                if route.name == "api.create":
                    body = handlers.parse_json_body(self._read_body())
                    return handlers.create_section(
                        conn, section, body, api_root,
                    )

                if route.name == "api.update":
                    body = handlers.parse_json_body(self._read_body())
                    return handlers.update_section(
                        conn, section, route.params["id"], body, api_root,
                    )

                raise errors.not_found(f"no dispatch for {route.name}")
            finally:
                conn.close()

        # ── frontend ────────────────────────────────────────────────

        def _serve_frontend(self, path: str) -> None:
            if static.is_asset(path):
                asset = static.load_asset(web_root, path)
                if asset is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                body, ctype = asset
                self._send(200, ctype, body)
                return

            parts = [p for p in path.strip("/").split("/") if p]
            if path in ("/", "/index.html") or _is_spa_route(parts):
                self._send_html(200, static.load_index(web_root))
                return

            self.send_response(404)
            self.end_headers()

        # ── helpers ─────────────────────────────────────────────────

        def _read_body(self) -> str:
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length).decode("utf-8", errors="replace")

        def _send_error(self, exc: errors.HttpError) -> None:
            self._send_json(exc.status, exc.to_body())

        def _send_html(self, status: int, body: bytes) -> None:
            self._send(status, "text/html; charset=utf-8", body)

        def _send_json(self, status: int, obj: dict) -> None:
            body = json.dumps(obj, indent=2).encode("utf-8")
            self._send(status, "application/json; charset=utf-8", body)

        def _send(self, status: int, ctype: str, body: bytes) -> None:
            ctype = _safe_header_value(ctype)
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:  # noqa: ARG002
            print(f"  {self.command} {self.path} → {args[1]}")

    return Handler


def _is_spa_route(parts: list[str]) -> bool:
    if len(parts) == 1 and parts[0] in BY_SLUG:
        return True
    if len(parts) == 2 and parts[0] in BY_SLUG and parts[1] == "new":
        return True
    return (
        len(parts) == 3
        and parts[0] in BY_SLUG
        and parts[1] == "edit"
        and bool(parts[2])
        and BY_SLUG[parts[0]].update is not None
    )
