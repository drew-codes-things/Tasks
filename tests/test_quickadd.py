from datetime import date

import pytest
from conftest import london

from tasks.errors import TaskError
from tasks.quickadd import parse_quick_add, parse_schedule

FRIDAY = london(2026, 11, 13, 10, 0)


def parsed(text, now=FRIDAY):
    return parse_quick_add(text, now).summary()


def test_the_three_examples_from_the_brief():
    assert parsed("pay rent every month on the 1st") == {
        "title": "pay rent", "dueDate": "2026-12-01", "dueTime": None, "priority": None, "list": None,
        "repeat": {"kind": "monthly", "day": 1}, "repeatText": "every month on the 1st"}
    call = parsed("call mum tomorrow 5pm")
    assert (call["title"], call["dueDate"], call["dueTime"]) == ("call mum", "2026-11-14", "17:00")
    dentist = parsed("dentist fri 9:30 !1 #health", now=london(2026, 11, 12, 10, 0))
    assert (dentist["title"], dentist["dueDate"], dentist["dueTime"], dentist["priority"], dentist["list"]) == (
        "dentist", "2026-11-13", "09:30", 1, "health")


def test_rent_typed_on_the_first_is_due_that_day():
    assert parsed("pay rent every month on the 1st", now=london(2026, 12, 1, 8, 0))["dueDate"] == "2026-12-01"


def test_a_bare_weekday_on_that_weekday_means_a_week_later():
    assert parsed("dentist fri", now=FRIDAY)["dueDate"] == "2026-11-20"


@pytest.mark.parametrize("now, text, expected", [
    (london(2026, 11, 16), "standup next monday", "2026-11-23"),
    (london(2026, 11, 16), "standup monday", "2026-11-23"),
    (london(2026, 11, 15), "standup next monday", "2026-11-16"),
    (london(2026, 11, 18), "standup next monday", "2026-11-23"),
    (london(2026, 11, 18), "standup monday", "2026-11-23"),
    (london(2026, 11, 18), "bins next friday", "2026-11-27"),
    (london(2026, 11, 18), "bins friday", "2026-11-20"),
    (london(2026, 11, 18), "plan next week", "2026-11-23"),
    (london(2026, 11, 22), "plan next week", "2026-11-23"),
])
def test_next_weekday_rules(now, text, expected):
    assert parsed(text, now)["dueDate"] == expected


@pytest.mark.parametrize("now, text, expected", [
    (london(2026, 12, 31), "party tomorrow", "2027-01-01"),
    (london(2026, 12, 31), "party 1 jan", "2027-01-01"),
    (london(2026, 12, 31), "party dec 31", "2026-12-31"),
    (london(2026, 11, 13), "x 13 nov", "2026-11-13"),
    (london(2026, 11, 14), "x 13 nov", "2027-11-13"),
    (london(2026, 1, 31), "x in 1 days", "2026-02-01"),
    (london(2026, 2, 28), "x tomorrow", "2026-03-01"),
    (london(2028, 2, 28), "x tomorrow", "2028-02-29"),
    (london(2026, 11, 13), "x in 2 weeks", "2026-11-27"),
    (london(2026, 11, 13), "x on 1 dec", "2026-12-01"),
    (london(2026, 11, 13), "x 1st of december 2027", "2027-12-01"),
    (london(2026, 11, 13), "x december 25th", "2026-12-25"),
    (london(2026, 11, 13), "x 25/12", "2026-12-25"),
    (london(2026, 11, 13), "x 31/12/2027", "2027-12-31"),
    (london(2026, 11, 13), "x 2027-01-31", "2027-01-31"),
])
def test_calendar_dates(now, text, expected):
    result = parsed(text, now)
    assert result["dueDate"] == expected
    assert result["title"] == text.split()[0]


@pytest.mark.parametrize("text", ["meet 31 nov", "meet 30/02", "meet 29 feb 2027", "meet 2027-02-29", "meet 1/13", "meet 3000-01-01"])
def test_days_that_do_not_exist_stay_in_the_title(text):
    result = parsed(text)
    assert result["dueDate"] is None
    assert result["title"] == text


def test_leap_day_without_a_year_is_the_next_one_that_exists():
    assert parsed("meet 29 feb")["dueDate"] == "2028-02-29"
    assert parsed("meet 29/02/2028")["dueDate"] == "2028-02-29"
    assert parsed("meet 29 feb", now=london(2028, 2, 29))["dueDate"] == "2028-02-29"
    assert parsed("meet 29 feb", now=london(2028, 3, 1))["dueDate"] == "2032-02-29"


