import os
import stat

import pytest

from tasks.auth import LocalAuth, ProxyAuth, SecretKeyError, csrf_matches, csrf_token, load_or_create_secret
from tasks.config import ConfigError, load_settings

HEADER = "X-Forwarded-User"


def proxy_auth(**overrides):
    options = {"trusted_proxies": ("127.0.0.1", "::1"), "user_header": HEADER}
    options.update(overrides)
    return ProxyAuth(**options)


def test_user_header_from_the_proxy_is_trusted():
    assert proxy_auth().user_for("127.0.0.1", {HEADER: "demo-user"}) == "demo-user"
    assert proxy_auth().user_for("::1", {HEADER: "demo-user"}) == "demo-user"


def test_user_header_from_anyone_else_is_ignored():
    assert proxy_auth().user_for("172.16.1.50", {HEADER: "demo-user"}) is None
    assert proxy_auth().user_for("", {HEADER: "demo-user"}) is None
    assert proxy_auth().user_for("not-an-address", {HEADER: "demo-user"}) is None


def test_trusted_proxies_can_be_networks():
    auth = proxy_auth(trusted_proxies=("10.0.0.0/24",))
    assert auth.user_for("10.0.0.7", {HEADER: "demo-user"}) == "demo-user"
    assert auth.user_for("10.0.1.7", {HEADER: "demo-user"}) is None


@pytest.mark.parametrize("value", [None, "", "   ", "x" * 255, "demo-user\nadmin"])
def test_missing_or_odd_user_header_is_not_a_user(value):
    headers = {} if value is None else {HEADER: value}
    assert proxy_auth().user_for("127.0.0.1", headers) is None


def test_allowed_users_limit_who_gets_in():
    auth = proxy_auth(allowed_users=("demo-user",))
    assert auth.user_for("127.0.0.1", {HEADER: "demo-user"}) == "demo-user"
    assert auth.user_for("127.0.0.1", {HEADER: "someone"}) is None


def test_a_bad_trusted_proxy_entry_is_refused():
    with pytest.raises(ValueError):
        proxy_auth(trusted_proxies=("not-an-address",))


def test_local_auth_is_always_the_local_user():
    assert LocalAuth("me", port=8000).user_for("127.0.0.1", {}) == "me"


def test_csrf_token_matches_only_its_user_and_secret():
    token = csrf_token(b"s" * 32, "demo-user")
    assert csrf_matches(b"s" * 32, "demo-user", token)
    assert not csrf_matches(b"s" * 32, "other", token)
    assert not csrf_matches(b"t" * 32, "demo-user", token)
    assert not csrf_matches(b"s" * 32, "demo-user", "")
    assert not csrf_matches(b"s" * 32, "demo-user", None)


def test_secret_is_made_once_private_and_reused(tmp_path):
    first = load_or_create_secret(tmp_path, "")
    second = load_or_create_secret(tmp_path, "")
    assert first == second
    assert len(first) >= 32
    mode = stat.S_IMODE(os.stat(tmp_path / "secret_key").st_mode)
    assert mode == 0o600


def test_configured_secret_wins(tmp_path):
    configured = "configured-secret-" + "x" * 15
    assert load_or_create_secret(tmp_path, configured) == configured.encode()
    assert not (tmp_path / "secret_key").exists()


@pytest.mark.parametrize("configured", ["x", "configured-secret", "y" * 31])
def test_a_configured_secret_shorter_than_32_bytes_is_refused(tmp_path, configured):
    with pytest.raises(SecretKeyError) as caught:
        load_or_create_secret(tmp_path, configured)
    assert "SECRET_KEY" in str(caught.value)
    assert not (tmp_path / "secret_key").exists()


