#!/usr/bin/env bash
# Installs pluto-tx, pluto-advanced-rx, pluto-cli and Web-TRX from this checkout, on Debian or Ubuntu.
# (Normal operation is easier with the .deb package: see README.)
#
#   ./install.sh                 menu: what is there, how long each feature takes
#   ./install.sh --all           everything
#   ./install.sh m17 rade        only these (the base comes along)
#   ./install.sh --list          what exists and what is installed; changes nothing
#   ./install.sh --dry-run ...   what would happen; changes nothing
#   ./install.sh --force ...     also what is already current
#   ./install.sh --yes ...       no questions; without features: the base
#   ./install.sh web-trx -- --reuse-dist DIR
#                                options after -- go to the one feature named
#
# Same command to update: only what changed is built again. A failed feature does not stop the others.
# Not with sudo: it calls sudo itself where needed.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/scripts/install/lib.sh"
. "$INSTALL_DIR/features.sh"

usage() { sed -n '2,/^# Not with sudo/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

in_list() {
    local needle="$1" item; shift
    for item in "$@"; do [ "$item" = "$needle" ] && return 0; done
    return 1
}

requested=() extra=()
all=0 dry=0 force=0 yes=0 list=0
while [ $# -gt 0 ]; do
    case "$1" in
        --all) all=1 ;;
        --list) list=1 ;;
        --dry-run) dry=1 ;;
        --force) force=1 ;;
        --yes|-y) yes=1 ;;
        -h|--help) usage; exit 0 ;;
        --) shift; extra=("$@"); break ;;
        -*) echo "Unbekannte Option: $1 (siehe ./install.sh --help)" >&2; exit 64 ;;
        *)
            if ! in_list "$1" "${FEATURES[@]}"; then
                echo "Unbekanntes Feature: $1. Vorhanden: ${FEATURES[*]}" >&2
                exit 64
            fi
            requested+=("$1") ;;
    esac
    shift
