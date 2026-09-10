"""Static integrity checks on the UI config (app.json) and its assets.

app.json drives the whole UI declaratively, so a dangling pane reference, a
misspelled service, or a JS module that no longer exists fails silently in
the browser rather than at import time. These tests fail the build instead.
"""

import json
import re
import tomllib
from pathlib import Path

import pytest
from mkio.history import (
    HISTORY_META_COLUMNS,
    VERSION_COLUMN,
    base_table_name,
    history_table_name,
    is_history_table,
    primary_key_columns,
    source_columns,
)

PKG = Path(__file__).resolve().parent.parent / "mktask"
STATIC = PKG / "static"

#: Ops TaskTransactions implements itself, with no steps in the TOML: each
#: expands to mkio's per-row cursor moves over every row one action wrote.
SERVICE_OPS = {"undo_action", "redo_action"}

#: The keys mkui's parseHistorySpec understands in a pane's `history` block.
#: It drops an unknown one with a console warning rather than failing, so a
#: typo would only ever show up here.
HISTORY_KEYS = {
    "table", "key", "versions", "state", "feed", "asOf", "undo", "redo",
    "columns", "fields", "confirm",
}

#: Actions mkui registers that a button or menu item may fire.
MKUI_ACTIONS = {
    "pane.show", "layout.save", "layout.restore", "layout.reset", "layout.refresh",
    "window.tileH", "window.tileV", "window.grid", "window.cascade",
    "edit.copy", "edit.selectAll", "edit.find", "edit.undo", "edit.redo",
    "table.filter", "table.sort", "table.columns", "table.link", "table.expand",
    "table.select", "table.history", "auth.logout",
}


@pytest.fixture(scope="module")
def app_config() -> dict:
    return json.loads((STATIC / "app.json").read_text())


@pytest.fixture(scope="module")
def server_config() -> dict:
    return tomllib.loads((PKG / "mktask.toml").read_text())


def _walk(node):
    """Yield every dict in a nested JSON structure."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _layout_panes(layout):
    for child in layout.get("children", []):
        if isinstance(child, str):
            yield child
        else:
            yield from _layout_panes(child)


def test_frames_reference_known_panes(app_config):
    panes = set(app_config["panes"])
    for frame in app_config["frames"]:
        for pane in _layout_panes(frame["layout"]):
            assert pane in panes, f"frame {frame['id']} references unknown pane {pane}"


def test_menu_actions_reference_known_panes(app_config):
    panes = set(app_config["panes"])
    for item in _walk(app_config["menubar"]):
        if item.get("action") == "pane.show":
            assert item["args"] in panes, f"menu item {item['label']} shows unknown pane"
        if item.get("action") in ("table.filter", "table.sort", "table.columns", "table.expand"):
            assert item["args"]["pane"] in panes, f"menu item {item['label']} targets unknown pane"


def test_services_exist(app_config, server_config):
    services = server_config["services"]
    for pane_id, pane in app_config["panes"].items():
        if pane.get("type") == "mkio-table":
            assert pane["service"] in services, f"pane {pane_id} subscribes to unknown service"
    for node in _walk(app_config["panes"]):
        if "service" in node and "op" in node:
            svc = services.get(node["service"])
            assert svc is not None, f"unknown service {node['service']}"
            if node["op"] in SERVICE_OPS:
                continue
            assert node["op"] in svc["ops"], f"service {node['service']} has no op {node['op']}"


def test_transaction_fields_are_declared(app_config, server_config):
    """Every field a dialog or button sends must be one the op accepts."""
    services = server_config["services"]
    for node in _walk(app_config["panes"]):
        if not ("service" in node and "op" in node) or node["op"] in SERVICE_OPS:
            continue
        ops = services[node["service"]]["ops"][node["op"]]
        allowed = set()
        for op in ops:
            allowed.update(op.get("fields", []))
            allowed.update(op.get("key", []))
        sent = set(node.get("data", {}))
        dialog = next((d for d in _walk(app_config["panes"])
                       if d.get("submit") is node), None)
        if dialog is not None:
            for field in _walk(dialog["fields"]):
                if "name" in field and not field["name"].startswith("_") and field.get("type") != "readonly":
                    sent.add(field["name"])  # `_x` is scratch, readonly is display: neither is sent
        assert sent <= allowed, f"{node['service']}.{node['op']} sends undeclared {sent - allowed}"


def test_index_imports_resolve():
    html = (STATIC / "index.html").read_text()
    for path in re.findall(r'(?:import|href=|src=)\s*"(/[^"]+)"', html):
        if path.startswith("/mkui/"):
            import mkui
            assert (Path(mkui.static_dir) / path[len("/mkui/"):]).is_file(), path
        elif path.startswith("/static/"):
            assert (STATIC / path[len("/static/"):]).is_file(), path


def test_no_pinned_server_version(app_config):
    """A pinned version turns every release into a 'server mismatch' banner."""
    assert "version" not in app_config["mkio"].get("expect", {})


def test_no_version_in_server_toml(server_config):
    assert "version" not in server_config


# ─── Column and state references ───────────────────────────────────

@pytest.fixture(scope="module")
def task_columns(server_config) -> set:
    return set(server_config["tables"]["tasks"]["columns"])


@pytest.fixture(scope="module")
def tasks_pane(app_config) -> dict:
    return app_config["panes"]["tasks"]


def _table_panes(app_config):
    return [(pid, p) for pid, p in app_config["panes"].items() if p.get("type") == "mkio-table"]


def _table_columns(table, server_config) -> set:
    """Every column a table returns, mkio's own included.

    A `versioned = true` table carries `_mkio_version` (the cursor into its
    recorded versions) beside the `_mkio_ref` every table gets. A history
    table is not in the config at all — mkio derives it — so its columns are
    the metadata it prepends plus the base table's own.
    """
    if is_history_table(table):
        base = server_config["tables"][base_table_name(table)]
        return set(HISTORY_META_COLUMNS) | set(source_columns(base)) | {"_mkio_ref"}
    spec = server_config["tables"][table]
    known = set(spec["columns"]) | {"_mkio_ref"}
    return known | {VERSION_COLUMN} if spec.get("versioned") else known


def _columns_of(pane, server_config) -> set:
    """The columns a pane's query returns: its service's primary table."""
    return _table_columns(server_config["services"][pane["service"]]["primary_table"], server_config)


def _pane_columns(pane, table_columns):
    return table_columns | set(pane.get("values", {}))


def test_table_columns_exist(app_config, server_config):
    for pane_id, pane in _table_panes(app_config):
        known = _pane_columns(pane, _columns_of(pane, server_config))
        for key in ("columns", "visible"):
            for col in pane.get(key, []):
                assert col in known, f"{pane_id}.{key} names unknown column {col}"
        for key in ("labels", "styles", "types", "filters", "display"):
            for col in pane.get(key, {}):
                assert col in known, f"{pane_id}.{key} names unknown column {col}"


def test_sort_names_known_columns(app_config, server_config):
    for pane_id, pane in _table_panes(app_config):
        known = _pane_columns(pane, _columns_of(pane, server_config))
        sort = pane.get("sort", [])
        for spec in [sort] if isinstance(sort, str) else sort:
            col = spec["col"] if isinstance(spec, dict) else spec.lstrip("-")
            assert col in known, f"{pane_id} sorts on unknown column {col}"


def test_row_references_name_real_columns(app_config, server_config):
    """`${row.x}` in a pane's buttons and dialogs reads a column its query returns."""
    seen = 0
    for pane_id, pane in _table_panes(app_config):
        text = json.dumps(pane.get("buttons", []))
        refs = set(re.findall(r"\$\{row\.([a-z_]+)", text)) | set(re.findall(r"\br(?:ow)?\.([a-z_]+)", text))
        seen += len(refs)
        known = _columns_of(pane, server_config)
        assert refs <= known, f"{pane_id} reads unknown row columns {refs - known}"
    assert seen, "expected at least one row reference"


