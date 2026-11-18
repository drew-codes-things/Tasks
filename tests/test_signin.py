import http.client
import os
import stat
import threading
from pathlib import Path

import pytest
from waitress.server import create_server

import tasks.__main__ as entry
from tasks.__main__ import build_auth
from tasks.auth import ProxyAuth
from tasks.config import ConfigError, load_settings
from tasks.signin import SESSION_SECONDS, FailureLimiter, OwnerAccount, PasswordAuth
from tasks.web import create_app

SECRET = b"s" * 32
COOKIE = "tasks_session"
OWNER_NAME = "owner"
PASSWORD = "correct horse battery"
HEADER = "X-Forwarded-User"
REPO = Path(__file__).resolve().parent.parent
DEPLOY_ENV = REPO / "deploy" / "tasks.env"
PROTECTED_API = "/api/lists"
WRITE_PATH = "/api/lists"
WRITE_BODY = {"name": "Work"}
WRITE_STATUS = 201
SETTING_NAMES = ("ALLOWED_HOSTS", "AUTH_MODE", "BIND_HOST", "PORT", "TRUSTED_PROXIES", "USER_HEADER", "ALLOWED_USERS", "DATA_DIR",
                 "SECRET_KEY", "LOCAL_USER", "SECURE_COOKIES")


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def password_auth(tmp_path, clock):
    return PasswordAuth(OwnerAccount(tmp_path / "owner.json"), SECRET, COOKIE, clock=clock, limiter=FailureLimiter(clock=clock),
                        device_limiter=FailureLimiter(clock=clock, before_strict=None))


@pytest.fixture
def make_app(store):
    def app_for(auth):
        flask_app = create_app(store, auth, SECRET)
        flask_app.testing = True
        return flask_app
    return app_for


@pytest.fixture
def web(password_auth, make_app):
    return make_app(password_auth).test_client()


def set_up_owner(web, auth, form=None, **kwargs):
    details = {"code": auth.setup_code(), "user": OWNER_NAME, "password": PASSWORD, "confirm": PASSWORD}
    details.update(form or {})
    return web.post("/setup", data=details, **kwargs)


def sign_in(web, password=PASSWORD, **kwargs):
    return web.post("/signin", data={"user": OWNER_NAME, "password": password}, **kwargs)


def forget_the_browser(web) -> None:
    web.delete_cookie(COOKIE)
    web.delete_cookie(COOKIE + "_device", path="/signin")


def session_cookie(response) -> str:
    return next(value for value in response.headers.getlist("Set-Cookie") if value.startswith(COOKIE + "="))


def settings_with(monkeypatch, tmp_path, **values):
    monkeypatch.chdir(tmp_path)
    for name in SETTING_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return load_settings()


def test_the_default_is_the_builtin_sign_in(monkeypatch, tmp_path):
    settings = settings_with(monkeypatch, tmp_path)
    assert settings.auth_mode == "builtin"
    assert settings.trusted_proxies == ()
    assert settings.secure_cookies == "auto"
    assert isinstance(build_auth(settings, SECRET), PasswordAuth)


def test_proxy_mode_needs_its_proxy_named(monkeypatch, tmp_path):
    with pytest.raises(ConfigError, match="TRUSTED_PROXIES"):
        settings_with(monkeypatch, tmp_path, AUTH_MODE="proxy")


def test_a_bad_secure_cookies_value_is_refused(monkeypatch, tmp_path):
    with pytest.raises(ConfigError, match="SECURE_COOKIES"):
        settings_with(monkeypatch, tmp_path, SECURE_COOKIES="maybe")


def test_the_deploy_env_keeps_the_gate(monkeypatch, tmp_path):
    values = dict(line.split("=", 1) for line in DEPLOY_ENV.read_text().splitlines() if "=" in line)
    settings = settings_with(monkeypatch, tmp_path, **values)
    assert settings.auth_mode == "proxy"
    assert settings.trusted_proxies
    assert isinstance(build_auth(settings, SECRET), ProxyAuth)


