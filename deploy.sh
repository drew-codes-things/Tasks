#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SITE_ENV="${SITE_ENV:-$REPO/deploy/site.env}"
SITE_NAMES=(PROXMOX CT SITE_HOST SIGNED_IN_USER)
SITE_VALUE_PATTERN='^[A-Za-z0-9][A-Za-z0-9._:-]*$'
SITE_SIGNIN="${SITE_SIGNIN:-$HOME/Projects/site-signin}"
CT_SCRIPT=/root/tasks-deploy-ct.sh

usage() {
  cat <<'USAGE'
usage: deploy.sh backup | apply [commit] | check | undo

Runs from the laptop and reaches the container through ssh to the Proxmox host
and `pct`. The host, the container, the site name and the signed-in user come
from deploy/site.env (copy deploy/site.env.example).
UNTESTED: written without access to the live server.

  backup          copy the Caddyfile, /opt/tasks, /etc/tasks, the unit,
                  /srv/signin/tasks and a consistent copy of the database into
                  /root/tasks-before-<stamp>.tgz (mode 600) in the container
  apply [commit]  install or update Tasks from the commit (default HEAD):
                  service user, venv, /var/lib/tasks, env file (kept if present),
                  systemd unit, sign-in page, the marked Caddy site block (added once,
                  rewritten when PORT changed), then check; keeps the last 3 old code copies
  check           service, health, sign-in redirects through Caddy, the header rules in
                  the marked Caddy block, file modes, errors
  undo            put back what the last apply changed: the earlier code and sign-in
                  page (or stop and disable the service after a first install) and only the tasks
                  block in the Caddyfile (other sites and later edits stay);
                  tasks in /var/lib/tasks are never touched

The port comes from PORT= in /etc/tasks/tasks.env in the container (from
deploy/tasks.env on a first install); the Caddy block is filled in from it.
USAGE
}

site_value() {
  sed -n "s/^$1=//p" "$SITE_ENV" | tail -n 1
}

load_site() {
  local name value
  if [ ! -f "$SITE_ENV" ]; then
    echo "refusing: $SITE_ENV is missing; copy deploy/site.env.example to deploy/site.env and fill it in"
    exit 1
  fi
  for name in "${SITE_NAMES[@]}"; do
    value="${!name:-$(site_value "$name")}"
    if [[ ! "$value" =~ $SITE_VALUE_PATTERN ]]; then
      echo "refusing: $name in $SITE_ENV must be a plain host name, address, number or user name, got '$value'"
      exit 1
    fi
    printf -v "$name" '%s' "$value"
  done
}

