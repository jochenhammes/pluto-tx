"""The ESP32-S3 TX device (pluto_tx/devices/espdr.py), without hardware: the chip is espdr.sim.SimTx, a simulated ESP32-S3 that plays the
records it is sent in virtual time (real time here: speed 1)."""
import logging
import math
import os
import tempfile
import time
import unittest
import wave
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from pluto_tx import config, devices
from pluto_tx import flowgraph as txf
from pluto_tx.flowgraph import PlutoTxFlowgraph as FG

Q4_HZ = 30e6 / 65536 / 16                 # one unit of the frequency field of a record

espdr_available = devices.ESPDR_AVAILABLE
if espdr_available:
    from espdr import config as espdr_config, nb, sim, txlink
    from pluto_tx.devices import espdr as dev


def tone_wav(path, seconds=3.0, rate=8000):
    t = np.arange(int(seconds * rate)) / rate
    x = 0.4 * np.sin(2 * np.pi * (500 + 300 * np.sin(2 * np.pi * 1.1 * t)) * t) + 0.2 * np.sin(2 * np.pi * 1700 * t)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((x * 20000).astype("<i2").tobytes())
    return path


def record_freq_hz(rec):
    return (rec & 0xFFFF).astype(np.uint16).view(np.int16).astype(float) * Q4_HZ


