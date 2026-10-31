"""Pure-Python/numpy session backend -- no GNU Radio, no libiio, no
hardware. Exists so the entire session-manager/WebSocket/frontend stack is
developable and testable in environments that don't have GNU Radio
installed (this dev container, CI) and so the WS wire protocol gets
exercised end-to-end before the real GnuRadioBackend exists. Generates
plausible-looking but NOT physically meaningful spectra -- it proves the
pipes carry data correctly, it does not model real RF propagation.
"""
from __future__ import annotations

import asyncio
import math
import os
import time
from dataclasses import dataclass, field

import numpy as np

from . import ft8_series, js8_series, pluto_path, station
from .session import AudioFrame, SessionBackend, SessionError, SpectrumFrame

FFT_SIZE = 2048
SPECTRUM_RATE_HZ = 20.0
DEFAULT_SPAN_HZ = 2_500_000.0

AUDIO_SAMPLE_RATE_HZ = 8000
AUDIO_CHUNK_S = 0.1
# Distinct tone per mode purely so a human listening while developing can
# tell modes apart -- not a claim that SimBackend demodulates anything.
AUDIO_TONE_HZ = {"fm": 600.0, "ssb": 900.0, "lsb": 750.0, "m17": 440.0, "rade": 520.0, "pocsag": 300.0,
                 "rtty": 2125.0}

# Mirrors the MVP mode set from docs/PROJECT_PLAN.md section 4 (lsb kept
# alongside ssb the same way pluto-tx exposes both as separate
# sideband-fixed modes rather than one mode with a sideband flag).
TX_MODES = ("fm", "ssb", "lsb", "m17", "rade", "pocsag", "rtty", "digitext", "ft8", "js8", "aprs")
RX_MODES = ("fm", "ssb", "lsb", "m17", "rade", "pocsag", "rtty", "ft8", "js8", "adsl", "adsb", "aprs")

# APRS receive: invented stations around the station data, built with the real codec and reported by the real
# state class, so the fields are the ones GnuRadioBackend sends. A neighbour messages the station's own callsign.
APRS_FREQ_HZ = 144_800_000
APRS_SIM_PERIOD_S = 5.0

# ADS-L receive: invented aircraft around the station, one packet per second each, in the event fields a real
# packet has. Deliberately no encoder here: nothing in the application can produce an ADS-L frame.
ADSL_LO_HZ = 868_850_000
ADSB_FREQ_HZ = 1_090_000_000
FIXED_TUNE_HZ = {"adsl": ADSL_LO_HZ, "adsb": ADSB_FREQ_HZ}
ADSL_CHANNELS_HZ = (868_200_000, 868_400_000)
ADSL_DEFAULT_CENTER = (53.5, 9.0)
ADSL_SIM_PERIOD_S = 1.0

# JS8 receive: frames built with pluto-tx' real JS8 codec and assembled like GnuRadioBackend does -- our own
# transmissions come back in their periods (loopback), plus a simulated neighbour (CQ, heartbeat, and a
# report to the station's own callsign when one is set).
JS8_DECODE_LAG_S = 0.3
JS8_SIM_NEIGHBOUR = ("DL1ABC", "JO62")

# FT8 receive: invented decodes per 15 s slot, delivered like pluto-tx's
# Ft8Receiver (~14.8 s after the slot start) -- enough to develop the decode
# table, the CQ filter and click-to-reply without hardware. Includes a CQ and
# a reply to the station's own callsign.
FT8_SLOT_S = 15.0
FT8_DECODE_AT_S = 14.8
FT8_SIM_STATIONS = (("DL1ABC", "JO62"), ("G4XYZ", "IO91"), ("OK1RR", "JN79"), ("EA3AAA", "JN11"))
ONE_SHOT_TX_MODES = ("pocsag", "rtty", "digitext", "aprs")  # auto-unkey, like GnuRadioBackend
# FT8 transmit: slot timing as in pluto_tx/config.py; airtime of one frame
# (79 symbols x 0.16 s) plus pluto-tx' tail.
FT8_KEY_EARLY_S = 3.0
FT8_START_IN_SLOT_S = 0.5
FT8_LATE_START_MAX_S = 1.0
FT8_FRAME_S = 12.64 + 0.1


def sim_plan_transmission(now: float, parity: str = "any") -> tuple[float, float]:
    """Test double of pluto_tx.ft8.plan_transmission() (tests/test_ft8_series.py
    compares the two) -> (key_at, start_at)."""
    slot_parity = ft8_series.slot_parity
    cur = now // FT8_SLOT_S * FT8_SLOT_S
    if (parity == "any" or slot_parity(cur) == parity) and now <= cur + FT8_START_IN_SLOT_S + FT8_LATE_START_MAX_S:
        start = max(cur + FT8_START_IN_SLOT_S, now)
        return max(now, start - FT8_KEY_EARLY_S), start
    slot = cur + FT8_SLOT_S
    while parity != "any" and slot_parity(slot) != parity:
        slot += FT8_SLOT_S
    start = slot + FT8_START_IN_SLOT_S
    return max(now, start - FT8_KEY_EARLY_S), start
DEVICE_TYPES = ("sim",)


@dataclass
class _DirectionState:
    device_type: str | None = None
    connection: str | None = None
    mode: str | None = None
    mode_params: dict = field(default_factory=dict)
    freq_hz: float = 432_500_000.0
    gains: dict = field(default_factory=dict)