write_ct_script() {
  cat <<'CT_SIDE'
#!/usr/bin/env bash
set -euo pipefail
APP=/opt/tasks
DATA=/var/lib/tasks
CONF=/etc/tasks
ENV_FILE="$CONF/tasks.env"
UNIT=/etc/systemd/system/tasks.service
SIGNIN=/srv/signin/tasks
CADDYFILE=/etc/caddy/Caddyfile
STATE=/root
BACKUP_POINTER="$STATE/tasks-last-backup"
BEFORE_POINTER="$STATE/tasks-code-before"
CADDY_CHANGE="$STATE/tasks-caddy-change"
CADDY_BLOCK_BEFORE="$STATE/tasks-caddy-block-before"
SIGNIN_BEFORE="$STATE/tasks-signin-before"
CADDY_BLOCK_BEGIN="# >>> tasks block, added and removed by the tasks deploy.sh"
CADDY_BLOCK_END="# <<< tasks block"
CADDY_BLOCK_SOURCE="$APP/deploy/Caddyfile.tasks"
SITE_HOST="${SITE_HOST:?SITE_HOST is missing: deploy.sh passes it from deploy/site.env}"
SIGNED_IN_USER="${SIGNED_IN_USER:?SIGNED_IN_USER is missing: deploy.sh passes it from deploy/site.env}"
SERVICE_USER=tasks
KEPT_CODE_COPIES=3
PORT=""
PORT_SOURCE=""
LOCAL=""

port_in() {
  sed -n 's/^PORT=\([0-9]\{1,5\}\)[[:space:]]*$/\1/p' "$1" | tail -n 1
}

load_port() {
  local env_path="${1:-$ENV_FILE}"
  if [ ! -f "$env_path" ]; then
    echo "refusing: $env_path is missing, so the port is unknown"
    exit 1
  fi
  PORT="$(port_in "$env_path")"
  if [ -z "$PORT" ]; then
    echo "refusing: $env_path has no PORT=<number> line"
    exit 1
  fi
  LOCAL="http://127.0.0.1:$PORT"
}

code_of() {
  curl -s -o /dev/null -w '%{http_code}' "$@"
}

wait_for_health() {
  for _ in $(seq 1 30); do
    if [ "$(code_of "$LOCAL/api/health")" = 200 ]; then
      return 0
    fi
    sleep 1
  done
  echo "Tasks did not answer /api/health within 30 s"
  return 1
}

copy_database() {
  local target="$1" scratch
  scratch="$(mktemp -d)"
  chown "$SERVICE_USER:$SERVICE_USER" "$scratch"
  runuser -u "$SERVICE_USER" -- python3 -I - "$DATA/tasks.db" "$scratch/copy.db" <<'PY'
import sqlite3, sys
source = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
copy = sqlite3.connect(sys.argv[2])
source.backup(copy)
copy.close()
source.close()
PY
  mv "$scratch/copy.db" "$target"
  rmdir "$scratch"
}

do_backup() {
  local stamp backup work
  stamp="$(date +%Y%m%d-%H%M%S)"
  backup="$STATE/tasks-before-$stamp.tgz"
  work="$(mktemp -d)"
  if [ -f "$DATA/tasks.db" ]; then
    copy_database "$work/tasks.db"
  fi
  local parts=(etc/caddy/Caddyfile)
  for path in opt/tasks etc/tasks etc/systemd/system/tasks.service srv/signin/tasks; do
    if [ -e "/$path" ]; then parts+=("$path"); fi
  done
  ( umask 077 && tar -czf "$backup" -C / --exclude=opt/tasks/.venv "${parts[@]}" -C "$work" . )
  rm -rf "$work"
  chmod 600 "$backup"
  echo "$backup" > "$BACKUP_POINTER"
  chmod 600 "$BACKUP_POINTER"
  echo "backup: $backup ($(du -h "$backup" | cut -f1))"
}

port_is_taken_by_another_program() {
  local listeners main_pid
  listeners="$(ss -ltnp "sport = :$PORT" | grep LISTEN || true)"
  [ -n "$listeners" ] || return 1
  main_pid="$(systemctl show -p MainPID --value tasks 2>/dev/null || true)"
  case "$main_pid" in ''|0) return 0 ;; esac
  ! grep -q "pid=$main_pid," <<< "$listeners"
}

ensure_user_and_folders() {
  if ! id -u "$SERVICE_USER" > /dev/null 2>&1; then
    useradd --system --home-dir "$DATA" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    echo "user: made $SERVICE_USER"
  fi
  install -d -m 700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA"
  install -d -m 750 -o root -g "$SERVICE_USER" "$CONF"
}

fill_in_site_values() {
  sed -e "s/__SITE_HOST__/$SITE_HOST/g" -e "s/__SIGNED_IN_USER__/$SIGNED_IN_USER/g" "$@"
}

install_env_file() {
  if [ -f "$ENV_FILE" ]; then
    echo "env: $ENV_FILE kept as it is"
    return
  fi
  install -m 640 -o root -g "$SERVICE_USER" /dev/null "$ENV_FILE"
  fill_in_site_values "$APP/deploy/tasks.env" > "$ENV_FILE"
  echo "env: wrote $ENV_FILE"
}

