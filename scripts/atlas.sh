#!/usr/bin/env bash
# ATLAS one-command start: scripts/atlas.sh up | down | status | help
#
# Starts (or stops) everything ATLAS needs on this Mac: Postgres + Redis in Docker, the database
# schema, the API on :8000 and the dashboard on :3000. Idempotent: running `up` twice starts
# nothing twice. Written for the bash 3.2 that macOS ships (no arrays, mapfile or ${var,,}).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

STATE_DIR="$ROOT/.atlas"
LOG_DIR="$ROOT/logs"
COMPOSE_FILE="infra/docker/docker-compose.yml"
API_PORT=8000
WEB_PORT=3000
# Both servers listen on this Mac only (127.0.0.1): ATLAS has no login. The URLs below are what
# we show; the checks use the loopback address, because "localhost" may try ::1 first.
LOOPBACK=127.0.0.1
API_URL="http://localhost:$API_PORT"
WEB_URL="http://localhost:$WEB_PORT"
API_CHECK="http://$LOOPBACK:$API_PORT"
WEB_CHECK="http://$LOOPBACK:$WEB_PORT"
API_WAIT_S="${ATLAS_API_WAIT_S:-90}"
WEB_WAIT_S="${ATLAS_WEB_WAIT_S:-120}"

# --- output ------------------------------------------------------------------------------

say() { printf '%s\n' "$*"; }
warn() { printf '%s\n' "$*" >&2; }
die() {
  printf '%s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
ATLAS: stock screener and swing-trade ideas, one command to run it.

Usage: scripts/atlas.sh <command>

Commands:
  up       Start Postgres + Redis (Docker), apply database migrations, start the API (:8000)
           and the dashboard (:3000), then open it in the browser. Safe to run twice.
  down     Stop the API and the dashboard, then stop (not remove) the Docker containers.
           Your data stays.
  status   Show what is running, and the nightly scan's state: data as of, last and next
           scan, first-time download progress, whether Alpaca and Telegram are set up.
  help     Show this text.

Environment flags:
  ATLAS_DISABLE_SCHEDULER=1   Start without the automatic after-close scan. "Run scan now" in
                              the dashboard still works.
  ATLAS_NO_OPEN=1             Don't open the browser after "up".
  ATLAS_API_WAIT_S / ATLAS_WEB_WAIT_S
                              Seconds "up" waits for the API (default 90) and the dashboard
                              (default 120) before giving up.

Logs are in logs/api.log and logs/web.log; process ids are in .atlas/*.pid.
EOF
}

# --- process bookkeeping ------------------------------------------------------------------
# A pid file holds two lines: the pid, then the process's start time. A pid alone is not enough
# (after a reboot the same number belongs to some other program, and `down` must never kill it),
# so a process counts as ours only while its start time still matches the recorded one.
# `ps -o lstart` prints local time in the locale's format, so it is always asked in UTC and the C
# locale: a pid file written under one TZ must still match when read under another.

pid_file() { printf '%s/%s.pid' "$STATE_DIR" "$1"; }

start_time_of() {
  LC_ALL=C TZ=UTC ps -p "$1" -o lstart= 2>/dev/null | sed 's/^ *//; s/ *$//' || true
}

record_pid() { # name pid
  local started
  started="$(start_time_of "$2")"
  printf '%s\n%s\n' "$2" "$started" >"$(pid_file "$1")"
}

# Prints the pid of the process ATLAS started under this name, or nothing.
live_pid() {
  local file pid recorded
  file="$(pid_file "$1")"
  [ -f "$file" ] || return 0
  pid="$(sed -n '1p' "$file" | tr -dc '0-9')"
  recorded="$(sed -n '2p' "$file")"
  if [ -n "$pid" ] && [ -n "$recorded" ] && [ "$(start_time_of "$pid")" = "$recorded" ]; then
    printf '%s' "$pid"
  fi
}

# Every process below $1, deepest first (uv and pnpm start the real servers as children).
descendants() {
  local child
  for child in $(pgrep -P "$1" 2>/dev/null || true); do
    descendants "$child"
    printf '%s\n' "$child"
  done
}

port_holder() { # port -> the pid listening on it, if any
  command -v lsof >/dev/null 2>&1 || return 0
  lsof -ti "tcp:$1" -sTCP:LISTEN 2>/dev/null | head -n 1 || true
}

# Stops a process ATLAS started and everything under it: TERM the children, then the process,
# wait up to 10 s, then KILL whatever is left.
stop_tree() { # label pid
  local pids p left waited=0
  pids="$(descendants "$2" | tr '\n' ' ') $2"
  for p in $pids; do kill -TERM "$p" 2>/dev/null || true; done
  while :; do
    left=""
    for p in $pids; do
      if kill -0 "$p" 2>/dev/null; then left="$left $p"; fi
    done
    [ -z "$left" ] && break
    if [ "$waited" -ge 10 ]; then
      for p in $left; do kill -KILL "$p" 2>/dev/null || true; done
      break
    fi
    sleep 1
    waited=$((waited + 1))
  done
  say "Stopped the $1 (pid $2)"
}

