"""Static frontend serving (Vue shell + source modules + vendor)."""
from __future__ import annotations

import mimetypes
from pathlib import Path

_VUE_VENDOR_PATH = "/vendor/vue.esm-browser.prod.js"
_VUE_VENDOR_FILE = Path("node_modules/vue/dist/vue.esm-browser.prod.js")
_ASSET_ROOTS = {
    "/src/": Path("src"),
    "/assets/": Path("assets"),
}
_ASSET_PREFIXES = (*_ASSET_ROOTS, "/vendor/")


def is_asset(path: str) -> bool:
    return path.startswith(_ASSET_PREFIXES)


def load_index(web_root: str) -> bytes:
    return (Path(web_root) / "index.html").read_bytes()


def load_asset(
    web_root: str, request_path: str,
) -> tuple[bytes, str] | None:
    if request_path == _VUE_VENDOR_PATH:
        path = Path(web_root) / _VUE_VENDOR_FILE
        if not path.is_file():
            return None
        return path.read_bytes(), "text/javascript"

    path = _find_asset(Path(web_root), request_path)
    if path is None:
        return None
    if not path.is_file():
        return None
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if path.suffix == ".ts":
        ctype = "text/javascript"
    return path.read_bytes(), ctype


def _find_asset(web_root: Path, request_path: str) -> Path | None:
    for prefix, asset_root in _ASSET_ROOTS.items():
        if not request_path.startswith(prefix):
            continue

        wanted = request_path.removeprefix(prefix)
        if not wanted or _has_unsafe_segment(wanted):
            return None

        root = (web_root / asset_root).resolve()
        if not root.is_dir():
            return None

        for candidate in root.rglob("*"):
            if not candidate.is_file():
                continue
            rel = candidate.relative_to(root).as_posix()
            if rel == wanted:
                return candidate
        return None

    return None


def _has_unsafe_segment(path: str) -> bool:
    return any(part in ("", ".", "..") for part in path.split("/"))
