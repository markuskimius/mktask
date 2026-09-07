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

PKG = Path(__file__).resolve().parent.parent / "mktask"
STATIC = PKG / "static"


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
            assert node["op"] in svc["ops"], f"service {node['service']} has no op {node['op']}"


def test_transaction_fields_are_declared(app_config, server_config):
    """Every field a dialog or button sends must be one the op accepts."""
    services = server_config["services"]
    for node in _walk(app_config["panes"]):
        if not ("service" in node and "op" in node):
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
                if "name" in field:
                    sent.add(field["name"])
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


def _columns_of(pane, server_config) -> set:
    """The columns a pane's query returns: its service's primary table."""
    table = server_config["services"][pane["service"]]["primary_table"]
    return set(server_config["tables"][table]["columns"])


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


def test_detail_pane_reads_real_columns(app_config, task_columns):
    """`state.selected_task.<col>` mirrors a row, so <col> must be a real column."""
    for pane_id, root in (("task-detail", "selected_task"), ("linked-task", "linked_task")):
        text = json.dumps(app_config["panes"][pane_id])
        cols = set(re.findall(rf"state\.{root}\.([a-z_]+)", text))
        assert cols, f"{pane_id} reads state.{root}"
        assert cols <= task_columns, f"{pane_id} reads unknown {cols - task_columns}"


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
    """refs.js reads selected_task, selected_ref, and linked_task, calls two
    request-reply services, opens the Linked Task pane, and posts to /files."""
    js = (STATIC / "refs.js").read_text()
    for root in ("selected_task", "selected_ref", "linked_task"):
        assert root in app_config["state"], root
        assert f'"{root}"' in js, f"refs.js does not read state.{root}"
    for svc in re.findall(r'request\("([a-z_]+)"', js):
        assert server_config["services"][svc]["protocol"] == "reqrep", svc
    for op in re.findall(r'op: "([a-z_]+)"', js):
        assert op in server_config["services"]["tasks"]["ops"], op
    assert 'fireAction("pane.show"' in js
    assert '"linked-task"' in js and "linked-task" in app_config["panes"]
    assert "table.filter" not in js, "the References pane follows the selection through mkui's table linking"
    assert 'const UPLOAD_URL = "/files"' in js
    modes = {w.get("mode") for p in app_config["panes"].values() for w in p.get("widgets", []) if w["type"] == "task-refs"}
    assert modes == {None, "linked"}, "one drop box in Detail, one viewer in Linked Task"


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
    assert seen == 2, "Tasks and References each have a Delete"


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


def test_reference_dialog_offers_url_and_text(app_config):
    from mktask.services import REF_KINDS
    dialog = _dialog_by_op(app_config, "tasks", "add_ref", "Reference")
    fields = {f["name"]: f for f in _walk(dialog["fields"]) if "name" in f}
    assert fields["task_id"]["type"] == "hidden" and fields["task_id"]["value"] == "${row.task_id}"
    kinds = [o["value"] for o in fields["kind"]["options"]]
    assert kinds == ["url", "text"] and set(kinds) <= set(REF_KINDS)
    assert fields["href"]["showWhen"] == "kind == 'url'" and fields["href"]["required"] is True
    assert fields["body"]["showWhen"] == "kind == 'text'" and fields["body"]["required"] is True


def test_link_dialog_picks_a_task_by_title(app_config, server_config):
    from mktask.services import RELATIONS
    dialog = _dialog_by_op(app_config, "tasks", "add_ref", "Link")
    fields = {f["name"]: f for f in _walk(dialog["fields"]) if "name" in f}
    assert fields["kind"] == {"name": "kind", "type": "hidden", "value": "task"}
    assert [o["value"] for o in fields["relation"]["options"]] == list(RELATIONS)
    href = fields["href"]
    assert href["required"] is True
    source = href["optionsFrom"]
    svc = server_config["services"][source["service"]]
    assert svc["protocol"] == "reqrep"
    assert f":{next(iter(source['params']))}" in svc["sql"], "the select's params feed the SQL"
    assert source["params"]["task_id"] == "${row.task_id}", "excludes the task itself"
    assert {source["value"], source["label"]} == {"value", "label"}
    assert "value" in svc["sql"] and "label" in svc["sql"]


