"""Integration tests: the full mktask server over HTTP and WebSocket."""

import asyncio
import json
import re
import socket
import subprocess
import sys
import time

import aiohttp
import pytest

from mktask import __version__
from mktask.services import SEED_RELATIONS

SEED = json.loads(SEED_RELATIONS.read_text())
WORDINGS = sorted({w for r in SEED for w in (r["forward"], r["backward"])})  # every direction of every seeded pair


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(*args, db=":memory:"):
    """Launch `mktask` on a free port and wait for it to listen. Returns (proc, base_url)."""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "mktask", "-d", db, "-p", str(port), *args],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            if proc.poll() is not None:
                out = proc.stdout.read().decode()
                raise RuntimeError(f"server exited early:\n{out}")
            time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError("server did not start within 10s")
    return proc, f"http://127.0.0.1:{port}"


def _stop(proc):
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture(scope="module")
def server():
    """One server for the module, started as user "mark" so Task IDs are TKMA...."""
    proc, base = _start("--user", "mark")
    yield base
    _stop(proc)


async def _recv_json(ws):
    msg = await asyncio.wait_for(ws.receive(), timeout=5)
    return json.loads(msg.data)


async def _txn(ws, op, data, ref, expect="result"):
    await ws.send_json({"service": "tasks", "type": "transaction", "op": op,
                        "data": data, "ref": ref})
    resp = await _recv_json(ws)
    assert resp["type"] == expect, resp
    return resp


NOW = "2026-09-06 00:00:00"
TASK_ID = re.compile(r"^TK[A-Z0-9]{2}\d{8,}$")


async def _complete(ws, task_id, ref):
    return await _txn(ws, "complete", {"task_id": task_id, "completed_at": NOW, "updated_at": NOW}, ref)


async def _reopen(ws, task_id, ref):
    return await _txn(ws, "reopen", {"task_id": task_id, "updated_at": NOW}, ref)


async def _split(ws, parent, title, ref, **fields):
    return await _txn(ws, "split", {"title": title, "parent_task_id": parent, **fields}, ref)


async def _snapshot(ws, subid):
    await ws.send_json({"service": "all_tasks", "type": "subscribe",
                        "protocol": "query", "subid": subid, "ref": f"q-{subid}"})
    snap = await _recv_json(ws)
    await ws.send_json({"service": "all_tasks", "type": "unsubscribe", "subid": subid})
    return {r["title"]: r for r in snap["rows"]}


async def _request(ws, service, data):
    await ws.send_json({"service": service, "type": "request", "reqid": "rq", "data": data})
    resp = await _recv_json(ws)
    assert resp["type"] == "reply", resp
    return resp["rows"]


async def _add(ws, title, ref):
    """Add a task and return its Task ID, read back from an `all_tasks` snapshot."""
    await _txn(ws, "add", {"title": title}, ref)
    rows = await _snapshot(ws, f"s-{ref}")
    return rows[title]["task_id"]


async def _refs(ws, task_id):
    """A task's references, newest first — through the live `task_refs` query
    with the same server-side filter the References pane subscribes with."""
    await ws.send_json({"service": "task_refs", "type": "subscribe", "protocol": "query",
                        "subid": f"r-{task_id}", "ref": f"r-{task_id}",
                        "filter": f"task_id == '{task_id}'"})
    snap = await _recv_json(ws)
    await ws.send_json({"service": "task_refs", "type": "unsubscribe", "subid": f"r-{task_id}"})
    return sorted(snap["rows"], key=lambda r: r["ref_id"], reverse=True)


async def _add_ref(ws, task_id, ref, expect="result", **fields):
    return await _txn(ws, "add_ref", {"task_id": task_id, **fields}, ref, expect=expect)


async def _upload(session, server, body, mime):
    async with session.post(server + "/files", data=body, headers={"Content-Type": mime}) as resp:
        return resp.status, (await resp.json() if resp.status == 200 else await resp.text())