def settings_with(monkeypatch, tmp_path, **values):
    monkeypatch.chdir(tmp_path)
    for name in ("AUTH_MODE", "BIND_HOST", "PORT", "TRUSTED_PROXIES", "USER_HEADER", "ALLOWED_USERS", "DATA_DIR", "SECRET_KEY", "LOCAL_USER", "TIME_ZONE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return load_settings()


def test_default_settings(monkeypatch, tmp_path):
    settings = settings_with(monkeypatch, tmp_path)
    assert settings.auth_mode == "builtin"
    assert settings.bind_host == "127.0.0.1"
    assert settings.port == 8100
    assert settings.trusted_proxies == ()
    assert settings.user_header == "X-Forwarded-User"
    assert settings.allowed_users == ()


def test_lists_are_split_on_commas(monkeypatch, tmp_path):
    settings = settings_with(monkeypatch, tmp_path, TRUSTED_PROXIES="127.0.0.1, 10.0.0.0/8", ALLOWED_USERS="a@example.com,b@example.com")
    assert settings.trusted_proxies == ("127.0.0.1", "10.0.0.0/8")
    assert settings.allowed_users == ("a@example.com", "b@example.com")


def test_local_mode_refuses_to_listen_beyond_this_machine(monkeypatch, tmp_path):
    with pytest.raises(ConfigError):
        settings_with(monkeypatch, tmp_path, AUTH_MODE="local", BIND_HOST="0.0.0.0")


@pytest.mark.parametrize("name, value", [("AUTH_MODE", "none"), ("PORT", "abc"), ("PORT", "0"), ("TRUSTED_PROXIES", "not-an-address")])
def test_bad_settings_are_refused(monkeypatch, tmp_path, name, value):
    with pytest.raises(ConfigError):
        settings_with(monkeypatch, tmp_path, **{name: value})


@pytest.mark.parametrize("value, reason", [
    ("0.0.0.0/0", "public"), ("::/0", "public"), ("127.0.0.1,0.0.0.0/1", "public"), ("8.8.0.0/16", "public"),
    ("172.16.1.16/24", "host bits"), ("127.0.0.1,::1,172.16.1.16/24", "host bits"),
])
def test_trusted_proxies_that_reach_too_far_are_refused(monkeypatch, tmp_path, value, reason):
    with pytest.raises(ConfigError) as caught:
        settings_with(monkeypatch, tmp_path, TRUSTED_PROXIES=value)
    assert reason in str(caught.value)


@pytest.mark.parametrize("value", ["127.0.0.1,::1", "172.16.1.16", "172.16.1.0/24", "10.0.0.0/8", "203.0.113.7", "fd00::/8"])
def test_trusted_proxies_of_one_address_or_a_private_network_are_accepted(monkeypatch, tmp_path, value):
    assert settings_with(monkeypatch, tmp_path, TRUSTED_PROXIES=value).trusted_proxies == tuple(value.split(","))


def test_env_file_is_read(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("PORT=9001\n# a comment line in the env file\n")
    monkeypatch.delenv("PORT", raising=False)
    assert settings_with(monkeypatch, tmp_path).port == 9001


@pytest.mark.parametrize("host", ["localhost:8000", "LOCALHOST:8000", "127.0.0.1:8000", "[::1]:8000"])
def test_local_auth_accepts_only_loopback_names_on_its_port(host):
    assert LocalAuth("me", port=8000).accepts_host(host)


@pytest.mark.parametrize("host", ["evil.example:8000", "localhost", "localhost:8001", "127.0.0.2:8000", "localhost.evil.example:8000",
                                  "[::1]", "", "127.0.0.1:8000@evil.example"])
def test_local_auth_refuses_other_hosts(host):
    assert not LocalAuth("me", port=8000).accepts_host(host)


def test_local_auth_on_port_80_also_accepts_the_bare_names():
    assert LocalAuth("me", port=80).accepts_host("localhost")
    assert LocalAuth("me", port=80).accepts_host("127.0.0.1:80")


def test_proxy_auth_leaves_the_host_to_the_proxy():
    assert proxy_auth().accepts_host("tasks.example.test")


@pytest.mark.parametrize("content", [b"", b"short"])
def test_an_empty_or_short_secret_file_is_refused(tmp_path, content):
    (tmp_path / "secret_key").write_bytes(content)
    with pytest.raises(SecretKeyError) as caught:
        load_or_create_secret(tmp_path, "")
    assert "secret_key" in str(caught.value)


def test_dates_follow_london_unless_told_otherwise(monkeypatch, tmp_path):
    assert settings_with(monkeypatch, tmp_path).time_zone == "Europe/London"
    assert settings_with(monkeypatch, tmp_path, TIME_ZONE="Europe/Paris").time_zone == "Europe/Paris"


@pytest.mark.parametrize("zone", ["Mars/Olympus", "../etc/passwd", ""])
def test_an_unknown_time_zone_is_refused(monkeypatch, tmp_path, zone):
    with pytest.raises(ConfigError):
        settings_with(monkeypatch, tmp_path, TIME_ZONE=zone)
