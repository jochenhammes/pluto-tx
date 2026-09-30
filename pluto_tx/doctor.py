"""pluto-tx-doctor: what this installation finds, and what stands in the way.

  python3 -m pluto_tx.doctor [--scan] [--fix-launchers]

Checks: which pluto-tx/pluto-advanced-rx/pluto-cli/web-trx wins on PATH (old
launchers of a git installation in ~/.local/bin hide the package's /usr/bin
ones), the optional components (M17, LoRa, RADE, FT8, JS8, Meshtastic), USB
access to connected HackRF/RTL-SDR devices, the RTL-SDR DVB-T kernel driver,
the clock (FT8/JS8 need it within about a second) and the Web-TRX server.
--scan also lists the SDRs SoapySDR and libiio find (takes a few seconds).
--fix-launchers renames old launchers that hide the package's ones to
<name>.git-launcher (nothing is deleted).

Never opens a device and never transmits. Exit code 0: nothing to report,
1: at least one warning or failure.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import paths
from .version import version

PROGRAMS = ("pluto-tx", "pluto-advanced-rx", "pluto-cli", "web-trx")
PACKAGE_BIN = Path("/usr/bin")
# (vendor, product) -> name; the same devices as the package's udev rule
USB_SDRS = {
    ("1d50", "6089"): "HackRF One", ("1d50", "604b"): "HackRF Jawbreaker", ("1d50", "cc15"): "rad1o",
    ("1fc9", "000c"): "HackRF (DFU mode)", ("0bda", "2838"): "RTL-SDR", ("0bda", "2832"): "RTL-SDR",
    ("0456", "b673"): "PlutoSDR", ("0456", "b674"): "PlutoSDR (DFU mode)",
}


class Report:
    def __init__(self):
        self.problems = 0

    def line(self, level: str, text: str, hint: str = ""):
        if level in ("WARN", "FAIL"):
            self.problems += 1
        print(f"[{level:^4}] {text}")
        if hint:
            for h in hint.splitlines():
                print(f"       {h}")

    def ok(self, text, hint=""):
        self.line("OK", text, hint)

    def warn(self, text, hint=""):
        self.line("WARN", text, hint)

    def fail(self, text, hint=""):
        self.line("FAIL", text, hint)

    def info(self, text, hint=""):
        self.line("INFO", text, hint)


def _run(cmd, timeout=10.0) -> tuple[int, str]:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return out.returncode, (out.stdout + out.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)


def check_launchers(r: Report, fix: bool):
    packaged = [name for name in PROGRAMS if (PACKAGE_BIN / name).exists()]
    for name in PROGRAMS:
        found = shutil.which(name)
        if found is None:
            r.warn(f"{name}: not on PATH")
            continue
        found_path = Path(found)
        if name in packaged and found_path.resolve() != (PACKAGE_BIN / name).resolve():
            if fix:
                backup = found_path.with_name(found_path.name + ".git-launcher")
                found_path.rename(backup)
                r.ok(f"{name}: old launcher {found} renamed to {backup.name}; /usr/bin/{name} is used now")
            else:
                r.warn(f"{name}: {found} hides the package's /usr/bin/{name}",
                       "an old launcher of a git installation -- rename it with: pluto-tx-doctor --fix-launchers")
        else:
            r.ok(f"{name}: {found}")


def check_components(r: Report):
    try:
        from gnuradio import gr
        r.ok(f"GNU Radio {gr.version()}")
    except ImportError as e:
        r.fail(f"GNU Radio not importable: {e}", "sudo apt install gnuradio")
        return
    for mod, label in (("m17", "M17 (gr-m17)"), ("lora_sdr", "LoRa (gr-lora_sdr)")):
        try:
            __import__(f"gnuradio.{mod}")
            r.ok(label)
        except ImportError as e:
            r.warn(f"{label} not available: {e}", _hint("install-m17.sh" if mod == "m17" else "install-lora.sh"))
    from . import ft8_ctypes, rade_ctypes
    lib = next((c for c in paths.library_candidates("libft8wrap.so")[1:]), "via loader path")
    if ft8_ctypes.FT8_AVAILABLE:
        r.ok(f"FT8 (ft8_lib): {lib}")
    else:
        r.warn("FT8 (ft8_lib) not available", _hint("install-ft8.sh"))
    jt9 = shutil.which("jt9")
    if jt9:
        r.ok(f"FT8 decoder jt9 (WSJT-X): {jt9}")
    else:
        r.info("jt9 (WSJT-X) not installed -- FT8 RX uses ft8_lib", "optional, better decoder: sudo apt install wsjtx")
    if rade_ctypes.RADE_AVAILABLE:
        r.ok(f"RADE (rade_c): lpcnet_demo {paths.program('lpcnet_demo')}")
    else:
        r.warn("RADE (rade_c) not available", _hint("install-rade.sh"))
    js8ref, jsc = paths.program("js8ref"), paths.data_file("jsc.json")
    if js8ref and jsc:
        r.ok(f"JS8: js8ref {js8ref}, word lists {jsc}")
    else:
        r.warn(f"JS8 incomplete (js8ref: {js8ref or 'missing'}, jsc.json: {jsc or 'missing'})", _hint("install-js8.sh"))
    for mod, label in (("meshtastic", "Meshtastic packet layers"), ("cryptography", "cryptography (MeshCore)")):
        try:
            __import__(mod)
            r.ok(label)
        except ImportError:
            r.warn(f"{label}: Python package '{mod}' missing")


def _hint(installer: str) -> str:
    if paths.is_package():
        return "part of the pluto-tx package -- reinstall it: sudo apt install --reinstall pluto-tx"
    return f"git installation: run ./{installer}"


def check_usb(r: Report):
    base = Path("/sys/bus/usb/devices")
    found = 0
    for dev in sorted(base.glob("*")) if base.is_dir() else []:
        try:
            vid = (dev / "idVendor").read_text().strip()
            pid = (dev / "idProduct").read_text().strip()
        except OSError:
            continue
        name = USB_SDRS.get((vid, pid))
        if not name:
            continue
        found += 1
        try:
            node = Path(f"/dev/bus/usb/{int((dev / 'busnum').read_text()):03d}/{int((dev / 'devnum').read_text()):03d}")
        except (OSError, ValueError):
            r.info(f"{name} ({vid}:{pid}) connected")
            continue
        if name.startswith("PlutoSDR"):
            r.ok(f"{name} connected ({node})")
        elif os.access(node, os.R_OK | os.W_OK):
            r.ok(f"{name} connected, accessible ({node})")
        else:
            r.warn(f"{name} connected, but no access to {node}",
                   "replug it once after installing the package (udev rule 60-pluto-tx.rules), or log in again")
    if not found:
        r.info("no HackRF/RTL-SDR/PlutoSDR on USB (a PlutoSDR on the network is fine)")
    if Path("/sys/module/dvb_usb_rtl28xxu").exists():
        r.warn("kernel DVB-T driver dvb_usb_rtl28xxu is loaded -- it blocks RTL-SDR dongles",
               "unplug and replug the RTL-SDR, or: sudo rmmod dvb_usb_rtl28xxu")


def check_clock(r: Report):
    if not shutil.which("timedatectl"):
        r.info("timedatectl missing -- cannot check the clock (FT8/JS8 need it within ~1 s)")
        return
    rc, out = _run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], timeout=5)
    if rc == 0 and out == "yes":
        r.ok("clock synchronized (NTP) -- good for FT8/JS8")
    elif rc == 0:
        r.warn("clock not NTP-synchronized -- FT8/JS8 need it within about a second",
               "sudo timedatectl set-ntp true")
    else:
        r.info(f"clock state unknown ({out or rc})")


def check_webtrx(r: Report):
    from . import webtrx_control as wc
    ctl = wc.WebTrxControl()
    st = ctl.status(force_idle_health=True)
    where = f"data in {ctl.run_dir}"
    if st.state == wc.RUNNING:
        r.ok(f"Web-TRX running (PID {st.pid}, backend {st.backend}) at {st.url} -- {where}")
    elif st.state == wc.STOPPED:
        r.ok(f"Web-TRX installed, stopped (port {ctl.port()}) -- start: web-trx start")
    elif st.state == wc.NOT_INSTALLED:
        r.info(f"Web-TRX not installed: {st.detail}")
    else:
        r.warn(f"Web-TRX {st.state}: {st.detail or st.url}")


def scan(r: Report):
    if shutil.which("SoapySDRUtil"):
        rc, out = _run(["SoapySDRUtil", "--find"], timeout=20)
        devices = [ln.strip() for ln in out.splitlines() if ln.strip().startswith(("driver", "label"))]
        r.info("SoapySDR: " + ("; ".join(devices) if devices else "no devices"))
    if shutil.which("iio_info"):
        rc, out = _run(["iio_info", "-s"], timeout=20)
        contexts = [ln.strip() for ln in out.splitlines() if "[" in ln and ("usb:" in ln or "ip:" in ln)]
        r.info("libiio: " + ("; ".join(contexts) if contexts else "no PlutoSDR found"))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="pluto-tx-doctor", description=__doc__.split("\n\n")[0])
    p.add_argument("--version", action="version", version=f"%(prog)s {version()}")
    p.add_argument("--scan", action="store_true", help="also list the SDRs SoapySDR and libiio find")
    p.add_argument("--fix-launchers", action="store_true",
                   help="rename old git-installation launchers that hide the package's ones")
    args = p.parse_args(argv)
    r = Report()
    layout = "package" if paths.is_package() else f"git checkout {paths.REPO_ROOT}"
    print(f"pluto-tx {version()} ({layout})\n")
    for title, fn in (("Programs", lambda: check_launchers(r, args.fix_launchers)),
                      ("Components", lambda: check_components(r)),
                      ("Devices", lambda: check_usb(r)),
                      ("Clock", lambda: check_clock(r)),
                      ("Web-TRX", lambda: check_webtrx(r))):
        print(f"-- {title}")
        try:
            fn()
        except Exception as e:  # a broken check must not hide the others
            r.fail(f"check failed: {type(e).__name__}: {e}")
        print()
    if args.scan:
        print("-- Scan")
        scan(r)
        print()
    print("Nothing to report." if r.problems == 0 else f"{r.problems} warning(s) or failure(s), see above.")
    return 0 if r.problems == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
