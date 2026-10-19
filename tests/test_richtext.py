"""static/vendor/tiptap.js under node: the Markdown the editor reads, writes
and renders, through the bundle's own schema (editor/entry.js). Skipped
without node.

Three promises, one class each: text typed into a paragraph comes back as
typed however much it looks like Markdown (the serializer escapes inline
syntax, `escapeLineStarts` a line's start); Markdown the editor writes reads
back as itself; and what is rendered is built by the schema, so a script, an
attribute or an unsafe link typed into a note arrives as text, never markup.
The renderer is handed a stub document that serializes what it is asked to
build, so each render case reads as the markup the Detail pane would show.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

BUNDLE = Path(__file__).resolve().parent.parent / "mktask" / "static" / "vendor" / "tiptap.js"

# Loaded from a data: URL: without a package.json saying "module", node
# would read the file as CommonJS.
HARNESS = r"""
import { readFileSync } from "node:fs";
const src = readFileSync(process.argv[1], "utf8");
const T = await import("data:text/javascript," + encodeURIComponent(src));

const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
class Node_ {
  constructor(tag) { this.tag = tag; this.attrs = []; this.children = []; }
  appendChild(c) { this.children.push(c); return c; }
  setAttribute(k, v) { this.attrs.push(` ${k}="${esc(String(v)).replace(/"/g, "&quot;")}"`); }
  get html() {
    const inner = this.children.map((c) => c.html).join("");
    if (!this.tag) return inner;
    if (this.tag === "br" || this.tag === "hr") return `<${this.tag}>`;
    return `<${this.tag}${this.attrs.join("")}>${inner}</${this.tag}>`;
  }
}
const doc = {
  createDocumentFragment: () => new Node_(null),
  createElement: (tag) => new Node_(tag),
  createTextNode: (text) => ({ html: esc(text) }),
};
const paragraphs = (json) => (json.content || []).map(
  (p) => `${p.type}:${(p.content || []).map((n) => n.text ?? "").join("")}`).join("|");

