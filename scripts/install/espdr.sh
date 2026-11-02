#!/usr/bin/env bash
# The ESP32-S3 as a TX device ("ESP32-S3 (13 cm)": FM and M17 on 2320 to 2450 MHz). Run through ./install.sh. Needs a
# board with two USB ports; the code and firmware images are in this repository.
#
#   esptool 5 or newer loads the firmware into the board's RAM. The distribution's esptool 4 does not work with this
#   loader, and pip may not write to the system Python: hence pipx.
#   The board's udev rule keeps ModemManager from probing the port with AT commands; any byte it sends would stop the
#   transmitter.
#
# Option after --: --no-udev. The rule is installed with sudo after asking, or at once with --yes.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
refuse_root

udev=1
for a in "$@"; do
    case "$a" in
        --no-udev) udev=0 ;;
        *) echo "Unbekannte Option: $a" >&2; exit 2 ;;
    esac
done

RULE_SRC="$REPO_DIR/espdr/udev/70-espdr.rules"
RULE_DST=/etc/udev/rules.d/70-espdr.rules
if [ ! -f "$REPO_DIR/espdr/images/tx.bin" ]; then
    echo "FEHLER: $REPO_DIR/espdr/images/tx.bin fehlt: Der Checkout ist unvollstaendig. tools/sync-espdr.sh holt es." >&2
    exit 1
fi

apt_need python3-numpy python3-serial pipx

need_esptool=1
if command -v esptool >/dev/null 2>&1; then
    ver="$(esptool version 2>/dev/null | grep -Eo '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -n1 || true)"
    if [ -n "$ver" ] && [ "${ver%%.*}" -ge 5 ] 2>/dev/null; then
        echo "esptool $ver is installed."
        need_esptool=0
    else
        echo "esptool ${ver:-unknown} is on the PATH, but 5 or newer is needed."
    fi
fi
if [ "$need_esptool" = 1 ]; then
    # --force replaces an older pipx install; a distribution's esptool 4 in /usr/bin is shadowed by ~/.local/bin
    pipx install --force esptool
    pipx ensurepath
fi

if [ "$udev" = 1 ]; then
    if [ -f "$RULE_DST" ] && cmp -s "$RULE_SRC" "$RULE_DST"; then
        echo "$RULE_DST is already installed."
    else
        ok="${PLUTO_INSTALL_YES:-0}"
        if [ "$ok" = 0 ] && [ -t 0 ]; then
            read -r -p "install $RULE_DST with sudo (keeps ModemManager off the board)? [y/N] " answer || answer=n
            case "$answer" in y|Y|j|J|yes|YES) ok=1 ;; esac
        fi
        if [ "$ok" = 1 ]; then
            sudo cp "$RULE_SRC" "$RULE_DST"
            # no udevd in a container or chroot: the rule still applies on the next boot
            sudo udevadm control --reload && sudo udevadm trigger || notice "ESP32-S3: udev laeuft hier nicht, die Regel gilt ab dem naechsten Start."
        else
            notice "ESP32-S3: udev-Regel nicht installiert. Ohne sie ModemManager selbst stoppen, sonst kann die erste Sendung scheitern."
        fi
    fi
fi

notice "ESP32-S3: Vor der ersten Sendung den Lizenzhinweis einmal bestaetigen (sendet nichts): python3 -m espdr.cli_tx -f 2345 --test-tone 1000 --dry-run --accept-licence"
