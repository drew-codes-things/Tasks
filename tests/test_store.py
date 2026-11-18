import pytest
from conftest import LONDON, utc

from tasks.db import INBOX_ID
from tasks.errors import TaskError
from tasks.store import TaskStore

DAY = 24 * 3600


def titles(tasks):
    return [task["title"] for task in tasks]


def add(store, title, **fields):
    return store.create({"title": title, **fields})


def test_a_new_task_lands_in_the_inbox_with_the_lowest_priority(store):
    task = add(store, "  water   the plants ")
    assert task["title"] == "water the plants"
    assert (task["listId"], task["list"], task["priority"]) == (INBOX_ID, "Inbox", 4)
    assert task["subtasks"] == []
    assert task["completed"] is False and task["trashed"] is False


@pytest.mark.parametrize("fields", [
    {}, {"title": ""}, {"title": "   "}, {"title": 5}, {"title": "x" * 501},
    {"title": "x", "priority": 0}, {"title": "x", "priority": 5}, {"title": "x", "priority": True}, {"title": "x", "priority": "1"},
    {"title": "x", "dueDate": "2026-02-30"}, {"title": "x", "dueDate": "13/11/2026"}, {"title": "x", "dueDate": "1899-12-31"},
    {"title": "x", "dueDate": "3000-01-01"}, {"title": "x", "dueDate": "２０２６-11-13"},
    {"title": "x", "dueTime": "09:00"}, {"title": "x", "dueDate": "2026-11-13", "dueTime": "9:00"},
    {"title": "x", "dueDate": "2026-11-13", "dueTime": "24:00"},
    {"title": "x", "repeat": {"kind": "daily"}}, {"title": "x", "listId": 999}, {"title": "x", "listId": True},
    {"title": "x", "colour": "red"}, {"title": "x", "notes": "n" * 10_001}, {"title": "x", "parentId": 2 ** 63},
])
def test_bad_tasks_are_refused(store, fields):
    with pytest.raises(TaskError):
        store.create(fields)
    assert store.list_tasks("list", INBOX_ID) == []


def test_lists_are_unique_without_case_and_the_inbox_is_fixed(store):
    work = store.create_list("  Work  ")
    assert work["name"] == "Work"
    with pytest.raises(TaskError) as duplicate:
        store.create_list("work")
    assert duplicate.value.status == 409
    for action in (lambda: store.rename_list(INBOX_ID, "Other"), lambda: store.delete_list(INBOX_ID)):
        with pytest.raises(TaskError) as refused:
            action()
        assert refused.value.status == 409
    assert store.rename_list(work["id"], "Office")["name"] == "Office"
    assert [entry["name"] for entry in store.lists()] == ["Inbox", "Office"]
    with pytest.raises(TaskError):
        store.create_list("")


def test_deleting_a_list_moves_its_tasks_to_the_bin_and_restore_puts_them_in_the_inbox(store):
    shop = store.create_list("Shopping")
    milk = add(store, "milk", listId=shop["id"])
    add(store, "semi-skimmed", parentId=milk["id"])
    assert store.delete_list(shop["id"]) == 2
    assert [entry["name"] for entry in store.lists()] == ["Inbox"]
    binned = store.list_tasks("bin")
    assert titles(binned) == ["milk"]
    restored = store.restore(milk["id"])
    assert restored["list"] == "Inbox"
    assert titles(restored["subtasks"]) == ["semi-skimmed"]


def test_subtasks_go_one_level_deep_and_follow_their_parent(store):
    work = store.create_list("Work")
    parent = add(store, "launch", listId=work["id"])
    child = add(store, "write notes", parentId=parent["id"], listId=INBOX_ID)
    assert child["listId"] == work["id"]
    assert child["parentTitle"] == "launch"
    with pytest.raises(TaskError):
        add(store, "grandchild", parentId=child["id"])
    with pytest.raises(TaskError):
        store.update(parent["id"], {"parentId": add(store, "other")["id"]})
    with pytest.raises(TaskError):
        store.update(parent["id"], {"parentId": parent["id"]})
    moved = store.update(parent["id"], {"listId": INBOX_ID})
    assert moved["subtasks"][0]["listId"] == INBOX_ID
    assert store.update(child["id"], {"listId": work["id"]})["listId"] == INBOX_ID


