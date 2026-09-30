"""JS8 automation (J9, Qt-free): automatic replies, heartbeat, relay and inbox, ported from JS8Call
(docs/js8/SPEC.md 7, mainwindow.cpp at the pinned commit), plus pluto-tx' own limits (SPEC 7.8).

Js8Auto gets the commands pluto_advanced_rx.js8_commands.Js8CommandParser recognised and decides what JS8Call
would reply (process_commands, a port of processCommandActivity :9510-10441). Replies, heartbeats and HB-ACKs
go through a prioritised queue (processTxQueue :10727-10792); the caller asks next_transmission() whether a
message may go out now, sends it like a typed message and reports it back with transmitted(). Nothing here
touches a radio.

Safety (SPEC 7.5, 7.8): everything starts off; the idle watchdog (no operator activity for idle_watchdog_min)
switches autoreply, heartbeat and the queue off like JS8Call's tx_watchdog (:12291); at most
max_auto_per_hour automatic transmissions in any 60 minutes (pluto-tx); the caller also calls
operator_gone() when no operator is connected any more, which does the same at once."""
import collections
import dataclasses
import itertools
import random
import re
import time

from . import js8_message as M
from . import js8_phy
from .js8_inbox import base_callsign

PRIORITY_LOW = 10               # mainwindow.h:654-658
PRIORITY_NORMAL = 100
PRIORITY_HIGH = 1000
HB_PRIORITY = PRIORITY_LOW + 1  # sendHB :6998, sendHeartbeatAck :7023
ALLCALL_TIMEOUT_S = 15 * 60     # :9797-9798 (secsTo / 60 < 15)
LOW_PRIORITY_GAP_S = 30.0       # :10764-10766
BAND_FREE_S = 30.0              # isFreqOffsetFree :6258
HB_BAND = (500, 1000, 50)       # sendHB :6990, sendHeartbeatAck :7020
HB_INTERVALS_MIN = (10, 15, 30, 60)   # buildRepeatMenu :6891-6911 (heartbeat: no 1/5 min); custom 1..1440
PLUTO_VERSION = "PLUTO-TX"      # <MYVERSION> (JS8Call puts its own version there)
HB_LEAD_S = 4.0                 # queue the heartbeat this early (pluto-tx keys a message JS8_KEY_EARLY_S=3 s ahead)

# mainwindow.cpp:9908 (callToPattern) and :10666 (callDePattern)
_CALLSIGN = (r"(?P<callsign>\b(?P<prefix>[A-Z0-9]{1,4}/)?(?P<base>([0-9A-Z])?([0-9A-Z])([0-9])([A-Z])?([A-Z])?"
             r"([A-Z])?)(?P<suffix>/[A-Z0-9]{1,4})?)")
_CALL_TO_RE = re.compile(r"^(?P<callsign>\b(?P<prefix>[A-Z0-9]{1,4}/)?(?P<base>([0-9A-Z])?([0-9A-Z])([0-9])([A-Z])?"
                         r"([A-Z])?([A-Z])?)(?P<suffix>/[A-Z0-9]{1,4})?(?P<type>[> ]))\b")
_CALL_DE_RE = re.compile(r"\s([*]DE[*]|VIA)\s" + _CALLSIGN + r"\b")


@dataclasses.dataclass
class Js8AutoConfig:
    mycall: str
    grid: str = ""
    groups: tuple = ()                      # MyGroups (default none)
    autoreply: bool = False                 # JS8Call starts with it on; pluto-tx: off (SPEC 7.8)
    confirm: bool = True                    # AutoreplyConfirmation (default on)
    confirm_timeout_s: float = 90.0         # confirmThenEnqueueMessage(90, ...)
    hb_mode: bool = False                   # "HB" mode (actionModeJS8HB), default off
    hb_interval_min: int = 0                # 0 = heartbeat timer off
    hb_ack: bool = False                    # actionHeartbeatAcknowledgements, default off
    hb_qso_pause: bool = True               # HeartbeatQSOPause
    hb_anywhere: bool = False               # BeaconAnywhere
    relay: bool = False                     # JS8Call: RelayOFF=false (relay on); pluto-tx: off (SPEC 7.8)
    avoid_allcall: bool = False             # AvoidAllcall
    info: str = ""                          # MyInfo
    status: str = "IDLE <MYIDLE> VERSION <MYVERSION>"   # MyStatus
    whitelist: tuple = ()
    blacklist: tuple = ()
    hb_blacklist: tuple = ()
    callsign_aging_min: int = 0             # CallsignAging (0 = never)
    idle_watchdog_min: int = 60             # TxIdleWatchdog
    max_auto_per_hour: int = 20             # pluto-tx (plan section 0: at most 20 in a row)


