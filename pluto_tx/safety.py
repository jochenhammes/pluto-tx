"""Raw-libiio safety layer for the PlutoSDR TX chain.

Deliberately independent of gr-iio / GNU Radio: this must keep working even
if the GNU Radio runtime has crashed or never finished starting. It owns its
own iio.Context and is the single place that knows how to make the TX chain
go dark (force_safe_state), reused from every shutdown path in the app
(normal exit, window close, SIGINT/SIGTERM, uncaught exceptions, atexit).
"""
import sys

import iio

from . import config


class PlutoSafety:
    def __init__(self, uri: str = config.DEFAULT_URI):
        self.uri = uri
        self._ctx = iio.Context(uri)
        self._phy = self._ctx.find_device("ad9361-phy")
        if self._phy is None:
            raise RuntimeError(f"ad9361-phy device not found on context '{uri}'")
        self._tx_voltage0 = self._phy.find_channel("voltage0", True)
        self._tx_lo = self._phy.find_channel("altvoltage1", True)
        if self._tx_voltage0 is None or self._tx_lo is None:
            raise RuntimeError("ad9361-phy TX channels (voltage0/altvoltage1) not found")

    def force_min_attenuation(self):
        self._tx_voltage0.attrs["hardwaregain"].value = str(config.MIN_ATTEN)

    def set_attenuation(self, db: float):
        db = max(config.MIN_ATTEN, min(config.MAX_ATTEN, db))
        self._tx_voltage0.attrs["hardwaregain"].value = str(db)

    def power_down_lo(self, down: bool):
        self._tx_lo.attrs["powerdown"].value = "1" if down else "0"

    def force_safe_state(self):
        """The single 'make it dark' call. Idempotent, never raises."""
        try:
            self.force_min_attenuation()
        except Exception as e:
            print(f"WARNING: force_min_attenuation failed: {e}", file=sys.stderr)
        try:
            self.power_down_lo(True)
        except Exception as e:
            print(f"WARNING: power_down_lo failed: {e}", file=sys.stderr)

    def prepare_for_start(self):
        """Atten to minimum, then LO up. Call BEFORE constructing the GR sink."""
        self.force_min_attenuation()
        self.power_down_lo(False)

    def read_state(self):
        gain_raw = self._tx_voltage0.attrs["hardwaregain"].value
        return {
            "hardwaregain_db": float(gain_raw.split()[0]),
            "lo_powerdown": self._tx_lo.attrs["powerdown"].value == "1",
        }
