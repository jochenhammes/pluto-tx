"""RX-side persistent POCSAG traffic state -- structural mirror of meshtastic_state.py (Qt/GNU-Radio-free,
owned once by MainWindow, survives AdvancedRxFlowgraph rebuilds, lock-guarded mutate / snapshot for the Qt
poll timer). Rows keep the raw decoded payload; the text is rendered at snapshot time so changing the
interpretation (auto/alpha/numeric) or the charset re-renders the whole table."""
import threading
import time

from pluto_tx import pocsag_codec

MAX_ROWS = 500


class PocsagState:
    def __init__(self):
        self._lock = threading.Lock()
        self._rows = []
        self._version = 0
        self.interpretation = "auto"
        self.charset = "ascii"
        self.hide_damaged = True  # calls with unrecoverable codewords (weak/noisy signal) are usually junk

    @property
    def version(self):
        with self._lock:
            return self._version

    def set_interpretation(self, interpretation):
        with self._lock:
            self.interpretation = interpretation
            self._version += 1

    def set_hide_damaged(self, hide):
        with self._lock:
            self.hide_damaged = bool(hide)
            self._version += 1

    def set_charset(self, charset):
        with self._lock:
            self.charset = charset
            self._version += 1

    def on_message(self, msg):
        with self._lock:
            self._rows.append(dict(msg))
            del self._rows[:-MAX_ROWS]
            self._version += 1

    def counts(self):
        """-> (calls received, of which damaged, calls hidden in the table)"""
        with self._lock:
            damaged = sum(1 for m in self._rows if m["uncorrectable"])
            return len(self._rows), damaged, damaged if self.hide_damaged else 0

    def clear(self):
        with self._lock:
            self._rows.clear()
            self._version += 1

    def get_snapshot(self):
        """-> (version, rows); each row: time (HH:MM:SS), baud, ric, function, text, corrected, uncorrectable."""
        with self._lock:
            interp, charset, rows = self.interpretation, self.charset, list(self._rows)
            hide = self.hide_damaged
            version = self._version
        out = []
        for m in rows:
            if hide and m["uncorrectable"]:
                continue
            out.append(dict(
                time=time.strftime("%H:%M:%S", time.localtime(m["time"])), baud=m["baud"], ric=m["ric"],
                function=m["function"], text=pocsag_codec.decode_message(m, interp, charset),
                corrected=m["corrected"], uncorrectable=m["uncorrectable"], polarity=m["polarity"],
            ))
        return version, out
