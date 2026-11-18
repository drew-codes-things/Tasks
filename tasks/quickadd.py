import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .dates import is_due_date_in_range
from .errors import TaskError, storable_text
from .repeat import MAX_INTERVAL_DAYS, MAX_MONTH_DAY, describe, first_on_or_after

MAX_TEXT_LENGTH = 1000
DAYS_IN_WEEK = 7
LEAP_YEAR_SEARCH = 8
TRAILING_PUNCTUATION = ",;"
WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "weds": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}
MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
    "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9, "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
WEEKDAYS_THAT_ARE_ALSO_WORDS = ("sun", "sat", "wed")
TODAY_WORDS = ("today",)
TOMORROW_WORDS = ("tomorrow", "tmrw")
NOON_WORDS = ("noon", "midday")
LIST_SEPARATORS = ("and", "&")
DAY_UNITS = ("day", "days")
WEEK_UNITS = ("week", "weeks")
NUMBER = re.compile(r"^\d{1,3}$", re.ASCII)
ORDINAL = re.compile(r"^(\d{1,2})(st|nd|rd|th)?$", re.ASCII)
ORDINAL_WITH_SUFFIX = re.compile(r"^(\d{1,2})(st|nd|rd|th)$", re.ASCII)
YEAR = re.compile(r"^\d{4}$", re.ASCII)
SLASH_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})(?:/(\d{4}))?$", re.ASCII)
ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$", re.ASCII)
TWELVE_HOUR = re.compile(r"^(\d{1,2})(?::([0-5]\d))?(am|pm)$", re.ASCII)
TWENTY_FOUR_HOUR = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$", re.ASCII)
MERIDIEM = re.compile(r"^(am|pm)$", re.ASCII)
PRIORITY = re.compile(r"^!([1-4])$", re.ASCII)
LIST_NAME = re.compile(r"^#([^\W\d_][\w-]{0,49})$")
QUOTED_OR_WORD = re.compile(r'"([^"]*)"|(\S+)')


@dataclass
class Token:
    text: str
    word: str | None


@dataclass
class Found:
    size: int
    value: object
    is_guess: bool = False


@dataclass
class QuickAdd:
    title: str
    due_date: date | None = None
    due_time: str | None = None
    priority: int | None = None
    list_name: str | None = None
    repeat: dict | None = None

    def summary(self) -> dict:
        return {
            "title": self.title,
            "dueDate": self.due_date.isoformat() if self.due_date else None,
            "dueTime": self.due_time,
            "priority": self.priority,
            "list": self.list_name,
            "repeat": self.repeat,
            "repeatText": describe(self.repeat),
        }


def _tokens(text: str) -> list[Token]:
    tokens = []
    for match in QUOTED_OR_WORD.finditer(text):
        if match.group(1) is not None:
            if match.group(1).strip():
                tokens.append(Token(match.group(1).strip(), None))
            continue
        raw = match.group(2)
        tokens.append(Token(raw, raw.lower().rstrip(TRAILING_PUNCTUATION)))
    return tokens


def _word(tokens: list[Token], index: int) -> str | None:
    return tokens[index].word if 0 <= index < len(tokens) else None


def _real_date(year: int, month: int, day: int) -> date | None:
    try:
        candidate = date(year, month, day)
    except ValueError:
        return None
    return candidate if is_due_date_in_range(candidate) else None


def _date_without_year(month: int, day: int, today: date) -> date | None:
    for year in range(today.year, today.year + LEAP_YEAR_SEARCH + 1):
        candidate = _real_date(year, month, day)
        if candidate and candidate >= today:
            return candidate
    return None


def _day_and_month(day: int, month: int, year_word: str | None, today: date) -> tuple[date | None, bool]:
    if year_word and YEAR.match(year_word):
        return _real_date(int(year_word), month, day), True
    return _date_without_year(month, day, today), False


def next_weekday_after(today: date, weekday: int) -> date:
    ahead = (weekday - today.weekday()) % DAYS_IN_WEEK or DAYS_IN_WEEK
    return today + timedelta(days=ahead)


