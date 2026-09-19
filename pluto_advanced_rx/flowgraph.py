"""GNU Radio flowgraph for the PlutoSDR advanced RX app: FM and SSB(USB)
demodulators plus a FftProbe tap feeding the interactive pyqtgraph waterfall
widget (instead of GNU Radio's opaque qtgui.waterfall_sink_c, which has no
data-out port a Python-side widget could read).

Independent of pluto_tx: RX can't radiate, so none of pluto_tx's
attenuation/LO-powerdown safety machinery applies here. (This app started
as a self-contained copy of a simpler predecessor, `pluto_rx`, which has
since been removed as fully superseded -- see git history if that lineage
is ever relevant.)

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
from .rtty_deframer import RTTYBaudotDeframer
from .meshtastic_deframer import MeshtasticDeframer
from .pocsag_deframer import PocsagDeframer
from pluto_tx.lora_airtime import cr_index as _lora_cr_index

# LoRa/Meshtastic is optional, same reasoning as M17/RADE above: gr-lora_sdr
# is a from-source build (install-lora.sh), and the packet layer needs the
# `meshtastic` + `cryptography` pip packages.
try:
    from .lora_rx import LoraRxDecoder, LORA_AVAILABLE
    from pluto_tx.lora import lora_resampler_taps as _lora_resampler_taps
    from pluto_tx import meshtastic_codec as _meshtastic_codec  # noqa: F401 (import check)
except ImportError:
    LoraRxDecoder = None
    LORA_AVAILABLE = False

# RADE V1 is optional, same reasoning as pluto_tx: from-source build (see
# install-rade.sh), not something every user has.
from . import rade_ctypes as _rade_ctypes
from .rade import RadeDecoder
RADE_AVAILABLE = _rade_ctypes.RADE_AVAILABLE

# gr-m17 is optional, same reasoning as RADE above and matching
# pluto_tx/flowgraph.py's own M17_AVAILABLE gate exactly (this app links
# the same vendored OOT module, see install-m17.sh) -- unlike RADE, no
# ctypes involved: m17.m17_decoder/m17.codec2_decoder are native
# gr::block/pybind11 blocks, confirmed already built and importable in
# this exact toolchain (install-m17.sh's own self-test).
from .m17_deframer import M17FieldsDeframer
try:
    from gnuradio import m17 as _m17
    M17_AVAILABLE = True
except ImportError:
    _m17 = None
    M17_AVAILABLE = False


class AdvancedRxFlowgraph(gr.top_block):
    MODE_FM = 0
    MODE_SSB = 1
    MODE_RADE = 2
    MODE_M17 = 3
    MODE_BASEBAND = 4
    MODE_LSB = 5  # shares the SSB branch/selector port; only the filter's side differs

    def __init__(self, uri=None, frequency=config.DEFAULT_FREQUENCY,
                 sample_rate=None, gain_mode=config.DEFAULT_GAIN_MODE,
                 manual_gain_db=config.DEFAULT_MANUAL_GAIN_DB, demod_mode=MODE_FM,
                 nf_gain=config.DEFAULT_NF_GAIN, fft_size=config.DEFAULT_FFT_SIZE,
                 fm_demod_width_hz=config.FM_DEMOD_WIDTH_DEFAULT_HZ,
                 ssb_demod_width_hz=config.SSB_DEMOD_WIDTH_DEFAULT_HZ, device_type="pluto",
                 gain_values=None, on_filebroadcast_frame=None, on_psk31_char=None,
                 psk31_tone_hz=config.PSK31_DEFAULT_TONE_HZ, audio_device="",
                 on_m17_fields=None, baseband_width_hz=config.BASEBAND_WIDTH_DEFAULT_HZ,
                 on_rtty_char=None, rtty_mark_hz=config.RTTY_MARK_HZ_DEFAULT,
                 rtty_shift_hz=config.RTTY_SHIFT_HZ_DEFAULT, rtty_baud_rate=config.RTTY_BAUD_RATE_DEFAULT,
                 rtty_reverse=False, active_digimode=None, buffer_size=None,
                 on_meshtastic_frame=None, meshtastic_preset_index=0,
                 frequency_correction_ppm=0.0, direct_sampling=0, on_pocsag_message=None):
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
        further manual-only stages, though none implemented so far do.

        buffer_size is Pluto-only (RxDevice.supports_buffer_size gates it,
        harmless no-op on other backends) -- the client-side libiio buffer
        size passed to fmcomms2_source_fc32(), see config.py's
        PLUTO_RX_BUFFER_SIZE_* for the real-hardware throughput story
        behind it. None keeps today's default (32768) unchanged."""
        super().__init__("AdvancedRxFlowgraph")

        if demod_mode == self.MODE_RADE and not RADE_AVAILABLE:
            # demod_selector below would otherwise be constructed pointing
            # at a mode it can't actually decode (RADE bypasses it entirely
            # -- see below) -- fall back rather than build a broken
            # flowgraph, same pattern as pluto_tx's MODE_RADE fallback.
            demod_mode = self.MODE_FM
        if demod_mode == self.MODE_M17 and not M17_AVAILABLE:
            demod_mode = self.MODE_FM  # same reasoning as MODE_RADE's fallback above
        self.demod_mode = demod_mode  # tracked so set_demod_mode() knows the PREVIOUS producer to swap away from

        self.nominal_freq_hz = float(frequency)
        self.fine_offset_hz = 0.0

        self.device = devices.build_device(
            device_type, connection=uri, frequency_hz=self.nominal_freq_hz,
            sample_rate_hz=sample_rate, bandwidth_hz=None, buffer_size=buffer_size,
        )
        self.uri = self.device.connection  # the actual (possibly defaulted) connection string
        self.sample_rate = self.device.sample_rate_hz  # the actual (possibly defaulted) sample rate
        # Oscillator correction (ppm, device-error convention) and RTL-SDR direct
        # sampling are stored on the device BEFORE the source exists, so
        # build_source() already programs the corrected/right tuning path.
        self.device.set_frequency_correction_ppm(frequency_correction_ppm)
        self.device.set_direct_sampling(direct_sampling)
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
        # RX_BANDWIDTH span directly off pluto_source. Always on -- cheap
        # enough (throttled compute rate, see fft_probe.py) that no
        # enable/disable toggle is needed.
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
        # pure decimation) with auto-designed taps -- beats a
        # firdes.low_pass'd filter with a fixed absolute-Hz transition
        # width (thousands of taps at wider presets for no accuracy benefit).
        decim = max(1, round(self.sample_rate / config.DEMOD_IF_RATE))
        self.if_rate = self.sample_rate / decim
        self.if_filter = filter.rational_resampler_ccf(
            interpolation=1, decimation=decim, taps=[], fractional_bw=0.4,
        )
        if self.device.is_audio_only():
            # No RF LO to retune, unlike every other backend -- shifts the
            # chosen audio frequency down to 0 Hz baseband instead, the
            # same position a real SDR's own LO retune already puts "the
            # signal" at for every downstream branch (if_filter onward).
            # rotator_cc multiplies by e^(j*phase_inc*n); shifting a signal
            # AT +actual Hz DOWN to 0 needs phase_inc = -2*pi*actual/fs.
            # fft_probe (above) keeps tapping pluto_source directly, BEFORE
            # this rotator -- see gui.py's _sync_waterfall() for why the
            # waterfall's displayed axis must stay pinned to that raw,
            # unrotated spectrum rather than following this shift.
            self.audio_rotator = blocks.rotator_cc(
                -2 * math.pi * (self.nominal_freq_hz + self.fine_offset_hz) / self.sample_rate
            )
            self.connect(self.pluto_source, self.audio_rotator)
            self.connect(self.audio_rotator, self.if_filter)
        else:
            self.audio_rotator = None
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
        # LSB (MODE_LSB) uses the same branch with the mirrored band below the carrier.
        ssb_taps = self._ssb_taps(ssb_demod_width_hz, demod_mode == self.MODE_LSB)
        self.ssb_filter = filter.fir_filter_ccc(1, ssb_taps)
        self.ssb_to_real = blocks.complex_to_real()
        self.connect(self.if_filter, self.ssb_filter)
        self.connect(self.ssb_filter, self.ssb_to_real)

        # --- Baseband branch: wide, unprocessed demodulated audio for
        # digimode software (fldigi etc.) consuming this app's Audio
        # Output (real device, or the persistent PipeWire loopback node --
        # see audio_devices.py). Its OWN IF-domain channel filter
        # (baseband_channel_filter), wider-range than FM's
        # fm_channel_filter, is kept -- necessary anti-alias/adjacent-
        # channel-selectivity engineering, not voice-shaping "processing."
        # What it deliberately has NO equivalent of is fm_audio_filter:
        # that fixed 3kHz post-demod low-pass would hard-cap exactly the
        # wide content (RTTY/PSK31/Olivia/MFSK, several kHz) this mode
        # exists to carry -- straight to the resampler instead.
        self.baseband_width_hz = baseband_width_hz
        baseband_channel_taps = firdes.low_pass(1.0, self.if_rate, baseband_width_hz / 2,
                                                 config.FM_CHANNEL_TRANS_HZ, window.WIN_HAMMING)
        self.baseband_channel_filter = filter.fir_filter_ccc(1, baseband_channel_taps)
        baseband_gain = self.if_rate / (2 * math.pi * config.BASEBAND_DEVIATION_HZ)
        self.baseband_demod = analog.quadrature_demod_cf(baseband_gain)
        self.connect(self.if_filter, self.baseband_channel_filter)
        self.connect(self.baseband_channel_filter, self.baseband_demod)

        # --- Resample all three demodulated-audio branches (at the fixed
        # if_rate) up/down to AUDIO_RATE, then pick the active mode.
        # fractional_bw=0.47 (not the 0.4 FM/SSB use) for Baseband's own
        # resampler -- Digitext's own precedent for exactly this problem
        # (its comment nearby): 0.4's passband starts rolling off around
        # 18-19kHz at AUDIO_RATE=48000; 0.47 stays flat to ~22kHz, needed
        # since Baseband explicitly wants content wider than typical
        # 3kHz voice/SSB audio.
        g = math.gcd(int(self.if_rate), config.AUDIO_RATE)
        self.fm_resampler = filter.rational_resampler_fff(
            interpolation=config.AUDIO_RATE // g, decimation=int(self.if_rate) // g,
            taps=[], fractional_bw=0.4,
        )
        self.ssb_resampler = filter.rational_resampler_fff(
            interpolation=config.AUDIO_RATE // g, decimation=int(self.if_rate) // g,
            taps=[], fractional_bw=0.4,
        )
        self.baseband_resampler = filter.rational_resampler_fff(
            interpolation=config.AUDIO_RATE // g, decimation=int(self.if_rate) // g,
            taps=[], fractional_bw=0.47,
        )
        self.connect(self.fm_audio_filter, self.fm_resampler)
        self.connect(self.ssb_to_real, self.ssb_resampler)
        self.connect(self.baseband_demod, self.baseband_resampler)

        # NOTE: blocks.selector's ninputs is only known once the flowgraph is
        # actually running -- the initial index must go through the
        # constructor (see pluto_tx/flowgraph.py for the full explanation of
        # this gotcha). set_demod_mode() below is for RUNTIME switching only.
        # demod_selector carries FM/SSB/Baseband (3 inputs) -- RADE and
        # M17 are deliberately NOT selector inputs, same scheduler-risk
        # reasoning as pluto_tx's own M17/FreeDV/RADE producers (a
        # frame-quantized block continuously producing output while
        # unselected would back up against an unread selector branch --
        # RadeDecoder's variable nin()/irregular output cadence is the
        # most extreme case of this, but the multi-stage M17 decode chain
        # is complex/stateful enough to warrant the same proven-safe
        # dedicated-producer pattern rather than risking it as a 3rd
        # selector input). Baseband, unlike RADE/M17, IS safe as a
        # selector input -- structurally identical to FM/SSB (fixed-rate,
        # stateless, always-continuously-producing float at AUDIO_RATE),
        # none of the variable-rate/frame-quantized/stateful properties
        # that ruled RADE/M17 out apply to it. Its initial index is
        # irrelevant when demod_mode is MODE_RADE/MODE_M17 (demod_selector's
        # output drains into a null_sink in that case, see
        # _audio_producer_map()/set_demod_mode() below) -- clamp to a
        # valid FM/SSB/Baseband index either way.
        _initial_selector_index = {
            self.MODE_SSB: 1, self.MODE_LSB: 1, self.MODE_BASEBAND: 2,
        }.get(demod_mode, 0)
        self.demod_selector = blocks.selector(gr.sizeof_float, _initial_selector_index, 0)
        self.demod_selector.set_enabled(True)
        # NOTE: MODE_FM=0/MODE_SSB=1 happen to already match their
        # selector port indices, but MODE_BASEBAND=4 (the demod-mode enum
        # value, shared with RADE=2/M17=3 which DON'T use this selector)
        # does NOT -- port index 2 is the correct, explicit 3rd port here,
        # matching _initial_selector_index/set_demod_mode()'s own
        # {MODE_SSB: 1, MODE_BASEBAND: 2} port-index maps below.
        self.connect(self.fm_resampler, (self.demod_selector, self.MODE_FM))
        self.connect(self.ssb_resampler, (self.demod_selector, self.MODE_SSB))
        self.connect(self.baseband_resampler, (self.demod_selector, 2))

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

        # --- M17 branch (optional, only if gr-m17 is built -- see
        # M17_AVAILABLE above). Taps if_filter's output, same reasoning as
        # RADE just above (keeps the resampler ratio small; if_filter's
        # anti-alias low-pass is transparent to M17's narrow ~9.6kHz
        # FM-deviation signal). This exact chain (quad demod -> DC-bias
        # removal -> RRC matched filter -> digital.symbol_sync_ff ->
        # m17.m17_decoder -> m17.codec2_decoder) was verified against a
        # real, working TX->RX M17 loopback this session -- gr-m17's own
        # decoder correctly decoded src/dst callsigns and produced real
        # audio -- before being wired in here. Mirrors gr-m17's own
        # shipped example flowgraphs (receiverRTLSDR.grc/
        # m17_loopback_noisychannel.grc), which use GNU Radio's stock
        # digital.symbol_sync_ff rather than gr-m17's own m17.symbol_sync
        # (present in gr-m17's source but not compiled in this checkout --
        # see the repo README's own M17 RX ToDo entry, now resolved).
        if M17_AVAILABLE:
            g_m17 = math.gcd(int(self.if_rate), config.M17_BASEBAND_RATE)
            self.m17_rx_resampler = filter.rational_resampler_ccf(
                interpolation=config.M17_BASEBAND_RATE // g_m17,
                decimation=int(self.if_rate) // g_m17,
                taps=[], fractional_bw=0.4,
            )
            self.connect(self.if_filter, self.m17_rx_resampler)
            self.m17_quad_demod = analog.quadrature_demod_cf(
                config.M17_BASEBAND_RATE / (2 * math.pi * config.M17_DEVIATION_HZ)
            )
            self.connect(self.m17_rx_resampler, self.m17_quad_demod)
            # DC-bias removal -- a real FM demod's output isn't perfectly
            # zero-mean at real-world frequency offsets, which would
            # otherwise bias the 4-level FSK symbol slicer. Mirrors
            # gr-m17's own shipped RX examples exactly: a moving-average
            # estimate of the local DC level, subtracted back out.
            m17_dc_avg_len = int(0.1 * config.M17_BASEBAND_RATE)
            self.m17_dc_avg = blocks.moving_average_ff(m17_dc_avg_len, 1.0 / m17_dc_avg_len, 4000)
            self.m17_dc_sub = blocks.sub_ff()
            self.connect(self.m17_quad_demod, self.m17_dc_avg)
            self.connect(self.m17_quad_demod, (self.m17_dc_sub, 0))
            self.connect(self.m17_dc_avg, (self.m17_dc_sub, 1))
            m17_rx_rrc_taps = firdes.root_raised_cosine(
                1.0, config.M17_BASEBAND_RATE, config.M17_SYMBOL_RATE,
                config.M17_RRC_ALPHA, config.M17_RRC_NTAPS,
            )
            self.m17_matched_filter = filter.fir_filter_fff(1, m17_rx_rrc_taps)
            self.connect(self.m17_dc_sub, self.m17_matched_filter)
            self.m17_sym_sync = digital.symbol_sync_ff(
                digital.TED_GARDNER, config.M17_RRC_SPS, 2 * math.pi * 0.0015,
                1.0, 1.0, 0.05, 1,
                digital.constellation_bpsk().base(), digital.IR_MMSE_8TAP, 128, [],
            )
            self.connect(self.m17_matched_filter, self.m17_sym_sync)
            # debug_data/debug_ctrl off, sw_threshold/vt_threshold at
            # gr-m17's own GRC-template defaults (verified working in this
            # session's loopback test), callsign display on, no
            # encryption/scrambler.
            self.m17_decoder = _m17.m17_decoder(False, False, 2.0, 30.0, True, False, 0, "", "")
            self.connect(self.m17_sym_sync, self.m17_decoder)
            self.m17_fields_deframer = M17FieldsDeframer(on_m17_fields or (lambda fields: None))
            self.msg_connect(self.m17_decoder, "fields", self.m17_fields_deframer, "fields")
            self.m17_codec2_decoder = _m17.codec2_decoder()
            self.connect(self.m17_decoder, self.m17_codec2_decoder)
            self.m17_short_to_float = blocks.short_to_float(1, 32767.0)
            self.connect(self.m17_codec2_decoder, self.m17_short_to_float)
            g_m17_up = math.gcd(config.M17_CODEC2_RATE, config.AUDIO_RATE)
            self.m17_audio_resampler_up = filter.rational_resampler_fff(
                interpolation=config.AUDIO_RATE // g_m17_up,
                decimation=config.M17_CODEC2_RATE // g_m17_up,
                taps=[], fractional_bw=0.4,
            )
            self.connect(self.m17_short_to_float, self.m17_audio_resampler_up)

        # --- Digimodes (PSK31, RTTY -- more may be added later). `self.
        # active_digimode` (None/"psk31"/"rtty") selects which ONE
        # digimode's decode chain is actually connected to if_filter (+
        # its own AFC probe) -- per explicit user request, there's no
        # good reason for a digimode decoder to keep consuming real CPU
        # when it isn't the one currently selected/visible, and this
        # scales to more digimodes being added later without each new one
        # silently costing cycles by default. Both digimodes' blocks are
        # still always CONSTRUCTED here regardless of active_digimode
        # (cheap -- plain object construction, no connections means no
        # scheduling/CPU cost, confirmed empirically: a GNU Radio block
        # with zero connect() calls touching it anywhere is never part of
        # the running flowgraph at all), so e.g. gui.py's status/signal-
        # label code can always read tb.psk31_deframer.chars_decoded /
        # tb.rtty_deframer.chars_decoded regardless of which one is
        # currently active (an inactive one simply never advances past 0,
        # which is exactly correct).
        #
        # IMPORTANT GNU Radio constraint this design works around, found
        # empirically while building this: a block with a REQUIRED input
        # port (min 1) cannot be left with zero connections while the
        # flowgraph is unlocked/running -- confirmed via a direct test,
        # both at the initial start() and via a live lock()/disconnect()/
        # unlock() cycle on an already-running flowgraph (both raise the
        # identical "insufficient connected input ports" RuntimeError).
        # This rules out the originally-planned lighter-weight design (one
        # persistent flowgraph, a runtime set_active_digimode() method
        # cheaply reconnecting if_filter's tap on demand, mirroring
        # set_demod_mode()'s own already-proven pattern) -- that pattern
        # only ever SWAPS which of several producers feeds one shared
        # sink (always exactly one connected), never truly disconnects a
        # required input to zero. Switching which digimode is active
        # therefore rebuilds the whole AdvancedRxFlowgraph instance (see
        # gui.py's _on_digimode_changed()/_on_mode_tab_changed()) --
        # mirrors the already-established rebuild pattern
        # _on_bandwidth_changed()/_on_audio_device_changed() use for their
        # own "can't reconfigure this live" GNU Radio limitations, not a
        # new mechanism.

        # --- PSK31 (BPSK31 keyboard-to-keyboard chat digimode). Taps
        # if_filter's output (NOT pluto_source directly, unlike File
        # Broadcast) -- PSK31's ~50-60Hz occupied bandwidth is tiny, the
        # same if_filter tap point RADE already uses above is more than
        # enough, avoiding the documented real scheduler-crash risk of
        # decimating straight off a multi-Msps pluto_source in one stage.
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
        self.psk31_costas_loop = digital.costas_loop_cc(config.PSK31_LOOP_BW, 2, False)
        self.psk31_complex_to_real = blocks.complex_to_real()
        psk31_sps = self.psk31_working_rate / config.PSK31_SYMBOL_RATE_HZ
        self.psk31_symbol_sync = digital.symbol_sync_ff(
            digital.TED_MUELLER_AND_MULLER, psk31_sps, config.PSK31_LOOP_BW, 1.0, 1.0, 0.05, 1,
            digital.constellation_bpsk().base(), digital.IR_MMSE_8TAP, 128, [],
        )
        self.psk31_slicer = digital.binary_slicer_fb()
        self.psk31_diff_decoder = digital.diff_decoder_bb(2)
        # Inverter: digital.diff_decoder_bb's standard convention
        # (decoded[n] = encoded[n] XOR encoded[n-1], 1=transition) is the
        # OPPOSITE of PSK31's own (0=phase reversal, 1=no change) --
        # determined empirically during Phase 0 PHY testing, see pluto_tx/
        # psk31.py's own docstring.
        self.psk31_inverter = blocks.not_bb()
        self.psk31_deframer = PSK31VaricodeDeframer(on_psk31_char or (lambda ch: None))
        # Dedicated AFC search probe -- see the AFC comment above.
        self.psk31_afc_probe = FftProbe(
            config.PSK31_AFC_FFT_SIZE, self.if_rate, config.WATERFALL_WINDOW, config.PSK31_AFC_COMPUTE_RATE_HZ,
        )
        self._psk31_afc_gen = -1
        if active_digimode == "psk31":
            # The ENTIRE chain is wired here, all at once, only for the
            # active digimode -- a GNU Radio block with a required input
            # left with zero connect() calls touching it anywhere (not
            # just its OWN input disconnected, but literally never
            # mentioned in any connect() call at all, including as a
            # SOURCE further downstream) is simply never part of the
            # running flowgraph and costs nothing -- confirmed
            # empirically. Wiring only part of an inactive chain (e.g.
            # just skipping the if_filter tap but still connecting
            # psk31_tone_filter->psk31_costas_loop) does NOT work: it
            # still makes psk31_tone_filter part of the graph (as a
            # connect() source), so its own required input is still
            # validated and found lacking -- confirmed the hard way while
            # building this.
            self.connect(self.if_filter, self.psk31_tone_filter)
            self.connect(self.psk31_tone_filter, self.psk31_costas_loop)
            self.connect(self.psk31_costas_loop, self.psk31_complex_to_real)
            self.connect(self.psk31_complex_to_real, self.psk31_symbol_sync)
            self.connect(self.psk31_symbol_sync, self.psk31_slicer)
            self.connect(self.psk31_slicer, self.psk31_diff_decoder)
            self.connect(self.psk31_diff_decoder, self.psk31_inverter)
            self.connect(self.psk31_inverter, self.psk31_deframer)
            self.connect(self.if_filter, self.psk31_afc_probe)

        # --- RTTY (2-tone FSK Baudot digimode). Same if_filter tap point
        # as PSK31, same "not connected until selected" deferral (see the
        # Digimodes intro comment above). Unlike PSK31 (fixed 31.25 baud,
        # Costas-loop phase-tracked), mark/shift/baud/reverse are all
        # real runtime-adjustable settings here -- see
        # set_rtty_mark_hz()/set_rtty_shift_hz()/set_rtty_baud_rate()/
        # set_rtty_reverse() and rtty_deframer.py's own docstring for why
        # this chain uses a quadrature-FM-discriminator + open-loop UART
        # framing instead of PSK31's Costas-loop/symbol_sync_ff approach
        # (Baudot's genuinely-asynchronous idle gaps rule that out).
        #
        # rtty_mark_hz/rtty_shift_hz below are the STABLE, operator-set
        # NOMINAL values (what set_rtty_mark_hz()/set_rtty_shift_hz()
        # change, what rtty_afc_step()'s search is anchored at) --
        # rtty_mark_center_hz/rtty_shift_center_hz are the AFC-tracked
        # CURRENTLY-APPLIED values the filter/demod are actually tuned
        # to, exactly mirroring psk31_tone_hz vs. psk31_tone_center_hz's
        # own nominal-vs-tracked split above (same rationale: a manual
        # retune should restart the drift search from the new nominal,
        # not keep whatever the AFC had already accumulated against the
        # old one).
        self.rtty_mark_hz = float(rtty_mark_hz)
        self.rtty_shift_hz = float(rtty_shift_hz)
        self.rtty_mark_center_hz = self.rtty_mark_hz
        self.rtty_shift_center_hz = self.rtty_shift_hz
        self.rtty_baud_rate = float(rtty_baud_rate)
        self.rtty_reverse = bool(rtty_reverse)
        rtty_decim = max(1, round(self.if_rate / config.RTTY_WORKING_RATE_HZ))
        self.rtty_working_rate = self.if_rate / rtty_decim
        self.rtty_band_filter = filter.freq_xlating_fir_filter_ccf(
            rtty_decim, self._rtty_filter_taps(), self._rtty_center_hz(), self.if_rate,
        )
        self.rtty_demod = analog.quadrature_demod_cf(self._rtty_demod_gain())
        rtty_lowpass_taps = firdes.low_pass(
            1.0, self.rtty_working_rate, config.RTTY_LOWPASS_CUTOFF_HZ, config.RTTY_LOWPASS_TRANS_HZ,
            window.WIN_HAMMING,
        )
        self.rtty_lowpass = filter.fir_filter_fff(1, rtty_lowpass_taps)
        self.rtty_slicer = digital.binary_slicer_fb()
        self.rtty_deframer = RTTYBaudotDeframer(
            on_rtty_char or (lambda ch: None), self.rtty_working_rate, self.rtty_baud_rate,
            stop_bits=config.RTTY_STOP_BITS, reverse=self.rtty_reverse,
        )
        self.rtty_afc_probe = FftProbe(
            config.RTTY_AFC_FFT_SIZE, self.if_rate, config.WATERFALL_WINDOW, config.RTTY_AFC_COMPUTE_RATE_HZ,
        )
        self._rtty_afc_gen = -1
        if active_digimode == "rtty":
            # See the PSK31 branch's identical comment above for why the
            # WHOLE chain must be wired here, all at once, only when this
            # digimode is the active one.
            self.connect(self.if_filter, self.rtty_band_filter)
            self.connect(self.rtty_band_filter, self.rtty_demod)
            self.connect(self.rtty_demod, self.rtty_lowpass)
            self.connect(self.rtty_lowpass, self.rtty_slicer)
            self.connect(self.rtty_slicer, self.rtty_deframer)
            self.connect(self.if_filter, self.rtty_afc_probe)

        # --- Meshtastic (LoRa CSS) RX. Taps pluto_source DIRECTLY (like File
        # Broadcast, not the 50 kHz if_filter output: LongFast is 250 kHz wide),
        # resamples to 4 x LoRa bandwidth with EXPLICIT taps, then gr-lora_sdr's
        # decoder emits one message per CRC-verified frame to the deframer.
        # Constructed only when active (the decoder is heavy, unlike the plain
        # deframers above); the deframer object always exists so the GUI can
        # read tb.meshtastic_deframer.frames_decoded regardless.
        self.meshtastic_preset_index = int(meshtastic_preset_index)
        self.meshtastic_deframer = MeshtasticDeframer(on_meshtastic_frame or (lambda raw: None))
        if active_digimode == "meshtastic":
            if not LORA_AVAILABLE:
                raise ValueError("Meshtastic needs gr-lora_sdr and the meshtastic package -- see install-lora.sh")
            if self.device.is_audio_only():
                raise ValueError("Meshtastic (LoRa) needs an RF device, not a soundcard")
            lp = config.MESHTASTIC_PRESETS[self.meshtastic_preset_index]
            lora_bw = int(lp.bandwidth_hz)
            lora_fs = lora_bw * 4  # LoraRxDecoder's proven oversampling (samp_rate_mult=4)
            if self.sample_rate < 2 * lora_bw:
                raise ValueError(
                    f"RX bandwidth {self.sample_rate / 1e6:g} MS/s is too low for {lp.name} "
                    f"({lora_bw / 1e3:g} kHz) -- pick at least {2 * lora_bw / 1e6:g} MS/s")
            g_lora = math.gcd(int(self.sample_rate), lora_fs)
            lora_interp, lora_decim = lora_fs // g_lora, int(self.sample_rate) // g_lora
            self.meshtastic_rx_resampler = filter.rational_resampler_ccf(
                interpolation=lora_interp, decimation=lora_decim,
                taps=_lora_resampler_taps(lora_bw, float(lora_interp), self.sample_rate * lora_interp,
                                          min(self.sample_rate, lora_fs)),
            )
            self.meshtastic_decoder = LoraRxDecoder(
                lp.spreading_factor, lora_bw, _lora_cr_index(lp.coding_rate),
                center_freq_hz=self.nominal_freq_hz,
            )
            self.connect(self.pluto_source, self.meshtastic_rx_resampler)
            self.connect(self.meshtastic_rx_resampler, self.meshtastic_decoder)
            self.msg_connect(self.meshtastic_decoder, "msg", self.meshtastic_deframer, "msg")

        # --- POCSAG paging RX (25 kHz channel, 2-FSK +-4.5 kHz, 512/1200/2400 Bd). Taps the 50 kHz
        # if_filter like RTTY/PSK31: channel low-pass -> FM discriminator (+-1 = +-4.5 kHz) -> DC removal
        # (carrier offset) -> three parallel per-baud branches (low-pass -> symbol_sync_ff -> slicer ->
        # PocsagDeframer), so the bit rate needs no setting. The deframers always exist (the GUI reads
        # their counters); the chain is only wired when active, like every digimode above.
        self.pocsag_channel_filter = filter.fir_filter_ccc(1, firdes.low_pass(
            1.0, self.if_rate, config.POCSAG_CHANNEL_CUTOFF_HZ, config.POCSAG_CHANNEL_TRANS_HZ, window.WIN_HAMMING))
        self.pocsag_demod = analog.quadrature_demod_cf(self.if_rate / (2 * math.pi * config.POCSAG_DEVIATION_HZ))
        self.pocsag_dc_iir = filter.single_pole_iir_filter_ff(1.0 - math.exp(-1.0 / (config.POCSAG_DC_TAU_S * self.if_rate)))
        self.pocsag_dc_sub = blocks.sub_ff()
        self.pocsag_deframers = {}
        pocsag_branches = []
        for baud in config.POCSAG_BAUD_RATES:
            lowpass = filter.fir_filter_fff(1, firdes.low_pass(
                1.0, self.if_rate, config.POCSAG_LOWPASS_BAUD_FACTOR * baud, 0.5 * baud, window.WIN_HAMMING))
            sync = digital.symbol_sync_ff(
                digital.TED_MUELLER_AND_MULLER, self.if_rate / baud, config.POCSAG_LOOP_BW, 1.0, 1.0, 0.05, 1,
                digital.constellation_bpsk().base(), digital.IR_MMSE_8TAP, 128, [],
            )
            slicer = digital.binary_slicer_fb()
            deframer = PocsagDeframer(on_pocsag_message or (lambda msg: None), baud)
            self.pocsag_deframers[baud] = deframer
            pocsag_branches.append((lowpass, sync, slicer, deframer))
        if active_digimode == "pocsag":
            if self.device.is_audio_only():
                # Sound card fed by an FM radio's discriminator/data output: the audio already IS the
                # demodulated signal, so no channel filter/FM demod (nor the audio tuning rotator) -- real part, DC removal, then an AGC
                # (the audio level is unknown) ahead of the same per-baud branches.
                self.pocsag_audio_real = blocks.complex_to_real()
                self.pocsag_agc = analog.agc_ff(1e-4, 1.0, 1.0)
                self.pocsag_clip = analog.rail_ff(-1.5, 1.5)  # AGC start-up spikes must not upset the timing loops
                self.connect(self.pluto_source, self.pocsag_audio_real)  # before the tuning rotator: plain audio
                self.connect(self.pocsag_audio_real, (self.pocsag_dc_sub, 0))
                self.connect(self.pocsag_audio_real, self.pocsag_dc_iir)
                self.connect(self.pocsag_dc_iir, (self.pocsag_dc_sub, 1))
                self.connect(self.pocsag_dc_sub, self.pocsag_agc)
                self.connect(self.pocsag_agc, self.pocsag_clip)
                branch_input = self.pocsag_clip
            else:
                self.connect(self.if_filter, self.pocsag_channel_filter)
                self.connect(self.pocsag_channel_filter, self.pocsag_demod)
                self.connect(self.pocsag_demod, (self.pocsag_dc_sub, 0))
                self.connect(self.pocsag_demod, self.pocsag_dc_iir)
                self.connect(self.pocsag_dc_iir, (self.pocsag_dc_sub, 1))
                branch_input = self.pocsag_dc_sub
            for lowpass, sync, slicer, deframer in pocsag_branches:
                self.connect(branch_input, lowpass)
                self.connect(lowpass, sync)
                self.connect(sync, slicer)
                self.connect(slicer, deframer)

        if active_digimode not in (None, "psk31", "rtty", "meshtastic", "pocsag"):
            raise ValueError(f"unknown active_digimode {active_digimode!r}")
        # Which digimode (None/"psk31"/"rtty") has its branch connected to
        # if_filter -- fixed for this instance's lifetime (see the
        # Digimodes intro comment above for why switching rebuilds the
        # whole flowgraph instead of changing this at runtime).
        self.active_digimode = active_digimode

        self.nf_gain = blocks.multiply_const_ff(nf_gain)
        # Exactly one of {demod_selector, rade_audio_resampler_up} feeds
        # nf_gain at a time -- the other drains into a null_sink. See
        # _audio_producer_map()/set_demod_mode() for the runtime swap logic
        # (RX-side mirror of PlutoTxFlowgraph.set_mode()'s tx_gain producer
        # swap, same lock()/connect()/disconnect() pattern).
        self._null_sink_selector = blocks.null_sink(gr.sizeof_float)
        if RADE_AVAILABLE:
            self._null_sink_rade = blocks.null_sink(gr.sizeof_float)
        if M17_AVAILABLE:
            self._null_sink_m17 = blocks.null_sink(gr.sizeof_float)
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
        if self.audio_rotator is not None:
            self.audio_rotator.set_phase_inc(-2 * math.pi * actual / self.sample_rate)
        else:
            self.device.set_frequency(actual)

    def set_frequency(self, freq_hz: float):
        self.nominal_freq_hz = freq_hz
        self._retune()

    def set_frequency_correction_ppm(self, ppm: float):
        """Live change of the oscillator correction; retunes the hardware, the
        TRUE frequency (nominal_freq_hz) and everything downstream stay as is."""
        self.device.set_frequency_correction_ppm(ppm)

    def set_direct_sampling(self, mode: int):
        """RTL-SDR only (0 off / 1 I / 2 Q); no-op for other backends."""
        self.device.set_direct_sampling(mode)

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
        """mode -> the block that should feed nf_gain in that mode. FM/SSB/
        Baseband share demod_selector (fast index switch, no reconnect);
        RADE/M17 get their own dedicated producer (see the comment above
        demod_selector's construction for why they can't share it)."""
        producers = {
            self.MODE_FM: self.demod_selector, self.MODE_SSB: self.demod_selector,
            self.MODE_LSB: self.demod_selector, self.MODE_BASEBAND: self.demod_selector,
        }
        if RADE_AVAILABLE:
            producers[self.MODE_RADE] = self.rade_audio_resampler_up
        if M17_AVAILABLE:
            producers[self.MODE_M17] = self.m17_audio_resampler_up
        return producers

    def _null_sink_for_audio(self, producer):
        if producer is self.demod_selector:
            return self._null_sink_selector
        if RADE_AVAILABLE and producer is self.rade_audio_resampler_up:
            return self._null_sink_rade
        if M17_AVAILABLE and producer is self.m17_audio_resampler_up:
            return self._null_sink_m17
        raise ValueError(f"no null_sink registered for producer {producer!r}")

    def set_demod_mode(self, mode: int):
        prev_mode = self.demod_mode
        producers = self._audio_producer_map()
        prev_producer = producers[prev_mode]
        new_producer = producers[mode]
        self.demod_mode = mode

        if new_producer is not prev_producer:
            # Entering/leaving RADE or M17 mode: reroute nf_gain's upstream
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

        if mode in (self.MODE_RADE, self.MODE_M17):
            return  # both bypass demod_selector entirely, nothing to retap there

        if mode in (self.MODE_SSB, self.MODE_LSB):
            self.ssb_filter.set_taps(self._ssb_taps(self.ssb_demod_width_hz, mode == self.MODE_LSB))
        self.demod_selector.set_input_index(
            {self.MODE_SSB: 1, self.MODE_LSB: 1, self.MODE_BASEBAND: 2}.get(mode, 0))

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

    def set_baseband_width(self, width_hz: float):
        """Retapes baseband_channel_filter in place -- exact mirror of
        set_fm_demod_width() above, just a wider default/range (see
        config.BASEBAND_WIDTH_RANGE_HZ)."""
        self.baseband_width_hz = width_hz
        taps = firdes.low_pass(1.0, self.if_rate, width_hz / 2, config.FM_CHANNEL_TRANS_HZ, window.WIN_HAMMING)
        self.baseband_channel_filter.set_taps(taps)

    def _ssb_taps(self, width_hz, lower_sideband):
        f_lo = config.SSB_AUDIO_BAND_HZ[0]
        lo, hi = f_lo, f_lo + width_hz
        if lower_sideband:
            lo, hi = -hi, -lo
        return firdes.complex_band_pass(1.0, self.if_rate, lo, hi,
                                        config.SSB_AUDIO_BAND_HZ[2], window.WIN_HAMMING)

    def set_ssb_demod_width(self, width_hz: float):
        self.ssb_demod_width_hz = width_hz
        self.ssb_filter.set_taps(self._ssb_taps(width_hz, self.demod_mode == self.MODE_LSB))

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

    def _rtty_center_hz(self):
        return self.rtty_mark_center_hz + self.rtty_shift_center_hz / 2.0

    def _rtty_filter_taps(self):
        cutoff = self.rtty_shift_center_hz / 2.0 + config.RTTY_FILTER_GUARD_HZ
        return firdes.low_pass(1.0, self.if_rate, cutoff, config.RTTY_FILTER_GUARD_HZ, window.WIN_HAMMING)

    def _rtty_demod_gain(self):
        return self.rtty_working_rate / (2 * math.pi * (self.rtty_shift_center_hz / 2.0))

    def set_rtty_mark_hz(self, mark_hz: float):
        """Retunes rtty_band_filter's center in place -- cheap, mirrors
        set_psk31_tone_hz(). Resets the AFC-tracked center back to this
        new nominal, same rationale as set_psk31_tone_hz()'s own reset."""
        self.rtty_mark_hz = float(mark_hz)
        self.rtty_mark_center_hz = self.rtty_mark_hz
        self.rtty_band_filter.set_center_freq(self._rtty_center_hz())

    def set_rtty_shift_hz(self, shift_hz: float):
        """Retapes rtty_band_filter's taps AND rtty_demod's gain in place
        -- unlike set_psk31_tone_hz()'s cheap center-only retune, a shift
        change alters the filter's required WIDTH (not just its center)
        and the discriminator gain needed to map the (now different)
        tone separation to a clean bipolar output. Both are live,
        already-proven-safe operations in this codebase (set_taps()/
        set_gain() at runtime -- same technique set_fm_demod_width()/
        set_baseband_width() already rely on), not new ground. Also
        resets the AFC-tracked center, same rationale as
        set_rtty_mark_hz()."""
        self.rtty_shift_hz = float(shift_hz)
        self.rtty_shift_center_hz = self.rtty_shift_hz
        self.rtty_band_filter.set_center_freq(self._rtty_center_hz())
        self.rtty_band_filter.set_taps(self._rtty_filter_taps())
        self.rtty_demod.set_gain(self._rtty_demod_gain())

    def set_rtty_baud_rate(self, baud_rate: float):
        """No GNU Radio reconfiguration needed at all -- rtty_deframer's
        own baud_rate is a cheap arithmetic parameter (see
        rtty_deframer.py's set_baud_rate()), and rtty_lowpass's fixed,
        baud-independent passband already comfortably covers every baud
        preset (see config.RTTY_LOWPASS_CUTOFF_HZ's own comment)."""
        self.rtty_baud_rate = float(baud_rate)
        self.rtty_deframer.set_baud_rate(self.rtty_baud_rate)

    def set_rtty_reverse(self, reverse: bool):
        self.rtty_reverse = bool(reverse)
        self.rtty_deframer.set_reverse(self.rtty_reverse)

    def rtty_afc_step(self):
        """Structural mirror of psk31_afc_step() -- see its own docstring
        for the shared parts of this design (fixed-nominal search anchor,
        large deadband, safe/cheap to call often). Two real differences
        (see config.py's RTTY_AFC section comment for the why): searches
        for the mark and space peaks SEPARATELY and averages the results
        into a fresh (mark, shift) pair, instead of one wide centroid
        search across both tones; search radius scales with the
        configured (nominal) shift instead of a static constant."""
        row, self._rtty_afc_gen = self.rtty_afc_probe.get_latest_row(self._rtty_afc_gen)
        if row is None:
            return None
        radius = self.rtty_shift_hz / 2.0 + config.RTTY_AFC_SEARCH_MARGIN_HZ
        space_nominal_hz = self.rtty_mark_hz + self.rtty_shift_hz
        mark_est = rade_autotune.estimate_signal_center(
            row, center_hz=0.0, span_hz=self.if_rate, freq_hz=self.rtty_mark_hz,
            radius_hz=radius, threshold_db=config.RTTY_AFC_THRESHOLD_DB,
        )
        space_est = rade_autotune.estimate_signal_center(
            row, center_hz=0.0, span_hz=self.if_rate, freq_hz=space_nominal_hz,
            radius_hz=radius, threshold_db=config.RTTY_AFC_THRESHOLD_DB,
        )
        if mark_est is None or space_est is None:
            return None
        est_shift = space_est - mark_est
        if est_shift <= 0:
            return None  # nonsensical (space below mark) -- a bad estimate, ignore
        center_est = (mark_est + space_est) / 2.0
        current_center = self.rtty_mark_center_hz + self.rtty_shift_center_hz / 2.0
        if abs(center_est - current_center) > config.RTTY_AFC_DEADBAND_HZ:
            self.rtty_mark_center_hz = mark_est
            self.rtty_shift_center_hz = est_shift
            self.rtty_band_filter.set_center_freq(self._rtty_center_hz())
            self.rtty_band_filter.set_taps(self._rtty_filter_taps())
            self.rtty_demod.set_gain(self._rtty_demod_gain())
        return center_est

    def shutdown(self):
        """Stop the flowgraph. Safe to call more than once."""
        try:
            self.stop()
            self.wait()
        except Exception as e:
            print(f"WARNING: flowgraph stop() failed: {e}", file=sys.stderr)
        self.device.close()
