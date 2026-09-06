# mktask

[![PyPI](https://img.shields.io/pypi/v/mktask)](https://pypi.org/project/mktask/)
[![Python](https://img.shields.io/pypi/pyversions/mktask)](https://pypi.org/project/mktask/)
[![License](https://img.shields.io/pypi/l/mktask)](https://github.com/markuskimius/mktask/blob/main/LICENSE)

A work task prioritizer built on [mkio](https://github.com/markuskimius/mkio)
(config-driven microservice backend) and
[mkui](https://github.com/markuskimius/mkui) (config-driven Web Components
workspace with dockable panes).

Tasks live in a local SQLite database and show up in a live-updating blotter
you can sort, filter, and arrange however you like. Each task carries an
importance and an urgency (1–5); the blotter derives a score from them so the
most pressing work floats to the top.

## Quick start

```bash
pip install mktask
mktask                    # http://127.0.0.1:8080/
```

Everything installs via `pip`; nothing is fetched at runtime.

## CLI

```
mktask [config] [-p PORT] [--host HOST] [-d PATH] [--version]
```

- `config` — path to a `mktask.toml`. Defaults to `./mktask.toml` if
  present, otherwise the one bundled with the package.
- `-p, --port` — override the listening port (default 8080).
- `--host` — override the listening host (default `127.0.0.1`).
- `-d, --db` — database file; `.db` is appended when there is no
  extension. `:memory:` runs without persistence.

## Customizing

Copy the bundled config out and edit it:

```bash
python -c "import mktask, pathlib; print(pathlib.Path(mktask.__file__).parent / 'mktask.toml')"
```

`mktask.toml` declares the SQLite tables, the mkio services, and the static
routes; `static/app.json` next to it declares the UI (menus, panes, frames,
dialogs). Both are plain config — see the mkio and mkui READMEs for the
formats.

## Development

```bash
pip install -e '.[test]'
mktask -d :memory:
python -m pytest
```

## License

Apache License 2.0. See [LICENSE](LICENSE).