@unittest.skipUnless(espdr_available, "the espdr package (./espdr) is not importable here")
class EspdrDeviceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wav = tone_wav(os.path.join(self.tmp.name, "tone.wav"))
        self.esp = sim.SimTx(speed=1.0)
        # no board: the chip is the simulator, and the licence notice counts as read
        p = mock.patch.object(dev.EspdrDevice, "_open_link", lambda s: setattr(s, "_link", nb.Link(self.esp)))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(espdr_config, "licence_accepted", lambda: True)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(espdr_config, "board_model", lambda bridge=None: None)       # no thermal model of a real board here
        p.start()
        self.addCleanup(p.stop)

    def flowgraph(self, mode, power_ceiling=None):
        fg = FG(device_type="espdr", connection="", frequency=2.345e9, mode=mode, source=FG.SRC_FILE, wav_path=self.wav, m17_src_callsign="DA2JH",
                power_ceiling=power_ceiling)
        self.addCleanup(fg.shutdown_safe)
        return fg

    def transmit(self, fg, seconds):
        fg.start()
        time.sleep(0.3)
        fg.key_ptt()
        time.sleep(seconds)
        fg.unkey_ptt()
        if fg.mode == FG.MODE_M17:
            time.sleep(0.5)
            fg.finish_unkey_m17()
        fg.stop()
        fg.wait()
        return np.array(self.esp.played, dtype=np.uint32)

    # ---- the contract
    def test_registered_with_one_primary_stage_and_the_13cm_range(self):
        cls = devices.DEVICE_REGISTRY["espdr"]
        self.assertEqual(cls.frequency_range_hz, (2_320_000_000, 2_450_000_000))
        self.assertEqual(sum(s.is_primary for s in cls.power_stages), 1)
        d = devices.build_device("espdr", "", 2.345e9)
        self.assertEqual(d.sample_rate_hz, 80_000)
        self.assertEqual(d.primary_stage.name, "gain")
        self.assertFalse(cls.is_audio_only())
        self.assertEqual(config.in_amateur_band(2.345e9), "13cm")
        self.assertLessEqual(cls.default_power_ceiling, 0.0)

    def test_only_constant_envelope_modes_are_supported(self):
        cls = devices.DEVICE_REGISTRY["espdr"]
        self.assertTrue(cls.supports_mode("fm") and cls.supports_mode("m17"))
        for name in ("ssb", "lsb", "freedv", "rade", "psk31", "ft8", "js8", "digitext", "meshtastic", "meshcore", "filebroadcast", "pocsag"):
            self.assertFalse(cls.supports_mode(name), name)
        for kind in ("pluto", "hackrf", "soundcard", "aioc"):
            if kind in devices.DEVICE_REGISTRY:
                self.assertTrue(devices.DEVICE_REGISTRY[kind].supports_mode("ssb"))

    def test_the_mode_names_cover_every_mode_constant(self):
        constants = {v for k, v in vars(FG).items() if k.startswith("MODE_") and k != "MODE_NAMES"}
        self.assertEqual(constants, set(FG.MODE_NAMES))
        self.assertEqual(len(set(FG.MODE_NAMES.values())), len(FG.MODE_NAMES))

    def test_the_connection_string(self):
        self.assertEqual(dev._parse_connection(""), (None, None))
        self.assertEqual(dev._parse_connection("/dev/ttyACM1"), ("/dev/ttyACM1", None))
        self.assertEqual(dev._parse_connection("/dev/ttyACM1,/dev/ttyACM0"), ("/dev/ttyACM1", "/dev/ttyACM0"))
        self.assertEqual(dev._parse_connection(",/dev/ttyACM0"), (None, "/dev/ttyACM0"))

    def test_the_power_is_clamped_and_the_frequency_checked(self):
        d = devices.build_device("espdr", "", 2.345e9)
        d.set_power("gain", 5.0)
        self.assertEqual(d._power_db, 0.0)
        d.set_power("gain", -99.0)
        self.assertEqual(d._power_db, -17.9)
        with self.assertRaises(ValueError):
            d.set_frequency(432.15e6)
        d.set_frequency(2.4e9)
        self.assertEqual(d.frequency_hz, 2.4e9)

    def test_probe_and_scan_without_a_board(self):
        with mock.patch.object(dev.board, "native_ports", lambda: []), mock.patch.object(dev.board, "bridge_ports", lambda: []):
            self.assertIsInstance(dev.EspdrDevice.probe_with_timeout("", 1.0), Exception)
        with mock.patch.object(dev.board, "native_ports", lambda: []), mock.patch.object(dev.board, "bridge_ports", lambda: ["/dev/ttyACM0"]):
            self.assertIsNone(dev.EspdrDevice.probe_with_timeout("", 1.0))
        found, err = dev.EspdrDevice.scan_devices_with_timeout(1.0)
        self.assertIsNone(err)
        self.assertIsInstance(found, dict)

    def test_nothing_to_end_and_safe_state_never_raises(self):
        d = devices.build_device("espdr", "", 2.345e9)
        d.post_unkey()
        d.force_safe_state()
        d.force_safe_state()
        self.assertEqual(d.read_hw_state()["keyed"], False)

    def test_without_the_licence_notice_the_device_does_not_start(self):
        with mock.patch.object(espdr_config, "licence_accepted", lambda: False):
            d = devices.build_device("espdr", "", 2.345e9)
            with self.assertRaises(RuntimeError) as e:
                d.prepare_for_start()
        self.assertIn("amateur radio licence", str(e.exception))

    # ---- the sink
    def test_the_sink_runs_at_the_real_time_rate_while_nothing_is_sent(self):
        d = devices.build_device("espdr", "", 2.345e9)
        sink = d.build_sink()
        block = [np.zeros(8000, dtype=np.complex64)]                      # 0.1 s at 80 kHz
        t0 = time.monotonic()
        for _ in range(4):
            self.assertEqual(sink.work(block, None), 8000)
        self.assertGreater(time.monotonic() - t0, 0.3)                    # not swallowed at once
        self.assertLess(time.monotonic() - t0, 0.6)

    # ---- keying against the simulated chip
    def test_fm_through_the_flowgraph_reaches_the_chip(self):
        fg = self.flowgraph(FG.MODE_FM)
        self.assertEqual(fg.mode, FG.MODE_FM)
        rec = self.transmit(fg, 2.0)
        self.assertGreater(len(rec), 60_000)                              # 2 s of records and the ring
        self.assertTrue((rec[-1] >> 24) & 1)                              # the end record
        ops = [op for op, _ in self.esp.ops]
        self.assertIn(txlink.TX_OP_BEGIN, ops)
        self.assertLess(ops.index(txlink.TX_OP_LO), ops.index(txlink.TX_OP_BEGIN))
        f = record_freq_hz(rec[:-1])
        body = f[10_000:-5_000]
        self.assertLess(abs(np.median(body) - np.median(f)), 4000)
        self.assertGreater(np.abs(body - np.median(body)).max(), 1500)    # FM deviation of the default voice setting
        self.assertLess(np.abs(body - np.median(body)).max(), 4000)
        codes = {int(r >> 16) & 0xFF for r in rec}
        self.assertTrue(codes <= set(range(64, 128)))

    def middle_code(self, rec):
        codes = [int(r >> 16) & 0xFF for r in rec[:-1]]
        return codes[len(codes) // 2]

    def test_the_gain_follows_the_power_setting_up_to_the_ceiling(self):
        from espdr import txmodes
        fg = self.flowgraph(FG.MODE_FM, power_ceiling=0.0)
        fg.set_target_power(-6.0)
        self.assertEqual(self.middle_code(self.transmit(fg, 1.5)), txmodes.peak_code(-6.0))

    def test_the_default_power_is_the_conservative_ceiling(self):
        from espdr import txmodes
        self.assertEqual(self.middle_code(self.transmit(self.flowgraph(FG.MODE_FM), 1.5)),
                         txmodes.peak_code(dev.EspdrDevice.default_power_ceiling))

    def test_the_ppm_correction_moves_the_signal(self):
        fg = self.flowgraph(FG.MODE_FM)
        fg.device.set_frequency_correction_ppm(5.0)
        self.transmit(fg, 1.0)
        lo_ops = [a for op, a in self.esp.ops if op == txlink.TX_OP_LO]
        plain = txlink.plan_lo(2.345e9, 0.0, 4000.0)["lo_hz"]
        corrected = txlink.plan_lo(2.345e9, 5.0, 4000.0)["lo_hz"]
        self.assertEqual(lo_ops[-1], corrected // 100)
        self.assertNotEqual(plain, corrected)

    def test_a_second_pre_key_does_nothing_and_the_frequency_waits_for_the_next_key(self):
        fg = self.flowgraph(FG.MODE_FM)
        fg.start()
        fg.key_ptt()
        d = fg.device
        before = d._active
        d.pre_key()
        self.assertIs(d._active, before)
        with self.assertLogs("pluto_tx.devices.espdr", level="WARNING"):
            d.set_frequency(2.4e9)
        self.assertEqual(d.frequency_hz, 2.4e9)
        self.assertEqual(before.plan["center_hz"] // 1_000_000, 2345)       # still the old one on the air
        fg.unkey_ptt()
        fg.stop()
        fg.wait()

    def test_an_unsupported_mode_falls_back_to_fm(self):
        fg = FG(device_type="espdr", connection="", frequency=2.345e9, mode=FG.MODE_SSB, source=FG.SRC_FILE, wav_path=self.wav)
        self.addCleanup(fg.shutdown_safe)
        self.assertEqual(fg.mode, FG.MODE_FM)

    def test_the_cli_refuses_modes_the_device_cannot_carry(self):
        from pluto_cli import tx as cli_tx
        messages = []

        class Emitter:
            def error(self, message, **kw):
                messages.append(message)

        args = mock.Mock(device="espdr", uri="")
        rc = cli_tx._run(args, FG.MODE_SSB, Emitter())
        self.assertEqual(rc, 1)
        self.assertIn("cannot send ssb", messages[0])
        self.assertIn("fm, m17", messages[0])

    # ---- M17
    @unittest.skipUnless(txf.M17_AVAILABLE, "gr-m17 is not available")
    def test_m17_through_the_flowgraph_decodes_with_the_call_sign(self):
        from gnuradio import analog, blocks, digital, gr
        from gnuradio import m17 as gr_m17
        from gnuradio.filter import firdes, fir_filter_fff
        from espdr import dsp
        from pluto_advanced_rx.m17_deframer import M17FieldsDeframer

        fg = self.flowgraph(FG.MODE_M17)
        self.assertEqual(fg.mode, FG.MODE_M17)
        rec = self.transmit(fg, 4.5)
        f = record_freq_hz(rec[:-1])
        f = f - np.median(f)
        iq = np.exp(2j * np.pi * np.cumsum(f) / 40_000)
        iq48 = dsp.Resampler(40_000, 48_000).process(iq).astype(np.complex64)
        found = []
        tb = gr.top_block()
        src = blocks.vector_source_c(iq48.tolist(), False)
        demod = analog.quadrature_demod_cf(config.M17_BASEBAND_RATE / (2 * math.pi * config.M17_DEVIATION_HZ))
        n_avg = int(0.1 * config.M17_BASEBAND_RATE)
        avg, sub = blocks.moving_average_ff(n_avg, 1.0 / n_avg, 4000), blocks.sub_ff()
        mf = fir_filter_fff(1, firdes.root_raised_cosine(1.0, config.M17_BASEBAND_RATE, config.M17_SYMBOL_RATE, config.M17_RRC_ALPHA,
                                                         config.M17_RRC_NTAPS))
        sync = digital.symbol_sync_ff(digital.TED_GARDNER, config.M17_RRC_SPS, 2 * math.pi * 0.0015, 1.0, 1.0, 0.05, 1,
                                      digital.constellation_bpsk().base(), digital.IR_MMSE_8TAP, 128, [])
        decoder = gr_m17.m17_decoder(False, False, 2.0, 30.0, True, False, 0, "", "")
        deframer = M17FieldsDeframer(lambda fields: found.append(fields))
        voice = blocks.null_sink(gr.sizeof_short)
        tb.connect(src, demod, sub)
        tb.connect(demod, avg)
        tb.connect(avg, (sub, 1))
        tb.connect(sub, mf, sync, decoder)
        tb.msg_connect(decoder, "fields", deframer, "fields")
        tb.connect(decoder, gr_m17.codec2_decoder(), voice)
        tb.run()
        self.assertGreaterEqual(len(found), 5)
        self.assertEqual({fields["src"] for fields in found}, {"DA2JH"})


@unittest.skipUnless(espdr_available, "the espdr package (./espdr) is not importable here")
class EspdrGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt5 import QtCore, QtWidgets
        from tests import fakes
        QtCore.QStandardPaths.setTestModeEnabled(True)                   # keep QSettings out of ~/.config
        fakes.register_tx()
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def window(self):
        from pluto_tx import gui
        w = gui.MainWindow("x")
        w.device_type_combo.addItem("fake", "fake")
        self.addCleanup(lambda: w.tb is not None and w.tb.shutdown_safe())
        return w

    def enabled(self, w, mode):
        for combo in (w.mode_combo, w.digimode_combo):
            i = combo.findData(mode)
            if i >= 0:
                return combo.model().item(i).isEnabled()
        raise AssertionError(f"mode {mode} is in no combo")

    def select(self, w, device):
        w.device_type_combo.setCurrentIndex(w.device_type_combo.findData(device))
        w._sync_mode_combo_availability()

    def test_the_gui_greys_out_what_the_esp32_cannot_send_and_brings_it_back(self):
        w = self.window()
        before = {m: self.enabled(w, m) for m in FG.MODE_NAMES if m not in (FG.MODE_MESHTASTIC, FG.MODE_MESHCORE) or True}
        self.select(w, "espdr")
        self.assertTrue(self.enabled(w, FG.MODE_FM))
        for m in (FG.MODE_SSB, FG.MODE_LSB, FG.MODE_PSK31, FG.MODE_RTTY, FG.MODE_FT8, FG.MODE_DIGITEXT, FG.MODE_BASEBAND, FG.MODE_FILEBROADCAST):
            self.assertFalse(self.enabled(w, m), FG.MODE_NAMES[m])
        self.assertEqual(self.enabled(w, FG.MODE_M17), txf.M17_AVAILABLE)
        self.select(w, "fake")
        after = {m: self.enabled(w, m) for m in before}
        self.assertEqual(before, after)

    def test_the_connection_label_is_the_ones_of_the_esp32(self):
        w = self.window()
        self.select(w, "espdr")
        w._update_device_connection_labels()
        self.assertIn("ESP32", w.device_label.text())

    def test_the_current_mode_goes_to_fm_when_the_device_cannot_carry_it(self):
        w = self.window()
        w.mode_combo.setCurrentIndex(w.mode_combo.findData(FG.MODE_SSB))
        w._current_mode = FG.MODE_SSB
        self.select(w, "espdr")
        self.assertEqual(w.mode_combo.currentData(), FG.MODE_FM)


@unittest.skipUnless(espdr_available, "the espdr package (./espdr) is not importable here")
class EspdrCopyTests(unittest.TestCase):
    """./espdr is a copy made by tools/sync-espdr.sh; these keep it honest."""

    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "espdr")

    def test_the_images_match_their_sums(self):
        import hashlib
        sums = dict(line.split()[::-1] for line in open(os.path.join(self.root, "images", "SHA256SUMS.txt")).read().splitlines() if line.strip())
        self.assertEqual(set(sums), {"rx.bin", "tx.bin", "bootloader.bin", "partition-table.bin"})
        for name, digest in sums.items():
            with open(os.path.join(self.root, "images", name), "rb") as fh:
                self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), digest, name)

    def test_upstream_records_where_the_copy_came_from(self):
        text = open(os.path.join(self.root, "UPSTREAM")).read()
        self.assertIn("repository: https://github.com/jochenhammes/esp32-sdr-trx", text)
        self.assertRegex(text, r"commit: [0-9a-f]{40}")
        self.assertIn("0BSD", text)
        with open(os.path.join(self.root, "LICENSE")) as fh:
            self.assertIn("Zero-Clause BSD", fh.read())                                          # the licence text itself

    def test_the_copy_is_the_package_that_is_imported(self):
        import espdr
        self.assertEqual(os.path.realpath(os.path.dirname(espdr.__file__)), os.path.realpath(self.root))
        self.assertTrue(os.path.exists(txlink.__file__))


if __name__ == "__main__":
    unittest.main()
