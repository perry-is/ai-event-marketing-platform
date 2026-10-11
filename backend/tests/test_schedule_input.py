from datetime import date

import pytest

from app.schedule_input import parse_schedule_answer, parse_source_schedule


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("Oct 6", "2026-10-06"),
        ("October 6 at noon", "2026-10-06"),
        ("10/06/26 12:00 PM", "2026-10-06"),
        ("10-06-2026 mountain 12:00pm -2:00 pm", "2026-10-06"),
        ("2026-10-06", "2026-10-06"),
    ],
)
def test_common_schedule_dates_keep_date_and_infer_year(source, expected):
    value = parse_source_schedule(source, "America/Denver", date(2026, 10, 1))
    assert value["date_only"] == expected
    assert value["year_inferred"] is (source in {"Oct 6", "October 6 at noon"})


def test_january_without_year_rolls_into_next_local_year():
    value = parse_source_schedule(
        "Jan 12 from 11:30 AM to 1 PM", "America/Denver", date(2026, 10, 9)
    )
    assert value["local_start"] == "2027-01-12T11:30:00"
    assert value["local_end"] == "2027-01-12T13:00:00"
    assert value["year_inferred"] is True


def test_past_inferred_date_warns_without_changing_components():
    value = parse_source_schedule("Oct 6 at noon", "America/Denver", date(2026, 10, 9))
    assert value["date_only"] == "2026-10-06"
    assert value["past_date"] is True
    assert "has passed" in value["interpretation_note"]


def test_ambiguous_numeric_date_is_documented_as_us_month_day():
    value = parse_source_schedule("10/06/26 12pm", "America/Denver", date(2026, 1, 1))
    assert value["date_only"] == "2026-10-06"
    assert "U.S. month/day convention" in value["interpretation_note"]


def test_time_answer_accepts_screenshot_format_and_preserves_end_time():
    value = parse_schedule_answer("10-06-2026 mountain 12:00pm -2:00 pm", "America/Denver")
    assert value["local_start"] == "2026-10-06T12:00:00"
    assert value["local_end"] == "2026-10-06T14:00:00"
    assert value["timezone"] == "America/Denver"
