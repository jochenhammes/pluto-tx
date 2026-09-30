#!/usr/bin/env bash
# web-trx from the pluto-tx package: start/stop/status of the Web-TRX server
# (the same scripts a git checkout uses, in /usr/share/pluto-tx/web-trx).
# Runtime data: ~/.local/state/web-trx, settings: ~/.config/pluto-tx/web-trx.env.
DIR=/usr/share/pluto-tx/web-trx
PYTHONPATH="/usr/share/pluto-tx:/usr/lib/pluto-tx/python${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
RUN_DIR="${WEB_TRX_RUN_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/web-trx}"
SERVICE_USER="$(id -un)"
UNIT="web-trx@$SERVICE_USER.service"

service() {
    case "${1:-}" in
        enable)
            if [ "$(id -u)" = "0" ]; then
                echo "Als normaler Benutzer ausführen (der Dienst läuft als dieser Benutzer); sudo wird selbst aufgerufen." >&2
                exit 1
            fi
            # The unit needs the real run directory (its PID file); systemd's
            # %h would be root's home for a system unit.
            dropin="/etc/systemd/system/$UNIT.d"
            tmp="$(mktemp)"
            cat >"$tmp" <<UNIT_EOF
# Written by 'web-trx service enable'
[Service]
Environment=HOME=$HOME
Environment=WEB_TRX_RUN_DIR=$RUN_DIR
PIDFile=$RUN_DIR/web-trx.pid
UNIT_EOF
            mkdir -p "$RUN_DIR"
            if ! systemctl is-active --quiet "$UNIT"; then "$DIR/scripts/stop.sh" || true; fi
            echo "Richte $UNIT ein (Start beim Booten, ohne Anmeldung; sudo-Passwort wird ggf. abgefragt) ..."
            sudo install -d "$dropin" && sudo install -m 644 "$tmp" "$dropin/paths.conf" && rm -f "$tmp"
            sudo systemctl daemon-reload
            sudo systemctl enable --now "$UNIT"
            systemctl --no-pager status "$UNIT" | head -5
            ;;
        disable)
            sudo systemctl disable --now "$UNIT"
            sudo rm -rf "/etc/systemd/system/$UNIT.d"
            sudo systemctl daemon-reload
            echo "Dienst $UNIT entfernt."
            ;;
        status|"")
            systemctl --no-pager status "$UNIT" ;;
        *)  echo "Usage: web-trx service enable|disable|status" >&2; exit 64 ;;
    esac
}

cmd="${1:-status}"
[ $# -gt 0 ] && shift
case "$cmd" in
    start)   exec "$DIR/scripts/start.sh" "$@" ;;
    stop)    exec "$DIR/scripts/stop.sh" ;;
    restart)
        rc=0; "$DIR/scripts/stop.sh" || rc=$?
        if [ "$rc" -eq 2 ]; then
            echo "Web-TRX musste hart beendet werden -- kein automatischer Neustart. Safe-State pruefen, dann 'web-trx start'." >&2
            exit 2
        elif [ "$rc" -ne 0 ]; then
            exit "$rc"
        fi
        exec "$DIR/scripts/start.sh" "$@" ;;
    status)  exec "$DIR/scripts/status.sh" "$@" ;;
    open)    url="$(cd /usr/share/pluto-tx && python3 -P -m pluto_tx.webtrx_control url)" && exec xdg-open "$url" ;;
    log)     exec tail -n 100 -F "$RUN_DIR/web-trx.log" ;;
    service) service "$@" ;;
    --version|version) exec python3 -P -c 'from pluto_tx.version import version; print("web-trx", version())' ;;
    *)       echo "Usage: web-trx start|stop|restart|status [--json]|open|log|service enable|disable|--version" >&2; exit 64 ;;
esac
