"""JS8 received commands (Qt-free): decoded frames -> the directed commands JS8Call reacts to.

A port of how JS8Call turns decodes into its command queue (docs/js8/SPEC.md 7.1, mainwindow.cpp at the pinned
commit): heartbeat/CQ frames become commands to @HB/@ALLCALL (:4627-4683); a directed frame is a command
(:4701-4761), buffered on its audio offset while it waits for data frames (buffered commands like MSG, `>`,
QUERY) or for a compound callsign in a separate frame (`<....>`); data frames join the buffer within the
speed's rxThreshold (:4583-4595, :4812-4840); a FIRST frame drops an older buffer there (:4569-4581). Buffers
are grouped with their compound callsigns (processCompoundActivity, :9316-9414) and closed on their LAST frame,
with the checksum of checksummed commands verified (processBufferedActivity, :9416-9508): a buffer quiet for
60 s counts as ended, one quiet for 90 s is dropped."""
from dataclasses import dataclass

from pluto_tx import js8_message as M
from pluto_tx import js8_phy

BUFFER_LAST_AFTER_S = 60.0      # mainwindow.cpp:9435
BUFFER_DROP_AFTER_S = 90.0      # mainwindow.cpp:9441


@dataclass
class Js8Command:
    """JS8Call's CommandDetail."""
    from_call: str
    to: str
    cmd: str                    # with JS8Call's leading space, e.g. " SNR?", or ">"
    utc: float                  # wall clock of the (first) frame's period end
    submode: int
    offset_hz: float
    snr_db: float
    bits: int = 0
    extra: str = ""
    grid: str = ""
    text: str = ""
    relay_path: str = ""
    is_buffered: bool = False

    @property
    def is_last(self):
        return bool(self.bits & M.FLAG_LAST)


@dataclass
class _Compound:
    call: str
    grid: str
    bits: int
    utc: float


class _Buffer:
    def __init__(self):
        self.cmd = None             # Js8Command
        self.compound = []          # [_Compound]
        self.msgs = []              # [(text, bits, utc)]


