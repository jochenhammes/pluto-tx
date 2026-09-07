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

from gnuradio import gr, blocks, filter, analog, audio, qtgui
from gnuradio.filter import firdes
from gnuradio.fft import window

from . import config
from . import devices
from . import dynamics

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

    def __init__(self, device_type="pluto", connection=None, frequency=config.DEFAULT_FREQUENCY,
                 power_ceiling=None, audio_device="",
                 wav_path=None, mode=MODE_FM, source=SRC_MIC, enable_waterfall=False,
                 m17_src_callsign="", m17_dst_callsign=config.M17_DEFAULT_DST_CALLSIGN,
                 freedv_variant=config.FREEDV_DEFAULT_MODE, freedv_callsign=""):
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
        self._secondary_power = {}  # non-primary power stages (e.g. HackRF's AMP), see set_secondary_power()

        if mode == self.MODE_M17 and not M17_AVAILABLE:
            # mode_selector below would otherwise be constructed pointing at
            # an input that's never connected (the M17 branch only gets
            # wired if M17_AVAILABLE) -- fall back rather than build a
            # broken flowgraph.
            mode = self.MODE_FM
        if mode == self.MODE_FREEDV and not FREEDV_AVAILABLE:
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
        self.ssb_resampler = filter.rational_resampler_ccf(
            interpolation=quad_rate // g, decimation=config.AUDIO_RATE // g,
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
                "TX Basisband (vor Geraete-Sink)", 1
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

        # Exactly one of {mode_selector, m17_tx_resampler, freedv_ssb_resampler}
        # feeds tx_gain at a time -- the rest drain into their null_sinks.
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
        producers = {self.MODE_FM: self.mode_selector, self.MODE_SSB: self.mode_selector}
        if M17_AVAILABLE:
            producers[self.MODE_M17] = self.m17_tx_resampler
        if FREEDV_AVAILABLE:
            producers[self.MODE_FREEDV] = self.freedv_ssb_resampler
        return producers

    def _null_sink_for(self, producer):
        if producer is self.mode_selector:
            return self._null_sink_selector
        if M17_AVAILABLE and producer is self.m17_tx_resampler:
            return self._null_sink_m17
        if FREEDV_AVAILABLE and producer is self.freedv_ssb_resampler:
            return self._null_sink_freedv
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

        if mode in (self.MODE_M17, self.MODE_FREEDV):
            return  # both bypass the NF filter/dynamics chain entirely, nothing to retap

        self.mode_selector.set_input_index(1 if mode == self.MODE_SSB else 0)
        preset = "SSB" if mode == self.MODE_SSB else "FM"
        f_lo, f_hi, trans = config.NF_FILTER_PRESETS[preset]
        taps = firdes.band_pass(1.0, config.AUDIO_RATE, f_lo, f_hi, trans, window.WIN_HAMMING)
        self.nf_filter.set_taps(taps)

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
        actually does."""
        self._m17_ending = False
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
        self._keyed = True

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
        layer, at any point during the tail."""
        if self.mode == self.MODE_M17:
            self.ptt_mute.set_k(0.0)
            self.m17_coder.post(_pmt.intern("transmission_control"), _pmt.intern("EOT"))
            self._m17_ending = True
        else:
            self.tx_gain.set_k(0.0 + 0j)
            stage = self.device.primary_stage
            self.device.set_power(stage.name, stage.off_value)
            self.ptt_mute.set_k(0.0)
            self.device.post_unkey()
            if not self.device.supports_persistent_sink:
                self._rebuild_device_sink()
            self._keyed = False

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
