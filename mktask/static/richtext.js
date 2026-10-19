// The `richtext` dialog field: a formatted-text editor (TipTap, bundled in
// /static/vendor/tiptap.js) that a dialog reads and writes as Markdown, so
// what is stored stays plain text any tool can read. The formatting is seen
// while it is typed: Cmd/Ctrl+B in the middle of a word bolds just those
// letters, `1. ` at the start of a line starts a numbered list, and the
// toolbar shows which formats the cursor is in.
//
// mkui's dialog owns the label, `required` (an empty note is `""`),
// `showWhen`, `value` and the submit; the field owns the editor. A value
// set into it is kept as given until the user edits, so a dialog opened and
// saved untouched sends back exactly what it was handed.
//
// `literal = true` on the field reads what is set into it as plain text,
// not Markdown: each line as written, a blank line between paragraphs, no
// character taken for formatting. The value it hands back is still
// Markdown (escaped where it has to be). That is how a plain note becomes a
// formatted one in the Edit dialogs: the textarea's text, computed into a
// literal editor.
//
// `mountRichText` is the editor itself — the toolbar, the link box, the
// text — for anything else that edits in place: the Text pane (textpane.js)
// puts the same editor where a note was being read.
//
// Also the one place the rest of the app gets Markdown rendering from
// (`renderMarkdown`), so the Detail pane and the Text pane show a note
// through the same schema the editor wrote it with.

import { registerFieldType } from "/mkui/src/index.js";
import { icon } from "/mkui/src/lib/icons.js";
import * as T from "/static/vendor/tiptap.js";

export const { renderMarkdown, htmlToMarkdown } = T;

const MAC = /Mac|iPhone|iPad/.test(navigator.platform ?? "");
const keys = (k) => (MAC ? k.replace(/Mod-/g, "⌘").replace(/Shift-/g, "⇧").replace(/Alt-/g, "⌥")
  : k.replace(/Mod-/g, "Ctrl+").replace(/Shift-/g, "Shift+").replace(/Alt-/g, "Alt+"));

// The toolbar: [label, title, shortcut, run(chain), active?(editor)], and
// null for a gap. The shortcuts are TipTap's own, shown in the titles.
const TOOLS = [
  ["B", "Bold", "Mod-B", (c) => c.toggleBold(), (e) => e.isActive("bold")],
  ["I", "Italic", "Mod-I", (c) => c.toggleItalic(), (e) => e.isActive("italic")],
  ["S", "Strikethrough", "Mod-Shift-S", (c) => c.toggleStrike(), (e) => e.isActive("strike")],
  ["</>", "Code", "Mod-E", (c) => c.toggleCode(), (e) => e.isActive("code")],
  null,
  ["H1", "Heading", "Mod-Alt-1", (c) => c.toggleHeading({ level: 1 }), (e) => e.isActive("heading", { level: 1 })],
  ["H2", "Subheading", "Mod-Alt-2", (c) => c.toggleHeading({ level: 2 }), (e) => e.isActive("heading", { level: 2 })],
  null,
  ["•", "Bulleted list (type - and a space)", "Mod-Shift-8", (c) => c.toggleBulletList(), (e) => e.isActive("bulletList")],
  ["1.", "Numbered list (type 1. and a space)", "Mod-Shift-7", (c) => c.toggleOrderedList(), (e) => e.isActive("orderedList")],
  ["❝", "Quote", "Mod-Shift-B", (c) => c.toggleBlockquote(), (e) => e.isActive("blockquote")],
  ["{ }", "Code block", "Mod-Alt-C", (c) => c.toggleCodeBlock(), (e) => e.isActive("codeBlock")],
  null,
  ["link", "Link", "Mod-K", null, (e) => e.isActive("link")],
  ["Tx", "Clear formatting", null, (c) => c.unsetAllMarks().clearNodes(), null],
];

const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
};

const SAFE = new RegExp(`^(${T.SAFE_PROTOCOLS.join("|")}):`, "i");
/** What was typed into the link box as a target: a bare host gets https,
 *  and anything outside SAFE_PROTOCOLS is refused (null). */
function linkTarget(typed) {
  const v = typed.trim();
  if (!v) return "";
  if (SAFE.test(v)) return v;
  if (/^[a-z][a-z0-9+.-]*:/i.test(v)) return null;
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v) ? `mailto:${v}` : `https://${v}`;
}

/** Plain text as a document: paragraphs at blank lines, a line break at
 *  every other newline, every character as typed. */
function literalDoc(text) {
  const paragraphs = text.replace(/\r\n?/g, "\n").split(/\n[ \t]*\n/).filter((p) => p.trim());
  return {
    type: "doc",
    content: paragraphs.map((p) => ({
      type: "paragraph",
      content: p.split("\n").flatMap((line, i) => [
        ...(i ? [{ type: "hardBreak" }] : []),
        ...(line ? [{ type: "text", text: line }] : []),
      ]),
    })),
  };
}

/** The editor and its toolbar, mounted in a new element (`.el`).
 *
 *  `literal` reads a set value as plain text. `onChange(markdown)` runs on
 *  every edit.
 *  `set` keeps the value as given until an edit, so `get` answers exactly
 *  what was set while nothing has been typed. */
