from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

LONDON = ZoneInfo("Europe/London")
START_TIME = datetime(2026, 11, 13, 10, 0, tzinfo=timezone.utc).timestamp()
OWNER = "demo@example.com"
SECRET = b"k" * 32


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp()


def london(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=LONDON)


class FakeClock:
    def __init__(self, start: float = START_TIME):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def set(self, moment: float) -> None:
        self.now = moment


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def database(tmp_path):
    from tasks.db import Database
    return Database(tmp_path / "tasks.db")


@pytest.fixture
def store(database, clock):
    from tasks.store import TaskStore
    return TaskStore(database, clock=clock, zone=LONDON)


@pytest.fixture
def app(store):
    from tasks.auth import ProxyAuth
    from tasks.web import create_app
    auth = ProxyAuth(trusted_proxies=("127.0.0.1", "::1"), user_header="X-Forwarded-User")
    flask_app = create_app(store, auth, SECRET)
    flask_app.testing = True
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


class SignedInClient:
    def __init__(self, client):
        self.client = client
        self.headers = {"X-Forwarded-User": OWNER}
        session = client.get("/api/session", headers=self.headers).get_json()
        self.write_headers = {**self.headers, "X-CSRF-Token": session["csrfToken"]}

    def get(self, path, **kwargs):
        return self.client.get(path, headers=self.headers, **kwargs)

    def post(self, path, json=None, **kwargs):
        return self.client.post(path, json=json, headers={**self.write_headers, **kwargs.pop("headers", {})}, **kwargs)

    def patch(self, path, json=None, **kwargs):
        return self.client.patch(path, json=json, headers=self.write_headers, **kwargs)

    def delete(self, path, **kwargs):
        return self.client.delete(path, headers=self.write_headers, **kwargs)


@pytest.fixture
def owner(client):
    return SignedInClient(client)
