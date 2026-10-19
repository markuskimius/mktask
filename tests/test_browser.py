"""The browser side end to end: a real server, headless Chrome, real keys.

What only a browser can show: the formatted-text editor in a dialog (Cmd/
Ctrl+B mid-word, a typed list, Enter as a newline), a plain note made
formatted character for character, the Text pane's edit in place (the
cursor where the click landed, the text not moving, the edit held open
across a new pick, one save one version), a paste that keeps its
formatting only when it had some, and a rendered snippet that turns no
typed markup into markup. Every page must also leave the console clean.

Chrome is driven over the DevTools protocol with aiohttp, which mkio
already brings in; nothing new is installed. Skipped when no Chrome is
found (MKTASK_CHROME names one explicitly).
"""

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import aiohttp
import pytest

from tests.test_server import _start, _stop

CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
CHROME_NAMES = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"]


def _find_chrome():
    if os.environ.get("MKTASK_CHROME"):
        return os.environ["MKTASK_CHROME"] if Path(os.environ["MKTASK_CHROME"]).is_file() else None
    for name in CHROME_NAMES:
        if shutil.which(name):
            return shutil.which(name)
    return next((p for p in CHROME_PATHS if Path(p).is_file()), None)


@pytest.fixture(scope="module")
def chrome(tmp_path_factory):
    """Headless Chrome on a port it picks itself, read back from the profile."""
    exe = _find_chrome()
    if exe is None:
        pytest.skip("no Chrome or Chromium found (set MKTASK_CHROME)")
    profile = tmp_path_factory.mktemp("chrome")
    args = [exe, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check", "--disable-gpu", "about:blank"]
    if sys.platform.startswith("linux") and os.geteuid() == 0:
        args.insert(1, "--no-sandbox")   # Chrome refuses to sandbox as root
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    port_file = profile / "DevToolsActivePort"
    deadline = time.time() + 20
    while not (port_file.is_file() and port_file.read_text(encoding="utf-8").strip()):
        if proc.poll() is not None or time.time() > deadline:
            proc.kill()
            pytest.skip("Chrome did not start")
        time.sleep(0.1)
    yield f"http://127.0.0.1:{port_file.read_text(encoding='utf-8').split()[0]}"
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """One server on a database file, so a test can read what was written."""
    db = tmp_path_factory.mktemp("db") / "browser.db"
    proc, base = _start("--user", "mark", db=str(db))
    yield base, db
    _stop(proc)


def _rows(db, sql, *params):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, params)]
    finally:
        c.close()


async def _txn(base, op, data):
    """A transaction from another client, as a second browser would send it."""
    async with aiohttp.ClientSession() as s, s.ws_connect(base + "/ws") as ws:
        await ws.send_json({"service": "tasks", "type": "transaction", "op": op, "data": data,
                            "ref": f"browser-{op}-{time.monotonic_ns()}"})
        resp = json.loads((await asyncio.wait_for(ws.receive(), 5)).data)
        assert resp["type"] == "result", resp
        return resp


async def _add_task(base, db, title, notes="", fmt="plain"):
    await _txn(base, "add", {"title": title, "notes": notes, "notes_format": fmt})
    return _rows(db, "SELECT task_id FROM tasks WHERE title = ?", title)[0]["task_id"]


# Found in the page and in every shadow root under it (mkui's tables).
HELPERS = """
window.__all = (root, sel) => {
  const out = [...root.querySelectorAll(sel)];
  for (const e of root.querySelectorAll("*")) if (e.shadowRoot) out.push(...__all(e.shadowRoot, sel));
  return out;
};
window.__app = () => document.querySelector("mkui-app")?.app;
window.__dialog = () => __all(document, ".mkui-dialog-content").filter((e) => e.offsetParent).at(-1) ?? null;
window.__word = (rootSel, word) => {
  const root = document.querySelector(rootSel);
  const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let n; (n = walk.nextNode());) {
    const i = n.data.indexOf(word);
    if (i < 0 || n.parentElement.closest(".richtext-bar")) continue;
    const r = document.createRange();
    r.setStart(n, i); r.setEnd(n, i + word.length);
    const b = r.getBoundingClientRect();
    return [b.x, b.y, b.width, b.height];
  }
  return null;
};
"""


