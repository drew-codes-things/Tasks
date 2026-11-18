import re
import sqlite3
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .config import DEFAULT_TIME_ZONE
from .dates import (due_at, is_due_date_in_range, local_now, parse_due_date, parse_due_time, stored_due_date,
                    stored_due_time, to_iso)
from .db import INBOX_ID, Database
from .errors import TaskError, storable_text
from .quickadd import parse_quick_add
from .repeat import clean_rule, describe, next_due_date, rule_from_text, rule_to_text

BIN_DAYS = 30
SECONDS_PER_DAY = 24 * 3600
UPCOMING_DAYS = 7
MAX_TITLE_LENGTH = 500
MAX_NOTES_LENGTH = 10_000
MAX_LIST_NAME_LENGTH = 50
MAX_QUERY_LENGTH = 200
MAX_SEARCH_TERMS = 8
MAX_LISTED_TASKS = 500
MAX_SUBTASKS_PER_TASK = 500
DEFAULT_PRIORITY = 4
PRIORITIES = (1, 2, 3, 4)
LARGEST_DATABASE_INTEGER = 2 ** 63 - 1
SEARCH_TERM = re.compile(r"\w+")
VIEWS = ("today", "upcoming", "list", "completed", "bin", "search")
TASK_DEFAULTS = {"title": "", "notes": "", "dueDate": None, "dueTime": None, "priority": DEFAULT_PRIORITY,
                 "listId": INBOX_ID, "parentId": None, "repeat": None}
COLUMN_FOR_FIELD = {"title": "title", "notes": "notes", "dueDate": "due_date", "dueTime": "due_time", "priority": "priority",
                    "listId": "list_id", "parentId": "parent_id", "repeat": "repeat_rule"}
TASK_SELECT = ("SELECT t.*, l.name AS list_name, p.title AS parent_title FROM tasks t JOIN lists l ON l.id = t.list_id"
               " LEFT JOIN tasks p ON p.id = t.parent_id")
IS_LIVE = "t.trashed_at IS NULL AND (p.id IS NULL OR p.trashed_at IS NULL)"
IS_OPEN = "t.completed_at IS NULL"
DUE_ORDER = "t.due_date, t.due_time IS NULL, t.due_time, t.priority, t.id"
LIST_ORDER = "t.due_date IS NULL, t.due_date, t.due_time IS NULL, t.due_time, t.priority, t.id"
VIEW_FILTERS = {
    "today": f"{IS_LIVE} AND {IS_OPEN} AND t.due_date IS NOT NULL AND t.due_date <= :today",
    "upcoming": f"{IS_LIVE} AND {IS_OPEN} AND t.due_date > :today AND t.due_date <= :horizon",
    "list": f"{IS_LIVE} AND {IS_OPEN} AND t.parent_id IS NULL AND t.list_id = :list_id",
    "completed": f"{IS_LIVE} AND t.completed_at IS NOT NULL",
    "bin": "t.trashed_at IS NOT NULL AND (p.id IS NULL OR p.trashed_at IS NULL)",
    "search": f"{IS_LIVE} AND t.id IN (SELECT rowid FROM tasks_fts WHERE tasks_fts MATCH :match)",
}
VIEW_ORDER = {
    "today": DUE_ORDER,
    "upcoming": DUE_ORDER,
    "list": LIST_ORDER,
    "completed": "t.completed_at DESC, COALESCE(t.parent_id, t.id) DESC, t.parent_id IS NOT NULL, t.id",
    "bin": "t.trashed_at DESC, t.id DESC",
    "search": f"t.completed_at IS NOT NULL, {LIST_ORDER}",
}