@pytest.mark.parametrize("now, text, expected", [
    (london(2026, 11, 13), "every month on the 31st", "2026-11-30"),
    (london(2027, 2, 10), "every month on the 29th", "2027-02-28"),
    (london(2028, 2, 10), "every month on the 29th", "2028-02-29"),
    (london(2026, 12, 15), "monthly on the 1st", "2027-01-01"),
    (london(2026, 11, 13), "every 15th", "2026-11-15"),
    (london(2026, 11, 13), "monthly", "2026-11-13"),
])
def test_monthly_repeats_start_on_the_right_day(now, text, expected):
    result = parsed(f"bills {text}", now)
    assert result["dueDate"] == expected
    assert result["repeat"]["kind"] == "monthly"


@pytest.mark.parametrize("text, repeat", [
    ("gym daily", {"kind": "daily"}),
    ("gym every day", {"kind": "daily"}),
    ("gym every weekday", {"kind": "weekdays"}),
    ("gym weekdays", {"kind": "weekdays"}),
    ("gym weekly", {"kind": "weekly", "days": [4]}),
    ("gym every week", {"kind": "weekly", "days": [4]}),
    ("gym every mon, wed and fri", {"kind": "weekly", "days": [0, 2, 4]}),
    ("gym every tue & thurs", {"kind": "weekly", "days": [1, 3]}),
    ("gym every other day", {"kind": "interval", "days": 2}),
    ("gym every 3 days", {"kind": "interval", "days": 3}),
    ("gym every 1 day", {"kind": "daily"}),
])
def test_repeat_phrases(text, repeat):
    result = parsed(text)
    assert result["repeat"] == repeat
    assert result["title"] == "gym"


def test_every_weekday_typed_on_a_saturday_starts_on_monday():
    assert parsed("standup every weekday", now=london(2026, 11, 14))["dueDate"] == "2026-11-16"


def test_weekly_days_start_on_the_first_matching_day_including_today():
    assert parsed("gym every mon and fri", now=FRIDAY)["dueDate"] == "2026-11-13"
    assert parsed("gym every mon and thu", now=FRIDAY)["dueDate"] == "2026-11-16"


def test_a_repeat_with_a_time_already_passed_starts_on_the_next_day():
    late = london(2026, 11, 13, 10, 0)
    assert parsed("water plants every day 9am", now=late)["dueDate"] == "2026-11-14"
    assert parsed("water plants every day 11am", now=late)["dueDate"] == "2026-11-13"
    weekly = parsed("review weekly 9am", now=late)
    assert weekly["repeat"] == {"kind": "weekly", "days": [4]}
    assert weekly["dueDate"] == "2026-11-20"


def test_an_explicit_start_date_anchors_the_repeat():
    result = parsed("report every month 15 dec")
    assert result["repeat"] == {"kind": "monthly", "day": 15}
    assert result["dueDate"] == "2026-12-15"


@pytest.mark.parametrize("text", ["every 400 days", "every 0 days", "every 32nd"])
def test_out_of_range_repeats_stay_in_the_title(text):
    result = parsed(f"odd {text}")
    assert result["repeat"] is None
    assert result["title"] == f"odd {text}"


@pytest.mark.parametrize("text, expected", [
    ("x 5pm", "17:00"), ("x 5 pm", "17:00"), ("x 5:30pm", "17:30"), ("x 12am", "00:00"), ("x 12pm", "12:00"),
    ("x at 9", "09:00"), ("x at 21", "21:00"), ("x 9:30", "09:30"), ("x 21:05", "21:05"), ("x noon", "12:00"),
    ("x at 7:45am", "07:45"),
])
def test_times(text, expected):
    assert parsed(f"{text} tomorrow")["dueTime"] == expected


@pytest.mark.parametrize("text", ["x 13pm", "x 25:00", "x 9", "x 0am", "x 7:60"])
def test_things_that_are_not_times_stay_in_the_title(text):
    result = parsed(text)
    assert result["dueTime"] is None
    assert result["title"] == text


def test_a_time_alone_is_today_or_tomorrow_when_it_has_passed():
    assert parsed("call 5pm", now=london(2026, 11, 13, 16, 59))["dueDate"] == "2026-11-13"
    assert parsed("call 5pm", now=london(2026, 11, 13, 17, 0))["dueDate"] == "2026-11-13"
    assert parsed("call 5pm", now=london(2026, 11, 13, 17, 1))["dueDate"] == "2026-11-14"


