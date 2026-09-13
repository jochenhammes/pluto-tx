"""RX-side persistent PSK31 chat transcript state: a single rolling text
buffer, built up one decoded Varicode character at a time. Deliberately
Qt/GNU-Radio-free (same separation as filebroadcast_state.py/fft_probe.py)
-- owned by MainWindow (gui.py), constructed ONCE at startup, so it survives
AdvancedRxFlowgraph rebuilds (bandwidth changes) by construction, exactly
like FileBroadcastState's own survives-a-rebuild relationship.

psk31_deframer.PSK31VaricodeDeframer calls on_char() directly from its own
work() method, on the GNU Radio scheduler thread; the GUI polls
get_snapshot() from the Qt thread. _lock guards every access from both
sides -- same "mutate under the lock, return a lock-free plain-data copy"
idiom already proven by FileBroadcastState.get_snapshot()."""
import threading

# Rolling cap on the transcript's length -- avoids unbounded memory growth
# over a long receive session; old text simply scrolls off, matching how a
# real terminal-style chat log behaves.
MAX_TRANSCRIPT_CHARS = 20_000


class Psk31ChatState:
    def __init__(self):
        self._lock = threading.Lock()
        self._text = ""

    def on_char(self, char: str):
        """char is a single decoded Varicode character. Real PSK31 traffic
        from any standard station (fldigi etc., not just this app's own
        TX side) conventionally uses CR ('\\r', chr(13) -- occasionally LF
        too) as end-of-line and BS ('\\x08', chr(8)) for backspace/
        correction -- both are handled here so a transcript received from
        a real remote station reads naturally as text, not as raw control
        bytes embedded in the line."""
        with self._lock:
            if char in ("\r", "\n"):
                self._text += "\n"
            elif char == "\x08":
                # Trim the last character of the CURRENT line only -- never
                # eat back across an already-committed newline.
                if self._text and self._text[-1] != "\n":
                    self._text = self._text[:-1]
            else:
                self._text += char
            if len(self._text) > MAX_TRANSCRIPT_CHARS:
                self._text = self._text[-MAX_TRANSCRIPT_CHARS:]

    def get_snapshot(self):
        """Plain string, safe to use outside the lock (matches
        FileBroadcastState.get_snapshot()'s own always-a-fresh-value
        contract -- str is immutable, so this needs no explicit copy)."""
        with self._lock:
            return self._text

    def clear(self):
        with self._lock:
            self._text = ""