install_code() {
  local bundle="$1" stamp="$2" fresh="$APP-new-$2"
  mkdir "$fresh"
  if ! { tar -xzf "$bundle" -C "$fresh" --no-same-owner && python3 -m venv "$fresh/.venv" &&
         "$fresh/.venv/bin/pip" install -q --disable-pip-version-check -r "$fresh/requirements.txt"; }; then
    rm -rf "$fresh" "$bundle"
    echo "refusing: the new code could not be unpacked or its packages installed; nothing was changed"
    exit 1
  fi
  rm -f "$bundle"
  chown -R root:root "$fresh"
  chmod -R u=rwX,go=rX "$fresh"
  if [ -d "$APP" ]; then
    mv "$APP" "$APP-before-$stamp"
    echo "$APP-before-$stamp" > "$BEFORE_POINTER"
  else
    echo none > "$BEFORE_POINTER"
  fi
  mv "$fresh" "$APP"
}

keep_newest_copies() {
  local pattern="$1" copies surplus index
  shopt -s nullglob
  copies=($pattern)
  shopt -u nullglob
  surplus=$(( ${#copies[@]} - KEPT_CODE_COPIES ))
  for (( index = 0; index < surplus; index++ )); do
    rm -rf -- "${copies[index]}"
    echo "old copy removed: ${copies[index]}"
  done
}

prune_old_copies() {
  keep_newest_copies "$APP-before-*"
  keep_newest_copies "$APP-undone-*"
  keep_newest_copies "$SIGNIN-before-*"
  keep_newest_copies "$SIGNIN-undone-*"
  keep_newest_copies "$CADDYFILE.bak-tasks-[0-9]*"
  keep_newest_copies "$CADDYFILE.bak-tasks-undo-*"
}

install_signin_page() {
  local bundle="$1" stamp="$2"
  if [ -d "$SIGNIN" ]; then
    mv "$SIGNIN" "$SIGNIN-before-$stamp"
    echo "$SIGNIN-before-$stamp" > "$SIGNIN_BEFORE"
  else
    echo none > "$SIGNIN_BEFORE"
  fi
  mkdir -p "$SIGNIN"
  tar -xzf "$bundle" -C "$SIGNIN" --no-same-owner
  rm -f "$bundle"
  chmod -R u=rwX,go=rX "$SIGNIN"
}

marker_count() {
  grep -cxF -- "$1" "$CADDYFILE" || true
}

own_block_state() {
  local begins ends
  begins="$(marker_count "$CADDY_BLOCK_BEGIN")"
  ends="$(marker_count "$CADDY_BLOCK_END")"
  if [ "$begins" = 1 ] && [ "$ends" = 1 ]; then
    echo one
  elif [ "$begins" = 0 ] && [ "$ends" = 0 ]; then
    echo none
  else
    echo damaged
  fi
}

print_own_block() {
  awk -v begin="$CADDY_BLOCK_BEGIN" -v end="$CADDY_BLOCK_END" '
    $0 == begin { inside = 1 }
    inside { print }
    $0 == end { inside = 0 }
  ' "$CADDYFILE"
}

render_caddy_block() {
  printf '%s\n' "$CADDY_BLOCK_BEGIN"
  sed -e '/./,$!d' -e "s/__PORT__/$PORT/g" "$CADDY_BLOCK_SOURCE" | fill_in_site_values
  printf '%s\n' "$CADDY_BLOCK_END"
}

caddyfile_without_own_block() {
  awk -v begin="$CADDY_BLOCK_BEGIN" -v end="$CADDY_BLOCK_END" '
    $0 == begin { inside = 1; held = 0; next }
    inside { if ($0 == end) inside = 0; next }
    held { print ""; held = 0 }
    $0 == "" { held = 1; next }
    { print }
    END { if (held) print "" }
  ' "$CADDYFILE"
}

caddyfile_with_own_block_from() {
  awk -v begin="$CADDY_BLOCK_BEGIN" -v end="$CADDY_BLOCK_END" -v replacement="$1" '
    $0 == begin { inside = 1; while ((getline line < replacement) > 0) print line; next }
    inside { if ($0 == end) inside = 0; next }
    { print }
  ' "$CADDYFILE"
}

caddyfile_is_valid() {
  local report
  if ! command -v caddy > /dev/null 2>&1; then
    echo "caddy: no caddy command here, the Caddyfile was not validated"
    return 0
  fi
  if ! report="$(caddy validate --config "$CADDYFILE" --adapter caddyfile 2>&1)"; then
    printf '%s\n' "$report" | tail -n 5
    return 1
  fi
}

switch_caddyfile() {
  local next="$1" backup="$2"
  cp -a "$CADDYFILE" "$backup" || return 1
  cat "$next" > "$CADDYFILE"
  if ! caddyfile_is_valid; then
    cat "$backup" > "$CADDYFILE"
    echo "caddy: the changed Caddyfile did not validate, so it was put back from $backup"
    return 1
  fi
  if ! systemctl reload caddy; then
    cat "$backup" > "$CADDYFILE"
    echo "caddy: the reload failed, so the Caddyfile was put back from $backup; Caddy keeps the config it was running"
    return 1
  fi
}

add_caddy_site() {
  local stamp="$1" work change
  rm -f "$CADDY_CHANGE" "$CADDY_BLOCK_BEFORE"
  work="$(mktemp -d)"
  case "$(own_block_state)" in
    one)
      render_caddy_block > "$work/block"
      if [ "$(print_own_block)" = "$(cat "$work/block")" ]; then
        rm -rf "$work"
        echo "caddy: the marked $SITE_HOST block is up to date, left as is"
        return 0
      fi
      print_own_block > "$work/before"
      caddyfile_with_own_block_from "$work/block" > "$work/next"
      change=replaced
      ;;
    none)
      if grep -qxF "http://$SITE_HOST {" "$CADDYFILE"; then
        rm -rf "$work"
        echo "caddy: an unmarked $SITE_HOST block is already present, left as is"
        return 0
      fi
      { cat "$CADDYFILE"; printf '\n'; render_caddy_block; } > "$work/next"
      change=added
      ;;
    *)
      rm -rf "$work"
      echo "caddy: the tasks markers in $CADDYFILE are damaged (not one begin and one end line); fix them by hand"
      return 1
      ;;
  esac
  if ! switch_caddyfile "$work/next" "$CADDYFILE.bak-tasks-$stamp"; then
    rm -rf "$work"
    return 1
  fi
  if [ "$change" = replaced ]; then
    mv "$work/before" "$CADDY_BLOCK_BEFORE"
  fi
  echo "$change" > "$CADDY_CHANGE"
  rm -rf "$work"
  echo "caddy: $change the marked $SITE_HOST block for port $PORT and reloaded (earlier file: $CADDYFILE.bak-tasks-$stamp)"
}

