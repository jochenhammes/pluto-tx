"""ESP32-S3 TX backend: a Wi-Fi chip's test tone, steered in frequency 40 000 times a second (polar modulation), on 13 cm (2320..2450 MHz).

The chip cannot play I/Q samples through this transmitter. What it can do is move its carrier in frequency and (a little) in amplitude, which
is all a signal with a CONSTANT ENVELOPE needs: this device takes the complex baseband the flowgraph produces, keeps its instantaneous frequency
(espdr.polar.IqToPolar) and sends that to the chip as one record per update (espdr.txlink.Streamer). So FM and M17 (and, later, other FSK modes)
work as they are, and everything with amplitude modulation (SSB, PSK, FT8, ...) does not: supported_modes lists what is allowed.

The host side is the `espdr` package, shipped in this repository as ./espdr (a copy of github.com/jochenhammes/esp32-sdr-trx, Zero-Clause BSD
licence, synchronised by tools/sync-espdr.sh; do not edit it here). Needs numpy and pyserial, and esptool 5 or newer to load the firmware into
the board's RAM (`pipx install esptool`; see install-espdr.sh).

The chip radiates a carrier whenever a session runs, and a session is what pre_key()/post_unkey() open and close: "off" is the END of the
session, not a power setting. Setting up a session takes a moment (PLL tuning in the chip), so pre_key() blocks briefly like the Pluto's. The
chip's ring buffer holds about 100..200 ms: the carrier comes on that much after pre_key() and ends that much after post_unkey().

The frequency cannot change while a session runs: set_frequency() while keyed is applied at the next pre_key() (with a warning).

Connection string: "" (find the board), "NATIVE_PORT" (the board's own USB port, the firmware must already run), or "NATIVE_PORT,BRIDGE_PORT"
(the board's USB-UART port loads the firmware: "/dev/ttyACM1,/dev/ttyACM0", or ",/dev/ttyACM0" if only the bridge is known)."""
import logging
import threading
import time
from dataclasses import dataclass

import numpy as np
from gnuradio import gr

from espdr import board, config as espdr_config, nb, polar, thermal as espdr_thermal, txlink, txmodes

from .base import PowerStage, TxDevice

log = logging.getLogger(__name__)

UPDATE_RATE = 40_000                  # records the chip plays a second
SAMPLE_RATE = 80_000                  # what the flowgraph runs this device at (2 samples per record, 5/3 of M17's 48 kHz)
DEVIATION_RESERVE_HZ = 4_000.0        # of the +-19.2 kHz range of the records, kept free for the signal itself (M17 peaks at 3.5 kHz)
SESSION_LIMIT_S = 3600                # the chip's own limit on one session
SWITCH_ON_DRIFT_HZ = 210              # espdr-tx's default of --drift (the first seconds of a cold start)
PUSH_TIMEOUT_S = 2.0
LICENCE_HELP = ("Transmitting takes an amateur radio licence. Read the notice of the ESP32 transmitter and confirm it once with\n"
                "    python3 -m espdr.cli_tx -f 2345 --test-tone 1000 --dry-run --accept-licence\n"
                "(it sends nothing; it only records that you have read it).")


def _parse_connection(connection):
    """"" -> (None, None); "NATIVE" -> (NATIVE, None); "NATIVE,BRIDGE" or ",BRIDGE" -> (NATIVE or None, BRIDGE)."""
    native, _, bridge = (connection or "").strip().partition(",")
    return native.strip() or None, bridge.strip() or None


@dataclass
class _Session:
    """What exists between pre_key() and post_unkey()."""
    session: object
    streamer: object
    modulator: object
    thermal: object
    plan: dict
    started: float
    underruns_while_pushing: int = 0     # the chip's count when the last block went to it: later ones are the pause before the end


class EspdrSink(gr.sync_block):
    """Consumes the flowgraph's complex baseband at SAMPLE_RATE. Not keyed: samples are dropped, but at the real-time rate (the flowgraph
    produces samples all the time and gates them with tx_gain; a sink that swallowed them at once would let its sources run away). Keyed:
    the samples become records and are pushed to the chip, which slows the flowgraph down to its own pace."""

    def __init__(self, device):
        gr.sync_block.__init__(self, name="EspdrSink", in_sig=[np.complex64], out_sig=None)
        self._device = device
        self._due = None

    def _pace(self, n):
        now = time.monotonic()
        if self._due is None or self._due < now - 0.5:
            self._due = now
        self._due += n / SAMPLE_RATE
        delay = self._due - now
        if delay > 0:
            time.sleep(min(delay, 0.5))

    def work(self, input_items, output_items):
        x = input_items[0]
        n = len(x)
        if not self._device._consume(x):
            self._pace(n)
        else:
            self._due = None
        return n


