"""Client for the standard `rtl_tcp` server (osmocom rtl-sdr) -- lets the RX app use
an RTL-SDR that is plugged into another machine.

Protocol (unchanged since rtl_tcp was written): on connect the server sends a
12-byte header (b"RTL0", tuner type BE32, tuner gain count BE32), then streams
raw unsigned 8-bit interleaved I/Q forever. The client controls the dongle with
5-byte commands: one command byte + one BE32 parameter. The server serves ONE
client at a time (a second connection waits in the listen backlog until the
first one leaves).

Design notes:
- No native support exists in the installed SoapyRTLSDR/librtlsdr and gr-osmosdr
  is not a dependency of this project, so this is a small self-contained client:
  `RtlTcpClient` (socket + reader thread + auto-reconnect, GNU Radio free, unit
  testable against tests/fake_rtl_tcp.py) and `RtlTcpSource` (the gr.sync_block
  wrapper the flowgraph connects like any other source).
- The reader thread connects at construction and reconnects with backoff, so
  constructing a source never raises and never blocks: the RX GUI builds the new
  flowgraph BEFORE shutting the old one down, and a single-client server only
  accepts the new connection once the old one is closed -- the pending connection
  simply waits in the backlog.
- Every parameter ever set is remembered and replayed after a (re)connect.
- Adaptive jitter buffer (measured against a real server over WLAN): rtl_tcp hands
  the stream out in 256 kB blocks (librtlsdr's default buffer), i.e. one burst
  every 0.5 s at 250 kS/s / 0.13 s at 1 MS/s, plus WLAN stalls of a few hundred
  ms. Feeding that straight into the flowgraph underruns the audio every burst.
  The client therefore collects data until `target_s` seconds are buffered before
  it starts delivering, and after an underrun refills to the target again instead
  of trickling. `target_s` follows the longest stall seen in the last 30 s (x1.5)
  and grows after every underrun; cost: that much extra audio latency.
"""
import collections
import re
import socket
import struct
import threading
import time

import numpy as np
from gnuradio import gr

DEFAULT_PORT = 1234
HEADER_MAGIC = b"RTL0"
HEADER_LEN = 12

# rtl_tcp command bytes (rtl_tcp.c)
CMD_SET_FREQ = 0x01
CMD_SET_SAMPLE_RATE = 0x02
CMD_SET_GAIN_MODE = 0x03  # 0 = tuner AGC, 1 = manual
CMD_SET_GAIN = 0x04  # tenth of a dB
CMD_SET_AGC_MODE = 0x08  # RTL2832 digital AGC
CMD_SET_DIRECT_SAMPLING = 0x09  # 0 = off, 1 = I branch, 2 = Q branch
# order the remembered parameters are replayed in after a (re)connect
_REPLAY_ORDER = (CMD_SET_SAMPLE_RATE, CMD_SET_DIRECT_SAMPLING, CMD_SET_FREQ, CMD_SET_GAIN_MODE, CMD_SET_AGC_MODE, CMD_SET_GAIN)

# jitter buffer (seconds of stream)
MIN_TARGET_S = 0.3
MAX_TARGET_S = 3.0
STALL_WINDOW_S = 30.0
STALL_MIN_S = 0.02  # gaps between recv() returns shorter than this are normal packet spacing

TUNER_NAMES = {1: "E4000", 2: "FC0012", 3: "FC0013", 4: "FC2580", 5: "R820T", 6: "R828D"}

_HOST_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class RtlTcpError(Exception):
    pass


