"""Unit tests for Task ID formatting, the user prefix, and reference defaults (no server)."""

import pytest

from mktask.services import (
    FILES_ROUTE, REF_KINDS, SEED_RELATIONS, TASK_ID_PATTERN, TOP_LEVEL_DETAIL,
    default_label, event_phrase, format_task_id, user_prefix,
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
    ("task", "TKMA00000002", "", ""),                            # a link's label is the linked title, set by the service
])
def test_default_label(kind, href, body, expected):
    assert default_label(kind, href, body) == expected


def test_seed_relations_are_well_formed():
    import json
    seed = json.loads(SEED_RELATIONS.read_text())
    assert {r["forward"] for r in seed} == {"blocks", "relates to"}
    wordings = [w for r in seed for w in {r["forward"], r["backward"]}]
    assert wordings and len(wordings) == len({w.lower() for w in wordings}), "unique across both columns"
    assert all(w == w.strip() and w for w in wordings)
    assert next(r for r in seed if r["forward"] == "relates to")["backward"] == "relates to", "symmetric"


def test_reference_kinds():
    assert REF_KINDS == ("url", "text", "file", "task")
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
    (("ref_added", "Example", "url"), "Added a URL: Example"),
    (("ref_added", "a note", "text"), "Added a snippet: a note"),
    (("ref_added", "blocks TKMA00000002", "task"), "Linked: blocks TKMA00000002"),
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
