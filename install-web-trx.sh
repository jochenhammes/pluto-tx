#!/usr/bin/env bash
# Optional, separate installer for Web-TRX (web-trx/), the browser UI for
# pluto-tx/pluto-advanced-rx. Kept apart from install.sh on purpose, like
# install-m17.sh & co: install.sh stays pure apt, and nothing here touches
# what install.sh set up (packages, udev, the pluto-* launchers).
#
# What it does:
#   - checks the prerequisites (GNU Radio + libiio from install.sh, Python
#     >= 3.11, python3-venv, openssl, Node.js >= 18 for the frontend build)
#   - creates web-trx/backend/.venv WITH --system-site-packages (GNU Radio
#     and libiio come from the system packages, pip cannot install them) and
#     installs the backend into it
#   - builds the frontend (web-trx/frontend/dist)
#   - self-test: GNU Radio, numpy, libiio and the backend import together
#     in that venv, and numpy is the system's one (a second numpy in the
#     venv would not match GNU Radio's compiled modules)
#   - writes the launcher ~/.local/bin/web-trx
#
# It never creates web-trx/web-trx.env (start.sh generates a password on the
# first start) and never copies or moves an existing venv.
#
# Usage:
#   ./install.sh                  # first, if you haven't already
#   ./install-web-trx.sh [--constraints FILE] [--reuse-dist DIR]
#
#   --constraints FILE   pip constraints, e.g. a `pip freeze` of an older
#                        Web-TRX venv, to get exactly the versions that ran
#                        before (lines for web-trx itself and editable or
#                        direct-URL installs are dropped automatically)
#   --reuse-dist DIR     take DIR/frontend/dist from an older Web-TRX checkout
#                        instead of building (no Node.js needed) -- only if
#                        that checkout's frontend sources are identical
#   --sim-only           machine without GNU Radio (development): skip the
#                        GNU Radio/libiio checks; only WEB_TRX_BACKEND=sim works
#
# Safe to re-run.
set -euo pipefail

if [ "$(id -u)" = "0" ]; then
    echo "FEHLER: Dieses Skript nicht mit sudo oder als root ausfuehren." >&2
    echo "Richtig: ./install-web-trx.sh   (das Skript ruft sudo intern fuer apt-get auf)" >&2
    exit 1
fi
if [ -n "${SUDO_USER:-}" ]; then
    HOME="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
    export HOME
    echo "HINWEIS: HOME auf $HOME korrigiert (SUDO_USER=$SUDO_USER)." >&2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEB_TRX_DIR="$SCRIPT_DIR/web-trx"
VENV="$WEB_TRX_DIR/backend/.venv"
CONSTRAINTS=""
REUSE_DIST=""
SIM_ONLY=0

while [ $# -gt 0 ]; do
    case "$1" in
        --constraints) CONSTRAINTS="${2:?--constraints needs a file}"; shift 2 ;;
        --reuse-dist) REUSE_DIST="${2:?--reuse-dist needs a directory}"; shift 2 ;;
        --sim-only) SIM_ONLY=1; shift ;;
        -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 0 ;;
        *) echo "Unbekannte Option: $1 (siehe --help)" >&2; exit 64 ;;
    esac
done

echo "== Web-TRX installer =="
echo "Repo directory: $SCRIPT_DIR"
echo

if [ ! -f "$WEB_TRX_DIR/backend/pyproject.toml" ] || [ ! -f "$WEB_TRX_DIR/frontend/package-lock.json" ]; then
    echo "FEHLER: $WEB_TRX_DIR ist unvollstaendig (backend/pyproject.toml oder frontend/package-lock.json fehlt)." >&2
    exit 1
fi

# --- prerequisites ---------------------------------------------------------
if [ "$SIM_ONLY" = 1 ]; then
    echo "HINWEIS: --sim-only -- ohne GNU Radio, nur WEB_TRX_BACKEND=sim wird funktionieren."
elif ! python3 -c 'import gnuradio, iio' 2>/dev/null; then
    echo "FEHLER: GNU Radio/libiio sind fuer python3 nicht importierbar -- zuerst ./install.sh ausfuehren." >&2
    exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "FEHLER: Web-TRX braucht Python >= 3.11 (gefunden: $(python3 --version 2>&1))." >&2
    exit 1