@dataclasses.dataclass
class QueuedMessage:
    """PrioritizedMessage: text as typed into JS8Call's message box (the caller builds the frames)."""
    id: int
    created: float
    priority: int
    text: str
    offset_hz: float                        # -1: the operator's current offset
    reason: str                             # what caused it (command, "heartbeat", ...)
    on_sent: object = None                  # JS8Call's Callback, run once the message went out
    expires: float = 0.0                    # confirmations only


@dataclasses.dataclass
class _Heard:
    call: str
    snr_db: float
    utc: float
    grid: str = ""


def format_snr(snr):
    return M.format_snr(int(round(snr)))


def since(seconds):
    """since() (mainwindow.cpp:127-140) for a delta in seconds."""
    d = int(seconds)
    if d >= 86400:
        return f"{d // 86400}d"
    if d >= 3600:
        return f"{d // 3600}h"
    if d >= 60:
        return f"{d // 60}m"
    if d >= 15:
        return f"{d - d % 15}s"
    return "now"


def parse_relay_path(from_call, text):
    """parseRelayPathCallsigns (mainwindow.cpp:10663-10675): FROM plus every ` *DE* X` / ` VIA X` in the text,
    the last one first."""
    calls = [m.group("callsign") for m in _CALL_DE_RE.finditer(text)]
    return [from_call] + calls[::-1]


