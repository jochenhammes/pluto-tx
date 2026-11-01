#!/usr/bin/env bash
# Install test for the pluto-tx .deb in a FRESH container of the target
# distribution (no build tools) -- docs/RELEASE_PLAN.md R3 "install-test".
#
# Usage (host): packaging/test-install.sh <target> <path/to/pluto-tx_*.deb>
#   target: ubuntu26.04 | deb13
#
# Inside the container (as root, then as an unprivileged user "tester"):
#   apt install ./pluto-tx_*.deb (must resolve from the distribution alone),
#   --version of all programs, gr-m17/gr-lora_sdr imports, FT8/RADE/JS8 via
#   pluto_tx.paths, js8ref decodes the reference WAV, pluto-tx-doctor, Web-TRX
#   start (sim backend) -> /health with version -> login -> stop with data in
#   ~/.local/state/web-trx and nothing written below /usr, the full pluto-tx
#   unittest suite against the installed code, then remove and purge leave
#   nothing behind.
set -euo pipefail

if [[ "${1:-}" != "--inside" ]]; then
    target="${1:?usage: test-install.sh <ubuntu26.04|deb13> <deb>}"
    deb="$(cd "$(dirname "${2:?deb file}")" && pwd)/$(basename "$2")"
    case "$target" in
        ubuntu26.04) base=ubuntu:26.04 ;;
        deb13) base=debian:trixie ;;
        *) echo "unknown target $target" >&2; exit 64 ;;
    esac
    HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    REPO="$(cd "$HERE/.." && pwd)"
    ENGINE="$(command -v podman || command -v docker)"
    exec "$ENGINE" run --rm -v "$deb:/pkg/$(basename "$deb"):Z,ro" -v "$REPO/tests:/src/tests:Z,ro" \
        -v "$HERE/test-install.sh:/test-install.sh:Z,ro" "docker.io/library/$base" bash /test-install.sh --inside
fi

# ------------------------------------------------------------------ inside
export DEBIAN_FRONTEND=noninteractive
step() { printf '\n==== %s\n' "$*"; }
fail() { echo "FAILED: $*" >&2; exit 1; }

step "apt install ./pluto-tx_*.deb"
apt-get update -qq
# the plain system, no compilers: prove the package resolves from the distribution
! command -v gcc >/dev/null || fail "gcc present in the test container"
apt-get install -y -qq /pkg/pluto-tx_*.deb >/tmp/apt.log 2>&1 || { tail -40 /tmp/apt.log; fail "apt install"; }
tail -3 /tmp/apt.log
dpkg -s pluto-tx | grep -E '^(Version|Installed-Size):'
apt-get install -y -qq curl procps >/dev/null

useradd -m tester
touch /tmp/before-tests
sleep 1

run_as() { su tester -s /bin/bash -c "$*"; }

step "--version / --help"
for p in pluto-tx pluto-advanced-rx pluto-cli web-trx pluto-tx-doctor; do
    out="$(run_as "QT_QPA_PLATFORM=offscreen $p --version" 2>&1)" || fail "$p --version: $out"
    echo "$out" | tail -1
done
run_as "pluto-cli --help" >/dev/null || fail "pluto-cli --help"

step "native components via the launcher's search path"
run_as 'cd /tmp && PYTHONPATH=/usr/share/pluto-tx:/usr/lib/pluto-tx/python python3 -P - <<"EOF"
from gnuradio import m17, lora_sdr
print("gnuradio.m17", m17.m17_coder, "| gnuradio.lora_sdr", lora_sdr.lora_sdr_lora_tx)
from pluto_tx import ft8_ctypes, rade_ctypes, paths, js8, js8_message
from pluto_advanced_rx import ft8_ctypes as rx_ft8, rade_ctypes as rx_rade, js8_decoder
assert ft8_ctypes.FT8_AVAILABLE and rx_ft8.FT8_AVAILABLE, "FT8"
assert rade_ctypes.RADE_AVAILABLE and rx_rade.RADE_AVAILABLE, "RADE"
assert js8.codec_self_test(), "JS8 codec self-test"
assert js8_message.jsc_available(), "JSC word lists"
assert "js8" in js8_decoder.available_backends(), js8_decoder.available_backends()
print("FT8, RADE, JS8 (codec, JSC, js8ref):", paths.program("lpcnet_demo"), paths.program("js8ref"), paths.data_file("jsc.json"))
frames = js8_message.build_frames("DA2JH", "", "@ALLCALL JS8 SELF TEST", js8.FAST)
print("JS8 test message ->", len(frames), "frame(s)")
EOF' || fail "components"
out="$(run_as "/usr/lib/pluto-tx/bin/js8ref decode /src/tests/data/js8/wav/cq_sm0_f0.wav 1" 2>/dev/null)"
grep -q "@ALLCALL CQ CQ CQ" <<<"$out" || fail "js8ref decode: $out"
echo "js8ref decodes the reference WAV"
run_as "/usr/lib/pluto-tx/bin/lpcnet_demo >/dev/null 2>&1; test \$? -ne 127" || fail "lpcnet_demo does not start"
echo "lpcnet_demo starts"

