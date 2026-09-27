"""FT8: encoder vs. both decoders (WSJT-X jt9 as the independent judge, ft8_lib), real recordings
from ft8_lib's test set, the TX flowgraph on the fake device, the RX flowgraph branch, slot
planning, message composition and the GUI groups of both apps."""
import glob
import os
import time
import unittest
import wave
from unittest import mock

import numpy as np
from scipy import signal

from pluto_tx import ft8, ft8_ctypes
from pluto_tx import flowgraph as txf
from pluto_advanced_rx import ft8_decoder
from pluto_advanced_rx.ft8_decoder import Ft8SlotDecoder

FT8 = txf.PlutoTxFlowgraph.MODE_FT8
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_WAVS = os.path.join(REPO, "ft8_lib", "test", "wav")
BACKENDS = ft8_decoder.available_backends()
MSGS = ["CQ DA2JH JO31", "DL1ABC DA2JH -12", "DA2JH DL1ABC R-08", "DL1ABC DA2JH RR73", "CQ DX DA2JH JO31"]


def slot_with(signals, noise=0.05, seed=0):
    """15 s of 12 kHz audio, each (text, tone_hz) starting at the nominal 0.5 s, plus white noise."""
    x = np.zeros(15 * 12000)
    for text, tone in signals:
        a, _ = ft8.encode_message(text, 12000, tone, amplitude=0.15, pad_to_slot=False)
        x[6000:6000 + len(a)] += a
    return x + np.random.default_rng(seed).normal(0, noise, len(x))


def load_wav(path):
    with wave.open(path) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64)


@unittest.skipUnless(ft8_ctypes.FT8_AVAILABLE, "ft8_lib not built (install-ft8.sh)")
class CodecTests(unittest.TestCase):
    def test_all_backends_decode_five_signals_with_frequency_dt_and_snr(self):
        x = slot_with([(m, 800 + 400 * i) for i, m in enumerate(MSGS)])
        for be in BACKENDS:
            with self.subTest(backend=be):
                got = {d.text: d for d in Ft8SlotDecoder(be).decode(x)}
                self.assertEqual(set(got), set(MSGS))
                for i, m in enumerate(MSGS):
                    self.assertAlmostEqual(got[m].freq_hz, 800 + 400 * i, delta=4)
                    self.assertAlmostEqual(got[m].dt_s, 0.0, delta=0.2)
                    self.assertGreater(got[m].snr_db, 0)

    def test_encode_iq_is_the_complex_version_of_the_audio(self):
        audio, _ = ft8.encode_message("CQ DA2JH JO31", 12000, 1500, amplitude=0.7, pad_to_slot=False)
        iq, dur = ft8.encode_iq("CQ DA2JH JO31", 12000, 1500, amplitude=0.7, tail_s=0.1)
        self.assertAlmostEqual(dur, 12.64 + 0.1, places=3)
        np.testing.assert_allclose(np.imag(iq[:len(audio)]), audio, atol=1e-5)   # sin(phi) = Im(exp(j phi))
        spec = np.abs(np.fft.fft(iq[:len(audio)]))
        f = np.fft.fftfreq(len(audio), 1 / 12000)
        self.assertLess(spec[f < 0].max(), 1e-3 * spec.max())                  # single sideband (USB)

    def test_unpackable_message_is_refused(self):
        self.assertIsNone(ft8.encode_iq("THIS IS WAY TOO LONG FOR FT8", 48000, 1500)[0])
        self.assertFalse(ft8.can_encode(""))

    @unittest.skipUnless(os.path.isdir(TEST_WAVS), "ft8_lib test recordings not present")
    def test_real_recordings_ft8lib_finds_most_of_the_reference(self):
        """ft8_lib's own reference recordings (real HF audio, decodes by WSJT-X in the .txt files)."""
        found = total = 0
        dec = Ft8SlotDecoder("ft8lib")
        for wav in sorted(glob.glob(os.path.join(TEST_WAVS, "*.wav")))[:12]:
            if not os.path.exists(wav[:-4] + ".txt"):
                continue
            ref = set()
            with open(wav[:-4] + ".txt") as f:
                for line in f:
                    parts = line.split("~")
                    if len(parts) == 2:
                        ref.add(" ".join(parts[1].split()))
            got = {d.text for d in dec.decode(load_wav(wav))}
            found += len(ref & got)
            total += len(ref)
        self.assertGreater(total, 20)
        self.assertGreater(found / total, 0.5)

    def test_callsign_hash_table_resolves_later_hashed_calls(self):
        table = ft8_decoder.CallsignHashTable()
        table.save("DA2JH", 0x2ABCDE)
        self.assertEqual(table.lookup(0, 0x2ABCDE), "DA2JH")
        self.assertEqual(table.lookup(1, 0x2ABCDE >> 10), "DA2JH")
        self.assertEqual(table.lookup(2, 0x2ABCDE >> 12), "DA2JH")
        self.assertIsNone(table.lookup(0, 0x123))