class EspdrDevice(TxDevice):
    device_type = "espdr"
    display_name = "ESP32-S3 (13 cm)"
    connection_kind = "serial"
    DEFAULT_CONNECTION = ""

    frequency_range_hz = (txlink.TX_MIN_HZ, txlink.TX_MAX_HZ)
    sample_rate_range_hz = (UPDATE_RATE, 160_000)
    default_sample_rate_hz = SAMPLE_RATE
    default_bandwidth_hz = None
    supports_persistent_sink = True       # the sink object stays; it drops samples while no session runs
    supports_frequency_correction = True  # the board's crystal is off by some ppm (a few kHz at 2.3 GHz)
    tx_end_loss_s = 0.0                   # post_unkey() waits until the chip has played everything
    # What a constant-envelope baseband can carry: the frequency is all that is sent.
    supported_modes = frozenset({"fm", "m17"})

    # One stage, in dB against the strongest gain code of the chip (code 64); 0.28 dB per code over 17.9 dB. "Off" is the end of the session.
    power_stages = (
        PowerStage(name="gain", label="TX Power (gain)", kind="continuous_db",
                   min_value=-txmodes.RANGE_DB, max_value=0.0, unit="dB", is_primary=True, off_value=-txmodes.RANGE_DB),
    )
    default_power_ceiling = -12.0         # conservative: the output is some microwatts, but nothing here is calibrated

    def __init__(self, connection, frequency_hz, sample_rate_hz, bandwidth_hz):
        super().__init__(connection, frequency_hz, sample_rate_hz, bandwidth_hz)
        self._native, self._bridge = _parse_connection(connection)
        self._lock = threading.RLock()
        self._link = None
        self._active = None
        self._power_db = self.power_stages[0].off_value
        self._sink = None
        self._status = None
        self._clip_warned = False
        self._temperature_c = None

    # ---- the board
    def _open_link(self):
        """Finds the board, loads the transmitter firmware into its RAM if it is not running, and opens its port."""
        if self._link is not None:
            return
        self._native = board.ensure(board.TX, port=self._native, bridge=self._bridge, load="auto", log=log.info)
        self._link = nb.open_link(self._native)

    def prepare_for_start(self):
        if not espdr_config.licence_accepted():
            raise RuntimeError(espdr_config.LICENCE_NOTICE + "\n" + LICENCE_HELP)
        self._open_link()

    def build_sink(self):
        self._sink = EspdrSink(self)
        return self._sink

    # ---- settings
    def _check_frequency(self, freq_hz):
        lo, hi = self.frequency_range_hz
        if not lo <= freq_hz <= hi:
            raise ValueError(f"{freq_hz / 1e6:.4f} MHz is outside {lo / 1e6:.0f} .. {hi / 1e6:.0f} MHz, the only range the ESP32 transmitter sends in")

    def set_frequency(self, freq_hz):
        self._check_frequency(freq_hz)
        self.frequency_hz = freq_hz
        self._apply_frequency()

    def _apply_frequency(self):
        if self._active is not None:
            log.warning("the ESP32 transmitter cannot retune while it sends: %.4f MHz applies at the next key-down", self.frequency_hz / 1e6)

    def set_power(self, stage_name, value):
        assert stage_name == "gain", f"EspdrDevice has no power stage {stage_name!r}"
        stage = self.power_stages[0]
        self._power_db = max(stage.min_value, min(stage.max_value, float(value)))
        act = self._active
        if act is not None:
            act.modulator.set_power(self._power_db)

    # ---- keying
    def _make_thermal(self, session, carrier_hz):
        path = espdr_config.board_model(self._bridge)
        if not path:
            return None
        params = espdr_thermal.load_params(path)
        start_c = params["idle"]
        try:
            start_c = session.chip_temperature()
            self._temperature_c = start_c
        except Exception as e:                      # noqa: BLE001 - the correction then assumes the idle temperature
            log.warning("the chip's temperature could not be read (%s); assuming %g C", e, start_c)
        return espdr_thermal.Thermal(start_c, UPDATE_RATE, carrier_hz, params)

    def pre_key(self):
        with self._lock:
            if self._active is not None:
                return
            self._check_frequency(self.frequency_hz)
            self._open_link()
            # the correction for the board's crystal is the flowgraph's ppm (set_frequency_correction_ppm), applied by plan_lo
            plan = txlink.plan_lo(self.frequency_hz, self._frequency_correction_ppm, DEVIATION_RESERVE_HZ)
            if abs(plan["miss_hz"]) > 20:
                log.warning("the ESP32's PLL word cannot reach %.6f MHz here; the signal will be at %.6f MHz (%+.0f Hz). Change the frequency "
                            "by a few kHz or tune the receiver there", self.frequency_hz / 1e6, plan["center_hz"] / 1e6, plan["miss_hz"])
            session = txlink.Session(self._link)
            thermal = self._make_thermal(session, plan["center_hz"])
            session.configure(plan["lo_hz"], UPDATE_RATE, drift_hz=SWITCH_ON_DRIFT_HZ, limit_s=SESSION_LIMIT_S)
            session.begin(expect_word=txlink.lo_word(plan["lo_hz"]))
            modulator = polar.IqModulator(self.sample_rate_hz, UPDATE_RATE, power_db=self._power_db, static_hz=plan["static_hz"])
            streamer = txlink.Streamer(session, on_status=self._on_status)
            self._clip_warned = False
            self._active = _Session(session, streamer, modulator, thermal, plan, time.monotonic())

    def _on_status(self, ev):
        self._status = ev

    def _consume(self, x):
        """Called by the sink with a block of baseband; True if it went to the chip, False if there is no session (or it is over)."""
        act = self._active
        if act is None or not act.streamer.alive:
            return False
        rec = act.modulator.process(x)
        if act.thermal is not None:
            rec = act.thermal.apply(rec)
        if act.modulator.clipped and not self._clip_warned:
            self._clip_warned = True
            log.warning("the signal's frequency excursion exceeds what the chip's records carry; it is clipped")
        ok = act.streamer.push(rec, timeout=PUSH_TIMEOUT_S)
        act.underruns_while_pushing = (self._status or {}).get("underruns", 0)
        return ok

    def post_unkey(self):
        with self._lock:
            act, self._active = self._active, None
        if act is None:
            return
        try:
            summary = act.streamer.finish(timeout=10.0)
            # Underruns after the last block are only the wait for this call (the flowgraph stops delivering when the signal is over, e.g.
            # after the end of an M17 transmission); the chip repeats its last frequency until the end record arrives.
            if summary and (summary["reason"] != 0 or act.underruns_while_pushing or summary["late"]):
                log.warning("the ESP32 session ended: %s; underruns while sending %d, late updates %d",
                            txlink.END_REASONS.get(summary["reason"], summary["reason"]), act.underruns_while_pushing, summary["late"])
        except Exception as e:                      # noqa: BLE001 - post_unkey must not raise into the flowgraph
            log.error("the ESP32 session did not end cleanly: %s", e)
            act.streamer.close(timeout=3.0)

    def force_safe_state(self):
        try:
            with self._lock:
                act, self._active = self._active, None
                link, self._link = self._link, None
            if act is not None:
                act.streamer.close(timeout=3.0)
            if link is not None:
                try:
                    link.ser.close()
                except Exception:                    # noqa: BLE001
                    pass
        except Exception as e:                      # noqa: BLE001 - never raises
            log.error("force_safe_state: %s", e)

    def read_hw_state(self):
        act = self._active
        st = self._status or {}
        return {
            "keyed": act is not None,
            "lo_mhz": act.plan["lo_hz"] / 1e6 if act else None,
            "center_mhz": act.plan["center_hz"] / 1e6 if act else None,
            "fill": st.get("fill"),
            "underruns": st.get("underruns"),
            "late": st.get("late"),
            "temperature_c": None if act else self._temperature_c,
            "firmware_loaded": self._link is not None,
        }

    # ---- finding the board
    @staticmethod
    def probe_with_timeout(connection, timeout_s=5.0):
        try:
            native, bridge = _parse_connection(connection)
            ports = board.native_ports() + board.bridge_ports()
            if native and native not in ports and not bridge:
                return RuntimeError(f"{native} is not a port of an ESP32-S3 board")
            if not ports:
                return RuntimeError("no ESP32-S3 board found (its USB ports are missing)")
            return None
        except Exception as e:                      # noqa: BLE001
            return e

    @staticmethod
    def scan_devices_with_timeout(timeout_s=5.0):
        try:
            from serial.tools import list_ports
            natives = board.native_ports()
            found = {}
            for p in list_ports.comports():
                if (p.vid, p.pid) in board.BRIDGES:
                    native = natives[0] if len(natives) == 1 else ""
                    found[f"{native},{p.device}"] = f"ESP32-S3 board, UART port {p.device}" + (f", serial {p.serial_number}" if p.serial_number else "")
            if not found and natives:
                found[natives[0]] = f"ESP32-S3 board, USB port {natives[0]}"
            return found, None
        except Exception as e:                      # noqa: BLE001
            return None, e
