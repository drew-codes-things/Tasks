import json
import logging
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, Response, abort, g, jsonify, redirect, request, send_from_directory
from werkzeug.exceptions import HTTPException

from . import __version__
from .auth import csrf_matches, csrf_token
from .errors import TaskError
from .signin import SIGN_IN_PATHS, PasswordAuth, add_sign_in_pages
from .store import LARGEST_DATABASE_INTEGER, TaskStore
from .transfer import MAX_IMPORT_LISTS, MAX_IMPORT_TASKS, export_tasks, import_todoist_csv, import_tasks

STATIC_DIR = Path(__file__).resolve().parent / "static"
APP_NAME = "Tasks"
MAX_REQUEST_BYTES = 10 * 1024 * 1024
MAX_ID_DIGITS = 19
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
PUBLIC_PATHS = ("/api/health",)
SIGNED_OUT_TEXT = "Sign in through the main sign-in page first."
NESTED_TOO_DEEPLY_TEXT = "that JSON is nested too deeply"
WRONG_HOST_TEXT = ("this app does not answer on that host name (AUTH_MODE=local answers on localhost, 127.0.0.1 or "
                   "[::1] at its own port; the own sign-in answers on the names in ALLOWED_HOSTS, or with it empty on "
                   "localhost, IP addresses and this machine's name)")
MISDIRECTED_REQUEST = 421
V1_FIELDS = ("id", "title", "notes", "list", "parentTitle", "dueDate", "dueTime", "dueAt", "priority", "repeatText", "overdue", "completed")
CONTENT_SECURITY_POLICY = "; ".join((
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "font-src 'self'",
    "img-src 'self' data:",
    "connect-src 'self'",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "form-action 'self'",
))

log = logging.getLogger("tasks.web")


def _error(message: str, status: int):
    return jsonify({"error": message}), status


def _body() -> dict:
    try:
        data = request.get_json(silent=True)
    except RecursionError as error:
        raise TaskError(NESTED_TOO_DEEPLY_TEXT) from error
    if not isinstance(data, dict):
        raise TaskError("send a JSON object")
    return data


def _id_arg(name: str) -> int | None:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return None
    if not (raw.isascii() and raw.isdigit() and len(raw) <= MAX_ID_DIGITS) or not 1 <= int(raw) <= LARGEST_DATABASE_INTEGER:
        raise TaskError(f"{name} must be a list id", status=400)
    return int(raw)


def _is_same_origin() -> bool:
    origin = request.headers.get("Origin")
    if origin is None:
        return True
    return urlsplit(origin).netloc == request.host


def _v1_item(task: dict | None) -> dict | None:
    if task is None:
        return None
    return {name: task[name] for name in V1_FIELDS}


def _attachment(body: str, mimetype: str, file_name: str) -> Response:
    response = Response(body, mimetype=mimetype)
    response.headers["Content-Disposition"] = f"attachment; filename={file_name}"
    return response


