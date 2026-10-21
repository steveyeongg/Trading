"""``scripts/atlas.sh``: the one-command start for ATLAS (``up``, ``down``, ``status``, ``help``).

None of these tests starts Docker, the API or the dashboard. Each one that runs a command copies
the script into a throwaway repo root (the script resolves its root from its own location, so its
``.atlas/``, ``logs/`` and ``.env`` stay inside that folder) and runs it with a ``PATH`` made of a
folder of fake ``docker``, ``uv``, ``pnpm``, ``curl``, ``lsof`` and ``open`` programs plus the
system directories. The fakes record their calls, and the "servers" are ``sleep`` processes.

The script is executed through its shebang with that ``PATH``, so on a Mac it runs under the
system's bash 3.2: the oldest bash it has to support.
"""

from __future__ import annotations

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

# curl succeeds (exit FAKE_CURL_EXIT), answers the status route with JSON and `-w` with "200".
_FAKE_CURL = """#!/bin/sh
for arg in "$@"; do
  case "$arg" in
    *v1/system/status)
      if [ "${FAKE_CURL_EXIT:-0}" != "0" ]; then exit "$FAKE_CURL_EXIT"; fi
      printf '%s' "$FAKE_STATUS_JSON"; exit 0 ;;
  esac
done
if [ "${FAKE_CURL_EXIT:-0}" != "0" ]; then exit "$FAKE_CURL_EXIT"; fi
for arg in "$@"; do
  case "$arg" in '%{http_code}') printf 200; exit 0 ;; esac
done
exit 0
"""