class Page:
    """One tab: evaluate, wait, type, click, and the console's complaints."""

    def __init__(self, ws):
        self.ws = ws
        self.n = 0
        self.problems = []
        self.mod = 4   # Cmd; set from the page's own platform test once loaded

    async def call(self, method, **params):
        self.n += 1
        mid = self.n
        await self.ws.send_json({"id": mid, "method": method, "params": params})
        while True:
            m = json.loads((await asyncio.wait_for(self.ws.receive(), 15)).data)
            if m.get("method") == "Runtime.exceptionThrown":
                self.problems.append(m["params"]["exceptionDetails"].get("exception", {}).get("description", "exception"))
            elif m.get("method") == "Runtime.consoleAPICalled" and m["params"]["type"] in ("error", "warning"):
                self.problems.append(" ".join(str(a.get("value", a.get("description", ""))) for a in m["params"]["args"]))
            if m.get("id") == mid:
                return m.get("result", m)

    async def js(self, expr):
        r = await self.call("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in r:
            raise AssertionError(f"{expr[:80]}: {r['exceptionDetails'].get('exception', {}).get('description')}")
        return r.get("result", {}).get("value")

    async def wait(self, expr, timeout=8, what=None):
        deadline = time.monotonic() + timeout
        while True:
            value = await self.js(expr)
            if value:
                return value
            if time.monotonic() > deadline:
                raise AssertionError(f"timed out waiting for {what or expr}")
            await asyncio.sleep(0.1)

    async def key(self, key, code=None, vk=0, mods=0, text=None):
        extra = {"text": text} if text else {}
        await self.call("Input.dispatchKeyEvent", type="keyDown", key=key, code=code or key,
                        windowsVirtualKeyCode=vk, modifiers=mods, **extra)
        await self.call("Input.dispatchKeyEvent", type="keyUp", key=key, code=code or key,
                        windowsVirtualKeyCode=vk, modifiers=mods)

    async def type(self, text):
        for ch in text:
            if ch == "\n":
                await self.key("Enter", vk=13, text="\r")
            else:
                await self.key(ch, text=ch)

    async def chord(self, letter):
        """Cmd+letter on a Mac, Ctrl+letter elsewhere."""
        await self.key(letter, code="Key" + letter.upper(), vk=ord(letter.upper()), mods=self.mod)

    async def submit(self):
        await self.key("Enter", vk=13, mods=self.mod)

    async def escape(self):
        await self.key("Escape", vk=27)

    async def click(self, x, y):
        for kind in ("mousePressed", "mouseReleased"):
            await self.call("Input.dispatchMouseEvent", type=kind, x=x, y=y, button="left", clickCount=1)

    async def select_task(self, task_id):
        """Select a task in the Tasks pane, once the table holds it."""
        await self.wait(f"(() => {{ const r = __app().fireAction('table.select', {{ pane: 'tasks', keys: [{json.dumps(task_id)}] }});"
                        f" return r && r.ok !== false && !(r.missing ?? []).length; }})()", what=f"task {task_id} in the table")
        await asyncio.sleep(0.3)

    async def button(self, label):
        ok = await self.js(f"""(() => {{ const b = __all(document, 'button').filter(b => b.offsetParent)
            .find(b => b.textContent.trim() === {json.dumps(label)}); b?.click(); return !!b; }})()""")
        assert ok, f"no visible {label!r} button"


@pytest.fixture
async def page(chrome, server):
    base, _ = server
    async with aiohttp.ClientSession() as s:
        async with s.put(f"{chrome}/json/new?about:blank") as r:
            target = await r.json(content_type=None)
        async with s.ws_connect(target["webSocketDebuggerUrl"], max_msg_size=0) as ws:
            p = Page(ws)
            await p.call("Runtime.enable")
            await p.call("Page.enable")
            await p.call("Emulation.setDeviceMetricsOverride", width=1400, height=900, deviceScaleFactor=1, mobile=False)
            await p.call("Page.addScriptToEvaluateOnNewDocument", source=HELPERS)
            await p.call("Page.navigate", url=base + "/")
            await p.wait("document.readyState === 'complete' && !!window.__app?.()?.fireAction", what="the app")
            await p.wait("document.body.innerText.includes('Connected')", what="the server connection")
            p.mod = 4 if await p.js("/Mac|iPhone|iPad/.test(navigator.platform)") else 2
            yield p
            assert not p.problems, f"the console complained: {p.problems}"
        async with s.get(f"{chrome}/json/close/{target['id']}"):
            pass


async def _focus_editor(page, index=0):
    """Put the cursor at the end of a dialog's visible editor."""
    await page.js(f"""(() => {{ const pm = [...__dialog().querySelectorAll('.ProseMirror')].filter(e => e.offsetParent)[{index}];
        pm.focus(); const r = document.createRange(); r.selectNodeContents(pm); r.collapse(false);
        const s = getSelection(); s.removeAllRanges(); s.addRange(r); return 1; }})()""")


class TestDialogEditor:
    """The richtext field in a real dialog."""

    async def test_new_writes_what_the_formatting_shows(self, page, server):
        base, db = server
        await page.button("New")
        await page.wait("!!__dialog()?.querySelector('.ProseMirror')", what="the New dialog")
        assert await page.js("document.activeElement?.tagName") == "INPUT", "the title takes the first focus"
        await page.type("Formatted as typed")
        await _focus_editor(page)
        await page.type("un")
        await page.chord("b")
        await page.type("believ")
        await page.chord("b")
        await page.type("able\n1. ")
        await page.chord("b")
        await page.type("First")
        await page.chord("b")
        await page.type(" step")
        assert await page.js("!!__dialog()"), "Enter in the editor is a new line, not a submit"
        assert "<strong>believ</strong>" in await page.js("__dialog().querySelector('.ProseMirror').innerHTML")
        assert await page.js("!!__dialog().querySelector('.ProseMirror ol li strong')"), "a typed `1. ` is a list"
        await page.submit()
        await page.wait("!__dialog()", what="the dialog to close")
        row = _rows(db, "SELECT notes, notes_format FROM tasks WHERE title = 'Formatted as typed'")[0]
        assert row == {"notes": "un**believ**able\n\n1. **First** step", "notes_format": "markdown"}

    async def test_edit_makes_a_plain_note_formatted_as_written(self, page, server):
        base, db = server
        task = await _add_task(base, db, "Plain to formatted", "Call *Bob* at 5\n1. not a list")
        await page.select_task(task)
        await page.button("Edit")
        await page.wait("!!__dialog()?.querySelector('textarea')", what="the plain notes box")
        assert not await page.js("[...__dialog().querySelectorAll('.ProseMirror')].some(e => e.offsetParent)"), \
            "a plain note opens as written, not in the editor"
        await page.js("""(() => { const s = [...__dialog().querySelectorAll('select')]
            .find(s => [...s.options].some(o => o.value === 'markdown') && s.offsetParent);
            s.value = 'markdown'; s.dispatchEvent(new Event('change', { bubbles: true })); return 1; })()""")
        await page.wait("[...__dialog().querySelectorAll('.ProseMirror')].some(e => e.offsetParent)", what="the editor")
        shown = await page.js("[...__dialog().querySelectorAll('.ProseMirror')].find(e => e.offsetParent).innerText")
        assert shown.replace("\n\n", "\n").strip() == "Call *Bob* at 5\n1. not a list", "taken character for character"
        await page.submit()
        await page.wait("!__dialog()", what="the dialog to close")
        row = _rows(db, "SELECT notes, notes_format FROM tasks WHERE task_id = ?", task)[0]
        assert row["notes_format"] == "markdown"
        assert row["notes"] == "Call \\*Bob\\* at 5  \n1\\. not a list", "escaped, so it reads back as written"


class TestTextPane:
    """The Text pane: the text alone, edited where it is read."""

    async def _start_edit(self, page, word, after=False):
        """Click into `word` (its middle, or just past its end) to start editing."""
        before = await page.wait(f"__word('.textpane', {json.dumps(word)})", what=f"{word!r} in the Text pane")
        x = before[0] + before[2] + 1 if after else before[0] + before[2] / 2
        await page.click(x, before[1] + before[3] / 2)
        await page.wait("!!document.querySelector('.textpane .ProseMirror')", what="the editor")
        return before

    async def test_a_click_edits_in_place_and_one_save_is_one_version(self, page, server):
        base, db = server
        task = await _add_task(base, db, "Edit in place", "un**believ**able\n\n1. **First** step", "markdown")
        other = await _add_task(base, db, "Somewhere else", "Other notes", "markdown")
        await page.select_task(task)
        assert await page.js("document.querySelector('.textpane-view').innerHTML") == \
            '<div class="textpane-text task-refs-md"><p>un<strong>believ</strong>able</p>' \
            '<ol><li><p><strong>First</strong> step</p></li></ol></div>', "the text alone, rendered"
        before = await self._start_edit(page, "believ")
        after = await page.js("__word('.textpane', 'believ')")
        assert [round(v) for v in after[:2]] == [round(v) for v in before[:2]], "the text does not move"
        assert await page.js("getSelection().anchorNode.data") == "believ"
        assert await page.js("getSelection().anchorOffset") == 3, "the cursor lands where the click did"
        bar, actions = await page.js("""(() => { const b = document.querySelector('.textpane .richtext-bar').getBoundingClientRect();
            const t = document.querySelector('.textpane .ProseMirror').getBoundingClientRect(); return [b.top, t.bottom]; })()""")
        assert bar >= actions - 1, "the toolbar is under the text"
        assert await page.js("!!document.querySelector('.textpane .richtext-bar .textpane-actions .mkui-btn-primary')"), \
            "Save is on the toolbar's row"
        await page.type("XX")
        assert await page.js("document.querySelector('.textpane-status').textContent") == "Unsaved"

        await page.select_task(other)
        assert await page.js("!!document.querySelector('.textpane .ProseMirror')"), "the edit stays open"
        assert await page.js("document.querySelector('.textpane-status').textContent") == "Save or cancel to move on"

        version = _rows(db, "SELECT _mkio_version FROM tasks WHERE task_id = ?", task)[0]["_mkio_version"]
        await page.submit()
        await page.wait("!document.querySelector('.textpane .ProseMirror')", what="the save to close the edit")
        row = _rows(db, "SELECT notes, notes_format, last_event, _mkio_version FROM tasks WHERE task_id = ?", task)[0]
        assert row == {"notes": "un**belXXiev**able\n\n1. **First** step", "notes_format": "markdown",
                       "last_event": "Edited", "_mkio_version": version + 1}
        await page.wait("document.querySelector('.textpane').innerText.includes('Other notes')",
                        what="the pane to follow the pick made while editing")

    async def test_escape_cancels_and_writes_nothing(self, page, server):
        base, db = server
        task = await _add_task(base, db, "Cancel an edit", "Call *Bob* at 5")
        await page.select_task(task)
        await self._start_edit(page, "Bob")
        assert await page.js("document.querySelector('.textpane .ProseMirror').innerText") == "Call *Bob* at 5", \
            "a plain note opens as written"
        await page.type("ZZ")
        await page.escape()
        await page.wait("!document.querySelector('.textpane .ProseMirror')", what="Escape to close the edit")
        row = _rows(db, "SELECT notes, notes_format, _mkio_version FROM tasks WHERE task_id = ?", task)[0]
        assert row == {"notes": "Call *Bob* at 5", "notes_format": "plain", "_mkio_version": 1}

    async def test_a_change_made_elsewhere_is_flagged_not_applied(self, page, server):
        base, db = server
        task = await _add_task(base, db, "Edited twice", "mine to change", "markdown")
        await page.select_task(task)
        await self._start_edit(page, "change", after=True)
        await page.type("d")
        await _txn(base, "edit_notes", {"task_id": task, "notes": "someone else's", "updated_at": "2026-01-01 00:00:00"})
        await page.wait("document.querySelector('.textpane-status').textContent === 'Changed elsewhere — saving replaces it'",
                        what="the changed-elsewhere warning")
        assert await page.js("document.querySelector('.textpane .ProseMirror').innerText") == "mine to changed", \
            "the edit is kept"
        await page.escape()
        await page.wait("document.querySelector('.textpane').innerText.includes(\"someone else's\")",
                        what="the other change once the edit is cancelled")

    async def test_a_snippet_keeps_its_typed_label(self, page, server):
        base, db = server
        task = await _add_task(base, db, "Snippet in place")
        await _txn(base, "add_ref", {"task_id": task, "kind": "text", "body": "Typed body", "label": "My label",
                                     "format": "markdown"})
        ref_id = _rows(db, "SELECT ref_id FROM task_refs WHERE task_id = ?", task)[0]["ref_id"]
        await page.select_task(task)
        await page.wait("__all(document, 'tr').some(r => r.offsetParent && r.textContent.includes('My label'))",
                        what="the snippet in the References pane")
        x, y = await page.js("""(() => { const r = __all(document, 'tr').filter(r => r.offsetParent && r.textContent.includes('My label')).at(-1);
            const b = r.getBoundingClientRect(); return [b.x + 40, b.y + b.height / 2]; })()""")
        await page.click(x, y)
        await self._start_edit(page, "body", after=True)
        await page.type(" more")
        await page.button("Save")
        await page.wait("!document.querySelector('.textpane .ProseMirror')", what="Save to close the edit")
        row = _rows(db, "SELECT body, label, format FROM task_refs WHERE ref_id = ?", ref_id)[0]
        assert row == {"body": "Typed body more", "label": "My label", "format": "markdown"}


class TestPasteAndRender:
    """The Detail pane: what a paste becomes, and what a snippet renders as."""

    async def _paste(self, page, text, html=""):
        await page.js(f"""(() => {{ document.activeElement?.blur(); const dt = new DataTransfer();
            dt.setData('text/plain', {json.dumps(text)}); if ({json.dumps(html)}) dt.setData('text/html', {json.dumps(html)});
            window.dispatchEvent(new ClipboardEvent('paste', {{ clipboardData: dt, bubbles: true, cancelable: true }})); return 1; }})()""")

    async def test_a_paste_is_formatted_only_when_what_was_copied_was(self, page, server):
        base, db = server
        task = await _add_task(base, db, "Pasted into")
        await page.select_task(task)
        cases = [
            ("Bold and it\nOne\nTwo",
             '<b style="font-weight:normal;" id="docs-internal-guid-1"><p><span style="font-weight:700;">Bold</span>'
             '<span> and </span><span style="font-style:italic;">it</span></p><ol><li><p>One</p></li><li><p>Two</p></li></ol></b>',
             {"kind": "text", "format": "markdown", "body": "**Bold** and *it*\n\n1. One\n2. Two", "label": "Bold and it"}),
            ("def f():\n    return 1",
             '<div style="white-space:pre;"><div><span style="color:#569cd6;">def</span><span> f():</span></div>'
             '<div><span>    return 1</span></div></div>',
             {"kind": "text", "format": "plain", "body": "def f():\n    return 1", "label": "def f():"}),
            ("https://a.example/page", '<a href="https://a.example/page">https://a.example/page</a>',
             {"kind": "url", "format": "plain", "body": "", "label": "https://a.example/page"}),
            ("click me", '<p><a href="javascript:alert(1)">click me</a></p>',
             {"kind": "text", "format": "plain", "body": "click me", "label": "click me"}),
        ]
        for i, (text, html, expected) in enumerate(cases, start=1):
            await self._paste(page, text, html)
            deadline = time.monotonic() + 5
            while len(rows := _rows(db, "SELECT kind, format, body, label FROM task_refs WHERE task_id = ? ORDER BY ref_id", task)) < i:
                assert time.monotonic() < deadline, f"paste {i} added nothing"
                await asyncio.sleep(0.1)
            assert rows[-1] == expected, text

    async def test_a_snippet_renders_no_typed_markup(self, page, server):
        base, db = server
        task = await _add_task(base, db, "Rendered")
        await _txn(base, "add_ref", {"task_id": task, "kind": "text", "format": "markdown",
                                     "body": "**bold** [bad](javascript:alert(1)) [ok](https://a.example) <img src=x onerror=alert(1)>\nnext"})
        await page.select_task(task)
        html = await page.wait("document.querySelector('.task-refs-body.task-refs-md')?.innerHTML", what="the rendered snippet")
        assert "<strong>bold</strong>" in html
        assert 'href="https://a.example"' in html
        # Raw HTML is read through the schema, which has no image: dropped,
        # with nothing it carried (an attribute, a script) left behind.
        assert "javascript" not in html and "<img" not in html and "onerror" not in html, html
        assert await page.js("getComputedStyle(document.querySelector('.task-refs-body.task-refs-md')).whiteSpace") == "pre-wrap", \
            "a single newline shows as a line break"
