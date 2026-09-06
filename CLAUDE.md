# mktask

Work task prioritizer built on [mkio](https://github.com/markuskimius/mkio) (config-driven microservice backend: SQLite + WebSocket services) and [mkui](https://github.com/markuskimius/mkui) (config-driven Web Components workspace). Sibling of [mkfix](../mkfix), whose structure this repo copies.

## Quick start

```bash
pip install -e '.[test]'
mktask                    # http://127.0.0.1:8080/, mktask.db in cwd
mktask -d :memory: -p 9090 --user mark
python -m pytest
```

## Project layout

```
mktask/
  __init__.py       __version__ (single source of truth) + lazy serve()
  __main__.py       CLI (argparse) → mkio create_app(); resolves "__mkui__" static route; --user
  services.py       TaskTransactions: Task ID assignment + complete/reopen/delete cascades
  mktask.toml       tables, services, static routes (no version key — injected at load)
  static/
    index.html      imports /mkui/src/index.js, fetches app.json, sets mkio.url from location
    app.json        the whole UI: menubar, statusbar, panes, frames, dialogs, layouts
    mktask.css      app overrides on top of mkui.css
tests/
  test_cli.py       --help / --version / bad config path (subprocess)
  test_config.py    _load_config, _find_config, main() parsing, serve() overrides, --user → prefix, banner
  test_services.py  user_prefix, format_task_id, TASK_ID_PATTERN (pure)
  test_server.py    boots the real server on a free port (-d :memory: --user mark): HTTP routes, every
                    task op, Task ID sequence (rejection, no reuse, restart, past 8 digits, lost
                    counter), split + cascades, server-side query filter, saved layouts, CLI flags
  test_ui_config.py static integrity of app.json against mktask.toml and index.html
PLAN.md             the original skeleton plan; phases and deferred work
```

## Architecture

- `serve()` in `__main__.py` loads `mktask.toml` via `mkio.config.load_config`, injects `version = __version__`, resolves `"__mkui__"` to `mkui.static_dir` and relative static dirs against the TOML's directory, sets `services.tasks.prefix` from `user` (`--user`, default `getpass.getuser()`), probes the port (`_check_port`), then `create_app(cfg).run()`. A startup hook prints the banner. The username is never stored in the mkio config (unknown top-level keys warn).
- `_check_port` runs before `create_app`: a bind failure inside `app.start()` happens after the startup hooks have opened the database, whose aiosqlite threads then keep the process alive after the traceback. The probe turns that hang into an exit-1 with a one-line error (`test_port_in_use_fails_cleanly`).
- `_find_config()` prefers `./mktask.toml` over the packaged one, so a user can copy the TOML out and customize it.
- The UI is JSON, not TOML: `index.html` fetches `/static/app.json` with `cache: "no-cache"` (mkio serves statics without Cache-Control) and calls `setConfig`. No `[config]` route in the TOML.
- `mkio.expect` in app.json pins `name` and `expr` only. Never pin `version` — every release would then show "Server mismatch" (`test_ui_config.py` guards this).

## Vocabulary

A task is **split** into **child** tasks (never "subtask"); a child can be split again, to any depth. A task is **open** or **complete** (never "done"). Its identifier is its **Task ID** (never "ID"). `test_ui_config.py` fails on the word "done" anywhere in app.json or the TOML.

## Data model

`tasks`: `task_id` (TEXT PK), `parent_task_id` (`''` = top level, else the parent's Task ID), `title`, `notes`, `status` (`open` | `complete`), `importance` and `urgency` (1..5), `due` (ISO date or `''`), `created_at` / `updated_at` / `completed_at` (UTC `YYYY-MM-DD HH:MM:SS`, matching SQLite's `CURRENT_TIMESTAMP`). Timestamps on update come from the client: buttons and the Edit dialog send `${TIME(NOW(), '%Y-%m-%d %H:%M:%S')}` (mkio expression stdlib, UTC by default) because mkio transaction `defaults` are static values.

`counters`: one row `name = "task"`, `last` = the last Task ID number issued.

**Task IDs** are `TK` + two prefix letters + a decimal sequence padded to at least 8 digits (`TKMA00000001`; pattern `^TK[A-Z0-9]{2}\d{8,}$`). The prefix is `user_prefix(username)`: alphanumerics, uppercased, first two, `X`-padded. The sequence is global (one counter regardless of prefix), never reused after a delete, and never wraps: `TKMA99999999` → `TKMA100000000`. Nothing may depend on id order (text order breaks at the rollover); chronology is `created_at`.

The `score` column is virtual: `values.score = "importance * urgency"` on the `tasks` pane. It is the placeholder for the real prioritization model (see PLAN.md, Deferred).

**No migrations before 1.0.** A schema change means "delete mktask.db"; the README says so. `auto_migrate = true` only covers additive changes.

## Services (`mktask.toml`)

- `tasks` — `protocol = "mktask.services.TaskTransactions"`, a `TransactionService` subclass; the ops stay in the TOML. `add` and `split` are two steps (upsert `counters.last`, insert the task): the class takes the next number from an in-memory counter (seeded in `start()` from `counters`, else from the largest existing Task ID), injects `task_id` and `last` into the data, rejects a request that carries its own `task_id`, and rejects a `split` whose `parent_task_id` is not an existing task (no orphans; a rejected request consumes no number). The `task_id = ""` / `last = 0` defaults exist only so mkio treats them as optional for the client. `complete` and `delete` run the single-row op over the subtree (recursive CTE, parents first), `reopen` over the task and its ancestors, all in one `writer.submit` so the change bus announces every row. `edit` is a plain update. mkio skips op validation for dotted protocols; `test_ui_config.py` and `test_server.py` are what catch a bad field name.
- `all_tasks` — query on `tasks`, `filterable = ["status", "parent_task_id"]`. The blotter subscribes to this.
- `mkui_layouts`, `mkui_layouts_list`, `mkui_layouts_get` — verbatim from mkui's scaffold; mktask has no login so every save lands under owner `''`.

## UI (`app.json`)

- `tasks` pane: `mkio-table` with `tree = { child = "parent_task_id", parent = "task_id", column = "task_id", expand = "all", filterScope = "all" }` (mkui ≥ 0.2.12; the package pins ≥ 0.2.16 for button `style`): children nest under their parent, each level in the pane's sort order (`["-score", "created_at"]`), opened at load (expansion is not saved in layouts), caret and indent on the Task ID column. `filterScope = "all"` (Branch) makes the default filter `status = ["open"]` test every row while keeping the way to a match, so a complete child under an open parent is hidden and an open child keeps its complete parent visible. `select.state = "selected_task"`. Buttons: `New` (dialog → `tasks.add`), `Split` (`unit = "row"`, dialog with hidden `parent_task_id = ${row.task_id}`, levels prefilled from the parent → `tasks.split`), `Edit` (`unit = "row"`, hidden `task_id` and `updated_at`), `Complete` / `Reopen` (transaction buttons gated by `enable.when` on `status`), `Delete` (a dialog with a readonly title line acts as the confirmation — mkui has no confirm on transaction buttons; `style` rules (mkui ≥ 0.2.16) turn it red only `when = "enabled"`, with no bold/caps so its width never shifts). No dialog ever sends `task_id` or `last` on add/split.
- `task-detail` pane: three `text` widgets over `state.selected_task` (title line, "Split from Task ID …" when `parent_task_id` is set, notes). Text widget templates see `state.<path>` (not bare names) and the expression language has no ternary — use `IF(...)`.
- Menubar `Tasks`: `table.filter` entries (`{"status": ["open"]}` with `merge` shows open only; `{"status": null}` with `merge` clears that column's filter) and `table.expand` entries (`depth = "all"` expands everything; no `depth` collapses everything — there is no separate collapse action).
- `test_ui_config.py` checks every dialog/button field against the op's `fields` + `key` (and that every field or key without a `defaults` entry is sent); it also checks that `${row.x}`, `values`, `styles`, `sort`, `filters`, `tree`, and text-widget `state.x` references name real columns and declared state. Add the column to `mktask.toml` before using it in the UI.

## Versioning and release

`mktask/__init__.py` `__version__` is the single source of truth (hatch dynamic version). Release (`/cut`): bump it, update README/CLAUDE/tests, commit as `Release vX.Y.Z: summary`, push, `rm -f dist/*; python -m build`, `twine upload dist/*`, then `pip install --upgrade mktask` so the user's venv runs the published wheel (siblings mkfix/mkui are installed non-editable there; mkio is editable). No git tags, matching mkfix.