def test_display_templates_read_real_columns(app_config, server_config):
    for pane_id, pane in _table_panes(app_config):
        known = _pane_columns(pane, _columns_of(pane, server_config)) | {"value", "row", "col", "state"}
        for col, template in pane.get("display", {}).items():
            body = re.sub(r"'[^']*'", "", template)
            names = {n for n in re.findall(r"\b([a-z_]+)\b", body)}
            assert names <= known, f"{pane_id}.display.{col} reads unknown {names - known}"


def test_derived_values_read_real_columns(app_config, server_config):
    for pane_id, pane in _table_panes(app_config):
        known = _columns_of(pane, server_config)
        for col, expr in pane.get("values", {}).items():
            names = set(re.findall(r"\b([a-z_]+)\b", expr))
            assert names <= known, f"{pane_id}.values.{col} reads unknown {names - known}"


def test_style_rules_read_known_names(app_config, server_config):
    for pane_id, pane in _table_panes(app_config):
        known = _pane_columns(pane, _columns_of(pane, server_config)) | {"value", "row", "col", "state"}
        rules = list(pane.get("rowStyle", []))
        for col_rules in pane.get("styles", {}).values():
            rules.extend(col_rules)
        for rule in rules:
            when = re.sub(r"'[^']*'", "", rule.get("when", ""))
            names = {n for n in re.findall(r"\b([a-z_]+)\b", when)}
            assert names <= known, f"{pane_id} style rule {when!r} reads unknown {names - known}"


def test_selection_state_declared(app_config):
    published = set()
    for pane_id, pane in _table_panes(app_config):
        if "select" in pane:
            path = pane["select"]["state"]
            assert path.split(".")[0] in app_config["state"], f"{pane_id} publishes undeclared state"
            published.add(path)
    assert {"selected_task", "selected_ref"} <= published


def _task_fields() -> list:
    """The fields refs.js shows in the Detail pane's task block."""
    js = (STATIC / "refs.js").read_text()
    block = re.search(r"const TASK_FIELDS = \[(.*?)\];", js, re.S)
    assert block, "refs.js declares the task block's fields"
    return re.findall(r'\["([a-z_]+)", "([^"]+)"\]', block.group(1))


def test_detail_pane_reads_real_columns(app_config, task_columns, tasks_pane):
    """The task block mirrors a row of `tasks`, so every field it shows is a
    real column — or the blotter's own derived value, and no other."""
    names = [name for name, _ in _task_fields()]
    assert names, "the detail pane shows the selected task's fields"
    derived = set(tasks_pane.get("values", {}))
    unknown = set(names) - task_columns - derived
    assert not unknown, f"the detail pane shows unknown {unknown}"
    assert "title" not in names, "the title is the block's heading, not a field"


def test_detail_pane_edits_what_the_blotter_edits(app_config, server_config):
    """Everything the task block shows that the `edit` op does not accept is
    read-only there: it is changed by another button (status by Complete /
    Reopen, the parent by Move) or by nobody (created_at)."""
    editable = {f for step in server_config["services"]["tasks"]["ops"]["edit"]
                for f in step["fields"]}
    shown = {name for name, _ in _task_fields()}
    assert {"notes", "due", "importance", "urgency"} <= shown & editable
    assert not (shown & {"task_id", "last"}), "the block is not a form"


