// The `ref-reader` widget: the Reader pane's whole body. It shows the last
// thing picked, full height, so a long note reads without opening anything:
//
//   - a reference picked in the References pane (`state.selected_ref`) or
//     under the Detail pane's cursor (`state.detail_ref`, published by
//     refs.js): a snippet as its text, rendered when it is Markdown; an image
//     full width; a file or a link as something to open; a task reference as
//     the related task;
//   - the selected task (`state.selected_task`) when a task was picked last:
//     its title and its notes, rendered when they are Markdown.
//
// "Last picked" is which of the three moved to a *different* record most
// recently. mkui republishes a selection when a live update lands on its row,
// and refs.js republishes its cursor the same way, so a republish of the same
// record repaints it in place and never steals the pane from another source.
// A source going empty hands the pane on (`fallback`): the Detail cursor
// returning to the task block shows the task. Read-only: editing stays with the panes that own the records.

import { registerWidget } from "/mkui/src/index.js";
import { renderMarkdown } from "/static/markdown.js";

const SOURCES = ["selected_task", "selected_ref", "detail_ref"];

const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
};

const keyOf = (source, row) => (row ? (source === "selected_task" ? row.task_id : row.ref_id) : null);

/** Plain text as written, or Markdown rendered. */
function textBody(text, format) {
  if (format !== "markdown") return el("pre", "reader-text", text);
  const div = el("div", "reader-text task-refs-md");
  div.appendChild(renderMarkdown(text));
  return div;
}

function link(href, text) {
  const a = el("a", "reader-link mkui-rich-link", text);
  a.href = href;
  a.target = "_blank";
  a.rel = "noopener";
  return a;
}

function renderTask(task) {
  const out = [];
  const head = el("div", "reader-head");
  head.append(el("span", "reader-id", task.task_id), el("span", "reader-title", task.title));
  out.push(head, el("div", "reader-sub", "Notes"));
  out.push(task.notes ? textBody(task.notes, task.notes_format) : el("div", "reader-empty", "No notes."));
  return out;
}

function renderRef(ref) {
  const head = el("div", "reader-head");
  head.appendChild(el("span", "reader-title", ref.kind === "task" ? `${ref.relation} ${ref.href}` : ref.label));
  const kind = ref.kind === "url" ? "link" : ref.kind === "text" ? "snippet" : ref.kind;
  const sub = el("div", "reader-sub", `${kind} · ${ref.task_id}${ref.task_title ? `  ${ref.task_title}` : ""}`);
  const out = [head, sub];
  if (ref.kind === "text") out.push(textBody(ref.body, ref.format));
  else if (ref.kind === "file" && /^image\//.test(ref.mime || "")) {
    const a = link(ref.href, null);
    a.title = "Open full size";
    const img = el("img", "reader-image");
    img.src = ref.href;
    img.alt = ref.label;
    a.appendChild(img);
    out.push(a);
  } else if (ref.kind === "file") out.push(link(ref.href, `Open ${ref.label}`), el("div", "reader-sub", ref.mime));
  else if (ref.kind === "url") out.push(link(ref.href, ref.href));
  else if (ref.kind === "task") out.push(el("div", "reader-text", ref.label));
  return out;
}

registerWidget("ref-reader", (spec, app, host) => {
  const root = el("div", `reader ${spec.class ?? ""}`.trim());
  host.appendChild(root);

  const rows = {};       // source → the row it last published, or null
  let current = null;    // the source on show

  /** What to show when `source` goes empty. The Detail cursor going back to
   *  the task block means the task; the References pane's selection going,
   *  the Detail cursor if it is on a reference. */
  const fallback = (source) =>
    (source === "selected_ref" && rows.detail_ref ? "detail_ref"
      : rows.selected_task ? "selected_task" : null);

  function render() {
    const row = current ? rows[current] : null;
    root.replaceChildren(...(row
      ? (current === "selected_task" ? renderTask(row) : renderRef(row))
      : [el("div", "reader-empty", "Pick a task or a reference to read it here.")]));
  }

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
      else if (source !== current) return;
      render();
    });
  }
  render();
});