def weekday_next_week(today: date, weekday: int) -> date:
    monday_next_week = today + timedelta(days=DAYS_IN_WEEK - today.weekday())
    return monday_next_week + timedelta(days=weekday)


def _relative_day(tokens: list[Token], index: int, today: date) -> Found | None:
    word = _word(tokens, index)
    if word in TODAY_WORDS:
        return Found(1, today)
    if word in TOMORROW_WORDS:
        return Found(1, today + timedelta(days=1))
    if word in WEEKDAYS:
        return Found(1, next_weekday_after(today, WEEKDAYS[word]), is_guess=word in WEEKDAYS_THAT_ARE_ALSO_WORDS)
    if word == "next":
        following = _word(tokens, index + 1)
        if following in WEEKDAYS:
            return Found(2, weekday_next_week(today, WEEKDAYS[following]))
        if following in WEEK_UNITS[:1]:
            return Found(2, weekday_next_week(today, 0))
    if word == "in":
        amount, unit = _word(tokens, index + 1), _word(tokens, index + 2)
        if amount and NUMBER.match(amount) and int(amount) > 0:
            if unit in DAY_UNITS:
                return Found(3, today + timedelta(days=int(amount)))
            if unit in WEEK_UNITS:
                return Found(3, today + timedelta(weeks=int(amount)))
    return None


def _calendar_day(tokens: list[Token], index: int, today: date) -> Found | None:
    word = _word(tokens, index)
    if word is None:
        return None
    iso = ISO_DATE.match(word)
    if iso:
        found = _real_date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        return Found(1, found) if found else None
    slash = SLASH_DATE.match(word)
    if slash:
        day, month = int(slash.group(1)), int(slash.group(2))
        if not 1 <= month <= 12:
            return None
        found, _ = _day_and_month(day, month, slash.group(3), today)
        return Found(1, found, is_guess=slash.group(3) is None) if found else None
    ordinal = ORDINAL.match(word)
    if ordinal:
        offset = 2 if _word(tokens, index + 1) == "of" else 1
        month_word = _word(tokens, index + offset)
        if month_word in MONTHS:
            found, used_year = _day_and_month(int(ordinal.group(1)), MONTHS[month_word], _word(tokens, index + offset + 1), today)
            return Found(offset + 1 + used_year, found) if found else None
    if word in MONTHS:
        day_word = _word(tokens, index + 1)
        day_match = ORDINAL.match(day_word or "")
        if day_match:
            found, used_year = _day_and_month(int(day_match.group(1)), MONTHS[word], _word(tokens, index + 2), today)
            return Found(2 + used_year, found) if found else None
    return None


def _date_phrase(tokens: list[Token], index: int, today: date) -> Found | None:
    lead = 1 if _word(tokens, index) == "on" else 0
    found = _relative_day(tokens, index + lead, today) or _calendar_day(tokens, index + lead, today)
    if found is None:
        return None
    return Found(found.size + lead, found.value, is_guess=found.is_guess and not lead)


def _clock(hours: int, minutes: int) -> str:
    return f"{hours:02d}:{minutes:02d}"


def _twelve_hour(hour: int, minutes: int, meridiem: str) -> str | None:
    if not 1 <= hour <= 12:
        return None
    hours = hour % 12 + (12 if meridiem == "pm" else 0)
    return _clock(hours, minutes)


def _bare_time(tokens: list[Token], index: int, after_at: bool) -> Found | None:
    word = _word(tokens, index)
    if word is None:
        return None
    if word in NOON_WORDS:
        return Found(1, "12:00")
    twelve = TWELVE_HOUR.match(word)
    if twelve:
        found = _twelve_hour(int(twelve.group(1)), int(twelve.group(2) or 0), twelve.group(3))
        return Found(1, found) if found else None
    twenty_four = TWENTY_FOUR_HOUR.match(word)
    if twenty_four:
        hour, minutes = int(twenty_four.group(1)), int(twenty_four.group(2))
        meridiem = MERIDIEM.match(_word(tokens, index + 1) or "")
        twelve_hour_clock = _twelve_hour(hour, minutes, meridiem.group(1)) if meridiem else None
        if twelve_hour_clock:
            return Found(2, twelve_hour_clock)
        return Found(1, _clock(hour, minutes))
    if not (word.isascii() and word.isdigit() and len(word) <= 2):
        return None
    meridiem = MERIDIEM.match(_word(tokens, index + 1) or "")
    if meridiem:
        found = _twelve_hour(int(word), 0, meridiem.group(1))
        return Found(2, found) if found else None
    if after_at and int(word) <= 23:
        return Found(1, _clock(int(word), 0))
    return None


