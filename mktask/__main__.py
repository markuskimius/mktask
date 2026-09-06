"""mktask CLI and server entry point."""

from __future__ import annotations

import argparse
import getpass
import socket
import sys
from pathlib import Path
from typing import Any

from mkio import create_app
from mkio.config import load_config

from mktask import __version__
from mktask.services import user_prefix


def serve(
    config: str | Path | dict[str, Any] = "mktask.toml",
    host: str | None = None,
    port: int | None = None,
    db_path: str | None = None,
    user: str | None = None,
) -> None:
    """Start the mktask server. Blocks until shutdown.

    `user` (default: the OS login name) supplies the two prefix letters of
    every Task ID this server assigns.
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
    if "tasks" in cfg.get("services", {}):
        cfg["services"]["tasks"]["prefix"] = user_prefix(user)

    # Probe the port before anything else starts: a bind failure inside
    # app.start() happens after the startup hooks have opened the database,
    # whose aiosqlite threads then keep the process alive after the traceback.
    _check_port(cfg["host"], cfg["port"])

    app = create_app(cfg)

    async def announce() -> None:
        print(_banner(cfg, config, user), flush=True)

    app.on_startup(announce)
    app.run()


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


def _banner(cfg: dict[str, Any], config: str | Path | dict[str, Any], user: str | None = None) -> str:
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
    serve(config_path, host=args.host, port=args.port, db_path=db_path, user=args.user)


if __name__ == "__main__":
    main()
