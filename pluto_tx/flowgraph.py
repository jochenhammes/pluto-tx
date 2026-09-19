"""GNU Radio flowgraph for the PlutoSDR FM/SSB(USB) TX app.

Both modulation branches (FM, SSB) and both audio sources (mic, file) are
always connected. Switching mode or source at runtime is a plain
blocks.selector index change, never a graph rebuild -- this keeps the
RF-safety-critical wiring identical across every build stage and avoids any
retune/relock when the operator flips a combo box mid-session.

SSB is built with the Hilbert phasing method at AUDIO RATE, not at the TX
baseband rate: a 129-tap Hilbert transformer only approximates 90-degree
phase shift well over a sane fractional bandwidth. At 48 kHz, a 300-2700 Hz
voice band is ~11% of Nyquist -- plenty. At the 2.5 MSps TX rate, the same
voice band is <0.2% of Nyquist, deep in the transformer's do-nothing region
near DC, which produced both sidebands instead of just USB. So: Hilbert at
audio rate -> complex resampler up to TX rate, not real-resample-then-Hilbert.
"""
import math
import os
import sys
import time
import wave

import numpy as np

from gnuradio import gr, blocks, filter, analog, qtgui, digital
from gnuradio.filter import firdes
from gnuradio.fft import window

from . import audio_devices
from . import config
from . import dcs
from . import devices
from . import digitext
from . import dynamics
from . import filebroadcast
from .filebroadcast_source import FileBroadcastSource
from . import psk31
from . import pocsag
from . import pocsag_codec
from . import rtty
from . import lora_airtime

# LoRa/Meshtastic is optional: gr-lora_sdr is a from-source build (see
# install-lora.sh) and the protocol layer needs the `meshtastic` +
# `cryptography` pip packages. Same posture as M17_AVAILABLE above -- the app
# stays fully usable without them, the GUI greys the mode out.
try:
    from . import meshtastic_codec
    from .lora import LORA_AVAILABLE, LoraTxEncoder, lora_resampler_taps
except ImportError:
    meshtastic_codec = None
    LoraTxEncoder = None
    LORA_AVAILABLE = False

# M17 digital voice is optional: gr-m17 is a from-source build (see
# install-m17.sh), not something every pluto_tx user necessarily has. The
# app must stay fully usable for FM/SSB without it -- import lazily and let
# the GUI grey out the M17 mode entry when unavailable, rather than a hard
# top-level ImportError that would break the whole app.
try:
    from gnuradio import m17 as _m17
    import pmt as _pmt
    M17_AVAILABLE = True
except ImportError:
    _m17 = None
    _pmt = None
    M17_AVAILABLE = False

# FreeDV 2020/2020B is also optional in principle, but unlike gr-m17 it
# needs no separate install step: libcodec2 (with full LPCNet/2020/2020B
# support) is already a transitive dependency of the `gnuradio` apt package
# via libgnuradio-vocoder -- confirmed this session (apt-cache rdepends,
# and a direct runtime freedv_open()/freedv_tx() smoke test, both on a
# fresh install.sh-only system). FREEDV_AVAILABLE is therefore a defensive
# check (older libcodec2 without 2020 support), not an expected path.
from . import freedv_ctypes as _freedv_ctypes
from .freedv import FreeDVEncoder
FREEDV_AVAILABLE = _freedv_ctypes.FREEDV_AVAILABLE

# RADE V1 is also optional, same reasoning as M17: from-source build (see
# install-rade.sh), not something every user has.
from . import rade_ctypes as _rade_ctypes
from .rade import RadeEncoder
RADE_AVAILABLE = _rade_ctypes.RADE_AVAILABLE

_PLACEHOLDER_WAV = os.path.join(os.path.dirname(__file__), "_silence.wav")
_DEFAULT_WAV = os.path.join(os.path.dirname(__file__), "da2jh-test.wav")


def _ensure_placeholder_wav(path=_PLACEHOLDER_WAV, seconds=1.0, rate=config.AUDIO_RATE):
    """A valid-but-silent mono WAV, used only if the real default file is missing."""
    if not os.path.exists(path):
        n = int(seconds * rate)
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(b"\x00\x00" * n)
    return path


def _default_wav_path():
    return _DEFAULT_WAV if os.path.exists(_DEFAULT_WAV) else _ensure_placeholder_wav()


