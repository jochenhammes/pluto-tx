"""AiocPtt (pure serial DTR/RTS PTT logic), the AIOC TX device backend, and the
needs_ptt_control gate in PlutoTxFlowgraph's is_audio_only() early-return branches
(key_ptt()/unkey_ptt()) -- all with a fake serial.Serial, no real AIOC hardware."""
import time
import unittest
from unittest import mock

from gnuradio import blocks

from pluto_tx import aioc_ptt
from pluto_tx import flowgraph as txf
from pluto_tx.devices.aioc import AiocDevice, DEFAULT_ALSA_DEVICE, DEFAULT_SERIAL_PORT, _parse_connection

FM = txf.PlutoTxFlowgraph.MODE_FM
SSB = txf.PlutoTxFlowgraph.MODE_SSB
DIGITEXT = txf.PlutoTxFlowgraph.MODE_DIGITEXT


class FakeSerial:
    """Stand-in for serial.Serial: tracks every dtr/rts write and close() call."""
    instances = []

    def __init__(self, port):
        self.port = port
        self._dtr = False
        self._rts = False
        self.closed = False
        self.dtr_history = []
        self.raise_on_dtr = False
        FakeSerial.instances.append(self)

    @property
    def dtr(self):
        return self._dtr

    @dtr.setter
    def dtr(self, value):
        if self.raise_on_dtr:
            raise OSError("simulated USB CDC-ACM failure")
        self._dtr = value
        self.dtr_history.append(value)

    @property
    def rts(self):
        return self._rts

    @rts.setter
    def rts(self, value):
        self._rts = value

    def close(self):
        self.closed = True


def _patch_serial():
    FakeSerial.instances = []
    return mock.patch.object(aioc_ptt.serial, "Serial", FakeSerial)


class AiocPttTests(unittest.TestCase):
    def test_key_sets_dtr_and_clears_rts(self):
        with _patch_serial():
            ptt = aioc_ptt.AiocPtt("/dev/ttyFAKE", lead_in_s=0.0, tail_out_s=0.0)
            ser = FakeSerial.instances[-1]
            self.assertEqual(ser.port, "/dev/ttyFAKE")
            self.assertFalse(ser.rts)  # cleared at open, before any key() -- see module docstring
            ptt.key()
            self.assertTrue(ser.dtr)
            self.assertFalse(ser.rts)

    def test_on_keyed_runs_before_the_settle_sleep(self):
        with _patch_serial():
            ptt = aioc_ptt.AiocPtt("/dev/ttyFAKE", lead_in_s=0.05, tail_out_s=0.0)
            t0 = time.monotonic()
            seen = []
            ptt.key(on_keyed=lambda: seen.append(time.monotonic() - t0))
            self.assertEqual(len(seen), 1)
            self.assertLess(seen[0], 0.02)  # called right after dtr=True, before the 0.05s settle sleep
            self.assertGreaterEqual(time.monotonic() - t0, 0.045)  # key() itself still blocked for the sleep

    def test_unkey_holds_ptt_for_tail_out_then_releases(self):
        with _patch_serial():
            ptt = aioc_ptt.AiocPtt("/dev/ttyFAKE", lead_in_s=0.0, tail_out_s=0.03)
            ser = FakeSerial.instances[-1]
            ptt.key()
            t0 = time.monotonic()
            ptt.unkey()
            self.assertGreaterEqual(time.monotonic() - t0, 0.025)
            self.assertFalse(ser.dtr)

    def test_force_release_is_immediate_and_idempotent(self):
        with _patch_serial():
            ptt = aioc_ptt.AiocPtt("/dev/ttyFAKE", lead_in_s=0.0, tail_out_s=0.0)
            ser = FakeSerial.instances[-1]
            ptt.key()
            t0 = time.monotonic()
            ptt.force_release()
            self.assertLess(time.monotonic() - t0, 0.01)
            self.assertFalse(ser.dtr)
            ptt.force_release()  # a second call (e.g. signal handler after finally) must not raise
            self.assertFalse(ser.dtr)

    def test_force_release_never_raises_even_if_the_port_is_gone(self):
        with _patch_serial():
            ptt = aioc_ptt.AiocPtt("/dev/ttyFAKE", lead_in_s=0.0, tail_out_s=0.0)
            ser = FakeSerial.instances[-1]
            ptt.key()
            ser.raise_on_dtr = True
            ptt.force_release()  # must not raise -- E-STOP/signal-handler contract

    def test_close_closes_the_serial_port(self):
        with _patch_serial():
            ptt = aioc_ptt.AiocPtt("/dev/ttyFAKE", lead_in_s=0.0, tail_out_s=0.0)
            ser = FakeSerial.instances[-1]
            ptt.close()
            self.assertTrue(ser.closed)


