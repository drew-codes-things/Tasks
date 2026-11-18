import calendar
import json
from datetime import date, timedelta

from .dates import LATEST_DUE_DATE
from .errors import TaskError

KINDS = ("daily", "weekdays", "weekly", "monthly", "interval")
DAYS_IN_WEEK = 7
LAST_WEEKDAY_INDEX = 4
MAX_INTERVAL_DAYS = 365
MAX_MONTH_DAY = 31
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
RULE_KEYS = {"daily": set(), "weekdays": set(), "weekly": {"days"}, "monthly": {"day"}, "interval": {"days"}}


def _is_whole(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _clean_week_days(value) -> list[int]:
    if not isinstance(value, list) or not value:
        raise TaskError("a weekly repeat needs days, a list of 0 (Monday) to 6 (Sunday)")
    if not all(_is_whole(day) and 0 <= day < DAYS_IN_WEEK for day in value):
        raise TaskError("weekly repeat days run from 0 (Monday) to 6 (Sunday)")
    return sorted(set(value))


def clean_rule(value) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("kind") not in KINDS:
        raise TaskError(f"repeat must be null or an object whose kind is one of {', '.join(KINDS)}")
    kind = value["kind"]
    unknown = sorted(set(value) - {"kind"} - RULE_KEYS[kind])
    if unknown:
        raise TaskError(f"a {kind} repeat does not take {', '.join(unknown)}")
    if kind == "weekly":
        return {"kind": kind, "days": _clean_week_days(value.get("days"))}
    if kind == "monthly":
        day = value.get("day")
        if not _is_whole(day) or not 1 <= day <= MAX_MONTH_DAY:
            raise TaskError(f"a monthly repeat needs day, 1 to {MAX_MONTH_DAY}")
        return {"kind": kind, "day": day}
    if kind == "interval":
        days = value.get("days")
        if not _is_whole(days) or not 1 <= days <= MAX_INTERVAL_DAYS:
            raise TaskError(f"an every-N-days repeat needs days, 1 to {MAX_INTERVAL_DAYS}")
        return {"kind": kind, "days": days}
    return {"kind": kind}


def rule_to_text(rule: dict | None) -> str | None:
    return None if rule is None else json.dumps(rule, separators=(",", ":"), sort_keys=True)


def rule_from_text(text: str | None) -> dict | None:
    if not text:
        return None
    try:
        return clean_rule(json.loads(text))
    except (ValueError, TaskError):
        return None


def _ordinal(number: int) -> str:
    if 10 <= number % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def describe(rule: dict | None) -> str | None:
    if rule is None:
        return None
    kind = rule["kind"]
    if kind == "daily":
        return "every day"
    if kind == "weekdays":
        return "every weekday"
    if kind == "weekly":
        return "every " + ", ".join(DAY_NAMES[day] for day in rule["days"])
    if kind == "monthly":
        return f"every month on the {_ordinal(rule['day'])}"
    if rule["days"] == 1:
        return "every day"
    return f"every {rule['days']} days"


def day_in_month(year: int, month: int, wanted_day: int) -> date:
    return date(year, month, min(wanted_day, calendar.monthrange(year, month)[1]))


def _following_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _first_monthly(wanted_day: int, start: date) -> date:
    candidate = day_in_month(start.year, start.month, wanted_day)
    if candidate >= start:
        return candidate
    return day_in_month(*_following_month(start.year, start.month), wanted_day)


def _first_matching_weekday(days, start: date) -> date:
    for offset in range(DAYS_IN_WEEK):
        candidate = start + timedelta(days=offset)
        if candidate.weekday() in days:
            return candidate
    raise TaskError("a weekly repeat needs at least one day")


def first_on_or_after(rule: dict, start: date) -> date:
    kind = rule["kind"]
    if kind == "weekdays":
        return _first_matching_weekday(range(LAST_WEEKDAY_INDEX + 1), start)
    if kind == "weekly":
        return _first_matching_weekday(rule["days"], start)
    if kind == "monthly":
        return _first_monthly(rule["day"], start)
    return start


def _days_between(earlier: date, later: date) -> int:
    return (later - earlier).days


def next_due_date(rule: dict, due: date, today: date) -> date | None:
    if due >= LATEST_DUE_DATE:
        return None
    if rule["kind"] == "interval":
        step = rule["days"]
        steps = max(1, -(-_days_between(due, today) // step))
        candidate = due + timedelta(days=step * steps)
    else:
        start = max(due + timedelta(days=1), today)
        if start > LATEST_DUE_DATE:
            return None
        candidate = first_on_or_after(rule, start)
    return candidate if candidate <= LATEST_DUE_DATE else None
