"""The `tasks` transaction service: Task ID assignment and subtree cascades.

mkio's transaction service runs the ops declared in mktask.toml verbatim.
This subclass keeps those ops as the single description of what a request
writes and adds the two things a config cannot express:

- `add` and `split` need a Task ID: "TK", a two-letter prefix from the
  username, and a decimal sequence number padded to at least eight digits
  (TKMA00000001). The number comes from an in-memory counter seeded from the
  `counters` table at startup and written back in the same transaction as
  the insert, so a crash can neither reuse nor skip a number.
- `complete` and `delete` apply to the whole subtree under the given task,
  and `reopen` to the task and every ancestor, so the open-only view never
  shows a child without its parent. Every touched row goes through the
  writer in one transaction, so the change bus announces each of them.
"""

from __future__ import annotations

import getpass
import re
from typing import Any

from aiohttp.web import WebSocketResponse

from mkio.services.transaction import TransactionService, _extract_params
from mkio.ws_protocol import make_error, make_result

TASK_ID_PATTERN = re.compile(r"^TK[A-Z0-9]{2}\d{8,}$")
COUNTER_NAME = "task"
_MIN_DIGITS = 8


def user_prefix(username: str | None) -> str:
    """Two prefix letters for a username: alphanumerics, uppercased, X-padded.

    "mark" -> "MA", "m" -> "MX", "" -> "XX", "_bob.k" -> "BO".
    """
    letters = [c for c in (username or "") if c.isascii() and c.isalnum()]
    return ("".join(letters[:2]).upper() + "XX")[:2]


def format_task_id(prefix: str, number: int) -> str:
    """TK + prefix + the number, zero-padded to at least eight digits, never wrapped."""
    return f"TK{prefix}{number:0{_MIN_DIGITS}d}"


class TaskTransactions(TransactionService):
    """mkio transaction service for `tasks` with Task IDs and cascades."""

    CASCADE_OPS = ("complete", "reopen", "delete")

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.prefix = self.config.get("prefix") or user_prefix(getpass.getuser())
        self._last = 0

    async def start(self) -> None:
        await super().start()
        rows = await self.db.read(
            "SELECT last FROM counters WHERE name = ?", (COUNTER_NAME,)
        )
        if rows:
            self._last = int(rows[0]["last"])
        else:
            # No counter row yet: continue after the highest number in use, so
            # a lost counter can never hand out an existing Task ID.
            rows = await self.db.read("SELECT task_id FROM tasks")
            self._last = max(
                (int(r["task_id"][4:]) for r in rows if TASK_ID_PATTERN.match(r["task_id"])),
                default=0,
            )

    def next_task_id(self) -> tuple[str, int]:
        """Reserve the next number. Synchronous, so concurrent requests cannot collide."""
        self._last += 1
        return format_task_id(self.prefix, self._last), self._last

    async def on_message(self, ws: WebSocketResponse, msg: dict[str, Any]) -> None:
        op = msg.get("op")
        data = msg.get("data", {})
        if msg.get("type") == "check" or not isinstance(data, dict):
            return await super().on_message(ws, msg)

        if op in ("add", "split"):
            if data.get("task_id"):
                return await self._error(ws, msg, "Task ID is assigned by the server")
            if op == "split" and not await self._exists(data.get("parent_task_id")):
                return await self._error(ws, msg, f"Cannot split: no task {data.get('parent_task_id')!r}")
            task_id, number = self.next_task_id()
            data = {**data, "task_id": task_id, "last": number}
            return await super().on_message(ws, {**msg, "data": data})

        if op in self.CASCADE_OPS:
            return await self._cascade(ws, msg, op, data)

        return await super().on_message(ws, msg)

    # ── Cascades ──────────────────────────────────────────────────────

    async def _cascade(self, ws: WebSocketResponse, msg: dict[str, Any], op: str, data: dict[str, Any]) -> None:
        ref = msg.get("ref")
        txnid = msg.get("txnid")
        try:
            task_id = data["task_id"]
            if op == "reopen":
                keys = await self._ancestors_and_self(task_id, status="complete")
            else:
                keys = await self._subtree(task_id, status="open" if op == "complete" else None)
            if not keys:
                keys = [task_id]  # let the single-row op report a no-op or a bad key as it would today
            compiled = self._resolve_ops({"op": op})
            ops = tuple(step for _ in keys for step in compiled)
            params = tuple(
                _extract_params(step, {**data, "task_id": key})
                for key in keys for step in compiled
            )
            result = await self.writer.submit(ops, params, data, ref=ref)
            if ref is not None:
                self._cache_result(ref, result)
            resp = make_result(ref, self.name, result, txnid=txnid)
        except KeyError as e:
            resp = make_error(ref, f"Missing required field {e} in op '{op}'", txnid=txnid)
        except Exception as e:  # noqa: BLE001 — mirror mkio: any failure becomes an error reply
            resp = make_error(ref, str(e), txnid=txnid)
        await ws.send_bytes(resp)
        await self.notify_monitors("out", resp)

    async def _subtree(self, task_id: str, status: str | None) -> list[str]:
        """The task and every descendant, parents before children."""
        rows = await self.db.read(
            """
            WITH RECURSIVE sub(task_id, status, depth) AS (
                SELECT task_id, status, 0 FROM tasks WHERE task_id = ?
                UNION ALL
                SELECT t.task_id, t.status, sub.depth + 1
                FROM tasks t JOIN sub ON t.parent_task_id = sub.task_id
            )
            SELECT task_id, status FROM sub ORDER BY depth
            """,
            (task_id,),
        )
        return [r["task_id"] for r in rows if status is None or r["status"] == status]

    async def _ancestors_and_self(self, task_id: str, status: str | None) -> list[str]:
        """The task and every ancestor, the task first."""
        rows = await self.db.read(
            """
            WITH RECURSIVE up(task_id, parent_task_id, status, depth) AS (
                SELECT task_id, parent_task_id, status, 0 FROM tasks WHERE task_id = ?
                UNION ALL
                SELECT t.task_id, t.parent_task_id, t.status, up.depth + 1
                FROM tasks t JOIN up ON t.task_id = up.parent_task_id
            )
            SELECT task_id, status FROM up ORDER BY depth
            """,
            (task_id,),
        )
        return [r["task_id"] for r in rows if status is None or r["status"] == status]

    async def _exists(self, task_id: Any) -> bool:
        if not isinstance(task_id, str) or not task_id:
            return False
        rows = await self.db.read("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,))
        return bool(rows)

    async def _error(self, ws: WebSocketResponse, msg: dict[str, Any], text: str) -> None:
        resp = make_error(msg.get("ref"), text, txnid=msg.get("txnid"))
        await ws.send_bytes(resp)
        await self.notify_monitors("out", resp)
