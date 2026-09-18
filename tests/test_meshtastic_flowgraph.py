"""Software-loopback tests of the Meshtastic digimode in both apps: the real
PlutoTxFlowgraph (fake device) -> captured IQ -> the real AdvancedRxFlowgraph
(fake device replaying it). Needs gr-lora_sdr and the meshtastic package;
skipped otherwise. Run: python3 -m unittest discover tests  (with
LD_LIBRARY_PATH including ~/.local/lib/x86_64-linux-gnu if that is where
install-lora.sh put the module -- the pluto-tx launchers set it)."""
import time
import unittest

import numpy as np

from pluto_tx import config, meshtastic_codec as mc
from pluto_tx import flowgraph as txf

LORA_OK = txf.LORA_AVAILABLE


@unittest.skipUnless(LORA_OK, "gr-lora_sdr / meshtastic not installed")
class TxFlowgraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests import fakes
        fakes.register_tx()
        cls.fakes = fakes

    def make(self, **kw):
        kw.setdefault("meshtastic_text", "Test von Pluto")
        kw.setdefault("meshtastic_node_id", 0x5A1D0F42)
        kw.setdefault("meshtastic_preset_index", 1)
        return txf.PlutoTxFlowgraph(device_type="fake", mode=txf.PlutoTxFlowgraph.MODE_MESHTASTIC, **kw)

    def test_mode_sets_preset_frequency_and_wide_rf_bandwidth(self):
        self.fakes.FakeTxDevice.rf_bandwidth_calls.clear()
        fg = self.make()
        self.assertEqual(fg.mode, fg.MODE_MESHTASTIC)
        self.assertEqual(fg.nominal_freq_hz, config.MESHTASTIC_PRESETS[1].frequency_hz)
        self.assertGreaterEqual(self.fakes.FakeTxDevice.rf_bandwidth_calls[-1], 250_000)
        fg.set_mode(fg.MODE_RTTY)  # leaving restores the device default (FM would need a started flowgraph)
        self.assertEqual(self.fakes.FakeTxDevice.rf_bandwidth_calls[-1], 200_000)

    def test_prepare_refusals(self):
        fg = self.make(meshtastic_text="")
        self.assertFalse(fg.prepare_meshtastic_tx()[0])                       # empty text
        fg.set_meshtastic_text("x" * 300)
        self.assertFalse(fg.prepare_meshtastic_tx()[0])                       # too long
        fg.set_meshtastic_text("ok")
        fg.set_meshtastic_channel(None, "not-base64!")
        self.assertIn("PSK", fg.prepare_meshtastic_tx()[1])                   # bad PSK
        fg.set_meshtastic_channel(None, "AQ==")
        self.assertTrue(fg.prepare_meshtastic_tx()[0])

    def test_ham_mode_needs_callsign_and_forces_no_encryption(self):
        fg = self.make(meshtastic_preset_index=0)  # 433 MHz, Ham Mode
        ok, msg, _ = fg.prepare_meshtastic_tx()
        self.assertFalse(ok)
        self.assertIn("callsign", msg.lower())
        fg.set_meshtastic_callsign("da2jh")
        ok, _msg, info = fg.prepare_meshtastic_tx()
        self.assertTrue(ok)
        self.assertIn("[DA2JH]", info["text"])
        s = mc.summarize_packet(info["packet"], psk=b"")                      # readable WITHOUT a key
        self.assertEqual(s["kind"], "text")
        self.assertIn("DA2JH", s["text"])

    def test_duty_cycle_blocks_868_but_not_when_budget_allows(self):
        fg = self.make()
        ok, _, info = fg.prepare_meshtastic_tx(now=0.0)
        self.assertTrue(ok)
        fg._meshtastic_duty.record(359.9, 0.0)  # budget (10 % of 1 h) nearly used up
        ok, msg, _ = fg.prepare_meshtastic_tx(now=10.0)
        self.assertFalse(ok)
        self.assertIn("Duty-cycle", msg)
        self.assertTrue(fg.prepare_meshtastic_tx(now=3700.0)[0])

    def test_key_ptt_refuses_before_any_rf_action(self):
        fg = self.make(meshtastic_text="")
        with self.assertRaises(ValueError):
            fg.key_ptt()
        self.assertFalse(fg.keyed)

    def test_soundcard_falls_back_to_fm(self):
        fg = txf.PlutoTxFlowgraph(device_type="soundcard", mode=txf.PlutoTxFlowgraph.MODE_MESHTASTIC)
        self.assertEqual(fg.mode, fg.MODE_FM)

    def test_tx_to_rx_loopback(self):
        from pluto_advanced_rx import flowgraph as rxf
        from pluto_advanced_rx.meshtastic_state import MeshtasticState
        from tests import fakes

        fg = self.make()
        ok, msg, info = fg.prepare_meshtastic_tx()
        self.assertTrue(ok, msg)
        fg.start()
        time.sleep(1.0)
        fg.key_ptt()
        time.sleep(fg.meshtastic_hold_s + 0.5)
        fg.unkey_ptt()
        fg.stop()
        fg.wait()
        iq = np.array(fg.device.sink.data(), dtype=np.complex64)
        self.assertGreater(len(iq), 1_500_000)
        on_air_s = float((np.abs(iq) > 1e-3).sum()) / 2_500_000
        self.assertAlmostEqual(on_air_s, info["airtime_s"], delta=0.03)  # airtime formula == real signal length
        self.assertLess(float(np.abs(iq).max()), 1.2)

        for rate in (2_500_000, 1_000_000):
            with self.subTest(rx_rate=rate):
                pad = np.zeros(rate, np.complex64)
                data = iq
                if rate != 2_500_000:
                    from scipy.signal import resample_poly
                    data = resample_poly(iq, 2, 5).astype(np.complex64)
                fakes.make_fake_rx(np.concatenate([pad, data, pad, pad]), rate)
                state = MeshtasticState()
                rx = rxf.AdvancedRxFlowgraph(
                    uri="x", frequency=869_525_000.0, sample_rate=rate, device_type="fake",
                    active_digimode="meshtastic", meshtastic_preset_index=1, on_meshtastic_frame=state.on_frame)
                rx.start()
                time.sleep(8)
                rx.stop()
                rx.wait()
                _v, rows = state.get_snapshot()
                self.assertEqual(len(rows), 1)
                self.assertEqual((rows[0]["kind"], rows[0]["text"], rows[0]["from"]), ("text", "Test von Pluto", 0x5A1D0F42))

    def test_rx_audio_only_device_is_refused(self):
        from pluto_advanced_rx import flowgraph as rxf
        with self.assertRaises(ValueError):
            rxf.AdvancedRxFlowgraph(device_type="audio", active_digimode="meshtastic")


if __name__ == "__main__":
    unittest.main()
