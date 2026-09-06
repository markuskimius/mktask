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
    assert "task prioritizer" in result.stdout
    assert "--port" in result.stdout
    assert "--db" in result.stdout


def test_version():
    result = _run("--version")
    assert result.returncode == 0
    assert f"mktask {__version__}" in result.stdout


def test_bad_config():
    result = _run("/nonexistent/config.toml")
    assert result.returncode != 0
    assert "not found" in result.stderr
