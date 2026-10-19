// The TipTap bundle mktask serves as /static/vendor/tiptap.js (build.mjs).
// Everything mktask needs from TipTap goes through these exports, so the
// rest of the app never imports a package and runs without npm.
//
// Markdown stays the stored format: an editor opens on Markdown and hands
// Markdown back, and the read-only views render it through the same schema,
// so what is shown is what the editor would have made of it.

import { Editor, getSchema } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import Paragraph from "@tiptap/extension-paragraph";
import Placeholder from "@tiptap/extension-placeholder";
import { Markdown, MarkdownManager } from "@tiptap/markdown";
import { DOMParser as PMDOMParser, DOMSerializer, Node as PMNode } from "@tiptap/pm/model";

// The link targets a note may carry; anything else is shown as its text.
export const SAFE_PROTOCOLS = ["http", "https", "mailto", "message", "outlook", "ftp"];
const SAFE_HREF = new RegExp(`^(${SAFE_PROTOCOLS.join("|")}):`, "i");
const allowed = (url) => SAFE_HREF.test(String(url ?? "").trim());

// A paragraph line that Markdown would read as the start of something else
// (a heading, a list item, a setext underline, indented code) gets its
// marker escaped, so "1. not a list" typed as text reopens as text. The
// serializer escapes inline syntax itself, but nothing at the start of a line.
const LINE_STARTS = [
  [/^(?: {4}|\t)/gm, (lead) => (lead === "\t" ? "&#9;" : "&#32;   ")],
  [/^( {0,3})(#{1,6})(?=[ \t]|$)/gm, "$1\\$2"],
  [/^( {0,3})(\d{1,9})([.)])(?=[ \t]|$)/gm, "$1$2\\$3"],
  [/^( {0,3})([-+])(?=[ \t]|$)/gm, "$1\\$2"],
  [/^( {0,3})([-=])([-=]*[ \t]*)$/gm, "$1\\$2$3"],
];
export function escapeLineStarts(text) {
  return LINE_STARTS.reduce((s, [re, to]) => s.replace(re, to), text);
}
const LiteralParagraph = Paragraph.extend({
  renderMarkdown(node, h, ctx) {
    return escapeLineStarts(this.parent(node, h, ctx));
  },
});

/** The schema's extensions: what a note can hold, in the editor and out. */
export function extensions({ placeholder = "" } = {}) {
  return [
    StarterKit.configure({
      underline: false,                  // Markdown has no underline
      paragraph: false,                  // LiteralParagraph instead
      link: {
        openOnClick: false,              // a click in the editor places the cursor
        autolink: true,
        isAllowedUri: (url) => allowed(url),
        HTMLAttributes: { target: "_blank", rel: "noopener noreferrer" },
      },
    }),
    LiteralParagraph,
    Placeholder.configure({ placeholder }),
    Markdown,
  ];
}

let shared = null;   // schema and Markdown manager for the read-only paths
function core() {
  if (!shared) {
    const exts = extensions();
    shared = { schema: getSchema(exts), manager: new MarkdownManager({ extensions: exts }) };
  }
  return shared;
}

/** An editor on `element`, opened on `markdown`. `onChange(markdown)` runs
 *  on every edit, `onSelection()` when the cursor moves (a toolbar's
 *  pressed buttons). The caller owns `destroy()`. */
export function createEditor({ element, markdown = "", placeholder = "", editable = true, onChange, onSelection, onFocus, onBlur } = {}) {
  return new Editor({
    element,
    extensions: extensions({ placeholder }),
    content: parseMarkdown(markdown),
    editable,
    onUpdate: ({ editor }) => onChange?.(toMarkdown(editor)),
    onSelectionUpdate: () => onSelection?.(),
    onTransaction: () => onSelection?.(),
    onFocus: () => onFocus?.(),
    onBlur: () => onBlur?.(),
  });
}

/** The editor's content as Markdown, `""` when there is nothing in it.
 *  Trailing whitespace goes: the empty line an editor keeps after a list
 *  and a line break with nothing after it say nothing. */
export function toMarkdown(editor) {
  return editor.isEmpty ? "" : editor.getMarkdown().replace(/\s+$/, "");
}

/** Markdown → TipTap JSON, and back: the round trip the editor makes. */
export function parseMarkdown(markdown) {
  return safeLinks(core().manager.parse(String(markdown ?? "")));
}

// A link whose target is not one of SAFE_PROTOCOLS keeps its text and loses
// the link, in the editor and out of it.
function safeLinks(node) {
  if (node.marks) node.marks = node.marks.filter((m) => m.type !== "link" || allowed(m.attrs?.href));
  if (node.marks && !node.marks.length) delete node.marks;
  node.content?.forEach(safeLinks);
  return node;
}
export function serializeMarkdown(json) {
  return core().manager.serialize(json).replace(/\s+$/, "");
}

/** Markdown as a DocumentFragment built by the schema's own serializer:
 *  elements and text nodes only, never assigned HTML. Raw HTML in the
 *  Markdown is read through the schema too, so only what a note can hold
 *  survives it: no script, no attribute, no unsafe link. */
export function renderMarkdown(markdown, doc = globalThis.document) {
  const { schema } = core();
  const node = PMNode.fromJSON(schema, parseMarkdown(markdown));
  return DOMSerializer.fromSchema(schema).serializeFragment(node.content, { document: doc });
}

/** Whether Markdown holds any formatting: a mark (bold, a link…) or a block
 *  other than a paragraph (a list, a heading…). Text that merely reads as
 *  Markdown once escaped holds none. */
export function isFormatted(markdown) {
  const plain = new Set(["doc", "paragraph", "text", "hardBreak"]);
  const walk = (node) => !!node.marks?.length || !plain.has(node.type) || (node.content ?? []).some(walk);
  return walk(parseMarkdown(markdown));
}

/** Pasted HTML → Markdown, through the schema: what the editor would keep
 *  of it, and nothing else. `""` for nothing worth keeping. */
export function htmlToMarkdown(html) {
  const { schema } = core();
  const dom = new globalThis.DOMParser().parseFromString(String(html ?? ""), "text/html");
  const node = PMDOMParser.fromSchema(schema).parse(dom.body);
  if (!node.textContent.trim()) return "";
  return serializeMarkdown(node.toJSON()).trim();
}
