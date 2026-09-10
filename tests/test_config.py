"""Unit tests for config loading, config discovery, and CLI argument handling."""

import socket
import sys
from pathlib import Path
from unittest import mock

import pytest

import mktask
from mktask import __main__ as cli
from mktask import __version__

PKG_TOML = Path(mktask.__file__).parent / "mktask.toml"


class TestLoadConfig:
    def test_injects_version(self):
        cfg = cli._load_config(PKG_TOML)
        assert cfg["version"] == __version__

    def test_resolves_mkui_placeholder(self):
        import mkui
        cfg = cli._load_config(PKG_TOML)
        assert cfg["static"]["/mkui"] == str(mkui.static_dir)
        assert (Path(cfg["static"]["/mkui"]) / "src" / "index.js").is_file()

    def test_resolves_relative_static_against_toml_dir(self, tmp_path):
        toml = tmp_path / "custom.toml"
        toml.write_text('name = "x"\n[static]\n"/" = "./www"\n')
        cfg = cli._load_config(toml)
        assert cfg["static"]["/"] == str((tmp_path / "www").resolve())

    def test_relative_static_is_not_cwd_relative(self, tmp_path, monkeypatch):
        toml = tmp_path / "custom.toml"
        toml.write_text('name = "x"\n[static]\n"/" = "./www"\n')
        monkeypatch.chdir(tmp_path.parent)
        cfg = cli._load_config(toml)
        assert cfg["static"]["/"] == str((tmp_path / "www").resolve())

    def test_dict_config_resolves_against_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cfg = cli._load_config({"name": "x", "static": {"/": "./www", "/mkui": "__mkui__"}})
        assert cfg["static"]["/"] == str((tmp_path / "www").resolve())
        assert cfg["static"]["/mkui"].endswith("static")

    def test_bundled_defaults(self):
        cfg = cli._load_config(PKG_TOML)
        assert cfg["port"] == 8080
        assert cfg["host"] == "127.0.0.1"
        assert cfg["db_path"] == "mktask.db"
        assert (Path(cfg["static"]["/"]) / "index.html").is_file()


