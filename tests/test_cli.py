"""Tests for CLI argument handling."""

import subprocess
import sys

from mktask import __version__


def _run(*args):
    return subprocess.run(
        [sys.executable, "-m", "mktask", *args],
        capture_output=True, text=True,
    )


def test_help():
    result = _run("--help")
    assert result.returncode == 0
    # argparse wraps to the terminal's width, so a phrase can straddle a line
    text = " ".join(result.stdout.split())
    assert "task prioritizer" in text
    assert "web server" in text and "http://127.0.0.1:8080/" in text
    assert "--port" in text and "8080 as shipped" in text
    assert "--db" in text and "mktask.db in the current directory" in text
    assert "--user" in text and "Task IDs" in text
    assert "examples:" in text and "mktask -d :memory:" in text


def test_version():
    result = _run("--version")
    assert result.returncode == 0
    assert f"mktask {__version__}" in result.stdout


def test_bad_config():
    result = _run("/nonexistent/config.toml")
    assert result.returncode != 0
    assert "not found" in result.stderr


def test_help_defaults_match_shipped_config():
    # The help spells the defaults out, so it must follow the packaged TOML.
    import tomllib
    from pathlib import Path

    import mktask

    toml = Path(mktask.__file__).parent / "mktask.toml"
    cfg = tomllib.loads(toml.read_text(encoding="utf-8"))
    text = " ".join(_run("--help").stdout.split())
    assert f"{cfg['port']} as shipped" in text
    assert f"{cfg['host']} as shipped" in text
    assert f"http://{cfg['host']}:{cfg['port']}/" in text
    assert f"{cfg['db_path']} in the current directory" in text


def test_usage_names_every_option():
    # The usage line is written by hand; an option added later must join it.
    import re

    usage, _, rest = _run("--help").stdout.partition("\n\n")
    options = rest[rest.index("options:"):rest.index("examples:")]
    flags = set(re.findall(r"(?<![\w-])--[a-z]+", options))
    assert flags >= {"--port", "--host", "--db", "--user", "--files"}
    for flag in flags:
        short = re.search(rf"(-[a-z]), {flag}\b", options)
        assert (short.group(1) if short else flag) in usage or flag in usage, flag
    assert usage.index("[config]") < usage.index("[-p PORT]")


def test_bad_flag():
    result = _run("--bogus")
    assert result.returncode == 2
    assert result.stderr.startswith("usage: mktask [config]")
    assert "unrecognized arguments: --bogus" in result.stderr