def test_references_pane_links_and_words_relations(references_pane):
    from mktask.services import RELATIONS
    label = references_pane["display"]["label"]
    assert "LINK(label, href)" in label, "a reference with an href is a hyperlink"
    assert "kind == 'task'" in label, "a Task ID is not a hyperlink"
    relation = references_pane["display"]["relation"]
    for name in RELATIONS:
        assert f"relation == '{name}'" in relation, f"{name} is worded for display"
    assert references_pane["select"]["state"] == "selected_ref"
    assert references_pane["service"] == "task_refs"


def test_reference_edit_never_touches_a_task_link(app_config):
    pane = app_config["panes"]["references"]
    edit = next(b for b in pane["buttons"] if b["label"] == "Edit")
    assert "row.kind != 'task'" in edit["enable"]["when"]
    dialog = edit["action"]["dialog"]
    assert _hidden(dialog, "ref_id") == "${row.ref_id}"
    fields = {f["name"]: f for f in _walk(dialog["fields"]) if "name" in f}
    assert fields["href"]["showWhen"] == "row.kind == 'url'"
    assert fields["body"]["showWhen"] == "row.kind == 'text'"
    delete = _dialog_by_op(app_config, "references", "delete_ref")
    assert _hidden(delete, "ref_id") == "${row.ref_id}"


def test_reference_kinds_and_relations_match_the_service(server_config):
    from mktask.services import RELATIONS, REF_KINDS
    comment = (PKG / "mktask.toml").read_text()
    for kind in REF_KINDS:
        assert f'"{kind}"' in comment, f"kind {kind} is documented in the TOML"
    for relation in RELATIONS:
        assert f'"{relation}"' in comment, f"relation {relation} is documented in the TOML"
    add_ref = server_config["services"]["tasks"]["ops"]["add_ref"][0]
    assert add_ref["defaults"]["kind"] == "url"
    assert "task_refs" in server_config["tables"]
    assert set(server_config["services"]["task_refs"]["filterable"]) == {"task_id", "kind", "relation"}


def test_references_follow_the_tasks_selection_by_link(app_config, server_config):
    """mkui table linking (≥ 0.2.19): Tasks broadcasts its Task ID under a name,
    References filters its own task_id column by it, with the toolbar chips off
    since the link is part of the setup, not something to fiddle with."""
    tasks, refs = app_config["panes"]["tasks"], app_config["panes"]["references"]
    assert tasks["link"] == {"broadcast": {"task_id": "task_id"}, "chips": False}
    assert refs["link"] == {"listen": {"task_id": "task_id"}, "chips": False}
    for name, col in tasks["link"]["broadcast"].items():
        assert col in _columns_of(tasks, server_config)
        listened = refs["link"]["listen"][name]
        listened = listened["column"] if isinstance(listened, dict) else listened
        assert listened in _columns_of(refs, server_config)
    assert "task_id" not in refs.get("filters", {}), "the link supplies the filter"
    import mkui
    assert tuple(int(x) for x in mkui.__version__.split(".")[:3]) >= (0, 2, 19)


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
    assert "linked-task" not in placed, "opened on demand, not at start"
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
        if not ("service" in node and "op" in node):
            continue
        ops = services[node["service"]]["ops"][node["op"]]
        sent = set(node.get("data", {}))
        dialog = next((d for d in _walk(app_config["panes"]) if d.get("submit") is node), None)
        if dialog is not None:
            sent |= {f["name"] for f in _walk(dialog["fields"]) if "name" in f}
        for op in ops:
            required = set(op.get("fields", [])) | set(op.get("key", []))
            required -= set(op.get("defaults", {}))
            assert required <= sent, f"{node['service']}.{node['op']} misses {required - sent}"
