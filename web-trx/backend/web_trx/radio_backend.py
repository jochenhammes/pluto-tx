"""GnuRadioBackend -- the real SessionBackend, wiring pluto-tx's
PlutoTxFlowgraph/AdvancedRxFlowgraph/FftProbe into the WebSocket session.

Deliberately thin, following pluto_cli (pluto_cli/runtime.py, rx.py, tx.py):
probe the connection with a bounded timeout, build the flowgraph, start it,
and translate the flowgraph's callbacks into session events. Everything
that blocks (probing, flowgraph construction, start/stop, key/unkey) runs
via asyncio.to_thread; callbacks from GNU Radio's scheduler threads are
handed back to the event loop with run_coroutine_threadsafe.

Scope of this first cut (see docs/PROJECT_PLAN.md section 9):
- RX: FM/SSB/LSB/M17/RADE demodulation, POCSAG and RTTY decoding (parallel
  digimode branch), waterfall rows from the flowgraph's own FftProbe,
  demodulated audio tapped off the flowgraph into the browser.
- TX: FM/SSB/LSB/M17/RADE from the browser microphone (the flowgraph's local
  mic source is swapped for a jitter-buffered WebSocket source, see
  tx_audio.py); POCSAG, RTTY and Waterfall Writer as one-shot text modes. A watchdog unkeys voice modes when the browser
  stops sending audio or the time-out timer expires.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from . import clock, pluto_path, station
from .session import AudioFrame, SessionBackend, SessionError, SpectrumFrame
from .tx_audio import JitterBuffer

pluto_path.ensure_importable()

from gnuradio import analog, gr
from gnuradio import filter as gr_filter
from pluto_advanced_rx import config as rx_config
from pluto_advanced_rx import devices as rx_devices
from pluto_advanced_rx.flowgraph import M17_AVAILABLE as RX_M17_AVAILABLE
from pluto_advanced_rx.flowgraph import RADE_AVAILABLE as RX_RADE_AVAILABLE
from pluto_advanced_rx.flowgraph import AdvancedRxFlowgraph
from pluto_advanced_rx.ft8_decoder import available_backends as ft8_rx_backends
from pluto_advanced_rx.ft8_decoder import sender_call as ft8_sender_call
from pluto_advanced_rx.pocsag_state import PocsagState
from pluto_tx import config as tx_config
from pluto_tx import devices as tx_devices
from pluto_tx.config import normalize_uri
from pluto_tx.flowgraph import M17_AVAILABLE as TX_M17_AVAILABLE
from pluto_tx.flowgraph import RADE_AVAILABLE as TX_RADE_AVAILABLE
from pluto_tx.flowgraph import PlutoTxFlowgraph

logger = logging.getLogger("web_trx.radio_backend")

SPECTRUM_POLL_HZ = 25.0
PROBE_TIMEOUT_S = 5.0
SCAN_TIMEOUT_S = 5.0

# Browser audio: the flowgraph's 48 kHz demod output, decimated by 3.
AUDIO_DECIMATION = 3
AUDIO_RATE_HZ = rx_config.AUDIO_RATE // AUDIO_DECIMATION
AUDIO_CHUNK_SAMPLES = AUDIO_RATE_HZ // 10  # 100 ms per WS frame, same cadence as SimBackend

RX_DEMOD_MODES = {
    "fm": AdvancedRxFlowgraph.MODE_FM,
    "ssb": AdvancedRxFlowgraph.MODE_SSB,
    "lsb": AdvancedRxFlowgraph.MODE_LSB,
    "m17": AdvancedRxFlowgraph.MODE_M17,
    "rade": AdvancedRxFlowgraph.MODE_RADE,
    # Digimodes run as a parallel branch next to a demodulator (one at a
    # time, fixed at construction -- pluto_cli/rx.py): POCSAG next to FM,
    # RTTY next to USB so the tones are audible too.
    "pocsag": AdvancedRxFlowgraph.MODE_FM,
    "rtty": AdvancedRxFlowgraph.MODE_SSB,
    "ft8": AdvancedRxFlowgraph.MODE_SSB,
}
RX_DIGIMODES = {"pocsag": "pocsag", "rtty": "rtty", "ft8": "ft8"}


def rx_bands() -> dict[str, list[float]]:
    """Receive band per RX mode as [low, high] offsets (Hz) from the tuned frequency, for the
    waterfall marker -- computed like pluto-advanced-rx's own demod-band shading
    (pluto_advanced_rx/gui.py _sync_waterfall()). Web-TRX never changes the filter widths, so
    the configured defaults are the real widths. FT8 has its own marker (the audio band)."""
    fm_half = rx_config.FM_DEMOD_WIDTH_DEFAULT_HZ / 2
    ssb_lo = rx_config.SSB_AUDIO_BAND_HZ[0]
    ssb_hi = ssb_lo + rx_config.SSB_DEMOD_WIDTH_DEFAULT_HZ
    m17_half = rx_config.M17_DEVIATION_HZ + rx_config.M17_SYMBOL_RATE
    return {
        "fm": [-fm_half, fm_half], "pocsag": [-fm_half, fm_half],
        "ssb": [ssb_lo, ssb_hi], "rtty": [ssb_lo, ssb_hi],
        "lsb": [-ssb_hi, -ssb_lo],
        "rade": [rx_config.RADE_OFDM_LOW_HZ, rx_config.RADE_OFDM_HIGH_HZ],
        "m17": [-m17_half, m17_half],
    }
SIDEBAND_RX_MODES = ("ssb", "lsb", "rtty", "ft8")  # squelch probe after the SSB filter
FT8_STATUS_S = 15.0  # ft8_status at least once per slot, and on every change
RTTY_FLUSH_S = 0.2
# Squelch (pluto-tx's RX flowgraph has none): average power of the
# channel-filtered IF signal, gates the audio sent to the browser.
# Decoders (POCSAG/RTTY/M17/RADE) tap earlier and are never gated.
SQUELCH_OFF_DB = -120.0  # at or below: squelch off (the slider's left end)
SQUELCH_HYSTERESIS_DB = 3.0
SQUELCH_HANG_S = 0.4
SQUELCH_POLL_S = 0.05
LEVEL_REPORT_S = 0.2
LEVEL_PROBE_ALPHA = 1e-3  # ~20 ms averaging at the 50 kHz IF rate  # received RTTY characters are sent in batches, not one WS message each
RADE_STATUS_S = 2.0
TX_MODES = {
    "fm": PlutoTxFlowgraph.MODE_FM,
    "ssb": PlutoTxFlowgraph.MODE_SSB,
    "lsb": PlutoTxFlowgraph.MODE_LSB,
    "m17": PlutoTxFlowgraph.MODE_M17,
    "rade": PlutoTxFlowgraph.MODE_RADE,
    "pocsag": PlutoTxFlowgraph.MODE_POCSAG,
    "rtty": PlutoTxFlowgraph.MODE_RTTY,
    "digitext": PlutoTxFlowgraph.MODE_DIGITEXT,
}
# One-shot modes render their whole transmission at key time and unkey on
# their own (pluto_cli.runtime.run_tx_session); all others take the
# browser microphone while PTT is held.
ONE_SHOT_TX_MODES = ("pocsag", "rtty", "digitext")
ONE_SHOT_TAIL_S = 0.3

# TX audio processing (FM/SSB/LSB; M17 bypasses this chain in pluto-tx).
# FM deviation/pre-emphasis/CTCSS are FM *mode* parameters instead
# (web_trx/modes.py), so they show up in the TX log with each over.
# name -> (default, PlutoTxFlowgraph setter, min, max). Defaults and ranges
# mirror pluto-tx's own GUI (pluto_tx/gui.py) and config.py.
TX_SETTINGS = {
    "nf_gain": (tx_config.DEFAULT_NF_GAIN, "set_nf_gain", 0.0, 3.0),
    "gate_enabled": (True, "set_gate_enabled", None, None),
    "gate_threshold_db": (tx_config.GATE_THRESHOLD_DB, "set_gate_threshold", -80.0, -20.0),
    "compressor_enabled": (True, "set_compressor_enabled", None, None),
    "compressor_threshold_db": (tx_config.COMPRESSOR_THRESHOLD_DB, "set_compressor_threshold", -40.0, 0.0),
    "compressor_ratio": (tx_config.COMPRESSOR_RATIO, "set_compressor_ratio", 1.0, 10.0),
    "limiter_enabled": (True, "set_limiter_enabled", None, None),
    "subtone_level_pct": (float(tx_config.SUBTONE_LEVEL_DEFAULT_PCT), "set_subtone_level",
                          *map(float, tx_config.SUBTONE_LEVEL_RANGE_PCT)),
}
# RX gain: "gain_mode" (one of the device's agc_modes) and one "stage:<NAME>"
# per gain stage the device declares (Pluto "gain", RTL-SDR "TUNER", HackRF
# "LNA"/"VGA"/"AMP") -- see pluto_advanced_rx/devices/base.py GainStage.
RX_GAIN_NAMES = ("gain_mode", "fft_zoom", "fft_avg", "fft_size", "sample_rate", "freq_correction_ppm",
                 "squelch_db", "direct_sampling")
# RTL-SDR direct sampling: the ADC samples the antenna input without the
# tuner -> HF up to 28.8 MHz (aliased above 14.4 MHz). "q" is the input the
# RTL-SDR Blog V3's HF port is wired to. pluto-tx: 0 off, 1 I, 2 Q.
DIRECT_SAMPLING_MODES = {"off": 0, "i": 1, "q": 2}
TX_GAIN_NAMES = ("power", "freq_correction_ppm")  # "power" is stored per device, see _Direction.power_by_device
# Oscillator error correction (pluto-tx: positive = the device runs too high).
# Two different devices easily differ by ~0.5 ppm (~70 Hz at 145 MHz) --
# enough to break RTTY (170 Hz shift), whose AFC deliberately ignores
# offsets below RTTY_AFC_DEADBAND_HZ.
PPM_RANGE = (-200.0, 200.0)
# Waterfall: FftProbe's zoom-FFT (FFT of fft_size*zoom samples, centre bins
# kept) stays sharp where cropping rows in the browser got pixelated. The
# zoom cap keeps the largest FFT at 2048*128 = 262144 points (the probe
# then computes fewer rows per second, not more CPU per row).
FFT_ZOOM_MAX = 128
FFT_AVG_MAX = 50
DEFAULT_FFT_SIZE = 2048

# Voice TX safety: unkey when the browser's microphone stream stops, and a
# time-out timer like a repeater's (WEB_TRX_TX_TIMEOUT_S overrides it).
MIC_SILENCE_UNKEY_S = 1.5
DEFAULT_TX_TIMEOUT_S = 180.0
MIC_BLOCK_S = 0.01  # granularity of the paced mic source

DEVICE_LABELS = {"pluto": "PlutoSDR", "hackrf": "HackRF One", "rtlsdr": "RTL-SDR"}
RX_DEVICE_TYPES = tuple(t for t in ("pluto", "hackrf", "rtlsdr") if t in rx_devices.DEVICE_REGISTRY)
TX_DEVICE_TYPES = tuple(t for t in ("pluto", "hackrf") if t in tx_devices.DEVICE_REGISTRY)


class _AudioTap(gr.sync_block):
    """Collects float audio from the flowgraph and hands 16-bit PCM chunks to
    `deliver` (called on GNU Radio's scheduler thread). A sink, so it never
    backpressures the demodulator."""

    def __init__(self, deliver):
        gr.sync_block.__init__(self, name="web_trx_audio_tap", in_sig=[np.float32], out_sig=None)
        self._deliver = deliver
        self._pending: list[np.ndarray] = []
        self._pending_len = 0

    def work(self, input_items, output_items):
        samples = input_items[0]
        self._pending.append(samples.copy())
        self._pending_len += len(samples)
        if self._pending_len >= AUDIO_CHUNK_SAMPLES:
            chunk = np.concatenate(self._pending)
            self._pending.clear()
            self._pending_len = 0
            pcm16 = (np.clip(chunk, -1.0, 1.0) * 32767).astype("<i2")
            self._deliver(pcm16.tobytes())
        return len(samples)


class _BrowserMicSource(gr.sync_block):
    """Stands in for pluto-tx's ALSA mic source: emits the jitter buffer's
    audio at AUDIO_RATE, paced by the wall clock like a real sound card.
    Without pacing, the source would run ahead filling every downstream
    buffer with silence, and live audio would then queue behind it
    (seconds of latency). Silence whenever no browser audio is due."""

    def __init__(self, buffer: JitterBuffer):
        gr.sync_block.__init__(self, name="web_trx_browser_mic", in_sig=None, out_sig=[np.float32])
        self._buffer = buffer
        self._rate = buffer.rate_hz
        self._t0: float | None = None
        self._produced = 0

    def work(self, input_items, output_items):
        out = output_items[0]
        now = time.monotonic()
        if self._t0 is None:
            self._t0 = now
        due = int((now - self._t0) * self._rate) - self._produced
        if due > self._rate // 4:
            # Stalled (downstream backpressure, scheduler hiccup) -- don't
            # burst to catch up, restart the clock instead.
            self._t0, self._produced, due = now, 0, 0
        if due <= 0:
            time.sleep(MIC_BLOCK_S)
            due = int(MIC_BLOCK_S * self._rate)
        n = min(len(out), due)
        out[:n] = self._buffer.pull(n)
        self._produced += n
        return n


@dataclass
class _Direction:
    device_type: str | None = None
    connection: str | None = None
    mode: str | None = None
    mode_params: dict = field(default_factory=dict)
    freq_hz: float = 432_500_000.0
    gains: dict = field(default_factory=dict)
    tb: object | None = None  # AdvancedRxFlowgraph | PlutoTxFlowgraph
    power_by_device: dict = field(default_factory=dict)  # TX only: device_type -> last power


class GnuRadioBackend(SessionBackend):
    def __init__(self):
        self.tx = _Direction(freq_hz=float(tx_config.DEFAULT_FREQUENCY))
        self.rx = _Direction(freq_hz=float(rx_config.DEFAULT_FREQUENCY))
        self.keyed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._spectrum_task: asyncio.Task | None = None
        self._unkey_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._mic_buffer = JitterBuffer(tx_config.AUDIO_RATE)
        self._keying = False
        self._over_stats = _new_over_stats()
        self._keyed_at = 0.0
        self._last_mic_at = 0.0
        self._tx_timeout_s = float(os.environ.get("WEB_TRX_TX_TIMEOUT_S", DEFAULT_TX_TIMEOUT_S))
        self._rx_lock = asyncio.Lock()
        self._tx_lock = asyncio.Lock()
        self._pocsag_rx_state = PocsagState()
        self._squelch_open = True
        self._rtty_lock = threading.Lock()
        self._rtty_pending: list[str] = []
        self._ft8_last_slot = 0.0
        # Server-side speaker output of the RX demodulator stays muted unless
        # asked for -- the operator listens in the browser.
        self._rx_local_audio = os.environ.get("WEB_TRX_RX_LOCAL_AUDIO", "false").lower() == "true"
        # Operator settings (frequencies, modes, mode parameters, power, TX
        # audio processing) survive a server restart when this is set --
        # scripts/start.sh points it at run/settings.json.
        self._settings_path = os.environ.get("WEB_TRX_SETTINGS_PATH")
        self._restore: dict[str, str] = {}  # direction -> connection to re-open at start ("" = auto)
        self._restoring: set[str] = set()     # directions _restore_connections() is still working on
        self.station = station.from_environment()  # start value; a saved one wins (_load_settings)
        self._load_settings()

    # -- SessionBackend --

    def features(self) -> dict:
        return {"fft_zoom_max": FFT_ZOOM_MAX, "fft_avg_max": FFT_AVG_MAX,
                "fft_sizes": rx_config.FFT_SIZE_PRESETS,
                "rx_modes": [m for m in RX_DEMOD_MODES if _rx_mode_available(m)],
                "rx_bands": rx_bands(),
                "rx_sample_rates": {t: list(rx_devices.DEVICE_REGISTRY[t].sample_rate_hz_choices)
                                    for t in RX_DEVICE_TYPES},
                "rx_gain": {t: _rx_gain_info(rx_devices.DEVICE_REGISTRY[t]) for t in RX_DEVICE_TYPES},
                "rx_direct_sampling": [t for t in RX_DEVICE_TYPES if _supports_direct_sampling(t)],
                # tx: FT8 transmit comes in phase F3 of docs/FT8_PLAN.md.
                "ft8": {"tx": "ft8" in TX_MODES, "rx_backends": ft8_rx_backends(),
                        "clock_synced": clock.ntp_synchronized()},
                "tx_modes": [m for m in TX_MODES if _tx_mode_available(m)]}

    def device_types(self) -> dict[str, list[list[str]]]:
        return {
            "rx": [[t, DEVICE_LABELS[t]] for t in RX_DEVICE_TYPES],
            "tx": [[t, DEVICE_LABELS[t]] for t in TX_DEVICE_TYPES],
        }

    async def scan(self, direction: str, device_type: str) -> dict[str, str]:
        device_cls = self._device_cls(direction, device_type)
        found, error = await asyncio.to_thread(device_cls.scan_devices_with_timeout, SCAN_TIMEOUT_S)
        if error is not None:
            raise SessionError(f"scan failed: {error}")
        return found

    async def connect(self, direction: str, device_type: str, connection: str) -> None:
        device_cls = self._device_cls(direction, device_type)
        connection = _resolve_connection(device_cls, connection)
        error = await asyncio.to_thread(device_cls.probe_with_timeout, connection, PROBE_TIMEOUT_S)
        if error is not None:
            raise SessionError(f"could not connect to {device_cls.display_name} ({connection or 'auto'}): {error}")
        await self.disconnect(direction)
        st = self._state(direction)
        st.device_type = device_type
        st.connection = connection
        self._save_settings()

    async def disconnect(self, direction: str) -> None:
        st = self._state(direction)
        if direction == "tx":
            async with self._tx_lock:
                await self._teardown_tx()
        else:
            async with self._rx_lock:
                await self._teardown_rx()
        # device_type/mode stay as the operator's preferred choice for the
        # next connect (and in the saved settings); `connection` is what
        # says whether the direction is connected.
        st.connection = None
        self._save_settings()

    async def select_mode(self, direction: str, mode: str, params: dict) -> None:
        st = self._state(direction)
        if st.connection is None:
            raise SessionError(f"{direction}: not connected")
        if direction == "rx":
            await self._select_rx_mode(mode, params)
        else:
            await self._select_tx_mode(mode, params)
        self._save_settings()

    async def tune(self, direction: str, freq_hz: float) -> None:
        st = self._state(direction)
        if direction == "tx" and self.keyed:
            raise SessionError("tx: cannot retune while keyed")
        # Allowed while disconnected: kept and used by the next flowgraph build.
        st.freq_hz = freq_hz
        if st.tb is not None:
            await asyncio.to_thread(st.tb.set_frequency, freq_hz)
        self._save_settings()

    async def set_gain(self, direction: str, name: str, value: float) -> None:
        st = self._state(direction)
        if direction == "tx" and name.startswith("stage:"):
            await self._set_tx_secondary_stage(name[6:], value)
            return
        if name == "freq_correction_ppm":
            value = float(value)
            if not PPM_RANGE[0] <= value <= PPM_RANGE[1]:
                raise SessionError(f"freq_correction_ppm must be within {PPM_RANGE[0]:g}..{PPM_RANGE[1]:g}")
        elif direction == "rx" and (name == "gain_mode" or name.startswith("stage:")):
            value = _check_rx_gain(st.device_type, name, value)
        elif direction == "tx" and name not in TX_GAIN_NAMES:
            value = _check_tx_setting(name, value)
        elif name not in (RX_GAIN_NAMES if direction == "rx" else TX_GAIN_NAMES):
            raise SessionError(f"unknown {direction} gain '{name}'")
        elif name == "sample_rate":
            value = int(value)
            choices = rx_devices.DEVICE_REGISTRY[st.device_type].sample_rate_hz_choices if st.device_type else ()
            if value not in choices:
                raise SessionError(f"sample_rate must be one of {list(choices)} for {st.device_type}")
        elif name == "fft_size" and int(value) not in rx_config.FFT_SIZE_PRESETS:
            raise SessionError(f"fft_size must be one of {rx_config.FFT_SIZE_PRESETS}")
        elif name == "direct_sampling":
            if value not in DIRECT_SAMPLING_MODES:
                raise SessionError(f"direct_sampling must be one of {list(DIRECT_SAMPLING_MODES)}")
            if value != "off" and not _supports_direct_sampling(st.device_type):
                raise SessionError(f"{st.device_type} has no direct sampling (RTL-SDR only)")
        if direction == "tx" and name == "power":
            if st.device_type is None:
                raise SessionError("tx: select a device first")
            st.power_by_device[st.device_type] = float(value)
        else:
            st.gains[name] = value
        self._save_settings()
        tb = st.tb
        if name in ("sample_rate", "direct_sampling"):
            # Both are fixed per flowgraph: rebuild in the current mode.
            if tb is not None and st.mode:
                await self._select_rx_mode(st.mode, st.mode_params)
            return
        if direction == "tx" and name not in TX_GAIN_NAMES:
            if tb is not None:
                await asyncio.to_thread(_apply_tx_setting, tb, name, value)
            await self._emit_event("tx_settings", {"settings": self._tx_settings()})
            return
        if direction == "rx":
            # Wait for a rebuild in progress (mode change, connect) to finish: it may already have
            # read the settings, so "applied when the flowgraph is built" would lose this change.
            async with self._rx_lock:
                tb = st.tb
                if tb is not None:
                    await asyncio.to_thread(_apply_rx_gain, tb, name, value)
            return
        if tb is None:
            return  # applied when the flowgraph is built
        else:
            await asyncio.to_thread(_apply_tx_gain, tb, name, value)
            await self._emit_event("tx_power", _power_info(tb))

    def _load_settings(self) -> None:
        if not self._settings_path or not os.path.exists(self._settings_path):
            return
        try:
            with open(self._settings_path) as f:
                saved = json.load(f)
            if isinstance(saved.get("station"), dict):
                try:
                    self.station = station.normalize(saved["station"].get("call", ""),
                                                     saved["station"].get("locator", ""))
                except ValueError:
                    logger.warning("ignoring invalid saved station data %r", saved["station"])
            for direction in ("rx", "tx"):
                st, d = self._state(direction), saved.get(direction, {})
                st.freq_hz = float(d.get("freq_hz", st.freq_hz))
                st.mode = d.get("mode")
                st.mode_params = dict(d.get("mode_params") or {})
                st.device_type = d.get("device_type")
                if d.get("connected") and st.device_type and st.mode:
                    self._restore[direction] = d["connection"]
                gains = dict(d.get("gains") or {})
                gains.pop("power_unlocked", None)  # an older setting, no longer used
                if direction == "tx":
                    if isinstance(d.get("power_by_device"), dict):
                        gains["power_by_device"] = d["power_by_device"]
                    self.tx.power_by_device = _load_tx_powers(gains, st.device_type)
                for name, value in gains.items():
                    try:
                        if direction == "tx" and name not in TX_GAIN_NAMES:
                            value = _check_tx_setting(name, value)
                        st.gains[name] = value
                    except SessionError:
                        logger.warning("ignoring saved %s setting %s=%r", direction, name, value)
        except (OSError, ValueError, TypeError):
            logger.exception("could not load %s, starting with defaults", self._settings_path)

    def _save_settings(self) -> None:
        if not self._settings_path:
            return
        data = {
            direction: {
                "freq_hz": st.freq_hz, "mode": st.mode, "mode_params": st.mode_params,
                "device_type": st.device_type, "gains": st.gains,
                # restored on the next server start; "" is a valid connection
                # (auto-detect, e.g. RTL-SDR/HackRF), hence the explicit flag
                "connected": st.connection is not None, "connection": st.connection,
            }
            for direction, st in (("rx", self.rx), ("tx", self.tx))
        }
        data["tx"]["power_by_device"] = self.tx.power_by_device
        data["station"] = self.station
        tmp = self._settings_path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self._settings_path)
        except OSError:
            logger.exception("could not save %s", self._settings_path)

    async def _set_tx_secondary_stage(self, stage_name: str, value) -> None:
        """A non-primary TX power stage (HackRF: "AMP", the +14 dB RF amp).
        Deliberately not saved: every new TX flowgraph (connect, restart)
        starts with it off, the device's own safe build state."""
        tb = self.tx.tb
        if tb is None:
            raise SessionError("tx: not connected")
        stage = next((p for p in tb.device.power_stages if p.name == stage_name and not p.is_primary), None)
        if stage is None:
            raise SessionError(f"{self.tx.device_type} has no switchable power stage '{stage_name}'")
        if stage.kind == "bool":
            value = bool(value)
        else:
            value = float(value)
            if not stage.min_value <= value <= stage.max_value:
                raise SessionError(f"{stage.label} must be within {stage.min_value:g}..{stage.max_value:g}")
        await asyncio.to_thread(tb.set_secondary_power, stage.name, value)
        tb.web_trx_secondary_power[stage.name] = value
        await self._emit_event("tx_power", _power_info(tb))

    def set_station(self, value: dict) -> None:
        self.station = dict(value)
        self._save_settings()
        if self.rx.mode == "ft8" and self.rx.tb is not None and self._loop is not None:
            # jt9 gets the station data at construction only.
            self._loop.create_task(self._select_rx_mode("ft8", self.rx.mode_params))

    def _tx_settings(self) -> dict:
        """Effective TX audio-processing settings: pluto-tx defaults,
        overridden by whatever the operator changed."""
        return {name: self.tx.gains.get(name, spec[0]) for name, spec in TX_SETTINGS.items()}

    async def ptt(self, on: bool) -> None:
        if on:
            await self._key()
        else:
            self._cancel_tx_tasks()
            await self._unkey()

    async def estop(self) -> None:
        """Never raises, never waits for the TX lock (a key/unkey in progress
        must not delay it). shutdown_safe() unkeys, stops the flowgraph and
        forces the device's safe state (Pluto: max attenuation, TX LO off)."""
        self._cancel_tx_tasks()
        was_keyed = self.keyed
        mode = self.tx.mode
        self.keyed = False
        tb, self.tx.tb = self.tx.tb, None
        self.tx.mode = None
        self._mic_buffer.clear()
        if tb is not None:
            try:
                await asyncio.to_thread(tb.shutdown_safe)
            except Exception:  # E-STOP must not raise
                logger.exception("shutdown_safe() failed during E-STOP")
        await self._emit_event("estop", {})
        if was_keyed:
            await self._emit_event("unkeyed", {"mode": mode})

    async def submit_tx_audio(self, frame: AudioFrame) -> None:
        if not (self.keyed or self._keying) or self.tx.mode in (None, *ONE_SHOT_TX_MODES):
            return
        now = time.monotonic()
        self._mic_buffer.push_pcm16(frame.pcm16, frame.sample_rate_hz)
        st = self._over_stats
        if st["frames"]:
            st["max_gap_s"] = max(st["max_gap_s"], now - self._last_mic_at)
        else:
            st["first_at"] = now
        st["frames"] += 1
        st["seconds"] += len(frame.pcm16) / 2 / frame.sample_rate_hz
        st["rate_hz"] = frame.sample_rate_hz
        self._last_mic_at = now

    def snapshot(self) -> dict:
        tx = _snapshot_direction(self.tx, self.keyed)
        tx["power"] = _power_info(self.tx.tb) if self.tx.tb is not None else None
        tx["settings"] = self._tx_settings()
        return {
            "tx": tx,
            "rx": _snapshot_direction(self.rx, False),
        }

    async def shutdown(self) -> None:
        if self._spectrum_task is not None:
            self._spectrum_task.cancel()
            self._aux_task.cancel()
            self._squelch_task.cancel()
            self._spectrum_task = None
        self._cancel_tx_tasks()
        self.keyed = False
        for tb in (self.tx.tb, self.rx.tb):
            if tb is None:
                continue
            try:
                await asyncio.to_thread(_stop_flowgraph, tb)
            except Exception:
                logger.exception("flowgraph teardown failed during shutdown")
        self.tx.tb = self.rx.tb = None

    def start_background_tasks(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self._spectrum_task is None:
            self._spectrum_task = asyncio.create_task(self._spectrum_loop())
            self._aux_task = asyncio.create_task(self._rx_aux_loop())
            self._squelch_task = asyncio.create_task(self._squelch_loop())
        if self._restore:
            self._restore_task = asyncio.create_task(self._restore_connections())

    async def _restore_connections(self) -> None:
        """After a server restart: re-open the RX/TX connections that were
        open before, in their saved mode, so a reconnecting browser finds
        everything as it was. Builds flowgraphs only -- never keys."""
        restore, self._restore = self._restore, {}
        self._restoring = set(restore)
        for direction in ("rx", "tx"):
            connection = restore.get(direction)
            if connection is None:
                continue
            st = self._state(direction)
            mode, params = st.mode, dict(st.mode_params)
            try:
                await self.connect(direction, st.device_type, connection)
                await self._emit_event("connected", {"direction": direction})
                await self.select_mode(direction, mode, params)
                await self._emit_event("mode", {"direction": direction, "mode": mode, "params": params})
                logger.warning("restored %s connection %s (%s)", direction, connection, mode)
            except SessionError as e:
                logger.warning("could not restore %s connection %s: %s", direction, connection, e)
                await self._emit_event("error", {"message": f"{direction}: Wiederverbinden fehlgeschlagen: {e}",
                                                 "direction": direction})
            finally:
                self._restoring.discard(direction)

    def restoring(self) -> set[str]:
        """Directions whose saved connection is about to be re-opened (server
        start) -- reported by /health so the local Qt apps treat that device as
        taken already, not only once the restore has finished."""
        return set(self._restore) | self._restoring

    # -- RX --

    async def _select_rx_mode(self, mode: str, params: dict) -> None:
        if mode not in RX_DEMOD_MODES:
            raise SessionError(f"unsupported rx mode '{mode}'")
        if not _rx_mode_available(mode):
            raise SessionError(f"{mode.upper()} is not available on this server (see pluto-tx install-{mode}.sh)")
        st = self.rx
        async with self._rx_lock:
            # active_digimode is fixed at construction (pluto_cli/rx.py), so a
            # mode change is a rebuild, exactly like the CLI's one-flowgraph-
            # per-invocation model.
            await self._teardown_rx()
            try:
                st.tb = await asyncio.to_thread(self._build_rx, mode, params)
            except Exception as e:
                raise SessionError(f"rx: could not start {st.device_type} ({st.connection}): {e}") from e
            st.mode = mode
            st.mode_params = dict(params)

    def _build_rx(self, mode: str, params: dict):
        st = self.rx
        gain_mode, manual_gain_db, gain_values = _rx_gain_build_args(
            rx_devices.DEVICE_REGISTRY[st.device_type], st.gains)
        tb = AdvancedRxFlowgraph(
            device_type=st.device_type, uri=st.connection, frequency=st.freq_hz,
            gain_mode=gain_mode, manual_gain_db=manual_gain_db, gain_values=gain_values,
            demod_mode=RX_DEMOD_MODES[mode],
            sample_rate=self._rx_sample_rate(),
            direct_sampling=(DIRECT_SAMPLING_MODES.get(st.gains.get("direct_sampling", "off"), 0)
                             if _supports_direct_sampling(st.device_type) else 0),
            frequency_correction_ppm=float(st.gains.get("freq_correction_ppm", 0.0)),
            fm_deemphasis=bool(params.get("deemphasis", rx_config.FM_DEEMPH_DEFAULT)),
            active_digimode=RX_DIGIMODES.get(mode),
            on_pocsag_message=self._on_pocsag_message,
            on_m17_fields=self._on_m17_fields,
            on_rtty_char=self._on_rtty_char,
            rtty_mark_hz=params.get("mark_hz", rx_config.RTTY_MARK_HZ_DEFAULT),
            rtty_shift_hz=params.get("shift_hz", rx_config.RTTY_SHIFT_HZ_DEFAULT),
            rtty_baud_rate=params.get("baud", rx_config.RTTY_BAUD_RATE_DEFAULT),
            rtty_reverse=bool(params.get("reverse", False)),
            # FT8: decoded once per 15 s slot in pluto-tx' own worker thread;
            # the station data lets jt9 decode replies to us a priori.
            on_ft8_decodes=self._on_ft8_decodes,
            ft8_backend=params.get("decoder", "auto"),
            ft8_my_call=self.station.get("call", ""), ft8_my_grid=self.station.get("locator", ""),
        )
        try:
            decim = gr_filter.rational_resampler_fff(1, AUDIO_DECIMATION)
            tap = _AudioTap(self._deliver_audio)
            tb.connect(tb.nf_gain, decim, tap)
            # connect() holds no Python reference -- without this, the
            # gateway block gets garbage-collected under the running
            # scheduler (seen as a C++ terminate on real hardware).
            tb.web_trx_audio_tap = (decim, tap)
            # Channel power for squelch and level meter: after the FM
            # channel filter, or the SSB filter for sideband modes.
            probe = analog.probe_avg_mag_sqrd_c(0.0, LEVEL_PROBE_ALPHA)
            tb.connect(tb.ssb_filter if mode in SIDEBAND_RX_MODES else tb.fm_channel_filter, probe)
            tb.web_trx_level_probe = probe
            tb.set_fft_size(int(st.gains.get("fft_size", DEFAULT_FFT_SIZE)))
            for name in ("fft_zoom", "fft_avg"):
                if name in st.gains:
                    _apply_rx_gain(tb, name, st.gains[name])
            tb.start()
            tb.set_rx_muted(not self._rx_local_audio)
        except Exception:
            tb.shutdown()
            raise
        return tb

    def _rx_sample_rate(self) -> int | None:
        """Saved choice if the current device supports it, else the device default."""
        rate = self.rx.gains.get("sample_rate")
        choices = rx_devices.DEVICE_REGISTRY[self.rx.device_type].sample_rate_hz_choices
        return int(rate) if rate is not None and int(rate) in choices else None

    async def _teardown_rx(self) -> None:
        tb, self.rx.tb = self.rx.tb, None
        if tb is not None:
            await asyncio.to_thread(tb.shutdown)

    async def _spectrum_loop(self) -> None:
        period_s = 1.0 / SPECTRUM_POLL_HZ
        generation = -1
        while True:
            await asyncio.sleep(period_s)
            tb = self.rx.tb
            if tb is None:
                continue
            try:
                # Every row made since the last poll, not only the newest: the probe makes ~30 rows/s,
                # a 25 Hz poll that keeps only the latest dropped every few and made the waterfall
                # advance in uneven steps. The browser paces the drawing itself.
                probe = tb.fft_probe
                rows, latest = probe.get_rows_since(generation)
                if latest < generation:  # a rebuilt flowgraph starts counting again
                    rows, latest = probe.get_rows_since(-1)
                generation = latest
                span_hz = tb.sample_rate / probe.zoom
                for row in rows:
                    await self._on_spectrum(SpectrumFrame(
                        row=row, center_hz=tb.nominal_freq_hz, span_hz=span_hz, generation=generation,
                    ))
            except Exception:  # keep the loop alive across rebuilds
                logger.exception("spectrum poll failed")

    async def _squelch_loop(self) -> None:
        """Opens at the threshold, closes SQUELCH_HYSTERESIS_DB below it
        after SQUELCH_HANG_S (no chopping on fades/syllable gaps). Reports
        the level for the frontend's meter."""
        last_report = closed_since = 0.0
        while True:
            await asyncio.sleep(SQUELCH_POLL_S)
            tb = self.rx.tb
            probe = getattr(tb, "web_trx_level_probe", None)
            if probe is None:
                self._squelch_open = True
                continue
            level_db = 10.0 * np.log10(max(probe.level(), 1e-20))
            threshold = float(self.rx.gains.get("squelch_db", SQUELCH_OFF_DB))
            now = time.monotonic()
            if threshold <= SQUELCH_OFF_DB or level_db >= threshold:
                self._squelch_open, closed_since = True, 0.0
            elif level_db < threshold - SQUELCH_HYSTERESIS_DB:
                closed_since = closed_since or now
                if now - closed_since >= SQUELCH_HANG_S:
                    self._squelch_open = False
            if now - last_report >= LEVEL_REPORT_S:
                last_report = now
                await self._emit_event("rx_level", {"level_db": round(level_db, 1), "squelch_open": self._squelch_open})

    def _deliver_audio(self, pcm16: bytes) -> None:
        if not self._squelch_open:
            return  # the browser's player just runs dry (silence), no bandwidth used
        self._from_gr_thread(self._on_audio(AudioFrame(pcm16=pcm16, sample_rate_hz=AUDIO_RATE_HZ)))

    def _on_pocsag_message(self, msg: dict) -> None:
        if msg.get("uncorrectable"):
            return  # usually junk from a weak signal, pluto-cli hides these by default too
        self._pocsag_rx_state.on_message(msg)
        row = self._pocsag_rx_state.get_snapshot()[1][-1]
        self._from_gr_thread(self._emit_event("pocsag_message", {
            "direction": "rx", "baud": row["baud"], "ric": row["ric"], "function": row["function"],
            "text": row["text"], "corrected": row["corrected"],
        }))

    def _on_rtty_char(self, char: str) -> None:
        with self._rtty_lock:  # GNU Radio thread; flushed by _rx_aux_loop
            self._rtty_pending.append(char)

    async def _rx_aux_loop(self) -> None:
        """Periodic RX housekeeping pluto-tx's GUI does from a Qt timer
        (pluto_cli.runtime.run_rx_session does it in a sleep loop): RTTY
        AFC steps (nothing inside the flowgraph calls them), batched RTTY
        text, RADE sync/SNR status."""
        last_afc = last_rade = last_ft8 = 0.0
        last_rade_state = last_ft8_state = None
        while True:
            await asyncio.sleep(RTTY_FLUSH_S)
            tb, mode = self.rx.tb, self.rx.mode
            with self._rtty_lock:
                text, self._rtty_pending = "".join(self._rtty_pending), []
            if tb is None:
                continue
            now = time.monotonic()
            try:
                if text:
                    await self._emit_event("rtty_text", {"text": text})
                if mode == "rtty" and now - last_afc >= rx_config.RTTY_AFC_POLL_INTERVAL_S:
                    last_afc = now
                    await asyncio.to_thread(tb.rtty_afc_step)
                receiver = getattr(tb, "ft8_receiver", None)
                if mode == "ft8" and receiver is not None:
                    decoder = getattr(receiver.decoder, "backend", None)
                    state = (decoder, receiver.slots_decoded, receiver.last_error)
                    if state != last_ft8_state or now - last_ft8 >= FT8_STATUS_S:
                        last_ft8_state, last_ft8 = state, now
                        await self._emit_event("ft8_status", {
                            "decoder": decoder, "slots_decoded": receiver.slots_decoded,
                            "last_error": receiver.last_error, "clock_synced": clock.ntp_synchronized(),
                        })
                if mode == "rade" and getattr(tb, "rade_decoder", None) is not None:
                    dec = tb.rade_decoder
                    state = (dec.synced, round(dec.snr_db), round(dec.freq_offset_hz))
                    if state != last_rade_state or now - last_rade >= RADE_STATUS_S:
                        last_rade_state, last_rade = state, now
                        await self._emit_event("rade_status", {
                            "synced": dec.synced, "snr_db": round(dec.snr_db, 1),
                            "freq_offset_hz": round(dec.freq_offset_hz, 1),
                        })
            except Exception:  # keep the loop alive across rebuilds
                logger.exception("rx housekeeping failed")

    def _on_ft8_decodes(self, slot_start: float, decodes: list) -> None:
        """Decoder thread -> one ft8_slot event per slot with all decodes
        (a busy band yields dozens at once). A slot reported twice (the
        decoder's wait can wake a hair early) is dropped."""
        if slot_start <= self._ft8_last_slot:
            return
        self._ft8_last_slot = slot_start
        utc = time.strftime("%H%M%S", time.gmtime(slot_start))
        self._from_gr_thread(self._emit_event("ft8_slot", {
            "slot_start": slot_start, "utc": utc,
            "decodes": [
                {"snr_db": round(d.snr_db), "dt_s": round(d.dt_s, 1), "freq_hz": round(d.freq_hz),
                 "text": d.text, "sender": ft8_sender_call(d.text)}
                for d in decodes
            ],
        }))

    def _on_m17_fields(self, fields: dict) -> None:
        self._from_gr_thread(self._emit_event("m17_fields", _json_safe(fields)))

    def _from_gr_thread(self, coro) -> None:
        if self._loop is None or self._loop.is_closed():
            coro.close()
            return
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    # -- TX --

    async def _select_tx_mode(self, mode: str, params: dict) -> None:
        if mode not in TX_MODES:
            raise SessionError(f"unsupported tx mode '{mode}'")
        if not _tx_mode_available(mode):
            raise SessionError(f"{mode.upper()} is not available on this server (see pluto-tx install-{mode}.sh)")
        if self.keyed:
            raise SessionError("tx: cannot change mode while keyed")
        st = self.tx
        async with self._tx_lock:
            if st.tb is None:
                try:
                    st.tb = await asyncio.to_thread(self._build_tx, mode)
                except Exception as e:
                    raise SessionError(f"tx: could not start {st.device_type} ({st.connection}): {e}") from e
            elif st.mode != mode:
                # Live switch, no rebuild -- the same path pluto-tx's own GUI uses.
                await asyncio.to_thread(st.tb.set_mode, TX_MODES[mode])
            try:
                await asyncio.to_thread(_apply_tx_params, st.tb, mode, params)
            except (TypeError, ValueError) as e:
                raise SessionError(f"tx: invalid {mode} parameters: {e}") from e
            st.mode = mode
            st.mode_params = dict(params)
        await self._emit_event("tx_power", _power_info(st.tb))

    def _build_tx(self, mode: str):
        st = self.tx
        tb = PlutoTxFlowgraph(
            device_type=st.device_type, connection=st.connection, frequency=st.freq_hz,
            mode=TX_MODES[mode], source=PlutoTxFlowgraph.SRC_MIC,
        )
        try:
            # Swap the server's local microphone for the browser's: the
            # selector's mic input gets our jitter-buffered source instead.
            # Before start(), so no lock()/unlock() is needed.
            mic = _BrowserMicSource(self._mic_buffer)
            tb.disconnect(tb.mic_source, (tb.source_selector, PlutoTxFlowgraph.SRC_MIC))
            tb.connect(mic, (tb.source_selector, PlutoTxFlowgraph.SRC_MIC))
            tb.web_trx_mic_source = mic  # see _build_rx: connect() keeps no Python reference
            # The slider always spans the device's whole range (Pluto: up to
            # 0 dB attenuation) -- unlike pluto-tx's GUI there's no separate
            # unlock; the safe default only applies after a restart (see
            # _load_settings) and to a first connect without a saved power.
            tb.power_ceiling = tb.device.primary_stage.max_value
            tb.web_trx_secondary_power = {}  # e.g. HackRF AMP: always off on a new flowgraph
            # Each device keeps its own last power (a HackRF VGA gain of 20 dB
            # means something else than a Pluto attenuation); none saved yet
            # -> the device's safe default the flowgraph starts with.
            saved_power = st.power_by_device.get(st.device_type)
            if saved_power is not None:
                tb.set_target_power(saved_power)
            if st.gains.get("freq_correction_ppm"):
                tb.device.set_frequency_correction_ppm(float(st.gains["freq_correction_ppm"]))
            for name, value in self._tx_settings().items():
                _apply_tx_setting(tb, name, value)
            tb.start()
        except Exception:
            tb.shutdown_safe()
            raise
        return tb

    async def _teardown_tx(self) -> None:
        self._cancel_tx_tasks()
        self.keyed = False
        tb, self.tx.tb = self.tx.tb, None
        if tb is not None:
            await asyncio.to_thread(tb.shutdown_safe)

    def _cancel_tx_tasks(self) -> None:
        for task in (self._unkey_task, self._watchdog_task):
            if task is not None:
                task.cancel()
        self._unkey_task = self._watchdog_task = None

    async def _key(self) -> None:
        async with self._tx_lock:
            tb = self.tx.tb
            mode = self.tx.mode
            if tb is None or mode is None:
                raise SessionError("tx: not connected or no mode selected")
            if self.keyed:
                return
            problem = _tx_problem(tb, mode)
            if problem:
                raise SessionError(f"{mode}: {problem}")
            self._mic_buffer.clear()
            self._over_stats = _new_over_stats()
            # Accept mic audio while the device is still keying up (Pluto: LO
            # relock), so the first syllable isn't dropped.
            self._keying = True
            try:
                await asyncio.to_thread(tb.key_ptt)
            except ValueError as e:  # a refusal from the flowgraph, before any RF
                raise SessionError(str(e)) from e
            finally:
                self._keying = False
            self.keyed = True
            self._keyed_at = self._last_mic_at = time.monotonic()
        await self._emit_event("keyed", {"mode": mode})
        if mode == "pocsag":
            await self._emit_event("pocsag_message", {
                "direction": "tx", "ric": self.tx.mode_params.get("ric"),
                "text": self.tx.mode_params.get("text", ""), "duration_s": round(tb.pocsag_duration_s, 3),
            })
            self._unkey_task = asyncio.create_task(self._auto_unkey_after(tb.pocsag_hold_s))
        elif mode in ONE_SHOT_TX_MODES:
            # The rendered duration is only known after key_ptt() (it renders there).
            duration_s = tb.rtty_duration_s if mode == "rtty" else tb.digitext_duration_s
            await self._emit_event("tx_text", {
                "mode": mode, "text": self.tx.mode_params.get("text", ""), "duration_s": round(duration_s, 2),
            })
            self._unkey_task = asyncio.create_task(self._auto_unkey_after(duration_s + ONE_SHOT_TAIL_S))
        else:
            self._watchdog_task = asyncio.create_task(self._audio_watchdog())

    async def _auto_unkey_after(self, seconds: float) -> None:
        """Same self-cancellation trap as SimBackend._auto_unkey_after (see
        docs/DEBUGGING.md): clear the task reference before unkeying."""
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        self._unkey_task = None
        await self._unkey()

    async def _audio_watchdog(self) -> None:
        """While keyed in a voice mode: unkey if the browser stops sending
        microphone audio (closed tab, dropped network -- otherwise the
        transmitter would sit on an open carrier) or the time-out timer
        runs out. Reports jitter-buffer stats once a second."""
        last_report = 0.0
        try:
            while self.keyed:
                await asyncio.sleep(0.2)
                now = time.monotonic()
                reason = None
                if now - self._last_mic_at > MIC_SILENCE_UNKEY_S:
                    reason = f"no microphone audio for {MIC_SILENCE_UNKEY_S:g} s"
                elif now - self._keyed_at > self._tx_timeout_s:
                    reason = f"time-out timer ({self._tx_timeout_s:g} s)"
                if reason:
                    self._watchdog_task = None
                    await self._emit_event("error", {"message": f"tx: unkeyed automatically -- {reason}"})
                    await self._unkey()
                    return
                if now - last_report >= 1.0:
                    last_report = now
                    await self._emit_event("tx_audio", self._tx_audio_stats())
        except asyncio.CancelledError:
            return

    async def _unkey(self) -> None:
        async with self._tx_lock:
            if not self.keyed:
                return
            self.keyed = False
            tb = self.tx.tb
            if tb is not None:
                await asyncio.to_thread(tb.unkey_ptt)
                if self.tx.mode == "m17":
                    # EOT tail, then RF down -- pluto_cli.runtime._tail_wait_tx().
                    await asyncio.sleep(tx_config.M17_EOT_HOLD_S)
                    await asyncio.to_thread(tb.finish_unkey_m17)
                elif self.tx.mode == "rade" and getattr(tb, "rade_eoo_enabled", False):
                    await asyncio.sleep(tb.rade_eoo_hold_s)
                    await asyncio.to_thread(tb.finish_unkey_rade)
            if self.tx.mode not in ONE_SHOT_TX_MODES:
                stats = self._tx_audio_stats()
                logger.warning("tx over done: %s", stats)
                await self._emit_event("tx_audio", {**stats, "final": True})
            self._mic_buffer.clear()
        await self._emit_event("unkeyed", {"mode": self.tx.mode})

    def _tx_audio_stats(self) -> dict:
        """Per-over microphone diagnostics: what the browser delivered
        (audio seconds vs. wall-clock seconds, largest gap between frames)
        and what the jitter buffer had to do about it."""
        st = self._over_stats
        wall_s = (self._last_mic_at - st["first_at"]) if st["frames"] > 1 else 0.0
        tb = self.tx.tb
        return {
            "compressor_gr_db": round(tb.compressor.gain_reduction_db(), 1) if tb is not None else None,
            "buffer_ms": round(self._mic_buffer.buffered_s * 1000),
            "underruns": self._mic_buffer.underruns,
            "dropped_ms": round(self._mic_buffer.dropped_samples / tx_config.AUDIO_RATE * 1000),
            "frames": st["frames"], "audio_s": round(st["seconds"], 2), "wall_s": round(wall_s, 2),
            "max_gap_ms": round(st["max_gap_s"] * 1000), "rate_hz": st["rate_hz"],
        }

    # -- helpers --

    def _state(self, direction: str) -> _Direction:
        if direction == "tx":
            return self.tx
        if direction == "rx":
            return self.rx
        raise SessionError(f"unknown direction '{direction}'")

    def _device_cls(self, direction: str, device_type: str):
        self._state(direction)
        allowed = RX_DEVICE_TYPES if direction == "rx" else TX_DEVICE_TYPES
        if device_type not in allowed:
            raise SessionError(f"unknown {direction} device type '{device_type}'")
        registry = rx_devices.DEVICE_REGISTRY if direction == "rx" else tx_devices.DEVICE_REGISTRY
        return registry[device_type]


def _load_tx_powers(gains: dict, device_type: str | None) -> dict:
    """Saved TX powers per device, capped after a restart: the TX never
    starts above the device's safe default (Pluto: -20 dB attenuation), a
    lower saved power is kept. A plain "power" from before per-device
    storage belongs to the device that was selected when it was saved.
    Pops both keys from `gains`."""
    powers = dict(gains.pop("power_by_device", None) or {})
    legacy = gains.pop("power", None)
    if legacy is not None and device_type and device_type not in powers:
        powers[device_type] = legacy
    out = {}
    for dev, value in powers.items():
        if dev in tx_devices.DEVICE_REGISTRY:
            out[dev] = min(float(value), tx_devices.DEVICE_REGISTRY[dev].default_power_ceiling)
    return out


def _supports_direct_sampling(device_type: str | None) -> bool:
    return bool(device_type) and getattr(rx_devices.DEVICE_REGISTRY[device_type], "supports_direct_sampling", False)


def _rx_mode_available(mode: str) -> bool:
    if mode == "ft8":
        return bool(ft8_rx_backends())
    return {"m17": RX_M17_AVAILABLE, "rade": RX_RADE_AVAILABLE}.get(mode, True)


def _tx_mode_available(mode: str) -> bool:
    return {"m17": TX_M17_AVAILABLE, "rade": TX_RADE_AVAILABLE}.get(mode, True)


def _new_over_stats() -> dict:
    return {"frames": 0, "seconds": 0.0, "first_at": 0.0, "max_gap_s": 0.0, "rate_hz": None}


def _resolve_connection(device_cls, connection: str) -> str:
    """pluto_cli.runtime.resolve_connection(), minus the CLI's None handling:
    empty means the device class's own default."""
    connection = (connection or "").strip()
    if not connection:
        return device_cls.DEFAULT_CONNECTION
    if device_cls.connection_kind == "uri":
        return normalize_uri(connection)
    return connection


def _rx_gain_info(device_cls) -> dict:
    """What the frontend needs to build the gain controls for a device."""
    return {
        "agc_modes": list(device_cls.agc_modes),
        "default_gain_mode": device_cls.default_gain_mode,
        "stages": [
            {"name": g.name, "label": g.label, "kind": g.kind, "min": g.min_value, "max": g.max_value,
             "step": g.step, "unit": g.unit, "default": g.default_value, "controls_agc": g.controls_agc}
            for g in device_cls.gain_stages
        ],
    }


def _check_rx_gain(device_type: str | None, name: str, value):
    if device_type is None:
        raise SessionError("rx: select a device first")
    device_cls = rx_devices.DEVICE_REGISTRY[device_type]
    if name == "gain_mode":
        if value not in device_cls.agc_modes:
            raise SessionError(f"gain_mode must be one of {list(device_cls.agc_modes)} for {device_type}")
        return value
    stage = next((g for g in device_cls.gain_stages if f"stage:{g.name}" == name), None)
    if stage is None:
        raise SessionError(f"{device_type} has no gain stage '{name[6:]}'")
    if stage.kind == "bool":
        return bool(value)
    value = float(value)
    if not stage.min_value <= value <= stage.max_value:
        raise SessionError(f"{stage.label} must be within {stage.min_value:g}..{stage.max_value:g} {stage.unit}")
    return value


def _rx_gain_build_args(device_cls, gains: dict):
    """Saved gain settings -> AdvancedRxFlowgraph(gain_mode, manual_gain_db,
    gain_values). Settings saved for another device type are ignored."""
    mode = gains.get("gain_mode")
    if mode not in device_cls.agc_modes:
        mode = device_cls.default_gain_mode or rx_config.DEFAULT_GAIN_MODE
    manual_gain_db = rx_config.DEFAULT_MANUAL_GAIN_DB
    gain_values = {}
    for g in device_cls.gain_stages:
        value = gains.get(f"stage:{g.name}")
        if value is None:
            continue
        if g.controls_agc:
            manual_gain_db = float(value)
        else:
            gain_values[g.name] = bool(value) if g.kind == "bool" else float(value)
    agc_stage = next((g for g in device_cls.gain_stages if g.controls_agc), None)
    if agc_stage is not None and f"stage:{agc_stage.name}" not in gains:
        manual_gain_db = agc_stage.default_value
    return mode, manual_gain_db, gain_values or None


def _apply_rx_gain(tb, name: str, value) -> None:
    if name == "gain_mode":
        tb.set_gain_mode(value)
    elif name.startswith("stage:"):
        stage = next(g for g in tb.device.gain_stages if g.name == name[6:])
        if stage.controls_agc:
            tb.set_manual_gain(float(value))
        else:
            tb.device.set_gain(stage.name, value)
    elif name == "fft_zoom":
        tb.set_fft_zoom(max(1, min(int(value), FFT_ZOOM_MAX)))
    elif name == "fft_avg":
        tb.set_fft_avg_count(max(1, min(int(value), FFT_AVG_MAX)))
    elif name == "freq_correction_ppm":
        tb.set_frequency_correction_ppm(float(value))
    elif name == "squelch_db":
        pass  # read live by _squelch_loop
    elif name == "fft_size":
        if int(value) not in rx_config.FFT_SIZE_PRESETS:
            raise SessionError(f"fft_size must be one of {rx_config.FFT_SIZE_PRESETS}")
        tb.set_fft_size(int(value))
    else:
        raise SessionError(f"unknown rx gain '{name}'")


def _check_tx_setting(name: str, value):
    spec = TX_SETTINGS.get(name)
    if spec is None:
        raise SessionError(f"unknown tx setting '{name}'")
    default, _setter, lo, hi = spec
    if isinstance(default, bool):
        return bool(value)
    value = float(value)
    if not lo <= value <= hi:
        raise SessionError(f"{name} must be within {lo}..{hi}")
    return value


def _apply_tx_setting(tb, name: str, value) -> None:
    getattr(tb, TX_SETTINGS[name][1])(value)


def _apply_tx_gain(tb, name: str, value) -> None:
    if name == "power":
        tb.set_target_power(value)
    elif name == "freq_correction_ppm":
        tb.device.set_frequency_correction_ppm(float(value))
    else:
        raise SessionError(f"unknown tx gain '{name}'")


def _apply_tx_params(tb, mode: str, params: dict) -> None:
    # params are already validated/normalised by modes.normalize_params().
    if mode == "pocsag":
        _apply_pocsag_params(tb, params)
    elif mode == "rtty":
        tb.set_rtty_text(params.get("text", ""))
        tb.set_rtty_mark_hz(params["mark_hz"])
        tb.set_rtty_shift_hz(params["shift_hz"])
        tb.set_rtty_baud_rate(params["baud"])
        tb.set_rtty_reverse(params["reverse"])
    elif mode == "digitext":
        from pluto_tx import digitext

        tb.set_digitext_text(params.get("text", ""))
        tb.set_digitext_layout(digitext.LAYOUT_VERTICAL if params["layout"] == "vertical"
                               else digitext.LAYOUT_HORIZONTAL)
        tb.set_digitext_zoom(params["zoom"])
        tb.set_digitext_min_freq_hz(params["min_freq_hz"])
    elif mode == "rade":
        tb.set_rade_eoo_enabled(bool(params.get("eoo", False)))
    elif mode == "fm":
        # Already validated/normalised by modes.normalize_params().
        tb.set_fm_deviation(params.get("deviation_hz", tx_config.FM_DEVIATION_HZ))
        tb.set_fm_preemphasis(params.get("preemphasis", tx_config.FM_PREEMPH_DEFAULT))
        ctcss = params.get("ctcss_hz")
        if ctcss:
            tb.set_subtone("ctcss", float(ctcss))
        else:
            tb.set_subtone("off")
    elif mode == "m17":
        if "src_callsign" in params:
            tb.set_m17_src_callsign(str(params["src_callsign"]).strip().upper())
        if "dst_callsign" in params:
            tb.set_m17_dst_callsign(str(params["dst_callsign"]).strip().upper())


def _tx_problem(tb, mode: str) -> str | None:
    """Refusals before any RF action, like pluto_cli's preflight checks."""
    if mode == "pocsag":
        return tb.pocsag_problem()
    if mode == "m17" and not tb.m17_src_callsign:
        return "source callsign is empty"
    if mode in ("rtty", "digitext") and not (tb.rtty_text if mode == "rtty" else tb.digitext_text).strip():
        return "text is empty"
    return None


def _power_info(tb) -> dict:
    stage = tb.device.primary_stage
    return {
        "label": stage.label, "unit": stage.unit, "min": stage.min_value,
        "ceiling": tb.power_ceiling, "value": tb.target_power,
        "max": stage.max_value, "default_ceiling": type(tb.device).default_power_ceiling,
        # Further switchable stages (HackRF: RF amp +14 dB), current value from
        # what Web-TRX set on this flowgraph -- else the stage's off value.
        "secondary": [
            {"name": p.name, "label": p.label, "kind": p.kind, "min": p.min_value, "max": p.max_value,
             "unit": p.unit,
             "value": getattr(tb, "web_trx_secondary_power", {}).get(p.name, p.off_value)}
            for p in tb.device.power_stages if not p.is_primary
        ],
    }


def _apply_pocsag_params(tb, params: dict) -> None:
    if "ric" in params:
        tb.set_pocsag_ric(int(params["ric"]))
    if "text" in params:
        tb.set_pocsag_text(str(params["text"]))
    if "function" in params:
        tb.set_pocsag_function(int(params["function"]))
    if "baud" in params:
        tb.set_pocsag_baud(int(params["baud"]))
    if "charset" in params:
        tb.set_pocsag_charset(str(params["charset"]))


def _stop_flowgraph(tb) -> None:
    if isinstance(tb, PlutoTxFlowgraph):
        tb.shutdown_safe()
    else:
        tb.shutdown()


def _json_safe(value):
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, bytes):
        return value.hex()
    if hasattr(value, "tolist"):
        return value.tolist()
    return value


def _snapshot_direction(st: _Direction, keyed: bool) -> dict:
    return {
        "device_type": st.device_type,
        "connection": st.connection,
        "mode": st.mode,
        "mode_params": dict(st.mode_params),
        "freq_hz": st.freq_hz,
        "gains": dict(st.gains),
        "keyed": keyed,
    }
