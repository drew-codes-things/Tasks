from datetime import date

import pytest

from tasks.errors import TaskError
from tasks.repeat import clean_rule, describe, next_due_date, rule_from_text, rule_to_text

DAILY = {"kind": "daily"}


@pytest.mark.parametrize("rule, due, today, expected", [
    (DAILY, date(2026, 11, 13), date(2026, 11, 13), date(2026, 11, 14)),
    (DAILY, date(2026, 11, 10), date(2026, 11, 13), date(2026, 11, 13)),
    (DAILY, date(2026, 11, 20), date(2026, 11, 13), date(2026, 11, 21)),
    (DAILY, date(2026, 12, 31), date(2026, 12, 31), date(2027, 1, 1)),
    ({"kind": "weekdays"}, date(2026, 11, 13), date(2026, 11, 13), date(2026, 11, 16)),
    ({"kind": "weekdays"}, date(2026, 11, 16), date(2026, 11, 16), date(2026, 11, 17)),
    ({"kind": "weekly", "days": [0, 3]}, date(2026, 11, 16), date(2026, 11, 16), date(2026, 11, 19)),
    ({"kind": "weekly", "days": [0, 3]}, date(2026, 11, 19), date(2026, 11, 19), date(2026, 11, 23)),
    ({"kind": "weekly", "days": [4]}, date(2026, 11, 13), date(2026, 11, 13), date(2026, 11, 20)),
    ({"kind": "monthly", "day": 31}, date(2027, 1, 31), date(2027, 1, 31), date(2027, 2, 28)),
    ({"kind": "monthly", "day": 31}, date(2027, 2, 28), date(2027, 2, 28), date(2027, 3, 31)),
    ({"kind": "monthly", "day": 31}, date(2028, 1, 31), date(2028, 1, 31), date(2028, 2, 29)),
    ({"kind": "monthly", "day": 31}, date(2027, 3, 31), date(2027, 3, 31), date(2027, 4, 30)),
    ({"kind": "monthly", "day": 29}, date(2028, 1, 29), date(2028, 1, 29), date(2028, 2, 29)),
    ({"kind": "monthly", "day": 1}, date(2026, 12, 1), date(2026, 12, 1), date(2027, 1, 1)),
    ({"kind": "monthly", "day": 15}, date(2026, 11, 10), date(2026, 11, 10), date(2026, 11, 15)),
    ({"kind": "monthly", "day": 1}, date(2026, 8, 1), date(2026, 11, 13), date(2026, 12, 1)),
    ({"kind": "interval", "days": 3}, date(2026, 11, 13), date(2026, 11, 13), date(2026, 11, 16)),
    ({"kind": "interval", "days": 3}, date(2026, 11, 1), date(2026, 11, 13), date(2026, 11, 13)),
    ({"kind": "interval", "days": 3}, date(2026, 11, 1), date(2026, 11, 14), date(2026, 11, 16)),
])
def test_next_due_date(rule, due, today, expected):
    assert next_due_date(rule, due, today) == expected


def test_repeats_stop_at_the_end_of_the_date_range():
    assert next_due_date(DAILY, date(2999, 12, 30), date(2999, 12, 30)) == date(2999, 12, 31)
    assert next_due_date(DAILY, date(2999, 12, 31), date(2999, 12, 31)) is None
    assert next_due_date({"kind": "monthly", "day": 1}, date(2999, 12, 1), date(2999, 12, 1)) is None
    assert next_due_date({"kind": "interval", "days": 365}, date(2999, 6, 1), date(2999, 6, 1)) is None


@pytest.mark.parametrize("value", [
    "daily", {"kind": "yearly"}, {"kind": "weekly"}, {"kind": "weekly", "days": []}, {"kind": "weekly", "days": [7]},
    {"kind": "weekly", "days": [True]}, {"kind": "monthly", "day": 0}, {"kind": "monthly", "day": 32},
    {"kind": "monthly", "day": "1"}, {"kind": "interval", "days": 0}, {"kind": "interval", "days": 366},
    {"kind": "daily", "days": 2},
])
def test_bad_rules_are_refused(value):
    with pytest.raises(TaskError):
        clean_rule(value)


def test_rules_are_cleaned_and_described():
    assert clean_rule({"kind": "weekly", "days": [4, 0, 4]}) == {"kind": "weekly", "days": [0, 4]}
    assert describe({"kind": "weekly", "days": [0, 4]}) == "every Mon, Fri"
    assert describe({"kind": "monthly", "day": 1}) == "every month on the 1st"
    assert describe({"kind": "monthly", "day": 22}) == "every month on the 22nd"
    assert describe({"kind": "monthly", "day": 13}) == "every month on the 13th"
    assert describe({"kind": "interval", "days": 3}) == "every 3 days"
    assert describe({"kind": "weekdays"}) == "every weekday"
    assert describe(None) is None


def test_stored_rules_round_trip_and_damaged_ones_read_as_none():
    rule = {"kind": "monthly", "day": 31}
    assert rule_from_text(rule_to_text(rule)) == rule
    assert rule_from_text("{not json") is None
    assert rule_from_text('{"kind": "monthly", "day": 99}') is None
    assert rule_from_text(None) is None