undo_caddy_site() {
  local change state stamp work
  change="$(cat "$CADDY_CHANGE" 2>/dev/null || true)"
  [ -n "$change" ] || return 0
  state="$(own_block_state)"
  if [ "$state" = none ]; then
    rm -f "$CADDY_CHANGE" "$CADDY_BLOCK_BEFORE"
    echo "caddy: the marked tasks block is already gone, Caddyfile left as is"
    return 0
  fi
  if [ "$state" != one ]; then
    echo "caddy: the tasks markers in $CADDYFILE are damaged; nothing changed, fix them by hand"
    return 1
  fi
  stamp="$(date +%Y%m%d-%H%M%S)"
  work="$(mktemp -d)"
  case "$change" in
    added) caddyfile_without_own_block > "$work/next" ;;
    replaced) caddyfile_with_own_block_from "$CADDY_BLOCK_BEFORE" > "$work/next" ;;
    *)
      rm -rf "$work"
      echo "caddy: $CADDY_CHANGE holds '$change', which this script does not know; nothing changed"
      return 1
      ;;
  esac
  if ! switch_caddyfile "$work/next" "$CADDYFILE.bak-tasks-undo-$stamp"; then
    rm -rf "$work"
    return 1
  fi
  rm -rf "$work"
  rm -f "$CADDY_CHANGE" "$CADDY_BLOCK_BEFORE"
  echo "caddy: undid the $change tasks block only, other sites untouched (earlier file: $CADDYFILE.bak-tasks-undo-$stamp)"
}

