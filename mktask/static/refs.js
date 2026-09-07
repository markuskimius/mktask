// The `task-refs` widget: the whole Detail body — everything mkui's
// declarative pieces cannot do. A toolbar, the selected task's own fields,
// a drop box that accepts a pasted or dropped file, and the references of
// the selected task **and of every task split from it**: task links grouped
// by relation, then snippets, images, files, and URLs.
//
// The pane has one cursor, over the task block or over a reference, and a
// toolbar above it whose `Edit` opens the dialog that matches — the Tasks
// pane's Edit dialog for the task, the References pane's for a reference —
// while `Delete` is a reference's alone: a task is deleted from the blotter,
// where the row you are deleting is the row you picked. **The dialogs are
// borrowed from those panes by name** (`dialogs` in app.json), never copied,
// so the two places a task is edited cannot drift apart. mkio-table draws
// its toolbar itself and mkui has none for a widget pane, so this one is
// drawn here with mkui's own classes.
//
// One instance, in the Detail pane. It reads `state.selected_task` (published
// by the Tasks pane) and holds two live queries of its own — `all_tasks`, for
// the parent/child edges the subtree needs, and `task_refs` filtered to that
// subtree — so the pane answers "what does this task, and the work under it,
// refer to?" from the moment a task is selected. mkio-table is not the only
// thing allowed to subscribe. A reference a descendant owns carries that
// task's Task ID, so a line always says whose it is.
//
// The subtree is computed from the tasks, not from the Tasks pane's
// broadcast: the blotter's filter decides what the *blotter* shows, and a
// complete child's references are still this task's dossier.
//
// `state.selected_ref` (published by the References pane) only *marks* the
// matching line and opens it: a cursor in another pane must not decide what
// this pane is about, and a reference this pane does not list is not ours to
// mark.
//
// A line's body — an image, a snippet, a URL — opens on click, one at a time.
//
// "Go to" on a task link selects the linked task in the Tasks pane
// (`table.select`, mkui ≥ 0.2.23). Selecting it there is the whole job:
// mkui publishes the row exactly as a click does, so the Detail pane and
// the References pane's link filter follow on their own. A key the pane's
// filters hide comes back `hidden` rather than selected — the default
// filter shows open tasks only, so a complete linked task lands there —
// and `reveal` (config) then clears the status filter and tries again.

import { registerWidget, ensureMkio } from "/mkui/src/index.js";

const UPLOAD_URL = "/files";
const REFS_SERVICE = "task_refs";
const TASKS_SERVICE = "all_tasks";
const TASKS_SUBID = "task-refs-tasks";

// The sections below the task links, in the order they are shown: the
// snippets first, since what was written down about a task reads before what
// was filed with it. Images are files, but they are shown as a grid of
// thumbnails: a picture is its own label, and a wall of file names is not.
const SECTIONS = [["text", "Snippets"], ["image", "Images"], ["file", "Files"], ["url", "URLs"]];
const GRID_SECTIONS = new Set(["image"]);

// The task's own fields, under the title, in the order the block shows them.
// `score` is the blotter's derived column (`values.score` in app.json), not a
// stored one. A field whose value is empty is left out — a top-level task has
// no parent to name, most tasks have no due date — except those in ALWAYS,
// which say something by their value alone.
const TASK_FIELDS = [
  ["status", "Status"],
  ["importance", "Importance"],
  ["urgency", "Urgency"],
  ["score", "Score"],
  ["due", "Due"],
  ["parent_task_id", "Split from"],
  ["created_at", "Created"],
  ["notes", "Notes"],
];
const ALWAYS = new Set(["status", "importance", "urgency", "score"]);

const fieldValue = (task, name) =>
  name === "score" ? Number(task.importance) * Number(task.urgency) : task[name];

const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
};

const isUrl = (s) => /^(https?:|mailto:|message:|outlook:|ftp:)\S+$/i.test(s.trim()) && !/\s/.test(s.trim());