def test_the_dialog_module_refs_js_imports_exists():
    """refs.js opens a dialog with mkui's own `openDialog`, which mkui does not
    re-export from index.js — mkio-table reaches it by the same deep import.
    A path this test does not see move is one the browser fails on silently."""
    import mkui

    js = (STATIC / "refs.js").read_text()
    path = re.search(r'import\("(/mkui/src/[^"]+)"\)', js)
    assert path, "refs.js imports the dialog module"
    module = Path(mkui.static_dir) / path.group(1).removeprefix("/mkui/")
    assert module.is_file(), f"mkui has no {path.group(1)}"
    assert "export function openDialog(" in module.read_text()


def test_detail_toolbar_borrows_the_pane_dialogs(app_config):
    """The Detail pane's Edit / Delete open the dialogs the Tasks and
    References panes already declare — borrowed by name, never copied, so the
    two places a task or a reference is edited cannot drift apart."""
    widget = next(w for w in app_config["panes"]["task-detail"]["widgets"]
                  if w["type"] == "task-refs")
    dialogs = widget["dialogs"]
    assert set(dialogs) == {"editTask", "editRef", "deleteRef"}
    expected = {"editTask": "edit", "editRef": "edit_ref", "deleteRef": "delete_ref"}
    for name, src in dialogs.items():
        pane = app_config["panes"][src["pane"]]
        button = next((b for b in pane["buttons"] if b["label"] == src["button"]), None)
        assert button, f"{name}: no {src['button']} button on the {src['pane']} pane"
        assert button["action"]["type"] == "dialog", f"{name} is not a dialog button"
        assert button["action"]["dialog"]["submit"]["op"] == expected[name]
    js = (STATIC / "refs.js").read_text()
    for name in dialogs:
        assert f'"{name}"' in js, f"refs.js never opens {name}"


def test_text_widgets_read_declared_state(app_config):
    roots = set(app_config["state"])
    for pane_id, pane in app_config["panes"].items():
        for w in pane.get("widgets", []):
            if w.get("type") != "text":
                continue
            for root in re.findall(r"state\.([a-z_]+)", w.get("text", "")):
                assert root in roots, f"pane {pane_id} reads undeclared state.{root}"
            if "bind" in w:
                assert w["bind"].split(".")[0] in roots
    for w in app_config["statusbar"]["left"] + app_config["statusbar"]["right"]:
        if "bind" in w:
            assert w["bind"].split(".")[0] in roots
    for path in app_config["statusbar"].get("bindStyle", {}).values():
        assert path.split(".")[0] in roots


def test_custom_widgets_are_registered(app_config):
    """Every non-text widget type is registered by a module index.html imports."""
    html = (STATIC / "index.html").read_text()
    registered = set()
    for path in re.findall(r'import\s*"/static/([^"]+)"', html):
        registered |= set(re.findall(r'registerWidget\("([a-z-]+)"', (STATIC / path).read_text()))
    for pane_id, pane in app_config["panes"].items():
        for w in pane.get("widgets", []):
            if w["type"] != "text":
                assert w["type"] in registered, f"pane {pane_id} uses unregistered widget {w['type']}"


def test_task_refs_widget_state_and_services(app_config, server_config):
    """refs.js reads the two published selections, calls only declared ops,
    and opens a linked task by selecting it in the Tasks pane."""
    js = (STATIC / "refs.js").read_text()
    for root in ("selected_task", "selected_ref"):
        assert root in app_config["state"], root
        assert f'"{root}"' in js, f"refs.js does not read state.{root}"
    for op in re.findall(r'op: "([a-z_]+)"', js):
        assert op in server_config["services"]["tasks"]["ops"], op
    assert "table.filter" not in js.split("reveal")[0], \
        "the References pane follows the Tasks selection through mkui's table linking"
    assert 'const UPLOAD_URL = "/files"' in js
    widgets = [w for p in app_config["panes"].values() for w in p.get("widgets", []) if w["type"] == "task-refs"]
    assert len(widgets) == 1 and "mode" not in widgets[0], "one widget, in the Detail pane"


def test_detail_pane_shows_the_selected_task_and_its_descendants(server_config):
    """The Detail pane's references come from the selected task and every task
    split from it, not from the References pane's cursor: the widget holds its
    own live queries (mkio-table is not the only thing allowed to subscribe) —
    `all_tasks` for the parent/child edges, `task_refs` filtered to the
    subtree, server-side either way — and `state.selected_ref` only marks a
    line the list already shows."""
    js = (STATIC / "refs.js").read_text()
    assert 'const REFS_SERVICE = "task_refs"' in js
    assert 'const TASKS_SERVICE = "all_tasks"' in js
    for service in ("REFS_SERVICE", "TASKS_SERVICE"):
        assert f'client.subscribe({service}, "query"' in js, f"{service} is subscribed live"
    refs, tasks = server_config["services"]["task_refs"], server_config["services"]["all_tasks"]
    assert refs["protocol"] == tasks["protocol"] == "query"
    assert "task_id" in refs["filterable"], "the widget filters its references on task_id"
    for column in ("task_id", "parent_task_id", "title"):
        assert f'"{column}"' in js, f"the subtree is built from {column}"
    # one task, or the whole subtree, filtered on the server either way
    assert "`task_id == ${quote(ids[0])}`" in js
    assert "CONTAINS([${ids.map(quote).join(\", \")}], task_id)" in js
    assert "refs.has(selectedRefId)" in js, "a reference this pane does not list marks nothing"
    assert "client.unsubscribe(subid)" in js, "one reference subscription at a time"


