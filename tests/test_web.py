import pytest
from conftest import OWNER, SECRET

from tasks.auth import LocalAuth
from tasks.db import INBOX_ID
from tasks.web import create_app

BIG_ID = "9" * 30


def test_health_needs_no_sign_in(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.get_json() == {"ok": True}


def test_health_reports_a_broken_database(client, database):
    database.path.unlink()
    database.path.mkdir()
    response = client.get("/api/health")
    assert response.status_code == 503
    assert response.get_json()["ok"] is False


@pytest.mark.parametrize("path", ["/api/tasks", "/api/lists", "/api/session", "/api/v1/today", "/api/export", "/api/parse?text=x"])
def test_api_without_the_user_header_is_refused(client, path):
    assert client.get(path).status_code == 401


def test_user_header_sent_from_outside_the_proxy_is_refused(client):
    response = client.get("/api/v1/today", headers={"X-Forwarded-User": OWNER}, environ_base={"REMOTE_ADDR": "172.16.1.77"})
    assert response.status_code == 401


def test_pages_need_a_user_too(client, owner):
    assert client.get("/").status_code == 401
    page = owner.get("/")
    assert page.status_code == 200
    assert b"<title>Tasks</title>" in page.data


def test_session_names_the_user_and_hands_out_a_csrf_token(owner):
    session = owner.get("/api/session").get_json()
    assert session["user"] == OWNER
    assert len(session["csrfToken"]) >= 32
    assert (session["timeZone"], session["today"]) == ("Europe/London", "2026-11-13")


@pytest.mark.parametrize("path, body", [
    ("/api/tasks", {"title": "x"}), ("/api/tasks/quick", {"text": "x"}), ("/api/lists", {"name": "x"}),
    ("/api/v1/tasks", {"text": "x"}), ("/api/import", {"app": "tasks", "version": 1, "tasks": [{"title": "x"}]}),
])
def test_writes_without_a_csrf_token_are_refused(client, owner, path, body):
    assert client.post(path, json=body, headers=owner.headers).status_code == 403
    assert client.post(path, json=body, headers={**owner.headers, "X-CSRF-Token": "0" * 64}).status_code == 403
    assert owner.get("/api/tasks?view=search&q=x").get_json()["tasks"] == []


def test_writes_from_another_origin_are_refused(owner):
    response = owner.post("/api/tasks", json={"title": "x"}, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_writes_from_the_same_origin_are_accepted(owner):
    response = owner.post("/api/tasks", json={"title": "x"}, headers={"Origin": "http://localhost"})
    assert response.status_code == 201


def test_local_mode_answers_only_loopback_host_names(store):
    local = create_app(store, LocalAuth("me", port=8100), SECRET).test_client()
    assert local.get("/api/session", headers={"Host": "127.0.0.1:8100"}).status_code == 200
    assert local.get("/api/session", headers={"Host": "localhost:8100"}).status_code == 200
    rebound = local.get("/api/session", headers={"Host": "evil.example:8100"})
    assert rebound.status_code == 421
    planted = local.post("/api/tasks", json={"title": "planted"},
                         headers={"Host": "evil.example:8100", "Origin": "http://evil.example:8100"})
    assert planted.status_code == 421
    assert store.list_tasks("list", INBOX_ID) == []


def test_security_headers(owner):
    response = owner.get("/api/tasks")
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cache-Control"] == "no-store"
    assert "Access-Control-Allow-Origin" not in owner.get("/api/session").headers


@pytest.mark.parametrize("path", ["/static/../auth.py", "/static/%2e%2e/auth.py", "/static/fonts/../../web.py"])
def test_static_files_cannot_climb_out(owner, path):
    assert owner.get(path).status_code == 404


def test_static_files_are_served(owner):
    for name in ("app.js", "app.css", "icon.svg", "fonts/ibm-plex-sans-latin-400-normal.woff2"):
        assert owner.get(f"/static/{name}").status_code == 200


@pytest.mark.parametrize("method, path", [
    ("get", f"/api/tasks/{BIG_ID}"), ("patch", f"/api/tasks/{BIG_ID}"), ("delete", f"/api/tasks/{BIG_ID}"),
    ("post", f"/api/tasks/{BIG_ID}/complete"), ("post", f"/api/v1/tasks/{BIG_ID}/complete"), ("patch", f"/api/lists/{BIG_ID}"),
    ("get", "/api/tasks/9223372036854775808"),
])
def test_ids_beyond_the_database_answer_404(owner, method, path):
    kwargs = {} if method in ("get", "delete") else {"json": {}}
    assert getattr(owner, method)(path, **kwargs).status_code == 404


@pytest.mark.parametrize("body", [{"title": "x", "parentId": 10 ** 30}, {"title": "x", "listId": 10 ** 30}])
def test_big_numbers_in_a_body_answer_400(owner, body):
    assert owner.post("/api/tasks", json=body).status_code == 400


@pytest.mark.parametrize("query", [f"list={BIG_ID}", "list=²", "list=-1", "list=abc", "list=0"])
def test_bad_list_arguments_answer_400(owner, query):
    response = owner.get(f"/api/tasks?view=list&{query}")
    assert response.status_code == 400


def test_deep_json_answers_400(owner):
    response = owner.client.post("/api/import", data="[" * 100_000, content_type="application/json", headers=owner.write_headers)
    assert response.status_code == 400
    assert owner.post("/api/tasks", json=["not", "an", "object"]).status_code == 400


def test_too_large_requests_answer_413(owner):
    response = owner.client.post("/api/import/todoist", data=b"x" * (11 * 1024 * 1024), headers=owner.write_headers)
    assert response.status_code == 413


def test_task_life_cycle(owner):
    created = owner.post("/api/tasks", json={"title": "Trip", "dueDate": "2026-11-13"})
    assert created.status_code == 201
    task_id = created.get_json()["id"]
    child = owner.post("/api/tasks", json={"title": "tickets", "parentId": task_id}).get_json()
    assert [task["id"] for task in owner.get("/api/tasks?view=today").get_json()["tasks"]] == [task_id]
    edited = owner.patch(f"/api/tasks/{task_id}", json={"priority": 1, "notes": "by train"}).get_json()
    assert (edited["priority"], edited["notes"]) == (1, "by train")
    assert owner.post(f"/api/tasks/{child['id']}/complete").get_json()["task"]["completed"] is True
    assert owner.post(f"/api/tasks/{task_id}/complete").get_json()["next"] is None
    assert owner.get("/api/tasks?view=today").get_json()["tasks"] == []
    assert [task["title"] for task in owner.get("/api/tasks?view=completed").get_json()["tasks"]] == ["Trip", "tickets"]
    assert owner.post(f"/api/tasks/{task_id}/uncomplete").get_json()["task"]["completed"] is False
    assert owner.post(f"/api/tasks/{task_id}/trash").get_json()["trashed"] is True
    assert owner.patch(f"/api/tasks/{task_id}", json={"title": "y"}).status_code == 409
    open_task = owner.post("/api/tasks", json={"title": "still open"}).get_json()
    assert owner.delete(f"/api/tasks/{open_task['id']}").status_code == 409
    assert owner.post(f"/api/tasks/{task_id}/restore").get_json()["trashed"] is False
    owner.post(f"/api/tasks/{task_id}/trash")
    assert owner.delete(f"/api/tasks/{task_id}").status_code == 204
    assert owner.get(f"/api/tasks/{task_id}").status_code == 404
    assert owner.get(f"/api/tasks/{child['id']}").status_code == 404


def test_quick_add_and_its_preview(owner):
    preview = owner.get("/api/parse?text=dentist%20fri%209:30%20!1%20%23health").get_json()
    assert (preview["title"], preview["dueDate"], preview["dueTime"], preview["priority"], preview["list"]) == (
        "dentist", "2026-11-20", "09:30", 1, "health")
    assert owner.get("/api/parse?text=tomorrow").status_code == 400
    created = owner.post("/api/tasks/quick", json={"text": "dentist fri 9:30 !1 #health"})
    assert created.status_code == 201
    assert created.get_json()["task"]["list"] == "health"
    lists = owner.get("/api/lists").get_json()
    assert [entry["name"] for entry in lists["lists"]] == ["Inbox", "health"]
    assert lists["counts"]["upcoming"] == 1
    assert owner.post("/api/tasks/quick", json={"text": 5}).status_code == 400


def test_lists_api(owner):
    work = owner.post("/api/lists", json={"name": "Work"})
    assert work.status_code == 201
    work_id = work.get_json()["id"]
    assert owner.post("/api/lists", json={"name": "work"}).status_code == 409
    assert owner.patch(f"/api/lists/{work_id}", json={"name": "Office"}).get_json()["name"] == "Office"
    owner.post("/api/tasks", json={"title": "report", "listId": work_id})
    assert [task["title"] for task in owner.get(f"/api/tasks?view=list&list={work_id}").get_json()["tasks"]] == ["report"]
    assert owner.get("/api/tasks?view=list&list=999").status_code == 404
    assert owner.delete(f"/api/lists/{work_id}").get_json() == {"binned": 1}
    assert owner.delete(f"/api/lists/{INBOX_ID}").status_code == 409
    assert owner.get("/api/tasks?view=someday").status_code == 400


def test_search_view(owner):
    owner.post("/api/tasks", json={"title": "Ring the plumber"})
    found = owner.get("/api/tasks?view=search&q=plumb").get_json()["tasks"]
    assert [task["title"] for task in found] == ["Ring the plumber"]
    assert owner.get("/api/tasks?view=search&q=" + "x" * 201).status_code == 400


def test_bin_empty(owner):
    task_id = owner.post("/api/tasks", json={"title": "x"}).get_json()["id"]
    owner.post(f"/api/tasks/{task_id}/trash")
    assert owner.post("/api/bin/empty").get_json() == {"removed": 1}


def test_export_and_import(owner):
    owner.post("/api/tasks/quick", json={"text": "pay rent every month on the 1st #Home"})
    response = owner.get("/api/export")
    assert response.headers["Content-Disposition"] == "attachment; filename=tasks-2026-11-13.json"
    exported = response.get_json()
    assert exported["tasks"][0]["repeat"] == {"kind": "monthly", "day": 1}
    assert owner.post("/api/import", json=exported).get_json() == {"imported": 1}
    assert owner.post("/api/import", json={"app": "other"}).status_code == 400


def test_todoist_import_endpoint(owner):
    csv_text = "TYPE,CONTENT,DESCRIPTION,PRIORITY,INDENT,AUTHOR,RESPONSIBLE,DATE,DATE_LANG,TIMEZONE\ntask,Milk,,2,1,,,tomorrow,en,\n"
    response = owner.client.post("/api/import/todoist?list=Shopping", data=csv_text.encode(), content_type="text/csv",
                                 headers=owner.write_headers)
    assert response.status_code == 200
    assert response.get_json()["added"] == 1
    bad = owner.client.post("/api/import/todoist", data=b"\xff\xfe", content_type="text/csv", headers=owner.write_headers)
    assert bad.status_code == 400


def test_arty_api_today_add_and_complete(owner, store):
    store.create({"title": "late", "dueDate": "2026-11-12", "priority": 1})
    added = owner.post("/api/v1/tasks", json={"text": "call mum today 5pm"})
    assert added.status_code == 201
    item = added.get_json()["task"]
    assert item == {"id": item["id"], "title": "call mum", "notes": "", "list": "Inbox", "parentTitle": None,
                    "dueDate": "2026-11-13", "dueTime": "17:00", "dueAt": "2026-11-13T17:00:00Z", "priority": 4,
                    "repeatText": None, "overdue": False, "completed": False}
    today = owner.get("/api/v1/today").get_json()
    assert (today["date"], today["timeZone"]) == ("2026-11-13", "Europe/London")
    assert [entry["title"] for entry in today["overdue"]] == ["late"]
    assert [entry["title"] for entry in today["today"]] == ["call mum"]
    done = owner.post(f"/api/v1/tasks/{item['id']}/complete").get_json()
    assert done["task"]["completed"] is True and done["next"] is None
    assert owner.get("/api/v1/today").get_json()["today"] == []


def test_arty_completing_a_repeat_reports_the_next_one(owner):
    item = owner.post("/api/v1/tasks", json={"text": "water plants every day 9pm"}).get_json()["task"]
    done = owner.post(f"/api/v1/tasks/{item['id']}/complete").get_json()
    assert (done["next"]["dueDate"], done["next"]["dueTime"], done["next"]["repeatText"]) == ("2026-11-14", "21:00", "every day")


@pytest.mark.parametrize("body", [{}, {"text": ""}, {"text": "tomorrow"}, {"text": ["x"]}])
def test_arty_add_refuses_text_without_a_task(owner, body):
    assert owner.post("/api/v1/tasks", json=body).status_code == 400


def test_arty_complete_of_a_missing_task_is_404(owner):
    assert owner.post("/api/v1/tasks/999/complete").status_code == 404


@pytest.mark.parametrize("path, body", [
    ("/api/tasks", '{"title": "a\\ud800b"}'),
    ("/api/tasks", '{"title": "ok", "notes": "x\\udfffy"}'),
    ("/api/tasks/quick", '{"text": "a\\ud800b tomorrow"}'),
    ("/api/v1/tasks", '{"text": "a\\ud800b tomorrow"}'),
    ("/api/lists", '{"name": "bad \\udfff"}'),
    ("/api/import", '{"app": "tasks", "version": 1, "tasks": [{"title": "good"}, {"title": "bad \\udfff"}]}'),
])
def test_a_lone_surrogate_answers_400_and_stores_nothing(owner, path, body):
    response = owner.client.post(path, data=body, content_type="application/json", headers=owner.write_headers)
    assert response.status_code == 400
    assert "not valid text" in response.get_json()["error"]
    assert owner.get(f"/api/tasks?view=list&list={INBOX_ID}").get_json()["tasks"] == []


def test_views_and_the_arty_today_say_when_they_are_cut(owner, database):
    with database.connect(immediate=True) as connection:
        connection.executemany("INSERT INTO tasks (list_id, title, due_date, created_at, updated_at) VALUES (1, ?, '2026-11-13', 1, 1)",
                               [(f"t{number}",) for number in range(501)])
    listing = owner.get(f"/api/tasks?view=list&list={INBOX_ID}").get_json()
    assert (len(listing["tasks"]), listing["more"]) == (500, True)
    today = owner.get("/api/v1/today").get_json()
    assert (len(today["today"]), today["more"]) == (500, True)
    assert owner.get("/api/tasks?view=upcoming").get_json()["more"] is False


def test_the_session_names_the_import_limits_and_an_import_can_be_checked_first(owner):
    limits = owner.get("/api/session").get_json()["importLimits"]
    assert limits == {"tasks": 10_000, "lists": 1000, "bytes": 10 * 1024 * 1024}
    payload = {"app": "tasks", "version": 1, "tasks": [{"title": "x"}]}
    assert owner.post("/api/import?check=1", json=payload).get_json() == {"checked": 1}
    assert owner.get(f"/api/tasks?view=list&list={INBOX_ID}").get_json()["tasks"] == []
    assert owner.post("/api/import?check=1", json={**payload, "tasks": [{}]}).status_code == 400
    assert owner.post("/api/import", json=payload).get_json() == {"imported": 1}
