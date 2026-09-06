# mktask — skeleton application plan

mktask is a work-task prioritizer built on [mkio](https://github.com/markuskimius/mkio)
(TOML-driven backend: SQLite + WebSocket services) and
[mkui](https://github.com/markuskimius/mkui) (config-driven Web Components
workspace). Apache-2.0, published to PyPI as `mktask` (name is free on PyPI
as of 2026-09-05).

The structure follows `../mkfix`, the same author's existing mkio+mkui app:
one Python package that bundles its server TOML and static UI, a `mktask`
console script that runs the server, and mkui's assets served from the
installed package rather than copied into the repo.

**Status (2026-09-05):** All five phases are done. v0.1.0 is published to PyPI
and the repo is on GitHub; 75 tests pass and the UI was exercised end to end
in a browser.

**Status (2026-09-06):** Phase 6 (splitting, Task IDs, Complete) shipped as
v0.2.0: 124 tests pass and the tree, Split dialog, cascades, and the red
Delete button were exercised in a browser.

## Target layout

```
mktask/
  __init__.py          __version__ (single source of truth) + lazy serve()
  __main__.py          CLI (argparse) → mkio create_app(); resolves "__mkui__"
  mktask.toml          tables, services, static routes (no version key)
  static/
    index.html         loads /mkui/src/index.js, fetches app.json, sets mkio.url
    app.json           mkui config: menubar, statusbar, panes, frames, layouts
    mktask.css         app-specific overrides (may start empty)
tests/
  test_cli.py          --help, --version, bad config path
  test_server.py       boots on a free port with -d :memory:, exercises services over WS
  test_ui_config.py    app.json integrity: pane refs, service names, index.html imports
pyproject.toml         hatchling, dynamic version, deps mkio>=0.2.0 mkui>=0.2.9
LICENSE                Apache-2.0
README.md              badges, quick start, screenshot placeholder
CLAUDE.md              project notes for future sessions
.gitignore             __pycache__, *.db*, *.egg-info, dist/, build/
```

## Phase 1 — repository scaffolding

1. `LICENSE`: Apache License 2.0 full text, copyright Mark Kim.
2. `pyproject.toml`: hatchling build; `name = "mktask"`, `dynamic = ["version"]`
   read from `mktask/__init__.py`; `license = "Apache-2.0"`;
   `requires-python = ">=3.11"` (mkio's floor); deps `mkio>=0.2.0`,
   `mkui>=0.2.9`; optional `test = [pytest, pytest-asyncio, pytest-aiohttp]`;
   `[project.scripts] mktask = "mktask.__main__:main"`; classifiers include
   `License :: OSI Approved :: Apache Software License` and
   `Development Status :: 3 - Alpha`; URLs to github.com/markuskimius/mktask.
   Hatch includes non-.py files under the package by default, so
   `mktask.toml` and `static/` ship in the wheel without extra config.
3. `mktask/__init__.py`: `__version__ = "0.1.0"` and a lazy `serve()` wrapper.
4. `.gitignore`, `README.md` (short for now), `CLAUDE.md` (layout + commands).
5. Install editable: `pip install -e '.[test]'`. Commit as "Scaffold package".

## Phase 2 — server (`mktask.toml` + `__main__.py`)

Data model for the skeleton. Keep it deliberately small; the prioritization
model is the app's real subject and should be designed after the skeleton
runs.

```toml
name = "mktask"
port = 8080
host = "127.0.0.1"        # personal tool: loopback by default
db_path = "mktask.db"
auto_migrate = true

[tables.tasks]
columns = { id = "INTEGER PRIMARY KEY AUTOINCREMENT",
            title = "TEXT NOT NULL",
            notes = "TEXT DEFAULT ''",
            status = "TEXT NOT NULL DEFAULT 'open'",     # open | done
            importance = "INTEGER NOT NULL DEFAULT 3",   # 1..5
            urgency = "INTEGER NOT NULL DEFAULT 3",      # 1..5
            due = "TEXT DEFAULT ''",                     # ISO date or ''
            created_at = "TEXT DEFAULT CURRENT_TIMESTAMP",
            updated_at = "TEXT DEFAULT CURRENT_TIMESTAMP",
            done_at = "TEXT DEFAULT ''" }

[tables.mkui_layouts]   # copied verbatim from mkui's scaffold (saved layouts)
```

Services:

- `tasks` — `protocol = "transaction"` with ops `add` (insert: title, notes,
  importance, urgency, due, with explicit `defaults` for every optional
  field), `edit` (update by id), `done` (update status/done_at by id),
  `reopen`, `delete` (by id). One transaction service with several ops keeps
  the UI's action names in one place.
- `all_tasks` — `protocol = "query"`, `primary_table = "tasks"`,
  `filterable = ["status"]`. The blotter subscribes to this.
- `mkui_layouts`, `mkui_layouts_list`, `mkui_layouts_get` — verbatim from
  mkui's `init` scaffold so the Layout menu works. mkfix runs without login
  and stores layouts under owner `''`; do the same.

Static routes: `"/" = "./static"`, `"/mkui" = "__mkui__"`. No `[config]`
route: like mkfix, the UI is JSON (`app.json`) fetched by `index.html`, so
no TOML→JSON serving is needed.

`__main__.py` mirrors mkfix's without the FIX engine:

- `_load_config(path)`: `mkio.config.load_config`, inject
  `cfg["version"] = __version__`, resolve `"__mkui__"` to `mkui.static_dir`
  and relative static dirs against the TOML's directory.
- `_find_config()`: `./mktask.toml` if present, else the packaged one. This
  lets a user copy the TOML out and customize it.
- `serve(config, host, port, db_path)`: `create_app(cfg)` then `app.run()`.
  Skip mkfix's port pre-probe and custom banner initially; add them only if
  the default `run()` output proves insufficient.
- `main()`: positional optional `config`, `-p/--port`, `--host`, `-d/--db`
  (`.db` appended when no suffix, `:memory:` passthrough), `--version`.

Verify: `mktask -d :memory:` serves `http://127.0.0.1:8080/`, `/mkui/src/index.js`
returns JS, and `mkio ls` (or a WS client) lists the services. Commit.

## Phase 3 — UI (`static/`)

`index.html`: copy mkfix's pattern — import `/mkui/src/index.js`, fetch
`app.json` with `cache: "no-cache"`, set `config.mkio.url` from
`location.host`, call `setConfig`. No custom pane modules yet.

`app.json`:

- `app`: title "mktask", theme dark.
- `state`: `status.message/background/color`, `selected_task: null`.
- `menubar`: Edit (copy, select all), Tasks (Open Tasks, All Tasks, Done via
  `pane.show`; a `table.filter` entry "Hide done"), Layout (save, restore
  submenu, reset), Window (cascade, tile submenu, `windows: true`).
- `statusbar`: left `status.message`; right version text; `bindStyle` on
  `status.*`.
- `mkio`: `connected`/`disconnected` state maps; `expect = { name = "mktask" }`
  (do not pin `version`, mkfix's tests exist precisely because a pinned
  version leaks past releases).
- `layouts`: `{ keep = 10, keepDays = 7 }`.
- `panes.tasks`: `mkio-table` on `all_tasks`, columns id, title, importance,
  urgency, score, due, status, created_at; `values.score` derives
  `importance * urgency` as a virtual column (listed in `columns`);
  `sort = "-score"`; `filters.status = ["open"]`; `rowStyle` greys done
  rows; `select = { state = "selected_task" }`.
  Toolbar buttons: "+ Add Task" (dialog → `tasks` op `add`), "Edit"
  (`unit = "row"`, dialog prefilled from the row → op `edit`), "Done"
  (`unit = "rows"`, transaction op `done`, `enable.when` status == open),
  "Reopen", "Delete" (transaction op `delete`, confirm). Copy the dialog and
  transaction action shapes from mkfix's `app.json` and mkui's `init`
  scaffold.
- `panes.task-detail`: a `text` widget bound to `selected_task.notes` as a
  placeholder for a future custom pane.
- `frames`: one main frame with tasks (0.03, 0.05, 0.64, 0.9) and one aux
  frame with task-detail.

`mktask.css`: empty file with a comment, imported by index.html so the hook
exists.

Verify in a browser: add, edit, complete, reopen, delete a task; rows
flash live; saved layouts round-trip after reload. Commit.

## Phase 4 — tests

- `test_cli.py`: as mkfix's — help text, version string, nonexistent
  config exits non-zero.
- `test_server.py`: module-scoped fixture launches `python -m mktask -d
  :memory: -p <free port>`; assert `/` and `/mkui/src/index.js` are 200;
  over WS, `add` a task then `all_tasks` query returns it; `done` flips
  status; `delete` removes it.
- `test_ui_config.py`: every `pane.show` arg and frame layout child names a
  pane; every `mkio-table` `service` and every transaction/dialog service
  exists in `mktask.toml`; every `import "..."` in index.html resolves under
  `static/` or `/mkui/`; `mkio.expect` has no `version`.

`python -m pytest` green. Commit.

## Phase 5 — publish

1. README: badges (PyPI, Python, License), one-paragraph pitch, quick start
   (`pip install mktask` / `mktask`), CLI flags, how to customize the TOML,
   screenshot placeholder, license section.
2. Tag `v0.1.0`; `python -m build`; upload to TestPyPI first, install into a
   clean venv, run `mktask -d :memory:`; then `twine upload dist/*`.
3. Create the GitHub repo `markuskimius/mktask`, push, add the PyPI link.

## Phase 6 — splitting tasks, Task IDs, "Complete" (2026-09-06)

Vocabulary: a task is *split* into *child* tasks, to any depth; a task is
*open* or *complete*; its identifier is its *Task ID*. No "subtask", "done",
or bare "ID" anywhere.

- **Schema.** `tasks.task_id TEXT PRIMARY KEY`, `parent_task_id` (`''` =
  top level), `completed_at` replaces `done_at`, status `complete` replaces
  `done`. New `counters` table (`name`, `last`) holds the Task ID sequence.
  No migration from 0.1.0: before 1.0 a schema change means deleting the
  database.
- **Task IDs.** `TK` + two letters from the username (`--user`, default the
  OS login; alphanumerics, uppercased, `X`-padded) + a global sequence
  padded to at least 8 digits that never repeats and never wraps. Assigned
  by `mktask/services.py` (`TaskTransactions`, a mkio `TransactionService`
  subclass named by dotted path in the TOML), which upserts `counters` and
  inserts the task in one transaction and rejects a client-supplied id. The
  counter seeds from `counters` at start, else from the largest existing id.
- **Cascades.** In the same class: `complete` completes the subtree,
  `reopen` reopens the task and its ancestors, `delete` removes the subtree —
  each as one `writer.submit` over the rows a recursive CTE finds, so every
  row is announced live.
- **UI.** mkui ≥ 0.2.12 tree rows on the `tasks` pane (`child =
  parent_task_id`, `parent = task_id`, caret on `task_id`, `expand = "all"`,
  `filterScope = "all"` so "Show Open Only" tests every row but keeps the way
  to a match). Split button and dialog (hidden `parent_task_id`, levels
  prefilled from the parent), Complete/Reopen buttons, Expand All / Collapse
  All menu items (`table.expand`), "Split from Task ID …" line in the detail
  pane, `Task ID` column label.
- **Tests.** `test_services.py` (prefix, formatting, pattern),
  `test_server.py` (sequence rejection / no reuse / restart / rollover past
  8 digits / lost counter, split links, cascades, `--user` prefixes),
  `test_ui_config.py` (tree links real columns, no dialog sends `task_id` or
  `last`, the word "done" is gone).

### Deferred from phase 6

- **Move** a task under another parent: a `parent_task_id` update plus a
  cycle check (refuse a target inside the node's own subtree); the client
  re-indexes on the update.
- **Auto-complete a split parent** when its last open child completes
  (server-side, in `TaskTransactions`); today a parent stays open.
- A **children count** or `depth` in style scopes to dim a split parent's
  score — needs mkui to expose them.
- A **Subtasks pane** following the selection (`parent_task_id` is already
  server-filterable).
- Persisting tree **expansion state** in saved layouts (mkui).

## Deferred (not in the skeleton)

- The prioritization model itself: weighted scoring, Eisenhower quadrants,
  aging/due-date decay, projects/tags, recurring tasks. The `score` derived
  column is the placeholder where this lands; it may move server-side into a
  `reqrep` or computed column later.
- Custom pane types (task detail editor, quadrant board, calendar).
- Authentication (mkui `auth` + mkio `_mkio_users`) if the app is ever
  multi-user; the per-owner layouts scaffold already supports it.
- CI workflow (mkui and mkfix have none; add GitHub Actions if wanted).

## Assumptions to confirm

- Python >= 3.11 is acceptable (forced by mkio).
- Single-user, no login, loopback host by default.
- `importance`/`urgency` 1..5 integers are a fine placeholder schema for the
  skeleton; the real model comes later.