def _time_phrase(tokens: list[Token], index: int) -> Found | None:
    if _word(tokens, index) == "at":
        found = _bare_time(tokens, index + 1, after_at=True)
        return Found(found.size + 1, found.value) if found else None
    return _bare_time(tokens, index, after_at=False)


def _weekday_list(tokens: list[Token], index: int) -> Found | None:
    days, size = [], 0
    while True:
        word = _word(tokens, index + size)
        if word not in WEEKDAYS:
            break
        days.append(WEEKDAYS[word])
        size += 1
        if _word(tokens, index + size) in LIST_SEPARATORS and _word(tokens, index + size + 1) in WEEKDAYS:
            size += 1
    return Found(size, sorted(set(days))) if days else None


def _month_day_after(tokens: list[Token], index: int) -> Found | None:
    size = 0
    if _word(tokens, index) == "on":
        size += 1
    if _word(tokens, index + size) == "the":
        size += 1
    match = ORDINAL.match(_word(tokens, index + size) or "")
    if size and match and 1 <= int(match.group(1)) <= MAX_MONTH_DAY:
        return Found(size + 1, int(match.group(1)))
    return None


def _monthly(tokens: list[Token], index: int, lead: int, is_guess: bool = False) -> Found:
    day = _month_day_after(tokens, index + lead)
    if day:
        return Found(lead + day.size, {"kind": "monthly", "day": day.value})
    return Found(lead, {"kind": "monthly"}, is_guess=is_guess)


def _every_phrase(tokens: list[Token], index: int) -> Found | None:
    word = _word(tokens, index + 1)
    if word in DAY_UNITS[:1]:
        return Found(2, {"kind": "daily"})
    if word in ("weekday", "weekdays"):
        return Found(2, {"kind": "weekdays"})
    if word in WEEK_UNITS[:1]:
        return Found(2, {"kind": "weekly"})
    if word == "month":
        return _monthly(tokens, index, 2)
    if word == "other" and _word(tokens, index + 2) in DAY_UNITS[:1]:
        return Found(3, {"kind": "interval", "days": 2})
    days = _weekday_list(tokens, index + 1)
    if days:
        return Found(1 + days.size, {"kind": "weekly", "days": days.value})
    suffixed = ORDINAL_WITH_SUFFIX.match(word or "")
    names_a_weekday_of_the_month = _word(tokens, index + 2) in WEEKDAYS
    if suffixed and 1 <= int(suffixed.group(1)) <= MAX_MONTH_DAY and not names_a_weekday_of_the_month:
        return Found(2, {"kind": "monthly", "day": int(suffixed.group(1))})
    if word and NUMBER.match(word) and _word(tokens, index + 2) in DAY_UNITS:
        amount = int(word)
        if 1 <= amount <= MAX_INTERVAL_DAYS:
            return Found(3, {"kind": "daily"} if amount == 1 else {"kind": "interval", "days": amount})
    return None


def _repeat_phrase(tokens: list[Token], index: int) -> Found | None:
    word = _word(tokens, index)
    if word == "every":
        return _every_phrase(tokens, index)
    if word == "daily":
        return Found(1, {"kind": "daily"}, is_guess=True)
    if word == "weekdays":
        return Found(1, {"kind": "weekdays"}, is_guess=True)
    if word == "weekly":
        return Found(1, {"kind": "weekly"}, is_guess=True)
    if word == "monthly":
        return _monthly(tokens, index, 1, is_guess=True)
    return None


def _priority(tokens: list[Token], index: int) -> Found | None:
    match = PRIORITY.match(_word(tokens, index) or "")
    return Found(1, int(match.group(1))) if match else None


