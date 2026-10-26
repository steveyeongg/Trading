"""``scripts/atlas.sh``: the one-command start for ATLAS (``up``, ``down``, ``status``, ``help``).

None of these tests starts Docker, the API or the dashboard. Each one that runs a command copies
the script into a throwaway repo root (the script resolves its root from its own location, so its
``.atlas/``, ``logs/`` and ``.env`` stay inside that folder) and runs it with a ``PATH`` made of a
folder of fake ``docker``, ``uv``, ``pnpm``, ``curl``, ``lsof`` and ``open`` programs plus the
system directories. The fakes record their calls, and the "servers" are ``sleep`` processes.

The script is executed through its shebang with that ``PATH``, so on a Mac it runs under the
system's bash 3.2: the oldest bash it has to support.

Regression notes (review of the first version):

* ``ps -o lstart`` prints local time, so a pid file written under one ``TZ`` no longer matched
  the same process read under another, and ``down`` left the servers running
  (``test_pid_check_survives_a_different_timezone_and_locale``).
* ``pnpm dev`` binds Next to every interface, and the dashboard has no login: the script now binds
  both servers to 127.0.0.1 and pins the dashboard's port.
* The waits counted loop passes, not seconds, so a slow ``curl`` made "90 s" much longer.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "atlas.sh"

DOCKER_DOWN = "Docker isn't running. Start Docker Desktop, then run: scripts/atlas.sh up"
READY = (
    "ATLAS is up: http://localhost:3000 "
    "(first run downloads data in the background; progress is on the Today page)"
)
ALPACA_WARNING = "Alpaca keys are not set in .env: ATLAS can't download prices until you add them."
STALE_START = "Mon Jan  1 00:00:00 2001"

STATUS_JSON = (
    '{"data_as_of": "2026-10-16", "last_scan": {"session": "2026-10-16", "kind": "nightly", '
    '"started_at": "2026-10-16T20:45:00+00:00", "generated_at": "2026-10-16T20:47:00+00:00", '
    '"status": "ok", "steps": {}, "error": null}, "next_scan_at": "2026-10-19T20:45:00+00:00", '
    '"backfill": {"done": 3200, "total": 6400}, "alpaca_configured": true, '
    '"telegram_configured": false, "auto_execution": false, "scheduler_running": true, '
    '"disabled_reason": null}'
)

# --- the fake programs --------------------------------------------------------------------

_FAKE_DOCKER = """#!/bin/sh
echo "$*" >> "$FAKE_LOG/docker.log"
if [ "$1" = "info" ]; then exit "${FAKE_DOCKER_INFO_EXIT:-0}"; fi
exit 0
"""

# `uv run ... uvicorn` is the API: a shell that starts a child and waits, so the tests can see
# that `down` stops the child as well as the parent. Anything else (the migration) just succeeds.
_FAKE_UV = """#!/bin/sh
echo "$*" >> "$FAKE_LOG/uv.log"
case "$*" in
  *uvicorn*)
    echo "api boot line"
    sleep 600 &
    echo $! > "$FAKE_LOG/api-child.pid"
    wait
    ;;
esac
exit 0
"""

_FAKE_PNPM = """#!/bin/sh
echo "$*" >> "$FAKE_LOG/pnpm.log"
if [ "$1" = "dev" ]; then
  echo "web boot line"
  sleep 600 &
  echo $! > "$FAKE_LOG/web-child.pid"
  wait
fi
exit 0
"""

# curl exits FAKE_CURL_EXIT (after FAKE_CURL_DELAY seconds; only for URLs containing FAKE_FAIL_URL
# when that is set), answers the status route with
# FAKE_STATUS_JSON and FAKE_STATUS_CODE, and honours `-w '%{http_code}'` the way the real one does
# (including the "000" it prints when nothing answered).
_FAKE_CURL = """#!/bin/sh
echo "$*" >> "$FAKE_LOG/curl.log"
if [ -n "${FAKE_CURL_DELAY:-}" ]; then sleep "$FAKE_CURL_DELAY"; fi
fmt=""; url=""
for arg in "$@"; do
  case "$arg" in
    *'%{http_code}') fmt="$arg" ;;
    http://*) url="$arg" ;;
  esac
