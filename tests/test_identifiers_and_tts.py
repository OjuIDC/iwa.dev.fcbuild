import pytest

from src.thread_ids import format_thread_id, is_valid_club_id, parse_thread_id


@pytest.mark.parametrize("value", ["330323", "0", "123456789012"])
def test_club_id_accepts_ascii_digits(value):
    assert is_valid_club_id(value)


@pytest.mark.parametrize("value", ["٣٣٠٣٢٣", "３３０３２３", "-330323", "+330323", " 330323", "330323 ",
                                   "330.323", "33a0323", "", "330323\n", "1234567890123", None, 330323])
def test_club_id_rejects_unicode_digits(value):
    assert not is_valid_club_id(value)


def test_thread_id_format_roundtrip():
    from datetime import date
    tid = format_thread_id("330323", date(2026, 10, 1), 7)
    assert tid == "fc27-330323-run-20261001-007"
    assert parse_thread_id(tid) == {"club": "330323", "date": "20261001", "seq": "007"}
    assert parse_thread_id(tid + "\n") is None