# lsof reports a listener only for the port in FAKE_BUSY_PORT.
_FAKE_LSOF = """#!/bin/sh
case "$*" in *"tcp:${FAKE_BUSY_PORT:-none}"*) echo 4242; exit 0 ;; esac
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


def test_up_warns_when_alpaca_keys_are_missing_and_never_prints_env_values(box: Sandbox) -> None:
    secret, other = "KEYVALUE-1f3a9c", "SECRETVALUE-77b2e0"
    # No .env at all: say so, point at the template, warn about the keys.
    out = box.run("up", FAKE_DOCKER_INFO_EXIT="1")
    assert ".env.example" in out.stderr and ALPACA_WARNING in out.stderr
    # Only the key: the secret is still missing.
    (box.root / ".env").write_text(f"ALPACA_API_KEY={secret}\nALPACA_API_SECRET=\n")
    out = box.run("up", FAKE_DOCKER_INFO_EXIT="1")
    assert ALPACA_WARNING in out.stderr and ".env.example" not in out.stderr
    # Both set: no warning. Values are never printed in any case.
    (box.root / ".env").write_text(f"ALPACA_API_KEY={secret}\nALPACA_API_SECRET={other}\n")
    out = box.run("up", FAKE_DOCKER_INFO_EXIT="1")
    assert ALPACA_WARNING not in out.stdout + out.stderr
    assert DOCKER_DOWN in out.stderr
    for run_output in (out.stdout, out.stderr):
        assert secret not in run_output and other not in run_output
    # A commented-out key is not a key, and neither is an uncommented template line with a note.
    (box.root / ".env").write_text("# ALPACA_API_KEY=abc\n# ALPACA_API_SECRET=def\n")
    out = box.run("up", FAKE_DOCKER_INFO_EXIT="1")
    assert ALPACA_WARNING in out.stderr
    (box.root / ".env").write_text("ALPACA_API_KEY=   # from alpaca\nALPACA_API_SECRET=# here\n")
    out = box.run("up", FAKE_DOCKER_INFO_EXIT="1")
    assert ALPACA_WARNING in out.stderr


def test_up_refuses_a_port_held_by_another_process_and_kills_nothing(box: Sandbox) -> None:
    out = box.run("up", FAKE_BUSY_PORT="3000")
    assert out.returncode == 1
    assert "3000" in out.stderr and "in use" in out.stderr
    assert box.calls("uv") == [] and box.calls("pnpm") == []
    assert not (box.root / ".atlas").exists() or not list((box.root / ".atlas").glob("*.pid"))
    out = box.run("up", FAKE_BUSY_PORT="8000")
    assert out.returncode == 1 and "8000" in out.stderr


# --- up, status and down on fakes ----------------------------------------------------------


def test_up_status_up_again_and_down(box: Sandbox) -> None:
    # A stale pid file (a process that has exited) is replaced, never trusted.
    gone = subprocess.Popen(["true"])
    gone.wait()
    (box.root / ".atlas").mkdir()
    (box.root / ".atlas" / "api.pid").write_text(f"{gone.pid}\nMon Jan  1 00:00:00 2001\n")

    first = box.run("up", ATLAS_NO_OPEN="1")
    assert first.returncode == 0, first.stdout + first.stderr
    assert READY in first.stdout
    assert box.calls("open") == [], "ATLAS_NO_OPEN must stop the browser opening"

    # The steps ran in the brief's order, with its commands.
    docker = box.calls("docker")
    assert docker[0] == "info"
    assert "compose -f infra/docker/docker-compose.yml up -d" in docker
    assert any(c.startswith("exec atlas-postgres pg_isready") for c in docker)
    uv = box.calls("uv")
    migrate = [i for i, c in enumerate(uv) if "atlas_shared.migrate up" in c]
    serve = [i for i, c in enumerate(uv) if "uvicorn signal_service.main:app --port 8000" in c]
    assert migrate and serve and migrate[0] < serve[0]
    assert "--package signal-service" in uv[serve[0]] and "--reload" not in uv[serve[0]]
    assert box.calls("pnpm") == ["install --frozen-lockfile", "dev"]
    assert (box.root / "logs" / "api.log").exists() and (box.root / "logs" / "web.log").exists()
    assert "api boot line" in (box.root / "logs" / "api.log").read_text()

    api_pid, web_pid = box.pid("api"), box.pid("web")
    assert api_pid != gone.pid and _alive(api_pid) and _alive(web_pid)
    api_child = _read_pid_file(box.logs / "api-child.pid")
    web_child = _read_pid_file(box.logs / "web-child.pid")

    # Running it again starts nothing new, and opens the browser (on a Mac).
    again = box.run("up")
    assert again.returncode == 0, again.stdout + again.stderr
    assert f"API already running (pid {api_pid})" in again.stdout
    assert f"Web already running (pid {web_pid})" in again.stdout
    assert READY in again.stdout
    assert box.pid("api") == api_pid and box.pid("web") == web_pid
    assert len([c for c in box.calls("uv") if "uvicorn" in c]) == 1
    assert box.calls("pnpm").count("dev") == 1
    if platform.system() == "Darwin":
        assert box.calls("open") == ["http://localhost:3000"]
    else:
        assert box.calls("open") == []

    # status: one line each, then the nightly scan's summary.
    status = box.run("status")
    assert status.returncode == 0, status.stderr
    for text in (
        "Docker",
        "running",
        "Postgres",
        f"pid {api_pid}",
        f"pid {web_pid}",
        "200",
        "2026-10-16",
        "3200",
        "6400",
    ):
        assert text in status.stdout, (text, status.stdout)
    assert "Telegram" in status.stdout and "Alpaca" in status.stdout

    # A server that is running but not answering is reported as such, and status still succeeds.
    sick = box.run("status", FAKE_CURL_EXIT="7")
    assert sick.returncode == 0, sick.stderr
    assert f"pid {api_pid} is running but" in sick.stdout
    assert f"pid {web_pid} is running but" in sick.stdout and "answered nothing" in sick.stdout

    # down: parents and their children stop, pid files go, containers stop (not removed).
    down = box.run("down")
    assert down.returncode == 0, down.stdout + down.stderr
    for pid in (api_pid, web_pid, api_child, web_child):
        assert _wait_dead(pid), f"pid {pid} still running after down"
    assert not list((box.root / ".atlas").glob("*.pid"))
    docker = box.calls("docker")
    assert "compose -f infra/docker/docker-compose.yml stop" in docker
    assert not any(" down" in f" {c}" for c in docker)

    # And with nothing running, status says so and still succeeds.
    after = box.run("status", FAKE_CURL_EXIT="7")
    assert after.returncode == 0
    assert "not running" in after.stdout


def test_up_reports_an_api_that_never_answers_with_the_tail_of_its_log(box: Sandbox) -> None:
    out = box.run("up", ATLAS_NO_OPEN="1", FAKE_CURL_EXIT="7", ATLAS_API_WAIT_S="1")
    assert out.returncode == 1
    shown = out.stdout + out.stderr
    assert "The API didn't start; see logs/api.log" in shown
    assert "api boot line" in shown, "the last lines of logs/api.log are printed"
    assert READY not in shown
    assert "scripts/atlas.sh down" in shown, "the way to stop what was started is named"


def test_a_pid_file_for_someone_elses_process_is_never_trusted_or_killed(box: Sandbox) -> None:
    """The pid in the file is alive but is not the process ATLAS started (its start time differs):
    a reboot reuses pids. ``down`` must not kill it, ``status`` must not call it ATLAS's API."""
    stranger = subprocess.Popen(["sleep", "60"])
    try:
        (box.root / ".atlas").mkdir()
        for name in ("api", "web"):
            (box.root / ".atlas" / f"{name}.pid").write_text(
                f"{stranger.pid}\nMon Jan  1 00:00:00 2001\n"
            )
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


# --- status and down never fail hard -------------------------------------------------------


def test_status_succeeds_with_everything_down(box: Sandbox) -> None:
    out = box.run("status", FAKE_DOCKER_INFO_EXIT="1", FAKE_CURL_EXIT="7")
    assert out.returncode == 0, out.stderr
    assert "Traceback" not in out.stdout + out.stderr
    for text in ("Docker", "Postgres", "API", "Web", "not running"):
        assert text in out.stdout, (text, out.stdout)


def test_down_with_nothing_running_and_docker_stopped_succeeds(box: Sandbox) -> None:
    out = box.run("down", FAKE_DOCKER_INFO_EXIT="1")
    assert out.returncode == 0, out.stdout + out.stderr
    assert not any("stop" in c for c in box.calls("docker"))