class ParseConnectionTests(unittest.TestCase):
    def test_empty_uses_both_defaults(self):
        self.assertEqual(_parse_connection(""), (DEFAULT_SERIAL_PORT, DEFAULT_ALSA_DEVICE))

    def test_bare_string_is_treated_as_the_serial_port(self):
        self.assertEqual(_parse_connection("/dev/ttyUSB3"), ("/dev/ttyUSB3", DEFAULT_ALSA_DEVICE))

    def test_pipe_separated_splits_both_sides(self):
        self.assertEqual(
            _parse_connection("/dev/ttyACM1|plughw:CARD=Foo,DEV=0"),
            ("/dev/ttyACM1", "plughw:CARD=Foo,DEV=0"),
        )

    def test_missing_side_falls_back_to_its_own_default(self):
        self.assertEqual(_parse_connection("/dev/ttyACM1|"), ("/dev/ttyACM1", DEFAULT_ALSA_DEVICE))
        self.assertEqual(_parse_connection("|plughw:CARD=Foo,DEV=0"), (DEFAULT_SERIAL_PORT, "plughw:CARD=Foo,DEV=0"))


class AiocDeviceTests(unittest.TestCase):
    def test_capability_flags(self):
        self.assertTrue(AiocDevice.needs_ptt_control)
        self.assertTrue(AiocDevice.is_audio_only())
        self.assertFalse(AiocDevice.supports_frequency_correction)

    def test_audio_sink_device_is_only_the_alsa_part(self):
        dev = AiocDevice("/dev/ttyFAKE|plughw:CARD=X,DEV=0", 0.0, AiocDevice.default_sample_rate_hz, None)
        self.assertEqual(dev.audio_sink_device, "plughw:CARD=X,DEV=0")
        self.assertEqual(dev.connection, "/dev/ttyFAKE|plughw:CARD=X,DEV=0")  # raw connection unparsed

    def test_prepare_for_start_opens_once_and_key_unkey_cycles_toggle_dtr(self):
        with _patch_serial():
            dev = AiocDevice("/dev/ttyFAKE|plughw:CARD=X,DEV=0", 0.0, AiocDevice.default_sample_rate_hz, None)
            dev.prepare_for_start()
            ser = FakeSerial.instances[-1]
            self.assertEqual(ser.port, "/dev/ttyFAKE")
            dev.pre_key()
            self.assertTrue(ser.dtr)
            dev.post_unkey()
            self.assertFalse(ser.dtr)
            dev.pre_key()  # a second cycle on the SAME (still open) port
            self.assertTrue(ser.dtr)
            dev.post_unkey()
            self.assertFalse(ser.dtr)
            self.assertEqual(len(FakeSerial.instances), 1)  # never reopened

    def test_force_safe_state_before_prepare_for_start_does_not_raise(self):
        dev = AiocDevice("", 0.0, AiocDevice.default_sample_rate_hz, None)
        dev.force_safe_state()

    def test_force_safe_state_releases_immediately_and_keeps_the_port_open(self):
        with _patch_serial():
            dev = AiocDevice("/dev/ttyFAKE|plughw:CARD=X,DEV=0", 0.0, AiocDevice.default_sample_rate_hz, None)
            dev.prepare_for_start()
            dev.pre_key()
            dev.force_safe_state()
            ser = FakeSerial.instances[-1]
            self.assertFalse(ser.dtr)
            self.assertFalse(ser.closed)  # Re-arm after E-STOP must still be able to key again
            dev.pre_key()
            self.assertTrue(ser.dtr)

    def test_probe_with_timeout_reports_serial_failure(self):
        class _BrokenSerial:
            def __init__(self, port):
                raise OSError("no such device")
        with mock.patch.object(aioc_ptt.serial, "Serial", _BrokenSerial):
            err = AiocDevice.probe_with_timeout("/dev/ttyNOPE|plughw:CARD=X,DEV=0")
            self.assertIsInstance(err, OSError)

    def test_probe_with_timeout_reports_audio_failure(self):
        with _patch_serial(), \
             mock.patch("pluto_tx.devices.aioc.audio_devices.probe_device", return_value=RuntimeError("busy")):
            err = AiocDevice.probe_with_timeout("/dev/ttyFAKE|plughw:CARD=X,DEV=0")
            self.assertIsInstance(err, RuntimeError)

    def test_probe_with_timeout_succeeds(self):
        with _patch_serial(), \
             mock.patch("pluto_tx.devices.aioc.audio_devices.probe_device", return_value=None):
            self.assertIsNone(AiocDevice.probe_with_timeout("/dev/ttyFAKE|plughw:CARD=X,DEV=0"))