const stamp = () => new Date().toISOString().slice(0, 16).replace("T", " ");

/** A string literal for an mkio filter expression. */
const quote = (s) => "'" + String(s).replace(/\\/g, "\\\\").replace(/'/g, "\\'") + "'";

const capitalize = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);

function inTextField() {
  const a = document.activeElement;
  return !!a && (a.tagName === "INPUT" || a.tagName === "TEXTAREA" || a.isContentEditable);
}

/** POST bytes to /files; resolves { href, mime, size }. */
async function upload(blob, mime) {
  const res = await fetch(UPLOAD_URL, {
    method: "POST",
    headers: { "Content-Type": mime || blob.type || "application/octet-stream" },
    body: blob,
  });
  if (!res.ok) throw new Error(`upload failed: ${res.status} ${await res.text()}`);
  return res.json();
}

const isImage = (ref) => ref.kind === "file" && /^image\//.test(ref.mime || "");

/** The section a reference belongs to: its kind, images apart from files. */
const sectionOf = (ref) => (ref.kind === "file" ? (isImage(ref) ? "image" : "file") : ref.kind);

/** Does this reference have anything to show beyond its line or tile? */
const hasBody = (ref) =>
  ref.kind === "text"
  || (ref.kind === "url" && ref.label !== ref.href)   // the line already is the URL otherwise
  || isImage(ref);

/**
 * A reference row rendered as one line: the linked task, or the label, plus
 * the owner's Task ID when the reference belongs to a task split from the
 * selected one — a line must never leave the reader guessing whose it is.
 */
function renderRefLine(ref, { onGoTo, expanded, owner } = {}) {
  const line = el("div", "task-refs-line");
  const ownerTag = () => {
    if (!owner) return;
    const tag = el("span", "task-refs-owner", `from ${ref.task_id}`);
    tag.title = owner.title ? `${ref.task_id}  ${owner.title}` : ref.task_id;
    line.appendChild(tag);
  };
  if (ref.kind === "task") {
    line.appendChild(el("span", "task-refs-taskid", ref.href));
    line.appendChild(el("span", "task-refs-label", ref.label));
    ownerTag();
    if (onGoTo) {
      const go = el("button", "task-refs-goto", "Go to");
      go.type = "button";
      go.addEventListener("click", (ev) => { ev.stopPropagation(); onGoTo(ref.href); });
      line.appendChild(go);
    }
    return line;
  }
  line.appendChild(el("span", "task-refs-caret", hasBody(ref) ? (expanded ? "▾" : "▸") : ""));
  if (ref.href) {
    const a = el("a", "task-refs-label mkui-rich-link", ref.label || ref.href);
    a.href = ref.href;
    a.target = "_blank";
    a.rel = "noopener";
    a.addEventListener("click", (ev) => ev.stopPropagation());  // follow the link, don't toggle
    line.appendChild(a);
  } else {
    line.appendChild(el("span", "task-refs-label", ref.label));
  }
  ownerTag();
  return line;
}

/**
 * An image rendered as a thumbnail tile: the picture, its label, and — when a
 * task split from the selected one owns it — whose it is. The thumbnail is
 * the file itself, scaled by CSS: mktask stores no derived images, and a
 * screenshot is small enough that a second copy would cost more than it saves.
 */
function renderRefTile(ref, { owner } = {}) {
  const tile = el("button", "task-refs-tile");
  tile.type = "button";
  tile.title = owner ? `${ref.label}\n${ref.task_id}  ${owner.title ?? ""}`.trimEnd() : ref.label;
  const img = el("img", "task-refs-thumb");
  img.src = ref.href;
  img.alt = ref.label;
  img.loading = "lazy";
  img.decoding = "async";
  tile.appendChild(img);
  tile.appendChild(el("span", "task-refs-tile-label", ref.label));
  // A tile is too narrow for "from TKMA00000005": the Task ID under a "↳"
  // says the same, and the tooltip carries the task's title.
  tile.appendChild(el("span", "task-refs-tile-owner", owner ? `↳ ${ref.task_id}` : ""));
  return tile;
}

