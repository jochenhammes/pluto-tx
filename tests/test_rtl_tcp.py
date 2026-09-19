"""rtl_tcp client tests against tests/fake_rtl_tcp.py. Run: python3 -m unittest discover tests"""
import time
import unittest

import numpy as np

from pluto_advanced_rx import rtl_tcp
from tests.fake_rtl_tcp import FakeRtlTcpServer


def wait_for(cond, timeout=5.0):
    t = time.time()
    while time.time() - t < timeout:
        if cond():
            return True
        time.sleep(0.02)
    return False


class ParseConnectionTests(unittest.TestCase):
    def test_serials_and_blank_are_local(self):
        for text in ("", "  ", "00000001", "mydongle", "driver=rtlsdr,serial=1"):
            self.assertIsNone(rtl_tcp.parse_connection(text), text)

    def test_network_forms(self):
        p = rtl_tcp.parse_connection
        self.assertEqual(p("192.168.178.34:1234"), ("192.168.178.34", 1234))
        self.assertEqual(p("192.168.178.34"), ("192.168.178.34", 1234))
        self.assertEqual(p("radio.local:5555"), ("radio.local", 5555))
        self.assertEqual(p("rtl_tcp://pi"), ("pi", 1234))
        self.assertEqual(p("tcp:raspberrypi:7373"), ("raspberrypi", 7373))

    def test_bad_network_forms(self):
        for text in ("1.2.3.4:99999", "1.2.3.4:abc", ":1234", "tcp:"):
            with self.assertRaises(ValueError, msg=text):
                rtl_tcp.parse_connection(text)


class ProbeTests(unittest.TestCase):
    def test_probe_ok_and_failures(self):
        srv = FakeRtlTcpServer()
        try:
            self.assertIsNone(rtl_tcp.probe("127.0.0.1", srv.port, 2.0))
        finally:
            srv.close()
        bad = FakeRtlTcpServer(magic=b"XXXX")
        try:
            err = rtl_tcp.probe("127.0.0.1", bad.port, 2.0)
            self.assertIn("not an rtl_tcp server", str(err))
        finally:
            bad.close()
        silent = FakeRtlTcpServer(send_header=False)
        try:
            self.assertIn("no rtl_tcp header", str(rtl_tcp.probe("127.0.0.1", silent.port, 0.5)))
        finally:
            silent.close()
        self.assertIsNotNone(rtl_tcp.probe("127.0.0.1", 1, 0.5))  # nothing listens on port 1


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.srv = FakeRtlTcpServer(tone_hz=100_000.0)
        self.addCleanup(self.srv.close)

    def test_header_commands_and_stream(self):
        src = rtl_tcp.RtlTcpSource("127.0.0.1", self.srv.port, sample_rate_hz=1_024_000)
        self.addCleanup(src.client.close)
        src.set_frequency(433_775_000)
        src.set_gain_mode(False)
        src.set_gain(20.0)
        self.assertTrue(wait_for(lambda: src.status == "connected"))
        self.assertEqual(src.client.tuner_type, 5)
        self.assertTrue(wait_for(lambda: self.srv.last_param(rtl_tcp.CMD_SET_GAIN) == 200))
        self.assertTrue(wait_for(lambda: self.srv.last_param(rtl_tcp.CMD_SET_FREQ) == 433_775_000), "rtl_tcp.CMD_SET_FREQ not seen")
        self.assertTrue(wait_for(lambda: self.srv.last_param(rtl_tcp.CMD_SET_SAMPLE_RATE) == 1_024_000), "rtl_tcp.CMD_SET_SAMPLE_RATE not seen")
        self.assertTrue(wait_for(lambda: self.srv.last_param(rtl_tcp.CMD_SET_GAIN_MODE) == 1), "rtl_tcp.CMD_SET_GAIN_MODE not seen")  # manual
        self.assertTrue(wait_for(lambda: self.srv.last_param(rtl_tcp.CMD_SET_AGC_MODE) == 0), "rtl_tcp.CMD_SET_AGC_MODE not seen")
        src.set_gain_mode(True)
        self.assertTrue(wait_for(lambda: self.srv.last_param(rtl_tcp.CMD_SET_GAIN_MODE) == 0))
        self.assertTrue(wait_for(lambda: self.srv.last_param(rtl_tcp.CMD_SET_AGC_MODE) == 1))  # sent right after 0x03

        # stream -> complex conversion: the tone lands in the right FFT bin
        got = np.empty(0, np.complex64)
        out = [np.zeros(8192, np.complex64)]
        t = time.time()
        while len(got) < 65536 and time.time() - t < 5:
            n = src.work([], out)
            got = np.concatenate([got, out[0][:n]])
        self.assertGreaterEqual(len(got), 65536)
        got = got[:65536]
        self.assertLess(np.abs(got).max(), 1.0)
        spec = np.abs(np.fft.fft(got * np.hanning(len(got))))
        peak_hz = np.fft.fftfreq(len(got), 1 / 1_024_000)[spec.argmax()]
        self.assertAlmostEqual(peak_hz, 100_000.0, delta=100.0)

    def test_reconnect_replays_settings(self):
        src = rtl_tcp.RtlTcpSource("127.0.0.1", self.srv.port, sample_rate_hz=1_024_000)
        self.addCleanup(src.client.close)
        src.set_frequency(100_000_000)
        src.set_gain_mode(False)
        src.set_gain(30.0)
        self.assertTrue(wait_for(lambda: src.status == "connected"))
        self.assertTrue(wait_for(lambda: self.srv.last_param(rtl_tcp.CMD_SET_GAIN) == 300))
        self.srv.commands.clear()
        self.srv.drop_client()
        self.assertTrue(wait_for(lambda: self.srv.connections >= 2 and src.status == "connected", 8.0))
        self.assertTrue(wait_for(lambda: self.srv.last_param(rtl_tcp.CMD_SET_GAIN) == 300))
        self.assertTrue(wait_for(lambda: self.srv.last_param(rtl_tcp.CMD_SET_FREQ) == 100_000_000), "rtl_tcp.CMD_SET_FREQ not seen")
        self.assertGreaterEqual(src.client.reconnects, 1)

    def test_slow_consumer_drops_oldest_and_counts(self):
        client = rtl_tcp.RtlTcpClient("127.0.0.1", self.srv.port)  # nobody reads -> the buffer cap is hit
        self.addCleanup(client.close)
        self.assertTrue(wait_for(lambda: client.dropped_bytes > 0, 8.0))

    def test_close_frees_the_single_client_server(self):
        first = rtl_tcp.RtlTcpClient("127.0.0.1", self.srv.port)
        self.assertTrue(wait_for(lambda: first.status == "connected"))
        second = rtl_tcp.RtlTcpClient("127.0.0.1", self.srv.port)  # waits in the backlog
        self.addCleanup(second.close)
        time.sleep(0.5)
        self.assertNotEqual(second.status, "connected")
        first.close()
        self.assertTrue(wait_for(lambda: second.status == "connected", 8.0))


