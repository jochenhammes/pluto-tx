"""Audio Input RX backend: lets an externally-connected radio (a
conventional SSB transceiver, e.g. receiving RADE audio-injected over its
own RF path) feed this app via a sound card, instead of any SDR hardware.

No RF concept applies here at all -- no frequency, no gain. Device scan/
probe DO apply, though (see scan_devices_with_timeout()/probe_with_timeout()
below): real ALSA input devices, PipeWire sink monitors, and a persistent
qpwgraph loopback node are all selectable, the same "monitor:<node.name>"
mechanism pluto_tx/audio_devices.py already established.

The one real piece of DSP this backend does: convert the sound card's REAL
mono audio into the complex64 stream every other RxDevice.build_source()
already promises, so the REST of AdvancedRxFlowgraph (fft_probe, if_filter,
FM/SSB branches, the RADE branch) needs zero changes to accept it.

That conversion follows freedv/rade_c's own documented real-audio-input
convention (confirmed by reading its source this session, not assumed):
RadeAPIUse.md's "Scaling to 16 bits" section and rade_rx_wav.c (which reads
mono PCM, scales by 2.0f/RADE_INT16_SCALE -- double the genuine-IQ scale
factor -- and sets imag=0.0f) are explicit that a real-valued input (e.g.
"a real SSB radio's mono audio output") needs roughly double the amplitude
of true complex IQ, with NO Hilbert transform -- rade_rx()'s own OFDM
demodulator is evidently robust enough to the resulting negative-frequency
mirror image. RADE's OFDM carriers are themselves centered at 1500 Hz
(rade_ofdm.c, "middle of SSB passband") entirely inside librade.so -- this
module doesn't need to shift anything to make that true.
"""
from gnuradio import gr, blocks

from pluto_tx import audio_devices

from .base import RxDevice

# Nyquist = 24kHz -- a single fixed choice, not a curated list like the RF
# backends, since there's no RF-bandwidth-vs-throughput tradeoff here. 48000
# (not e.g. 44100) specifically: psk31_decim's rate math (flowgraph.py,
# round(if_rate/PSK31_WORKING_RATE_HZ)) stays an EXACT ratio at 48000
# (48000/24=2000.0) but not at 44100; it's also already proven to work with
# this exact ALSA/PipeWire audio.source() wrapper -- it's config.AUDIO_RATE
# elsewhere in this same flowgraph (the audio OUTPUT sink) and
# audio_devices.py's own default. Was 20_000 (Nyquist 10kHz) until real
# audio-spectrum tuning made that ceiling too low to reach a real signal
# sitting anywhere above 10kHz in the incoming audio.
SAMPLE_RATE_HZ = 48_000

# See the module docstring: real-only input needs roughly double the
# amplitude a genuine complex IQ stream would, per rade_c's own
# RadeAPIUse.md/rade_rx_wav.c.
REAL_INPUT_GAIN = 2.0


class _AudioToComplexSource(gr.hier_block2):
    """audio.source (real) -> 2x gain -> float_to_complex (imag=0), bundled
    behind ONE connectable complex64 output -- matching every other
    RxDevice.build_source()'s single-block contract (devices/base.py) even
    though this backend needs 3 real GNU Radio blocks internally."""

    def __init__(self, sample_rate_hz, device_name):
        gr.hier_block2.__init__(
            self, "audio_to_complex_source",
            gr.io_signature(0, 0, 0),
            gr.io_signature(1, 1, gr.sizeof_gr_complex),
        )
        self.audio_source = audio_devices.open_input_device(int(sample_rate_hz), device_name)
        self.gain = blocks.multiply_const_ff(REAL_INPUT_GAIN)
        self.to_complex = blocks.float_to_complex(1)
        self.connect(self.audio_source, self.gain)
        self.connect(self.gain, self.to_complex)
        self.connect(self.to_complex, self)


