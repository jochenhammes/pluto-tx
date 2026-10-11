"""The host side of the transmit protocol (firmware/protocol/transmit.h): set up a session, stream records, read the chip's status."""
import collections
import queue
import struct
import threading
import time
import zlib

import numpy as np

from . import nb

TX_OP_LO, TX_OP_RATE, TX_OP_DRIFT, TX_OP_LIMIT, TX_OP_BEGIN, TX_OP_END, TX_OP_TEMP = 40, 41, 42, 43, 44, 45, 46
TX_MIN_HZ, TX_MAX_HZ = 2_320_000_000, 2_450_000_000
TX_LOW_MARGIN = 48
TX_STATUS_MAGIC, TX_STATUS_BYTES = 0xB6, 8
TX_RING_RECORDS = 3 * 65536 // 4
TX_PREFILL = 4000
END_REASONS = {0: "finished", 1: "the chip stopped because no data arrived for 500 ms", 2: "the longest session was reached",
               3: "the receiver could not be set up again"}
PLL_STEP_HZ = 30e6 / 65536


class TxError(Exception):
    pass


def lo_word(hz):
    """The PLL word the firmware programs for an LO of `hz` (esp32s3_plan_lo, normal conversion): LO = 30 MHz * (32 + W / 65536)."""
    scaled = (hz * 65536 + 15_000_000) // 30_000_000
    return scaled - 32 * 65536


def word_hz(word):
    """The LO a PLL word really programs (the inverse of lo_word): 30 MHz * (32 + W / 65536); it can differ from the requested frequency by up to 229 Hz."""
    return 30e6 * (32 + word / 65536)


def choose_lo(hz, max_offset_steps=44):
    """Moves the requested LO by the smallest amount that puts the low byte of the PLL word into 48 .. 207, where the offsets of a
    modulated signal cannot carry into the next byte. Returns (lo_hz in steps of 100 Hz, shift in Hz)."""
    hz = int(round(hz / 100.0)) * 100
    for d in range(0, 400):
        for sign in (1, -1):
            cand = hz + sign * d * 100
            low = lo_word(cand) & 0xFF
            if TX_LOW_MARGIN <= low <= 255 - TX_LOW_MARGIN:
                return cand, cand - hz
    raise TxError("no usable LO frequency near the request")


def plan_lo(center_hz, ppm=0.0, reserve_hz=0.0):
    """Where to put the LO for a signal centred on `center_hz`, and what the records have to add. The PLL word's low byte must leave room
    for the modulation (`choose_lo`), which moves the LO by up to 22 kHz; a constant in every record (`static_hz`) makes up for that
    move. The records reach +-19.2 kHz (`txmodes.MAX_Q4`), of which `reserve_hz` is kept free for the signal's own deviation. `ppm` is the
    board's crystal error (positive: the board transmits too high), as for `--ppm`.

    Returns a dict: `lo_hz` (the LO to give the chip, in steps of 100 Hz), `static_hz` (a whole number of record units), `center_hz` (the
    centre the signal really gets), `miss_hz` (that against the wanted one, nonzero only when the move was too large for the records)."""
    from . import txmodes
    k = 1.0 + ppm * 1e-6
    lo_hz, _ = choose_lo(center_hz / k)
    lo_real = word_hz(lo_word(lo_hz)) * k
    unit = txmodes.Q4_HZ * k                                   # one record unit on the air
    limit = txmodes.MAX_Q4 - int(np.ceil(reserve_hz / unit))
    static_q4 = int(round((center_hz - lo_real) / unit))
    static_q4 = min(max(static_q4, -limit), limit)
    real = lo_real + static_q4 * unit
    return dict(lo_hz=lo_hz, static_hz=static_q4 * txmodes.Q4_HZ, center_hz=real, miss_hz=real - center_hz)