def create_app(store: TaskStore, auth, secret: bytes) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
    has_own_sign_in = isinstance(auth, PasswordAuth)
    if has_own_sign_in:
        add_sign_in_pages(app, auth, APP_NAME)

    @app.url_value_preprocessor
    def refuse_ids_beyond_the_database(endpoint, values):
        if any(isinstance(value, int) and value > LARGEST_DATABASE_INTEGER for value in (values or {}).values()):
            abort(404)

    @app.before_request
    def identify_and_guard_writes():
        if request.path in PUBLIC_PATHS:
            return None
        if not auth.accepts_host(request.host):
            return _error(WRONG_HOST_TEXT, MISDIRECTED_REQUEST)
        if has_own_sign_in and request.path in SIGN_IN_PATHS:
            return None
        user = auth.user_for(request.remote_addr or "", request.headers)
        if user is None:
            if request.path.startswith("/api/"):
                return _error("sign in first", 401)
            if has_own_sign_in:
                return redirect(auth.entry_path(), code=303)
            return Response(SIGNED_OUT_TEXT, status=401, mimetype="text/plain")
        g.user = user
        if request.method in SAFE_METHODS:
            return None
        if not _is_same_origin():
            return _error("this request came from another site", 403)
        if not csrf_matches(secret, user, request.headers.get(CSRF_HEADER)):
            return _error("missing or wrong CSRF token, reload the page", 403)
        return None

    @app.after_request
    def add_security_headers(response: Response) -> Response:
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(TaskError)
    def task_refused(error: TaskError):
        return _error(str(error), error.status)

    @app.errorhandler(HTTPException)
    def http_error(error: HTTPException):
        if error.code == 413:
            return _error("that request is too large", 413)
        if request.path.startswith("/api/"):
            return _error(error.description or error.name, error.code or 500)
        return error

    @app.get("/")
    def index():
        response = send_from_directory(STATIC_DIR, "index.html", max_age=0)
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/static/<path:name>")
    def static_file(name: str):
        return send_from_directory(STATIC_DIR, name, max_age=0)

    @app.get("/api/health")
    def health():
        if not store.database.is_reachable():
            log.error("health check: the database could not be read at %s", store.database.path)
            return jsonify({"ok": False, "error": "database unavailable"}), 503
        return jsonify({"ok": True})

    @app.get("/api/session")
    def session_state():
        return jsonify({"user": g.user, "csrfToken": csrf_token(secret, g.user), "version": __version__,
                        "timeZone": store.zone.key, "today": store.today().isoformat(),
                        "importLimits": {"tasks": MAX_IMPORT_TASKS, "lists": MAX_IMPORT_LISTS, "bytes": MAX_REQUEST_BYTES}})

    @app.get("/api/lists")
    def list_lists():
        return jsonify({"lists": store.lists(), "counts": store.counts(), "today": store.today().isoformat()})

    @app.post("/api/lists")
    def create_list():
        return jsonify(store.create_list(_body().get("name"))), 201

    @app.patch("/api/lists/<int:list_id>")
    def rename_list(list_id: int):
        return jsonify(store.rename_list(list_id, _body().get("name")))

    @app.delete("/api/lists/<int:list_id>")
    def delete_list(list_id: int):
        return jsonify({"binned": store.delete_list(list_id)})

    @app.get("/api/tasks")
    def list_tasks():
        listing = store.list_view(request.args.get("view", "today"), _id_arg("list"), request.args.get("q", ""))
        return jsonify({**listing, "today": store.today().isoformat()})

    @app.post("/api/tasks")
    def create_task():
        return jsonify(store.create(_body())), 201

    @app.post("/api/tasks/quick")
    def quick_add_task():
        data = _body()
        task, understood = store.quick_add(data.get("text"), data.get("listId"), data.get("parentId"))
        return jsonify({"task": task, "understood": understood}), 201

    @app.get("/api/parse")
    def preview_quick_add():
        return jsonify(store.preview_quick_add(request.args.get("text", "")))

    @app.get("/api/tasks/<int:task_id>")
    def read_task(task_id: int):
        return jsonify(store.get(task_id))

    @app.patch("/api/tasks/<int:task_id>")
    def update_task(task_id: int):
        return jsonify(store.update(task_id, _body()))

    @app.delete("/api/tasks/<int:task_id>")
    def delete_task(task_id: int):
        store.delete_forever(task_id)
        return "", 204

    @app.post("/api/tasks/<int:task_id>/complete")
    def complete_task(task_id: int):
        return jsonify(store.complete(task_id))

    @app.post("/api/tasks/<int:task_id>/uncomplete")
    def uncomplete_task(task_id: int):
        return jsonify(store.uncomplete(task_id))

    @app.post("/api/tasks/<int:task_id>/trash")
    def trash_task(task_id: int):
        return jsonify(store.trash(task_id))

    @app.post("/api/tasks/<int:task_id>/restore")
    def restore_task(task_id: int):
        return jsonify(store.restore(task_id))

    @app.post("/api/bin/empty")
    def empty_bin():
        return jsonify({"removed": store.empty_bin()})

    @app.get("/api/export")
    def export_download():
        body = json.dumps(export_tasks(store), ensure_ascii=False, indent=2)
        return _attachment(body, "application/json", f"tasks-{store.today().isoformat()}.json")

    @app.post("/api/import")
    def import_upload():
        if request.args.get("check") == "1":
            return jsonify({"checked": import_tasks(store, _body(), check_only=True)})
        return jsonify({"imported": import_tasks(store, _body())})

    @app.post("/api/import/todoist")
    def import_todoist():
        return jsonify(import_todoist_csv(store, request.get_data(cache=False), request.args.get("list")))

    @app.get("/api/v1/today")
    def api_today():
        today = store.today().isoformat()
        listing = store.list_view("today")
        tasks = listing["tasks"]
        return jsonify({
            "date": today,
            "timeZone": store.zone.key,
            "overdue": [_v1_item(task) for task in tasks if task["dueDate"] < today],
            "today": [_v1_item(task) for task in tasks if task["dueDate"] >= today],
            "more": listing["more"],
        })

    @app.post("/api/v1/tasks")
    def api_add():
        task, understood = store.quick_add(_body().get("text"))
        return jsonify({"task": _v1_item(task), "understood": understood}), 201

    @app.post("/api/v1/tasks/<int:task_id>/complete")
    def api_complete(task_id: int):
        result = store.complete(task_id)
        return jsonify({"task": _v1_item(result["task"]), "next": _v1_item(result["next"])})

    return app