def test_the_detail_pane_is_the_one_widget(app_config):
    """The widget is the whole Detail body — the task block replaced the text
    widgets that showed the title and the notes — and every class it paints
    with has a rule of its own."""
    pane = app_config["panes"]["task-detail"]
    assert [w["type"] for w in pane["widgets"]] == ["task-refs"], "one widget, no text widgets"
    js = (STATIC / "refs.js").read_text()
    css = (STATIC / "mktask.css").read_text()
    for cls in ("task-refs-toolbar", "task-refs-main", "task-refs-task", "task-refs-task-marked",
                "task-refs-task-title", "task-refs-task-id", "task-refs-fields",
                "task-refs-field-label", "task-refs-field-value"):
        assert f".{cls}" in css, f"class {cls} has no CSS rule"
        assert cls in js, f"class {cls} is not used"


def test_the_toolbar_acts_on_the_pane_cursor(app_config):
    """One cursor over the body: the task block or a reference, whichever was
    clicked last. Edit follows it; Delete is a reference's alone, so a task is
    never deleted from the pane that shows its notes."""
    js = (STATIC / "refs.js").read_text()
    assert '"mkui-table-toolbar task-refs-toolbar"' in js, "mkui's own toolbar classes"
    assert '"mkui-btn mkui-toolbar-btn"' in js, "mkui's own button classes"
    for label in ('toolbarBtn("Edit"', 'toolbarBtn("Delete"'):
        assert label in js, f"the toolbar has no {label}"
    assert 'openBorrowed("editRef", ref)' in js and 'openBorrowed("editTask", task)' in js,         "Edit opens the dialog the cursor calls for"
    assert 'openBorrowed("deleteRef", ref)' in js
    assert "deleteBtn.disabled = !ref" in js, "Delete needs a reference"
    assert "editBtn.disabled = !ref && !task" in js
    # A live update republishes the selected row (mkui's publishRow), so the
    # block repaints without a refetch; only a new Task ID resets the cursor.
    assert "const changed = id !== taskId" in js and "if (changed)" in js


def test_snippets_come_before_the_files():
    """Section order: what was written down about a task reads before what was
    filed with it. Task links are not in SECTIONS — they group by relation
    above these — and `image` is a presentation of `file`, not a fifth kind."""
    from mktask.services import REF_KINDS

    js = (STATIC / "refs.js").read_text()
    order = re.findall(r'\["([a-z]+)", "[A-Za-z]+"\]',
                       re.search(r"const SECTIONS = \[(.*?)\];", js).group(1))
    assert order == ["text", "image", "file", "url"], f"unexpected section order {order}"
    assert set(order) - {"image"} == set(REF_KINDS) - {"task"}, \
        "a reference kind with no section, or a section with no kind"
    assert 'GRID_SECTIONS = new Set(["image"])' in js, "only the images are a grid"


def test_images_preview_as_a_grid_of_thumbnails(server_config):
    """An image reference shows as a thumbnail, not as a file name: images get
    their own section, told from other files by the stored mime, and the
    thumbnail is the file itself (mktask keeps no derived images)."""
    js = (STATIC / "refs.js").read_text()
    css = (STATIC / "mktask.css").read_text()
    assert '["image", "Images"]' in js and '["file", "Files"]' in js, "images sit apart from files"
    assert "mime" in server_config["tables"]["task_refs"]["columns"], "the section comes from the mime"
    assert 'isImage = (ref) => ref.kind === "file"' in js
    assert "img.src = ref.href" in js, "the thumbnail is the stored file, scaled by CSS"
    assert 'img.loading = "lazy"' in js
    for cls in ("task-refs-grid", "task-refs-tile", "task-refs-thumb",
                "task-refs-tile-marked", "task-refs-tile-owner"):
        assert f".{cls}" in css, f"class {cls} has no CSS rule"
        assert cls in js, f"class {cls} is not used"


def test_go_to_selects_the_linked_task(app_config):
    """mkui >= 0.2.23: `table.select` on the Tasks pane publishes the row as a
    click does, so the Detail and References panes follow. No viewer pane."""
    import mkui
    assert tuple(int(x) for x in mkui.__version__.split(".")[:3]) >= (0, 2, 23)
    js = (STATIC / "refs.js").read_text()
    assert 'fireAction("table.select"' in js
    assert 'app.fireAction("table.select", { pane: tasksPane, keys: [taskId] })' in js
    assert 'spec.tasksPane ?? "tasks"' in js, "targets the Tasks pane by default"
    assert "pane.show" not in js, "Go to selects the task; it opens no window"
    for gone in ("linked-task", "linked_task"):
        assert gone not in json.dumps(app_config), f"{gone} went with the Linked Task pane"
    assert "linked-task" not in app_config["panes"]
    # the three outcomes table.select reports are all handled
    for outcome in ("selected", "hidden", "missing"):
        assert f"result?.{outcome}" in js, f"refs.js ignores a {outcome} result"


def test_every_lookup_service_is_used_and_exists(app_config, server_config):
    """A reqrep is named by a dialog's optionsFrom or by refs.js, and every
    such name is a real reqrep: neither side goes stale on its own."""
    js = (STATIC / "refs.js").read_text()
    wanted = set(re.findall(r'request\("([a-z_]+)"', js))
    wanted |= {n.get("optionsFrom", {}).get("service") for n in _walk(app_config["panes"])
               if isinstance(n, dict) and "optionsFrom" in n}
    # mkui asks a pane's `history` services itself: `versions` for what a
    # step is about to change, `state` for whether a redo is there to offer.
    for _, pane in _table_panes(app_config):
        hist = pane.get("history", {})
        wanted |= {hist.get("versions"), hist.get("state")}
    wanted.discard(None)
    reqreps = {name for name, svc in server_config["services"].items() if svc["protocol"] == "reqrep"}
    mkui_owned = {n for n in reqreps if n.startswith("mkui_")}  # mkui's layout store calls these itself
    assert wanted <= reqreps, f"unknown lookup service {wanted - reqreps}"
    assert reqreps - mkui_owned <= wanted, f"unused lookup service {reqreps - mkui_owned - wanted}"


