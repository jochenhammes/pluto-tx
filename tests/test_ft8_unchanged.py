"""FT8 must not change while JS8 reuses its synthesis (docs/JS8_PLAN.md, section 0): ft8.encode_iq() stays
byte-identical, and the public ft8.gfsk_iq() computes exactly the same signal."""
import hashlib
import unittest

import numpy as np

from pluto_tx import ft8, ft8_ctypes

# sha256 of encode_iq(msg, 48000, 1500.0, tail_s=0.25), recorded on main at 6fb300e before gfsk_iq() existed
CHECKSUMS = {
    "CQ DA2JH JO31": "4acb4df1b1b398200958d6a8534f256989d0e9ca613b6e0d16828b6f43e14db6",
    "DL1ABC DA2JH -12": "32fff6585a90fc7b6d1fc8e813b3517e6ae1717263d5b0ad9afa60d8bb5456c4",
    "DL1ABC DA2JH RR73": "cffdef3dac96d15d1e7f6c41b9a4b9dd2d06f57bd1b2f8dc008fdf686c5725fc",
}


@unittest.skipUnless(ft8_ctypes.FT8_AVAILABLE, "ft8_lib not built (install-ft8.sh)")
class Ft8UnchangedTests(unittest.TestCase):
    def test_encode_iq_checksums(self):
        for msg, want in CHECKSUMS.items():
            iq, duration = ft8.encode_iq(msg, 48000, 1500.0, tail_s=0.25)
            self.assertEqual(hashlib.sha256(iq.tobytes()).hexdigest(), want, msg)
            self.assertAlmostEqual(duration, 12.89)

    def test_gfsk_iq_matches_encode_iq(self):
        for msg in CHECKSUMS:
            iq, _ = ft8.encode_iq(msg, 48000, 1500.0)
            g = ft8.gfsk_iq(ft8_ctypes.encode_message(msg), 1500.0, ft8.FT8_SYMBOL_PERIOD, ft8.FT8_SYMBOL_BT, 48000)
            self.assertTrue(np.array_equal(g, iq))


if __name__ == "__main__":
    unittest.main()