class JitterBufferTests(unittest.TestCase):
    def _consume(self, client, seconds, rate):
        """Read like an audio-paced flowgraph: fixed slices in real time. Returns
        (received bytes, empty-read polls after the initial fill)."""
        got = empty = 0
        t_end = time.monotonic() + seconds
        started = False
        while time.monotonic() < t_end:
            d = client.read(2 * int(rate * 0.02), timeout_s=0.05)
            if d:
                started = True
                got += len(d)
                time.sleep(0.02)  # the sink drains 20 ms of stream per slice, in real time
            elif started:
                empty += 1
        return got, empty

    def test_bursty_server_is_smoothed(self):
        # 200 kS/s in 0.32 s bursts (128 kB) -- the shape of a real rtl_tcp at low rates
        srv = FakeRtlTcpServer(burst_bytes=131_072)
        self.addCleanup(srv.close)
        srv.sample_rate = 200_000
        client = rtl_tcp.RtlTcpClient("127.0.0.1", srv.port, sample_rate_hz=200_000)
        self.addCleanup(client.close)
        client.send_command(rtl_tcp.CMD_SET_SAMPLE_RATE, 200_000)
        self.assertTrue(wait_for(lambda: client.status == "connected"))
        self._consume(client, 8.0, 200_000)  # let the buffer adapt to the burst size
        self.assertGreater(client.target_s, 0.3)  # target grew beyond the minimum
        u0 = client.underruns
        got, empty = self._consume(client, 6.0, 200_000)
        self.assertEqual(client.underruns, u0, "still underrunning after adapting")
        self.assertEqual(empty, 0)
        self.assertGreater(got, 0.8 * 2 * 200_000 * 6.0)  # real-time throughput preserved

    def test_target_follows_stalls_and_is_capped(self):
        client = rtl_tcp.RtlTcpClient("127.0.0.1", 1)  # never connects; only the arithmetic is used
        self.addCleanup(client.close)
        client._note_gap(0.5, 100.0)
        self.assertAlmostEqual(client.target_s, 0.85)
        client._note_gap(10.0, 101.0)
        self.assertEqual(client.target_s, rtl_tcp.MAX_TARGET_S)
        client._note_gap(0.001, 200.0)  # the old stalls fall out of the 30 s window
        self.assertEqual(client.target_s, rtl_tcp.MIN_TARGET_S)


class DeviceAndFlowgraphTests(unittest.TestCase):
    def test_rtlsdr_device_network_probe_and_state(self):
        from pluto_advanced_rx.devices.rtlsdr import RtlSdrDevice
        srv = FakeRtlTcpServer()
        self.addCleanup(srv.close)
        conn = f"127.0.0.1:{srv.port}"
        self.assertIsNone(RtlSdrDevice.probe_with_timeout(conn, 2.0))
        self.assertIsNotNone(RtlSdrDevice.probe_with_timeout("127.0.0.1:1", 0.5))
        self.assertIsInstance(RtlSdrDevice.probe_with_timeout("1.2.3.4:99999", 0.5), ValueError)

    def test_flowgraph_receives_the_tone_over_rtl_tcp(self):
        from pluto_advanced_rx import flowgraph as rxf
        srv = FakeRtlTcpServer(tone_hz=150_000.0)
        self.addCleanup(srv.close)
        fg = rxf.AdvancedRxFlowgraph(uri=f"127.0.0.1:{srv.port}", frequency=100_000_000.0,
                                     sample_rate=1_024_000, device_type="rtlsdr", fft_size=1024)
        fg.start()
        try:
            self.assertTrue(wait_for(lambda: fg.device.connection_status() == "connected"))
            row, gen = None, -1
            t = time.time()
            while row is None and time.time() - t < 6:
                time.sleep(0.1)
                row, gen = fg.fft_probe.get_latest_row(gen)
            self.assertIsNotNone(row, "no FFT row -- no samples reached the flowgraph")
            freqs = np.fft.fftshift(np.fft.fftfreq(len(row), 1 / 1_024_000))
            self.assertAlmostEqual(freqs[int(np.argmax(row))], 150_000.0, delta=2_000.0)
            state = fg.device.read_hw_state()
            self.assertIn("connected", state["rtl_tcp"])
            self.assertEqual(state["tuner"], "R820T")
        finally:
            fg.shutdown()
        self.assertTrue(wait_for(lambda: srv._client is None, 5.0))  # shutdown() released the single-client server


if __name__ == "__main__":
    unittest.main()