class Js8CommandParser:
    def __init__(self):
        self._buffers = {}          # offset (Hz) -> _Buffer

    # --- decodes -> commands -------------------------------------------------------------------------------
    def feed(self, slot_start, submode, decodes):
        """Decodes (pluto_advanced_rx.js8_decoder.Js8Decode) of one period of one speed -> the commands that
        became complete, in JS8Call's order (compound grouping, buffered commands, then direct ones)."""
        now = slot_start + js8_phy.SUBMODES[submode]["period_s"]
        direct = []
        for d in decodes:
            direct.extend(self._on_decode(now, submode, d))
        return self._process_compound() + self._process_buffered(now) + direct

    def expire(self, now):
        """Buffers that end or drop on time alone (call about once per period of the fastest speed)."""
        return self._process_compound() + self._process_buffered(now)

    def open_buffer_offsets(self):
        return list(self._buffers)

    def has_open_buffer_to(self, calls):
        """hasExistingMessageBufferToMe (mainwindow.cpp:4797)."""
        return any(b.cmd is not None and b.cmd.to in calls for b in self._buffers.values())

    # --- mainwindow.cpp:4540-4795 ---------------------------------------------------------------------------
    def _on_decode(self, now, submode, d):
        info = M.decode_frame(d.frame, d.flags, submode)
        offset = float(round(d.freq_hz))
        bits = d.flags
        is_compound = bool(info["compound"])
        is_directed = len(info["directed"]) > 2
        out = []

        if bits & M.FLAG_FIRST:                                          # :4569-4581
            key = self._existing(submode, offset, move=True)
            if key is not None:
                del self._buffers[key]
        key = self._existing(submode, offset, move=True)                 # :4583-4595
        if key is not None and not is_compound and not is_directed:
            self._buffers[key].msgs.append((info["message"], bits, now))

        if is_compound and not is_directed:                              # :4611-4694
            if info["is_heartbeat"]:
                out.append(Js8Command(
                    from_call=info["compound"], to="@ALLCALL" if info["is_alt"] else "@HB",
                    cmd=" CQ" if info["is_alt"] else " HEARTBEAT", utc=now, submode=submode, offset_hz=offset,
                    snr_db=d.snr_db, bits=bits, grid=info["extra"],
                    text=info["message"] if info["is_alt"] else ""))
            else:
                self._existing(submode, offset, move=True)
                self._buffers.setdefault(offset, _Buffer()).compound.append(
                    _Compound(info["compound"], info["extra"], bits, now))

        if is_directed:                                                  # :4700-4761
            parts = info["directed"]
            cmd = Js8Command(from_call=parts[0], to=parts[1], cmd=parts[2], utc=now, submode=submode,
                             offset_hz=offset, snr_db=d.snr_db, bits=bits, extra=" ".join(parts[3:]))
            if ((M.is_command_buffered(cmd.cmd) and not cmd.is_last)
                    or "<....>" in (cmd.from_call, cmd.to)):
                self._existing(submode, offset, move=True)
                buf = self._buffers.setdefault(offset, _Buffer())
                buf.cmd = cmd
                buf.msgs = []
            else:
                out.append(cmd)
        return out

    def _existing(self, submode, offset, move):
        """hasExistingMessageBuffer (mainwindow.cpp:4812-4840): the buffer on this offset or within the speed's
        rxThreshold, moved to this offset (the signal drifted) -> its key, or None."""
        if offset in self._buffers:
            return offset
        tol = js8_phy.SUBMODES[submode]["rx_threshold"]
        near = [k for k in self._buffers if abs(k - offset) <= tol]
        if not near:
            return None
        prev = min(near, key=lambda k: abs(k - offset))
        if move:
            self._buffers[offset] = self._buffers.pop(prev)
            return offset
        return prev

    # --- mainwindow.cpp:9316-9414 ---------------------------------------------------------------------------
    def _process_compound(self):
        out = []
        for key in list(self._buffers):
            buf = self._buffers[key]
            if not buf.compound or buf.cmd is None:
                continue
            cmd = buf.cmd
            if cmd.from_call == "<....>" and cmd.to == "<....>" and len(buf.compound) < 2:
                continue
            if "<....>" in (cmd.from_call, cmd.to) and len(buf.compound) < 1:
                continue
            if cmd.from_call == "<....>":
                c = buf.compound.pop(0)
                cmd.from_call, cmd.grid, cmd.utc = c.call, c.grid, min(cmd.utc, c.utc)
                if c.bits & M.FLAG_LAST:
                    cmd.bits = c.bits
            if cmd.to == "<....>":
                c = buf.compound.pop(0)
                cmd.to, cmd.utc = c.call, min(cmd.utc, c.utc)
                if c.bits & M.FLAG_LAST:
                    cmd.bits = c.bits
            if not cmd.is_last:
                continue
            cmd.utc = min([cmd.utc] + [c.utc for c in buf.compound] + [m[2] for m in buf.msgs])
            out.append(cmd)
            del self._buffers[key]
        return out

    # --- mainwindow.cpp:9416-9508 ---------------------------------------------------------------------------
    def _process_buffered(self, now):
        out = []
        for key in list(self._buffers):
            buf = self._buffers[key]
            latest = max([now - 86400.0]
                         + ([buf.cmd.utc] if buf.cmd is not None else [])
                         + ([buf.compound[-1].utc] if buf.compound else [])
                         + ([buf.msgs[-1][2]] if buf.msgs else []))
            if now - latest > BUFFER_LAST_AFTER_S and buf.msgs:
                text, bits, utc = buf.msgs[-1]
                buf.msgs[-1] = (text, bits | M.FLAG_LAST, utc)
            if now - latest > BUFFER_DROP_AFTER_S:
                del self._buffers[key]
                continue
            if not buf.msgs or not buf.msgs[-1][1] & M.FLAG_LAST:
                continue
            message = M.rstrip("".join(m[0] for m in buf.msgs))
            valid = True
            if buf.cmd is not None and M.is_command_buffered(buf.cmd.cmd):
                size = M.is_command_checksummed(buf.cmd.cmd)
                if size == 32:
                    message = M.lstrip(message)
                    checksum, message = message[-6:], message[:len(message) - 7]
                    valid = M.checksum32_valid(checksum, message)
                elif size == 16:
                    message = M.lstrip(message)
                    checksum, message = message[-3:], message[:len(message) - 4]
                    valid = M.checksum16_valid(checksum, message)
            if valid and buf.cmd is not None:
                cmd = buf.cmd
                cmd.bits |= M.FLAG_LAST
                cmd.text = message
                cmd.is_buffered = True
                out.append(cmd)
            del self._buffers[key]
        return out