# ─── Dialogs ───────────────────────────────────────────────────────

def _dialogs(app_config):
    for node in _walk(app_config["panes"]):
        if "dialog" in node and isinstance(node["dialog"], dict):
            yield node["dialog"]


def test_level_selects_offer_one_to_five(app_config):
    seen = 0
    for dialog in _dialogs(app_config):
        for field in _walk(dialog["fields"]):
            if field.get("name") in ("importance", "urgency"):
                seen += 1
                assert field["type"] == "select"
                assert [o["value"] for o in field["options"]] == [1, 2, 3, 4, 5]
                value = field["value"]
                assert value.startswith("${row.") if isinstance(value, str) else 1 <= value <= 5
    assert seen == 6, "Add, Split, and Edit each carry importance and urgency"


def test_dialogs_have_required_title(app_config):
    for dialog in _dialogs(app_config):
        assert dialog.get("title")
        assert dialog.get("submit", {}).get("label")
        if dialog["submit"]["op"] in ("add", "split", "edit"):
            title = next(f for f in _walk(dialog["fields"]) if f.get("name") == "title")
            assert title.get("required") is True


def _hidden(dialog, name):
    fields = [f for f in _walk(dialog["fields"]) if f.get("name") == name]
    assert fields, f"{dialog['title']} has no {name} field"
    assert fields[0]["type"] == "hidden"
    return fields[0]["value"]


def test_edit_and_delete_carry_hidden_task_id(app_config):
    for dialog in _dialogs(app_config):
        if dialog["submit"]["op"] in ("edit", "delete"):
            assert _hidden(dialog, "task_id") == "${row.task_id}"


def test_move_picks_its_parent_from_move_options(app_config, server_config):
    """The picker's "top level" is a sentinel, never '': mkui's optionsFrom
    select prepends its own empty option and cannot be preselected, so an
    empty value would be the same entry and an untouched dialog would
    promote the task. `required` is what refuses the empty option."""
    from mktask.services import TOP_LEVEL

    move = next(d for d in _dialogs(app_config) if d["submit"]["op"] == "move")
    assert _hidden(move, "task_id") == "${row.task_id}"
    field = next(f for f in _walk(move["fields"]) if f.get("name") == "parent_task_id")
    assert field["type"] == "select"
    assert field["required"] is True
    assert "value" not in field, "mkui cannot preselect an optionsFrom select"
    assert field["optionsFrom"]["service"] == "move_options"
    assert field["optionsFrom"]["params"] == {"task_id": "${row.task_id}"}
    assert TOP_LEVEL in server_config["services"]["move_options"]["sql"]


def test_move_says_where_the_task_sits_now(app_config):
    """The select cannot show the current parent, so a readonly line must."""
    move = next(d for d in _dialogs(app_config) if d["submit"]["op"] == "move")
    lines = [f for f in _walk(move["fields"]) if f.get("type") == "readonly"]
    assert any("parent_task_id" in f["value"] for f in lines), "no 'where it sits now' line"


def test_split_links_child_to_selected_row(app_config):
    """Split carries the parent's Task ID hidden and never picks the child's."""
    split = next(d for d in _dialogs(app_config) if d["submit"]["op"] == "split")
    assert _hidden(split, "parent_task_id") == "${row.task_id}"


def test_no_client_sends_server_filled_fields(app_config):
    """Task IDs and the sequence number come from TaskTransactions, never a client."""
    for node in _walk(app_config["panes"]):
        if "service" in node and "op" in node and node["op"] in ("add", "split"):
            assert not ({"task_id", "last"} & set(node.get("data", {})))
    for dialog in _dialogs(app_config):
        if dialog["submit"]["op"] in ("add", "split"):
            names = {f["name"] for f in _walk(dialog["fields"]) if "name" in f}
            assert not ({"task_id", "last"} & names), dialog["title"]


def test_delete_buttons_are_red_only_when_armed(app_config):
    """A styled button must not change width between states: colors only."""
    seen = 0
    for pane_id, pane in _table_panes(app_config):
        for delete in (b for b in pane.get("buttons", []) if b["label"] == "Delete"):
            seen += 1
            rules = delete["style"]
            armed = next(r for r in rules if r.get("when") == "enabled")
            assert armed["background"].lower() in ("#c62828", "red")
            for rule in rules:
                assert not ({"bold", "caps"} & set(rule)), "size-changing keys shift the toolbar"
            assert all(set(r) - {"when"} <= {"color", "background"} for r in rules)
    assert seen == 3, "Tasks, References, and Relations each have a Delete"


def test_detail_delete_borrows_the_red(app_config):
    """The Detail pane paints its Delete from the same button's `when =
    "enabled"` rule, so the three Deletes cannot end up different reds."""
    widget = next(w for w in app_config["panes"]["task-detail"]["widgets"]
                  if w["type"] == "task-refs")
    src = widget["dialogs"]["deleteRef"]
    button = next(b for b in app_config["panes"][src["pane"]]["buttons"]
                  if b["label"] == src["button"])
    assert any(r.get("when") == "enabled" for r in button["style"])
    js = (STATIC / "refs.js").read_text()
    assert 'rule.when === "enabled"' in js, "refs.js reads the armed rule"
    assert "mkui-btn-styled" in js, "refs.js paints it the way mkui does"


def test_row_buttons_declare_row_unit(app_config):
    for pane_id, pane in _table_panes(app_config):
        for button in pane.get("buttons", []):
            action = button["action"]
            uses_row = "${row." in json.dumps(action)
            if action["type"] == "dialog" and uses_row:
                assert button.get("unit") == "row", f"{pane_id}: {button['label']} prefills from a row"
            if action["type"] == "transaction":
                assert button["enable"].get("minSelected", 0) >= 1, button["label"]


