import tasks.__main__ as entry
from tasks.web import MAX_REQUEST_BYTES


def test_waitress_refuses_bodies_over_the_request_cap_before_the_app_sees_them(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in ("AUTH_MODE", "BIND_HOST", "PORT", "TRUSTED_PROXIES", "USER_HEADER", "ALLOWED_USERS", "SECRET_KEY", "LOCAL_USER", "TIME_ZONE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    served = {}
    monkeypatch.setattr(entry, "serve", lambda app, **options: served.update(options))
    assert entry.main() == 0
    assert served["max_request_body_size"] == MAX_REQUEST_BYTES
    assert (served["host"], served["port"]) == ("127.0.0.1", 8100)
