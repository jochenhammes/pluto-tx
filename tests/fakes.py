"""Fake RF devices so the TX/RX flowgraphs can be built and run without
hardware: the TX fake collects everything sent to the sink, the RX fake
replays a recorded/generated IQ array."""
from gnuradio import blocks

from pluto_advanced_rx import devices as rx_devices
from pluto_advanced_rx.devices.base import RxDevice
from pluto_advanced_rx.devices.pluto import PlutoDevice as RxPluto
from pluto_tx import devices as tx_devices
from pluto_tx.devices.base import TxDevice
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


def make_fake_rx(iq, rate):
    class FakeRxDevice(RxPluto):
        device_type = "fake"
        display_name = "fake"
        DEFAULT_CONNECTION = "x"
        default_sample_rate_hz = rate
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