def test_a_subtask_cannot_repeat(store):
    parent = add(store, "weekly review", dueDate="2026-11-13", repeat={"kind": "weekly", "days": [4]})
    with pytest.raises(TaskError):
        add(store, "inbox zero", parentId=parent["id"], dueDate="2026-11-13", repeat={"kind": "daily"})
    child = add(store, "inbox zero", parentId=parent["id"], dueDate="2026-11-13")
    with pytest.raises(TaskError):
        store.update(child["id"], {"repeat": {"kind": "daily"}})
    with pytest.raises(TaskError):
        store.update(parent["id"], {"parentId": add(store, "x")["id"]})


def test_a_task_can_become_a_subtask_and_back(store):
    parent = add(store, "move house")
    loose = add(store, "book van", priority=1)
    nested = store.update(loose["id"], {"parentId": parent["id"]})
    assert nested["parentId"] == parent["id"]
    assert titles(store.get(parent["id"])["subtasks"]) == ["book van"]
    freed = store.update(loose["id"], {"parentId": None})
    assert freed["parentId"] is None and "subtasks" in freed


def test_clearing_the_due_date_clears_its_time_and_repeat(store):
    task = add(store, "water plants", dueDate="2026-11-13", dueTime="09:00", repeat={"kind": "daily"})
    cleared = store.update(task["id"], {"dueDate": None})
    assert (cleared["dueDate"], cleared["dueTime"], cleared["repeat"]) == (None, None, None)
    with pytest.raises(TaskError):
        store.update(task["id"], {"dueTime": "10:00"})


def test_completing_a_parent_completes_its_open_subtasks(store, clock):
    parent = add(store, "trip")
    first = add(store, "tickets", parentId=parent["id"])
    add(store, "bags", parentId=parent["id"])
    store.complete(first["id"])
    clock.advance(60)
    done = store.complete(parent["id"])
    assert done["next"] is None
    assert all(subtask["completed"] for subtask in done["task"]["subtasks"])
    with pytest.raises(TaskError) as refused:
        store.uncomplete(first["id"])
    assert refused.value.status == 409
    assert store.uncomplete(parent["id"])["task"]["completed"] is False
    assert store.uncomplete(first["id"])["task"]["completed"] is False


def test_completing_a_repeating_task_makes_the_next_one(store):
    task = add(store, "pay rent", dueDate="2026-11-01", dueTime="09:00", priority=1, notes="standing order",
               repeat={"kind": "monthly", "day": 1})
    add(store, "check statement", parentId=task["id"], dueDate="2026-11-02")
    result = store.complete(task["id"])
    following = result["next"]
    assert result["task"]["completed"] is True
    assert (following["title"], following["dueDate"], following["dueTime"], following["priority"], following["notes"]) == (
        "pay rent", "2026-12-01", "09:00", 1, "standing order")
    assert following["repeat"] == {"kind": "monthly", "day": 1}
    assert [(subtask["title"], subtask["dueDate"], subtask["completed"]) for subtask in following["subtasks"]] == [
        ("check statement", "2026-12-02", False)]
    assert titles(store.list_tasks("completed")) == ["pay rent", "check statement"]


def test_completing_twice_does_not_make_two_next_tasks(store):
    task = add(store, "water plants", dueDate="2026-11-13", repeat={"kind": "daily"})
    first = store.complete(task["id"])
    second = store.complete(task["id"])
    assert first["next"]["id"] == second["next"]["id"]
    assert titles(store.list_tasks("list", INBOX_ID)) == ["water plants"]


def test_uncompleting_a_repeating_task_bins_the_untouched_next_one(store):
    task = add(store, "water plants", dueDate="2026-11-13", repeat={"kind": "daily"})
    following = store.complete(task["id"])["next"]
    undone = store.uncomplete(task["id"])
    assert undone["binned"] == following["id"]
    assert undone["task"]["completed"] is False
    assert [entry["id"] for entry in store.list_tasks("list", INBOX_ID)] == [task["id"]]
    assert store.complete(task["id"])["next"]["id"] != following["id"]


def test_a_next_task_already_done_is_left_alone_on_uncomplete(store):
    task = add(store, "water plants", dueDate="2026-11-13", repeat={"kind": "daily"})
    following = store.complete(task["id"])["next"]
    store.complete(following["id"])
    assert store.uncomplete(task["id"])["binned"] is None
    assert store.get(following["id"])["completed"] is True


def test_a_repeat_past_the_last_supported_day_simply_ends(store):
    task = add(store, "far future", dueDate="2999-12-31", repeat={"kind": "daily"})
    assert store.complete(task["id"])["next"] is None