class MessageAndSlotTests(unittest.TestCase):
    def test_compose_standard_messages(self):
        c = lambda kind, **kw: ft8.compose(kind, "da2jh", "jo31lk", kw.get("dx", "dl1abc"), kw.get("rpt", -7))
        self.assertEqual(c("cq"), "CQ DA2JH JO31")
        self.assertEqual(c("reply"), "DL1ABC DA2JH JO31")
        self.assertEqual(c("report"), "DL1ABC DA2JH -07")
        self.assertEqual(c("r_report", rpt=5), "DL1ABC DA2JH R+05")
        self.assertEqual(c("rr73"), "DL1ABC DA2JH RR73")
        self.assertEqual(c("report", dx=""), "")
        self.assertEqual(ft8.compose("free", "", free_text="tnx fer qso 73"), "TNX FER QSO 7")

    def test_jt9_output_parsing_drops_doubtful_and_placeholder_decodes(self):
        out = ("173045  18  1.9 1405 ~  CQ DA2JH JO31\n"
               "173045 -21  0.3  900 ~  K1ABC 9Z1QDU R KL33 ?\n"
               "173045 -19  0.1 1200 ~  K1ABC DL1ABC JO62               a2\n"
               "173045 -15  0.2 2100 ~  DA2JH DL1ABC -12                a3\n"
               "<DecodeFinished>   0   4        0\n")
        got = ft8_decoder.parse_jt9_output(out)
        self.assertEqual([(d.text, d.snr_db, d.dt_s, d.freq_hz) for d in got],
                         [("CQ DA2JH JO31", 18, 1.9, 1405), ("DA2JH DL1ABC -12", -15, 0.2, 2100)])
        self.assertEqual(len(ft8_decoder.parse_jt9_output(out, my_call="K1ABC")), 3)   # a real K1ABC keeps its a2

    def test_sender_call(self):
        f = ft8_decoder.sender_call
        self.assertEqual(f("CQ DA2JH JO31"), "DA2JH")
        self.assertEqual(f("CQ DX DL1ABC JO62"), "DL1ABC")
        self.assertEqual(f("DA2JH DL1ABC -12"), "DL1ABC")
        self.assertEqual(f("<DL1ABC> DA2JH RR73"), "DA2JH")
        self.assertIsNone(f("TNX FER QSO"))

    def test_plan_transmission(self):
        base = 1_000_000_005.0                         # a slot boundary (odd parity)
        self.assertEqual(ft8.slot_parity(base), "odd")
        for now_off, parity, key_off, start_off in [
            (0.0, "any", 0.0, 0.5),       # right at the boundary: key now, signal (after silence) at +0.5
            (1.2, "any", 1.2, 1.2),       # 0.7 s late: still this slot, keyed now, as soon as possible
            (2.0, "any", 12.5, 15.5),     # too late: next slot, keyed FT8_KEY_EARLY_S ahead
            (10.0, "odd", 27.5, 30.5),    # odd wanted -> skip the even slot
            (10.0, "even", 12.5, 15.5),
            (14.0, "any", 14.0, 15.5),    # less than key_early left: key right away
        ]:
            with self.subTest(now=now_off, parity=parity):
                key, start = ft8.plan_transmission(base + now_off, parity, 3.0, 0.5, 1.0)
                self.assertAlmostEqual(key - base, key_off, places=6)
                self.assertAlmostEqual(start - base, start_off, places=6)


