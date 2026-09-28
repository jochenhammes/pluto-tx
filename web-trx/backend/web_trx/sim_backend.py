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
import time
from dataclasses import dataclass, field

import numpy as np

from . import station
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
TX_MODES = ("fm", "ssb", "lsb", "m17", "rade", "pocsag", "rtty", "digitext")
RX_MODES = ("fm", "ssb", "lsb", "m17", "rade", "pocsag", "rtty", "ft8")

# FT8 receive: invented decodes per 15 s slot, delivered like pluto-tx's
# Ft8Receiver (~14.8 s after the slot start) -- enough to develop the decode
# table, the CQ filter and click-to-reply without hardware. Includes a CQ and
# a reply to the station's own callsign.
FT8_SLOT_S = 15.0
FT8_DECODE_AT_S = 14.8
FT8_SIM_STATIONS = (("DL1ABC", "JO62"), ("G4XYZ", "IO91"), ("OK1RR", "JN79"), ("EA3AAA", "JN11"))
ONE_SHOT_TX_MODES = ("pocsag", "rtty", "digitext")  # auto-unkey, like GnuRadioBackend
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
                "ft8": {"tx": "ft8" in TX_MODES, "rx_backends": ["sim"], "clock_synced": True}}

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
        if direction == "tx" and self.keyed:
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
        st.mode = mode
        st.mode_params = params

    async def tune(self, direction: str, freq_hz: float) -> None:
        # Allowed while disconnected too: the frequency is kept and used on
        # the next connect (same as GnuRadioBackend).
        self._state(direction).freq_hz = freq_hz

    async def set_gain(self, direction: str, name: str, value: float) -> None:
        self._state(direction).gains[name] = value

    async def ptt(self, on: bool) -> None:
        if on:
            if self.tx.connection is None or self.tx.mode is None:
                raise SessionError("tx: not connected or no mode selected")
            if self.keyed:
                return
            self.keyed = True
            await self._emit_event("keyed", {"mode": self.tx.mode})
            if self.tx.mode == "pocsag":
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
            if self._pocsag_unkey_task is not None:
                self._pocsag_unkey_task.cancel()
                self._pocsag_unkey_task = None
            if not self.keyed:
                return
            self.keyed = False
            await self._emit_event("unkeyed", {"mode": self.tx.mode})

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
            "tx": _snapshot_direction(self.tx, self.keyed),
            "rx": _snapshot_direction(self.rx, False),
        }

    async def shutdown(self) -> None:
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
        decodes = []
        for i, text in enumerate(texts):
            sender = text.split()[2] if text.startswith("CQ ") else text.split()[1]
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
            if self.rx.connection is None or self.rx.mode is None:
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