step "pluto-tx-doctor"
run_as "pluto-tx-doctor" || true   # warnings expected in a container (no devices, no NTP)

step "Web-TRX (sim backend) as user tester"
run_as 'mkdir -p ~/.config/pluto-tx && printf "WEB_TRX_BACKEND=sim\nWEB_TRX_TLS=false\nWEB_TRX_HOST=127.0.0.1\nWEB_TRX_PASSWORD=testpw\n" > ~/.config/pluto-tx/web-trx.env'
run_as "web-trx start" || { run_as 'tail -30 ~/.local/state/web-trx/web-trx.log'; fail "web-trx start"; }
health="$(curl -fsS http://127.0.0.1:8321/health)"
echo "/health: $health"
grep -q '"version":"' <<<"$health" || fail "/health without version"
code="$(curl -s -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -d '{"password":"testpw"}' http://127.0.0.1:8321/login)"
[[ "$code" == 200 ]] || fail "login answered $code"
echo "login OK"
code="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8321/)"
[[ "$code" == 200 ]] || fail "frontend answered $code"
echo "frontend OK"
run_as "web-trx status" || fail "web-trx status"
run_as "web-trx stop" || fail "web-trx stop"
run_as 'ls ~/.local/state/web-trx/' | tr '\n' ' '; echo
run_as 'test -f ~/.local/state/web-trx/web-trx.log' || fail "no log in ~/.local/state/web-trx"

step "full pluto-tx test suite against the installed code"
# The tests next to a web-trx/scripts directory, as in a checkout -- here the
# packaged scripts. No sound card in a container: GNU Radio's ALSA default ->
# ALSA's null device, exactly like the headless web-trx@.service.
run_as 'rm -rf /tmp/t && mkdir -p /tmp/t/web-trx && cp -r /src/tests /tmp/t/ && \
    ln -s /usr/share/pluto-tx/web-trx/scripts /tmp/t/web-trx/scripts && cd /tmp/t && \
    PYTHONPATH=/usr/share/pluto-tx:/usr/lib/pluto-tx/python QT_QPA_PLATFORM=offscreen \
    GR_CONF_AUDIO_ALSA_DEFAULT_OUTPUT_DEVICE=null GR_CONF_AUDIO_ALSA_DEFAULT_INPUT_DEVICE=null PYTHONFAULTHANDLER=1 \
    timeout 3000 python3 -P -m unittest discover -v -s tests -t . > /tmp/unittest-full.log 2>&1; \
    grep -E "^(FAIL|ERROR): " /tmp/unittest-full.log; grep -A40 "Fatal Python error" /tmp/unittest-full.log | head -60; tail -n 4 /tmp/unittest-full.log' | tee /tmp/unittest.log
grep -qE '^OK' /tmp/unittest.log || fail "unit tests"

step "nothing written below /usr"
written="$(find /usr -newer /tmp/before-tests -not -type d 2>/dev/null | grep -v -E '^/usr/lib/python3/dist-packages/.*__pycache__' || true)"
[[ -z "$written" ]] || fail "written below /usr: $written"
echo "none"

step "remove, purge"
apt-get remove -y -qq pluto-tx >/dev/null
apt-get purge -y -qq pluto-tx >/dev/null
left="$(find /usr/share/pluto-tx /usr/lib/pluto-tx /usr/bin/pluto-tx /usr/bin/web-trx 2>/dev/null || true)"
[[ -z "$left" ]] || fail "left behind: $left"
echo "clean"

printf '\n==== ALL INSTALL TESTS PASSED\n'