done
emit() {
  printf '%s' "$1"
  case "$fmt" in '\\n'*) printf '\\n' ;; esac
  if [ -n "$fmt" ]; then printf '%s' "$2"; fi
}
if [ "${FAKE_CURL_EXIT:-0}" != "0" ]; then
  emit "" 000
  exit "$FAKE_CURL_EXIT"
fi
if [ -n "${FAKE_FAIL_URL:-}" ]; then
  case "$url" in *"$FAKE_FAIL_URL"*) emit "" 000; exit 7 ;; esac
fi
case "$url" in
  *v1/system/status) emit "$FAKE_STATUS_JSON" "${FAKE_STATUS_CODE:-200}" ;;
  *) emit "" 200 ;;
esac
exit 0
"""

# lsof reports a listener for the port in FAKE_BUSY_PORT: for ever, or for the first
# FAKE_BUSY_CALLS calls about that port when that is set.
_FAKE_LSOF = """#!/bin/sh
case "$*" in
  *"tcp:${FAKE_BUSY_PORT:-none}"*)
    n=0
    if [ -f "$FAKE_LOG/lsof.count" ]; then n=$(cat "$FAKE_LOG/lsof.count"); fi
    n=$((n + 1))
    echo "$n" > "$FAKE_LOG/lsof.count"
    if [ -n "${FAKE_BUSY_CALLS:-}" ] && [ "$n" -gt "$FAKE_BUSY_CALLS" ]; then exit 1; fi
    echo 4242
    exit 0
    ;;
esac
exit 1
"""

_FAKE_OPEN = """#!/bin/sh
echo "$*" >> "$FAKE_LOG/open.log"
exit 0
"""


@dataclass
class Sandbox:
    """A throwaway repo root with a copy of the script, and the fake programs."""

    root: Path
    bin: Path
    logs: Path
    script: Path

    def env(self, **extra: str) -> dict[str, str]:
        # Built from scratch: nothing of the real environment (keys, ports, flags) leaks in.
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(self.root),
            "FAKE_LOG": str(self.logs),
            "FAKE_STATUS_JSON": STATUS_JSON,
        }
        env.update(extra)
        return env

    def run(self, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.script), *args],
            cwd=self.root,
            env=self.env(**extra),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def calls(self, program: str) -> list[str]:
        path = self.logs / f"{program}.log"
        return path.read_text().splitlines() if path.exists() else []

    def pid(self, name: str) -> int:
        return int((self.root / ".atlas" / f"{name}.pid").read_text().splitlines()[0])

    def write_env(self, text: str) -> None:
        (self.root / ".env").write_text(text)


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_dead(pid: int, seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def _read_pid_file(path: Path, seconds: float = 5.0) -> int:
    """The fake servers write their child's pid a moment after ``up`` has already returned."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if path.exists() and path.read_text().strip():
            return int(path.read_text().split()[0])
        time.sleep(0.05)
    raise AssertionError(f"{path.name} was never written")


def _kill_leftovers(box: Sandbox) -> None:
    """Whatever a failing test left running: the fake servers and their sleeping children."""
    for path in [*(box.root / ".atlas").glob("*.pid"), *box.logs.glob("*-child.pid")]:
        try:
            pid = int(path.read_text().split()[0])
            os.kill(pid, signal.SIGKILL)
        except (OSError, ValueError, IndexError):
            continue


