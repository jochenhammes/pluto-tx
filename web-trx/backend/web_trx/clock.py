"""Is the server clock NTP-synchronised? FT8 slot timing happens entirely on
the server (network/VPN latency doesn't matter), so the server clock must be
right to about +-1 s -- the browser's clock only drives the display.

Checked the same way pluto-tx's GUIs do (pluto_tx/gui.py _ntp_synchronized):
`timedatectl show -p NTPSynchronized --value`. Cached for CHECK_INTERVAL_S;
if it can't be determined (no systemd, timeout), it counts as synchronised,
as in pluto-tx -- the check is a warning, not a gate.
"""
from __future__ import annotations

import subprocess
import time

CHECK_INTERVAL_S = 300.0

_cache: tuple[float, bool] | None = None  # (checked_at monotonic, result)


def _query() -> bool:
    try:
        out = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True, text=True, timeout=2.0, check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return True
    return out != "no"


def ntp_synchronized() -> bool:
    global _cache
    now = time.monotonic()
    if _cache is None or now - _cache[0] >= CHECK_INTERVAL_S:
        _cache = (now, _query())
    return _cache[1]
