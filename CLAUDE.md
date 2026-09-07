# mktask

Work task prioritizer built on [mkio](https://github.com/markuskimius/mkio) (config-driven microservice backend: SQLite + WebSocket services) and [mkui](https://github.com/markuskimius/mkui) (config-driven Web Components workspace). Sibling of [mkfix](../mkfix), whose structure this repo copies.

## Quick start

```bash
pip install -e '.[test]'
mktask                    # http://127.0.0.1:8080/, mktask.db in cwd
mktask -d :memory: -p 9090 --user mark
mktask --files ~/refs     # uploaded reference files (default: <db>.files beside the database)
python -m pytest
```

## Project layout

```
mktask/
  __init__.py       __version__ (single source of truth) + lazy serve()
  __main__.py       CLI (argparse) → mkio create_app(); resolves "__mkui__" static route; --user;
                    --files dir + "/files" static route + POST /files upload handler
  services.py       TaskTransactions: Task ID assignment, complete/reopen/delete cascades, references
                    (add_ref/edit_ref/delete_ref, mirrored task links, relabel on edit, file cleanup)
  mktask.toml       tables, services, static routes (no version key — injected at load)
  static/
    index.html      imports /mkui/src/index.js and /static/refs.js, fetches app.json, sets mkio.url
    app.json        the whole UI: menubar, statusbar, panes, frames, dialogs, layouts
    refs.js         the `task-refs` widget: drop/paste/pick a file, selected-reference preview,
                    Linked Task viewer, "Go to" a linked task
    mktask.css      app overrides on top of mkui.css
tests/
  test_cli.py       --help / --version / bad config path (subprocess)
  test_config.py    _load_config, _find_config, main() parsing, serve() overrides, --user → prefix,
                    --files / files dir derivation, banner
  test_services.py  user_prefix, format_task_id, TASK_ID_PATTERN (pure)
  test_server.py    boots the real server on a free port (-d :memory: --user mark): HTTP routes, every
                    task op, Task ID sequence (rejection, no reuse, restart, past 8 digits, lost
                    counter), split + cascades, live delete announcements, references (kinds,
                    validation, edit rules, cascades, server-side filter), task links (mirror rows,
                    validation, unlink, relabel, delete either end, task_options), file upload /
                    dedupe / cleanup, saved layouts, CLI flags
  test_ui_config.py static integrity of app.json against mktask.toml, index.html, and refs.js
                    (every table pane's columns, dialogs, widgets, state, frames)
PLAN.md             the original skeleton plan; phases and deferred work
```

## Architecture

- `serve()` in `__main__.py` loads `mktask.toml` via `mkio.config.load_config`, injects `version = __version__`, resolves `"__mkui__"` to `mkui.static_dir` and relative static dirs against the TOML's directory, sets `services.tasks.prefix` from `user` (`--user`, default `getpass.getuser()`), resolves the files directory (`_files_dir`: `--files`, else `<db_path>.files`, else a temp dir for `:memory:`), creates it, adds it as the `/files` static route and as `services.tasks.files_dir`, probes the port (`_check_port`), then `create_app(cfg, routes=[("POST", "/files", …)]).run()`. A startup hook prints the banner. Neither the username nor the files dir is a top-level mkio config key (unknown keys warn); tests that call `serve()` chdir to a scratch dir because it creates the files directory.
- `POST /files` (`_upload_handler`): raw body, `Content-Type` → extension (`_EXTENSIONS` overrides, then `mimetypes`, else `.bin`), streamed under `MAX_UPLOAD` (20 MB) rather than aiohttp's 1 MB `client_max_size`, written as `<sha256>.<ext>` (temp + `os.replace`), returns `{href, mime, size}`. Content-addressed: repeats dedupe, no client name reaches the filesystem. The route never writes a row; the client follows with `add_ref`.
- `_check_port` runs before `create_app`: a bind failure inside `app.start()` happens after the startup hooks have opened the database, whose aiosqlite threads then keep the process alive after the traceback. The probe turns that hang into an exit-1 with a one-line error (`test_port_in_use_fails_cleanly`).
- `_find_config()` prefers `./mktask.toml` over the packaged one, so a user can copy the TOML out and customize it.
- The UI is JSON, not TOML: `index.html` fetches `/static/app.json` with `cache: "no-cache"` (mkio serves statics without Cache-Control) and calls `setConfig`. No `[config]` route in the TOML.
- `mkio.expect` in app.json pins `name` and `expr` only. Never pin `version` — every release would then show "Server mismatch" (`test_ui_config.py` guards this).

## Vocabulary

A task is **split** into **child** tasks (never "subtask"); a child can be split again, to any depth. A task is **open** or **complete** (never "done"). Its identifier is its **Task ID** (never "ID"). A task has **references** (never "attachment"): a **URL**, a **text** snippet, a **file**, or a **task link** with a **relation** (`blocks`, `blocked_by`, `relates`); the other end is the **linked task**. "Link" means only a task-to-task reference. `test_ui_config.py` fails on the word "done" anywhere in app.json or the TOML.

## Data model

`tasks`: `task_id` (TEXT PK), `parent_task_id` (`''` = top level, else the parent's Task ID), `title`, `notes`, `status` (`open` | `complete`), `importance` and `urgency` (1..5), `due` (ISO date or `''`), `created_at` / `updated_at` / `completed_at` (UTC `YYYY-MM-DD HH:MM:SS`, matching SQLite's `CURRENT_TIMESTAMP`). Timestamps on update come from the client: buttons and the Edit dialog send `${TIME(NOW(), '%Y-%m-%d %H:%M:%S')}` (mkio expression stdlib, UTC by default) because mkio transaction `defaults` are static values.

`task_refs`: `ref_id` (`INTEGER PRIMARY KEY AUTOINCREMENT`; nobody refers to a reference by name), `task_id` (the owner), `kind` (`url` | `text` | `file` | `task`), `relation` (task links only: `blocks` | `blocked_by` | `relates`, else `''`), `label` (what the UI shows), `href` (`url`: the URL; `file`: `/files/<sha256>.<ext>`; `task`: the linked Task ID; `text`: `''`), `body` (`text` only), `mime` (`file` only), `created_at` / `updated_at`. One-to-many: a reference belongs to one task; the same URL or file under two tasks is two rows, and for files that is what keeps a shared file alive (`_unlink_orphans` counts rows by `href`). **A task link is two mirrored rows**, one per side with inverse relations, so a pane filtered on `task_id` shows every link of a task from its own side with no client logic — mkio's live updates cannot carry joined columns, which is why links are not a separate table. `href` means three things by kind; `display.label` in app.json branches on `kind` because a Task ID is not a hyperlink. Files live on disk, never in SQLite (query subscribers receive whole rows).

`counters`: one row `name = "task"`, `last` = the last Task ID number issued.

**Task IDs** are `TK` + two prefix letters + a decimal sequence padded to at least 8 digits (`TKMA00000001`; pattern `^TK[A-Z0-9]{2}\d{8,}$`). The prefix is `user_prefix(username)`: alphanumerics, uppercased, first two, `X`-padded. The sequence is global (one counter regardless of prefix), never reused after a delete, and never wraps: `TKMA99999999` → `TKMA100000000`. Nothing may depend on id order (text order breaks at the rollover); chronology is `created_at`.

The `score` column is virtual: `values.score = "importance * urgency"` on the `tasks` pane. It is the placeholder for the real prioritization model (see PLAN.md, Deferred).

**No migrations before 1.0.** A schema change means "delete mktask.db"; the README says so. `auto_migrate = true` only covers additive changes.

## Services (`mktask.toml`)

- `tasks` — `protocol = "mktask.services.TaskTransactions"`, a `TransactionService` subclass; the ops stay in the TOML. `add` and `split` are two steps (upsert `counters.last`, insert the task): the class takes the next number from an in-memory counter (seeded in `start()` from `counters`, else from the largest existing Task ID), injects `task_id` and `last` into the data, rejects a request that carries its own `task_id`, and rejects a `split` whose `parent_task_id` is not an existing task (no orphans; a rejected request consumes no number). The `task_id = ""` / `last = 0` defaults exist only so mkio treats them as optional for the client. Every other op goes through `_guarded` → `_op_<name>`, which replies mkio-style (`KeyError` → "Missing required field", anything else → its message). `complete` runs the single-row op over the subtree (recursive CTE, parents first), `reopen` over the task and its ancestors, in one `writer.submit` (`_submit`) so the change bus announces every RETURNING row. **Deletes go one request per row** (`_submit_each`, gathered so they land in one batch): mkio announces a delete with the request's `data`, so one request for a subtree announced the parent's Task ID for every row and a live table kept the children (`test_subtree_delete_announces_every_row`). `delete` also removes every reference the subtree owns or is linked to (`task_id IN subtree OR (kind = 'task' AND href IN subtree)`), then unlinks orphaned files. `edit` is the plain update plus a `relabel_ref` per link that points at the task (same submit). `add_ref` requires the owning task, fills a missing label (`default_label`), forces `href`/`body` by kind, and for kind `task` checks relation, self, target, and duplicate, then inserts the row and its mirror; `edit_ref` refuses a task link, keeps a file's `href`, blanks a snippet's; `delete_ref` deletes the mirror too, then the file if nothing names it. `relabel_ref` exists for the service, not the UI. mkio skips op validation for dotted protocols; `test_ui_config.py` and `test_server.py` are what catch a bad field name.
- `all_tasks` — query on `tasks`, `filterable = ["status", "parent_task_id"]`. The blotter subscribes to this.
- `task_refs` — query on `task_refs`, `filterable = ["task_id", "kind", "relation"]`; the References pane subscribes. `task_get` (one task), `task_refs_get` (a task's references, newest first) and `task_options` (open tasks other than `:task_id`, as `value`/`label`) are reqreps for `refs.js` and the Link dialog's `optionsFrom`. A query with a JOIN would not update live (mkio publishes the bare primary-table row and drops secondary-table changes), so nothing joins.
- `mkui_layouts`, `mkui_layouts_list`, `mkui_layouts_get` — verbatim from mkui's scaffold; mktask has no login so every save lands under owner `''`.

## UI (`app.json`)

- `tasks` pane: `mkio-table` with `tree = { child = "parent_task_id", parent = "task_id", column = "task_id", expand = "all", filterScope = "all" }` (mkui ≥ 0.2.12; the package pins ≥ 0.2.16 for button `style`): children nest under their parent, each level in the pane's sort order (`["-score", "created_at"]`), opened at load (expansion is not saved in layouts), caret and indent on the Task ID column. `filterScope = "all"` (Branch) makes the default filter `status = ["open"]` test every row while keeping the way to a match, so a complete child under an open parent is hidden and an open child keeps its complete parent visible. `select.state = "selected_task"`. Buttons: `New` (dialog → `tasks.add`), `Split` (`unit = "row"`, dialog with hidden `parent_task_id = ${row.task_id}`, levels prefilled from the parent → `tasks.split`), `Edit` (`unit = "row"`, hidden `task_id` and `updated_at`), `Complete` / `Reopen` (transaction buttons gated by `enable.when` on `status`), `Delete` (a dialog with a readonly title line acts as the confirmation — mkui has no confirm on transaction buttons; `style` rules (mkui ≥ 0.2.16) turn it red only `when = "enabled"`, with no bold/caps so its width never shifts). No dialog ever sends `task_id` or `last` on add/split.
- `tasks` pane also has `Reference` (`unit = "row"`, dialog: hidden `task_id`, `kind` select `url`/`text`, `href` shown `when kind == 'url'`, `body` textarea when `text`, optional `label` → `tasks.add_ref`) and `Link` (`unit = "row"`, dialog: hidden `task_id` and `kind = "task"`, `relation` select, `href` select via `optionsFrom` `task_options` with `params.task_id = ${row.task_id}` so the task itself is excluded → `tasks.add_ref`). Fields hidden by `showWhen` are not sent, which is why `href`/`body` carry `defaults` on `add_ref`/`edit_ref`; two fields may not share a name in one dialog, which is why Reference and Link are two buttons.
- `references` pane: `mkio-table` on `task_refs`, columns task_id/kind/relation/label/created_at, `display.label` = `LINK(label, href)` when there is an href and not a task link (a Task ID is not a URL), `display.relation` words the relation, `select.state = "selected_ref"`, `Edit` (`enable.when "row != NULL && row.kind != 'task'"`; `href`/`body` `showWhen` on `row.kind`) and red `Delete` (readonly-confirmation dialog). Sorted `-created_at`. The main frame is a vertical `split` of `tasks` over `references`.
- `task-detail` pane: three `text` widgets over `state.selected_task` (title line, "Split from Task ID …" when `parent_task_id` is set, notes) and a `task-refs` widget (`refs.js`): the drop box (drop / paste / click-to-pick a file → `POST /files` → `add_ref` kind `file`; pasted URL → `url`; pasted text → `text`; paste is a `window` listener active only while a task is selected and no text field has focus), the preview of `state.selected_ref` (image inline, snippet in full, "Go to" on a task link), and the **selection follow**: on every `selected_task` change it fires `table.filter` on the `references` pane with `task_id = [id]` (`merge`), or `task_id = null` (show all) when nothing is selected — mkui has no state-bound filter, so the widget does what the menu's filter entries do. Text widget templates see `state.<path>` (not bare names) and the expression language has no ternary — use `IF(...)`. Text widgets render `textContent` only, so links live in tables and in `refs.js`, never in a text widget.
- `linked-task` pane: not in the default frames; opened by "Go to" (`refs.js`: `task_get` → `state.linked_task` → `pane.show`) or the Tasks menu. Text widgets over `state.linked_task` plus `task-refs` in `mode = "linked"`, which lists that task's references from `task_refs_get` (a viewer, refetched per task, not live) with their own "Go to". Panes are config singletons: one such window, replaced on each Go To. mkui has no select-a-row action, so "Go to" never changes the Tasks selection.
- Menubar `Tasks`: `pane.show` for every pane (the test insists), `table.filter` entries (`{"status": ["open"]}` with `merge` shows open only; `{"status": null}` with `merge` clears that column's filter) and `table.expand` entries (`depth = "all"` expands everything; no `depth` collapses everything — there is no separate collapse action).
- `test_ui_config.py` checks every dialog/button field against the op's `fields` + `key` (and that every field or key without a `defaults` entry is sent); it also checks, per table pane (columns resolved through the pane's service → `primary_table`), that `${row.x}`, `row.x`, `values`, `styles`, `display`, `sort`, `filters`, `tree`, and text-widget `state.x` references name real columns and declared state, that every non-text widget type is registered by a module index.html imports, that `refs.js` only calls declared reqreps/ops/state, and that every pane is either in the default frames or reachable from the Tasks menu. Add the column to `mktask.toml` before using it in the UI.

## Versioning and release

`mktask/__init__.py` `__version__` is the single source of truth (hatch dynamic version). Release (`/cut`): bump it, update README/CLAUDE/tests, commit as `Release vX.Y.Z: summary`, push, `rm -f dist/*; python -m build`, `twine upload dist/*`, then `pip install --upgrade mktask` so the user's venv runs the published wheel (siblings mkfix/mkui are installed non-editable there; mkio is editable). No git tags, matching mkfix.
