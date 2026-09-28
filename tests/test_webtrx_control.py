"""pluto_tx/webtrx_control.py and the real web-trx/scripts/start.sh, stop.sh
-- against a throw-away copy of web-trx/ in a temp directory whose "uvicorn"
is a tiny stand-in server (no FastAPI, no GNU Radio, no hardware). Never
touches the real web-trx/run/ or a running production server: every test
uses its own directory and a free port on 127.0.0.1.
"""
import contextlib
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from pluto_tx import webtrx_control as wc

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_SCRIPTS = REPO_ROOT / "web-trx" / "scripts"

FAKE_UVICORN = r'''#!/usr/bin/env python3
# Stand-in for uvicorn (tests/test_webtrx_control.py): answers GET /health
# like web_trx.server does. Its cmdline carries "uvicorn" and
# "web_trx.server:create_app", just like the real process.
import http.server, json, os, signal, sys
args = sys.argv[1:]
host, port = args[args.index("--host") + 1], int(args[args.index("--port") + 1])
devices = json.loads(os.environ.get("FAKE_WEBTRX_DEVICES", "null"))

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        body = {"status": "ok", "backend": "FakeBackend"}
        if devices is not None:
            body["devices"] = devices
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass

server = http.server.ThreadingHTTPServer((host, port), Handler)
if os.environ.get("FAKE_WEBTRX_IGNORE_TERM") == "1":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
else:
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
print("INFO:     Application startup complete.", flush=True)
server.serve_forever()
'''


def stop_process(proc):
    proc.kill()
    proc.wait()


def run_cli(*args):
    """wc.main() with its output captured -> (exit code, output)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = wc.main(list(args))
    return code, out.getvalue()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def clean_environ(**extra):
    """os.environ without any WEB_TRX_* (a developer shell might have some)
    and without the integration switch."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("WEB_TRX_") and k != wc.DISABLE_ENV}
    env.update(extra)
    return env


