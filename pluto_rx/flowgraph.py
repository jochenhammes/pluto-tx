"""GNU Radio flowgraph for the PlutoSDR RX app: FM and SSB(USB) demodulators
plus a live waterfall, all fed from the Pluto's own AD9361 RX branch.

Deliberately kept independent of pluto_tx: RX can't radiate, so none of
pluto_tx's attenuation/LO-powerdown safety machinery (safety.py) applies here.

Signal path: pluto_source (RX_BANDWIDTH, "zoom" span) --> IF decimation filter
(real-tap low-pass, complex in/out) down to a FIXED DEMOD_IF_RATE --> both
demodulator branches always connected (FM: quadrature_demod_cf; SSB: a
complex band-pass that selects only the upper-sideband region, then
complex_to_real -- the mirror image of pluto_tx's Hilbert-based USB
modulator) --> resample to AUDIO_RATE --> blocks.selector picks the active
mode --> NF (audio) gain --> audio.sink (system default output).

Keeping DEMOD_IF_RATE fixed regardless of the chosen RX bandwidth "zoom"
preset means only the IF filter's decimation ratio/taps depend on the
preset -- the whole demod+resampler chain downstream never changes. GNU
Radio's FIR/resampler blocks can't change their decimation ratio at runtime,
so a "zoom"/bandwidth change is a full flowgraph rebuild (see gui.py); this
is a deliberate, infrequent user action, not something the safety-critical
pluto_tx side ever needs to do.
"""
import math
import sys

from gnuradio import gr, blocks, filter, analog, audio, iio, qtgui
from gnuradio.filter import firdes
from gnuradio.fft import window

from . import config


class PlutoRxFlowgraph(gr.top_block):
    MODE_FM = 0
    MODE_SSB = 1

    def __init__(self, uri=config.DEFAULT_URI, frequency=config.DEFAULT_FREQUENCY,
                 sample_rate=config.DEFAULT_RX_BANDWIDTH, gain_mode=config.DEFAULT_GAIN_MODE,
                 manual_gain_db=config.DEFAULT_MANUAL_GAIN_DB, demod_mode=MODE_FM,
                 nf_gain=config.DEFAULT_NF_GAIN, enable_waterfall=True):
        super().__init__("PlutoRxFlowgraph")

        self.uri = uri
        self.sample_rate = sample_rate
        self.nominal_freq_hz = float(frequency)
        self.fine_offset_hz = 0.0

        self.pluto_source = iio.fmcomms2_source_fc32(uri, [True, True], 0x8000)
        self.pluto_source.set_frequency(int(self.nominal_freq_hz))
        self.pluto_source.set_samplerate(int(sample_rate))
        self.pluto_source.set_gain_mode(0, gain_mode)
        self.pluto_source.set_gain(0, manual_gain_db)
        self.pluto_source.set_quadrature(True)
        self.pluto_source.set_rfdc(True)
        self.pluto_source.set_bbdc(True)
        self.pluto_source.set_filter_params("Auto", "", 0, 0)

        # --- Live view of the tuned frequency, at whatever span the current
        # RX bandwidth preset gives -- constructed with the ACTUAL center
        # frequency (not 0) so the operator can visually fine-tune against
        # the waterfall, and retuned live via set_frequency_range() in
        # set_frequency()/set_fine_offset() below (no rebuild needed for
        # that). FFT size ("zoom resolution") is also live-adjustable via
        # set_fft_size(); the widget's own click-drag zoom works for free.
        self.waterfall = None
        if enable_waterfall:
            self.waterfall = qtgui.waterfall_sink_c(
                config.DEFAULT_FFT_SIZE, window.WIN_BLACKMAN_hARRIS,
                int(self.nominal_freq_hz), sample_rate, "RX Baseband", 1
            )
            self.connect(self.pluto_source, self.waterfall)

        # --- IF stage: decimate from the RX bandwidth preset down to the
        # fixed DEMOD_IF_RATE. Uses rational_resampler_ccf (interpolation=1,
        # i.e. pure decimation) with auto-designed taps -- same established
        # pattern as pluto_tx's resamplers -- rather than a manually
        # firdes.low_pass'd filter with a fixed ABSOLUTE Hz transition width:
        # designing an absolute-Hz transition at the FULL pre-decimation rate
        # made the filter thousands of taps long at the wider presets (e.g.
        # 8031 taps at 10 MSps for a 3 kHz transition), needlessly expensive
        # for no accuracy benefit. The auto-designed filter's cutoff/transition
        # scale with the decimation ratio instead, staying cheap at every
        # preset while still using the full available IF bandwidth as the
        # anti-alias cutoff.
        decim = max(1, round(sample_rate / config.DEMOD_IF_RATE))
        self.if_rate = sample_rate / decim
        self.if_filter = filter.rational_resampler_ccf(
            interpolation=1, decimation=decim, taps=[], fractional_bw=0.4,
        )
        self.connect(self.pluto_source, self.if_filter)

        # --- FM branch: quadrature demod, then an audio low-pass to clean
        # up demod noise above the voice band.
        fm_gain = self.if_rate / (2 * math.pi * config.FM_DEVIATION_HZ)
        self.fm_demod = analog.quadrature_demod_cf(fm_gain)
        fm_audio_taps = firdes.low_pass(1.0, self.if_rate, 3000, 500, window.WIN_HAMMING)
        self.fm_audio_filter = filter.fir_filter_fff(1, fm_audio_taps)
        self.connect(self.if_filter, self.fm_demod)
        self.connect(self.fm_demod, self.fm_audio_filter)

        # --- SSB (USB) branch: a complex band-pass that keeps only the
        # upper-sideband region (300-2700 Hz above the tuned frequency) --
        # this IS the demodulator, exactly mirroring pluto_tx's Hilbert-based
        # USB modulator in reverse. Taking the real part afterwards gives
        # the demodulated audio directly, no further mixing needed.
        f_lo, f_hi, trans = config.SSB_AUDIO_BAND_HZ
        ssb_taps = firdes.complex_band_pass(1.0, self.if_rate, f_lo, f_hi, trans, window.WIN_HAMMING)
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

        # Standard Linux default audio output (empty device string).
        self.audio_sink = audio.sink(config.AUDIO_RATE, "", True)
        self.connect(self.nf_gain, self.audio_sink)

    def _retune(self):
        actual = self.nominal_freq_hz + self.fine_offset_hz
        self.pluto_source.set_frequency(int(actual))
        if self.waterfall is not None:
            self.waterfall.set_frequency_range(actual, self.sample_rate)

    def set_frequency(self, freq_hz: float):
        self.nominal_freq_hz = freq_hz
        self._retune()

    def set_fine_offset(self, offset_hz: float):
        self.fine_offset_hz = offset_hz
        self._retune()

    def set_gain_mode(self, mode: str):
        self.pluto_source.set_gain_mode(0, mode)

    def set_manual_gain(self, gain_db: float):
        self.pluto_source.set_gain(0, gain_db)

    def set_demod_mode(self, mode: int):
        self.demod_selector.set_input_index(mode)

    def set_nf_gain(self, gain: float):
        self.nf_gain.set_k(gain)

    def set_fft_size(self, n: int):
        if self.waterfall is not None:
            self.waterfall.set_fft_size(n)

    def shutdown(self):
        """Stop the flowgraph. Safe to call more than once."""
        try:
            self.stop()
            self.wait()
        except Exception as e:
            print(f"WARNING: flowgraph stop() failed: {e}", file=sys.stderr)