own_block_port() {
  print_own_block | sed -n 's/^[[:space:]]*reverse_proxy 127\.0\.0\.1:\([0-9]\{1,5\}\) {$/\1/p' | head -n 1
}

own_block_replaces_the_user_header() {
  local block
  block="$(print_own_block)"
  grep -qE '^[[:space:]]*request_header -X-Forwarded-User[[:space:]]*$' <<< "$block" &&
    grep -qE "^[[:space:]]*header_up X-Forwarded-User ${SIGNED_IN_USER//./\\.}[[:space:]]*\$" <<< "$block"
}

caddy_answer() {
  curl -s -o /dev/null -w '%{http_code} %{redirect_url}' -H "Host: $SITE_HOST" "$@" "http://127.0.0.1/"
}

do_check() {
  local since="${1:-}" failures=0 health direct plain dummy header_rules signin tracebacks block_port
  load_port
  echo "service: $(systemctl is-active tasks || true)"
  health="$(curl -s "$LOCAL/api/health" || true)"
  echo "health: $health"
  direct="$(code_of "$LOCAL/api/tasks")"
  echo "app without the user header: $direct (401 expected)"
  plain="$(caddy_answer)"
  dummy="$(caddy_answer -H 'Cookie: starfire_session=not-a-session')"
  signin="$(curl -s -o /dev/null -w '%{http_code}' -H "Host: $SITE_HOST" http://127.0.0.1/signin/)"
  if own_block_replaces_the_user_header; then header_rules=yes; else header_rules=no; fi
  echo "through Caddy: no cookie -> $plain, made-up cookie -> $dummy, /signin/ -> $signin"
  echo "marked Caddy block drops a visitor's X-Forwarded-User and sets its own: $header_rules (read from the Caddyfile; a live test needs a signed-in admin session)"
  block_port="$(own_block_port)"
  echo "marked Caddy block sends to port ${block_port:-none}, PORT in $ENV_FILE is $PORT"
  echo "modes: env $(stat -c '%a %U:%G' "$ENV_FILE"), data $(stat -c '%a %U:%G' "$DATA")"
  [ "$(systemctl is-active tasks || true)" = active ] || failures=$((failures + 1))
  case "$health" in *'"ok":true'*) ;; *) failures=$((failures + 1)) ;; esac
  [ "$direct" = 401 ] || failures=$((failures + 1))
  for answer in "$plain" "$dummy"; do
    case "$answer" in "303 "*"/signin/") ;; *) failures=$((failures + 1)) ;; esac
  done
  [ "$header_rules" = yes ] || failures=$((failures + 1))
  [ "$block_port" = "$PORT" ] || failures=$((failures + 1))
  [ "$signin" = 200 ] || failures=$((failures + 1))
  [ "$(stat -c '%a' "$ENV_FILE")" = 640 ] || failures=$((failures + 1))
  [ "$(stat -c '%a' "$DATA")" = 700 ] || failures=$((failures + 1))
  if [ -n "$since" ]; then
    tracebacks="$(journalctl -u tasks --since "@$since" --no-pager | grep -c Traceback || true)"
    echo "tracebacks in the journal since restart: $tracebacks"
    [ "$tracebacks" = 0 ] || failures=$((failures + 1))
  fi
  echo "check failures: $failures"
  [ "$failures" = 0 ]
}