# --- checks --------------------------------------------------------------------------------

# Presence only: the value is never read into a variable or printed. In .env a key counts as set
# when it has a value the dotenv loader would read as non-empty: `KEY=v`, `export KEY=v` and
# `KEY = v` are set; `KEY=`, `KEY=""`, `KEY=''` and `KEY=   # note` are not.
env_has() { # NAME
  if [ -n "$(printenv "$1" 2>/dev/null || true)" ]; then return 0; fi
  grep -Eq "^[[:space:]]*(export[[:space:]]+)?$1[[:space:]]*=[[:space:]]*([^[:space:]#\"']|\"[^\"]|'[^'])" \
    "$ROOT/.env" 2>/dev/null
}

preflight() {
  command -v uv >/dev/null 2>&1 || die "uv isn't installed. Install it with: brew install uv"
  command -v pnpm >/dev/null 2>&1 ||
    die "pnpm isn't installed. Install it with: npm install -g pnpm   (or: corepack enable pnpm)"
  if [ ! -f "$ROOT/.env" ]; then
    warn ".env not found. Create it with: cp .env.example .env   (then add your Alpaca keys)"
  fi
  if ! env_has ALPACA_API_KEY || ! env_has ALPACA_API_SECRET; then
    warn "Alpaca keys are not set in .env: ATLAS can't download prices until you add them."
  fi
}

docker_running() { docker info >/dev/null 2>&1; }

require_docker() {
  command -v docker >/dev/null 2>&1 ||
    die "Docker isn't installed. Install Docker Desktop, then run: scripts/atlas.sh up"
  docker_running || die "Docker isn't running. Start Docker Desktop, then run: scripts/atlas.sh up"
}

# A port held by a process ATLAS did not start is reported, never freed.
require_free_port() { # name label port
  local holder
  if [ -n "$(live_pid "$1")" ]; then return 0; fi
  holder="$(port_holder "$3")"
  if [ -n "$holder" ]; then
    die "Port $3 is already in use by another process (pid $holder), so the $2 can't start there. ATLAS didn't start it and won't stop it. Free the port, then run: scripts/atlas.sh up"
  fi
}

# The waits below are deadlines on the shell's clock ($SECONDS), so "90 s" means 90 seconds of
# wall time however long each attempt takes.
wait_for_postgres() {
  local deadline=$((SECONDS + 60))
  until docker exec atlas-postgres pg_isready -U atlas -d atlas >/dev/null 2>&1; do
    [ "$SECONDS" -lt "$deadline" ] || die "Postgres didn't become ready in 60 s. See: docker logs atlas-postgres"
    sleep 1
  done
  say "Postgres is ready."
}

# 0 once $1 answers; 1 after $2 seconds or as soon as process $3 is gone.
wait_for_url() { # url seconds pid
  local deadline=$((SECONDS + $2))
  while :; do
    if curl -fsS --max-time 3 "$1" >/dev/null 2>&1; then return 0; fi
    if ! kill -0 "$3" 2>/dev/null; then return 1; fi
    if [ "$SECONDS" -ge "$deadline" ]; then return 1; fi
    sleep 1
  done
}

failed_to_start() { # label logname
  {
    printf "The %s didn't start; see logs/%s.log\n" "$1" "$2"
    tail -n 20 "$LOG_DIR/$2.log" 2>/dev/null || true
    say "(If it is still starting, run scripts/atlas.sh up again. scripts/atlas.sh down stops it.)"
  } >&2
  exit 1
}

# --- up ------------------------------------------------------------------------------------

start_api() {
  local pid
  pid="$(live_pid api)"
  if [ -n "$pid" ]; then
    say "API already running (pid $pid)"
    return 0
  fi
  rm -f "$(pid_file api)"
  say "Starting the API on :$API_PORT (log: logs/api.log) ..."
  nohup uv run --package signal-service uvicorn signal_service.main:app \
    --host "$LOOPBACK" --port "$API_PORT" >>"$LOG_DIR/api.log" 2>&1 </dev/null &
  record_pid api "$!"
  if [ "${ATLAS_DISABLE_SCHEDULER:-}" = "1" ]; then
    say "ATLAS_DISABLE_SCHEDULER=1: the automatic after-close scan is off. Use Run scan now."
  fi
}

web_needs_install() {
  [ ! -d "$ROOT/apps/web/node_modules" ] ||
    [ "$ROOT/apps/web/pnpm-lock.yaml" -nt "$ROOT/apps/web/node_modules" ]
}

