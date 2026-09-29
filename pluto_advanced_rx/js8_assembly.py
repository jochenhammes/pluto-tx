"""JS8 multi-frame assembly and station list (Qt-free).

A transmission is a run of frames in consecutive periods of one speed on one audio offset, from a frame
flagged FIRST to one flagged LAST (docs/js8/SPEC.md 3.1). Frames belong to the same transmission when
their offsets differ by at most JS8Call's rxThreshold for the speed (10/16/32/10 Hz; mainwindow.cpp:4814,
SPEC 3.3). A transmission that misses frames is kept but marked incomplete: a gap of one or more periods
inserts GAP_MARK, and one that hears nothing for MISSING_SLOTS_TIMEOUT periods without its LAST frame is
closed with GAP_MARK at the end. The text is the concatenation of what JS8Call displays per frame
(js8_message.decode_frame / DecodedText)."""
import itertools
import re
from dataclasses import dataclass, field

from pluto_tx import js8_phy

GAP_MARK = " … "
MISSING_SLOTS_TIMEOUT = 2
_SENDER_RE = re.compile(r"^([A-Z0-9/@<>.]+):")


def rx_threshold_hz(submode):
    return js8_phy.SUBMODES[submode]["rx_threshold"]


@dataclass
class Js8Message:
    id: int
    submode: int
    freq_hz: float
    first_slot: float
    last_slot: float
    parts: list = field(default_factory=list)       # displayed text per frame (GAP_MARK for gaps)
    complete: bool = False                           # LAST frame seen (may still be incomplete)
    incomplete: bool = False                         # a frame is missing somewhere
    closed: bool = False
    snr_db: float = -99.0
    sender: str = ""
    to: str = ""

    @property
    def text(self):
        return "".join(self.parts).strip()


@dataclass
class Js8Station:
    call: str
    grid: str = ""
    snr_db: float = -99.0
    freq_hz: float = 0.0
    submode: int = js8_phy.NORMAL
    last_heard: float = 0.0
    last_text: str = ""


class Js8Assembler:
    def __init__(self):
        self._ids = itertools.count(1)
        self._open = []                  # open Js8Message objects
        self.stations = {}               # call -> Js8Station

    def feed(self, slot_start, submode, decodes):
        """Decodes of one period of one speed -> list of messages that changed (open or just closed)."""
        changed = []
        period = js8_phy.SUBMODES[submode]["period_s"]
        tol = rx_threshold_hz(submode)
        for d in sorted(decodes, key=lambda d: d.freq_hz):
            self._note_station(slot_start, d)
            msg = self._match(submode, d.freq_hz, tol)
            if d.is_first or msg is None:
                if msg is not None:                           # a new FIRST ends the previous one
                    self._close(msg, incomplete=not msg.complete)
                    changed.append(msg)
                msg = Js8Message(next(self._ids), submode, d.freq_hz, slot_start, slot_start)
                if not d.is_first:
                    msg.parts.append(GAP_MARK.lstrip())       # we joined in the middle
                    msg.incomplete = True
                self._open.append(msg)
            elif slot_start - msg.last_slot > period + 0.5:
                msg.parts.append(GAP_MARK)
                msg.incomplete = True
            msg.parts.append(d.text)
            msg.last_slot = slot_start
            msg.freq_hz = d.freq_hz
            msg.snr_db = max(msg.snr_db, d.snr_db)
            if not msg.sender:
                m = _SENDER_RE.match(d.text)
                if m:
                    msg.sender = m.group(1)
                    rest = d.text[m.end():].split()
                    msg.to = rest[0] if rest else ""
            if d.is_last:
                msg.complete = True
                self._close(msg, incomplete=msg.incomplete)
            if msg not in changed:
                changed.append(msg)
        return changed

    def expire(self, now):
        """Close transmissions that missed MISSING_SLOTS_TIMEOUT periods without their LAST frame."""
        closed = []
        for msg in list(self._open):
            period = js8_phy.SUBMODES[msg.submode]["period_s"]
            if now - (msg.last_slot + period) >= MISSING_SLOTS_TIMEOUT * period:
                msg.parts.append(GAP_MARK.rstrip())
                self._close(msg, incomplete=True)
                closed.append(msg)
        return closed

    def open_messages(self):
        return list(self._open)

    def _match(self, submode, freq, tol):
        best = None
        for msg in self._open:
            if msg.submode == submode and abs(msg.freq_hz - freq) <= tol:
                if best is None or abs(msg.freq_hz - freq) < abs(best.freq_hz - freq):
                    best = msg
        return best

    def _close(self, msg, incomplete):
        msg.incomplete = incomplete
        msg.closed = True
        if msg in self._open:
            self._open.remove(msg)

    def _note_station(self, slot_start, d):
        m = _SENDER_RE.match(d.text)
        if not m:
            return
        call = m.group(1)
        if call.startswith("@") or call == "<....>":
            return
        st = self.stations.get(call) or Js8Station(call)
        st.snr_db, st.freq_hz, st.submode, st.last_heard = d.snr_db, d.freq_hz, d.submode, slot_start
        st.last_text = d.text.strip()
        grid = re.search(r"\b([A-R]{2}[0-9]{2})\s*$", d.text.strip())
        if grid and ("HEARTBEAT" in d.text or "CQ" in d.text):
            st.grid = grid.group(1)
        self.stations[call] = st