class Js8Auto:
    def __init__(self, config, inbox=None, clock=time.time, rng=None):
        self.config = config
        self.inbox = inbox
        self.clock = clock
        self.rng = rng or random.Random()
        self._ids = itertools.count(1)
        self.heard = {}                     # m_callActivity: call -> _Heard
        self.allcall_cache = {}             # m_txAllcallCommandCache: call -> time
        self.band_activity = {}             # m_bandActivity: offset -> last time heard
        self.queue = []                     # m_txMessageQueue
        self.pending = []                   # confirmThenEnqueueMessage dialogs
        self.last_tx_message = ""           # m_lastTxMessage
        self.last_tx_start = 0.0            # m_lastTxStartTime
        self.last_activity = clock()        # idle timer (reset by operator activity)
        self.watchdog = False               # m_tx_watchdog
        self.operator_draft = False         # text in the message box (blocks replies and the queue)
        self.selected_call = ""             # callsignSelected()
        self.hb_next = None                 # TxLoop m_next_activity (None = loop inactive)
        self.submode = js8_phy.NORMAL
        self._auto_sent = collections.deque()
        self.log = []                       # human-readable decisions, newest last

    # --- configuration / operator -----------------------------------------------------------------------------
    @property
    def mycalls(self):
        return (self.config.mycall.strip(), base_callsign(self.config.mycall).strip())

    def configure(self, **changes):
        hb_before = (self.config.hb_interval_min, self.config.hb_mode)
        self.config = dataclasses.replace(self.config, **changes)
        if (self.config.hb_interval_min, self.config.hb_mode) != hb_before:
            self._restart_hb(self.clock())

    def set_submode(self, submode):
        """TxLoop::onModeChange: a new speed restarts the heartbeat schedule on its period."""
        if submode != self.submode:
            self.submode = submode
            self._restart_hb(self.clock())

    def operator_active(self, now=None):
        """A key press / click: resets the idle timer and lifts a triggered watchdog (resetIdleTimer + tx_watchdog
        (false), mainwindow.cpp:3477-3478)."""
        self.last_activity = self.clock() if now is None else now
        if self.watchdog:
            self.watchdog = False
            self._note("operator back: automation may send again (switches were turned off by the watchdog)")

    def operator_gone(self, reason="no operator connected"):
        """pluto-tx: nobody watches any more -> like the watchdog, at once."""
        return self._trigger_watchdog(reason)

    # --- received commands (processCommandActivity, mainwindow.cpp:9510-10441) -------------------------------
    def note_decode(self, offset_hz, now=None):
        """Every decoded frame's offset (m_bandActivity, for the free-offset search)."""
        self.band_activity[round(offset_hz)] = self.clock() if now is None else now

    def process_commands(self, commands, now=None, open_buffer_to_me=False):
        now = self.clock() if now is None else now
        queue = collections.deque(commands)
        events = []
        while queue:
            d = queue.popleft()
            events.extend(self._process_one(d, now, open_buffer_to_me, queue))
        return events

    def _process_one(self, d, now, open_buffer_to_me, queue):
        cfg = self.config
        if "<....>" in (d.from_call, d.to):                                   # :9541
            return []
        if not M.is_command_allowed(d.cmd):                                    # :9546
            return []
        to_me = d.to in self.mycalls                                           # :9551
        is_allcall = "@ALLCALL" in d.to or "@HB" in d.to                       # :9066
        is_group = d.to in cfg.groups                                          # :9070
        self._heard(d.from_call, d.snr_db, d.utc, d.grid)                      # :9556-9569
        if d.cmd == " GRID":                                                   # :9586-9609
            for grid in re.findall(r"\b[A-R]{2}[0-9]{2}(?:[A-X]{2})?\b", d.text):
                self._heard(d.from_call, d.snr_db, d.utc, grid)
        if is_allcall and cfg.avoid_allcall and d.cmd not in (" CQ", " HB", " HEARTBEAT"):   # :9668
            return []
        if not is_allcall and not to_me and not is_group:                      # :9675
            return []
        if cfg.whitelist and not ({d.from_call, base_callsign(d.from_call)} & set(cfg.whitelist)):  # :9775
            return self._skip(d, "not on the whitelist")
        if {d.from_call, base_callsign(d.from_call)} & set(cfg.blacklist):     # :9785
            return self._skip(d, "on the blacklist")
        t = self.allcall_cache.get(d.from_call)                                # :9797
        if is_allcall and t is not None and (now - t) / 60 < 15:
            return self._skip(d, "already answered an allcall from this station within 15 min")
        if self.watchdog:                                                      # :9805
            return self._skip(d, "idle watchdog")
        if M.is_command_autoreply(d.cmd) and d.relay_path and not d.cmd.startswith((" MSG", " QUERY")):  # :9813
            d = dataclasses.replace(d, from_call=d.relay_path)

        reply, priority, offset, on_sent = "", PRIORITY_NORMAL, -1, None
        c = d.cmd
        if c == " SNR?" and not is_allcall:                                    # :9824
            reply = f"{d.from_call} SNR {format_snr(d.snr_db)}"
        elif c == " INFO?" and not is_allcall:                                 # :9831
            if not cfg.info:
                return []
            reply = f"{d.from_call} INFO {self._macros(cfg.info, now)}"
        elif c == " STATUS?" and not is_allcall:                               # :9843
            if not cfg.status:
                return []
            reply = f"{d.from_call} STATUS {self._macros(cfg.status, now)}"
        elif c == " GRID?" and not is_allcall:                                 # :9855
            if not cfg.grid:
                return []
            reply = f"{d.from_call} GRID {cfg.grid}"
        elif c == " HEARING?" and not is_allcall:                              # :9865-9898
            calls = [h for h in sorted(self.heard.values(), key=lambda h: h.utc, reverse=True)
                     if h.call != d.from_call and not self._aged(h, now)][:4]
            reply = " ".join([f"{d.from_call} HEARING"] + [h.call for h in calls])
        elif c == ">" and not is_allcall and cfg.relay:                        # :9901-10012
            m = _CALL_TO_RE.match(d.text)
            if m and not is_group:
                text = d.text
                if m.group("type") != ">":
                    text = text[:m.start("type")] + ">" + text[m.end("type"):]
                reply = f"{text} *DE* {d.from_call}"
            elif not d.text.startswith("ACK"):
                calls = parse_relay_path(d.from_call, d.text)
                for call in calls:
                    self.heard.setdefault(call, _Heard(call, -64, now))
                d = dataclasses.replace(d, relay_path=">".join(calls))
                reply = f"{d.relay_path} ACK"
                relayed = self._relayed_command(d)
                if relayed is not None:
                    queue.appendleft(relayed)
                    return []
        elif c == " MSG TO:" and not is_allcall and cfg.relay:                 # :10015-10056
            segs = d.text.split(" ")
            to, text = segs[0], " ".join(segs[1:]).strip()
            if not to:
                return []
            calls = parse_relay_path(d.from_call, text)
            path = ">".join(calls)
            if self.inbox is not None:
                self.inbox.add_command("STORE", dataclasses.replace(d, to=base_callsign(to), text=text,
                                                                    relay_path=path))
            self._note(f"stored a message from {d.from_call} for {base_callsign(to)}")
            reply = f"{path if len(calls) > 1 else d.from_call} ACK"
        elif c == " AGN?" and not is_allcall and not is_group and self.last_tx_message:   # :10059
            reply = M.rstrip(self.last_tx_message)
        elif c in (" HB", " HEARTBEAT") and self._hb_allowed():                # :10068-10125
            if open_buffer_to_me:
                return self._skip(d, "a message buffer is open")
            if cfg.hb_qso_pause and self.selected_call:
                return self._skip(d, "HB paused during a QSO")
            if {d.from_call, base_callsign(d.from_call)} & set(cfg.hb_blacklist):
                return self._skip(d, "on the HB blacklist")
            extra = ""
            mid = self.inbox.next_message_id_for(d.from_call) if self.inbox is not None else -1
            if mid != -1:
                extra = f"MSG ID {mid}"
            elif is_group and self.inbox is not None:
                mid = self.inbox.next_group_message_id_for(d.to, d.from_call, now)
                if mid != -1:
                    extra = f"MSG ID {mid}"
            ev = self._send_heartbeat_ack(d.from_call, d.snr_db, extra, now)
            if is_allcall:
                self.allcall_cache[d.from_call] = now
            return ev
        elif c in (" HEARTBEAT SNR", " CQ", " CMD") or (c == " ACK" and not is_allcall):   # :10128-10193
            return []
        elif c == " MSG" and not is_allcall:                                   # :10140-10174
            calls = parse_relay_path(d.from_call, d.text)
            d = dataclasses.replace(d, cmd=" MSG ", relay_path=">".join(calls))
            mid = self.inbox.add_command("UNREAD", d) if self.inbox is not None else -1
            self._note(f"new message from {d.from_call} in the inbox")
            reply = f"{d.relay_path if len(calls) > 1 else d.from_call} ACK"
            return [{"type": "inbox", "id": mid}] + self._reply(d, reply, priority, offset, on_sent, now,
                                                                is_allcall)
        elif c == " QUERY" and not is_allcall:                                 # :10196-10289
            reply, on_sent = self._query_msg(d, now)
        elif c == " QUERY MSGS" and cfg.autoreply:                             # :10292-10328
            who, path = self._who(d)
            mid = self.inbox.next_message_id_for(who) if self.inbox is not None else -1
            if mid != -1:
                reply = f"{path} YES MSG ID {mid}"
            elif is_group and self.inbox is not None:
                mid = self.inbox.next_group_message_id_for(d.to, d.from_call, now)
                if mid != -1:
                    reply = f"{path} YES MSG ID {mid}"
            if not is_allcall and not reply:
                reply = f"{path} NO"
        elif c == " QUERY CALL" and cfg.autoreply:                             # :10331-10380
            path = d.relay_path if ">" in d.relay_path else d.from_call
            calls = M.parse_callsigns(d.text)
            if not calls:
                return []
            base = calls[0]
            hit = next((h for h in self.heard.values() if not self._aged(h, now)
                        and (base == h.call or base == base_callsign(h.call))), None)
            if hit is not None:
                reply = f"{path} YES {format_snr(hit.snr_db)} ({since(now - hit.utc)})".strip()
                if is_allcall:
                    self.allcall_cache[d.from_call] = now
        if not reply:                                                          # :10395
            return []
        return self._reply(d, reply, priority, offset, on_sent, now, is_allcall)

    def _reply(self, d, reply, priority, offset, on_sent, now, is_allcall):
        if not self.config.autoreply and is_allcall:                           # :10400
            return []
        if self.operator_draft:                                                # :10413
            return self._skip(d, "the operator is typing a message")
        if is_allcall:                                                         # :10426
            self.allcall_cache[d.from_call] = now
        return self._enqueue(priority, reply, offset, f"{d.from_call}:{d.cmd.strip()}", on_sent, now)

    def _relayed_command(self, d):
        """:9948-10004: a relayed text that itself starts with an autoreply command is processed as that."""
        words = d.text.split(" ")
        if not words:
            return None
        first = words[0]
        if not M.is_command_allowed(first):
            first = " " + first
            if M.is_command_allowed(first):
                words = words[1:]
        if words:
            if first == " MSG":
                second = words[0]
                if second == "TO:":
                    first, words = " MSG TO:", words[1:]
                elif second.startswith("TO:"):
                    first, words = " MSG TO:", [second[3:]] + words[1:]
            elif first == " QUERY":
                second = words[0]
                if second in ("MSGS", "MSGS?"):
                    first, words = " QUERY MSGS", words[1:]
                elif second == "CALL":
                    first, words = " QUERY CALL", words[1:]
        if M.is_command_allowed(first) and M.is_command_autoreply(first):
            return dataclasses.replace(d, cmd=first, text=" ".join(words))
        return None

    def _who(self, d):
        if ">" in d.relay_path:
            return d.relay_path.split(">")[-1], d.relay_path
        return d.from_call, d.from_call

    def _query_msg(self, d, now):
        who, path = self._who(d)
        segs = d.text.split(" ")
        if len(segs) < 2 or segs[0] != "MSG" or self.inbox is None:
            return "", None
        try:
            mid = int(segs[1])
        except ValueError:
            return "", None
        msg = self.inbox.value(mid)
        params = (msg or {}).get("params") or {}
        if not params:
            return "", None
        frm, to = str(params.get("FROM", "")).strip(), str(params.get("TO", "")).strip()
        is_group_msg = to.startswith("@")
        if not is_group_msg and to != who and to != base_callsign(who):
            return "", None
        text = str(params.get("TEXT", "")).strip()
        if not text:
            return "", None
        if is_group_msg:
            def on_sent():
                self.inbox.mark_group_delivered(mid, who)
        else:
            def on_sent():
                self.inbox.mark_delivered(mid)
        ahead = self.inbox.lookahead_message_id_for(who, mid)
        if ahead == -1 and is_group_msg:
            ahead = self.inbox.lookahead_group_message_id_for(d.to, who, mid, now)
        if ahead != -1:
            return f"{path} MSG {text} FROM {frm} NEXT MSG ID {ahead}", on_sent
        return f"{path} MSG {text} FROM {frm}", on_sent

    # --- heartbeat (sendHB :6968, sendHeartbeatAck :7002, TxLoop.cpp:181-205) --------------------------------
    def _hb_allowed(self):
        cfg = self.config
        return (self.submode != js8_phy.TURBO and cfg.hb_mode and cfg.autoreply and cfg.hb_ack)

    def heartbeat_now(self, own_offset_hz, now=None):
        """sendHB: queue one heartbeat (the "Send Heartbeat Now" / timer path)."""
        now = self.clock() if now is None else now
        grid = self.config.grid[:4]
        text = f"{self.config.mycall}: HEARTBEAT {grid}".strip()
        f = self.find_free_offset(*HB_BAND, now=now)
        if own_offset_hz <= 1000:
            f = own_offset_hz
        elif self.config.hb_anywhere:
            f = -1
        return self._enqueue(HB_PRIORITY, text, f, "heartbeat", None, now, confirm=False)

    def _send_heartbeat_ack(self, to, snr, extra, now):
        text = f"{to} HEARTBEAT SNR {format_snr(snr)} {extra}".strip()      # :7012 (#else branch)
        f = -1 if self.config.hb_anywhere else self.find_free_offset(*HB_BAND, now=now)
        return self._enqueue(HB_PRIORITY, text, f, f"{to}:HB", None, now)

    def _restart_hb(self, now):
        """on_hbMacroButton_toggled / onTxLoopPeriodChangeStart: first heartbeat one interval from now, on the
        speed's period grid."""
        cfg = self.config
        if not cfg.hb_mode or cfg.hb_interval_min <= 0 or self.submode == js8_phy.TURBO:
            self.hb_next = None
            return
        period = js8_phy.SUBMODES[self.submode]["period_s"]
        earliest = now + cfg.hb_interval_min * 60
        rem = earliest % period
        self.hb_next = earliest if rem == 0 else earliest + period - rem

    def find_free_offset(self, fmin, fmax, bw, now=None):
        """findFreeFreqOffset (:6271-6292) with isFreqOffsetFree (:6245-6269)."""
        now = self.clock() if now is None else now
        nslots = (fmax - fmin) // bw

        def free(f):
            return all(now - t >= BAND_FREE_S or abs(off - f) >= bw for off, t in self.band_activity.items())
        for _ in range(nslots):
            f = fmin + bw * self.rng.randrange(nslots)
            if free(f):
                return f
        for _ in range(nslots):
            f = fmin + self.rng.randrange(fmax - fmin)
            if free(f):
                return f
        return fmin

    # --- queue (confirmThenEnqueueMessage :5886, enqueueMessage :5911, processTxQueue :10727) ---------------
    def _enqueue(self, priority, text, offset, reason, on_sent, now, confirm=None):
        confirm = self.config.confirm if confirm is None else confirm
        msg = QueuedMessage(next(self._ids), now, priority, text, offset, reason, on_sent)
        if confirm:
            msg.expires = now + self.config.confirm_timeout_s
            self.pending.append(msg)
            self._note(f"reply needs confirmation: {text}")
            return [{"type": "confirm", "id": msg.id, "text": text, "reason": reason, "expires": msg.expires}]
        self.queue.append(msg)
        self._note(f"queued: {text}")
        return [{"type": "queued", "id": msg.id, "text": text, "reason": reason}]

    def confirm(self, msg_id, yes, now=None):
        now = self.clock() if now is None else now
        msg = next((m for m in self.pending if m.id == msg_id), None)
        if msg is None:
            return []
        self.pending.remove(msg)
        if not yes or now > msg.expires:
            self._note(f"not sent (declined or too late): {msg.text}")
            return [{"type": "declined", "id": msg.id, "text": msg.text}]
        self.queue.append(msg)
        self._note(f"confirmed and queued: {msg.text}")
        return [{"type": "queued", "id": msg.id, "text": msg.text, "reason": msg.reason}]

    def tick(self, own_offset_hz, now=None, lead_s=HB_LEAD_S):
        """Once a second or so: idle watchdog, expired confirmations, the heartbeat timer. The heartbeat is queued
        lead_s before its period (JS8Call's TxLoop fires tx_delay ahead; pluto-tx keys a message 3 s early)."""
        now = self.clock() if now is None else now
        events = []
        wd = self.config.idle_watchdog_min
        if wd and not self.watchdog and now - self.last_activity >= wd * 60:
            events.extend(self._trigger_watchdog(f"idle for {wd} min"))
        for m in [m for m in self.pending if now > m.expires]:
            self.pending.remove(m)
            events.append({"type": "declined", "id": m.id, "text": m.text})
        if self.hb_next is not None and not self.watchdog and now >= self.hb_next - lead_s:
            period = js8_phy.SUBMODES[self.submode]["period_s"]
            interval = self.config.hb_interval_min * 60
            nxt = self.hb_next + interval
            nxt -= nxt % period
            while nxt < now:                                  # TxLoop::onTimer: skip missed loop periods
                nxt += interval
            self.hb_next = nxt
            events.extend(self.heartbeat_now(own_offset_hz, now))
        return events

    def next_transmission(self, busy, own_offset_hz, now=None):
        """processTxQueue: the next message if it may go out now -> (QueuedMessage, offset, auto) or None.
        auto=False: JS8Call would only put it into the message box for the operator (no autoreply)."""
        now = self.clock() if now is None else now
        if self.watchdog or not self.queue or busy or self.operator_draft:
            return None
        head = max(self.queue, key=lambda m: (m.priority, -m.id))
        f = own_offset_hz if head.offset_hz == -1 else head.offset_hz
        if f <= 0:
            return None
        if head.priority <= PRIORITY_LOW and now - self.last_tx_start <= LOW_PRIORITY_GAP_S:
            return None
        auto = (head.priority >= PRIORITY_HIGH or " HEARTBEAT " in head.text or " HB " in head.text
                or " ACK " in head.text or self.config.autoreply)
        if auto and self.auto_count(now) >= self.config.max_auto_per_hour:
            self._note(f"hourly limit of {self.config.max_auto_per_hour} automatic transmissions reached")
            return None
        self.queue.remove(head)
        return head, f, auto

    def auto_count(self, now=None):
        now = self.clock() if now is None else now
        while self._auto_sent and now - self._auto_sent[0] >= 3600:
            self._auto_sent.popleft()
        return len(self._auto_sent)

    def transmitted(self, msg, text, auto, now=None):
        """The caller sent msg (or the operator sent text by hand, msg=None): m_lastTxMessage, m_lastTxStartTime,
        the message's callback, the hourly count."""
        now = self.clock() if now is None else now
        self.last_tx_message = text
        self.last_tx_start = now
        if auto:
            self._auto_sent.append(now)
            self._note(f"sent: {text}")
        if msg is not None and msg.on_sent is not None:
            msg.on_sent()

    # --- helpers --------------------------------------------------------------------------------------------
    def _trigger_watchdog(self, reason):
        """tx_watchdog(true) (:12291-12340): autoreply, heartbeat timer and queue off."""
        was = self.watchdog
        self.watchdog = True
        dropped = len(self.queue) + len(self.pending)
        self.queue.clear()
        self.pending.clear()
        # JS8Call restores the switches once the operator acknowledges; pluto-tx leaves them off (SPEC 7.8)
        self.config = dataclasses.replace(self.config, autoreply=False, hb_mode=False)
        self.hb_next = None
        if not was or dropped:
            self._note(f"watchdog ({reason}): autoreply and heartbeat off, {dropped} queued message(s) dropped")
        return [{"type": "watchdog", "reason": reason, "dropped": dropped}]

    def _heard(self, call, snr, utc, grid=""):
        if not call or call.startswith("@") or call == "<....>":
            return
        h = self.heard.get(call)
        if h is None:
            self.heard[call] = _Heard(call, snr, utc, grid)
        else:
            h.snr_db, h.utc = snr, max(h.utc, utc)
            if grid:
                h.grid = grid

    def _aged(self, h, now):
        aging = self.config.callsign_aging_min
        return bool(aging) and (now - h.utc) / 60 >= aging

    def _macros(self, text, now):
        """replaceMacros with buildMacroValues (:7851-7891), the station-level macros."""
        idle_min = int((now - self.last_activity) // 60)
        values = {"<MYCALL>": self.config.mycall, "<MYGRID4>": self.config.grid[:4],
                  "<MYGRID12>": self.config.grid[:12], "<MYINFO>": self.config.info,
                  "<MYSTATUS>": self.config.status, "<MYVERSION>": PLUTO_VERSION,
                  "<MYIDLE>": since(idle_min * 60).upper().replace("NOW", "0M")}
        for _ in range(2):                                   # MYINFO/MYSTATUS may contain macros themselves
            for k, v in values.items():
                text = text.replace(k, v)
        return text

    def _skip(self, d, why):
        self._note(f"no reply to {d.from_call}{d.cmd}: {why}")
        return []

    def _note(self, text):
        self.log.append((self.clock(), text))
        del self.log[:-200]
