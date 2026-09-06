"""mktask CLI and server entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from mkio import create_app
from mkio.config import load_config

from mktask import __version__


def serve(
    config: str | Path | dict[str, Any] = "mktask.toml",
    host: str | None = None,
    port: int | None = None,
    db_path: str | None = None,
) -> None:
    """Start the mktask server. Blocks until shutdown."""
    cfg = _load_config(config)
    if host is not None:
        cfg["host"] = host
    if port is not None:
        cfg["port"] = port
    if db_path is not None:
        cfg["db_path"] = db_path

    app = create_app(cfg)

    async def announce() -> None:
        print(_banner(cfg, config), flush=True)

    app.on_startup(announce)
    app.run()


def _banner(cfg: dict[str, Any], config: str | Path | dict[str, Any]) -> str:
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

    return "\n".join([
        f"mktask {__version__}",
        f"  Web UI:    http://{url_host}:{port}/",
        f"  Listening: {listen}",
        f"  Config:    {config_desc}",
        f"  Database:  {database}",
        "  Press Ctrl+C to stop.",
    ])


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
    serve(config_path, host=args.host, port=args.port, db_path=db_path)


if __name__ == "__main__":
    main()
