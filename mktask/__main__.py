"""mktask CLI and server entry point."""

from __future__ import annotations

import argparse
import atexit
import getpass
import hashlib
import mimetypes
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path
from typing import Any

from aiohttp import web
from mkio import create_app
from mkio.config import load_config

from mktask import __version__
from mktask.services import FILES_ROUTE, user_prefix

MAX_UPLOAD = 20 * 1024 * 1024  # bytes; a screenshot is well under 1 MB
_EXTENSIONS = {  # mimetypes' guesses are unfriendly for the common ones
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
    "image/svg+xml": ".svg", "text/plain": ".txt", "text/html": ".html", "application/pdf": ".pdf",
    "message/rfc822": ".eml", "application/json": ".json",
}


def serve(
    config: str | Path | dict[str, Any] = "mktask.toml",
    host: str | None = None,
    port: int | None = None,
    db_path: str | None = None,
    user: str | None = None,
    files_dir: str | Path | None = None,
) -> None:
    """Start the mktask server. Blocks until shutdown.

    `user` (default: the OS login name) supplies the two prefix letters of
    every Task ID this server assigns. `files_dir` (default: `<db_path>.files`
    beside the database, a temporary directory for `:memory:`) holds uploaded
    reference files, served at /files and accepted at POST /files.
    """
    cfg = _load_config(config)
    if host is not None:
        cfg["host"] = host
    if port is not None:
        cfg["port"] = port
    if db_path is not None:
        cfg["db_path"] = db_path
    if user is None:
        user = getpass.getuser()
    files = _files_dir(cfg.get("db_path", "mkio.db"), files_dir)
    files.mkdir(parents=True, exist_ok=True)
    cfg.setdefault("static", {})[FILES_ROUTE.rstrip("/")] = str(files)
    if "tasks" in cfg.get("services", {}):
        cfg["services"]["tasks"]["prefix"] = user_prefix(user)
        cfg["services"]["tasks"]["user"] = user
        cfg["services"]["tasks"]["files_dir"] = str(files)

    # Probe the port before anything else starts: a bind failure inside
    # app.start() happens after the startup hooks have opened the database,
    # whose aiosqlite threads then keep the process alive after the traceback.
    _check_port(cfg["host"], cfg["port"])

    app = create_app(cfg, routes=[("POST", FILES_ROUTE.rstrip("/"), _upload_handler(files))])

    async def announce() -> None:
        print(_banner(cfg, config, user, files), flush=True)

    async def undo_redo(event: Any) -> None:
        # Registered before start(), which is when on_undo_redo insists on
        # being called — so the service is looked up per event rather than
        # captured: app.services is empty until the server is running.
        service = app.services.get("tasks")
        if service is not None:
            await service.undo_redo_hook(event)

    app.on_startup(announce)
    app.on_undo_redo(undo_redo)
    app.run()


def _files_dir(db_path: str, files_dir: str | Path | None) -> Path:
    """Where uploaded files live: the given directory, else `<db_path>.files`
    beside the database, else (in-memory database) a temp dir removed at exit."""
    if files_dir is not None:
        return Path(files_dir)
    if db_path == ":memory:":
        tmp = Path(tempfile.mkdtemp(prefix="mktask-files-"))
        atexit.register(shutil.rmtree, tmp, True)
        return tmp
    return Path(f"{db_path}.files")


def _extension(mime: str) -> str:
    return _EXTENSIONS.get(mime) or mimetypes.guess_extension(mime) or ".bin"


