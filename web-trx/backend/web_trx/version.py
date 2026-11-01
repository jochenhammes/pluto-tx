"""The pluto-tx version for /health. Loads pluto_tx/version.py by file path
from the pluto-tx code Web-TRX runs against (pluto_path.py's choice), without
touching sys.path -- the 'sim' backend never imports pluto_tx otherwise."""
from __future__ import annotations

import functools
import importlib.util
import os
import re
from pathlib import Path

from .pluto_path import REPO_PATH


@functools.lru_cache(maxsize=1)
def pluto_tx_version() -> str:
    configured = os.environ.get("WEB_TRX_PLUTO_TX_PATH")
    root = Path(configured).expanduser() if configured else REPO_PATH
    try:
        generated = root / "pluto_tx" / "_version.py"   # written by the package build
        if generated.is_file():
            m = re.search(r'^VERSION = "([^"]+)"$', generated.read_text(), re.MULTILINE)
            return m.group(1) if m else "0+unknown"
        spec = importlib.util.spec_from_file_location("_pluto_tx_version", root / "pluto_tx" / "version.py")
        if spec is None or spec.loader is None:
            return "0+unknown"
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.version()
    except (OSError, SyntaxError, AttributeError, ImportError):
        return "0+unknown"