class AudioDevice(RxDevice):
    device_type = "audio"
    display_name = "Audio Input"
    connection_kind = "audio_device"
    DEFAULT_CONNECTION = ""  # empty device string -- gr-audio picks the system default

    frequency_range_hz = (0.0, 0.0)  # no RF tuning concept -- gui.py hides freq_spin for this backend
    # Real baseband tuning DOES apply, though -- a narrowband PSK31/RADE/SSB
    # signal can sit at any tone within the incoming ~24kHz audio span, so
    # gui.py shows a dedicated Hz-native tuner (audio_tune_spin) for this
    # range; AdvancedRxFlowgraph shifts it to 0 Hz baseband with a rotator
    # (see flowgraph.py's audio_rotator). Half of SAMPLE_RATE_HZ, i.e. the
    # Nyquist limit of this backend's own sample rate.
    audio_tuning_range_hz = (0.0, SAMPLE_RATE_HZ / 2)
    sample_rate_hz_choices = (SAMPLE_RATE_HZ,)
    default_sample_rate_hz = SAMPLE_RATE_HZ
    default_bandwidth_hz = None
    gain_stages = ()  # no gain concept -- a sound card's own input level isn't controlled here
    supports_agc_mode = False
    agc_modes = ()
    default_gain_mode = None
    # FftProbe's zoom is a real zoom-FFT -- fft_size*zoom FRESH samples are
    # needed per output row (fft_probe.py), so row rate = sample_rate /
    # (fft_size*zoom). At this backend's 48kHz (vs. RF backends' >=1MHz)
    # that degrades far faster for the same zoom number -- capped here to
    # keep the waterfall's row rate above ~5-6 Hz at the default FFT size
    # (48000/(1024*8) = 5.9 Hz) instead of stuttering badly at higher zoom.
    max_waterfall_zoom = 8
    supports_dc_iq_correction = False

    def __init__(self, connection, frequency_hz, sample_rate_hz, bandwidth_hz, buffer_size=None):
        super().__init__(connection, frequency_hz, sample_rate_hz, bandwidth_hz, buffer_size)
        self._source = None

    def build_source(self):
        self._source = _AudioToComplexSource(self.sample_rate_hz, self.connection or "")
        return self._source

    def set_frequency(self, freq_hz):
        """No-op -- no RF to tune. Called harmlessly by AdvancedRxFlowgraph's
        _retune() regardless of backend; frequency_hz is still stored (base
        class __init__) but never acted on."""
        self.frequency_hz = freq_hz

    def set_gain(self, stage_name, value):
        raise AssertionError(f"AudioDevice has no gain stage {stage_name!r} (gain_stages is empty)")

    def read_hw_state(self) -> dict:
        return {}

    @staticmethod
    def probe_with_timeout(connection, timeout_s=5.0):
        """`timeout_s` unused -- opening/closing a local ALSA/PipeWire device
        is near-instant, no network/USB round trip like Pluto's/HackRF's own
        probe needs a timeout for. Real devices are now selectable (see
        scan_devices_with_timeout() below), so unlike the old
        always-typed-manually assumption, a genuinely busy/unavailable
        device is a real possibility -- audio_devices.probe_device() catches
        it before AdvancedRxFlowgraph construction is even attempted, same
        as pluto_tx's Soundcard-mode output probe."""
        return audio_devices.probe_device("input", connection)

    @staticmethod
    def scan_devices_with_timeout(timeout_s=5.0):
        """`timeout_s` unused -- audio_devices.list_input_devices()/
        list_monitor_devices() shell out to `arecord -l`/`pw-dump`, which
        read already-enumerated ALSA/PipeWire state in-process, not a real
        hardware scan (no network/USB probing like Pluto's/HackRF's own
        scan needs a timeout for).

        Real ALSA input devices (System Default first), PipeWire sink
        monitors, and a persistent, qpwgraph-visible pw-loopback node
        (ensure_persistent_input_node(), created lazily on first scan here)
        -- so an external application (or qpwgraph) can feed audio INTO
        this RX app as if it were a real sound card. Exact same enumeration
        this app's own Source combo uses on the pluto_tx side -- see
        pluto_tx/audio_devices.py and pluto_tx/gui.py's source_combo
        construction."""
        devices = audio_devices.list_input_devices()
        devices.update(audio_devices.list_monitor_devices())
        persistent = audio_devices.ensure_persistent_input_node(name="pluto-advanced-rx-input")
        if persistent is not None:
            devices[persistent] = "pluto-advanced-rx Input (qpwgraph)"
        return devices, None