load_port_for_apply() {
  local code_bundle="$1" bundled_env
  if [ -f "$ENV_FILE" ]; then
    PORT_SOURCE="$ENV_FILE"
    load_port "$ENV_FILE"
    return
  fi
  PORT_SOURCE="deploy/tasks.env"
  bundled_env="$(mktemp)"
  tar -xzOf "$code_bundle" ./deploy/tasks.env > "$bundled_env"
  load_port "$bundled_env"
  rm -f "$bundled_env"
}

do_apply() {
  local code_bundle="$1" signin_bundle="$2" stamp since
  [ -s "$BACKUP_POINTER" ] && [ -s "$(cat "$BACKUP_POINTER")" ] || { echo "refusing: run backup first"; exit 1; }
  rm -f "$BEFORE_POINTER" "$CADDY_CHANGE" "$CADDY_BLOCK_BEFORE" "$SIGNIN_BEFORE"
  load_port_for_apply "$code_bundle"
  if port_is_taken_by_another_program; then
    echo "refusing: something other than tasks listens on port $PORT; change PORT in $PORT_SOURCE, the Caddy block follows it"
    exit 1
  fi
  stamp="$(date +%Y%m%d-%H%M%S)"
  ensure_user_and_folders
  install_code "$code_bundle" "$stamp"
  install_env_file
  install -m 644 "$APP/deploy/tasks.service" "$UNIT"
  systemctl daemon-reload
  systemctl enable tasks > /dev/null
  since="$(date +%s)"
  systemctl restart tasks
  wait_for_health
  install_signin_page "$signin_bundle" "$stamp"
  add_caddy_site "$stamp"
  prune_old_copies
  sleep 1
  echo "earlier code: $(cat "$BEFORE_POINTER")"
  do_check "$since"
}

undo_signin_page() {
  local stamp="$1" before
  before="$(cat "$SIGNIN_BEFORE" 2>/dev/null || true)"
  [ -n "$before" ] || return 0
  if [ "$before" != none ] && [ ! -d "$before" ]; then
    rm -f "$SIGNIN_BEFORE"
    echo "sign-in page: the earlier copy $before is gone, the current page stays"
    return 0
  fi
  if [ -d "$SIGNIN" ]; then
    mv "$SIGNIN" "$SIGNIN-undone-$stamp"
  fi
  if [ "$before" != none ]; then
    mv "$before" "$SIGNIN"
  fi
  rm -f "$SIGNIN_BEFORE"
  echo "sign-in page: earlier version back in place (undone one kept in $SIGNIN-undone-$stamp)"
}

warn_when_block_and_port_differ() {
  local block_port
  echo "env: undo does not change $ENV_FILE (PORT=$PORT)"
  [ "$(own_block_state)" = one ] || return 0
  block_port="$(own_block_port)"
  if [ "$block_port" != "$PORT" ]; then
    echo "WARNING: the Caddy block now sends to port $block_port but tasks listens on $PORT; set PORT=$block_port in $ENV_FILE and restart tasks, or run apply again"
  fi
}

do_undo() {
  local before stamp
  before="$(cat "$BEFORE_POINTER" 2>/dev/null || true)"
  if [ -z "$before" ]; then
    echo "nothing to undo: the last apply changed nothing, or undo already ran"
    return 0
  fi
  if [ "$before" != none ] && [ ! -d "$before" ]; then
    echo "refusing: the earlier code $before is missing, so nothing was changed"
    exit 1
  fi
  load_port
  stamp="$(date +%Y%m%d-%H%M%S)"
  if ! undo_caddy_site; then
    echo "undo stopped before touching the code; the Caddyfile is as it was before undo"
    exit 1
  fi
  undo_signin_page "$stamp"
  if [ "$before" = none ]; then
    systemctl disable --now tasks > /dev/null 2>&1 || true
    rm -f "$UNIT"
    systemctl daemon-reload
    mv "$APP" "$APP-undone-$stamp"
    rm -f "$BEFORE_POINTER"
    echo "service stopped and removed, code kept in $APP-undone-$stamp, tasks left in $DATA"
  else
    mv "$APP" "$APP-undone-$stamp"
    mv "$before" "$APP"
    rm -f "$BEFORE_POINTER"
    install -m 644 "$APP/deploy/tasks.service" "$UNIT"
    systemctl daemon-reload
    systemctl restart tasks
    wait_for_health
    echo "earlier code back in place, undone code kept in $APP-undone-$stamp"
  fi
  warn_when_block_and_port_differ
  prune_old_copies
}