@pytest.fixture
def box(tmp_path: Path) -> Iterator[Sandbox]:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "apps" / "web").mkdir(parents=True)
    (root / "apps" / "web" / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n")
    bin_dir, logs = tmp_path / "bin", tmp_path / "calls"
    bin_dir.mkdir()
    logs.mkdir()
    script = root / "scripts" / "atlas.sh"
    shutil.copy(SCRIPT, script)
    script.chmod(0o755)
    for name, body in (
        ("docker", _FAKE_DOCKER),
        ("uv", _FAKE_UV),
        ("pnpm", _FAKE_PNPM),
        ("curl", _FAKE_CURL),
        ("lsof", _FAKE_LSOF),
        ("open", _FAKE_OPEN),
        # The status summary is read with python3: use this interpreter, not the system's.
        ("python3", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n'),
    ):
        _write_exec(bin_dir / name, body)
    sandbox = Sandbox(root=root, bin=bin_dir, logs=logs, script=script)
    yield sandbox
    _kill_leftovers(sandbox)


@dataclass
class Running:
    """A sandbox after a first ``up`` (browser opening off) on top of a stale pid file."""

    box: Sandbox
    first: subprocess.CompletedProcess[str]
    stale_pid: int
    api_pid: int
    web_pid: int
    api_child: int
    web_child: int


@pytest.fixture
def running(box: Sandbox) -> Running:
    # A stale pid file (a process that has exited) is replaced, never trusted.
    gone = subprocess.Popen(["true"])
    gone.wait()
    (box.root / ".atlas").mkdir()
    (box.root / ".atlas" / "api.pid").write_text(f"{gone.pid}\n{STALE_START}\n")

    first = box.run("up", ATLAS_NO_OPEN="1")
    assert first.returncode == 0, first.stdout + first.stderr
    return Running(
        box=box,
        first=first,
        stale_pid=gone.pid,
        api_pid=box.pid("api"),
        web_pid=box.pid("web"),
        api_child=_read_pid_file(box.logs / "api-child.pid"),
        web_child=_read_pid_file(box.logs / "web-child.pid"),
    )


# --- the brief's test ---------------------------------------------------------------------


def test_atlas_script_parses_and_prints_usage() -> None:
    assert subprocess.run(["bash", "-n", str(SCRIPT)], cwd=ROOT).returncode == 0
    out = subprocess.run(
        ["bash", str(SCRIPT), "help"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert out.returncode == 0 and all(w in out.stdout for w in ("up", "down", "status"))


def test_script_is_executable_and_its_state_directory_is_ignored() -> None:
    assert os.access(SCRIPT, os.X_OK), "scripts/atlas.sh must be executable"
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    assert ".atlas/" in ignored, ".atlas/ (pid files) must be git-ignored"
    assert "logs/" in ignored, "logs/ must be git-ignored"


def test_the_preview_config_binds_the_dashboard_to_localhost_too() -> None:
    launch = ROOT / ".claude" / "launch.json"
    if not launch.exists():
        pytest.skip("no .claude/launch.json in this checkout")
    configs = {c["name"]: c for c in json.loads(launch.read_text())["configurations"]}
    assert configs["atlas-web"]["runtimeArgs"] == ["dev", "-H", "127.0.0.1", "-p", "3000"]


def test_postgres_redis_and_the_dashboard_listen_on_this_mac_only() -> None:
    """The incident: Docker published Postgres (dev password in the compose file) and Redis (no
    password) on every interface, and ``pnpm dev`` without ``-H`` served the dashboard to the
    whole network. Both now bind 127.0.0.1. The data stays in bind-mounted folders, so
    ``docker compose up -d`` recreating the containers with the new ports keeps it."""
    compose = (ROOT / "infra" / "docker" / "docker-compose.yml").read_text()
    ports = [line.split("- ", 1)[1].split("#")[0].strip().strip('"')
             for line in compose.splitlines() if line.strip().startswith('- "') and ":" in line
             and line.strip()[3].isdigit()]  # fmt: skip
    assert ports == ["127.0.0.1:5432:5432", "127.0.0.1:6379:6379"]
    assert "./data/postgres:/var/lib/postgresql/data" in compose and "./data/redis:/data" in compose
    package = json.loads((ROOT / "apps" / "web" / "package.json").read_text())
    assert package["scripts"]["dev"] == "next dev -H 127.0.0.1"


# --- usage ---------------------------------------------------------------------------------


def test_help_and_no_arguments_list_commands_and_flags(box: Sandbox) -> None:
    for args in (("help",), (), ("--help",)):
        out = box.run(*args)
        assert out.returncode == 0, out.stderr
        for word in ("up", "down", "status", "ATLAS_DISABLE_SCHEDULER", "ATLAS_NO_OPEN"):
            assert word in out.stdout, (args, word)


def test_unknown_subcommand_exits_2_and_prints_the_usage(box: Sandbox) -> None:
    out = box.run("frobnicate")
    assert out.returncode == 2
    shown = out.stdout + out.stderr
    assert "frobnicate" in shown
    assert all(w in shown for w in ("up", "down", "status"))
    # Nothing was touched.
    assert not (box.root / ".atlas").exists() and not (box.root / "logs").exists()
    assert box.calls("docker") == []


# --- up: preflight -------------------------------------------------------------------------


def test_up_stops_with_the_docker_message_when_docker_is_not_running(box: Sandbox) -> None:
    out = box.run("up", FAKE_DOCKER_INFO_EXIT="1")
    assert out.returncode == 1
    assert DOCKER_DOWN in out.stdout + out.stderr
    # Docker was asked once, whether it was running. Nothing was started, migrated or installed.
    assert box.calls("docker") == ["info"]
    assert box.calls("uv") == [] and box.calls("pnpm") == []
    assert not (box.root / ".atlas").exists() and not (box.root / "logs").exists()


def test_up_names_a_missing_uv_or_pnpm_before_touching_docker(box: Sandbox) -> None:
    system_path = "/usr/bin:/bin:/usr/sbin:/sbin"
    if shutil.which("uv", path=system_path) or shutil.which("pnpm", path=system_path):
        pytest.skip("uv or pnpm is installed in a system directory on this machine")
    (box.bin / "uv").unlink()
    out = box.run("up")
    assert out.returncode == 1
    assert "uv" in out.stderr and "brew install uv" in out.stderr
    (box.bin / "pnpm").unlink()
    _write_exec(box.bin / "uv", _FAKE_UV)
    out = box.run("up")
    assert out.returncode == 1
    assert "pnpm" in out.stderr and "install -g pnpm" in out.stderr
    assert box.calls("docker") == []


SECRET, OTHER = "KEYVALUE-1f3a9c", "SECRETVALUE-77b2e0"


@pytest.mark.parametrize(
    ("env_text", "warns"),
    [
        pytest.param(f"ALPACA_API_KEY={SECRET}\nALPACA_API_SECRET=\n", True, id="secret-missing"),
        pytest.param("", True, id="empty-file"),
        pytest.param(f"ALPACA_API_KEY={SECRET}\nALPACA_API_SECRET={OTHER}\n", False, id="both-set"),
        pytest.param("# ALPACA_API_KEY=abc\n# ALPACA_API_SECRET=def\n", True, id="commented-out"),
        pytest.param(
            "ALPACA_API_KEY=   # from alpaca\nALPACA_API_SECRET=# here\n", True, id="template-notes"
        ),
        pytest.param(
            f"export ALPACA_API_KEY={SECRET}\nexport ALPACA_API_SECRET={OTHER}\n",
            False,
            id="export-form",
        ),
        pytest.param(
            f"ALPACA_API_KEY = {SECRET}\nALPACA_API_SECRET   =   {OTHER}\n",
            False,
            id="spaces-around-equals",
        ),
        pytest.param(
            f"ALPACA_API_KEY=\"{SECRET}\"\nALPACA_API_SECRET='{OTHER}'\n", False, id="quoted-values"
        ),
        pytest.param(f'ALPACA_API_KEY=""\nALPACA_API_SECRET={OTHER}\n', True, id="empty-double"),
        pytest.param(f"ALPACA_API_KEY={SECRET}\nALPACA_API_SECRET=''\n", True, id="empty-single"),
        pytest.param(
            f"export ALPACA_API_KEY=''\nexport ALPACA_API_SECRET={OTHER}\n", True, id="export-empty"
        ),
    ],
)
def test_alpaca_key_check_reads_env_file_shapes_and_never_prints_values(
    box: Sandbox, env_text: str, warns: bool
) -> None:
    box.write_env(env_text)
    out = box.run("up", FAKE_DOCKER_INFO_EXIT="1")
    assert DOCKER_DOWN in out.stderr
    assert (ALPACA_WARNING in out.stderr) is warns, out.stderr
    for shown in (out.stdout, out.stderr):
        assert SECRET not in shown and OTHER not in shown


def test_up_without_an_env_file_points_at_the_template(box: Sandbox) -> None:
    out = box.run("up", FAKE_DOCKER_INFO_EXIT="1")
    assert ".env.example" in out.stderr and ALPACA_WARNING in out.stderr
    box.write_env(f"ALPACA_API_KEY={SECRET}\nALPACA_API_SECRET={OTHER}\n")
    out = box.run("up", FAKE_DOCKER_INFO_EXIT="1")
    assert ".env.example" not in out.stderr


def test_keys_exported_in_the_shell_count_as_set(box: Sandbox) -> None:
    out = box.run(
        "up", FAKE_DOCKER_INFO_EXIT="1", ALPACA_API_KEY="shell-key", ALPACA_API_SECRET="shell-sec"
    )
    assert ALPACA_WARNING not in out.stderr
    assert "shell-key" not in out.stdout + out.stderr


def test_up_refuses_a_port_held_by_another_process_and_kills_nothing(box: Sandbox) -> None:
    out = box.run("up", FAKE_BUSY_PORT="3000")
    assert out.returncode == 1
    assert "3000" in out.stderr and "in use" in out.stderr
    assert box.calls("uv") == [] and box.calls("pnpm") == []
    assert not (box.root / ".atlas").exists() or not list((box.root / ".atlas").glob("*.pid"))
    out = box.run("up", FAKE_BUSY_PORT="8000")
    assert out.returncode == 1 and "8000" in out.stderr


# --- up ------------------------------------------------------------------------------------


def test_up_runs_the_steps_in_order_and_starts_both_servers(running: Running) -> None:
    box = running.box
    assert READY in running.first.stdout
    assert box.calls("open") == [], "ATLAS_NO_OPEN must stop the browser opening"

    docker = box.calls("docker")
    assert docker[0] == "info"
    assert "compose -f infra/docker/docker-compose.yml up -d" in docker
    assert any(c.startswith("exec atlas-postgres pg_isready") for c in docker)

    uv = box.calls("uv")
    migrate = [i for i, c in enumerate(uv) if "atlas_shared.migrate up" in c]
    serve = [i for i, c in enumerate(uv) if "uvicorn signal_service.main:app" in c]
    assert migrate and serve and migrate[0] < serve[0]
    assert "--package signal-service" in uv[serve[0]] and "--reload" not in uv[serve[0]]
    assert uv[serve[0]].endswith("--host 127.0.0.1 --port 8000")

    # Install, then the dashboard bound to this Mac only, on the port the script waits on.
    assert box.calls("pnpm") == ["install --frozen-lockfile", "dev -H 127.0.0.1 -p 3000"]

    assert (box.root / "logs" / "api.log").exists() and (box.root / "logs" / "web.log").exists()
    assert "api boot line" in (box.root / "logs" / "api.log").read_text()
    assert "web boot line" in (box.root / "logs" / "web.log").read_text()
    assert _alive(running.api_pid) and _alive(running.web_pid)

    # The waits ask the loopback address, not "localhost" (which may try ::1 first).
    curl = " ".join(box.calls("curl"))
    assert "http://127.0.0.1:8000/healthz" in curl and "http://127.0.0.1:3000" in curl


def test_up_replaces_a_stale_pid_file(running: Running) -> None:
    assert running.api_pid != running.stale_pid
    assert (running.box.root / ".atlas" / "api.pid").read_text().splitlines()[1] != STALE_START


def test_up_again_starts_nothing_new(running: Running) -> None:
    box = running.box
    again = box.run("up", ATLAS_NO_OPEN="1")
    assert again.returncode == 0, again.stdout + again.stderr
    assert f"API already running (pid {running.api_pid})" in again.stdout
    assert f"Web already running (pid {running.web_pid})" in again.stdout
    assert READY in again.stdout
    assert box.pid("api") == running.api_pid and box.pid("web") == running.web_pid
    assert len([c for c in box.calls("uv") if "uvicorn" in c]) == 1
    assert [c for c in box.calls("pnpm") if c.startswith("dev")] == ["dev -H 127.0.0.1 -p 3000"]


def test_up_again_opens_the_browser_on_a_mac(running: Running) -> None:
    again = running.box.run("up")
    assert again.returncode == 0, again.stdout + again.stderr
    if platform.system() == "Darwin":
        assert running.box.calls("open") == ["http://localhost:3000"]
    else:
        assert running.box.calls("open") == []


def test_up_reports_an_api_that_never_answers_with_the_tail_of_its_log(box: Sandbox) -> None:
    """Also pins the wait to wall-clock time: each failed attempt takes 2 s here, so counting
    loop passes (the first version) would run for about 9 s, a deadline for about 5."""
    started = time.monotonic()
    out = box.run(
        "up", ATLAS_NO_OPEN="1", FAKE_CURL_EXIT="7", FAKE_CURL_DELAY="2", ATLAS_API_WAIT_S="3"
    )
    elapsed = time.monotonic() - started
    assert out.returncode == 1
    shown = out.stdout + out.stderr
    assert "The API didn't start; see logs/api.log" in shown
    assert "api boot line" in shown, "the last lines of logs/api.log are printed"
    assert "scripts/atlas.sh down" in shown, "the way to stop what was started is named"
    assert READY not in shown
    assert elapsed < 7.5, f"the 3 s wait took {elapsed:.1f} s"


def test_up_reports_a_dashboard_that_never_answers_with_the_tail_of_its_log(
    box: Sandbox,
) -> None:
    out = box.run("up", ATLAS_NO_OPEN="1", FAKE_FAIL_URL=":3000", ATLAS_WEB_WAIT_S="2")
    assert out.returncode == 1
    shown = out.stdout + out.stderr
    assert "The web app didn't start; see logs/web.log" in shown
    assert "web boot line" in shown
    assert "The API didn't start" not in shown and READY not in shown


# --- status --------------------------------------------------------------------------------


def test_status_reports_each_piece_and_the_nightly_scan(running: Running) -> None:
    status = running.box.run("status")
    assert status.returncode == 0, status.stderr
    for text in (
        "Docker",
        "running",
        "Postgres",
        f"pid {running.api_pid}",
        f"pid {running.web_pid}",
        "200",
        "2026-10-16",
        "3200",
        "6400",
    ):
        assert text in status.stdout, (text, status.stdout)
    assert "Telegram" in status.stdout and "Alpaca" in status.stdout


def _backfilling(data_as_of: str | None, trigger: str) -> str:
    status = json.loads(STATUS_JSON)
    status["data_as_of"] = data_as_of
    status["last_scan"].update(status="running", steps={
        "bars": {"status": "running", "done": 3200, "total": 6400, "trigger": trigger}})  # fmt: skip
    return json.dumps(status)


def test_status_calls_a_later_backfill_downloading_prices_and_says_why(running: Running) -> None:
    """Every running backfill used to read "First download", even the weekly one after the
    asset list was refreshed. Only a store that was never scanned is the first download."""
    later = running.box.run("status", FAKE_STATUS_JSON=_backfilling("2026-10-16", "asset list refreshed"))
    assert later.returncode == 0, later.stderr
    assert "Downloading prices:" in later.stdout and "3200 of 6400 symbols (50%)" in later.stdout
    assert "because: asset list refreshed" in later.stdout
    assert "first download" not in later.stdout.lower()

    first = running.box.run("status", FAKE_STATUS_JSON=_backfilling(None, "no SPY bars stored"))
    assert "Downloading prices:" in first.stdout and "first download" in first.stdout


def test_status_reports_a_running_server_that_does_not_answer(running: Running) -> None:
    sick = running.box.run("status", FAKE_CURL_EXIT="7")
    assert sick.returncode == 0, sick.stderr
    assert f"pid {running.api_pid} is running but" in sick.stdout
    assert f"pid {running.web_pid} is running but" in sick.stdout
    assert "answered nothing" in sick.stdout


def test_status_with_the_database_down_says_so_and_gives_the_reason(box: Sandbox) -> None:
    detail = "Can't reach the database. Is Postgres running? (connection refused)"
    out = box.run("status", FAKE_STATUS_CODE="503", FAKE_STATUS_JSON=json.dumps({"detail": detail}))
    assert out.returncode == 0, out.stderr
    assert "API is up but its database isn't reachable" in out.stdout
    assert detail in out.stdout
    assert "didn't answer" not in out.stdout and "Traceback" not in out.stdout + out.stderr


def test_status_with_an_answer_that_is_not_json_falls_back_without_a_traceback(
    box: Sandbox,
) -> None:
    out = box.run("status", FAKE_STATUS_JSON="<html>502 Bad Gateway</html>")
    assert out.returncode == 0, out.stderr
    assert "couldn't read the status summary" in out.stdout
    assert "Traceback" not in out.stdout + out.stderr and out.stderr == ""
    # Valid JSON that is not the expected object behaves the same.
    out = box.run("status", FAKE_STATUS_JSON="[1, 2, 3]")
    assert "couldn't read the status summary" in out.stdout
    assert "Traceback" not in out.stdout + out.stderr


def test_status_succeeds_with_everything_down(box: Sandbox) -> None:
    out = box.run("status", FAKE_DOCKER_INFO_EXIT="1", FAKE_CURL_EXIT="7")
    assert out.returncode == 0, out.stderr
    assert "Traceback" not in out.stdout + out.stderr
    for text in ("Docker", "Postgres", "API", "Web", "not running", "didn't answer"):
        assert text in out.stdout, (text, out.stdout)


# --- down ----------------------------------------------------------------------------------


def test_down_stops_process_trees_and_stops_containers(running: Running) -> None:
    box = running.box
    down = box.run("down")
    assert down.returncode == 0, down.stdout + down.stderr
    for pid in (running.api_pid, running.web_pid, running.api_child, running.web_child):
        assert _wait_dead(pid), f"pid {pid} still running after down"
    assert not list((box.root / ".atlas").glob("*.pid"))
    docker = box.calls("docker")
    assert "compose -f infra/docker/docker-compose.yml stop" in docker
    assert not any(" down" in f" {c}" for c in docker)


def test_status_after_down_says_not_running(running: Running) -> None:
    running.box.run("down")
    after = running.box.run("status", FAKE_CURL_EXIT="7")
    assert after.returncode == 0
    assert "not running" in after.stdout


def test_pid_check_survives_a_different_timezone_and_locale(box: Sandbox) -> None:
    """``ps -o lstart`` prints local time. The start time recorded by ``up`` under one TZ and
    locale must still match when ``status`` and ``down`` run under another, or ``down`` would
    leave the servers running."""
    up = box.run(
        "up",
        ATLAS_NO_OPEN="1",
        TZ="Asia/Kuala_Lumpur",
        LANG="en_GB.UTF-8",
        LC_ALL="en_GB.UTF-8",
    )
    assert up.returncode == 0, up.stdout + up.stderr
    api_pid, web_pid = box.pid("api"), box.pid("web")
    api_child = _read_pid_file(box.logs / "api-child.pid")
    web_child = _read_pid_file(box.logs / "web-child.pid")

    status = box.run("status", TZ="UTC")
    assert f"pid {api_pid}" in status.stdout and f"pid {web_pid}" in status.stdout
    again = box.run("up", ATLAS_NO_OPEN="1", TZ="America/Los_Angeles")
    assert f"API already running (pid {api_pid})" in again.stdout, again.stdout + again.stderr

    down = box.run("down", TZ="UTC")
    assert down.returncode == 0, down.stdout + down.stderr
    for pid in (api_pid, web_pid, api_child, web_child):
        assert _wait_dead(pid), f"pid {pid} still running after down under another TZ"
    assert not list((box.root / ".atlas").glob("*.pid"))


def test_a_pid_file_for_someone_elses_process_is_never_trusted_or_killed(box: Sandbox) -> None:
    """The pid in the file is alive but is not the process ATLAS started (its start time differs):
    a reboot reuses pids. ``down`` must not kill it, ``status`` must not call it ATLAS's API."""
    stranger = subprocess.Popen(["sleep", "60"])
    try:
        (box.root / ".atlas").mkdir()
        for name in ("api", "web"):
            (box.root / ".atlas" / f"{name}.pid").write_text(f"{stranger.pid}\n{STALE_START}\n")
        status = box.run("status", FAKE_CURL_EXIT="7")
        assert status.returncode == 0
        assert f"pid {stranger.pid}" not in status.stdout
        down = box.run("down")
        assert down.returncode == 0, down.stdout + down.stderr
        assert stranger.poll() is None, "down killed a process ATLAS did not start"
        assert not list((box.root / ".atlas").glob("*.pid"))
    finally:
        stranger.kill()
        stranger.wait()


def test_down_with_nothing_running_and_docker_stopped_succeeds(box: Sandbox) -> None:
    out = box.run("down", FAKE_DOCKER_INFO_EXIT="1")
    assert out.returncode == 0, out.stdout + out.stderr
    assert not any("stop" in c for c in box.calls("docker"))


def test_down_reports_a_port_that_stays_in_use_after_a_pause(box: Sandbox) -> None:
    started = time.monotonic()
    out = box.run("down", FAKE_BUSY_PORT="8000")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "Port 8000 is still in use by pid 4242" in out.stderr
    assert time.monotonic() - started >= 2, "the port was re-checked before it was reported"


def test_down_does_not_report_a_port_that_frees_up_a_moment_later(box: Sandbox) -> None:
    out = box.run("down", FAKE_BUSY_PORT="8000", FAKE_BUSY_CALLS="1")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "still in use" not in out.stdout + out.stderr
    assert int((box.logs / "lsof.count").read_text()) >= 2