export function mountRichText({ literal = false, placeholder = "", rows = 0, onChange } = {}) {
  const root = el("div", "richtext");
  const bar = el("div", "richtext-bar");
  bar.setAttribute("role", "toolbar");
  const linkRow = el("div", "richtext-link");
  linkRow.hidden = true;
  const linkInput = el("input", "richtext-link-input");
  linkInput.type = "text";
  linkInput.placeholder = "https://… or an email address";
  linkInput.setAttribute("aria-label", "Link target");
  const linkApply = el("button", "mkui-btn", "Apply");
  const linkRemove = el("button", "mkui-btn", "Remove link");
  linkApply.type = linkRemove.type = "button";
  linkRow.append(linkInput, linkApply, linkRemove);
  const body = el("div", "richtext-body");
  if (rows) body.style.setProperty("--richtext-rows", String(rows));
  // The tools under the text, so opening an editor never moves a word.
  root.append(body, linkRow, bar);

  let value = "";            // what get() answers: as set, until an edit
  let given = "";            // what was last set, until an edit (null after)
  const buttons = [];        // [button, active]

  const editor = T.createEditor({
    element: body,
    placeholder,
    onChange: (md) => { value = md; given = null; onChange?.(md); },
    onSelection: () => paint(),
  });

  // Styled as a rendered snippet is (mktask.css), so written and read look alike.
  editor.view.dom.classList.add("task-refs-md");

  function paint() {
    for (const [b, active] of buttons) b.setAttribute("aria-pressed", String(!!active?.(editor)));
  }

  for (const tool of TOOLS) {
    if (!tool) { bar.appendChild(el("span", "richtext-gap")); continue; }
    const [label, title, shortcut, run, active] = tool;
    const b = el("button", "richtext-tool");
    b.type = "button";
    b.tabIndex = -1;           // Tab goes from the text to the next field
    if (label === "link") b.appendChild(icon("link"));
    else b.textContent = label;
    b.dataset.tool = title;
    b.title = shortcut ? `${title} (${keys(shortcut)})` : title;
    b.setAttribute("aria-label", title);
    // Pressing a tool must not take the focus (and the selection) from
    // the text it is about to format.
    b.addEventListener("mousedown", (e) => e.preventDefault());
    b.addEventListener("click", () => (run ? run(editor.chain().focus()).run() : openLink()));
    bar.appendChild(b);
    if (active) buttons.push([b, active]);
  }

  // The link box: over the toolbar rather than a prompt, which would block
  // the page. Enter applies, Escape goes back to the text without closing
  // the dialog.
  function openLink() {
    linkInput.value = editor.getAttributes("link").href ?? "";
    linkInput.removeAttribute("aria-invalid");
    linkRemove.hidden = !editor.isActive("link");
    linkRow.hidden = false;
    linkInput.focus();
    linkInput.select();
  }
  function closeLink() {
    linkRow.hidden = true;
    editor.commands.focus();
  }
  function applyLink() {
    const href = linkTarget(linkInput.value);
    if (href === null) { linkInput.setAttribute("aria-invalid", "true"); return; }
    const chain = editor.chain().focus().extendMarkRange("link");
    if (!href) chain.unsetLink().run();
    else if (editor.state.selection.empty && !editor.isActive("link")) {
      chain.insertContent({ type: "text", text: linkInput.value.trim(), marks: [{ type: "link", attrs: { href } }] }).run();
    } else chain.setLink({ href }).run();
    linkRow.hidden = true;
  }
  linkApply.addEventListener("click", applyLink);
  linkRemove.addEventListener("click", () => { editor.chain().focus().extendMarkRange("link").unsetLink().run(); linkRow.hidden = true; });
  linkInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.ctrlKey && !e.metaKey) { e.preventDefault(); e.stopPropagation(); applyLink(); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeLink(); }
  });
  body.addEventListener("keydown", (e) => {
    if ((MAC ? e.metaKey : e.ctrlKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "k") {
      e.preventDefault();
      openLink();
    }
  });

  // A dialog re-applies `disabled` on every pass, so only a real change
  // touches the editor, and that without an update event: an update is an
  // edit, which would run the pass again.
  let enabled = true;
  const setEnabled = (on) => {
    if (on === enabled) return;
    enabled = on;
    editor.setEditable(on, false);
    root.classList.toggle("richtext-off", !on);
    for (const b of bar.querySelectorAll("button")) b.disabled = !on;
    if (!on) linkRow.hidden = true;
  };

  paint();
  return {
    el: root,
    editor,
    get: () => value,
    set(v) {
      // The same value again is no change: a compute re-run until it
      // settles must find it settled, and a literal one hands back
      // Markdown, not what it was given.
      const next = v == null ? "" : String(v);
      if (next === given) return;
      given = next;
      if (literal) {
        editor.commands.setContent(literalDoc(next), { emitUpdate: false });
        value = T.toMarkdown(editor);
      } else {
        editor.commands.setContent(T.parseMarkdown(next), { emitUpdate: false });
        value = next;
      }
      paint();
    },
    /** Put the cursor at a document position, else at the end. */
    focus: (pos) => editor.commands.focus(pos ?? "end"),
    setEnabled,
    destroy: () => editor.destroy(),
  };
}

registerFieldType("richtext", (field, ctx) => {
  const rt = mountRichText({
    literal: !!field.literal,
    placeholder: ctx.placeholder,
    rows: field.rows,
    onChange: () => ctx.changed(),
  });
  const grown = new ResizeObserver(() => ctx.resized());
  grown.observe(rt.el);
  return {
    el: rt.el,
    get: rt.get,
    set: rt.set,
    focus: () => rt.focus(),
    setDisabled: (b) => rt.setEnabled(!b),
    setReadOnly: (b) => rt.setEnabled(!b),
    destroy() { grown.disconnect(); rt.destroy(); },
    keepsEnter: true,
  };
});
