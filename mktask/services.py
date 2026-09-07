"""The `tasks` transaction service: Task ID assignment, cascades, references.

mkio's transaction service runs the ops declared in mktask.toml verbatim.
This subclass keeps those ops as the single description of what a request
writes and adds what a config cannot express:

- `add` and `split` need a Task ID: "TK", a two-letter prefix from the
  username, and a decimal sequence number padded to at least eight digits
  (TKMA00000001). The number comes from an in-memory counter seeded from the
  `counters` table at startup and written back in the same transaction as
  the insert, so a crash can neither reuse nor skip a number.
- `complete` and `delete` apply to the whole subtree under the given task,
  and `reopen` to the task and every ancestor, so the open-only view never
  shows a child without its parent. A delete also removes every reference
  the subtree owns or is linked to, and unlinks files nothing refers to any
  more. Updates go through the writer in one transaction; deletes go one
  request per row, because the writer announces a delete with the request's
  data, and one request for many rows would announce the same key for all.
- References (`task_refs`): `add_ref` checks the owning task exists, fills
  a missing label, and for kind "task" writes the row and its mirror on the
  other task with the inverse relation. `delete_ref` removes both halves of
  a link. `edit_ref` refuses a task link (its label follows the linked
  task) and keeps a file's href. `edit` on a task relabels every link that
  points at it.
"""

from __future__ import annotations

import asyncio
import getpass
import os
import re
from pathlib import Path
from typing import Any

from aiohttp.web import WebSocketResponse

from mkio._ref import next_ref
from mkio.services.transaction import TransactionService, _extract_params
from mkio.ws_protocol import make_error, make_result

TASK_ID_PATTERN = re.compile(r"^TK[A-Z0-9]{2}\d{8,}$")
COUNTER_NAME = "task"
_MIN_DIGITS = 8

REF_KINDS = ("url", "text", "file", "task")
SEED_RELATIONS = Path(__file__).with_name("relations.json")  # what a new database starts with
FILES_ROUTE = "/files/"
_LABEL_MAX = 80


def user_prefix(username: str | None) -> str:
    """Two prefix letters for a username: alphanumerics, uppercased, X-padded.

    "mark" -> "MA", "m" -> "MX", "" -> "XX", "_bob.k" -> "BO".
    """
    letters = [c for c in (username or "") if c.isascii() and c.isalnum()]
    return ("".join(letters[:2]).upper() + "XX")[:2]


def format_task_id(prefix: str, number: int) -> str:
    """TK + prefix + the number, zero-padded to at least eight digits, never wrapped."""
    return f"TK{prefix}{number:0{_MIN_DIGITS}d}"


def default_label(kind: str, href: str, body: str) -> str:
    """The label a reference shows when the client sent none."""
    if kind == "url":
        return href
    if kind == "file":
        return href.rsplit("/", 1)[-1]
    if kind == "text":
        first = next((line.strip() for line in body.splitlines() if line.strip()), "")
        return first if len(first) <= _LABEL_MAX else first[: _LABEL_MAX - 1] + "…"
    return ""


