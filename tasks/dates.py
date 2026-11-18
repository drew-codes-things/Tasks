import math
import re
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from .errors import TaskError

EARLIEST_DUE_DATE = date(1900, 1, 1)
LATEST_DUE_DATE = date(2999, 12, 31)
EARLIEST_TIMESTAMP = datetime(1, 1, 2, tzinfo=timezone.utc).timestamp()
LATEST_TIMESTAMP = datetime(9999, 12, 30, tzinfo=timezone.utc).timestamp()
MAX_TIMESTAMP_LENGTH = 40
DUE_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$", re.ASCII)
DUE_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$", re.ASCII)
DUE_DATE_HINT = "a date like 2027-01-31"


def is_showable_timestamp(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and EARLIEST_TIMESTAMP <= value <= LATEST_TIMESTAMP


def to_iso(timestamp: float | None) -> str | None:
    if not is_showable_timestamp(timestamp):
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value, field: str) -> float:
    if not isinstance(value, str) or len(value) > MAX_TIMESTAMP_LENGTH:
        raise TaskError(f"{field} must be a date and time like 2027-01-31T09:00:00Z")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as error:
        raise TaskError(f"{field} must be a date and time like 2027-01-31T09:00:00Z") from error
    if moment.tzinfo is None:
        raise TaskError(f"{field} needs a time zone, like 2027-01-31T09:00:00Z")
    timestamp = moment.timestamp()
    if not is_showable_timestamp(timestamp):
        raise TaskError(f"{field} must fall between the years 1 and 9999 in UTC")
    return timestamp


def is_due_date_in_range(day: date) -> bool:
    return EARLIEST_DUE_DATE <= day <= LATEST_DUE_DATE


def parse_due_date(value, field: str = "dueDate") -> date:
    if not isinstance(value, str) or not DUE_DATE_PATTERN.match(value):
        raise TaskError(f"{field} must be {DUE_DATE_HINT}")
    try:
        day = date.fromisoformat(value)
    except ValueError as error:
        raise TaskError(f"{field} must be {DUE_DATE_HINT}, and a day that exists") from error
    if not is_due_date_in_range(day):
        raise TaskError(f"{field} must fall between {EARLIEST_DUE_DATE.isoformat()} and {LATEST_DUE_DATE.isoformat()}")
    return day


def parse_due_time(value, field: str = "dueTime") -> str:
    if not isinstance(value, str) or not DUE_TIME_PATTERN.match(value):
        raise TaskError(f"{field} must be a 24-hour time like 09:30")
    return value


def stored_due_date(value: str | None) -> date | None:
    if not isinstance(value, str) or not DUE_DATE_PATTERN.match(value):
        return None
    try:
        day = date.fromisoformat(value)
    except ValueError:
        return None
    return day if is_due_date_in_range(day) else None


def stored_due_time(value: str | None) -> str | None:
    if isinstance(value, str) and DUE_TIME_PATTERN.match(value):
        return value
    return None


def local_now(clock_value: float, zone: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(clock_value, zone)


def due_at(day: date | None, clock_time: str | None, zone: ZoneInfo) -> str | None:
    if day is None or clock_time is None:
        return None
    hours, minutes = (int(part) for part in clock_time.split(":"))
    moment = datetime.combine(day, time(hours, minutes), tzinfo=zone)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
