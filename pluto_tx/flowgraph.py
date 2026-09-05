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
from . import dynamics
from .safety import PlutoSafety

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
    MODE_M17 = 2

    def __init__(self, uri=config.DEFAULT_URI, frequency=config.DEFAULT_FREQUENCY,
                 atten_ceiling_db=config.DEFAULT_ATTEN_CEILING, audio_device="",
                 wav_path=None, mode=MODE_FM, source=SRC_MIC, enable_waterfall=False,
                 m17_src_callsign="", m17_dst_callsign=config.M17_DEFAULT_DST_CALLSIGN):
        super().__init__("PlutoTxFlowgraph")

        self.uri = uri
        self.nominal_freq_hz = float(frequency)
        self.fine_offset_hz = 0.0
        self.atten_ceiling_db = atten_ceiling_db
        self.target_atten_db = atten_ceiling_db
        self._keyed = False
        self._m17_ending = False  # True during the brief EOT tail after unkey_ptt() in M17 mode

        if mode == self.MODE_M17 and not M17_AVAILABLE:
            # mode_selector below would otherwise be constructed pointing at
            # an input that's never connected (the M17 branch only gets
            # wired if M17_AVAILABLE) -- fall back rather than build a
            # broken flowgraph.
            mode = self.MODE_FM
        self.mode = mode  # the ACTUAL mode (post-fallback) -- GUI reads this
        # to sync mode_combo's initial selection, otherwise it always shows
        # "FM" regardless of what mode the flowgraph was actually built with.

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
            g_m17_tx = math.gcd(config.QUAD_RATE, config.M17_BASEBAND_RATE)
            self.m17_tx_resampler = filter.rational_resampler_ccf(
                interpolation=config.QUAD_RATE // g_m17_tx, decimation=config.M17_BASEBAND_RATE // g_m17_tx,
                taps=[], fractional_bw=0.4,
            )

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
        self.mode_selector = blocks.selector(gr.sizeof_gr_complex, 1 if mode == self.MODE_SSB else 0, 0)
        self.mode_selector.set_enabled(True)

        self.tx_gain = blocks.multiply_const_cc(1.0 + 0j)

        # Whichever of {mode_selector, m17_tx_resampler} is NOT currently
        # feeding tx_gain must still drain into something -- GNU Radio
        # requires every output port to be connected. null_sink is a
        # standard no-op drain for exactly this purpose.
        self._null_sink_selector = blocks.null_sink(gr.sizeof_gr_complex)
        if M17_AVAILABLE:
            self._null_sink_m17 = blocks.null_sink(gr.sizeof_gr_complex)

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
        self.connect(self.nf_filter, self.gate)
        self.connect(self.gate, self.agc)
        self.connect(self.agc, self.compressor)
        self.connect(self.compressor, self.nf_gain)
        self.connect(self.nf_gain, self.limiter_smooth)
        self.connect(self.limiter_smooth, self.limiter)

        self.connect(self.limiter, self.fm_resampler)
        self.connect(self.fm_resampler, self.fm_mod)
        self.connect(self.fm_mod, (self.mode_selector, self.MODE_FM))

        self.connect(self.limiter, self.ssb_mod)
        self.connect(self.ssb_mod, self.ssb_resampler)
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
            # m17_tx_resampler feeds tx_gain directly ONLY while in M17 mode
            # (NOT through mode_selector -- see the comment above
            # mode_selector's construction). Whichever path isn't active
            # drains into a null_sink instead.
            if mode == self.MODE_M17:
                self.connect(self.m17_tx_resampler, self.tx_gain)
                self.connect(self.mode_selector, self._null_sink_selector)
            else:
                self.connect(self.mode_selector, self.tx_gain)
                self.connect(self.m17_tx_resampler, self._null_sink_m17)
        else:
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
        prev_mode = self.mode
        was_m17 = M17_AVAILABLE and prev_mode == self.MODE_M17
        is_m17 = M17_AVAILABLE and mode == self.MODE_M17
        self.mode = mode

        if is_m17 != was_m17:
            # Entering or leaving M17 mode: reroute tx_gain's upstream
            # connection (mode_selector <-> m17_tx_resampler direct feed --
            # see the comment above mode_selector's construction for why
            # M17 can't go through mode_selector). Brief pause (lock/
            # unlock), same graph-reconfiguration pattern already used
            # elsewhere in this app (e.g. WAV file changes). FM<->SSB
            # switching below is completely unaffected by this branch.
            self.lock()
            try:
                if is_m17:
                    self.disconnect(self.mode_selector, self.tx_gain)
                    self.disconnect(self.m17_tx_resampler, self._null_sink_m17)
                    self.connect(self.m17_tx_resampler, self.tx_gain)
                    self.connect(self.mode_selector, self._null_sink_selector)
                else:
                    self.disconnect(self.m17_tx_resampler, self.tx_gain)
                    self.disconnect(self.mode_selector, self._null_sink_selector)
                    self.connect(self.mode_selector, self.tx_gain)
                    self.connect(self.m17_tx_resampler, self._null_sink_m17)
            finally:
                self.unlock()

        if is_m17:
            return  # M17 bypasses the NF filter/dynamics chain entirely, nothing to retap

        self.mode_selector.set_input_index(1 if mode == self.MODE_SSB else 0)
        preset = "SSB" if mode == self.MODE_SSB else "FM"
        f_lo, f_hi, trans = config.NF_FILTER_PRESETS[preset]
        taps = firdes.band_pass(1.0, config.AUDIO_RATE, f_lo, f_hi, trans, window.WIN_HAMMING)
        self.nf_filter.set_taps(taps)

    def set_m17_src_callsign(self, callsign: str):
        self.m17_src_callsign = callsign
        if M17_AVAILABLE:
            self.m17_coder.set_src_id(callsign)

    def set_m17_dst_callsign(self, callsign: str):
        self.m17_dst_callsign = callsign or config.M17_DEFAULT_DST_CALLSIGN
        if M17_AVAILABLE:
            self.m17_coder.set_dst_id(self.m17_dst_callsign)

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
        """PTT press: unmute audio, then raise RF power. In M17 mode, also
        sends SOT (start of transmission) -- without it m17_coder silently
        discards all input and emits nothing (verified this session)."""
        self._m17_ending = False
        if self.mode == self.MODE_M17:
            self.m17_coder.post(_pmt.intern("transmission_control"), _pmt.intern("SOT"))
            self.m17_codec2_encoder.post(_pmt.intern("state_reset"), _pmt.intern("SOT"))
        self.ptt_mute.set_k(1.0)
        self.pluto_sink.set_attenuation(0, _gr_atten(self.target_atten_db))
        self._keyed = True

    def unkey_ptt(self):
        """PTT release. FM/SSB: kill RF power FIRST, then mute audio (order
        is safety-critical). M17 is different: muting audio and sending EOT
        happen immediately, but RF must stay up briefly afterward for the
        encoder's EOT tail (final frame + EOT frames, ~80ms minimum) to
        actually transmit -- cutting RF instantly would leave the receiver
        hanging with no clean end-of-stream. self._keyed stays True and
        self._m17_ending is set; the GUI drives finish_unkey_m17() after a
        bounded delay to actually lower power. This tail is NOT a safety
        gap: force_safe_state() (E-STOP, shutdown_safe()) forces attenuation
        down immediately regardless, via the independent PlutoSafety layer,
        at any point during the tail."""
        if self.mode == self.MODE_M17:
            self.ptt_mute.set_k(0.0)
            self.m17_coder.post(_pmt.intern("transmission_control"), _pmt.intern("EOT"))
            self._m17_ending = True
        else:
            self.pluto_sink.set_attenuation(0, _gr_atten(config.MIN_ATTEN))
            self.ptt_mute.set_k(0.0)
            self._keyed = False

    def finish_unkey_m17(self):
        """Called by the GUI a bounded delay after unkey_ptt() in M17 mode,
        once the EOT tail has had time to actually transmit. No-op if the
        operator already keyed up again in the meantime (key_ptt() clears
        _m17_ending, making a stale pending call here harmless)."""
        if not self._m17_ending:
            return
        self.pluto_sink.set_attenuation(0, _gr_atten(config.MIN_ATTEN))
        self._m17_ending = False
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
