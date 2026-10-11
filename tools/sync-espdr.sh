#!/usr/bin/env bash
# Copies the espdr package (the host side of the ESP32-S3 transmitter, https://github.com/jochenhammes/esp32-sdr-trx, Zero-Clause BSD
# licence) from a checkout of that repository into ./espdr, together with the firmware images the package carries, its licence and its
# NOTICE, and records which commit it came from in espdr/UPSTREAM. pluto-tx ships this copy so that the ESP32-S3 device works on an
# installation that has nothing else of the ESP32 project.
#
#   tools/sync-espdr.sh [--from DIR] [--ref REF]
#
# --from  the esp32-sdr-trx checkout (default: ../esp32/esp32-sdr-trx next to this repository's parent, or $ESPDR_SRC)
# --ref   a branch, tag or commit of that checkout (default: its current HEAD)
#
# The firmware images (rx.bin, tx.bin, bootloader.bin, partition-table.bin) are build products and not in git: they are copied from
# src/espdr/images/ of the checkout (staged by its scripts/stage-images.sh after `make -C firmware images`, or taken from a release) and
# their SHA-256 sums go into espdr/UPSTREAM. The script stops if they are missing; the device in pluto-tx needs tx.bin.
# Never edit espdr/ here: change it in esp32-sdr-trx and run this script again. tests/test_espdr_sync.py checks that the copy and
# UPSTREAM agree with each other.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src="${ESPDR_SRC:-$here/../esp32/esp32-sdr-trx}"
ref=""
while [ $# -gt 0 ]; do
    case "$1" in
        --from) src="$2"; shift 2 ;;
        --ref) ref="$2"; shift 2 ;;
        -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1 (see --help)" >&2; exit 2 ;;
    esac
done

[ -d "$src/.git" ] || { echo "$src is not a git checkout of esp32-sdr-trx (use --from DIR or set ESPDR_SRC)" >&2; exit 1; }
[ -f "$src/src/espdr/txlink.py" ] || { echo "$src does not look like esp32-sdr-trx (src/espdr/txlink.py is missing)" >&2; exit 1; }
ref="${ref:-$(git -C "$src" rev-parse HEAD)}"
commit="$(git -C "$src" rev-parse --verify "$ref^{commit}")"
if [ -n "$(git -C "$src" status --porcelain -- src/espdr LICENSE NOTICE)" ] && [ "$ref" = "$(git -C "$src" rev-parse HEAD)" ]; then
    echo "note: $src has uncommitted changes in src/espdr; only the committed state $commit is copied" >&2
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
git -C "$src" archive "$commit" src/espdr LICENSE NOTICE | tar -x -C "$tmp"

rm -rf "$here/espdr"
mkdir -p "$here/espdr"
cp -a "$tmp/src/espdr/." "$here/espdr/"
cp "$tmp/LICENSE" "$here/espdr/LICENSE"
cp "$tmp/NOTICE" "$here/espdr/NOTICE"
find "$here/espdr" -name __pycache__ -type d -prune -exec rm -rf {} +
images="$src/src/espdr/images"
for f in rx.bin tx.bin bootloader.bin partition-table.bin; do
    [ -f "$images/$f" ] || { echo "$images/$f is missing: stage the images first (scripts/stage-images.sh in esp32-sdr-trx)" >&2; rm -rf "$here/espdr"; exit 1; }
done
mkdir -p "$here/espdr/images"
cp "$images"/rx.bin "$images"/tx.bin "$images"/bootloader.bin "$images"/partition-table.bin "$here/espdr/images/"
( cd "$here/espdr/images" && sha256sum rx.bin tx.bin bootloader.bin partition-table.bin > SHA256SUMS.txt )
{
    echo "repository: https://github.com/jochenhammes/esp32-sdr-trx"
    echo "commit: $commit"
    echo "subject: $(git -C "$src" log -1 --format=%s "$commit")"
    echo "date: $(git -C "$src" log -1 --format=%cI "$commit")"
    echo "licence: 0BSD (espdr/LICENSE); the rest of pluto-tx is GPL-3.0"
    echo "images (build products of the firmware of that repository, copied as they were; sha256 in espdr/images/SHA256SUMS.txt):"
    sed 's/^/  /' "$here/espdr/images/SHA256SUMS.txt"
} > "$here/espdr/UPSTREAM"
echo "espdr copied from $src at $commit"
