import csv
import io

from .dates import parse_iso, to_iso
from .errors import TaskError
from .quickadd import parse_schedule
from .store import DEFAULT_PRIORITY, MAX_NOTES_LENGTH, MAX_SUBTASKS_PER_TASK, TaskStore, clean_fields, clean_list_name

APP_NAME = "tasks"
EXPORT_VERSION = 1
MAX_IMPORT_TASKS = 10_000
MAX_IMPORT_LISTS = 1000
DEFAULT_TODOIST_LIST = "Todoist"
TASK_FIELDS = ("title", "notes", "dueDate", "dueTime", "priority", "repeat")
SUBTASK_FIELDS = ("title", "notes", "dueDate", "dueTime", "priority")
TIMESTAMP_FIELDS = ("completedAt", "trashedAt", "createdAt", "updatedAt")
TODOIST_TASK = "task"
TODOIST_NOTE = "note"
TODOIST_SECTION = "section"
TODOIST_TOP_INDENT = 1
TODOIST_REQUIRED_COLUMNS = ("TYPE", "CONTENT")
TODOIST_DATE_NOTE = "Todoist date: "
NOTES_JOINER = "\n\n"


def _exported(task: dict, fields: tuple[str, ...]) -> dict:
    exported = {name: task[name] for name in fields}
    exported.update({name: task[name] for name in TIMESTAMP_FIELDS})
    return exported


def export_tasks(store: TaskStore) -> dict:
    tasks = []
    for task in store.all_tasks():
        exported = {"list": task["list"], **_exported(task, TASK_FIELDS)}
        exported["subtasks"] = [_exported(subtask, SUBTASK_FIELDS) for subtask in task["subtasks"]]
        tasks.append(exported)
    return {
        "app": APP_NAME,
        "version": EXPORT_VERSION,
        "exportedAt": to_iso(store.clock()),
        "lists": [entry["name"] for entry in store.lists()],
        "tasks": tasks,
    }


def _check_schedule(fields: dict) -> None:
    if fields.get("dueTime") is not None and fields.get("dueDate") is None:
        raise TaskError("a due time needs a due date")
    if fields.get("repeat") is not None and fields.get("dueDate") is None:
        raise TaskError("a repeating task needs a due date")


def _imported(raw, allowed: tuple[str, ...]) -> dict:
    if not isinstance(raw, dict):
        raise TaskError("each task must be an object")
    extra = {"list", "subtasks"} if "repeat" in allowed else set()
    unknown = sorted(set(raw) - set(allowed) - set(TIMESTAMP_FIELDS) - extra)
    if unknown:
        raise TaskError(f"unknown field: {', '.join(unknown)}")
    fields = clean_fields({name: raw[name] for name in allowed if name in raw})
    if "title" not in fields:
        raise TaskError("a task needs a title")
    _check_schedule(fields)
    timestamps = {name: None if raw.get(name) is None else parse_iso(raw[name], name) for name in TIMESTAMP_FIELDS}
    return {"fields": fields, **timestamps}


def _imported_task(raw, position: int) -> dict:
    try:
        task = _imported(raw, TASK_FIELDS)
        list_name = raw.get("list")
        task["list"] = None if list_name is None else clean_list_name(list_name)
        subtasks = raw.get("subtasks", [])
        if not isinstance(subtasks, list) or len(subtasks) > MAX_SUBTASKS_PER_TASK:
            raise TaskError(f"subtasks must be a list of at most {MAX_SUBTASKS_PER_TASK} tasks")
        task["subtasks"] = [_imported(subtask, SUBTASK_FIELDS) for subtask in subtasks]
        return task
    except TaskError as error:
        raise TaskError(f"task {position}: {error}") from error


def import_tasks(store: TaskStore, payload, check_only: bool = False) -> int:
    if not isinstance(payload, dict) or payload.get("app") != APP_NAME:
        raise TaskError("this is not a Starfire Tasks export")
    if payload.get("version") != EXPORT_VERSION:
        raise TaskError(f"this export is version {payload.get('version')!r}, only version {EXPORT_VERSION} can be read")
    lists, tasks = payload.get("lists", []), payload.get("tasks")
    if not isinstance(lists, list) or len(lists) > MAX_IMPORT_LISTS:
        raise TaskError(f"lists must be a list of at most {MAX_IMPORT_LISTS} names")
    if not isinstance(tasks, list):
        raise TaskError("the export has no list of tasks")
    cleaned = [_imported_task(raw, position) for position, raw in enumerate(tasks, start=1)]
    rows = sum(1 + len(task["subtasks"]) for task in cleaned)
    if rows > MAX_IMPORT_TASKS:
        raise TaskError(f"an import can hold at most {MAX_IMPORT_TASKS} tasks, subtasks included")
    names = [clean_list_name(name) for name in lists] + [task["list"] for task in cleaned if task["list"]]
    if len({name.lower() for name in names}) > MAX_IMPORT_LISTS:
        raise TaskError(f"an import can name at most {MAX_IMPORT_LISTS} lists, counting the lists array and every task's list")
    if check_only:
        return rows
    return store.insert_imported(names, cleaned)