case "${1:-}" in
  backup) do_backup ;;
  apply) do_apply "${2:?apply needs the code bundle}" "${3:?apply needs the sign-in bundle}" ;;
  check) do_check ;;
  undo) do_undo ;;
  sums) cd "$APP" && find . -type f ! -path './.venv/*' ! -path '*/__pycache__/*' -print0 | sort -z | xargs -0 sha256sum ;;
  *) echo "unknown ct action"; exit 2 ;;
esac
CT_SIDE
}

push_to_ct() {
  local source="$1" target="$2" mode="$3" name
  name="$(basename "$target")"
  scp -q "$source" "$PROXMOX:/root/$name"
  ssh -o BatchMode=yes "$PROXMOX" "pct push $CT /root/$name $target --perms $mode && rm /root/$name"
}

push_ct_script() {
  local local_copy
  local_copy="$(mktemp)"
  write_ct_script > "$local_copy"
  push_to_ct "$local_copy" "$CT_SCRIPT" 700
  rm -f "$local_copy"
}

in_ct() {
  ssh -o BatchMode=yes "$PROXMOX" "pct exec $CT -- env SITE_HOST=$SITE_HOST SIGNED_IN_USER=$SIGNED_IN_USER bash $CT_SCRIPT $*"
}

build_signin_bundle() {
  local work="$1"
  [ -f "$SITE_SIGNIN/build.py" ] || { echo "refusing: no site-signin builder at $SITE_SIGNIN (set SITE_SIGNIN)"; exit 1; }
  python3 "$SITE_SIGNIN/build.py" --first Tasks --second "" --joined --tagline "Sign in to reach your tasks." \
    --icon "$REPO/tasks/static/icon.svg" --out "$work/signin"
  tar -czf "$work/signin.tgz" -C "$work/signin" .
}

deploy_code() {
  local commit="$1" work
  work="$(mktemp -d)"
  mkdir "$work/tree"
  git -C "$REPO" archive --format=tar "$commit" | tar -x -C "$work/tree"
  ( cd "$work/tree" && find . -type f -print0 | sort -z | xargs -0 sha256sum ) > "$work/expected.sums"
  tar -czf "$work/code.tgz" -C "$work/tree" .
  build_signin_bundle "$work"
  push_to_ct "$work/code.tgz" /root/tasks-code.tgz 600
  push_to_ct "$work/signin.tgz" /root/tasks-signin.tgz 600
  if ! in_ct apply /root/tasks-code.tgz /root/tasks-signin.tgz; then
    rm -rf "$work"
    echo "apply reported failures: read them, then run undo if needed"
    exit 1
  fi
  if diff <(sort -k2 "$work/expected.sums") <(in_ct sums | sort -k2) > /dev/null; then
    echo "sha256: all $(wc -l < "$work/expected.sums") files in /opt/tasks match $(git -C "$REPO" rev-parse --short "$commit")"
  else
    echo "sha256 MISMATCH between /opt/tasks and $commit"
    rm -rf "$work"
    exit 1
  fi
  rm -rf "$work"
}

main() {
  local action="${1:-}"
  case "$action" in
    backup|check|undo)
      load_site
      push_ct_script
      in_ct "$action"
      ;;
    apply)
      load_site
      push_ct_script
      deploy_code "${2:-HEAD}"
      ;;
    -h|--help|help)
      usage
      ;;
    *)
      usage
      exit 2
      ;;
  esac
}

main "$@"
