# shellcheck shell=bash disable=SC2034
# Shared by install.sh and the feature scripts next to this file. Sourced, not run.
# Every feature script: safe to re-run, normal user, sudo only for apt and system files.

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$INSTALL_DIR/../.." && pwd)"
LOCK="$REPO_DIR/packaging/components.lock"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/pluto-tx/install"
LOG_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/pluto-tx/install"

# Exit code of a feature script for "cannot run here, and that is not an error" (Web-TRX without Node.js).
EXIT_SKIPPED=3

refuse_root() {
    # Running this with sudo sets $HOME=/root, which sends the launchers to /root/.local/bin.
    if [ "$(id -u)" = "0" ]; then
        echo "FEHLER: Nicht mit sudo oder als root ausfuehren: ./install.sh ruft sudo selbst auf, wo es noetig ist." >&2
        exit 1
    fi
    # Some container/CI setups keep HOME=/root even for a normal user.
    if [ -n "${SUDO_USER:-}" ]; then
        HOME="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
        export HOME
    fi
}

require_apt() {
    if ! command -v apt-get >/dev/null 2>&1; then
        echo "FEHLER: Nur fuer Debian/Ubuntu (apt-get fehlt). Was gebraucht wird, steht in scripts/install/." >&2
        exit 1
    fi
}

# A note for the end of the run, after all the build output.
notice() {
    if [ -n "${PLUTO_INSTALL_NOTICES:-}" ]; then
        printf '%s\n' "$*" >>"$PLUTO_INSTALL_NOTICES"
    else
        echo "HINWEIS: $*"
    fi
}

apt_update_once() {
    local marker="${PLUTO_INSTALL_NOTICES:-/nonexistent}.apt-updated"
    [ -f "$marker" ] && return 0
    sudo apt-get update
    if [ -n "${PLUTO_INSTALL_NOTICES:-}" ]; then : >"$marker"; fi
}

# Installs what dpkg does not have yet; no sudo at all if everything is there.
apt_need() {
    local missing=() pkg
    for pkg in "$@"; do
        dpkg-query -W -f='${db:Status-Status}' "$pkg" 2>/dev/null | grep -qx installed || missing+=("$pkg")
    done
    [ ${#missing[@]} -eq 0 ] && return 0
    require_apt
    echo "Installing: ${missing[*]}"
    export NEEDRESTART_MODE=a
    apt_update_once
    sudo apt-get install -y "${missing[@]}"
}

# True if the distribution offers the package.
apt_has() {
    apt-cache show "$1" >/dev/null 2>&1
}

lock_get() {
    local value
    value="$(sed -n "s/^$1=//p" "$LOCK")"
    if [ -z "$value" ]; then
        echo "FEHLER: $1 fehlt in $LOCK" >&2
        exit 1
    fi
    printf '%s' "$value"
}

# git_at_commit <dir> <repo> <commit> [--recursive]: clone if needed, then exactly that commit.
git_at_commit() {
    local dir="$1" repo="$2" commit="$3" recursive="${4:-}"
    if [ ! -d "$dir/.git" ]; then
        echo "Cloning $repo ..."
        git clone ${recursive:+--recursive} "$repo" "$dir"
    fi
    if ! git -C "$dir" cat-file -e "$commit^{commit}" 2>/dev/null; then
        git -C "$dir" fetch --tags origin
    fi
    git -C "$dir" checkout -q "$commit"
    if [ -n "$recursive" ]; then
        git -C "$dir" submodule update --init --recursive
    fi
}

multiarch() {
    dpkg-architecture -qDEB_HOST_MULTIARCH 2>/dev/null || gcc -print-multiarch 2>/dev/null || echo "$(uname -m)-linux-gnu"
}

# The directory under ~/.local holding a GNU Radio module's library, or nothing.
local_lib_dir() {
    { find "$HOME/.local/lib" -iname "$1" -printf '%h\n' 2>/dev/null || true; } | head -1
}
