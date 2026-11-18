import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tasks.db import INBOX_ID

SCRIPT = Path(__file__).resolve().parent.parent / "tasks" / "static" / "import-parts.js"
LIMITS = {"tasks": 10_000, "lists": 1000, "bytes": 10 * 1024 * 1024}
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is needed to run the page's import splitter")


def split(payload, limits=LIMITS):
    program = ("const { importParts } = require(process.argv[1]);"
               "let input = '';"
               "process.stdin.on('data', (chunk) => { input += chunk; });"
               "process.stdin.on('end', () => { const { payload, limits } = JSON.parse(input);"
               " process.stdout.write(JSON.stringify(importParts(payload, limits))); });")
    result = subprocess.run([NODE, "-e", program, str(SCRIPT)], input=json.dumps({"payload": payload, "limits": limits}),
                            capture_output=True, text=True, check=True, timeout=60)
    return json.loads(result.stdout)


def export_of(tasks, lists=()):
    return {"app": "tasks", "version": 1, "lists": list(lists), "tasks": tasks}


def rows(part):
    return sum(1 + len(task.get("subtasks", [])) for task in part["tasks"])


def test_a_small_export_stays_one_part():
    payload = export_of([{"title": "a", "list": "Work"}], ["Inbox", "Work"])
    assert split(payload) == [{"app": "tasks", "version": 1, "lists": ["Work", "Inbox"], "tasks": payload["tasks"]}]


def test_parts_hold_at_most_the_task_cap_and_keep_the_order():
    tasks = [{"title": f"t{number}", "subtasks": [{"title": "s"}] * 3} for number in range(2600)]
    parts = split(export_of(tasks))
    assert [rows(part) for part in parts] == [10_000, 400]
    assert [task for part in parts for task in part["tasks"]] == tasks


def test_parts_stay_under_the_byte_cap():
    tasks = [{"title": f"t{number}", "notes": "é" * 9_000, "subtasks": [{"title": "s", "notes": "ü" * 9_000}] * 40}
             for number in range(30)]
    parts = split(export_of(tasks))
    assert len(parts) > 1
    for part in parts:
        assert len(json.dumps(part, ensure_ascii=False).encode()) <= LIMITS["bytes"]
    assert [task for part in parts for task in part["tasks"]] == tasks


def test_parts_name_at_most_the_list_cap_and_every_list_comes_along():
    tasks = [{"title": f"t{number}", "list": f"list {number}"} for number in range(2500)]
    lists = [f"list {number}" for number in range(2600)]
    parts = split(export_of(tasks, lists))
    assert all(len(part["lists"]) <= LIMITS["lists"] for part in parts)
    assert sorted(name for part in parts for name in part["lists"]) == sorted(lists)
    for part in parts:
        assert {task["list"] for task in part["tasks"]} <= set(part["lists"])


@pytest.mark.parametrize("payload", [[1, 2], {"app": "tasks", "version": 1}, {"app": "tasks", "tasks": "x"}, None])
def test_anything_that_is_not_an_export_is_sent_as_it_is(payload):
    assert split(payload) == [payload]


def test_an_export_bigger_than_one_import_comes_back_whole_through_the_parts(owner, database, store):
    with database.connect(immediate=True) as connection:
        connection.executemany("INSERT INTO tasks (list_id, title, created_at, updated_at) VALUES (1, ?, 1, 1)",
                               [(f"t{number}",) for number in range(10_001)])
    exported = json.loads(owner.get("/api/export").get_data(as_text=True))
    limits = owner.get("/api/session").get_json()["importLimits"]
    with database.connect(immediate=True) as connection:
        connection.execute("DELETE FROM tasks")
    parts = split(exported, limits)
    assert len(parts) == 2
    for part in parts:
        assert owner.post("/api/import?check=1", json=part).status_code == 200
    assert sum(owner.post("/api/import", json=part).get_json()["imported"] for part in parts) == 10_001
    assert len(store.all_tasks()) == 10_001
    assert owner.get(f"/api/tasks?view=list&list={INBOX_ID}").get_json()["more"] is True