class PlutoTxFlowgraph(gr.top_block):
    SRC_MIC = 0
    SRC_FILE = 1
    MODE_FM = 0
    MODE_SSB = 1
    MODE_M17 = 2
    MODE_FREEDV = 3
    MODE_RADE = 4
    MODE_DIGITEXT = 5
    MODE_FILEBROADCAST = 6
    MODE_PSK31 = 7
    MODE_BASEBAND = 8
    MODE_RTTY = 9
    MODE_MESHTASTIC = 10
    MODE_LSB = 11
    MODE_POCSAG = 12

    def __init__(self, device_type="pluto", connection=None, frequency=config.DEFAULT_FREQUENCY,
                 power_ceiling=None, audio_device="",
                 wav_path=None, mode=MODE_FM, source=SRC_MIC, enable_waterfall=False,
                 m17_src_callsign="", m17_dst_callsign=config.M17_DEFAULT_DST_CALLSIGN,
                 freedv_variant=config.FREEDV_DEFAULT_MODE, freedv_callsign="",
                 digitext_text=config.DIGITEXT_DEFAULT_TEXT, digitext_layout=digitext.LAYOUT_HORIZONTAL,
                 digitext_zoom=1, digitext_min_freq_hz=config.DIGITEXT_MIN_FREQ_HZ,
                 psk31_text="", psk31_tone_hz=config.PSK31_DEFAULT_TONE_HZ,
                 rtty_text="", rtty_mark_hz=config.RTTY_MARK_HZ_DEFAULT,
                 rtty_shift_hz=config.RTTY_SHIFT_HZ_DEFAULT, rtty_baud_rate=config.RTTY_BAUD_RATE_DEFAULT,
                 rtty_reverse=False,
                 meshtastic_preset_index=0, meshtastic_text="", meshtastic_node_id=None,
                 meshtastic_channel_name=None, meshtastic_psk_b64=config.MESHTASTIC_DEFAULT_PSK_B64,
                 meshtastic_hop_limit=config.MESHTASTIC_DEFAULT_HOP_LIMIT, meshtastic_callsign="",
                 pocsag_ric=config.POCSAG_DEFAULT_RIC, pocsag_function=3, pocsag_kind="alpha", pocsag_text="",
                 pocsag_baud=config.POCSAG_BAUD_DEFAULT, pocsag_charset="ascii"):
        super().__init__("PlutoTxFlowgraph")

        device_cls = devices.DEVICE_REGISTRY[device_type]
        if power_ceiling is None:
            power_ceiling = device_cls.default_power_ceiling

        self.nominal_freq_hz = float(frequency)
        self.fine_offset_hz = 0.0
        self.power_ceiling = power_ceiling
        self.target_power = power_ceiling
        self._keyed = False
        self._m17_ending = False  # True during the brief EOT tail after unkey_ptt() in M17 mode
        self._rade_ending = False  # True during the brief EOO tail after unkey_ptt() in RADE mode (if enabled)
        self._rade_eoo_source = None
        self.rade_eoo_enabled = False  # off by default -- see unkey_ptt()'s RADE branch; needs its own
        # isolated hardware verification (Phase I2 of the RADE integration plan) before being turned on.
        self._secondary_power = {}  # non-primary power stages (e.g. HackRF's AMP), see set_secondary_power()

        if mode == self.MODE_M17 and not M17_AVAILABLE:
            # mode_selector below would otherwise be constructed pointing at
            # an input that's never connected (the M17 branch only gets
            # wired if M17_AVAILABLE) -- fall back rather than build a
            # broken flowgraph.
            mode = self.MODE_FM
        if mode == self.MODE_FREEDV and not FREEDV_AVAILABLE:
            mode = self.MODE_FM
        if mode == self.MODE_RADE and not RADE_AVAILABLE:
            mode = self.MODE_FM
        if mode == self.MODE_MESHTASTIC and (not LORA_AVAILABLE or device_cls.is_audio_only()):
            # LoRa is RF-only (125-500 kHz wide) -- a 48 kHz soundcard can't carry it.
            mode = self.MODE_FM
        self.mode = mode  # the ACTUAL mode (post-fallback) -- GUI reads this
        # to sync mode_combo's initial selection, otherwise it always shows
        # "FM" regardless of what mode the flowgraph was actually built with.

        # Device layer first: attenuation to minimum, LO up (Pluto's
        # prepare_for_start()) -- BEFORE the GR sink (which enables TX
        # channels at construction time for Pluto) exists. The device is
        # returned to its safe/idle state once construction/wiring is
        # finished below -- see the end of __init__ and key_ptt()/
        # unkey_ptt() for the PTT<->device-safety hard tie (LO-powerdown on
        # Pluto; see each TxDevice subclass for its own backend).
        self.device = devices.build_device(device_type, connection=connection, frequency_hz=self.nominal_freq_hz)
        self.device.prepare_for_start()
        # Which real output device RADE/Digitext/PSK31's own soundcard-out
        # sinks should write to -- only meaningful for is_audio_only() (the
        # Soundcard device type), where `connection` is an ALSA device
        # string chosen via the GUI's Scan/Connect flow (audio_devices.
        # list_output_devices()). For Pluto/HackRF, `connection` is an RF
        # URI/serial -- must NOT be handed to audio.sink() as a device
        # name, so those sinks stay on "" (system default) exactly as
        # before this device-selection feature existed.
        self._soundcard_audio_device = self.device.connection if self.device.is_audio_only() else ""

        # --- Sources ---------------------------------------------------
        self.mic_source = audio_devices.open_input_device(config.AUDIO_RATE, audio_device)
        self.wav_path = wav_path or _default_wav_path()
        file_mono = self._build_file_source(self.wav_path)
        # blocks.wavfile_source (inside _build_file_source() above) has no
        # hardware-driven pacing the way mic_source does -- ALSA/PipeWire's
        # own audio.source() is called by its driver at a steady, hardware-
        # timed cadence (one period at a time), which naturally trickles
        # samples into the flowgraph smoothly. A file source instead just
        # returns however many samples the scheduler asks for (noutput_items)
        # as fast as disk/page-cache I/O allows, relying ENTIRELY on
        # downstream buffer-full backpressure to avoid running ahead of real
        # time -- which means whenever downstream buffer room opens up in a
        # burst (plausible given M17's own already-documented scheduling
        # quirks: output_multiple(192) plus a huge downstream expansion
        # factor, RRC x10 /resampler x~52), the file source can be pulled in
        # equally bursty chunks instead of a smooth trickle. Confirmed on
        # real hardware this session: mic source -> zero RF dropouts over a
        # sustained M17 TX; file source -> reliably reproduces the reported
        # chopping, REGARDLESS of audio content/loudness (tested quiet vs.
        # loud) or of whether the downmix/resample stages below even run
        # (tested a pre-converted mono/48kHz file, skipping both) -- i.e.
        # audio content and the extra processing stages are both ruled out,
        # leaving wavfile_source's own delivery pattern as the remaining
        # variable. This throttle forces it back to a smooth, real-time
        # trickle, mirroring the identical fix already applied to File
        # Broadcast's own unthrottled background source above.
        self.file_throttle = blocks.throttle(
            gr.sizeof_float, config.AUDIO_RATE, True, config.FILE_THROTTLE_CHUNK_SAMPLES,
        )
        self.connect(file_mono, self.file_throttle)
        file_mono = self.file_throttle

        # NOTE: blocks.selector's ninputs is only known once the flowgraph is
        # actually running (its io_signature is unbounded at construction
        # time -- calling set_input_index() before tb.start() raises
        # IndexError even with valid connections already made). So the
        # *initial* index must be passed into the constructor directly; the
        # set_source()/set_mode() methods below are for RUNTIME switching
        # only, after tb.start().
        self.source_selector = blocks.selector(gr.sizeof_float, source, 0)
        self.source_selector.set_enabled(True)

        # --- PTT audio mute, NF band-pass filter, AGC, manual NF gain ---
        self.ptt_mute = blocks.multiply_const_ff(0.0)

        initial_preset = "SSB" if mode in (self.MODE_SSB, self.MODE_LSB) else "FM"
        f_lo, f_hi, trans = config.NF_FILTER_PRESETS[initial_preset]
        self._nf_taps = firdes.band_pass(1.0, config.AUDIO_RATE, f_lo, f_hi, trans, window.WIN_HAMMING)
        self.nf_filter = filter.fir_filter_fff(1, self._nf_taps)

        # max_gain=65536 (effectively unbounded) let the AGC spiral up to
        # huge gain during quiet/silent passages, then slam a 30x-amplified
        # signal into the modulator on the next loud transient -- measured
        # offline against the real WAV file: peak_abs ~33 with the old
        # settings. That overdrive, not Hilbert sideband rejection, was the
        # real source of the broadband splatter/opposite-sideband energy
        # seen on the RTL-SDR waterfall. A bounded max_gain keeps normal
        # operation clean; the rail_ff below is the hard backstop for the
        # rare transient that still overshoots.
        self.agc = analog.agc2_ff(0.2, 0.005, 0.3, 1.0)
        self.agc.set_max_gain(4.0)

        # Manual "mic gain" trim, applied AFTER the AGC so it's a direct,
        # predictable final drive-level control (a slider before the AGC
        # would just get normalized away again).
        self.nf_gain = blocks.multiply_const_ff(config.DEFAULT_NF_GAIN)

        # Hard safety limiter: guarantees the modulator/DAC never sees a
        # sample outside [-1, 1] regardless of AGC transient overshoot.
        self.limiter = analog.rail_ff(-1.0, 1.0)

        # --- NF dynamics processing: noise gate, compressor, smooth limiter.
        # Gate sits BEFORE the AGC so the AGC never "sees" and reacts to
        # room noise/hiss during pauses -- only real speech. No native
        # enable/disable exists on pwr_squelch_ff (confirmed via dir()); the
        # bypass in set_gate_enabled() below sets the threshold to a floor
        # that effectively never gates, caching the real value to restore.
        self._gate_threshold_db = config.GATE_THRESHOLD_DB
        self.gate = analog.pwr_squelch_ff(
            config.GATE_THRESHOLD_DB, config.GATE_ALPHA, config.GATE_RAMP_SAMPLES, False,
        )
        # Compressor: moderate ratio/knee, slower release -- evens out
        # average speech level. Limiter: high ratio, fast attack, small
        # knee -- catches transient peaks BEFORE the hard-clip safety net
        # below, so that net engages rarely/never on normal material
        # instead of being the routine (harmonic-distortion-generating)
        # ceiling. Both are the same DynamicsProcessor class, see
        # dynamics.py's module docstring for why one class covers both roles.
        self.compressor = dynamics.DynamicsProcessor(
            config.AUDIO_RATE, config.COMPRESSOR_THRESHOLD_DB, config.COMPRESSOR_RATIO,
            config.COMPRESSOR_KNEE_DB, config.COMPRESSOR_ATTACK_MS, config.COMPRESSOR_RELEASE_MS,
        )
        self.limiter_smooth = dynamics.DynamicsProcessor(
            config.AUDIO_RATE, config.LIMITER_THRESHOLD_DB, config.LIMITER_RATIO,
            config.LIMITER_KNEE_DB, config.LIMITER_ATTACK_MS, config.LIMITER_RELEASE_MS,
        )

        # --- FM branch: real audio @ AUDIO_RATE -> resample -> FM ---------
        # quad_rate is the device's TX baseband rate -- Pluto's is fixed
        # (devices.pluto.QUAD_RATE); other backends may differ.
        quad_rate = int(self.device.sample_rate_hz)
        g = math.gcd(quad_rate, config.AUDIO_RATE)
        self.fm_resampler = filter.rational_resampler_fff(
            interpolation=quad_rate // g, decimation=config.AUDIO_RATE // g,
            taps=[], fractional_bw=0.4,
        )
        self.fm_sensitivity = 2 * math.pi * config.FM_DEVIATION_HZ / quad_rate
        self.fm_mod = analog.frequency_modulator_fc(self.fm_sensitivity)

        # --- SSB branch: Hilbert AT AUDIO RATE (see module docstring),
        # then resample the resulting complex analytic signal up to TX rate.
        # 401 taps: measured offline (synthetic tone sweep, no RF) opposite-
        # sideband suppression across the 300-2700 Hz voice band -- 129 taps
        # only gave ~16 dB at the 300 Hz low edge (audible LSB leakage,
        # matches what was heard over the air); 401 taps gives >=61 dB
        # across the whole band.
        self.ssb_mod = filter.hilbert_fc(401, window.WIN_HAMMING, 6.76)
        # LSB = complex conjugate of the analytic signal: flip the sign of the imaginary part
        # (+1 = USB, -1 = LSB) -- shares the (expensive) resampler and mode_selector port with SSB.
        self.ssb_split = blocks.complex_to_float(1)
        self.ssb_imag_sign = blocks.multiply_const_ff(-1.0 if mode == self.MODE_LSB else 1.0)
        self.ssb_join = blocks.float_to_complex(1)
        self.ssb_resampler = filter.rational_resampler_ccf(
            interpolation=quad_rate // g, decimation=config.AUDIO_RATE // g,
            taps=[], fractional_bw=0.4,
        )

        # --- FM sub-audible tone (CTCSS/DCS), summed AFTER the limiter (the NF band-pass would
        # remove it). All three branches always exist; set_subtone() only changes their gains.
        self._subtone_kind = "off"
        self._subtone_value = None
        self._subtone_level_pct = config.SUBTONE_LEVEL_DEFAULT_PCT
        self.subtone_voice = blocks.multiply_const_ff(1.0)
        self.ctcss_source = analog.sig_source_f(config.AUDIO_RATE, analog.GR_SIN_WAVE,
                                                config.CTCSS_TONES_HZ[8], 1.0)
        self.ctcss_gain = blocks.multiply_const_ff(0.0)
        self.dcs_source = blocks.vector_source_f(dcs.render_loop(dcs.STANDARD_CODES[0]).tolist(), True)
        self.dcs_gain = blocks.multiply_const_ff(0.0)
        self.subtone_adder = blocks.add_ff(1)

        # --- M17 branch (optional, only if gr-m17 is installed): deliberately
        # bypasses the entire analog dynamics chain above (nf_filter/gate/
        # agc/compressor/nf_gain/limiter_smooth/limiter) -- it taps
        # ptt_mute's output directly. Codec2 has its own internal level
        # handling; a broadcast-style compressor ahead of a low-bitrate
        # vocoder is more likely to hurt intelligibility than help.
        # ptt_mute is still reused (defense in depth: no mic audio flows
        # even before m17_coder's own SOT/EOT-gated _active state is
        # considered). Parameters/topology are taken from gr-m17's own
        # examples/transmitterPLUTOSDR.grc reference flowgraph and verified
        # this session with a real offline m17_coder->m17_decoder round-trip
        # (encoded test bytes and src/dst callsigns both decoded correctly).
        self.m17_src_callsign = m17_src_callsign
        self.m17_dst_callsign = m17_dst_callsign or config.M17_DEFAULT_DST_CALLSIGN
        if M17_AVAILABLE:
            g_m17 = math.gcd(config.AUDIO_RATE, config.M17_CODEC2_RATE)
            self.m17_audio_resampler = filter.rational_resampler_fff(
                interpolation=config.M17_CODEC2_RATE // g_m17, decimation=config.AUDIO_RATE // g_m17,
                taps=[], fractional_bw=0.4,
            )
            self.m17_float_to_short = blocks.float_to_short(1, 32767.0)
            self.m17_codec2_encoder = _m17.codec2_encoder()
            # type=2 (Voice), no encryption/signing/CAN -- verified this
            # session via a real offline coder->decoder loopback (payload
            # and src/dst callsigns both round-tripped correctly with these
            # exact parameters).
            self.m17_coder = _m17.m17_coder(
                self.m17_src_callsign, self.m17_dst_callsign, 2, 0, 0, 0, 0,
                "", "", "", False, False, "", 1,
            )
            # gain=M17_RRC_SPS compensates for interp_fir_filter_fff's
            # zero-stuffing interpolation (an interpolating FIR needs its
            # taps scaled by the interpolation factor to preserve amplitude,
            # otherwise the output is attenuated by ~1/interpolation) --
            # matches gr-m17's own reference example's gain=10 exactly.
            self._m17_rrc_taps = firdes.root_raised_cosine(
                config.M17_RRC_SPS, config.M17_BASEBAND_RATE, config.M17_SYMBOL_RATE,
                config.M17_RRC_ALPHA, config.M17_RRC_NTAPS,
            )
            self.m17_rrc = filter.interp_fir_filter_fff(config.M17_RRC_SPS, self._m17_rrc_taps)
            self.m17_fm_mod = analog.frequency_modulator_fc(
                2 * math.pi * config.M17_DEVIATION_HZ / config.M17_BASEBAND_RATE
            )
            g_m17_tx = math.gcd(quad_rate, config.M17_BASEBAND_RATE)
            self.m17_tx_resampler = filter.rational_resampler_ccf(
                interpolation=quad_rate // g_m17_tx, decimation=config.M17_BASEBAND_RATE // g_m17_tx,
                taps=[], fractional_bw=0.4,
            )

        # --- FreeDV 2020/2020B branch (optional, only if libcodec2 has
        # 2020/2020B support -- see FREEDV_AVAILABLE above). Also bypasses
        # the NF dynamics chain, same reasoning as M17: compressing/AGC-ing
        # an already-modulated OFDM waveform would corrupt it. Architecturally
        # much simpler than M17: freedv_tx() outputs a plain AUDIO-band
        # modulated waveform (8kHz PCM) meant to be fed into an ordinary SSB
        # transmitter's mic input -- exactly how real FreeDV operation works
        # (freedv-gui feeds a real radio's mic-in the same way) -- so this
        # reuses the EXACT same Hilbert-based USB modulation technique as
        # the ssb_mod/ssb_resampler above, just as separate dedicated
        # instances (avoids any runtime-switching complexity between normal
        # voice and FreeDV-modem audio sharing one Hilbert block).
        self.freedv_callsign = freedv_callsign
        self.freedv_variant = freedv_variant
        if FREEDV_AVAILABLE:
            g_fdv_down = math.gcd(config.AUDIO_RATE, config.FREEDV_SPEECH_RATE)
            self.freedv_audio_resampler = filter.rational_resampler_fff(
                interpolation=config.FREEDV_SPEECH_RATE // g_fdv_down, decimation=config.AUDIO_RATE // g_fdv_down,
                taps=[], fractional_bw=0.4,
            )
            self.freedv_float_to_short = blocks.float_to_short(1, 32767.0)
            self.freedv_encoder = FreeDVEncoder(mode=self.freedv_variant, text=self.freedv_callsign)
            self.freedv_short_to_float = blocks.short_to_float(1, 32767.0)
            g_fdv_up = math.gcd(config.AUDIO_RATE, config.FREEDV_MODEM_RATE)
            self.freedv_audio_resampler_up = filter.rational_resampler_fff(
                interpolation=config.AUDIO_RATE // g_fdv_up, decimation=config.FREEDV_MODEM_RATE // g_fdv_up,
                taps=[], fractional_bw=0.4,
            )
            self.freedv_ssb_mod = filter.hilbert_fc(401, window.WIN_HAMMING, 6.76)
            self.freedv_ssb_resampler = filter.rational_resampler_ccf(
                interpolation=quad_rate // g, decimation=config.AUDIO_RATE // g,
                taps=[], fractional_bw=0.4,
            )

        # --- RADE V1 branch (optional, only if librade.so + lpcnet_demo are
        # both available -- see RADE_AVAILABLE above). Also bypasses the NF
        # dynamics chain, same reasoning as M17/FreeDV: RadeEncoder's own
        # FARGAN/OFDM pipeline has its own internal level handling: an
        # upstream compressor/AGC would just distort what it feeds in.
        # RadeEncoder outputs raw complex IQ directly at RADE_MODEM_SAMPLE_
        # RATE (8kHz, confirmed via rade_api.h and this session's own
        # feasibility spike reading rade_tx_wav.c's WAV writer) -- the SDR
        # path (rade_tx_resampler) resamples that straight to quad_rate, no
        # Hilbert/SSB modulation step at all (the OFDM modulation is
        # already baked into RadeEncoder's IQ output). The Soundcard output
        # path (rade_to_real/rade_audio_resampler_out/rade_audio_gain,
        # below) is an ALTERNATIVE to that, not an extra stage on top of
        # it -- selected by device_type="soundcard" (devices/soundcard.py),
        # see key_ptt()/unkey_ptt()'s is_audio_only() branch.
        if RADE_AVAILABLE:
            g_rade_down = math.gcd(config.AUDIO_RATE, _rade_ctypes.RADE_SPEECH_SAMPLE_RATE)
            self.rade_audio_resampler = filter.rational_resampler_fff(
                interpolation=_rade_ctypes.RADE_SPEECH_SAMPLE_RATE // g_rade_down,
                decimation=config.AUDIO_RATE // g_rade_down,
                taps=[], fractional_bw=0.4,
            )
            self.rade_float_to_short = blocks.float_to_short(1, 32767.0)
            self.rade_encoder = RadeEncoder()
            g_rade_up = math.gcd(quad_rate, _rade_ctypes.RADE_MODEM_SAMPLE_RATE)
            self.rade_tx_resampler = filter.rational_resampler_ccf(
                interpolation=quad_rate // g_rade_up,
                decimation=_rade_ctypes.RADE_MODEM_SAMPLE_RATE // g_rade_up,
                taps=[], fractional_bw=0.4,
            )

            # --- Soundcard output alternative: lets an externally-connected
            # SSB transceiver transmit RADE instead of an SDR, selected by
            # choosing device_type="soundcard" (devices/soundcard.py) rather
            # than a mode-internal flag -- see key_ptt()/unkey_ptt()'s
            # self.device.is_audio_only() branch below, which unmutes THIS
            # parallel branch instead of the normal tx_gain/device path.
            # Confirmed in freedv/rade_c's own source this session
            # (RadeAPIUse.md's "Scaling to 16 bits" section, rade_tx_wav.c
            # writing only iq[i].real with no extra scaling): the REAL PART
            # of RadeEncoder's complex IQ is, by itself, a legitimate
            # SSB-injectable audio signal -- librade.so already centers the
            # OFDM carriers at 1500Hz (the middle of a normal SSB passband)
            # internally, so no Hilbert transform or frequency shift is
            # needed here, unlike FreeDV's own audio path (freedv_ssb_mod
            # above).
            self.rade_to_real = blocks.complex_to_real()
            self.connect(self.rade_encoder, self.rade_to_real)
            g_rade_audio_out = math.gcd(_rade_ctypes.RADE_MODEM_SAMPLE_RATE, config.AUDIO_RATE)
            self.rade_audio_resampler_out = filter.rational_resampler_fff(
                interpolation=config.AUDIO_RATE // g_rade_audio_out,
                decimation=_rade_ctypes.RADE_MODEM_SAMPLE_RATE // g_rade_audio_out,
                taps=[], fractional_bw=0.4,
            )
            self.connect(self.rade_to_real, self.rade_audio_resampler_out)
            # Starts muted, exactly like tx_gain -- see key_ptt()/unkey_ptt()'s
            # early-return branch for a Soundcard device (is_audio_only()),
            # which unmutes/mutes THIS gate instead of tx_gain/the device.
            self.rade_audio_gain = blocks.multiply_const_ff(0.0)
            self.connect(self.rade_audio_resampler_out, self.rade_audio_gain)
            self.rade_audio_sink = audio_devices.open_output_device(config.AUDIO_RATE, self._soundcard_audio_device)
            self.connect(self.rade_audio_gain, self.rade_audio_sink)

        # --- Digitext branch (waterfall-text digimode, pluto_tx/digitext.py):
        # ALWAYS available, unlike M17/FreeDV/RADE above -- Pillow/NumPy are
        # plain Python dependencies (python3-pil, added to install.sh this
        # session), not an external from-source C library that might be
        # missing, so no _AVAILABLE gate is needed here. Outputs a plain real
        # AUDIO waveform (like FreeDV, not raw IQ like M17/RADE), so it reuses
        # this app's existing Hilbert-based USB modulation technique with its
        # own dedicated instances -- see digitext.py's module docstring for
        # the actual encoding (row-by-row inverse-STFT). Does NOT tap
        # ptt_mute like every other mode -- the "source" is the typed text,
        # not mic/file audio, so there's no upstream audio to mute; only
        # tx_gain (downstream, common to every mode) needs to gate it.
        # digitext_source starts as a 1-sample silent placeholder -- the
        # real per-transmission waveform is built lazily (see
        # _ensure_digitext_audio()) and swapped in fresh on every key_ptt()
        # (same "rebuild a fresh one-shot vector_source_c" idiom already
        # proven for RADE's EOO tail, just one stage earlier in the chain --
        # here it feeds the Hilbert modulator instead of tx_gain directly,
        # since this source is real-valued audio, not already-modulated IQ).
        self.digitext_text = digitext_text
        self.digitext_layout = digitext_layout
        self.digitext_zoom = digitext_zoom
        self.digitext_min_freq_hz = digitext_min_freq_hz
        self._digitext_audio = None
        self._digitext_audio_dirty = True
        self.digitext_duration_s = 0.0
        self.digitext_source = blocks.vector_source_f([0.0], repeat=False)
        self.digitext_ssb_mod = filter.hilbert_fc(401, window.WIN_HAMMING, 6.76)
        # fractional_bw=0.47, not the 0.4 every other SSB-ish resampler in
        # this file uses -- a real bug found on real hardware this session:
        # operators hit a hard ceiling around 18000Hz of occupied bandwidth
        # (text cut off with a slow fade, not an abrupt edge -- exactly a
        # resampler passband roll-off signature, not a hard clip). Measured
        # offline (pure tones through this exact resampler config, 48kHz in
        # -> Pluto's QUAD_RATE out): at 0.4 the passband is flat only to
        # ~18-19kHz, already rolled off to -6dB by 20kHz and near-zero by
        # 23.5kHz -- NOT the Hilbert modulator (digitext_ssb_mod measured
        # flat to within 0.1dB up to 23.8kHz in isolation, so that's not the
        # bottleneck). Raising fractional_bw pushes the resampler's own
        # passband edge outward (0.45->flat to ~21kHz, 0.47->flat to
        # ~22kHz); 0.5 is the mathematical Nyquist limit where the required
        # transition band shrinks to zero (infeasible). 0.47 leaves a
        # deliberate ~2kHz safety margin below Nyquist (24kHz) for the
        # anti-alias stopband while giving Digitext roughly 4kHz more usable
        # bandwidth than the previous default -- specific to THIS resampler,
        # not changed for ssb_resampler/freedv_ssb_resampler (real SSB voice
        # and FreeDV's own audio never get remotely close to 18kHz anyway,
        # no reason to touch their filter design).
        self.digitext_ssb_resampler = filter.rational_resampler_ccf(
            interpolation=quad_rate // g, decimation=config.AUDIO_RATE // g,
            taps=[], fractional_bw=0.47,
        )
        self.connect(self.digitext_source, self.digitext_ssb_mod)
        self.connect(self.digitext_ssb_mod, self.digitext_ssb_resampler)

        # --- Digitext Soundcard output alternative: mirrors RADE's own
        # Soundcard branch above (same device_type="soundcard"/
        # is_audio_only() selection, not a separate mode-internal flag).
        # Simpler than RADE's version: digitext_source is ALREADY plain
        # real audio (no complex_to_real() step needed) and already sits in
        # the normal SSB voice band (DIGITEXT_MIN_FREQ_HZ=300Hz upward, see
        # digitext.py) -- exactly what a real SSB transceiver's mic input
        # expects, no extra shifting needed. Fans out from the SAME
        # digitext_source that also feeds digitext_ssb_mod for the SDR
        # path -- key_ptt() reconnects BOTH downstream branches whenever it
        # rebuilds digitext_source fresh for a new transmission.
        self.digitext_audio_gain = blocks.multiply_const_ff(0.0)  # starts muted, like tx_gain/rade_audio_gain
        self.connect(self.digitext_source, self.digitext_audio_gain)
        self.digitext_audio_sink = audio_devices.open_output_device(config.AUDIO_RATE, self._soundcard_audio_device)
        self.connect(self.digitext_audio_gain, self.digitext_audio_sink)

        # --- PSK31 branch (BPSK31 keyboard-chat digimode, pluto_tx/psk31.py
        # -- see the plan). Architecturally a direct structural mirror of
        # the Digitext branch just above: a one-shot rebuilt-per-press
        # vector_source_f feeding this app's existing Hilbert-based USB
        # modulation chain (own dedicated instances), plus the same
        # Soundcard-output alternative. Unlike File Broadcast's raw-IQ
        # GFSK branch below, PSK31 is audio-domain (like Digitext/FreeDV),
        # so it reuses this technique instead of a separate IQ path.
        self.psk31_text = psk31_text
        self.psk31_tone_hz = psk31_tone_hz
        self._psk31_audio = None
        self._psk31_audio_dirty = True
        self.psk31_duration_s = 0.0
        self.psk31_source = blocks.vector_source_f([0.0], repeat=False)
        self.psk31_ssb_mod = filter.hilbert_fc(401, window.WIN_HAMMING, 6.76)
        # fractional_bw=0.4 -- the DEFAULT every other narrowband audio-domain
        # mode in this file uses, NOT Digitext's special-cased 0.47: PSK31's
        # own real-hardware-verified occupied bandwidth is only ~50-60Hz
        # (see pluto_tx/psk31.py), nowhere near Digitext's ~18-23kHz signal
        # that actually needed the wider passband fix.
        self.psk31_ssb_resampler = filter.rational_resampler_ccf(
            interpolation=quad_rate // g, decimation=config.AUDIO_RATE // g,
            taps=[], fractional_bw=0.4,
        )
        self.connect(self.psk31_source, self.psk31_ssb_mod)
        self.connect(self.psk31_ssb_mod, self.psk31_ssb_resampler)
        self.psk31_audio_gain = blocks.multiply_const_ff(0.0)  # starts muted, like tx_gain/digitext_audio_gain
        self.connect(self.psk31_source, self.psk31_audio_gain)
        self.psk31_audio_sink = audio_devices.open_output_device(config.AUDIO_RATE, self._soundcard_audio_device)
        self.connect(self.psk31_audio_gain, self.psk31_audio_sink)

        # --- RTTY branch (2-tone FSK Baudot digimode, pluto_tx/rtty.py --
        # see the plan). Exact structural mirror of the PSK31 branch above
        # (one-shot rebuilt-per-press vector_source_f, same Hilbert-based
        # USB modulation chain, same Soundcard-output alternative) --
        # unlike PSK31, mark/shift/baud-rate are all real runtime-
        # adjustable settings, cached the same lazy/dirty way.
        self.rtty_text = rtty_text
        self.rtty_mark_hz = rtty_mark_hz
        self.rtty_shift_hz = rtty_shift_hz
        self.rtty_baud_rate = rtty_baud_rate
        self.rtty_reverse = rtty_reverse
        self._rtty_audio = None
        self._rtty_audio_dirty = True
        self.rtty_duration_s = 0.0
        self.rtty_source = blocks.vector_source_f([0.0], repeat=False)
        self.rtty_ssb_mod = filter.hilbert_fc(401, window.WIN_HAMMING, 6.76)
        # fractional_bw=0.4 -- same default as PSK31's resampler, not
        # Digitext's special-cased 0.47: even RTTY's widest preset
        # (850Hz shift, mark up to 2700Hz) stays well under the ~18kHz
        # onset of 0.4's rolloff at 48kHz audio rate.
        self.rtty_ssb_resampler = filter.rational_resampler_ccf(
            interpolation=quad_rate // g, decimation=config.AUDIO_RATE // g,
            taps=[], fractional_bw=0.4,
        )
        self.connect(self.rtty_source, self.rtty_ssb_mod)
        self.connect(self.rtty_ssb_mod, self.rtty_ssb_resampler)
        self.rtty_audio_gain = blocks.multiply_const_ff(0.0)  # starts muted, like tx_gain/psk31_audio_gain
        self.connect(self.rtty_source, self.rtty_audio_gain)
        self.rtty_audio_sink = audio_devices.open_output_device(config.AUDIO_RATE, self._soundcard_audio_device)
        self.connect(self.rtty_audio_gain, self.rtty_audio_sink)

        # --- Meshtastic (LoRa CSS) branch. IQ-native like M17/RADE: gr-lora_sdr's
        # modulate emits complex baseband directly at 4x the LoRa bandwidth, so
        # the only thing needed on top is a resampler up to the device rate --
        # with EXPLICIT low-pass taps (a taps=[] auto-design corrupted the
        # phase-continuous FSK modes, see backlog/ft8, and a chirp is just as
        # phase-sensitive). The encoder has no stream input: it stays silent
        # until key_ptt() pushes a packet, so it is always built (when
        # available) and just drains into a null sink while another mode is
        # active. The encoder is built for the PHY (SF/BW/CR) of the preset
        # given at construction; presets with another PHY need a rebuilt
        # flowgraph (meshtastic_phy_matches()), presets that only differ in
        # frequency/region switch live.
        self.meshtastic_preset_index = int(meshtastic_preset_index)
        self.meshtastic_text = meshtastic_text
        if meshtastic_node_id is None and LORA_AVAILABLE:
            meshtastic_node_id = meshtastic_codec.random_node_id()
        self.meshtastic_node_id = meshtastic_node_id
        self.meshtastic_channel_name = meshtastic_channel_name
        self.meshtastic_psk_b64 = meshtastic_psk_b64
        self.meshtastic_hop_limit = int(meshtastic_hop_limit)
        self.meshtastic_callsign = meshtastic_callsign
        self.meshtastic_hold_s = 0.0  # RF hold of the frame currently/last keyed (airtime + tail)
        self.meshtastic_last_airtime_s = 0.0
        self._meshtastic_pending = None  # (packet, airtime_s, preset) prepared by prepare_meshtastic_tx()
        self._meshtastic_busy_until = 0.0  # monotonic; the previous frame is still draining until then
        self._meshtastic_duty = lora_airtime.DutyCycleLimiter(None)
        self._lora_rf_bandwidth_active = False
        self._lora_phy = None
        if LORA_AVAILABLE:
            lp = config.MESHTASTIC_PRESETS[self.meshtastic_preset_index]
            self._lora_phy = self._meshtastic_phy(lp)
            lora_bw = int(lp.bandwidth_hz)
            self.lora_encoder = LoraTxEncoder(lp.spreading_factor, lora_bw, lora_airtime.cr_index(lp.coding_rate))
            lora_fs = int(self.lora_encoder.samp_rate)
            g_lora = math.gcd(quad_rate, lora_fs)
            lora_interp, lora_decim = quad_rate // g_lora, lora_fs // g_lora
            self._lora_tx_taps = lora_resampler_taps(lora_bw, lora_interp, lora_fs * lora_interp,
                                                     min(lora_fs, quad_rate))
            self.lora_tx_resampler = filter.rational_resampler_ccf(
                interpolation=lora_interp, decimation=lora_decim, taps=self._lora_tx_taps,
            )
            self.connect(self.lora_encoder, self.lora_tx_resampler)

        # --- File Broadcast branch (repetitive file-broadcast mode, 23cm
        # broadband GFSK -- see pluto_tx/filebroadcast.py and the plan).
        # IQ-native like M17/RADE (gfsk_mod produces complex baseband
        # directly, no Hilbert/SSB step) -- unlike Digitext/RADE-soundcard,
        # there is no Soundcard-output alternative: this mode's whole
        # purpose is a broadband SDR-native link, not something a
        # voice-bandwidth external transceiver could carry anyway.
        #
        # PTT model is deliberately the SAME plain Start/Stop hold-to-
        # transmit path as FM/SSB (see key_ptt()/unkey_ptt() -- this mode
        # gets NO special-case branch there at all, just the existing GUI
        # PTT toggle button already used for FM/SSB, per the plan's
        # locked-in "Start/Stop toggle PTT model" scope decision -- Start
        # calls key_ptt() once, Stop calls unkey_ptt() once, continuous
        # rotation in between).
        #
        # filebroadcast_source (Phase 2's queue-fed custom GNU Radio source
        # block, see filebroadcast_source.py) stays PERMANENTLY connected
        # and running, exactly like mic_source/file_source in FM/SSB mode
        # (always producing samples regardless of PTT -- tx_gain downstream
        # is what actually gates whether any of this reaches the device) --
        # NOT rebuilt per key_ptt() press the way Phase 1's fixed
        # vector_source_b(repeat=True) was. This is what lets files be
        # added/removed live (add_filebroadcast_file()/
        # clear_filebroadcast_files() below just call
        # filebroadcast_source.request_rebuild(), no lock()/disconnect()/
        # connect() flowgraph surgery needed at all, unlike Digitext's
        # still-necessary per-press source rebuild for its one-shot
        # waveform).
        self.filebroadcast_planner = filebroadcast.FileBroadcastPlanner()
        self.filebroadcast_source = FileBroadcastSource(self.filebroadcast_planner, config.FILEBROADCAST_CHUNK_SIZE)
        # filebroadcast_source has NO natural rate limiting (in_sig=None,
        # a pure gr.sync_block reading from a queue/planner) -- unlike
        # mic_source/audio_alsa_source, which every other mode's own
        # always-running background branch ultimately depends on and
        # which real audio hardware paces to real time regardless of
        # whether that branch is actually selected. Without this throttle,
        # this branch free-runs at full CPU speed permanently (confirmed
        # on real hardware via per-thread pidstat: this branch's resampler
        # thread sat at 96-99% of one core continuously, even with a
        # DIFFERENT mode -- M17 -- both selected and actively
        # transmitting), competing for CPU with whichever mode IS active
        # and producing intermittent real-time dropouts in that mode's own
        # output (observed as choppy segments in an M17 transmission's
        # waterfall). Rate matches the byte stream this source emits:
        # FILEBROADCAST_SYMBOL_RATE_HZ bits/sec / 8 = bytes/sec (do_unpack=True
        # below unpacks each byte into 8 one-bit GFSK symbols).
        self.filebroadcast_throttle = blocks.throttle(
            gr.sizeof_char, config.FILEBROADCAST_SYMBOL_RATE_HZ / 8,
        )
        fb_sensitivity = 2 * math.pi * config.FILEBROADCAST_DEVIATION_HZ / config.FILEBROADCAST_WORKING_RATE_HZ
        self.filebroadcast_mod = digital.gfsk_mod(
            samples_per_symbol=config.FILEBROADCAST_SPS, sensitivity=fb_sensitivity,
            bt=config.FILEBROADCAST_BT, do_unpack=True,
        )
        # EXPLICIT taps, never taps=[] (auto-design) -- a real, confirmed
        # Phase 0 finding: rational_resampler_ccf's auto-designed taps
        # corrupt phase-continuous GFSK badly enough to break the
        # receiver's symbol clock recovery, even in a lossless software
        # round-trip. Cutoff/transition formula matches Phase 0's own
        # verified _gfsk_resampler() exactly (cutoff at working_rate/2*0.9,
        # transition at working_rate*0.3).
        g_fb = math.gcd(quad_rate, int(config.FILEBROADCAST_WORKING_RATE_HZ))
        fb_interp, fb_decim = quad_rate // g_fb, int(config.FILEBROADCAST_WORKING_RATE_HZ) // g_fb
        fb_taps = firdes.low_pass(
            fb_interp, quad_rate, config.FILEBROADCAST_WORKING_RATE_HZ / 2 * 0.9,
            config.FILEBROADCAST_WORKING_RATE_HZ * 0.3, window.WIN_HAMMING,
        )
        self.filebroadcast_tx_resampler = filter.rational_resampler_ccf(
            interpolation=fb_interp, decimation=fb_decim, taps=fb_taps,
        )
        self.connect(self.filebroadcast_source, self.filebroadcast_throttle)
        self.connect(self.filebroadcast_throttle, self.filebroadcast_mod)
        self.connect(self.filebroadcast_mod, self.filebroadcast_tx_resampler)

        # --- Baseband branch: raw, unprocessed audio -> wide FM, for
        # digimode software (fldigi etc.) feeding already-modulated audio
        # in via the Source combo (mic/file/PipeWire-monitor/persistent-
        # loopback -- see audio_devices.py) that just needs to be carried
        # to RF as close to unaltered as possible. Deliberately bypasses
        # the ENTIRE NF filter/AGC/compressor/limiter dynamics chain above
        # (nf_filter/gate/agc/nf_gain/compressor/limiter_smooth/limiter),
        # same reasoning as M17/FreeDV/RADE/Digitext/PSK31/File Broadcast
        # just above/below -- that chain is voice-shaping (a fixed
        # 300-3000Hz band-pass, a compressor tuned for speech dynamics),
        # exactly what would clip/distort a wider, unfamiliar digimode
        # signal (RTTY/PSK31/Olivia/MFSK can span several kHz). Taps
        # ptt_mute directly, like those other bypassing modes -- ptt_mute
        # itself is just the PTT gate (mute when unkeyed), not "processing."
        #
        # fractional_bw=0.47 (not the 0.4 used everywhere else in this
        # file) -- Digitext's own precedent for exactly this problem
        # (comment near its own resampler): 0.4's passband starts rolling
        # off around 18-19kHz at AUDIO_RATE=48000, -6dB by 20kHz; 0.47
        # stays flat to ~22kHz, needed here since Baseband explicitly
        # wants to carry content wider than typical 3kHz voice/SSB audio.
        #
        # config.BASEBAND_DEVIATION_HZ (not FM_DEVIATION_HZ, which is a
        # fixed narrowband-voice value) -- operator-adjustable via
        # set_baseband_deviation(), sized via Carson's rule (BW ~=
        # 2*(deviation+audio_bandwidth)) for whatever content width the
        # operator is actually feeding in.
        self.baseband_deviation_hz = config.BASEBAND_DEVIATION_HZ
        g_baseband = math.gcd(quad_rate, config.AUDIO_RATE)
        self.baseband_resampler = filter.rational_resampler_fff(
            interpolation=quad_rate // g_baseband, decimation=config.AUDIO_RATE // g_baseband,
            taps=[], fractional_bw=0.47,
        )
        self.connect(self.ptt_mute, self.baseband_resampler)
        self.baseband_sensitivity = 2 * math.pi * self.baseband_deviation_hz / quad_rate
        self.baseband_mod = analog.frequency_modulator_fc(self.baseband_sensitivity)
        self.connect(self.baseband_resampler, self.baseband_mod)

        # --- POCSAG branch (paging, pluto_tx/pocsag.py): a shaped +-1 NRZ stream at AUDIO_RATE goes
        # straight into its own frequency modulator (+-POCSAG_DEVIATION_HZ). Like RTTY/PSK31 the
        # source is one-shot and rebuilt on every key_ptt(); tx_gain stays muted until then.
        self.pocsag_ric = int(pocsag_ric)
        self.pocsag_function = int(pocsag_function)
        self.pocsag_kind = pocsag_kind
        self.pocsag_text = pocsag_text
        self.pocsag_baud = int(pocsag_baud)
        self.pocsag_charset = pocsag_charset
        self._pocsag_audio = None
        self._pocsag_audio_dirty = True
        self.pocsag_duration_s = 0.0
        self.pocsag_source = blocks.vector_source_f([0.0], repeat=False)
        self.pocsag_resampler = filter.rational_resampler_fff(
            interpolation=quad_rate // g_baseband, decimation=config.AUDIO_RATE // g_baseband,
            taps=[], fractional_bw=0.47,
        )
        self.pocsag_mod = analog.frequency_modulator_fc(2 * math.pi * config.POCSAG_DEVIATION_HZ / quad_rate)
        self.connect(self.pocsag_source, self.pocsag_resampler)
        self.connect(self.pocsag_resampler, self.pocsag_mod)
        # Soundcard device: the same NRZ stream goes to the sound card instead (into the data/mic input of an
        # FM radio, which does the FM modulation). Only built for audio-only devices; starts muted like tx_gain.
        self.pocsag_audio_gain = None
        if self.device.is_audio_only():
            self.pocsag_audio_gain = blocks.multiply_const_ff(0.0)
            self.pocsag_audio_sink = audio_devices.open_output_device(config.AUDIO_RATE, self._soundcard_audio_device)
            self.connect(self.pocsag_source, self.pocsag_audio_gain)
            self.connect(self.pocsag_audio_gain, self.pocsag_audio_sink)

        # mode_selector only ever carries FM/SSB (2 inputs) -- M17 is
        # deliberately NOT a third selector input. Measured this session:
        # m17_coder's unusual output_multiple(192)-plus-large-downstream-
        # expansion (RRC x10, resampler x~52) starves completely when routed
        # through blocks.selector alongside sibling FM/SSB inputs (confirmed
        # via a stage-by-stage probe on real hardware: m17_coder's own
        # general_work() was never even called once PTT was pressed, zero
        # output at every stage) -- while the identical M17 chain connected
        # DIRECTLY to the real hardware sink (no selector) worked perfectly
        # (proper symbol stream, correct EOT tail, no underruns). No buffer-
        # size tuning fixed the selector case (set_max_output_buffer gets
        # silently capped well below what's needed) -- this is a structural
        # scheduler incompatibility, not a tunable parameter, so M17 gets
        # its own dedicated tx_gain connection instead, swapped via
        # lock()/connect()/disconnect() in set_mode() when entering/leaving
        # M17 mode (see set_mode() below). FM<->SSB switching is completely
        # unaffected -- still the original fast mode_selector.set_input_index().
        self.mode_selector = blocks.selector(gr.sizeof_gr_complex, 1 if mode in (self.MODE_SSB, self.MODE_LSB) else 0, 0)
        self.mode_selector.set_enabled(True)

        # Starts MUTED (0), not the pass-through 1.0+0j it used to be:
        # ptt_mute alone (upstream, before every modulator) is NOT
        # sufficient to guarantee zero output downstream -- confirmed on
        # real HackRF hardware. FM-modulating silence is mathematically a
        # full-amplitude, unmodulated CARRIER (frequency_modulator_fc maps
        # a constant zero input to a constant, non-zero complex output), and
        # FreeDV's OFDM modem keeps emitting pilot/sync tones continuously
        # regardless of speech content -- neither is "silence" downstream of
        # ptt_mute, even though the input truly is. This was always true,
        # even before HackRF existed in this app; it was just invisible on
        # Pluto, whose ~90dB attenuator + LO powerdown buried it regardless
        # of what the digital baseband was actually carrying. HackRF has
        # neither, so the operator saw it directly: a carrier present from
        # the moment the flowgraph started (tx_gain defaulted to
        # pass-through), not just something that appeared on key_ptt().
        # key_ptt()/unkey_ptt()/finish_unkey_m17() now mute/unmute tx_gain
        # in lockstep with ptt_mute -- the actual RF path is severed at the
        # LAST point before the device, downstream of every modulation
        # branch, instead of relying on each modulator happening to produce
        # zero output for zero input (SSB/M17 do; FM/FreeDV don't).
        self.tx_gain = blocks.multiply_const_cc(0.0 + 0j)

        # Whichever of {mode_selector, m17_tx_resampler, freedv_ssb_resampler}
        # is NOT currently feeding tx_gain must still drain into something --
        # GNU Radio requires every output port to be connected. null_sink is
        # a standard no-op drain for exactly this purpose. Same reasoning as
        # M17 above applies to FreeDV: gets its own dedicated tx_gain
        # connection rather than a 3rd/4th mode_selector input.
        self._null_sink_selector = blocks.null_sink(gr.sizeof_gr_complex)
        if M17_AVAILABLE:
            self._null_sink_m17 = blocks.null_sink(gr.sizeof_gr_complex)
        if FREEDV_AVAILABLE:
            self._null_sink_freedv = blocks.null_sink(gr.sizeof_gr_complex)
        if RADE_AVAILABLE:
            self._null_sink_rade = blocks.null_sink(gr.sizeof_gr_complex)
        self._null_sink_digitext = blocks.null_sink(gr.sizeof_gr_complex)  # always built, see Digitext branch above
        self._null_sink_psk31 = blocks.null_sink(gr.sizeof_gr_complex)  # always built, see PSK31 branch above
        self._null_sink_rtty = blocks.null_sink(gr.sizeof_gr_complex)  # always built, see RTTY branch above
        if LORA_AVAILABLE:
            self._null_sink_lora = blocks.null_sink(gr.sizeof_gr_complex)
        self._null_sink_filebroadcast = blocks.null_sink(gr.sizeof_gr_complex)  # always built, see File Broadcast branch above
        self._null_sink_baseband = blocks.null_sink(gr.sizeof_gr_complex)  # always built, see Baseband branch above
        self._null_sink_pocsag = blocks.null_sink(gr.sizeof_gr_complex)  # always built, see POCSAG branch above

        # --- Live view of the modulated baseband actually fed to the sink,
        # zoomed in on a fixed span around center (WATERFALL_ZOOM_BANDWIDTH_HZ)
        # rather than the device's full quad_rate -- at quad_rate itself
        # (2.5-8+ MHz), the actual modulated signal (a few kHz to ~10kHz
        # wide for any mode this app supports) is a barely visible sliver.
        # A cheap decimating resampler ahead of the waterfall crops the
        # displayed span down, instead of raising the FFT size across the
        # full bandwidth -- much cheaper, and the resolution improvement
        # (same FFT size, far fewer Hz/bin) comes for free from the
        # narrower span, no bigger FFT needed. Optional: a qtgui sink is a
        # real Qt widget and needs a QApplication to already exist -- only
        # requested by the GUI, so headless CLI use (stage 1/2 style) stays
        # Qt-free.
        self.waterfall = None
        self.waterfall_zoom_resampler = None
        if enable_waterfall:
            g_wf = math.gcd(config.WATERFALL_ZOOM_BANDWIDTH_HZ, quad_rate)
            self.waterfall_zoom_resampler = filter.rational_resampler_ccf(
                interpolation=config.WATERFALL_ZOOM_BANDWIDTH_HZ // g_wf, decimation=quad_rate // g_wf,
                taps=[], fractional_bw=0.4,
            )
            self.waterfall = qtgui.waterfall_sink_c(
                1024, window.WIN_BLACKMAN_hARRIS, 0, config.WATERFALL_ZOOM_BANDWIDTH_HZ,
                "", 1
            )

        # --- Device sink: build_sink() ALWAYS constructs at the device's
        # minimum power, never the operator's target power (Pluto's sink
        # constructor enables TX channels immediately, before tb.start() is
        # ever called -- see device.prepare_for_start() above). Stored on
        # self (not just a local var) because _rebuild_device_sink() below
        # needs to disconnect/replace it later, for devices where zeroing
        # gain alone doesn't actually stop transmission (confirmed for
        # HackRF -- see devices/hackrf.py and devices/base.py's
        # supports_persistent_sink).
        self._device_sink = self.device.build_sink()

        # --- Wiring ---------------------------------------------------
        self.connect(self.mic_source, (self.source_selector, self.SRC_MIC))
        self.connect(file_mono, (self.source_selector, self.SRC_FILE))
        self.connect(self.source_selector, self.ptt_mute)
        self.connect(self.ptt_mute, self.nf_filter)
        self.connect(self.nf_filter, self.gate)
        self.connect(self.gate, self.agc)
        self.connect(self.agc, self.compressor)
        self.connect(self.compressor, self.nf_gain)
        self.connect(self.nf_gain, self.limiter_smooth)
        self.connect(self.limiter_smooth, self.limiter)

        self.connect(self.limiter, self.subtone_voice)
        self.connect(self.subtone_voice, (self.subtone_adder, 0))
        self.connect(self.ctcss_source, self.ctcss_gain)
        self.connect(self.ctcss_gain, (self.subtone_adder, 1))
        self.connect(self.dcs_source, self.dcs_gain)
        self.connect(self.dcs_gain, (self.subtone_adder, 2))
        self.connect(self.subtone_adder, self.fm_resampler)
        self.connect(self.fm_resampler, self.fm_mod)
        self.connect(self.fm_mod, (self.mode_selector, self.MODE_FM))

        self.connect(self.limiter, self.ssb_mod)
        self.connect(self.ssb_mod, self.ssb_split)
        self.connect((self.ssb_split, 0), (self.ssb_join, 0))
        self.connect((self.ssb_split, 1), self.ssb_imag_sign, (self.ssb_join, 1))
        self.connect(self.ssb_join, self.ssb_resampler)
        self.connect(self.ssb_resampler, (self.mode_selector, self.MODE_SSB))

        if M17_AVAILABLE:
            # Taps ptt_mute directly (bypasses the analog dynamics chain --
            # see the M17 branch construction comment above).
            self.connect(self.ptt_mute, self.m17_audio_resampler)
            self.connect(self.m17_audio_resampler, self.m17_float_to_short)
            self.connect(self.m17_float_to_short, self.m17_codec2_encoder)
            self.connect(self.m17_codec2_encoder, self.m17_coder)
            self.connect(self.m17_coder, self.m17_rrc)
            self.connect(self.m17_rrc, self.m17_fm_mod)
            self.connect(self.m17_fm_mod, self.m17_tx_resampler)

        if FREEDV_AVAILABLE:
            # Taps ptt_mute directly -- see the FreeDV branch construction
            # comment above.
            self.connect(self.ptt_mute, self.freedv_audio_resampler)
            self.connect(self.freedv_audio_resampler, self.freedv_float_to_short)
            self.connect(self.freedv_float_to_short, self.freedv_encoder)
            self.connect(self.freedv_encoder, self.freedv_short_to_float)
            self.connect(self.freedv_short_to_float, self.freedv_audio_resampler_up)
            self.connect(self.freedv_audio_resampler_up, self.freedv_ssb_mod)
            self.connect(self.freedv_ssb_mod, self.freedv_ssb_resampler)

        if RADE_AVAILABLE:
            # Taps ptt_mute directly -- see the RADE branch construction
            # comment above. No Hilbert/SSB step: RadeEncoder's output is
            # already modulated IQ, just resampled up to quad_rate.
            self.connect(self.ptt_mute, self.rade_audio_resampler)
            self.connect(self.rade_audio_resampler, self.rade_float_to_short)
            self.connect(self.rade_float_to_short, self.rade_encoder)
            self.connect(self.rade_encoder, self.rade_tx_resampler)

        # Exactly one of {mode_selector, m17_tx_resampler, freedv_ssb_resampler,
        # rade_tx_resampler} feeds tx_gain at a time -- the rest drain into
        # their null_sinks.
        # See _tx_gain_producer_map()/set_mode() for the runtime swap logic
        # (same lock()/connect()/disconnect() pattern proven on real
        # hardware for M17 this session, reused here for FreeDV).
        producers = self._tx_gain_producer_map()
        active_producer = producers[self.mode]
        self.connect(active_producer, self.tx_gain)
        for producer in set(producers.values()):
            if producer is not active_producer:
                self.connect(producer, self._null_sink_for(producer))

        self.connect(self.tx_gain, self._device_sink)
        if self.waterfall is not None:
            self.connect(self.tx_gain, self.waterfall_zoom_resampler)
            self.connect(self.waterfall_zoom_resampler, self.waterfall)

        # Idle state once construction is done (e.g. Pluto: LO powered back
        # down -- its sink constructor needs the LO up to initialize, see
        # the comment on prepare_for_start() above, but the app starts
        # unkeyed). See key_ptt()/unkey_ptt() for the PTT<->device-safety
        # hard tie.
        if self.mode == self.MODE_MESHTASTIC:
            self._apply_mode_rf_settings(self.mode)
        self.device.post_unkey()

    def _build_file_source(self, wav_path):
        """Return a mono, AUDIO_RATE float stream from a WAV file of any
        channel count / sample rate (downmix + resample as needed)."""
        src = blocks.wavfile_source(wav_path, True)
        self.file_source = src
        n_ch = src.channels()
        file_rate = int(src.sample_rate())

        if n_ch == 1:
            mono = src
        else:
            adder = blocks.add_ff()
            for ch in range(n_ch):
                self.connect((src, ch), (adder, ch))
            downmix = blocks.multiply_const_ff(1.0 / n_ch)
            self.connect(adder, downmix)
            mono = downmix

        if file_rate != config.AUDIO_RATE:
            g = math.gcd(config.AUDIO_RATE, file_rate)
            resamp = filter.rational_resampler_fff(
                interpolation=config.AUDIO_RATE // g, decimation=file_rate // g,
                taps=[], fractional_bw=0.4,
            )
            self.connect(mono, resamp)
            mono = resamp

        return mono

    # --- runtime control (call only after tb.start(), see note above) ---
    def set_source(self, source: int):
        self.source_selector.set_input_index(source)

    def _tx_gain_producer_map(self):
        """mode -> the block that should feed tx_gain in that mode. FM/SSB
        share mode_selector (fast index switch, no reconnect); M17/FreeDV
        each get their own dedicated producer (see the comment above
        mode_selector's construction for why they can't share it)."""
        producers = {self.MODE_FM: self.mode_selector, self.MODE_SSB: self.mode_selector,
                     self.MODE_LSB: self.mode_selector}
        if M17_AVAILABLE:
            producers[self.MODE_M17] = self.m17_tx_resampler
        if FREEDV_AVAILABLE:
            producers[self.MODE_FREEDV] = self.freedv_ssb_resampler
        if RADE_AVAILABLE:
            producers[self.MODE_RADE] = self.rade_tx_resampler
        producers[self.MODE_DIGITEXT] = self.digitext_ssb_resampler  # always available, see its branch above
        producers[self.MODE_PSK31] = self.psk31_ssb_resampler  # always available, see its branch above
        producers[self.MODE_RTTY] = self.rtty_ssb_resampler  # always available, see its branch above
        if LORA_AVAILABLE:
            producers[self.MODE_MESHTASTIC] = self.lora_tx_resampler
        producers[self.MODE_FILEBROADCAST] = self.filebroadcast_tx_resampler  # always available, see its branch above
        producers[self.MODE_BASEBAND] = self.baseband_mod  # always available, see its branch above
        producers[self.MODE_POCSAG] = self.pocsag_mod  # always available, see its branch above
        return producers

    def _null_sink_for(self, producer):
        if producer is self.mode_selector:
            return self._null_sink_selector
        if M17_AVAILABLE and producer is self.m17_tx_resampler:
            return self._null_sink_m17
        if FREEDV_AVAILABLE and producer is self.freedv_ssb_resampler:
            return self._null_sink_freedv
        if RADE_AVAILABLE and producer is self.rade_tx_resampler:
            return self._null_sink_rade
        if producer is self.digitext_ssb_resampler:
            return self._null_sink_digitext
        if producer is self.psk31_ssb_resampler:
            return self._null_sink_psk31
        if producer is self.rtty_ssb_resampler:
            return self._null_sink_rtty
        if LORA_AVAILABLE and producer is self.lora_tx_resampler:
            return self._null_sink_lora
        if producer is self.filebroadcast_tx_resampler:
            return self._null_sink_filebroadcast
        if producer is self.baseband_mod:
            return self._null_sink_baseband
        if producer is self.pocsag_mod:
            return self._null_sink_pocsag
        raise ValueError(f"no null_sink registered for producer {producer!r}")

    def set_mode(self, mode: int):
        prev_mode = self.mode
        producers = self._tx_gain_producer_map()
        prev_producer = producers[prev_mode]
        new_producer = producers[mode]
        self.mode = mode

        if new_producer is not prev_producer:
            # Entering/leaving a dedicated-producer mode (M17, FreeDV):
            # reroute tx_gain's upstream connection. Brief pause (lock/
            # unlock), same graph-reconfiguration pattern already used
            # elsewhere in this app (e.g. WAV file changes) and proven on
            # real hardware for M17 this session. FM<->SSB switching never
            # hits this branch (both map to the same mode_selector producer).
            self.lock()
            try:
                self.disconnect(prev_producer, self.tx_gain)
                self.disconnect(new_producer, self._null_sink_for(new_producer))
                self.connect(new_producer, self.tx_gain)
                self.connect(prev_producer, self._null_sink_for(prev_producer))
            finally:
                self.unlock()

        self._apply_mode_rf_settings(mode)

        if mode in (self.MODE_M17, self.MODE_FREEDV, self.MODE_RADE, self.MODE_DIGITEXT,
                    self.MODE_PSK31, self.MODE_RTTY, self.MODE_FILEBROADCAST, self.MODE_BASEBAND,
                    self.MODE_MESHTASTIC, self.MODE_POCSAG):
            return  # all nine bypass the NF filter/dynamics chain entirely, nothing to retap

        sideband_mode = mode in (self.MODE_SSB, self.MODE_LSB)
        self.ssb_imag_sign.set_k(-1.0 if mode == self.MODE_LSB else 1.0)
        self.mode_selector.set_input_index(1 if sideband_mode else 0)
        preset = "SSB" if sideband_mode else "FM"
        f_lo, f_hi, trans = config.NF_FILTER_PRESETS[preset]
        taps = firdes.band_pass(1.0, config.AUDIO_RATE, f_lo, f_hi, trans, window.WIN_HAMMING)
        self.nf_filter.set_taps(taps)

    # --- POCSAG ---------------------------------------------------------

    def set_pocsag_ric(self, ric: int):
        self.pocsag_ric = int(ric)
        self._pocsag_audio_dirty = True

    def set_pocsag_function(self, function: int):
        self.pocsag_function = int(function)
        self._pocsag_audio_dirty = True

    def set_pocsag_kind(self, kind: str):
        self.pocsag_kind = kind
        self._pocsag_audio_dirty = True

    def set_pocsag_text(self, text: str):
        self.pocsag_text = text
        self._pocsag_audio_dirty = True

    def set_pocsag_baud(self, baud: int):
        self.pocsag_baud = int(baud)
        self._pocsag_audio_dirty = True

    def set_pocsag_charset(self, charset: str):
        self.pocsag_charset = charset
        self._pocsag_audio_dirty = True

    def pocsag_problem(self):
        """None if the current POCSAG settings can be sent, else a short reason."""
        if not 0 <= self.pocsag_ric <= pocsag_codec.RIC_MAX:
            return f"RIC must be 0..{pocsag_codec.RIC_MAX}"
        if self.pocsag_ric == 0 and self.pocsag_function == 0:
            return "RIC 0 with function 0 is the all-zero codeword (choose another function)"
        if self.pocsag_kind != "tone" and not self.pocsag_text.strip():
            return "message text is empty"
        if len(self.pocsag_text) > config.POCSAG_MAX_TEXT_LEN:
            return f"message is longer than {config.POCSAG_MAX_TEXT_LEN} characters"
        return None

    def _pocsag_start_source(self):
        """Swap in a fresh one-shot source with the rendered call. Done only after the output path is unmuted
        and powered: the source is unthrottled, so starting it earlier would run the first samples (part of
        the preamble) through the muted chain."""
        self.lock()
        try:
            self.disconnect(self.pocsag_source, self.pocsag_resampler)
            if self.pocsag_audio_gain is not None:
                self.disconnect(self.pocsag_source, self.pocsag_audio_gain)
            self.pocsag_source = blocks.vector_source_f(self._pocsag_audio.tolist(), repeat=False)
            self.connect(self.pocsag_source, self.pocsag_resampler)
            if self.pocsag_audio_gain is not None:
                self.connect(self.pocsag_source, self.pocsag_audio_gain)
        finally:
            self.unlock()

    def _ensure_pocsag_audio(self):
        problem = self.pocsag_problem()
        if problem:
            raise ValueError(problem)
        if self._pocsag_audio_dirty or self._pocsag_audio is None:
            audio, duration_s = pocsag.encode_message(
                self.pocsag_ric, self.pocsag_function, self.pocsag_kind, self.pocsag_text,
                self.pocsag_baud, self.pocsag_charset)
            pad = int(self.device.tx_end_loss_s * config.AUDIO_RATE)  # unmodulated carrier the device may swallow
            self._pocsag_audio = np.concatenate([audio, np.zeros(pad, dtype=np.float32)])
            self.pocsag_duration_s = duration_s + pad / config.AUDIO_RATE
            self._pocsag_audio_dirty = False

    @property
    def pocsag_hold_s(self):
        return self.pocsag_duration_s + config.POCSAG_TX_TAIL_S

    # --- FM sub-audible tone (CTCSS / DCS) --------------------------------

    def set_subtone(self, kind, value=None):
        """kind: "off", "ctcss" (value = tone in Hz) or "dcs" (value = (octal code int, "N"|"I")).
        Live, no rebuild; only audible in FM mode (SSB/LSB branch hangs off the limiter before the sum)."""
        if kind not in ("off", "ctcss", "dcs"):
            raise ValueError(f"unknown sub-audible tone kind {kind!r}")
        if kind == "ctcss":
            value = float(value)
            self.ctcss_source.set_frequency(value)
        elif kind == "dcs":
            code, polarity = value
            if code not in dcs.STANDARD_CODES or polarity not in dcs.POLARITIES:
                raise ValueError(f"invalid DCS code {value!r}")
            self.dcs_source.set_data(dcs.render_loop(code, polarity).tolist())
            value = (code, polarity)
        self._subtone_kind = kind
        self._subtone_value = value
        self._apply_subtone_gains()

    def set_subtone_level(self, percent):
        lo, hi = config.SUBTONE_LEVEL_RANGE_PCT
        self._subtone_level_pct = min(max(float(percent), lo), hi)
        self._apply_subtone_gains()

    def _apply_subtone_gains(self):
        level = self._subtone_level_pct / 100.0
        kind = self._subtone_kind
        self.ctcss_gain.set_k(level if kind == "ctcss" else 0.0)
        self.dcs_gain.set_k(level if kind == "dcs" else 0.0)
        self.subtone_voice.set_k(1.0 if kind == "off" else 1.0 - level)

    # --- Meshtastic ----------------------------------------------------

    @property
    def meshtastic_preset(self):
        return config.MESHTASTIC_PRESETS[self.meshtastic_preset_index]

    def _apply_mode_rf_settings(self, mode):
        """Per-mode RF front-end settings the generic wiring doesn't cover:
        while a LoRa preset is active the device's analog TX bandwidth must
        cover the whole +-BW/2 chirp (the Pluto default of 200 kHz would clip
        LongFast's 250 kHz edges) and the carrier goes to the preset's
        frequency; leaving the mode restores the device default."""
        if mode == self.MODE_MESHTASTIC:
            preset = self.meshtastic_preset
            default_bw = self.device.default_bandwidth_hz or 0
            self.device.set_rf_bandwidth(max(default_bw, preset.bandwidth_hz * config.LORA_TX_RF_BANDWIDTH_MARGIN))
            self._lora_rf_bandwidth_active = True
            self.set_frequency(preset.frequency_hz)
        elif self._lora_rf_bandwidth_active:
            if self.device.default_bandwidth_hz:
                self.device.set_rf_bandwidth(self.device.default_bandwidth_hz)
            self._lora_rf_bandwidth_active = False

    @staticmethod
    def _meshtastic_phy(preset):
        return (preset.spreading_factor, int(preset.bandwidth_hz), preset.coding_rate)

    def meshtastic_phy_matches(self, index: int) -> bool:
        """True if preset `index` can be selected live, i.e. has the same
        SF/BW/CR the encoder was built for; otherwise the flowgraph has to
        be rebuilt with that preset."""
        return self._meshtastic_phy(config.MESHTASTIC_PRESETS[int(index)]) == self._lora_phy

    def set_meshtastic_preset(self, index: int):
        if not self.meshtastic_phy_matches(index):
            raise ValueError("preset has a different SF/BW/CR -- rebuild the flowgraph with it")
        self.meshtastic_preset_index = int(index)
        self._meshtastic_pending = None
        if self.mode == self.MODE_MESHTASTIC:
            self.set_frequency(self.meshtastic_preset.frequency_hz)

    def set_meshtastic_text(self, text: str):
        self.meshtastic_text = text
        self._meshtastic_pending = None

    def set_meshtastic_node_id(self, node_id: int):
        self.meshtastic_node_id = int(node_id)
        self._meshtastic_pending = None

    def set_meshtastic_channel(self, channel_name, psk_b64: str):
        """channel_name None = the default "LongFast" channel name. psk_b64 is
        parsed at prepare time so a half-typed key never raises here."""
        self.meshtastic_channel_name = channel_name
        self.meshtastic_psk_b64 = psk_b64
        self._meshtastic_pending = None

    def set_meshtastic_hop_limit(self, hop_limit: int):
        self.meshtastic_hop_limit = max(0, min(config.MESHTASTIC_MAX_HOP_LIMIT, int(hop_limit)))
        self._meshtastic_pending = None

    def set_meshtastic_callsign(self, callsign: str):
        self.meshtastic_callsign = callsign.strip().upper()
        self._meshtastic_pending = None

    def meshtastic_duty_status(self, now=None):
        """(used_s, budget_s) of the current preset's rolling duty-cycle window;
        budget_s is 0 when the preset has no limit."""
        now = time.monotonic() if now is None else now
        self._meshtastic_duty.limit = self.meshtastic_preset.duty_cycle_limit
        return self._meshtastic_duty.used_s(now), self._meshtastic_duty.budget_s()

    def prepare_meshtastic_tx(self, now=None):
        """Build the over-the-air packet for the current settings and run every
        pre-flight check WITHOUT touching RF. Returns (ok, message, info):
        info has "packet", "airtime_s" and "preset" when ok. Checks: text set
        and within the byte limit, valid PSK, Ham-Mode rules on presets that
        require them (callsign mandatory and appended to the text, encryption
        forced off), previous frame no longer draining, and the preset's
        duty-cycle budget. key_ptt() calls this itself when nothing is
        pending; the GUI calls it first so a refusal never reaches the key."""
        now = time.monotonic() if now is None else now
        preset = self.meshtastic_preset
        text = self.meshtastic_text.strip()
        if not text:
            return False, "Enter a message first.", None
        if now < self._meshtastic_busy_until:
            return False, "The previous frame is still being sent.", None
        callsign = self.meshtastic_callsign.strip().upper()
        channel_name = self.meshtastic_channel_name or preset.default_channel_name
        try:
            psk = meshtastic_codec.parse_psk(self.meshtastic_psk_b64)
        except ValueError as e:
            return False, f"Invalid PSK: {e}", None
        if preset.ham_mode_required:
            if not callsign:
                return False, f"{preset.name}: Ham Mode needs your callsign.", None
            psk = b""  # amateur bands: no encryption
            if callsign not in text.upper():
                text = f"{text} [{callsign}]"
        if len(text.encode("utf-8")) > config.MESHTASTIC_TEXT_MAX_BYTES:
            return False, f"Text is longer than {config.MESHTASTIC_TEXT_MAX_BYTES} bytes.", None
        packet = meshtastic_codec.build_text_packet(
            text, self.meshtastic_node_id, channel_name=channel_name, psk=psk, hop_limit=self.meshtastic_hop_limit,
        )
        airtime_s = lora_airtime.lora_airtime_s(
            len(packet), preset.spreading_factor, preset.bandwidth_hz, lora_airtime.cr_index(preset.coding_rate),
            config.MESHTASTIC_PREAMBLE_LEN,
        )
        self._meshtastic_duty.limit = preset.duty_cycle_limit
        ok, wait_s = self._meshtastic_duty.check(airtime_s, now)
        if not ok:
            if wait_s == float("inf"):
                return False, "This frame alone exceeds the duty-cycle budget.", None
            return False, (f"Duty-cycle limit ({preset.duty_cycle_limit:.0%} per hour) reached -- "
                           f"next frame possible in {wait_s / 60:.1f} min."), None
        info = {"packet": packet, "airtime_s": airtime_s, "preset": preset, "text": text}
        self._meshtastic_pending = (packet, airtime_s, preset)
        return True, f"{len(packet)} B, {airtime_s:.2f} s airtime", info

    def set_baseband_deviation(self, hz: float):
        """Retune baseband_mod's FM sensitivity in place -- mirrors
        AdvancedRxFlowgraph's set_fm_demod_width()/set_baseband_width()
        pattern (recompute from the stored Hz value, push into the
        already-built block, no reconnect needed)."""
        self.baseband_deviation_hz = float(hz)
        quad_rate = int(self.device.sample_rate_hz)
        self.baseband_sensitivity = 2 * math.pi * self.baseband_deviation_hz / quad_rate
        self.baseband_mod.set_sensitivity(self.baseband_sensitivity)

    def set_freedv_variant(self, freedv_variant: int):
        """Switch between FreeDV 2020/2020B at runtime. Frame sizes differ
        between the two (2020: 2880->1440, 2020B: 1440->720 samples/call --
        measured this session), so this rebuilds the FreeDVEncoder instance
        rather than attempting to reconfigure output_multiple/relative_rate
        on an already-scheduled live block (uncertain, unverified territory)
        -- same lock()/connect()/disconnect() swap pattern as set_mode()."""
        if not FREEDV_AVAILABLE or freedv_variant == self.freedv_variant:
            return
        self.lock()
        try:
            old_encoder = self.freedv_encoder
            self.disconnect(self.freedv_float_to_short, old_encoder)
            self.disconnect(old_encoder, self.freedv_short_to_float)
            self.freedv_encoder = FreeDVEncoder(mode=freedv_variant, text=self.freedv_callsign)
            self.connect(self.freedv_float_to_short, self.freedv_encoder)
            self.connect(self.freedv_encoder, self.freedv_short_to_float)
        finally:
            self.unlock()
        old_encoder.close()
        self.freedv_variant = freedv_variant

    def set_m17_src_callsign(self, callsign: str):
        self.m17_src_callsign = callsign
        if M17_AVAILABLE:
            self.m17_coder.set_src_id(callsign)

    def set_m17_dst_callsign(self, callsign: str):
        self.m17_dst_callsign = callsign or config.M17_DEFAULT_DST_CALLSIGN
        if M17_AVAILABLE:
            self.m17_coder.set_dst_id(self.m17_dst_callsign)

    def set_freedv_callsign(self, callsign: str):
        self.freedv_callsign = callsign
        if FREEDV_AVAILABLE:
            self.freedv_encoder.set_text(callsign)

    def set_digitext_text(self, text: str):
        """Only caches the value and marks the cached audio stale -- the
        actual (comparatively expensive) render+ISTFT-encode happens lazily
        in _ensure_digitext_audio(), called from key_ptt(), not on every
        keystroke."""
        self.digitext_text = text
        self._digitext_audio_dirty = True

    def set_digitext_layout(self, layout: str):
        assert layout in (digitext.LAYOUT_HORIZONTAL, digitext.LAYOUT_VERTICAL), f"unknown digitext layout {layout!r}"
        self.digitext_layout = layout
        self._digitext_audio_dirty = True

    def set_digitext_zoom(self, zoom: int):
        self.digitext_zoom = max(1, int(zoom))
        self._digitext_audio_dirty = True

    def set_digitext_min_freq_hz(self, min_freq_hz: float):
        """Operator-adjustable via a GUI slider, by explicit request -- lets
        the operator trade off against the AD9361 mirror-image finding
        (config.py's DIGITEXT_MIN_FREQ_HZ comment) themselves, on their own
        receiver, instead of only the one fixed default this app ships
        with."""
        self.digitext_min_freq_hz = float(min_freq_hz)
        self._digitext_audio_dirty = True

    def _ensure_digitext_audio(self):
        """Renders/encodes the current digitext_text/digitext_layout into
        self._digitext_audio if it isn't already cached and up to date --
        called right before a Digitext transmission starts (key_ptt()), not
        eagerly on every text/layout change, so fast typing doesn't trigger
        repeated PIL renders + audio synthesis for no reason."""
        if self._digitext_audio_dirty or self._digitext_audio is None:
            self._digitext_audio, self.digitext_duration_s = digitext.encode_text(
                self.digitext_text, self.digitext_layout, config.DIGITEXT_FONT_SIZE_PX,
                config.DIGITEXT_SAMPLE_RATE, config.DIGITEXT_HZ_PER_COL, config.DIGITEXT_ROW_DWELL_S,
                min_freq_hz=self.digitext_min_freq_hz, tail_s=config.DIGITEXT_TAIL_S + self.device.tx_end_loss_s,
                col_downsample=config.DIGITEXT_COL_DOWNSAMPLE, zoom=self.digitext_zoom,
            )
            self._digitext_audio_dirty = False

    def set_psk31_text(self, text: str):
        """Only caches the value and marks the cached audio stale -- mirrors
        set_digitext_text() exactly (the actual, comparatively expensive
        Varicode/BPSK synthesis happens lazily in _ensure_psk31_audio(),
        called from key_ptt(), not on every keystroke)."""
        self.psk31_text = text
        self._psk31_audio_dirty = True

    def set_psk31_tone_hz(self, tone_hz: float):
        self.psk31_tone_hz = float(tone_hz)
        self._psk31_audio_dirty = True

    def _ensure_psk31_audio(self):
        """Mirrors _ensure_digitext_audio() exactly."""
        if self._psk31_audio_dirty or self._psk31_audio is None:
            self._psk31_audio, self.psk31_duration_s = psk31.encode_text(
                self.psk31_text, config.AUDIO_RATE, self.psk31_tone_hz,
                tail_s=config.PSK31_TAIL_S + self.device.tx_end_loss_s, preamble_chars=config.PSK31_PREAMBLE_CHARS,
            )
            self._psk31_audio_dirty = False

    def set_rtty_text(self, text: str):
        """Mirrors set_psk31_text() exactly."""
        self.rtty_text = text
        self._rtty_audio_dirty = True

    def set_rtty_mark_hz(self, mark_hz: float):
        self.rtty_mark_hz = float(mark_hz)
        self._rtty_audio_dirty = True

    def set_rtty_shift_hz(self, shift_hz: float):
        self.rtty_shift_hz = float(shift_hz)
        self._rtty_audio_dirty = True

    def set_rtty_baud_rate(self, baud_rate: float):
        self.rtty_baud_rate = float(baud_rate)
        self._rtty_audio_dirty = True

    def set_rtty_reverse(self, reverse: bool):
        self.rtty_reverse = bool(reverse)
        self._rtty_audio_dirty = True

    def _ensure_rtty_audio(self):
        """Mirrors _ensure_psk31_audio() exactly -- the dirty flag covers
        all five operator-adjustable RTTY parameters (text/mark/shift/
        baud/reverse), any of which requires a fresh synthesis pass."""
        if self._rtty_audio_dirty or self._rtty_audio is None:
            self._rtty_audio, self.rtty_duration_s = rtty.encode_text(
                self.rtty_text, config.AUDIO_RATE, self.rtty_mark_hz, self.rtty_shift_hz,
                self.rtty_baud_rate, reverse=self.rtty_reverse,
                tail_s=config.RTTY_TAIL_S + self.device.tx_end_loss_s, preamble_s=config.RTTY_PREAMBLE_S,
                stop_bits=config.RTTY_STOP_BITS,
            )
            self._rtty_audio_dirty = False

    def add_filebroadcast_file(self, filename: str, data: bytes):
        """Adds a file to the rotation -- can be called at ANY time,
        including while already keyed/mid-broadcast (Phase 2's whole point,
        see filebroadcast_source.py): request_rebuild() only takes effect
        at the end of the CURRENT rotation cycle, never interrupting a
        frame already in flight. Returns the assigned file_id."""
        file_id = self.filebroadcast_planner.add_file(filename, data)
        self.filebroadcast_source.request_rebuild()
        return file_id

    def remove_filebroadcast_file(self, file_id):
        self.filebroadcast_planner.remove_file(file_id)
        self.filebroadcast_source.request_rebuild()

    def clear_filebroadcast_files(self):
        self.filebroadcast_planner.clear()
        self.filebroadcast_source.request_rebuild()

    def filebroadcast_files(self):
        return self.filebroadcast_planner.files()

    def set_nf_gain(self, gain: float):
        self.nf_gain.set_k(gain)

    def set_gate_enabled(self, enabled: bool):
        # pwr_squelch_ff has no enable/disable API -- bypass by dropping the
        # threshold to a floor that effectively never gates, restoring the
        # cached real value when re-enabled.
        self.gate.set_threshold(self._gate_threshold_db if enabled else config.GATE_BYPASS_THRESHOLD_DB)

    def set_gate_threshold(self, db: float):
        self._gate_threshold_db = db
        self.gate.set_threshold(db)

    def set_compressor_enabled(self, enabled: bool):
        self.compressor.set_enabled(enabled)

    def set_compressor_threshold(self, db: float):
        self.compressor.set_threshold_db(db)

    def set_compressor_ratio(self, ratio: float):
        self.compressor.set_ratio(ratio)

    def set_limiter_enabled(self, enabled: bool):
        self.limiter_smooth.set_enabled(enabled)

    def set_fine_offset(self, offset_hz: float):
        self.fine_offset_hz = offset_hz
        self.device.set_frequency(self.nominal_freq_hz + self.fine_offset_hz)

    def set_frequency(self, freq_hz: float):
        self.nominal_freq_hz = freq_hz
        self.device.set_frequency(self.nominal_freq_hz + self.fine_offset_hz)

    def set_target_power(self, value: float):
        stage = self.device.primary_stage
        self.target_power = max(stage.min_value, min(self.power_ceiling, value))
        if self._keyed:
            self.device.set_power(stage.name, self.target_power)

    def set_secondary_power(self, stage_name: str, value):
        """For non-primary power stages (e.g. HackRF's coarse AMP on/off)
        that the power ceiling/'unlock full power' clamp doesn't apply to --
        no equivalent exists on single-stage devices like Pluto. Stored and
        applied immediately if already keyed, exactly like
        set_target_power() for the primary stage. unkey_ptt()/
        finish_unkey_m17() don't need to separately reset this: the device's
        own post_unkey() hook is responsible for forcing every non-primary
        stage back to its safe/off value between transmissions (see each
        TxDevice subclass) -- the cached value here just gets re-applied at
        the next key_ptt(), matching how target_power persists across
        key/unkey cycles too."""
        self._secondary_power[stage_name] = value
        if self._keyed:
            self.device.set_power(stage_name, value)

    @property
    def keyed(self):
        return self._keyed

    def key_ptt(self):
        """PTT press: run the device's pre-key hook (on Pluto: power the TX
        LO back up -- it's kept powered down whenever unkeyed, see
        unkey_ptt()/finish_unkey_m17() -- and wait for the synthesizer to
        relock), then unmute audio AND tx_gain, then raise RF power. In M17
        mode, also sends SOT (start of transmission) -- without it
        m17_coder silently discards all input and emits nothing (verified
        this session).

        tx_gain (the last block before the device sink, downstream of
        EVERY modulation branch) is unmuted here alongside ptt_mute, not
        left permanently at pass-through -- see the comment on tx_gain's
        construction above for why muting only upstream (ptt_mute) isn't
        enough: FM-modulating silence is a full-amplitude carrier, and
        FreeDV's OFDM modem keeps emitting pilot/sync tones regardless of
        speech content. Confirmed on real HackRF hardware: a carrier was
        present from the moment the flowgraph started, not just after
        key_ptt(), because tx_gain used to default to pass-through.

        The device's pre-key/post-unkey hooks are hard-tied to PTT in every
        mode, not just at app shutdown: on Pluto, the TX attenuator alone
        (down to MIN_ATTEN) does not fully suppress LO leakage, and an
        external PA connected to the TX port amplifies that residual
        leakage into a real, measurable spike whenever the LO is left
        running between transmissions -- powering the synthesizer off (not
        just attenuating it) is the only way to actually make it disappear,
        regardless of which modulation branch is active. See each TxDevice
        subclass (pluto_tx/devices/) for what its own pre_key()/post_unkey()
        actually does.

        RADE with a Soundcard device selected (device_type="soundcard", see
        devices/soundcard.py) is a special case, handled as an early return
        below: the operator has chosen to transmit via an externally-
        connected radio over a sound card instead of this app's own SDR, so
        the SDR device must stay COMPLETELY untouched -- no pre_key(), no
        LO, no power change of any kind. Only rade_audio_gain (a dedicated
        mute gate on the soundcard-output branch, starting muted just like
        tx_gain) is unmuted -- PLUS tx_gain itself, unlike every other early
        return below: SoundcardDevice.build_sink() is a null_sink (nothing
        downstream to protect), and the TX waterfall taps tx_gain's output
        (see enable_waterfall below), so leaving it muted here would show a
        flat/empty waterfall during an otherwise-live transmission (a real
        bug found by the operator on real hardware -- the waterfall went
        completely blank in Soundcard mode). The self.mode == self.MODE_RADE
        check is kept alongside is_audio_only() (not just the latter alone)
        as a defensive guard for the RADE_AVAILABLE=False edge case, where
        mode already falls back to FM in __init__ but rade_audio_gain never
        gets built at all -- unreachable via the GUI (it greys out the
        Soundcard device entry when RADE is unavailable), but cheap
        insurance against an AttributeError if ever constructed some other
        way."""
        self._m17_ending = False
        self._rade_ending = False
        if self.mode == self.MODE_MESHTASTIC and self._meshtastic_pending is None:
            ok, message, _info = self.prepare_meshtastic_tx()
            if not ok:
                raise ValueError(message)  # refused before any RF action
        if self.mode == self.MODE_RADE and self.device.is_audio_only():
            self.ptt_mute.set_k(1.0)
            self.tx_gain.set_k(1.0 + 0j)
            self.rade_audio_gain.set_k(1.0)
            self._keyed = True
            return
        if self.mode == self.MODE_DIGITEXT:
            # Rebuild digitext_source fresh (reset to the start) right
            # before every transmission -- vector_source_f has no seek/
            # reset API, so a new instance is the same "fresh one-shot
            # source per transmission" idiom already proven for RADE's EOO
            # tail (unkey_ptt()'s rade_eoo_enabled branch below). Reconnects
            # BOTH downstream fan-out branches (the SDR path's Hilbert
            # modulator AND the Soundcard path's audio_gain, see the
            # Digitext Soundcard branch construction comment) -- happens
            # regardless of which device is actually selected, same as the
            # rest of this app's "every branch always connected" pattern.
            self._ensure_digitext_audio()
            self.lock()
            try:
                self.disconnect(self.digitext_source, self.digitext_ssb_mod)
                self.disconnect(self.digitext_source, self.digitext_audio_gain)
                self.digitext_source = blocks.vector_source_f(self._digitext_audio.tolist(), repeat=False)
                self.connect(self.digitext_source, self.digitext_ssb_mod)
                self.connect(self.digitext_source, self.digitext_audio_gain)
            finally:
                self.unlock()
            if self.device.is_audio_only():
                # Mirrors RADE's Soundcard early return above: SDR device
                # stays completely untouched, tx_gain stays unmuted purely
                # so the TX waterfall (which taps it) keeps showing the
                # live signal (SoundcardDevice.build_sink() is a null_sink,
                # nothing downstream to protect) -- see that branch's
                # docstring for the full reasoning, identical here.
                self.tx_gain.set_k(1.0 + 0j)
                self.digitext_audio_gain.set_k(1.0)
                self._keyed = True
                return
        if self.mode == self.MODE_PSK31:
            # Exact structural mirror of the MODE_DIGITEXT branch just
            # above -- see its own comments for the full reasoning
            # (one-shot fresh vector_source_f per press, both downstream
            # fan-out branches reconnected, Soundcard early return).
            self._ensure_psk31_audio()
            self.lock()
            try:
                self.disconnect(self.psk31_source, self.psk31_ssb_mod)
                self.disconnect(self.psk31_source, self.psk31_audio_gain)
                self.psk31_source = blocks.vector_source_f(self._psk31_audio.tolist(), repeat=False)
                self.connect(self.psk31_source, self.psk31_ssb_mod)
                self.connect(self.psk31_source, self.psk31_audio_gain)
            finally:
                self.unlock()
            if self.device.is_audio_only():
                self.tx_gain.set_k(1.0 + 0j)
                self.psk31_audio_gain.set_k(1.0)
                self._keyed = True
                return
        if self.mode == self.MODE_POCSAG:
            self._ensure_pocsag_audio()  # raises ValueError before any RF action; the source starts last
            if self.device.is_audio_only():
                self.tx_gain.set_k(1.0 + 0j)
                self.pocsag_audio_gain.set_k(config.POCSAG_SOUNDCARD_LEVEL)
                self._keyed = True
                self._pocsag_start_source()
                return
        if self.mode == self.MODE_RTTY:
            # Exact structural mirror of the MODE_PSK31 branch just above.
            self._ensure_rtty_audio()
            self.lock()
            try:
                self.disconnect(self.rtty_source, self.rtty_ssb_mod)
                self.disconnect(self.rtty_source, self.rtty_audio_gain)
                self.rtty_source = blocks.vector_source_f(self._rtty_audio.tolist(), repeat=False)
                self.connect(self.rtty_source, self.rtty_ssb_mod)
                self.connect(self.rtty_source, self.rtty_audio_gain)
            finally:
                self.unlock()
            if self.device.is_audio_only():
                self.tx_gain.set_k(1.0 + 0j)
                self.rtty_audio_gain.set_k(1.0)
                self._keyed = True
                return
        self.device.pre_key()
        if self.mode == self.MODE_M17:
            self.m17_coder.post(_pmt.intern("transmission_control"), _pmt.intern("SOT"))
            self.m17_codec2_encoder.post(_pmt.intern("state_reset"), _pmt.intern("SOT"))
        self.ptt_mute.set_k(1.0)
        self.tx_gain.set_k(1.0 + 0j)
        self.device.set_power(self.device.primary_stage.name, self.target_power)
        # Only apply cached secondary-stage values the CURRENT device
        # actually has -- self._secondary_power can carry a stale entry from
        # a previous device_type (e.g. the GUI unconditionally caches
        # HackRF's "AMP" checkbox state even while Pluto, which has no such
        # stage, is connected).
        device_stage_names = {s.name for s in self.device.power_stages}
        for stage_name, value in self._secondary_power.items():
            if stage_name in device_stage_names:
                self.device.set_power(stage_name, value)
        if self.mode == self.MODE_MESHTASTIC:
            packet, airtime_s, preset = self._meshtastic_pending
            self._meshtastic_pending = None
            now = time.monotonic()
            self.meshtastic_last_airtime_s = airtime_s
            # the encoder's delay block shifts the frame by 10.1 symbols before it
            # starts (see pluto_tx/lora.py, bug 2) -- RF must stay up for that too
            lead_s = 10.1 * lora_airtime.symbol_time_s(preset.spreading_factor, preset.bandwidth_hz)
            self.meshtastic_hold_s = lead_s + airtime_s + config.MESHTASTIC_TX_TAIL_S
            self._meshtastic_busy_until = now + self.meshtastic_hold_s
            self._meshtastic_duty.record(airtime_s, now)
            self.lora_encoder.send_payload_bytes(packet)
        self._keyed = True
        if self.mode == self.MODE_POCSAG:
            self._pocsag_start_source()

    def unkey_ptt(self):
        """PTT release. FM/SSB: mute tx_gain FIRST (severs the actual RF
        path at the last point before the device, downstream of every
        modulator -- see tx_gain's construction comment for why ptt_mute
        alone isn't sufficient), then kill RF power, mute audio, then run
        the device's post-unkey hook (order is safety-critical -- power to
        minimum before that hook even runs; on Pluto, LO-off is what finally
        kills the leakage spike for good). M17 is different: muting audio
        and sending EOT happen immediately, but RF (and therefore whatever
        the post-unkey hook would otherwise do) must stay up briefly
        afterward for the encoder's EOT tail (final frame + EOT frames,
        ~80ms minimum) to actually transmit -- cutting RF instantly would
        leave the receiver hanging with no clean end-of-stream. self._keyed
        stays True and self._m17_ending is set; the GUI drives
        finish_unkey_m17() after a bounded delay to actually lower power and
        run the post-unkey hook. This tail is NOT a safety gap:
        force_safe_state() (E-STOP, shutdown_safe()) forces the device dark
        immediately regardless, via the device's own independent safety
        layer, at any point during the tail.

        RADE is a THIRD variant, only when rade_eoo_enabled=True (off by
        default -- see its own docstring): unlike M17's autonomous coder-
        driven tail, RADE's End-of-Over is one explicit, deterministic,
        one-shot call (send_eoo(), n_tx_eoo_out samples = 144ms at
        RADE_MODEM_SAMPLE_RATE) -- not a continuous stream to keep feeding.
        tx_gain's upstream is briefly rerouted from rade_tx_resampler to a
        one-shot vector_source_c holding exactly that EOO IQ, held up for
        rade_eoo_hold_s (computed from n_tx_eoo_out, not a guessed
        constant), then finish_unkey_rade() (GUI-timer-driven, same pattern
        as finish_unkey_m17()) restores the normal streaming connection and
        actually lowers power. With rade_eoo_enabled=False (default), RADE
        falls through to the plain else branch below -- immediate full
        unkey, no tail, identical to FreeDV's behavior.

        RADE with a Soundcard device selected is, again, an early-return
        special case (see key_ptt()'s matching branch): mutes rade_audio_gain
        AND tx_gain (both were unmuted there, tx_gain purely so the TX
        waterfall keeps showing the live signal -- see key_ptt()'s docstring),
        immediately, no tail (EOO is meaningful for a receiving SDR's
        acquisition state machine, not documented/designed for an
        audio-injected signal into a conventional radio -- out of scope here
        regardless of rade_eoo_enabled). The SDR device is never touched, so
        there's nothing to power down."""
        if self.mode == self.MODE_RADE and self.device.is_audio_only():
            self.ptt_mute.set_k(0.0)
            self.tx_gain.set_k(0.0 + 0j)
            self.rade_audio_gain.set_k(0.0)
            self._keyed = False
            return
        if self.mode == self.MODE_DIGITEXT and self.device.is_audio_only():
            # Mirrors RADE's Soundcard early return immediately above.
            self.tx_gain.set_k(0.0 + 0j)
            self.digitext_audio_gain.set_k(0.0)
            self._keyed = False
            return
        if self.mode == self.MODE_PSK31 and self.device.is_audio_only():
            self.tx_gain.set_k(0.0 + 0j)
            self.psk31_audio_gain.set_k(0.0)
            self._keyed = False
            return
        if self.mode == self.MODE_RTTY and self.device.is_audio_only():
            self.tx_gain.set_k(0.0 + 0j)
            self.rtty_audio_gain.set_k(0.0)
            self._keyed = False
            return
        if self.mode == self.MODE_POCSAG and self.device.is_audio_only():
            self.tx_gain.set_k(0.0 + 0j)
            self.pocsag_audio_gain.set_k(0.0)
            self._keyed = False
            return
        if self.mode == self.MODE_M17:
            self.ptt_mute.set_k(0.0)
            self.m17_coder.post(_pmt.intern("transmission_control"), _pmt.intern("EOT"))
            self._m17_ending = True
        elif self.mode == self.MODE_RADE and self.rade_eoo_enabled:
            self.ptt_mute.set_k(0.0)
            eoo_iq = self.rade_encoder.send_eoo()
            self._rade_eoo_source = blocks.vector_source_c(eoo_iq.tolist(), repeat=False)
            self.lock()
            try:
                self.disconnect(self.rade_tx_resampler, self.tx_gain)
                self.connect(self._rade_eoo_source, self.tx_gain)
            finally:
                self.unlock()
            self._rade_ending = True
        else:
            self.tx_gain.set_k(0.0 + 0j)
            stage = self.device.primary_stage
            self.device.set_power(stage.name, stage.off_value)
            self.ptt_mute.set_k(0.0)
            self.device.post_unkey()
            if not self.device.supports_persistent_sink:
                self._rebuild_device_sink()
            self._keyed = False

    @property
    def rade_eoo_hold_s(self):
        """Seconds the GUI must hold RF up after unkey_ptt() in RADE mode
        with rade_eoo_enabled=True before calling finish_unkey_rade() --
        computed from the encoder's own n_tx_eoo_out, not a guessed
        constant (mirrors M17_EOT_HOLD_S's real-hardware-calibrated intent,
        but here it's exact by construction rather than measured)."""
        return self.rade_encoder.n_tx_eoo_out / _rade_ctypes.RADE_MODEM_SAMPLE_RATE if RADE_AVAILABLE else 0.0

    def finish_unkey_m17(self):
        """Called by the GUI a bounded delay after unkey_ptt() in M17 mode,
        once the EOT tail has had time to actually transmit. No-op if the
        operator already keyed up again in the meantime (key_ptt() clears
        _m17_ending, making a stale pending call here harmless)."""
        if not self._m17_ending:
            return
        self.tx_gain.set_k(0.0 + 0j)
        stage = self.device.primary_stage
        self.device.set_power(stage.name, stage.off_value)
        self.device.post_unkey()
        if not self.device.supports_persistent_sink:
            self._rebuild_device_sink()
        self._m17_ending = False
        self._keyed = False

    def finish_unkey_rade(self):
        """Called by the GUI a bounded delay (rade_eoo_hold_s) after
        unkey_ptt() in RADE mode with rade_eoo_enabled=True, once the EOO
        tail has had time to actually transmit. No-op if the operator
        already keyed up again in the meantime (key_ptt() clears
        _rade_ending, making a stale pending call here harmless) -- same
        contract as finish_unkey_m17()."""
        if not self._rade_ending:
            return
        self.tx_gain.set_k(0.0 + 0j)
        stage = self.device.primary_stage
        self.device.set_power(stage.name, stage.off_value)
        self.device.post_unkey()
        if not self.device.supports_persistent_sink:
            self._rebuild_device_sink()
        self.lock()
        try:
            self.disconnect(self._rade_eoo_source, self.tx_gain)
            self.connect(self.rade_tx_resampler, self.tx_gain)
        finally:
            self.unlock()
        self._rade_eoo_source = None
        self._rade_ending = False
        self._keyed = False

    def set_rade_eoo_enabled(self, enabled: bool):
        self.rade_eoo_enabled = enabled

    def _rebuild_device_sink(self):
        """For devices where zeroing every power stage isn't enough to
        actually stop transmission (TxDevice.supports_persistent_sink=False
        -- confirmed on real hardware for HackRF, see devices/hackrf.py):
        close and reopen the device's sink block, via the same
        lock()/disconnect()/connect()/unlock() pattern already proven safe
        here for mode switching (set_mode()/set_freedv_variant() above).
        The old sink's only remaining Python reference is overwritten
        below, so it's garbage-collected (releasing the underlying
        hardware/USB handle) immediately -- the same effect Disconnect
        already had, just scoped to the device sink instead of tearing
        down the whole flowgraph. build_sink() always constructs at the
        device's minimum power (see its docstring), so the freshly rebuilt
        sink is silent by construction, not just by a gain setting that
        turned out not to be trustworthy on its own."""
        self.lock()
        try:
            self.disconnect(self.tx_gain, self._device_sink)
            self._device_sink = self.device.build_sink()
            self.connect(self.tx_gain, self._device_sink)
        finally:
            self.unlock()

    def shutdown_safe(self):
        """Stop the flowgraph and force the TX chain dark. Safe to call more than once."""
        if self._keyed:
            self.unkey_ptt()
        try:
            self.stop()
            self.wait()
        except Exception as e:
            print(f"WARNING: flowgraph stop() failed: {e}", file=sys.stderr)
        self.device.force_safe_state()
