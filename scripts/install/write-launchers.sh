#!/usr/bin/env bash
# Writes ~/.local/bin/pluto-tx, pluto-advanced-rx and pluto-cli from what is installed right now, so no feature has to
# edit them. Runs after every ./install.sh.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

libs=() bins=()
for pattern in 'libgnuradio-m17.so*' 'libgnuradio-lora_sdr.so*'; do
    dir="$(local_lib_dir "$pattern")"
    [ -n "$dir" ] || continue
    case " ${libs[*]:-} " in *" $dir "*) ;; *) libs+=("$dir") ;; esac
done
if [ -f "$REPO_DIR/rade_c/build/src/librade.so" ]; then
    libs=("$REPO_DIR/rade_c/build/src" ${libs[@]+"${libs[@]}"})
    bins+=("$REPO_DIR/rade_c/build/src")
fi

write_launcher() {
    local launcher="$HOME/.local/bin/$1" module="$2" new
    new="$(
        echo '#!/usr/bin/env bash'
        if [ ${#libs[@]} -gt 0 ]; then
            echo "export LD_LIBRARY_PATH=\"$(IFS=:; echo "${libs[*]}"):\${LD_LIBRARY_PATH:-}\""
        fi
        if [ ${#bins[@]} -gt 0 ]; then
            echo "export PATH=\"$(IFS=:; echo "${bins[*]}"):\${PATH:-}\""
        fi
        echo "cd \"$REPO_DIR\" && exec python3 -m $module \"\$@\""
    )"
    # The first time only: older installers let a launcher collect entries this one may not know.
    if [ -f "$launcher" ] && [ ! -f "$launcher.bak" ] && [ "$(cat "$launcher")" != "$new" ]; then
        cp -p "$launcher" "$launcher.bak"
    fi
    printf '%s\n' "$new" >"$launcher"
    chmod +x "$launcher"
}

mkdir -p "$HOME/.local/bin"
write_launcher pluto-tx "pluto_tx.app --gui"
write_launcher pluto-advanced-rx pluto_advanced_rx.app
write_launcher pluto-cli pluto_cli.app