def _list_name(tokens: list[Token], index: int) -> Found | None:
    if tokens[index].word is None:
        return None
    match = LIST_NAME.match(tokens[index].text.rstrip(TRAILING_PUNCTUATION))
    return Found(1, match.group(1)) if match else None


def _complete_rule(rule: dict, start: date) -> dict:
    if rule["kind"] == "weekly" and "days" not in rule:
        return {"kind": "weekly", "days": [start.weekday()]}
    if rule["kind"] == "monthly" and "day" not in rule:
        return {"kind": "monthly", "day": start.day}
    return rule


def _resolve_due(found: dict, now: datetime) -> tuple[date | None, dict | None]:
    today = now.date()
    explicit, clock_time, rule = found.get("date"), found.get("time"), found.get("repeat")
    has_passed_today = clock_time is not None and explicit is None and clock_time < _clock(now.hour, now.minute)
    if rule is not None:
        rule = _complete_rule(rule, explicit or today)
        start = explicit or (today + timedelta(days=1) if has_passed_today else today)
        return first_on_or_after(rule, start), rule
    if explicit is not None:
        return explicit, None
    if clock_time is not None:
        return (today + timedelta(days=1) if has_passed_today else today), None
    return None, None


MATCHERS = (
    ("repeat", lambda tokens, index, now: _repeat_phrase(tokens, index)),
    ("date", lambda tokens, index, now: _date_phrase(tokens, index, now.date())),
    ("time", lambda tokens, index, now: _time_phrase(tokens, index)),
    ("priority", lambda tokens, index, now: _priority(tokens, index)),
    ("list", lambda tokens, index, now: _list_name(tokens, index)),
)


def _match_at(tokens: list[Token], index: int, now: datetime, found: dict) -> tuple[str, Found] | None:
    for kind, matcher in MATCHERS:
        if kind in found:
            continue
        match = matcher(tokens, index, now)
        if match is not None:
            return kind, match
    return None


def _is_end_or_understood(tokens: list[Token], index: int, now: datetime, found: dict) -> bool:
    if index >= len(tokens):
        return True
    following = _match_at(tokens, index, now, found)
    return following is not None and not following[1].is_guess


def _match_in_title(tokens: list[Token], index: int, now: datetime, found: dict) -> tuple[str, Found] | None:
    match = _match_at(tokens, index, now, found)
    if match is None or not match[1].is_guess:
        return match
    kind, result = match
    return match if _is_end_or_understood(tokens, index + result.size, now, {**found, kind: result.value}) else None


def parse_quick_add(text, now: datetime) -> QuickAdd:
    if not isinstance(text, str):
        raise TaskError("text must be text")
    if len(text) > MAX_TEXT_LENGTH:
        raise TaskError(f"the quick add text is too long (at most {MAX_TEXT_LENGTH} characters)")
    tokens = _tokens(storable_text(text, "the quick add text"))
    found: dict = {}
    title_words: list[str] = []
    index = 0
    while index < len(tokens):
        match = _match_in_title(tokens, index, now, found)
        if match is None:
            title_words.append(tokens[index].text)
            index += 1
            continue
        kind, result = match
        found[kind] = result.value
        index += result.size
    title = " ".join(title_words).strip()
    if not title:
        raise TaskError("say what the task is as well as when, for example: call mum tomorrow 5pm")
    due_date, rule = _resolve_due(found, now)
    return QuickAdd(title=title, due_date=due_date, due_time=found.get("time") if due_date else None,
                    priority=found.get("priority"), list_name=found.get("list"), repeat=rule)


SCHEDULE_KINDS = ("repeat", "date", "time")


def parse_schedule(text: str, now: datetime) -> tuple[date | None, str | None, dict | None] | None:
    tokens = _tokens(text[:MAX_TEXT_LENGTH])
    found: dict = {}
    index = 0
    while index < len(tokens):
        match = _match_at(tokens, index, now, found)
        if match is None or match[0] not in SCHEDULE_KINDS:
            return None
        kind, result = match
        found[kind] = result.value
        index += result.size
    if not found:
        return None
    due_date, rule = _resolve_due(found, now)
    return due_date, found.get("time") if due_date else None, rule
