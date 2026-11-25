import os
import re
import sqlite3
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
OWN = "tasks"
APPS = {
    "keep-notes": {"host": "keep.example.com", "signin": "keep", "port": 8097},
    "mail-sorter": {"host": "mail.example.com", "signin": "mail", "port": 8101},
    "read-later": {"host": "read.example.com", "signin": "read", "port": 8099},
    "tasks": {"host": "tasks.example.com", "signin": "tasks", "port": 8100},
}
ALL_HOSTS = ["starfire.example", "keep.example.com", "mail.example.com", "read.example.com", "tasks.example.com"]
PATH_CONSTANTS = re.compile(r"^(APP|DATA|CONF|UNIT|SIGNIN|CADDYFILE|STATE)=/", re.MULTILINE)
CT_SIDE = re.compile(r"^  cat <<'CT_SIDE'\n(.*?)^CT_SIDE$", re.MULTILINE | re.DOTALL)
DISPATCH = re.compile(r"^case \"\$\{1:-\}\" in\n.*?^esac\n", re.MULTILINE | re.DOTALL)
STARFIRE = "http://starfire.example {\n\treverse_proxy 127.0.0.1:8090\n}\n"
SIGNED_IN_USER = "admin"
LAPTOP_SITE = {"PROXMOX": "proxmox-host", "CT": "4242", "SITE_HOST": APPS[OWN]["host"],
               "SIGNED_IN_USER": SIGNED_IN_USER}
LAPTOP_STUB = '#!/bin/sh\necho "$(basename "$0") $*" >> "$STUB_LOG"\n'
STUBS = {
    "systemctl": '#!/bin/sh\necho "systemctl $*" >> "$STUB_LOG"\n',
    "caddy": '#!/bin/sh\necho "caddy $*" >> "$STUB_LOG"\nif grep -q BROKEN-DIRECTIVE "$3"; then exit 1; fi\nexit 0\n',
    "runuser": '#!/bin/sh\necho "runuser $*" >> "$STUB_LOG"\nshift 3\nexec "$@"\n',
    "chown": '#!/bin/sh\necho "chown $*" >> "$STUB_LOG"\n',
}


def site_values(name: str) -> dict:
    return {"SITE_HOST": APPS[name]["host"], "SIGNED_IN_USER": SIGNED_IN_USER}


def ct_script_of_this_repo() -> str:
    return CT_SIDE.search((REPO / "deploy.sh").read_text()).group(1)


def as_app(text: str, name: str) -> str:
    own, other = APPS[OWN], APPS[name]
    return (text.replace(f"/srv/signin/{own['signin']}", f"/srv/signin/{other['signin']}").replace(OWN, name))


class Server:
    def __init__(self, root: Path, with_caddy: bool = True):
        self.root = root
        self.stubs = root / "stubs"
        self.stubs.mkdir(parents=True)
        for name, body in STUBS.items():
            if name == "caddy" and not with_caddy:
                continue
            stub = self.stubs / name
            stub.write_text(body)
            stub.chmod(0o755)
        self.log = root / "stub.log"
        self.log.touch()
        self.caddyfile = root / "etc/caddy/Caddyfile"
        self.caddyfile.parent.mkdir(parents=True)
        self.caddyfile.write_text(STARFIRE)
        (root / "root").mkdir()
        self.scripts = {name: self._install(name) for name in APPS}

    def _install(self, name: str) -> Path:
        text = PATH_CONSTANTS.sub(lambda match: f"{match.group(1)}={self.root}/", as_app(ct_script_of_this_repo(), name))
        script = self.root / f"{name}-ct.sh"
        script.write_text(text)
        deploy = self.root / "opt" / name / "deploy"
        deploy.mkdir(parents=True)
        own_block = (REPO / "deploy" / f"Caddyfile.{OWN}").read_text()
        (deploy / f"Caddyfile.{name}").write_text(as_app(own_block, name))
        self.set_port(name, APPS[name]["port"])
        return script

    def set_port(self, name: str, port: int) -> None:
        own_env = (REPO / "deploy" / f"{OWN}.env").read_text()
        env = re.sub(r"^PORT=.*$", f"PORT={port}", as_app(own_env, name), flags=re.MULTILINE)
        conf = self.root / "etc" / name
        conf.mkdir(parents=True, exist_ok=True)
        (conf / f"{name}.env").write_text(env)

    def run(self, name: str, shell: str) -> subprocess.CompletedProcess:
        functions = self.root / f"{name}-functions.sh"
        functions.write_text(DISPATCH.sub("", self.scripts[name].read_text()))
        environment = {**os.environ, **site_values(name), "PATH": f"{self.stubs}:{os.environ['PATH']}",
                       "STUB_LOG": str(self.log)}
        return subprocess.run(["bash", "-c", f"source {functions}\n{shell}"], capture_output=True, text=True,
                              env=environment, cwd=self.root, timeout=60)

    def apply_caddy(self, name: str, stamp: str = "20261113-000001") -> subprocess.CompletedProcess:
        return self.run(name, f"load_port 2> /dev/null || true\nadd_caddy_site {stamp}")

    def undo(self, name: str) -> subprocess.CompletedProcess:
        (self.root / "root" / f"{name}-code-before").write_text("none\n")
        environment = {**os.environ, **site_values(name), "PATH": f"{self.stubs}:{os.environ['PATH']}",
                       "STUB_LOG": str(self.log)}
        return subprocess.run(["bash", str(self.scripts[name]), "undo"], capture_output=True, text=True, env=environment,
                              cwd=self.root, timeout=60)

    def sites(self) -> list[str]:
        return re.findall(r"^http://(\S+) \{$", self.caddyfile.read_text(), flags=re.MULTILINE)


