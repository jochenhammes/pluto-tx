#!/usr/bin/env bash
# Installs everything the pluto_tx / pluto_rx / pluto_advanced_rx apps need
# on a Debian/Ubuntu Linux machine: GNU Radio (which pulls in gr-iio and
# PyQt5 as hard dependencies of its own "gnuradio" package), raw
# python3-libiio (used directly by safety.py/netutil.py for the TX
# attenuation/LO-powerdown safety layer and the connect-timeout/device-scan
# helpers -- a different Python module ("iio") from gnuradio.iio, not
# pulled in by "gnuradio" itself), libiio-utils (iio_info/iio_attr, handy
# for manual troubleshooting), avahi-daemon (actually RESOLVES "*.local"
# mDNS hostnames like plutoplus.local -- libiio only gets the avahi CLIENT
# libraries for free as a hard dependency; the daemon itself is merely an
# apt "Suggests", so it's easy to end up without it on a fresh install),
# python3-pyqtgraph (pluto_advanced_rx's interactive waterfall widget --
# only a "Recommends" of "gnuradio", not a hard dependency, so also easy to
# end up without on a fresh install), and the HackRF/RTL-SDR/SoapySDR pieces
# pluto_tx (HackRF TX) and pluto_advanced_rx (HackRF/RTL-SDR RX) need for
# their non-Pluto device backends (gnuradio.soapy itself ships inside
# "gnuradio" already -- these packages are the missing driver/tooling bits).
#
# Usage:
#   ./install.sh
#
# Safe to re-run: apt-get install on already-installed packages is a no-op.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== pluto-tx / pluto-rx / pluto-advanced-rx installer =="
echo "Repo directory: $SCRIPT_DIR"
echo

if ! command -v apt-get >/dev/null 2>&1; then
    cat >&2 <<'EOF'
This installer only supports Debian/Ubuntu-family systems (apt-get not
found). Install these manually instead, then re-run this script to get the
launcher scripts and the self-test:
  - GNU Radio 3.10+ with the gr-iio ("iio") blocks and PyQt5 support
  - python3-libiio (raw libiio Python bindings, importable as "iio")
  - libiio-utils (iio_info, iio_attr -- optional but handy)
  - an mDNS resolver/daemon (e.g. avahi-daemon) if you want to use
    hostnames like plutoplus.local instead of a bare IP address
  - python3-pyqtgraph, if you want to use pluto_advanced_rx's interactive
    waterfall
  - soapysdr-module-hackrf, hackrf, and python3-soapysdr, if you want to use
    pluto_tx's HackRF One TX backend or pluto_advanced_rx's HackRF RX backend
  - soapysdr-module-rtlsdr and rtl-sdr, if you want to use pluto_advanced_rx's
    RTL-SDR RX backend
  - Pillow (python3-pil) and a monospace TTF font (e.g. fonts-dejavu-mono), if
    you want to use pluto_tx's Digitext digimode
EOF
    exit 1
fi

PACKAGES=(
    gnuradio          # pulls in gr-iio (libgnuradio-iio) and python3-pyqt5 as hard deps
    python3-libiio    # raw libiio Python bindings ("import iio") -- NOT pulled in by gnuradio
    libiio-utils      # iio_info, iio_attr -- optional, useful for manual troubleshooting
    avahi-daemon      # resolves "*.local" mDNS hostnames; libiio only gets the client libs for free
    python3-pyqtgraph # pluto_advanced_rx's interactive waterfall -- only a gnuradio "Recommends", not a hard dep
    soapysdr-module-hackrf # gr-soapy's HackRF driver .so -- gnuradio.soapy itself is already
                            # part of "gnuradio" above; this is the one missing piece for the
                            # HackRF backends in pluto_tx (TX) and pluto_advanced_rx (RX)
    hackrf            # hackrf_info etc., for manual troubleshooting -- mirrors libiio-utils above
    soapysdr-module-rtlsdr # gr-soapy's RTL-SDR driver .so, for pluto_advanced_rx's RTL-SDR RX backend
    rtl-sdr           # rtl_test etc., for manual troubleshooting -- mirrors libiio-utils above
    python3-soapysdr  # raw SoapySDR Python bindings, used only for structured HackRF/RTL-SDR
                       # device enumeration in the GUI Scan buttons -- gnuradio.soapy's own
                       # source/sink blocks don't need this, they link libsoapysdr directly in C++
    python3-pil        # Pillow, for pluto_tx's Digitext digimode (text-to-image rendering,
                        # pluto_tx/digitext.py) -- a plain Python dependency, no from-source build
    fonts-dejavu-mono   # DejaVu Sans Mono specifically (verified via `dpkg -S`: the Mono variant
                        # is its OWN package, separate from fonts-dejavu-core, which only has the
                        # proportional Sans/Serif faces) -- digitext.py's preferred font, not
                        # guaranteed present on a minimal system otherwise; falls back to Pillow's
                        # own built-in bitmap font if this is somehow still missing
    git               # to clone/update this repo
)

