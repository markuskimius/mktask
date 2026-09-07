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

**Move** re-files a task: pick a new parent, or *— Top level —* to make it a
top-level task again. Everything split from it comes along. The picker offers
every open task except the ones under the task being moved, and a move that
would land a linked task in its own tree is refused, naming the links to
remove first.

Every task gets a **Task ID** like `TKMA00000042`: `TK`, two letters from
the username the server runs as (`--user`), and a sequence number that
starts at `00000001`, never repeats, and grows past eight digits rather than
wrapping (`TKMA100000000` follows `TKMA99999999`).

A task carries **references**: the things to refer back to while working on
it. A reference is a URL (including `mailto:` and mail-client links), a
pasted text snippet (an email, a chat exchange), a file (a screenshot, a
PDF), or a **link to another task** with a **relation**. One Reference
button adds any of them: pick the kind and the form follows, with the
label suggested from the URL or the first line of the text. Files are
added from the Detail pane instead: select a task and drop, paste, or pick
a file there; an image pastes straight from the clipboard, a pasted URL
becomes a URL reference, pasted text a snippet. The Detail pane opens with
the task itself — its status, importance, urgency, score, due date and notes
— and lists every reference of the selected task the moment it is selected,
**and every reference of the tasks split from it** — a section per relation ("Blocks",
"Blocked by"), then the snippets, images, files, and URLs, each section
counted, and a reference a child owns tagged with its Task ID. **Images show
as a grid of thumbnails**; clicking one opens it full size under the grid, and
clicking a snippet or a URL opens it where it stands. The subtree comes from
the tasks themselves, not from what the blotter is showing, so a completed
child's references stay with its ancestor.

**The Detail pane edits what it shows.** It has its own toolbar, and one
cursor: click the task block or click a reference. *Edit* opens the dialog
that matches — the same Edit dialog the blotter opens for a task, the same
one the References pane opens for a reference — and *Delete* removes a
reference (a task is deleted from the blotter, where the row being deleted
is the row that was picked). An edit shows up here the moment the server
announces it.

The References pane follows the
selected tasks (mkui table linking; clear the selection to see every
reference), with URLs and files as links; selecting one there opens and marks
the same line in the Detail pane, and a reference the Detail pane does not
list leaves it alone. A task link shows on both tasks, worded from each side,
and *Go to* selects the linked task in the blotter, so the Detail and
References panes follow it (a linked task the current filter hides is revealed
first). Only tasks from different trees can be linked; tasks that share a root
are already related by splitting.

**Relations are yours to define** under Tasks › Relations: each is a pair
of wordings, one from this task to that one ("blocks") and one back
("blocked by"), or a single wording that reads the same both ways ("relates
to"). A new database starts with those two. Renaming a relation rewrites
every link that uses it; a relation in use cannot be deleted. Uploaded
files live beside the database in `<db>.files/`, named by content hash;
deleting the last reference to a file, or its task, removes the file.

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
routes (keep `relations.json`, the seed for the relations table, beside a
copied config); `static/app.json` next to it declares the UI (menus, panes,
frames, dialogs). Both are plain config — see the mkio and mkui READMEs for the
formats. The one piece of code is `mktask/services.py`, which the `tasks`
service points at: it assigns Task IDs, runs the complete, reopen, and
delete cascades, re-parents a task on a move (refusing a cycle or a move
that would swallow a task link), and manages references and relations (task links are
written as a mirrored pair, labels follow the linked task's title, a
renamed relation rewrites its links, orphaned files are removed).
`static/refs.js` is the one custom widget and the whole Detail body: the
toolbar, the task block, the drop box, the reference list of the selected
task and its descendants (its own live queries), and *Go to*. Its Edit and
Delete open the dialogs the Tasks and References panes already declare,
borrowed from `app.json` by pane and button name rather than copied.

## Upgrading

Until 1.0, a release may change the database schema without migrating an
older database. 0.2.0 did (Task IDs, splitting, and `complete` replacing
`done`): delete a 0.1.0 `mktask.db` before starting a newer version. 0.3.0
only adds the `task_refs` table, which `auto_migrate` creates in an existing
database, so a 0.2.0 database carries over as is. 0.3.1 changes no schema.
0.4.0 adds the `relations` table (created and seeded on first start) and
stores a link's relation as its wording ("blocked by") rather than a key
(`blocked_by`): links made before 0.4.0 must be removed and re-added. 0.5.0
changes no schema — the Detail pane's new reference list reads the tables
0.4.0 already had, and neither does 0.6.0: moving a task rewrites one
existing column. 0.7.0 changes no schema either: the Detail pane's toolbar
sends the ops that were already there.

## Development

```bash
pip install -e '.[test]'
mktask -d :memory:
python -m pytest
```

The tests cover the CLI and config loading, Task ID formatting, a real
server over HTTP and WebSocket (every task op, splitting and the cascades,
moving a task to another parent or the top level, the Task ID sequence
across restarts and past eight digits, references and task links with their
validation, the same-tree rule, and cascades,
user-defined relations (seeding, uniqueness, rename rewriting links in both
directions, a swap flipping them, delete refused in use), file upload,
dedupe, and cleanup, live delete announcements, the query filter, saved
layouts, the port, host, user, and files flags), and the static integrity of
`app.json` against `mktask.toml` and `refs.js` (every column, dialog field,
service, and state path a pane names, the Detail pane's task block and the
dialogs its toolbar borrows, and the reference sections against the kinds
the server accepts).

## License

Apache License 2.0. See [LICENSE](LICENSE).
