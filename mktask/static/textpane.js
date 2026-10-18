// The `text-pane` widget: the Text pane's whole body. It shows the text of
// the last thing picked, and nothing else, full height:
//
//   - the selected task (`state.selected_task`): its notes;
//   - a reference picked in the References pane (`state.selected_ref`) or
//     under the Detail pane's cursor (`state.detail_ref`, published by
//     refs.js): a snippet's text, or an image. A link, a file or a task
//     reference shows nothing — the Detail pane is where those are opened.
//
// Markdown is rendered; plain text is shown as written.
//
// "Last picked" is which of the three moved to a *different* record most
// recently. mkui republishes a selection when a live update lands on its row,
// and refs.js republishes its cursor the same way, so a republish of the same
// record repaints it in place and never steals the pane from another source.
// A source going empty hands the pane on (`fallback`): the Detail cursor
// returning to the task block shows the task.
//
// A click in the text edits it there: the same editor the dialogs use
// (richtext.js) takes the text's place, the cursor where the click landed,
// and a bar under it — the formatting tools, Cancel and Save — takes a row
// of its own, so it never covers a word. The edit stays open until Save
// (or Cmd/Ctrl+Enter) or Cancel (or Escape): clicking elsewhere, or
// picking another task, leaves it as it is, and the pane follows the new
// pick only once the edit is closed. A save writes the text alone
// (`edit_notes`, `edit_snippet`), always formatted: a plain note opens in
// the editor as written and is saved as Markdown, as Edit → Format →
// Formatted does. One save is one version, so Undo steps it back whole.

import { registerWidget, ensureMkio } from "/mkui/src/index.js";
import { renderMarkdown, mountRichText } from "/static/richtext.js";

const SOURCES = ["selected_task", "selected_ref", "detail_ref"];
const MAC = /Mac|iPhone|iPad/.test(navigator.platform ?? "");

const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
};

const keyOf = (source, row) => (row ? (source === "selected_task" ? row.task_id : row.ref_id) : null);

/** What a row puts in the pane: text that can be edited, an image, or
 *  nothing (null). `key` names the record whichever source carried it. */
