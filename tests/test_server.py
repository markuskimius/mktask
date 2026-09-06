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
        assert {"tasks", "all_tasks", "mkui_layouts",
                "mkui_layouts_list", "mkui_layouts_get"} <= names


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