def _todoist_rows(data: bytes) -> list[dict]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise TaskError("that file is not UTF-8 text; export it from Todoist as CSV again") from error
    reader = csv.DictReader(io.StringIO(text, newline=""))
    try:
        header = [name.strip().upper() for name in (reader.fieldnames or [])]
        if not all(column in header for column in TODOIST_REQUIRED_COLUMNS):
            raise TaskError("that is not a Todoist CSV export (it needs TYPE and CONTENT columns)")
        rows = []
        for raw in reader:
            rows.append({(key or "").strip().upper(): (value or "").strip() if isinstance(value, str) else "" for key, value in raw.items()})
            if len(rows) > MAX_IMPORT_TASKS:
                raise TaskError(f"a Todoist file can hold at most {MAX_IMPORT_TASKS} rows")
    except csv.Error as error:
        raise TaskError(f"that CSV file could not be read: {error}") from error
    return rows


def _todoist_priority(value: str, row_number: int) -> int:
    if not value:
        return DEFAULT_PRIORITY
    if value not in ("1", "2", "3", "4"):
        raise TaskError(f"row {row_number}: PRIORITY must be 1 to 4, got {value!r}")
    return int(value)


def _todoist_indent(value: str) -> int:
    return int(value) if value.isascii() and value.isdigit() and len(value) <= 2 else TODOIST_TOP_INDENT


def _with_note(notes: str, extra: str) -> str:
    return f"{notes}{NOTES_JOINER}{extra}" if notes else extra


class TodoistImport:
    def __init__(self, store: TaskStore):
        self.store = store
        self.tasks: list[dict] = []
        self.last: dict | None = None
        self.sections = 0
        self.dates_in_notes = 0

    def add_task(self, row: dict, row_number: int) -> None:
        notes = row.get("DESCRIPTION", "")
        schedule = self._schedule(row.get("DATE", ""))
        if schedule is None and row.get("DATE"):
            notes = _with_note(notes, TODOIST_DATE_NOTE + row["DATE"])
            self.dates_in_notes += 1
        due_date, due_time, rule = schedule or (None, None, None)
        is_subtask = _todoist_indent(row.get("INDENT", "")) > TODOIST_TOP_INDENT and bool(self.tasks)
        if is_subtask and len(self.tasks[-1]["subtasks"]) >= MAX_SUBTASKS_PER_TASK:
            raise TaskError(f"row {row_number}: a task can hold at most {MAX_SUBTASKS_PER_TASK} subtasks")
        if is_subtask and rule is not None:
            notes = _with_note(notes, TODOIST_DATE_NOTE + row["DATE"])
            rule = None
        raw = {"title": row.get("CONTENT", ""), "notes": notes, "priority": _todoist_priority(row.get("PRIORITY", ""), row_number),
               "dueDate": due_date.isoformat() if due_date else None, "dueTime": due_time}
        if not is_subtask:
            raw["repeat"] = rule
        try:
            fields = clean_fields(raw)
        except TaskError as error:
            raise TaskError(f"row {row_number}: {error}") from error
        task = {"fields": fields, "completedAt": None, "trashedAt": None, "createdAt": None, "updatedAt": None}
        if is_subtask:
            self.tasks[-1]["subtasks"].append(task)
        else:
            task.update({"list": None, "subtasks": []})
            self.tasks.append(task)
        self.last = task

    def add_note(self, row: dict, row_number: int) -> None:
        if self.last is None or not row.get("CONTENT"):
            return
        notes = _with_note(self.last["fields"]["notes"], row["CONTENT"])
        if len(notes) > MAX_NOTES_LENGTH:
            raise TaskError(f"row {row_number}: the comments make the notes longer than {MAX_NOTES_LENGTH} characters")
        self.last["fields"]["notes"] = notes

    def _schedule(self, text: str):
        if not text:
            return None
        return parse_schedule(text, self.store.now())


def import_todoist_csv(store: TaskStore, data: bytes, list_name=None) -> dict:
    name = clean_list_name(list_name or DEFAULT_TODOIST_LIST)
    importer = TodoistImport(store)
    for row_number, row in enumerate(_todoist_rows(data), start=2):
        kind = row.get("TYPE", "").lower()
        if kind == TODOIST_TASK:
            importer.add_task(row, row_number)
        elif kind == TODOIST_NOTE:
            importer.add_note(row, row_number)
        elif kind == TODOIST_SECTION:
            importer.sections += 1
        elif kind:
            raise TaskError(f"row {row_number}: TYPE {kind!r} is not task, note or section")
    for task in importer.tasks:
        task["list"] = name
    added = store.insert_imported([name], importer.tasks)
    return {"added": added, "list": name, "sectionsSkipped": importer.sections, "datesKeptInNotes": importer.dates_in_notes}