class SimBackend(SessionBackend):
    def __init__(self):
        self.station = station.from_environment()
        self.tx = _DirectionState()
        self.rx = _DirectionState()
        self.keyed = False
        self._generation = 0
        self._rng = np.random.default_rng(1)
        self._spectrum_task: asyncio.Task | None = None
        self._audio_task: asyncio.Task | None = None
        self._pocsag_unkey_task: asyncio.Task | None = None
        self._ft8_task: asyncio.Task | None = None
        self._ft8_slots = 0
        self._ft8_series: ft8_series.Ft8Series | None = None
        # Loopback: our own transmissions show up in the ft8_slot of their slot.
        self._ft8_sent: list[tuple[float, str]] = []
        self._adsl_task: asyncio.Task | None = None
        self._adsl_state = _adsl_state_module().AdslState()
        self._adsl_counts = {hz: 0 for hz in ADSL_CHANNELS_HZ}
        self._adsl_tick = 0
        self._aprs_tick = 0
        self._aprs_task: asyncio.Task | None = None
        self._aprs_state = None
        self._aprs_auto_ack = False
        self._aprs_packets = 0
        self._adsb_tick = 0
        self._adsb_task: asyncio.Task | None = None
        self._adsb_aircraft: dict[str, dict] = {}
        self._adsb_messages = 0
        self._freq_before_fixed: float | None = None
        self._js8_task: asyncio.Task | None = None
        self._js8_series: js8_series.Js8Series | None = None
        self._js8_sent: list[tuple[float, int, str, int]] = []   # (period start, submode, frame, flags)
        self._js8_assembler = None
        self._js8_periods = 0
        self._js8_auto = None                      # web_trx/js8_automation.py, created with the event loop
        self._js8_injected: list[list] = []        # [freq, [(frame, flags), ...]] one frame per period
        self._audio_phase = 0.0
        self.tx_audio_frames_received = 0  # test/diagnostic hook, see submit_tx_audio()

    def _state(self, direction: str) -> _DirectionState:
        if direction == "tx":
            return self.tx
        if direction == "rx":
            return self.rx
        raise SessionError(f"unknown direction '{direction}'")

    def device_types(self) -> dict[str, list[list[str]]]:
        return {"rx": [["sim", "Simulator"]], "tx": [["sim", "Simulator"]]}

    def features(self) -> dict:
        return {"rx_modes": list(RX_MODES), "tx_modes": list(TX_MODES),
                "map_tile_url": os.environ.get("WEB_TRX_MAP_TILE_URL", ""),
                "ft8": {"tx": "ft8" in TX_MODES, "rx_backends": ["sim"], "clock_synced": True},
                "js8": {"tx": "js8" in TX_MODES, "rx_backends": ["sim"], "submodes": ["normal", "fast", "turbo", "slow"],
                        "jsc": _js8_modules()[1].jsc_available(), "clock_synced": True}}

    async def scan(self, direction: str, device_type: str) -> dict[str, str]:
        if device_type not in DEVICE_TYPES:
            raise SessionError(f"unknown device type '{device_type}'")
        return {"sim:0": "Simulated SDR (no hardware)"}

    async def connect(self, direction: str, device_type: str, connection: str) -> None:
        if device_type not in DEVICE_TYPES:
            raise SessionError(f"unknown device type '{device_type}'")
        st = self._state(direction)
        st.device_type = device_type
        st.connection = connection or "sim:0"

    async def disconnect(self, direction: str) -> None:
        if direction == "tx":
            await self._cancel_ft8("disconnect")
            await self._cancel_js8("disconnect")
            if self.keyed:
                await self.ptt(False)
        st = self._state(direction)
        st.device_type = None
        st.connection = None
        st.mode = None

    async def select_mode(self, direction: str, mode: str, params: dict) -> None:
        st = self._state(direction)
        if st.connection is None:
            raise SessionError(f"{direction}: not connected")
        allowed = TX_MODES if direction == "tx" else RX_MODES
        if mode not in allowed:
            raise SessionError(f"unsupported {direction} mode '{mode}'")
        if direction == "tx" and self._ft8_armed():
            raise SessionError("tx: FT8 series is armed -- cancel it first")
        if direction == "tx":
            await self._cancel_js8("mode_change")  # like GnuRadioBackend: a mode change ends a JS8 message
        if direction == "rx":
            before = st.freq_hz
            if mode in FIXED_TUNE_HZ:
                if self._freq_before_fixed is None:
                    self._freq_before_fixed = st.freq_hz
                st.freq_hz = FIXED_TUNE_HZ[mode]
            elif mode == "aprs":  # a starting point only, the operator can retune
                if self._freq_before_fixed is None:
                    self._freq_before_fixed = st.freq_hz
                if st.mode != "aprs":
                    st.freq_hz = APRS_FREQ_HZ
            elif self._freq_before_fixed is not None:
                st.freq_hz, self._freq_before_fixed = self._freq_before_fixed, None
            if st.freq_hz != before:
                await self._emit_event("tuned", {"direction": "rx", "freq_hz": st.freq_hz})
        st.mode = mode
        st.mode_params = params

    async def tune(self, direction: str, freq_hz: float) -> None:
        # Allowed while disconnected too: the frequency is kept and used on
        # the next connect (same as GnuRadioBackend).
        if direction == "rx" and self.rx.connection is not None and self.rx.mode in FIXED_TUNE_HZ:
            raise SessionError(f"rx: {self.rx.mode.upper()} tunes itself, the frequency can't be changed in this mode")
        if direction == "rx" and self.rx.connection is None and self._freq_before_fixed is not None:
            self._freq_before_fixed = freq_hz  # disconnected: for the next non-ADS-L mode
            return
        self._state(direction).freq_hz = freq_hz

    async def set_gain(self, direction: str, name: str, value: float) -> None:
        self._state(direction).gains[name] = value

    def _ft8_armed(self) -> bool:
        return self._ft8_series is not None and self._ft8_series.armed

    async def _cancel_ft8(self, reason: str, device_safe: bool = False) -> None:
        series, self._ft8_series = self._ft8_series, None
        if series is not None:
            await series.cancel(reason, device_safe=device_safe)

    async def ptt(self, on: bool, reason: str = "ptt_off") -> None:
        if on:
            if self.tx.connection is None or self.tx.mode is None:
                raise SessionError("tx: not connected or no mode selected")
            if self.tx.mode == "ft8":
                await self._arm_ft8()
                return
            if self.tx.mode == "js8":
                await self._arm_js8()
                return
            if self.keyed:
                return
            if self.tx.mode == "aprs":
                self._aprs_check_tx()
            self.keyed = True
            await self._emit_event("keyed", {"mode": self.tx.mode})
            if self.tx.mode == "aprs":
                await self._emit_event("aprs_sent", self._aprs_sim_sent())
            elif self.tx.mode == "pocsag":
                await self._emit_event("pocsag_message", {
                    "ric": self.tx.mode_params.get("ric", 1234567),
                    "text": self.tx.mode_params.get("text", ""),
                })
            elif self.tx.mode in ONE_SHOT_TX_MODES:
                text = self.tx.mode_params.get("text", "")
                await self._emit_event("tx_text", {"mode": self.tx.mode, "text": text, "duration_s": 1.5})
                if self.tx.mode == "rtty":
                    # Loopback: "receive" what was sent, so the RTTY text panel
                    # can be developed without hardware.
                    await self._emit_event("rtty_text", {"text": text + "\n"})
            if self.tx.mode in ONE_SHOT_TX_MODES:
                # One-shot in the real flowgraph too (see pluto-tx
                # pluto_cli/README.md section 4) -- a fixed short hold stands
                # in for computing real airtime here.
                self._pocsag_unkey_task = asyncio.create_task(self._auto_unkey_after(1.5))
        else:
            await self._cancel_ft8(reason)
            await self._cancel_js8(reason)
            if self._pocsag_unkey_task is not None:
                self._pocsag_unkey_task.cancel()
                self._pocsag_unkey_task = None
            if not self.keyed:
                return
            self.keyed = False
            await self._emit_event("unkeyed", {"mode": self.tx.mode})

    async def _arm_ft8(self) -> None:
        if self._ft8_armed():
            return
        params = self.tx.mode_params
        text = ft8_series.compose_text(self.station, params)
        if not text:
            raise SessionError("ft8: no message (station callsign or DX callsign missing)")

        async def key(start_at: float) -> float:
            self.keyed = True
            await self._emit_event("keyed", {"mode": "ft8", "text": text})
            await self._emit_event("tx_text", {"mode": "ft8", "text": text, "duration_s": FT8_FRAME_S})
            self._ft8_sent.append((start_at // FT8_SLOT_S * FT8_SLOT_S, text))
            return max(0.0, start_at - time.time()) + FT8_FRAME_S

        async def unkey() -> None:
            if self.keyed:
                self.keyed = False
                await self._emit_event("unkeyed", {"mode": "ft8"})

        self._ft8_series = ft8_series.Ft8Series(
            plan=sim_plan_transmission, key=key, unkey=unkey, emit=self._emit_event,
            parity=params.get("slot", "any"), count=params.get("repeat_count", 1))
        self._ft8_series.start()

    def _js8_armed(self) -> bool:
        return self._js8_series is not None and self._js8_series.armed

    async def _cancel_js8(self, reason: str, device_safe: bool = False) -> None:
        series, self._js8_series = self._js8_series, None
        if series is not None:
            await series.cancel(reason, device_safe=device_safe)

    async def _arm_js8(self) -> None:
        params = self.tx.mode_params
        try:
            frames, text = js8_series.build_message(self.station, params)
        except ValueError as e:
            raise SessionError(f"js8: {e}") from e
        if self._arm_js8_frames(params, frames, text, auto=False) and self._js8_auto is not None:
            self._js8_auto.note_manual_tx(text)

    async def _js8_auto_send(self, text: str, offset_hz: float) -> None:
        params = {**(self.tx.mode_params or {}), "offset_hz": float(offset_hz)}
        try:
            frames, shown = js8_series.build_text(self.station, text, params["submode"])
        except (ValueError, KeyError) as e:
            raise SessionError(f"js8: {e}") from e
        if not self._arm_js8_frames(params, frames, shown, auto=True):
            raise SessionError("js8: transmitter busy")

    def _js8_tx_state(self) -> dict:
        js8, _msg = _js8_modules()
        params = self.tx.mode_params or {}
        ready = self.tx.connection is not None and self.tx.mode == "js8"
        return {"ready": ready, "busy": self.keyed or self._js8_armed(),
                "offset_hz": float(params.get("offset_hz", 1500.0)),
                "submode": js8.submode_from_name(params["submode"]) if ready and "submode" in params else None}

    async def js8_automation_request(self, action: str, params: dict) -> None:
        if self._js8_auto is None:
            raise SessionError("JS8 automation is not available")
        await self._js8_auto.request(action, params)

    def operator_activity(self) -> None:
        if self._js8_auto is not None:
            self._js8_auto.operator_active()

    def operator_gone(self) -> None:
        self._aprs_auto_ack = False
        if self._js8_auto is not None:
            self._js8_auto.operator_gone()

    def inject_js8(self, call: str, grid: str, text: str, freq_hz: float = 1300.0) -> int:
        """Test hook: a simulated station sends `text` -- its frames arrive in the next decoded periods of the
        RX speed. -> number of frames."""
        _js8, msg = _js8_modules()
        frames = msg.build_frames(call, grid, text, self._js8_rx_submode())
        self._js8_injected.append([float(freq_hz), list(frames)])
        return len(frames)

    def _arm_js8_frames(self, params: dict, frames: list, text: str, auto: bool) -> bool:
        if self._js8_armed():
            return False
        js8, _msg = _js8_modules()
        submode = js8.submode_from_name(params["submode"])
        info = js8.speed_info(submode)
        frame_s = info["data_duration_s"] + 0.1

        def plan(now: float) -> list:
            return js8.plan_frames(now, submode, len(frames))

        async def key(i: int, start_at: float) -> float:
            self.keyed = True
            await self._emit_event("keyed", {"mode": "js8", "text": text, "frame": i + 1, "of": len(frames)})
            if i == 0:
                await self._emit_event("tx_text", {"mode": "js8", "text": text, "frames": len(frames),
                                                   "duration_s": round(js8.transmission_time_s(len(frames), submode), 2)})
            period = info["period_s"]
            self._js8_sent.append((start_at // period * period, submode, *frames[i]))
            if self._js8_auto is not None:
                self._js8_auto.note_own_frame(start_at, submode)
            return max(0.0, start_at - time.time()) + frame_s

        async def unkey() -> None:
            if self.keyed:
                self.keyed = False
                await self._emit_event("unkeyed", {"mode": "js8"})

        async def abort() -> None:
            pass

        self._js8_series = js8_series.Js8Series(
            plan=plan, key=key, unkey=unkey, abort=abort, emit=self._emit_event, count=len(frames),
            info={"text": text, "speed": params["submode"], "offset_hz": params.get("offset_hz", 1500.0),
                  "auto": auto})
        self._js8_series.start()
        return True

    async def _auto_unkey_after(self, seconds: float) -> None:
        """Runs as self._pocsag_unkey_task. Must clear that reference
        *before* calling ptt(False) below -- otherwise ptt(False) finds
        self._pocsag_unkey_task still pointing at the task currently
        executing this very coroutine and cancels itself, which raises
        CancelledError out of the await self._emit_event("unkeyed", ...)
        inside ptt(False) and silently drops the event (caught by the
        except clause here). An externally-triggered cancel (ptt_off /
        estop while still sleeping) still works exactly as before: this
        coroutine is torn down at the `await asyncio.sleep()` line, never
        reaching the ptt(False) call at all."""
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        self._pocsag_unkey_task = None
        await self.ptt(False)

    async def estop(self) -> None:
        self._aprs_auto_ack = False
        await self._cancel_ft8("estop", device_safe=True)
        await self._cancel_js8("estop", device_safe=True)
        if self._pocsag_unkey_task is not None:
            self._pocsag_unkey_task.cancel()
            self._pocsag_unkey_task = None
        was_keyed = self.keyed
        self.keyed = False
        await self._emit_event("estop", {})
        if was_keyed:
            await self._emit_event("unkeyed", {"mode": self.tx.mode})

    async def submit_tx_audio(self, frame: AudioFrame) -> None:
        # SimBackend never actually transmits audio -- accepting/counting
        # frames is enough to exercise the mic-capture -> WS -> backend leg
        # of the pipeline end-to-end without real hardware, see tests.
        self.tx_audio_frames_received += 1

    def snapshot(self) -> dict:
        return {
            "tx": {**_snapshot_direction(self.tx, self.keyed), "ft8_armed": self._ft8_armed(),
                   "js8_armed": self._js8_armed()},
            "rx": _snapshot_direction(self.rx, False),
        }

    async def shutdown(self) -> None:
        if self._js8_auto is not None:
            auto, self._js8_auto = self._js8_auto, None
            await auto.stop()
        await self._cancel_ft8("shutdown", device_safe=True)
        await self._cancel_js8("shutdown", device_safe=True)
        if self._adsl_task is not None:
            self._adsl_task.cancel()
        if self._aprs_task is not None:
            self._aprs_task.cancel()
        if self._adsb_task is not None:
            self._adsb_task.cancel()
        if self._js8_task is not None:
            self._js8_task.cancel()
        if self._spectrum_task is not None:
            self._spectrum_task.cancel()
        if self._audio_task is not None:
            self._audio_task.cancel()
        if self._pocsag_unkey_task is not None:
            self._pocsag_unkey_task.cancel()
        if self._ft8_task is not None:
            self._ft8_task.cancel()
        self.keyed = False

    # -- Background generators. Neither is part of SessionBackend's ABC (no
    # control request triggers them) -- both exercise their binary WS
    # channel continuously once RX is connected, independent of
    # control-plane activity, exactly like a real FftProbe/audio sink would
    # (see pluto-tx pluto_advanced_rx/fft_probe.py). --

    def start_background_tasks(self) -> None:
        if self._spectrum_task is None:
            self._spectrum_task = asyncio.create_task(self._spectrum_loop())
        if self._audio_task is None:
            self._audio_task = asyncio.create_task(self._audio_loop())
        if self._ft8_task is None:
            self._ft8_task = asyncio.create_task(self._ft8_loop())
        if self._adsl_task is None:
            self._adsl_task = asyncio.create_task(self._adsl_loop())
        if self._adsb_task is None:
            self._adsb_task = asyncio.create_task(self._adsb_loop())
        if self._aprs_task is None:
            self._aprs_task = asyncio.create_task(self._aprs_loop())
        if self._js8_task is None:
            self._js8_task = asyncio.create_task(self._js8_loop())
        if self._js8_auto is None:
            from .js8_automation import Js8Automation

            self._js8_auto = Js8Automation(
                emit=self._emit_event, send=self._js8_auto_send, tx_state=self._js8_tx_state,
                station=lambda: self.station, inbox_path=os.environ.get("WEB_TRX_JS8_INBOX_PATH", ":memory:"))
            self._js8_auto.start()

    def _js8_rx_submode(self) -> int:
        js8, _msg = _js8_modules()
        name = (self.rx.mode_params or {}).get("submode", "normal")
        return js8.SPEEDS.get(name, js8.NORMAL)          # "all": the simulator plays Normal

    async def _js8_loop(self) -> None:
        js8, _msg = _js8_modules()
        last = None
        while True:
            period = js8.speed_info(self._js8_rx_submode())["period_s"]
            now = time.time()
            slot = (now - JS8_DECODE_LAG_S) // period * period
            if slot == last:
                slot += period
            await asyncio.sleep(max(0.0, slot + period + JS8_DECODE_LAG_S - time.time()))
            last = slot
            if self.rx.connection is None or self.rx.mode != "js8":
                continue
            for name, fields in self.fake_js8_period(slot, self._js8_rx_submode()):
                await self._emit_event(name, fields)
            self._js8_periods += 1
            await self._emit_event("js8_status", {"decoder": "sim", "periods_decoded": self._js8_periods,
                                                   "last_error": "", "clock_synced": True,
                                                   "speeds": [js8.speed_info(self._js8_rx_submode())["name"].lower()]})

    def fake_js8_period(self, slot_start: float, submode: int) -> list:
        """The events GnuRadioBackend sends for one decoded period: js8_frames, then js8_message for every
        message that changed. Every 4th period the neighbour calls CQ, every 4th + 2 it sends a heartbeat
        and -- with a station callsign set -- a report to us; our own frames come back in their period."""
        js8, msg = _js8_modules()
        from pluto_advanced_rx.js8_assembly import Js8Assembler
        from pluto_advanced_rx.js8_decoder import Js8Decode

        if self._js8_assembler is None:
            self._js8_assembler = Js8Assembler()
        n = int(slot_start // js8.speed_info(submode)["period_s"])
        call, grid = JS8_SIM_NEIGHBOUR
        texts = []
        if n % 4 == 0:
            texts.append((1200.0, msg.build_frames(call, grid, f"CQ CQ CQ {grid}", submode)[0]))
        elif n % 4 == 2:
            texts.append((700.0, msg.build_frames(call, grid, f"{call}: HEARTBEAT {grid}", submode)[0]))
            my_call = self.station.get("call") or ""
            if my_call:
                texts.append((1600.0, msg.build_frames(call, grid, f"{my_call} SNR -07", submode)[0]))
        for item in list(self._js8_injected):              # test hook: one frame of each per period
            texts.append((item[0], item[1].pop(0)))
            if not item[1]:
                self._js8_injected.remove(item)
        mine = [(sm, f, b) for s, sm, f, b in self._js8_sent if s == slot_start and sm == submode]
        self._js8_sent = [e for e in self._js8_sent if e[0] > slot_start]
        offset = float((self.tx.mode_params or {}).get("offset_hz", 1500.0))
        texts += [(offset, (f, b)) for _sm, f, b in mine]
        decodes = []
        for freq, (frame, flags) in texts:
            d = msg.decode_frame(frame, flags, submode)
            decodes.append(Js8Decode(frame, flags, submode, float(self._rng.integers(-20, 5)),
                                     round(float(self._rng.normal(0.0, 0.2)), 1), freq, d["message"], d["frame_type"]))
        speed = js8.speed_info(submode)["name"].lower()
        events = [("js8_frames", {
            "slot_start": slot_start, "utc": time.strftime("%H%M%S", time.gmtime(slot_start)), "speed": speed,
            "decodes": [{"snr_db": round(d.snr_db), "dt_s": d.dt_s, "freq_hz": round(d.freq_hz), "text": d.text,
                         "frame": d.frame, "flags": d.flags} for d in decodes],
        })]
        changed = self._js8_assembler.feed(slot_start, submode, decodes)
        changed += self._js8_assembler.expire(slot_start + js8.speed_info(submode)["period_s"])
        if self._js8_auto is not None:
            self._js8_auto.on_decodes(slot_start, submode, decodes)
        for m in changed:
            events.append(("js8_message", {
                "id": m.id, "speed": speed, "utc": time.strftime("%H%M%S", time.gmtime(m.first_slot)),
                "first_slot": m.first_slot, "last_slot": m.last_slot, "freq_hz": round(m.freq_hz),
                "snr_db": round(m.snr_db), "sender": m.sender, "to": m.to, "text": m.text, "closed": m.closed,
                "complete": m.complete and not m.incomplete, "incomplete": m.incomplete}))
        return events

    async def adsl_clear(self) -> None:
        self._adsl_state.clear()

    def adsl_snapshot(self) -> dict:
        return {"aircraft": self._adsl_state.get_aircraft_json()}

    def fake_adsl_packets(self) -> list[dict]:
        """One second of invented traffic: a circling glider, a transit, one in distress, one without a position."""
        lat0, lon0 = station.locator_center(self.station.get("locator", "")) or ADSL_DEFAULT_CENTER
        scale = math.cos(math.radians(lat0))
        t = self._adsl_tick
        self._adsl_tick += 1

        def packet(table, address, category, state, emergency, lat, lon, alt, speed, climb, track):
            channel = ADSL_CHANNELS_HZ[t % 2]
            self._adsl_counts[channel] += 1
            return {"version": 0, "key_index": 0, "kind": "traffic", "payload_type": 2, "raw": "",
                    "address_table": table[0], "address_table_name": table[1], "address": address, "relayed": False,
                    "timestamp_s": (t % 60) * 0.25, "category": category, "flight_state": state, "emergency": emergency,
                    "lat": lat, "lon": lon, "alt_m": alt, "speed_mps": speed, "climb_mps": climb, "track_deg": track,
                    "channel_hz": channel, "correlation": 0.9, "frequency_offset_hz": -300, "inverted": False}

        angle = t * 0.08  # about 80 s per circle
        return [
            packet((6, "FLARM"), 0xDD1234, "glider", "airborne", "none", lat0 + 0.12 + 0.015 * math.cos(angle),
                   lon0 - 0.15 + 0.015 * math.sin(angle) / scale, 1250, 25.0, 1.5, math.degrees(angle + math.pi / 2) % 360),
            packet((5, "ICAO"), 0x3C6444, "light fixed wing", "airborne", "none", lat0 - 0.25 + 0.0006 * (t % 900),
                   lon0 + 0.3 - 0.0009 * (t % 900) / scale, 1600, 62.0, 0.0, 305.0),
            packet((7, "OGN"), 0x07A1B2, "light rotorcraft", "airborne", "general", lat0 - 0.1, lon0 - 0.2 + 0.0005 * (t % 600) / scale,
                   420, 30.0, -2.0, 90.0),
            packet((0, "RND"), 0x51F0AA, "paraglider", "airborne", "none", None, None, None, None, None, None),
        ]

    def _aprs(self):
        if self._aprs_state is None:
            from pluto_advanced_rx.aprs_state import AprsState
            self._aprs_state = AprsState()
        return self._aprs_state

    def _aprs_check_tx(self) -> None:
        """The refusals an operator meets on the real backend."""
        params = self.tx.mode_params
        if not self.station.get("call"):
            raise SessionError("aprs: no callsign set")
        if params.get("kind") == "beacon" and not self.station.get("locator"):
            raise SessionError("aprs: beacon needs a position (station locator or latitude/longitude)")
        if params.get("kind") == "message" and not (params.get("to") and params.get("text")):
            raise SessionError("aprs: message needs an addressee and a text")

    def _aprs_sim_sent(self) -> dict:
        pluto_path.ensure_importable()
        from pluto_tx import aprs_codec
        params = self.tx.mode_params
        path = () if params["path"] == "none" else tuple(params["path"].split(","))
        if params["kind"] == "message":
            self._aprs_tick += 1
            info = aprs_codec.message_info(params["to"], params["text"], str(self._aprs_tick))
        else:
            lat, lon = station.locator_center(self.station["locator"])
            info = aprs_codec.beacon_info(lat, lon, params["symbol"], params["comment"])
        frame = aprs_codec.parse_frame(aprs_codec.build_frame(self.station["call"], "APZPTX", path, info))
        return {**aprs_codec.summarize(frame), "duration_s": 0.5, "automatic": False}

    async def aprs_clear(self) -> None:
        self._aprs().clear()

    async def aprs_auto_ack(self, enabled: bool) -> None:
        self._aprs_auto_ack = enabled
        await self._emit_event("aprs_auto_ack", {"enabled": enabled, "reason": "operator"})

    def aprs_snapshot(self) -> dict:
        return {"stations": self._aprs().get_stations(), "packets": self._aprs().get_rows(), "auto_ack": self._aprs_auto_ack}

    def fake_aprs_frames(self) -> list[dict]:
        """One round of invented traffic through the real codec: mobile, digipeater, weather, object, status."""
        pluto_path.ensure_importable()
        from pluto_tx import aprs_codec as c
        lat0, lon0 = station.locator_center(self.station.get("locator", "")) or ADSL_DEFAULT_CENTER
        self._aprs_tick += 1
        phase = (self._aprs_tick % 720) * 0.002
        wide = ("WIDE1-1", "WIDE2-1")
        infos = [
            ("DL1ABC-9", wide, c.beacon_info(lat0 + 0.2 - phase, lon0 + 0.3, "/>", "Sim mobil")),
            ("DB0XYZ-10", ("WIDE2-1",), c.beacon_info(lat0 - 0.4, lon0 - 0.5, "/#", "Sim Digipeater")),
            ("DK2WX", wide, f"@111111z{c.beacon_info(lat0 + 0.6, lon0 - 0.2, '/_', '').decode()[1:]}"
                            "220/004g005t059r000p000P000h50b10132".encode()),
            ("DB0OBJ", wide, f";438.600FM*111111z{c.beacon_info(lat0 + 0.1, lon0 + 0.7, '/r', '').decode()[1:]}Sim Relais".encode()),
            ("DL2STA", wide, b">Sim Status <b>ohne Position</b>"),
        ]
        if self.station.get("call") and self._aprs_tick % 6 == 1:
            infos.append(("DL1ABC-9", wide, c.message_info(self.station["call"], "Hallo aus dem Simulator", str(self._aprs_tick % 100))))
        return [c.parse_frame(c.build_frame(source, "APRS", path, info)) for source, path, info in infos]

    async def _aprs_loop(self) -> None:
        while True:
            await asyncio.sleep(APRS_SIM_PERIOD_S)
            if self.rx.connection is None or self.rx.mode != "aprs":
                continue
            rows = [self._aprs().on_frame(frame) for frame in self.fake_aprs_frames()]
            self._aprs_packets += len(rows)
            _rows, count = self._aprs().counts()
            await self._emit_event("aprs_update", {"stations": self._aprs().take_changed(), "packets": rows, "count": count,
                                                   "frames_ok": self._aprs_packets})
            for row in rows:
                if self._aprs_auto_ack and row["kind"] == "message" and row.get("addressee") == self.station.get("call"):
                    await self._emit_event("aprs_ack_result", {"to": row["source"], "message_id": row["message_id"], "sent": True, "reason": ""})

    async def adsb_clear(self) -> None:
        self._adsb_aircraft = {}

    def adsb_snapshot(self) -> dict:
        return {"aircraft": [dict(a, trail=list(a["trail"])) for a in self._adsb_aircraft.values()]}

    def fake_adsb_step(self, now: float) -> list[dict]:
        """One second of invented traffic in the fields AdsbState reports: airliners crossing, one in distress, one on
        the ground, one not located yet. Deliberately no encoder: nothing in the application can produce a message."""
        lat0, lon0 = station.locator_center(self.station.get("locator", "")) or ADSL_DEFAULT_CENTER
        scale = math.cos(math.radians(lat0))
        t = self._adsb_tick
        self._adsb_tick += 1
        phase = (t % 1800) * 0.0016
        fields = {
            "3C6444": {"callsign": "DLH4KA", "alt_ft": 36000, "alt_type": "baro", "speed_kt": 445.0, "track_deg": 85.0, "vrate_fpm": 0, "on_ground": False,
                           "lat": lat0 + 0.6, "lon": lon0 - 1.2 + phase / scale},
            "4CA7B5": {"callsign": "RYR82LM", "alt_ft": 24500, "alt_type": "baro", "speed_kt": 390.0, "track_deg": 205.0, "vrate_fpm": -1408, "on_ground": False,
                           "lat": lat0 + 1.0 - phase, "lon": lon0 + 0.5 - phase * 0.4 / scale},
            "406B21": {"callsign": "BAW912", "alt_ft": 7000, "alt_type": "baro", "speed_kt": 250.0, "track_deg": 120.0, "vrate_fpm": -640, "on_ground": False,
                           "lat": lat0 - 0.4 - phase * 0.3, "lon": lon0 - 0.6 + phase * 0.5 / scale, "emergency": "general", "squawk": "7700"},
            "3D1234": {"callsign": "DEABC", "alt_ft": None, "speed_kt": 12.0, "speed_type": "ground", "track_deg": 40.0, "on_ground": True,
                           "lat": lat0 + 0.02, "lon": lon0 + 0.05},
            "4B1A22": {"callsign": "SWR7", "alt_ft": 38000, "alt_type": "baro", "lat": None, "lon": None},
        }
        changed = []
        for icao, values in fields.items():
            a = self._adsb_aircraft.setdefault(icao, {"icao": icao, "messages": 0, "trail": [], "position_time": None})
            a.update(values)
            a["messages"] += 4
            a["time"] = now
            self._adsb_messages += 4
            if a["lat"] is not None:
                a["position_time"] = now
                a["trail"].append([now, a["lat"], a["lon"]])
                del a["trail"][:-200]
            changed.append({k: v for k, v in a.items() if k != "trail"})
        return changed

    async def _adsb_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            if self.rx.connection is None or self.rx.mode != "adsb":
                continue
            changed = self.fake_adsb_step(time.time())
            await self._emit_event("adsb_update", {"aircraft": changed, "count": len(self._adsb_aircraft),
                                                    "messages": self._adsb_messages, "corrected": 0, "crc_failures": 0,
                                                    "positions_rejected": 0})

    async def _adsl_loop(self) -> None:
        while True:
            await asyncio.sleep(ADSL_SIM_PERIOD_S)
            if self.rx.connection is None or self.rx.mode != "adsl":
                continue
            codec = _adsl_state_module().adsl_codec
            for info in self.fake_adsl_packets():
                row = self._adsl_state.on_packet(info)
                await self._emit_event("adsl_packet", {**codec.event_fields(info), "time": row["time"]})
            await self._emit_event("adsl_status", {"channels": [
                {"channel_hz": hz, "packets": count, "crc_failures": 0} for hz, count in self._adsl_counts.items()]})

    async def _ft8_loop(self) -> None:
        last_slot = None
        while True:
            now = time.time()
            slot = (now - FT8_DECODE_AT_S) // FT8_SLOT_S * FT8_SLOT_S + FT8_SLOT_S
            if slot == last_slot:  # asyncio.sleep() may wake a hair early: never report a slot twice
                slot += FT8_SLOT_S
            await asyncio.sleep(max(0.0, slot + FT8_DECODE_AT_S - time.time()))
            last_slot = slot
            if self.rx.connection is None or self.rx.mode != "ft8":
                continue
            await self._emit_event("ft8_slot", self.fake_ft8_slot(slot))
            self._ft8_slots += 1
            await self._emit_event("ft8_status", {"decoder": "sim", "slots_decoded": self._ft8_slots,
                                                   "last_error": "", "clock_synced": True})

    def fake_ft8_slot(self, slot_start: float) -> dict:
        """One ft8_slot event as GnuRadioBackend sends it: a CQ, a reply to
        our own callsign (if set), and some traffic between others."""
        n = int(slot_start // FT8_SLOT_S)
        stations = FT8_SIM_STATIONS
        cq_call, cq_loc = stations[n % len(stations)]
        a, _ = stations[(n + 1) % len(stations)]
        b, b_loc = stations[(n + 2) % len(stations)]
        texts = [f"CQ {cq_call} {cq_loc}", f"{a} {b} {b_loc}", f"{b} {a} R-11"]
        my_call = self.station.get("call") or ""
        if my_call:
            texts.append(f"{my_call} {cq_call} -07")
        texts += [text for slot, text in self._ft8_sent if slot == slot_start]  # loopback, like RTTY
        self._ft8_sent = [(slot, text) for slot, text in self._ft8_sent if slot > slot_start]
        decodes = []
        for i, text in enumerate(texts):
            words = text.split()
            sender_at = 2 if words[0] == "CQ" else 1
            sender = words[sender_at] if len(words) > sender_at else ""
            decodes.append({
                "snr_db": int(self._rng.integers(-22, 10)), "dt_s": round(float(self._rng.normal(0.1, 0.2)), 1),
                "freq_hz": 400 + 450 * i + int(self._rng.integers(0, 60)), "text": text, "sender": sender,
            })
        return {"slot_start": slot_start, "utc": time.strftime("%H%M%S", time.gmtime(slot_start)), "decodes": decodes}

    async def _spectrum_loop(self) -> None:
        period_s = 1.0 / SPECTRUM_RATE_HZ
        while True:
            await asyncio.sleep(period_s)
            if self.rx.connection is None:
                continue
            self._generation += 1
            await self._on_spectrum(SpectrumFrame(
                row=self._synthetic_row(), center_hz=self.rx.freq_hz, span_hz=DEFAULT_SPAN_HZ,
                generation=self._generation,
            ))

    async def _audio_loop(self) -> None:
        """A steady sine tone (frequency depends on RX mode, purely so a
        human can tell modes apart while listening) once RX is connected
        AND a mode is selected -- NOT a demodulation simulation, just
        enough to exercise the RX audio WS channel and the browser's
        Web Audio playback path (frontend/src/lib/audio.ts) end-to-end
        without real hardware."""
        n = int(AUDIO_SAMPLE_RATE_HZ * AUDIO_CHUNK_S)
        while True:
            await asyncio.sleep(AUDIO_CHUNK_S)
            if self.rx.connection is None or self.rx.mode is None or self.rx.mode in FIXED_TUNE_HZ:
                continue
            freq_hz = AUDIO_TONE_HZ.get(self.rx.mode, 500.0)
            t = (np.arange(n) + self._audio_phase) / AUDIO_SAMPLE_RATE_HZ
            samples = (0.2 * np.sin(2 * np.pi * freq_hz * t) * 32767).astype("<i2")
            self._audio_phase += n
            await self._on_audio(AudioFrame(pcm16=samples.tobytes(), sample_rate_hz=AUDIO_SAMPLE_RATE_HZ))

    def _synthetic_row(self) -> np.ndarray:
        """NOT a physical simulation -- a fixed noise floor plus a few fake
        carrier bumps at relative bin offsets, and (while keyed) a bump at
        TX's offset from RX center if it falls inside the displayed span.
        Only meant to give the waterfall widget something plausible-looking
        to render while developing against SimBackend."""
        row = -95.0 + self._rng.normal(0.0, 2.5, FFT_SIZE).astype(np.float32)
        for rel_bin, height in ((-600, 18.0), (-150, 10.0), (300, 14.0), (700, 8.0)):
            idx = FFT_SIZE // 2 + rel_bin
            if 0 <= idx < FFT_SIZE:
                row[max(0, idx - 3):idx + 4] += height
        if self.keyed and self.tx.connection is not None:
            offset_hz = self.tx.freq_hz - self.rx.freq_hz
            bin_width_hz = DEFAULT_SPAN_HZ / FFT_SIZE
            idx = int(FFT_SIZE // 2 + offset_hz / bin_width_hz)
            if 0 <= idx < FFT_SIZE:
                row[max(0, idx - 4):idx + 5] += 45.0
        return row


def _snapshot_direction(st: _DirectionState, keyed: bool) -> dict:
    return {
        "device_type": st.device_type,
        "connection": st.connection,
        "mode": st.mode,
        "mode_params": dict(st.mode_params),
        "freq_hz": st.freq_hz,
        "gains": dict(st.gains),
        "keyed": keyed,
    }


def _adsl_state_module():
    """pluto_advanced_rx.adsl_state (no GNU Radio or Qt), imported late like _js8_modules()."""
    pluto_path.ensure_importable()
    from pluto_advanced_rx import adsl_state

    return adsl_state


def _js8_modules():
    """pluto_tx.js8 / js8_message (numpy only, no GNU Radio), imported late like ft8_series.compose_text."""
    pluto_path.ensure_importable()
    from pluto_tx import js8, js8_message

    return js8, js8_message
