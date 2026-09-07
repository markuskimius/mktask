// The `task-refs` widget: everything about references that mkui's declarative
// pieces cannot do — accept a pasted or dropped file, upload it, and show the
// selected reference (an image inline, a snippet in full, a task link with a
// way to open the linked task).
//
// Two modes, both reading app state that mkio-table panes publish:
//
//   { type: "task-refs" }                  (Detail pane)
//     A drop box for `state.selected_task` — drop or paste a file, paste a
//     URL or some text, or click to choose a file — and a preview of
//     `state.selected_ref`, the row selected in the References pane. It
//     also keeps the References pane on the selected task: mkui has no
//     state-bound filter, so the widget fires `table.filter` (the action
//     the Tasks menu uses) with the selected Task ID on every change, and
//     clears that column's filter when nothing is selected.
//
//   { type: "task-refs", mode: "linked" }  (Linked Task pane)
//     The references of `state.linked_task`, fetched once per task through
//     the `task_refs_get` request-reply service. A viewer, not a live pane.
//
// "Go to" on a task link fetches that task (`task_get`), publishes it as
// `state.linked_task`, and shows the Linked Task pane (`pane.show`), so a
// chain of links can be followed without touching the selection in Tasks.

import { registerWidget, ensureMkio } from "/mkui/src/index.js";

const UPLOAD_URL = "/files";
const RELATION_WORDS = { blocks: "blocks", blocked_by: "blocked by", relates: "relates to" };

const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
};

const isUrl = (s) => /^(https?:|mailto:|message:|outlook:|ftp:)\S+$/i.test(s.trim()) && !/\s/.test(s.trim());

const stamp = () => new Date().toISOString().slice(0, 16).replace("T", " ");

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

/** A reference row rendered as one line: kind badge, label (a link when it has one), relation. */
function renderRefLine(ref, { onGoTo } = {}) {
  const line = el("div", "task-refs-line");
  line.appendChild(el("span", `task-refs-kind task-refs-kind-${ref.kind}`, ref.kind));
  if (ref.kind === "task") {
    line.appendChild(el("span", "task-refs-relation", RELATION_WORDS[ref.relation] ?? ref.relation));
    line.appendChild(el("span", "task-refs-taskid", ref.href));
    line.appendChild(el("span", "task-refs-label", ref.label));
    if (onGoTo) {
      const go = el("button", "task-refs-goto", "Go to");
      go.type = "button";
      go.addEventListener("click", () => onGoTo(ref.href));
      line.appendChild(go);
    }
  } else if (ref.href) {
    const a = el("a", "task-refs-label mkui-rich-link", ref.label || ref.href);
    a.href = ref.href;
    a.target = "_blank";
    a.rel = "noopener";
    line.appendChild(a);
  } else {
    line.appendChild(el("span", "task-refs-label", ref.label));
  }
  return line;
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

registerWidget("task-refs", (spec, app, host) => {
  const root = el("div", `task-refs ${spec.class ?? ""}`.trim());
  host.appendChild(root);
  const clientP = ensureMkio(app.config.mkio.url);

  const goTo = async (taskId) => {
    const client = await clientP;
    const resp = await client.request("task_get", { task_id: taskId });
    const row = (resp?.rows ?? [])[0] ?? null;
    if (!row) { setStatus(`No task ${taskId}`); return; }
    app.state.set("linked_task", row);
    app.fireAction("pane.show", spec.linkedPane ?? "linked-task");
  };

  let statusEl = null;
  const setStatus = (text, isError = false) => {
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.classList.toggle("task-refs-error", isError);
  };

  if (spec.mode === "linked") {
    const list = el("div", "task-refs-list");
    root.appendChild(list);
    let current = null;
    app.state.subscribe("linked_task", async (task) => {
      list.replaceChildren();
      if (!task) { list.appendChild(el("div", "task-refs-empty", "Follow a task link to open a task here.")); return; }
      current = task.task_id;
      const client = await clientP;
      const resp = await client.request("task_refs_get", { task_id: task.task_id });
      if (current !== task.task_id) return;
      const rows = resp?.rows ?? [];
      if (!rows.length) { list.appendChild(el("div", "task-refs-empty", "No references.")); return; }
      for (const ref of rows) {
        const item = el("div", "task-refs-item");
        item.appendChild(renderRefLine(ref, { onGoTo: goTo }));
        const body = renderRefBody(ref);
        if (body) item.appendChild(body);
        list.appendChild(item);
      }
    });
    return;
  }

  // ── Detail pane: drop box + selected reference ──────────────────────

  const box = el("div", "task-refs-dropbox");
  const boxText = el("span", "task-refs-dropbox-text");
  const input = el("input");
  input.type = "file"; input.multiple = true; input.hidden = true;
  box.append(boxText, input);
  statusEl = el("div", "task-refs-status");
  const preview = el("div", "task-refs-preview");
  root.append(box, statusEl, preview);

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

  app.state.subscribe("selected_task", (task) => {
    app.fireAction("table.filter", {
      pane: spec.referencesPane ?? "references",
      filters: { task_id: task ? [task.task_id] : null },
      merge: true,
    });
    box.classList.toggle("task-refs-dropbox-disabled", !task);
    boxText.textContent = task
      ? `Drop, paste, or click to add a file, URL, or snippet to ${task.task_id}`
      : "Select a task to add references";
    setStatus("");
  });

  app.state.subscribe("selected_ref", (ref) => {
    preview.replaceChildren();
    if (!ref) return;
    preview.appendChild(renderRefLine(ref, { onGoTo: goTo }));
    const body = renderRefBody(ref);
    if (body) preview.appendChild(body);
  });
});