@unittest.skipUnless(ft8_ctypes.FT8_AVAILABLE and BACKENDS, "ft8_lib not built")
class TxFlowgraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests import fakes
        fakes.register_tx()

    def test_fake_device_capture_decodes_with_exact_tone_spacing(self):
        fg = txf.PlutoTxFlowgraph(device_type="fake", mode=FT8, ft8_text="CQ DA2JH JO31", ft8_tone_hz=1500.0)
        self.assertIsNone(fg.ft8_problem())
        fg.start()
        self.addCleanup(lambda: (fg.stop(), fg.wait()))
        time.sleep(0.3)
        start = time.time() + 4.0                        # longer than the (unpaced fake's) source swap
        fg.ft8_start_at = start
        fg.key_ptt()
        self.assertIsNone(fg.ft8_start_at)                                       # consumed
        self.assertAlmostEqual(time.time() + fg.ft8_hold_s, start + fg.ft8_duration_s, delta=0.1)
        time.sleep(fg.ft8_hold_s + 0.5)
        from pluto_tx import config          # silence until then, minus the device latency it compensates
        self.assertAlmostEqual(fg.ft8_source.signal_started_at, start - config.FT8_TX_LATENCY_S, delta=0.05)
        fg.unkey_ptt()
        iq = np.array(fg.device.sink.data(), dtype=np.complex64)
        on = np.flatnonzero(np.abs(iq) > 0.05)
        self.assertAlmostEqual((on[-1] - on[0]) / 2_500_000, 12.64, delta=0.05)
        bb = signal.resample_poly(iq[on[0]:], 3, 625)                           # 12 kHz complex
        audio = np.concatenate([np.zeros(6000), np.real(bb)])[:180000]
        for be in BACKENDS:
            got = Ft8SlotDecoder(be).decode(audio + np.random.default_rng(0).normal(0, 0.1, len(audio)))
            self.assertEqual([d.text for d in got], ["CQ DA2JH JO31"], be)
            self.assertAlmostEqual(got[0].freq_hz, 1500, delta=2)
        tones = ft8_ctypes.encode_message("CQ DA2JH JO31")
        for k in range(1, 78):                                                   # steady symbols
            if tones[k - 1] == tones[k] == tones[k + 1]:
                seg = bb[k * 1920 + 400:(k + 1) * 1920 - 400]
                ph = np.unwrap(np.angle(seg))
                f = np.polyfit(np.arange(len(ph)) / 12000, ph, 1)[0] / (2 * np.pi)
                self.assertAlmostEqual(f, 1500 + 6.25 * tones[k], delta=0.05)

    def test_drift_precompensation_chirp_and_model(self):
        from pluto_tx import config
        model = dict(tx_ppb_s=-1.0, warmup_ppb_s=-2.0, warmup_tau_s=45.0, cooldown_ppb_s=-0.3, cooldown_tau_s=60.0)
        with mock.patch.dict(config.FT8_DRIFT_MODEL, {"fake": model}):
            fg = txf.PlutoTxFlowgraph(device_type="fake", mode=FT8, ft8_text="CQ DA2JH JO31",
                                      frequency=1_000_000_000.0)
            fg.start()
            self.addCleanup(lambda: (fg.stop(), fg.wait()))
            # right after start: tx + warm-up (pause ~0) -> about -3 ppb/s = -3 Hz/s at 1 GHz
            self.assertAlmostEqual(fg.ft8_drift_hz_per_s(), -3.0, delta=0.1)
            with mock.patch("pluto_tx.flowgraph.time.monotonic", lambda: fg._run_started + 1000.0):
                self.assertAlmostEqual(fg.ft8_drift_hz_per_s(), -1.3, delta=0.01)   # warm, long pause
                fg._ft8_last_tx_end = fg._run_started + 1000.0 - 17.0
                self.assertAlmostEqual(fg.ft8_drift_hz_per_s(), -1.0 - 0.3 * (1 - np.exp(-17 / 60)), delta=0.01)
            fg.ft8_drift_comp_enabled = False
            self.assertEqual(fg.ft8_drift_hz_per_s(), 0.0)
            fg.ft8_drift_comp_enabled = True
            with mock.patch.object(fg, "ft8_drift_hz_per_s", lambda: -1.0):
                fg.key_ptt()
                time.sleep(fg.ft8_hold_s + 0.5)
                fg.unkey_ptt()
        iq = np.array(fg.device.sink.data(), dtype=np.complex64)
        on = np.flatnonzero(np.abs(iq) > 0.05)
        bb = signal.resample_poly(iq[on[0]:on[-1]], 3, 625)
        tones = ft8_ctypes.encode_message("CQ DA2JH JO31")
        offs = []
        for k in range(79):
            seg = bb[k * 1920 + 480:(k + 1) * 1920 - 480]
            sp = np.abs(np.fft.fft(seg * np.hanning(len(seg)), 1 << 16))
            f = np.fft.fftfreq(1 << 16, 1 / 12000)
            m = (f > 1400) & (f < 1600)
            offs.append(f[m][np.argmax(sp[m])] - 6.25 * tones[k])
        # the baseband rises by +1 Hz/s so that a device falling by 1 Hz/s stays put on the air
        self.assertAlmostEqual(np.polyfit(np.arange(79) * 0.16, offs, 1)[0], 1.0, delta=0.05)
        self.assertEqual(config.FT8_DRIFT_MODEL.get("fake"), None)
        self.assertIn("pluto", config.FT8_DRIFT_MODEL)

    def test_problem_reports(self):
        fg = txf.PlutoTxFlowgraph(device_type="fake", mode=FT8, ft8_text="")
        self.assertIn("no message", fg.ft8_problem())
        fg.set_ft8_text("NOT A VALID FT8 MESSAGE AT ALL")
        self.assertIn("can't be packed", fg.ft8_problem())
        fg.set_ft8_text("CQ DA2JH JO31")
        fg.set_ft8_tone_hz(5000)
        self.assertIn("audio offset", fg.ft8_problem())
        with self.assertRaises(ValueError):
            fg.key_ptt()                                                         # refused before RF
        self.assertFalse(fg.keyed)