def processes_under(directory):
    """PIDs of live processes whose cmdline mentions `directory`."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if str(directory).encode() in cmdline and b"web_trx.server" in cmdline:
            found.append(int(entry.name))
    return found


class FakeInstallation:
    """A web-trx/ copy with the real scripts and a fake venv/frontend."""

    def __init__(self, with_scripts=True, installed=True):
        self.tmp = tempfile.mkdtemp(prefix="webtrx-test-")
        self.dir = Path(self.tmp) / "web-trx"
        (self.dir / "scripts").mkdir(parents=True)
        (self.dir / "backend").mkdir()
        if with_scripts:
            for name in ("start.sh", "stop.sh"):
                shutil.copy2(REAL_SCRIPTS / name, self.dir / "scripts" / name)
        if installed:
            venv_bin = self.dir / "backend" / ".venv" / "bin"
            venv_bin.mkdir(parents=True)
            (venv_bin / "uvicorn").write_text(FAKE_UVICORN)
            (venv_bin / "uvicorn").chmod(0o755)
            (self.dir / "frontend" / "dist").mkdir(parents=True)
            (self.dir / "frontend" / "dist" / "index.html").write_text("<html></html>")
        self.port = free_port()
        self.write_env(f"WEB_TRX_PORT={self.port}\nWEB_TRX_HOST=127.0.0.1\nWEB_TRX_TLS=false\n"
                       "WEB_TRX_BACKEND=sim\nWEB_TRX_PASSWORD=test\n")
        self.ctl = wc.WebTrxControl(self.dir)

    def write_env(self, text):
        (self.dir / "web-trx.env").write_text(text)

    def cleanup(self):
        for pid in processes_under(self.dir):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)


@unittest.skipUnless((REAL_SCRIPTS / "start.sh").exists(), "web-trx/ not in this checkout")
@unittest.skipUnless(shutil.which("flock") and Path("/proc/self/cmdline").exists(), "needs Linux + flock")
class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.inst = FakeInstallation()
        self.addCleanup(self.inst.cleanup)
        patcher = mock.patch.dict(os.environ, clean_environ(), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ctl = self.inst.ctl

    def test_start_status_stop_cycle(self):
        self.assertEqual(self.ctl.status().state, wc.STOPPED)
        result = self.ctl.start()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        st = self.ctl.status()
        self.assertEqual(st.state, wc.RUNNING)
        self.assertEqual(st.backend, "FakeBackend")
        self.assertEqual(st.url, f"http://localhost:{self.inst.port}")
        self.assertEqual(self.ctl.start().returncode, 3)          # already running
        self.assertEqual(len(processes_under(self.inst.dir)), 1)
        result = self.ctl.stop()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.ctl.status().state, wc.STOPPED)
        self.assertEqual(processes_under(self.inst.dir), [])
        self.assertEqual(self.ctl.stop().returncode, 0)           # stopping twice is fine

    def test_concurrent_starts_start_exactly_one_server(self):
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.ctl.start().returncode)) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
        self.assertEqual(sorted(results), [0, 3])
        pids = processes_under(self.inst.dir)
        self.assertEqual(len(pids), 1)
        self.assertEqual(self.ctl.read_pid(), pids[0])
        self.assertEqual(self.ctl.stop().returncode, 0)

    def test_stale_pid_of_a_foreign_process_is_never_killed(self):
        foreign = subprocess.Popen(["sleep", "60"])
        self.addCleanup(stop_process, foreign)
        (self.inst.dir / "run").mkdir()
        (self.inst.dir / "run" / "web-trx.pid").write_text(f"{foreign.pid}\n")
        self.assertIsNone(self.ctl.read_pid())
        self.assertEqual(self.ctl.status().state, wc.STOPPED)
        result = self.ctl.stop()
        self.assertEqual(result.returncode, 0)
        self.assertIsNone(foreign.poll())                         # still alive
        self.assertFalse((self.inst.dir / "run" / "web-trx.pid").exists())
        # and start.sh starts normally over such a stale file
        self.assertEqual(self.ctl.start().returncode, 0)
        self.assertIsNone(foreign.poll())
        self.assertEqual(self.ctl.stop().returncode, 0)

    def test_devices_from_health_and_holds(self):
        devices = {"rx": {"connected": True, "device_type": "rtlsdr"}, "tx": {"connected": False, "device_type": "pluto"}}
        with mock.patch.dict(os.environ, {"FAKE_WEBTRX_DEVICES": json.dumps(devices)}):
            self.assertEqual(self.ctl.start().returncode, 0)
        st = self.ctl.status()
        self.assertEqual(st.devices, devices)
        self.assertEqual(st.held_devices(), [("rx", "rtlsdr")])
        self.assertIs(self.ctl.holds("rtlsdr"), True)
        self.assertIs(self.ctl.holds("pluto"), False)
        self.assertEqual(self.ctl.stop().returncode, 0)

    def test_older_server_without_devices_field_means_unknown(self):
        self.assertEqual(self.ctl.start().returncode, 0)
        self.assertIsNone(self.ctl.status().devices)
        self.assertIsNone(self.ctl.holds("pluto"))
        self.assertEqual(self.ctl.stop().returncode, 0)

    def test_proxy_settings_are_ignored_for_the_health_check(self):
        self.assertEqual(self.ctl.start().returncode, 0)
        dead_proxy = f"http://127.0.0.1:{free_port()}"
        with mock.patch.dict(os.environ, {"http_proxy": dead_proxy, "https_proxy": dead_proxy,
                                          "HTTP_PROXY": dead_proxy, "HTTPS_PROXY": dead_proxy}):
            self.assertEqual(self.ctl.status().state, wc.RUNNING)
        self.assertEqual(self.ctl.stop().returncode, 0)

    def test_server_not_started_from_this_checkout(self):
        proc = subprocess.Popen([sys.executable, str(self.inst.dir / "backend/.venv/bin/uvicorn"),
                                 "web_trx.server:create_app", "--host", "127.0.0.1", "--port", str(self.inst.port)],
                                stdout=subprocess.DEVNULL)
        self.addCleanup(stop_process, proc)
        deadline = time.monotonic() + 10
        while self.ctl.health() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        st = self.ctl.status(force_idle_health=True)
        self.assertEqual(st.state, wc.RUNNING_ELSEWHERE)
        self.assertEqual(run_cli("--dir", str(self.inst.dir), "status")[0], 6)

    def test_start_generates_a_password_once(self):
        self.inst.write_env(f"WEB_TRX_PORT={self.inst.port}\nWEB_TRX_HOST=127.0.0.1\nWEB_TRX_TLS=false\n")
        self.assertEqual(self.ctl.start().returncode, 0)
        env_text = (self.inst.dir / "web-trx.env").read_text()
        self.assertEqual(env_text.count("WEB_TRX_PASSWORD="), 1)
        self.assertEqual(oct((self.inst.dir / "web-trx.env").stat().st_mode & 0o777), "0o600")
        self.assertEqual(self.ctl.stop().returncode, 0)
        self.assertEqual(self.ctl.start().returncode, 0)
        self.assertEqual((self.inst.dir / "web-trx.env").read_text(), env_text)
        self.assertEqual(self.ctl.stop().returncode, 0)

    def test_stop_reports_a_forced_kill(self):
        with mock.patch.dict(os.environ, {"FAKE_WEBTRX_IGNORE_TERM": "1"}):
            self.assertEqual(self.ctl.start().returncode, 0)
        result = self.ctl.stop()                                  # ~15 s: SIGTERM is ignored
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("Safe-State", result.stdout)
        self.assertEqual(processes_under(self.inst.dir), [])


class StatusTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, clean_environ(), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_not_installed(self):
        inst = FakeInstallation(installed=False)
        self.addCleanup(inst.cleanup)
        st = inst.ctl.status()
        self.assertEqual(st.state, wc.NOT_INSTALLED)
        self.assertIn("install-web-trx.sh", st.detail)
        self.assertIs(st.holds("pluto"), False)
        self.assertEqual(run_cli("--dir", str(inst.dir), "status")[0], 5)

    def test_live_process_without_health_is_starting_then_unresponsive(self):
        inst = FakeInstallation()
        self.addCleanup(inst.cleanup)
        # cmdline like the real server, but nothing listens
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "uvicorn",
                                 "web_trx.server:create_app", str(inst.dir)])
        self.addCleanup(stop_process, proc)
        (inst.dir / "run").mkdir()
        (inst.dir / "run" / "web-trx.pid").write_text(str(proc.pid))
        self.assertEqual(inst.ctl.read_pid(), proc.pid)
        self.assertEqual(inst.ctl.status().state, wc.STARTING)
        self.assertIsNone(inst.ctl.holds("pluto"))                # unknown, not "free"
        with mock.patch.object(wc, "UNRESPONSIVE_AFTER_S", -1.0):
            self.assertEqual(inst.ctl.status().state, wc.UNRESPONSIVE)
        self.assertEqual(run_cli("--dir", str(inst.dir), "status")[0], 4)

    def test_env_file_is_read_like_bash_does(self):
        inst = FakeInstallation()
        self.addCleanup(inst.cleanup)
        inst.write_env("# comment\nexport WEB_TRX_PORT=$((8000 + 123))\nWEB_TRX_TLS='false'\n"
                       "OTHER=ignored\nWEB_TRX_PASSWORD=\"a b\"\n")
        cfg = inst.ctl.config()
        self.assertEqual(cfg["WEB_TRX_PORT"], "8123")
        self.assertEqual(cfg["WEB_TRX_PASSWORD"], "a b")
        self.assertNotIn("OTHER", cfg)
        self.assertEqual(inst.ctl.url(), "http://localhost:8123")
        # changed file -> re-read
        time.sleep(0.01)
        inst.write_env("WEB_TRX_PORT=9000\n")
        os.utime(inst.dir / "web-trx.env", ns=(time.time_ns(), time.time_ns() + 1_000_000))
        self.assertEqual(inst.ctl.url(), "https://localhost:9000")  # TLS is the default

    def test_env_parser_fallback(self):
        path = Path(tempfile.mkdtemp()) / "web-trx.env"
        self.addCleanup(shutil.rmtree, path.parent)
        path.write_text("WEB_TRX_PORT=1234\nexport WEB_TRX_TLS=\"false\"\n#WEB_TRX_HOST=x\n")
        self.assertEqual(wc._parse_env_file(path), {"WEB_TRX_PORT": "1234", "WEB_TRX_TLS": "false"})

    def test_environment_is_overridden_by_the_env_file(self):
        inst = FakeInstallation()
        self.addCleanup(inst.cleanup)
        with mock.patch.dict(os.environ, {"WEB_TRX_PORT": "1111", "WEB_TRX_HOST": "10.0.0.1"}):
            cfg = inst.ctl.config()
        self.assertEqual(cfg["WEB_TRX_PORT"], str(inst.port))
        self.assertEqual(cfg["WEB_TRX_HOST"], "127.0.0.1")

    def test_health_url_for_wildcard_hosts(self):
        inst = FakeInstallation()
        self.addCleanup(inst.cleanup)
        for host, expected in (("0.0.0.0", "127.0.0.1"), ("::", "[::1]"), ("192.168.1.5", "192.168.1.5")):
            inst.write_env(f"WEB_TRX_HOST={host}\nWEB_TRX_PORT=8321\n")
            os.utime(inst.dir / "web-trx.env", ns=(time.time_ns(), time.time_ns() + hash(host) % 10**9))
            self.assertEqual(inst.ctl._health_url(), f"https://{expected}:8321/health")

    def test_restore_devices(self):
        inst = FakeInstallation()
        self.addCleanup(inst.cleanup)
        self.assertEqual(inst.ctl.restore_devices(), [])
        (inst.dir / "run").mkdir()
        (inst.dir / "run" / "settings.json").write_text(json.dumps({
            "rx": {"connected": True, "device_type": "rtlsdr", "mode": "fm", "connection": ""},
            "tx": {"connected": True, "device_type": "pluto", "mode": None, "connection": "ip:pluto.local"},
        }))
        self.assertEqual(inst.ctl.restore_devices(), [("rx", "rtlsdr", "")])  # TX without a mode is not re-opened
        (inst.dir / "run" / "settings.json").write_text("{broken")
        self.assertEqual(inst.ctl.restore_devices(), [])

    def test_is_webtrx_process(self):
        self.assertFalse(wc.is_webtrx_process(os.getpid()))
        self.assertFalse(wc.is_webtrx_process(999_999_999))

    def test_status_holds(self):
        running = wc.Status(wc.RUNNING, devices={"rx": {"connected": False, "device_type": "pluto"},
                                                 "tx": {"connected": True, "device_type": "hackrf"}})
        self.assertIs(running.holds("hackrf"), True)
        self.assertIs(running.holds("pluto"), False)
        self.assertIsNone(wc.Status(wc.RUNNING).holds("pluto"))
        self.assertIsNone(wc.Status(wc.STARTING).holds("pluto"))
        self.assertIs(wc.Status(wc.STOPPED).holds("pluto"), False)
        self.assertIsNone(running.holds(None))

    def test_integration_switch(self):
        for value, disabled in (("off", True), ("0", True), ("OFF", True), ("on", False), ("", False)):
            with mock.patch.dict(os.environ, {wc.DISABLE_ENV: value}):
                self.assertEqual(wc.integration_disabled(), disabled, value)
        inst = FakeInstallation()
        self.addCleanup(inst.cleanup)
        with mock.patch.dict(os.environ, {wc.DISABLE_ENV: "off"}):
            self.assertIsNone(inst.ctl.holds("pluto"))

    def test_cli_url_and_devices(self):
        inst = FakeInstallation()
        self.addCleanup(inst.cleanup)
        code, out = run_cli("--dir", str(inst.dir), "url")
        self.assertEqual((code, out.strip()), (0, f"http://localhost:{inst.port}"))
        code, out = run_cli("--dir", str(inst.dir), "devices")
        self.assertEqual(code, 3)
        self.assertIn("offen: keine", out)
        code, out = run_cli("--dir", str(inst.dir), "status", "--json")
        self.assertEqual((code, json.loads(out)["state"]), (3, wc.STOPPED))

if __name__ == "__main__":
    unittest.main()
