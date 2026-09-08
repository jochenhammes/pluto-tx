"""GNU Radio flowgraph for the PlutoSDR advanced RX app: FM and SSB(USB)
demodulators (a straight copy of pluto_rx/flowgraph.py's demod chain) plus a
FftProbe tap feeding the interactive pyqtgraph waterfall widget, instead of
pluto_rx's opaque qtgui.waterfall_sink_c.

Deliberately a SELF-CONTAINED COPY of pluto_rx's flowgraph, not an import of
PlutoRxFlowgraph -- see pluto_advanced_rx/config.py's module docstring for
why. Independent of pluto_tx: RX can't radiate, so none of pluto_tx's
attenuation/LO-powerdown safety machinery applies here.

Signal path: device source (RX_BANDWIDTH, "zoom" span) --> IF decimation
filter (real-tap low-pass, complex in/out) down to a FIXED DEMOD_IF_RATE -->
both demodulator branches always connected (FM: quadrature_demod_cf; SSB: a
complex band-pass that selects only the upper-sideband region, then
complex_to_real) --> resample to AUDIO_RATE --> blocks.selector picks the
active mode --> NF (audio) gain --> rx_mute (starts muted, see
set_rx_muted()) --> audio.sink. In parallel, FftProbe taps the device source
directly (full RX_BANDWIDTH span, before IF decimation) and exposes FFT rows
for the waterfall widget to poll.

Backend-agnostic via devices/ (RxDevice/GainStage) -- see devices/base.py.
Only "pluto" is registered so far; device_type defaults to "pluto" so this
constructor's signature and behavior are unchanged for existing callers.
"""
import math
import sys

from gnuradio import gr, blocks, filter, analog, audio
from gnuradio.filter import firdes
from gnuradio.fft import window

from . import config
from . import devices
from .fft_probe import FftProbe


