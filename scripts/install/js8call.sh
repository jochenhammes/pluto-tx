#!/usr/bin/env bash
# The JS8Call application from the distribution, as a counterpart to compare with. pluto-tx itself does not need it.
# Run through ./install.sh.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
refuse_root

if command -v js8call >/dev/null 2>&1; then
    echo "JS8Call already there: $(command -v js8call)"
    exit 0
fi
apt_need js8call
