"""Where pluto-tx finds its optional native parts, and where it may write.

The one place that knows the two installation layouts:

  git checkout   the install-*.sh scripts build next to the code:
                 ft8_lib/libft8wrap.so, rade_c/build/src/{librade.so,lpcnet_demo},
                 js8call/build-js8ref/js8ref, js8call/jsc.json
  .deb package   /usr/lib/pluto-tx/{lib,bin}  architecture-dependent, private
                 /usr/share/pluto-tx/js8/jsc.json
                 (the Python code itself lives in /usr/share/pluto-tx, marked
                 by a file named .pluto-tx-package next to pluto_tx/)

Lookup order for every resource: environment variable, then the checkout
paths exactly as before, then the package paths. A checkout without a
package installed therefore behaves as it always did.

Writable places follow XDG (user_dir()). Inside a checkout the Web-TRX
runtime data stays in web-trx/run as before; see webtrx_control.py.

Standard library only: imported by the TX and RX apps, the CLI, Web-TRX and
the tests, with or without GNU Radio.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

PACKAGE_LIBDIR = Path("/usr/lib/pluto-tx")
PACKAGE_SHAREDIR = Path("/usr/share/pluto-tx")
PACKAGE_MARKER = ".pluto-tx-package"

# PLUTO_TX_LIBDIR=<dir>: <dir>/lib and <dir>/bin are searched before
# anything else (a custom build, or a package tree unpacked elsewhere).
LIBDIR_ENV = "PLUTO_TX_LIBDIR"

# Checkout locations, relative to REPO_ROOT -- what the installers produce.
_CHECKOUT_LIBS = {
    "libft8wrap.so": "ft8_lib/libft8wrap.so",
    "librade.so": "rade_c/build/src/librade.so",
}
_CHECKOUT_PROGRAMS = {
    "lpcnet_demo": "rade_c/build/src/lpcnet_demo",
    "js8ref": "js8call/build-js8ref/js8ref",
}
# Environment variables that name one program directly (checked first).
_PROGRAM_ENV = {
    "js8ref": "JS8REF",
}
_CHECKOUT_DATA = {
    "jsc.json": "js8call/jsc.json",
}
_PACKAGE_DATA = {
    "jsc.json": "js8/jsc.json",
}

_XDG = {
    "config": ("XDG_CONFIG_HOME", ".config"),
    "state": ("XDG_STATE_HOME", ".local/state"),
    "cache": ("XDG_CACHE_HOME", ".cache"),
    "data": ("XDG_DATA_HOME", ".local/share"),
}


def is_package() -> bool:
    """True when this code runs from the installed .deb (not a checkout)."""
    return (REPO_ROOT / PACKAGE_MARKER).exists()


def _env_dir(sub: str) -> Path | None:
    base = os.environ.get(LIBDIR_ENV, "").strip()
    return Path(base).expanduser() / sub if base else None


def library_candidates(name: str) -> list[str]:
    """Names/paths to try with ctypes.CDLL, in order: the bare name (the
    dynamic loader's own search, i.e. LD_LIBRARY_PATH from a launcher), then
    PLUTO_TX_LIBDIR/lib, the checkout build, the package's private libdir.
    Only existing files are listed after the bare name."""
    out = [name]
    env = _env_dir("lib")
    paths = [env / name] if env else []
    if name in _CHECKOUT_LIBS:
        paths.append(REPO_ROOT / _CHECKOUT_LIBS[name])
    paths.append(PACKAGE_LIBDIR / "lib" / name)
    for p in paths:
        if p.is_file() and str(p) not in out:
            out.append(str(p))
    return out


def program(name: str) -> str | None:
    """Absolute path of a helper program (lpcnet_demo, js8ref), or None.
    Order: its own variable (JS8REF), PLUTO_TX_LIBDIR/bin, PATH, the checkout
    build, the package's private bindir."""
    var = _PROGRAM_ENV.get(name)
    if var and os.environ.get(var):
        p = os.environ[var]
        return p if os.access(p, os.X_OK) else None
    env = _env_dir("bin")
    candidates = [env / name] if env else []
    found = shutil.which(name)
    if found:
        candidates.append(Path(found))
    if name in _CHECKOUT_PROGRAMS:
        candidates.append(REPO_ROOT / _CHECKOUT_PROGRAMS[name])
    candidates.append(PACKAGE_LIBDIR / "bin" / name)
    for p in candidates:
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def data_file(name: str) -> str | None:
    """Path of a data file (jsc.json), checkout first, then the package."""
    candidates = []
    if name in _CHECKOUT_DATA:
        candidates.append(REPO_ROOT / _CHECKOUT_DATA[name])
    if name in _PACKAGE_DATA:
        candidates.append(PACKAGE_SHAREDIR / _PACKAGE_DATA[name])
    for p in candidates:
        if p.is_file():
            return str(p)
    return None


def user_dir(kind: str, app: str = "pluto-tx", create: bool = False) -> Path:
    """Per-user directory by XDG kind (config, state, cache, data):
    $XDG_<KIND>_HOME/<app>, default ~/.config/<app> etc."""
    var, default = _XDG[kind]
    base = os.environ.get(var, "").strip()
    root = Path(base) if base and os.path.isabs(base) else Path.home() / default
    path = root / app
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path
