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

**Status (2026-09-06):** Phase 7 (references, task links, file uploads, the
References pane following the selection) shipped as v0.3.0: 168 tests pass
and every flow was exercised in a browser. v0.3.1 (2026-09-07) moved the
selection follow onto mkui 0.2.19's table linking.

**Status (2026-09-07):** Phase 8 (user-defined relations, one Reference
dialog) built on mkui 0.2.22: 175 tests pass, exercised in a browser.
Phase 9 (Go to selects the task; the Linked Task window removed) built on
mkui 0.2.23. Phases 8 and 9 shipped together as v0.4.0: 180 tests pass,
every flow exercised in a browser.

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

## Phase 7 — references and task links (2026-09-06)

A task carries zero or more *references*: things to refer back to — a URL,
an email, a pasted snippet of a conversation, a screenshot, or another task.
Vocabulary: *reference* (never "attachment"); *link* means only a
task-to-task reference, with a *relation*; the other end is the *linked
task*.

- **One table, `task_refs`**, for every kind, task links included. The
  first draft had a separate `task_links` table joined to `tasks` for the
  linked title, but mkio's query service cannot keep a JOIN live (it
  publishes the bare primary-table row and drops secondary-table changes),
  and a UNION of two tables has the same problem. "Everything a task points
  at, live, in one pane" therefore needs one plain table. Columns: `ref_id`
  (AUTOINCREMENT), `task_id` (owner), `kind` (`url` | `text` | `file` |
  `task`), `relation` (task links only), `label`, `href` (URL, file path,
  or linked Task ID by kind), `body` (snippets), `mime` (files),
  timestamps. One-to-many: a reference belongs to one task.
- **A task link is two mirrored rows**, one per side with inverse relations
  (`blocks` / `blocked_by`, `relates` / `relates`), written and deleted
  together, so a pane filtered on `task_id` shows every link from that
  task's side with no client logic. The label carries the linked task's
  title and `edit` on a task rewrites it on every row that points there
  (same transaction). `add_ref` refuses a self-link, an unknown relation or
  task, and a duplicate, writing nothing. A task delete removes rows the
  subtree owns *or is linked to*.
- **Files on disk, not in SQLite** (query subscribers receive whole rows):
  `<db>.files/` beside the database (`mktask.db.files/`, covered by the
  `*.db.files/` gitignore entry; a temp dir for `:memory:`), `--files DIR` to
  override — a CLI/`serve()` argument like `user`, never a TOML key.
  `POST /files` (a custom aiohttp route via `create_app(routes=…)`) stores
  the raw body as `<sha256>.<ext>` under a 20 MB cap, streamed past
  aiohttp's 1 MB `client_max_size`; the rows are the only tracking of a
  file, and `delete_ref` / task delete unlink a file no row names any more.
- **The live-delete bug.** mkio announces a delete with the *request's*
  data, so the phase-6 subtree delete announced the parent's Task ID for
  every row and a live table kept the children until reload. Deletes now go
  one request per row (`_submit_each`, gathered into one batch);
  `test_subtree_delete_announces_every_row` pins it.
- **UI.** `Reference` (URL or snippet; `showWhen` on `kind`) and `Link`
  (relation + a task picker fed by the `task_options` reqrep through
  `optionsFrom`, so the user sees titles and never the task itself) buttons
  on the Tasks pane; two buttons because a dialog cannot have two fields
  named `href`. A References pane under Tasks (vertical split): `LINK` on
  the label for URLs and files, relations worded per side, Edit gated off
  task links, red Delete with the readonly confirmation. `static/refs.js`
  registers the `task-refs` widget: in the Detail pane a drop box (drop,
  paste, or click to pick a file → upload → `add_ref`; a pasted URL or text
  becomes a reference with no upload; the paste listener is on `window`,
  active while a task is selected and no text field has focus) and a
  preview of the reference selected in the References pane (image inline,
  snippet in full, "Go to" on a task link). "Go to" fetches the task
  (`task_get`), publishes `state.linked_task`, and `pane.show`s the Linked
  Task pane — text widgets plus `task-refs` in `mode = "linked"` listing
  that task's references (`task_refs_get`, refetched per task) with their
  own "Go to". mkui has no select-a-row action, so "Go to" opens a viewer
  rather than moving the Tasks selection; panes are config singletons, so
  it is one window replaced per jump.