@pytest.fixture
def ct(tmp_path):
    return Server(tmp_path)


def test_undo_of_one_app_removes_only_its_own_block(ct):
    for name in APPS:
        result = ct.apply_caddy(name)
        assert result.returncode == 0, result.stdout + result.stderr
    assert ct.sites() == ALL_HOSTS
    edited = ct.caddyfile.read_text().replace("reverse_proxy 127.0.0.1:8090", "reverse_proxy 127.0.0.1:8091", 1)
    ct.caddyfile.write_text(edited + "\nhttp://later.example {\n\trespond \"added by hand\"\n}\n")
    result = ct.undo(OWN)
    assert result.returncode == 0, result.stdout + result.stderr
    remaining = [host for host in [*ALL_HOSTS, "later.example"] if host != APPS[OWN]["host"]]
    assert ct.sites() == remaining
    text = ct.caddyfile.read_text()
    assert "reverse_proxy 127.0.0.1:8091" in text
    assert "respond \"added by hand\"" in text
    assert "systemctl reload caddy" in ct.log.read_text()


def test_undo_of_tasks_then_keep_leaves_mail_and_read_alone(ct):
    for name in APPS:
        ct.apply_caddy(name)
    for name in ("tasks", "keep-notes"):
        assert ct.undo(name).returncode == 0
    assert ct.sites() == ["starfire.example", "mail.example.com", "read.example.com"]
    assert ct.caddyfile.read_text().startswith(STARFIRE)


def test_apply_then_undo_gives_back_the_same_caddyfile(ct):
    before = ct.caddyfile.read_text()
    ct.apply_caddy(OWN)
    assert ct.undo(OWN).returncode == 0
    assert ct.caddyfile.read_text() == before


def test_the_block_is_marked_and_uses_the_port_from_the_env_file(ct):
    ct.set_port(OWN, 9123)
    assert ct.apply_caddy(OWN).returncode == 0
    text = ct.caddyfile.read_text()
    begin, end = text.index(f"# >>> {OWN} block"), text.index(f"# <<< {OWN} block")
    assert "reverse_proxy 127.0.0.1:9123 {" in text[begin:end]
    assert "__PORT__" not in text


def test_a_block_that_does_not_validate_is_taken_out_again(ct):
    block = ct.root / "opt" / OWN / "deploy" / f"Caddyfile.{OWN}"
    block.write_text(block.read_text() + "\nBROKEN-DIRECTIVE\n")
    before = ct.caddyfile.read_text()
    result = ct.apply_caddy(OWN)
    assert result.returncode != 0
    assert ct.caddyfile.read_text() == before
    assert "reload" not in ct.log.read_text()


def test_an_undo_that_does_not_validate_puts_the_caddyfile_back(ct):
    ct.apply_caddy(OWN)
    applied = ct.caddyfile.read_text() + "BROKEN-DIRECTIVE\n"
    ct.caddyfile.write_text(applied)
    result = ct.undo(OWN)
    assert result.returncode != 0
    assert ct.caddyfile.read_text() == applied
    assert result.stdout.count("put back") == 1