def _upload_handler(files: Path):
    """POST /files: the raw body becomes `<sha256>.<ext>` under `files`.

    Content-addressed names dedupe repeats and keep client-supplied names off
    the filesystem. The body is streamed under our own cap rather than
    aiohttp's 1 MB `client_max_size`, which `request.read()` would enforce.
    """
    async def handler(request: web.Request) -> web.Response:
        mime = (request.content_type or "application/octet-stream").lower()
        if request.content_length is not None and request.content_length > MAX_UPLOAD:
            raise web.HTTPRequestEntityTooLarge(max_size=MAX_UPLOAD, actual_size=request.content_length)
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.content.iter_chunked(64 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD:
                raise web.HTTPRequestEntityTooLarge(max_size=MAX_UPLOAD, actual_size=size)
            chunks.append(chunk)
        body = b"".join(chunks)
        if not body:
            raise web.HTTPBadRequest(text="empty upload")
        name = hashlib.sha256(body).hexdigest() + _extension(mime)
        path = files / name
        if not path.exists():
            tmp = files / f".{name}.{os.getpid()}.part"
            tmp.write_bytes(body)
            os.replace(tmp, path)
        return web.json_response({"href": FILES_ROUTE + name, "mime": mime, "size": len(body)})

    return handler


def _check_port(host: str, port: int) -> None:
    """Fail fast, before anything else starts, if the web port can't be bound."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
    except OSError as exc:
        print(f"Error: cannot listen on {host}:{port}: {exc.strerror or exc}", file=sys.stderr)
        raise SystemExit(1) from None


def _banner(cfg: dict[str, Any], config: str | Path | dict[str, Any], user: str | None = None,
            files: Path | None = None) -> str:
    """Startup summary: where the UI is and what it is running on."""
    host = cfg.get("host", "127.0.0.1")
    port = cfg.get("port", 8080)
    url_host = "localhost" if host in ("", "0.0.0.0", "::") else host
    if ":" in url_host:
        url_host = f"[{url_host}]"
    listen = f"{host}:{port}"
    if host in ("", "0.0.0.0", "::"):
        listen += " (all interfaces)"

    db_path = cfg.get("db_path", "mkio.db")
    database = "in-memory (nothing persists)" if db_path == ":memory:" else str(Path(db_path).resolve())
    config_desc = "<dict>" if isinstance(config, dict) else str(Path(config).resolve())

    lines = [
        f"mktask {__version__}",
        f"  Web UI:    http://{url_host}:{port}/",
        f"  Listening: {listen}",
        f"  Config:    {config_desc}",
        f"  Database:  {database}",
    ]
    if user is not None:
        lines.append(f"  Task IDs:  TK{user_prefix(user)}nnnnnnnn (user {user!r})")
    if files is not None:
        lines.append(f"  Files:     {files.resolve()}")
    lines.append("  Press Ctrl+C to stop.")
    return "\n".join(lines)


def _load_config(config: str | Path | dict[str, Any]) -> dict[str, Any]:
    """Load config, resolving the mkui static path and relative directories."""
    config_dir = Path(config).parent.resolve() if isinstance(config, (str, Path)) else Path.cwd()
    cfg = load_config(config)
    cfg["version"] = __version__

    statics = cfg.get("static", {})
    for route, directory in list(statics.items()):
        if directory == "__mkui__":
            import mkui
            statics[route] = str(mkui.static_dir)
        else:
            statics[route] = str((config_dir / directory).resolve())

    return cfg


def _find_config() -> str:
    """Look for mktask.toml in the current directory, then the package."""
    cwd = Path.cwd() / "mktask.toml"
    if cwd.exists():
        return str(cwd)

    pkg = Path(__file__).parent / "mktask.toml"
    if pkg.exists():
        return str(pkg)

    print("Error: mktask.toml not found. Provide a config path as argument.", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="mktask",
        description="Work task prioritizer built on mkio and mkui",
    )
    parser.add_argument(
        "config", nargs="?", default=None,
        help="path to mktask.toml config file (default: ./mktask.toml, else the built-in one)",
    )
    parser.add_argument(
        "-p", "--port", type=int, default=None,
        help="override listening port",
    )
    parser.add_argument(
        "--host", default=None,
        help="override listening host",
    )
    parser.add_argument(
        "-d", "--db", default=None, metavar="PATH",
        help="database filename (.db added if no extension; use ':memory:' for in-memory)",
    )
    parser.add_argument(
        "-u", "--user", default=None,
        help="username whose first two letters prefix new Task IDs (default: the OS login name)",
    )
    parser.add_argument(
        "--files", default=None, metavar="DIR",
        help="directory for uploaded reference files (default: <db>.files beside the database)",
    )
    parser.add_argument(
        "--version", action="version", version=f"mktask {__version__}",
    )
    args = parser.parse_args()

    db_path = args.db
    if db_path is not None and db_path != ":memory:" and not Path(db_path).suffix:
        db_path += ".db"

    config_path = args.config or _find_config()
    if not Path(config_path).is_file():
        print(f"Error: config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)
    serve(config_path, host=args.host, port=args.port, db_path=db_path, user=args.user, files_dir=args.files)


if __name__ == "__main__":
    main()