def test_status_gates_on_complete_and_reopen(tasks_pane):
    by_label = {b["label"]: b for b in tasks_pane["buttons"]}
    assert "r.status == 'open'" in by_label["Complete"]["enable"]["when"]
    assert "r.status == 'complete'" in by_label["Reopen"]["enable"]["when"]


def test_the_word_done_is_gone(app_config, server_config):
    """A task is open or complete; "done" is not a status, a label, or an op."""
    text = json.dumps(app_config) + (PKG / "mktask.toml").read_text()
    assert not re.search(r"\bdone\b", text, re.IGNORECASE)


def test_timestamps_are_stamped_client_side(app_config):
    text = json.dumps(app_config["panes"])
    stamp = "${TIME(NOW(), '%Y-%m-%d %H:%M:%S')}"
    assert text.count(json.dumps(stamp)[1:-1]) >= 4, "complete, reopen, and edit stamp timestamps"


# ─── Tree rows ─────────────────────────────────────────────────────

def test_tree_links_real_columns(tasks_pane, task_columns):
    tree = tasks_pane["tree"]
    assert tree["child"] == "parent_task_id" and tree["parent"] == "task_id"
    assert {tree["child"], tree["parent"]} <= task_columns
    assert tree["column"] in tasks_pane["columns"]
    assert tree["expand"] == "all" or isinstance(tree["expand"], int)
    assert tree["filterScope"] in ("roots", "children", "all")


def test_open_only_filter_tests_every_row(tasks_pane):
    """With Branch scope a complete child under an open parent is hidden and an
    open child keeps its complete parent visible as the way to it."""
    assert tasks_pane["tree"]["filterScope"] == "all"
    assert tasks_pane["filters"]["status"] == ["open"]


def test_expand_menu_targets_the_tasks_pane(app_config):
    items = {i["label"]: i for i in _walk(app_config["menubar"]) if i.get("action") == "table.expand"}
    assert items["Expand All"]["args"] == {"pane": "tasks", "depth": "all"}
    assert items["Collapse All"]["args"] == {"pane": "tasks"}


# ─── References ────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def references_pane(app_config) -> dict:
    return app_config["panes"]["references"]


def _dialog_by_op(app_config, pane_id, op, label=None):
    pane = app_config["panes"][pane_id]
    for button in pane["buttons"]:
        dialog = button["action"].get("dialog")
        if dialog and dialog["submit"]["op"] == op and (label is None or button["label"] == label):
            return dialog
    raise AssertionError(f"{pane_id} has no {op} dialog")


def _fields(dialog):
    return {f["name"]: f for f in _walk(dialog["fields"]) if "name" in f}


def test_one_reference_dialog_for_every_kind(app_config, server_config):
    """URL, text, and task link share one dialog. Distinct scratch inputs
    (`_url`, `_task`) feed one computed hidden `href`, so the server sees a
    single field and no dialog ever names two fields alike (mkui ≥ 0.2.21)."""
    from mktask.services import REF_KINDS
    tasks = app_config["panes"]["tasks"]
    assert [b["label"] for b in tasks["buttons"] if b["label"] in ("Reference", "Link")] == ["Reference"]
    dialog = _dialog_by_op(app_config, "tasks", "add_ref")
    f = _fields(dialog)
    assert f["task_id"] == {"name": "task_id", "type": "hidden", "value": "${row.task_id}"}
    kinds = [o["value"] for o in f["kind"]["options"]]
    assert kinds == ["url", "text", "task"] and set(kinds) < set(REF_KINDS), "file goes through the drop box"
    assert f["_url"]["showWhen"] == "kind == 'url'" and f["_url"]["required"] is True
    assert f["body"]["showWhen"] == "kind == 'text'" and f["body"]["required"] is True
    assert f["relation"]["showWhen"] == "kind == 'task'" and f["_task"]["showWhen"] == "kind == 'task'"
    assert f["href"]["type"] == "hidden" and "showWhen" not in f["href"], "always submitted"
    assert f["href"]["compute"] == "IF(kind == 'task', _task, IF(kind == 'url', _url, ''))"
    assert f["label"]["showWhen"] == "kind != 'task'" and "compute" in f["label"], "a live suggestion"
    assert "${" in dialog["title"] and "${" in dialog["footer"]["note"], "title and note follow the kind"
    for name, params in (("relation", None), ("_task", {"task_id": "${row.task_id}"})):
        src = f[name]["optionsFrom"]
        svc = server_config["services"][src["service"]]
        assert svc["protocol"] == "reqrep"
        assert {src["value"], src["label"]} == {"value", "label"} and "value" in svc["sql"] and "label" in svc["sql"]
        assert src.get("params") == params
        for p in (params or {}):
            assert f":{p}" in svc["sql"], "the select's params feed the SQL"


def test_references_pane_shows_the_stored_wording(references_pane):
    label = references_pane["display"]["label"]
    assert "LINK(label, href)" in label, "a reference with an href is a hyperlink"
    assert "kind == 'task'" in label, "a Task ID is not a hyperlink"
    assert "relation" not in references_pane.get("display", {}), "the wording is the value"
    assert references_pane["select"]["state"] == "selected_ref"
    assert references_pane["service"] == "task_refs"


