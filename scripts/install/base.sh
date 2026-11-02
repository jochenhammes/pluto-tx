#!/usr/bin/env bash
# The base of every installation: apt packages, groups, the DVB-T blacklist, a check of the Python side.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
refuse_root
require_apt

required=() optional=()
while read -r line; do
    case "$line" in
        ''|'#'*) ;;
        '?'*) optional+=("${line#\?}") ;;
        *) required+=("$line") ;;
    esac
done <"$INSTALL_DIR/packages-base.txt"

apt_need "${required[@]}"
for pkg in "${optional[@]}"; do
    if apt_has "$pkg"; then
        apt_need "$pkg"
    else
        notice "$pkg gibt es auf dieser Distribution nicht als Paket: Meshtastic bleibt aus. Abhilfe: pip install --user $pkg"
    fi
done

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
#
# dialout is the same story for the serial ports: the X6200's CAT, the AIOC's PTT, the ESP32-S3.
added=()
for group in plugdev dialout; do
    if ! id -nG "$USER" | tr ' ' '\n' | grep -qx "$group"; then
        sudo usermod -aG "$group" "$USER"
        added+=("$group")
    fi
done
if [ ${#added[@]} -gt 0 ]; then
    notice "Neu in den Gruppen ${added[*]}: Das gilt erst nach erneutem Anmelden (oder Neustart). Bis dahin fehlt der Zugriff auf HackRF, RTL-SDR und serielle Geraete; der Pluto geht schon."
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
# Written by pluto-tx's ./install.sh: dvb_usb_rtl28xxu is the Linux kernel's
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
    import serial  # noqa: F401  -- pluto_tx's AIOC TX device backend (devices/aioc.py)
except ImportError as e:
    print(f"FAILED: {e}", file=sys.stderr)
    sys.exit(1)
print("All required Python modules import cleanly.")
EOF
