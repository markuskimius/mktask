"""The `tasks` transaction service: Task ID assignment, cascades, references.

mkio's transaction service runs the ops declared in mktask.toml verbatim.
This subclass keeps those ops as the single description of what a request
writes and adds what a config cannot express:

- `add` and `split` need a Task ID: "TK", a two-letter prefix from the
  username, and a decimal sequence number padded to at least eight digits
  (TKMA00000001). The number comes from an in-memory counter seeded from the
  `counters` table at startup and written back in the same transaction as
  the insert, so a crash can neither reuse nor skip a number.
- `move` re-parents one task, its subtree coming along: it refuses a cycle
  and a move that would leave a task link inside one tree, and reopens the
  new ancestors when open work lands under a complete parent.
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
- The Assigned To dropdown is a list of its own (`assignees`), managed by
  `add_assignee` / `edit_assignee` / `delete_assignee` — and grown by
  `add`, `split` and `edit`, which add a name typed into the picker that
  the list does not hold yet. Nothing here reaches into `tasks`: a task
  keeps the name it was given whatever later becomes of the list.
- Every op also writes the narrative to `task_events`: what happened, to
  which task, in the same transaction as the change itself.

`tasks` and `task_refs` are `versioned = true`, so mkio records every
version of every row and its `undo`/`redo` ops step one row along its own
chain. That leaves two things to an application:

- `undo_action` / `redo_action` step *every row one user action wrote*, not
  just the one the user has selected. mkio stamps each row it writes with
  the transaction's `_mkio_ref`, so the group is a lookup, not bookkeeping:
  a link's two mirrored rows, a subtree completion and a move with its
  reopened ancestors all step together, under three guards.
- `undo_redo_hook` is what mkio's `on_undo_redo` calls after a cursor
  moves. It writes the `undone` / `redone` event and re-labels any link
  left pointing at a stale title, which a cursor move would otherwise
  bypass — `_op_edit` is not on that path.

A task delete stays outside all of it: mkio drops a deleted row's history
with the row, so a delete is permanent. `_op_delete` makes that thorough,
taking the subtree's events and any undone reference chain with it.
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
#: The versioned tables, each with its key column and its cursor-move ops.
#: mkio records every version of these in `<table>__history`; everything
#: undo/redo does is driven off this map.
_VERSIONED = {
    "tasks": ("task_id", "undo_task", "redo_task"),
    "task_refs": ("ref_id", "undo_ref", "redo_ref"),
}
TOP_LEVEL = "__top__"  # the Move picker's "top level"; see move_options in mktask.toml
#: The Assigned To picker's one sentinel: "let me type a name". Unassigned
#: needs none — it is mkui's own blank entry, which submits as ''. The
#: dialog maps this before it submits; the service maps it again, so the
#: sentinel can never reach the column.
NEW_ASSIGNEE = "__new__"
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


#: `moved` records where the task went; this is what it records for the top
#: level, which has no Task ID to name.
TOP_LEVEL_DETAIL = "top level"

#: What adding a reference is called, by kind. The point of `last_event` is
#: to read as a sentence in the blotter, so a file is "attached" and a task
#: is "linked" rather than both being "a reference added".
_REF_ADDED = {
    "file": "Attached a file", "url": "Added a URL",
    "text": "Added a snippet", "task": "Linked",
}


def event_phrase(action: str, detail: str = "", kind: str = "") -> str:
    """The one line a task's Last Event shows for an event.

    The same phrase goes into `tasks.last_event` and, because `tasks` is
    versioned, into every recorded version — so a task's history says what
    each version was *about*, which `_mkio_op` ("insert", "update") cannot.
    """
    def named(head: str) -> str:
        return f"{head}: {detail}" if detail else head

    if action == "created":
        return "Created"
    if action == "split_from":
        return f"Split from {detail}" if detail else "Split from another task"
    if action == "split_to":
        return f"Split into {detail}" if detail else "Split into a new task"
    if action == "edited":
        return "Edited"
    if action == "moved":
        return "Moved to the top level" if detail in ("", TOP_LEVEL_DETAIL) else f"Moved under {detail}"
    if action == "completed":
        return "Completed"
    if action == "reopened":
        return "Reopened"
    if action == "ref_added":
        return named(_REF_ADDED.get(kind, "Added a reference"))
    if action == "ref_edited":
        return named("Edited a reference")
    if action == "ref_deleted":
        return named("Removed a reference")
    return named(action.replace("_", " ").capitalize())


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

    NEW_OPS = ("add", "split")
    CASCADE_OPS = ("complete", "reopen", "delete")
    TREE_OPS = ("edit", "move")
    REF_OPS = ("add_ref", "edit_ref", "delete_ref")
    RELATION_OPS = ("add_relation", "edit_relation", "delete_relation")
    ASSIGNEE_OPS = ("add_assignee", "edit_assignee", "delete_assignee")
    STEP_OPS = ("undo_action", "redo_action")

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.prefix = self.config.get("prefix") or user_prefix(getpass.getuser())
        self.user = self.config.get("user") or getpass.getuser()
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

        if op in self.NEW_OPS:
            if data.get("task_id"):
                return await self._error(ws, msg, "Task ID is assigned by the server")
            if op == "split" and not await self._exists(data.get("parent_task_id")):
                return await self._error(ws, msg, f"Cannot split: no task {data.get('parent_task_id')!r}")
            task_id, number = self.next_task_id()
            data = {**data, "task_id": task_id, "last": number}
            return await self._guarded(ws, {**msg, "data": data}, op, data)

        if (op in self.CASCADE_OPS or op in self.REF_OPS or op in self.RELATION_OPS
                or op in self.ASSIGNEE_OPS or op in self.TREE_OPS or op in self.STEP_OPS):
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

    def _event(
        self, task_id: str, action: str, detail: Any = "", ref_id: Any = 0, kind: str = "",
    ) -> dict[str, Any]:
        """One `task_events` row: what happened, to which task.

        It carries `last_event` too — the same event as one readable line.
        `add_event` ignores the extra key (mkio takes only a step's declared
        fields), and `touch` takes the row as it stands, so an event and the
        Last Event it sets are never written from two different places.
        """
        return {
            "task_id": task_id, "action": action, "detail": str(detail or ""),
            "ref_id": int(ref_id or 0), "actor": self.user,
            "last_event": event_phrase(action, str(detail or ""), kind),
        }

    def _event_steps(self, events: Any) -> tuple[tuple, tuple]:
        """The `add_event` steps for a list of `_event` rows."""
        rows = list(events)
        return self._steps("add_event", rows) if rows else ((), ())

    def _touch_steps(self, events: Any, wrote: Any) -> tuple[tuple, tuple]:
        """Steps setting Last Event on the tasks this op does not itself write.

        An op that writes the task row carries `last_event` as one of its own
        fields — a second update in the same transaction would record a
        second version of the task. `wrote` names those, so what is left is
        the tasks a reference change concerns: the owner, and both ends of a
        link. Touching them is what puts a reference event in the task's
        recorded history. One touch per task, the last event winning.
        """
        pending = {e["task_id"]: e for e in events if e["task_id"] not in wrote}
        return self._steps("touch", list(pending.values())) if pending else ((), ())

    async def _submit(
        self, op: str, rows: list[dict[str, Any]], data: dict[str, Any], ref: str | None,
        events: Any = (), wrote: Any = (), extra: tuple = ((), ()),
    ) -> dict[str, Any]:
        """One transaction over every row, plus its narrative. Right for
        inserts and updates, whose RETURNING rows are what the change bus
        announces.

        `extra` is a second op's steps riding along in the same transaction
        — the `add_assignee` a newly typed name needs.
        """
        ops, params = self._steps(op, rows)
        x_ops, x_params = extra
        e_ops, e_params = self._event_steps(events)
        t_ops, t_params = self._touch_steps(events, wrote)
        return await self.writer.submit(
            ops + tuple(x_ops) + t_ops + e_ops,
            params + tuple(x_params) + t_params + e_params, data, ref=ref
        )

    async def _submit_each(
        self, op: str, rows: list[dict[str, Any]], ref: str | None, events: Any = (),
        wrote: Any = (),
    ) -> dict[str, Any]:
        """One request per row, queued together so they land in one batch.
        Right for deletes: the writer announces a delete with the request's
        data, so each row needs its own request to be announced by its key.

        The events ride on the first request — one batch, one commit, and
        which request carries them makes no difference to what lands.
        """
        e_ops, e_params = self._event_steps(events)
        t_ops, t_params = self._touch_steps(events, wrote)
        e_ops, e_params = t_ops + e_ops, t_params + e_params
        if not rows:
            if not e_ops:
                return {"ok": True}
            return await self.writer.submit(e_ops, e_params, {}, ref=ref)
        submits = []
        for i, row in enumerate(rows):
            ops, params = self._steps(op, [row])
            if i == 0:
                ops, params = ops + e_ops, params + e_params
            submits.append(self.writer.submit(ops, params, row, ref=ref))
        results = await asyncio.gather(*submits)
        return results[-1] if results else {"ok": True}

    # ── Task cascades ────────────────────────────────────────────────

    def _rows_for(self, events: list[dict[str, Any]], base: dict[str, Any]) -> list[dict[str, Any]]:
        """One row per event, carrying that task's key and its Last Event.

        The op writes the task row itself, so `last_event` rides along as one
        of its fields rather than as a separate touch — which would record a
        second version of the same task in the same transaction.
        """
        return [{**base, "task_id": e["task_id"], "last_event": e["last_event"]} for e in events]

    async def _op_add(self, data: dict[str, Any], ref: str | None) -> dict[str, Any]:
        data, *assignee = await self._assignee_steps(data)
        events = [self._event(data["task_id"], "created", data.get("title", ""))]
        return await self._submit(
            "add", self._rows_for(events, data), data, ref,
            events=events, wrote={data["task_id"]}, extra=tuple(assignee),
        )

    async def _op_split(self, data: dict[str, Any], ref: str | None) -> dict[str, Any]:
        """The new task records where it came from; the parent, what came of it.

        The parent's row is not otherwise written, so its half is a touch —
        which is how a split shows up in the parent's own history too.
        """
        # The parent's name is what the picker opens on, so inheriting it is
        # not typing it: a name taken off the list stays off when a task
        # carrying it is split.
        was = await self._task(data["parent_task_id"])
        data, *assignee = await self._assignee_steps(
            data, previous=(was or {}).get("assigned_to", "")
        )
        task_id, parent = data["task_id"], data["parent_task_id"]
        events = [
            self._event(task_id, "split_from", parent),
            self._event(parent, "split_to", data.get("title", "")),
        ]
        return await self._submit(
            "split", self._rows_for(events[:1], data), data, ref,
            events=events, wrote={task_id}, extra=tuple(assignee),
        )

    async def _op_complete(self, data: dict[str, Any], ref: str | None) -> dict[str, Any]:
        keys = await self._subtree(data["task_id"], status="open") or [data["task_id"]]
        events = [self._event(k, "completed") for k in keys]
        return await self._submit(
            "complete", self._rows_for(events, data), data, ref,
            events=events, wrote=set(keys),
        )

    async def _op_reopen(self, data: dict[str, Any], ref: str | None) -> dict[str, Any]:
        keys = await self._ancestors_and_self(data["task_id"], status="complete") or [data["task_id"]]
        events = [self._event(k, "reopened") for k in keys]
        return await self._submit(
            "reopen", self._rows_for(events, data), data, ref,
            events=events, wrote=set(keys),
        )

    async def _op_delete(self, data: dict[str, Any], ref: str | None) -> dict[str, Any]:
        """Delete the task, everything split from it, and every trace of both.

        Permanent, and the one thing in mktask that is: mkio drops a
        versioned row's history when the row is deleted, so there is nothing
        left to step back onto. That is the whole reason this goes to some
        length — a half-deleted task would leave a snippet's text, or a
        file, reachable through history for a task that no longer exists.

        It takes, in order: any reference of the subtree left as history by
        an undo (resurrected first, so deleting it drops its chain the same
        way), every reference the subtree owns *or that points at it* from a
        surviving task, the tasks themselves, and the subtree's events. The
        files go last, once nothing names them.
        """
        keys = await self._subtree(data["task_id"], status=None) or [data["task_id"]]
        marks = ", ".join("?" for _ in keys)
        scope = (
            f"task_id IN ({marks}) OR (kind = 'task' AND href IN ({marks}))"
        )  # the same reach for the live rows and for the recorded versions

        # A reference undone but not redone has no row, only a chain. Step it
        # forward so it is a row again: mkio rebuilds version 1 from history,
        # and the delete below then drops the whole chain with it.
        undone = await self.db.read(
            f"SELECT DISTINCT ref_id FROM task_refs__history h WHERE ({scope}) "
            f"AND NOT EXISTS (SELECT 1 FROM task_refs r WHERE r.ref_id = h.ref_id)",
            (*keys, *keys),
        )
        if undone:
            await self._submit_each("redo_ref", [{"ref_id": r["ref_id"]} for r in undone], ref)

        refs = await self.db.read(
            f"SELECT ref_id, task_id, kind, href, label FROM task_refs WHERE {scope}",
            (*keys, *keys),
        )
        gone = set(keys)
        if refs:
            # A link removed from a task that survives is a change to that
            # task; one removed from a task being deleted is not worth saying.
            await self._submit_each(
                "delete_ref", [{"ref_id": r["ref_id"]} for r in refs], ref,
                events=[
                    self._event(r["task_id"], "ref_deleted", r["label"], r["ref_id"], kind=r["kind"])
                    for r in refs if r["task_id"] not in gone
                ],
            )
        result = await self._submit_each("delete", [{**data, "task_id": k} for k in keys], ref)

        events = await self.db.read(
            f"SELECT event_id FROM task_events WHERE task_id IN ({marks})", tuple(keys)
        )
        if events:
            await self._submit_each("delete_event", [{"event_id": e["event_id"]} for e in events], ref)
        await self._unlink_orphans(r["href"] for r in refs if r["kind"] == "file")
        return result

    async def _op_edit(self, data: dict[str, Any], ref: str | None) -> dict[str, Any]:
        """A plain update, plus a relabel of every link that points at this task."""
        was = await self._task(data["task_id"])
        data, a_ops, a_params = await self._assignee_steps(
            data, previous=(was or {}).get("assigned_to", "")
        )
        event = self._event(data["task_id"], "edited", data.get("title", ""))
        compiled = self._resolve_ops({"op": "edit"})
        ops = list(compiled)
        params = [_extract_params(step, {**data, "last_event": event["last_event"]})
                  for step in compiled]
        ops.extend(a_ops)
        params.extend(a_params)
        if "title" in data:
            r_ops, r_params = await self._relabel_steps(data["task_id"], data["title"])
            ops.extend(r_ops)
            params.extend(r_params)
        e_ops, e_params = self._event_steps([event])
        return await self.writer.submit(
            tuple(ops) + e_ops, tuple(params) + e_params, data, ref=ref
        )

    async def _relabel_steps(self, task_id: str, title: str) -> tuple[tuple, tuple]:
        """Steps setting every link that points at `task_id` to its new title.

        Only the rows that disagree: a relabel is an ordinary write on a
        versioned table, so a no-op one would still spend a version and
        discard that reference's redo branch. It also makes the operation
        idempotent, which is what lets the undo/redo hook re-run it blindly.
        """
        links = await self.db.read(
            "SELECT ref_id FROM task_refs WHERE kind = 'task' AND href = ? AND label != ?",
            (task_id, title),
        )
        return self._steps("relabel_ref", [
            {"ref_id": r["ref_id"], "label": title} for r in links
        ]) if links else ((), ())

    async def _op_move(self, data: dict[str, Any], ref: str | None) -> dict[str, Any]:
        """Re-parent one task; every task split from it comes along.

        `parent_task_id = ""` — or `TOP_LEVEL`, which is what the picker
        sends — moves it to the top level. Only the moved task's own row
        changes: a Task ID says nothing about where the task sits, so
        nothing else has to be rewritten.
        """
        task_id = data["task_id"]
        parent = str(data.get("parent_task_id") or "")
        if parent == TOP_LEVEL:
            parent = ""
        if await self._task(task_id) is None:
            raise ValueError(f"Cannot move: no task {task_id!r}")

        reopen: list[str] = []
        if parent:
            if parent == task_id:
                raise ValueError("Cannot move: a task cannot be split from itself")
            if await self._task(parent) is None:
                raise ValueError(f"Cannot move: no task {parent!r}")
            moved = await self._subtree(task_id, status=None)
            if parent in moved:
                raise ValueError(f"Cannot move: {parent} was split from {task_id}")
            await self._check_no_links_merge(moved, parent)
            if await self._subtree(task_id, status="open"):
                # Open work may not hang under a complete parent, the same
                # rule `reopen` keeps when a child is reopened.
                reopen = await self._ancestors_and_self(parent, status="complete")

        moved = self._event(task_id, "moved", parent or TOP_LEVEL_DETAIL)
        compiled = self._resolve_ops({"op": "move"})
        ops = list(compiled)
        params = [_extract_params(
            step, {**data, "parent_task_id": parent, "last_event": moved["last_event"]}
        ) for step in compiled]
        events = [moved]
        if reopen:
            reopened = [self._event(k, "reopened") for k in reopen]
            r_ops, r_params = self._steps("reopen", self._rows_for(reopened, data))
            ops.extend(r_ops)
            params.extend(r_params)
            events.extend(reopened)
        e_ops, e_params = self._event_steps(events)
        return await self.writer.submit(
            tuple(ops) + e_ops, tuple(params) + e_params, data, ref=ref
        )

    async def _check_no_links_merge(self, moved: list[str], parent: str) -> None:
        """Refuse a move that would leave a task link inside one tree.

        `add_ref` forbids linking two tasks of the same tree, because
        splitting already relates them. A move joins the moved subtree to
        the new parent's tree, so a link across that seam would become one
        of those — silently. Refusing says which links are in the way; the
        user unlinks them and moves again.
        """
        root = (await self._ancestors_and_self(parent, status=None))[-1]
        dest = await self._subtree(root, status=None)
        rows = await self.db.read(
            "SELECT task_id, relation, href FROM task_refs WHERE kind = 'task'"
            f" AND task_id IN ({', '.join('?' for _ in moved)})"
            f" AND href IN ({', '.join('?' for _ in dest)})",
            (*moved, *dest),
        )
        if not rows:
            return
        # Short on purpose: mkui clips a dialog's status line to one row.
        n, r = len(rows), rows[0]
        s = "s" if n != 1 else ""
        raise ValueError(
            f"Cannot move: {r['task_id']} {r['relation']} {r['href']} ({n} link{s}). Unlink first."
        )

    # ── Undo and redo, by user action ─────────────────────────────────

    async def _op_undo_action(self, data: dict[str, Any], ref: str | None) -> dict[str, Any]:
        return await self._step_action("undo", data, ref)

    async def _op_redo_action(self, data: dict[str, Any], ref: str | None) -> dict[str, Any]:
        return await self._step_action("redo", data, ref)

    def _subject(self, data: dict[str, Any]) -> tuple[str, str, Any]:
        """The record a step was asked for: mkui sends its `history.key`.

        A reference's key is checked first — the References pane's rows carry
        a `task_id` too, and there "undo" means this reference, not its task.
        """
        if data.get("ref_id") not in (None, ""):
            return "task_refs", "ref_id", data["ref_id"]
        if data.get("task_id"):
            return "tasks", "task_id", data["task_id"]
        raise KeyError("'task_id'")

    async def _step_action(self, direction: str, data: dict[str, Any], ref: str | None) -> dict[str, Any]:
        """Step every row one user action wrote, not just the one selected.

        mkio's undo moves one row's cursor, but a mktask action is rarely
        one row: a link is two mirrored rows, completing a task completes
        its subtree, a move reopens the ancestors it lands under. Undoing
        one row of those would leave the rest behind.

        The group needs no bookkeeping of ours — mkio stamps every row a
        transaction writes with that transaction's `_mkio_ref`, and indexes
        it. So: find the ref that wrote the version this record is stepping
        off, take every row that ref wrote, check the three ways a step
        could leave the data wrong, and step them all.

        The steps go one request per row, because mkio reads an undo's key
        from the request's own data; they are queued together, so they land
        in one batch and one commit.
        """
        table, key_col, key = self._subject(data)
        group_ref = await self._group_ref(direction, table, key_col, key)
        group = await self._group_rows(group_ref)
        await self._check_step(direction, group)

        submits = []
        for tbl, rows in group.items():
            col, op = _VERSIONED[tbl][0], _VERSIONED[tbl][1 if direction == "undo" else 2]
            for row in rows:
                one = {col: row[col]}
                submits.append(self.writer.submit(*self._steps(op, [one]), one, ref=ref))
        results = await asyncio.gather(*submits)
        return {**(results[-1] if results else {"ok": True}), "stepped": sum(len(r) for r in group.values())}

    async def _group_ref(self, direction: str, table: str, key_col: str, key: Any) -> str:
        """The transaction ref of the action this record would step onto.

        Undoing steps off the version the row is on; redoing steps onto the
        one above it — and onto version 1 when there is no row at all, which
        is where an undone-out-of-existence record comes back from.
        """
        hist = f"{table}__history"
        if direction == "undo":
            rows = await self.db.read(
                f"SELECT _mkio_ref FROM {hist} WHERE {key_col} = ? AND _mkio_version = "
                f"(SELECT _mkio_version FROM {table} WHERE {key_col} = ?)",
                (key, key),
            )
            if not rows:
                raise ValueError("Nothing to undo")
        else:
            rows = await self.db.read(
                f"SELECT _mkio_ref FROM {hist} WHERE {key_col} = ? AND _mkio_version = "
                f"COALESCE((SELECT _mkio_version FROM {table} WHERE {key_col} = ?), 0) + 1",
                (key, key),
            )
            if not rows:
                raise ValueError("Nothing to redo")
        return rows[0]["_mkio_ref"]

    async def _group_rows(self, group_ref: str) -> dict[str, list[dict[str, Any]]]:
        """Every recorded version that transaction wrote, by table."""
        out: dict[str, list[dict[str, Any]]] = {}
        for table in _VERSIONED:
            rows = await self.db.read(
                f"SELECT * FROM {table}__history WHERE _mkio_ref = ? ORDER BY _mkio_version",
                (group_ref,),
            )
            if rows:
                out[table] = [dict(r) for r in rows]
        return out

    async def _check_step(self, direction: str, group: dict[str, list[dict[str, Any]]]) -> None:
        """Refuse a step that would land on something other than what it left.

        Three ways it could: the data moved on since (someone edited a row
        the action wrote), the step would strand a child or a reference
        under a task it is about to remove, or it would restore a row whose
        parent or linked task is no longer there.
        """
        for table, rows in group.items():
            key_col = _VERSIONED[table][0]
            for row in rows:
                key, version = row[key_col], row["_mkio_version"]
                live = await self.db.read(
                    f"SELECT _mkio_version FROM {table} WHERE {key_col} = ?", (key,)
                )
                at = live[0]["_mkio_version"] if live else 0
                want = version if direction == "undo" else version - 1
                if at != want:
                    raise ValueError(
                        f"Cannot {direction}: {key} has changed since (v{at}, expected v{want})"
                    )
                if direction == "undo" and version == 1:
                    await self._check_removable(table, key)
                else:
                    dest = version - 1 if direction == "undo" else version
                    await self._check_restorable(table, key, dest, row)

    async def _check_removable(self, table: str, key: Any) -> None:
        """An undo at version 1 removes the row: refuse to strand anything."""
        if table != "tasks":
            return
        kids = await self.db.read(
            "SELECT task_id FROM tasks WHERE parent_task_id = ? LIMIT 1", (key,)
        )
        if kids:
            raise ValueError(f"Cannot undo: {key} still has {kids[0]['task_id']} split from it")
        refs = await self.db.read("SELECT ref_id FROM task_refs WHERE task_id = ? LIMIT 1", (key,))
        if refs:
            raise ValueError(f"Cannot undo: {key} still has references")

    async def _check_restorable(self, table: str, key: Any, version: int, row: dict[str, Any]) -> None:
        """The version being stepped onto must still make sense.

        A task's parent and a link's other end can have been deleted since
        the version was recorded, and a delete is permanent — so the step
        would restore a row pointing at nothing.
        """
        rows = await self.db.read(
            f"SELECT * FROM {table}__history WHERE "
            f"{_VERSIONED[table][0]} = ? AND _mkio_version = ?",
            (key, version),
        )
        if not rows:
            return
        dest = dict(rows[0])
        if table == "tasks":
            parent = dest.get("parent_task_id") or ""
            if parent and not await self._exists(parent):
                raise ValueError(f"Cannot restore {key}: {parent} no longer exists")
        elif dest.get("kind") == "task":
            for end in (dest.get("task_id"), dest.get("href")):
                if end and not await self._exists(end):
                    raise ValueError(f"Cannot restore link: {end} no longer exists")

    # ── What an undo left behind ──────────────────────────────────────

    async def undo_redo_hook(self, event: Any) -> None:
        """mkio's `on_undo_redo`: put right what the cursor move did not.

        Moving a row's cursor restores the row, but not what followed from
        the write it reverses. Two things followed here, and neither is on
        the path a cursor move takes:

        - A link shows the title of the task it points at, refreshed by
          `_op_edit`. A step that changes a title leaves those stale.
        - The narrative. `undone` / `redone` belong in it as much as the
          write they reverse does.

        Only a `tasks` step is narrated: a reference's own steps are its
        version history's to tell, and narrating them here would put a
        "redone" in a task's activity for the resurrection `_op_delete`
        does on its way to deleting a reference for good.
        """
        if event.cause not in ("undo", "redo") or event.table != "tasks":
            return
        if event.new is None:
            return  # undone out of existence: nothing left to narrate it against
        old, new = event.old or {}, event.new
        task_id = new.get("task_id") or old.get("task_id")
        if not task_id:
            return
        title = new.get("title")
        ops: tuple = ()
        params: tuple = ()
        if title is not None and title != old.get("title"):
            # Idempotent, so running it after a group step that already
            # carried the link rows along costs nothing.
            ops, params = await self._relabel_steps(task_id, title)
        e_ops, e_params = self._event_steps([
            self._event(task_id, "undone" if event.cause == "undo" else "redone",
                        title or old.get("title", "")),
        ])
        await self.writer.submit(ops + e_ops, params + e_params, {"task_id": task_id})

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
            # A link is a change to both tasks, so both say so. The events
            # name no ref_id: the row's is assigned by SQLite as it is
            # inserted, and nothing hands it back within the transaction.
            return await self._submit("add_ref", rows, data, ref, events=[
                self._event(task_id, "ref_added", f"{relation} {href}", kind=kind),
                self._event(href, "ref_added", f"{inverse} {task_id}", kind=kind),
            ])

        if kind == "url" and not href:
            raise ValueError("A URL reference needs a URL")
        if kind == "text" and not body.strip():
            raise ValueError("A text reference needs some text")
        if kind == "file" and not href.startswith(FILES_ROUTE):
            raise ValueError("A file reference must point under /files/")
        row = {**data, "kind": kind, "relation": "", "href": href if kind != "text" else "",
               "body": body if kind == "text" else "", "label": label or default_label(kind, href, body)}
        return await self._submit("add_ref", [row], data, ref, events=[
            self._event(task_id, "ref_added", row["label"], kind=kind),
        ])

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
            events = [self._event(row["task_id"], "ref_edited", f"{relation} {row['href']}",
                                  row["ref_id"], kind=row["kind"])]
            if mirror:
                rows.append({**mirror, "relation": inverse, "updated_at": data.get("updated_at", mirror["updated_at"])})
                events.append(self._event(
                    mirror["task_id"], "ref_edited", f"{inverse} {mirror['href']}",
                    mirror["ref_id"], kind=mirror["kind"],
                ))
            return await self._submit("edit_ref", rows, data, ref, events=events)
        href = str(data.get("href") or "").strip()
        body = str(data.get("body") or "")
        if row["kind"] == "file":
            href = row["href"]  # the file is what it is; the label is the editable part
        elif row["kind"] == "text":
            href = ""
        elif not href:
            raise ValueError("A URL reference needs a URL")
        label = str(data.get("label") or "").strip() or default_label(row["kind"], href, body)
        return await self._submit(
            "edit_ref", [{**data, "href": href, "body": body, "label": label, "relation": ""}], data, ref,
            events=[self._event(row["task_id"], "ref_edited", label, row["ref_id"], kind=row["kind"])],
        )

    async def _op_delete_ref(self, data: dict[str, Any], ref: str | None) -> dict[str, Any]:
        row = await self._ref(data["ref_id"])
        rows = [{"ref_id": data["ref_id"]}]
        events = []
        if row is not None:
            events.append(self._event(row["task_id"], "ref_deleted", row["label"],
                                      row["ref_id"], kind=row["kind"]))
        if row is not None and row["kind"] == "task":
            mirror = await self._mirror(row)
            if mirror:
                rows.append({"ref_id": mirror["ref_id"]})
                events.append(self._event(
                    mirror["task_id"], "ref_deleted", mirror["label"],
                    mirror["ref_id"], kind=mirror["kind"],
                ))
        result = await self._submit_each("delete_ref", rows, ref, events=events)
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

    # ── Assignees ─────────────────────────────────────────────────────

    async def _assignee_steps(
        self, data: dict[str, Any], previous: str | None = None,
    ) -> tuple[dict[str, Any], tuple, tuple]:
        """`data` with `assigned_to` settled, and the steps adding a name the
        dropdown does not hold yet.

        Typing a name into the Assigned To picker is what puts it in the
        dropdown, so a name the list does not know joins it in the same
        transaction as the task it was typed on. Only a name this write
        *changes*: a name deleted from the list stays on the tasks that
        carry it, and an edit of one of those — a new title, a new due date
        — must not put it back on the list behind the user's back. A name
        the list already holds needs nothing, and its spelling wins, so
        "alice" typed over "Alice" is the same person, not a second entry
        that reads the same.
        """
        if "assigned_to" not in data:
            return data, (), ()
        name = str(data.get("assigned_to") or "").strip()
        if name == NEW_ASSIGNEE:
            name = ""  # the dialog maps its own sentinel; this is the backstop
        listed = await self._assignee(name) if name else None
        if listed is not None:
            name = listed["name"]
        data = {**data, "assigned_to": name}
        if not name or listed is not None or name == (previous or ""):
            return data, (), ()
        return (data, *self._steps("add_assignee", [{"name": name, "notes": ""}]))

    async def _assignee(self, name: str) -> dict[str, Any] | None:
        """The row holding a name, matched case-insensitively; None if unlisted."""
        wanted = name.strip().lower()
        rows = await self.db.read("SELECT * FROM assignees")
        return next((dict(r) for r in rows if r["name"].lower() == wanted), None)

    async def _assignee_data(self, data: dict[str, Any], exclude_id: Any = None) -> dict[str, Any]:
        """A trimmed name, checked unique across the list, case-insensitively."""
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("An assignee needs a name")
        listed = await self._assignee(name)
        if listed is not None and listed["assignee_id"] != exclude_id:
            raise ValueError(f"{name!r} is already on the list ({listed['name']!r})")
        return {**data, "name": name, "notes": str(data.get("notes") or "")}

    async def _op_add_assignee(self, data: dict[str, Any], ref: str | None) -> dict[str, Any]:
        return await self._submit("add_assignee", [await self._assignee_data(data)], data, ref)

    async def _op_edit_assignee(self, data: dict[str, Any], ref: str | None) -> dict[str, Any]:
        """A rename changes the dropdown and nothing else.

        `tasks.assigned_to` holds the name itself, not a key into this
        table, so a task keeps the name it was given: the list is only what
        the picker offers next time. That is the same promise a delete
        makes, and it is why nothing here reaches into `tasks` the way
        `edit_relation` reaches into `task_refs`.
        """
        row = await self._assignee_row(data["assignee_id"])
        if row is None:
            raise ValueError(f"No assignee {data['assignee_id']!r}")
        new = await self._assignee_data(data, exclude_id=row["assignee_id"])
        return await self._submit("edit_assignee", [new], data, ref)

    async def _op_delete_assignee(self, data: dict[str, Any], ref: str | None) -> dict[str, Any]:
        """Off the list, and that is all: every task already assigned to the
        name keeps it, and the name simply stops being offered."""
        return await self._submit_each(
            "delete_assignee", [{"assignee_id": data["assignee_id"]}], ref
        )

    async def _assignee_row(self, assignee_id: Any) -> dict[str, Any] | None:
        rows = await self.db.read("SELECT * FROM assignees WHERE assignee_id = ?", (assignee_id,))
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
        """Remove files under the files directory that nothing names any more.

        Nothing means neither a live reference nor a recorded version of one:
        an undone file reference is a chain with no row, and redoing it must
        find its file still there. A delete is what empties both — mkio drops
        a deleted row's history with it, and `_op_delete` takes the chains
        an undo left behind — so a file outlives its last reference only for
        as long as something could still bring that reference back.
        """
        if self.files_dir is None:
            return
        for href in set(hrefs):
            if not href.startswith(FILES_ROUTE):
                continue
            still = await self.db.read(
                "SELECT 1 FROM task_refs WHERE kind = 'file' AND href = ? "
                "UNION ALL "
                "SELECT 1 FROM task_refs__history WHERE kind = 'file' AND href = ? LIMIT 1",
                (href, href),
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
