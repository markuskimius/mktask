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
        if item.get("action") in ("table.filter", "table.sort", "table.columns"):
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


def _pane_columns(pane, table_columns):
    return table_columns | set(pane.get("values", {}))


def test_table_columns_exist(tasks_pane, task_columns):
    known = _pane_columns(tasks_pane, task_columns)
    for key in ("columns", "visible"):
        for col in tasks_pane.get(key, []):
            assert col in known, f"{key} names unknown column {col}"
    for key in ("labels", "styles", "types", "filters", "display"):
        for col in tasks_pane.get(key, {}):
            assert col in known, f"{key} names unknown column {col}"


def test_sort_names_known_columns(tasks_pane, task_columns):
    known = _pane_columns(tasks_pane, task_columns)
    sort = tasks_pane.get("sort", [])
    for spec in [sort] if isinstance(sort, str) else sort:
        col = spec["col"] if isinstance(spec, dict) else spec.lstrip("-")
        assert col in known, f"sort names unknown column {col}"


def test_row_references_name_real_columns(app_config, task_columns):
    """`${row.x}` in buttons and dialogs reads a column the query returns."""
    text = json.dumps(app_config["panes"])
    refs = set(re.findall(r"\$\{row\.([a-z_]+)", text)) | set(re.findall(r"\br\.([a-z_]+)", text))
    assert refs, "expected at least one row reference"
    assert refs <= task_columns, f"unknown row columns {refs - task_columns}"


def test_derived_values_read_real_columns(tasks_pane, task_columns):
    for col, expr in tasks_pane.get("values", {}).items():
        names = set(re.findall(r"\b([a-z_]+)\b", expr))
        assert names <= task_columns, f"values.{col} reads unknown {names - task_columns}"


def test_style_rules_read_known_names(tasks_pane, task_columns):
    known = _pane_columns(tasks_pane, task_columns) | {"value", "row", "col", "state"}
    rules = list(tasks_pane.get("rowStyle", []))
    for col_rules in tasks_pane.get("styles", {}).values():
        rules.extend(col_rules)
    for rule in rules:
        when = re.sub(r"'[^']*'", "", rule.get("when", ""))
        names = {n for n in re.findall(r"\b([a-z_]+)\b", when)}
        assert names <= known, f"style rule {when!r} reads unknown {names - known}"


def test_selection_state_declared(app_config, tasks_pane):
    path = tasks_pane["select"]["state"]
    assert path.split(".")[0] in app_config["state"]


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
    assert seen == 4, "Add and Edit each carry importance and urgency"


def test_dialogs_have_required_title(app_config):
    for dialog in _dialogs(app_config):
        assert dialog.get("title")
        assert dialog.get("submit", {}).get("label")
        if dialog["submit"]["op"] in ("add", "edit"):
            title = next(f for f in _walk(dialog["fields"]) if f.get("name") == "title")
            assert title.get("required") is True


def test_edit_and_delete_carry_hidden_id(app_config):
    for dialog in _dialogs(app_config):
        if dialog["submit"]["op"] in ("edit", "delete"):
            ids = [f for f in _walk(dialog["fields"]) if f.get("name") == "id"]
            assert ids and ids[0]["type"] == "hidden" and ids[0]["value"] == "${row.id}"


def test_row_buttons_declare_row_unit(tasks_pane):
    for button in tasks_pane["buttons"]:
        action = button["action"]
        uses_row = "${row." in json.dumps(action)
        if action["type"] == "dialog" and uses_row:
            assert button.get("unit") == "row", f"{button['label']} prefills from a row"
        if action["type"] == "transaction":
            assert button["enable"].get("minSelected", 0) >= 1, button["label"]


def test_status_gates_on_done_and_reopen(tasks_pane):
    by_label = {b["label"]: b for b in tasks_pane["buttons"]}
    assert "r.status == 'open'" in by_label["Done"]["enable"]["when"]
    assert "r.status == 'done'" in by_label["Reopen"]["enable"]["when"]


def test_timestamps_are_stamped_client_side(app_config):
    text = json.dumps(app_config["panes"])
    stamp = "${TIME(NOW(), '%Y-%m-%d %H:%M:%S')}"
    assert text.count(json.dumps(stamp)[1:-1]) >= 4, "done, reopen, and edit stamp timestamps"


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


def test_frames_cover_every_pane_once(app_config):
    placed = [p for f in app_config["frames"] for p in _layout_panes(f["layout"])]
    assert sorted(placed) == sorted(app_config["panes"])
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
            required = set(op.get("fields", [])) - set(op.get("defaults", {}))
            required |= set(op.get("key", []))
            assert required <= sent, f"{node['service']}.{node['op']} misses {required - sent}"
