// The `task-refs` widget: everything about references that mkui's declarative
// pieces cannot do — accept a pasted or dropped file, upload it, and show the
// references of the selected task **and of every task split from it**: task
// links grouped by relation, then files, URLs, and snippets.
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

// The sections below the task links, in the order they are shown. Images are
// files, but they are shown as a grid of thumbnails: a picture is its own
// label, and a wall of file names is not.
const SECTIONS = [["image", "Images"], ["file", "Files"], ["url", "URLs"], ["text", "Snippets"]];
const GRID_SECTIONS = new Set(["image"]);

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
 * (task links, alphabetically), then Files, URLs, and Snippets. Inside a
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
  root.append(box, statusEl, list);

  const selectedTask = () => app.state.get("selected_task");

  const addRef = async (data) => {
    const task = selectedTask();
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

  const acceptFiles = guarded(async (files) => {
    for (const f of files) await addFile(f, f.name);
  });

  box.addEventListener("click", () => { if (selectedTask()) input.click(); });
  input.addEventListener("change", () => { acceptFiles([...input.files]); input.value = ""; });
  box.addEventListener("dragover", (ev) => { if (selectedTask()) { ev.preventDefault(); box.classList.add("task-refs-dropbox-over"); } });
  box.addEventListener("dragleave", () => box.classList.remove("task-refs-dropbox-over"));
  box.addEventListener("drop", (ev) => {
    ev.preventDefault();
    box.classList.remove("task-refs-dropbox-over");
    if (!selectedTask()) return;
    const files = [...(ev.dataTransfer?.files ?? [])];
    if (files.length) return acceptFiles(files);
    const text = ev.dataTransfer?.getData("text/uri-list") || ev.dataTransfer?.getData("text/plain");
    if (text) guarded(addText)(text);
  });

  // Paste anywhere while a task is selected and no text field has focus:
  // an image or file uploads, a URL becomes a url reference, text a snippet.
  window.addEventListener("paste", (ev) => {
    if (!selectedTask() || inTextField() || !root.isConnected) return;
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

  function render() {
    if (selectedRefId != null && selectedRefId !== appliedRefId && refs.has(selectedRefId)) {
      appliedRefId = openId = selectedRefId;   // the References pane's cursor opens its line
    }
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
      const toggle = (ref) => { openId = openId === ref.ref_id ? null : ref.ref_id; render(); };

      // Images: a grid of thumbnails, the open one full size beneath it.
      if (group.grid) {
        const grid = el("div", "task-refs-grid");
        for (const ref of group.refs) {
          const tile = renderRefTile(ref, { owner: ownerOf(ref) });
          if (ref.ref_id === selectedRefId) tile.classList.add("task-refs-tile-marked");
          if (ref.ref_id === openId) tile.classList.add("task-refs-tile-open");
          tile.addEventListener("click", () => toggle(ref));
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
        if (ref.ref_id === selectedRefId) item.classList.add("task-refs-item-marked");
        const open = ref.ref_id === openId;
        const line = renderRefLine(ref, { onGoTo: goTo, expanded: open, owner: ownerOf(ref) });
        if (hasBody(ref)) {
          line.classList.add("task-refs-line-open");
          line.addEventListener("click", () => toggle(ref));
        }
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

  app.state.subscribe("selected_task", (task) => {
    box.classList.toggle("task-refs-dropbox-disabled", !task);
    boxText.textContent = task
      ? `Drop, paste, or click to add a file, URL, or snippet to ${task.task_id}`
      : "Select a task to add references";
    setStatus("");
    if ((task?.task_id ?? null) === taskId) return;
    taskId = task?.task_id ?? null;
    openId = appliedRefId = null;
    applyScope();
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