def test_a_site_block_that_was_there_before_apply_is_left_alone(ct):
    hand_made = f"\nhttp://{APPS[OWN]['host']} {{\n\trespond \"hand made\"\n}}\n"
    ct.caddyfile.write_text(STARFIRE + hand_made)
    assert ct.apply_caddy(OWN).returncode == 0
    assert ct.undo(OWN).returncode == 0
    assert ct.caddyfile.read_text() == STARFIRE + hand_made


def test_a_new_port_replaces_the_block_and_undo_brings_the_old_one_back(ct):
    ct.apply_caddy(OWN, "20261113-000001")
    ct.set_port(OWN, 9124)
    assert ct.apply_caddy(OWN, "20261113-000002").returncode == 0
    assert "127.0.0.1:9124" in ct.caddyfile.read_text()
    assert ct.undo(OWN).returncode == 0
    text = ct.caddyfile.read_text()
    assert f"127.0.0.1:{APPS[OWN]['port']}" in text
    assert "127.0.0.1:9124" not in text
    assert text.count(f"# >>> {OWN} block") == 1


def test_without_caddy_on_the_box_the_block_is_added_unvalidated(tmp_path):
    server = Server(tmp_path, with_caddy=False)
    result = server.apply_caddy(OWN)
    assert result.returncode == 0, result.stderr
    assert "not validated" in result.stdout
    assert APPS[OWN]["host"] in server.sites()


def test_only_the_last_three_old_code_copies_are_kept(ct):
    stamps = [f"20261113-00000{number}" for number in range(1, 6)]
    for stamp in stamps:
        (ct.root / "opt" / f"{OWN}-before-{stamp}").mkdir()
    (ct.root / "opt" / f"{OWN}-undone-20261101-000000").mkdir()
    assert ct.run(OWN, "prune_old_copies").returncode == 0
    left = sorted(path.name for path in (ct.root / "opt").iterdir() if path.name.startswith(f"{OWN}-"))
    assert left == [f"{OWN}-before-{stamp}" for stamp in stamps[2:]] + [f"{OWN}-undone-20261101-000000"]


def test_the_database_copy_runs_as_the_service_user(ct):
    data = ct.root / "var" / "lib" / OWN
    data.mkdir(parents=True)
    source = sqlite3.connect(data / f"{OWN}.db")
    source.execute("PRAGMA journal_mode = WAL")
    source.execute("CREATE TABLE kept (value TEXT)")
    source.execute("INSERT INTO kept VALUES ('safe')")
    source.commit()
    source.close()
    target = ct.root / "copy.db"
    result = ct.run(OWN, f"copy_database {target}")
    assert result.returncode == 0, result.stderr
    assert f"runuser -u {OWN} -- python3" in ct.log.read_text()
    copy = sqlite3.connect(target)
    assert copy.execute("SELECT value FROM kept").fetchall() == [("safe",)]
    copy.close()


def test_the_check_reads_the_header_rules_from_the_marked_block(ct):
    ct.apply_caddy(OWN)
    assert ct.run(OWN, "own_block_replaces_the_user_header").returncode == 0
    ct.caddyfile.write_text(ct.caddyfile.read_text().replace("\trequest_header -X-Forwarded-User\n", ""))
    assert ct.run(OWN, "own_block_replaces_the_user_header").returncode != 0


def code_bundle_like_deploy_code(root: Path, port: int) -> Path:
    tree = root / "tree" / "deploy"
    tree.mkdir(parents=True)
    env = (REPO / "deploy" / f"{OWN}.env").read_text()
    (tree / f"{OWN}.env").write_text(re.sub(r"^PORT=.*$", f"PORT={port}", env, flags=re.MULTILINE))
    bundle = root / "code.tgz"
    subprocess.run(["tar", "-czf", str(bundle), "-C", str(root / "tree"), "."], check=True)
    return bundle


def test_a_first_install_takes_the_port_from_the_env_file_in_the_code(ct):
    (ct.root / "etc" / OWN / f"{OWN}.env").unlink()
    bundle = code_bundle_like_deploy_code(ct.root, 9200)
    result = ct.run(OWN, f'load_port_for_apply {bundle}\necho "$PORT $PORT_SOURCE $LOCAL"')
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.split() == ["9200", f"deploy/{OWN}.env", "http://127.0.0.1:9200"]


