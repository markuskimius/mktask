"""Integration tests: the full mktask server over HTTP and WebSocket."""

import asyncio
import itertools
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


_REF_SEQ = itertools.count(1)


async def _txn(ws, op, data, ref, expect="result"):
    """Send one transaction. `ref` is a label, not an identity.

    mkio stamps every row a transaction writes with its ref, and
    `undo_action` groups a user action by exactly that — so two tests
    reusing a label on one server would have their rows grouped together
    and undone as one action. A real client mints a fresh ref per
    transaction; making these unique is what models that.
    """
    ref = f"{ref}-{next(_REF_SEQ)}"
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
                "all_assignees", "assignee_options", "task_assignee_options",
                "task_options", "move_options", "ref_owner_options",
                "mkui_layouts", "mkui_layouts_list", "mkui_layouts_get"} <= names


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
                                        "due": "", "assigned_to": "",
                                        "updated_at": "2026-09-05 00:00:00"}, "r3")
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
                                        "urgency": 1, "due": "", "assigned_to": "",
                                        "updated_at": NOW}, "E4")
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


class TestMove:
    """A move re-parents one task; everything split from it comes along."""

    async def _move(self, ws, task_id, parent, ref, expect="result"):
        return await _txn(ws, "move", {"task_id": task_id, "parent_task_id": parent,
                                       "updated_at": NOW}, ref, expect=expect)

    async def _parents(self, ws, subid):
        return {t: r["parent_task_id"] for t, r in (await _snapshot(ws, subid)).items()}

    async def test_move_reparents_and_promotes(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                root = await _add(ws, "MV-root", "mv1")
                await _split(ws, root, "MV-child", "mv2")
                await asyncio.sleep(0.2)
                child = (await _snapshot(ws, "mv-s1"))["MV-child"]["task_id"]
                await _split(ws, child, "MV-grandchild", "mv3")
                host = await _add(ws, "MV-host", "mv4")
                await asyncio.sleep(0.2)

                await self._move(ws, child, host, "mv5")
                await asyncio.sleep(0.2)
                moved = await self._parents(ws, "mv-s2")

                await self._move(ws, child, "__top__", "mv6")  # what the picker sends
                await asyncio.sleep(0.2)
                promoted = await self._parents(ws, "mv-s3")
        assert moved["MV-child"] == host
        assert moved["MV-grandchild"] == child, "the subtree comes along"
        assert moved["MV-root"] == ""
        assert promoted["MV-child"] == "", "the picker's sentinel means the top level"
        assert promoted["MV-grandchild"] == child

    async def test_an_empty_parent_promotes_too(self, server):
        """The picker sends `__top__` because mkui's select cannot tell an
        unmade choice from an empty value; the column's own '' still works."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                root = await _add(ws, "MP-root", "mp1")
                await _split(ws, root, "MP-child", "mp2")
                await asyncio.sleep(0.2)
                child = (await _snapshot(ws, "mp-s1"))["MP-child"]["task_id"]
                await self._move(ws, child, "", "mp3")
                await asyncio.sleep(0.2)
                parents = await self._parents(ws, "mp-s2")
        assert parents["MP-child"] == ""

    async def test_move_refuses_a_cycle_or_an_unknown_parent(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                root = await _add(ws, "MC-root", "mc1")
                await _split(ws, root, "MC-child", "mc2")
                await asyncio.sleep(0.2)
                child = (await _snapshot(ws, "mc-s1"))["MC-child"]["task_id"]

                r = await self._move(ws, root, root, "mc3", expect="error")
                assert "itself" in r["message"]
                r = await self._move(ws, root, child, "mc4", expect="error")
                assert "was split from" in r["message"]
                r = await self._move(ws, root, "TKMA99999997", "mc5", expect="error")
                assert "no task" in r["message"]
                r = await self._move(ws, "TKMA99999996", "", "mc6", expect="error")
                assert "no task" in r["message"]
                await asyncio.sleep(0.2)
                parents = await self._parents(ws, "mc-s2")
        assert parents["MC-root"] == "" and parents["MC-child"] == root, "a refused move writes nothing"

    async def test_move_refuses_to_swallow_a_task_link(self, server):
        """`add_ref` forbids a link inside one tree, so a move that would
        create one is refused rather than silently dropping the link."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                root = await _add(ws, "ML-root", "ml1")
                await _split(ws, root, "ML-child", "ml2")
                other = await _add(ws, "ML-other", "ml3")
                await asyncio.sleep(0.2)
                child = (await _snapshot(ws, "ml-s1"))["ML-child"]["task_id"]
                await _add_ref(ws, child, "ml4", kind="task", relation="blocks", href=other)
                await asyncio.sleep(0.2)

                r = await self._move(ws, root, other, "ml5", expect="error")
                assert "blocks" in r["message"] and "(1 link)" in r["message"]
                parents = await self._parents(ws, "ml-s2")
                assert parents["ML-root"] == ""

                [link] = [x for x in await _refs(ws, child) if x["kind"] == "task"]
                await _txn(ws, "delete_ref", {"ref_id": link["ref_id"]}, "ml6")
                await asyncio.sleep(0.2)
                await self._move(ws, root, other, "ml7")
                await asyncio.sleep(0.2)
                parents = await self._parents(ws, "ml-s3")
        assert parents["ML-root"] == other, "the move goes through once the link is gone"

    async def test_move_reopens_a_complete_new_parent(self, server):
        """Open work may not hang under a complete parent: the new ancestors reopen."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                host = await _add(ws, "MR-host", "mr1")
                await _split(ws, host, "MR-inner", "mr2")
                await asyncio.sleep(0.2)
                inner = (await _snapshot(ws, "mr-s1"))["MR-inner"]["task_id"]
                loose = await _add(ws, "MR-loose", "mr3")
                await _complete(ws, host, "mr4")
                await asyncio.sleep(0.2)
                assert (await _snapshot(ws, "mr-s2"))["MR-host"]["status"] == "complete"

                await self._move(ws, loose, inner, "mr5")
                await asyncio.sleep(0.2)
                rows = await _snapshot(ws, "mr-s3")
        assert rows["MR-loose"]["status"] == "open"
        assert rows["MR-inner"]["status"] == "open", "the new parent reopens"
        assert rows["MR-host"]["status"] == "open", "and so does every ancestor of it"
        assert rows["MR-host"]["completed_at"] == ""

    async def test_a_move_reaches_a_filtered_subscriber(self, server):
        """The blotter re-nests from the live update alone: a query filtered
        on the new parent must hear the moved row arrive, and the old
        parent's must hear it leave."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                old_parent = await _add(ws, "MF-old", "mf1")
                new_parent = await _add(ws, "MF-new", "mf2")
                await _split(ws, old_parent, "MF-mover", "mf3")
                await asyncio.sleep(0.2)
                mover = (await _snapshot(ws, "mf-s1"))["MF-mover"]["task_id"]

                for subid, parent in (("mf-old", old_parent), ("mf-new", new_parent)):
                    await ws.send_json({"service": "all_tasks", "type": "subscribe",
                                        "protocol": "query", "subid": subid, "ref": subid,
                                        "filter": f"parent_task_id == '{parent}'"})
                    await _recv_json(ws)  # the opening snapshot
                # The announcements share the socket with the reply, so read
                # whatever arrives until both queries have spoken.
                await ws.send_json({"service": "tasks", "type": "transaction", "op": "move",
                                    "data": {"task_id": mover, "parent_task_id": new_parent,
                                             "updated_at": NOW}, "ref": "mf4"})
                seen, replied = {}, False
                deadline = time.time() + 5
                while (len(seen) < 2 or not replied) and time.time() < deadline:
                    msg = await _recv_json(ws)
                    if msg.get("type") == "update":
                        seen[msg["subid"]] = msg
                    elif msg.get("type") == "result":
                        replied = True
                for subid in ("mf-old", "mf-new"):
                    await ws.send_json({"service": "all_tasks", "type": "unsubscribe", "subid": subid})
        assert replied, "the move itself is answered"
        assert set(seen) == {"mf-old", "mf-new"}, seen
        assert seen["mf-new"]["op"] in ("insert", "update")
        assert seen["mf-new"]["row"]["task_id"] == mover
        assert seen["mf-new"]["row"]["parent_task_id"] == new_parent
        assert seen["mf-old"]["op"] == "delete", "the row leaves the old parent's query"

    async def test_move_options_offer_the_top_level_and_open_outsiders(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                root = await _add(ws, "MO-root", "mo1")
                await _split(ws, root, "MO-child", "mo2")
                outside = await _add(ws, "MO-outside", "mo3")
                finished = await _add(ws, "MO-finished", "mo4")
                await _complete(ws, finished, "mo5")
                await asyncio.sleep(0.2)
                child = (await _snapshot(ws, "mo-s1"))["MO-child"]["task_id"]
                rows = await _request(ws, "move_options", {"task_id": root})
        assert rows[0] == {"value": "__top__", "label": "— Top level —"}, "the top level comes first"
        values = {r["value"] for r in rows}
        assert outside in values
        assert root not in values and child not in values, "never its own subtree"
        assert finished not in values, "a complete task is not offered as a parent"
        assert next(r["label"] for r in rows if r["value"] == outside).endswith("MO-outside")


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


class TestSubtreeReferences:
    """What the Detail pane's own queries need from the server: a filter that
    names a whole subtree, and the parent/child fields the subtree is computed
    from. The widget lives in the browser; these contracts are tested here.
    """

    async def _tree(self, ws, tag):
        root = await _add(ws, f"{tag}-root", f"{tag}1")
        await _split(ws, root, f"{tag}-child", f"{tag}2")
        await asyncio.sleep(0.2)
        child = (await _snapshot(ws, f"{tag}-s1"))[f"{tag}-child"]["task_id"]
        await _split(ws, child, f"{tag}-grandchild", f"{tag}3")
        await asyncio.sleep(0.2)
        grandchild = (await _snapshot(ws, f"{tag}-s2"))[f"{tag}-grandchild"]["task_id"]
        return root, child, grandchild

    @staticmethod
    async def _txn_updates(ws, op, data, ref):
        """A transaction on a socket that also holds a subscription: the live
        updates land before the result."""
        await ws.send_json({"service": "tasks", "type": "transaction", "op": op,
                            "data": data, "ref": ref})
        updates = []
        while True:
            msg = await _recv_json(ws)
            if msg["type"] == "result":
                return updates
            assert msg["type"] == "update", msg
            updates.append(msg)

    async def test_a_subtree_filter_names_every_task_in_it(self, server):
        """`CONTAINS([...], task_id)` is what the Detail pane subscribes with
        once a task has children: `filterable` gates whether a filter is
        honoured, not which expression may name the column."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                root, child, grandchild = await self._tree(ws, "SR")
                outside = await _add(ws, "SR-outside", "SR4")
                await _add_ref(ws, root, "SR5", kind="url", href="https://root")
                await _add_ref(ws, grandchild, "SR6", kind="text", body="grandchild")
                await _add_ref(ws, outside, "SR7", kind="url", href="https://outside")
                await asyncio.sleep(0.2)
                outside_ref = (await _refs(ws, outside))[0]["ref_id"]

                ids = ", ".join(f"'{t}'" for t in (root, child, grandchild))
                await ws.send_json({"service": "task_refs", "type": "subscribe", "protocol": "query",
                                    "subid": "sr", "ref": "SR8",
                                    "filter": f"CONTAINS([{ids}], task_id)"})
                snap = await _recv_json(ws)

                # A reference added anywhere in the subtree arrives live...
                added = await self._txn_updates(
                    ws, "add_ref", {"task_id": child, "kind": "url", "href": "https://child"}, "SR9")
                assert [(u["op"], u["row"]["task_id"]) for u in added] == [("insert", child)]
                ref_id = added[0]["row"]["ref_id"]

                # ...and its delete does too, announced with the request's data
                # (a ref_id, no task_id — mkio replays what it sent, so a
                # filtered subscription still hears the row leave).
                removed = await self._txn_updates(ws, "delete_ref", {"ref_id": ref_id}, "SR10")
                assert [(u["op"], u["row"]["ref_id"]) for u in removed] == [("delete", ref_id)]

                # A reference outside the subtree is silent in both directions.
                assert await self._txn_updates(
                    ws, "add_ref", {"task_id": outside, "kind": "text", "body": "quiet"}, "SR11") == []
                assert await self._txn_updates(ws, "delete_ref", {"ref_id": outside_ref}, "SR12") == []

                await ws.send_json({"service": "task_refs", "type": "unsubscribe", "subid": "sr"})
        assert sorted(r["task_id"] for r in snap["rows"]) == sorted([root, grandchild])

    async def test_all_tasks_projects_the_fields_the_subtree_needs(self, server):
        """The widget subscribes to `all_tasks` with `fields`: three columns
        are all a tree needs, and the rows come back with exactly those."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                root, child, _ = await self._tree(ws, "SF")
                await ws.send_json({"service": "all_tasks", "type": "subscribe", "protocol": "query",
                                    "subid": "sf", "ref": "SF4",
                                    "fields": ["task_id", "parent_task_id", "title"]})
                snap = await _recv_json(ws)
                await ws.send_json({"service": "all_tasks", "type": "unsubscribe", "subid": "sf"})
        rows = {r["title"]: r for r in snap["rows"]}
        assert rows["SF-child"]["parent_task_id"] == root
        assert rows["SF-grandchild"]["parent_task_id"] == child
        assert rows["SF-root"]["parent_task_id"] == ""
        for row in snap["rows"]:
            assert {k for k in row if not k.startswith("_mkio_")} == {"task_id", "parent_task_id", "title"}


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
                                        "urgency": 3, "due": "", "assigned_to": "",
                                        "updated_at": NOW}, "tt4")
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

    async def test_ref_owner_options_lists_every_task_open_first(self, server):
        """The Task picker of the URL / Text / Link dialogs: every task, so a
        reference can be added to any of them from the References pane, open
        tasks first and each labelled by its title. No params — a param that
        resolves to '' would empty the list, and the picker exists for the case
        where nothing is selected."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                a = await _add(ws, "Owner beta", "ro1")
                b = await _add(ws, "Owner alpha", "ro2")
                c = await _add(ws, "Owner complete", "ro3")
                await _complete(ws, c, "ro4")
                await asyncio.sleep(0.2)
                rows = await _request(ws, "ref_owner_options", {})
        values = [r["value"] for r in rows]
        assert values.index(b) < values.index(a) < values.index(c), "open first, then by title"
        labels = {r["value"]: r["label"] for r in rows}
        assert labels[a] == f"{a}  Owner beta" and labels[c] == f"{c}  Owner complete"


class TestAssignees:
    """The Assigned To dropdown: a list of its own, grown by the picker, and
    deliberately unable to reach back into the tasks that carry a name."""

    async def _list(self, ws, subid):
        await ws.send_json({"service": "all_assignees", "type": "subscribe", "protocol": "query",
                            "subid": subid, "ref": f"as-{subid}"})
        snap = await _recv_json(ws)
        await ws.send_json({"service": "all_assignees", "type": "unsubscribe", "subid": subid})
        return {r["name"]: r for r in snap["rows"]}

    async def _task(self, ws, task_id, subid):
        rows = await _snapshot(ws, subid)
        return next(r for r in rows.values() if r["task_id"] == task_id)

    async def test_a_name_typed_on_a_task_joins_the_list(self, server):
        """The picker is the list's front door: a name it does not hold is
        added in the same transaction as the task it was typed on."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                await _txn(ws, "add", {"title": "Typed a name", "assigned_to": " Ada Lovelace "}, "an1")
                await asyncio.sleep(0.3)
                rows = await _snapshot(ws, "an2")
                listed = await self._list(ws, "an3")
                options = await _request(ws, "assignee_options", {})
        assert rows["Typed a name"]["assigned_to"] == "Ada Lovelace", "trimmed, and kept on the task"
        assert "Ada Lovelace" in listed, "and offered from then on"
        values = [o["value"] for o in options]
        assert values[0] == "__new__", "the one sentinel leads"
        assert values[1:] == sorted(values[1:]), "then the names, alphabetically"
        assert "__none__" not in values, "unassigned is mkui's own blank entry, not a row"
        assert "Ada Lovelace" in values
        by_value = {o["value"]: o["label"] for o in options}
        assert by_value["Ada Lovelace"] == "Ada Lovelace"
        assert "New name" in by_value["__new__"]

    async def test_a_dialog_on_a_task_offers_the_name_that_task_carries(self, server):
        """A name off the list would leave the Edit picker blank on the very
        task that still carries it — so that task's own name joins its
        options, once, whether or not the list still holds it."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                task = await _add(ws, "Carries a retired name", "ao1")
                await _edit(ws, task, "ao2", title="Carries a retired name",
                            assigned_to="Gone Away")
                await asyncio.sleep(0.3)
                listed = await self._list(ws, "ao3")
                scoped = await _request(ws, "task_assignee_options", {"task_id": task})
                assert [o["value"] for o in scoped].count("Gone Away") == 1, "listed once, not twice"
                await _txn(ws, "delete_assignee",
                           {"assignee_id": listed["Gone Away"]["assignee_id"]}, "ao4")
                await asyncio.sleep(0.3)
                plain = await _request(ws, "assignee_options", {})
                scoped = await _request(ws, "task_assignee_options", {"task_id": task})
                other = await _request(ws, "task_assignee_options",
                                       {"task_id": await _add(ws, "Carries nobody", "ao5")})
        assert "Gone Away" not in {o["value"] for o in plain}, "off the list everywhere else"
        assert "Gone Away" in {o["value"] for o in scoped}, "still offered where it is in use"
        assert "Gone Away" not in {o["value"] for o in other}
        assert [o["value"] for o in scoped][0] == "__new__"

    async def test_a_name_rides_the_task_it_was_typed_on(self, server):
        """`add_assignee` is a step in the same transaction as the task, so
        a task that never lands takes the name down with it. A name that is
        only whitespace is no name at all and joins nothing."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                r = await _txn(ws, "add", {"assigned_to": "Never Landed"}, "aa1", expect="error")
                assert r["message"], "a task with no title cannot be written"
                await _txn(ws, "add", {"title": "Blank name", "assigned_to": "   "}, "aa2")
                await asyncio.sleep(0.3)
                rows = await _snapshot(ws, "aa3")
                listed = await self._list(ws, "aa4")
        assert "Never Landed" not in listed, "the name was rolled back with the task"
        assert rows["Blank name"]["assigned_to"] == ""
        assert not [n for n in listed if not n.strip()], "whitespace is not a name"

    async def test_a_listed_name_wins_on_spelling(self, server):
        """Typing "grace" when the list holds "Grace Hopper"'s twin is the
        same person: one entry, spelled the way the list spells it."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                await _txn(ws, "add_assignee", {"name": "Grace", "notes": ""}, "as1")
                await asyncio.sleep(0.2)
                await _txn(ws, "add", {"title": "Typed it in lowercase", "assigned_to": "grace"}, "as2")
                await asyncio.sleep(0.3)
                rows = await _snapshot(ws, "as3")
                listed = await self._list(ws, "as4")
        assert rows["Typed it in lowercase"]["assigned_to"] == "Grace"
        assert [n for n in listed if n.lower() == "grace"] == ["Grace"], "no second entry reading the same"

    async def test_the_pickers_sentinel_never_reaches_the_column(self, server):
        """The dialog maps "__new__" to the name typed beside it before it
        submits; the service maps it again, so it cannot be stored as one.
        Unassigned needs no sentinel — mkui's blank entry submits as ''."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                await _txn(ws, "add", {"title": "Sentinel new", "assigned_to": "__new__"}, "sn1")
                await _txn(ws, "add", {"title": "Picked the blank", "assigned_to": ""}, "sn2")
                await asyncio.sleep(0.3)
                rows = await _snapshot(ws, "sn3")
                listed = await self._list(ws, "sn4")
        assert rows["Sentinel new"]["assigned_to"] == ""
        assert rows["Picked the blank"]["assigned_to"] == "", "the blank means nobody"
        assert "__new__" not in listed

    async def test_the_blank_clears_an_assignment(self, server):
        """What the blank means on an edit: nobody. It is the only way to
        take a name off a task, and the picker lands on it by itself when
        the task has no name to preselect."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                task = await _add(ws, "Assigned then not", "ab1")
                await _edit(ws, task, "ab2", title="Assigned then not", assigned_to="Briefly Owned")
                await asyncio.sleep(0.3)
                assert (await self._task(ws, task, "ab3"))["assigned_to"] == "Briefly Owned"
                await _edit(ws, task, "ab4", title="Assigned then not", assigned_to="")
                await asyncio.sleep(0.3)
                cleared = await self._task(ws, task, "ab5")
                listed = await self._list(ws, "ab6")
        assert cleared["assigned_to"] == ""
        assert "Briefly Owned" in listed, "clearing a task does not take the name off the list"

    async def test_split_and_edit_carry_the_assignment(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                parent = await _add(ws, "Assign parent", "ac1")
                await _split(ws, parent, "Assign child", "ac2", assigned_to="Kay")
                await _edit(ws, parent, "ac3", title="Assign parent", assigned_to="Kay")
                await asyncio.sleep(0.3)
                rows = await _snapshot(ws, "ac4")
                listed = await self._list(ws, "ac5")
        assert rows["Assign child"]["assigned_to"] == "Kay"
        assert rows["Assign parent"]["assigned_to"] == "Kay"
        assert len([n for n in listed if n == "Kay"]) == 1, "the second task found the name already there"

    async def test_a_deleted_name_stays_on_its_tasks_and_off_the_list(self, server):
        """The whole point of storing the name rather than a key: taking a
        name off the dropdown is a change to the dropdown, nothing else.
        An unrelated edit of a task carrying a retired name must not put
        the name back on the list either."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                task = await _add(ws, "Left behind", "ad1")
                await _edit(ws, task, "ad2", title="Left behind", assigned_to="Retiree")
                await asyncio.sleep(0.3)
                listed = await self._list(ws, "ad3")
                await _txn(ws, "delete_assignee",
                           {"assignee_id": listed["Retiree"]["assignee_id"]}, "ad4")
                await asyncio.sleep(0.3)
                gone = await self._list(ws, "ad5")
                still = await self._task(ws, task, "ad6")
                assert "Retiree" not in gone, "off the dropdown"
                assert still["assigned_to"] == "Retiree", "and still on the task"

                # A later edit of that task sends the name back unchanged.
                await _edit(ws, task, "ad7", title="Left behind, edited", assigned_to="Retiree")
                await asyncio.sleep(0.3)
                after = await self._list(ws, "ad8")
                kept = await self._task(ws, task, "ad9")
        assert "Retiree" not in after, "an unchanged name does not rejoin the list"
        assert kept["assigned_to"] == "Retiree" and kept["title"] == "Left behind, edited"

    async def test_splitting_a_task_inherits_its_name_without_reviving_it(self, server):
        """The Split picker opens on the parent's name, so accepting it is
        inheriting, not typing: a retired name goes to the child and stays
        off the list. Typing a different one still puts that one on."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                parent = await _add(ws, "Retired parent", "ai1")
                await _edit(ws, parent, "ai2", title="Retired parent", assigned_to="Ex Owner")
                await asyncio.sleep(0.3)
                listed = await self._list(ws, "ai3")
                await _txn(ws, "delete_assignee",
                           {"assignee_id": listed["Ex Owner"]["assignee_id"]}, "ai4")
                await asyncio.sleep(0.3)
                await _split(ws, parent, "Inherits it", "ai5", assigned_to="Ex Owner")
                await _split(ws, parent, "Gets someone else", "ai6", assigned_to="Fresh Owner")
                await asyncio.sleep(0.3)
                rows = await _snapshot(ws, "ai7")
                after = await self._list(ws, "ai8")
        assert rows["Inherits it"]["assigned_to"] == "Ex Owner", "the child takes the name"
        assert "Ex Owner" not in after, "and inheriting does not put it back on the list"
        assert rows["Gets someone else"]["assigned_to"] == "Fresh Owner"
        assert "Fresh Owner" in after, "a name the split actually introduces does join"

    async def test_a_rename_changes_the_list_and_no_task(self, server):
        """Unlike a relation, whose wording a link is looked up by, a name
        is only ever displayed — so a rename leaves every task alone."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                task = await _add(ws, "Renamed away", "ar1")
                await _edit(ws, task, "ar2", title="Renamed away", assigned_to="Mispelt")
                await asyncio.sleep(0.3)
                listed = await self._list(ws, "ar3")
                await _txn(ws, "edit_assignee", {"assignee_id": listed["Mispelt"]["assignee_id"],
                                                 "name": " Misspelt ", "notes": "fixed",
                                                 "updated_at": NOW}, "ar4")
                await asyncio.sleep(0.3)
                after = await self._list(ws, "ar5")
                kept = await self._task(ws, task, "ar6")
        assert "Mispelt" not in after and after["Misspelt"]["notes"] == "fixed"
        assert kept["assigned_to"] == "Mispelt", "the task keeps the name it was given"

    async def test_the_list_refuses_a_blank_or_repeated_name(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                await _txn(ws, "add_assignee", {"name": "Unique One", "notes": ""}, "au1")
                await asyncio.sleep(0.2)
                r = await _txn(ws, "add_assignee", {"name": "  ", "notes": ""}, "au2", expect="error")
                assert "name" in r["message"]
                r = await _txn(ws, "add_assignee", {"name": "UNIQUE ONE", "notes": ""}, "au3",
                               expect="error")
                assert "already on the list" in r["message"]
                listed = await self._list(ws, "au4")
                one = listed["Unique One"]["assignee_id"]
                # its own name is not a clash
                await _txn(ws, "edit_assignee", {"assignee_id": one, "name": "Unique One",
                                                 "notes": "same", "updated_at": NOW}, "au5")
                r = await _txn(ws, "edit_assignee", {"assignee_id": 99999, "name": "Nobody",
                                                     "notes": "", "updated_at": NOW}, "au6",
                               expect="error")
                assert "No assignee" in r["message"]
                await asyncio.sleep(0.2)
                after = await self._list(ws, "au7")
        assert after["Unique One"]["notes"] == "same"
        assert len([n for n in after if n.lower() == "unique one"]) == 1

    async def test_an_assignment_steps_back_with_the_task(self, server):
        """`assigned_to` is a column of a versioned table, so it rides in
        every recorded version and undo puts back what was there before.
        The list itself is not versioned and does not step: the name stays
        on offer, which is what makes the redo worth having."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                task = await _add(ws, "Reassigned", "av1")
                await _edit(ws, task, "av2", title="Reassigned", assigned_to="First Owner")
                await asyncio.sleep(0.3)
                await _edit(ws, task, "av3", title="Reassigned", assigned_to="Second Owner")
                await asyncio.sleep(0.3)
                assert (await self._task(ws, task, "av4"))["assigned_to"] == "Second Owner"
                await _undo(ws, {"task_id": task}, "av5")
                await asyncio.sleep(0.4)
                back = await self._task(ws, task, "av6")
                listed = await self._list(ws, "av7")
                await _redo(ws, {"task_id": task}, "av8")
                await asyncio.sleep(0.4)
                forward = await self._task(ws, task, "av9")
                chain = await _chain(ws, "task_version_chain", {"task_id": task})
        assert back["assigned_to"] == "First Owner"
        assert forward["assigned_to"] == "Second Owner"
        assert {"First Owner", "Second Owner"} <= set(listed), "the list does not step back"
        # Every version carries the column, which is what History diffs on.
        assert [c["assigned_to"] for c in chain] == ["", "First Owner", "Second Owner"]


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


# ─── Recorded versions, undo and redo ───────────────────────────────
#
# mkio keeps every version of a `versioned = true` row in <table>__history and
# its undo/redo ops step the live row's cursor along that chain. What mktask
# adds is the grouping — one user action is rarely one row — the guards, and
# the narrative in task_events. These exercise mktask's part of it, through
# the services and ops the UI actually uses.

async def _chain(ws, service, key):
    """One record's recorded versions, oldest first."""
    return await _request(ws, service, key)


async def _version(ws, task_id):
    """Where a task's cursor sits, and how high its chain goes."""
    row = (await _request(ws, "task_version_state", {"task_id": task_id}))[0]
    return row["current"], row["top"]


async def _activity(ws, task_id):
    """A task's events, oldest first, through the live query the pane uses."""
    subid = f"a-{task_id}"
    await ws.send_json({"service": "task_activity", "type": "subscribe", "protocol": "query",
                        "subid": subid, "ref": subid, "filter": f"task_id == '{task_id}'"})
    snap = await _recv_json(ws)
    await ws.send_json({"service": "task_activity", "type": "unsubscribe", "subid": subid})
    return sorted(snap["rows"], key=lambda r: r["event_id"])


async def _actions(ws, task_id):
    return [r["action"] for r in await _activity(ws, task_id)]


async def _undo(ws, key, ref, expect="result"):
    return await _txn(ws, "undo_action", key, ref, expect=expect)


async def _redo(ws, key, ref, expect="result"):
    return await _txn(ws, "redo_action", key, ref, expect=expect)


async def _edit(ws, task_id, ref, **fields):
    """`edit` declares no defaults, so every field is required: fill them in."""
    full = {"title": "", "notes": "", "importance": 3, "urgency": 3, "due": "", "assigned_to": ""}
    return await _txn(ws, "edit", {"task_id": task_id, "updated_at": NOW, **full, **fields}, ref)


class TestVersions:
    async def test_every_write_records_a_version(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                t = await _add(ws, "Versioned", "v1")
                assert await _version(ws, t) == (1, 1)
                await _edit(ws, t, "v2", title="Versioned twice")
                await asyncio.sleep(0.2)
                assert await _version(ws, t) == (2, 2)
                chain = await _chain(ws, "task_version_chain", {"task_id": t})
                assert [r["_mkio_version"] for r in chain] == [1, 2]
                assert [r["title"] for r in chain] == ["Versioned", "Versioned twice"]
                # mkio stamps the op that wrote each version, and the user.
                assert [r["_mkio_op"] for r in chain] == ["insert", "update"]

    async def test_undo_and_redo_step_the_cursor(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                t = await _add(ws, "Steppable", "u1")
                await _edit(ws, t, "u2", title="Stepped once", importance=5)
                await asyncio.sleep(0.2)
                await _undo(ws, {"task_id": t}, "u3")
                await asyncio.sleep(0.2)
                rows = await _snapshot(ws, "su1")
                assert "Steppable" in rows and rows["Steppable"]["importance"] == 3
                assert await _version(ws, t) == (1, 2)   # the redo is still there
                await _redo(ws, {"task_id": t}, "u4")
                await asyncio.sleep(0.2)
                rows = await _snapshot(ws, "su2")
                assert rows["Stepped once"]["importance"] == 5
                assert await _version(ws, t) == (2, 2)

    async def test_undo_of_a_creation_removes_the_task_and_redo_rebuilds_it(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                t = await _add(ws, "Fleeting", "c1")
                await _undo(ws, {"task_id": t}, "c2")
                await asyncio.sleep(0.2)
                assert "Fleeting" not in await _snapshot(ws, "sc1")
                assert await _version(ws, t) == (None, 1)   # no row, chain intact
                await _redo(ws, {"task_id": t}, "c3")
                await asyncio.sleep(0.2)
                assert "Fleeting" in await _snapshot(ws, "sc2")

    async def test_a_new_write_discards_the_redo_branch(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                t = await _add(ws, "Branching", "b1")
                await _edit(ws, t, "b2", title="Branch A")
                await asyncio.sleep(0.2)
                await _undo(ws, {"task_id": t}, "b3")
                await asyncio.sleep(0.2)
                assert await _version(ws, t) == (1, 2)
                await _edit(ws, t, "b4", title="Branch B")
                await asyncio.sleep(0.2)
                assert await _version(ws, t) == (2, 2)     # not 3: v2 was rewritten
                chain = await _chain(ws, "task_version_chain", {"task_id": t})
                assert [r["title"] for r in chain] == ["Branching", "Branch B"]
                await _redo(ws, {"task_id": t}, "b5", expect="error")

    async def test_undo_steps_every_row_the_action_wrote(self, server):
        """A completion cascades over the subtree; undoing it takes the whole
        cascade back, not just the task the button was pressed on."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                parent = await _add(ws, "Cascade parent", "g1")
                await _split(ws, parent, "Cascade child", "g2")
                await asyncio.sleep(0.2)
                child = (await _snapshot(ws, "sg1"))["Cascade child"]["task_id"]
                await _complete(ws, parent, "g3")
                await asyncio.sleep(0.2)
                rows = await _snapshot(ws, "sg2")
                assert rows["Cascade parent"]["status"] == "complete"
                assert rows["Cascade child"]["status"] == "complete"
                await _undo(ws, {"task_id": parent}, "g4")
                await asyncio.sleep(0.3)
                rows = await _snapshot(ws, "sg3")
                assert rows["Cascade parent"]["status"] == "open"
                assert rows["Cascade child"]["status"] == "open", "the child was left behind"
                assert await _version(ws, child) == (1, 2)

    async def test_undo_steps_both_halves_of_a_link(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                a = await _add(ws, "Link left", "l1")
                b = await _add(ws, "Link right", "l2")
                wording = WORDINGS[0]
                await _add_ref(ws, a, "l3", kind="task", href=b, relation=wording)
                await asyncio.sleep(0.2)
                assert len(await _refs(ws, a)) == 1 and len(await _refs(ws, b)) == 1
                ref_id = (await _refs(ws, a))[0]["ref_id"]
                await _undo(ws, {"ref_id": ref_id}, "l4")
                await asyncio.sleep(0.3)
                assert await _refs(ws, a) == [], "the near half survived"
                assert await _refs(ws, b) == [], "the mirror was left behind"

    async def test_undo_of_a_split_takes_the_child_back(self, server):
        """Splitting sets the parent's Last Event too, so the parent's latest
        version *is* the split: undoing it removes the child and steps the
        parent back, rather than trying to undo the parent's creation."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                parent = await _add(ws, "Split parent", "sp1")
                await _split(ws, parent, "Split child", "sp2")
                await asyncio.sleep(0.2)
                assert await _version(ws, parent) == (2, 2)
                await _undo(ws, {"task_id": parent}, "sp3")
                await asyncio.sleep(0.3)
                rows = await _snapshot(ws, "ssp1")
                assert "Split child" not in rows
                assert rows["Split parent"]["last_event"] == "Created"

    async def test_undo_refuses_to_strand_a_child(self, server):
        """A task moved under another leaves that parent untouched, so the
        parent can still be sitting on the version that created it while
        having a child — the one way undoing to nothing would strand one."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                parent = await _add(ws, "Guard parent", "s1")
                child = await _add(ws, "Guard child", "s2")
                await _txn(ws, "move", {"task_id": child, "parent_task_id": parent,
                                        "updated_at": NOW}, "s3")
                await asyncio.sleep(0.2)
                assert await _version(ws, parent) == (1, 1)
                resp = await _undo(ws, {"task_id": parent}, "s4", expect="error")
                assert "still has" in resp["message"]
                assert "Guard parent" in await _snapshot(ws, "ss1")

    async def test_undo_refuses_when_a_row_of_the_group_moved_on(self, server):
        """Undo steps every row off the version the action wrote. If one of
        them has moved since, the group no longer describes what is there —
        stepping the rest would half-apply it.

        Going through `undo_action` keeps a group together, so the way to
        pull one apart is mkio's own per-row op, which is what this does.
        """
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                a = await _add(ws, "Merge left", "m1")
                b = await _add(ws, "Merge right", "m2")
                await _add_ref(ws, a, "m3", kind="task", href=b, relation=WORDINGS[0])
                await asyncio.sleep(0.2)
                near = (await _refs(ws, a))[0]["ref_id"]
                far = (await _refs(ws, b))[0]["ref_id"]
                await _txn(ws, "undo_ref", {"ref_id": far}, "m4")   # one half only
                await asyncio.sleep(0.2)
                resp = await _undo(ws, {"ref_id": near}, "m5", expect="error")
                assert "changed since" in resp["message"]
                assert len(await _refs(ws, a)) == 1, "the near half was stepped anyway"

    async def test_undoing_a_title_restores_the_labels_of_links_to_it(self, server):
        """The hook's reason for existing: a cursor move is not `edit`, so
        nothing else refreshes a link that shows the title it moved off."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                a = await _add(ws, "Labelled left", "h1")
                b = await _add(ws, "Labelled right", "h2")
                await _add_ref(ws, a, "h3", kind="task", href=b, relation=WORDINGS[0])
                await asyncio.sleep(0.2)
                await _edit(ws, b, "h4", title="Renamed right")
                await asyncio.sleep(0.2)
                assert (await _refs(ws, a))[0]["label"] == "Renamed right"
                await _undo(ws, {"task_id": b}, "h5")
                await asyncio.sleep(0.4)
                assert (await _refs(ws, a))[0]["label"] == "Labelled right"

    async def test_a_step_is_narrated(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                t = await _add(ws, "Narrated", "n1")
                await _edit(ws, t, "n2", title="Narrated again")
                await asyncio.sleep(0.2)
                await _undo(ws, {"task_id": t}, "n3")
                await asyncio.sleep(0.4)
                await _redo(ws, {"task_id": t}, "n4")
                await asyncio.sleep(0.4)
                assert await _actions(ws, t) == ["created", "edited", "undone", "redone"]


class TestActivity:
    async def test_every_op_is_recorded(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                parent = await _add(ws, "Story parent", "e1")
                await _split(ws, parent, "Story child", "e2")
                await asyncio.sleep(0.2)
                child = (await _snapshot(ws, "se1"))["Story child"]["task_id"]
                await _edit(ws, child, "e3", title="Story child edited")
                await _add_ref(ws, child, "e4", kind="url", href="https://example.com")
                await asyncio.sleep(0.2)
                ref_id = (await _refs(ws, child))[0]["ref_id"]
                await _txn(ws, "edit_ref", {"ref_id": ref_id, "href": "https://example.org",
                                            "label": "Example", "updated_at": NOW}, "e5")
                await _txn(ws, "delete_ref", {"ref_id": ref_id}, "e6")
                await _complete(ws, child, "e7")
                await _reopen(ws, child, "e8")
                await asyncio.sleep(0.3)
                assert await _actions(ws, child) == [
                    "split_from", "edited", "ref_added", "ref_edited", "ref_deleted",
                    "completed", "reopened",
                ]
                assert "split_to" in await _actions(ws, parent)

    async def test_a_move_records_where_it_went(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                a = await _add(ws, "Mover", "mv1")
                b = await _add(ws, "New parent", "mv2")
                await _txn(ws, "move", {"task_id": a, "parent_task_id": b, "updated_at": NOW}, "mv3")
                await asyncio.sleep(0.2)
                moved = [r for r in await _activity(ws, a) if r["action"] == "moved"]
                assert moved and moved[0]["detail"] == b

    async def test_the_actor_is_recorded(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                t = await _add(ws, "Attributed", "ac1")
                await asyncio.sleep(0.2)
                assert (await _activity(ws, t))[0]["actor"] == "mark"


class TestDeleteIsPermanent:
    """A delete is the one thing here that cannot be undone, so it must leave
    nothing behind: mkio drops a versioned row's history with the row, and
    `_op_delete` takes the subtree's events and any chain an undo left."""

    def test_delete_leaves_nothing(self, tmp_path):
        files = tmp_path / "refs"
        proc, base = _start("--user", "mark", "--files", str(files))
        try:
            asyncio.run(self._delete_leaves_nothing(base, files))
        finally:
            _stop(proc)

    async def _delete_leaves_nothing(self, base, files):
        png = b"\x89PNG\r\n\x1a\n" + b"deleteme"
        async with aiohttp.ClientSession() as s:
            _, up = await _upload(s, base, png, "image/png")
            name = up["href"].rsplit("/", 1)[1]
            async with s.ws_connect(base + "/ws") as ws:
                parent = await _add(ws, "Doomed parent", "d1")
                await _split(ws, parent, "Doomed child", "d2")
                await asyncio.sleep(0.2)
                child = (await _snapshot(ws, "sd1"))["Doomed child"]["task_id"]
                await _edit(ws, child, "d3", title="Doomed child edited")
                await _add_ref(ws, child, "d4", kind="file", href=up["href"], mime=up["mime"])
                await _add_ref(ws, child, "d5", kind="text", body="a secret note")
                await asyncio.sleep(0.3)
                assert (files / name).exists()
                # created, edited, and one version per reference added
                assert len(await _chain(ws, "task_version_chain", {"task_id": child})) == 4

                await _txn(ws, "delete", {"task_id": parent}, "d6")
                await asyncio.sleep(0.4)

                for task in (parent, child):
                    assert await _chain(ws, "task_version_chain", {"task_id": task}) == [], task
                    assert await _activity(ws, task) == [], task
                assert "Doomed parent" not in await _snapshot(ws, "sd2")
                assert not (files / name).exists(), "the file outlived its only reference"
                # Nothing of the snippet is left to read back.
                rows = await _request(ws, "ref_version_chain", {"ref_id": 0})
                assert all("secret" not in json.dumps(r) for r in rows)

    def test_delete_takes_an_undone_reference_with_it(self, tmp_path):
        files = tmp_path / "refs"
        proc, base = _start("--user", "mark", "--files", str(files))
        try:
            asyncio.run(self._undone_reference(base, files))
        finally:
            _stop(proc)

    async def _undone_reference(self, base, files):
        png = b"\x89PNG\r\n\x1a\n" + b"undone"
        async with aiohttp.ClientSession() as s:
            _, up = await _upload(s, base, png, "image/png")
            name = up["href"].rsplit("/", 1)[1]
            async with s.ws_connect(base + "/ws") as ws:
                t = await _add(ws, "Holds a file", "uf1")
                await _add_ref(ws, t, "uf2", kind="file", href=up["href"], mime=up["mime"])
                await asyncio.sleep(0.2)
                ref_id = (await _refs(ws, t))[0]["ref_id"]

                # Undone, not deleted: the row goes, the chain and the file
                # stay, because a redo has to be able to bring both back.
                await _undo(ws, {"ref_id": ref_id}, "uf3")
                await asyncio.sleep(0.3)
                assert await _refs(ws, t) == []
                assert (files / name).exists(), "redo would have nothing to show"
                assert await _chain(ws, "ref_version_chain", {"ref_id": ref_id}) != []
                await _redo(ws, {"ref_id": ref_id}, "uf4")
                await asyncio.sleep(0.3)
                assert (await _refs(ws, t))[0]["href"] == up["href"]
                async with s.get(base + up["href"]) as resp:
                    assert resp.status == 200

                # Undone again, then the task deleted: now it is for good.
                await _undo(ws, {"ref_id": ref_id}, "uf5")
                await asyncio.sleep(0.3)
                await _txn(ws, "delete", {"task_id": t}, "uf6")
                await asyncio.sleep(0.4)
                assert await _chain(ws, "ref_version_chain", {"ref_id": ref_id}) == []
                assert not (files / name).exists(), "an undone reference kept the file alive"

    async def test_delete_takes_an_undone_link_from_a_surviving_task(self, server):
        """The half owned by the task that survives is a chain under *its*
        key, so the sweep has to reach it by where the link pointed."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                a = await _add(ws, "Survivor", "ul1")
                b = await _add(ws, "Doomed link target", "ul2")
                await _add_ref(ws, a, "ul3", kind="task", href=b, relation=WORDINGS[0])
                await asyncio.sleep(0.2)
                near = (await _refs(ws, a))[0]["ref_id"]
                far = (await _refs(ws, b))[0]["ref_id"]
                await _undo(ws, {"ref_id": near}, "ul4")
                await asyncio.sleep(0.3)
                assert await _refs(ws, a) == [] and await _refs(ws, b) == []

                await _txn(ws, "delete", {"task_id": b}, "ul5")
                await asyncio.sleep(0.4)
                assert "Survivor" in await _snapshot(ws, "sul1")
                for ref_id in (near, far):
                    assert await _chain(ws, "ref_version_chain", {"ref_id": ref_id}) == [], ref_id


class TestLastEvent:
    """`tasks.last_event` says what happened to a task most recently, in
    words. Because `tasks` is versioned it rides along in every recorded
    version, so a task's history says what each version was *about* —
    `_mkio_op` only ever says "insert" or "update"."""

    async def _last(self, ws, task_id):
        rows = await _snapshot(ws, f"le-{task_id}")
        return {r["task_id"]: r["last_event"] for r in rows.values()}[task_id]

    async def test_each_op_describes_itself(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                t = await _add(ws, "Describe me", "le1")
                assert await self._last(ws, t) == "Created"

                await _edit(ws, t, "le2", title="Describe me twice")
                await asyncio.sleep(0.2)
                assert await self._last(ws, t) == "Edited"

                await _complete(ws, t, "le3")
                await asyncio.sleep(0.2)
                assert await self._last(ws, t) == "Completed"

                await _reopen(ws, t, "le4")
                await asyncio.sleep(0.2)
                assert await self._last(ws, t) == "Reopened"

                under = await _add(ws, "A new home", "le5")
                await _txn(ws, "move", {"task_id": t, "parent_task_id": under, "updated_at": NOW}, "le6")
                await asyncio.sleep(0.2)
                assert await self._last(ws, t) == f"Moved under {under}"

                await _txn(ws, "move", {"task_id": t, "parent_task_id": "__top__", "updated_at": NOW}, "le7")
                await asyncio.sleep(0.2)
                assert await self._last(ws, t) == "Moved to the top level"

    async def test_a_split_describes_both_sides(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                parent = await _add(ws, "Splitter", "ls1")
                await _split(ws, parent, "Split off piece", "ls2")
                await asyncio.sleep(0.3)
                child = (await _snapshot(ws, "sls1"))["Split off piece"]["task_id"]
                assert await self._last(ws, child) == f"Split from {parent}"
                assert await self._last(ws, parent) == "Split into Split off piece"

    async def test_a_reference_names_its_kind(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                t = await _add(ws, "Collector", "lr1")
                await _add_ref(ws, t, "lr2", kind="url", href="https://example.com", label="Example")
                await asyncio.sleep(0.3)
                assert await self._last(ws, t) == "Added a URL: Example"
                await _add_ref(ws, t, "lr3", kind="file", href="/files/abc.png",
                               mime="image/png", label="shot.png")
                await asyncio.sleep(0.3)
                assert await self._last(ws, t) == "Attached a file: shot.png"
                await _add_ref(ws, t, "lr4", kind="text", body="a pasted note")
                await asyncio.sleep(0.3)
                assert await self._last(ws, t) == "Added a snippet: a pasted note"

    async def test_a_link_describes_both_tasks(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                a = await _add(ws, "Link A", "ll1")
                b = await _add(ws, "Link B", "ll2")
                forward = WORDINGS[0]
                await _add_ref(ws, a, "ll3", kind="task", href=b, relation=forward)
                await asyncio.sleep(0.3)
                assert await self._last(ws, a) == f"Linked: {forward} {b}"
                assert (await self._last(ws, b)).startswith("Linked: ")
                assert a in await self._last(ws, b)

    async def test_a_reference_makes_a_version_of_its_task(self, server):
        """The point of the touch: a reference change is a change to the task
        that owns it, so it shows up in that task's recorded history."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                t = await _add(ws, "Versioned by reference", "lv1")
                assert await _version(ws, t) == (1, 1)
                await _add_ref(ws, t, "lv2", kind="url", href="https://example.com", label="Site")
                await asyncio.sleep(0.3)
                assert await _version(ws, t) == (2, 2), "the reference did not make a version"
                chain = await _chain(ws, "task_version_chain", {"task_id": t})
                assert [r["last_event"] for r in chain] == ["Created", "Added a URL: Site"]

                ref_id = (await _refs(ws, t))[0]["ref_id"]
                await _txn(ws, "delete_ref", {"ref_id": ref_id}, "lv3")
                await asyncio.sleep(0.3)
                assert await self._last(ws, t) == "Removed a reference: Site"
                assert await _version(ws, t) == (3, 3)

    async def test_a_reference_and_its_task_step_back_together(self, server):
        """The touch is written under the same transaction ref as the
        reference, so it is part of the same action to undo."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                t = await _add(ws, "Steps together", "lt1")
                await _add_ref(ws, t, "lt2", kind="url", href="https://example.com", label="Site")
                await asyncio.sleep(0.3)
                ref_id = (await _refs(ws, t))[0]["ref_id"]
                await _undo(ws, {"ref_id": ref_id}, "lt3")
                await asyncio.sleep(0.4)
                assert await _refs(ws, t) == []
                assert await self._last(ws, t) == "Created", "the task kept the reference's Last Event"
                assert await _version(ws, t) == (1, 2)

    async def test_a_step_restores_the_last_event_it_lands_on(self, server):
        """The hook does not set Last Event: an undo restores the version's
        own, and writing one would extend the chain past the cursor and
        discard the redo branch it just made."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                t = await _add(ws, "Steps back", "lb1")
                await _complete(ws, t, "lb2")
                await asyncio.sleep(0.2)
                assert await self._last(ws, t) == "Completed"
                await _undo(ws, {"task_id": t}, "lb3")
                await asyncio.sleep(0.4)
                assert await self._last(ws, t) == "Created"
                assert await _version(ws, t) == (1, 2), "redo was discarded"
                await _redo(ws, {"task_id": t}, "lb4")
                await asyncio.sleep(0.4)
                assert await self._last(ws, t) == "Completed"


class TestStepGuards:
    """The refusals. Each is a way a step could leave the data describing
    something that is no longer there, so each is refused whole rather than
    applied in part."""

    async def test_restore_refused_when_the_old_parent_is_gone(self, server):
        """A delete is permanent, so a version recorded while the task sat
        under it can no longer be restored."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                home = await _add(ws, "Old home", "g1")
                mover = await _add(ws, "Mover away", "g2")
                await _txn(ws, "move", {"task_id": mover, "parent_task_id": home,
                                        "updated_at": NOW}, "g3")
                await asyncio.sleep(0.2)
                await _txn(ws, "move", {"task_id": mover, "parent_task_id": "__top__",
                                        "updated_at": NOW}, "g4")
                await asyncio.sleep(0.2)
                await _txn(ws, "delete", {"task_id": home}, "g5")
                await asyncio.sleep(0.3)

                resp = await _undo(ws, {"task_id": mover}, "g6", expect="error")
                assert "no longer exists" in resp["message"] and home in resp["message"]
                rows = await _snapshot(ws, "sg1")
                assert rows["Mover away"]["parent_task_id"] == "", "the move was undone anyway"

    async def test_nothing_left_to_step(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                t = await _add(ws, "Only once", "n1")
                resp = await _redo(ws, {"task_id": t}, "n2", expect="error")
                assert resp["message"] == "Nothing to redo"
                await _undo(ws, {"task_id": t}, "n3")          # removes it
                await asyncio.sleep(0.3)
                resp = await _undo(ws, {"task_id": t}, "n4", expect="error")
                assert resp["message"] == "Nothing to undo"

    async def test_redo_restores_a_whole_cascade(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                parent = await _add(ws, "Redo parent", "rc1")
                await _split(ws, parent, "Redo child", "rc2")
                await asyncio.sleep(0.2)
                await _complete(ws, parent, "rc3")
                await asyncio.sleep(0.3)
                await _undo(ws, {"task_id": parent}, "rc4")
                await asyncio.sleep(0.4)
                rows = await _snapshot(ws, "src1")
                assert rows["Redo parent"]["status"] == "open"
                assert rows["Redo child"]["status"] == "open"
                await _redo(ws, {"task_id": parent}, "rc5")
                await asyncio.sleep(0.4)
                rows = await _snapshot(ws, "src2")
                assert rows["Redo parent"]["status"] == "complete"
                assert rows["Redo child"]["status"] == "complete", "the child was left behind"

    async def test_a_reference_reports_its_own_cursor(self, server):
        """`ref_version_state` is what tells mkui whether a reference has a
        redo to offer, including once it has been undone out of existence."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                t = await _add(ws, "Cursor holder", "rs1")
                await _add_ref(ws, t, "rs2", kind="url", href="https://example.com", label="Site")
                await asyncio.sleep(0.3)
                ref_id = (await _refs(ws, t))[0]["ref_id"]
                state = (await _request(ws, "ref_version_state", {"ref_id": ref_id}))[0]
                assert (state["current"], state["top"]) == (1, 1)
                await _undo(ws, {"ref_id": ref_id}, "rs3")
                await asyncio.sleep(0.4)
                state = (await _request(ws, "ref_version_state", {"ref_id": ref_id}))[0]
                assert (state["current"], state["top"]) == (None, 1), "redo would not be offered"

    async def test_a_link_is_recorded_on_both_tasks(self, server):
        """A link is two rows and two events: the task on the far side of one
        has had something happen to it as much as the near side."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                a = await _add(ws, "Near side", "bl1")
                b = await _add(ws, "Far side", "bl2")
                await _add_ref(ws, a, "bl3", kind="task", href=b, relation=WORDINGS[0])
                await asyncio.sleep(0.3)
                assert await _actions(ws, a) == ["created", "ref_added"]
                assert await _actions(ws, b) == ["created", "ref_added"]

    async def test_a_survivor_records_the_link_a_delete_took(self, server):
        """Deleting a task removes the links pointing into it. The task that
        survives lost a reference, and says so — its own events are not the
        deleted task's, so they stay."""
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                keeper = await _add(ws, "Keeper", "sv1")
                doomed = await _add(ws, "Doomed", "sv2")
                await _add_ref(ws, keeper, "sv3", kind="task", href=doomed, relation=WORDINGS[0])
                await asyncio.sleep(0.3)
                await _txn(ws, "delete", {"task_id": doomed}, "sv4")
                await asyncio.sleep(0.4)
                assert await _actions(ws, keeper) == ["created", "ref_added", "ref_deleted"]
                assert await _activity(ws, doomed) == []
                assert await _refs(ws, keeper) == []
                last = (await _snapshot(ws, "ssv1"))["Keeper"]["last_event"]
                assert last.startswith("Removed a reference")


class TestUpgrade:
    """A database made before versioning must carry over. mkio adds the
    counter and the history tables and records a `baseline` version for
    every existing row, which is what an undo of a first edit steps onto —
    without it, a row that predates the feature could never be stepped
    back. The README promises this; this is what holds it."""

    def _pre_history_db(self, tmp_path):
        """A database shaped the way 0.7.0 left one: no `_mkio_version`, no
        history tables, no `task_events`, no `last_event`. Built from the
        current config minus everything added since, so it stays in step."""
        import sqlite3
        import tomllib
        cfg = tomllib.loads((__import__("pathlib").Path(__file__).resolve().parent.parent
                             / "mktask" / "mktask.toml").read_text())
        db = tmp_path / "old.db"
        con = sqlite3.connect(db)
        for name in ("tasks", "task_refs", "relations", "counters"):
            cols = dict(cfg["tables"][name]["columns"])
            cols.pop("last_event", None)
            cols.pop("assigned_to", None)
            con.execute(f"CREATE TABLE {name} ({', '.join(f'{c} {d}' for c, d in cols.items())})")
        con.execute("INSERT INTO tasks (task_id, title, importance, urgency) "
                    "VALUES ('TKMA00000001', 'Made before history', 5, 3)")
        con.execute("INSERT INTO task_refs (task_id, kind, href, label) "
                    "VALUES ('TKMA00000001', 'url', 'https://old.example', 'old link')")
        con.execute("INSERT INTO counters (name, last) VALUES ('task', 1)")
        con.commit()
        con.close()
        return db

    def test_a_pre_history_database_carries_over(self, tmp_path):
        db = self._pre_history_db(tmp_path)
        proc, base = _start("--user", "mark", db=str(db))
        try:
            asyncio.run(self._carries_over(base))
        finally:
            _stop(proc)

    async def _carries_over(self, base):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(base + "/ws") as ws:
                rows = await _snapshot(ws, "up1")
                task = rows["Made before history"]
                assert task["_mkio_version"] == 1, "no cursor on a row that predates versioning"
                assert task["last_event"] == "", "a row from before has no event to name"
                assert task["assigned_to"] == "", "a column added later starts empty"

                chain = await _chain(ws, "task_version_chain", {"task_id": task["task_id"]})
                assert [c["_mkio_op"] for c in chain] == ["baseline"]
                refs = await _refs(ws, task["task_id"])
                assert [r["label"] for r in refs] == ["old link"]

                # The point of the baseline: the first edit is undoable.
                await _edit(ws, task["task_id"], "up2", title="Edited after upgrading",
                            importance=5, urgency=3)
                await asyncio.sleep(0.3)
                assert "Edited after upgrading" in await _snapshot(ws, "up3")
                await _undo(ws, {"task_id": task["task_id"]}, "up4")
                await asyncio.sleep(0.4)
                back = await _snapshot(ws, "up5")
                assert "Made before history" in back, "nothing to step back onto"
                assert back["Made before history"]["_mkio_version"] == 1

                # And the Task ID counter picks up where it left off.
                new_id = await _add(ws, "Made after upgrading", "up6")
                assert new_id == "TKMA00000002"
