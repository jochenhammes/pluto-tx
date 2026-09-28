"""Makes the pluto-tx packages (pluto_tx, pluto_advanced_rx) importable.

Which checkout is used is decided by WEB_TRX_PLUTO_TX_PATH. On the radio
server that is the already-installed copy (~/Dokumente/plutosdr) -- the one
install.sh built gr-m17/gr-lora_sdr/rade_c next to. Unset, it falls back to
the pinned vendor/pluto-tx submodule. Only radio_backend.py imports this
module, so selecting the 'sim' backend never touches sys.path (see
docs/DEBUGGING.md: no side effects on plain import of library modules).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

VENDOR_PATH = Path(__file__).resolve().parents[2] / "vendor" / "pluto-tx"


def ensure_importable() -> Path:
    configured = os.environ.get("WEB_TRX_PLUTO_TX_PATH")
    path = Path(configured).expanduser() if configured else VENDOR_PATH
    if not (path / "pluto_tx").is_dir():
        raise RuntimeError(f"no pluto-tx checkout at {path} (set WEB_TRX_PLUTO_TX_PATH)")
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
    return path
