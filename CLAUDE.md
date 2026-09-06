# mktask

Work task prioritizer built on [mkio](https://github.com/markuskimius/mkio) (config-driven microservice backend: SQLite + WebSocket services) and [mkui](https://github.com/markuskimius/mkui) (config-driven Web Components workspace). Sibling of [mkfix](../mkfix), whose structure this repo copies.

## Quick start

```bash
pip install -e '.[test]'
mktask                    # http://127.0.0.1:8080/, mktask.db in cwd
mktask -d :memory: -p 9090
python -m pytest
```

## Project layout

```
mktask/
  __init__.py       __version__ (single source of truth) + lazy serve()
  __main__.py       CLI (argparse) → mkio create_app(); resolves "__mkui__" static route
  mktask.toml       tables, services, static routes (no version key — injected at load)
  static/
    index.html      imports /mkui/src/index.js, fetches app.json, sets mkio.url from location
    app.json        the whole UI: menubar, statusbar, panes, frames, dialogs, layouts
    mktask.css      app overrides on top of mkui.css
tests/
  test_cli.py       --help / --version / bad config path (subprocess)
  test_config.py    _load_config, _find_config, main() parsing, serve() overrides, _check_port, banner
  test_server.py    boots the real server on a free port (-d :memory:): HTTP routes, every task op,
                    server-side query filter, saved-layouts round trip, --host, busy-port exit
  test_ui_config.py static integrity of app.json against mktask.toml and index.html
PLAN.md             the original skeleton plan; phases and deferred work
```

## Architecture

- `serve()` in `__main__.py` loads `mktask.toml` via `mkio.config.load_config`, injects `version = __version__`, resolves `"__mkui__"` to `mkui.static_dir` and relative static dirs against the TOML's directory, probes the port (`_check_port`), then `create_app(cfg).run()`. A startup hook prints the banner.
- `_check_port` runs before `create_app`: a bind failure inside `app.start()` happens after the startup hooks have opened the database, whose aiosqlite threads then keep the process alive after the traceback. The probe turns that hang into an exit-1 with a one-line error (`test_port_in_use_fails_cleanly`).
- `_find_config()` prefers `./mktask.toml` over the packaged one, so a user can copy the TOML out and customize it.
- The UI is JSON, not TOML: `index.html` fetches `/static/app.json` with `cache: "no-cache"` (mkio serves statics without Cache-Control) and calls `setConfig`. No `[config]` route in the TOML.
- `mkio.expect` in app.json pins `name` and `expr` only. Never pin `version` — every release would then show "Server mismatch" (`test_ui_config.py` guards this).

## Data model

One table, `tasks`: `title`, `notes`, `status` (`open` | `done`), `importance` and `urgency` (1..5), `due` (ISO date or `''`), `created_at` / `updated_at` / `done_at` (UTC `YYYY-MM-DD HH:MM:SS`, matching SQLite's `CURRENT_TIMESTAMP`). Timestamps on update come from the client: buttons and the Edit dialog send `${TIME(NOW(), '%Y-%m-%d %H:%M:%S')}` (mkio expression stdlib, UTC by default) because mkio transaction `defaults` are static values.

The `score` column is virtual: `values.score = "importance * urgency"` on the `tasks` pane. It is the placeholder for the real prioritization model (see PLAN.md, Deferred).

## Services (`mktask.toml`)

- `tasks` — transaction; ops `add` (defaults for notes/importance/urgency/due), `edit`, `done` (defaults `status = "done"`), `reopen` (defaults `status = "open"`, `done_at = ""`), `delete`. Fields listed without a `defaults` entry are required by mkio.
- `all_tasks` — query on `tasks`, `filterable = ["status"]`. The blotter subscribes to this.
- `mkui_layouts`, `mkui_layouts_list`, `mkui_layouts_get` — verbatim from mkui's scaffold; mktask has no login so every save lands under owner `''`.

## UI (`app.json`)

- `tasks` pane: `mkio-table` with default filter `status = ["open"]`, `sort = "-score"`, `select.state = "selected_task"`. Buttons: `+ Add` (dialog → `tasks.add`), `Edit` (`unit = "row"`, dialog prefilled from `${row.*}` with hidden `id` and `updated_at`), `Done` / `Reopen` (transaction buttons gated by `enable.when` on `status`), `Delete` (a dialog with a readonly title line acts as the confirmation — mkui has no confirm on transaction buttons).
- `task-detail` pane: two `text` widgets over `state.selected_task`. Text widget templates see `state.<path>` (not bare names) and the expression language has no ternary — use `IF(...)`.
- Menubar `Tasks` has `table.filter` entries: `{"status": ["open"]}` with `merge` shows open only; `{"status": null}` with `merge` clears that column's filter.
- `test_ui_config.py` checks every dialog/button field against the op's `fields` + `key` (and that every field without a `defaults` entry is sent); it also checks that `${row.x}`, `values`, `styles`, `sort`, `filters`, and text-widget `state.x` references name real columns and declared state. Add the column to `mktask.toml` before using it in the UI.

## Versioning and release

`mktask/__init__.py` `__version__` is the single source of truth (hatch dynamic version). Release (`/cut`): bump it, update README/CLAUDE/tests, commit as `Release vX.Y.Z: summary`, push, `rm -f dist/*; python -m build`, `twine upload dist/*`, then `pip install --upgrade mktask` so the user's venv runs the published wheel (siblings mkfix/mkui are installed non-editable there; mkio is editable). No git tags, matching mkfix.