echo "Installing: ${PACKAGES[*]}"
echo "(you may be asked for your sudo password)"
echo
export NEEDRESTART_MODE=a  # avoid an interactive "restart services?" prompt if 'needrestart' happens to be installed
sudo apt-get update
sudo apt-get install -y "${PACKAGES[@]}"

echo
echo "Enabling avahi-daemon (mDNS hostname resolution, e.g. plutoplus.local)..."
if command -v systemctl >/dev/null 2>&1; then
    sudo systemctl enable --now avahi-daemon \
        || echo "WARNING: could not enable/start avahi-daemon -- '.local' hostnames may not resolve; use a bare IP instead." >&2
else
    echo "No systemctl found -- start avahi-daemon yourself if you want '.local' hostname resolution." >&2
fi

echo
echo "Setting up non-root USB access for HackRF/RTL-SDR..."
# PlutoSDR's own udev rule (shipped by libiio0, a python3-libiio dependency)
# is world-accessible (MODE="666") -- nothing further needed for it. HackRF/
# RTL-SDR are different: their udev rules (shipped by libhackrf0/librtlsdr0,
# pulled in transitively by the hackrf/rtl-sdr packages above) grant access
# only to the "plugdev" group (MODE="0660", GROUP="plugdev") -- verified by
# reading the installed rule files this session, not assumed. plugdev is a
# base-passwd system group (Priority: required), so it always exists on
# Debian/Ubuntu; adding the user to it is something this script CAN do, but
# a NEW group membership only takes effect in a FRESH login session (a new
# shell in the current session is not enough) -- that final step is
# unavoidably manual, so it's called out explicitly below.
if id -nG "$USER" | tr ' ' '\n' | grep -qx plugdev; then
    echo "$USER is already in the plugdev group."
else
    echo "Adding $USER to the plugdev group (needed for non-root HackRF/RTL-SDR USB access)..."
    sudo usermod -aG plugdev "$USER"
    PLUGDEV_JUST_ADDED=1
fi

# RTL-SDR dongles are auto-claimed by the Linux kernel's own DVB-T driver
# (dvb_usb_rtl28xxu) the moment they're plugged in -- the single most common
# real-world "RTL-SDR on Linux" gotcha, and unrelated to the plugdev
# permission above (this is the KERNEL grabbing the device before librtlsdr/
# gr-soapy ever get a chance to, not a file-permission problem -- typically
# surfaces as "usb_claim_interface error -6"). Verified on this system this
# session: the module is present/loadable and not blacklisted anywhere.
# Only relevant if you actually use an RTL-SDR dongle as an SDR receiver,
# not as a DVB-T TV tuner -- if you need both uses on the same machine,
# remove the blacklist file below instead of running this script.
RTL_BLACKLIST_FILE="/etc/modprobe.d/blacklist-rtl-sdr.conf"
if [ -f "$RTL_BLACKLIST_FILE" ]; then
    echo "RTL-SDR's DVB-T kernel driver is already blacklisted ($RTL_BLACKLIST_FILE)."
else
    echo "Blacklisting the RTL-SDR DVB-T kernel driver (dvb_usb_rtl28xxu)..."
    sudo tee "$RTL_BLACKLIST_FILE" >/dev/null <<'BLACKLIST_EOF'