def test_an_update_takes_the_port_from_the_env_file_on_the_server(ct):
    ct.set_port(OWN, 9201)
    bundle = code_bundle_like_deploy_code(ct.root, 9200)
    result = ct.run(OWN, f'load_port_for_apply {bundle}\necho "$PORT"')
    assert result.stdout.split() == ["9201"]


def test_an_env_file_without_a_port_is_refused(ct):
    (ct.root / "etc" / OWN / f"{OWN}.env").write_text("AUTH_MODE=proxy\n")
    result = ct.run(OWN, "load_port")
    assert result.returncode == 1
    assert "no PORT=<number> line" in result.stdout


def test_the_deploy_script_parses():
    assert subprocess.run(["bash", "-n", str(REPO / "deploy.sh")]).returncode == 0
    ct_side = REPO / "deploy.sh"
    script = CT_SIDE.search(ct_side.read_text()).group(1)
    assert subprocess.run(["bash", "-n"], input=script, text=True).returncode == 0


def test_the_port_is_free_of_the_other_apps_and_named_once():
    env = (REPO / "deploy" / f"{OWN}.env").read_text()
    assert re.findall(r"^PORT=(\d+)$", env, flags=re.MULTILINE) == [str(APPS[OWN]["port"])]
    assert len({app["port"] for app in APPS.values()}) == len(APPS)
    assert "__PORT__" in (REPO / "deploy" / f"Caddyfile.{OWN}").read_text()
    assert str(APPS[OWN]["port"]) not in (REPO / "deploy.sh").read_text()


BOX_STUBS = {
    "systemctl": """#!/bin/sh
echo "systemctl $*" >> "$STUB_LOG"
case "$1" in
  reload) if [ -e "$BOX/reload-fails" ]; then echo "Job for caddy.service failed." >&2; exit 1; fi ;;
  restart|enable) echo active > "$BOX/active" ;;
  disable) echo inactive > "$BOX/active" ;;
  is-active) cat "$BOX/active" 2>/dev/null || echo inactive ;;
  show) cat "$BOX/main-pid" 2>/dev/null || echo 0 ;;
esac
exit 0
""",
    "caddy": STUBS["caddy"],
    "runuser": STUBS["runuser"],
    "chown": STUBS["chown"],
    "id": "#!/bin/sh\nexit 0\n",
    "sleep": "#!/bin/sh\nexit 0\n",
    "journalctl": "#!/bin/sh\nexit 0\n",
    "ss": """#!/bin/sh
if [ -e "$BOX/listener-pid" ]; then
  echo "State Recv-Q Send-Q Local Address:Port Peer Address:Port Process"
  echo "LISTEN 0 1024 127.0.0.1:1 0.0.0.0:* users:((\\"python\\",pid=$(cat "$BOX/listener-pid"),fd=3))"
fi
exit 0
""",
    "install": """#!/bin/sh
args=""
while [ $# -gt 0 ]; do case "$1" in -o|-g) shift ;; *) args="$args $1" ;; esac; shift; done
exec /usr/bin/install $args
""",
    "python3": """#!/bin/sh
if [ "$1" = -m ] && [ "$2" = venv ]; then
  mkdir -p "$3/bin"
  printf '#!/bin/sh\\nif [ -e "$BOX/pip-fails" ]; then echo "ERROR: no matching distribution" >&2; exit 1; fi\\nexit 0\\n' > "$3/bin/pip"
  chmod 755 "$3/bin/pip"
  exit 0
fi
exec /usr/bin/python3 "$@"
""",
    "curl": """#!/bin/sh
format=""; url=""; code=000; body=""; redirect=""
while [ $# -gt 0 ]; do case "$1" in -w) format="$2"; shift ;; -H|-o) shift ;; -s) ;; *) url="$1" ;; esac; shift; done
case "$url" in
  */api/health) if [ ! -e "$BOX/health-down" ]; then code=200; body='{"ok":true}'; fi ;;
  */api/*) code=401 ;;
  */signin/) code=200 ;;
  http://127.0.0.1/) code=303; redirect="http://own.example/signin/" ;;
esac
case "$format" in
  '%{http_code}') printf '%s' "$code" ;;
  '%{http_code} %{redirect_url}') printf '%s %s' "$code" "$redirect" ;;
  *) printf '%s' "$body" ;;
esac
[ "$code" != 000 ]
""",
    "date": """#!/bin/sh
if [ "$1" = "+%Y%m%d-%H%M%S" ]; then
  count=$(( $(cat "$BOX/stamps" 2>/dev/null || echo 0) + 1 ))
  echo "$count" > "$BOX/stamps"
  printf '20261113-%06d\\n' "$count"
  exit 0
fi
exec /usr/bin/date "$@"
""",
}


