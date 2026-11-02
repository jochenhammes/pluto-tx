#!/usr/bin/env bash
# WSJT-X from the distribution, for its jt9: the receiver's preferred FT8 decoder, it finds about 1.5 times as many
# signals as ft8_lib. Run through ./install.sh.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
refuse_root

if command -v jt9 >/dev/null 2>&1; then
    echo "jt9 already there: $(command -v jt9)"
    exit 0
fi
apt_need wsjtx