def is_database_id(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= LARGEST_DATABASE_INTEGER


def _clean_text(value, name: str, limit: int) -> str:
    if not isinstance(value, str):
        raise TaskError(f"{name} must be text")
    if len(value) > limit:
        raise TaskError(f"{name} is too long (at most {limit} characters)")
    return storable_text(value, name)


def _clean_title(value) -> str:
    title = " ".join(_clean_text(value, "the title", MAX_TITLE_LENGTH).split())
    if not title:
        raise TaskError("a task needs a title")
    return title


def _clean_priority(value) -> int:
    if isinstance(value, bool) or value not in PRIORITIES:
        raise TaskError("priority must be 1 (highest), 2, 3 or 4 (lowest)")
    return value


def _clean_id(value, name: str) -> int:
    if not is_database_id(value):
        raise TaskError(f"{name} must be the id of an existing {name[:-2]}")
    return value


def _optional(cleaner):
    return lambda value: None if value is None else cleaner(value)


FIELD_CLEANERS = {
    "title": _clean_title,
    "notes": lambda value: _clean_text(value, "the notes", MAX_NOTES_LENGTH),
    "dueDate": _optional(lambda value: parse_due_date(value).isoformat()),
    "dueTime": _optional(parse_due_time),
    "priority": _clean_priority,
    "listId": lambda value: _clean_id(value, "listId"),
    "parentId": _optional(lambda value: _clean_id(value, "parentId")),
    "repeat": clean_rule,
}


def clean_fields(data) -> dict:
    if not isinstance(data, dict):
        raise TaskError("send a JSON object")
    unknown = sorted(set(data) - set(FIELD_CLEANERS))
    if unknown:
        raise TaskError(f"unknown field: {', '.join(unknown)}")
    return {name: FIELD_CLEANERS[name](value) for name, value in data.items()}


def clean_list_name(value) -> str:
    name = " ".join(_clean_text(value, "the list name", MAX_LIST_NAME_LENGTH).split())
    if not name or not name.isprintable():
        raise TaskError("a list needs a name")
    return name


def fts_query(text: str) -> str:
    terms = SEARCH_TERM.findall(text or "")[:MAX_SEARCH_TERMS]
    return " ".join(f'"{term}"*' for term in terms)


def _shifted(day: str | None, days: int) -> str | None:
    stored = stored_due_date(day)
    if stored is None:
        return None
    try:
        moved = stored + timedelta(days=days)
    except OverflowError:
        return None
    return moved.isoformat() if is_due_date_in_range(moved) else None


class TaskStore:
    def __init__(self, database: Database, clock=time.time, zone: ZoneInfo | None = None, bin_days: int = BIN_DAYS):
        self.database = database
        self.clock = clock
        self.zone = zone or ZoneInfo(DEFAULT_TIME_ZONE)
        self.bin_seconds = bin_days * SECONDS_PER_DAY

    def now(self) -> datetime:
        return local_now(self.clock(), self.zone)

    def today(self) -> date:
        return self.now().date()

    def lists(self) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT l.id, l.name, (SELECT COUNT(*) FROM tasks t WHERE t.list_id = l.id AND t.parent_id IS NULL"
                " AND t.completed_at IS NULL AND t.trashed_at IS NULL) AS open_count FROM lists l"
                " ORDER BY l.id != ?, l.name COLLATE NOCASE", (INBOX_ID,)).fetchall()
        return [{"id": row["id"], "name": row["name"], "isInbox": row["id"] == INBOX_ID, "open": row["open_count"]} for row in rows]

    def counts(self) -> dict:
        today = self.today()
        horizon = today + timedelta(days=UPCOMING_DAYS)
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT SUM(t.due_date < :today) AS overdue, SUM(t.due_date <= :today) AS due_today,"
                f" SUM(t.due_date > :today AND t.due_date <= :horizon) AS upcoming"
                f" FROM tasks t LEFT JOIN tasks p ON p.id = t.parent_id WHERE {IS_LIVE} AND {IS_OPEN} AND t.due_date IS NOT NULL",
                {"today": today.isoformat(), "horizon": horizon.isoformat()}).fetchone()
            binned = connection.execute(f"SELECT COUNT(*) FROM tasks t LEFT JOIN tasks p ON p.id = t.parent_id WHERE {VIEW_FILTERS['bin']}").fetchone()[0]
        return {"today": row["due_today"] or 0, "overdue": row["overdue"] or 0, "upcoming": row["upcoming"] or 0, "bin": binned}

    def create_list(self, name) -> dict:
        clean = clean_list_name(name)
        with self.database.connect(immediate=True) as connection:
            list_id = self._insert_list(connection, clean)
        return self._list_entry(list_id)

    def rename_list(self, list_id: int, name) -> dict:
        clean = clean_list_name(name)
        with self.database.connect(immediate=True) as connection:
            self._list_row(connection, list_id)
            if list_id == INBOX_ID:
                raise TaskError("the Inbox cannot be renamed", status=409)
            if self._find_list(connection, clean) not in (None, list_id):
                raise TaskError(f"there is already a list called {clean}", status=409)
            connection.execute("UPDATE lists SET name = ? WHERE id = ?", (clean, list_id))
        return self._list_entry(list_id)

    def delete_list(self, list_id: int) -> int:
        with self.database.connect(immediate=True) as connection:
            self._list_row(connection, list_id)
            if list_id == INBOX_ID:
                raise TaskError("the Inbox cannot be deleted", status=409)
            moved = connection.execute("UPDATE tasks SET trashed_at = COALESCE(trashed_at, ?), list_id = ? WHERE list_id = ?",
                                       (self.clock(), INBOX_ID, list_id)).rowcount
            connection.execute("DELETE FROM lists WHERE id = ?", (list_id,))
        return moved

    def list_id_for_name(self, connection: sqlite3.Connection, name: str) -> int:
        clean = clean_list_name(name)
        found = self._find_list(connection, clean)
        return found if found is not None else self._insert_list(connection, clean)

    def create(self, data) -> dict:
        fields = {**TASK_DEFAULTS, **clean_fields(data)}
        with self.database.connect(immediate=True) as connection:
            self._check_new_task(connection, fields)
            task_id = self._insert(connection, fields)
            return self._load(connection, task_id)

    def preview_quick_add(self, text) -> dict:
        return parse_quick_add(text, self.now()).summary()

    def quick_add(self, text, list_id=None, parent_id=None) -> tuple[dict, dict]:
        parsed = parse_quick_add(text, self.now())
        fields = {**TASK_DEFAULTS, "title": parsed.title, "dueDate": parsed.due_date.isoformat() if parsed.due_date else None,
                  "dueTime": parsed.due_time, "repeat": parsed.repeat,
                  "priority": parsed.priority or DEFAULT_PRIORITY}
        if list_id is not None:
            fields["listId"] = _clean_id(list_id, "listId")
        if parent_id is not None:
            fields["parentId"] = _clean_id(parent_id, "parentId")
        with self.database.connect(immediate=True) as connection:
            if parsed.list_name:
                fields["listId"] = self.list_id_for_name(connection, parsed.list_name)
            self._check_new_task(connection, fields)
            task_id = self._insert(connection, fields)
            return self._load(connection, task_id), parsed.summary()

    def get(self, task_id: int) -> dict:
        with self.database.connect() as connection:
            return self._load(connection, task_id)

    def update(self, task_id: int, data) -> dict:
        changes = clean_fields(data)
        if changes.get("dueDate", "unchanged") is None:
            changes.setdefault("dueTime", None)
            changes.setdefault("repeat", None)
        with self.database.connect(immediate=True) as connection:
            row = self._editable_row(connection, task_id)
            fields = {**self._fields_of(row), **changes}
            self._check_changed_task(connection, task_id, row, fields)
            columns = [name for name in COLUMN_FOR_FIELD if name in changes or name == "listId"]
            values = [self._column_value(name, fields[name]) for name in columns]
            assignments = "".join(f"{COLUMN_FOR_FIELD[name]} = ?, " for name in columns)
            connection.execute(f"UPDATE tasks SET {assignments}updated_at = ? WHERE id = ?", (*values, self.clock(), task_id))
            if fields["parentId"] is None:
                connection.execute("UPDATE tasks SET list_id = ? WHERE parent_id = ?", (fields["listId"], task_id))
            return self._load(connection, task_id)

    def complete(self, task_id: int) -> dict:
        with self.database.connect(immediate=True) as connection:
            row = self._editable_row(connection, task_id)
            if row["completed_at"] is not None:
                return {"task": self._load(connection, task_id), "next": self._load_if_present(connection, row["next_id"])}
            current = self.clock()
            connection.execute("UPDATE tasks SET completed_at = ?, updated_at = ? WHERE id = ? OR (parent_id = ? AND completed_at IS NULL)",
                               (current, current, task_id, task_id))
            next_id = self._spawn_next(connection, row)
            if next_id is not None:
                connection.execute("UPDATE tasks SET next_id = ? WHERE id = ?", (next_id, task_id))
            return {"task": self._load(connection, task_id), "next": self._load_if_present(connection, next_id)}

    def uncomplete(self, task_id: int) -> dict:
        with self.database.connect(immediate=True) as connection:
            row = self._editable_row(connection, task_id)
            binned = None
            if row["completed_at"] is not None:
                self._refuse_when_parent_done(connection, row)
                binned = self._bin_open_successor(connection, row["next_id"])
                current = self.clock()
                connection.execute("UPDATE tasks SET completed_at = NULL, updated_at = ? WHERE parent_id = ? AND completed_at = ?",
                                   (current, task_id, row["completed_at"]))
                connection.execute("UPDATE tasks SET completed_at = NULL, next_id = NULL, updated_at = ? WHERE id = ?",
                                   (current, task_id))
            return {"task": self._load(connection, task_id), "binned": binned}

    def trash(self, task_id: int) -> dict:
        with self.database.connect(immediate=True) as connection:
            self._row(connection, task_id)
            current = self.clock()
            connection.execute("UPDATE tasks SET trashed_at = ? WHERE (id = ? OR parent_id = ?) AND trashed_at IS NULL",
                               (current, task_id, task_id))
            return self._load(connection, task_id)

    def restore(self, task_id: int) -> dict:
        with self.database.connect(immediate=True) as connection:
            row = self._row(connection, task_id)
            if row["trashed_at"] is None:
                return self._load(connection, task_id)
            if row["parent_id"] is not None and self._row(connection, row["parent_id"])["trashed_at"] is not None:
                raise TaskError("restore the parent task first, its subtasks come back with it", status=409)
            connection.execute("UPDATE tasks SET trashed_at = NULL WHERE id = ? OR (parent_id = ? AND trashed_at = ?)",
                               (task_id, task_id, row["trashed_at"]))
            return self._load(connection, task_id)

    def delete_forever(self, task_id: int) -> None:
        with self.database.connect(immediate=True) as connection:
            if self._row(connection, task_id)["trashed_at"] is None:
                raise TaskError("move the task to the bin first", status=409)
            connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

    def empty_bin(self) -> int:
        with self.database.connect(immediate=True) as connection:
            return connection.execute("DELETE FROM tasks WHERE trashed_at IS NOT NULL").rowcount

    def purge_expired(self) -> int:
        cutoff = self.clock() - self.bin_seconds
        with self.database.connect(immediate=True) as connection:
            return connection.execute("DELETE FROM tasks WHERE trashed_at IS NOT NULL AND trashed_at <= ?", (cutoff,)).rowcount

    def list_tasks(self, view: str = "today", list_id=None, query: str = "") -> list[dict]:
        return self.list_view(view, list_id, query)["tasks"]

    def list_view(self, view: str = "today", list_id=None, query: str = "") -> dict:
        if view not in VIEWS:
            raise TaskError(f"view must be one of {', '.join(VIEWS)}")
        self.purge_expired()
        today = self.today()
        parameters = {"today": today.isoformat(), "horizon": (today + timedelta(days=UPCOMING_DAYS)).isoformat()}
        if view == "list":
            parameters["list_id"] = _clean_id(list_id, "listId")
        if view == "search":
            search = _clean_text(query, "the search", MAX_QUERY_LENGTH)
            parameters["match"] = fts_query(search)
            if not parameters["match"]:
                return {"tasks": [], "more": False}
        sql = f"{TASK_SELECT} WHERE {VIEW_FILTERS[view]} ORDER BY {VIEW_ORDER[view]} LIMIT {MAX_LISTED_TASKS + 1}"
        with self.database.connect() as connection:
            if view == "list":
                self._list_row(connection, parameters["list_id"])
            rows = connection.execute(sql, parameters).fetchall()
            tasks = [self._task_dict(connection, row) for row in rows[:MAX_LISTED_TASKS]]
            return {"tasks": tasks, "more": len(rows) > MAX_LISTED_TASKS}

    def all_tasks(self) -> list[dict]:
        self.purge_expired()
        with self.database.connect() as connection:
            rows = connection.execute(f"{TASK_SELECT} WHERE t.parent_id IS NULL ORDER BY t.id").fetchall()
            return [self._task_dict(connection, row, include_binned_subtasks=True) for row in rows]

    def insert_imported(self, list_names: list[str], tasks: list[dict]) -> int:
        added = 0
        with self.database.connect(immediate=True) as connection:
            list_ids = {name.lower(): self.list_id_for_name(connection, name) for name in list_names}
            for task in tasks:
                parent_id = self._insert_imported(connection, task, list_ids, None)
                added += 1
                for subtask in task["subtasks"]:
                    self._insert_imported(connection, subtask, list_ids, parent_id)
                    added += 1
        return added

    def _insert_imported(self, connection: sqlite3.Connection, task: dict, list_ids: dict, parent_id: int | None) -> int:
        current = self.clock()
        list_id = list_ids[task["list"].lower()] if task.get("list") else INBOX_ID
        if parent_id is not None:
            list_id = connection.execute("SELECT list_id FROM tasks WHERE id = ?", (parent_id,)).fetchone()[0]
        fields = {**TASK_DEFAULTS, **task["fields"], "listId": list_id, "parentId": parent_id}
        if parent_id is not None or fields["dueDate"] is None:
            fields["repeat"] = None
        if fields["dueDate"] is None:
            fields["dueTime"] = None
        return self._insert(connection, fields, completed_at=task.get("completedAt"), trashed_at=task.get("trashedAt"),
                            created_at=task.get("createdAt") or current, updated_at=task.get("updatedAt") or current)

    def _insert_list(self, connection: sqlite3.Connection, name: str) -> int:
        if self._find_list(connection, name) is not None:
            raise TaskError(f"there is already a list called {name}", status=409)
        return connection.execute("INSERT INTO lists (name, created_at) VALUES (?, ?)", (name, self.clock())).lastrowid

    def _find_list(self, connection: sqlite3.Connection, name: str) -> int | None:
        row = connection.execute("SELECT id FROM lists WHERE name = ?", (name,)).fetchone()
        return row["id"] if row else None

    def _list_row(self, connection: sqlite3.Connection, list_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM lists WHERE id = ?", (list_id,)).fetchone()
        if row is None:
            raise TaskError("no such list", status=404)
        return row

    def _list_entry(self, list_id: int) -> dict:
        return next(entry for entry in self.lists() if entry["id"] == list_id)

    def _check_parent(self, connection: sqlite3.Connection, task_id: int | None, fields: dict) -> None:
        parent_id = fields["parentId"]
        if parent_id is None:
            return
        if parent_id == task_id:
            raise TaskError("a task cannot be its own subtask")
        parent = connection.execute("SELECT * FROM tasks WHERE id = ?", (parent_id,)).fetchone()
        if parent is None:
            raise TaskError("no such parent task", status=404)
        if parent["parent_id"] is not None:
            raise TaskError("subtasks go one level deep: pick a task that is not a subtask itself")
        if parent["trashed_at"] is not None:
            raise TaskError("that parent task is in the bin", status=409)
        if parent["completed_at"] is not None:
            raise TaskError("that parent task is done; mark it not done first", status=409)
        held = connection.execute("SELECT COUNT(*) FROM tasks WHERE parent_id = ? AND id != ?", (parent_id, task_id or 0)).fetchone()[0]
        if held >= MAX_SUBTASKS_PER_TASK:
            raise TaskError(f"a task can hold at most {MAX_SUBTASKS_PER_TASK} subtasks, done and binned ones included", status=409)
        if fields["repeat"] is not None:
            raise TaskError("a subtask cannot repeat; set the repeat on its parent")
        fields["listId"] = parent["list_id"]

    def _check_due(self, fields: dict) -> None:
        if fields["dueTime"] is not None and fields["dueDate"] is None:
            raise TaskError("a due time needs a due date")
        if fields["repeat"] is not None and fields["dueDate"] is None:
            raise TaskError("a repeating task needs a due date")

    def _check_new_task(self, connection: sqlite3.Connection, fields: dict) -> None:
        if not fields["title"]:
            raise TaskError("a task needs a title")
        self._check_due(fields)
        self._check_parent(connection, None, fields)
        self._list_row(connection, fields["listId"])

    def _check_changed_task(self, connection: sqlite3.Connection, task_id: int, row: sqlite3.Row, fields: dict) -> None:
        self._check_due(fields)
        if fields["parentId"] is not None and fields["repeat"] is not None:
            raise TaskError("a subtask cannot repeat; set the repeat on its parent")
        if fields["parentId"] is not None:
            has_subtasks = connection.execute("SELECT 1 FROM tasks WHERE parent_id = ? LIMIT 1", (task_id,)).fetchone()
            if has_subtasks:
                raise TaskError("this task has subtasks, so it cannot become a subtask itself")
            if row["parent_id"] is None or fields["parentId"] != row["parent_id"]:
                self._check_parent(connection, task_id, fields)
            else:
                fields["listId"] = row["list_id"]
        self._list_row(connection, fields["listId"])

    def _column_value(self, name: str, value):
        return rule_to_text(value) if name == "repeat" else value

    def _fields_of(self, row: sqlite3.Row) -> dict:
        return {"title": row["title"], "notes": row["notes"], "dueDate": row["due_date"], "dueTime": row["due_time"],
                "priority": row["priority"], "listId": row["list_id"], "parentId": row["parent_id"],
                "repeat": rule_from_text(row["repeat_rule"])}

    def _insert(self, connection: sqlite3.Connection, fields: dict, completed_at=None, trashed_at=None,
                created_at=None, updated_at=None) -> int:
        current = self.clock()
        cursor = connection.execute(
            "INSERT INTO tasks (list_id, parent_id, title, notes, due_date, due_time, priority, repeat_rule, completed_at,"
            " trashed_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (fields["listId"], fields["parentId"], fields["title"], fields["notes"], fields["dueDate"], fields["dueTime"],
             fields["priority"], rule_to_text(fields["repeat"]), completed_at, trashed_at,
             created_at or current, updated_at or current))
        return cursor.lastrowid

    def _spawn_next(self, connection: sqlite3.Connection, row: sqlite3.Row) -> int | None:
        rule = rule_from_text(row["repeat_rule"])
        due = stored_due_date(row["due_date"])
        if rule is None or due is None or row["parent_id"] is not None:
            return None
        following = next_due_date(rule, due, self.today())
        if following is None:
            return None
        fields = {**self._fields_of(row), "dueDate": following.isoformat(), "dueTime": stored_due_time(row["due_time"])}
        next_id = self._insert(connection, fields)
        shift = (following - due).days
        subtasks = connection.execute("SELECT * FROM tasks WHERE parent_id = ? AND trashed_at IS NULL ORDER BY id", (row["id"],)).fetchall()
        for subtask in subtasks:
            subtask_fields = {**self._fields_of(subtask), "parentId": next_id, "repeat": None,
                              "dueDate": _shifted(subtask["due_date"], shift)}
            if subtask_fields["dueDate"] is None:
                subtask_fields["dueTime"] = None
            self._insert(connection, subtask_fields)
        return next_id

    def _bin_open_successor(self, connection: sqlite3.Connection, next_id: int | None) -> int | None:
        if next_id is None:
            return None
        successor = connection.execute("SELECT * FROM tasks WHERE id = ?", (next_id,)).fetchone()
        if successor is None or successor["completed_at"] is not None or successor["trashed_at"] is not None:
            return None
        connection.execute("UPDATE tasks SET trashed_at = ? WHERE (id = ? OR parent_id = ?) AND trashed_at IS NULL",
                           (self.clock(), next_id, next_id))
        return next_id

    def _refuse_when_parent_done(self, connection: sqlite3.Connection, row: sqlite3.Row) -> None:
        if row["parent_id"] is None:
            return
        if self._row(connection, row["parent_id"])["completed_at"] is not None:
            raise TaskError("the parent task is done; mark it not done first", status=409)

    def _row(self, connection: sqlite3.Connection, task_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskError("no such task", status=404)
        return row

    def _editable_row(self, connection: sqlite3.Connection, task_id: int) -> sqlite3.Row:
        row = self._row(connection, task_id)
        is_binned = row["trashed_at"] is not None
        if not is_binned and row["parent_id"] is not None:
            is_binned = self._row(connection, row["parent_id"])["trashed_at"] is not None
        if is_binned:
            raise TaskError("restore the task from the bin before changing it", status=409)
        return row

    def _load(self, connection: sqlite3.Connection, task_id: int) -> dict:
        row = connection.execute(f"{TASK_SELECT} WHERE t.id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskError("no such task", status=404)
        return self._task_dict(connection, row)

    def _load_if_present(self, connection: sqlite3.Connection, task_id: int | None) -> dict | None:
        if task_id is None:
            return None
        row = connection.execute(f"{TASK_SELECT} WHERE t.id = ?", (task_id,)).fetchone()
        return None if row is None else self._task_dict(connection, row)

    def _is_overdue(self, row: sqlite3.Row, due: date | None, clock_time: str | None, now: datetime) -> bool:
        if due is None or row["completed_at"] is not None:
            return False
        today = now.date()
        if due != today:
            return due < today
        return clock_time is not None and clock_time < now.strftime("%H:%M")

    def _task_dict(self, connection: sqlite3.Connection, row: sqlite3.Row, include_binned_subtasks: bool = False) -> dict:
        due = stored_due_date(row["due_date"])
        clock_time = stored_due_time(row["due_time"]) if due else None
        rule = rule_from_text(row["repeat_rule"])
        task = {
            "id": row["id"],
            "title": row["title"],
            "notes": row["notes"],
            "listId": row["list_id"],
            "list": row["list_name"],
            "parentId": row["parent_id"],
            "parentTitle": row["parent_title"],
            "dueDate": due.isoformat() if due else None,
            "dueTime": clock_time,
            "dueAt": due_at(due, clock_time, self.zone),
            "priority": row["priority"],
            "repeat": rule,
            "repeatText": describe(rule),
            "completed": row["completed_at"] is not None,
            "completedAt": to_iso(row["completed_at"]),
            "trashed": row["trashed_at"] is not None,
            "trashedAt": to_iso(row["trashed_at"]),
            "overdue": self._is_overdue(row, due, clock_time, self.now()) and row["trashed_at"] is None,
            "createdAt": to_iso(row["created_at"]),
            "updatedAt": to_iso(row["updated_at"]),
        }
        if row["parent_id"] is None:
            task["subtasks"] = self._subtasks(connection, row, include_binned_subtasks)
        return task

    def _subtasks(self, connection: sqlite3.Connection, row: sqlite3.Row, include_binned: bool) -> list[dict]:
        if include_binned:
            condition = ""
        elif row["trashed_at"] is None:
            condition = " AND t.trashed_at IS NULL"
        else:
            condition = " AND t.trashed_at = :trashed_at"
        rows = connection.execute(f"{TASK_SELECT} WHERE t.parent_id = :parent_id{condition} ORDER BY t.completed_at IS NOT NULL, t.id",
                                  {"parent_id": row["id"], "trashed_at": row["trashed_at"]}).fetchall()
        return [self._task_dict(connection, subtask) for subtask in rows]