def parse_connection(text):
    """(host, port) if `text` names an rtl_tcp server, else None (= a local
    serial number / blank). Network forms: "host:port", "a.b.c.d" (port 1234),
    "rtl_tcp://host[:port]", "tcp:host[:port]". A bare word without '.' or ':'
    stays a serial number unless it carries a tcp:/rtl_tcp:// prefix. Raises
    ValueError for a network form with a bad host/port."""
    text = (text or "").strip()
    if not text or "=" in text or "," in text:
        return None  # blank, or a full Soapy args string
    forced = False
    for prefix in ("rtl_tcp://", "rtl_tcp:", "tcp://", "tcp:"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
            forced = True
            break
    if not forced and ":" not in text and "." not in text:
        return None
    host, port = text, DEFAULT_PORT
    if ":" in text:
        host, _, port_text = text.rpartition(":")
        if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
            raise ValueError(f"invalid port {port_text!r} in {text!r}")
        port = int(port_text)
    host = host.strip("[]")
    if not host or not _HOST_RE.match(host):
        raise ValueError(f"invalid host {host!r}")
    return host, port


def read_header(sock):
    """Read and validate the 12-byte header from a freshly connected socket ->
    (tuner_type, gain_count). Raises RtlTcpError."""
    data = b""
    while len(data) < HEADER_LEN:
        try:
            chunk = sock.recv(HEADER_LEN - len(data))
        except socket.timeout:
            raise RtlTcpError("no rtl_tcp header received (server busy with another client, or not an rtl_tcp server)") from None
        if not chunk:
            raise RtlTcpError("connection closed before the rtl_tcp header")
        data += chunk
    if data[:4] != HEADER_MAGIC:
        raise RtlTcpError(f"not an rtl_tcp server (bad magic {data[:4]!r})")
    tuner, gains = struct.unpack(">II", data[4:12])
    return tuner, gains


def probe(host, port, timeout_s=5.0):
    """None if an rtl_tcp server answers with a valid header, else the exception.
    Closes the probe connection immediately (the server serves one client)."""
    try:
        with socket.create_connection((host, port), timeout=timeout_s) as s:
            s.settimeout(timeout_s)
            read_header(s)
    except (OSError, RtlTcpError) as e:
        return e
    return None


class RtlTcpClient:
    """Socket + reader thread. Never raises from the background thread; state is
    exposed via `status` ("connecting" / "connected" / "lost") and counters."""

    CONNECT_TIMEOUT_S = 5.0
    HEADER_TIMEOUT_S = 15.0  # long: a single-client server only answers once the previous client left
    BACKOFF_S = 1.0
    RECV_BYTES = 262144

    def __init__(self, host, port, sample_rate_hz=2_400_000):
        self.host = host
        self.port = port
        self._default_rate = sample_rate_hz
        self._settings = {}  # cmd -> param, replayed after every (re)connect
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._chunks = collections.deque()
        self._buffered = 0
        self._sock = None
        self._closed = False
        self.status = "connecting"
        self.tuner_type = None
        self.gain_count = None
        self.dropped_bytes = 0
        self.reconnects = 0
        self.underruns = 0
        self._playing = False  # False = (re)filling the jitter buffer up to target_s
        self._stalls = collections.deque()  # (monotonic time, gap s) of recent stream stalls
        self._underrun_target_s = 0.0
        self._target_s = MIN_TARGET_S
        self._thread = threading.Thread(target=self._run, name=f"rtl_tcp {host}:{port}", daemon=True)
        self._thread.start()

    # -- commands --------------------------------------------------------
    def send_command(self, cmd: int, param: int):
        """Remember and (if connected) send one command. Never raises."""
        param &= 0xFFFFFFFF
        with self._lock:
            self._settings[cmd] = param
            sock = self._sock if self.status == "connected" else None
        if sock is not None:
            self._send(sock, cmd, param)

    @staticmethod
    def _send(sock, cmd, param):
        try:
            sock.sendall(struct.pack(">BI", cmd, param))
        except OSError:
            pass  # the reader notices the dead socket and reconnects

    # -- jitter buffer ---------------------------------------------------
    def _bytes_per_s(self):
        return 2 * self._settings.get(CMD_SET_SAMPLE_RATE, self._default_rate)

    @property
    def target_s(self):
        return self._target_s

    @property
    def buffered_s(self):
        return self._buffered / self._bytes_per_s()

    def _note_gap(self, gap, now):
        """(under self._lock) track stream stalls and derive the buffer target."""
        if gap > STALL_MIN_S:
            self._stalls.append((now, gap))
        while self._stalls and self._stalls[0][0] < now - STALL_WINDOW_S:
            self._stalls.popleft()
        longest = max((g for _, g in self._stalls), default=0.0)
        target = max(1.5 * longest + 0.1, self._underrun_target_s, MIN_TARGET_S)
        self._target_s = min(MAX_TARGET_S, target)

    # -- stream ----------------------------------------------------------
    def read(self, max_bytes: int, timeout_s: float = 0.1) -> bytes:
        """Up to max_bytes of raw stream data; b"" while the jitter buffer is
        (re)filling or nothing arrived in time."""
        deadline = time.monotonic() + timeout_s
        with self._cond:
            while True:
                if self._playing:
                    if self._buffered > 0:
                        break
                    self._playing = False  # ran dry: refill to the target instead of trickling
                    self.underruns += 1
                    self._underrun_target_s = min(MAX_TARGET_S, 1.5 * self._target_s)
                    self._target_s = max(self._target_s, self._underrun_target_s)
                elif self._buffered >= self._target_s * self._bytes_per_s():
                    self._playing = True
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return b""
                self._cond.wait(remaining)
            out = bytearray()
            while self._chunks and len(out) < max_bytes:
                chunk = self._chunks.popleft()
                room = max_bytes - len(out)
                if len(chunk) > room:
                    self._chunks.appendleft(chunk[room:])
                    chunk = chunk[:room]
                out += chunk
            self._buffered -= len(out)
            return bytes(out)

    def close(self):
        with self._cond:
            self._closed = True
            sock, self._sock = self._sock, None
            self._cond.notify_all()
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    # -- reader thread ---------------------------------------------------
    def _run(self):
        while not self._closed:
            sock = None
            try:
                sock = socket.create_connection((self.host, self.port), timeout=self.CONNECT_TIMEOUT_S)
                sock.settimeout(self.HEADER_TIMEOUT_S)
                self.tuner_type, self.gain_count = read_header(sock)
                sock.settimeout(1.0)
                with self._lock:
                    if self._closed:
                        break
                    self._chunks.clear()  # a reconnect is a stream discontinuity anyway
                    self._buffered = 0
                    self._playing = False
                    self._sock = sock
                    replay = [(c, self._settings[c]) for c in _REPLAY_ORDER if c in self._settings]
                    self.status = "connected"
                for cmd, param in replay:
                    self._send(sock, cmd, param)
                self._stream(sock)
            except (OSError, RtlTcpError):
                pass
            finally:
                with self._lock:
                    if self._sock is sock:
                        self._sock = None
                    if not self._closed:
                        self.status = "lost" if self.tuner_type is not None else "connecting"
                        self.reconnects += 1
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            deadline = time.monotonic() + self.BACKOFF_S
            while not self._closed and time.monotonic() < deadline:
                time.sleep(0.05)

    def _stream(self, sock):
        last = None
        while not self._closed:
            try:
                data = sock.recv(self.RECV_BYTES)
            except socket.timeout:
                continue
            if not data:
                return  # server closed
            now = time.monotonic()
            with self._cond:
                if last is not None:
                    self._note_gap(now - last, now)
                last = now
                self._chunks.append(data)
                self._buffered += len(data)
                max_buffer = max(4_000_000, int(self._bytes_per_s() * (2 * self._target_s + 0.5)))
                while self._buffered > max_buffer and len(self._chunks) > 1:
                    dropped = self._chunks.popleft()  # consumer too slow: drop the oldest data
                    self._buffered -= len(dropped)
                    self.dropped_bytes += len(dropped)
                self._cond.notify()


class RtlTcpSource(gr.sync_block):
    """complex64 source fed by an RtlTcpClient (u8 I/Q -> (x - 127.5) / 127.5)."""

    def __init__(self, host, port, sample_rate_hz=2_400_000):
        gr.sync_block.__init__(self, name="rtl_tcp_source", in_sig=None, out_sig=[np.complex64])
        self.client = RtlTcpClient(host, port, sample_rate_hz)
        self._carry = b""
        self.set_sample_rate(sample_rate_hz)

    # -- control ---------------------------------------------------------
    def set_frequency(self, hz):
        self.client.send_command(CMD_SET_FREQ, int(hz))

    def set_sample_rate(self, hz):
        self.client.send_command(CMD_SET_SAMPLE_RATE, int(hz))

    def set_direct_sampling(self, mode: int):
        self.client.send_command(CMD_SET_DIRECT_SAMPLING, int(mode))

    def set_gain_mode(self, auto: bool):
        """auto=True: tuner AGC + RTL AGC (mirrors SoapyRTLSDR setGainMode)."""
        self.client.send_command(CMD_SET_GAIN_MODE, 0 if auto else 1)
        self.client.send_command(CMD_SET_AGC_MODE, 1 if auto else 0)

    def set_gain(self, db):
        self.client.send_command(CMD_SET_GAIN, int(round(float(db) * 10)))

    @property
    def status(self):
        return self.client.status

    def stop(self):
        self.client.close()
        return True

    def __del__(self):
        try:
            self.client.close()
        except Exception:
            pass

    # -- stream ----------------------------------------------------------
    def work(self, input_items, output_items):
        out = output_items[0]
        data = self._carry + self.client.read(2 * len(out) - len(self._carry))
        n = len(data) // 2
        self._carry = data[2 * n:]
        if n == 0:
            return 0
        f = np.frombuffer(data, dtype=np.uint8, count=2 * n).astype(np.float32)
        f -= 127.5
        f *= 1.0 / 127.5
        out[:n] = f.view(np.complex64)
        return n
