import hashlib
import hmac
import ipaddress
import os
import secrets
from pathlib import Path

MAX_USER_LENGTH = 254
SECRET_FILE_NAME = "secret_key"
SECRET_BYTES = 32
LOOPBACK_HOST_NAMES = ("localhost", "127.0.0.1", "[::1]")
DEFAULT_HTTP_PORT = 80
CSRF_PURPOSE = b"tasks csrf\x00"


class SecretKeyError(ValueError):
    pass


def _is_printable_name(value: str) -> bool:
    return value.isprintable() and 0 < len(value) <= MAX_USER_LENGTH


class ProxyAuth:
    def __init__(self, trusted_proxies, user_header: str, allowed_users=()):
        self.networks = tuple(ipaddress.ip_network(entry, strict=False) for entry in trusted_proxies)
        self.user_header = user_header
        self.allowed_users = frozenset(allowed_users)

    def _is_trusted(self, remote_address: str) -> bool:
        try:
            address = ipaddress.ip_address(remote_address)
        except ValueError:
            return False
        return any(address in network for network in self.networks)

    def accepts_host(self, host: str) -> bool:
        return True

    def user_for(self, remote_address: str, headers) -> str | None:
        if not self._is_trusted(remote_address or ""):
            return None
        user = (headers.get(self.user_header) or "").strip()
        if not _is_printable_name(user):
            return None
        if self.allowed_users and user not in self.allowed_users:
            return None
        return user


def _loopback_hosts(port: int) -> frozenset:
    hosts = {f"{name}:{port}" for name in LOOPBACK_HOST_NAMES}
    if port == DEFAULT_HTTP_PORT:
        hosts.update(LOOPBACK_HOST_NAMES)
    return frozenset(hosts)


class LocalAuth:
    def __init__(self, local_user: str, port: int):
        self.local_user = local_user
        self.allowed_hosts = _loopback_hosts(port)

    def accepts_host(self, host: str) -> bool:
        return (host or "").lower() in self.allowed_hosts

    def user_for(self, remote_address: str, headers) -> str:
        return self.local_user


def csrf_token(secret: bytes, user: str) -> str:
    return hmac.new(secret, CSRF_PURPOSE + user.encode(), hashlib.sha256).hexdigest()


def csrf_matches(secret: bytes, user: str, presented) -> bool:
    if not isinstance(presented, str) or not presented:
        return False
    return hmac.compare_digest(csrf_token(secret, user), presented)


def _usable_secret(path: Path) -> bytes:
    secret = path.read_bytes()
    if len(secret) < SECRET_BYTES:
        raise SecretKeyError(f"{path} holds {len(secret)} bytes, fewer than the {SECRET_BYTES} a key needs; "
                             "stop the app, delete the file and start it again to make a new key")
    return secret


def load_or_create_secret(data_dir: Path, configured: str) -> bytes:
    if configured:
        if len(configured.encode()) < SECRET_BYTES:
            raise SecretKeyError(f"SECRET_KEY holds {len(configured.encode())} bytes, fewer than the {SECRET_BYTES} a key needs; "
                                 "set a longer one or leave it empty so a key file is made")
        return configured.encode()
    path = Path(data_dir) / SECRET_FILE_NAME
    if path.exists():
        return _usable_secret(path)
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    secret = secrets.token_hex(SECRET_BYTES).encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(secret)
    return secret