/** The body of a reference: an image, a snippet, or nothing more than its line. */
function renderRefBody(ref) {
  if (ref.kind === "file" && /^image\//.test(ref.mime || "")) {
    const a = el("a", "task-refs-image-link");
    a.href = ref.href; a.target = "_blank"; a.rel = "noopener"; a.title = "Open full size";
    const img = el("img", "task-refs-image");
    img.src = ref.href; img.alt = ref.label;
    a.appendChild(img);
    return a;
  }
  if (ref.kind === "text") return el("pre", "task-refs-body", ref.body);
  if (ref.kind === "url") return el("div", "task-refs-href", ref.href);
  return null;
}

/**
 * The references as the sections the pane shows: one per relation wording
 * (task links, alphabetically), then Snippets, Images, Files, and URLs. Inside a
 * section the selected task's own come first, then each descendant's
 * together, oldest first — so a new reference lands at the end of its own
 * task's run.
 */
function groupRefs(refs, ownerId) {
  const rows = [...refs].sort((a, b) =>
    (a.task_id === ownerId ? 0 : 1) - (b.task_id === ownerId ? 0 : 1)
    || String(a.task_id).localeCompare(String(b.task_id))
    || String(a.created_at).localeCompare(String(b.created_at))
    || a.ref_id - b.ref_id);
  const links = new Map();
  const bySection = new Map();
  for (const ref of rows) {
    const [map, key] = ref.kind === "task" ? [links, ref.relation || "links to"] : [bySection, sectionOf(ref)];
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(ref);
  }
  const groups = [...links.keys()].sort().map((rel) => ({ title: capitalize(rel), refs: links.get(rel) }));
  for (const [section, title] of SECTIONS) {
    if (bySection.has(section)) {
      groups.push({ title, refs: bySection.get(section), grid: GRID_SECTIONS.has(section) });
    }
  }
  return groups;
}