class Box:
    def __init__(self, root: Path):
        self.root = root
        self.flags = root / "box"
        self.stubs = root / "stubs"
        for folder in (self.flags, self.stubs, root / "root", root / "etc/caddy", root / "opt", root / "srv/signin",
                       root / "var/lib", root / "etc/systemd/system"):
            folder.mkdir(parents=True)
        for name, body in BOX_STUBS.items():
            (self.stubs / name).write_text(body)
            (self.stubs / name).chmod(0o755)
        self.caddyfile = root / "etc/caddy/Caddyfile"
        self.caddyfile.write_text(STARFIRE)
        self.script = root / "ct.sh"
        self.script.write_text(PATH_CONSTANTS.sub(lambda match: f"{match.group(1)}={root}/", ct_script_of_this_repo()))
        backup = root / "root" / f"{OWN}-before-test.tgz"
        backup.write_text("stand-in for a real backup\n")
        (root / "root" / f"{OWN}-last-backup").write_text(f"{backup}\n")
        self.builds = 0

    def flag(self, name: str, value: str | None = "") -> None:
        path = self.flags / name
        if value is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(value)

    def run(self, *arguments: str) -> subprocess.CompletedProcess:
        environment = {**os.environ, **site_values(OWN), "PATH": f"{self.stubs}:{os.environ['PATH']}",
                       "STUB_LOG": str(self.root / "stub.log"),
                       "BOX": str(self.flags)}
        return subprocess.run(["bash", str(self.script), *arguments], capture_output=True, text=True, env=environment,
                              cwd=self.root, timeout=60)

    def apply(self, label: str, signin_text: str = "sign in") -> subprocess.CompletedProcess:
        self.builds += 1
        build = self.root / f"build-{self.builds}"
        tree, signin = build / "tree", build / "signin"
        signin.mkdir(parents=True)
        subprocess.run(["cp", "-r", str(REPO / "deploy"), str(REPO / "requirements.txt"), str(tree.parent)], check=True)
        tree.mkdir()
        (build / "deploy").rename(tree / "deploy")
        (build / "requirements.txt").rename(tree / "requirements.txt")
        (tree / "VERSION-LABEL").write_text(label)
        (signin / "index.html").write_text(signin_text)
        code, page = build / "code.tgz", build / "signin.tgz"
        subprocess.run(["tar", "-czf", str(code), "-C", str(tree), "."], check=True)
        subprocess.run(["tar", "-czf", str(page), "-C", str(signin), "."], check=True)
        return self.run("apply", str(code), str(page))

    def set_port(self, port: int) -> None:
        env = self.root / "etc" / OWN / f"{OWN}.env"
        env.write_text(re.sub(r"^PORT=.*$", f"PORT={port}", env.read_text(), flags=re.MULTILINE))

    def code(self) -> str:
        label = self.root / "opt" / OWN / "VERSION-LABEL"
        return label.read_text() if label.exists() else "none"

    def sites(self) -> list[str]:
        return re.findall(r"^http://(\S+) \{$", self.caddyfile.read_text(), flags=re.MULTILINE)

    def block_ports(self) -> list[str]:
        return re.findall(r"reverse_proxy 127\.0\.0\.1:(\d+) \{\n\t\t\theader_up X-Forwarded-User", self.caddyfile.read_text())

    def service(self) -> str:
        return (self.flags / "active").read_text().strip()

    def signin_page(self) -> str:
        page = self.root / "srv" / "signin" / APPS[OWN]["signin"] / "index.html"
        return page.read_text() if page.exists() else "none"

    def opt(self) -> list[str]:
        return sorted(re.sub(r"\d{8}-\d{6}", "STAMP", path.name) for path in (self.root / "opt").iterdir())


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


