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


@pytest.fixture(scope="module")
def server():
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "mktask", "-d", ":memory:", "-p", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
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
    yield base
    proc.terminate()
    proc.wait(timeout=5)


async def _recv_json(ws):
    msg = await asyncio.wait_for(ws.receive(), timeout=5)
    return json.loads(msg.data)


async def _txn(ws, op, data, ref):
    await ws.send_json({"service": "tasks", "type": "transaction", "op": op,
                        "data": data, "ref": ref})
    resp = await _recv_json(ws)
    assert resp["type"] == "result", resp
    return resp


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
                task_id = rows["Write tests"]["id"]

                await _txn(ws, "edit", {"id": task_id, "title": "Write tests!",
                                        "notes": "", "importance": 2, "urgency": 2,
                                        "due": "", "updated_at": "2026-09-05 00:00:00"}, "r3")
                await _txn(ws, "done", {"id": task_id, "done_at": "2026-09-05 01:00:00",
                                        "updated_at": "2026-09-05 01:00:00"}, "r4")
                await asyncio.sleep(0.2)
                rows = await _snapshot(ws, "s2")
                assert "Write tests" not in rows
                row = rows["Write tests!"]
                assert row["status"] == "done"
                assert row["done_at"] == "2026-09-05 01:00:00"
                assert row["importance"] == 2

                await _txn(ws, "reopen", {"id": task_id,
                                          "updated_at": "2026-09-05 02:00:00"}, "r5")
                await asyncio.sleep(0.2)
                row = (await _snapshot(ws, "s3"))["Write tests!"]
                assert row["status"] == "open"
                assert row["done_at"] == ""

                await _txn(ws, "delete", {"id": task_id}, "r6")
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
                await _txn(ws, "add", {"title": "F-done"}, "f2")
                await asyncio.sleep(0.2)
                done_id = (await _snapshot(ws, "f-s1"))["F-done"]["id"]
                await _txn(ws, "done", {"id": done_id, "done_at": "2026-09-05 00:00:00",
                                        "updated_at": "2026-09-05 00:00:00"}, "f3")
                await asyncio.sleep(0.2)

                await ws.send_json({"service": "all_tasks", "type": "subscribe",
                                    "protocol": "query", "subid": "f-s2", "ref": "q",
                                    "filter": "status == 'done'"})
                snap = await _recv_json(ws)
                await ws.send_json({"service": "all_tasks", "type": "unsubscribe", "subid": "f-s2"})
        titles = {r["title"] for r in snap["rows"]}
        assert "F-done" in titles
        assert "F-open" not in titles

    async def test_add_stamps_created_at(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                await _txn(ws, "add", {"title": "Stamped"}, "c1")
                await asyncio.sleep(0.2)
                row = (await _snapshot(ws, "c-s1"))["Stamped"]
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", row["created_at"])
        assert row["updated_at"] == row["created_at"]
        assert row["done_at"] == ""
        assert row["status"] == "open"

    async def test_add_requires_title(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                await ws.send_json({"service": "tasks", "type": "transaction", "op": "add",
                                    "data": {"notes": "no title"}, "ref": "t1"})
                resp = await _recv_json(ws)
        assert resp["type"] == "error"


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
