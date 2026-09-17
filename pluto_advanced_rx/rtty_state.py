"""RX-side persistent RTTY chat transcript state -- structural mirror of
psk31_state.py's Psk31ChatState (see its own docstring for the full
rationale: Qt/GNU-Radio-free, owned once by MainWindow, survives
AdvancedRxFlowgraph rebuilds, lock-guarded mutate/lock-free-snapshot
idiom).

rtty_deframer.RTTYBaudotDeframer calls on_char() directly from its own
work() method, on the GNU Radio scheduler thread; the GUI polls
get_snapshot() from the Qt thread."""
import threading

MAX_TRANSCRIPT_CHARS = 20_000


class RttyChatState:
    def __init__(self):
        self._lock = threading.Lock()
        self._text = ""

    def on_char(self, char: str):
        """char is a single decoded Baudot character. Real RTTY traffic
        conventionally uses CR ('\\r') and/or LF ('\\n') as end-of-line
        (Baudot's own LTRS table has both as distinct codes) -- both are
        normalized to a single '\\n' here so a transcript reads naturally
        as text."""
        with self._lock:
            if char in ("\r", "\n"):
                self._text += "\n"
            else:
                self._text += char
            if len(self._text) > MAX_TRANSCRIPT_CHARS:
                self._text = self._text[-MAX_TRANSCRIPT_CHARS:]

    def get_snapshot(self):
        with self._lock:
            return self._text

    def clear(self):
        with self._lock:
            self._text = ""
