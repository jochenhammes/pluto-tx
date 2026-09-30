"""JS8 automation glue (web_trx/js8_automation.py, pluto-tx J9): received frames -> pluto_tx.js8_auto ->
the backend's JS8 series, with the Web-TRX' operator-presence rules. Pure Python (no GNU Radio)."""
import asyncio
import dataclasses

import pytest

from web_trx import pluto_path
from web_trx.js8_automation import Js8Automation
from web_trx.session import SessionError

pluto_path.ensure_importable()
from pluto_tx import js8
from pluto_tx import js8_message as M

T0 = 1_790_000_010.0


@dataclasses.dataclass
class D:
    frame: str
    flags: int
    submode: int
    snr_db: float
    freq_hz: float


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


class Rig:
    def __init__(self, ready=True, busy=False, call="DA2JH"):
        self.clock = Clock()
        self.events = []
        self.sent = []
        self.state = {"ready": ready, "busy": busy, "offset_hz": 1500.0, "submode": js8.NORMAL}
        self.station = {"call": call, "locator": "JO31"}
        self.fail = None

        async def emit(name, fields):
            self.events.append((name, fields))

        async def send(text, offset):
            if self.fail:
                raise SessionError(self.fail)
            self.sent.append((text, offset))

        self.auto = Js8Automation(emit=emit, send=send, tx_state=lambda: self.state, station=lambda: self.station,
                                  inbox_path=":memory:", clock=self.clock)

    def hear(self, text, call="DL1ABC", snr=-12.0, freq=1200.0, submode=js8.NORMAL):
        period = js8.speed_info(submode)["period_s"]
        for i, (frame, flags) in enumerate(M.build_frames(call, "JO62", text, submode)):
            self.auto.on_decodes(self.clock.t, submode, [D(frame, flags, submode, snr, freq)])
            self.clock.t += period

    def auto_events(self, kind):
        return [f for n, f in self.events if n == "js8_auto_event" and f["type"] == kind]


async def test_reply_goes_out_through_the_series():
    r = Rig()
    await r.auto.request("config", {"config": {"autoreply": True, "confirm": False}})
    r.hear("DA2JH SNR?")
    await r.auto.step()
    assert r.sent == [("DL1ABC SNR -12", 1500.0)]
    assert r.auto_events("sent")[0]["text"] == "DL1ABC SNR -12"
    status = [f for n, f in r.events if n == "js8_auto"][-1]
    assert status["auto_last_hour"] == 1 and status["config"]["autoreply"]


async def test_everything_starts_off_and_a_directed_query_is_only_suggested():
    r = Rig()
    assert not r.auto.auto.config.autoreply and not r.auto.auto.config.hb_mode
    await r.auto.request("config", {"config": {"confirm": False}})
    r.hear("DA2JH SNR?")
    await r.auto.step()
    assert r.sent == []
    assert r.auto_events("suggest")[0]["text"] == "DL1ABC SNR -12"


async def test_confirmation_from_the_browser():
    r = Rig()
    await r.auto.request("config", {"config": {"autoreply": True}})       # confirmation stays on
    r.hear("DA2JH SNR?")
    await r.auto.step()
    (pending,) = r.auto_events("confirm")
    assert r.sent == []
    await r.auto.request("confirm", {"id": pending["id"], "yes": True})
    await r.auto.step()
    assert r.sent == [("DL1ABC SNR -12", 1500.0)]


async def test_own_periods_and_own_call_are_ignored():
    r = Rig()
    await r.auto.request("config", {"config": {"autoreply": True, "confirm": False}})
    r.auto.note_own_frame(r.clock.t + 0.1, js8.NORMAL)                     # we transmit in this period
    r.hear("DA2JH SNR?")
    r.hear("DL1ABC SNR?", call="DA2JH")                                    # our own signal heard back
    await r.auto.step()
    assert r.sent == []


async def test_not_ready_keeps_the_queue_and_a_failed_send_is_reported():
    r = Rig(ready=False)
    await r.auto.request("config", {"config": {"autoreply": True, "confirm": False}})
    r.hear("DA2JH SNR?")
    await r.auto.step()
    assert r.sent == [] and len(r.auto.auto.queue) == 1
    r.state["ready"] = True
    r.fail = "tx: not connected"
    await r.auto.step()
    assert r.auto_events("not_sent")[0]["reason"] == "tx: not connected"