class TaskTransactions(TransactionService):
    """mkio transaction service for `tasks` with Task IDs, cascades, and references."""

    CASCADE_OPS = ("complete", "reopen", "delete")
    REF_OPS = ("add_ref", "edit_ref", "delete_ref")
    RELATION_OPS = ("add_relation", "edit_relation", "delete_relation")

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.prefix = self.config.get("prefix") or user_prefix(getpass.getuser())
        files_dir = self.config.get("files_dir")
        self.files_dir = Path(files_dir) if files_dir else None
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

        if op in self.CASCADE_OPS or op in self.REF_OPS or op in self.RELATION_OPS or op == "edit":
            return await self._guarded(ws, msg, op, data)

        return await super().on_message(ws, msg)

    # ── Dispatch with mkio-style error replies ────────────────────────

    async def _guarded(self, ws: WebSocketResponse, msg: dict[str, Any], op: str, data: dict[str, Any]) -> None:
        ref = msg.get("ref") or next_ref()
        txnid = msg.get("txnid")
        try:
            handler = getattr(self, f"_op_{op}")
            result = await handler(data, ref)
            self._cache_result(ref, result)
            resp = make_result(ref, self.name, result, txnid=txnid)
        except KeyError as e:
            resp = make_error(ref, f"Missing required field {e} in op '{op}'", txnid=txnid)
        except Exception as e:  # noqa: BLE001 — mirror mkio: any failure becomes an error reply
            resp = make_error(ref, str(e), txnid=txnid)
        await ws.send_bytes(resp)
        await self.notify_monitors("out", resp)

    def _steps(self, op: str, rows: list[dict[str, Any]]) -> tuple[tuple, tuple]:
        """The compiled steps of `op` once per row, with that row's params."""
        compiled = self._resolve_ops({"op": op})
        ops = tuple(step for _ in rows for step in compiled)
        params = tuple(_extract_params(step, row) for row in rows for step in compiled)
        return ops, params

    async def _submit(self, op: str, rows: list[dict[str, Any]], data: dict[str, Any], ref: str | None) -> dict[str, Any]:
        """One transaction over every row. Right for inserts and updates, whose
        RETURNING rows are what the change bus announces."""
        ops, params = self._steps(op, rows)
        return await self.writer.submit(ops, params, data, ref=ref)

    async def _submit_each(self, op: str, rows: list[dict[str, Any]], ref: str | None) -> dict[str, Any]:
        """One request per row, queued together so they land in one batch.
        Right for deletes: the writer announces a delete with the request's
        data, so each row needs its own request to be announced by its key."""
        results = await asyncio.gather(*(
            self.writer.submit(*self._steps(op, [row]), row, ref=ref) for row in rows
        ))
        return results[-1] if results else {"ok": True}

    # ── Task cascades ────────────────────────────────────────────────

    async def _op_complete(self, data: dict[str, Any], ref: str | None) -> dict[str, Any]:
        keys = await self._subtree(data["task_id"], status="open") or [data["task_id"]]
        return await self._submit("complete", [{**data, "task_id": k} for k in keys], data, ref)

    async def _op_reopen(self, data: dict[str, Any], ref: str | None) -> dict[str, Any]:
        keys = await self._ancestors_and_self(data["task_id"], status="complete") or [data["task_id"]]
        return await self._submit("reopen", [{**data, "task_id": k} for k in keys], data, ref)

    async def _op_delete(self, data: dict[str, Any], ref: str | None) -> dict[str, Any]:
        keys = await self._subtree(data["task_id"], status=None) or [data["task_id"]]
        marks = ", ".join("?" for _ in keys)
        refs = await self.db.read(
            f"SELECT ref_id, kind, href FROM task_refs "
            f"WHERE task_id IN ({marks}) OR (kind = 'task' AND href IN ({marks}))",
            (*keys, *keys),
        )
        if refs:
            await self._submit_each("delete_ref", [{"ref_id": r["ref_id"]} for r in refs], ref)
        result = await self._submit_each("delete", [{**data, "task_id": k} for k in keys], ref)
        await self._unlink_orphans(r["href"] for r in refs if r["kind"] == "file")
        return result

    async def _op_edit(self, data: dict[str, Any], ref: str | None) -> dict[str, Any]:
        """A plain update, plus a relabel of every link that points at this task."""
        compiled = self._resolve_ops({"op": "edit"})
        ops = list(compiled)
        params = [_extract_params(step, data) for step in compiled]
        if "title" in data:
            links = await self.db.read(
                "SELECT ref_id FROM task_refs WHERE kind = 'task' AND href = ?", (data["task_id"],)
            )
            r_ops, r_params = self._steps("relabel_ref", [
                {"ref_id": r["ref_id"], "label": data["title"]} for r in links
            ])
            ops.extend(r_ops)
            params.extend(r_params)
        return await self.writer.submit(tuple(ops), tuple(params), data, ref=ref)

    # ── References ────────────────────────────────────────────────────

    async def _op_add_ref(self, data: dict[str, Any], ref: str | None) -> dict[str, Any]:
        task_id = data["task_id"]
        kind = data.get("kind") or "url"
        href = str(data.get("href") or "").strip()
        body = str(data.get("body") or "")
        label = str(data.get("label") or "").strip()
        if kind not in REF_KINDS:
            raise ValueError(f"Unknown reference kind {kind!r}")
        task = await self._task(task_id)
        if task is None:
            raise ValueError(f"Cannot add a reference: no task {task_id!r}")

        if kind == "task":
            relation = str(data.get("relation") or "").strip()
            inverse = await self._inverse(relation)
            if inverse is None:
                raise ValueError(f"Unknown relation {relation!r}")
            if href == task_id:
                raise ValueError("A task cannot be linked to itself")
            other = await self._task(href)
            if other is None:
                raise ValueError(f"Cannot link: no task {href!r}")
            mine = await self._ancestors_and_self(task_id, status=None)
            theirs = await self._ancestors_and_self(href, status=None)
            if href in mine:
                raise ValueError(f"Cannot link: {href} is an ancestor of {task_id} (it was split from it)")
            if task_id in theirs:
                raise ValueError(f"Cannot link: {href} is a descendant of {task_id} (it was split from it)")
            if mine[-1] == theirs[-1]:
                raise ValueError(f"Cannot link: {task_id} and {href} are in the same tree (split from {mine[-1]})")
            dup = await self.db.read(
                "SELECT 1 FROM task_refs WHERE task_id = ? AND kind = 'task' AND href = ? AND relation = ?",
                (task_id, href, relation),
            )
            if dup:
                raise ValueError(f"{task_id} already {relation.replace('_', ' ')} {href}")
            rows = [
                {**data, "kind": kind, "relation": relation, "href": href,
                 "label": other["title"], "body": "", "mime": ""},
                {**data, "task_id": href, "kind": kind, "relation": inverse,
                 "href": task_id, "label": task["title"], "body": "", "mime": ""},
            ]
            return await self._submit("add_ref", rows, data, ref)

        if kind == "url" and not href:
            raise ValueError("A URL reference needs a URL")
        if kind == "text" and not body.strip():
            raise ValueError("A text reference needs some text")
        if kind == "file" and not href.startswith(FILES_ROUTE):
            raise ValueError("A file reference must point under /files/")
        row = {**data, "kind": kind, "relation": "", "href": href if kind != "text" else "",
               "body": body if kind == "text" else "", "label": label or default_label(kind, href, body)}
        return await self._submit("add_ref", [row], data, ref)

    async def _op_edit_ref(self, data: dict[str, Any], ref: str | None) -> dict[str, Any]:
        row = await self._ref(data["ref_id"])
        if row is None:
            raise ValueError(f"No reference {data['ref_id']!r}")
        if row["kind"] == "task":
            # Only the relation is editable: the label follows the linked
            # task and a new target is a new link. Both halves change together.
            relation = str(data.get("relation") or "").strip()
            inverse = await self._inverse(relation)
            if inverse is None:
                raise ValueError(f"Unknown relation {relation!r}")
            mirror = await self._mirror(row)
            rows = [{**data, **row, "relation": relation, "updated_at": data.get("updated_at", row["updated_at"])}]
            if mirror:
                rows.append({**mirror, "relation": inverse, "updated_at": data.get("updated_at", mirror["updated_at"])})
            return await self._submit("edit_ref", rows, data, ref)
        href = str(data.get("href") or "").strip()
        body = str(data.get("body") or "")
        if row["kind"] == "file":
            href = row["href"]  # the file is what it is; the label is the editable part
        elif row["kind"] == "text":
            href = ""
        elif not href:
            raise ValueError("A URL reference needs a URL")
        label = str(data.get("label") or "").strip() or default_label(row["kind"], href, body)
        return await self._submit("edit_ref", [{**data, "href": href, "body": body, "label": label, "relation": ""}], data, ref)

    async def _op_delete_ref(self, data: dict[str, Any], ref: str | None) -> dict[str, Any]:
        row = await self._ref(data["ref_id"])
        rows = [{"ref_id": data["ref_id"]}]
        if row is not None and row["kind"] == "task":
            mirror = await self._mirror(row)
            if mirror:
                rows.append({"ref_id": mirror["ref_id"]})
        result = await self._submit_each("delete_ref", rows, ref)
        if row is not None and row["kind"] == "file":
            await self._unlink_orphans([row["href"]])
        return result

    # ── Relations ─────────────────────────────────────────────────────

    async def _relation_data(self, data: dict[str, Any], exclude_id: Any = None) -> dict[str, Any]:
        """Trimmed wordings with backward defaulting to forward, checked unique."""
        forward = str(data.get("forward") or "").strip()
        backward = str(data.get("backward") or "").strip() or forward
        if not forward:
            raise ValueError("A relation needs a wording")
        rows = await self.db.read("SELECT relation_id, forward, backward FROM relations")
        taken = {}
        for r in rows:
            if exclude_id is not None and r["relation_id"] == exclude_id:
                continue
            taken[r["forward"].lower()] = r["forward"]
            taken[r["backward"].lower()] = r["backward"]
        for wording in {forward, backward}:
            if wording.lower() in taken:
                raise ValueError(f"{wording!r} is already a relation wording ({taken[wording.lower()]!r})")
        return {**data, "forward": forward, "backward": backward, "notes": str(data.get("notes") or "")}

    async def _op_add_relation(self, data: dict[str, Any], ref: str | None) -> dict[str, Any]:
        return await self._submit("add_relation", [await self._relation_data(data)], data, ref)

    async def _op_edit_relation(self, data: dict[str, Any], ref: str | None) -> dict[str, Any]:
        """Rename: every link carrying an old wording takes the new one, same transaction."""
        old = await self._relation(data["relation_id"])
        if old is None:
            raise ValueError(f"No relation {data['relation_id']!r}")
        new = await self._relation_data(data, exclude_id=old["relation_id"])
        compiled = self._resolve_ops({"op": "edit_relation"})
        ops = list(compiled)
        params = [_extract_params(step, new) for step in compiled]
        rewrite = [(old["forward"], new["forward"]), (old["backward"], new["backward"])]
        for was, now in rewrite:
            if was == now:
                continue
            links = await self.db.read(
                "SELECT ref_id FROM task_refs WHERE kind = 'task' AND relation = ?", (was,)
            )
            r_ops, r_params = self._steps("reword_ref", [{"ref_id": r["ref_id"], "relation": now} for r in links])
            ops.extend(r_ops)
            params.extend(r_params)
        return await self.writer.submit(tuple(ops), tuple(params), data, ref=ref)

    async def _op_delete_relation(self, data: dict[str, Any], ref: str | None) -> dict[str, Any]:
        row = await self._relation(data["relation_id"])
        if row is not None:
            used = await self.db.read(
                "SELECT COUNT(*) AS n FROM task_refs WHERE kind = 'task' AND relation IN (?, ?)",
                (row["forward"], row["backward"]),
            )
            n = int(used[0]["n"])
            if n:
                links = n // 2  # a link is two rows, whatever the wordings
                raise ValueError(
                    f"Cannot delete {row['forward']!r}: {links} link{'s' if links != 1 else ''} use it. "
                    "Remove those links first, or rename the relation instead."
                )
        return await self._submit_each("delete_relation", [{"relation_id": data["relation_id"]}], ref)

    async def _relation(self, relation_id: Any) -> dict[str, Any] | None:
        rows = await self.db.read("SELECT * FROM relations WHERE relation_id = ?", (relation_id,))
        return dict(rows[0]) if rows else None

    async def _inverse(self, wording: str) -> str | None:
        """The other wording of the pair a wording belongs to; None if unknown."""
        if not wording:
            return None
        rows = await self.db.read(
            "SELECT forward, backward FROM relations WHERE forward = ? OR backward = ?", (wording, wording)
        )
        if not rows:
            return None
        r = rows[0]
        return r["backward"] if r["forward"] == wording else r["forward"]

    async def _mirror(self, row: dict[str, Any]) -> dict[str, Any] | None:
        """The other half of a task link: on the linked task, pointing back, with the inverse wording."""
        inverse = await self._inverse(row["relation"])
        if inverse is None:
            return None
        rows = await self.db.read(
            "SELECT * FROM task_refs WHERE task_id = ? AND kind = 'task' AND href = ? AND relation = ?",
            (row["href"], row["task_id"], inverse),
        )
        return dict(rows[0]) if rows else None

    async def _unlink_orphans(self, hrefs: Any) -> None:
        """Remove files under the files directory that no reference names any more."""
        if self.files_dir is None:
            return
        for href in set(hrefs):
            if not href.startswith(FILES_ROUTE):
                continue
            still = await self.db.read(
                "SELECT 1 FROM task_refs WHERE kind = 'file' AND href = ?", (href,)
            )
            if still:
                continue
            name = href[len(FILES_ROUTE):]
            if "/" in name or name in ("", ".", ".."):
                continue
            path = self.files_dir / name
            try:
                await asyncio.to_thread(os.unlink, path)
            except FileNotFoundError:
                pass

    # ── Lookups ───────────────────────────────────────────────────────

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

    async def _task(self, task_id: Any) -> dict[str, Any] | None:
        if not isinstance(task_id, str) or not task_id:
            return None
        rows = await self.db.read("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        return dict(rows[0]) if rows else None

    async def _exists(self, task_id: Any) -> bool:
        return await self._task(task_id) is not None

    async def _ref(self, ref_id: Any) -> dict[str, Any] | None:
        rows = await self.db.read("SELECT * FROM task_refs WHERE ref_id = ?", (ref_id,))
        return dict(rows[0]) if rows else None

    async def _error(self, ws: WebSocketResponse, msg: dict[str, Any], text: str) -> None:
        resp = make_error(msg.get("ref"), text, txnid=msg.get("txnid"))
        await ws.send_bytes(resp)
        await self.notify_monitors("out", resp)
