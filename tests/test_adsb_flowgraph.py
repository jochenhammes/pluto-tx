"""ADS-B through the real RX flowgraph and CLI (GNU Radio blocks, fake device replaying synthetic IQ)."""
import contextlib
import io
import json
import signal
import sys
import unittest

import numpy as np

from pluto_advanced_rx import config
from pluto_advanced_rx.adsb_state import AdsbState
from pluto_tx import adsb_codec as c
from tests import adsb_testgen as gen

ICAO = 0x3C6444


def scene(rate, spike=0.0, seed=1):
    rng = np.random.default_rng(seed)
    spu = rate / 1e6
    parts = [np.zeros(int(0.03 * rate), np.complex128)]
    messages = [gen.callsign_message(ICAO, "DLH123"), gen.position_message(ICAO, 53.5, 9.5, 35000, 0),
                gen.position_message(ICAO, 53.5, 9.5, 35000, 1), gen.velocity_message(ICAO, 300, 400, -640)]
    for m in messages:
        parts += [gen.iq(m, rate, phase_rad=float(rng.uniform(0, 6)), delay_us=float(rng.random())), np.zeros(int(300 * spu), np.complex128)]
    x = np.concatenate(parts + [np.zeros(int(0.03 * rate), np.complex128)])
    x = x + 0.03 * (rng.standard_normal(len(x)) + 1j * rng.standard_normal(len(x))) + spike
    return x.astype(np.complex64)


def run(iq, rate):
    from pluto_advanced_rx import flowgraph as rxf
    from tests import fakes
    fakes.make_fake_rx(iq, rate)
    state = AdsbState()
    rx = rxf.AdvancedRxFlowgraph(uri="x", frequency=config.ADSB_FREQ_HZ, sample_rate=rate, device_type="fake",
                                 active_digimode="adsb", on_adsb_message=state.on_message)
    rx.start()
    rx.wait()
    rx.adsb_deframer.detector.flush()
    return state, rx


class FlowgraphTests(unittest.TestCase):
    def check(self, state):
        a = state.get_aircraft()
        self.assertEqual(len(a), 1)
        a = a[0]
        self.assertEqual(a["icao"], "3C6444")
        self.assertEqual(a["callsign"], "DLH123")
        self.assertEqual(a["alt_ft"], 35000)
        self.assertAlmostEqual(a["lat"], 53.5, delta=0.001)
        self.assertAlmostEqual(a["lon"], 9.5, delta=0.001)
        self.assertAlmostEqual(a["speed_kt"], 500, delta=1)
        self.assertEqual(a["vrate_fpm"], -640)
        self.assertAlmostEqual(a["track_deg"], 36.87, delta=0.5)

    def test_full_aircraft_at_each_device_rate(self):
        for rate in (2_400_000, 2_500_000, 4_000_000, 5_000_000):
            with self.subTest(rate=rate):
                self.check(run(scene(rate), rate)[0])

    def test_dc_spike_is_removed(self):
        self.check(run(scene(2_400_000, spike=1.0), 2_400_000)[0])

    def test_too_low_rate_is_refused(self):
        from pluto_advanced_rx import flowgraph as rxf
        from tests import fakes
        fakes.make_fake_rx(np.zeros(100, np.complex64), 2_000_000)
        with self.assertRaisesRegex(ValueError, "too low for ADS-B"):
            rxf.AdvancedRxFlowgraph(uri="x", frequency=config.ADSB_FREQ_HZ, sample_rate=2_000_000, device_type="fake", active_digimode="adsb")

    def test_other_modes_do_not_wire_it(self):
        from pluto_advanced_rx import flowgraph as rxf
        from tests import fakes
        fakes.make_fake_rx(np.zeros(100, np.complex64), 2_500_000)
        self.assertIsNone(rxf.AdvancedRxFlowgraph(uri="x", frequency=433e6, sample_rate=2_500_000, device_type="fake").adsb_deframer)

    def test_no_transmit_path_exists(self):
        import pluto_cli.tx as tx
        import pluto_tx.flowgraph as txf
        for module in (tx, txf):
            with open(module.__file__, encoding="utf-8") as f:
                self.assertNotIn("adsb", f.read().lower())


def run_cli(argv):
    from pluto_cli import app
    out = io.StringIO()
    saved = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM), sys.excepthook)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                code = app.main(argv)
            except SystemExit as e:
                code = e.code
    finally:
        signal.signal(signal.SIGINT, saved[0])
        signal.signal(signal.SIGTERM, saved[1])
        sys.excepthook = saved[2]
    events = []
    for line in out.getvalue().splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    return code, events


class CliTests(unittest.TestCase):
    def test_aircraft_as_json_events_and_silent(self):
        from pluto_advanced_rx import flowgraph as rxf
        from tests import fakes
        from unittest import mock
        rate = 2_500_000
        fakes.make_fake_rx(np.concatenate([scene(rate), np.zeros(rate, np.complex64)]), rate)
        with mock.patch.object(rxf.AdvancedRxFlowgraph, "set_rx_muted") as muted:
            code, events = run_cli(["rx", "fm", "--device", "fake", "--uri", "x", "--freq", "433000000", "--digimode", "adsb",
                                    "--adsb-locator", "JO43", "--duration", "2.5", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(next(e for e in events if e["event"] == "adsb_listen")["freq_hz"], config.ADSB_FREQ_HZ)
        aircraft = [e for e in events if e["event"] == "adsb_aircraft"]
        self.assertGreaterEqual(len(aircraft), 1)
        self.assertEqual(aircraft[-1]["icao"], "3C6444")
        self.assertEqual(aircraft[-1]["callsign"], "DLH123")
        self.assertNotIn("trail", aircraft[-1])
        self.assertNotIn(mock.call(False), muted.call_args_list)

    def test_refusals(self):
        from tests import fakes
        fakes.make_fake_rx(np.zeros(100, np.complex64), 2_500_000)
        code, events = run_cli(["rx", "fm", "--device", "fake", "--uri", "x", "--digimode", "adsb", "--bandwidth", "1000000", "--json"])
        self.assertEqual(code, 1)
        self.assertIn("needs at least", json.dumps(events))
        code, events = run_cli(["rx", "fm", "--device", "fake", "--uri", "x", "--digimode", "adsb", "--adsb-locator", "XX99", "--json"])
        self.assertEqual(code, 1)
        self.assertIn("invalid --adsb-locator", json.dumps(events))


class LocatorTests(unittest.TestCase):
    def test_centers(self):
        self.assertEqual(c.locator_center("JO43"), (53.5, 9.0))
        self.assertEqual(c.locator_center(""), None)
        self.assertEqual(c.locator_center("XX99"), None)


if __name__ == "__main__":
    unittest.main()