registerWidget("task-refs", (spec, app, host) => {
  const root = el("div", `task-refs ${spec.class ?? ""}`.trim());
  host.appendChild(root);
  const clientP = ensureMkio(app.config.mkio.url);

  const tasksPane = spec.tasksPane ?? "tasks";

  const select = (taskId) =>
    app.fireAction("table.select", { pane: tasksPane, keys: [taskId] });

  const goTo = (taskId) => {
    let result = select(taskId);
    let revealed = false;
    if (result?.hidden?.length && spec.reveal) {
      // Opt-in: drop the status filter (the usual reason a linked task is
      // out of view) and try once more. Other filters are left alone.
      app.fireAction("table.filter", { pane: tasksPane, filters: { status: null }, merge: true });
      result = select(taskId);
      revealed = !!result?.selected?.length;
    }
    if (result?.selected?.length) {
      return setStatus(revealed ? `Selected ${taskId} (showing all tasks)` : `Selected ${taskId}`);
    }
    if (result?.hidden?.length) {
      return setStatus(`${taskId} is hidden by a filter (Tasks › Show All)`, true);
    }
    if (result?.missing?.length) return setStatus(`No task ${taskId}`, true);
    setStatus(`Cannot open ${taskId}`, true);
  };

  let statusEl = null;
  const setStatus = (text, isError = false) => {
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.classList.toggle("task-refs-error", isError);
  };

  // ── The drop box and the task's references ──────────────────────────

  const box = el("div", "task-refs-dropbox");
  const boxText = el("span", "task-refs-dropbox-text");
  const input = el("input");
  input.type = "file"; input.multiple = true; input.hidden = true;
  box.append(boxText, input);
  statusEl = el("div", "task-refs-status");
  const list = el("div", "task-refs-list");
  const taskEl = el("div", "task-refs-task");
  const toolbar = el("div", "mkui-table-toolbar task-refs-toolbar");
  const main = el("div", "task-refs-main");
  main.append(taskEl, box, statusEl, list);
  root.append(toolbar, main);

  let task = null;     // state.selected_task's row, what the block shows

  // The pane's cursor: the task block, or one reference. `null` until a task
  // is selected. Clicking the block takes the cursor back from a reference.
  let cursor = null;   // { kind: "task" } | { kind: "ref", ref_id }
  const setCursor = (next) => { cursor = next; render(); };
  taskEl.addEventListener("click", () => setCursor(task ? { kind: "task" } : null));

  const addRef = async (data) => {
    if (!task) { setStatus("Select a task first.", true); return; }
    const client = await clientP;
    const resp = await client.send("tasks", { task_id: task.task_id, ...data }, { op: "add_ref" });
    if (resp?.type === "error") throw new Error(resp.message);
    setStatus(`Added ${data.kind} reference to ${task.task_id}`);
  };

  const addFile = async (blob, name) => {
    const mime = blob.type || "application/octet-stream";
    setStatus(`Uploading ${name ?? mime}…`);
    const up = await upload(blob, mime);
    const label = name && !/^image\.(png|jpe?g|gif|webp)$/i.test(name) ? name
      : `Screenshot ${stamp()}`;
    await addRef({ kind: "file", href: up.href, mime: up.mime, label });
  };

  const addText = async (text) => {
    const t = text.trim();
    if (!t) return;
    if (isUrl(t)) await addRef({ kind: "url", href: t });
    else await addRef({ kind: "text", body: text });
  };

  const guarded = (fn) => async (...args) => {
    try { await fn(...args); } catch (e) { setStatus(String(e.message ?? e), true); }
  };

  /** The button `spec.dialogs[name]` points at, on another pane: its dialog
   *  is opened here, and its `style` is what paints Delete red. */
  const borrowedButton = (name) => {
    const src = spec.dialogs?.[name];
    const pane = app.config.panes?.[src?.pane];
    return (pane?.buttons ?? []).find((b) => b.label === src?.button) ?? null;
  };

  /** Open a borrowed dialog over one row. mkui does the rest — the fields,
   *  the validation, the submit, and the server's refusal in its footer. */
  const openBorrowed = async (name, row) => {
    const dialog = borrowedButton(name)?.action?.dialog;
    if (!dialog) throw new Error(`No ${name} dialog in app.json`);
    if (!client) throw new Error("Not connected");
    const { openDialog } = await import("/mkui/src/widgets/mkui-dialog.js");
    await openDialog(dialog, {
      row, rows: [row],
      selection: { count: 1, rowCount: 1, unit: "row" },
      state: app.state.get(),
    }, app, { client });
  };

  /** The reference the cursor is on, if it still exists. */
  const cursorRef = () => (cursor?.kind === "ref" ? refs.get(cursor.ref_id) ?? null : null);

  const editCursor = guarded(async () => {
    const ref = cursorRef();
    if (ref) return openBorrowed("editRef", ref);
    if (task) return openBorrowed("editTask", task);
    setStatus("Select a task first.", true);
  });

  const deleteCursor = guarded(async () => {
    const ref = cursorRef();
    if (ref) await openBorrowed("deleteRef", ref);   // the button is off otherwise
  });

  const toolbarBtn = (label, onClick) => {
    const btn = el("button", "mkui-btn mkui-toolbar-btn", label);
    btn.type = "button";
    btn.addEventListener("click", onClick);
    toolbar.appendChild(btn);
    return btn;
  };
  const editBtn = toolbarBtn("Edit", editCursor);
  const deleteBtn = toolbarBtn("Delete", deleteCursor);

  // Delete wears the red the blotter and the References pane paint on theirs,
  // borrowed from the same button as its dialog: the `when = "enabled"` rule,
  // red only while the button is armed. Painted the way mkui paints a styled
  // button — a custom property behind a marker class — so hover and press
  // still register as a tint on it rather than replacing it.
  const armedStyle = (borrowedButton("deleteRef")?.style ?? [])
    .find((rule) => rule.when === "enabled") ?? null;

  /** Edit follows the cursor; Delete is a reference's alone. */
  function updateButtons() {
    const ref = cursorRef();
    editBtn.disabled = !ref && !task;
    deleteBtn.disabled = !ref;
    const armed = ref && armedStyle;
    deleteBtn.classList.toggle("mkui-btn-styled", !!armed);
    if (armed) {
      deleteBtn.style.setProperty("--mkui-btn-bg", armedStyle.background);
      deleteBtn.style.color = armedStyle.color ?? "";
    } else {
      deleteBtn.style.removeProperty("--mkui-btn-bg");
      deleteBtn.style.color = "";
    }
  }

  const acceptFiles = guarded(async (files) => {
    for (const f of files) await addFile(f, f.name);
  });

  box.addEventListener("click", () => { if (task) input.click(); });
  input.addEventListener("change", () => { acceptFiles([...input.files]); input.value = ""; });
  box.addEventListener("dragover", (ev) => { if (task) { ev.preventDefault(); box.classList.add("task-refs-dropbox-over"); } });
  box.addEventListener("dragleave", () => box.classList.remove("task-refs-dropbox-over"));
  box.addEventListener("drop", (ev) => {
    ev.preventDefault();
    box.classList.remove("task-refs-dropbox-over");
    if (!task) return;
    const files = [...(ev.dataTransfer?.files ?? [])];
    if (files.length) return acceptFiles(files);
    const text = ev.dataTransfer?.getData("text/uri-list") || ev.dataTransfer?.getData("text/plain");
    if (text) guarded(addText)(text);
  });

  // Paste anywhere while a task is selected and no text field has focus:
  // an image or file uploads, a URL becomes a url reference, text a snippet.
  window.addEventListener("paste", (ev) => {
    if (!task || inTextField() || !root.isConnected) return;
    const dt = ev.clipboardData;
    if (!dt) return;
    const files = [...(dt.files ?? [])];
    if (files.length) { ev.preventDefault(); return acceptFiles(files); }
    const text = dt.getData("text/plain");
    if (text && text.trim()) { ev.preventDefault(); guarded(addText)(text); }
  });

  // ── The reference list ──────────────────────────────────────────────

  const refs = new Map();     // ref_id → row, every reference in scope
  const tasks = new Map();    // task_id → row, for the parent/child edges
  let taskId = null;          // the selected task
  let scope = new Set();      // it and every task split from it
  let scopeKey = "";          // the scope as one string, to spot a real change
  let openId = null;          // the one line whose body is open
  let selectedRefId = null;   // state.selected_ref, ours or another task's
  let appliedRefId = null;    // the last one this pane opened, so a line the
                              // user closed by hand stays closed

  /** The task and everything split from it, to any depth. */
  function subtree(id) {
    const out = new Set();
    if (!id) return out;
    out.add(id);
    const kids = new Map();
    for (const [tid, task] of tasks) {
      const parent = task.parent_task_id;
      if (!parent) continue;
      if (!kids.has(parent)) kids.set(parent, []);
      kids.get(parent).push(tid);
    }
    const queue = [id];
    while (queue.length) {
      for (const kid of kids.get(queue.pop()) ?? []) {
        if (out.has(kid)) continue;   // a task tree cannot loop; never trust it to
        out.add(kid);
        queue.push(kid);
      }
    }
    return out;
  }

  /** The task block: its Task ID and title, then the fields it carries. */
  function renderTask() {
    taskEl.replaceChildren();
    taskEl.classList.toggle("task-refs-task-marked", cursor?.kind === "task");
    if (!task) {
      taskEl.appendChild(el("div", "task-refs-task-empty", "Select a task to see its detail."));
      return;
    }
    const head = el("div", "task-refs-task-title");
    head.appendChild(el("span", "task-refs-task-id", task.task_id));
    head.appendChild(el("span", "task-refs-task-name", task.title));
    taskEl.appendChild(head);
    const fields = el("div", "task-refs-fields");
    for (const [name, label] of TASK_FIELDS) {
      const value = fieldValue(task, name);
      if ((value == null || value === "") && !ALWAYS.has(name)) continue;
      fields.appendChild(el("span", "task-refs-field-label", label));
      fields.appendChild(el("span", `task-refs-field-value task-refs-field-${name}`, String(value ?? "")));
    }
    taskEl.appendChild(fields);
  }

  function render() {
    if (selectedRefId != null && selectedRefId !== appliedRefId && refs.has(selectedRefId)) {
      // The References pane's cursor opens its line here — and takes this
      // pane's cursor with it, so Edit edits the reference it points at.
      appliedRefId = openId = selectedRefId;
      cursor = { kind: "ref", ref_id: selectedRefId };
    }
    // The reference the cursor was on can be deleted out from under it.
    if (cursor?.kind === "ref" && !refs.has(cursor.ref_id)) cursor = task ? { kind: "task" } : null;
    const markedId = cursor?.kind === "ref" ? cursor.ref_id : null;
    renderTask();
    updateButtons();
    list.replaceChildren();
    if (!taskId) return;
    const groups = groupRefs(refs.values(), taskId);
    if (!groups.length) {
      list.appendChild(el("div", "task-refs-empty", "No references yet."));
      return;
    }
    for (const group of groups) {
      list.appendChild(el("div", "task-refs-group", `${group.title} (${group.refs.length})`));
      const ownerOf = (ref) => (ref.task_id === taskId ? null : (tasks.get(ref.task_id) ?? {}));
      // A click puts the cursor on the reference (what the toolbar acts on)
      // and, when there is something more to see, opens or closes its body.
      const pick = (ref) => {
        cursor = { kind: "ref", ref_id: ref.ref_id };
        if (hasBody(ref)) openId = openId === ref.ref_id ? null : ref.ref_id;
        render();
      };

      // Images: a grid of thumbnails, the open one full size beneath it.
      if (group.grid) {
        const grid = el("div", "task-refs-grid");
        for (const ref of group.refs) {
          const tile = renderRefTile(ref, { owner: ownerOf(ref) });
          if (ref.ref_id === markedId) tile.classList.add("task-refs-tile-marked");
          if (ref.ref_id === openId) tile.classList.add("task-refs-tile-open");
          tile.addEventListener("click", () => pick(ref));
          grid.appendChild(tile);
        }
        list.appendChild(grid);
        const open = group.refs.find((ref) => ref.ref_id === openId);
        const body = open && renderRefBody(open);
        if (body) list.appendChild(body);
        continue;
      }

      for (const ref of group.refs) {
        const item = el("div", "task-refs-item");
        if (ref.ref_id === markedId) item.classList.add("task-refs-item-marked");
        const open = ref.ref_id === openId;
        const line = renderRefLine(ref, { onGoTo: goTo, expanded: open, owner: ownerOf(ref) });
        if (hasBody(ref)) line.classList.add("task-refs-line-open");
        line.addEventListener("click", () => pick(ref));
        item.appendChild(line);
        if (open) {
          const body = renderRefBody(ref);
          if (body) item.appendChild(body);
        }
        list.appendChild(item);
      }
    }
  }

  // Two subscriptions: the tasks (for the subtree) for the widget's life, and
  // the references of the current subtree. `gen` fences the reference
  // callbacks: a snapshot for the scope the user just left must not repaint
  // the one they moved to.
  let client = null;
  let subid = null;
  let gen = 0;

  function subscribeRefs() {
    const mine = ++gen;
    if (subid && client) { client.unsubscribe(subid); subid = null; }
    refs.clear();
    render();
    if (!client || !scope.size) return;
    const ids = [...scope];
    subid = `task-refs-${mine}`;
    client.subscribe(REFS_SERVICE, "query", {
      subid,
      // One task or a whole subtree: the server does the filtering either way.
      filter: ids.length === 1
        ? `task_id == ${quote(ids[0])}`
        : `CONTAINS([${ids.map(quote).join(", ")}], task_id)`,
      onSnapshot: (rows) => {
        if (mine !== gen) return;
        refs.clear();
        for (const row of rows) refs.set(row.ref_id, row);
        render();
      },
      onUpdate: (op, row) => {
        if (mine !== gen) return;
        // A delete is announced with the request's data — a ref_id, no more —
        // so it is matched by id, not by task. mkio also announces a row that
        // has left the filter as a delete, which is the same handling.
        if (op === "delete") { if (!refs.delete(row.ref_id)) return; }
        else if (!scope.has(row.task_id)) return;
        else refs.set(row.ref_id, row);
        render();
      },
      onDelta: (changes) => {
        if (mine !== gen) return;
        for (const ch of changes) {
          if (ch.op === "delete") refs.delete(ch.row.ref_id);
          else if (scope.has(ch.row.task_id)) refs.set(ch.row.ref_id, ch.row);
        }
        render();
      },
    });
  }

  /** Recompute the subtree; resubscribe only when it really changed. */
  function applyScope() {
    const next = subtree(taskId);
    const key = [...next].sort().join(",");
    if (key === scopeKey) return;
    scope = next;
    scopeKey = key;
    subscribeRefs();
  }

  function subscribeTasks() {
    client.subscribe(TASKS_SERVICE, "query", {
      subid: TASKS_SUBID,
      fields: ["task_id", "parent_task_id", "title"],
      onSnapshot: (rows) => {
        tasks.clear();
        for (const row of rows) tasks.set(row.task_id, row);
        applyScope();
        render();
      },
      onUpdate: (op, row) => {
        if (op === "delete") tasks.delete(row.task_id);
        else tasks.set(row.task_id, row);
        applyScope();
        render();
      },
      onDelta: (changes) => {
        for (const ch of changes) {
          if (ch.op === "delete") tasks.delete(ch.row.task_id);
          else tasks.set(ch.row.task_id, ch.row);
        }
        applyScope();
        render();
      },
    });
  }

  clientP.then((c) => {
    client = c;
    subscribeTasks();
    applyScope();   // subscribes to the references when a task is already selected
  }).catch((e) => setStatus(String(e.message ?? e), true));

  // mkui republishes the selected row when a live update lands on it, so an
  // edit made from this pane repaints the block on the server's announcement
  // with no refetch. Only a *different* task resets the cursor and the scope.
  app.state.subscribe("selected_task", (next) => {
    const id = next?.task_id ?? null;
    const changed = id !== taskId;
    task = next ?? null;
    box.classList.toggle("task-refs-dropbox-disabled", !task);
    boxText.textContent = task
      ? `Drop, paste, or click to add a file, URL, or snippet to ${task.task_id}`
      : "Select a task to add references";
    if (changed) {
      setStatus("");
      taskId = id;
      openId = appliedRefId = null;
      cursor = task ? { kind: "task" } : null;
      applyScope();
    }
    render();
  });

  // The References pane's cursor marks a line here and opens it — when the
  // reference is one this pane lists. Its selection ranges over the blotter's
  // idea of the subtree, and a reference that is not on show is not ours.
  app.state.subscribe("selected_ref", (ref) => {
    const next = ref?.ref_id ?? null;
    if (next === selectedRefId) return;
    selectedRefId = next;
    render();
  });

  // A closed pane holds no subscriptions; reopening it starts fresh ones.
  const paneEl = host.closest("mkui-pane");
  if (paneEl) {
    paneEl.addEventListener("mkui-pane-close", () => {
      if (client) {
        if (subid) client.unsubscribe(subid);
        client.unsubscribe(TASKS_SUBID);
      }
      subid = null;
      ++gen;
    });
    paneEl.addEventListener("mkui-pane-open", () => {
      if (!client) return;
      subscribeTasks();
      subscribeRefs();
    });
  }
});