class AdvancedRxFlowgraph(gr.top_block):
    MODE_FM = 0
    MODE_SSB = 1

    def __init__(self, uri=None, frequency=config.DEFAULT_FREQUENCY,
                 sample_rate=config.DEFAULT_RX_BANDWIDTH, gain_mode=config.DEFAULT_GAIN_MODE,
                 manual_gain_db=config.DEFAULT_MANUAL_GAIN_DB, demod_mode=MODE_FM,
                 nf_gain=config.DEFAULT_NF_GAIN, fft_size=config.DEFAULT_FFT_SIZE,
                 fm_demod_width_hz=config.FM_DEMOD_WIDTH_DEFAULT_HZ,
                 ssb_demod_width_hz=config.SSB_DEMOD_WIDTH_DEFAULT_HZ, device_type="pluto",
                 gain_values=None):
        """uri doubles as the generic "connection" string for every backend
        (a libiio URI for Pluto, a serial/Soapy-args string for HackRF) --
        default is None, NOT config.DEFAULT_URI: that Pluto-specific default
        must not leak into a non-Pluto device_type (a real bug caught this
        session -- an omitted uri for device_type="hackrf" was silently
        passed to Soapy as a bogus serial). None lets build_device() apply
        each device class's own DEFAULT_CONNECTION instead, exactly mirroring
        pluto_tx/flowgraph.py's connection=None pattern. Existing Pluto
        callers (gui.py) always pass uri explicitly, so this is unchanged
        behavior for them.

        gain_mode/manual_gain_db apply only to an AGC-capable device's one
        AGC-controlled stage (Pluto's "gain") -- unchanged from before the
        device abstraction existed, so Pluto callers don't need to change.
        gain_values is a {stage_name: value} dict for backends with no AGC
        stage at all (HackRF: LNA/AMP/VGA, all independently manual) --
        unset stages keep build_source()'s own defaults. The two are
        independent: a device could in principle have both an AGC stage and
        further manual-only stages, though none implemented so far do."""
        super().__init__("AdvancedRxFlowgraph")

        self.sample_rate = sample_rate
        self.nominal_freq_hz = float(frequency)
        self.fine_offset_hz = 0.0

        self.device = devices.build_device(
            device_type, connection=uri, frequency_hz=self.nominal_freq_hz,
            sample_rate_hz=sample_rate, bandwidth_hz=None,
        )
        self.uri = self.device.connection  # the actual (possibly defaulted) connection string
        # Constructed first, gain configured right after -- mirrors
        # TxDevice.build_sink()'s "construct, then configure power" split.
        self.pluto_source = self.device.build_source()
        self.device.set_gain_mode(gain_mode)  # no-op unless device.supports_agc_mode
        if self.device.supports_agc_mode:
            agc_stage = next(s for s in self.device.gain_stages if s.controls_agc)
            self.device.set_gain(agc_stage.name, manual_gain_db)
        for stage_name, value in (gain_values or {}).items():
            self.device.set_gain(stage_name, value)

        # --- FFT probe for the interactive waterfall widget: taps the full
        # RX_BANDWIDTH span directly off pluto_source, same point pluto_rx's
        # qtgui.waterfall_sink_c attaches at. Always on -- cheap enough
        # (throttled compute rate, see fft_probe.py) that there's no need
        # for pluto_rx's enable_waterfall toggle.
        self.fft_probe = FftProbe(fft_size, sample_rate, config.WATERFALL_WINDOW, config.FFT_COMPUTE_RATE_HZ)
        self.connect(self.pluto_source, self.fft_probe)

        # --- IF stage: decimate from the RX bandwidth preset down to the
        # fixed DEMOD_IF_RATE. rational_resampler_ccf (interpolation=1, i.e.
        # pure decimation) with auto-designed taps -- see pluto_rx's
        # identical comment for why this beats a firdes.low_pass'd filter
        # with a fixed absolute-Hz transition width (thousands of taps at
        # wider presets for no accuracy benefit).
        decim = max(1, round(sample_rate / config.DEMOD_IF_RATE))
        self.if_rate = sample_rate / decim
        self.if_filter = filter.rational_resampler_ccf(
            interpolation=1, decimation=decim, taps=[], fractional_bw=0.4,
        )
        self.connect(self.pluto_source, self.if_filter)

        # --- FM branch: quadrature demod, then an audio low-pass to clean
        # up demod noise above the voice band. fm_channel_filter is a
        # REAL low-pass filter applied to the COMPLEX IF signal -- a
        # standard technique for band-limiting a complex signal symmetric
        # around 0 Hz -- so it acts as the actual, operator-adjustable
        # RF/IF channel width (config.FM_DEMOD_WIDTH_DEFAULT_HZ, default a
        # standard 12.5 kHz NBFM channel), distinct from fm_audio_filter's
        # fixed post-demod audio cleanup cutoff.
        self.fm_demod_width_hz = fm_demod_width_hz
        fm_channel_taps = firdes.low_pass(1.0, self.if_rate, fm_demod_width_hz / 2,
                                           config.FM_CHANNEL_TRANS_HZ, window.WIN_HAMMING)
        self.fm_channel_filter = filter.fir_filter_ccc(1, fm_channel_taps)
        fm_gain = self.if_rate / (2 * math.pi * config.FM_DEVIATION_HZ)
        self.fm_demod = analog.quadrature_demod_cf(fm_gain)
        fm_audio_taps = firdes.low_pass(1.0, self.if_rate, config.FM_AUDIO_CUTOFF_HZ,
                                         config.FM_AUDIO_TRANS_HZ, window.WIN_HAMMING)
        self.fm_audio_filter = filter.fir_filter_fff(1, fm_audio_taps)
        self.connect(self.if_filter, self.fm_channel_filter)
        self.connect(self.fm_channel_filter, self.fm_demod)
        self.connect(self.fm_demod, self.fm_audio_filter)

        # --- SSB (USB) branch: a complex band-pass that keeps only the
        # upper-sideband region above the tuned frequency -- this IS the
        # demodulator, exactly mirroring pluto_tx's Hilbert-based USB
        # modulator in reverse. Taking the real part afterwards gives the
        # demodulated audio directly, no further mixing needed. The band's
        # low edge (config.SSB_AUDIO_BAND_HZ[0], standard practice to skip
        # low-frequency mains hum) stays fixed; its width -- and therefore
        # the high edge -- is operator-adjustable (set_ssb_demod_width()).
        self.ssb_demod_width_hz = ssb_demod_width_hz
        f_lo = config.SSB_AUDIO_BAND_HZ[0]
        ssb_taps = firdes.complex_band_pass(1.0, self.if_rate, f_lo, f_lo + ssb_demod_width_hz,
                                             config.SSB_AUDIO_BAND_HZ[2], window.WIN_HAMMING)
        self.ssb_filter = filter.fir_filter_ccc(1, ssb_taps)
        self.ssb_to_real = blocks.complex_to_real()
        self.connect(self.if_filter, self.ssb_filter)
        self.connect(self.ssb_filter, self.ssb_to_real)

        # --- Resample both demodulated-audio branches (at the fixed
        # if_rate) up/down to AUDIO_RATE, then pick the active mode.
        g = math.gcd(int(self.if_rate), config.AUDIO_RATE)
        self.fm_resampler = filter.rational_resampler_fff(
            interpolation=config.AUDIO_RATE // g, decimation=int(self.if_rate) // g,
            taps=[], fractional_bw=0.4,
        )
        self.ssb_resampler = filter.rational_resampler_fff(
            interpolation=config.AUDIO_RATE // g, decimation=int(self.if_rate) // g,
            taps=[], fractional_bw=0.4,
        )
        self.connect(self.fm_audio_filter, self.fm_resampler)
        self.connect(self.ssb_to_real, self.ssb_resampler)

        # NOTE: blocks.selector's ninputs is only known once the flowgraph is
        # actually running -- the initial index must go through the
        # constructor (see pluto_tx/flowgraph.py for the full explanation of
        # this gotcha). set_demod_mode() below is for RUNTIME switching only.
        self.demod_selector = blocks.selector(gr.sizeof_float, demod_mode, 0)
        self.demod_selector.set_enabled(True)
        self.connect(self.fm_resampler, (self.demod_selector, self.MODE_FM))
        self.connect(self.ssb_resampler, (self.demod_selector, self.MODE_SSB))

        self.nf_gain = blocks.multiply_const_ff(nf_gain)
        self.connect(self.demod_selector, self.nf_gain)

        # Mute gate, last block before the physical output -- starts muted
        # (0.0) regardless of what rx_muted the caller eventually asks for,
        # exactly mirroring pluto_tx's tx_gain lesson (2026-09-07 session):
        # a block right before hardware/output should default to silent and
        # only be explicitly unmuted, not default to pass-through and rely
        # on something further upstream to gate it. Deliberately AFTER
        # nf_gain, not before -- a pure on/off gate, independent of the
        # continuous "Audio Gain" volume control, so muting/unmuting never
        # depends on (or interferes with) whatever volume is currently set.
        self.rx_mute = blocks.multiply_const_ff(0.0)
        self.connect(self.nf_gain, self.rx_mute)

        # Standard Linux default audio output (empty device string).
        self.audio_sink = audio.sink(config.AUDIO_RATE, "", True)
        self.connect(self.rx_mute, self.audio_sink)

    def _retune(self):
        actual = self.nominal_freq_hz + self.fine_offset_hz
        self.device.set_frequency(actual)

    def set_frequency(self, freq_hz: float):
        self.nominal_freq_hz = freq_hz
        self._retune()

    def set_fine_offset(self, offset_hz: float):
        self.fine_offset_hz = offset_hz
        self._retune()

    def set_gain_mode(self, mode: str):
        self.device.set_gain_mode(mode)

    def set_manual_gain(self, gain_db: float):
        self.device.set_gain("gain", gain_db)

    def set_demod_mode(self, mode: int):
        self.demod_selector.set_input_index(mode)

    def set_nf_gain(self, gain: float):
        self.nf_gain.set_k(gain)

    def set_rx_muted(self, muted: bool):
        self.rx_mute.set_k(0.0 if muted else 1.0)

    def set_fft_size(self, n: int):
        self.fft_probe.set_fft_size(n)

    def set_fm_demod_width(self, width_hz: float):
        """Retapes fm_channel_filter in place (fir_filter_ccc.set_taps() is
        safe at runtime, no flowgraph rebuild needed -- same technique
        pluto_tx's set_mode() already relies on for its NF filter)."""
        self.fm_demod_width_hz = width_hz
        taps = firdes.low_pass(1.0, self.if_rate, width_hz / 2, config.FM_CHANNEL_TRANS_HZ, window.WIN_HAMMING)
        self.fm_channel_filter.set_taps(taps)

    def set_ssb_demod_width(self, width_hz: float):
        self.ssb_demod_width_hz = width_hz
        f_lo = config.SSB_AUDIO_BAND_HZ[0]
        taps = firdes.complex_band_pass(1.0, self.if_rate, f_lo, f_lo + width_hz,
                                         config.SSB_AUDIO_BAND_HZ[2], window.WIN_HAMMING)
        self.ssb_filter.set_taps(taps)

    def shutdown(self):
        """Stop the flowgraph. Safe to call more than once."""
        try:
            self.stop()
            self.wait()
        except Exception as e:
            print(f"WARNING: flowgraph stop() failed: {e}", file=sys.stderr)
