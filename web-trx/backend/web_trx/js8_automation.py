"""JS8 automation in the Web-TRX (pluto-tx docs/JS8_PLAN.md J9, docs/js8/SPEC.md 7): the glue between the
received JS8 frames, pluto-tx' automation core (pluto_tx.js8_auto, a port of JS8Call's autoreply/heartbeat/
relay/inbox logic) and the backend's JS8 transmit series.

Only the Web-TRX has receive and transmit in one process, so this is the one place automation exists. Nothing
is sent that an operator could not send by hand: an automatic message goes out through the same series as a
typed one (band check, power ceiling, E-STOP, the abort rule), on the TX device and speed of the selected JS8
TX mode. Everything starts off. The operator must be present: every request from a browser counts as
activity (JS8Call's idle watchdog, default 60 min), and with no browser connected everything is switched off
at once (Js8Auto.operator_gone()). At most 20 automatic transmissions per hour (pluto-tx).

Pure Python, no GNU Radio: GnuRadioBackend and SimBackend both use it."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable

from . import pluto_path
from .session import SessionError

logger = logging.getLogger("web_trx.js8_automation")

TICK_S = 1.0
STATUS_EVERY_S = 10.0

# What the operator may change (name -> type); the rest of Js8AutoConfig keeps JS8Call's defaults.
CONFIG_FIELDS = {
    "autoreply": bool, "confirm": bool, "hb_mode": bool, "hb_interval_min": int, "hb_ack": bool,
    "relay": bool, "info": str, "status": str, "idle_watchdog_min": int,
}
HB_INTERVAL_MAX_MIN = 1440          # buildRepeatMenu: custom 1..1440 min
IDLE_WATCHDOG_MAX_MIN = 60          # pluto-tx: never longer than JS8Call's default


def _modules():
    pluto_path.ensure_importable()
    from pluto_advanced_rx import js8_commands
    from pluto_tx import js8, js8_auto, js8_inbox, js8_message

    return js8, js8_message, js8_auto, js8_inbox, js8_commands


def default_inbox_path() -> str:
    path = os.environ.get("WEB_TRX_JS8_INBOX_PATH")
    if path:
        return path
    settings = os.environ.get("WEB_TRX_SETTINGS_PATH")
    base = os.path.dirname(settings) if settings else os.path.expanduser("~/.local/share/web-trx")
    return os.path.join(base, "js8_inbox.db3")


class Js8Automation:
    def __init__(self, *, emit: Callable[[str, dict], Awaitable[None]],
                 send: Callable[[str, float], Awaitable[None]], tx_state: Callable[[], dict],
                 station: Callable[[], dict], inbox_path: str | None = None, clock=time.time, rng=None):
        js8, _msg, js8_auto, js8_inbox, js8_commands = _modules()
        self._js8 = js8
        self._emit = emit
        self._send = send
        self._tx_state = tx_state
        self._station = station
        self._clock = clock
        call, grid = self._station_call()
        self.inbox = js8_inbox.Js8Inbox(inbox_path or default_inbox_path())
        self.auto = js8_auto.Js8Auto(js8_auto.Js8AutoConfig(mycall=call, grid=grid), self.inbox, clock=clock,
                                     rng=rng)
        self.parser = js8_commands.Js8CommandParser()
        self._own = set()                         # (period start, submode) we transmitted in
        self._events: list[tuple[str, dict]] = []
        self._task: asyncio.Task | None = None
        self._sending = False
        self._last_status = 0.0

    # --- life cycle -----------------------------------------------------------------------------------------
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.inbox.close()

    async def _run(self) -> None:
        while True:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("JS8 automation step failed")
            await asyncio.sleep(TICK_S)

    # --- inputs ---------------------------------------------------------------------------------------------
    def _station_call(self) -> tuple[str, str]:
        st = self._station() or {}
        return (st.get("call") or "").upper(), (st.get("locator") or "").upper()

    def on_decodes(self, slot_start: float, submode: int, decodes: list) -> None:
        """Every decoded period (event-loop thread). Periods we transmitted in are skipped, like JS8Call does
        not decode while it transmits (half duplex) -- the RX here may well hear our own signal."""
        for d in decodes:
            self.auto.note_decode(d.freq_hz, slot_start)
        commands = self.parser.feed(slot_start, submode, decodes)
        commands += self.parser.expire(slot_start + self._js8.speed_info(submode)["period_s"])
        if (round(slot_start, 1), submode) in self._own:
            return
        mine = set(self.auto.mycalls)
        commands = [c for c in commands if c.from_call not in mine]
        if commands:
            self._sync_config()
            self._push(self.auto.process_commands(commands, open_buffer_to_me=self.parser.has_open_buffer_to(mine)))

    def note_own_frame(self, start_at: float, submode: int) -> None:
        period = self._js8.speed_info(submode)["period_s"]
        self._own.add((round(start_at // period * period, 1), submode))
        if len(self._own) > 200:
            self._own = set(sorted(self._own)[-100:])

    def note_manual_tx(self, text: str) -> None:
        """The operator sent a JS8 message by hand (AGN? repeats it, the 30 s rule counts it)."""
        self.auto.transmitted(None, text, False)

    def operator_active(self) -> None:
        was = self.auto.watchdog
        self.auto.operator_active()
        if was:
            self._push([{"type": "watchdog_cleared"}])

    def operator_gone(self) -> None:
        if self.auto.config.autoreply or self.auto.config.hb_mode or self.auto.queue or self.auto.pending:
            self._push(self.auto.operator_gone("no browser connected"))

    # --- the loop -------------------------------------------------------------------------------------------
    async def step(self) -> None:
        self._sync_config()
        state = self._tx_state()
        if state.get("submode") is not None:
            self.auto.set_submode(state["submode"])
        self._push(self.auto.tick(state.get("offset_hz", 1500.0)))
        if not self._sending and state.get("ready") and not state.get("busy"):
            nxt = self.auto.next_transmission(False, state.get("offset_hz", 1500.0))
            if nxt is not None:
                msg, offset, is_auto = nxt
                if is_auto:
                    await self._transmit(msg, offset)
                else:  # JS8Call only fills the message box: the operator decides
                    self._push([{"type": "suggest", "id": msg.id, "text": msg.text}])
        now = self._clock()
        if now - self._last_status >= STATUS_EVERY_S and not self._events:
            self._events.append(("js8_auto", self.status()))
        await self._flush()

    async def _transmit(self, msg, offset: float) -> None:
        self._sending = True
        try:
            await self._send(msg.text, offset)
        except SessionError as e:
            self._push([{"type": "not_sent", "id": msg.id, "text": msg.text, "reason": str(e)}])
            return
        finally:
            self._sending = False
        self.auto.transmitted(msg, msg.text, True)
        self._push([{"type": "sent", "id": msg.id, "text": msg.text, "offset_hz": round(offset),
                     "reason": msg.reason}])

    def _sync_config(self) -> None:
        call, grid = self._station_call()
        cfg = self.auto.config
        if (cfg.mycall, cfg.grid) != (call, grid):
            self.auto.configure(mycall=call, grid=grid)

    def _push(self, events: list) -> None:
        for e in events:
            self._events.append(("js8_auto_event", {**e, "utc": time.strftime("%H:%M:%S", time.gmtime(self._clock()))}))
        if events:
            self._events.append(("js8_auto", self.status()))

    async def _flush(self) -> None:
        events, self._events = self._events, []
        if any(name == "js8_auto" for name, _ in events):
            self._last_status = self._clock()
        for name, fields in events:
            await self._emit(name, fields)

    # --- requests from the browser --------------------------------------------------------------------------
    async def request(self, action: str, params: dict) -> None:
        if action == "config":
            changes = {}
            for name, value in (params.get("config") or {}).items():
                kind = CONFIG_FIELDS.get(name)
                if kind is None:
                    raise SessionError(f"js8 automation: unknown setting '{name}'")
                try:
                    changes[name] = kind(value) if kind is not bool else _as_bool(value)
                except (TypeError, ValueError) as e:
                    raise SessionError(f"js8 automation: bad value for {name}") from e
            if "hb_interval_min" in changes and not 0 <= changes["hb_interval_min"] <= HB_INTERVAL_MAX_MIN:
                raise SessionError(f"js8 automation: heartbeat interval 0..{HB_INTERVAL_MAX_MIN} min")
            if "idle_watchdog_min" in changes and not 1 <= changes["idle_watchdog_min"] <= IDLE_WATCHDOG_MAX_MIN:
                raise SessionError(f"js8 automation: idle watchdog 1..{IDLE_WATCHDOG_MAX_MIN} min")
            if any(changes.get(k) for k in ("autoreply", "hb_mode")) and not self._station_call()[0]:
                raise SessionError("js8 automation: set the station callsign first")
            self.auto.configure(**changes)
            self._push([{"type": "config", "changes": changes}])
        elif action == "confirm":
            self._push(self.auto.confirm(int(params.get("id", -1)), _as_bool(params.get("yes"))))
        elif action == "heartbeat_now":
            if not self._station_call()[0]:
                raise SessionError("js8 automation: set the station callsign first")
            self._sync_config()
            self._push(self.auto.heartbeat_now(self._tx_state().get("offset_hz", 1500.0)))
        elif action == "clear_queue":
            dropped = len(self.auto.queue) + len(self.auto.pending)
            self.auto.queue.clear()
            self.auto.pending.clear()
            self._push([{"type": "cleared", "dropped": dropped}])
        elif action == "inbox":
            await self._emit("js8_inbox", {"messages": self.inbox_list()})
        elif action == "inbox_delete":
            self.inbox.delete(int(params.get("id", -1)))
            await self._emit("js8_inbox", {"messages": self.inbox_list()})
        elif action == "status":
            self._events.append(("js8_auto", self.status()))
        else:
            raise SessionError(f"js8 automation: unknown action '{action}'")
        await self._flush()

    def inbox_list(self) -> list[dict]:
        out = []
        for mid, msg in self.inbox.all():
            p = msg.get("params") or {}
            out.append({"id": mid, "type": msg.get("type"), "utc": p.get("UTC", ""), "from": p.get("FROM", ""),
                        "to": p.get("TO", ""), "path": p.get("PATH", ""), "text": p.get("TEXT", ""),
                        "snr_db": p.get("SNR")})
        return out

    def status(self) -> dict:
        a, cfg = self.auto, self.auto.config
        now = self._clock()
        return {
            "config": {name: getattr(cfg, name) for name in CONFIG_FIELDS},
            "watchdog": a.watchdog,
            "idle_s": round(now - a.last_activity),
            "queue": [{"id": m.id, "text": m.text, "priority": m.priority} for m in a.queue],
            "pending": [{"id": m.id, "text": m.text, "expires_in_s": max(0, round(m.expires - now))}
                        for m in a.pending],
            "auto_last_hour": a.auto_count(now), "auto_max_per_hour": cfg.max_auto_per_hour,
            "hb_next": a.hb_next,
            "log": [{"utc": time.strftime("%H:%M:%S", time.gmtime(t)), "text": text} for t, text in a.log[-20:]],
        }


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str) and value.lower() in ("true", "1", "yes", "on"):
        return True
    if isinstance(value, str) and value.lower() in ("false", "0", "no", "off", ""):
        return False
    raise ValueError(value)
