#!/usr/bin/env bash
# Optional, separate installer for the ESP32-S3 TX device of pluto-tx ("ESP32-S3 (13 cm)": FM and M17 on 2320..2450 MHz from an ESP32-S3 dev
# board). Kept apart from install.sh like install-m17.sh: the device is optional, and it needs two things apt cannot give:
#
#   * esptool 5 or newer, which loads the transmitter firmware into the board's RAM. Debian/Ubuntu's own esptool package is version 4, which
#     does not work with this firmware loader, and pip may not write to the system Python (PEP 668): esptool is installed with pipx.
#   * the board's udev rule (keeps ModemManager from probing the port with AT commands; any byte it sends would stop a transmission).
#
# The espdr code itself (host side of the ESP32 transmitter) and the firmware images are part of this repository (./espdr), nothing is
# downloaded for them. Needs an ESP32-S3 dev board with TWO USB ports (the second one is the USB-UART bridge that loads the firmware).
#
# Usage:
#   ./install.sh               # first, if you haven't already
#   ./install-espdr.sh [--yes] [--no-udev] [--dry-run]
#
#   --yes       do not ask before installing the udev rule (it is installed with sudo)
#   --no-udev   do not offer the udev rule
#   --dry-run   show what would be done, change nothing
#
# Safe to re-run. Transmitting takes an amateur radio licence: the transmitter asks you to confirm its notice once (see the end of this script).
set -euo pipefail

if [ "$(id -u)" = "0" ]; then
    echo "FEHLER: Dieses Skript nicht mit sudo oder als root ausfuehren." >&2
    echo "Richtig: ./install-espdr.sh   (das Skript ruft sudo intern fuer apt-get und die udev-Regel auf)" >&2
    exit 1
fi

yes=0; udev=1; dry=0
for a in "$@"; do
    case "$a" in
        --yes) yes=1 ;;
        --no-udev) udev=0 ;;
        --dry-run) dry=1 ;;
        -h|--help) sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option $a (see --help)" >&2; exit 2 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RULE_SRC="$SCRIPT_DIR/espdr/udev/70-espdr.rules"
RULE_DST=/etc/udev/rules.d/70-espdr.rules

say() { printf '\n== %s\n' "$*"; }
run() { if [ "$dry" = 1 ]; then echo "[dry run] $*"; else "$@"; fi; }

echo "== ESP32-S3 TX device installer =="
echo "Repo directory: $SCRIPT_DIR"
[ "$dry" = 1 ] && echo "(dry run: nothing is changed)"

[ -d "$SCRIPT_DIR/espdr" ] && [ -f "$SCRIPT_DIR/espdr/images/tx.bin" ] || {
    echo "$SCRIPT_DIR/espdr (with images/tx.bin) is missing: this checkout is incomplete. tools/sync-espdr.sh makes it." >&2; exit 1; }

say "Python modules (numpy, pyserial) and pipx"
if command -v apt-get >/dev/null 2>&1; then
    run sudo apt-get install -y python3-numpy python3-serial pipx
else
    echo "apt-get not found: install numpy, pyserial and pipx with your package manager." >&2
fi

say "esptool 5 or newer, with pipx"
need_esptool=1
if command -v esptool >/dev/null 2>&1; then
    ver="$(esptool version 2>/dev/null | grep -Eo '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -n1 || true)"
    if [ -n "$ver" ] && [ "${ver%%.*}" -ge 5 ] 2>/dev/null; then
        echo "esptool $ver is installed, good."
        need_esptool=0
    else
        echo "an esptool is on the PATH (version '${ver:-unknown}'), but 5 or newer is needed."
    fi
fi
if [ "$need_esptool" = 1 ]; then
    # --force replaces an older pipx install; a distribution's esptool 4 in /usr/bin is shadowed by ~/.local/bin on the PATH
    run pipx install --force esptool
    run pipx ensurepath
    case ":$PATH:" in
        *":$HOME/.local/bin:"*) ;;
        *) echo "note: $HOME/.local/bin is not on your PATH in this shell; open a new terminal (pipx ensurepath changed your profile)." ;;
    esac
fi

if [ "$udev" = 1 ]; then
    say "udev rule for the board's USB port"
    if [ -f "$RULE_DST" ] && cmp -s "$RULE_SRC" "$RULE_DST"; then
        echo "$RULE_DST is already installed."
    else
        ok=$yes
        if [ "$ok" = 0 ] && [ "$dry" = 0 ]; then
            read -r -p "install $RULE_DST with sudo (keeps ModemManager off the board)? [y/N] " answer || answer=n
            case "$answer" in y|Y|yes|YES) ok=1 ;; esac
        fi
        if [ "$ok" = 1 ] || [ "$dry" = 1 ]; then
            run sudo cp "$RULE_SRC" "$RULE_DST"
            run sudo udevadm control --reload
            run sudo udevadm trigger
        else
            echo "skipped. Without it, stop ModemManager yourself or the first transmission may fail."
        fi
    fi
fi

say "access to the board's serial ports"
if id -nG "$USER" | tr ' ' '\n' | grep -qx dialout; then
    echo "$USER is in the group dialout, good."
else
    echo "$USER is not in the group dialout; the board's USB-UART port (/dev/ttyACM* or /dev/ttyUSB*) needs it:"
    echo "    sudo usermod -aG dialout $USER     (then log out and in again)"
fi

say "done"
cat <<'EOF'
Start pluto-tx, choose the device "ESP32-S3 (13 cm)" and leave the connection blank (the board is found by its USB ports), or on the
command line:  pluto-cli tx fm --device espdr --freq 2345000000   (and `m17`, with --src-callsign). Only FM and M17 are carried by this device.
The firmware is loaded into the board's RAM when needed; nothing is written to its flash.

Before the first transmission confirm the licence notice of the ESP32 transmitter once (it sends nothing):
    python3 -m espdr.cli_tx -f 2345 --test-tone 1000 --dry-run --accept-licence
Transmitting takes an amateur radio licence, and you are responsible for what you send.
EOF
