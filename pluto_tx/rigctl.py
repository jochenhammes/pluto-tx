"""Client for Hamlib's rigctld (TCP, extended response protocol) and the rigctld process itself.

rigctld is the one owner of a radio's serial port: the serial port is exclusive, and pluto-tx, pluto-advanced-rx,
Web-TRX or WSJT-X must be able to share a rig. Standard library only, so rig_guard.py can run it next to a dying app.
Docs: rigctld(1), "Extended Response Protocol"; measured against a Xiegu X6200 in docs/x6200/TESTS.md."""
import shutil
import socket
import subprocess
import threading
import time

DEFAULT_TCP_PORT = 4532
RIGCTLD_START_TIMEOUT_S = 5.0


class RigError(ValueError):
    """A ValueError: the callers around key_ptt() already report those to the operator, before any RF action."""


class RigTimeout(RigError):
    pass


class RigctlClient:
    """One TCP connection to rigctld. One request at a time, from any thread."""

    def __init__(self, host="127.0.0.1", port=DEFAULT_TCP_PORT, timeout=3.0):
        self.host, self.port, self.timeout = host, int(port), float(timeout)
        self._sock = None
        self._buffer = b""
        self._lock = threading.Lock()

    def connect(self):
        with self._lock:
            self._reconnect()

    def _reconnect(self):
        self._drop()
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            raise RigError(f"rigctld at {self.host}:{self.port} is not reachable: {e}") from e
        self._sock.settimeout(self.timeout)

    def _drop(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._buffer = b""

    def close(self):
        with self._lock:
            self._drop()

    def request(self, line):
        """One command in rigctld's extended form -> {key: value} of its answer. Raises RigError on RPRT != 0.
        A connection that broke is reopened once: rigctld may have been restarted between two transmissions."""
        with self._lock:
            for attempt in (0, 1):
                try:
                    if self._sock is None:
                        self._reconnect()
                    self._sock.sendall(f"+{line}\n".encode())
                    return self._read_answer(line)
                except socket.timeout as e:
                    self._drop()
                    raise RigTimeout(f"rigctld did not answer {line!r}") from e
                except OSError as e:
                    self._drop()
                    if attempt:
                        raise RigError(f"rigctld connection lost during {line!r}: {e}") from e
        raise RigError(f"rigctld did not answer {line!r}")

    def _query(self, line):
        """A read can safely be repeated: one lost answer (seen once under load, docs/x6200/TESTS.md) must not end a session."""
        try:
            return self.request(line)
        except RigTimeout:
            return self.request(line)

    def _read_answer(self, line):
        values = {}
        while True:
            while b"\n" not in self._buffer:
                chunk = self._sock.recv(4096)
                if not chunk:
                    raise OSError("connection closed")
                self._buffer += chunk
            raw, self._buffer = self._buffer.split(b"\n", 1)
            text = raw.decode("ascii", "replace").strip()
            if text.startswith("RPRT"):
                code = int(text.split()[1])
                if code != 0:
                    raise RigError(f"radio refused {line!r} (Hamlib error {code})")
                return values
            key, sep, value = text.partition(":")
            if sep:
                values[key.strip()] = value.strip()
            elif text:
                values["value"] = text  # a level comes as a bare number after its "get_level: NAME" line

    def get_frequency(self):
        return float(self._query("f")["Frequency"])

    def set_frequency(self, hz):
        self.request(f"F {int(round(hz))}")

    def get_mode(self):
        return self._query("m")["Mode"]

    def set_mode(self, mode):
        self.request(f"M {mode} 0")

    def get_ptt(self):
        return int(self._query("t")["PTT"]) != 0

    def set_ptt(self, on):
        self.request(f"T {1 if on else 0}")

    def get_level(self, name):
        return float(self._query(f"l {name}")["value"])

    def set_level(self, name, value):
        self.request(f"L {name} {value:.4f}")


class RigctldManager:
    """Uses a rigctld already listening on the port, else starts one and leaves it running: no app may end it, because another
    one (a receiver next to a transmitter, Web-TRX next to a desktop app) or rig_guard.py may need it to release the PTT."""

    def __init__(self, serial_port, model, tcp_port=DEFAULT_TCP_PORT, baud=19200, program=None):
        self.serial_port, self.model, self.tcp_port, self.baud = serial_port, int(model), int(tcp_port), int(baud)
        self.program = program
        self.process = None

    def _responds(self):
        client = RigctlClient(port=self.tcp_port, timeout=1.0)
        try:
            client.get_frequency()
            return True
        except RigError:
            return False
        finally:
            client.close()

    def ensure_running(self):
        if self._responds():
            return
        program = self.program or _find_rigctld()
        if not program:
            raise RigError("rigctld is not installed (apt install libhamlib-utils)")
        self.process = subprocess.Popen(
            [program, "-m", str(self.model), "-r", self.serial_port, "-s", str(self.baud), "-T", "127.0.0.1", "-t", str(self.tcp_port)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)  # outlives the app: see the class docstring
        end = time.monotonic() + RIGCTLD_START_TIMEOUT_S
        while time.monotonic() < end:
            if self.process.poll() is not None:
                raise RigError(f"rigctld ended at once (exit {self.process.returncode}); is {self.serial_port} in use?")
            if self._responds():
                return
            time.sleep(0.1)
        self.process.terminate()
        raise RigError(f"no answer from the radio on {self.serial_port} through rigctld")


def _find_rigctld():
    try:
        from . import paths
    except ImportError:
        return shutil.which("rigctld")
    return paths.program("rigctld")