def test_bin_and_restore_keep_subtasks_together(store, clock):
    parent = add(store, "garden")
    alone = add(store, "mow", parentId=parent["id"])
    add(store, "weed", parentId=parent["id"])
    store.trash(alone["id"])
    clock.advance(10)
    store.trash(parent["id"])
    assert titles(store.list_tasks("bin")) == ["garden"]
    with pytest.raises(TaskError) as refused:
        store.restore(alone["id"])
    assert refused.value.status == 409
    for action in (store.complete, lambda task_id: store.update(task_id, {"title": "x"})):
        with pytest.raises(TaskError):
            action(parent["id"])
    restored = store.restore(parent["id"])
    assert titles(restored["subtasks"]) == ["weed"]
    assert titles(store.list_tasks("bin")) == ["mow"]


def test_delete_for_good_only_from_the_bin_and_the_bin_empties_itself(store, clock):
    task = add(store, "old")
    with pytest.raises(TaskError) as refused:
        store.delete_forever(task["id"])
    assert refused.value.status == 409
    store.trash(task["id"])
    store.delete_forever(task["id"])
    with pytest.raises(TaskError):
        store.get(task["id"])
    add(store, "binned later")
    store.trash(store.list_tasks("list", INBOX_ID)[0]["id"])
    clock.advance(31 * DAY)
    assert store.list_tasks("bin") == []
    store.trash(add(store, "one more")["id"])
    assert store.empty_bin() == 1


def test_today_holds_overdue_and_today_but_not_tomorrow(store):
    add(store, "late", dueDate="2026-11-10")
    add(store, "morning", dueDate="2026-11-13", dueTime="08:00")
    add(store, "evening", dueDate="2026-11-13", dueTime="18:00")
    add(store, "whenever today", dueDate="2026-11-13", priority=1)
    add(store, "tomorrow", dueDate="2026-11-14")
    add(store, "no date")
    store.complete(add(store, "done", dueDate="2026-11-13")["id"])
    store.trash(add(store, "binned", dueDate="2026-11-13")["id"])
    today = store.list_tasks("today")
    assert titles(today) == ["late", "morning", "evening", "whenever today"]
    assert [task["overdue"] for task in today] == [True, True, False, False]
    assert store.counts() == {"today": 4, "overdue": 1, "upcoming": 1, "bin": 1}


def test_upcoming_covers_the_next_seven_days(store):
    for day in ("2026-11-13", "2026-11-14", "2026-11-20", "2026-11-21"):
        add(store, day, dueDate=day)
    assert titles(store.list_tasks("upcoming")) == ["2026-11-14", "2026-11-20"]


def test_subtasks_with_their_own_dates_show_in_the_date_views(store):
    parent = add(store, "launch", dueDate="2026-11-20")
    add(store, "slides", parentId=parent["id"], dueDate="2026-11-13")
    today = store.list_tasks("today")
    assert [(task["title"], task["parentTitle"]) for task in today] == [("slides", "launch")]
    store.trash(parent["id"])
    assert store.list_tasks("today") == []


def test_a_list_shows_open_top_level_tasks_with_their_subtasks(store):
    work = store.create_list("Work")
    later = add(store, "later", listId=work["id"])
    soon = add(store, "soon", listId=work["id"], dueDate="2026-11-14")
    urgent = add(store, "urgent", listId=work["id"], priority=1)
    add(store, "step", parentId=soon["id"])
    store.complete(add(store, "done", listId=work["id"])["id"])
    listed = store.list_tasks("list", work["id"])
    assert titles(listed) == ["soon", "urgent", "later"]
    assert titles(listed[0]["subtasks"]) == ["step"]
    assert later["id"] and urgent["id"]
    with pytest.raises(TaskError):
        store.list_tasks("list", 999)
    with pytest.raises(TaskError):
        store.list_tasks("list", None)


def test_search_matches_words_and_prefixes_in_titles_and_notes(store):
    add(store, "Ring the plumber", notes="about the boiler")
    add(store, "Café visit")
    store.complete(add(store, "Plumbing invoice")["id"])
    store.trash(add(store, "plumber binned")["id"])
    assert titles(store.list_tasks("search", query="plumb")) == ["Ring the plumber", "Plumbing invoice"]
    assert titles(store.list_tasks("search", query="boiler")) == ["Ring the plumber"]
    assert titles(store.list_tasks("search", query="cafe")) == ["Café visit"]
    assert titles(store.list_tasks("search", query="ring boiler")) == ["Ring the plumber"]


