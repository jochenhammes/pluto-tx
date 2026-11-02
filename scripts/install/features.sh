# shellcheck shell=bash disable=SC2034
# What ./install.sh knows about each feature. Sourced after lib.sh.
# Installation order; base always comes first.
FEATURES=(base m17 lora rade ft8 wsjtx jtty js8 js8call espdr web-trx)
# Not part of --all: a separate application nobody needs for pluto-tx itself.
NOT_IN_ALL=(js8call)

feature_label() {
    case "$1" in
        base)    echo "Grundinstallation: Apps, FM, SSB, Digimodes, Pluto, HackRF, RTL-SDR, X6200, AIOC" ;;
        m17)     echo "M17 Digitalsprache (gr-m17)" ;;
        lora)    echo "LoRa: Meshtastic und MeshCore (gr-lora_sdr)" ;;
        rade)    echo "RADE Digitalsprache (rade_c)" ;;
        ft8)     echo "FT8 (ft8_lib)" ;;
        wsjtx)   echo "WSJT-X als besserer FT8-Decoder (jt9)" ;;
        jtty)    echo "JTTY (wsjt-codec)" ;;
        js8)     echo "JS8 (Decoder aus JS8Call)" ;;
        js8call) echo "die Anwendung JS8Call selbst (fuer pluto-tx nicht noetig)" ;;
        espdr)   echo "ESP32-S3 als Sender (esptool, udev-Regel)" ;;
        web-trx) echo "Web-TRX, die Browser-Oberflaeche (braucht Node.js 18 oder neuer)" ;;
        *)       return 1 ;;
    esac
}

# Measured from scratch in a container on 8 cores, Ubuntu 26.04 and Debian 13.
feature_cost() {
    case "$1" in
        base)    echo "1-2 Min" ;;
        lora|rade) echo "1 Min" ;;
        m17|js8|espdr|web-trx) echo "unter 1 Min" ;;
        *)       echo "Sekunden" ;;
    esac
}

feature_installed() {
    case "$1" in
        base)    dpkg-query -W -f='${db:Status-Status}' gnuradio 2>/dev/null | grep -qx installed && [ -x "$HOME/.local/bin/pluto-tx" ] ;;
        m17)     [ -n "$(local_lib_dir 'libgnuradio-m17.so*')" ] ;;
        lora)    [ -n "$(local_lib_dir 'libgnuradio-lora_sdr.so*')" ] ;;
        rade)    [ -f "$REPO_DIR/rade_c/build/src/librade.so" ] ;;
        ft8)     [ -f "$REPO_DIR/ft8_lib/libft8wrap.so" ] ;;
        wsjtx)   command -v jt9 >/dev/null 2>&1 ;;
        jtty)    [ -x "$REPO_DIR/wsjt-codec/bin/wsjt-codec" ] ;;
        js8)     [ -x "$REPO_DIR/js8call/build-js8ref/js8ref" ] ;;
        js8call) command -v js8call >/dev/null 2>&1 ;;
        espdr)   command -v esptool >/dev/null 2>&1 ;;
        web-trx) [ -f "$REPO_DIR/web-trx/frontend/dist/index.html" ] && [ -x "$REPO_DIR/web-trx/backend/.venv/bin/python" ] ;;
    esac
}

# The checkout and the pinned commit of a source build: a matching commit counts as current without a stamp, as on
# an installation from before these scripts.
feature_checkout() {
    case "$1" in
        m17)  echo "gr-m17 GR_M17_COMMIT" ;;
        lora) echo "gr-lora_sdr GR_LORA_SDR_COMMIT" ;;
        rade) echo "rade_c RADE_C_COMMIT" ;;
        ft8)  echo "ft8_lib FT8_LIB_COMMIT" ;;
        js8)  echo "js8call JS8_COMMIT" ;;
    esac
}

# Changes whenever the feature has to be installed again.
feature_fingerprint() {
    local id="$1"
    {
        cat "$INSTALL_DIR/$id.sh"
        case "$id" in
            base)    cat "$INSTALL_DIR/packages-base.txt" ;;
            m17)     lock_get GR_M17_COMMIT ;;
            lora)    lock_get GR_LORA_SDR_COMMIT ;;
            rade)    lock_get RADE_C_COMMIT ;;
            ft8)     lock_get FT8_LIB_COMMIT ;;
            jtty)    lock_get WSJT_CODEC_TAG; lock_get WSJT_CODEC_SHA256_X86_64 ;;
            js8)     lock_get JS8_COMMIT ;;
            web-trx) cat "$REPO_DIR/web-trx/backend/pyproject.toml" "$REPO_DIR/web-trx/frontend/package-lock.json"
                     find "$REPO_DIR/web-trx/frontend/src" -type f -print0 | sort -z | xargs -0 cat ;;
        esac
    } | sha256sum | cut -d' ' -f1
}

stamp_write() {
    mkdir -p "$STATE_DIR"
    feature_fingerprint "$1" >"$STATE_DIR/$1"
}

# aktuell | veraltet | fehlt
feature_status() {
    local id="$1" checkout dir key
    if ! feature_installed "$id"; then
        echo fehlt
        return
    fi
    if [ -f "$STATE_DIR/$id" ]; then
        if [ "$(cat "$STATE_DIR/$id")" = "$(feature_fingerprint "$id")" ]; then echo aktuell; else echo veraltet; fi
        return
    fi
    checkout="$(feature_checkout "$id")"
    if [ -n "$checkout" ]; then
        dir="${checkout% *}"; key="${checkout#* }"
        if [ "$(git -C "$REPO_DIR/$dir" rev-parse HEAD 2>/dev/null)" = "$(lock_get "$key")" ]; then
            echo aktuell
        else
            echo veraltet
        fi
        return
    fi
    case "$id" in
        wsjtx|js8call|espdr) echo aktuell ;;   # a program from elsewhere, nothing pinned
        *) echo veraltet ;;
    esac
}
