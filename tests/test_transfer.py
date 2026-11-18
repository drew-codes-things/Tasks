import json

import pytest
from conftest import LONDON

from tasks.db import Database
from tasks.errors import TaskError
from tasks.store import TaskStore
from tasks.transfer import export_tasks, import_todoist_csv, import_tasks

TODOIST_HEADER = "TYPE,CONTENT,DESCRIPTION,PRIORITY,INDENT,AUTHOR,RESPONSIBLE,DATE,DATE_LANG,TIMEZONE"


def fresh_store(tmp_path, clock, name):
    return TaskStore(Database(tmp_path / f"{name}.db"), clock=clock, zone=LONDON)


def seed(store):
    work = store.create_list("Work")
    store.create_list("Empty list")
    parent = store.create({"title": "launch", "listId": work["id"], "dueDate": "2026-11-20", "dueTime": "09:00",
                           "priority": 1, "notes": "big day", "repeat": {"kind": "weekly", "days": [4]}})
    store.create({"title": "slides", "parentId": parent["id"], "dueDate": "2026-11-19"})
    store.complete(store.create({"title": "done thing"})["id"])
    store.trash(store.create({"title": "binned thing"})["id"])


def comparable(store):
    return [{key: value for key, value in task.items() if key not in ("id", "listId", "parentId")}
            | {"subtasks": [{key: value for key, value in subtask.items() if key not in ("id", "listId", "parentId")}
                            for subtask in task["subtasks"]]}
            for task in store.all_tasks()]


def test_export_then_import_gives_the_same_tasks(store, tmp_path, clock):
    seed(store)
    exported = json.loads(json.dumps(export_tasks(store)))
    assert exported["app"] == "tasks" and exported["version"] == 1
    assert exported["lists"] == ["Inbox", "Empty list", "Work"]
    other = fresh_store(tmp_path, clock, "other")
    assert import_tasks(other, exported) == 4
    assert comparable(other) == comparable(store)
    assert [entry["name"] for entry in other.lists()] == ["Inbox", "Empty list", "Work"]


def test_import_adds_and_never_replaces(store):
    seed(store)
    exported = export_tasks(store)
    import_tasks(store, exported)
    assert len(store.all_tasks()) == 6


@pytest.mark.parametrize("payload", [
    None, [], {"app": "keep-notes", "version": 1, "tasks": []}, {"app": "tasks", "version": 2, "tasks": []},
    {"app": "tasks", "version": 1}, {"app": "tasks", "version": 1, "tasks": {}}, {"app": "tasks", "version": 1, "lists": "Work", "tasks": []},
    {"app": "tasks", "version": 1, "tasks": ["x"]},
    {"app": "tasks", "version": 1, "tasks": [{"notes": "no title"}]},
    {"app": "tasks", "version": 1, "tasks": [{"title": "x", "colour": "red"}]},
    {"app": "tasks", "version": 1, "tasks": [{"title": "x", "dueTime": "09:00"}]},
    {"app": "tasks", "version": 1, "tasks": [{"title": "x", "repeat": {"kind": "daily"}}]},
    {"app": "tasks", "version": 1, "tasks": [{"title": "x", "createdAt": "9999-12-31T23:59:59-23:59"}]},
    {"app": "tasks", "version": 1, "tasks": [{"title": "x", "createdAt": "0001-01-01T00:00:00+23:59"}]},
    {"app": "tasks", "version": 1, "tasks": [{"title": "x", "completedAt": "yesterday"}]},
    {"app": "tasks", "version": 1, "tasks": [{"title": "x", "dueDate": "9999-12-31"}]},
    {"app": "tasks", "version": 1, "tasks": [{"title": "x", "list": ""}]},
    {"app": "tasks", "version": 1, "tasks": [{"title": "x", "subtasks": "y"}]},
    {"app": "tasks", "version": 1, "tasks": [{"title": "x", "subtasks": [{"title": "y", "repeat": None}]}]},
    {"app": "tasks", "version": 1, "tasks": [{"title": "x", "subtasks": [{"title": "y", "subtasks": []}]}]},
])
def test_a_bad_file_is_refused_as_a_whole(store, payload):
    good = {"title": "fine"}
    if isinstance(payload, dict) and isinstance(payload.get("tasks"), list):
        payload = {**payload, "tasks": [good, *payload["tasks"]]}
    with pytest.raises(TaskError):
        import_tasks(store, payload)
    assert store.all_tasks() == []
    assert [entry["name"] for entry in store.lists()] == ["Inbox"]


def test_import_has_a_size_cap(store):
    tasks = [{"title": "x", "subtasks": [{"title": "y"}] * 4} for _ in range(2001)]
    with pytest.raises(TaskError):
        import_tasks(store, {"app": "tasks", "version": 1, "tasks": tasks})


def todoist(*rows):
    return ("﻿" + "\n".join([TODOIST_HEADER, *rows]) + "\n").encode()