@pytest.mark.parametrize("query", ['"', "NEAR(a b)", "title:x", "a AND", "*", "(", "-", "''", "   "])
def test_search_never_breaks_on_query_syntax(store, query):
    add(store, "a title")
    assert isinstance(store.list_tasks("search", query=query), list)


def test_unknown_views_and_long_searches_are_refused(store):
    with pytest.raises(TaskError):
        store.list_tasks("someday")
    with pytest.raises(TaskError):
        store.list_tasks("search", query="x" * 201)


def test_damaged_rows_never_break_a_read(store, database):
    task = add(store, "damaged", dueDate="2026-11-13", repeat={"kind": "daily"})
    with database.connect() as connection:
        connection.execute("UPDATE tasks SET due_date = '9999-99-99', due_time = 'soon', repeat_rule = '{bad',"
                           " created_at = 1e300, completed_at = NULL WHERE id = ?", (task["id"],))
    loaded = store.get(task["id"])
    assert (loaded["dueDate"], loaded["dueTime"], loaded["repeat"], loaded["createdAt"]) == (None, None, None, None)
    assert store.complete(task["id"])["next"] is None
    assert titles(store.list_tasks("completed")) == ["damaged"]


def test_quick_add_creates_the_list_and_fills_the_fields(store):
    task, understood = store.quick_add("dentist fri 9:30 !1 #health")
    assert (task["title"], task["dueDate"], task["dueTime"], task["priority"], task["list"]) == (
        "dentist", "2026-11-20", "09:30", 1, "health")
    again, _ = store.quick_add("physio tue #Health")
    assert again["listId"] == task["listId"]
    assert understood["list"] == "health"
    plain, _ = store.quick_add("milk")
    assert (plain["list"], plain["priority"], plain["dueDate"]) == ("Inbox", 4, None)
    child, _ = store.quick_add("bring card #elsewhere", parent_id=task["id"])
    assert child["listId"] == task["listId"]


def test_overdue_includes_a_time_earlier_today(store, clock):
    task = add(store, "call", dueDate="2026-11-13", dueTime="10:30")
    assert store.get(task["id"])["overdue"] is False
    clock.advance(31 * 60)
    assert store.get(task["id"])["overdue"] is True


def test_today_follows_the_london_date_across_the_clock_changes(database):
    after_midnight_in_summer_time = TaskStore(database, clock=lambda: utc(2026, 10, 24, 23, 30), zone=LONDON)
    assert after_midnight_in_summer_time.today().isoformat() == "2026-10-25"
    late_in_winter_time = TaskStore(database, clock=lambda: utc(2027, 3, 27, 23, 30), zone=LONDON)
    assert late_in_winter_time.today().isoformat() == "2027-03-27"
    assert late_in_winter_time.preview_quick_add("x tomorrow")["dueDate"] == "2027-03-28"
    first_summer_night = TaskStore(database, clock=lambda: utc(2027, 3, 28, 23, 30), zone=LONDON)
    assert first_summer_night.preview_quick_add("x today")["dueDate"] == "2027-03-29"


def test_due_at_is_the_wall_clock_time_in_london(store):
    def due_at(day, clock_time):
        return add(store, "x", dueDate=day, dueTime=clock_time)["dueAt"]
    assert due_at("2026-10-24", "09:00") == "2026-10-24T08:00:00Z"
    assert due_at("2026-10-26", "09:00") == "2026-10-26T09:00:00Z"
    assert due_at("2026-10-25", "01:30") == "2026-10-25T00:30:00Z"
    assert due_at("2027-03-28", "01:30") == "2027-03-28T01:30:00Z"


def test_a_daily_repeat_keeps_its_local_time_over_the_clock_change(store, clock):
    clock.set(utc(2026, 10, 24, 9, 0))
    task = add(store, "water plants", dueDate="2026-10-24", dueTime="09:00", repeat={"kind": "daily"})
    following = store.complete(task["id"])["next"]
    assert (following["dueDate"], following["dueTime"], following["dueAt"]) == ("2026-10-25", "09:00", "2026-10-25T09:00:00Z")
    assert task["dueAt"] == "2026-10-24T08:00:00Z"