async def test_operator_gone_switches_everything_off():
    r = Rig()
    await r.auto.request("config", {"config": {"autoreply": True, "hb_mode": True, "hb_interval_min": 10}})
    r.hear("DA2JH SNR?")
    r.auto.operator_gone()
    await r.auto.step()
    (wd,) = r.auto_events("watchdog")
    assert wd["reason"] == "no browser connected" and wd["dropped"] == 1
    cfg = r.auto.auto.config
    assert not cfg.autoreply and not cfg.hb_mode and r.auto.auto.hb_next is None
    r.hear("DA2JH SNR?")
    await r.auto.step()
    assert r.sent == []
    r.auto.operator_active()                                              # the browser is back
    await r.auto.step()
    assert r.auto_events("watchdog_cleared")


async def test_idle_watchdog():
    r = Rig()
    await r.auto.request("config", {"config": {"autoreply": True, "confirm": False, "idle_watchdog_min": 5}})
    r.clock.t += 5 * 60
    await r.auto.step()
    assert r.auto_events("watchdog")[0]["reason"] == "idle for 5 min"


async def test_heartbeat_now_and_timer():
    r = Rig()
    await r.auto.request("heartbeat_now", {})
    await r.auto.step()
    ((text, offset),) = r.sent
    assert text == "DA2JH: HEARTBEAT JO31" and 500 <= offset < 1000
    await r.auto.request("config", {"config": {"hb_mode": True, "hb_interval_min": 10}})
    nxt = r.auto.auto.hb_next
    r.clock.t = nxt - 3.5
    r.auto.auto.last_tx_start = 0.0
    await r.auto.step()
    assert len(r.sent) == 2


async def test_config_is_validated():
    r = Rig()
    for bad in ({"nope": 1}, {"hb_interval_min": 2000}, {"idle_watchdog_min": 90}, {"idle_watchdog_min": 0},
                {"autoreply": "maybe"}):
        with pytest.raises(SessionError):
            await r.auto.request("config", {"config": bad})
    r.station["call"] = ""
    with pytest.raises(SessionError):
        await r.auto.request("config", {"config": {"autoreply": True}})
    with pytest.raises(SessionError):
        await r.auto.request("frobnicate", {})


async def test_inbox_and_clear_queue():
    r = Rig()
    await r.auto.request("config", {"config": {"autoreply": True}})
    r.hear("DA2JH MSG MEET YOU ON 2 M")
    await r.auto.request("inbox", {})
    (inbox,) = [f for n, f in r.events if n == "js8_inbox"]
    assert [(m["type"], m["from"], m["text"]) for m in inbox["messages"]] == [("UNREAD", "DL1ABC", "MEET YOU ON 2 M")]
    assert r.auto.auto.pending                                            # the ACK waits for confirmation
    await r.auto.request("clear_queue", {})
    assert not r.auto.auto.pending and r.auto_events("cleared")[0]["dropped"] == 1
    await r.auto.request("inbox_delete", {"id": inbox["messages"][0]["id"]})
    assert [f for n, f in r.events if n == "js8_inbox"][-1]["messages"] == []


async def test_sim_backend_answers_a_query_with_an_armed_series():
    """SimBackend end to end: a simulated station asks SNR?, the automation arms the reply as a JS8 series."""
    from web_trx import modes
    from web_trx.sim_backend import SimBackend

    b = SimBackend()
    events = []

    async def emit(name, fields):
        events.append((name, fields))

    async def sink(_frame):
        pass

    b.bind(emit, sink, sink)
    b.station = {"call": "DA2JH", "locator": "JO31"}
    b._js8_auto = Js8Automation(emit=emit, send=b._js8_auto_send, tx_state=b._js8_tx_state,
                                station=lambda: b.station, inbox_path=":memory:")
    try:
        await b.connect("tx", "sim", "sim")
        await b.select_mode("tx", "js8", modes.normalize_params("tx", "js8", {"kind": "cq", "submode": "turbo"}))
        await b.connect("rx", "sim", "sim")
        await b.select_mode("rx", "js8", modes.normalize_params("rx", "js8", {"submode": "turbo"}))
        await b.js8_automation_request("config", {"config": {"autoreply": True, "confirm": False}})
        assert b.inject_js8("DL1ABC", "JO62", "DA2JH SNR?") == 1
        b.fake_js8_period(T0, js8.TURBO)
        await b._js8_auto.step()
        assert b._js8_armed()
        await asyncio.sleep(0.05)                                          # the series emits from its task
        armed = [f for n, f in events if n == "js8_armed"]
        assert armed and armed[-1]["auto"] and armed[-1]["text"].startswith("DA2JH: DL1ABC SNR ")
    finally:
        await b._cancel_js8("test")
        await b._js8_auto.stop()
