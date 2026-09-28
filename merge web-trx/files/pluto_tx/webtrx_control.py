"""Start, stop and query the Web-TRX server (web-trx/ in this repo) from the
Qt apps and from the command line.

Qt-free and standard library only, so it is unit-testable without a display
and usable from any Python >= 3.10. The Qt layer on top is webtrx_widget.py.

The actual start/stop logic stays in web-trx/scripts/start.sh and stop.sh
(the one place that knows about the venv, TLS, the lock file and the PID
file); this module only calls them and reads what they leave behind:

  web-trx/web-trx.env      operator settings, read exactly like start.sh does
                           (bash `set -a; . file`), only WEB_TRX_* keys used
  web-trx/run/web-trx.pid  PID of the uvicorn process -- trusted only while
                           that process is alive AND its /proc cmdline names
                           uvicorn + web_trx.server (a stale PID file after a
                           reboot can point at any unrelated process)
  GET /health              answered by the server itself; for requests from
                           127.0.0.1 it also says which SDR the server holds

Nothing here ever starts or stops the server on its own -- only on an
explicit call of start()/stop() (the widget's buttons, the CLI never does).

Command line (used by web-trx/scripts/status.sh and the 'web-trx' launcher):

  python3 -m pluto_tx.webtrx_control status [--json]
  python3 -m pluto_tx.webtrx_control url
  python3 -m pluto_tx.webtrx_control devices

Exit codes: 0 running, 3 stopped, 4 starting/unresponsive, 5 not installed,
6 running elsewhere (answers /health, but not started from this checkout).
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_TRX_DIR = REPO_ROOT / "web-trx"

NOT_INSTALLED = "not_installed"
STOPPED = "stopped"
STARTING = "starting"
RUNNING = "running"
UNRESPONSIVE = "unresponsive"
RUNNING_ELSEWHERE = "running_elsewhere"

EXIT_CODES = {RUNNING: 0, STOPPED: 3, STARTING: 4, UNRESPONSIVE: 4, NOT_INSTALLED: 5, RUNNING_ELSEWHERE: 6}

DEFAULT_PORT = 8321
UNRESPONSIVE_AFTER_S = 120.0   # a live process that still has not answered /health by then
HEALTH_TIMEOUT_S = 1.0
IDLE_HEALTH_INTERVAL_S = 10.0  # without an own process: how often to look for a foreign server
SCRIPT_TIMEOUT_S = 180.0       # start.sh may build the frontend on its first run
ENV_READ_TIMEOUT_S = 5.0

# PLUTO_WEBTRX_CONTROL=off switches the app integration off: the row shows a
# note, no polling, no connect guards. tests/__init__.py sets it so the
# existing GUI tests never talk to a real server on the machine.
DISABLE_ENV = "PLUTO_WEBTRX_CONTROL"


def integration_disabled() -> bool:
    return os.environ.get(DISABLE_ENV, "").strip().lower() in ("off", "0", "false", "no")


@dataclass(frozen=True)
class Status:
    state: str
    pid: int | None = None
    url: str | None = None
    backend: str | None = None
    # {"rx": {"connected": bool, "device_type": str | None}, "tx": {...}} as
    # reported by /health, or None when unknown (not running, older server
    # without the field, request not from loopback).
    devices: dict | None = None
    detail: str = ""
    checked_at: float = field(default_factory=time.monotonic)

    def holds(self, device_type: str | None) -> bool | None:
        """True/False if the server is known to hold / not hold a device of
        this type (RX or TX), None if that is unknown."""
        if self.state in (STOPPED, NOT_INSTALLED):
            return False
        if self.devices is None or not device_type:
            return None
        for direction in ("rx", "tx"):
            d = self.devices.get(direction) or {}
            if d.get("connected") and d.get("device_type") == device_type:
                return True
        return False

    def held_devices(self) -> list[tuple[str, str]]:
        """[(direction, device_type)] the server reports as open."""
        out = []
        for direction in ("rx", "tx"):
            d = (self.devices or {}).get(direction) or {}
            if d.get("connected") and d.get("device_type"):
                out.append((direction, d["device_type"]))
        return out

    def to_dict(self) -> dict:
        return {"state": self.state, "pid": self.pid, "url": self.url, "backend": self.backend,
                "devices": self.devices, "detail": self.detail}


class WebTrxControl:
    """One Web-TRX installation (a web-trx/ directory). Thread-safe enough for
    the widget's use: status() may run in a worker thread while the GUI thread
    reads last_status."""

    def __init__(self, web_trx_dir: Path | str = WEB_TRX_DIR):
        self.dir = Path(web_trx_dir)
        self.run_dir = self.dir / "run"
        self.pid_file = self.run_dir / "web-trx.pid"
        self.env_file = self.dir / "web-trx.env"
        self._lock = threading.Lock()
        self._env_cache: tuple[tuple, dict] | None = None
        self._last_idle_health = 0.0
        self._last_idle_result: dict | None = None
        self.last_status: Status | None = None

    # -- configuration -------------------------------------------------

    def config(self) -> dict:
        """Effective WEB_TRX_* settings like start.sh sees them: the process
        environment, overridden by web-trx.env (start.sh sources the file
        after the environment is set)."""
        cfg = {k: v for k, v in os.environ.items() if k.startswith("WEB_TRX_")}
        cfg.update(self._env_file_values())
        return cfg

    def _env_file_values(self) -> dict:
        try:
            st = self.env_file.stat()
        except OSError:
            return {}
        key = (st.st_mtime_ns, st.st_size)
        with self._lock:
            if self._env_cache is not None and self._env_cache[0] == key:
                return dict(self._env_cache[1])
        values = _source_env_file(self.env_file)
        with self._lock:
            self._env_cache = (key, values)
        return dict(values)

    def port(self) -> int:
        try:
            return int(self.config().get("WEB_TRX_PORT", DEFAULT_PORT))
        except ValueError:
            return DEFAULT_PORT

    def tls(self) -> bool:
        return self.config().get("WEB_TRX_TLS", "true") == "true"  # same test as start.sh

    def url(self) -> str:
        return f"{'https' if self.tls() else 'http'}://localhost:{self.port()}"

    def _health_url(self) -> str:
        host = self.config().get("WEB_TRX_HOST", "").strip()
        if host in ("", "0.0.0.0"):
            host = "127.0.0.1"
        elif host == "::":
            host = "::1"
        if ":" in host:
            host = f"[{host}]"
        return f"{'https' if self.tls() else 'http'}://{host}:{self.port()}/health"

    def installed(self) -> tuple[bool, str]:
        missing = [p for p in ("scripts/start.sh", "scripts/stop.sh", "backend/.venv/bin/uvicorn",
                               "frontend/dist/index.html") if not (self.dir / p).exists()]
        if missing:
            return False, "missing: " + ", ".join(missing) + " (run ./install-web-trx.sh)"
        return True, ""

    # -- process -------------------------------------------------------

    def read_pid(self) -> int | None:
        """PID of our own running Web-TRX server, or None."""
        try:
            pid = int(self.pid_file.read_text().strip())
        except (OSError, ValueError):
            return None
        return pid if pid > 0 and is_webtrx_process(pid) else None

    # -- health --------------------------------------------------------

    def health(self, timeout: float = HEALTH_TIMEOUT_S) -> dict | None:
        """The /health answer as a dict, or None if nothing answers."""
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # self-signed certificate from start.sh, and only loopback anyway
        # ProxyHandler({}): an http(s)_proxy in the app's environment must never
        # see (or break) a request to our own loopback server.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                             urllib.request.HTTPSHandler(context=ctx))
        try:
            with opener.open(self._health_url(), timeout=timeout) as resp:
                data = json.loads(resp.read(65536).decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError):
            return None
        return data if isinstance(data, dict) and data.get("status") == "ok" else None

    # -- status --------------------------------------------------------

    def status(self, health_timeout: float = HEALTH_TIMEOUT_S, force_idle_health: bool = False) -> Status:
        ok, why = self.installed()
        if not ok:
            st = Status(NOT_INSTALLED, detail=why)
            self.last_status = st
            return st
        url = self.url()
        pid = self.read_pid()
        if pid is not None:
            h = self.health(health_timeout)
            if h is not None:
                st = Status(RUNNING, pid=pid, url=url, backend=h.get("backend"), devices=_devices(h))
            else:
                age = process_age_s(pid)
                if age is not None and age > UNRESPONSIVE_AFTER_S:
                    st = Status(UNRESPONSIVE, pid=pid, url=url,
                                detail=f"process alive for {age:.0f} s, /health does not answer")
                else:
                    st = Status(STARTING, pid=pid, url=url)
        else:
            now = time.monotonic()
            if force_idle_health or now - self._last_idle_health >= IDLE_HEALTH_INTERVAL_S:
                self._last_idle_health = now
                self._last_idle_result = self.health(health_timeout)
            h = self._last_idle_result
            if h is not None:
                st = Status(RUNNING_ELSEWHERE, url=url, backend=h.get("backend"), devices=_devices(h),
                            detail="a Web-TRX server answers, but it was not started from this checkout")
            else:
                st = Status(STOPPED, url=url)
        self.last_status = st
        return st

    def holds(self, device_type: str | None, timeout: float = 0.5) -> bool | None:
        """One fresh query: does the server hold a device of this type? Used
        once at app startup (before the automatic connect). None = unknown."""
        if integration_disabled():
            return None
        return self.status(health_timeout=timeout, force_idle_health=True).holds(device_type)

    # -- start/stop ----------------------------------------------------

    def start(self) -> subprocess.CompletedProcess:
        """Runs scripts/start.sh. Call from a worker thread (it can take a
        while); its own session, so a Ctrl-C in the app's terminal never
        reaches the server. Exit codes: 0 started, 3 already running,
        4 process alive but not yet confirmed, anything else = error."""
        return self._run_script("start.sh")

    def stop(self) -> subprocess.CompletedProcess:
        """Runs scripts/stop.sh. Exit codes: 0 stopped (or was not running),
        2 had to be killed -- check the transmitter's safe state."""
        return self._run_script("stop.sh")

    def _run_script(self, name: str) -> subprocess.CompletedProcess:
        script = self.dir / "scripts" / name
        try:
            return subprocess.run(["bash", str(script)], cwd=str(self.dir), stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=SCRIPT_TIMEOUT_S,
                                  start_new_session=True)
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            return subprocess.CompletedProcess(e.cmd, 124, out, f"{name}: no result after {SCRIPT_TIMEOUT_S:.0f} s")
        except OSError as e:
            return subprocess.CompletedProcess(["bash", str(script)], 127, "", f"{name}: {e}")

    # -- settings ------------------------------------------------------

    def restore_devices(self) -> list[tuple[str, str, str]]:
        """[(direction, device_type, connection)] the server re-opens when it
        starts (run/settings.json, written by the backend): shown in the start
        dialog so the operator knows which SDR is about to be taken."""
        path = Path(self.config().get("WEB_TRX_SETTINGS_PATH") or self.run_dir / "settings.json")
        try:
            saved = json.loads(path.read_text())
        except (OSError, ValueError):
            return []
        out = []
        for direction in ("rx", "tx"):
            d = saved.get(direction) if isinstance(saved, dict) else None
            if isinstance(d, dict) and d.get("connected") and d.get("device_type") and d.get("mode"):
                out.append((direction, str(d["device_type"]), str(d.get("connection") or "")))
        return out