start_web() {
  local pid
  pid="$(live_pid web)"
  if [ -n "$pid" ]; then
    say "Web already running (pid $pid)"
    return 0
  fi
  rm -f "$(pid_file web)"
  if web_needs_install; then
    say "Installing the dashboard's packages (pnpm install --frozen-lockfile) ..."
    (cd "$ROOT/apps/web" && pnpm install --frozen-lockfile) ||
      die "pnpm install failed; see the messages above."
    if [ -d "$ROOT/apps/web/node_modules" ]; then touch "$ROOT/apps/web/node_modules"; fi
  fi
  say "Starting the dashboard on :$WEB_PORT (log: logs/web.log) ..."
  # Next listens on every interface unless told otherwise; -p pins the port instead of letting
  # Next move to 3001 when 3000 is taken (the waits and the browser use $WEB_PORT).
  (cd "$ROOT/apps/web" && exec nohup pnpm dev -H "$LOOPBACK" -p "$WEB_PORT" \
    >>"$LOG_DIR/web.log" 2>&1 </dev/null) &
  record_pid web "$!"
}

cmd_up() {
  preflight
  require_docker
  require_free_port api "API" "$API_PORT"
  require_free_port web "dashboard" "$WEB_PORT"
  mkdir -p "$STATE_DIR" "$LOG_DIR"

  say "Starting Postgres and Redis ..."
  docker compose -f "$COMPOSE_FILE" up -d
  wait_for_postgres
  say "Applying database migrations ..."
  uv run python -m atlas_shared.migrate up || die "Database migrations failed; see the messages above."

  start_api
  start_web

  say "Waiting for the API ..."
  wait_for_url "$API_CHECK/healthz" "$API_WAIT_S" "$(live_pid api || true)" || failed_to_start "API" api
  say "Waiting for the dashboard ..."
  wait_for_url "$WEB_CHECK" "$WEB_WAIT_S" "$(live_pid web || true)" || failed_to_start "web app" web

  say "ATLAS is up: $WEB_URL (first run downloads data in the background; progress is on the Today page)"
  if [ "$(uname)" = "Darwin" ] && [ -z "${ATLAS_NO_OPEN:-}" ]; then
    open "$WEB_URL" || true
  fi
}

# --- down ----------------------------------------------------------------------------------

# A killed server can take a moment to let go of its port, so look again for up to 2 s before
# reporting it.
stop_port_report() { # port
  local holder waited=0
  holder="$(port_holder "$1")"
  while [ -n "$holder" ] && [ "$waited" -lt 2 ]; do
    sleep 1
    waited=$((waited + 1))
    holder="$(port_holder "$1")"
  done
  if [ -n "$holder" ]; then
    warn "Port $1 is still in use by pid $holder. ATLAS didn't start it, or it hasn't exited."
  fi
}

cmd_down() {
  local pid
  pid="$(live_pid web)"
  if [ -n "$pid" ]; then stop_tree "dashboard" "$pid"; else say "Dashboard wasn't running"; fi
  pid="$(live_pid api)"
  if [ -n "$pid" ]; then stop_tree "API" "$pid"; else say "API wasn't running"; fi
  rm -f "$(pid_file web)" "$(pid_file api)"
  stop_port_report "$API_PORT"
  stop_port_report "$WEB_PORT"

  if docker_running; then
    docker compose -f "$COMPOSE_FILE" stop
  else
    say "Docker isn't running, so there are no containers to stop."
  fi
  say "ATLAS is stopped. Your data is kept; scripts/atlas.sh up starts it again."
}

# --- status --------------------------------------------------------------------------------