class Frames:
    """Splits the chip's output during a session into status frames and the final response."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        events = []
        i = 0
        b = self.buf
        while i < len(b):
            if b[i] == TX_STATUS_MAGIC:
                if i + TX_STATUS_BYTES > len(b):
                    break
                f = b[i:i + TX_STATUS_BYTES]
                if sum(f) & 0xFF == 0:
                    events.append(("status", dict(seq=f[1], fill=f[2] | f[3] << 8, underruns=f[4], late=f[5], carrier=bool(f[6] & 1))))
                    i += TX_STATUS_BYTES
                    continue
            elif b[i] == nb.RESP_MAGIC:
                if i + 16 > len(b):
                    break
                raw = bytes(b[i:i + 16])
                if struct.unpack("<I", raw[12:16])[0] == zlib.crc32(raw[:12]):
                    _, _, op, status, _seq, _, value = struct.unpack("<BBBBHHI", raw[:12])
                    events.append(("response", dict(op=op, status=status, value=value)))
                    i += 16
                    continue
            i += 1
        del b[:i]
        return events


class Session:
    """One transmission: configure, begin, stream, end."""

    def __init__(self, link):
        self.link = link
        self.ser = link.ser
        self.status = None          # last status frame
        self.summary = None

    def configure(self, lo_hz, rate, drift_hz=0, limit_s=600):
        c = self.link.command
        c(TX_OP_LO, int(round(lo_hz / 100.0)), allow=(nb.CTL_OK,))
        c(TX_OP_RATE, int(rate))
        c(TX_OP_DRIFT, int(drift_hz) & 0xFFFF)
        c(TX_OP_LIMIT, int(limit_s))

    def chip_temperature(self):
        """The chip's temperature in degrees C from its sensor (between sessions only). Range 2 is the SDK's default, range 1 takes over when it saturates."""
        for rng in (2, 1):
            _, v = self.link.command(TX_OP_TEMP, rng)
            idx, raw = v >> 24, (v & 0xFFFFFF) / 16.0
            c = 0.4386 * raw - 27.88 * (idx - 2) - 20.52
            if c < 70:
                return c
        return c

    def begin(self, expect_word=None):
        try:
            status, word = self.link.command(TX_OP_BEGIN, 0, allow=(nb.CTL_OK,))
        except nb.CommandError as e:
            raise TxError({nb.CTL_BAD_ARGUMENT: "the chip refused the frequency or the settings",
                           nb.CTL_NOT_READY: "the receiver of the chip is not ready",
                           nb.CTL_FAILED: "the PLL did not lock at this frequency (try another)"}.get(e.status, f"the chip answered status {e.status}"))
        if expect_word is not None and word != expect_word:
            raise TxError(f"the chip programmed PLL word 0x{word:06X}, expected 0x{expect_word:06X}")
        return word

    def stream(self, records, target_fill=8000, on_status=None, stop=None, idle_s=0.002):
        """records: a queue.Queue of numpy uint32 arrays; None ends the transmission. stop: a threading.Event that ends it too.
        Returns the chip's summary dict."""
        self.ser.timeout = idle_s
        self.ser.write_timeout = 1.0

        def source():
            try:
                return records.get_nowait()
            except queue.Empty:
                return False

        pump = _Pump(self, source, target_fill, on_status, stop)
        while True:
            summary = pump.step()
            if summary is not None:
                return summary


class _Pump:
    """The work of one transmission, one non-blocking step at a time: read the chip's frames, keep its ring near `target_fill` records,
    send the end record, wait for the summary. `source()` returns a numpy array of records, None when the data has ended, or False when
    there is nothing right now. Shared by `Session.stream` (the caller's thread) and `Streamer` (a thread of its own)."""

    def __init__(self, session, source, target_fill=8000, on_status=None, stop=None):
        self.session, self.ser, self.source = session, session.ser, source
        self.target_fill, self.on_status, self.stop = target_fill, on_status, stop
        self.frames = Frames()
        self.pending = None
        self.sent_since = 0
        self.fill = 0
        self.ending = False
        self.end_sent = False
        self.t_last = time.monotonic()
        self.end_by = None
        self._abort = False

    def abort(self):
        """From any thread: drop what has not been sent and end the transmission at once (the end record goes out regardless of room)."""
        self._abort = True

    def step(self):
        """One round: returns the chip's summary dict when the transmission is over, otherwise None. Raises TxError."""
        import serial
        if self._abort:
            self.pending = None
            self.ending = True
            self._abort = False
        ser = self.ser
        data = ser.read(ser.in_waiting or 1)
        if data:
            for kind, ev in self.frames.feed(data):
                if kind == "status":
                    self.session.status = ev
                    self.fill, self.sent_since = ev["fill"], 0
                    if self.on_status:
                        self.on_status(ev)
                elif ev["op"] == TX_OP_END:
                    v = ev["value"]
                    self.session.summary = dict(reason=v >> 24, underruns=(v >> 16) & 0xFF, late=v & 0xFFFF, status=ev["status"])
                    return self.session.summary
        now = time.monotonic()
        if not self.ending and self.stop is not None and self.stop.is_set():
            self.ending = True
        if not self.end_sent:
            room = self.target_fill - self.fill - self.sent_since
            if room >= 256 or (self.ending and self.pending is None):
                if self.pending is None and not self.ending:
                    item = self.source()
                    if item is None:
                        self.ending = True
                    elif item is not False:
                        self.pending = item
                if self.pending is not None and len(self.pending):
                    n = min(room, len(self.pending), 4096)
                    try:
                        ser.write(self.pending[:n].astype("<u4").tobytes())
                    except serial.SerialTimeoutException:
                        raise TxError("the chip does not take data any more (it stopped the transmission?)")
                    self.sent_since += n
                    self.pending = self.pending[n:] if n < len(self.pending) else None
                    self.t_last = now
                elif self.ending and self.pending is None:
                    import numpy as np
                    code = 127
                    ser.write(np.array([(code << 16) | (1 << 24)], dtype="<u4").tobytes())
                    self.end_sent = True
                    self.end_by = now + 5.0
        if self.end_by and now > self.end_by:
            raise TxError("the chip did not confirm the end of the transmission")
        if now - self.t_last > 5.0 and not self.end_sent and self.fill == 0 and self.session.status is None:
            raise TxError("the chip sends no status; is the transmitter firmware running?")
        return None