class TestFindConfig:
    def test_prefers_cwd(self, tmp_path, monkeypatch):
        local = tmp_path / "mktask.toml"
        local.write_text("")
        monkeypatch.chdir(tmp_path)
        assert cli._find_config() == str(local)

    def test_falls_back_to_package(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert cli._find_config() == str(PKG_TOML)

    def test_errors_when_nothing_found(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(cli, "__file__", str(tmp_path / "nowhere" / "__main__.py"))
        with pytest.raises(SystemExit) as exc:
            cli._find_config()
        assert exc.value.code == 1
        assert "mktask.toml not found" in capsys.readouterr().err


class TestMain:
    """`main()` parses arguments and hands them to `serve()`; serve is mocked."""

    def _run(self, monkeypatch, *argv):
        calls = []
        monkeypatch.setattr(cli, "serve", lambda *a, **kw: calls.append((a, kw)))
        monkeypatch.setattr(sys, "argv", ["mktask", *argv])
        cli.main()
        assert len(calls) == 1
        return calls[0]

    def test_defaults(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (a, kw) = self._run(monkeypatch)
        assert a == (str(PKG_TOML),)
        assert kw == {"host": None, "port": None, "db_path": None, "user": None, "files_dir": None}

    def test_port_short_and_long(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert self._run(monkeypatch, "-p", "9090")[1]["port"] == 9090
        assert self._run(monkeypatch, "--port", "9091")[1]["port"] == 9091

    def test_host(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert self._run(monkeypatch, "--host", "0.0.0.0")[1]["host"] == "0.0.0.0"

    def test_user_short_and_long(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert self._run(monkeypatch, "-u", "mark")[1]["user"] == "mark"
        assert self._run(monkeypatch, "--user", "bob")[1]["user"] == "bob"

    @pytest.mark.parametrize("given, expected", [
        ("work", "work.db"),
        ("work.db", "work.db"),
        ("work.sqlite", "work.sqlite"),
        (":memory:", ":memory:"),
        ("/tmp/x/work", "/tmp/x/work.db"),
    ])
    def test_db_suffix(self, tmp_path, monkeypatch, given, expected):
        monkeypatch.chdir(tmp_path)
        assert self._run(monkeypatch, "-d", given)[1]["db_path"] == expected

    def test_explicit_config_path(self, tmp_path, monkeypatch):
        toml = tmp_path / "mine.toml"
        toml.write_text("")
        (a, _) = self._run(monkeypatch, str(toml))
        assert a == (str(toml),)

    def test_files_dir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert self._run(monkeypatch, "--files", "/tmp/refs")[1]["files_dir"] == "/tmp/refs"

    def test_missing_explicit_config_exits(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(cli, "serve", lambda *a, **kw: pytest.fail("serve must not run"))
        monkeypatch.setattr(sys, "argv", ["mktask", str(tmp_path / "nope.toml")])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1
        assert "not found" in capsys.readouterr().err

    def test_non_integer_port_rejected(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["mktask", "-p", "eighty"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2


class TestServe:
    """`serve()` applies overrides to the loaded config before creating the app."""

    def _capture(self, monkeypatch, tmp_path):
        """Mock the app and run from a scratch directory: serve() creates the
        files directory beside the database, which must not land in the repo."""
        monkeypatch.chdir(tmp_path)
        captured = {}

        class FakeApp:
            services: dict = {}

            def on_startup(self, cb):
                captured["startup"] = cb

            def on_undo_redo(self, cb):
                captured["undo_redo"] = cb

            def run(self):
                captured["ran"] = True

        def fake_create_app(cfg, routes=None):
            captured["cfg"] = cfg
            captured["routes"] = routes
            return FakeApp()

        monkeypatch.setattr(cli, "create_app", fake_create_app)
        monkeypatch.setattr(cli, "_check_port", lambda host, port: captured.setdefault("probed", (host, port)))
        return captured

    def test_overrides_applied(self, monkeypatch, tmp_path):
        captured = self._capture(monkeypatch, tmp_path)
        cli.serve(PKG_TOML, host="0.0.0.0", port=9999, db_path=":memory:")
        cfg = captured["cfg"]
        assert (cfg["host"], cfg["port"], cfg["db_path"]) == ("0.0.0.0", 9999, ":memory:")
        assert captured["probed"] == ("0.0.0.0", 9999)
        assert cfg["version"] == __version__
        assert captured["ran"] is True

    def test_no_overrides_keeps_toml_values(self, monkeypatch, tmp_path):
        captured = self._capture(monkeypatch, tmp_path)
        cli.serve(PKG_TOML)
        cfg = captured["cfg"]
        assert (cfg["host"], cfg["port"], cfg["db_path"]) == ("127.0.0.1", 8080, "mktask.db")

    def test_user_becomes_the_task_id_prefix(self, monkeypatch, tmp_path):
        captured = self._capture(monkeypatch, tmp_path)
        cli.serve(PKG_TOML, user="mark")
        assert captured["cfg"]["services"]["tasks"]["prefix"] == "MA"

    def test_user_defaults_to_the_login_name(self, monkeypatch, tmp_path):
        captured = self._capture(monkeypatch, tmp_path)
        monkeypatch.setattr(cli.getpass, "getuser", lambda: "zed")
        cli.serve(PKG_TOML)
        assert captured["cfg"]["services"]["tasks"]["prefix"] == "ZE"
        assert "user" not in captured["cfg"], "the username is not an mkio config key"

    async def test_banner_printed_on_startup(self, monkeypatch, capsys, tmp_path):
        captured = self._capture(monkeypatch, tmp_path)
        cli.serve(PKG_TOML, port=1234, db_path=":memory:", user="mark")
        await captured["startup"]()
        out = capsys.readouterr().out
        assert f"mktask {__version__}" in out
        assert "http://127.0.0.1:1234/" in out
        assert "in-memory" in out
        assert "Task IDs:  TKMAnnnnnnnn (user 'mark')" in out

    def test_package_level_serve_delegates(self, monkeypatch, tmp_path):
        captured = self._capture(monkeypatch, tmp_path)
        mktask.serve(PKG_TOML, port=4321)
        assert captured["cfg"]["port"] == 4321

    def test_files_dir_sits_beside_the_database(self, monkeypatch, tmp_path):
        captured = self._capture(monkeypatch, tmp_path)
        cli.serve(PKG_TOML, db_path=str(tmp_path / "work.db"))
        files = tmp_path / "work.db.files"
        assert files.is_dir()
        assert captured["cfg"]["static"]["/files"] == str(files)
        assert captured["cfg"]["services"]["tasks"]["files_dir"] == str(files)
        assert [(m, p) for m, p, _ in captured["routes"]] == [("POST", "/files")]

    def test_files_dir_override(self, monkeypatch, tmp_path):
        captured = self._capture(monkeypatch, tmp_path)
        cli.serve(PKG_TOML, db_path=":memory:", files_dir=tmp_path / "elsewhere")
        assert (tmp_path / "elsewhere").is_dir()
        assert captured["cfg"]["static"]["/files"] == str(tmp_path / "elsewhere")

    def test_memory_database_gets_a_temp_files_dir(self, monkeypatch, tmp_path):
        captured = self._capture(monkeypatch, tmp_path)
        cli.serve(PKG_TOML, db_path=":memory:")
        files = Path(captured["cfg"]["static"]["/files"])
        assert files.is_dir()
        assert not files.is_relative_to(tmp_path), "not beside a database that does not exist"
        assert files.name.startswith("mktask-files-")


class TestCheckPort:
    def test_free_port_passes(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        cli._check_port("127.0.0.1", port)

    def test_busy_port_exits(self, capsys):
        with socket.socket() as blocker:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            port = blocker.getsockname()[1]
            with pytest.raises(SystemExit) as exc:
                cli._check_port("127.0.0.1", port)
        assert exc.value.code == 1
        assert f"cannot listen on 127.0.0.1:{port}" in capsys.readouterr().err

    def test_ipv6_host(self):
        cli._check_port("::1", 0)


class TestUpload:
    @pytest.mark.parametrize("mime, ext", [
        ("image/png", ".png"),
        ("image/jpeg", ".jpg"),
        ("text/plain", ".txt"),
        ("application/pdf", ".pdf"),
        ("message/rfc822", ".eml"),
        ("application/x-no-such-type", ".bin"),
    ])
    def test_extension_from_content_type(self, mime, ext):
        assert cli._extension(mime) == ext

    def test_upload_cap_is_twenty_megabytes(self):
        assert cli.MAX_UPLOAD == 20 * 1024 * 1024


class TestBanner:
    @pytest.mark.parametrize("host, url_host, listen_note", [
        ("127.0.0.1", "127.0.0.1", ""),
        ("0.0.0.0", "localhost", "(all interfaces)"),
        ("::", "localhost", "(all interfaces)"),
        ("::1", "[::1]", ""),
    ])
    def test_hosts(self, host, url_host, listen_note):
        text = cli._banner({"host": host, "port": 8080, "db_path": "x.db"}, "c.toml")
        assert f"http://{url_host}:8080/" in text
        assert listen_note in text

    def test_files_line(self, tmp_path):
        text = cli._banner({"host": "127.0.0.1", "port": 1, "db_path": ":memory:"}, {}, files=tmp_path / "f")
        assert f"Files:     {(tmp_path / 'f').resolve()}" in text
        assert "Files:" not in cli._banner({"host": "127.0.0.1", "port": 1, "db_path": ":memory:"}, {})

    def test_db_and_config_paths(self, tmp_path):
        text = cli._banner({"host": "127.0.0.1", "port": 1, "db_path": str(tmp_path / "t.db")},
                           tmp_path / "c.toml")
        assert str(tmp_path / "t.db") in text
        assert str(tmp_path / "c.toml") in text

    def test_dict_config(self):
        text = cli._banner({"host": "127.0.0.1", "port": 1, "db_path": ":memory:"}, {})
        assert "<dict>" in text
        assert "in-memory" in text
