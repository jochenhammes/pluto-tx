"""Minimal fake `rtl_tcp` server for tests: header + paced u8 I/Q tone stream, records
every 5-byte command. Like the real one it serves ONE client at a time."""
import socket
import struct
import threading
import time

import numpy as np


class FakeRtlTcpServer:
    def __init__(self, tone_hz=100_000.0, tuner=5, gains=29, send_header=True, magic=b"RTL0", burst_bytes=0):
        self.tone_hz = tone_hz
        self.burst_bytes = burst_bytes  # >0: send in blocks of this size like librtlsdr's 256 kB buffers
        self.tuner, self.gains, self.send_header, self.magic = tuner, gains, send_header, magic
        self.sample_rate = 1_024_000
        self.commands = []  # (cmd, param) in arrival order, across all clients
        self.connections = 0
        self._lock = threading.Lock()
        self._stop = False
        self._client = None
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self._srv.settimeout(0.2)
        self.port = self._srv.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.connections += 1
            self._client = conn
            try:
                self._handle(conn)
            except OSError:
                pass
            finally:
                self._client = None
                conn.close()

    def _handle(self, conn):
        if self.send_header:
            conn.sendall(self.magic + struct.pack(">II", self.tuner, self.gains))
        conn.settimeout(0.0)
        phase, buf, t_last, pending = 0, b"", time.monotonic(), b""
        while not self._stop:
            try:  # commands
                data = conn.recv(4096)
                if not data:
                    return
                buf += data
                while len(buf) >= 5:
                    cmd, param = struct.unpack(">BI", buf[:5])
                    buf = buf[5:]
                    with self._lock:
                        self.commands.append((cmd, param))
                    if cmd == 0x02:
                        self.sample_rate = param
            except BlockingIOError:
                pass
            if not self.send_header:
                time.sleep(0.01)
                continue
            n = int(self.sample_rate * (time.monotonic() - t_last))
            if n < 1024:
                time.sleep(0.002)
                continue
            t_last = time.monotonic()
            k = np.arange(n) + phase
            phase += n
            iq = np.exp(2j * np.pi * self.tone_hz / self.sample_rate * k)
            u8 = np.empty(2 * n, np.uint8)
            u8[0::2] = np.clip(127.5 + 100 * iq.real, 0, 255)
            u8[1::2] = np.clip(127.5 + 100 * iq.imag, 0, 255)
            pending += u8.tobytes()
            if len(pending) < self.burst_bytes:
                continue
            payload, pending = pending, b""
            try:
                conn.setblocking(True)
                conn.sendall(payload)
                conn.setblocking(False)
            except OSError:
                return

    def last_param(self, cmd):
        with self._lock:
            vals = [p for c, p in self.commands if c == cmd]
        return vals[-1] if vals else None

    def drop_client(self):
        c = self._client
        if c is not None:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def close(self):
        self._stop = True
        self.drop_client()
        self._srv.close()