def test_the_env_example_uses_the_builtin_sign_in(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text((REPO / ".env.example").read_text())
    assert settings_with(monkeypatch, tmp_path).auth_mode == "builtin"


def test_a_signed_out_page_goes_to_setup_then_to_sign_in(web, password_auth):
    assert web.get("/").headers["Location"].endswith("/setup")
    set_up_owner(web, password_auth)
    web.delete_cookie(COOKIE)
    response = web.get("/")
    assert response.status_code == 303
    assert response.headers["Location"].endswith("/signin")


def test_the_api_needs_a_sign_in(web):
    assert web.get(PROTECTED_API).status_code == 401
    assert web.get("/api/health").status_code == 200


def test_the_user_header_is_ignored_without_the_proxy_mode(web, password_auth):
    set_up_owner(web, password_auth)
    web.delete_cookie(COOKIE)
    assert web.get(PROTECTED_API, headers={HEADER: OWNER_NAME}).status_code == 401
    assert web.get("/api/session", headers={HEADER: OWNER_NAME}, environ_base={"REMOTE_ADDR": "::1"}).status_code == 401


def test_setup_needs_the_code_from_the_log(web, password_auth):
    response = set_up_owner(web, password_auth, form={"code": "not-the-code"})
    assert response.status_code == 401
    assert not password_auth.owner.exists()


def test_setup_makes_one_private_owner_and_then_closes(web, password_auth, tmp_path):
    response = set_up_owner(web, password_auth)
    assert response.status_code == 303
    assert web.get("/api/session").get_json()["user"] == OWNER_NAME
    assert stat.S_IMODE(os.stat(tmp_path / "owner.json").st_mode) == 0o600
    second = set_up_owner(web, password_auth, form={"user": "intruder"})
    assert second.headers["Location"].endswith("/signin")
    assert password_auth.owner.read()["user"] == OWNER_NAME
    assert web.get("/setup").headers["Location"].endswith("/signin")


@pytest.mark.parametrize("field, value", [("password", "short"), ("confirm", "something else entirely"), ("user", "has space")])
def test_setup_refuses_weak_or_odd_details(web, password_auth, field, value):
    assert set_up_owner(web, password_auth, form={field: value}).status_code == 400
    assert not password_auth.owner.exists()


def test_the_owner_signs_in_and_can_write(web, password_auth):
    set_up_owner(web, password_auth)
    web.delete_cookie(COOKIE)
    response = sign_in(web)
    assert response.status_code == 303
    assert web.get(response.headers["Location"]).headers["Location"] == "/"
    assert web.get("/").status_code == 200
    token = web.get("/api/session").get_json()["csrfToken"]
    written = web.post(WRITE_PATH, json=WRITE_BODY, headers={"X-CSRF-Token": token})
    assert written.status_code == WRITE_STATUS


def test_a_wrong_password_is_refused_and_then_rate_limited(web, password_auth):
    set_up_owner(web, password_auth)
    forget_the_browser(web)
    for _attempt in range(5):
        assert sign_in(web, password="wrong password!").status_code == 401
    blocked = sign_in(web)
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0
    assert sign_in(web, environ_base={"REMOTE_ADDR": "192.0.2.7"}).status_code == 303


def test_the_lock_lifts_after_the_window(web, password_auth, clock):
    set_up_owner(web, password_auth)
    forget_the_browser(web)
    for _attempt in range(5):
        sign_in(web, password="wrong password!")
    clock.now += 15 * 60 + 1
    assert sign_in(web).status_code == 303


def test_signing_out_ends_the_session_everywhere(web, password_auth, make_app):
    set_up_owner(web, password_auth)
    other_device = make_app(password_auth).test_client()
    sign_in(other_device)
    assert other_device.get("/api/session").status_code == 200
    assert web.post("/signout").status_code == 303
    assert web.get("/api/session").status_code == 401
    assert other_device.get("/api/session").status_code == 401


def test_an_old_or_tampered_session_is_refused(web, password_auth, clock):
    set_up_owner(web, password_auth)
    token = web.get_cookie(COOKIE).value
    payload, signature = token.split(".")
    web.set_cookie(COOKIE, payload + "." + "0" * len(signature))
    assert web.get("/api/session").status_code == 401
    web.set_cookie(COOKIE, token)
    assert web.get("/api/session").status_code == 200
    clock.now += SESSION_SECONDS + 1
    assert web.get("/api/session").status_code == 401


@pytest.mark.parametrize("token", ["\u00e9.\u00e9", "abc.\udcff", "no-dot", "", None])
def test_an_odd_session_value_is_refused_not_crashed(password_auth, token):
    assert password_auth.user_for_token(token) is None


def test_the_session_cookie_is_httponly_lax_and_secure_off_localhost(password_auth, make_app):
    local = set_up_owner(make_app(password_auth).test_client(), password_auth)
    assert "HttpOnly" in session_cookie(local) and "SameSite=Lax" in session_cookie(local)
    assert "Secure" not in session_cookie(local)
    remote = sign_in(make_app(password_auth).test_client(), base_url="https://192.0.2.1")
    assert "Secure" in session_cookie(remote) and "HttpOnly" in session_cookie(remote)
    behind_proxy = sign_in(make_app(password_auth).test_client(), base_url="http://192.0.2.1")
    assert "Secure" in session_cookie(behind_proxy)


@pytest.mark.parametrize("choice, secure", [("true", True), ("false", False)])
def test_secure_cookies_can_be_forced(tmp_path, make_app, choice, secure):
    auth = PasswordAuth(OwnerAccount(tmp_path / "owner.json"), SECRET, COOKIE, secure_cookies=choice)
    response = set_up_owner(make_app(auth).test_client(), auth, base_url="http://192.0.2.1")
    assert ("Secure" in session_cookie(response)) is secure


def test_a_dropped_cookie_is_explained(web, password_auth):
    set_up_owner(web, password_auth)
    web.delete_cookie(COOKIE)
    page = web.get("/signin?done=1")
    assert page.status_code == 200
    assert b"did not keep the sign-in" in page.data


def test_forms_from_another_site_are_refused(web, password_auth):
    foreign = {"Origin": "https://evil.example"}
    assert set_up_owner(web, password_auth, headers=foreign).status_code == 403
    assert not password_auth.owner.exists()
    set_up_owner(web, password_auth)
    assert sign_in(web, headers=foreign).status_code == 403
    assert web.post("/signout", headers=foreign).status_code == 403
    assert web.get("/api/session").status_code == 200


def test_the_gate_header_is_read_only_from_the_named_proxy(make_app):
    gate = ProxyAuth(trusted_proxies=("192.0.2.10",), user_header=HEADER)
    client = make_app(gate).test_client()
    from_proxy = client.get("/api/session", headers={HEADER: "gate-user"}, environ_base={"REMOTE_ADDR": "192.0.2.10"})
    assert from_proxy.get_json()["user"] == "gate-user"
    assert client.get("/api/session", headers={HEADER: "gate-user"}, environ_base={"REMOTE_ADDR": "192.0.2.11"}).status_code == 401
    assert client.get("/api/session", headers={HEADER: "gate-user"}).status_code == 401
    assert client.get("/signin", environ_base={"REMOTE_ADDR": "192.0.2.10"}).status_code == 401


def device_cookie(response) -> str:
    return next(value for value in response.headers.getlist("Set-Cookie") if value.startswith(COOKIE + "_device="))


def test_builtin_mode_takes_the_client_address_from_a_trusted_proxy(monkeypatch, tmp_path, make_app):
    auth = build_auth(settings_with(monkeypatch, tmp_path, TRUSTED_PROXIES="192.0.2.10"), SECRET)
    set_up_owner(make_app(auth).test_client(), auth)
    via_proxy = {"REMOTE_ADDR": "192.0.2.10"}
    attacker = make_app(auth).test_client()
    for _attempt in range(5):
        refused = sign_in(attacker, password="wrong password!", environ_base=via_proxy, headers={"X-Forwarded-For": "203.0.113.9"})
        assert refused.status_code == 401
    assert sign_in(attacker, environ_base=via_proxy, headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 429
    owner = make_app(auth).test_client()
    assert sign_in(owner, environ_base=via_proxy, headers={"X-Forwarded-For": "198.51.100.4"}).status_code == 303


def test_the_forwarded_address_from_anyone_else_is_ignored(monkeypatch, tmp_path, make_app):
    auth = build_auth(settings_with(monkeypatch, tmp_path, TRUSTED_PROXIES="192.0.2.10"), SECRET)
    set_up_owner(make_app(auth).test_client(), auth)
    attacker = make_app(auth).test_client()
    for number in range(5):
        spoofed = {"X-Forwarded-For": f"198.51.100.{number}"}
        sign_in(attacker, password="wrong password!", environ_base={"REMOTE_ADDR": "192.0.2.11"}, headers=spoofed)
    assert sign_in(attacker, environ_base={"REMOTE_ADDR": "192.0.2.11"}).status_code == 429


def test_a_rebinding_host_name_gets_421_and_costs_the_owner_nothing(web, password_auth, make_app):
    set_up_owner(web, password_auth)
    attacker = make_app(password_auth).test_client()
    for _attempt in range(6):
        assert sign_in(attacker, password="wrong password!", base_url="http://rebind.evil.example:8000").status_code == 421
    assert sign_in(attacker).status_code == 303


def test_allowed_hosts_reach_the_own_sign_in(monkeypatch, tmp_path):
    auth = build_auth(settings_with(monkeypatch, tmp_path, ALLOWED_HOSTS="app.example.com"), SECRET)
    assert auth.accepts_host("app.example.com")
    assert not auth.accepts_host("localhost")


def test_the_health_check_answers_on_any_host_when_allowed_hosts_is_set(monkeypatch, tmp_path, make_app):
    auth = build_auth(settings_with(monkeypatch, tmp_path, ALLOWED_HOSTS="app.example.com"), SECRET)
    client = make_app(auth).test_client()
    assert client.get("/api/health", base_url="http://127.0.0.1:8080").status_code == 200
    assert client.get("/", base_url="http://127.0.0.1:8080").status_code == 421
    assert client.get("/signin", base_url="http://127.0.0.1:8080").status_code == 421


def test_a_browser_that_signed_in_before_is_not_locked_out_by_others(web, password_auth, make_app):
    made = set_up_owner(web, password_auth)
    assert "HttpOnly" in device_cookie(made) and "Path=/signin" in device_cookie(made)
    assert web.post("/signout").status_code == 303
    attacker = make_app(password_auth).test_client()
    for _attempt in range(5):
        assert sign_in(attacker, password="wrong password!").status_code == 401
    assert sign_in(attacker).status_code == 429
    assert sign_in(web).status_code == 303


def forwarded_for_seen_behind_waitress(settings) -> str:
    seen = []

    def remember_forwarded_for(environ, start_response):
        seen.append(environ.get("HTTP_X_FORWARDED_FOR", ""))
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b""]

    server = create_server(remember_forwarded_for, host="127.0.0.1", port=0, **entry.waitress_options(settings))
    threading.Thread(target=server.run, daemon=True).start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.effective_port, timeout=10)
        connection.request("GET", "/", headers={"X-Forwarded-For": "203.0.113.9"})
        connection.getresponse().read()
        connection.close()
    finally:
        server.close()
    return seen[0]


@pytest.mark.parametrize("values, kept", [({"TRUSTED_PROXIES": "127.0.0.1"}, "203.0.113.9"), ({}, ""),
                                          ({"AUTH_MODE": "proxy", "TRUSTED_PROXIES": "127.0.0.1", "ALLOWED_USERS": "admin"}, "")])
def test_waitress_hands_x_forwarded_for_on_only_when_the_own_sign_in_trusts_a_proxy(monkeypatch, tmp_path, values, kept):
    assert forwarded_for_seen_behind_waitress(settings_with(monkeypatch, tmp_path, **values)) == kept


def test_main_hands_the_waitress_options_to_serve(monkeypatch, tmp_path):
    settings_with(monkeypatch, tmp_path, TRUSTED_PROXIES="127.0.0.1")
    given = {}
    monkeypatch.setattr(entry, "serve", lambda app, **options: given.update(options))
    assert entry.main() == 0
    assert given["clear_untrusted_proxy_headers"] is False