- **Tests.** `test_server.py`: kinds and label defaults, validation, edit
  rules (a file's href is fixed, a task link is not editable), delete_ref,
  task delete cascade, server-side filter, both sides of a link, unlink
  from either side, relabel, delete either end, `task_options`, upload
  round-trip / dedupe / 400 / 413 / extension mapping / cleanup, the live
  delete announcement. `test_config.py`: `--files`, files dir derivation
  (beside the db, override, temp for memory), the route list.
  `test_ui_config.py` generalized to every table pane, plus the dialogs,
  the widget registration, `refs.js` against declared services and state,
  and the frame/menu reachability rule. Exercised in a browser: Reference
  and Link dialogs, synthetic paste of a PNG and of text, inline image
  preview, Go to, Edit, Delete of one side of a link.

### Deferred from phase 7

- **mkui**: a `_select` hook and `table.select` action so "Go to" can
  move the Tasks selection; a `file` dialog field type so the drop box
  could retire. (Table linking landed in mkui 0.2.17–0.2.19 and replaced
  the widget's `table.filter` workaround: `link.broadcast` on Tasks,
  `link.listen` on References, `chips = false` on both.)
- **Prioritization hook**: `blocked_by` an open task should lower the
  blocked task's score. The mirrored rows make "is this task blocked" one
  filtered read.
- A per-task Linked Task window (panes are singletons today).
- Auth on `POST /files` if the app ever leaves loopback.

## Phase 8 — user-defined relations, one Reference dialog (2026-09-07)

Relations between tasks are data the user edits, not a constant in the
service; every kind of reference, task links included, goes through one
dialog. Needs mkui 0.2.22 (live dialogs).

- **`relations` table**, one row per pair: `forward` ("blocks"),
  `backward` ("blocked by"; equal to `forward` for "relates to"), `notes`.
  Every wording unique across both columns, case-insensitively
  (service-enforced). Seeded from `relations.json` in the package through
  mkio's `seed` key, which fires only when the table is first created, so
  a user who deletes the defaults is not fought.
- **Links store the wording** in `task_refs.relation`, not an id: no join
  (mkio cannot keep one live), and a rename rewrites the rows the way link
  labels already follow a title. `add_ref` looks the wording up for the
  mirror's inverse; `edit_ref` on a link changes the relation on both
  halves; `edit_relation` rewrites every link carrying an old wording in
  the same submit (folding to symmetric included); `delete_relation` is
  refused while any link uses the pair. The tree rule stays.
- **One Reference dialog.** `kind` select URL / Text / Task; scratch
  inputs `_url` and `_task` (never submitted) feed a hidden `href` through
  `compute`, so the server still sees one field and no dialog names two
  fields alike; `label` is a live suggestion (`compute` until typed in);
  title and footer note are templates. The Edit dialog on References
  became one form too (relation select for a link, readonly label). The
  relation display template and the widget's word map went. Files stay
  with the drop box (mkui has no file field).
- **Relations pane** (`all_relations`; New / Edit / red Delete), opened
  from the Tasks menu. `backward`'s placeholder is a `${forward}` template
  so a new relation reads as symmetric until a reverse wording is typed.
  `relation_options` feeds the dialogs' relation selects.
- **mkui 0.2.21 regression**: a readonly field without a name rendered
  blank; worked around with scratch names, then dropped again once mkui
  0.2.22 fixed it (the package pins 0.2.22).
- **Tests.** 175: seeding, `relation_options`, add/edit/delete with
  uniqueness and the symmetric default, rename rewriting links on both
  sides and both directions, delete refused in use, `edit_ref` changing a
  link's relation, unknown wordings rejected, the single dialog's fields
  and computed `href`, the Relations pane, the seed file. Browser: the
  dialog reshaping by kind, the live label, a link added, a relation
  created, a delete refused inline, a rename following into the link.

## Phase 9 — "Go to" selects the task; the Linked Task window goes (2026-09-07)

mkui 0.2.23 added `table.select` (`{ pane, keys, focus }` → the table's
`_select` hook → `selectRows`), so "open the linked task" can mean what it
should have meant from the start: select it in the Tasks pane. Everything
that followed a click already follows a programmatic selection —
`selectRows` runs the same `publishSelection`, so `state.selected_task`
updates and the References pane's link filter re-broadcasts. The Linked
Task window was the workaround for not having this; it goes.

- **Go to** (on the task-link preview in the Detail pane) becomes
  `app.fireAction("table.select", { pane: "tasks", keys: [taskId] })`.
  `fireAction` returns the table's `{ ok, selected, missing, hidden }`:
  `selected` needs nothing more (the row is focused, scrolled to, its
  collapsed ancestors opened); `hidden` means a filter hides it — in
  practice the default "open only" filter and a complete linked task — so
  the widget's status line says "TKMA… is hidden by a filter (Tasks › Show
  All)" and, when `spec.reveal` is set, clears the status filter with
  `table.filter` and selects again; `missing` means the task is gone
  (deleted under a stale reference) and says so. Filters are never
  touched without that opt-in.
- **Remove**: the `linked-task` pane, its Tasks-menu entry, `state.linked_task`,
  the `linked` mode of the `task-refs` widget and its CSS, and the
  `task_get` and `task_refs_get` reqreps (nothing else calls them). The
  `task-refs` widget keeps the drop box, the preview, and Go to.
- **Keep**: the References pane, table linking, the Relations pane,
  `task_options` and `relation_options` (the dialogs use them).
- **Pin** mkui ≥ 0.2.23.
- **Tests.** 180 across phases 8 and 9. `test_ui_config.py`: no `linked-task` pane or state,
  every reqrep named by a dialog or `refs.js` and every such name a real
  reqrep (neither side goes stale), `refs.js` fires `table.select` on the
  `tasks` pane and never `pane.show` and handles all three outcomes,
  installed mkui ≥ 0.2.23. `test_server.py`: `_refs` now reads through the
  live `task_refs` query with the pane's own server-side filter rather
  than the deleted reqrep, which tests the path the app uses. Browser: Go
  to selects the linked task (Detail and References follow, and the
  mirror's Go to walks back), and on a complete linked task under "open
  only" it reveals and selects, saying "(showing all tasks)".

## Deferred (not in the skeleton)

- The prioritization model itself: weighted scoring, Eisenhower quadrants,
  aging/due-date decay, projects/tags, recurring tasks. The `score` derived
  column is the placeholder where this lands; it may move server-side into a
  `reqrep` or computed column later.
- Custom pane types (quadrant board, calendar). The task detail editor
  landed in 0.7.0: the Detail pane's toolbar edits the task and its
  references through the dialogs the other panes already declare.
- Authentication (mkui `auth` + mkio `_mkio_users`) if the app is ever
  multi-user; the per-owner layouts scaffold already supports it.
- CI workflow (mkui and mkfix have none; add GitHub Actions if wanted).

## Assumptions to confirm

- Python >= 3.11 is acceptable (forced by mkio).
- Single-user, no login, loopback host by default.
- `importance`/`urgency` 1..5 integers are a fine placeholder schema for the
  skeleton; the real model comes later.
