"""static/markdown.js under node: the Markdown a snippet renders to.

The renderer is handed a stub document that serializes what it is asked to
build, so each case reads as the markup the Detail pane would show — and a
case that smuggles markup in shows it arriving as text. Skipped without node.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MARKDOWN_JS = Path(__file__).resolve().parent.parent / "mktask" / "static" / "markdown.js"

# Loaded from a data: URL: without a package.json saying "module", node
# would read the file as CommonJS.
HARNESS = r"""
import { readFileSync } from "node:fs";
const src = readFileSync(process.argv[1], "utf8");
const { renderMarkdown } = await import("data:text/javascript," + encodeURIComponent(src));

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
const cases = JSON.parse(readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(cases.map((c) => renderMarkdown(c, doc).html)));
"""

LINK = ' target="_blank" rel="noopener noreferrer"'

CASES = [
    # inline
    ("**Call Bob** re: _budget_", "<p><strong>Call Bob</strong> re: <em>budget</em></p>"),
    ("*a **b** c*", "<p><em>a <strong>b</strong> c</em></p>"),
    ("~~gone~~ and `a*b*c`", "<p><del>gone</del> and <code>a*b*c</code></p>"),
    ("snake_case_name, 2 * 3 * 4", "<p>snake_case_name, 2 * 3 * 4</p>"),
    (r"\*literal\* stars", "<p>*literal* stars</p>"),
    ("one\ntwo", "<p>one<br>two</p>"),
    ("see [docs](https://x.example/a \"title\")",
     f'<p>see <a href="https://x.example/a"{LINK}>docs</a></p>'),
    ("at https://x.example/path.", f'<p>at <a href="https://x.example/path"{LINK}>https://x.example/path</a>.</p>'),
    ("[Wiki](https://en.wikipedia.org/wiki/Foo_(bar))",
     f'<p><a href="https://en.wikipedia.org/wiki/Foo_(bar)"{LINK}>Wiki</a></p>'),
    # blocks
    ("# Title #\n\ntext", "<h1>Title</h1><p>text</p>"),
    ("- one\n- two\n  - nested\n- three",
     "<ul><li><p>one</p></li><li><p>two</p><ul><li><p>nested</p></li></ul></li><li><p>three</p></li></ul>"),
    ("5. five\n6. six", '<ol start="5"><li><p>five</p></li><li><p>six</p></li></ol>'),
    ("- a\n- b\n\n1. c", "<ul><li><p>a</p></li><li><p>b</p></li></ul><ol><li><p>c</p></li></ol>"),
    ("> quoted\n> more", "<blockquote><p>quoted<br>more</p></blockquote>"),
    ("above\n\n---\n\nbelow", "<p>above</p><hr><p>below</p>"),
    ("```js\n**not bold**\n  kept\n```", "<pre><code>**not bold**\n  kept</code></pre>"),
    ("```\nunclosed", "<pre><code>unclosed</code></pre>"),
    ("", ""),
    # nothing typed becomes markup
    ("<script>alert(1)</script>", "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>"),
    ("<b>x</b> **y**", "<p>&lt;b&gt;x&lt;/b&gt; <strong>y</strong></p>"),
    ("[click](javascript:alert(1))", "<p>click</p>"),
    ("[click](data:text/html,x)", "<p>click</p>"),
    ('[x](https://a.example/"onmouseover="alert(1))',
     f'<p><a href="https://a.example/&quot;onmouseover=&quot;alert(1)"{LINK}>x</a></p>'),   # one attribute value
]


@pytest.fixture(scope="module")
def rendered():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    out = subprocess.run(
        [node, "--input-type=module", "-e", HARNESS, str(MARKDOWN_JS)],
        input=json.dumps([c for c, _ in CASES]), capture_output=True, text=True,
        encoding="utf-8", check=True, timeout=30,
    )
    return dict(zip((c for c, _ in CASES), json.loads(out.stdout)))


@pytest.mark.parametrize("text, html", CASES, ids=[c[:30] or "empty" for c, _ in CASES])
def test_markdown(rendered, text, html):
    assert rendered[text] == html
