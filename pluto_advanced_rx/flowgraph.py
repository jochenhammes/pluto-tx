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

from gnuradio import gr, blocks, filter, analog, digital

from pluto_tx import audio_devices
from gnuradio.filter import firdes
from gnuradio.fft import window

from . import config
from . import devices
from . import rade_autotune
from .fft_probe import FftProbe
from .filebroadcast_deframer import FileBroadcastDeframer
from .psk31_deframer import PSK31VaricodeDeframer

# RADE V1 is optional, same reasoning as pluto_tx: from-source build (see
# install-rade.sh), not something every user has.
from . import rade_ctypes as _rade_ctypes
from .rade import RadeDecoder
RADE_AVAILABLE = _rade_ctypes.RADE_AVAILABLE


class AdvancedRxFlowgraph(gr.top_block):
    MODE_FM = 0
    MODE_SSB = 1
    MODE_RADE = 2

    def __init__(self, uri=None, frequency=config.DEFAULT_FREQUENCY,
                 sample_rate=None, gain_mode=config.DEFAULT_GAIN_MODE,
                 manual_gain_db=config.DEFAULT_MANUAL_GAIN_DB, demod_mode=MODE_FM,
                 nf_gain=config.DEFAULT_NF_GAIN, fft_size=config.DEFAULT_FFT_SIZE,
                 fm_demod_width_hz=config.FM_DEMOD_WIDTH_DEFAULT_HZ,
                 ssb_demod_width_hz=config.SSB_DEMOD_WIDTH_DEFAULT_HZ, device_type="pluto",
                 gain_values=None, on_filebroadcast_frame=None, on_psk31_char=None,
                 psk31_tone_hz=config.PSK31_DEFAULT_TONE_HZ, audio_device=""):
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

        sample_rate has the EXACT same "don't let one device's default leak
        into another's" issue and fix -- default is None, not
        config.DEFAULT_RX_BANDWIDTH (a real bug caught adding AudioDevice
        this session: omitting sample_rate for device_type="audio" silently
        tried to open a sound card at Pluto's 2.5Msps default, which ALSA
        naturally can't honor). build_device() already applies each device
        class's own default_sample_rate_hz when None is passed -- this just
        stops shadowing that fallback with a Pluto-specific literal.

        gain_mode/manual_gain_db apply only to an AGC-capable device's one
        AGC-controlled stage (Pluto's "gain") -- unchanged from before the
        device abstraction existed, so Pluto callers don't need to change.
        gain_values is a {stage_name: value} dict for backends with no AGC
        stage at all (HackRF: LNA/AMP/VGA, all independently manual) --
        unset stages keep build_source()'s own defaults. The two are
        independent: a device could in principle have both an AGC stage and
        further manual-only stages, though none implemented so far do."""
        super().__init__("AdvancedRxFlowgraph")

        if demod_mode == self.MODE_RADE and not RADE_AVAILABLE:
            # demod_selector below would otherwise be constructed pointing
            # at a mode it can't actually decode (RADE bypasses it entirely
            # -- see below) -- fall back rather than build a broken
            # flowgraph, same pattern as pluto_tx's MODE_RADE fallback.
            demod_mode = self.MODE_FM
        self.demod_mode = demod_mode  # tracked so set_demod_mode() knows the PREVIOUS producer to swap away from

        self.nominal_freq_hz = float(frequency)
        self.fine_offset_hz = 0.0

        self.device = devices.build_device(
            device_type, connection=uri, frequency_hz=self.nominal_freq_hz,
            sample_rate_hz=sample_rate, bandwidth_hz=None,
        )
        self.uri = self.device.connection  # the actual (possibly defaulted) connection string
        self.sample_rate = self.device.sample_rate_hz  # the actual (possibly defaulted) sample rate
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
        self.fft_probe = FftProbe(fft_size, self.sample_rate, config.WATERFALL_WINDOW, config.FFT_COMPUTE_RATE_HZ)
        self.connect(self.pluto_source, self.fft_probe)

        # --- File Broadcast branch (repetitive file-broadcast mode, 23cm
        # broadband GFSK -- see filebroadcast_deframer.py and the plan).
        # ALWAYS wired for RF-capable devices, unconditionally, independent
        # of demod_selector/nf_gain/audio_sink -- same "always-on parallel
        # branch" pattern as fft_probe just above, not the demod_selector-
        # registered pattern FM/SSB/RADE use below (this mode has no audio
        # output to select).
        #
        # Skipped entirely for audio-only devices (AudioDevice/Soundcard,
        # see RxDevice.is_audio_only()) -- a real bug found on real use:
        # this branch's resampler design assumes real RF bandwidth (its
        # cutoff is derived from FILEBROADCAST_WORKING_RATE_HZ=500kHz), but
        # AudioDevice's sample rate is only 20kHz (no RF concept at all,
        # just a sound card feeding RADE-over-soundcard) -- firdes.low_pass
        # raised IndexError (cutoff > sample_rate/2) at construction time,
        # making Soundcard mode completely unable to connect. File
        # Broadcast fundamentally needs a wideband RF front end; there is
        # no real signal for it to decode from a sound card anyway.
        #
        # Taps pluto_source DIRECTLY (the full, operator-selected
        # RX_BANDWIDTH span), NOT if_filter's narrowed-down
        # DEMOD_IF_RATE=50kHz output the way RADE does below -- a real
        # design catch made while wiring this up: File Broadcast's
        # Phase-0-verified 100kbaud/h=1 signal occupies roughly 235kHz
        # (2*deviation + (1+BT)*symbol_rate), far wider than
        # DEMOD_IF_RATE's 50kHz Nyquist bandwidth could ever pass without
        # severe aliasing/filtering loss -- if_filter's own anti-alias
        # low-pass (designed for narrowband FM/SSB/RADE) would butcher this
        # signal before the deframer ever saw it. fft_probe's tap point
        # (the full-bandwidth pluto_source output) is the only existing
        # branch point wide enough for this mode.
        self.filebroadcast_deframer = None  # stays None for audio-only devices -- see gui.py's own None-check
        if not self.device.is_audio_only():
            g_fb = math.gcd(int(self.sample_rate), int(config.FILEBROADCAST_WORKING_RATE_HZ))
            fb_interp = int(config.FILEBROADCAST_WORKING_RATE_HZ) // g_fb
            fb_decim = int(self.sample_rate) // g_fb
            # EXPLICIT taps, never taps=[] (auto-design) -- Phase 0's confirmed
            # finding: rational_resampler_ccf's auto-designed taps corrupt
            # phase-continuous GFSK badly enough to break the receiver's own
            # symbol clock recovery, even in a lossless software round-trip.
            # Every RX_BANDWIDTH_PRESETS entry is a clean multiple of
            # FILEBROADCAST_WORKING_RATE_HZ by construction (confirmed:
            # {1M,2.5M,5M,8M,10M} all divide evenly by 500kHz), so fb_interp is
            # 1 (pure decimation) for every currently offered preset -- gain=
            # fb_interp (not hardcoded 1.0) stays correct even if that ever
            # changes: an interpolating (fb_interp>1) stage needs its taps
            # scaled by the interpolation factor, the same reasoning already
            # documented for pluto_tx's M17 RRC filter and this mode's own
            # mirror-image TX resampler.
            fb_taps = firdes.low_pass(
                float(fb_interp), self.sample_rate, config.FILEBROADCAST_WORKING_RATE_HZ / 2 * 0.9,
                config.FILEBROADCAST_WORKING_RATE_HZ * 0.3, window.WIN_HAMMING,
            )
            self.filebroadcast_rx_resampler = filter.rational_resampler_ccf(
                interpolation=fb_interp, decimation=fb_decim, taps=fb_taps,
            )
            self.connect(self.pluto_source, self.filebroadcast_rx_resampler)
            fb_sensitivity = 2 * math.pi * config.FILEBROADCAST_DEVIATION_HZ / config.FILEBROADCAST_WORKING_RATE_HZ
            # gain_mu explicitly set (NOT gfsk_demod's own default 0.175) --
            # Phase 0's confirmed real-hardware finding: the default loop
            # bandwidth reproducibly cycle-slips mid-transmission at 100kbaud;
            # 0.005 gave BER well under 0.2%, twice, reproducibly, at these
            # exact PHY parameters.
            self.filebroadcast_demod = digital.gfsk_demod(
                samples_per_symbol=config.FILEBROADCAST_SPS, sensitivity=fb_sensitivity,
                gain_mu=config.FILEBROADCAST_GAIN_MU,
            )
            self.connect(self.filebroadcast_rx_resampler, self.filebroadcast_demod)
            self.filebroadcast_deframer = FileBroadcastDeframer(
                on_filebroadcast_frame or (lambda frame: None), config.FILEBROADCAST_CHUNK_SIZE,
            )
            self.connect(self.filebroadcast_demod, self.filebroadcast_deframer)

        # --- IF stage: decimate from the RX bandwidth preset down to the
        # fixed DEMOD_IF_RATE. rational_resampler_ccf (interpolation=1, i.e.
        # pure decimation) with auto-designed taps -- see pluto_rx's
        # identical comment for why this beats a firdes.low_pass'd filter
        # with a fixed absolute-Hz transition width (thousands of taps at
        # wider presets for no accuracy benefit).
        decim = max(1, round(self.sample_rate / config.DEMOD_IF_RATE))
        self.if_rate = self.sample_rate / decim
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
        # demod_selector only ever carries FM/SSB (2 inputs) -- RADE is
        # deliberately NOT a third selector input, same scheduler-risk
        # reasoning as pluto_tx's M17/FreeDV/RADE producers (a frame-
        # quantized block -- RadeDecoder's variable nin()/irregular output
        # cadence is an even more extreme profile than those TX-side
        # blocks). Its initial index is irrelevant when demod_mode==MODE_RADE
        # (demod_selector's output drains into a null_sink in that case, see
        # _audio_producer_map()/set_demod_mode() below) -- clamp to a valid
        # FM/SSB index either way, never MODE_RADE.
        self.demod_selector = blocks.selector(gr.sizeof_float, 1 if demod_mode == self.MODE_SSB else 0, 0)
        self.demod_selector.set_enabled(True)
        self.connect(self.fm_resampler, (self.demod_selector, self.MODE_FM))
        self.connect(self.ssb_resampler, (self.demod_selector, self.MODE_SSB))

        # --- RADE V1 branch (optional, only if librade.so + lpcnet_demo are
        # both available -- see RADE_AVAILABLE above). Taps if_filter's
        # output (already decimated to the fixed DEMOD_IF_RATE, e.g.
        # 50kHz), NOT pluto_source directly -- a real bug caught this
        # session: decimating straight from the RX bandwidth preset
        # (2.5Msps default) down to 8kHz in one rational_resampler_ccf stage
        # demands a huge single-call input chunk (>10000 items) that
        # exceeds GNU Radio's default max buffer size (8191), crashing the
        # scheduler ("requesting more input data than we can provide") --
        # confirmed reproducible at the app's own DEFAULT sample rate, not
        # just an extreme preset. Tapping if_filter instead keeps the
        # decimation ratio small (e.g. 25:4 at the default preset instead
        # of 625:2) with no functional difference for RADE's actual signal
        # content: if_filter's anti-alias low-pass is transparent to a
        # signal RADE's own few-kHz-wide OFDM waveform sits well inside of,
        # the same reasoning FM/SSB already rely on sharing that same
        # if_filter output. Runs THROUGH nf_gain/rx_mute afterward, same as
        # FM/SSB -- pure output volume/mute, none of TX's pre-modulator
        # dynamics-risk reasoning applies on the RX side.
        if RADE_AVAILABLE:
            g_rade = math.gcd(int(self.if_rate), _rade_ctypes.RADE_MODEM_SAMPLE_RATE)
            self.rade_rx_resampler = filter.rational_resampler_ccf(
                interpolation=_rade_ctypes.RADE_MODEM_SAMPLE_RATE // g_rade,
                decimation=int(self.if_rate) // g_rade,
                taps=[], fractional_bw=0.4,
            )
            self.connect(self.if_filter, self.rade_rx_resampler)
            self.rade_decoder = RadeDecoder()
            self.connect(self.rade_rx_resampler, self.rade_decoder)
            self.rade_short_to_float = blocks.short_to_float(1, 32767.0)
            self.connect(self.rade_decoder, self.rade_short_to_float)
            g_rade_up = math.gcd(_rade_ctypes.RADE_SPEECH_SAMPLE_RATE, config.AUDIO_RATE)
            self.rade_audio_resampler_up = filter.rational_resampler_fff(
                interpolation=config.AUDIO_RATE // g_rade_up,
                decimation=_rade_ctypes.RADE_SPEECH_SAMPLE_RATE // g_rade_up,
                taps=[], fractional_bw=0.4,
            )
            self.connect(self.rade_short_to_float, self.rade_audio_resampler_up)

        # --- PSK31 (BPSK31 keyboard-to-keyboard chat digimode) -- always-on
        # parallel branch, no audio output to select (same "always-on
        # parallel branch" pattern as fft_probe/File Broadcast above, not
        # the demod_selector-registered pattern FM/SSB/RADE use). Taps
        # if_filter's output (NOT pluto_source directly, unlike File
        # Broadcast) -- PSK31's ~50-60Hz occupied bandwidth is tiny, the
        # same if_filter tap point RADE already uses above is more than
        # enough, avoiding the documented real scheduler-crash risk of
        # decimating straight off a multi-Msps pluto_source in one stage.
        # Built UNCONDITIONALLY, no is_audio_only() gate (unlike File
        # Broadcast) -- if_filter's own decimation-ratio math degrades
        # gracefully for AudioDevice's ~20kHz rate (confirmed via a
        # headless construction test with device_type="audio" before this
        # was considered done), the same reason FM/SSB/RADE already work
        # uniformly across real SDR and Soundcard backends with zero
        # special-casing.
        #
        # AFC (frequency drift compensation): real over-the-air testing
        # during this mode's own Phase 0 PHY work found a substantial,
        # CONTINUOUSLY DRIFTING TX/RX frequency offset (see pluto_tx/
        # config.py's PSK31_AUTO_UNKEY_WATCHDOG_S comment for the full
        # progress-log finding) that a fixed/hardcoded correction cannot
        # track. Design (verified in an offline software round-trip test
        # before being wired in here -- see config.py's own PSK31_AFC_*
        # comment for the full rationale): a SECOND, dedicated FftProbe
        # instance (psk31_afc_probe below, independent of the main
        # waterfall's fft_probe -- own fft_size/compute_rate, no shared
        # zoom state) also taps if_filter's output; psk31_afc_step()
        # (called periodically by gui.py's _poll_psk31()) uses
        # rade_autotune.estimate_signal_center() -- REUSED, not
        # reimplemented, per the plan's own explicit recommendation -- to
        # find the tone's actual current center and retune
        # psk31_tone_filter to match. This was deliberately chosen over
        # reading costas_loop_cc.get_frequency() directly (tried first,
        # offline-tested, and rejected): the Costas loop can only report a
        # residual for a signal that's already within psk31_tone_filter's
        # own narrow (+-100Hz) passband, so it cannot recover once real
        # drift pushes the tone entirely outside that passband -- a
        # dedicated wideband FFT search can.
        self.psk31_tone_hz = float(psk31_tone_hz)
        # AFC's current best estimate of the tone's true center -- starts
        # at the operator's nominal, then drifts away from it over a
        # session as psk31_afc_step() finds real corrections. Deliberately
        # separate from psk31_tone_hz itself: see set_psk31_tone_hz()'s
        # docstring for why a manual retune resets this back to nominal.
        self.psk31_tone_center_hz = self.psk31_tone_hz
        psk31_decim = max(1, round(self.if_rate / config.PSK31_WORKING_RATE_HZ))
        self.psk31_working_rate = self.if_rate / psk31_decim
        psk31_xlate_taps = firdes.low_pass(
            1.0, self.if_rate, config.PSK31_XLATE_CUTOFF_HZ, config.PSK31_XLATE_TRANS_HZ, window.WIN_HAMMING,
        )
        self.psk31_tone_filter = filter.freq_xlating_fir_filter_ccf(
            psk31_decim, psk31_xlate_taps, self.psk31_tone_center_hz, self.if_rate,
        )
        self.connect(self.if_filter, self.psk31_tone_filter)
        self.psk31_costas_loop = digital.costas_loop_cc(config.PSK31_LOOP_BW, 2, False)
        self.connect(self.psk31_tone_filter, self.psk31_costas_loop)
        self.psk31_complex_to_real = blocks.complex_to_real()
        self.connect(self.psk31_costas_loop, self.psk31_complex_to_real)
        psk31_sps = self.psk31_working_rate / config.PSK31_SYMBOL_RATE_HZ
        self.psk31_symbol_sync = digital.symbol_sync_ff(
            digital.TED_MUELLER_AND_MULLER, psk31_sps, config.PSK31_LOOP_BW, 1.0, 1.0, 0.05, 1,
            digital.constellation_bpsk().base(), digital.IR_MMSE_8TAP, 128, [],
        )
        self.connect(self.psk31_complex_to_real, self.psk31_symbol_sync)
        self.psk31_slicer = digital.binary_slicer_fb()
        self.connect(self.psk31_symbol_sync, self.psk31_slicer)
        self.psk31_diff_decoder = digital.diff_decoder_bb(2)
        self.connect(self.psk31_slicer, self.psk31_diff_decoder)
        # Inverter: digital.diff_decoder_bb's standard convention
        # (decoded[n] = encoded[n] XOR encoded[n-1], 1=transition) is the
        # OPPOSITE of PSK31's own (0=phase reversal, 1=no change) --
        # determined empirically during Phase 0 PHY testing, see pluto_tx/
        # psk31.py's own docstring.
        self.psk31_inverter = blocks.not_bb()
        self.connect(self.psk31_diff_decoder, self.psk31_inverter)
        self.psk31_deframer = PSK31VaricodeDeframer(on_psk31_char or (lambda ch: None))
        self.connect(self.psk31_inverter, self.psk31_deframer)

        # Dedicated AFC search probe -- see the AFC comment above.
        self.psk31_afc_probe = FftProbe(
            config.PSK31_AFC_FFT_SIZE, self.if_rate, config.WATERFALL_WINDOW, config.PSK31_AFC_COMPUTE_RATE_HZ,
        )
        self.connect(self.if_filter, self.psk31_afc_probe)
        self._psk31_afc_gen = -1

        self.nf_gain = blocks.multiply_const_ff(nf_gain)
        # Exactly one of {demod_selector, rade_audio_resampler_up} feeds
        # nf_gain at a time -- the other drains into a null_sink. See
        # _audio_producer_map()/set_demod_mode() for the runtime swap logic
        # (RX-side mirror of PlutoTxFlowgraph.set_mode()'s tx_gain producer
        # swap, same lock()/connect()/disconnect() pattern).
        self._null_sink_selector = blocks.null_sink(gr.sizeof_float)
        if RADE_AVAILABLE:
            self._null_sink_rade = blocks.null_sink(gr.sizeof_float)
        producers = self._audio_producer_map()
        active_producer = producers[self.demod_mode]
        self.connect(active_producer, self.nf_gain)
        for producer in set(producers.values()):
            if producer is not active_producer:
                self.connect(producer, self._null_sink_for_audio(producer))

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

        # audio_device selects a real ALSA device, a PipeWire sink
        # monitor, or the persistent qpwgraph loopback node -- "" (system
        # default) unless the GUI's Audio Output combo picked something
        # else, see gui.py's audio_device_combo/_on_audio_device_changed().
        self.audio_sink = audio_devices.open_output_device(config.AUDIO_RATE, audio_device)
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
        """Applies to the device's AGC-controlled stage (Pluto: "gain",
        RTL-SDR: "TUNER") -- looked up by controls_agc, not hardcoded, so
        this works unchanged for every AGC-capable backend. A backend with
        no AGC stage at all (HackRF) has no single "the" gain for this to
        mean anything -- its stages are set individually instead, see
        __init__'s gain_values."""
        agc_stage = next(s for s in self.device.gain_stages if s.controls_agc)
        self.device.set_gain(agc_stage.name, gain_db)

    def _audio_producer_map(self):
        """mode -> the block that should feed nf_gain in that mode. FM/SSB
        share demod_selector (fast index switch, no reconnect); RADE gets
        its own dedicated producer (see the comment above demod_selector's
        construction for why it can't share it)."""
        producers = {self.MODE_FM: self.demod_selector, self.MODE_SSB: self.demod_selector}
        if RADE_AVAILABLE:
            producers[self.MODE_RADE] = self.rade_audio_resampler_up
        return producers

    def _null_sink_for_audio(self, producer):
        if producer is self.demod_selector:
            return self._null_sink_selector
        if RADE_AVAILABLE and producer is self.rade_audio_resampler_up:
            return self._null_sink_rade
        raise ValueError(f"no null_sink registered for producer {producer!r}")

    def set_demod_mode(self, mode: int):
        prev_mode = self.demod_mode
        producers = self._audio_producer_map()
        prev_producer = producers[prev_mode]
        new_producer = producers[mode]
        self.demod_mode = mode

        if new_producer is not prev_producer:
            # Entering/leaving RADE mode: reroute nf_gain's upstream
            # connection. Brief pause (lock/unlock), same pattern already
            # proven on real hardware for pluto_tx's M17/FreeDV/RADE mode
            # switching. FM<->SSB switching never hits this branch (both
            # map to the same demod_selector producer).
            self.lock()
            try:
                self.disconnect(prev_producer, self.nf_gain)
                self.disconnect(new_producer, self._null_sink_for_audio(new_producer))
                self.connect(new_producer, self.nf_gain)
                self.connect(prev_producer, self._null_sink_for_audio(prev_producer))
            finally:
                self.unlock()

        if mode == self.MODE_RADE:
            return  # bypasses demod_selector entirely, nothing to retap there

        self.demod_selector.set_input_index(1 if mode == self.MODE_SSB else 0)

    def set_nf_gain(self, gain: float):
        self.nf_gain.set_k(gain)

    def set_rx_muted(self, muted: bool):
        self.rx_mute.set_k(0.0 if muted else 1.0)

    def set_fft_size(self, n: int):
        self.fft_probe.set_fft_size(n)

    def set_fft_zoom(self, zoom: int):
        self.fft_probe.set_zoom(zoom)

    def set_fft_avg_count(self, n: int):
        self.fft_probe.set_avg_count(n)

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

    def set_psk31_tone_hz(self, tone_hz: float):
        """Retunes psk31_tone_filter in place (freq_xlating_fir_filter_ccf.
        set_center_freq() is safe at runtime, same technique
        set_fm_demod_width() already relies on for its own filter) and
        resets the AFC's own tracked center back to this new nominal -- an
        operator manually retuning implies "start the drift search over
        from here", not "keep whatever correction had accumulated against
        the OLD nominal"."""
        self.psk31_tone_hz = float(tone_hz)
        self.psk31_tone_center_hz = self.psk31_tone_hz
        self.psk31_tone_filter.set_center_freq(self.psk31_tone_hz)

    def psk31_afc_step(self):
        """Reads psk31_afc_probe's latest FFT row (if a new one is ready
        since the last call) and, if a real tone peak is found, retunes
        psk31_tone_filter to it once the estimate differs from the
        currently-applied center by more than PSK31_AFC_DEADBAND_HZ.
        Returns the estimated center (float) if a peak was found this
        call (whether or not it triggered a retune), or None if no real
        signal was found (weak/absent -- correctly leaves the current
        tuning alone rather than chasing noise, since
        estimate_signal_center() itself already returns None in that
        case; see PSK31_AFC_THRESHOLD_DB's own comment for a real,
        important finding about how permissive its default is on
        featureless noise).

        Phase 4.5 rework history (real-hardware findings, both tried and
        rejected before landing on the current design): the original
        version retuned unconditionally on every call with a SMALL (15Hz)
        deadband against a NARROW (+-100Hz) static filter -- real testing
        found the true drift is continuous and fast enough (~4-19Hz/s,
        sustained, no leveling off across 60+ seconds) that this either
        missed the signal entirely once it drifted outside the narrow
        filter, or (once the filter was widened, see
        PSK31_XLATE_CUTOFF_HZ's own comment) kept yanking the passband's
        center on every poll, each retune itself a discontinuity that
        disrupted the Costas loop's own ability to track the REST of the
        drift continuously on its own. A follow-up attempt gated retuning
        on "is a message currently actively decoding"
        (psk31_deframer.chars_decoded recently advancing) to stop exactly
        that mid-lock disruption -- also rejected: pure background noise
        alone reliably produces a low but non-zero trickle of spurious
        decoded characters (confirmed on real hardware, no TX active at
        all), so that gate almost never actually reported "idle" and the
        filter's center stayed pinned wherever it started.

        **What actually works, confirmed via a real over-the-air test with
        the actual app classes decoding a full message correctly**: keep
        retuning unconditionally (no activity gate at all), but use a
        LARGE deadband (PSK31_AFC_DEADBAND_HZ) instead of a small one --
        large enough that only a genuinely big offset (e.g. the initial
        gap between the operator's nominal tone and the real, currently-
        drifted position) triggers an external retune at all; ordinary
        continuous drift within a message stays under the deadband and is
        left entirely to the Costas loop's own NCO to track, which it can
        do well once given a wide-enough starting passband (see
        PSK31_XLATE_CUTOFF_HZ) and a fast-enough loop bandwidth (see
        PSK31_LOOP_BW) -- external retuning becomes a rare coarse
        correction, not a constant fight with the loop's own tracking.

        The search window is always anchored at psk31_tone_hz (the
        STABLE operator nominal), NOT at the last-applied
        psk31_tone_center_hz -- deliberately, to bound the worst case of
        an occasional bad estimate (e.g. a stray real interferer, or
        PSK31_AFC_THRESHOLD_DB's own known centroid-regresses-to-window-
        center bias on pure noise): searching from a WALKING center could
        let a string of small missteps drift the filter further and
        further from the true signal with nothing pulling it back: with a
        FIXED search center, each call is an independent fresh estimate
        of "where is the real tone relative to where the operator
        actually asked to listen", so a bad call can't compound with the
        next one, and PSK31_AFC_SEARCH_RADIUS_HZ already comfortably
        covers the full drift range observed in real testing. Safe/cheap
        to call often -- psk31_afc_probe internally throttles its own
        compute rate (PSK31_AFC_COMPUTE_RATE_HZ) independent of how often
        this is called; gui.py's _poll_psk31() additionally throttles the
        cadence at which it actually calls this, see its own comment."""
        row, self._psk31_afc_gen = self.psk31_afc_probe.get_latest_row(self._psk31_afc_gen)
        if row is None:
            return None
        est = rade_autotune.estimate_signal_center(
            row, center_hz=0.0, span_hz=self.if_rate, freq_hz=self.psk31_tone_hz,
            radius_hz=config.PSK31_AFC_SEARCH_RADIUS_HZ, threshold_db=config.PSK31_AFC_THRESHOLD_DB,
        )
        if est is None:
            return None
        if abs(est - self.psk31_tone_center_hz) > config.PSK31_AFC_DEADBAND_HZ:
            self.psk31_tone_center_hz = est
            self.psk31_tone_filter.set_center_freq(est)
        return est

    def shutdown(self):
        """Stop the flowgraph. Safe to call more than once."""
        try:
            self.stop()
            self.wait()
        except Exception as e:
            print(f"WARNING: flowgraph stop() failed: {e}", file=sys.stderr)