# Written by pluto-tx's install.sh: dvb_usb_rtl28xxu is the Linux kernel's
# own DVB-T driver for RTL2832U-based USB dongles. It auto-claims the
# device as soon as it's plugged in, which blocks librtlsdr/gr-soapy (used
# by pluto_advanced_rx's RTL-SDR RX backend) from ever opening it. Remove
# this file if you also want to use the same dongle as a normal DVB-T TV
# tuner.
blacklist dvb_usb_rtl28xxu
BLACKLIST_EOF
fi
if lsmod | grep -q '^dvb_usb_rtl28xxu'; then
    echo "NOTE: dvb_usb_rtl28xxu is currently loaded. The blacklist above only"
    echo "stops it from being loaded again -- unplug and replug your RTL-SDR (or"
    echo "run 'sudo rmmod dvb_usb_rtl28xxu' now) to actually unload it, instead"
    echo "of rebooting."
fi

echo
echo "Verifying the Python side..."
python3 - <<'EOF'
import sys
try:
    from gnuradio import gr, blocks, filter, analog, audio, iio, soapy, qtgui  # noqa: F401
    from gnuradio.fft import window  # noqa: F401
    from gnuradio.filter import firdes  # noqa: F401
    import iio as libiio  # noqa: F401  -- raw python3-libiio, distinct from gnuradio.iio above
    from PyQt5 import QtCore, QtWidgets, sip  # noqa: F401
    import pyqtgraph  # noqa: F401  -- pluto_advanced_rx's interactive waterfall
    import SoapySDR  # noqa: F401  -- HackRF/RTL-SDR device scanning in the GUI Scan buttons
    from PIL import Image, ImageDraw, ImageFont  # noqa: F401  -- pluto_tx's Digitext digimode
except ImportError as e:
    print(f"FAILED: {e}", file=sys.stderr)
    sys.exit(1)
print("All required Python modules import cleanly.")
EOF

echo
echo "Setting up launcher scripts in ~/.local/bin ..."
mkdir -p "$HOME/.local/bin"

cat > "$HOME/.local/bin/pluto-tx" <<EOF
#!/usr/bin/env bash
cd "$SCRIPT_DIR" && exec python3 -m pluto_tx.app --gui "\$@"
EOF
chmod +x "$HOME/.local/bin/pluto-tx"

cat > "$HOME/.local/bin/pluto-rx" <<EOF
#!/usr/bin/env bash
cd "$SCRIPT_DIR" && exec python3 -m pluto_rx.app "\$@"
EOF
chmod +x "$HOME/.local/bin/pluto-rx"

cat > "$HOME/.local/bin/pluto-advanced-rx" <<EOF
#!/usr/bin/env bash
cd "$SCRIPT_DIR" && exec python3 -m pluto_advanced_rx.app "\$@"
EOF
chmod +x "$HOME/.local/bin/pluto-advanced-rx"

echo "Created $HOME/.local/bin/pluto-tx, pluto-rx, and pluto-advanced-rx"

case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *)
        echo
        echo "NOTE: $HOME/.local/bin is not on your PATH yet. Add this to your"
        echo "~/.bashrc (or ~/.zshrc) and open a new shell:"
        echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
        ;;
esac

if [ "${PLUGDEV_JUST_ADDED:-}" = "1" ]; then
    echo
    echo "IMPORTANT: you were just added to the 'plugdev' group (needed for"
    echo "non-root HackRF/RTL-SDR USB access). This only takes effect in a NEW"
    echo "login session -- log out and back in (or reboot) before using a"
    echo "HackRF or RTL-SDR device. PlutoSDR access is unaffected, works now."
fi

echo
echo "== Done =="
echo "Start the apps with:"
echo "    pluto-tx"
echo "    pluto-rx"
echo "    pluto-advanced-rx"
echo "(all accept --uri/--freq/etc. -- see e.g. 'pluto-tx --help'."
echo " Without a launcher on PATH, run them directly from $SCRIPT_DIR instead:"
echo "    python3 -m pluto_tx.app --gui"
echo "    python3 -m pluto_rx.app"
echo "    python3 -m pluto_advanced_rx.app)"
