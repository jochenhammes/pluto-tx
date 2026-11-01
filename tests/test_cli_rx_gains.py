"""pluto-cli rx: the HackRF's gain stages reach the device, and a gain option a device has no use for is refused."""
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

from pluto_advanced_rx import config as rx_config, devices as rx_devices
from pluto_cli import app, rx as cli_rx


def run(*argv):
    out = io.StringIO()
    with redirect_stdout(out):
        try:
            code = app.main(["rx", "ssb", *argv, "--duration", "0.1", "--json"])
        except SystemExit as e:
            code = e.code
    return code, [json.loads(line) for line in out.getvalue().splitlines() if line.startswith("{")]


def built_with(*argv):
    """The flowgraph's arguments, without any hardware."""
    captured = {}

    class Stop(Exception):
        pass

    def fake_flowgraph(**kwargs):
        captured.update(kwargs)
        raise Stop

    with mock.patch.object(cli_rx, "AdvancedRxFlowgraph") as flowgraph, \
            mock.patch.object(cli_rx.runtime, "probe_or_exit"), mock.patch.object(cli_rx.runtime, "resolve_connection", return_value="x"), \
            mock.patch.object(cli_rx.runtime, "build_or_exit", side_effect=lambda build, *a: build()):
        flowgraph.side_effect = fake_flowgraph
        flowgraph.MODE_SSB = 1
        with mock.patch.dict(cli_rx.__dict__, {"AdvancedRxFlowgraph": flowgraph}):
            try:
                run(*argv)
            except Stop:
                pass
    return captured


class HackrfStageTests(unittest.TestCase):
    def test_stages_reach_the_flowgraph(self):
        kw = built_with("--device", "hackrf", "--lna-gain", "40", "--vga-gain", "30", "--rf-amp")
        self.assertEqual(kw["gain_values"], {"LNA": 40.0, "VGA": 30.0, "AMP": True})

    def test_nothing_given_leaves_the_device_defaults(self):
        self.assertEqual(built_with("--device", "hackrf")["gain_values"], {})

    def test_rf_amp_only_on_request(self):
        self.assertNotIn("AMP", built_with("--device", "hackrf", "--lna-gain", "8")["gain_values"])

    def test_out_of_range_or_off_step_is_refused(self):
        for option, value in (("--lna-gain", "48"), ("--lna-gain", "12"), ("--lna-gain", "-8"), ("--vga-gain", "64"), ("--vga-gain", "31")):
            code, events = run("--device", "hackrf", option, value)
            self.assertEqual(code, 1, (option, value))
            self.assertIn("steps of", json.dumps(events))

    def test_gain_without_effect_is_refused(self):
        for argv in (("--gain", "30"), ("--gain-mode", "manual")):
            code, events = run("--device", "hackrf", *argv)
            self.assertEqual(code, 1)
            self.assertIn("--lna-gain", json.dumps(events))


class OtherDeviceTests(unittest.TestCase):
    def test_hackrf_options_are_refused_elsewhere(self):
        for argv in (("--lna-gain", "16"), ("--vga-gain", "16"), ("--rf-amp",)):
            code, events = run("--device", "rtlsdr", *argv)
            self.assertEqual(code, 1, argv)
            self.assertIn("not available", json.dumps(events))

    def test_manual_gain_still_reaches_the_rtl(self):
        kw = built_with("--device", "rtlsdr", "--gain-mode", "manual", "--gain", "30")
        self.assertEqual((kw["gain_mode"], kw["manual_gain_db"], kw["gain_values"]), ("manual", 30.0, {}))

    def test_defaults_are_unchanged(self):
        kw = built_with("--device", "rtlsdr")
        self.assertEqual((kw["gain_mode"], kw["manual_gain_db"]), (rx_config.DEFAULT_GAIN_MODE, rx_config.DEFAULT_MANUAL_GAIN_DB))

    def test_every_device_declares_what_the_check_reads(self):
        for cls in rx_devices.DEVICE_REGISTRY.values():
            self.assertIsNotNone(cls.gain_stages)


if __name__ == "__main__":
    unittest.main()
