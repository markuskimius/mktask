// Markdown for a text reference whose `format` is "markdown": the subset a
// note about a task needs, rendered by the Detail pane (refs.js).
//
// Blocks: `#`–`######` headings, paragraphs (a single newline is a line
// break — notes are typed that way), `-` `*` `+` and `1.` lists, nested by
// indent, `>` quotes, ``` fenced code, and `---` rules. Inline: `code`,
// **bold**, *italic* / _italic_, ~~strike~~, [text](url), bare http(s)
// URLs, and backslash escapes. No tables, images or raw HTML.
//
// Two layers. `parseMarkdown` turns the text into a plain tree of
// `{ type, … }` objects and touches no DOM, so the tests run it under node.
// `renderMarkdown` builds that tree with createElement and createTextNode
// and **never assigns HTML**: markup typed into a snippet comes out as the
// characters typed, by construction, and a link whose target is not one of
// SAFE_HREF is shown as its text alone.

export const SAFE_HREF = /^(https?:|mailto:|message:|outlook:|ftp:)/i;

const FENCE = /^ {0,3}(`{3,}|~{3,})(.*)$/;
const HEADING = /^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$/;
const RULE = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
const QUOTE = /^ {0,3}> ?(.*)$/;
const ITEM = /^( *)([-*+]|\d{1,9}[.)])([ \t]+(.*))?$/;
const BLANK = /^\s*$/;

const indentOf = (line) => line.match(/^ */)[0].length;
const isBreak = (line) => FENCE.test(line) || HEADING.test(line) || RULE.test(line)
  || QUOTE.test(line) || ITEM.test(line);
const ordered = (marker) => /\d/.test(marker);

/** The whole text as a list of block nodes. */
export function parseMarkdown(text) {
  return blocks(String(text ?? "").replace(/\r\n?/g, "\n").replace(/\t/g, "    ").split("\n"));
}

function blocks(lines) {
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (BLANK.test(line)) { i++; continue; }

    let m = line.match(FENCE);
    if (m) {
      const close = new RegExp(`^ {0,3}${m[1][0]}{${m[1].length},}\\s*$`);
      const body = [];
      for (i++; i < lines.length && !close.test(lines[i]); i++) body.push(lines[i]);
      i++;   // the closing fence, or past the end
      out.push({ type: "codeblock", text: body.join("\n") });
      continue;
    }
    if (RULE.test(line)) { out.push({ type: "rule" }); i++; continue; }
    if ((m = line.match(HEADING))) {
      out.push({ type: "heading", level: m[1].length, children: inline(m[2] ?? "") });
      i++;
      continue;
    }
    if (QUOTE.test(line)) {
      const body = [];
      for (; i < lines.length && !BLANK.test(lines[i]); i++) {
        const q = lines[i].match(QUOTE);
        if (!q && isBreak(lines[i])) break;
        body.push(q ? q[1] : lines[i]);   // a line without `>` continues the quote
      }
      out.push({ type: "quote", children: blocks(body) });
      continue;
    }
    if (ITEM.test(line)) { i = list(lines, i, out); continue; }

    const para = [];
    for (; i < lines.length && !BLANK.test(lines[i]) && !(para.length && isBreak(lines[i])); i++) {
      para.push(lines[i].trim());
    }
    out.push({ type: "paragraph", children: inline(para.join("\n")) });
  }
  return out;
}

// One list from lines[i]: its items, each a list of blocks parsed from its
// own lines with the item's indent taken off, so a deeper marker is a list
// inside the item. Returns the index after the list.
function list(lines, i, out) {
  const first = lines[i].match(ITEM);
  const base = first[1].length;
  const node = { type: "list", ordered: ordered(first[2]), start: ordered(first[2]) ? parseInt(first[2], 10) : 1, items: [] };
  while (i < lines.length) {
    const m = lines[i].match(ITEM);
    if (!m || m[1].length !== base || ordered(m[2]) !== node.ordered) break;
    const content = base + m[2].length + (m[3] ? m[3].length - (m[4] ?? "").length : 1);
    const body = [m[4] ?? ""];
    let blank = false;
    for (i++; i < lines.length; i++) {
      const line = lines[i];
      if (BLANK.test(line)) { blank = true; body.push(""); continue; }
      const ind = indentOf(line);
      if (ind >= Math.min(content, base + 2)) {
        body.push(line.slice(Math.min(ind, content)));
      } else if (!blank && !isBreak(line)) {
        body.push(line.trim());   // a lazy line continues the item's paragraph
      } else break;
      blank = false;
    }
    while (body.length && body[body.length - 1] === "") body.pop();
    node.items.push(blocks(body));
  }
  out.push(node);
  return i;
}

// ─── inline ─────────────────────────────────────────────────────────────

const PUNCT = "\\`*_~[]()#>+-.!{}|<\"'";
const AUTOLINK = /^https?:\/\/[^\s<>]*[^\s<>.,:;"')\]!?*_~]/i;
const WORD = /[\p{L}\p{N}]/u;
const DELIMS = [["**", "strong"], ["__", "strong"], ["~~", "strike"], ["*", "em"], ["_", "em"]];

/** One paragraph's (or heading's) text as inline nodes. */
export function inline(s) {
  const out = [];
  let text = "";
  const flush = () => { if (text) out.push({ type: "text", text }); text = ""; };
  const push = (node) => { flush(); out.push(node); };

  let i = 0;
  scan: while (i < s.length) {
    const c = s[i];
    if (c === "\\" && i + 1 < s.length && PUNCT.includes(s[i + 1])) { text += s[i + 1]; i += 2; continue; }
    if (c === "\n") { push({ type: "break" }); i++; continue; }
    if (c === "`") {
      const run = s.slice(i).match(/^`+/)[0];
      const end = s.indexOf(run, i + run.length);
      if (end < 0 || s[end + run.length] === "`") { text += run; i += run.length; continue; }
      let code = s.slice(i + run.length, end).replace(/\n/g, " ");
      if (/^ .*[^ ].* $/.test(code)) code = code.slice(1, -1);
      push({ type: "code", text: code });
      i = end + run.length;
      continue;
    }
    if (c === "[") {
      const link = linkAt(s, i);
      if (link) {
        const children = inline(link.label);
        if (SAFE_HREF.test(link.href)) push({ type: "link", href: link.href, children });
        else for (const child of children) child.type === "text" ? (text += child.text) : push(child);
        i = link.end;
        continue;
      }
    }
    if ((c === "h" || c === "H") && !WORD.test(s[i - 1] ?? "")) {
      const m = s.slice(i).match(AUTOLINK);
      if (m) { push({ type: "link", href: m[0], children: [{ type: "text", text: m[0] }] }); i += m[0].length; continue; }
    }
    for (const [d, type] of DELIMS) {
      if (!s.startsWith(d, i)) continue;
      const end = closer(s, i, d);
      if (end < 0) continue;
      push({ type, children: inline(s.slice(i + d.length, end)) });
      i = end + d.length;
      continue scan;
    }
    text += c;
    i++;
  }
  flush();
  return out;
}

// Where the delimiter opened at `i` closes, or -1. An opener must be followed
// by a non-space and a closer preceded by one; `_` must not sit inside a
// word (snake_case stays as typed). Escapes and code spans are passed over,
// and a single `*` or `_` passes over a doubled one, so *a **b** c* nests.
function closer(s, i, d) {
  const single = d.length === 1;
  const after = s[i + d.length];
  if (after === undefined || /\s/.test(after)) return -1;
  if (single && after === d) return -1;
  if (d[0] === "_" && WORD.test(s[i - 1] ?? "")) return -1;
  for (let j = i + d.length + 1; j < s.length; j++) {
    const c = s[j];
    if (c === "\\") { j++; continue; }
    if (c === "`") {
      const run = s.slice(j).match(/^`+/)[0];
      const end = s.indexOf(run, j + run.length);
      if (end >= 0) { j = end + run.length - 1; continue; }
    }
    if (!s.startsWith(d, j)) continue;
    if (single && s[j + 1] === d) { j++; continue; }   // a doubled run is not ours
    if (/\s/.test(s[j - 1])) continue;
    if (d[0] === "_" && WORD.test(s[j + d.length] ?? "")) continue;
    return j;
  }
  return -1;
}

// `[label](href)` starting at `i`: the label (brackets and the target's
// parentheses may nest), the target's first word (a "title" after it is
// dropped), and the index past `)`.
function linkAt(s, i) {
  let depth = 0;
  let j = i;
  for (; j < s.length; j++) {
    if (s[j] === "\\") { j++; continue; }
    if (s[j] === "[") depth++;
    else if (s[j] === "]" && --depth === 0) break;
  }
  if (j >= s.length || s[j + 1] !== "(") return null;
  let close = j + 2;
  for (let parens = 0; close < s.length; close++) {   // a target's own (…) nest
    if (s[close] === "\n") return null;
    if (s[close] === "(") parens++;
    else if (s[close] === ")" && parens-- === 0) break;
  }
  if (close >= s.length) return null;
  let href = s.slice(j + 2, close).trim().split(/\s+/)[0] ?? "";
  if (href.startsWith("<") && href.endsWith(">")) href = href.slice(1, -1);
  return { label: s.slice(i + 1, j), href, end: close + 1 };
}

// ─── DOM ────────────────────────────────────────────────────────────────

const TAGS = {
  paragraph: "p", quote: "blockquote", strong: "strong", em: "em", strike: "del",
};

/** The text as a DocumentFragment: elements and text nodes only. */
export function renderMarkdown(text, doc = globalThis.document) {
  const frag = doc.createDocumentFragment();
  for (const node of parseMarkdown(text)) frag.appendChild(build(node, doc));
  return frag;
}

function build(node, doc) {
  const el = (tag, children = []) => {
    const e = doc.createElement(tag);
    for (const child of children) e.appendChild(build(child, doc));
    return e;
  };
  switch (node.type) {
    case "text": return doc.createTextNode(node.text);
    case "break": return doc.createElement("br");
    case "rule": return doc.createElement("hr");
    case "heading": return el(`h${node.level}`, node.children);
    case "code":
    case "codeblock": {
      const code = doc.createElement("code");
      code.appendChild(doc.createTextNode(node.text));
      if (node.type === "code") return code;
      const pre = doc.createElement("pre");
      pre.appendChild(code);
      return pre;
    }
    case "link": {
      const a = el("a", node.children);
      a.setAttribute("href", node.href);
      a.setAttribute("target", "_blank");
      a.setAttribute("rel", "noopener noreferrer");
      return a;
    }
    case "list": {
      const l = doc.createElement(node.ordered ? "ol" : "ul");
      if (node.ordered && node.start !== 1) l.setAttribute("start", String(node.start));
      for (const item of node.items) l.appendChild(el("li", item));
      return l;
    }
    default: return el(TAGS[node.type] ?? "span", node.children ?? []);
  }
}