class TestHttp:
    async def test_routes(self, server):
        async with aiohttp.ClientSession() as s:
            for path, expect in [
                ("/", "mktask"),
                ("/static/app.json", "menubar"),
                ("/static/mktask.css", "mktask"),
                ("/mkio.js", "mkio"),
                ("/mkui/src/index.js", "Mkui"),
            ]:
                async with s.get(server + path) as resp:
                    assert resp.status == 200, path
                    assert expect in await resp.text(), path

    async def test_services(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.get(server + "/api/services") as resp:
                names = {svc["name"] for svc in await resp.json()}
        assert {"tasks", "all_tasks", "task_refs", "all_relations", "relation_options",
                "task_options", "mkui_layouts", "mkui_layouts_list", "mkui_layouts_get"} <= names


class TestWebSocket:
    async def test_mkio_identity(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                await ws.send_json({"service": "_mkio", "type": "request",
                                    "ref": "r1",
                                    "data": {"name": "mktask", "version": __version__}})
                row = (await _recv_json(ws))["row"]
        assert row["name"] == "mktask"
        assert row["version"] == __version__
        assert row["compatible"] is True

    async def test_task_lifecycle(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                await _txn(ws, "add", {"title": "Write tests", "importance": 5,
                                       "urgency": 4, "due": "2026-09-30",
                                       "notes": "pytest"}, "r1")
                await _txn(ws, "add", {"title": "Defaults only"}, "r2")
                await asyncio.sleep(0.2)

                rows = await _snapshot(ws, "s1")
                assert rows["Write tests"]["status"] == "open"
                assert rows["Write tests"]["importance"] == 5
                assert rows["Write tests"]["due"] == "2026-09-30"
                assert rows["Defaults only"]["importance"] == 3
                assert rows["Defaults only"]["notes"] == ""
                task_id = rows["Write tests"]["task_id"]
                assert TASK_ID.match(task_id), task_id
                assert rows["Write tests"]["parent_task_id"] == ""

                await _txn(ws, "edit", {"task_id": task_id, "title": "Write tests!",
                                        "notes": "", "importance": 2, "urgency": 2,
                                        "due": "", "updated_at": "2026-09-05 00:00:00"}, "r3")
                await _txn(ws, "complete", {"task_id": task_id, "completed_at": "2026-09-05 01:00:00",
                                            "updated_at": "2026-09-05 01:00:00"}, "r4")
                await asyncio.sleep(0.2)
                rows = await _snapshot(ws, "s2")
                assert "Write tests" not in rows
                row = rows["Write tests!"]
                assert row["status"] == "complete"
                assert row["completed_at"] == "2026-09-05 01:00:00"
                assert row["importance"] == 2

                await _txn(ws, "reopen", {"task_id": task_id,
                                          "updated_at": "2026-09-05 02:00:00"}, "r5")
                await asyncio.sleep(0.2)
                row = (await _snapshot(ws, "s3"))["Write tests!"]
                assert row["status"] == "open"
                assert row["completed_at"] == ""

                await _txn(ws, "delete", {"task_id": task_id}, "r6")
                await asyncio.sleep(0.2)
                assert "Write tests!" not in await _snapshot(ws, "s4")

    async def test_bad_op_keeps_connection(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                for ref in ("r1", "r2"):
                    await ws.send_json({"service": "tasks", "type": "transaction",
                                        "op": "bogus", "data": {}, "ref": ref})
                    resp = await _recv_json(ws)
                    assert resp["type"] == "error"
                    assert resp["ref"] == ref


class TestQueryFilter:
    async def test_status_filter_is_server_side(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                await _txn(ws, "add", {"title": "F-open"}, "f1")
                await _txn(ws, "add", {"title": "F-complete"}, "f2")
                await asyncio.sleep(0.2)
                complete_id = (await _snapshot(ws, "f-s1"))["F-complete"]["task_id"]
                await _complete(ws, complete_id, "f3")
                await asyncio.sleep(0.2)

                await ws.send_json({"service": "all_tasks", "type": "subscribe",
                                    "protocol": "query", "subid": "f-s2", "ref": "q",
                                    "filter": "status == 'complete'"})
                snap = await _recv_json(ws)
                await ws.send_json({"service": "all_tasks", "type": "unsubscribe", "subid": "f-s2"})
        titles = {r["title"] for r in snap["rows"]}
        assert "F-complete" in titles
        assert "F-open" not in titles

    async def test_add_stamps_created_at(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                await _txn(ws, "add", {"title": "Stamped"}, "c1")
                await asyncio.sleep(0.2)
                row = (await _snapshot(ws, "c-s1"))["Stamped"]
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", row["created_at"])
        assert row["updated_at"] == row["created_at"]
        assert row["completed_at"] == ""
        assert row["status"] == "open"

    async def test_add_requires_title(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                await ws.send_json({"service": "tasks", "type": "transaction", "op": "add",
                                    "data": {"notes": "no title"}, "ref": "t1"})
                resp = await _recv_json(ws)
        assert resp["type"] == "error"


class TestTaskIds:
    """Task IDs are TK + two prefix letters + a sequence padded to at least 8 digits."""

    async def test_sequence_is_global_and_never_reused(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                await _txn(ws, "add", {"title": "Seq-a"}, "i1")
                await _txn(ws, "add", {"title": "Seq-b"}, "i2")
                await asyncio.sleep(0.2)
                rows = await _snapshot(ws, "i-s1")
                a, b = rows["Seq-a"]["task_id"], rows["Seq-b"]["task_id"]
                assert a.startswith("TKMA") and b.startswith("TKMA")
                assert int(b[4:]) == int(a[4:]) + 1
                assert len(a) == 12

                await _txn(ws, "delete", {"task_id": b}, "i3")
                await _txn(ws, "add", {"title": "Seq-c"}, "i4")
                await asyncio.sleep(0.2)
                c = (await _snapshot(ws, "i-s2"))["Seq-c"]["task_id"]
        assert int(c[4:]) == int(b[4:]) + 1, "a deleted number is not handed out again"

    async def test_client_may_not_choose_a_task_id(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                resp = await _txn(ws, "add", {"title": "Mine", "task_id": "TKMA00000001"}, "i5", expect="error")
                assert "assigned by the server" in json.dumps(resp)
                await asyncio.sleep(0.2)
                assert "Mine" not in await _snapshot(ws, "i-s3")

    @pytest.mark.parametrize("user, prefix", [("m", "TKMX"), ("bob", "TKBO"), ("_x.y", "TKXY"), ("", "TKXX")])
    def test_prefix_from_user_flag(self, user, prefix):
        proc, base = _start("--user", user)
        try:
            async def run():
                async with aiohttp.ClientSession() as s:
                    async with s.ws_connect(base + "/ws") as ws:
                        await _txn(ws, "add", {"title": "P"}, "p1")
                        await asyncio.sleep(0.2)
                        return (await _snapshot(ws, "p-s"))["P"]["task_id"]
            assert asyncio.run(run()) == prefix + "00000001"
        finally:
            _stop(proc)

    def test_counter_persists_and_grows_past_eight_digits(self, tmp_path):
        """The sequence survives a restart and the ninth digit appears without wrapping."""
        import sqlite3
        db = str(tmp_path / "seq.db")

        async def add(base, title):
            async with aiohttp.ClientSession() as s:
                async with s.ws_connect(base + "/ws") as ws:
                    await _txn(ws, "add", {"title": title}, "x1")
                    await asyncio.sleep(0.2)
                    return (await _snapshot(ws, "x-s"))[title]["task_id"]

        proc, base = _start("--user", "mark", db=db)
        try:
            assert asyncio.run(add(base, "First")) == "TKMA00000001"
        finally:
            _stop(proc)

        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT last FROM counters WHERE name = 'task'").fetchone() == (1,)
            conn.execute("UPDATE counters SET last = 99999999")

        proc, base = _start("--user", "mark", db=db)
        try:
            assert asyncio.run(add(base, "Rollover")) == "TKMA100000000"
            assert asyncio.run(add(base, "After")) == "TKMA100000001"
        finally:
            _stop(proc)

    def test_counter_seeds_from_existing_ids_when_row_is_lost(self, tmp_path):
        import sqlite3
        db = str(tmp_path / "lost.db")

        async def add(base, title):
            async with aiohttp.ClientSession() as s:
                async with s.ws_connect(base + "/ws") as ws:
                    await _txn(ws, "add", {"title": title}, "y1")
                    await asyncio.sleep(0.2)
                    return (await _snapshot(ws, "y-s"))[title]["task_id"]

        proc, base = _start("--user", "mark", db=db)
        try:
            asyncio.run(add(base, "One"))
            asyncio.run(add(base, "Two"))
        finally:
            _stop(proc)
        with sqlite3.connect(db) as conn:
            conn.execute("DELETE FROM counters")
        proc, base = _start("--user", "mark", db=db)
        try:
            assert asyncio.run(add(base, "Three")) == "TKMA00000003"
        finally:
            _stop(proc)


class TestSplit:
    """Splitting nests tasks to any depth; complete, reopen, and delete follow the tree."""

    async def _tree(self, ws, tag):
        await _txn(ws, "add", {"title": f"{tag}-root", "importance": 5, "urgency": 5}, f"{tag}1")
        await asyncio.sleep(0.2)
        root = (await _snapshot(ws, f"{tag}-s1"))[f"{tag}-root"]["task_id"]
        await _split(ws, root, f"{tag}-child", f"{tag}2", importance=4, urgency=4)
        await asyncio.sleep(0.2)
        child = (await _snapshot(ws, f"{tag}-s2"))[f"{tag}-child"]["task_id"]
        await _split(ws, child, f"{tag}-grandchild", f"{tag}3")
        await asyncio.sleep(0.2)
        rows = await _snapshot(ws, f"{tag}-s3")
        return root, child, rows[f"{tag}-grandchild"]["task_id"], rows

    async def test_split_links_parent_and_child(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                root, child, grandchild, rows = await self._tree(ws, "L")
        assert rows["L-child"]["parent_task_id"] == root
        assert rows["L-grandchild"]["parent_task_id"] == child
        assert rows["L-root"]["parent_task_id"] == ""
        assert rows["L-child"]["importance"] == 4
        assert rows["L-grandchild"]["importance"] == 3, "split defaults apply like add's"

    async def test_split_requires_an_existing_parent(self, server):
        """No parent, an empty parent, or an unknown parent: no orphan is created."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                await _txn(ws, "split", {"title": "Orphan-1"}, "o1", expect="error")
                await _txn(ws, "split", {"title": "Orphan-2", "parent_task_id": ""}, "o2", expect="error")
                resp = await _txn(ws, "split", {"title": "Orphan-3", "parent_task_id": "TKMA99999990"}, "o3", expect="error")
                assert "Cannot split" in json.dumps(resp)
                await asyncio.sleep(0.2)
                rows = await _snapshot(ws, "o-s")
        assert not {"Orphan-1", "Orphan-2", "Orphan-3"} & set(rows)

    async def test_rejected_split_does_not_consume_a_number(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                await _txn(ws, "add", {"title": "Before-reject"}, "n1")
                await _txn(ws, "split", {"title": "Nope", "parent_task_id": "TKMA99999991"}, "n2", expect="error")
                await _txn(ws, "add", {"title": "After-reject"}, "n3")
                await asyncio.sleep(0.2)
                rows = await _snapshot(ws, "n-s")
        before, after = rows["Before-reject"]["task_id"], rows["After-reject"]["task_id"]
        assert int(after[4:]) == int(before[4:]) + 1

    async def test_reopen_on_a_parent_leaves_complete_children_alone(self, server):
        """Reopen climbs, it never descends: a finished child stays finished."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                root, child, grandchild, _ = await self._tree(ws, "R")
                await _complete(ws, root, "R4")
                await _reopen(ws, root, "R5")
                await asyncio.sleep(0.2)
                rows = await _snapshot(ws, "R-s")
        assert rows["R-root"]["status"] == "open"
        assert rows["R-child"]["status"] == "complete"
        assert rows["R-grandchild"]["status"] == "complete"

    async def test_complete_twice_and_reopen_open_are_harmless(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                await _txn(ws, "add", {"title": "Idem"}, "I1")
                await asyncio.sleep(0.2)
                task_id = (await _snapshot(ws, "I-s1"))["Idem"]["task_id"]
                await _reopen(ws, task_id, "I2")
                await _complete(ws, task_id, "I3")
                await _complete(ws, task_id, "I4")
                await asyncio.sleep(0.2)
                row = (await _snapshot(ws, "I-s2"))["Idem"]
        assert row["status"] == "complete" and row["completed_at"] == NOW

    async def test_split_tasks_are_editable_and_deletable_alone(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                root, child, grandchild, _ = await self._tree(ws, "E")
                await _txn(ws, "edit", {"task_id": child, "title": "E-child!", "notes": "n", "importance": 1,
                                        "urgency": 1, "due": "", "updated_at": NOW}, "E4")
                await _txn(ws, "delete", {"task_id": grandchild}, "E5")
                await asyncio.sleep(0.2)
                rows = await _snapshot(ws, "E-s")
        assert rows["E-child!"]["parent_task_id"] == root, "editing keeps the link"
        assert "E-grandchild" not in rows and "E-root" in rows

    async def test_complete_cascades_down_and_reopen_climbs_up(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                root, child, grandchild, _ = await self._tree(ws, "C")
                await _complete(ws, root, "C4")
                await asyncio.sleep(0.2)
                rows = await _snapshot(ws, "C-s4")
                assert {rows[t]["status"] for t in ("C-root", "C-child", "C-grandchild")} == {"complete"}
                assert rows["C-grandchild"]["completed_at"] == NOW

                await _reopen(ws, grandchild, "C5")
                await asyncio.sleep(0.2)
                rows = await _snapshot(ws, "C-s5")
                assert {rows[t]["status"] for t in ("C-root", "C-child", "C-grandchild")} == {"open"}
                assert rows["C-root"]["completed_at"] == ""

    async def test_complete_child_leaves_parent_open(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                root, child, grandchild, _ = await self._tree(ws, "P")
                await _complete(ws, child, "P4")
                await asyncio.sleep(0.2)
                rows = await _snapshot(ws, "P-s4")
        assert rows["P-root"]["status"] == "open"
        assert rows["P-child"]["status"] == "complete"
        assert rows["P-grandchild"]["status"] == "complete"

    async def test_delete_removes_the_subtree(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                root, child, grandchild, _ = await self._tree(ws, "D")
                await _txn(ws, "add", {"title": "D-bystander"}, "D4")
                await _txn(ws, "delete", {"task_id": root}, "D5")
                await asyncio.sleep(0.2)
                rows = await _snapshot(ws, "D-s4")
        assert not {"D-root", "D-child", "D-grandchild"} & set(rows)
        assert "D-bystander" in rows

    async def test_children_are_filterable_server_side(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                root, child, grandchild, _ = await self._tree(ws, "F")
                await ws.send_json({"service": "all_tasks", "type": "subscribe",
                                    "protocol": "query", "subid": "F-q", "ref": "q",
                                    "filter": f"parent_task_id == '{root}'"})
                snap = await _recv_json(ws)
                await ws.send_json({"service": "all_tasks", "type": "unsubscribe", "subid": "F-q"})
        assert [r["title"] for r in snap["rows"]] == ["F-child"]


class TestLiveDelete:
    async def test_subtree_delete_announces_every_row(self, server):
        """A live subscriber must hear each deleted Task ID, not the parent's
        for every row: mkio announces a delete with the request's data, so
        the cascade sends one request per row."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                parent = await _add(ws, "Live parent", "ld1")
                await _split(ws, parent, "Live child", "ld2")
                await _split(ws, parent, "Live child 2", "ld3")
                children = {r["task_id"] for r in (await _snapshot(ws, "ld4")).values()
                            if r["parent_task_id"] == parent}
                await ws.send_json({"service": "all_tasks", "type": "subscribe",
                                    "protocol": "query", "subid": "live", "ref": "ld5"})
                await _recv_json(ws)  # snapshot
                await ws.send_json({"service": "tasks", "type": "transaction", "op": "delete",
                                    "data": {"task_id": parent}, "ref": "ld6"})
                deleted = set()
                while True:
                    msg = await _recv_json(ws)
                    if msg["type"] == "result":
                        break
                    assert msg["type"] == "update" and msg["op"] == "delete", msg
                    deleted.add(msg["row"]["task_id"])
                await ws.send_json({"service": "all_tasks", "type": "unsubscribe", "subid": "live"})
        assert deleted == {parent} | children


class TestReferences:
    async def test_url_text_and_file_references(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                task = await _add(ws, "Refs", "rf1")
                await _add_ref(ws, task, "rf2", kind="url", href="https://example.com/x")
                await _add_ref(ws, task, "rf3", kind="text", body="Subject: hi\n\nlong body")
                await _add_ref(ws, task, "rf4", kind="file", href="/files/abc.png", mime="image/png", label="shot.png")
                await asyncio.sleep(0.2)
                rows = {r["kind"]: r for r in await _refs(ws, task)}
        assert rows["url"]["label"] == "https://example.com/x", "label defaults to the URL"
        assert rows["url"]["href"] == "https://example.com/x"
        assert rows["text"]["label"] == "Subject: hi", "label defaults to the first line"
        assert rows["text"]["href"] == "" and rows["text"]["body"].endswith("long body")
        assert rows["file"]["label"] == "shot.png" and rows["file"]["mime"] == "image/png"
        assert all(r["relation"] == "" for r in rows.values())
        assert all(r["ref_id"] > 0 for r in rows.values())

    async def test_add_ref_validation(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                task = await _add(ws, "Validation", "rv1")
                r = await _add_ref(ws, "TKMA99999999", "rv2", expect="error", kind="url", href="h")
                assert "no task" in r["message"]
                r = await _add_ref(ws, task, "rv3", expect="error", kind="bogus", href="h")
                assert "kind" in r["message"]
                r = await _add_ref(ws, task, "rv4", expect="error", kind="url", href="")
                assert "URL" in r["message"]
                r = await _add_ref(ws, task, "rv5", expect="error", kind="text", body="  ")
                assert "text" in r["message"]
                r = await _add_ref(ws, task, "rv6", expect="error", kind="file", href="elsewhere.png")
                assert "/files/" in r["message"]
                assert await _refs(ws, task) == []

    async def test_edit_ref_keeps_kind_specific_fields(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                task = await _add(ws, "Edit refs", "re1")
                await _add_ref(ws, task, "re2", kind="file", href="/files/keep.pdf", mime="application/pdf")
                await _add_ref(ws, task, "re3", kind="url", href="https://a")
                await asyncio.sleep(0.2)
                rows = {r["kind"]: r for r in await _refs(ws, task)}
                await _txn(ws, "edit_ref", {"ref_id": rows["file"]["ref_id"], "label": "Spec",
                                            "href": "/files/other.pdf", "updated_at": NOW}, "re4")
                await _txn(ws, "edit_ref", {"ref_id": rows["url"]["ref_id"], "label": "",
                                            "href": "https://b", "updated_at": NOW}, "re5")
                r = await _txn(ws, "edit_ref", {"ref_id": rows["url"]["ref_id"], "label": "x",
                                                "href": "", "updated_at": NOW}, "re6", expect="error")
                assert "URL" in r["message"]
                await asyncio.sleep(0.2)
                after = {r["kind"]: r for r in await _refs(ws, task)}
        assert after["file"]["label"] == "Spec"
        assert after["file"]["href"] == "/files/keep.pdf", "a file's href is not editable"
        assert after["url"]["href"] == "https://b"
        assert after["url"]["label"] == "https://b", "an emptied label falls back to the URL"
        assert after["url"]["updated_at"] == NOW

    async def test_delete_ref(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                task = await _add(ws, "Delete refs", "rd1")
                await _add_ref(ws, task, "rd2", kind="url", href="https://a")
                await asyncio.sleep(0.2)
                [row] = await _refs(ws, task)
                await _txn(ws, "delete_ref", {"ref_id": row["ref_id"]}, "rd3")
                await asyncio.sleep(0.2)
                assert await _refs(ws, task) == []
                await _txn(ws, "delete_ref", {"ref_id": row["ref_id"]}, "rd4")  # gone already: harmless

    async def test_task_delete_takes_its_references(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                parent = await _add(ws, "Doomed", "rt1")
                await _split(ws, parent, "Doomed child", "rt2")
                await asyncio.sleep(0.2)
                child = next(r["task_id"] for r in (await _snapshot(ws, "rt3")).values()
                             if r["parent_task_id"] == parent)
                await _add_ref(ws, parent, "rt4", kind="url", href="https://p")
                await _add_ref(ws, child, "rt5", kind="url", href="https://c")
                await asyncio.sleep(0.2)
                await _txn(ws, "delete", {"task_id": parent}, "rt6")
                await asyncio.sleep(0.2)
                assert await _refs(ws, parent) == []
                assert await _refs(ws, child) == []

    async def test_references_are_filterable_server_side(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                a = await _add(ws, "Filter A", "rq1")
                b = await _add(ws, "Filter B", "rq2")
                await _add_ref(ws, a, "rq3", kind="url", href="https://a")
                await _add_ref(ws, b, "rq4", kind="text", body="b")
                await asyncio.sleep(0.2)
                await ws.send_json({"service": "task_refs", "type": "subscribe", "protocol": "query",
                                    "subid": "rq", "ref": "rq5", "filter": f"task_id == '{b}'"})
                snap = await _recv_json(ws)
                await ws.send_json({"service": "task_refs", "type": "unsubscribe", "subid": "rq"})
        assert [r["task_id"] for r in snap["rows"]] == [b]
        assert snap["rows"][0]["kind"] == "text"


class TestTaskLinks:
    async def test_link_writes_both_sides(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                a = await _add(ws, "Blocker", "tl1")
                b = await _add(ws, "Blocked", "tl2")
                await _add_ref(ws, a, "tl3", kind="task", relation="blocks", href=b, label="ignored")
                await asyncio.sleep(0.2)
                [ra] = await _refs(ws, a)
                [rb] = await _refs(ws, b)
        assert (ra["kind"], ra["relation"], ra["href"], ra["label"]) == ("task", "blocks", b, "Blocked")
        assert (rb["kind"], rb["relation"], rb["href"], rb["label"]) == ("task", "blocked by", a, "Blocker")
        assert ra["ref_id"] != rb["ref_id"]

    async def test_link_validation_writes_nothing(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                a = await _add(ws, "Lonely", "tv1")
                b = await _add(ws, "Other", "tv2")
                r = await _add_ref(ws, a, "tv3", expect="error", kind="task", relation="relates to", href=a)
                assert "itself" in r["message"]
                r = await _add_ref(ws, a, "tv4", expect="error", kind="task", relation="eats", href=b)
                assert "relation" in r["message"]
                r = await _add_ref(ws, a, "tv5", expect="error", kind="task", relation="blocks", href="TKMA99999998")
                assert "no task" in r["message"]
                await _add_ref(ws, a, "tv6", kind="task", relation="blocks", href=b)
                r = await _add_ref(ws, a, "tv7", expect="error", kind="task", relation="blocks", href=b)
                assert "already blocks" in r["message"]
                await _add_ref(ws, a, "tv8", kind="task", relation="relates to", href=b)  # a second relation is fine
                await asyncio.sleep(0.2)
                assert len(await _refs(ws, a)) == 2
                assert len(await _refs(ws, b)) == 2

    async def test_no_link_within_a_tree(self, server):
        """Splitting already relates a task to everything in its tree — its
        ancestors, its descendants, and every descendant of an ancestor — so
        a link may only join tasks from different trees."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                root = await _add(ws, "Tree root", "ta1")
                await _split(ws, root, "Tree child", "ta2")
                await _split(ws, root, "Tree uncle", "ta3")
                await asyncio.sleep(0.2)
                kids = {r["title"]: r["task_id"] for r in (await _snapshot(ws, "ta4")).values()
                        if r["parent_task_id"] == root}
                child, uncle = kids["Tree child"], kids["Tree uncle"]
                await _split(ws, child, "Tree grandchild", "ta5")
                await asyncio.sleep(0.2)
                grandchild = next(r["task_id"] for r in (await _snapshot(ws, "ta6")).values()
                                  if r["parent_task_id"] == child)
                other = await _add(ws, "Other tree", "ta7")
                cases = [(root, child, "descendant"), (child, root, "ancestor"),
                         (root, grandchild, "descendant"), (grandchild, root, "ancestor"),
                         (child, uncle, "same tree"), (uncle, grandchild, "same tree"),
                         (grandchild, uncle, "same tree")]
                for a, b, word in cases:
                    for relation in WORDINGS:
                        r = await _add_ref(ws, a, f"ta-{a}-{b}-{relation}", expect="error",
                                           kind="task", relation=relation, href=b)
                        assert word in r["message"], r
                await _add_ref(ws, grandchild, "ta8", kind="task", relation="relates to", href=other)  # another tree: fine
                await asyncio.sleep(0.2)
                assert len(await _refs(ws, grandchild)) == 1
                for t in (root, child, uncle):
                    assert await _refs(ws, t) == []
                # the Link dialog's picker leaves the whole tree out too
                for t in (root, child, uncle, grandchild):
                    got = {r["value"] for r in await _request(ws, "task_options", {"task_id": t})}
                    assert other in got, t
                    assert not ({root, child, uncle, grandchild} & got), t
                got = {r["value"] for r in await _request(ws, "task_options", {"task_id": other})}
                assert {root, child, uncle, grandchild} <= got

    async def test_unlink_removes_both_sides(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                a = await _add(ws, "Unlink A", "tu1")
                b = await _add(ws, "Unlink B", "tu2")
                await _add_ref(ws, a, "tu3", kind="task", relation="relates to", href=b)
                await asyncio.sleep(0.2)
                [rb] = await _refs(ws, b)
                await _txn(ws, "delete_ref", {"ref_id": rb["ref_id"]}, "tu4")  # from the mirror side
                await asyncio.sleep(0.2)
                assert await _refs(ws, a) == []
                assert await _refs(ws, b) == []

    async def test_link_label_follows_the_linked_task(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                a = await _add(ws, "Old title", "tt1")
                b = await _add(ws, "Watcher", "tt2")
                await _add_ref(ws, b, "tt3", kind="task", relation="relates to", href=a)
                await asyncio.sleep(0.2)
                await _txn(ws, "edit", {"task_id": a, "title": "New title", "notes": "", "importance": 3,
                                        "urgency": 3, "due": "", "updated_at": NOW}, "tt4")
                [rb] = await _refs(ws, b)
                # edit_ref on a link ignores a label: it follows the linked task
                await _txn(ws, "edit_ref", {"ref_id": rb["ref_id"], "label": "Mine", "relation": "relates to",
                                            "updated_at": NOW}, "tt5")
                await asyncio.sleep(0.2)
                [rb] = await _refs(ws, b)
        assert rb["label"] == "New title"

    async def test_edit_ref_changes_a_links_relation_on_both_sides(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                a = await _add(ws, "Rel A", "tr1")
                b = await _add(ws, "Rel B", "tr2")
                await _add_ref(ws, a, "tr3", kind="task", relation="relates to", href=b)
                await asyncio.sleep(0.2)
                [ra] = await _refs(ws, a)
                await _txn(ws, "edit_ref", {"ref_id": ra["ref_id"], "relation": "blocked by", "updated_at": NOW}, "tr4")
                r = await _txn(ws, "edit_ref", {"ref_id": ra["ref_id"], "relation": "eats", "updated_at": NOW},
                               "tr5", expect="error")
                await asyncio.sleep(0.2)
                [ra] = await _refs(ws, a)
                [rb] = await _refs(ws, b)
        assert (ra["relation"], ra["href"], ra["label"]) == ("blocked by", b, "Rel B")
        assert (rb["relation"], rb["href"], rb["label"]) == ("blocks", a, "Rel A")
        assert ra["updated_at"] == rb["updated_at"] == NOW
        assert "relation" in r["message"]

    async def test_deleting_either_task_removes_the_link(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                a = await _add(ws, "Goes away", "td1")
                b = await _add(ws, "Stays", "td2")
                c = await _add(ws, "Stays too", "td3")
                await _add_ref(ws, a, "td4", kind="task", relation="blocks", href=b)
                await _add_ref(ws, c, "td5", kind="task", relation="relates to", href=a)
                await _add_ref(ws, b, "td6", kind="url", href="https://keep")
                await asyncio.sleep(0.2)
                await _txn(ws, "delete", {"task_id": a}, "td7")
                await asyncio.sleep(0.2)
                assert await _refs(ws, a) == []
                assert [r["kind"] for r in await _refs(ws, b)] == ["url"], "b's own reference survives"
                assert await _refs(ws, c) == []

    async def test_task_options_lists_open_tasks_but_not_self(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                a = await _add(ws, "Option self", "to1")
                b = await _add(ws, "Option other", "to2")
                c = await _add(ws, "Option complete", "to3")
                await _complete(ws, c, "to4")
                await asyncio.sleep(0.2)
                rows = await _request(ws, "task_options", {"task_id": a})
                got = {r["value"]: r["label"] for r in rows}
        assert a not in got and c not in got
        assert got[b] == f"{b}  Option other", "the picker labels a task by its title"


class TestRelations:
    async def test_seeded_on_first_start(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                await ws.send_json({"service": "all_relations", "type": "subscribe", "protocol": "query",
                                    "subid": "rs", "ref": "rs1"})
                snap = await _recv_json(ws)
                await ws.send_json({"service": "all_relations", "type": "unsubscribe", "subid": "rs"})
                options = await _request(ws, "relation_options", {})
        pairs = {(r["forward"], r["backward"]) for r in snap["rows"]}
        assert {(r["forward"], r["backward"]) for r in SEED} <= pairs
        assert all(r["relation_id"] > 0 for r in snap["rows"])
        values = [o["value"] for o in options]
        assert values == sorted(set(values)), "one entry per wording, a symmetric pair once"
        assert set(WORDINGS) <= set(values)
        assert all(o["label"] == o["value"] for o in options)

    async def test_add_edit_delete_relation(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                await _txn(ws, "add_relation", {"forward": " depends on ", "backward": "needed by"}, "ra1")
                await _txn(ws, "add_relation", {"forward": "twins with"}, "ra2")  # symmetric
                r = await _txn(ws, "add_relation", {"forward": "  "}, "ra3", expect="error")
                assert "wording" in r["message"]
                r = await _txn(ws, "add_relation", {"forward": "Needed By"}, "ra4", expect="error")
                assert "already" in r["message"], "unique across both columns, case-insensitively"
                r = await _txn(ws, "add_relation", {"forward": "x", "backward": "BLOCKS"}, "ra5", expect="error")
                assert "already" in r["message"]
                await asyncio.sleep(0.2)
                rows = {r["forward"]: r for r in await self._relations(ws)}
                assert rows["depends on"]["backward"] == "needed by"
                assert rows["twins with"]["backward"] == "twins with"
                twins = rows["twins with"]["relation_id"]
                await _txn(ws, "edit_relation", {"relation_id": twins, "forward": "twinned with", "backward": "",
                                                 "notes": "n", "updated_at": NOW}, "ra6")
                r = await _txn(ws, "edit_relation", {"relation_id": twins, "forward": "blocks", "backward": "",
                                                     "notes": "", "updated_at": NOW}, "ra7", expect="error")
                assert "already" in r["message"]
                await _txn(ws, "edit_relation", {"relation_id": twins, "forward": "twinned with", "backward": "",
                                                 "notes": "same", "updated_at": NOW}, "ra8")  # own wording is fine
                await asyncio.sleep(0.2)
                rows = {r["forward"]: r for r in await self._relations(ws)}
                assert "twins with" not in rows
                assert (rows["twinned with"]["backward"], rows["twinned with"]["notes"]) == ("twinned with", "same")
                await _txn(ws, "delete_relation", {"relation_id": twins}, "ra9")
                await _txn(ws, "delete_relation", {"relation_id": rows["depends on"]["relation_id"]}, "ra10")
                await asyncio.sleep(0.2)
                rows = {r["forward"] for r in await self._relations(ws)}
        assert not ({"twinned with", "depends on"} & rows)

    async def test_rename_rewrites_links_and_delete_is_refused_in_use(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                await _txn(ws, "add_relation", {"forward": "waits for", "backward": "awaited by"}, "rr1")
                await asyncio.sleep(0.2)
                rel = next(r for r in await self._relations(ws) if r["forward"] == "waits for")
                a = await _add(ws, "Waiter", "rr2")
                b = await _add(ws, "Awaited", "rr3")
                c = await _add(ws, "Awaited too", "rr4")
                await _add_ref(ws, a, "rr5", kind="task", relation="waits for", href=b)
                await _add_ref(ws, c, "rr6", kind="task", relation="awaited by", href=a)
                await asyncio.sleep(0.2)
                r = await _txn(ws, "delete_relation", {"relation_id": rel["relation_id"]}, "rr7", expect="error")
                assert "2 links use it" in r["message"]
                await _txn(ws, "edit_relation", {"relation_id": rel["relation_id"], "forward": "pends on",
                                                 "backward": "pended by", "notes": "", "updated_at": NOW}, "rr8")
                await asyncio.sleep(0.2)
                ra = {r["href"]: r["relation"] for r in await _refs(ws, a)}
                [rb] = await _refs(ws, b)
                [rc] = await _refs(ws, c)
                assert ra == {b: "pends on", c: "pends on"}
                assert rb["relation"] == "pended by" and rc["relation"] == "pended by"
                # fold to symmetric: both wordings become the forward one
                await _txn(ws, "edit_relation", {"relation_id": rel["relation_id"], "forward": "pends with",
                                                 "backward": "", "notes": "", "updated_at": NOW}, "rr9")
                await asyncio.sleep(0.2)
                assert {r["relation"] for r in await _refs(ws, a)} == {"pends with"}
                [rb] = await _refs(ws, b)
                assert rb["relation"] == "pends with"
                # delete_ref still finds the mirror after the rename
                await _txn(ws, "delete_ref", {"ref_id": rb["ref_id"]}, "rr10")
                [rc] = await _refs(ws, c)
                await _txn(ws, "delete_ref", {"ref_id": rc["ref_id"]}, "rr11")
                await asyncio.sleep(0.2)
                assert await _refs(ws, a) == [] and await _refs(ws, b) == [] and await _refs(ws, c) == []
                await _txn(ws, "delete_relation", {"relation_id": rel["relation_id"]}, "rr12")
                await asyncio.sleep(0.2)
                assert "pends with" not in {r["forward"] for r in await self._relations(ws)}

    async def test_swapping_a_relations_two_wordings_flips_its_links(self, server):
        """forward and backward trade places: every link must end up on the
        other side, not back where it started. The rewrite reads the ref_ids
        of both wordings before it writes either, so the two passes cannot
        chase each other."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                await _txn(ws, "add_relation", {"forward": "feeds", "backward": "fed by"}, "rx1")
                await asyncio.sleep(0.2)
                rel = next(r for r in await self._relations(ws) if r["forward"] == "feeds")
                a = await _add(ws, "Feeder", "rx2")
                b = await _add(ws, "Fed", "rx3")
                await _add_ref(ws, a, "rx4", kind="task", relation="feeds", href=b)
                await asyncio.sleep(0.2)
                await _txn(ws, "edit_relation", {"relation_id": rel["relation_id"], "forward": "fed by",
                                                 "backward": "feeds", "notes": "", "updated_at": NOW}, "rx5")
                await asyncio.sleep(0.2)
                [ra] = await _refs(ws, a)
                [rb] = await _refs(ws, b)
        assert ra["relation"] == "fed by", "the row that said 'feeds' now says 'fed by'"
        assert rb["relation"] == "feeds"

    async def test_a_symmetric_link_cannot_be_added_from_the_other_side(self, server):
        """Both halves of a symmetric link carry the same wording, so adding it
        the other way round is the mirror row, not a second link."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                a = await _add(ws, "Sym A", "ry1")
                b = await _add(ws, "Sym B", "ry2")
                await _add_ref(ws, a, "ry3", kind="task", relation="relates to", href=b)
                await asyncio.sleep(0.2)
                r = await _add_ref(ws, b, "ry4", expect="error", kind="task", relation="relates to", href=a)
                assert "already" in r["message"]
                await asyncio.sleep(0.2)
                assert len(await _refs(ws, a)) == 1 and len(await _refs(ws, b)) == 1

    async def test_link_needs_a_known_wording(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                a = await _add(ws, "Word A", "rw1")
                b = await _add(ws, "Word B", "rw2")
                for bad in ("", "eats", "Blocks"):  # wordings are exact
                    r = await _add_ref(ws, a, f"rw-{bad}", expect="error", kind="task", relation=bad, href=b)
                    assert "relation" in r["message"], bad
                assert await _refs(ws, a) == []

    @staticmethod
    async def _relations(ws):
        await ws.send_json({"service": "all_relations", "type": "subscribe", "protocol": "query",
                            "subid": "rl", "ref": "rl"})
        snap = await _recv_json(ws)
        await ws.send_json({"service": "all_relations", "type": "unsubscribe", "subid": "rl"})
        return snap["rows"]


class TestFiles:
    def test_upload_and_cleanup(self, tmp_path):
        files = tmp_path / "refs"
        proc, base = _start("--user", "mark", "--files", str(files))
        try:
            asyncio.run(self._upload_and_cleanup(base, files))
        finally:
            _stop(proc)

    async def _upload_and_cleanup(self, base, files):
        png = b"\x89PNG\r\n\x1a\n" + b"x" * 100
        async with aiohttp.ClientSession() as s:
            status, up = await _upload(s, base, png, "image/png")
            assert status == 200
            digest = __import__("hashlib").sha256(png).hexdigest()
            assert up == {"href": f"/files/{digest}.png", "mime": "image/png", "size": len(png)}
            assert (files / f"{digest}.png").read_bytes() == png
            status, again = await _upload(s, base, png, "image/png")
            assert again == up, "content-addressed: the same bytes are one file"
            async with s.get(base + up["href"]) as resp:
                assert resp.status == 200
                assert resp.headers["Content-Type"].startswith("image/png")
                assert await resp.read() == png
            status, body = await _upload(s, base, b"", "image/png")
            assert status == 400
            status, body = await _upload(s, base, b"x" * (20 * 1024 * 1024 + 1), "application/octet-stream")
            assert status == 413
            status, bin_ = await _upload(s, base, b"?", "application/x-unknown-thing")
            assert bin_["href"].endswith(".bin")
            status, txt = await _upload(s, base, b"hello", "text/plain; charset=utf-8")
            assert txt["href"].endswith(".txt") and txt["mime"] == "text/plain"

            async with s.ws_connect(base + "/ws") as ws:
                a = await _add(ws, "File A", "fa")
                b = await _add(ws, "File B", "fb")
                await _add_ref(ws, a, "f1", kind="file", href=up["href"], mime=up["mime"])
                await _add_ref(ws, b, "f2", kind="file", href=up["href"], mime=up["mime"])
                await _add_ref(ws, b, "f3", kind="file", href=txt["href"], mime=txt["mime"])
                await asyncio.sleep(0.2)
                [ra] = await _refs(ws, a)
                await _txn(ws, "delete_ref", {"ref_id": ra["ref_id"]}, "f4")
                await asyncio.sleep(0.2)
                assert (files / f"{digest}.png").exists(), "b still refers to it"
                await _txn(ws, "delete", {"task_id": b}, "f5")
                await asyncio.sleep(0.3)
                assert not (files / f"{digest}.png").exists(), "the last reference went with task b"
                assert not (files / txt["href"].rsplit("/", 1)[1]).exists()
                assert (files / bin_["href"].rsplit("/", 1)[1]).exists(), "never referenced, never touched"


class TestSavedLayouts:
    """The Layout menu's services, copied from mkui's scaffold, must round-trip."""

    async def test_save_list_get_delete(self, server):
        layout = json.dumps({"frames": [{"id": "main"}]})
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                await ws.send_json({"service": "mkui_layouts", "type": "transaction",
                                    "op": "save", "ref": "l1",
                                    "data": {"app": "mktask", "owner": "", "layout": layout}})
                assert (await _recv_json(ws))["type"] == "result"

                await ws.send_json({"service": "mkui_layouts_list", "type": "request",
                                    "ref": "l2", "data": {"app": "mktask", "owner": ""}})
                rows = (await _recv_json(ws))["rows"]
                assert len(rows) == 1
                layout_id = rows[0]["id"]

                await ws.send_json({"service": "mkui_layouts_get", "type": "request",
                                    "ref": "l3", "data": {"id": layout_id}})
                got = (await _recv_json(ws))["rows"]
                assert len(got) == 1 and got[0]["layout"] == layout

                await ws.send_json({"service": "mkui_layouts_list", "type": "request",
                                    "ref": "l4", "data": {"app": "other", "owner": ""}})
                assert (await _recv_json(ws))["rows"] == []

                await ws.send_json({"service": "mkui_layouts", "type": "transaction",
                                    "op": "delete", "ref": "l5", "data": {"id": layout_id}})
                assert (await _recv_json(ws))["type"] == "result"

                await ws.send_json({"service": "mkui_layouts_list", "type": "request",
                                    "ref": "l6", "data": {"app": "mktask", "owner": ""}})
                assert (await _recv_json(ws))["rows"] == []


class TestCliFlags:
    def test_host_flag_binds_only_that_host(self):
        """--host 127.0.0.1 must not answer on other loopback addresses."""
        port = _free_port()
        proc = subprocess.Popen(
            [sys.executable, "-m", "mktask", "-d", ":memory:", "-p", str(port), "--host", "127.0.0.1"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("server did not start")
            with pytest.raises(OSError):
                socket.create_connection(("127.0.0.2", port), timeout=0.5)
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_banner_names_the_url(self):
        port = _free_port()
        proc = subprocess.Popen(
            [sys.executable, "-m", "mktask", "-d", ":memory:", "-p", str(port)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            deadline = time.time() + 10
            lines = []
            while time.time() < deadline and not any("Press Ctrl+C" in l for l in lines):
                lines.append(proc.stdout.readline())
            out = "".join(lines)
        finally:
            proc.terminate()
            proc.wait(timeout=5)
        assert f"mktask {__version__}" in out
        assert f"http://127.0.0.1:{port}/" in out
        assert "in-memory" in out
        assert re.search(r"Task IDs:  TK[A-Z0-9]{2}nnnnnnnn", out)

    def test_port_in_use_fails_cleanly(self):
        with socket.socket() as blocker:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            port = blocker.getsockname()[1]
            result = subprocess.run(
                [sys.executable, "-m", "mktask", "-d", ":memory:", "-p", str(port)],
                capture_output=True, text=True, timeout=20,
            )
        assert result.returncode == 1
        assert f"cannot listen on 127.0.0.1:{port}" in result.stderr
