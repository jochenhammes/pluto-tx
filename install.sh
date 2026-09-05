#!/usr/bin/env bash
# Installs everything the pluto_tx / pluto_rx apps need on a Debian/Ubuntu
# Linux machine: GNU Radio (which pulls in gr-iio and PyQt5 as hard
# dependencies of its own "gnuradio" package), raw python3-libiio (used
# directly by safety.py/netutil.py for the TX attenuation/LO-powerdown
# safety layer and the connect-timeout/device-scan helpers -- a different
# Python module ("iio") from gnuradio.iio, not pulled in by "gnuradio"
# itself), libiio-utils (iio_info/iio_attr, handy for manual
# troubleshooting), and avahi-daemon (actually RESOLVES "*.local" mDNS
# hostnames like plutoplus.local -- libiio only gets the avahi CLIENT
# libraries for free as a hard dependency; the daemon itself is merely an
# apt "Suggests", so it's easy to end up without it on a fresh install).
#
# Usage:
#   ./install.sh
#
# Safe to re-run: apt-get install on already-installed packages is a no-op.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== pluto-tx / pluto-rx installer =="
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
EOF
    exit 1
fi

PACKAGES=(
    gnuradio        # pulls in gr-iio (libgnuradio-iio) and python3-pyqt5 as hard deps
    python3-libiio  # raw libiio Python bindings ("import iio") -- NOT pulled in by gnuradio
    libiio-utils    # iio_info, iio_attr -- optional, useful for manual troubleshooting
    avahi-daemon    # resolves "*.local" mDNS hostnames; libiio only gets the client libs for free
    git             # to clone/update this repo
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
echo "Verifying the Python side..."
python3 - <<'EOF'
import sys
try:
    from gnuradio import gr, blocks, filter, analog, audio, iio, qtgui  # noqa: F401
    from gnuradio.fft import window  # noqa: F401
    from gnuradio.filter import firdes  # noqa: F401
    import iio as libiio  # noqa: F401  -- raw python3-libiio, distinct from gnuradio.iio above
    from PyQt5 import QtCore, QtWidgets, sip  # noqa: F401
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

echo "Created $HOME/.local/bin/pluto-tx and $HOME/.local/bin/pluto-rx"

case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *)
        echo
        echo "NOTE: $HOME/.local/bin is not on your PATH yet. Add this to your"
        echo "~/.bashrc (or ~/.zshrc) and open a new shell:"
        echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
        ;;
esac

echo
echo "== Done =="
echo "Start the apps with:"
echo "    pluto-tx"
echo "    pluto-rx"
echo "(both accept --uri/--freq/etc. -- see 'pluto-tx --help' / 'pluto-rx --help'."
echo " Without a launcher on PATH, run them directly from $SCRIPT_DIR instead:"
echo "    python3 -m pluto_tx.app --gui"
echo "    python3 -m pluto_rx.app)"