done
if [ ${#extra[@]} -gt 0 ] && [ ${#requested[@]} -ne 1 ]; then
    echo "Optionen nach -- gelten fuer genau ein Feature." >&2
    exit 64
fi

refuse_root

show_status() {
    local id n=1
    for id in "${FEATURES[@]}"; do
        printf '  %-8s %-9s %-12s %s\n' "$id" "$(feature_status "$id")" "$(feature_cost "$id")" "$(feature_label "$id")"
        n=$((n + 1))
    done
}

if [ "$list" = 1 ]; then
    show_status
    exit 0
fi

menu() {
    local -a selected=()
    local id n answer word status
    for id in "${FEATURES[@]}"; do
        status="$(feature_status "$id")"
        if [ "$id" = base ] || [ "$status" = veraltet ]; then selected+=(1); else selected+=(0); fi
    done
    while true; do
        echo
        echo "Was soll installiert werden?  ([x] = wird installiert)"
        n=1
        for id in "${FEATURES[@]}"; do
            printf ' [%s] %2d %-8s %-9s %-12s %s\n' "$([ "${selected[$((n - 1))]}" = 1 ] && echo x || echo ' ')" \
                "$n" "$id" "$(feature_status "$id")" "$(feature_cost "$id")" "$(feature_label "$id")"
            n=$((n + 1))
        done
        echo
        read -r -p "Nummern umschalten, a = alles, Enter = los, q = abbrechen: " answer || answer=q
        case "$answer" in
            '') break ;;
            q) echo "Abgebrochen."; exit 0 ;;
            a) n=0
               for id in "${FEATURES[@]}"; do
                   in_list "$id" "${NOT_IN_ALL[@]}" || selected[n]=1
                   n=$((n + 1))
               done ;;
            *) for word in $answer; do
                   if [[ "$word" =~ ^[0-9]+$ ]] && [ "$word" -ge 1 ] && [ "$word" -le ${#FEATURES[@]} ]; then
                       n=$((word - 1))
                       selected[n]=$((1 - selected[n]))
                   else
                       echo "Keine Nummer aus der Liste: $word"
                   fi
               done ;;
        esac
    done
    n=0
    for id in "${FEATURES[@]}"; do
        [ "${selected[$n]}" = 1 ] && requested+=("$id")
        n=$((n + 1))
    done
}

if [ "$all" = 1 ]; then
    for id in "${FEATURES[@]}"; do
        in_list "$id" "${NOT_IN_ALL[@]}" || requested+=("$id")
    done
elif [ ${#requested[@]} -eq 0 ]; then
    if [ "$yes" = 0 ] && { [ -t 0 ] && [ -t 1 ] || [ -n "${PLUTO_INSTALL_FORCE_MENU:-}" ]; }; then
        menu
    fi
fi

# Installation order, with the base first, whatever the order of the arguments.
plan=(base)
for id in "${FEATURES[@]}"; do
    if [ "$id" != base ] && [ ${#requested[@]} -gt 0 ] && in_list "$id" "${requested[@]}"; then plan+=("$id"); fi
done

todo=()
for id in "${plan[@]}"; do
    if [ "$force" = 1 ] || [ "$(feature_status "$id")" != aktuell ]; then todo+=("$id"); fi
done

if [ "$dry" = 1 ]; then
    echo "Es wuerde installiert (nichts wird geaendert):"
    for id in "${plan[@]}"; do
        if [ ${#todo[@]} -gt 0 ] && in_list "$id" "${todo[@]}"; then
            printf '  %-8s %s (%s)\n' "$id" "$(feature_label "$id")" "$(feature_cost "$id")"
        else
            printf '  %-8s aktuell, nichts zu tun\n' "$id"
        fi
    done
    echo "Danach: Starter in ~/.local/bin schreiben, Doctor."
    exit 0
fi

mkdir -p "$LOG_DIR"
PLUTO_INSTALL_NOTICES="$(mktemp)"
export PLUTO_INSTALL_NOTICES
sudo_keeper=""
cleanup() {
    [ -n "$sudo_keeper" ] && kill "$sudo_keeper" 2>/dev/null
    rm -f "$PLUTO_INSTALL_NOTICES" "$PLUTO_INSTALL_NOTICES.apt-updated"
}
trap cleanup EXIT

# Ask once, up front: a build must not stop half an hour in, waiting for a password.
if [ ${#todo[@]} -gt 0 ] && command -v sudo >/dev/null 2>&1 && [ -z "${PLUTO_INSTALL_NO_SUDO:-}" ]; then
    echo "Fuer apt-get und Systemdateien kann das Passwort noetig sein."
    if sudo -v; then
        ( while kill -0 $$ 2>/dev/null; do sudo -n true 2>/dev/null; sleep 60; done ) &
        sudo_keeper=$!
    fi
fi

declare -A result
for id in "${plan[@]}"; do
    result[$id]=aktuell
done

failed=0
for id in ${todo[@]+"${todo[@]}"}; do
    log="$LOG_DIR/$id.log"
    echo
    echo "=== $id: $(feature_label "$id") ==="
    started=$SECONDS
    (
        export PLUTO_INSTALL_YES="$yes"
        bash "$INSTALL_DIR/$id.sh" ${extra[@]+"${extra[@]}"}
    ) 2>&1 | tee "$log"
    rc=${PIPESTATUS[0]}
    case "$rc" in
        0) stamp_write "$id"; result[$id]="installiert ($((SECONDS - started)) s)" ;;
        "$EXIT_SKIPPED") result[$id]=uebersprungen ;;
        *) result[$id]="FEHLER, siehe $log"; failed=1 ;;
    esac
    if [ "$id" = base ] && [ "$rc" != 0 ]; then
        echo "Ohne die Grundinstallation lohnen die uebrigen Features nicht: Abbruch." >&2
        for later in "${plan[@]}"; do
            [ "$later" = base ] || result[$later]="nicht versucht"
        done
        break
    fi
done

launchers_ok=1
bash "$INSTALL_DIR/write-launchers.sh" || launchers_ok=0

echo
echo "=== Ergebnis ==="
for id in "${plan[@]}"; do
    printf '  %-8s %s\n' "$id" "${result[$id]}"
done
if [ "$launchers_ok" = 0 ]; then
    echo "  Starter in ~/.local/bin: FEHLER"
    failed=1
fi
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) notice "$HOME/.local/bin fehlt im PATH. In die .bashrc: export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac
if [ -s "$PLUTO_INSTALL_NOTICES" ]; then
    echo
    echo "=== Hinweise ==="
    sed 's/^/  /' "$PLUTO_INSTALL_NOTICES"
fi

if [ -z "${PLUTO_INSTALL_NO_DOCTOR:-}" ] && python3 -c 'import sys' 2>/dev/null; then
    echo
    echo "=== Doctor ==="
    eval "$(grep -hE '^export (LD_LIBRARY_PATH|PATH)=' "$HOME/.local/bin/pluto-cli" 2>/dev/null)"
    (cd "$REPO_DIR" && PATH="$HOME/.local/bin:$PATH" python3 -m pluto_tx.doctor) || true
fi

echo
echo "Starten: pluto-tx, pluto-advanced-rx, pluto-cli$(in_list web-trx "${plan[@]}" && echo ', web-trx start')"
exit "$failed"