class _FakePort:
    def __init__(self, device, description=""):
        self.device = device
        self.description = description
        self.product = ""
        self.manufacturer = ""


class ScanTests(unittest.TestCase):
    def test_pairs_a_single_aioc_serial_and_alsa_card(self):
        ports = [
            _FakePort("/dev/ttyACM0", description="AIOC AllInOneCable"),
            _FakePort("/dev/ttyUSB0", description="Some Other Adapter"),
        ]
        alsa = {
            "": "System Default",
            "plughw:CARD=AllInOneCable,DEV=0": "AllInOneCable (AIOC)",
            "plughw:CARD=PCH,DEV=0": "Built-in Audio",
        }
        with mock.patch("pluto_tx.devices.aioc.serial.tools.list_ports.comports", return_value=ports), \
             mock.patch("pluto_tx.devices.aioc.audio_devices.list_output_devices", return_value=alsa):
            found, err = AiocDevice.scan_devices_with_timeout()
        self.assertIsNone(err)
        self.assertEqual(list(found), ["/dev/ttyACM0|plughw:CARD=AllInOneCable,DEV=0"])

    def test_ambiguous_matches_fall_back_to_separate_candidates(self):
        # Neither port is DEFAULT_SERIAL_PORT here on purpose -- the "audio-only"
        # fallback candidate is keyed as "<DEFAULT_SERIAL_PORT>|<alsa dev>", which
        # would otherwise coincidentally collide with a "<port>|<DEFAULT_ALSA_DEVICE>"
        # serial candidate and silently drop one of the 3 expected entries.
        ports = [_FakePort("/dev/ttyACM5", description="AIOC #1"), _FakePort("/dev/ttyACM6", description="AIOC #2")]
        alsa = {"plughw:CARD=AllInOneCable,DEV=0": "AllInOneCable"}
        with mock.patch("pluto_tx.devices.aioc.serial.tools.list_ports.comports", return_value=ports), \
             mock.patch("pluto_tx.devices.aioc.audio_devices.list_output_devices", return_value=alsa):
            found, err = AiocDevice.scan_devices_with_timeout()
        self.assertIsNone(err)
        self.assertEqual(len(found), 3)  # 2 serial candidates + 1 alsa candidate, no confident 1:1 pairing

    def test_no_aioc_found_returns_empty(self):
        with mock.patch("pluto_tx.devices.aioc.serial.tools.list_ports.comports", return_value=[]), \
             mock.patch("pluto_tx.devices.aioc.audio_devices.list_output_devices", return_value={"": "System Default"}):
            found, err = AiocDevice.scan_devices_with_timeout()
        self.assertEqual(found, {})
        self.assertIsNone(err)