def test_an_update_that_fails_at_pip_leaves_nothing_for_undo(box):
    assert box.apply("v1").returncode == 0
    box.flag("pip-fails")
    failed = box.apply("v2")
    assert failed.returncode != 0
    assert box.opt() == [OWN]
    undo = box.run("undo")
    assert undo.returncode == 0, undo.stdout + undo.stderr
    assert "nothing to undo" in undo.stdout
    assert APPS[OWN]["host"] in box.sites()
    assert box.code() == "v1"
    assert box.service() == "active"
    assert (box.root / "etc/systemd/system" / f"{OWN}.service").exists()


def test_undo_after_an_update_that_failed_its_health_check_keeps_the_site(box):
    assert box.apply("v1").returncode == 0
    box.flag("health-down")
    assert box.apply("v2").returncode != 0
    box.flag("health-down", None)
    undo = box.run("undo")
    assert undo.returncode == 0, undo.stdout + undo.stderr
    assert box.code() == "v1"
    assert APPS[OWN]["host"] in box.sites()
    assert box.block_ports() == [str(APPS[OWN]["port"])]


def test_a_failed_caddy_reload_puts_the_caddyfile_back(box):
    box.flag("reload-fails")
    result = box.apply("v1")
    assert result.returncode != 0
    assert "put back" in result.stdout
    assert box.caddyfile.read_text() == STARFIRE
    box.flag("reload-fails", None)
    assert box.run("undo").returncode == 0
    assert box.caddyfile.read_text() == STARFIRE
    assert box.code() == "none"


def test_check_fails_when_the_block_and_the_port_disagree_after_undo(box):
    assert box.apply("v1").returncode == 0
    box.set_port(9131)
    assert box.apply("v2").returncode == 0
    undo = box.run("undo")
    assert undo.returncode == 0, undo.stdout + undo.stderr
    assert f"WARNING: the Caddy block now sends to port {APPS[OWN]['port']} but {OWN} listens on 9131" in undo.stdout
    check = box.run("check")
    assert check.returncode != 0
    assert f"marked Caddy block sends to port {APPS[OWN]['port']}, PORT in" in check.stdout
    assert "check failures: 1" in check.stdout


def test_apply_refuses_a_port_another_program_holds_while_the_app_runs(box):
    assert box.apply("v1").returncode == 0
    box.flag("main-pid", "111\n")
    box.flag("listener-pid", "222")
    box.set_port(9132)
    result = box.apply("v2")
    assert result.returncode != 0
    assert f"refusing: something other than {OWN} listens on port 9132" in result.stdout
    assert box.block_ports() == [str(APPS[OWN]["port"])]
    assert box.code() == "v1"


def test_an_update_on_the_port_the_app_itself_holds_goes_ahead(box):
    assert box.apply("v1").returncode == 0
    box.flag("main-pid", "111\n")
    box.flag("listener-pid", "111")
    result = box.apply("v2")
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.code() == "v2"


def test_a_second_undo_after_a_failed_health_check_changes_nothing(box):
    assert box.apply("v1").returncode == 0
    assert box.apply("v2").returncode == 0
    box.flag("health-down")
    assert box.run("undo").returncode != 0
    assert box.code() == "v1"
    box.flag("health-down", None)
    again = box.run("undo")
    assert again.returncode == 0, again.stdout + again.stderr
    assert "nothing to undo" in again.stdout
    assert box.code() == "v1"
    assert box.opt() == [OWN, f"{OWN}-undone-STAMP"]


def test_undo_puts_the_earlier_signin_page_back(box):
    assert box.apply("v1", signin_text="first page").returncode == 0
    assert box.apply("v2", signin_text="second page").returncode == 0
    assert box.signin_page() == "second page"
    assert box.run("undo").returncode == 0
    assert box.signin_page() == "first page"


def test_repeated_applies_and_undos_keep_three_copies_of_each_kind(box):
    for number in range(6):
        assert box.apply(f"v{number}").returncode == 0
        assert box.run("undo").returncode == 0
    opt = box.opt()
    assert opt.count(f"{OWN}-before-STAMP") <= 3
    assert opt.count(f"{OWN}-undone-STAMP") == 3
    signin = [path.name for path in (box.root / "srv/signin").iterdir()]
    assert len([name for name in signin if "-before-" in name]) <= 3
    assert len([name for name in signin if "-undone-" in name]) == 3
    backups = [path.name for path in (box.root / "etc/caddy").iterdir() if path.name.startswith("Caddyfile.bak-")]
    assert len([name for name in backups if "-undo-" not in name]) == 3
    assert len([name for name in backups if "-undo-" in name]) == 3


