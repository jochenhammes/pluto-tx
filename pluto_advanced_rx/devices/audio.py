"""Audio Input RX backend: lets an externally-connected radio (a
conventional SSB transceiver, e.g. receiving RADE audio-injected over its
own RF path) feed this app via a sound card, instead of any SDR hardware.

No RF concept applies here at all -- no frequency, no gain, no device scan.
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
from gnuradio import gr, blocks, audio

from .base import RxDevice

# Nyquist = 10kHz, matching the requested 0-10kHz spectrum/waterfall display
# for this backend -- a single fixed choice, not a curated list like the
# RF backends, since there's no RF-bandwidth-vs-throughput tradeoff here.
SAMPLE_RATE_HZ = 20_000

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
        self.audio_source = audio.source(int(sample_rate_hz), device_name, True)
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

    frequency_range_hz = (0.0, 0.0)  # no RF tuning concept -- gui.py hides freq_spin/fine_slider for this backend
    sample_rate_hz_choices = (SAMPLE_RATE_HZ,)
    default_sample_rate_hz = SAMPLE_RATE_HZ
    default_bandwidth_hz = None
    gain_stages = ()  # no gain concept -- a sound card's own input level isn't controlled here
    supports_agc_mode = False
    agc_modes = ()
    default_gain_mode = None
    supports_dc_iq_correction = False

    def __init__(self, connection, frequency_hz, sample_rate_hz, bandwidth_hz):
        super().__init__(connection, frequency_hz, sample_rate_hz, bandwidth_hz)
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
        """A sound card is treated as always available -- no equivalent of
        libiio's/SoapySDR's reachability check exists for gr-audio."""
        return None

    @staticmethod
    def scan_devices_with_timeout(timeout_s=5.0):
        """No structured device enumeration exists for gnuradio.audio --
        the GUI's existing "0 devices found" handling covers this fine."""
        return {}, None