fi

APT_MISSING=()
python3 -c 'import ensurepip' 2>/dev/null || APT_MISSING+=(python3-venv)
command -v openssl >/dev/null 2>&1 || APT_MISSING+=(openssl)
command -v flock >/dev/null 2>&1 || APT_MISSING+=(util-linux)
if [ ${#APT_MISSING[@]} -gt 0 ]; then
    if ! command -v apt-get >/dev/null 2>&1; then
        echo "FEHLER: bitte installieren: ${APT_MISSING[*]}" >&2
        exit 1
    fi
    echo "Installing: ${APT_MISSING[*]} (you may be asked for your sudo password)"
    export NEEDRESTART_MODE=a
    sudo apt-get update
    sudo apt-get install -y "${APT_MISSING[@]}"
fi

FRONTEND_SRC=(src index.html package.json package-lock.json svelte.config.js tsconfig.json vite.config.ts)
if [ -n "$REUSE_DIST" ]; then
    if [ ! -f "$REUSE_DIST/frontend/dist/index.html" ]; then
        echo "FEHLER: $REUSE_DIST/frontend/dist/index.html fehlt -- --reuse-dist nicht moeglich." >&2
        exit 1
    fi
    for item in "${FRONTEND_SRC[@]}"; do
        if ! diff -rq "$REUSE_DIST/frontend/$item" "$WEB_TRX_DIR/frontend/$item" >/dev/null 2>&1; then
            echo "FEHLER: frontend/$item in $REUSE_DIST unterscheidet sich -- dessen dist passt nicht, ohne --reuse-dist neu bauen." >&2
            exit 1
        fi
    done
else
    if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
        NODE_PROBLEM="Node.js/npm nicht gefunden"
    elif [ "$(node -p 'process.versions.node.split(".")[0]')" -lt 18 ]; then
        NODE_PROBLEM="Node.js $(node --version) ist zu alt"
    else
        NODE_PROBLEM=""
    fi
    if [ -n "$NODE_PROBLEM" ]; then
        cat >&2 <<EOF
FEHLER: $NODE_PROBLEM -- der Frontend-Build braucht Node.js >= 18.
Dieses Skript installiert Node.js bewusst nicht (das apt-Paket 'nodejs' ist
auf vielen Distributionen zu alt). Eine aktuelle LTS-Version z. B. mit nvm
installieren (https://github.com/nvm-sh/nvm), dann erneut ausfuehren:
    nvm install --lts
Oder ein bereits gebautes Frontend uebernehmen: --reuse-dist <alter Web-TRX-Checkout>
EOF
        exit 1
    fi
fi

# --- backend venv ----------------------------------------------------------
if [ -d "$VENV" ]; then
    if ! grep -qiE '^include-system-site-packages *= *true' "$VENV/pyvenv.cfg" 2>/dev/null; then
        cat >&2 <<EOF
FEHLER: $VENV existiert, sieht aber die Systempakete (GNU Radio, libiio) nicht
(kein --system-site-packages). Dieses Skript veraendert ein vorhandenes venv
nicht. Beiseitelegen und neu ausfuehren:
    mv "$VENV" "\$HOME/web-trx-venv.alt"
    ./install-web-trx.sh
EOF
        exit 1
    fi
    echo "Using existing venv: $VENV"
else
    echo "Creating venv (with system site packages): $VENV"
    python3 -m venv --system-site-packages "$VENV"
fi

PIP_ARGS=(install -e "$WEB_TRX_DIR/backend[dev]")
if [ -n "$CONSTRAINTS" ]; then
    FILTERED="$(mktemp)"
    trap 'rm -f "$FILTERED"' EXIT
    # web-trx itself, editable (-e) and direct-URL (pkg @ url) lines cannot be constraints
    grep -viE '^(-e |#|web[-_]trx[ =@])| @ ' "$CONSTRAINTS" >"$FILTERED" || true
    PIP_ARGS+=(-c "$FILTERED")
    echo "Using constraints from $CONSTRAINTS ($(wc -l <"$FILTERED") lines)"
fi
"$VENV/bin/python" -m pip "${PIP_ARGS[@]}"

# --- frontend --------------------------------------------------------------
if [ -n "$REUSE_DIST" ]; then
    echo "Taking the built frontend from $REUSE_DIST/frontend/dist"
    rm -rf "$WEB_TRX_DIR/frontend/dist"
    cp -a "$REUSE_DIST/frontend/dist" "$WEB_TRX_DIR/frontend/dist"
else
    echo "Building the frontend (npm ci, npm run build) ..."
    (cd "$WEB_TRX_DIR/frontend" && npm ci --no-audit --no-fund && npm run build)
fi

# --- self-test -------------------------------------------------------------
echo
echo "Self-test ..."
(
    # same library paths as web-trx/scripts/start.sh
    export LD_LIBRARY_PATH="$SCRIPT_DIR/rade_c/build/src:$HOME/.local/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}"
    PYTHONPATH="$(python3 -m site --user-site):${PYTHONPATH:-}"
    export PYTHONPATH
    unset WEB_TRX_PLUTO_TX_PATH
    cd "$WEB_TRX_DIR/backend"
    "$VENV/bin/python" - "$VENV" "$SIM_ONLY" <<'EOF'
import sys
from pathlib import Path
venv = Path(sys.argv[1]).resolve()
sim_only = sys.argv[2] == "1"
try:
    import numpy  # noqa: F401
    import web_trx.server  # noqa: F401
    if not sim_only:
        import gnuradio, iio  # noqa: F401
        import web_trx.radio_backend  # noqa: F401  -- imports pluto_tx/pluto_advanced_rx from this repo
except Exception as e:
    print(f"FAILED: {type(e).__name__}: {e}", file=sys.stderr)
    sys.exit(1)
if sim_only:
    print(f"OK (sim only): numpy {numpy.__version__}, web_trx")
    sys.exit(0)
numpy_path = Path(numpy.__file__).resolve()
if venv in numpy_path.parents:
    print(f"FAILED: numpy {numpy.__version__} was installed into the venv ({numpy_path}); GNU Radio needs the "
          "system numpy. Remove it: .venv/bin/pip uninstall numpy", file=sys.stderr)
    sys.exit(1)
print(f"OK: GNU Radio, libiio, numpy {numpy.__version__} (system), web_trx")
EOF
)

# --- launcher --------------------------------------------------------------
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/web-trx" <<EOF
#!/usr/bin/env bash
# Written by install-web-trx.sh -- Web-TRX in $SCRIPT_DIR/web-trx
DIR="$WEB_TRX_DIR"
cmd="\${1:-status}"
[ \$# -gt 0 ] && shift
case "\$cmd" in
    start)   exec "\$DIR/scripts/start.sh" "\$@" ;;
    stop)    exec "\$DIR/scripts/stop.sh" ;;
    restart)
        rc=0; "\$DIR/scripts/stop.sh" || rc=\$?
        if [ "\$rc" -eq 2 ]; then
            echo "Web-TRX musste hart beendet werden -- kein automatischer Neustart. Safe-State pruefen, dann 'web-trx start'." >&2
            exit 2
        elif [ "\$rc" -ne 0 ]; then
            exit "\$rc"
        fi
        exec "\$DIR/scripts/start.sh" "\$@" ;;
    status)  exec "\$DIR/scripts/status.sh" "\$@" ;;
    open)    url="\$(cd "\$DIR/.." && python3 -m pluto_tx.webtrx_control url)" && exec xdg-open "\$url" ;;
    log)     exec tail -n 100 -F "\$DIR/run/web-trx.log" ;;
    *)       echo "Usage: web-trx start|stop|restart|status [--json]|open|log" >&2; exit 64 ;;
esac
EOF
chmod +x "$HOME/.local/bin/web-trx"
echo "Created $HOME/.local/bin/web-trx"

case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *)
        echo
        echo "NOTE: $HOME/.local/bin is not on your PATH yet (see install.sh)."
        ;;
esac

echo
echo "== Done =="
echo "Start:   web-trx start      (or the Web-TRX row in pluto-tx / pluto-advanced-rx)"
echo "Status:  web-trx status"
echo "Config:  $WEB_TRX_DIR/web-trx.env (optional, see web-trx/README.md)"
