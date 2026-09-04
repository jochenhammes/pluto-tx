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
import wave

from gnuradio import gr, blocks, filter, analog, audio, iio, qtgui
from gnuradio.filter import firdes
from gnuradio.fft import window

from . import config
from .safety import PlutoSafety

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


def _gr_atten(db: float) -> float:
    """gr-iio's fmcomms2_sink_fc32.set_attenuation() expects a POSITIVE dB
    magnitude (it negates internally before writing the AD9361 hardwaregain
    register) -- the opposite sign convention from raw libiio/iio_attr, which
    this codebase otherwise uses everywhere (config.MIN_ATTEN = -89.75, etc).
    Convert only at this boundary."""
    return abs(db)


class PlutoTxFlowgraph(gr.top_block):
    SRC_MIC = 0
    SRC_FILE = 1
    MODE_FM = 0
    MODE_SSB = 1

    def __init__(self, uri=config.DEFAULT_URI, frequency=config.DEFAULT_FREQUENCY,
                 atten_ceiling_db=config.DEFAULT_ATTEN_CEILING, audio_device="",
                 wav_path=None, mode=MODE_FM, source=SRC_MIC, enable_waterfall=False):
        super().__init__("PlutoTxFlowgraph")

        self.uri = uri
        self.nominal_freq_hz = float(frequency)
        self.fine_offset_hz = 0.0
        self.atten_ceiling_db = atten_ceiling_db
        self.target_atten_db = atten_ceiling_db
        self._keyed = False

        # Safety layer first: attenuation to minimum, LO up -- BEFORE the GR
        # sink (which enables TX channels at construction time) exists.
        self.safety = PlutoSafety(uri)
        self.safety.prepare_for_start()

        # --- Sources ---------------------------------------------------
        self.mic_source = audio.source(config.AUDIO_RATE, audio_device, True)
        self.wav_path = wav_path or _default_wav_path()
        file_mono = self._build_file_source(self.wav_path)

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

        initial_preset = "SSB" if mode == self.MODE_SSB else "FM"
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

        # --- FM branch: real audio @ AUDIO_RATE -> resample -> FM ---------
        g = math.gcd(config.QUAD_RATE, config.AUDIO_RATE)
        self.fm_resampler = filter.rational_resampler_fff(
            interpolation=config.QUAD_RATE // g, decimation=config.AUDIO_RATE // g,
            taps=[], fractional_bw=0.4,
        )
        self.fm_sensitivity = 2 * math.pi * config.FM_DEVIATION_HZ / config.QUAD_RATE
        self.fm_mod = analog.frequency_modulator_fc(self.fm_sensitivity)

        # --- SSB branch: Hilbert AT AUDIO RATE (see module docstring),
        # then resample the resulting complex analytic signal up to TX rate.
        # 401 taps: measured offline (synthetic tone sweep, no RF) opposite-
        # sideband suppression across the 300-2700 Hz voice band -- 129 taps
        # only gave ~16 dB at the 300 Hz low edge (audible LSB leakage,
        # matches what was heard over the air); 401 taps gives >=61 dB
        # across the whole band.
        self.ssb_mod = filter.hilbert_fc(401, window.WIN_HAMMING, 6.76)
        self.ssb_resampler = filter.rational_resampler_ccf(
            interpolation=config.QUAD_RATE // g, decimation=config.AUDIO_RATE // g,
            taps=[], fractional_bw=0.4,
        )

        self.mode_selector = blocks.selector(gr.sizeof_gr_complex, mode, 0)
        self.mode_selector.set_enabled(True)

        self.tx_gain = blocks.multiply_const_cc(1.0 + 0j)

        # --- Live view of the modulated baseband actually fed to the sink.
        # Optional: a qtgui sink is a real Qt widget and needs a
        # QApplication to already exist -- only requested by the GUI, so
        # headless CLI use (stage 1/2 style) stays Qt-free.
        self.waterfall = None
        if enable_waterfall:
            self.waterfall = qtgui.waterfall_sink_c(
                1024, window.WIN_BLACKMAN_hARRIS, 0, config.QUAD_RATE, "TX Basisband (vor Pluto-Sink)", 1
            )

        # --- PlutoSDR sink: ALWAYS constructed at MIN_ATTEN, never the
        # operator's target power (the constructor enables TX channels
        # immediately, before tb.start() is ever called).
        self.pluto_sink = iio.fmcomms2_sink_fc32(uri, [True, True], 0x8000, False)
        self.pluto_sink.set_bandwidth(config.DEFAULT_BANDWIDTH)
        self.pluto_sink.set_frequency(int(self.nominal_freq_hz))
        self.pluto_sink.set_samplerate(config.QUAD_RATE)
        self.pluto_sink.set_attenuation(0, _gr_atten(config.MIN_ATTEN))

        # --- Wiring ---------------------------------------------------
        self.connect(self.mic_source, (self.source_selector, self.SRC_MIC))
        self.connect(file_mono, (self.source_selector, self.SRC_FILE))
        self.connect(self.source_selector, self.ptt_mute)
        self.connect(self.ptt_mute, self.nf_filter)
        self.connect(self.nf_filter, self.agc)
        self.connect(self.agc, self.nf_gain)
        self.connect(self.nf_gain, self.limiter)

        self.connect(self.limiter, self.fm_resampler)
        self.connect(self.fm_resampler, self.fm_mod)
        self.connect(self.fm_mod, (self.mode_selector, self.MODE_FM))

        self.connect(self.limiter, self.ssb_mod)
        self.connect(self.ssb_mod, self.ssb_resampler)
        self.connect(self.ssb_resampler, (self.mode_selector, self.MODE_SSB))

        self.connect(self.mode_selector, self.tx_gain)
        self.connect(self.tx_gain, self.pluto_sink)
        if self.waterfall is not None:
            self.connect(self.tx_gain, self.waterfall)

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

    def set_mode(self, mode: int):
        self.mode_selector.set_input_index(mode)
        preset = "SSB" if mode == self.MODE_SSB else "FM"
        f_lo, f_hi, trans = config.NF_FILTER_PRESETS[preset]
        taps = firdes.band_pass(1.0, config.AUDIO_RATE, f_lo, f_hi, trans, window.WIN_HAMMING)
        self.nf_filter.set_taps(taps)

    def set_nf_gain(self, gain: float):
        self.nf_gain.set_k(gain)

    def set_fine_offset(self, offset_hz: float):
        self.fine_offset_hz = offset_hz
        self.pluto_sink.set_frequency(int(self.nominal_freq_hz + self.fine_offset_hz))

    def set_frequency(self, freq_hz: float):
        self.nominal_freq_hz = freq_hz
        self.pluto_sink.set_frequency(int(self.nominal_freq_hz + self.fine_offset_hz))

    def set_target_power(self, atten_db: float):
        self.target_atten_db = max(config.MIN_ATTEN, min(self.atten_ceiling_db, atten_db))
        if self._keyed:
            self.pluto_sink.set_attenuation(0, _gr_atten(self.target_atten_db))

    @property
    def keyed(self):
        return self._keyed

    def key_ptt(self):
        """PTT press: unmute audio, then raise RF power."""
        self.ptt_mute.set_k(1.0)
        self.pluto_sink.set_attenuation(0, _gr_atten(self.target_atten_db))
        self._keyed = True

    def unkey_ptt(self):
        """PTT release: kill RF power FIRST, then mute audio (order is safety-critical)."""
        self.pluto_sink.set_attenuation(0, _gr_atten(config.MIN_ATTEN))
        self.ptt_mute.set_k(0.0)
        self._keyed = False

    def shutdown_safe(self):
        """Stop the flowgraph and force the TX chain dark. Safe to call more than once."""
        if self._keyed:
            self.unkey_ptt()
        try:
            self.stop()
            self.wait()
        except Exception as e:
            print(f"WARNING: flowgraph stop() failed: {e}", file=sys.stderr)
        self.safety.force_safe_state()