# -- helpers -----------------------------------------------------------------

def _devices(health: dict) -> dict | None:
    devices = health.get("devices")
    return devices if isinstance(devices, dict) else None


def is_webtrx_process(pid: int) -> bool:
    """True only for a live uvicorn process serving web_trx.server -- never
    for an unrelated process that inherited a stale PID (or a zombie, whose
    cmdline is empty)."""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return b"uvicorn" in cmdline and b"web_trx.server" in cmdline


def process_age_s(pid: int) -> float | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        uptime = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    # field 22 (starttime, clock ticks after boot); the command name in
    # field 2 may contain spaces/parentheses, so count from the last ')'.
    fields = stat[stat.rfind(")") + 2:].split()
    try:
        start_ticks = int(fields[19])
    except (IndexError, ValueError):
        return None
    return max(0.0, uptime - start_ticks / os.sysconf("SC_CLK_TCK"))


def _source_env_file(path: Path) -> dict:
    """KEY=value pairs of web-trx.env, evaluated by bash exactly like
    start.sh does. Clean environment, so only what the file itself sets comes
    back; falls back to a plain KEY=value parser if bash fails."""
    env = {"HOME": os.environ.get("HOME", ""), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    try:
        out = subprocess.run(["bash", "-c", 'set -a; . "$1" >/dev/null 2>&1; env -0', "_", str(path)],
                             env=env, capture_output=True, timeout=ENV_READ_TIMEOUT_S,
                             stdin=subprocess.DEVNULL, check=True).stdout
        pairs = (item.split(b"=", 1) for item in out.split(b"\0") if b"=" in item)
        return {k.decode(errors="replace"): v.decode(errors="replace")
                for k, v in pairs if k.startswith(b"WEB_TRX_")}
    except (OSError, subprocess.SubprocessError):
        return _parse_env_file(path)


def _parse_env_file(path: Path) -> dict:
    values = {}
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key.startswith("WEB_TRX_"):
            values[key] = value
    return values


# -- command line ------------------------------------------------------------

_STATE_TEXT = {
    RUNNING: "läuft", STOPPED: "gestoppt", STARTING: "startet", UNRESPONSIVE: "reagiert nicht",
    NOT_INSTALLED: "nicht installiert", RUNNING_ELSEWHERE: "läuft (nicht aus diesem Checkout gestartet)",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m pluto_tx.webtrx_control",
                                     description="Status of the Web-TRX server in this repo (web-trx/).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_status = sub.add_parser("status", help="state of the server (exit code: 0 running, 3 stopped, "
                                             "4 starting/unresponsive, 5 not installed, 6 running elsewhere)")
    p_status.add_argument("--json", action="store_true", help="machine-readable output")
    sub.add_parser("url", help="print the URL to open in a browser")
    sub.add_parser("devices", help="print which SDRs the server holds and which it re-opens at start")
    parser.add_argument("--dir", default=str(WEB_TRX_DIR), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    ctl = WebTrxControl(args.dir)
    if args.cmd == "url":
        print(ctl.url())
        return 0
    st = ctl.status(force_idle_health=True)
    if args.cmd == "status":
        if args.json:
            print(json.dumps(st.to_dict()))
        else:
            line = f"Web-TRX: {_STATE_TEXT.get(st.state, st.state)}"
            if st.pid:
                line += f" (PID {st.pid})"
            if st.backend:
                line += f", Backend {st.backend}"
            print(line)
            if st.state in (RUNNING, STARTING, RUNNING_ELSEWHERE) and st.url:
                print(f"  {st.url}")
            if st.detail:
                print(f"  {st.detail}")
        return EXIT_CODES.get(st.state, 1)
    # devices
    held = st.held_devices()
    print("offen: " + (", ".join(f"{d.upper()} {t}" for d, t in held) if held else
                       ("keine" if st.devices is not None or st.state == STOPPED else "unbekannt")))
    restore = ctl.restore_devices()
    print("beim Start wieder geöffnet: " + (", ".join(f"{d.upper()} {t} ({c or 'auto'})" for d, t, c in restore)
                                             if restore else "keine"))
    return EXIT_CODES.get(st.state, 1)


if __name__ == "__main__":
    sys.exit(main())