def test_the_service_stops_restarting_after_a_configuration_mistake():
    unit = (REPO / "deploy" / f"{OWN}.service").read_text()
    assert re.findall(r"^RestartPreventExitStatus=(\d+)$", unit, flags=re.MULTILINE) == ["2"]


def test_the_block_and_the_first_env_file_get_the_site_values(ct):
    assert ct.apply_caddy(OWN).returncode == 0
    text = ct.caddyfile.read_text()
    assert f"http://{APPS[OWN]['host']} {{\n" in text
    assert f"header_up X-Forwarded-User {SIGNED_IN_USER}\n" in text
    env_file = REPO / "deploy" / f"{OWN}.env"
    assert re.search(r"^ALLOWED_USERS=__SIGNED_IN_USER__$", env_file.read_text(), flags=re.MULTILINE)
    env = ct.run(OWN, f"fill_in_site_values {env_file}").stdout
    assert re.search(rf"^ALLOWED_USERS={SIGNED_IN_USER}$", env, flags=re.MULTILINE)
    assert "__" not in env


def run_laptop_side(tmp_path, site_text):
    stubs = tmp_path / "laptop-stubs"
    stubs.mkdir()
    for name in ("ssh", "scp"):
        (stubs / name).write_text(LAPTOP_STUB)
        (stubs / name).chmod(0o755)
    log = tmp_path / "laptop.log"
    log.touch()
    site = tmp_path / "site.env"
    if site_text is not None:
        site.write_text(site_text)
    environment = {name: value for name, value in os.environ.items() if name not in LAPTOP_SITE}
    environment.update(PATH=f"{stubs}:{os.environ['PATH']}", STUB_LOG=str(log), SITE_ENV=str(site))
    result = subprocess.run(["bash", str(REPO / "deploy.sh"), "check"], capture_output=True, text=True,
                            env=environment, timeout=60)
    return result, log.read_text()


def test_the_laptop_side_refuses_without_the_site_file(tmp_path):
    result, calls = run_laptop_side(tmp_path, None)
    assert result.returncode == 1
    assert "deploy/site.env.example" in result.stdout
    assert calls == ""


def test_the_laptop_side_passes_the_site_values_to_the_container(tmp_path):
    site_text = "".join(f"{name}={value}\n" for name, value in LAPTOP_SITE.items())
    result, calls = run_laptop_side(tmp_path, site_text)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (f"ssh -o BatchMode=yes proxmox-host pct exec 4242 -- env SITE_HOST={APPS[OWN]['host']} "
            f"SIGNED_IN_USER={SIGNED_IN_USER} bash /root/{OWN}-deploy-ct.sh check") in calls


@pytest.mark.parametrize("name, value", [("SITE_HOST", "own.example.com;reboot"), ("CT", ""),
                                         ("SIGNED_IN_USER", "admin user")])
def test_the_laptop_side_refuses_a_site_value_that_is_not_plain(tmp_path, name, value):
    site_text = "".join(f"{key}={value if key == name else default}\n" for key, default in LAPTOP_SITE.items())
    result, calls = run_laptop_side(tmp_path, site_text)
    assert result.returncode == 1
    assert f"{name} in" in result.stdout
    assert calls == ""


def test_the_caddy_refusal_names_starfire_tasks():
    block = (REPO / "deploy" / "Caddyfile.tasks").read_text()
    assert 'respond "Starfire Tasks is only for the admin account, so this account cannot open it. Sign in with the admin account instead, or ask the admin for what you need." 403' in block


def test_the_sign_in_page_is_built_as_starfire_tasks():
    assert "--first Starfire --second Tasks --tagline" in (REPO / "deploy.sh").read_text()


def test_the_caddy_refusal_says_who_the_app_is_for_and_what_to_do_instead():
    refusal = re.search(r'respond "([^"]+)" 403', next((REPO / "deploy").glob("Caddyfile.*")).read_text()).group(1)
    assert refusal.startswith("Starfire Tasks is only for the admin account")
    assert "Sign in with the admin account instead" in refusal
    assert "ask the admin" in refusal
    assert len(refusal) < 200