def test_one_edit_dialog_for_every_kind(app_config):
    pane = app_config["panes"]["references"]
    edit = next(b for b in pane["buttons"] if b["label"] == "Edit")
    assert edit["enable"] == {"connected": True}, "task links are editable too (their relation)"
    dialog = edit["action"]["dialog"]
    assert _hidden(dialog, "ref_id") == "${row.ref_id}"
    f = _fields(dialog)
    assert f["relation"]["showWhen"] == "row.kind == 'task'" and f["relation"]["value"] == "${row.relation}"
    assert f["relation"]["optionsFrom"]["service"] == "relation_options"
    assert f["label"]["showWhen"] == "row.kind != 'task'", "a link's label follows the linked task"
    assert f["href"]["showWhen"] == "row.kind == 'url'"
    assert f["body"]["showWhen"] == "row.kind == 'text'"
    linked = next(x for x in _walk(dialog["fields"]) if x.get("label") == "Linked task")
    assert linked["type"] == "readonly" and linked["showWhen"] == "row.kind == 'task'" and "name" not in linked
    delete = _dialog_by_op(app_config, "references", "delete_ref")
    assert _hidden(delete, "ref_id") == "${row.ref_id}"


def test_readonly_lines_carry_no_name(app_config):
    """Display-only lines need no name since mkui 0.2.22 (0.2.21 blanked nameless ones)."""
    for dialog in _dialogs(app_config):
        for f in _walk(dialog["fields"]):
            if f.get("type") == "readonly":
                assert "name" not in f, f"{dialog['title']}: readonly {f.get('label')!r} needs no name"


def test_relations_pane_and_dialogs(app_config, server_config):
    pane = app_config["panes"]["relations"]
    assert pane["service"] == "all_relations"
    assert set(pane["columns"]) == {"forward", "backward", "notes"}
    by_label = {b["label"]: b["action"]["dialog"] for b in pane["buttons"]}
    assert set(by_label) == {"New", "Edit", "Delete"}
    new = _fields(by_label["New"])
    assert new["forward"]["required"] is True and "required" not in new["backward"], "blank backward = symmetric"
    assert "${forward}" in new["backward"]["placeholder"], "the placeholder shows the symmetric default live"
    edit = _fields(by_label["Edit"])
    assert _hidden(by_label["Edit"], "relation_id") == "${row.relation_id}"
    assert edit["forward"]["value"] == "${row.forward}"
    assert "row.forward == row.backward" in edit["backward"]["value"], "a symmetric pair shows a blank backward"
    assert _hidden(by_label["Delete"], "relation_id") == "${row.relation_id}"
    assert "relations" in {i["args"] for i in _walk(app_config["menubar"]) if i.get("action") == "pane.show"}


def test_relations_are_seeded_from_the_package(server_config):
    table = server_config["tables"]["relations"]
    assert table["seed"] == "relations.json"
    seed = json.loads((PKG / "relations.json").read_text())
    assert {r["forward"] for r in seed} == {"blocks", "relates to"}
    wordings = [w for r in seed for w in {r["forward"], r["backward"]}]
    assert len(wordings) == len({w.lower() for w in wordings}), "seed wordings are unique"
    assert set(seed[0]) <= set(table["columns"])


def test_reference_kinds_match_the_service(server_config):
    from mktask.services import REF_KINDS
    comment = (PKG / "mktask.toml").read_text()
    for kind in REF_KINDS:
        assert f'"{kind}"' in comment, f"kind {kind} is documented in the TOML"
    add_ref = server_config["services"]["tasks"]["ops"]["add_ref"][0]
    assert add_ref["defaults"]["kind"] == "url"
    assert "task_refs" in server_config["tables"]
    assert set(server_config["services"]["task_refs"]["filterable"]) == {"task_id", "kind", "relation"}


def test_main_frame_stacks_tasks_over_references(app_config):
    main = next(f for f in app_config["frames"] if f["id"] == "main")
    layout = main["layout"]
    assert layout["type"] == "split" and layout["dir"] == "v"
    assert [c["children"] for c in layout["children"]] == [["tasks"], ["references"]]
    assert abs(sum(layout["ratios"]) - 1) < 1e-9


# ─── Wiring ────────────────────────────────────────────────────────

def test_expect_name_matches_server(app_config, server_config):
    assert app_config["mkio"]["expect"]["name"] == server_config["name"]


def test_layout_menu_matches_layouts_block(app_config, server_config):
    assert "layouts" in app_config, "Layout menu needs a layouts block"
    items = list(_walk(app_config["menubar"]))
    assert any(i.get("layouts") is True for i in items)
    assert any(i.get("action") == "layout.save" for i in items)
    assert any(i.get("action") == "layout.reset" for i in items)
    for svc in ("mkui_layouts", "mkui_layouts_list", "mkui_layouts_get"):
        assert svc in server_config["services"]
    assert "mkui_layouts" in server_config["tables"]


def test_window_menu_lists_open_windows(app_config):
    assert any(i.get("windows") is True for i in _walk(app_config["menubar"]))


def test_frames_place_every_pane_once_or_menu_reaches_it(app_config):
    """A pane is either in the default layout or opened on demand from the
    Tasks menu (the Linked Task pane opens when a task link is followed)."""
    placed = [p for f in app_config["frames"] for p in _layout_panes(f["layout"])]
    assert len(placed) == len(set(placed)), "a pane placed twice"
    assert set(placed) <= set(app_config["panes"])
    shown = {i["args"] for i in _walk(app_config["menubar"]) if i.get("action") == "pane.show"}
    assert shown == set(app_config["panes"]), "the Tasks menu shows every pane"
    assert "relations" not in placed, "opened on demand, not at start"
    assert {"tasks", "references", "task-detail"} <= set(placed)
    for frame in app_config["frames"]:
        assert 0 <= frame["x"] and frame["x"] + frame["w"] <= 1
        assert 0 <= frame["y"] and frame["y"] + frame["h"] <= 1


def test_static_routes_resolve(server_config):
    statics = server_config["static"]
    assert statics["/mkui"] == "__mkui__"
    assert (PKG / statics["/"]).is_dir()
    assert (PKG / statics["/"] / "index.html").is_file()


