#!/usr/bin/env bash
# Shows the state of the Web-TRX server. Same logic the TX/RX apps use
# (pluto_tx/webtrx_control.py). Options: --json
#
# Exit codes: 0 running, 3 stopped, 4 starting/not responding,
# 5 not installed, 6 running, but not started from this checkout.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
exec python3 -m pluto_tx.webtrx_control status "$@"