summarise_status() { # reads the JSON in $ATLAS_STATUS_JSON
  if command -v python3 >/dev/null 2>&1; then
    python3 - <<'PYEOF' || say "  (couldn't read the status summary)"
import json
import os
from datetime import datetime


def yes(value):
    return "yes" if value else "no"


def when(iso):
    if not iso:
        return None
    try:
        moment = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return str(iso)
    try:
        from zoneinfo import ZoneInfo

        moment = moment.astimezone(ZoneInfo("America/New_York"))
        return moment.strftime("%a %d %b %Y %H:%M ") + str(moment.tzname())
    except Exception:
        return moment.isoformat()


def summary(status):
    last = status.get("last_scan")
    rows = [("Data as of", status.get("data_as_of") or "nothing scanned yet")]
    if last:
        line = "session %s, %s (%s run)" % (last.get("session"), last.get("status"), last.get("kind"))
        if last.get("error"):
            line += ": " + str(last["error"])[:200]
        rows.append(("Last scan", line))
    else:
        rows.append(("Last scan", "none yet"))
    rows.append(("Next scan", when(status.get("next_scan_at")) or "not scheduled"))
    if status.get("scheduler_running"):
        rows.append(("Scheduler", "running"))
    else:
        rows.append(("Scheduler", "off: " + str(status.get("disabled_reason") or "not running")))
    progress = status.get("backfill")
    if progress and progress.get("total"):
        done, total = int(progress["done"]), int(progress["total"])
        rows.append(("First download", "%d of %d symbols (%d%%)" % (done, total, 100 * done // total)))
    rows.append(("Alpaca keys", yes(status.get("alpaca_configured"))))
    rows.append(("Telegram", yes(status.get("telegram_configured"))))
    rows.append(("Auto-execution", "on" if status.get("auto_execution") else "off"))
    return ["  %-15s %s" % (label + ":", value) for label, value in rows]


# Anything unexpected in the answer (not JSON, not an object, a wrong type) gives the one fallback
# line, never a traceback; nothing is printed until the whole summary is built.
try:
    status = json.loads(os.environ["ATLAS_STATUS_JSON"])
    if not isinstance(status, dict):
        raise ValueError("not a JSON object")
    lines = summary(status)
except Exception:
    print("  (couldn't read the status summary)")
else:
    print("\n".join(lines))
PYEOF
  else
    say "  (python3 not found, so the scan summary is skipped; see the Settings page)"
  fi
}

# The "detail" of an error answer ({"detail": "..."}), if it has one; reads $ATLAS_STATUS_JSON.
error_detail() {
  command -v python3 >/dev/null 2>&1 || return 0
  python3 - <<'PYEOF' 2>/dev/null || true
import json
import os

try:
    detail = json.loads(os.environ["ATLAS_STATUS_JSON"]).get("detail")
except Exception:
    detail = None
if isinstance(detail, str) and detail.strip():
    print(detail.strip()[:300])
PYEOF
}

cmd_status() {
  local pid code answer raw body status_code detail

  if docker_running; then
    say "Docker:    running"
    if docker exec atlas-postgres pg_isready -U atlas -d atlas >/dev/null 2>&1; then
      say "Postgres:  ready"
    else
      say "Postgres:  not ready (atlas-postgres isn't accepting connections)"
    fi
  else
    say "Docker:    not running"
    say "Postgres:  unknown (Docker isn't running)"
  fi

  pid="$(live_pid api)"
  if [ -n "$pid" ]; then
    if curl -fsS --max-time 3 "$API_CHECK/healthz" >/dev/null 2>&1; then
      say "API:       running (pid $pid), answering at $API_URL"
    else
      say "API:       pid $pid is running but $API_URL/healthz doesn't answer (see logs/api.log)"
    fi
  elif curl -fsS --max-time 3 "$API_CHECK/healthz" >/dev/null 2>&1; then
    say "API:       answering at $API_URL, but scripts/atlas.sh didn't start it"
  else
    say "API:       not running"
  fi

  pid="$(live_pid web)"
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$WEB_CHECK" 2>/dev/null || true)"
  answer="HTTP $code"
  if [ "$code" = "000" ] || [ -z "$code" ]; then answer="nothing"; fi # 000: curl's "no answer"
  if [ -n "$pid" ]; then
    if [ "$code" = "200" ]; then
      say "Web:       running (pid $pid), HTTP 200 at $WEB_URL"
    else
      say "Web:       pid $pid is running but $WEB_URL answered $answer (see logs/web.log)"
    fi
  elif [ "$code" = "200" ]; then
    say "Web:       answering at $WEB_URL, but scripts/atlas.sh didn't start it"
  else
    say "Web:       not running"
  fi

  # The body, a newline and the HTTP status (000 when nothing answered).
  raw="$(curl -s --max-time 5 -w '\n%{http_code}' "$API_CHECK/v1/system/status" 2>/dev/null || true)"
  status_code=""
  body="$raw"
  case "$raw" in
    *$'\n'*)
      status_code="${raw##*$'\n'}"
      body="${raw%$'\n'*}"
      ;;
  esac
  say ""
  case "$status_code" in
    200)
      say "Nightly scan:"
      ATLAS_STATUS_JSON="$body" summarise_status
      ;;
    503)
      say "Nightly scan: unknown (the API is up but its database isn't reachable)"
      detail="$(ATLAS_STATUS_JSON="$body" error_detail)"
      if [ -n "$detail" ]; then say "  $detail"; fi
      ;;
    "" | 000)
      say "Nightly scan: unknown (the API didn't answer /v1/system/status)"
      ;;
    *)
      say "Nightly scan: unknown (the API answered HTTP $status_code to /v1/system/status)"
      ;;
  esac
}

# --- main ----------------------------------------------------------------------------------

case "${1:-help}" in
  up) cmd_up ;;
  down) cmd_down ;;
  status) cmd_status ;;
  help | -h | --help) usage ;;
  *)
    warn "Unknown command: $1"
    warn ""
    usage >&2
    exit 2
    ;;
esac