def test_priority_and_list_markers():
    result = parsed("fix bug #123 !2 #Work-Stuff today")
    assert (result["title"], result["priority"], result["list"]) == ("fix bug #123", 2, "Work-Stuff")
    assert parsed("x !5")["title"] == "x !5"
    assert parsed("x !1 !2")["title"] == "x !2"
    assert parsed("x #a #b")["list"] == "a"


def test_only_the_first_date_is_used_and_the_rest_stays():
    result = parsed("move meeting from monday to friday")
    assert result["dueDate"] == "2026-11-16"
    assert result["title"] == "move meeting from to friday"


def test_quoted_words_are_kept_as_written():
    result = parsed('watch "Friday Night Lights" tomorrow')
    assert result["title"] == "watch Friday Night Lights"
    assert result["dueDate"] == "2026-11-14"


def test_ordinary_numbers_and_words_are_left_alone():
    assert parsed("buy 5 eggs")["title"] == "buy 5 eggs"
    assert parsed("check in with Sam")["title"] == "check in with Sam"
    assert parsed("turn on the heating")["title"] == "turn on the heating"


@pytest.mark.parametrize("text", ["tomorrow 5pm", "", "   ", "!1 #work", '""'])
def test_text_with_no_title_is_refused(text):
    with pytest.raises(TaskError):
        parse_quick_add(text, FRIDAY)


@pytest.mark.parametrize("value", [None, 5, ["x"], "x" * 1001])
def test_bad_text_is_refused(value):
    with pytest.raises(TaskError):
        parse_quick_add(value, FRIDAY)


def test_schedule_only_text_is_read_whole_or_not_at_all():
    assert parse_schedule("every day", FRIDAY) == (date(2026, 11, 13), None, {"kind": "daily"})
    assert parse_schedule("14 Nov 2026 17:00", FRIDAY) == (date(2026, 11, 14), "17:00", None)
    assert parse_schedule("Nov 20", FRIDAY) == (date(2026, 11, 20), None, None)
    assert parse_schedule("every day starting soon", FRIDAY) is None
    assert parse_schedule("!1", FRIDAY) is None
    assert parse_schedule("", FRIDAY) is None


def test_the_nth_weekday_of_a_month_is_not_mistaken_for_a_month_day():
    result = parsed("book club every 2nd tuesday")
    assert result["repeat"] is None
    assert parse_schedule("every 2nd tuesday", FRIDAY) is None


@pytest.mark.parametrize("text, title, due_date, due_time", [
    ("call mum 5:30 pm", "call mum", "2026-11-13", "17:30"),
    ("call mum at 5:30 pm", "call mum", "2026-11-13", "17:30"),
    ("call mum 9:30 am", "call mum", "2026-11-14", "09:30"),
    ("meet 12:15 pm", "meet", "2026-11-13", "12:15"),
    ("meet 12:15 am tomorrow", "meet", "2026-11-14", "00:15"),
    ("call mum 5:30pm", "call mum", "2026-11-13", "17:30"),
    ("call mum 5 pm", "call mum", "2026-11-13", "17:00"),
    ("train 17:30 pm", "train pm", "2026-11-13", "17:30"),
])
def test_a_clock_time_followed_by_am_or_pm_is_read_as_one_time(text, title, due_date, due_time):
    result = parsed(text)
    assert (result["title"], result["dueDate"], result["dueTime"]) == (title, due_date, due_time)


@pytest.mark.parametrize("text", [
    "weekly review", "write daily report", "monthly budget", "weekdays plan", "buy sun cream", "fix sat nav",
    "call 24/7 helpline", "add 1/2 cup sugar",
])
def test_words_that_only_look_like_dates_or_repeats_stay_in_the_title(text):
    result = parsed(text)
    assert (result["title"], result["dueDate"], result["repeat"]) == (text, None, None)


@pytest.mark.parametrize("text, title, due_date, repeat", [
    ("standup daily", "standup", "2026-11-13", {"kind": "daily"}),
    ("standup daily 9am #work", "standup", "2026-11-14", {"kind": "daily"}),
    ("budget monthly", "budget", "2026-11-13", {"kind": "monthly", "day": 13}),
    ("party sat", "party", "2026-11-14", None),
    ("party sat 8pm", "party", "2026-11-14", None),
    ("brunch on sun with jo", "brunch with jo", "2026-11-15", None),
    ("x 25/12", "x", "2026-12-25", None),
    ("x 25/12 !1", "x", "2026-12-25", None),
    ("x 1/2/2027 cup", "x cup", "2027-02-01", None),
])
def test_those_words_still_count_at_the_end_or_next_to_other_schedule_words(text, title, due_date, repeat):
    result = parsed(text)
    assert (result["title"], result["dueDate"], result["repeat"]) == (title, due_date, repeat)
