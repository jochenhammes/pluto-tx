"""The pluto-tx version, for --version, the apps and Web-TRX's /health.

The installed package carries a generated pluto_tx/_version.py (written by
packaging/debian/rules). A git checkout asks `git describe`; without git or
tags the answer is "0+unknown". Standard library only, computed on first use.
"""
from __future__ import annotations

import functools
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


@functools.lru_cache(maxsize=1)
def version() -> str:
    try:
        from ._version import VERSION  # type: ignore[import-not-found]
        return VERSION
    except ImportError:
        pass
    if (_REPO_ROOT / ".git").exists():
        try:
            out = subprocess.run(["git", "-C", str(_REPO_ROOT), "describe", "--tags", "--always", "--dirty"],
                                 capture_output=True, text=True, timeout=5)
            if out.returncode == 0 and out.stdout.strip():
                v = out.stdout.strip()
                return v[1:] if v.startswith("v") and v[1:2].isdigit() else v
        except (OSError, subprocess.SubprocessError):
            pass
    return "0+unknown"