const run = {
  typed(text) {
    const md = T.serializeMarkdown({ type: "doc", content: [{ type: "paragraph", content: [{ type: "text", text }] }] });
    return [md, paragraphs(T.parseMarkdown(md))];
  },
  roundtrip: (md) => T.serializeMarkdown(T.parseMarkdown(md)),
  escape: (text) => T.escapeLineStarts(text),
  render: (md) => T.renderMarkdown(md, doc).html,
  protocols: () => T.SAFE_PROTOCOLS,
};
const cases = JSON.parse(readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(cases.map(([fn, arg]) => run[fn](arg))));
"""

LINK = ' target="_blank" rel="noopener noreferrer"'

# Typed as text: each looks like Markdown and must not become it.
TYPED = [
    "* x", "*Bob*", "**b**", "_u_", "~~s~~", "`c`", "a\\b",
    "1. a", "10) x", "# x", "---", "===", "+ x", "- x", "> q",
    "    code", "\tx",
    "[a](https://b)", "![i](x.png)", "<b>x</b>", "a <script>", "&gt; q",
    "https://plain.example",
]

# What the editor writes, read back and written again unchanged.
ROUNDTRIP = [
    "un**believ**able\n\n1. **First** step\n2. second step",
    "- one\n- two",
    "> quoted",
    "# Head\n\ntext",
    "`code` and ~~gone~~",
    "```\nkept  **raw**\n```",
    "[docs](https://x.example/a)",
    "From: Ann  \nnext",
    "a *b* c",
    "1\\. not a list",
    "\\# not a heading",
]

# A paragraph line's start that Markdown would read as structure.
ESCAPE = [
    ("1. not a list", "1\\. not a list"),
    ("10) x", "10\\) x"),
    ("a\n2. b", "a\n2\\. b"),
    ("# x", "\\# x"),
    ("###### six", "\\###### six"),
    ("####### seven", "####### seven"),   # not a heading: seven is too many
    ("- x", "\\- x"),
    ("+ x", "\\+ x"),
    ("---", "\\---"),
    ("===", "\\==="),
    ("    code", "&#32;   code"),
    ("\tx", "&#9;x"),
    ("plain", "plain"),
    ("x # y", "x # y"),
]

RENDER = [
    ("**ok** _it_", "<p><strong>ok</strong> <em>it</em></p>"),
    ("a\nb", "<p>a\nb</p>"),   # one newline: pre-wrap shows it as a break
    ("1. **First**\n2. two", "<ol><li><p><strong>First</strong></p></li><li><p>two</p></li></ol>"),
    ("[ok](https://a.example)", f'<p><a{LINK} href="https://a.example">ok</a></p>'),
    ("[m](mailto:a@b.example)", f'<p><a{LINK} href="mailto:a@b.example">m</a></p>'),
    ("", ""),
    # nothing typed becomes markup
    ("[click](javascript:alert(1))", "<p>click</p>"),
    ("[x](data:text/html,y)", "<p>x</p>"),
    ("<script>alert(1)</script>", "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>"),
    ("<img src=x onerror=alert(1)>", "<p>&lt;img src=x onerror=alert(1)&gt;</p>"),
    ("```\n<b>x</b>\n```", "<pre><code>&lt;b&gt;x&lt;/b&gt;</code></pre>"),
]


@pytest.fixture(scope="module")
def bundle():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    def call(cases):
        out = subprocess.run(
            [node, "--input-type=module", "-e", HARNESS, str(BUNDLE)],
            input=json.dumps(cases), capture_output=True, text=True,
            encoding="utf-8", check=True, timeout=60,
        )
        return json.loads(out.stdout)

    cases = ([["typed", t] for t in TYPED] + [["roundtrip", m] for m in ROUNDTRIP]
             + [["escape", e] for e, _ in ESCAPE] + [["render", r] for r, _ in RENDER] + [["protocols", None]])
    results = iter(call(cases))
    return {
        "typed": dict(zip(TYPED, results)),
        "roundtrip": dict(zip(ROUNDTRIP, results)),
        "escape": dict(zip((e for e, _ in ESCAPE), results)),
        "render": dict(zip((r for r, _ in RENDER), results)),
        "protocols": next(results),
    }


class TestTypedText:
    """Text typed into a paragraph comes back exactly as typed."""

    @pytest.mark.parametrize("text", TYPED)
    def test_it_reads_back_as_typed(self, bundle, text):
        md, back = bundle["typed"][text]
        assert back == f"paragraph:{text}", f"{text!r} was written {md!r}"


class TestRoundTrip:
    """Markdown the editor writes reads back as itself."""

    @pytest.mark.parametrize("markdown", ROUNDTRIP)
    def test_it_is_written_again_unchanged(self, bundle, markdown):
        assert bundle["roundtrip"][markdown] == markdown


class TestLineStarts:
    """`escapeLineStarts`: the start of a line, which the serializer leaves alone."""

    @pytest.mark.parametrize("text, escaped", ESCAPE)
    def test_escape(self, bundle, text, escaped):
        assert bundle["escape"][text] == escaped


class TestRender:
    """The schema builds the nodes: markup typed into a note stays text."""

    @pytest.mark.parametrize("markdown, html", RENDER)
    def test_render(self, bundle, markdown, html):
        assert bundle["render"][markdown] == html

    def test_safe_protocols_are_the_mail_and_web_schemes(self, bundle):
        assert bundle["protocols"] == ["http", "https", "mailto", "message", "outlook", "ftp"]


def test_the_bundle_is_built_from_editor():
    """The committed bundle says where it comes from, and every package in it
    has its license beside it."""
    head = BUNDLE.read_text(encoding="utf-8").splitlines()[0]
    assert "Built from editor/" in head
    licenses = BUNDLE.with_name("tiptap.js.LICENSES.txt").read_text(encoding="utf-8")
    pins = json.loads((BUNDLE.parents[3] / "editor" / "package.json").read_text(encoding="utf-8"))["devDependencies"]
    for name, version in pins.items():
        if name != "esbuild":   # the builder, not bundled
            assert f"{name} {version} (MIT)" in licenses, name
