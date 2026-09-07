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

A task can be **split** into child tasks, and a child can be split again, to
any depth. The blotter nests children under their parent, each level sorted
by score, with carets to fold a subtree away. Completing a task completes
everything split from it; reopening a child reopens its ancestors; deleting a
task deletes its whole subtree.

Every task gets a **Task ID** like `TKMA00000042`: `TK`, two letters from
the username the server runs as (`--user`), and a sequence number that
starts at `00000001`, never repeats, and grows past eight digits rather than
wrapping (`TKMA100000000` follows `TKMA99999999`).

A task carries **references**: the things to refer back to while working on
it. A reference is a URL (including `mailto:` and mail-client links), a
pasted text snippet (an email, a chat exchange), a file (a screenshot, a
PDF), or a **link to another task** with a relation (*blocks*, *blocked by*,
*relates to*). Add a URL or snippet with the Reference button, link tasks
with the Link button, or select a task and drop, paste, or pick a file in
the Detail pane: an image pastes straight from the clipboard, a pasted URL
becomes a URL reference, pasted text a snippet. The References pane follows
the selected task (clear the selection to see every reference), with URLs
and files as links; selecting a reference previews it in the Detail pane,
an image inline. A task link shows on both tasks,
worded from each side, and *Go to* opens the linked task, with its own
references, in a Linked Task window. Uploaded files live beside the
database in `<db>.files/`, named by content hash; deleting the last
reference to a file, or its task, removes the file.

## Quick start

```bash
pip install mktask
mktask                    # http://127.0.0.1:8080/
```

Everything installs via `pip`; nothing is fetched at runtime.

## CLI

```
mktask [config] [-p PORT] [--host HOST] [-d PATH] [-u USER] [--files DIR] [--version]
```

- `config` — path to a `mktask.toml`. Defaults to `./mktask.toml` if
  present, otherwise the one bundled with the package.
- `-p, --port` — override the listening port (default 8080).
- `--host` — override the listening host (default `127.0.0.1`).
- `-d, --db` — database file; `.db` is appended when there is no
  extension. `:memory:` runs without persistence.
- `-u, --user` — the username whose first two alphanumeric characters,
  uppercased, prefix new Task IDs (`mark` → `TKMA…`; a one-character name is
  padded with `X`). Defaults to the OS login name.
- `--files` — directory for uploaded reference files, served at `/files`.
  Defaults to `<db>.files` beside the database (`mktask.db.files/`), a
  temporary directory for `:memory:`.

The server prints the URL to open once it is listening. If the port is
already taken it exits immediately with an error instead of starting.

## Customizing

Copy the bundled config out and edit it:

```bash
python -c "import mktask, pathlib; print(pathlib.Path(mktask.__file__).parent / 'mktask.toml')"
```

`mktask.toml` declares the SQLite tables, the mkio services, and the static
routes; `static/app.json` next to it declares the UI (menus, panes, frames,
dialogs). Both are plain config — see the mkio and mkui READMEs for the
formats. The one piece of code is `mktask/services.py`, which the `tasks`
service points at: it assigns Task IDs, runs the complete, reopen, and
delete cascades, and manages references (task links are written as a
mirrored pair, labels follow the linked task's title, orphaned files are
removed). `static/refs.js` is the one custom widget: the drop box, the
preview, and the Linked Task viewer.

## Upgrading

Until 1.0, a release may change the database schema without migrating an
older database. 0.2.0 did (Task IDs, splitting, and `complete` replacing
`done`): delete a 0.1.0 `mktask.db` before starting a newer version. 0.3.0
only adds the `task_refs` table, which `auto_migrate` creates in an existing
database, so a 0.2.0 database carries over as is.

## Development

```bash
pip install -e '.[test]'
mktask -d :memory:
python -m pytest
```

The tests cover the CLI and config loading, Task ID formatting, a real
server over HTTP and WebSocket (every task op, splitting and the cascades,
the Task ID sequence across restarts and past eight digits, references and
task links with their validation and cascades, file upload, dedupe, and
cleanup, live delete announcements, the query filter, saved layouts, the
port, host, user, and files flags), and the static integrity of `app.json`
against `mktask.toml` and `refs.js`.

## License

Apache License 2.0. See [LICENSE](LICENSE).
