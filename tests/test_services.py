"""Unit tests for Task ID formatting, the user prefix, and reference defaults (no server)."""

import pytest

from mktask.services import (
    FILES_ROUTE, REF_FORMATS, REF_KINDS, SEED_RELATIONS, TASK_ID_PATTERN, TOP_LEVEL_DETAIL,
    default_label, event_phrase, format_task_id, plain_text, user_prefix,
)


@pytest.mark.parametrize("user, prefix", [
    ("mark", "MA"),
    ("Mark Kim", "MA"),
    ("m", "MX"),
    ("", "XX"),
    (None, "XX"),
    ("_bob.k", "BO"),
    ("a1", "A1"),
    ("élan", "LA"),        # non-ASCII letters are skipped, not transliterated
    ("--", "XX"),
])
def test_user_prefix(user, prefix):
    assert user_prefix(user) == prefix


@pytest.mark.parametrize("number, expected", [
    (1, "TKMA00000001"),
    (42, "TKMA00000042"),
    (99999999, "TKMA99999999"),
    (100000000, "TKMA100000000"),
    (12345678901, "TKMA12345678901"),
])
def test_format_task_id_pads_to_eight_and_never_wraps(number, expected):
    task_id = format_task_id("MA", number)
    assert task_id == expected
    assert TASK_ID_PATTERN.match(task_id)


@pytest.mark.parametrize("bad", ["TKMA0000001", "tkma00000001", "TKM00000001", "TKMA0000000a", "TXMA00000001", ""])
def test_pattern_rejects_malformed_ids(bad):
    assert not TASK_ID_PATTERN.match(bad)


# ─── References ────────────────────────────────────────────────────

@pytest.mark.parametrize("kind, href, body, expected", [
    ("url", "https://example.com/a?b=c", "", "https://example.com/a?b=c"),
    ("file", "/files/3f9a.png", "", "3f9a.png"),
    ("text", "Subject: hi\n\nbody", "", ""),                      # body, not href, feeds a snippet
    ("text", "", "\n  \nSubject: hi\nbody", "Subject: hi"),       # first non-blank line, stripped
    ("text", "", "x" * 100, "x" * 79 + "…"),                     # long lines are truncated
    ("text", "", "   ", ""),
    ("task", "TKMA00000002", "", ""),                            # a task reference's label is the related task's title, set by the service
])
def test_default_label(kind, href, body, expected):
    assert default_label(kind, href, body) == expected


@pytest.mark.parametrize("line, words", [
    ("**Call Bob** re: _budget_", "Call Bob re: budget"),
    ("## Notes from the *Monday* call", "Notes from the Monday call"),
    ("- [x] ~~old~~ item", "[x] old item"),
    ("1. First, see [the spec](https://example.com/spec)", "First, see the spec"),
    ("> quoted `code`", "quoted code"),
    ("snake_case_name and 2 * 3 * 4", "snake_case_name and 2 * 3 * 4"),   # not emphasis
    (r"\*literal\* stars", "*literal* stars"),
    ("Plain words", "Plain words"),
    # What the editor writes: entities, every escaped punctuation, a break
    ("Q&amp;A &lt;tag&gt; &amp; more", "Q&A <tag> & more"),
    ("## Agenda &amp; notes", "Agenda & notes"),
    ("&#32;   indented", "indented"),                 # a leading space, kept from Markdown
    ("&#9;Tabbed", "Tabbed"),
    ("&nbsp;", ""),                                   # a blank line the editor kept
    ("&#169; 2026 &#x2014; ok", "\u00a9 2026 \u2014 ok"),
    ("&#42;not&#42; emphasis", "*not* emphasis"),       # decoded last: a character, not a marker
    (r"\<b\> and \| pipe \{x\}", "<b> and | pipe {x}"),
    (r"1\. not a list", "1. not a list"),
    (r"\# not a heading", "# not a heading"),
    (r"\* not a list", "* not a list"),
    (r"\-- sig", "-- sig"),
    ("line\\", "line"),                              # a backslash break ends the line
    (r"C:\\path\\", "C:\\path\\"),                   # an escaped backslash is a character
])
def test_plain_text(line, words):
    assert plain_text(line) == words


@pytest.mark.parametrize("body, fmt, expected", [
    ("**Call Bob** re: budget", "markdown", "Call Bob re: budget"),
    ("**Call Bob** re: budget", "plain", "**Call Bob** re: budget"),   # plain text is as typed
    ("```\ncode\n```\n# Heading", "markdown", "code"),                # a fence line is not a label
    ("#\n\n- one", "markdown", "one"),                                 # a bare marker says nothing
    ("&nbsp;\n\nreal first", "markdown", "real first"),                # a kept blank line is blank
    ("\n\nafter a blank line", "markdown", "after a blank line"),
    ("  \nafter a break", "markdown", "after a break"),
    ("\\--   \nSig", "markdown", "--"),                               # a signature's dashes
    ("Q&amp;A", "plain", "Q&amp;A"),                                   # plain text is never decoded
])
def test_default_label_of_markdown(body, fmt, expected):
    assert default_label("text", "", body, fmt) == expected


def test_seed_relations_are_well_formed():
    import json
    seed = json.loads(SEED_RELATIONS.read_text(encoding="utf-8"))
    assert {r["forward"] for r in seed} == {"blocks", "relates to"}
    wordings = [w for r in seed for w in {r["forward"], r["backward"]}]
    assert wordings and len(wordings) == len({w.lower() for w in wordings}), "unique across both columns"
    assert all(w == w.strip() and w for w in wordings)
    assert next(r for r in seed if r["forward"] == "relates to")["backward"] == "relates to", "symmetric"


def test_reference_kinds():
    assert REF_KINDS == ("url", "text", "file", "task")
    assert REF_FORMATS == ("plain", "markdown")
    assert FILES_ROUTE == "/files/"


@pytest.mark.parametrize("args, phrase", [
    (("created",), "Created"),
    (("split_from", "TKMA00000001"), "Split from TKMA00000001"),
    (("split_to", "Wire the pane"), "Split into Wire the pane"),
    (("edited",), "Edited"),
    (("moved", "TKMA00000007"), "Moved under TKMA00000007"),
    (("moved", TOP_LEVEL_DETAIL), "Moved to the top level"),
    (("moved",), "Moved to the top level"),
    (("completed",), "Completed"),
    (("reopened",), "Reopened"),
    (("ref_added", "shot.png", "file"), "Attached a file: shot.png"),
    (("ref_added", "Example", "url"), "Added a link: Example"),
    (("ref_added", "a note", "text"), "Added a snippet: a note"),
    (("ref_added", "blocks TKMA00000002", "task"), "Related: blocks TKMA00000002"),
    (("ref_edited", "Example"), "Edited a reference: Example"),
    (("ref_deleted", "Example"), "Removed a reference: Example"),
])
def test_event_phrase(args, phrase):
    assert event_phrase(*args) == phrase


def test_event_phrase_never_returns_nothing():
    """It fills a column the blotter shows, so an action nobody thought to
    word must still read as something rather than as a blank cell."""
    for action in ("undone", "redone", "something_new"):
        assert event_phrase(action).strip()
    assert event_phrase("ref_added", "", "file") == "Attached a file"
