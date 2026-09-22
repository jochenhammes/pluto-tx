"""Fake RF devices so the TX/RX flowgraphs can be built and run without
hardware: the TX fake collects everything sent to the sink, the RX fake
replays a recorded/generated IQ array."""
from gnuradio import gr, blocks

from pluto_advanced_rx import devices as rx_devices
from pluto_advanced_rx.devices.base import RxDevice
from pluto_advanced_rx.devices.pluto import PlutoDevice as RxPluto
from pluto_tx import devices as tx_devices
from pluto_tx.devices.base import PowerStage, TxDevice
from pluto_tx.devices.pluto import PlutoDevice as TxPluto


class FakeTxDevice(TxDevice):
    device_type = "fake"
    display_name = "fake"
    connection_kind = "uri"
    DEFAULT_CONNECTION = "x"
    frequency_range_hz = (1e6, 6e9)
    sample_rate_range_hz = (2_500_000, 2_500_000)
    default_sample_rate_hz = 2_500_000
    default_bandwidth_hz = 200_000
    power_stages = TxPluto.power_stages
    default_power_ceiling = TxPluto.default_power_ceiling
    rf_bandwidth_calls = []
    frequency_calls = []

    def prepare_for_start(self): pass
    def build_sink(self):
        self.sink = blocks.vector_sink_c()
        return self.sink
    def set_frequency(self, hz):
        self.frequency_hz = hz
        FakeTxDevice.frequency_calls.append(hz)
    def set_rf_bandwidth(self, hz): FakeTxDevice.rf_bandwidth_calls.append(hz)
    def set_power(self, name, value): pass
    def pre_key(self): pass
    def post_unkey(self): pass
    def force_safe_state(self): pass
    def read_hw_state(self): return {}
    @staticmethod
    def probe_with_timeout(connection, timeout_s=5): return None
    @staticmethod
    def scan_devices_with_timeout(timeout_s=5): return {}, None


def register_tx():
    tx_devices.DEVICE_REGISTRY["fake"] = FakeTxDevice


class FakeAudioTxDevice(TxDevice):
    """Audio-only fake with instrumented (call-counted) pre_key()/post_unkey() --
    tests PlutoTxFlowgraph's needs_ptt_control gate (devices/base.py, the FM/RADE/
    Digitext/PSK31/RTTY/POCSAG is_audio_only() early-return branches in
    flowgraph.py's key_ptt()/unkey_ptt()) without real AIOC serial hardware."""
    device_type = "fake_audio"
    display_name = "fake audio"
    connection_kind = "audio_device"
    DEFAULT_CONNECTION = ""
    frequency_range_hz = (0.0, 0.0)
    sample_rate_range_hz = (2_500_000, 2_500_000)
    default_sample_rate_hz = 2_500_000
    default_bandwidth_hz = None
    supports_frequency_correction = False
    needs_ptt_control = True
    power_stages = (
        PowerStage(name="none", label="N/A", kind="continuous_db",
                   min_value=0.0, max_value=0.0, unit="", is_primary=True, off_value=0.0),
    )
    default_power_ceiling = 0.0

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.pre_key_calls = 0
        self.post_unkey_calls = 0

    def prepare_for_start(self): pass
    def build_sink(self): return blocks.null_sink(gr.sizeof_gr_complex)
    def set_frequency(self, hz): self.frequency_hz = hz
    def set_power(self, name, value): pass
    def pre_key(self): self.pre_key_calls += 1
    def post_unkey(self): self.post_unkey_calls += 1
    def force_safe_state(self): pass
    def read_hw_state(self): return {}
    @staticmethod
    def probe_with_timeout(connection, timeout_s=5): return None
    @staticmethod
    def scan_devices_with_timeout(timeout_s=5): return {}, None


def register_fake_audio_tx():
    tx_devices.DEVICE_REGISTRY["fake_audio"] = FakeAudioTxDevice


def make_fake_rx(iq, rate):
    class FakeRxDevice(RxPluto):
        device_type = "fake"
        display_name = "fake"
        DEFAULT_CONNECTION = "x"
        default_sample_rate_hz = rate
        _source = None

        def __init__(self, *a, **k): RxDevice.__init__(self, *a, **k)
        def build_source(self): return blocks.vector_source_c(iq.tolist(), False)
        def set_frequency(self, hz): pass
        def set_gain(self, name, value): pass
        def set_gain_mode(self, mode): pass
        def read_hw_state(self): return {}
        @staticmethod
        def probe_with_timeout(connection, timeout_s=5): return None
    rx_devices.DEVICE_REGISTRY["fake"] = FakeRxDevice
    return FakeRxDevice
