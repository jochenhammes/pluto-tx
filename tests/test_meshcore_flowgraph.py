"""MeshCore through the real flowgraphs: TX (fake device) -> IQ -> RX (fake device replaying it), the optional
regression against the private HackRF/ESP32 capture (skipped when the folder is not there; its contents are never
copied into the repo), and GUI smoke tests. Needs gr-lora_sdr (LD_LIBRARY_PATH like the Meshtastic tests)."""
import os
import re
import tempfile
import time
import unittest

import numpy as np

from pluto_tx import config, meshcore_codec as mc
from pluto_tx import flowgraph as txf

CAPTURE_DIR = os.environ.get("MESHCORE_CAPTURE_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "meshcore_capture_2026-09-20")
MESHCORE = txf.PlutoTxFlowgraph.MODE_MESHCORE
RATE = 2_500_000

needs_lora = unittest.skipUnless(txf.MESHCORE_AVAILABLE, "gr-lora_sdr / cryptography not installed")


def make_tx(**kw):
    kw.setdefault("meshcore_identity", mc.Identity.generate())
    return txf.PlutoTxFlowgraph(device_type="fake", mode=MESHCORE, **kw)


def send(fg):
    fg.start()
    time.sleep(1.0)
    fg.key_ptt()
    time.sleep(fg.meshcore_hold_s + 0.5)
    fg.unkey_ptt()
    fg.stop()
    fg.wait()
    iq = np.array(fg.device.sink.data(), dtype=np.complex64)
    on = np.flatnonzero(np.abs(iq) > 1e-3)
    return iq, float(on[-1] - on[0]) / RATE


def receive(iq, channels=(), rate=RATE, offset_hz=0.0):
    from pluto_advanced_rx import flowgraph as rxf
    from pluto_advanced_rx.meshcore_state import MeshcoreState
    from tests import fakes
    if rate != RATE:
        from scipy.signal import resample_poly
        iq = resample_poly(iq, 2, 5).astype(np.complex64)
    if offset_hz:
        iq = (iq * np.exp(2j * np.pi * offset_hz * np.arange(len(iq)) / rate)).astype(np.complex64)
    pad = np.zeros(rate, np.complex64)
    fakes.make_fake_rx(np.concatenate([pad, iq, pad, pad]), rate)
    state = MeshcoreState()
    state.set_channels(list(channels))
    rx = rxf.AdvancedRxFlowgraph(uri="x", frequency=869.618e6, sample_rate=rate, device_type="fake",
                                 active_digimode="meshcore", on_meshcore_packet=state.on_frame)
    rx.start()
    time.sleep(8)
    rx.stop()
    rx.wait()
    return state.get_snapshot()[1]


@needs_lora
class TxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests import fakes
        fakes.register_tx()
        cls.fakes = fakes

    def test_mode_preset_and_rf_bandwidth(self):
        self.fakes.FakeTxDevice.rf_bandwidth_calls.clear()
        fg = make_tx()
        self.assertEqual(fg.mode, MESHCORE)
        self.assertEqual(fg.nominal_freq_hz, 869_618_000.0)
        self.assertEqual(fg.lora_encoder.sf, 8)
        self.assertEqual(fg.lora_encoder.bw, 62_500)
        self.assertEqual(fg.lora_encoder.cr, 4)
        self.assertGreaterEqual(self.fakes.FakeTxDevice.rf_bandwidth_calls[-1], 200_000)

    def test_meshtastic_and_meshcore_phy_are_distinct(self):
        fg = make_tx()
        self.assertTrue(fg.meshcore_phy_matches(0))
        self.assertFalse(fg.meshtastic_phy_matches(0))
        plain = txf.PlutoTxFlowgraph(device_type="fake", mode=txf.PlutoTxFlowgraph.MODE_FM)
        self.assertFalse(plain.meshcore_phy_matches(0))      # built with the Meshtastic PHY -> needs a rebuild
        self.assertTrue(plain.meshtastic_phy_matches(0))
        with self.assertRaises(ValueError):
            plain.set_meshcore_preset(0)

    def test_soundcard_falls_back_to_fm(self):
        fg = txf.PlutoTxFlowgraph(device_type="soundcard", mode=MESHCORE)
        self.assertEqual(fg.mode, fg.MODE_FM)

    def test_prepare_advert_and_group(self):
        fg = make_tx(meshcore_name="DA2JH-Test")
        ok, msg, info = fg.prepare_meshcore_tx()
        self.assertTrue(ok, msg)
        self.assertEqual((info["summary"]["kind"], info["summary"]["verified"], info["summary"]["name"]),
                         ("advert", True, "DA2JH-Test"))
        self.assertAlmostEqual(info["airtime_s"], 1.1315, delta=0.001)          # 113 B, SF8/62.5k/CR4-8/preamble 32
        fg.set_meshcore_message(kind="group", text="Hallo", name="DA2JH")
        ok, msg, info = fg.prepare_meshcore_tx()
        self.assertTrue(ok, msg)
        self.assertEqual((info["summary"]["kind"], info["summary"]["text"]), ("group_text", "Hallo"))
        fg.set_meshcore_message(channel_name="Test", channel_secret_hex="00112233445566778899aabbccddeeff", text="x")
        ok, _, info = fg.prepare_meshcore_tx()
        self.assertTrue(ok)
        self.assertEqual(info["summary"]["channel"], "Test")

    def test_refusals_before_any_rf_action(self):
        fg = make_tx(meshcore_kind="group", meshcore_text="")
        self.assertFalse(fg.prepare_meshcore_tx()[0])
        with self.assertRaises(ValueError):
            fg.key_ptt()
        self.assertFalse(fg.keyed)
        fg.set_meshcore_message(text="x" * 300)
        self.assertIn("longer", fg.prepare_meshcore_tx()[1])
        fg.set_meshcore_message(text="x", channel_secret_hex="12")
        self.assertIn("32 hex", fg.prepare_meshcore_tx()[1])

    def test_amateur_band_allows_only_a_named_advert(self):
        fg = make_tx()
        fg.set_frequency(433_500_000)                                          # 70 cm
        self.assertIn("callsign", fg.prepare_meshcore_tx()[1])                 # advert without a name
        fg.set_meshcore_message(name="DA2JH")
        self.assertTrue(fg.prepare_meshcore_tx()[0])
        fg.set_meshcore_message(kind="group", text="hi")
        ok, msg, _ = fg.prepare_meshcore_tx()
        self.assertFalse(ok)
        self.assertIn("encrypted", msg)

    def test_duty_cycle_and_busy_window(self):
        fg = make_tx(meshcore_name="x")
        ok, _, info = fg.prepare_meshcore_tx(now=0.0)
        self.assertTrue(ok)
        fg._meshcore_duty.record(359.0, 0.0)                                   # budget (10 % of 1 h) nearly used
        ok, msg, _ = fg.prepare_meshcore_tx(now=10.0)
        self.assertFalse(ok)
        self.assertIn("Duty-cycle", msg)
        self.assertTrue(fg.prepare_meshcore_tx(now=3700.0)[0])
        fg._meshcore_busy_until = 4000.0
        self.assertIn("still being sent", fg.prepare_meshcore_tx(now=3800.0)[1])


@needs_lora
class LoopbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests import fakes
        fakes.register_tx()

    def test_advert_flood_and_direct(self):
        for route in ("flood", "direct"):
            with self.subTest(route=route):
                fg = make_tx(meshcore_name="Loopback", meshcore_route=route, meshcore_role=2)
                ident = fg.meshcore_identity
                iq, on_air = send(fg)
                self.assertAlmostEqual(on_air, fg.meshcore_last_airtime_s, delta=0.05)     # airtime formula == signal
                self.assertLess(float(np.abs(iq).max()), 1.2)
                rows = receive(iq)
                self.assertEqual(len(rows), 1)
                r = rows[0]
                self.assertEqual((r["kind"], r["verified"], r["name"], r["role"], r["route"].lower()),
                                 ("advert", True, "Loopback", "Repeater", route))
                self.assertEqual(r["public_key"], ident.public_key.hex())

    def test_group_text_public_and_custom_channel_at_other_rate(self):
        custom = mc.GroupChannel("Team", bytes(range(16, 32)))
        cases = ((mc.PUBLIC_CHANNEL, "", RATE, 0.0), (custom, custom.secret.hex(), 1_000_000, 3000.0))
        for channel, key, rate, offset in cases:
            with self.subTest(channel=channel.name, rate=rate):
                fg = make_tx(meshcore_kind="group", meshcore_name="DA2JH", meshcore_text="Test äöü 123",
                             meshcore_channel_name=channel.name, meshcore_channel_secret_hex=key)
                iq, _ = send(fg)
                rows = receive(iq, [custom], rate, offset)
                self.assertEqual([(r["kind"], r["channel"], r["sender"], r["text"]) for r in rows],
                                 [("group_text", channel.name, "DA2JH", "Test äöü 123")])

    def test_unknown_channel_is_shown_but_unverified(self):
        fg = make_tx(meshcore_kind="group", meshcore_name="a", meshcore_text="private",
                     meshcore_channel_name="Team", meshcore_channel_secret_hex="00" * 15 + "01")
        iq, _ = send(fg)
        rows = receive(iq)                                                       # no extra channel configured
        self.assertEqual([(r["kind"], r["verified"]) for r in rows], [("other", False)])

    def test_meshtastic_still_decodes_after_the_refactor(self):
        if not txf.LORA_AVAILABLE:
            self.skipTest("meshtastic package not installed")
        from pluto_advanced_rx import flowgraph as rxf
        from pluto_advanced_rx.meshtastic_state import MeshtasticState
        from tests import fakes
        fg = txf.PlutoTxFlowgraph(device_type="fake", mode=txf.PlutoTxFlowgraph.MODE_MESHTASTIC, meshtastic_text="ping",
                                  meshtastic_node_id=0x5A1D0F42, meshtastic_preset_index=1)
        self.assertTrue(fg.prepare_meshtastic_tx()[0])
        fg.start()
        time.sleep(1.0)
        fg.key_ptt()
        time.sleep(fg.meshtastic_hold_s + 0.5)
        fg.unkey_ptt()
        fg.stop()
        fg.wait()
        iq = np.array(fg.device.sink.data(), dtype=np.complex64)
        pad = np.zeros(RATE, np.complex64)
        fakes.make_fake_rx(np.concatenate([pad, iq, pad, pad]), RATE)
        state = MeshtasticState()
        rx = rxf.AdvancedRxFlowgraph(uri="x", frequency=869_525_000.0, sample_rate=RATE, device_type="fake",
                                     active_digimode="meshtastic", meshtastic_preset_index=1, on_meshtastic_frame=state.on_frame)
        rx.start()
        time.sleep(8)
        rx.stop()
        rx.wait()
        rows = state.get_snapshot()[1]
        self.assertEqual([(r["kind"], r["text"]) for r in rows], [("text", "ping")])


def _log_packets(path):
    packets, current = [], None
    with open(path, errors="replace") as f:
        lines = f.read().splitlines()
    for line in lines:
        line = line.split(" ", 1)[1] if " " in line else line
        if line.startswith("Paket "):
            current = []
            packets.append(current)
        elif current is not None and "hex:" in line:
            current.append(line.split("hex:")[1].strip())
        elif current is not None and re.match(r"^\s+[0-9A-F]{2} ", line):
            current.append(line.strip())
        elif current is not None and "text:" in line:
            current = None
    return [bytes.fromhex(" ".join(p)) for p in packets]


@needs_lora
@unittest.skipUnless(os.path.isdir(CAPTURE_DIR), "private MeshCore capture folder not present")
class CaptureRegressionTests(unittest.TestCase):
    """Uses the user's private capture at run time only (never copied into the repo)."""

    @classmethod
    def setUpClass(cls):
        cls.reference = _log_packets(os.path.join(CAPTURE_DIR, "esp32_serial.log"))
        raw = np.fromfile(os.path.join(CAPTURE_DIR, "meshcore_869p9MHz_2Msps.iq"), dtype=np.int8,
                          count=2 * 2_000_000 * 20).reshape(-1, 2)                     # first 20 s: bursts 1-3
        x = (raw[:, 0].astype(np.float32) + 1j * raw[:, 1].astype(np.float32)) / 127.0
        cls.iq = (x * np.exp(2j * np.pi * 282e3 * np.arange(len(x)) / 2e6)).astype(np.complex64)   # signal to 0 Hz

    def test_rx_decodes_the_recorded_bursts_bit_exactly(self):
        from pluto_advanced_rx import flowgraph as rxf
        from pluto_advanced_rx.meshcore_state import MeshcoreState
        from tests import fakes
        frames = []
        fakes.make_fake_rx(self.iq, 2_000_000)
        state = MeshcoreState()
        rx = rxf.AdvancedRxFlowgraph(uri="x", frequency=869.618e6, sample_rate=2_000_000, device_type="fake",
                                     active_digimode="meshcore",
                                     on_meshcore_packet=lambda raw: (frames.append(raw), state.on_frame(raw)))
        rx.start()
        time.sleep(20)
        rx.stop()
        rx.wait()
        self.assertEqual(frames, self.reference[:3])
        rows = state.get_snapshot()[1]
        self.assertEqual([r["kind"] for r in rows], ["advert"] * 3)
        self.assertTrue(all(r["verified"] for r in rows))

    def test_tx_chirp_symbols_match_the_real_burst(self):
        from scipy import signal
        from gnuradio import gr, blocks
        from pluto_tx.lora import LoraTxEncoder
        packet = self.reference[0]
        tb = gr.top_block()
        enc = LoraTxEncoder(8, 62500, 4, preamb_len=32, sync_word=0x12)
        sink = blocks.vector_sink_c()
        tb.connect(enc, sink)
        tb.start()
        time.sleep(0.5)
        enc.send_payload_bytes(packet)
        time.sleep(3.0)
        tb.stop()
        tb.wait()
        tx = np.array(sink.data(), dtype=np.complex64)
        on = np.flatnonzero(np.abs(tx) > 0.05)
        tx = tx[on[0]:on[-1] + 1]
        x = self.iq[int(0.50 * 2e6):int(1.75 * 2e6)]
        cap = signal.resample_poly(x - x.mean(), 1, 8).astype(np.complex64)
        env = np.convolve(np.abs(cap) ** 2, np.ones(16) / 16, "same")
        start = int(np.argmax(env > np.median(env[:2000]) * 4))
        cap = cap[start:start + len(tx) + 200]
        n_sym = int(2 ** 8 / 62500 * 250e3)
        t = np.arange(n_sym) / 250e3
        up = np.exp(2j * np.pi * (-62500 / 2 * t + 62500 / (2 * (2 ** 8 / 62500)) * t ** 2))

        def symbols(y):
            def bins(w, ref):
                spec = np.abs(np.fft.fft(w * np.conj(ref)))
                m = len(spec) // 4
                comb = spec[:m] + spec[m:2 * m] + spec[2 * m:3 * m] + spec[3 * m:]
                k = int(np.argmax(comb))
                return k, comb[k] / (np.median(comb) + 1e-9)
            a = 4 * n_sym - 4 * bins(y[4 * n_sym:5 * n_sym], up)[0] - 4 * n_sym
            while a < 0:
                a += n_sym
            out = []
            for m in range(int((len(y) - a) / n_sym) - 1):
                w = y[a + m * n_sym:a + (m + 1) * n_sym]
                if len(w) < n_sym:
                    break
                ku, qu = bins(w, up)
                kd, qd = bins(w, np.conj(up))
                out.append(("U", ku) if qu >= qd else ("D", kd))
            return out
        a, b = symbols(tx), symbols(cap)
        self.assertAlmostEqual(len(tx) / 250e3, 1.0988, delta=0.005)
        # preamble, sync word (8, 16), header and all payload symbols are identical; only the SFD alignment
        # (down-chirp bin jitter of the measurement) and the final partial window may differ
        same = [i for i in range(min(len(a), len(b))) if a[i] == b[i]]
        self.assertGreaterEqual(len(same), 255)
        self.assertEqual(a[32:34], [("U", 8), ("U", 16)])
        self.assertEqual(a[36:259], b[36:259])


@needs_lora
class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt5 import QtWidgets
        from tests import fakes
        fakes.register_tx()
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.identity_dir = tempfile.mkdtemp()
        os.environ["PLUTO_TX_MESHCORE_IDENTITY"] = os.path.join(cls.identity_dir, "id.json")

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("PLUTO_TX_MESHCORE_IDENTITY", None)

    def test_tx_gui_mode_switching_rebuilds_for_the_other_phy(self):
        from pluto_tx import gui
        w = gui.MainWindow("x")
        w.device_type_combo.addItem("fake", "fake")
        w.device_type_combo.setCurrentIndex(w.device_type_combo.findData("fake"))
        w.mode_tab_widget.setCurrentIndex(1)
        w.digimode_combo.setCurrentIndex(w.digimode_combo.findData(MESHCORE))
        w._rebuild("x", None)
        self.addCleanup(lambda: w.tb is not None and w.tb.shutdown_safe())
        self.assertEqual(w.tb.mode, MESHCORE)
        self.assertTrue(w.meshcore_group_widget.isVisibleTo(w))
        self.assertAlmostEqual(w.freq_spin.value(), 869.618)
        self.assertTrue(w.meshcore_identity_label.text().startswith("Node key:"))
        w.meshcore_name_edit.setText("DA2JH")
        self.assertIn("B, ", w.meshcore_info_label.text())
        w.meshcore_kind_combo.setCurrentIndex(w.meshcore_kind_combo.findData("group"))
        self.assertTrue(w.meshcore_text_edit.isEnabled())
        self.assertFalse(w.meshcore_role_combo.isEnabled())
        old_tb = w.tb
        w.digimode_combo.setCurrentIndex(w.digimode_combo.findData(txf.PlutoTxFlowgraph.MODE_MESHTASTIC))
        self.assertIsNot(w.tb, old_tb)                                          # different PHY -> rebuilt
        self.assertEqual(w.tb.mode, txf.PlutoTxFlowgraph.MODE_MESHTASTIC if txf.LORA_AVAILABLE else txf.PlutoTxFlowgraph.MODE_FM)
        w.digimode_combo.setCurrentIndex(w.digimode_combo.findData(MESHCORE))
        self.assertEqual(w.tb.mode, MESHCORE)
        self.assertEqual(w.tb.meshcore_kind, "group")                           # settings survive the rebuild
        self.assertEqual(w.tb.meshcore_name, "DA2JH")

    def test_rx_gui_table_nodes_and_hiding(self):
        from pluto_advanced_rx import gui
        w = gui.MainWindow("")
        ident = mc.Identity.generate()
        w._meshcore_state.on_frame(mc.build_advert(ident, "Node A", location=(50.0, 8.0)))
        w._meshcore_state.on_frame(mc.build_group_text(mc.PUBLIC_CHANNEL, "Bob", "Hallo"))
        w._meshcore_state.on_frame(mc.build_packet(1, mc.PT_ACK, b"\x01\x02\x03\x04"))
        w._meshcore_state.on_frame(b"\x00")
        w._render_meshcore_table()
        table = w.meshcore_table
        self.assertEqual(table.rowCount(), 3)                                   # the garbage frame is never shown
        self.assertEqual(table.item(0, 4).text(), "Node A")
        self.assertIn("Hallo", table.item(1, 5).text())
        self.assertEqual(table.item(2, 6).text(), "no")
        self.assertEqual(w.meshcore_nodes_table.rowCount(), 1)
        w.meshcore_hide_unverified_checkbox.setChecked(True)
        w._render_meshcore_table()
        self.assertEqual(table.rowCount(), 2)
        w.meshcore_channel_name_edit.setText("Team")
        w.meshcore_channel_key_edit.setText("00" * 16)
        team = mc.GroupChannel("Team", bytes(16))
        w._meshcore_state.on_frame(mc.build_group_text(team, "Al", "geheim"))
        w._render_meshcore_table()
        self.assertEqual(w.meshcore_table.rowCount(), 3)
        w.meshcore_clear_button.click()
        self.assertEqual(w.meshcore_table.rowCount(), 0)


if __name__ == "__main__":
    unittest.main()