def test_todoist_csv_import(store):
    data = todoist(
        "section,Errands,,,,,,,,",
        'task,Pay rent,Standing order,1,1,Test User (1),,every month on the 1st,en,Europe/London',
        "task,Check statement,,4,2,Test User (1),,,en,Europe/London",
        "note,Bank app shows it,,,,Test User (1),,,,",
        "task,Call mum,,2,1,Test User (1),,tomorrow 5pm,en,Europe/London",
        "task,Odd date,,3,1,Test User (1),,every 2nd tuesday,en,Europe/London",
        "task,Deep child,,4,3,Test User (1),,every day,en,Europe/London",
        ",,,,,,,,,",
        "task,No priority,,,,,,,,",
    )
    result = import_todoist_csv(store, data, "Home")
    assert result == {"added": 6, "list": "Home", "sectionsSkipped": 1, "datesKeptInNotes": 1}
    home = next(entry for entry in store.lists() if entry["name"] == "Home")
    tasks = store.list_tasks("list", home["id"])
    by_title = {task["title"]: task for task in tasks}
    rent = by_title["Pay rent"]
    assert (rent["priority"], rent["dueDate"], rent["repeat"], rent["notes"]) == (1, "2026-12-01", {"kind": "monthly", "day": 1}, "Standing order")
    assert rent["subtasks"][0]["title"] == "Check statement"
    assert rent["subtasks"][0]["notes"] == "Bank app shows it"
    call = by_title["Call mum"]
    assert (call["dueDate"], call["dueTime"], call["priority"]) == ("2026-11-14", "17:00", 2)
    odd = by_title["Odd date"]
    assert (odd["dueDate"], odd["notes"]) == (None, "Todoist date: every 2nd tuesday")
    deep = odd["subtasks"][0]
    assert (deep["title"], deep["repeat"], deep["notes"]) == ("Deep child", None, "Todoist date: every day")
    assert by_title["No priority"]["priority"] == 4


def test_todoist_import_defaults_to_a_todoist_list_and_joins_an_existing_one(store):
    store.create_list("todoist")
    result = import_todoist_csv(store, todoist("task,One,,,,,,,,"))
    assert result["list"] == "Todoist"
    assert [entry["name"] for entry in store.lists()] == ["Inbox", "todoist"]


@pytest.mark.parametrize("data", [b"\xff\xfe\x00bad", b"", b"name,url\nx,y\n"])
def test_files_that_are_not_todoist_csv_are_refused(store, data):
    with pytest.raises(TaskError):
        import_todoist_csv(store, data)


@pytest.mark.parametrize("bad_row", [
    "project,Big,,,,,,,,", "task,,,,,,,,,", "task,x,,7,1,,,,,", "task," + "y" * 501 + ",,,,,,,,",
    'task,"' + "z" * 200_000 + '",,,,,,,,',
])
def test_one_bad_todoist_row_refuses_the_whole_file(store, bad_row):
    with pytest.raises(TaskError):
        import_todoist_csv(store, todoist("task,kept out,,,,,,,,", bad_row))
    assert store.all_tasks() == []
    assert [entry["name"] for entry in store.lists()] == ["Inbox"]


def test_todoist_comments_that_overflow_the_notes_are_refused(store):
    with pytest.raises(TaskError):
        import_todoist_csv(store, todoist("task,x,,,,,,,,", *[f"note,{'c' * 5000},,,,,,,," for _ in range(3)]))


def test_an_export_at_the_store_limits_imports_back(store, tmp_path, clock, database):
    parent = store.create({"title": "big"})
    with database.connect(immediate=True) as connection:
        connection.executemany("INSERT INTO tasks (list_id, parent_id, title, created_at, updated_at) VALUES (1, ?, ?, 1, 1)",
                               [(parent["id"], f"s{number}") for number in range(500)])
    exported = json.loads(json.dumps(export_tasks(store)))
    fresh = fresh_store(tmp_path, clock, "fresh")
    assert import_tasks(fresh, exported) == 501
    assert comparable(fresh) == comparable(store)


def test_a_todoist_file_cannot_give_a_task_more_than_500_subtasks(store):
    rows = ["task,Parent,,4,1,,,,,"] + [f"task,Sub {number},,4,2,,,,," for number in range(501)]
    with pytest.raises(TaskError) as refused:
        import_todoist_csv(store, todoist(*rows))
    assert "row 503" in str(refused.value)
    assert store.all_tasks() == []


def test_the_list_cap_counts_the_lists_named_by_tasks(store):
    payload = {"app": "tasks", "version": 1, "tasks": [{"title": f"t{number}", "list": f"list {number}"} for number in range(1001)]}
    with pytest.raises(TaskError) as refused:
        import_tasks(store, payload)
    assert "at most 1000 lists" in str(refused.value)
    assert [entry["name"] for entry in store.lists()] == ["Inbox"]
    payload["tasks"].pop()
    assert import_tasks(store, payload) == 1000


def test_a_check_only_import_validates_and_stores_nothing(store):
    payload = {"app": "tasks", "version": 1, "lists": ["Work"], "tasks": [{"title": "x", "subtasks": [{"title": "y"}]}]}
    assert import_tasks(store, payload, check_only=True) == 2
    assert store.all_tasks() == []
    assert [entry["name"] for entry in store.lists()] == ["Inbox"]
    with pytest.raises(TaskError):
        import_tasks(store, {**payload, "tasks": [{"title": ""}]}, check_only=True)
