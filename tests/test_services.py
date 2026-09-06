"""Unit tests for Task ID formatting and the user prefix (no server)."""

import pytest

from mktask.services import TASK_ID_PATTERN, format_task_id, user_prefix


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