def test_css_classes_used_by_widgets_exist(app_config):
    css = (STATIC / "mktask.css").read_text()
    for node in _walk(app_config["panes"]):
        if "class" in node and node.get("type") == "text":
            assert f".{node['class']}" in css, f"class {node['class']} has no CSS rule"


def test_every_transaction_field_has_default_or_is_sent(app_config, server_config):
    """A field without a default is required by mkio: the UI must send it."""
    services = server_config["services"]
    for node in _walk(app_config["panes"]):
        if not ("service" in node and "op" in node) or node["op"] in SERVICE_OPS:
            continue
        ops = services[node["service"]]["ops"][node["op"]]
        sent = set(node.get("data", {}))
        dialog = next((d for d in _walk(app_config["panes"]) if d.get("submit") is node), None)
        if dialog is not None:
            sent |= {f["name"] for f in _walk(dialog["fields"])
                     if "name" in f and not f["name"].startswith("_") and f.get("type") != "readonly"}
        for op in ops:
            required = set(op.get("fields", [])) | set(op.get("key", []))
            required -= set(op.get("defaults", {}))
            assert required <= sent, f"{node['service']}.{node['op']} misses {required - sent}"


# ─── Recorded versions ─────────────────────────────────────────────

def test_history_blocks_are_well_formed(app_config, server_config):
    """A pane's `history` block is what mkui needs to show a record's
    versions and to step it. mkui drops a key it does not know with a
    console warning rather than failing, so this is the only thing that
    catches a typo in one.
    """
    services, tables = server_config["services"], server_config["tables"]
    seen = 0
    for pane_id, pane in _table_panes(app_config):
        hist = pane.get("history")
        if hist is None:
            continue
        seen += 1
        assert set(hist) <= HISTORY_KEYS, f"{pane_id}.history has unknown {set(hist) - HISTORY_KEYS}"

        table = hist["table"]
        assert tables.get(table, {}).get("versioned"), f"{pane_id}.history.table {table} is not versioned"
        assert hist["key"] == primary_key_columns(tables[table]), \
            f"{pane_id}.history.key is not {table}'s primary key"

        # The pane shows the versions of the records it lists, not another
        # table's, so the two must agree on what a record is.
        assert services[pane["service"]]["primary_table"] == table, \
            f"{pane_id} lists {services[pane['service']]['primary_table']} but records {table}"

        feed = services[hist["feed"]]
        assert feed["protocol"] == "query", f"{pane_id}.history.feed must be a query"
        assert feed["primary_table"] == history_table_name(table)
        assert set(hist["key"]) <= set(feed.get("filterable", [])), \
            f"{hist['feed']} cannot be narrowed to one record"

        for name in ("versions", "state"):
            if name in hist:
                assert services[hist[name]]["protocol"] == "reqrep", f"{pane_id}.history.{name}"

        known = _table_columns(table, server_config)
        for col in hist.get("columns", []):
            assert col in known, f"{pane_id}.history.columns names unknown column {col}"

        for direction in ("undo", "redo"):
            step = hist.get(direction)
            if step is None:
                continue
            assert step["service"] in services, f"{pane_id}.history.{direction} unknown service"
            assert "ops" in services[step["service"]], \
                f"{pane_id}.history.{direction} names {step['service']}, which runs no ops"
            assert step["op"] in SERVICE_OPS, \
                f"{pane_id}.history.{direction} should step a whole action, not one row"
    assert seen == 2, "the Tasks and References panes both record versions"


def test_version_column_shows_only_where_a_pane_asks(app_config, server_config):
    """`_mkio_version` is mkio's, and mkui shows it only when a pane names
    it — so a pane that shows one must be recording versions to show."""
    for pane_id, pane in _table_panes(app_config):
        if VERSION_COLUMN in pane.get("columns", []):
            assert "history" in pane, f"{pane_id} shows a version but has no history block"


def test_actions_name_actions_mkui_registers(app_config):
    """A button firing an action mkui never registered does nothing at all,
    silently. The names are few and fixed, so they can simply be listed."""
    seen = 0
    for node in _walk(app_config["panes"]):
        action = node.get("action")
        if isinstance(action, dict) and action.get("type") == "action":
            seen += 1
            assert action["name"] in MKUI_ACTIONS, f"unknown action {action['name']}"
            pane = (action.get("args") or {}).get("pane") if isinstance(action.get("args"), dict) else None
            if pane is not None:
                assert pane in app_config["panes"], f"action targets unknown pane {pane}"
    for item in _walk(app_config["menubar"]):
        if isinstance(item.get("action"), str):
            seen += 1
            assert item["action"] in MKUI_ACTIONS, f"unknown menu action {item['action']}"
    # refs.js fires its own, and they go stale the same way.
    js = (STATIC / "refs.js").read_text()
    for name in set(re.findall(r'fireAction\("([a-z.]+)"', js)):
        seen += 1
        assert name in MKUI_ACTIONS, f"refs.js fires unknown action {name}"
    assert seen > 10


def test_delete_dialogs_say_it_cannot_be_undone(app_config):
    """Everything else on a pane that records versions steps back; a delete
    does not, because mkio drops a row's history with the row. The one place
    that asymmetry has to be stated is where the user is about to do it."""
    found = 0
    for pane_id, pane in _table_panes(app_config):
        if "history" not in pane:
            continue
        for button in pane.get("buttons", []):
            if button["label"] != "Delete":
                continue
            found += 1
            text = json.dumps(button["action"]["dialog"]["fields"])
            assert "cannot be undone" in text, f"{pane_id}'s Delete does not say it is permanent"
    assert found >= 2, "the Tasks and References panes both delete"
