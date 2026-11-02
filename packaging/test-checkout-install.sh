#!/usr/bin/env bash
# Install test of ./install.sh in a FRESH container: from a checkout, as a normal user with sudo, as on a new machine.
#
# Usage (host): packaging/test-checkout-install.sh <ubuntu26.04|deb13> [features ...]     default: --all
#
# Inside: the features, a second run that finds everything current, the launchers, the Doctor. No hardware in a container:
# devices stay untested here. Node.js is not installed, so Web-TRX must be reported as skipped unless NODE=1.
set -uo pipefail

if [[ "${1:-}" != "--inside" ]]; then
    target="${1:?usage: test-checkout-install.sh <ubuntu26.04|deb13> [features]}"; shift
    case "$target" in
        ubuntu26.04) base=ubuntu:26.04 ;;
        deb13) base=debian:trixie ;;
        *) echo "unknown target $target" >&2; exit 64 ;;
    esac
    HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    REPO="$(cd "$HERE/.." && pwd)"
    ENGINE="$(command -v podman || command -v docker)"
    SRC="$(mktemp -d)"
    trap 'rm -rf "$SRC"' EXIT
    # exactly what git tracks, plus what is staged or changed: no builds of the host
    git -C "$REPO" ls-files -z --cached --others --exclude-standard | (cd "$REPO" && tar --null -T - --ignore-failed-read -cf - 2>/dev/null) | tar -xf - -C "$SRC"
    exec "$ENGINE" run --rm -e "NODE=${NODE:-0}" -v "$SRC:/src:Z,ro" "docker.io/library/$base" bash /src/packaging/test-checkout-install.sh --inside "$@"
fi
shift
export DEBIAN_FRONTEND=noninteractive
step() { printf '\n==== %s\n' "$*"; }
fail() { echo "NICHT BESTANDEN: $*"; exit 1; }

step "Normal user with sudo, repository copy"
apt-get update -qq && apt-get install -y -qq sudo git python3 >/dev/null || fail "apt"
useradd -m tester
echo 'tester ALL=(ALL) NOPASSWD:ALL' >/etc/sudoers.d/tester
cp -a /src /home/tester/pluto-tx
chown -R tester: /home/tester/pluto-tx
if [ "${NODE:-0}" = 1 ]; then apt-get install -y -qq nodejs npm >/dev/null; fi
as_user() { su - tester -c "cd ~/pluto-tx && $*"; }

features="${*:---all}"
step "./install.sh $features --yes"
started=$SECONDS
as_user "./install.sh $features --yes" 2>&1 | tail -60
rc=${PIPESTATUS[0]}
echo "first run: exit $rc, $((SECONDS - started)) s"
[ "$rc" = 0 ] || { as_user "tail -n 30 ~/.cache/pluto-tx/install/*.log"; fail "first run"; }

step "Second run: nothing to do"
started=$SECONDS
out="$(as_user "./install.sh $features --yes" 2>&1)"
rc=$?
echo "$out" | sed -n '/=== Ergebnis/,/^$/p'
echo "second run: exit $rc, $((SECONDS - started)) s"
[ "$rc" = 0 ] || fail "second run"
if echo "$out" | sed -n '/=== Ergebnis/,/^$/p' | grep -q installiert; then fail "second run installed something again"; fi

step "Launchers run"
as_user 'export PATH=$HOME/.local/bin:$PATH; QT_QPA_PLATFORM=offscreen pluto-cli --version' || fail "pluto-cli"
as_user 'cat ~/.local/bin/pluto-tx'

step "Doctor"
as_user 'export PATH=$HOME/.local/bin:$PATH; eval "$(grep -hE "^export" ~/.local/bin/pluto-cli)"; python3 -m pluto_tx.doctor' || true

step "Bestanden"
