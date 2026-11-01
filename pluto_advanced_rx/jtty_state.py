"""The received JTTY messages as they grow: Qt-free and GNU-Radio-free, lock-guarded mutate / snapshot like the other
state classes, for the GUIs, Web-TRX and its simulator."""
import threading
import time

MAX_MESSAGES = 500


class JttyState:
    """The received messages, across flowgraph rebuilds: a row per message, updated while it grows."""

    def __init__(self, clock=time.time):
        self._lock = threading.Lock()
        self._clock = clock
        self._rows = {}             # (generation, codec id) -> row; a new process counts ids from 1 again
        self._generation = 0
        self._changed = set()
        self._version = 0

    def new_generation(self):
        with self._lock:
            self._generation += 1

    def on_update(self, update):
        text = update.get("text") or ""
        with self._lock:
            key = (self._generation, update["id"])
            row = self._rows.get(key)
            if row is None:
                if not text:  # nothing to show yet: a row appears with the first text
                    return
                start = update.get("utc_start") or self._clock()
                row = self._rows[key] = dict(id=f"{self._generation}-{update['id']}", time=start, direction="rx",
                                             utc=time.strftime("%H%M%S", time.gmtime(start)), snr=update.get("snr"))
            row.update(freq=update.get("freq"), text=text or row.get("text", ""), state=update.get("state", "growing"), gaps=list(update.get("gaps") or []))
            self._changed.add(key)
            while len(self._rows) > MAX_MESSAGES:
                oldest = next(iter(self._rows))
                del self._rows[oldest]
                self._changed.discard(oldest)
            self._version += 1

    def add_sent(self, text, tone_hz):
        """Our own message in the same list; not decoded, so it is not claimed as received."""
        now = self._clock()
        with self._lock:
            key = ("tx", now)
            self._rows[key] = dict(id=f"tx-{now:.3f}", time=now, direction="tx", utc=time.strftime("%H%M%S", time.gmtime(now)),
                                   snr=None, freq=float(tone_hz), text=text, state="complete", gaps=[])
            self._changed.add(key)
            self._version += 1

    @property
    def version(self):
        with self._lock:
            return self._version

    def get_rows(self):
        with self._lock:
            return [dict(r) for r in self._rows.values()]

    def take_changed(self):
        with self._lock:
            keys, self._changed = self._changed, set()
            return [dict(self._rows[k]) for k in keys if k in self._rows]

    def clear(self):
        with self._lock:
            self._rows.clear()
            self._changed.clear()
            self._version += 1