function itemOf(source, row) {
  if (!row) return null;
  if (source === "selected_task") {
    return { key: `task:${row.task_id}`, row, text: row.notes ?? "", format: row.notes_format };
  }
  if (row.kind === "text") return { key: `ref:${row.ref_id}`, row, text: row.body ?? "", format: row.format };
  if (row.kind === "file" && /^image\//.test(row.mime || "")) return { key: `ref:${row.ref_id}`, row, image: true };
  return null;
}

/** Plain text as written, or Markdown rendered. */
function textBody(text, format) {
  if (format !== "markdown") return el("pre", "textpane-text", text);
  const div = el("div", "textpane-text task-refs-md");
  div.appendChild(renderMarkdown(text));
  return div;
}

/** UTC, as SQLite's CURRENT_TIMESTAMP writes it. */
const stamp = () => new Date().toISOString().slice(0, 19).replace("T", " ");

registerWidget("text-pane", (spec, app, host) => {
  const root = el("div", `textpane ${spec.class ?? ""}`.trim());
  const view = el("div", "textpane-view");
  root.appendChild(view);
  host.appendChild(root);
  const clientP = ensureMkio(app.config.mkio.url);

  const rows = {};       // source → the row it last published, or null
  let current = null;    // the source on show
  let editing = null;    // the open edit: { item, rt, touched, saving, … }

  /** What to show when `source` goes empty. The Detail cursor going back to
   *  the task block means the task; the References pane's selection going,
   *  the Detail cursor if it is on a reference. */
  const fallback = (source) =>
    (source === "selected_ref" && rows.detail_ref ? "detail_ref"
      : rows.selected_task ? "selected_task" : null);

  const shown = () => (current ? itemOf(current, rows[current]) : null);

  function render() {
    if (editing) { paintBar(); return; }
    const item = shown();
    if (!current) view.replaceChildren(el("div", "textpane-empty", "Pick a task or a snippet to see its text here."));
    else if (!item) view.replaceChildren();
    else if (item.image) {
      const img = el("img", "textpane-image");
      img.src = item.row.href;
      img.alt = item.row.label;
      view.replaceChildren(img);
    } else if (!item.text) view.replaceChildren(el("div", "textpane-empty", "No notes. Click to write some."));
    else view.replaceChildren(textBody(item.text, item.format));
  }

  // ── Editing in place ─────────────────────────────────────────────────

  const status = el("span", "textpane-status");
  const cancelBtn = el("button", "mkui-btn", "Cancel");
  const saveBtn = el("button", "mkui-btn mkui-btn-primary", "Save");
  cancelBtn.type = saveBtn.type = "button";
  cancelBtn.title = "Cancel (Esc)";
  saveBtn.title = `Save (${MAC ? "⌘" : "Ctrl+"}Enter)`;
  cancelBtn.addEventListener("click", () => stop());
  saveBtn.addEventListener("click", () => save());

  /** The record being edited as the sources now have it (a live update), or
   *  null when none of them still carries it. */
  function editedNow() {
    for (const source of SOURCES) {
      const item = itemOf(source, rows[source]);
      if (item && item.key === editing.item.key) return item;
    }
    return null;
  }

  function paintBar() {
    const now = editedNow();
    if (now && now.text !== editing.item.text && !editing.saving) editing.stale = true;
    const moved = shown()?.key !== editing.item.key;
    const [text, warn] = editing.error ? [editing.error, true]
      : editing.saving ? ["Saving…", false]
      : editing.stale ? ["Changed elsewhere — saving replaces it", true]
      : moved ? ["Save or cancel to move on", false]
      : editing.touched ? ["Unsaved", false] : ["", false];
    status.textContent = text;
    status.classList.toggle("textpane-warn", warn);
    saveBtn.disabled = cancelBtn.disabled = !!editing.saving;
  }

  function start(item, x, y) {
    const scroll = view.scrollTop;
    const rt = mountRichText({
      literal: item.format !== "markdown",
      barAt: "bottom",
      extra: [status, cancelBtn, saveBtn],
      onChange: () => { editing.touched = true; editing.error = ""; paintBar(); },
    });
    rt.set(item.text);
    rt.el.classList.add("textpane-editor");
    editing = { item, rt, touched: false, saving: false, stale: false, error: "" };
    root.replaceChildren(rt.el);
    const body = rt.el.querySelector(".richtext-body");
    body.scrollTop = scroll;
    // The editor lays the text out as the view did, so the point clicked
    // is the same character; past the end of the text is the end.
    const at = x == null ? null : rt.editor.view.posAtCoords({ left: x, top: y });
    rt.focus(at?.pos);
    body.scrollTop = scroll;
    paintBar();
  }

  function stop() {
    if (!editing) return;
    const scroll = editing.rt.el.querySelector(".richtext-body").scrollTop;
    editing.rt.destroy();
    editing = null;
    root.replaceChildren(view);
    render();
    view.scrollTop = scroll;
  }

  async function save() {
    if (!editing || editing.saving) return;
    if (!editing.touched) { stop(); return; }   // nothing typed: nothing to write
    const { item, rt } = editing;
    const md = rt.get();
    const [op, data] = item.key.startsWith("task:")
      ? ["edit_notes", { task_id: item.row.task_id, notes: md, notes_format: "markdown", updated_at: stamp() }]
      : ["edit_snippet", { ref_id: item.row.ref_id, body: md, format: "markdown", updated_at: stamp() }];
    editing.saving = true;
    editing.error = "";
    paintBar();
    let error = "";
    try {
      const client = await clientP;
      const resp = await client.send("tasks", data, { op });
      if (resp?.type === "error") error = resp.message;
    } catch (e) {
      error = e.message || String(e);
    }
    if (!editing) return;
    editing.saving = false;
    if (!error) { stop(); return; }
    editing.error = error;     // the text stays, to try again or copy out
    paintBar();
  }

  view.addEventListener("click", (e) => {
    if (editing || e.target.closest("a")) return;   // a link opens
    if (!getSelection()?.isCollapsed) return;        // a drag selects, to copy
    const item = shown();
    if (item && !item.image) start(item, e.clientX, e.clientY);
  });

  // Cmd/Ctrl+Enter saves before the editor reads it as a line break; Escape
  // cancels once the link box has had its own Escape.
  root.addEventListener("keydown", (e) => {
    if (editing && e.key === "Enter" && (MAC ? e.metaKey : e.ctrlKey)) {
      e.preventDefault();
      e.stopPropagation();
      save();
    }
  }, true);
  root.addEventListener("keydown", (e) => {
    if (editing && e.key === "Escape" && !editing.saving) { e.preventDefault(); stop(); }
  });

  for (const source of SOURCES) {
    app.state.subscribe(source, (next) => {
      const row = next || null;   // "" is nothing, as the state block declares it
      const had = rows[source] ?? null;
      const moved = keyOf(source, row) !== keyOf(source, had);
      rows[source] = row;
      if (row && moved) current = source;
      // The Detail cursor leaving a reference is a pick of the task block,
      // whichever source the pane is on: the References pane's selection
      // and the Detail cursor often name the same reference, and which of
      // the two announced it last is only the order the widgets subscribed.
      else if (!row && had && source === "detail_ref") current = fallback(source);
      else if (!row && current === source) current = fallback(source);
      else if (source !== current && !editing) return;
      render();
    });
  }

  host.closest("mkui-pane")?.addEventListener("mkui-pane-close", () => editing?.rt.destroy());
  render();
});