class Streamer:
    """A transmission driven by pushing: `push(records)` from any thread (it blocks while more than `max_buffered` records wait, so the
    caller runs at the chip's pace), `finish()` ends it with the end record after everything pushed has played, `abort()` ends it at
    once, `close()` always returns. A thread of its own talks to the chip, so the caller needs no loop. Start it after `Session.begin()`.
    The carrier comes on when the chip's ring holds `TX_PREFILL` records (100 ms at 40000 a second), so the first 100 ms of what is
    pushed is the latency from key to carrier. After `Session.begin()` the first 4000 records must arrive within 3 s, or the chip ends the
    session without a carrier. `abort()` cannot be faster than the chip's ring: the end record queues behind what the chip holds
    (`target_fill` records, 125 ms at the default), so an abort takes up to about that long to silence the carrier."""

    def __init__(self, session, target_fill=5000, on_status=None, max_buffered=16384, idle_s=0.002):
        self.session = session
        self.max_buffered = max_buffered
        self.error = None
        self.summary = None
        self._cv = threading.Condition()
        self._buf = collections.deque()
        self._n = 0
        self._ending = False
        self._dead = False
        session.ser.timeout = idle_s
        session.ser.write_timeout = 1.0
        self._pump = _Pump(session, self._source, target_fill, on_status)
        self._thread = threading.Thread(target=self._run, name="espdr-streamer", daemon=True)
        self._thread.start()

    # -- the thread of its own
    def _source(self):
        with self._cv:
            if self._buf:
                a = self._buf.popleft()
                self._n -= len(a)
                self._cv.notify_all()
                return a
            return None if self._ending else False

    def _run(self):
        try:
            while True:
                summary = self._pump.step()
                if summary is not None:
                    self.summary = summary
                    return
        except Exception as e:                  # noqa: BLE001 - handed to the caller by finish() and push()
            self.error = e
        finally:
            with self._cv:
                self._dead = True
                self._buf.clear()
                self._n = 0
                self._cv.notify_all()

    # -- the caller's side
    @property
    def alive(self):
        return not self._dead

    @property
    def fill(self):
        """The chip's ring fill in records, as of its last status frame."""
        return self._pump.fill

    def push(self, records, timeout=None):
        """Queues records (numpy uint32). Blocks while the queue is full. Returns False if they were not taken (timeout, or the
        transmission is over or ending)."""
        import numpy as np
        a = np.asarray(records, dtype=np.uint32)
        if not len(a):
            return True
        end = None if timeout is None else time.monotonic() + timeout
        with self._cv:
            while self._n >= self.max_buffered and not self._dead and not self._ending:
                left = None if end is None else end - time.monotonic()
                if left is not None and left <= 0:
                    return False
                self._cv.wait(left if left is not None else 0.1)
            if self._dead or self._ending:
                return False
            self._buf.append(a)
            self._n += len(a)
        return True

    def finish(self, timeout=10.0):
        """Ends the transmission after everything pushed has been played; returns the chip's summary dict. Raises TxError if the
        chip does not confirm within `timeout` seconds or the transmission broke down before."""
        with self._cv:
            self._ending = True
            self._cv.notify_all()
        self._thread.join(timeout)
        if self._thread.is_alive():
            self.abort()
            self._thread.join(2.0)
            raise TxError("the chip did not confirm the end of the transmission in time")
        if self.error is not None:
            raise self.error if isinstance(self.error, TxError) else TxError(str(self.error))
        return self.summary

    def abort(self):
        """Ends the transmission at once; what has not been sent is dropped. Returns immediately (see close())."""
        self._pump.abort()
        with self._cv:
            self._ending = True
            self._buf.clear()
            self._n = 0
            self._cv.notify_all()

    def close(self, timeout=5.0):
        """Aborts if still running and waits for the thread, at most `timeout` seconds. Never raises. Returns the summary or None."""
        if self._thread.is_alive():
            self.abort()
            self._thread.join(timeout)
        return self.summary
