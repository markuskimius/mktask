// The `task-refs` widget: everything about references that mkui's declarative
// pieces cannot do — accept a pasted or dropped file, upload it, and show the
// selected reference (an image inline, a snippet in full, a task link with a
// way to open the linked task).
//
// One instance, in the Detail pane, over app state the mkio-table panes
// publish: a drop box for `state.selected_task` — drop or paste a file,
// paste a URL or some text, or click to choose a file — and a preview of
// `state.selected_ref`, the row selected in the References pane. (That pane
// follows the Tasks selection through mkui's table linking —
// `link.broadcast` / `link.listen` in app.json — not here.)
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
    line.appendChild(el("span", "task-refs-relation", ref.relation));
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
      return setStatus(`${taskId} is hidden by a filter (Tasks \u203a Show All)`, true);
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

  // ── The drop box and the selected reference ─────────────────────────

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