def test_insert_imported_creates_lists_and_keeps_timestamps(store):
    tasks = [{"list": "Home", "fields": {"title": "imported", "dueDate": None, "dueTime": "09:00", "repeat": {"kind": "daily"}},
              "completedAt": None, "trashedAt": None, "createdAt": 1_700_000_000.0, "updatedAt": None,
              "subtasks": [{"fields": {"title": "child"}, "completedAt": 1_700_000_100.0, "trashedAt": None,
                            "createdAt": None, "updatedAt": None}]}]
    assert store.insert_imported(["Home"], tasks) == 2
    home = next(entry for entry in store.lists() if entry["name"] == "Home")
    loaded = store.list_tasks("list", home["id"])[0]
    assert (loaded["dueTime"], loaded["repeat"], loaded["createdAt"]) == (None, None, "2023-11-14T22:13:20Z")
    assert loaded["subtasks"][0]["completed"] is True


def test_two_completions_at_once_make_one_next_task(store, database):
    import sqlite3
    import threading
    import time
    task = add(store, "water plants", dueDate="2026-11-13", repeat={"kind": "daily"})
    other = sqlite3.connect(database.path, timeout=10, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    worker = threading.Thread(target=store.complete, args=(task["id"],))
    worker.start()
    time.sleep(0.5)
    other.execute("UPDATE tasks SET completed_at = 1 WHERE id = ?", (task["id"],))
    other.execute("COMMIT")
    other.close()
    worker.join(timeout=15)
    assert not worker.is_alive()
    assert titles(store.list_tasks("list", INBOX_ID)) == []


def test_not_done_on_a_parent_reopens_the_subtasks_its_done_closed(store, clock):
    parent = add(store, "pack")
    early = add(store, "passport", parentId=parent["id"])
    for name in ("socks", "shirt", "charger"):
        add(store, name, parentId=parent["id"])
    store.complete(early["id"])
    clock.advance(60)
    store.complete(parent["id"])
    clock.advance(60)
    reopened = store.uncomplete(parent["id"])["task"]
    assert reopened["completed"] is False
    assert [(subtask["title"], subtask["completed"]) for subtask in reopened["subtasks"]] == [
        ("socks", False), ("shirt", False), ("charger", False), ("passport", True)]


def test_not_done_on_a_subtask_leaves_its_parent_and_siblings_alone(store, clock):
    parent = add(store, "pack")
    first = add(store, "socks", parentId=parent["id"])
    second = add(store, "shirt", parentId=parent["id"])
    store.complete(first["id"])
    store.complete(second["id"])
    clock.advance(60)
    store.uncomplete(first["id"])
    assert [(subtask["title"], subtask["completed"]) for subtask in store.get(parent["id"])["subtasks"]] == [
        ("socks", False), ("shirt", True)]


@pytest.mark.parametrize("fields", [{"title": "a\ud800b"}, {"title": "ok", "notes": "x\udfffy"}])
def test_text_with_a_lone_surrogate_is_refused(store, fields):
    with pytest.raises(TaskError) as refused:
        store.create(fields)
    assert refused.value.status == 400
    with pytest.raises(TaskError):
        store.quick_add("a\ud800b tomorrow")
    with pytest.raises(TaskError):
        store.create_list("bad \udfff")
    assert store.all_tasks() == []


def test_a_task_holds_at_most_500_subtasks_counting_done_and_binned_ones(store, database):
    parent = add(store, "big")
    with database.connect(immediate=True) as connection:
        connection.executemany("INSERT INTO tasks (list_id, parent_id, title, created_at, updated_at) VALUES (1, ?, ?, 1, 1)",
                               [(parent["id"], f"s{number}") for number in range(499)])
    last = add(store, "s499", parentId=parent["id"])
    store.trash(last["id"])
    for attempt in (lambda: add(store, "one more", parentId=parent["id"]),
                    lambda: store.quick_add("one more", parent_id=parent["id"]),
                    lambda: store.update(add(store, "loose")["id"], {"parentId": parent["id"]})):
        with pytest.raises(TaskError) as refused:
            attempt()
        assert refused.value.status == 409
        assert "at most 500 subtasks" in str(refused.value)


def test_a_list_view_says_when_it_is_cut(store, database):
    with database.connect(immediate=True) as connection:
        connection.executemany("INSERT INTO tasks (list_id, title, created_at, updated_at) VALUES (1, ?, 1, 1)",
                               [(f"t{number}",) for number in range(500)])
    assert store.list_view("list", INBOX_ID) == {"tasks": store.list_tasks("list", INBOX_ID), "more": False}
    add(store, "one more")
    view = store.list_view("list", INBOX_ID)
    assert (len(view["tasks"]), view["more"]) == (500, True)