class NeedsPttControlGateTests(unittest.TestCase):
    """The is_audio_only() early-return branches in key_ptt()/unkey_ptt() (FM/RADE/
    Digitext/PSK31/RTTY/POCSAG) must call device.pre_key()/post_unkey() when the
    device declares needs_ptt_control (AIOC), and must NOT when it doesn't
    (Soundcard) -- see devices/base.py's docstring on that flag."""

    @classmethod
    def setUpClass(cls):
        from tests import fakes
        fakes.register_tx()
        fakes.register_fake_audio_tx()

    def _cycle(self, mode, **kwargs):
        fg = txf.PlutoTxFlowgraph(device_type="fake_audio", mode=mode, **kwargs)
        # __init__ itself ends with an unconditional device.post_unkey() (every
        # backend's idle-safety backstop, e.g. Pluto: LO powered down -- see its
        # comment in flowgraph.py) -- reset counters so this test isolates just
        # the key_ptt()/unkey_ptt() cycle below, not construction-time init.
        fg.device.pre_key_calls = 0
        fg.device.post_unkey_calls = 0
        fg.start()
        time.sleep(0.05)
        fg.key_ptt()
        time.sleep(0.05)
        fg.unkey_ptt()
        fg.stop()
        fg.wait()
        return fg.device.pre_key_calls, fg.device.post_unkey_calls

    def test_fm(self):
        self.assertEqual(self._cycle(FM), (1, 1))

    def test_digitext(self):
        self.assertEqual(self._cycle(txf.PlutoTxFlowgraph.MODE_DIGITEXT, digitext_text="hi"), (1, 1))

    def test_psk31(self):
        self.assertEqual(self._cycle(txf.PlutoTxFlowgraph.MODE_PSK31, psk31_text="hi"), (1, 1))

    def test_rtty(self):
        self.assertEqual(self._cycle(txf.PlutoTxFlowgraph.MODE_RTTY, rtty_text="hi"), (1, 1))

    def test_pocsag(self):
        self.assertEqual(self._cycle(txf.PlutoTxFlowgraph.MODE_POCSAG, pocsag_text="DA2JH"), (1, 1))

    @unittest.skipUnless(txf.RADE_AVAILABLE, "RADE not installed")
    def test_rade(self):
        self.assertEqual(self._cycle(txf.PlutoTxFlowgraph.MODE_RADE), (1, 1))

    def test_soundcard_is_not_gated_on(self):
        # SoundcardDevice.needs_ptt_control is False -- the gate itself is
        # data-driven (see devices/base.py), so no instrumentation needed here,
        # just confirming the flag that controls it.
        fg = txf.PlutoTxFlowgraph(device_type="soundcard", mode=FM)
        self.assertFalse(fg.device.needs_ptt_control)


class SharedSoundcardSinkTests(unittest.TestCase):
    """Regression test for a real bug found against real AIOC hardware: an earlier
    version of this fix summed every audio-only branch into one blocks.add_ff --
    a sync block that requires data on EVERY connected input to produce ANY output.
    Digitext/PSK31/RTTY/POCSAG's sources are one-shot (repeat=False) and sit
    exhausted between transmissions, so that adder stalled forever the moment any
    one of them had already run once -- including the continuously-fed FM branch
    (PTT keyed, but literally no audio ever reached the sink; "nur ein Traeger").
    The fix instead reuses the exact producer-map/null_sink swap pattern already
    proven for tx_gain (_tx_gain_producer_map()/set_mode()) -- see
    _soundcard_audio_producer_map()."""

    @classmethod
    def setUpClass(cls):
        from tests import fakes
        fakes.register_tx()
        fakes.register_fake_audio_tx()

    def test_fm_audio_flows_after_a_one_shot_digimode_already_exhausted(self):
        sink = blocks.vector_sink_f()
        with mock.patch("pluto_tx.flowgraph.audio_devices.open_output_device", return_value=sink):
            fg = txf.PlutoTxFlowgraph(device_type="fake_audio", mode=DIGITEXT, digitext_text="hi")
            fg.start()
            time.sleep(0.05)
            fg.key_ptt()             # runs digitext_source to exhaustion (one-shot)
            time.sleep(0.3)
            fg.unkey_ptt()
            fg.set_mode(FM)
            sink.reset()
            fg.key_ptt()             # FM's own audio must still reach the shared sink
            time.sleep(0.3)
            fg.unkey_ptt()
            fg.stop()
            fg.wait()
        self.assertGreater(len(sink.data()), 1000)

    def test_switching_to_an_rf_only_mode_and_back_does_not_break_the_shared_sink(self):
        sink = blocks.vector_sink_f()
        with mock.patch("pluto_tx.flowgraph.audio_devices.open_output_device", return_value=sink):
            fg = txf.PlutoTxFlowgraph(device_type="fake_audio", mode=FM)
            fg.start()
            time.sleep(0.05)
            fg.set_mode(SSB)          # SSB has no audio-only branch at all
            fg.set_mode(FM)           # back to an audio-only-capable mode
            sink.reset()
            fg.key_ptt()
            time.sleep(0.3)
            fg.unkey_ptt()
            fg.stop()
            fg.wait()
        self.assertGreater(len(sink.data()), 1000)


if __name__ == "__main__":
    unittest.main()
