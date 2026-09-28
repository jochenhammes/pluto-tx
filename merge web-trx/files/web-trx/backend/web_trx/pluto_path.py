"""Makes the pluto-tx packages (pluto_tx, pluto_advanced_rx) importable.

Web-TRX lives inside the pluto-tx repository (web-trx/), so by default the
code comes from that same checkout -- the one install.sh built gr-m17,
gr-lora_sdr, rade_c and ft8_lib next to. WEB_TRX_PLUTO_TX_PATH overrides it
(e.g. to try Web-TRX against another pluto-tx checkout). Only
radio_backend.py imports this module, so selecting the 'sim' backend never
touches sys.path (see docs/DEBUGGING.md: no side effects on plain import of
library modules).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# web-trx/backend/web_trx/pluto_path.py -> parents[3] is the repository root
REPO_PATH = Path(__file__).resolve().parents[3]


def ensure_importable() -> Path:
    configured = os.environ.get("WEB_TRX_PLUTO_TX_PATH")
    path = Path(configured).expanduser() if configured else REPO_PATH
    if not (path / "pluto_tx").is_dir():
        raise RuntimeError(f"no pluto-tx checkout at {path} (set WEB_TRX_PLUTO_TX_PATH)")
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
    return path