@unittest.skipUnless(ft8_ctypes.FT8_AVAILABLE and BACKENDS, "no FT8 decoder")
class RxFlowgraphTests(unittest.TestCase):
    def test_usb_signal_decoded_lsb_mirror_rejected(self):
        from tests import fakes
        from pluto_advanced_rx import flowgraph as rxf
        rate = 1_000_000
        usb, _ = ft8.encode_iq("CQ DA2JH JO31", 48000, 1500.0, amplitude=0.3)
        lsb, _ = ft8.encode_iq("CQ DL1ABC JO62", 48000, 1200.0, amplitude=0.3)
        x = np.zeros(int(15.5 * 48000), complex)
        x[24000:24000 + len(usb)] += usb
        x[24000:24000 + len(lsb)] += np.conj(lsb)                               # at -1200 Hz: lower sideband
        iq = signal.resample_poly(x, 125, 6)
        rng = np.random.default_rng(1)
        iq = (iq + 0.02 * (rng.normal(size=len(iq)) + 1j * rng.normal(size=len(iq)))).astype(np.complex64)
        fakes.make_fake_rx(iq, rate)
        rx = rxf.AdvancedRxFlowgraph(uri="x", frequency=14_074_000.0, sample_rate=rate, device_type="fake",
                                     active_digimode="ft8")
        t0 = 1_000_000_005.0
        recv = rx.ft8_receiver
        recv._clock = lambda: t0 + recv._written / 12000.0                     # sample-driven clock
        recv.start = lambda: True                                                # no decode thread here
        rx.start()
        rx.wait()
        got = Ft8SlotDecoder(BACKENDS[0]).decode(recv.slot_audio(t0))
        self.assertEqual([d.text for d in got], ["CQ DA2JH JO31"])
        self.assertAlmostEqual(got[0].dt_s, 0.0, delta=0.2)

    def test_receiver_thread_decodes_each_slot_and_state_keeps_rows(self):
        from pluto_advanced_rx.ft8_rx import Ft8Receiver, Ft8State
        state = Ft8State()
        audio = slot_with([("CQ DA2JH JO31", 1000)]).astype(np.float32)
        t0 = 1_000_000_005.0
        fake_now = [t0 + 15.0]
        recv = Ft8Receiver(state.on_decodes, clock=lambda: fake_now[0])
        recv.work([audio], [])                                                  # the whole slot, just ended
        fake_now[0] = t0 + 14.7                                                  # thread plans slot t0 (at +14.8)
        recv.start()
        for _ in range(300):
            if state.slots:
                break
            time.sleep(0.05)
        recv.stop()
        version, rows = state.get_snapshot()
        self.assertEqual([r["text"] for r in rows], ["CQ DA2JH JO31"])
        self.assertEqual(rows[0]["utc"], time.strftime("%H%M%S", time.gmtime(t0)))


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt5 import QtCore, QtWidgets
        from tests import fakes
        QtCore.QStandardPaths.setTestModeEnabled(True)                           # keep QSettings out of ~/.config
        fakes.register_tx()
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def pump(self, seconds, until=None):
        end = time.time() + seconds
        while time.time() < end and not (until and until()):
            self.app.processEvents()
            time.sleep(0.01)

    @unittest.skipUnless(ft8_ctypes.FT8_AVAILABLE, "ft8_lib not built")
    def test_tx_group_builds_messages_and_arms_for_the_slot(self):
        from pluto_tx import gui
        real = time.time
        off = 13.0 - (real() % 15)                                              # pretend: 2 s before a slot
        with mock.patch("pluto_tx.gui.time.time", lambda: real() + off):
            w = gui.MainWindow("x")
            w.device_type_combo.addItem("fake", "fake")
            w.device_type_combo.setCurrentIndex(w.device_type_combo.findData("fake"))
            w.digimode_combo.setCurrentIndex(w.digimode_combo.findData(FT8))
            w.mode_tab_widget.setCurrentIndex(1)
            w._rebuild("x", None)
            self.addCleanup(lambda: w.tb is not None and w.tb.shutdown_safe())
            self.assertTrue(w.ft8_group_widget.isVisibleTo(w))
            w.ft8_mycall_edit.setText("da2jh")
            w.ft8_locator_edit.setText("jo31")
            self.assertEqual(w.ft8_message_edit.text(), "CQ DA2JH JO31")
            w.ft8_kind_combo.setCurrentIndex(w.ft8_kind_combo.findData("report"))
            w.ft8_dxcall_edit.setText("dl1abc")
            self.assertEqual(w.ft8_message_edit.text(), "DL1ABC DA2JH -10")
            self.assertEqual(w.tb.ft8_text, "DL1ABC DA2JH -10")
            w.ptt_button.setChecked(True)
            self.assertTrue(w._ft8_waiting)
            self.assertFalse(w.tb.keyed)
            self.assertIn("FT8 at", w.ptt_button.text())
            self.pump(20, until=lambda: w.tb.keyed)
            self.assertTrue(w.tb.keyed)
            self.pump(16, until=lambda: not w.tb.keyed)
            self.assertFalse(w.tb.keyed)
            self.assertEqual(w.ptt_button.text(), "PTT (click to send)")
            self.assertIn("DL1ABC DA2JH -10", w.ft8_sent_log.toPlainText())

    @unittest.skipUnless(ft8_ctypes.FT8_AVAILABLE, "ft8_lib not built")
    def test_tx_cancel_while_waiting_sends_nothing(self):
        from pluto_tx import gui
        w = gui.MainWindow("x")
        w.device_type_combo.addItem("fake", "fake")
        w.device_type_combo.setCurrentIndex(w.device_type_combo.findData("fake"))
        w.digimode_combo.setCurrentIndex(w.digimode_combo.findData(FT8))
        w.mode_tab_widget.setCurrentIndex(1)
        w._rebuild("x", None)
        self.addCleanup(lambda: w.tb is not None and w.tb.shutdown_safe())
        w.ft8_mycall_edit.setText("DA2JH")
        w.ft8_slot_combo.setCurrentIndex(w.ft8_slot_combo.findData(ft8.slot_parity(time.time() + 15)))
        w.ptt_button.setChecked(True)
        self.assertTrue(w._ft8_waiting)
        w.ptt_button.setChecked(False)
        self.assertFalse(w._ft8_waiting)
        self.pump(2)
        self.assertFalse(w.tb.keyed)
        self.assertEqual(w.ft8_sent_log.toPlainText(), "")

    @unittest.skipUnless(BACKENDS, "no FT8 decoder")
    def test_rx_group_table(self):
        from pluto_advanced_rx import gui
        w = gui.MainWindow("x")
        w.digimode_combo.setCurrentIndex(w.digimode_combo.findData("ft8"))
        self.assertFalse(w.ft8_group_widget.isHidden())
        self.assertTrue(w.pocsag_group_widget.isHidden())
        D = ft8_decoder.Ft8Decode
        w._ft8_state.on_decodes(1_000_000_005.0, [D("CQ DL1ABC JO62", -12, 0.2, 812),
                                                  D("DA2JH DL1ABC -05", 3, -0.1, 1500)])
        w._render_ft8_table()
        self.assertEqual(w.ft8_table.rowCount(), 2)
        self.assertEqual(w.ft8_table.item(0, 1).text(), "-12")
        w.ft8_cq_only_checkbox.setChecked(True)
        self.assertEqual(w.ft8_table.rowCount(), 1)
        w._on_ft8_row_double_clicked(0, 0)
        self.assertEqual(self.app.clipboard().text(), "DL1ABC")


if __name__ == "__main__":
    unittest.main()
